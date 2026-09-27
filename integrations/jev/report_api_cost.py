#!/usr/bin/env python3
"""API list-price equivalent for observed Jev attempts and fixed-token baselines.

These are counterfactual API prices, never ChatGPT subscription debits or a
claim that a different model would produce equal quality or token counts.
"""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request

def codex_home():
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def state_dir():
    configured = os.environ.get("JEV_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif os.sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return (base / "jev-codex-harness").resolve()


def default_paths():
    results = state_dir() / "cost"
    return codex_home() / "codex-router/jev-router-live.jsonl", results, results / "gonka-prices.json"


LOG, RESULTS, GONKA_CACHE = default_paths()  # Compatibility for existing imports.
GONKA_URL = "https://api.gonkagate.com/api/v1/public/pricing"
PRICE_CHECKED = date(2026, 9, 26)
D = Decimal
MILLION = D(1_000_000)
MONEY = D("0.00000001")

SOURCES = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
    "anthropic": "https://platform.claude.com/docs/de/models/opus-4-6/overview",
    "gonka": GONKA_URL,
    "wally": "Verify current pricing with the provider before using this estimate",
}


@dataclass(frozen=True)
class Rate:
    input: Decimal
    cached: Decimal
    output: Decimal
    cache_write: Decimal | None
    source: str
    provisional: bool = False
    openai: bool = False


OPENAI = {
    "gpt-6-luna": ("0.10", "0.01", "0.50", "0.125"),
    "gpt-6-sol": ("2.00", "0.20", "10.00", "2.50"),
    "gpt-6-astra": ("10.00", "1.00", "50.00", "12.50"),
    "gpt-5.6-luna": ("0.20", "0.02", "1.20", "0.25"),
    "gpt-5.6-terra": ("2.00", "0.20", "12.00", "2.50"),
    "gpt-5.6-sol": ("4.00", "0.40", "20.00", "5.00"),
}
NEXT_NATIVE = {"gpt-6-luna": "gpt-6-sol", "gpt-6-sol": "gpt-6-astra",
               "gpt-5.6-luna": "gpt-5.6-terra", "gpt-5.6-terra": "gpt-5.6-sol"}


def canonical_model(model):
    leaf = str(model or "").split("/")[-1]
    match = re.fullmatch(r"(gpt-(?:6|5\.6)-(?:luna|terra|sol|astra))(?:-(?:low|medium|high|xhigh|max))?", leaf)
    return match.group(1) if match else str(model or "")


def rate_for(model, day, gonka_prices):
    model = canonical_model(model)
    if model in OPENAI:
        inp, cached, out, write = (D(x) for x in OPENAI[model])
        return Rate(inp, cached, out, write, SOURCES["openai"], openai=True)
    if model == "antigravity/gemini-3.8-flash-tiered":
        multiplier = 1 if day <= date(2026, 12, 31) else 2
        return Rate(D("0.75") * multiplier, D("0.075") * multiplier,
                    D("3.75") * multiplier, None, SOURCES["google"])
    if model == "antigravity/claude-opus-4-6-thinking":
        return Rate(D(5), D("0.50"), D(25), D("6.25"), SOURCES["anthropic"])
    if model.startswith("gonkagate/"):
        price = gonka_prices.get(model.removeprefix("gonkagate/"))
        if price is not None:
            flat = D(str(price))
            return Rate(flat, flat, flat, flat, SOURCES["gonka"])
    if model == "wally/glm-5.3-flash":
        return Rate(D("0.10"), D("0.10"), D("0.35"), D("0.10"),
                    SOURCES["wally"], provisional=True)
    return None


def price_attempt(usage, rate, speed="default"):
    """Return cost bounds at the same token volume; None means unpriceable."""
    if not isinstance(usage, dict) or speed not in ("default", "fast", "priority"):
        return None
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    cached = usage.get("cached_input_tokens")
    write = usage.get("cache_write_input_tokens", 0)
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (inp, out, write)):
        return None
    if cached is not None and (isinstance(cached, bool) or not isinstance(cached, int) or cached < 0):
        return None
    if write > inp or cached is not None and cached + write > inp:
        return None
    if write and rate.cache_write is None:
        return None
    multiplier = D(2) if rate.openai and speed in ("fast", "priority") else D(1)
    context = inp > 272_000 and rate.openai
    input_rate = rate.input * multiplier * (2 if context else 1)
    cached_rate = rate.cached * multiplier * (2 if context else 1)
    write_rate = (rate.cache_write or D(0)) * multiplier * (2 if context else 1)
    output_rate = rate.output * multiplier * (D("1.5") if context else 1)
    def at(cache_reads):
        uncached = inp - cache_reads - write
        return (D(uncached) * input_rate + D(cache_reads) * cached_rate
                + D(write) * write_rate + D(out) * output_rate) / MILLION
    values = (at(cached),) if cached is not None else (at(0), at(inp - write))
    return {"low": min(values), "high": max(values), "cache_known": cached is not None,
            "cache_write_known": "cache_write_input_tokens" in usage,
            "long_context": context, "input_tokens": inp, "output_tokens": out}


def paired_difference(usage, actual_rate, baseline_rate, speed="default"):
    """Bound the difference using the SAME possible cache mix on both models."""
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    write = usage.get("cache_write_input_tokens", 0)
    if not isinstance(inp, int) or isinstance(inp, bool) or not isinstance(write, int):
        return None
    known = usage.get("cached_input_tokens")
    choices = (known,) if isinstance(known, int) and not isinstance(known, bool) else (0, inp - write)
    differences = []
    for cached in choices:
        candidate = {**usage, "cached_input_tokens": cached}
        actual = price_attempt(candidate, actual_rate, speed)
        baseline = price_attempt(candidate, baseline_rate, speed)
        if actual is None or baseline is None:
            return None
        differences.append(baseline["low"] - actual["low"])
    return min(differences), max(differences)


def read_jsonl(path):
    for source in (path.with_name(path.name + ".1"), path):
        try:
            stream = source.open(encoding="utf-8")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value


def amount(value):
    return str(value.quantize(MONEY))


def rate_card(rate):
    return {"input_per_million": str(rate.input), "cached_input_per_million": str(rate.cached),
            "output_per_million": str(rate.output),
            "cache_write_per_million": str(rate.cache_write) if rate.cache_write is not None else None,
            "source": rate.source, "provisional": rate.provisional}


def report(hours=24, *, routing_log=None, gonka_prices=None, now=None):
    if not 0 < hours <= 24 * 90:
        raise ValueError("hours must be between 0 and 2160")
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)
    gonka_prices = gonka_prices or {}
    groups = defaultdict(lambda: {"attempts": 0, "completed": 0, "usage_unknown": 0,
                                  "unpriced": 0, "cache_unknown": 0, "cache_write_unknown": 0,
                                  "long_context": 0, "provisional": False,
                                  "rate_cards": [], "next_tier_model": None,
                                  "routed_low": D(0), "routed_high": D(0),
                                  "sol_low": D(0), "sol_high": D(0),
                                  "astra_low": D(0), "astra_high": D(0),
                                  "save_sol_low": D(0), "save_sol_high": D(0),
                                  "save_astra_low": D(0), "save_astra_high": D(0),
                                  "next_low": D(0), "next_high": D(0),
                                  "save_next_low": D(0), "save_next_high": D(0),
                                  "next_attempts": 0})
    calls = 0
    source_rows = hashlib.sha256()
    log_first = log_last = None
    routing_log = Path(routing_log) if routing_log is not None else default_paths()[0]
    for row in read_jsonl(Path(routing_log)):
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        at = at.replace(tzinfo=now.tzinfo) if at.tzinfo is None else at.astimezone(now.tzinfo)
        log_first = at if log_first is None or at < log_first else log_first
        log_last = at if log_last is None or at > log_last else log_last
        if (at < cutoff or at > now or not isinstance(row.get("attempts"), list)
                or ("ladder_stage" not in row and "gate" not in row)):
            continue
        calls += 1
        source_rows.update(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8") + b"\n")
        for attempt in row.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            model = canonical_model(attempt.get("model"))
            group = groups[model]
            group["attempts"] += 1
            if attempt.get("http_status") == 200 and attempt.get("terminal_type") == "response.completed":
                group["completed"] += 1
            usage = attempt.get("usage")
            if not isinstance(usage, dict):
                group["usage_unknown"] += 1
                continue
            rate = rate_for(model, at.date(), gonka_prices)
            priced = price_attempt(usage, rate, attempt.get("speed", "default")) if rate else None
            if priced is None:
                group["unpriced"] += 1
                continue
            card = rate_card(rate)
            if card not in group["rate_cards"]:
                group["rate_cards"].append(card)
            group["provisional"] |= rate.provisional
            group["cache_unknown"] += not priced["cache_known"]
            group["cache_write_unknown"] += not priced["cache_write_known"]
            group["long_context"] += priced["long_context"]
            group["routed_low"] += priced["low"]
            group["routed_high"] += priced["high"]
            for baseline, prefix in (("gpt-6-sol", "sol"), ("gpt-6-astra", "astra")):
                base_rate = rate_for(baseline, at.date(), gonka_prices)
                base = price_attempt(usage, base_rate, attempt.get("speed", "default"))
                difference = paired_difference(usage, rate, base_rate, attempt.get("speed", "default"))
                group[prefix + "_low"] += base["low"]
                group[prefix + "_high"] += base["high"]
                group["save_" + prefix + "_low"] += difference[0]
                group["save_" + prefix + "_high"] += difference[1]
            next_model = NEXT_NATIVE.get(model)
            if next_model:
                group["next_tier_model"] = next_model
                next_rate = rate_for(next_model, at.date(), gonka_prices)
                next_cost = price_attempt(usage, next_rate, attempt.get("speed", "default"))
                difference = paired_difference(usage, rate, next_rate, attempt.get("speed", "default"))
                group["next_attempts"] += 1
                group["next_low"] += next_cost["low"]
                group["next_high"] += next_cost["high"]
                group["save_next_low"] += difference[0]
                group["save_next_high"] += difference[1]
    money_keys = {"routed_low", "routed_high", "sol_low", "sol_high", "astra_low", "astra_high",
                  "save_sol_low", "save_sol_high", "save_astra_low", "save_astra_high",
                  "next_low", "next_high", "save_next_low", "save_next_high"}
    totals = {key: sum((group[key] for group in groups.values()), D(0)) for key in money_keys}
    numeric = ("attempts", "completed", "usage_unknown", "unpriced", "cache_unknown",
               "cache_write_unknown", "long_context", "next_attempts")
    for key in numeric:
        totals[key] = sum(group[key] for group in groups.values())
    priced_attempts = totals["attempts"] - totals["usage_unknown"] - totals["unpriced"]
    provisional_models = sorted(model for model, group in groups.items() if group["provisional"])
    coverage = ("complete" if priced_attempts > 0 and priced_attempts == totals["attempts"]
                and not provisional_models and bool(log_first and log_first <= cutoff)
                and (now.date() - PRICE_CHECKED).days <= 30
                else "partial" if priced_attempts > 0 else "no_priced_data")
    def public(group):
        return {key: amount(value) if key in money_keys else value for key, value in group.items()}
    orchestrator_comparison = {}
    for model, prefix in (("gpt-6-sol", "sol"), ("gpt-6-astra", "astra")):
        orchestrator_comparison[model] = {
            "routed_cost_low_usd": amount(totals["routed_low"]),
            "routed_cost_high_usd": amount(totals["routed_high"]),
            "baseline_cost_low_usd": amount(totals[prefix + "_low"]),
            "baseline_cost_high_usd": amount(totals[prefix + "_high"]),
            "difference_low_usd": amount(totals["save_" + prefix + "_low"]),
            "difference_high_usd": amount(totals["save_" + prefix + "_high"]),
        }
    return {"window_hours": hours, "as_of": now.isoformat(), "price_checked": PRICE_CHECKED.isoformat(),
            "price_stale": (now.date() - PRICE_CHECKED).days > 30,
            "priced_attempts": priced_attempts, "coverage": coverage,
            "provisional_models": provisional_models,
            "log_first_at": log_first.isoformat() if log_first else None,
            "log_last_at": log_last.isoformat() if log_last else None,
            "source_rows_sha256": source_rows.hexdigest() if calls else None,
            "retained_log_covers_window_start": bool(log_first and log_first <= cutoff),
            "routed_calls": calls, "by_model": {model: public(group) for model, group in sorted(groups.items())},
            "totals": public(totals), "sources": SOURCES,
            "orchestrator_comparison": orchestrator_comparison,
            "baselines": {model: rate_card(rate_for(model, now.date(), gonka_prices))
                          for model in ("gpt-6-sol", "gpt-6-astra")},
            "actual_account_debit_usd": None,
            "method": "Same observed input/output tokens and same possible cache mix at public API list rates. Every usage-known jev/auto router attempt, including retries, is priced. Manual-model calls outside jev/auto, unknown usage, and standalone OmniRoute helper calls are excluded. Subscription quotas, promotional credits, tool fees, cache storage, quality, and changed token counts are not estimated."}


def _dollars(value):
    amount = D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    return f"${amount:,.2f}"


def _range(low, high):
    return _dollars(low) if D(str(low)) == D(str(high)) else f"{_dollars(low)}–{_dollars(high)}"


def render_summary(value, *, report_sha256=None):
    """Human-readable, reproducible view of one saved API-equivalent report."""
    at = datetime.fromisoformat(value["as_of"])
    hours = value["window_hours"]
    totals = value["totals"]
    attempts = totals["attempts"]
    priced = value.get("priced_attempts", attempts - totals["usage_unknown"] - totals["unpriced"])
    lines = [f"Снимок за последние {hours:g} ч на {at:%d.%m.%Y %H:%M:%S %Z}",
             "API-эквивалент по наблюдаемым токенам, не списание с подписки"]
    if priced <= 0:
        lines.append("Нет оценённых вызовов за выбранный период.")
    else:
        rows = [
            ("Фактически выбранные модели", _range(totals["routed_low"], totals["routed_high"]), "—"),
        ]
        for model in ("gpt-6-sol", "gpt-6-astra"):
            comparison = value["orchestrator_comparison"][model]
            rows.append((f"Вся оценённая работа на {model}",
                         _range(comparison["baseline_cost_low_usd"], comparison["baseline_cost_high_usd"]),
                         _range(comparison["difference_low_usd"], comparison["difference_high_usd"])))
        headers = ("Маршрут", "API-эквивалент", "Разница с маршрутом")
        widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(3)]
        lines.append("  ".join(label.ljust(widths[i]) for i, label in enumerate(headers)))
        for row in rows:
            lines.append("  ".join(label.ljust(widths[i]) for i, label in enumerate(row)))
    lines.append(f"Охват: {priced}/{attempts} попыток оценены; без usage {totals['usage_unknown']}; "
                 f"без цены {totals['unpriced']}; окно журнала "
                 + ("полное" if value["retained_log_covers_window_start"] else "неполное"))
    if priced > 0 and value.get("coverage") != "complete":
        lines.append("Оценка частичная: суммы относятся только к оценённым попыткам.")
    if value.get("price_stale"):
        lines.append("Прайс-лист устарел; перепроверьте цены перед сравнением.")
    if value.get("provisional_models"):
        lines.append("Предварительные тарифы: " + ", ".join(value["provisional_models"]))
    if value.get("gonka_pricing", {}).get("mode") == "cached":
        lines.append("Gonka: использован сохранённый прайс, а не свежий ответ провайдера.")
    lines.append("Сравнение держит число токенов и кеш неизменными; качество и реальный счёт не измерены.")
    lines.append("Учтены только вызовы jev/auto через этот роутер; ручные модели и отдельные инструменты вне отчёта.")
    if value.get("source_rows_sha256"):
        lines.append("Отпечаток строк журнала: " + value["source_rows_sha256"][:16])
    if report_sha256:
        lines.append("SHA-256 отчёта: " + report_sha256)
    return "\n".join(lines)


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness.platforms.windows import _private_state_dir
        _private_state_dir(path.parent, create=False)
    else:
        path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".jev-cost-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def live_gonka_prices(cache=None):
    cache = Path(cache) if cache is not None else default_paths()[2]
    try:
        with urllib.request.urlopen(GONKA_URL, timeout=10) as response:
            data = json.load(response).get("data") or {}
        prices = {row["id"]: row["usdPer1MTokens"]["total"]
                  for row in data.get("models") or [] if isinstance(row, dict)
                  and isinstance(row.get("usdPer1MTokens"), dict)
                  and row.get("id") in ("zai-org/glm-5.3-flash", "deepseek-ai/deepseek-v4-flash-0731")}
        if len(prices) != 2 or any(not 0 < D(str(value)) < 1 for value in prices.values()):
            raise ValueError("Gonka pricing response incomplete")
        snapshot = {"updated_at": data.get("updatedAt"), "fetched_at": datetime.now().astimezone().isoformat(),
                    "prices": prices, "source": GONKA_URL, "mode": "live"}
        save_private(cache, snapshot)
        return snapshot
    except (OSError, ValueError, KeyError, TypeError):
        try:
            previous = json.loads(cache.read_text())
            if isinstance(previous.get("prices"), dict):
                fetched = datetime.fromisoformat(str(previous.get("fetched_at")))
                if fetched.tzinfo and datetime.now().astimezone() - fetched <= timedelta(hours=24):
                    return {**previous, "mode": "cached"}
        except (OSError, ValueError):
            pass
        return {"updated_at": None, "prices": {}, "source": GONKA_URL, "mode": "unavailable"}


def gonka_used(routing_log, hours, now=None):
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)
    for row in read_jsonl(Path(routing_log)):
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        at = at.replace(tzinfo=now.tzinfo) if at.tzinfo is None else at.astimezone(now.tzinfo)
        if at < cutoff or at > now:
            continue
        if any(isinstance(attempt, dict) and str(attempt.get("model") or "").startswith("gonkagate/")
               for attempt in row.get("attempts") or []):
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", action="store_true", help="print the saved API-equivalent comparison")
    parser.add_argument("--from-report", type=Path, help="reprint a previously saved report without new API calls")
    args = parser.parse_args()
    if args.from_report:
        try:
            raw = args.from_report.read_bytes()
            value = json.loads(raw)
            text = render_summary(value, report_sha256=hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError, KeyError, TypeError):
            parser.error("saved report is missing or invalid")
        print(text)
        return
    now = datetime.now().astimezone()
    default_name = ("report-" + now.strftime("%Y%m%dT%H%M%S%f") + ".json"
                    if args.summary else "report-24h.json")
    output = args.output or (state_dir() / "cost" / default_name)
    gonka = (live_gonka_prices() if gonka_used(default_paths()[0], args.hours, now=now) else
             {"updated_at": None, "prices": {}, "source": GONKA_URL, "mode": "not_needed"})
    value = report(args.hours, gonka_prices=gonka["prices"], now=now)
    value["gonka_pricing"] = {key: gonka.get(key) for key in ("updated_at", "fetched_at", "mode", "source")}
    save_private(output, value)
    if args.summary:
        print(render_summary(value, report_sha256=hashlib.sha256(output.read_bytes()).hexdigest()))
        print("Файл отчёта: " + str(output))
    else:
        print(output)


if __name__ == "__main__":
    main()
