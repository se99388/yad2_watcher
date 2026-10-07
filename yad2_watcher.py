#!/usr/bin/env python3
"""
Yad2 car watcher - checks a saved Yad2 search and sends only NEW listings to Telegram.

Usage:
  python yad2_watcher.py            # single run (use with cron / K8s CronJob)
  python yad2_watcher.py --loop     # run forever, every CHECK_INTERVAL_MIN minutes
  python yad2_watcher.py --dry-run  # print new listings instead of sending
  python yad2_watcher.py --get-chat-id   # find your Telegram chat id
  python yad2_watcher.py --test          # send a test message

Config is read from environment variables (or a .env file next to the script).
"""
import argparse
import html as html_lib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("yad2")

ITEM_URL = "https://www.yad2.co.il/vehicles/item/{token}"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.yad2.co.il/vehicles/cars",
}


class BlockedError(Exception):
    """Yad2 returned a captcha / bot-protection page."""


# ---------------------------------------------------------------- config
def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def cfg(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        sys.exit(f"Missing required env var: {name}")
    return val


# ---------------------------------------------------------------- fetching
def with_page(url: str, page: int) -> str:
    if page == 1:
        return url
    url = re.sub(r"([?&])page=\d+&?", r"\1", url).rstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}page={page}"


def fetch_html(session: requests.Session, url: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        resp = session.get(url, headers=HEADERS, timeout=30)
        text = resp.text
        if resp.status_code == 200 and "__NEXT_DATA__" in text:
            return text
        low = text.lower()
        blocked = resp.status_code in (403, 429) or "captcha" in low or "shieldsquare" in low
        log.warning("Fetch attempt %d failed (status=%s, blocked=%s, len=%d)",
                    attempt + 1, resp.status_code, blocked, len(text))
        if os.environ.get("DEBUG_DIR"):
            d = Path(os.environ["DEBUG_DIR"]); d.mkdir(parents=True, exist_ok=True)
            (d / "last_response.html").write_text(text[:300000], encoding="utf-8")
            (d / "last_response.txt").write_text(
                f"url={url}\nstatus={resp.status_code}\nheaders={dict(resp.headers)}\n",
                encoding="utf-8")
        if attempt < retries:
            time.sleep(15 * (attempt + 1) + random.uniform(0, 10))
    raise BlockedError(f"Could not get listings from {url} (likely bot protection)")


# ---------------------------------------------------------------- browser fetch
_pw = None
_ctx = None


def _browser_ctx():
    """Lazily start one headless Chromium (passes Radware's JS challenge)."""
    global _pw, _ctx
    if _ctx is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        browser = _pw.chromium.launch(
            headless=True, channel=os.environ.get("BROWSER_CHANNEL") or None,
            args=["--disable-blink-features=AutomationControlled"])
        _ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"], locale="he-IL",
            timezone_id="Asia/Jerusalem", viewport={"width": 1366, "height": 900})
        _ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return _ctx


def fetch_html_browser(url: str, retries: int = 1) -> str:
    """Load url in Chromium; on a captcha, restart the browser and retry."""
    global _pw, _ctx
    for attempt in range(retries + 1):
        try:
            return _fetch_html_browser_once(url)
        except BlockedError:
            if attempt >= retries:
                raise
            log.warning("Captcha on attempt %d, restarting browser and retrying", attempt + 1)
            try:
                _ctx.browser.close(); _pw.stop()
            except Exception:
                pass
            _pw = _ctx = None
            time.sleep(random.uniform(20, 40))
    raise BlockedError(url)


def _fetch_html_browser_once(url: str) -> str:
    page = _browser_ctx().new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=45000)
        except Exception:
            pass
        html = page.content()
    finally:
        page.close()
    if "__NEXT_DATA__" not in html:
        if os.environ.get("DEBUG_DIR"):
            d = Path(os.environ["DEBUG_DIR"]); d.mkdir(parents=True, exist_ok=True)
            (d / "last_response.html").write_text(html[:300000], encoding="utf-8")
            (d / "last_response.txt").write_text(f"url={url}\nmode=browser\n", encoding="utf-8")
        raise BlockedError(f"Browser could not get listings from {url} (bot protection)")
    return html


# ---------------------------------------------------------------- parsing
def _get(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def extract_items(page_html: str) -> dict[str, dict]:
    """Find every listing object (has 'token' + car fields) inside __NEXT_DATA__."""
    m = NEXT_DATA_RE.search(page_html)
    if not m:
        return {}
    data = json.loads(m.group(1))
    found: dict[str, dict] = {}

    def walk(o):
        if isinstance(o, dict):
            tok = o.get("token")
            if isinstance(tok, str) and ("price" in o or "manufacturer" in o):
                found[tok] = o
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


def normalize(token: str, it: dict) -> dict:
    title = " ".join(
        x for x in (
            _get(it, "manufacturer", "text"),
            _get(it, "model", "text"),
            _get(it, "subModel", "text"),
        ) if x
    ) or "רכב"
    price = it.get("price")
    return {
        "token": token,
        "title": title,
        "year": _get(it, "vehicleDates", "yearOfProduction"),
        "km": it.get("km"),
        "hand": _get(it, "hand", "id") or it.get("hand"),
        "price": f"₪{price:,}" if isinstance(price, (int, float)) else "לא צוין",
        "area": _get(it, "address", "area", "text") or _get(it, "address", "city", "text"),
        "image": _get(it, "metaData", "coverImage"),
        "url": ITEM_URL.format(token=token),
    }


def search(session: requests.Session, url: str, max_pages: int) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        page_url = with_page(url, page)
        try:
            html = (fetch_html_browser(page_url) if os.environ.get("USE_BROWSER") == "1"
                    else fetch_html(session, page_url))
        except BlockedError:
            if page == 1:
                raise
            log.warning("Page %d blocked; using the %d listings already found", page, len(results))
            break
        items = extract_items(html)
        log.info("Page %d: %d listings", page, len(items))
        new_on_page = {k: v for k, v in items.items() if k not in results}
        if not new_on_page:
            break
        results.update(new_on_page)
        time.sleep(random.uniform(3, 7))  # be polite
    return {t: normalize(t, it) for t, it in results.items()}


# ---------------------------------------------------------------- state
def load_state(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, str], keep_days: int = 60) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- telegram
def format_listing(c: dict) -> str:
    details = " · ".join(str(x) for x in (
        c["year"],
        f'{c["km"]:,} ק"מ' if isinstance(c["km"], int) else None,
        f'יד {c["hand"]}' if c["hand"] else None,
        c["area"],
    ) if x)
    return f"🚗 <b>{html_lib.escape(c['title'])}</b>\n💰 {c['price']}\n{html_lib.escape(details)}\n{c['url']}"


def tg_api(method: str, **params) -> dict:
    resp = requests.post(
        f"https://api.telegram.org/bot{cfg('TG_BOT_TOKEN')}/{method}",
        json=params, timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data


def send_message(text: str) -> None:
    tg_api("sendMessage", chat_id=cfg("TG_CHAT_ID"), text=text,
           parse_mode="HTML", disable_web_page_preview=False)


def send_listings(listings: list[dict]) -> None:
    send_message(f"<b>{len(listings)} מודעות חדשות ביד2</b>")
    for c in listings:  # one message per car -> link preview with photo
        send_message(format_listing(c))
        time.sleep(1)


def print_chat_id() -> None:
    """Helper: send any message to your bot first, then run --get-chat-id."""
    updates = tg_api("getUpdates").get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send 'hi', then retry.")
    for cid, name in chats.items():
        print(f"TG_CHAT_ID={cid}   ({name})")


# ---------------------------------------------------------------- main
def run_once(dry_run: bool) -> None:
    urls = [u for u in re.split(r"[\s|]+", cfg("YAD2_SEARCH_URLS")) if u]  # separate searches with | or space
    state_path = Path(cfg("STATE_FILE", "seen.json"))
    max_pages = int(cfg("MAX_PAGES", "3"))

    session = requests.Session()
    current: dict[str, dict] = {}
    for url in urls:
        current.update(search(session, url, max_pages))
    log.info("Found %d listings in total", len(current))

    state = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat()

    if state is None:  # first run: remember everything, just confirm it works
        if not dry_run:
            send_message(f"✅ מעקב יד2 פעיל. {len(current)} מודעות קיימות נשמרו - "
                          f"תקבל הודעה רק על מודעות חדשות.")
        save_state(state_path, {t: now for t in current})
        log.info("First run - seeded %d listings", len(current))
        return

    new = [c for t, c in current.items() if t not in state]
    if not new:
        log.info("No new listings")
        return

    if dry_run:
        for c in new:
            print(f"{c['title']} | {c['year']} | {c['price']} | {c['url']}")
    else:
        send_listings(new)
        log.info("Sent %d new listings to Telegram", len(new))

    # only mark as seen after the message went out, so failures retry next hour
    state.update({c["token"]: now for c in new})
    save_state(state_path, state)


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--get-chat-id", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()

    if args.get_chat_id:
        print_chat_id()
        return
    if args.test:
        send_message("✅ Yad2 watcher can reach you on Telegram!")
        print("Test message sent.")
        return

    if not args.loop:
        streak_path = Path(cfg("BLOCK_STREAK_FILE", "block_streak.txt"))
        alert_after = int(cfg("BLOCK_ALERT_AFTER", "6"))
        try:
            run_once(args.dry_run)
        except BlockedError as e:
            # A blocked check is skipped, not failed: the next run catches up,
            # because new listings are compared against seen.json.
            streak = (int(streak_path.read_text()) if streak_path.exists() else 0) + 1
            streak_path.write_text(str(streak))
            log.warning("%s - skipped (blocked %d run(s) in a row)", e, streak)
            if streak == alert_after:
                try:
                    send_message(f"⚠️ יד2 חוסם את הבדיקה כבר {streak} פעמים ברצף. "
                                 "ייתכן שצריך לבדוק את המעקב.")
                except Exception:
                    log.exception("Could not send block alert")
            return
        if streak_path.exists():
            streak_path.unlink()
        return

    interval = int(cfg("CHECK_INTERVAL_MIN", "60")) * 60
    while True:
        try:
            run_once(args.dry_run)
        except Exception:
            log.exception("Run failed, will retry next cycle")
        time.sleep(interval + random.uniform(-120, 120))  # jitter


if __name__ == "__main__":
    main()
