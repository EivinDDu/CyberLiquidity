"""Engine benchmark: the workloads the app and the experiments actually run.

    python benchmarks/bench_engine.py                # table of median timings
    python benchmarks/bench_engine.py --json out.json

Each workload is timed ``--repeat`` times (default 5) and the median is reported, so
one slow run (another program, thermal throttling) does not decide the result. Run
both versions on the same machine in the same session when comparing engines; the
absolute numbers depend on the computer.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    import cyberliquidity
except ImportError:  # running from a fresh checkout without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))
    import cyberliquidity

from cyberliquidity import simulate
from cyberliquidity.analysis import plans_from_candidates, robustness
from cyberliquidity.compare import compare
from cyberliquidity.generator import random_scenario
from cyberliquidity.models import Scenario
from cyberliquidity.sweep import liquidity_backstop
from cyberliquidity.variants import replace, stress


def _load(name: str) -> Scenario:
    return Scenario.model_validate_json((ROOT / "scenarios" / name).read_text(encoding="utf-8"))


def _app_compare(s: Scenario) -> None:
    ivs = list(s.candidate_interventions)
    liquidity = {"liquidity_buffer", "credit_line"}
    pairs = [(a, b) for i, a in enumerate(ivs) for b in ivs[i + 1:] if {a.type, b.type} != liquidity]
    compare(stress(s), ivs, packages=pairs)


def _robust(s: Scenario) -> None:
    robustness(replace(s, disruptions=(), interventions=()), s.disruptions, plans_from_candidates(s))


def workloads() -> dict[str, Callable[[], None]]:
    default, cluster = _load("default_72h.json"), _load("cluster_gridlock_48h.json")
    large = [random_scenario(10_000 + seed, participants=40, services=12, routes=6, obligations=800, horizon=72)
             for seed in range(6)]
    return {
        "simulate: default scenario, stress run": lambda: simulate(stress(default)),
        "app: compare all interventions and pairs (default)": lambda: _app_compare(default),
        "app: compare all interventions and pairs (cluster)": lambda: _app_compare(cluster),
        "app: exact liquidity backstop (default)": lambda: liquidity_backstop(stress(default)),
        "app: robustness check, 10 variants (default)": lambda: _robust(default),
        "app: robustness check, 10 variants (cluster)": lambda: _robust(cluster),
        "scale: 6 random networks, 40 firms, 800 payments": lambda: [simulate(x) for x in large],
    }


def run(repeat: int) -> dict:
    results = {}
    for name, fn in workloads().items():
        fn()  # warm-up (imports, caches)
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        results[name] = {"median_s": statistics.median(times), "min_s": min(times), "runs": repeat}
    return {
        "engine": cyberliquidity.__version__,
        "module": str(Path(cyberliquidity.__file__).parent.name),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "workloads": results,
        "total_median_s": sum(r["median_s"] for r in results.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", type=Path, help="also write the timings to this JSON file")
    args = parser.parse_args(argv)
    report = run(args.repeat)
    width = max(len(n) for n in report["workloads"])
    print(f"cyberliquidity {report['engine']} · Python {report['python']} · {report['machine']} · "
          f"median of {args.repeat}")
    for name, r in report["workloads"].items():
        print(f"  {name:<{width}}  {r['median_s'] * 1000:9.1f} ms")
    print(f"  {'total':<{width}}  {report['total_median_s'] * 1000:9.1f} ms")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
