# Savings backtest

The method and data-handling rules live at the repository root:
see [BACKTEST.md](../BACKTEST.md). Results stay in local profile state.

The tool itself is `backtest_savings.py` in this folder:

```bash
python3 backtest_savings.py --days 7                # classify + price
python3 backtest_savings.py --days 7 --from-cache   # re-price without new calls
```
