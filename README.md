# cachetools-lru-opt-lab

`cachetools` is a pure-Python memoizing-collections library. This repo removes one
exception from `LRUCache`'s new-key insertion path: `__touch()` used
`OrderedDict.move_to_end(key)` inside `try/except KeyError` to detect a first
insert, so **every new key raised and caught a `KeyError`** before the item was
stored. The patch replaces that probe with an O(1) membership test.

Upstream: https://github.com/tkem/cachetools

- Upstream commit: `1fd6e4a1a3fdd165be118059c23522d5d2769d3a` (cachetools 8.0.0)
- Patch: `patch.diff` — 1 file, **+5 / −4 lines** (`git apply --check` verified on a clean clone)
- Scope: only the write path of `LRUCache`. Public API, return values and eviction
  order are unchanged; the differential test below re-checks them byte-for-byte.

## Measured result

`benchmark/bench.py`, CPython 3.12.3, AMD EPYC 7K62, 3 warmups + 5 timed reps,
fixed seed `20261118`, fresh cache per sample. Medians in ms:

| workload (keys) | baseline | optimized | speedup |
|---|---:|---:|---:|
| new keys, 1 000 | 0.681 | 0.416 | **1.638x** |
| new keys, 10 000 | 7.341 | 4.311 | **1.703x** |
| new keys, 50 000 | 50.946 | 35.422 | **1.438x** |
| real corpus, 2 165 unique keys | 1.500 | 0.886 | **1.693x** |

Median of the three scale speedups: **1.638x** (G3). Largest scale: **1.438x** (G4).

"Real corpus" is not a synthetic repeat loop: `bench.py` hashes every line of the
files in this repository (`.py`, `.md`, `.rst`, `.toml`, `.txt`) into distinct
`path:line:sha256` keys, and times one insertion pass over 2 165 unique keys.
The corpus is the checkout itself, so the number is reproducible offline.

### The cost, stated plainly

The membership test is also paid on **hits**, where the baseline raised nothing:

| hit path (existing keys) | baseline | optimized | ratio |
|---|---:|---:|---:|
| 1 000 | 0.267 | 0.291 | 0.916x |
| 10 000 | 2.828 | 3.019 | 0.937x |
| 50 000 | 20.026 | 21.734 | 0.921x |

So this is a **write-heavy-cache** optimization, not a universal win: read-only
caches regress by ~6–8%. Any general claim would be false. `bench.py` times both
paths so the regression is part of the published evidence, not a footnote.

## Exception attribution (`audit/raise_share.py`)

Using CPython 3.12's `sys.monitoring` events (`RAISE`, `EXCEPTION_HANDLED`), the
audit counts exceptions whose code object belongs to `cachetools/__init__.py`
while filling a fresh `LRUCache(maxsize=n+1)` with 20 000 new keys:

- `raises_in_cachetools = 20 000`, `handled_in_cachetools = 20 000` → **1.000 raise per new key**
- measured `raise + catch`: **214.4 ns**; membership test: **114.1 ns**
- estimated share of the unmonitored write loop: **29.7%** (upper bound: it assumes
  exactly one raise can be removed per event)

That attribution is what motivated the patch; the wall-clock bench above is the
independent confirmation.

## Differential test

`tests/test_diff.py` generates 4 004 operation traces, canonicalises each trace to
JSON and **deduplicates by sha256**, leaving **4 004 unique traces** (target ≥1 200).
Each trace runs on both builds and compares the exact result sequence (return value
or exception type + message) plus the final iteration order / length.

```
unique_traces: 4004
mismatches: 0
```

Boundary coverage includes `None`, `NaN`, bytes, non-ASCII keys, tuples,
`maxsize=1`, empty `popitem()`, `getsizeof`, and deletion of missing keys.

## Gates

| gate | result | reproducible command |
|---|---|---|
| G1 upstream tests | **416 passed, 24 subtests passed in 0.47s** | `cd /tmp/cachetools && /tmp/venv_ct/bin/python -m pytest tests -q` |
| G2 differential | **4 004 unique traces, 0 mismatch** | `python3 tests/test_diff.py --baseline-src baseline_src --optimized-src src` |
| G3 median speedup | **1.638x** (three scales) | see bench commands below |
| G3b real corpus | **1.693x** on 2 165 real-file keys | `python3 benchmark/bench.py --src src --label optimized --out benchmark/optimized.json --corpus-root .` |
| G4 largest scale | **1.438x** ≥ 0.95x | same bench |
| G5 peak RSS | **20 364 KB vs 20 364 KB = 1.00x** | `ru_maxrss` recorded in both JSONs |

Interpreter cross-check: this machine has **only CPython 3.12.3**; 3.10/3.11 are
not installed, so no multi-version numbers are claimed.

## 本轮创新

**B 档：把「缓存写入路径用异常做存在性探测」当成独立性能轴，并用
`sys.monitoring` 的 RAISE/EXCEPTION_HANDLED 事件把热点量化到 1.000 raise/次写入、
实测 214.4 ns/次，再以零语义差异补丁复证 1.638x。** 与台账里已有的热路径优化
（wcwidth/glom/jmespath/sqlparse 等）不同：那些优化的是正常路径的重复计算；这里
第一次针对「首次插入必抛 KeyError」这一被异常掩盖的写路径，并同时公开命中路径
6–8% 的回退，避免用写多读少的负载冒充通用收益。存量台账无 cachetools 条目。

## Reproduce

```bash
# 1. differential test, offline (sources shipped for this purpose)
python3 tests/test_diff.py --baseline-src baseline_src --optimized-src src

# 2. baseline + optimized benchmark, offline
python3 benchmark/bench.py --src baseline_src --label baseline --out benchmark/baseline.json --corpus-root .
python3 benchmark/bench.py --src src --label optimized --out benchmark/optimized.json --corpus-root .

# 3. upstream tests on a patched clean clone
git clone --depth=1 https://github.com/tkem/cachetools /tmp/cachetools
git -C /tmp/cachetools apply "$PWD/patch.diff"
python3 -m venv /tmp/venv_ct && /tmp/venv_ct/bin/pip install -q -e /tmp/cachetools pytest
cd /tmp/cachetools && /tmp/venv_ct/bin/python -m pytest tests -q
```

## Environment

- CPU: AMD EPYC 7K62 48-Core Processor (4 vCPU visible)
- RAM: 3 812 564 kB total
- Python: 3.12.3 (CPython); Node not used
- seed: 20261118; warmups: 3; timed reps: 5
- `benchmark/raw/` holds all 70 raw samples (per impl × workload × rep), not only aggregates.
- `benchmark/baseline.json` / `optimized.json` record command, CPU, RAM, version, RSS, and every sample.