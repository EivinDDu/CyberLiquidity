"""Command-line interface: validate, run, compare and sweep scenarios without the app.

    cyberliquidity validate scenarios/default_72h.json
    cyberliquidity run scenarios/default_72h.json --out runs/default
    cyberliquidity compare scenarios/default_72h.json --out runs/compare
    cyberliquidity sweep scenarios/default_72h.json --participants oakridge,silverfern --grid 0,5000,10000
    cyberliquidity brief scenarios/default_72h.json                    # plain-language answer
    cyberliquidity backstop scenarios/default_72h.json                 # exact liquidity backstop
    cyberliquidity criticality scenarios/default_72h.json --start 32 --duration 12
    cyberliquidity profile scenarios/default_72h.json --service cloud_nimbus --start 32
    cyberliquidity generate --seed 7 --participants 40 --obligations 800 --out runs/random_7.json
    cyberliquidity generate-default --out scenarios/default_72h.json
    cyberliquidity generate-default --which cluster --out scenarios/cluster_gridlock_48h.json
    cyberliquidity robustness scenarios/default_72h.json               # does the recommendation survive?
    cyberliquidity schema --out docs/scenario.schema.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compare import compare
from .engine import simulate
from .export import bundle_zip, export_run, rows_to_csv, save_scenario, scenario_digest
from .metrics import HEADLINE_METRICS, summarize
from .models import DISCLAIMER, Scenario
from .safety import CLI_LIMITS, Limits, ScenarioInputError, parse_scenario
from .sweep import OUTCOMES, buffer_sweep, liquidity_backstop
from .variants import stress


def _format(value, kind: str, scenario: Scenario) -> str:
    if value is None:
        return "n/a"
    if kind == "money":
        return scenario.currency.format(value)
    if kind == "money_hours":
        return scenario.currency.format(value) + "·h"
    if kind == "hours" and isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _print_summary(summary: dict, scenario: Scenario) -> None:
    for key, label, kind in HEADLINE_METRICS:
        print(f"  {label:<55} {_format(summary.get(key), kind, scenario)}")
    print(f"  {'Cash conserved within the accounting boundary':<55} {summary['conservation_ok']}")


_limits: Limits | None = CLI_LIMITS  # --allow-large switches the size and complexity checks off


def _load(path: str) -> Scenario:
    """Read and validate a scenario file; explain any problem in plain language and exit with status 2."""
    file = Path(path)
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        _refuse(path, f"There is no file at {path}", ["check the path (built-in scenarios are in scenarios/)"])
    except IsADirectoryError:
        _refuse(path, f"{path} is a folder, not a scenario file", [])
    except PermissionError:
        _refuse(path, f"{path} cannot be read (permission denied)", [])
    try:
        return parse_scenario(data, _limits, source=file.name)
    except ScenarioInputError as exc:
        hint = ["run with --allow-large to simulate it anyway"] if "larger than" in exc.title else []
        _refuse(path, exc.title, exc.problems + hint)


def _refuse(path: str, title: str, problems: list[str]):
    print(f"Scenario not loaded: {title}.", file=sys.stderr)
    for line in problems:
        print(f"  - {line}", file=sys.stderr)
    raise SystemExit(2)


def cmd_validate(args: argparse.Namespace) -> int:
    scenario = _load(args.scenario)
    print(f"OK: {scenario.id} - {len(scenario.services)} services, {len(scenario.routes)} routes, "
          f"{len(scenario.participants)} participants, {len(scenario.obligations)} obligations.")
    print(f"scenario digest {scenario_digest(scenario)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    scenario = _load(args.scenario)
    result = simulate(scenario)
    print(DISCLAIMER)
    print(f"Scenario {scenario.id} (digest {scenario_digest(scenario)[:16]}...)")
    _print_summary(summarize(result), scenario)
    print(f"results digest {result.digest()}")
    if args.out:
        print(f"exported to {export_run(result, args.out, label=scenario.id)}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    scenario = _load(args.scenario)
    comparison = compare(scenario)
    print(DISCLAIMER)
    header = f"  {'metric':<55}" + "".join(f"{key:>22}" for key in comparison.runs)
    print(header)
    for key, label, kind in HEADLINE_METRICS:
        cells = "".join(f"{_format(comparison.summaries[k].get(key), kind, scenario):>22}" for k in comparison.runs)
        print(f"  {label:<55}{cells}")
    for key in comparison.intervention_keys():
        print(f"\n{comparison.labels[key]}")
        for line in comparison.effects[key]:
            print(f"  - {line}")
    print("\nRanking by overdue value x hours (this scenario and these assumptions only):")
    for row in comparison.ranking("overdue_value_hours"):
        print(f"  {row['rank']}. {row['label']}: {_format(row['value'], 'money_hours', scenario)}")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        archive = out / "bundle.zip"
        archive.write_bytes(bundle_zip(comparison.runs, comparison.labels))
        for key, result in comparison.runs.items():
            export_run(result, out / key.replace(":", "_"), label=comparison.labels[key])
        print(f"\nexported to {out} (and {archive})")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    scenario = _load(args.scenario)
    grid = [scenario.currency.to_minor(float(x)) for x in args.grid.split(",") if x.strip()]
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    result = buffer_sweep(stress(scenario), participants, grid, outcome=args.outcome)
    money = scenario.currency.format
    print(DISCLAIMER)
    print(f"Outcome: {result.outcome_label}")
    for p in result.points:
        s = p.summary
        print(f"  {money(p.amount_each):>18} each  achieved={str(p.achieved):<5}  delayed={s['delayed_count']:>4}  "
              f"value-hours={money(s['overdue_value_hours'])}")
    print(result.verdict(money))
    if result.route_limited:
        print(f"Route-limited (not solvable by liquidity): {len(result.route_limited)} obligation(s)")
    return 0


def _pairs(items: list) -> list[tuple]:
    return [(a, b) for i, a in enumerate(items) for b in items[i + 1:]]


def cmd_brief(args: argparse.Namespace) -> int:
    from .analysis import plans_from_candidates, robustness
    from .briefing import briefing
    from .compare import INTERVENTION_LABELS
    from .variants import replace

    scenario = _load(args.scenario)
    candidates = list(scenario.candidate_interventions)
    comparison = compare(scenario, candidates, packages=_pairs(candidates))
    backstop = liquidity_backstop(stress(scenario))
    robust = None
    if scenario.disruptions and candidates:
        robust = robustness(replace(scenario, disruptions=(), interventions=()), scenario.disruptions,
                            plans_from_candidates(scenario), metric=args.metric,
                            labels={iv.id: INTERVENTION_LABELS[iv.type] for iv in candidates})
    print("\n\n".join(briefing(comparison, args.metric, backstop=backstop, robust=robust)))
    return 0


def cmd_backstop(args: argparse.Namespace) -> int:
    scenario = _load(args.scenario)
    result = liquidity_backstop(stress(scenario))
    money = scenario.currency.format
    names = scenario.names()
    print(DISCLAIMER)
    print(f"{'participant':<28}{'pre-funding needed':>22}{'credit-line peak':>20}{'first draw h':>14}{'tight':>8}")
    for row in result.rows:
        print(f"{names[row['participant']]:<28}{money(row['allocation']):>22}{money(row['peak_draw']):>20}"
              f"{row['first_draw_hour']!s:>14}{ {True: 'yes', False: 'no', None: '-'}[row['tight']]:>8}")
    print(result.verdict(money))
    print(f"({result.runs_used} simulation runs)")
    return 0


def _write_or_print(rows: list[dict], out: str | None, columns: list[str]) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(rows_to_csv(rows), encoding="utf-8")
        print(f"wrote {out}")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def cmd_criticality(args: argparse.Namespace) -> int:
    from .analysis import criticality_scan

    scenario = _load(args.scenario)
    rows = criticality_scan(scenario, args.start, args.duration, args.severity, backup_delay_hours=args.backup_delay)
    money = scenario.currency.format
    print(DISCLAIMER)
    print(f"Every service in turn: {args.severity}% capacity lost from hour {args.start} for {args.duration} h. "
          "Worst first, by overdue value x hours.")
    table = [{
        "service": r["service"], "routes_down": len(r["routes_unavailable"]), "delayed": r["delayed_count"],
        "value_hours": money(r["overdue_value_hours"]), "cash_short": r["participants_cash_short_count"],
        "contagion_steps": r["max_generation"] if r["max_generation"] is not None else "-",
        "best_intervention": r.get("best_intervention") or ("none helps" if r["overdue_value_hours"] else "-"),
        "best_cut": f"{r['best_reduction_pct']:.0f}%" if "best_reduction_pct" in r else "-",
        "backup_independent": r.get("backup_independent", "-"),
    } for r in rows]
    _write_or_print(table if not args.out else rows, args.out, list(table[0]))
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .analysis import duration_profile

    scenario = _load(args.scenario)
    durations = [int(x) for x in args.durations.split(",") if x.strip()]
    profile = duration_profile(scenario, args.service, args.start, durations, args.severity, outcome=args.outcome)
    money = scenario.currency.format
    print(DISCLAIMER)
    print(f"{args.service}: outage from hour {args.start}; outcome: {OUTCOMES[args.outcome]}")
    table = [{"duration_h": r["duration_h"], "delayed": r["delayed_count"], "value_hours": money(r["overdue_value_hours"]),
              "unsettled": r["unsettled_count"], "outcome": "achieved" if r["outcome_achieved"] else "breached"}
             for r in profile["rows"]]
    _write_or_print(table if not args.out else profile["rows"], args.out, list(table[0]))
    tol = profile["tolerance"]["none"]
    print(f"Longest tested outage with the outcome intact: {tol['longest_tolerated_h']} h; "
          f"first breach at {tol['first_breach_h']} h (tested: {', '.join(map(str, tol['tested']))}).")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from .generator import random_scenario

    scenario = random_scenario(args.seed, participants=args.participants, services=args.services, routes=args.routes,
                               obligations=args.obligations, horizon=args.horizon)
    save_scenario(scenario, args.out)
    print(f"wrote {args.out} ({len(scenario.participants)} participants, {len(scenario.obligations)} obligations, "
          f"digest {scenario_digest(scenario)[:16]}...)")
    return 0


BUILT_IN = {"default": "scenarios/default_72h.json", "cluster": "scenarios/cluster_gridlock_48h.json"}


def cmd_generate_default(args: argparse.Namespace) -> int:
    if args.which == "cluster":
        from .synthetic_cluster import build_cluster_scenario as build
    else:
        from .synthetic import build_default_scenario as build

    scenario = build()
    out = args.out or BUILT_IN[args.which]
    save_scenario(scenario, out)
    args.out = out
    print(f"wrote {args.out} ({len(scenario.obligations)} obligations, digest {scenario_digest(scenario)[:16]}...)")
    return 0


def cmd_robustness(args: argparse.Namespace) -> int:
    from .analysis import plans_from_candidates, robustness
    from .briefing import robustness_paragraph
    from .compare import INTERVENTION_LABELS
    from .variants import replace

    scenario = _load(args.scenario)
    if not scenario.disruptions or not scenario.candidate_interventions:
        print("The scenario needs at least one disruption and one candidate intervention.", file=sys.stderr)
        return 2
    labels = {iv.id: f"{INTERVENTION_LABELS[iv.type]} ({iv.id})" for iv in scenario.candidate_interventions}
    result = robustness(replace(scenario, disruptions=(), interventions=()), scenario.disruptions,
                        plans_from_candidates(scenario), metric=args.metric, labels=labels)
    money = scenario.currency.format
    rows = []
    for v in result["variants"]:
        row = {"variant": v["label"], "stress": money(v["stress_value"]), "best": "; ".join(v["best"])}
        for key in result["plans"]:
            pct = v["reduction_pct"].get(key)
            row[key] = "–" if pct is None else f"{pct:.0f}%"
        rows.append(row)
    if args.out:
        _write_or_print([
            {"variant": v["key"], "label": v["label"], "stress_value": v["stress_value"],
             **{f"{k}_value": v["values"].get(k) for k in result["plans"]},
             **{f"{k}_regret": v["regret"].get(k) for k in result["plans"]}, "best": ";".join(v["best"])}
            for v in result["variants"]], args.out, [])
    else:
        print(DISCLAIMER)
        print(f"Reduction of {args.metric} against each variant's own stress run (no intervention):")
        _write_or_print(rows, None, list(rows[0]))
    text = robustness_paragraph(result)
    if text:
        print("\n" + text.replace("**", ""))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    schema = json.dumps(Scenario.model_json_schema(), indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(schema, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(schema)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyberliquidity", description=DISCLAIMER)
    parser.add_argument("--allow-large", action="store_true",
                        help="skip the size and complexity limits for scenario files (see cyberliquidity.safety)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate a scenario JSON file")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="simulate a scenario exactly as specified")
    p.add_argument("scenario")
    p.add_argument("--out", help="directory for CSV/JSON exports")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="baseline vs stress vs each candidate intervention")
    p.add_argument("scenario")
    p.add_argument("--out", help="directory for exports and bundle.zip")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("sweep", help="liquidity buffer grid search on the stressed scenario")
    p.add_argument("scenario")
    p.add_argument("--participants", required=True, help="comma-separated participant ids")
    p.add_argument("--grid", required=True, help="comma-separated buffer amounts per participant (major units)")
    p.add_argument("--outcome", default="no_cash_blocking", choices=sorted(OUTCOMES))
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("brief", help="plain-language answer: delays, who runs short of cash, best intervention")
    p.add_argument("scenario")
    p.add_argument("--metric", default="overdue_value_hours")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("backstop", help="exact per-participant pre-funding that makes cash non-binding")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_backstop)

    p = sub.add_parser("criticality", help="the same outage applied to every service in turn")
    p.add_argument("scenario")
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--duration", type=int, required=True)
    p.add_argument("--severity", type=int, default=100)
    p.add_argument("--backup-delay", type=int, default=3, help="activation delay of the suggested backup (hours)")
    p.add_argument("--out", help="write the full table as CSV")
    p.set_defaults(func=cmd_criticality)

    p = sub.add_parser("profile", help="one service, growing outage durations: longest tolerated outage")
    p.add_argument("scenario")
    p.add_argument("--service", required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--durations", default="1,2,3,4,6,8,10,12,16,20,24,30,36")
    p.add_argument("--severity", type=int, default=100)
    p.add_argument("--outcome", default="priority_on_time", choices=sorted(OUTCOMES))
    p.add_argument("--out", help="write the full table as CSV")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("robustness", help="does the best candidate intervention stay best when assumptions change?")
    p.add_argument("scenario")
    p.add_argument("--metric", default="overdue_value_hours")
    p.add_argument("--out", help="write the per-variant table as CSV")
    p.set_defaults(func=cmd_robustness)

    p = sub.add_parser("generate", help="seeded random structural scenario (testing and scale)")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--participants", type=int, default=8)
    p.add_argument("--services", type=int, default=6)
    p.add_argument("--routes", type=int, default=3)
    p.add_argument("--obligations", type=int, default=60)
    p.add_argument("--horizon", type=int, default=24)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("generate-default", help="regenerate a built-in synthetic scenario")
    p.add_argument("--which", choices=sorted(BUILT_IN), default="default")
    p.add_argument("--out", help="default: the committed fixture for --which")
    p.set_defaults(func=cmd_generate_default)

    p = sub.add_parser("schema", help="print or write the scenario JSON Schema")
    p.add_argument("--out")
    p.set_defaults(func=cmd_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    global _limits
    args = build_parser().parse_args(argv)
    _limits = None if args.allow_large else CLI_LIMITS
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
