# polymarket-paper-tracker

Paper-only Polymarket tracker. It watches a short list of pregame favorites and records a simulated $5 buy when a live price dips. No real orders, no wallet, and no API keys.

GitHub Actions runs the tracker every 5 minutes (`America/New_York`). Once each evening at or after 10:00 PM ET — and also when `lean_watch.json` is missing or older than about 24 hours — it rebuilds the watch list for the next day. `screen_state.json` remembers the ET date of the last evening screen so later ticks that night do not screen again. In-progress games stay on the list. Changed state (`lean_watch.json`, `paper_state.json`, `paper_trades.csv`, `screen_state.json`, `logs/`) is committed back to this repo. Log files are truncated before commit.

New paper bets are appended to `logs/new_bets.jsonl`. If the repository secret `BUY_WEBHOOK_URL` is set, those new JSON lines are POSTed to it. The run continues if the webhook is missing or the request fails.

This is not financial advice and it does not place real trades.
