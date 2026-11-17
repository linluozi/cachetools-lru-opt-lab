#!/usr/bin/env python3
"""Attribute exception events to cachetools' write path with sys.monitoring.

CPython 3.12 exposes RAISE / EXCEPTION_HANDLED events through sys.monitoring.
We count raises that originate in cachetools/__init__.py while filling an
LRUCache, then compare that count with a directly measured raise+catch cost.
The estimated share is an upper bound for the savings: it assumes a patched
call site can remove exactly one raise per event.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time

TOOL_ID = 2
EVENTS = sys.monitoring.events


def load_impl(src: str, label: str):
    init = os.path.join(src, "cachetools", "__init__.py")
    spec = importlib.util.spec_from_file_location(
        label, init, submodule_search_locations=[os.path.join(src, "cachetools")]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


def raise_cost_ns(reps: int = 200_000) -> dict:
    """Measure one raise+catch vs a membership test in this interpreter."""
    sentinel = object()
    d = {i: i for i in range(32)}

    t0 = time.perf_counter()
    for i in range(reps):
        try:
            raise KeyError(i)
        except KeyError:
            pass
    raise_ns = (time.perf_counter() - t0) / reps * 1e9

    t0 = time.perf_counter()
    for i in range(reps):
        if i in d:
            d[i] = i
        else:
            d[i] = None
    member_ns = (time.perf_counter() - t0) / reps * 1e9
    return {"raise_catch_ns": raise_ns, "membership_ns": member_ns,
            "reps": reps, "sentinel_is_none": sentinel is None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--scale", type=int, default=20_000)
    ap.add_argument("--out", default="audit/raise_share.json")
    args = ap.parse_args()
    mod = load_impl(os.path.abspath(args.src), "ct_audit")
    target = os.path.abspath(os.path.join(args.src, "cachetools", "__init__.py"))

    counts = {"raise": 0, "handled": 0}
    active = {"on": False}

    def on_raise(code, offset, exc):
        if active["on"] and os.path.abspath(code.co_filename) == target:
            counts["raise"] += 1
        return None

    def on_handled(code, offset, exc):
        if active["on"] and os.path.abspath(code.co_filename) == target:
            counts["handled"] += 1
        return None

    sys.monitoring.use_tool_id(TOOL_ID, "excpath")
    sys.monitoring.register_callback(TOOL_ID, EVENTS.RAISE, on_raise)
    sys.monitoring.register_callback(TOOL_ID, EVENTS.EXCEPTION_HANDLED, on_handled)

    keys = list(range(args.scale))
    cache = mod.LRUCache(maxsize=args.scale + 1)
    active["on"] = True
    sys.monitoring.set_events(TOOL_ID, EVENTS.RAISE | EVENTS.EXCEPTION_HANDLED)
    t0 = time.perf_counter()
    for k in keys:
        cache[k] = k
    elapsed = time.perf_counter() - t0
    sys.monitoring.set_events(TOOL_ID, 0)
    active["on"] = False
    sys.monitoring.free_tool_id(TOOL_ID)

    # Second pass without monitoring, for an uninflated denominator.
    cache2 = mod.LRUCache(maxsize=args.scale + 1)
    t0 = time.perf_counter()
    for k in keys:
        cache2[k] = k
    plain_elapsed = time.perf_counter() - t0

    cost = raise_cost_ns()
    est_s = counts["raise"] * cost["raise_catch_ns"] / 1e9
    report = {
        "source": os.path.abspath(args.src),
        "scale": args.scale,
        "elapsed_s": elapsed,
        "plain_elapsed_s": plain_elapsed,
        "raises_in_cachetools": counts["raise"],
        "handled_in_cachetools": counts["handled"],
        "raises_per_set": counts["raise"] / args.scale,
        "cost": cost,
        "estimated_saved_s": est_s,
        "estimated_share_of_write_loop": est_s / plain_elapsed if plain_elapsed else None,
        "note": "estimated_share uses the unmonitored pass as denominator; upper bound (one raise per event).",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())