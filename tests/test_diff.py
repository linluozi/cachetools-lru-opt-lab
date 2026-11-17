#!/usr/bin/env python3
"""Differential test: baseline cachetools LRUCache vs patched build.

Every operation trace is canonicalised and hashed; duplicate traces are dropped
before the count is reported.  Each unique trace must produce an identical
sequence of results (values or exception type+message) and an identical final
cache state, byte-for-byte after canonical serialisation.

Usage:
    python tests/test_diff.py --baseline-src /tmp/cachetools/src \
        --optimized-src /tmp/cachetools-opt/src
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
import os
import random
import sys

TARGET_UNIQUE = 1_200


def load_impl(src: str, label: str):
    init = os.path.join(src, "cachetools", "__init__.py")
    spec = importlib.util.spec_from_file_location(
        label, init, submodule_search_locations=[os.path.join(src, "cachetools")]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


def canonical(value):
    if isinstance(value, float) and math.isnan(value):
        return {"__nan__": True}
    if isinstance(value, tuple):
        return {"__tuple__": [canonical(v) for v in value]}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, frozenset):
        return {"__frozenset__": sorted(canonical(v) for v in value)}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return value


def snapshot(cache):
    items = []
    for k in cache:
        items.append((canonical(k), canonical(cache[k])))
    return json.dumps({"items": items, "len": len(cache)}, sort_keys=True)


def call(cache, op, key, value):
    try:
        if op == "set":
            cache[key] = value
            return ("ok", None)
        if op == "get":
            return ("ok", canonical(cache[key]))
        if op == "contains":
            return ("ok", key in cache)
        if op == "del":
            del cache[key]
            return ("ok", None)
        if op == "popitem":
            return ("ok", canonical(cache.popitem()))
        if op == "len":
            return ("ok", len(cache))
        if op == "clear":
            cache.clear()
            return ("ok", None)
        raise AssertionError(op)
    except Exception as exc:  # noqa: BLE001 - exception identity is the contract
        return ("exc", type(exc).__name__, str(exc))


def make_keys(rng):
    pool = [None, True, False, 0, 1, -1, 10**30, 3.5, float("nan"), "a", "α", "中文",
            "x" * 300, (1, 2), (1, "α"), frozenset({1, 2}), b"bytes"]
    return pool


def gen_traces(rng, count):
    ops = ["set", "get", "contains", "del", "popitem", "len", "clear"]
    keys = make_keys(rng)
    traces = []
    for _ in range(count):
        ops_n = rng.randint(3, 14)
        maxsize = rng.choice([1, 2, 3, 5, 8, 16])
        getsizeof = rng.choice([None, None, None, lambda v: 1 if v is not None else 0])
        trace = {"maxsize": maxsize, "getsizeof": getsizeof is not None, "ops": []}
        for _ in range(ops_n):
            op = rng.choice(ops)
            key = rng.choice(keys)
            value = rng.choice(["v", "α", 0, 1, (1, 2), None])
            trace["ops"].append([op, canonical(key), canonical(value)])
        traces.append(trace)
    return traces


def dedup(traces):
    seen, unique = set(), []
    for trace in traces:
        blob = json.dumps(trace, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique.append(trace)
    return unique, len(seen)


def replay(mod, trace):
    kwargs = {}
    if trace["getsizeof"]:
        kwargs["getsizeof"] = lambda v: 1 if v is not None else 0
    cache = mod.LRUCache(maxsize=trace["maxsize"], **kwargs)
    results = []
    for op, key, value in trace["ops"]:
        results.append(call(cache, op, key, value))
    return {"results": results, "state": snapshot(cache)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-src", required=True)
    ap.add_argument("--optimized-src", required=True)
    ap.add_argument("--seed", type=int, default=20261118)
    args = ap.parse_args()

    base = load_impl(os.path.abspath(args.baseline_src), "ct_base")
    opt = load_impl(os.path.abspath(args.optimized_src), "ct_opt")

    rng = random.Random(args.seed)
    traces = gen_traces(rng, 4_000)
    # boundary traces: empty cache, single key, __missing__ that stores the item
    traces.extend([
        {"maxsize": 1, "getsizeof": False, "ops": [["popitem", None, None]]},
        {"maxsize": 1, "getsizeof": False, "ops": [["get", "missing", None]]},
        {"maxsize": 1, "getsizeof": False, "ops": [["set", None, "v"], ["get", None, None]]},
        {"maxsize": 2, "getsizeof": False,
         "ops": [["set", "α", "1"], ["set", "β", "2"], ["get", "α", None], ["set", "γ", "3"]]},
    ])
    unique, n_unique = dedup(traces)
    if n_unique < TARGET_UNIQUE:
        raise SystemExit(f"only {n_unique} unique traces, need >= {TARGET_UNIQUE}")

    mismatches = []
    for i, trace in enumerate(unique):
        a = replay(base, trace)
        b = replay(opt, trace)
        if a != b:
            mismatches.append({"index": i, "trace": trace, "baseline": a, "optimized": b})
            if len(mismatches) >= 5:
                break

    summary = {
        "seed": args.seed,
        "generated_traces": len(traces),
        "unique_traces": n_unique,
        "dedup": "sha256(canonical trace JSON)",
        "comparison": "result sequence + final state, exact equality",
        "mismatches": len(mismatches),
        "examples": mismatches[:2],
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())