#!/usr/bin/env python3
"""
Yad2 car watcher - checks a Yad2 search and sends only NEW listings to Telegram.

Usage:
  python yad2_watcher.py            # one check (GitHub Actions runs this)
  python yad2_watcher.py --dry-run  # print new listings instead of sending
  python yad2_watcher.py --test     # send a Telegram test message

Config comes from environment variables (or a .env file next to the script):
  YAD2_SEARCH_URLS   yad2 search URL(s), several separated by | or spaces
  TG_BOT_TOKEN       Telegram bot token
  TG_CHAT_ID         Telegram chat id
  STATE_FILE         listings already seen          (default seen.json)
  STREAK_FILE        consecutive problem counter    (default problem_streak.json)
  ALERT_AFTER        problems in a row before alert (default 6)
  MAX_PAGES          result pages to read           (default 1)
  BROWSER_CHANNEL    e.g. "chrome" to use an installed Google Chrome
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
    r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
KEEP_DAYS = 60


class CheckProblem(Exception):
    """The check could not be completed; skip this run and count it."""


class BlockedError(CheckProblem):
    """Yad2 returned a captcha / bot-protection page."""


class EmptyResultError(CheckProblem):
    """Page loaded but no listings were found - likely a yad2 site change."""


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


# ---------------------------------------------------------------- browser
class Browser:
    """One headless Chrome for the whole run (passes Radware's JS check)."""

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None

    def _context(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            browser = self._pw.chromium.launch(
                headless=True,
                channel=os.environ.get("BROWSER_CHANNEL") or None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._ctx = browser.new_context(
                user_agent=USER_AGENT, locale="he-IL", timezone_id="Asia/Jerusalem",
                viewport={"width": 1366, "height": 900},
            )
            self._ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        return self._ctx

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._ctx = None

    def _get_once(self, url: str) -> str:
        page = self._context().new_page()
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
            raise BlockedError(f"Yad2 bot protection blocked {url}")
        return html

    def get(self, url: str, retries: int = 1) -> str:
        """Load url; on a captcha, restart the browser once and retry."""
        for attempt in range(retries + 1):
            try:
                return self._get_once(url)
            except BlockedError:
                if attempt >= retries:
                    raise
                log.warning("Captcha on attempt %d, restarting browser and retrying", attempt + 1)
                self.close()
                time.sleep(random.uniform(20, 40))
        raise BlockedError(url)


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


def format_published(created_at) -> str | None:
    """yad2 'createdAt' is Israel local time, e.g. '2026-10-07T17:36:31'."""
    if not isinstance(created_at, str):
        return None
    try:
        dt = datetime.fromisoformat(created_at[:19])
    except ValueError:
        return None
    return dt.strftime("%d/%m/%Y %H:%M")


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
        "url": ITEM_URL.format(token=token),
        "published": format_published(it.get("createdAt")),
    }


def with_page(url: str, page: int) -> str:
    if page == 1:
        return url
    url = re.sub(r"([?&])page=\d+&?", r"\1", url).rstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}page={page}"


def search(browser: Browser, url: str, max_pages: int) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        try:
            html = browser.get(with_page(url, page))
        except BlockedError:
            if page == 1:
                raise
            log.warning("Page %d blocked; using the %d listings already found", page, len(results))
            break
        items = extract_items(html)
        log.info("Page %d: %d listings", page, len(items))
        if page == 1 and not items:
            raise EmptyResultError(f"Page loaded but no listings found for {url}")
        new_on_page = {k: v for k, v in items.items() if k not in results}
        if not new_on_page:
            break
        results.update(new_on_page)
        time.sleep(random.uniform(3, 7))  # be polite
    return {t: normalize(t, it) for t, it in results.items()}


# ---------------------------------------------------------------- state
def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def save_state(path: Path, state: dict[str, str], keep: set[str] = frozenset()) -> None:
    """Value = when the watcher first saw the listing. Drop entries older than
    KEEP_DAYS, but never ones still on yad2 (keep), so they aren't re-sent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    write_json(path, {k: v for k, v in state.items() if v >= cutoff or k in keep})


# ---------------------------------------------------------------- telegram
def tg_api(method: str, retries: int = 2, **params) -> dict:
    """Call the Telegram Bot API, waiting and retrying on rate limits."""
    url = f"https://api.telegram.org/bot{cfg('TG_BOT_TOKEN')}/{method}"
    for attempt in range(retries + 1):
        try:
            data = requests.post(url, json=params, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            if attempt >= retries:
                raise RuntimeError(f"Telegram {method} failed: {e}") from e
            time.sleep(5)
            continue
        if data.get("ok"):
            return data
        retry_after = _get(data, "parameters", "retry_after")
        if data.get("error_code") == 429 and retry_after and attempt < retries:
            log.warning("Telegram rate limit, waiting %ss", retry_after)
            time.sleep(int(retry_after) + 1)
            continue
        raise RuntimeError(f"Telegram {method} failed: {data}")
    raise RuntimeError(f"Telegram {method} failed")


def send_message(text: str) -> None:
    tg_api("sendMessage", chat_id=cfg("TG_CHAT_ID"), text=text,
           parse_mode="HTML", disable_web_page_preview=False)


def format_listing(c: dict) -> str:
    details = " · ".join(str(x) for x in (
        c["year"],
        f'{c["km"]:,} ק"מ' if isinstance(c["km"], int) else None,
        f'יד {c["hand"]}' if c["hand"] else None,
        c["area"],
    ) if x)
    published = f"\n🕒 פורסם: {c['published']}" if c.get("published") else ""
    return (f"🚗 <b>{html_lib.escape(c['title'])}</b>\n💰 {c['price']}\n"
            f"{html_lib.escape(details)}{published}\n{c['url']}")


# ---------------------------------------------------------------- main
def run_once(dry_run: bool) -> None:
    urls = [u for u in re.split(r"[\s|]+", cfg("YAD2_SEARCH_URLS")) if u]
    state_path = Path(cfg("STATE_FILE", "seen.json"))
    max_pages = int(cfg("MAX_PAGES", "1"))

    browser = Browser()
    try:
        current: dict[str, dict] = {}
        for url in urls:
            current.update(search(browser, url, max_pages))
    finally:
        browser.close()
    log.info("Found %d listings in total", len(current))

    state = load_json(state_path)
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
            print(f"{c['title']} | {c['year']} | {c['price']} | {c['published']} | {c['url']}")
        return

    send_message(f"<b>{len(new)} מודעות חדשות ביד2</b>")
    try:
        for c in new:  # one message per car -> link preview with photo
            send_message(format_listing(c))
            state[c["token"]] = now  # mark each car right after it was sent
            time.sleep(1)
    finally:
        # Saved even if Telegram fails midway, so sent cars are never re-sent.
        save_state(state_path, state, keep=set(current))
    log.info("Sent %d new listings to Telegram", len(new))


def track_problems(problem: CheckProblem | None) -> None:
    """Count skipped runs in a row; alert on Telegram once it reaches ALERT_AFTER."""
    path = Path(cfg("STREAK_FILE", "problem_streak.json"))
    if problem is None:
        if path.exists():
            path.unlink()
        return
    streak = (load_json(path) or {}).get("count", 0) + 1
    write_json(path, {"count": streak, "last": type(problem).__name__})
    log.warning("%s - skipped (%d problem run(s) in a row)", problem, streak)
    if streak != int(cfg("ALERT_AFTER", "6")):
        return
    if isinstance(problem, EmptyResultError):
        text = (f"⚠️ יד2 נטען אבל לא נמצאו מודעות {streak} פעמים ברצף. "
                "ייתכן שהאתר השתנה ויש לעדכן את המעקב.")
    else:
        text = f"⚠️ יד2 חוסם את הבדיקה כבר {streak} פעמים ברצף. ייתכן שצריך לבדוק את המעקב."
    try:
        send_message(text)
    except Exception:
        log.exception("Could not send problem alert")


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()

    if args.test:
        send_message("✅ Yad2 watcher can reach you on Telegram!")
        print("Test message sent.")
        return

    try:
        run_once(args.dry_run)
    except CheckProblem as e:
        # A skipped check loses nothing: the next run compares against seen.json.
        track_problems(e)
        return
    track_problems(None)


if __name__ == "__main__":
    main()
