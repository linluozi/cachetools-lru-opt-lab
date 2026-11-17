#!/usr/bin/env python3
"""Benchmark the LRUCache write path of one cachetools build.

Usage:
    python benchmark/bench.py --src /tmp/cachetools/src --label baseline \
        --out benchmark/baseline.json --corpus-root /tmp/cachetools

Each run loads exactly one implementation from --src, so the two builds never
share a process.  Raw per-sample files go to benchmark/raw/.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import random
import resource
import statistics
import sys
import time

SEED = 20261118
WARMUP = 3
REPS = 5
SCALES = (1_000, 10_000, 50_000)


def load_impl(src: str, label: str):
    init = os.path.join(src, "cachetools", "__init__.py")
    spec = importlib.util.spec_from_file_location(
        label, init, submodule_search_locations=[os.path.join(src, "cachetools")]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


def timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def real_corpus(corpus_root: str) -> list[str]:
    """Unique keys derived from real upstream files (not synthetic repeats)."""
    keys, seen = [], set()
    root = os.path.abspath(corpus_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".tox"})
        for name in sorted(filenames):
            if not name.endswith((".py", ".rst", ".md", ".toml", ".txt", ".ini")):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, "rb") as fh:
                    for lineno, line in enumerate(fh, 1):
                        digest = hashlib.sha256(line).hexdigest()[:16]
                        key = f"{rel}:{lineno}:{digest}"
                        if key not in seen:
                            seen.add(key)
                            keys.append(key)
            except OSError:
                continue
    return keys


def run_samples(mod, keys, prefill: bool, seed: int) -> list[float]:
    order = list(keys)
    random.Random(seed).shuffle(order)
    def fresh():
        cache = mod.LRUCache(maxsize=len(order) + 1)
        if prefill:
            for k in order:
                cache[k] = 1
        return cache

    for _ in range(WARMUP):
        cache = fresh()
        for k in order:
            cache[k] = 2
    samples = []
    for _ in range(REPS):
        cache = fresh()
        samples.append(timed(lambda: [cache.__setitem__(k, 3) for k in order]))
    return samples


def summarize(samples: list[float]) -> dict:
    med = statistics.median(samples)
    return {
        "median_ms": med * 1e3,
        "p25_ms": statistics.quantiles(samples, n=4)[0] * 1e3,
        "p75_ms": statistics.quantiles(samples, n=4)[2] * 1e3,
        "min_ms": min(samples) * 1e3,
        "max_ms": max(samples) * 1e3,
        "rsd": (statistics.pstdev(samples) / med) if med else 0.0,
        "samples_ms": [s * 1e3 for s in samples],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="path containing cachetools/ package")
    ap.add_argument("--label", required=True, choices=["baseline", "optimized"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--corpus-root", required=True, help="upstream checkout used as the real corpus")
    ap.add_argument("--raw-dir", default=os.path.join(os.path.dirname(__file__), "raw"))
    args = ap.parse_args()

    os.makedirs(args.raw_dir, exist_ok=True)
    mod = load_impl(os.path.abspath(args.src), "ct_" + args.label)

    corpus = real_corpus(args.corpus_root)
    if len(corpus) < 1_000:
        raise SystemExit(f"real corpus too small: {len(corpus)} unique keys")

    result = {
        "impl": args.label,
        "source": os.path.abspath(args.src),
        "cachetools_version": getattr(mod, "__version__", "unknown"),
        "seed": SEED,
        "warmup": WARMUP,
        "reps": REPS,
        "command": " ".join([sys.executable] + sys.argv),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "real_corpus": {"root": os.path.abspath(args.corpus_root), "unique_keys": len(corpus)},
        "workloads": {},
    }

    workloads = []
    for scale in SCALES:
        keys = [f"k{i}:{hashlib.sha256(str(i).encode()).hexdigest()[:8]}" for i in range(scale)]
        workloads.append((f"new_keys_{scale}", keys, False))
        workloads.append((f"hit_path_{scale}", keys, True))
    workloads.append(("real_corpus_write", corpus, False))

    for name, keys, prefill in workloads:
        samples = run_samples(mod, keys, prefill, SEED)
        result["workloads"][name] = {
            "keys": len(keys),
            "prefill": prefill,
            **summarize(samples),
        }
        for i, sample in enumerate(samples):
            with open(os.path.join(args.raw_dir, f"{args.label}-{name}-{i}.json"), "w") as fh:
                json.dump({"impl": args.label, "workload": name, "keys": len(keys),
                           "sample": i, "seconds": sample}, fh)

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    for name, w in result["workloads"].items():
        print(f"{args.label:9s} {name:20s} {w['median_ms']:10.3f} ms  rsd={w['rsd']*100:5.1f}%")
    print(f"peak_rss_kb={result['peak_rss_kb']} corpus={len(corpus)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())