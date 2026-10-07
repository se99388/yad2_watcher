# Yad2 watcher

Checks a Yad2 car search every hour (GitHub Actions) and sends only new listings to Telegram.

## Setup
1. Repo → Settings → Secrets and variables → Actions → New repository secret. Add:
   - `YAD2_SEARCH_URLS` – your yad2 search URL (several: separate with `|`)
   - `TG_BOT_TOKEN` – bot token from @BotFather
   - `TG_CHAT_ID` – your chat id from @userinfobot
2. Actions tab → enable workflows → "Yad2 watcher" → Run workflow.
   First run sends "watcher active" and remembers current listings.
3. From then on it runs every hour automatically.

`seen.json` is committed by the workflow to remember what was already sent.
If a run fails with "likely bot protection", Yad2 blocked GitHub's servers.
