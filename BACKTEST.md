# Routing backtest methodology

`poc/backtest_savings.py` estimates API-rate cost for alternative model routes
using token counts from a local Codex profile. It reads session logs on the
operator's machine and writes the aggregate result into that profile's local
router state. No session logs, prompts, per-turn routes, measured usage, or
operator-specific aggregate results belong in this repository or its release.

The comparison holds token counts constant. It does not measure changes in
quality, retries, prompt-cache behavior, subscription quota, or the actual bill.
The published price table in the script can become stale and must be checked
before interpreting a new result.

Run locally only when the session data may be sent to Jev for classification:

```sh
python3 poc/backtest_savings.py --days 7
```

Keep the resulting state file private. For served-call diagnostics without
reclassification, use `python3 server/report_routing.py --days 7` locally.
