#!/usr/bin/env python3
"""Bounded stdin/stdout bridge used by the pi extension; secrets stay in Python."""
import json
from pathlib import Path
import sys

from client import choose_context


def main():
    request = json.load(sys.stdin)
    root = Path(request["root"]).resolve(strict=True)
    cache = Path(request["cache_dir"]).resolve()
    goal = request.get("goal", "").encode("utf-8")[:2000].decode("utf-8", errors="ignore")
    if not goal.strip():
        raise ValueError("Actual task goal required")
    candidates = request.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 12:
        raise ValueError("Too many candidates")
    bounded = []
    total_bytes = 0
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("excerpt"), str):
            raise ValueError("Invalid candidate")
        size = len(item["excerpt"].encode("utf-8"))
        if size > 12000 or total_bytes + size > 24000:
            raise ValueError("Full candidate exceeds context budget; keep it")
        total_bytes += size
        bounded.append({"id": item["id"][:200], "excerpt": item["excerpt"]})
    result = choose_context(root, goal, bounded, cache)
    print(json.dumps({"pruned": result["pruned"], "jev": result["judgment"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
