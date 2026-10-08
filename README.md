# Yad2 watcher

Checks a Yad2 car search every hour and sends only **new** listings to Telegram.

## How it works
- `.github/workflows/watch.yml` runs `yad2_watcher.py` on GitHub Actions.
  It is triggered hourly by cron-job.org calling the `workflow_dispatch` API
  (GitHub's own schedule was too unreliable), or by the "Run workflow" button.
- The script opens the search in headless Google Chrome, reads the listings from
  the page's `__NEXT_DATA__` JSON and compares them with `seen.json`.
- New listings and price drops are sent to Telegram (title, price, year, km, hand, area,
  publish time, link). Older ads that newly enter the search get a label.
- `seen.json` (listing id → when first seen + last price) is committed back to the repo.

## Configuration
- Search URL: `YAD2_SEARCH_URLS` in `watch.yml` (several: separate with `|`).
- Repo secrets: `TG_BOT_TOKEN`, `TG_CHAT_ID`.

## Problems
If yad2 shows a captcha, or the page loads with no listings (site change), the run
is skipped and counted in `problem_streak.json`. After 6 problem runs in a row a
warning is sent to Telegram. Skipped runs lose nothing: the next run catches up.
