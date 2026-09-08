#!/usr/bin/env python3
"""Check that every account syncs cleanly through the pool.

    python3 deploy/linux/verify-pool.py --base http://127.0.0.1:8030 \
        --token "$MT5_API_API_TOKEN" --parallel 12

Runs one round of hard syncs over every active account and exits non-zero
if any of them failed, so it can gate a deploy. --rounds runs more than one
and judges the last.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def call(base, token, method, path, timeout=600):
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}"), time.time() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw[:300]
        return exc.code, body, time.time() - started
    except Exception as exc:
        return 0, repr(exc)[:200], time.time() - started


def reason(body):
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("error") or json.dumps(detail)[:160]
        if isinstance(detail, str):
            return detail[:160]
    return str(body)[:160]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8030")
    parser.add_argument("--token", required=True)
    parser.add_argument("--parallel", type=int, default=12, help="set this to the pool size")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()

    status, listing, _ = call(args.base, args.token, "GET", "/accounts", timeout=120)
    if status != 200 or not isinstance(listing, dict):
        print(f"cannot list accounts: {status} {listing}", file=sys.stderr)
        return 2

    ids = [a["account_id"] for a in listing["accounts"] if a["status"] == "active"]
    if not ids:
        print("no active accounts to check the pool with", file=sys.stderr)
        return 2
    print(f"{len(ids)} active accounts, {args.parallel} in parallel")

    failures: list[str] = []
    for round_no in range(1, args.rounds + 1):
        failures = []
        durations = []
        started = time.time()

        def sync(account_id):
            code, body, seconds = call(args.base, args.token, "POST", f"/accounts/{account_id}/sync?wait=true")
            ok = code == 200 and isinstance(body, dict) and body.get("ok")
            (durations if ok else failures).append(seconds if ok else reason(body))
            return ok

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(sync, ids))

        durations.sort()
        median = durations[len(durations) // 2] if durations else 0.0
        print(
            f"round {round_no}: {len(durations)} ok, {len(failures)} failed, "
            f"median {median:.1f}s, wall {time.time() - started:.0f}s"
        )
        for message in failures[:5]:
            print(f"    {message}")

    if failures:
        print(f"\npool did not settle: {len(failures)} accounts still failing", file=sys.stderr)
        return 1
    print("\npool is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
