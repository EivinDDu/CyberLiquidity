"""Research experiment: when does operational redundancy reduce payment disruption more
effectively than additional liquidity, and when does the ranking reverse?

The synthetic payment schedule (scenarios/default_72h.json: same obligations, balances
and network) is held constant. The experiment varies only

* the disrupted service (three cases with different transmission mechanisms),
* the outage duration,
* the intervention (none, backup route, pre-funded buffer at two sizes, an on-demand
  credit line, faster recovery, and backup + buffer together),
* two engine assumptions, as a robustness check (queue ordering, within-step recycling),
* the outage start hour, as a second robustness check (alignment with the calendar).

It also computes the exact liquidity backstop of every stressed run (the smallest
pre-funding that reproduces the unlimited-liquidity outcome, re-checked by simulation).

Everything is deterministic: there is no sampling and no Monte Carlo. Findings are
properties of this model under these assumptions, not statements about real systems.

    python experiments/redundancy_vs_liquidity.py            # writes experiments/output/redundancy_vs_liquidity/
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cyberliquidity import DISCLAIMER, ENGINE_VERSION, simulate
from cyberliquidity.compare import interaction
from cyberliquidity.engine import CATEGORIES
from cyberliquidity.export import environment_info, load_scenario, scenario_digest
from cyberliquidity.metrics import mechanism_shares, summarize
from cyberliquidity.models import Scenario
from cyberliquidity.sweep import liquidity_backstop
from cyberliquidity.variants import (
    make_backup,
    make_buffer,
    make_credit_line,
    make_disruption,
    make_faster_recovery,
    replace,
    suggest_backup,
    with_intervention,
    with_settings,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "scenarios" / "default_72h.json"
DEFAULT_OUT = ROOT / "experiments" / "output" / "redundancy_vs_liquidity"


@dataclass(frozen=True)
class Case:
    id: str
    label: str
    service: str
    start_hour: int


CASES = (
    Case("stratus_outage", "Case 1: Stratus Cloud outage (PSP Beta unavailable)", "cloud_stratus", 32),
    Case("hv_channel_outage", "Case 2: North High-Value Channel outage", "portal_north", 31),
    Case("idp_outage", "Case 3: KeyGate identity provider outage (common mode)", "idp_keygate", 32),
)
DURATIONS = (2, 4, 6, 8, 12, 16, 24, 36)
BACKUP_DELAY_H = 3
BUFFER_SIZES_EUR = (5_000, 20_000)  # per participant, paid to *every* participant at hour 0 (untargeted)
CREDIT_LIMIT_EUR = 20_000  # per participant, available from hour 0, drawn only when needed
RECOVERY_FRACTION = 0.5  # faster recovery halves the outage (rounded down to whole hours)
SETTINGS = {
    "default": {},
    "fifo_ordering": {"ordering": "due_id"},
    "no_within_step_recycling": {"liquidity_recycling": "next_step"},
}
START_OFFSETS = (-6, -3, 0, 3, 6)  # hours relative to each case's start, default settings only
METRIC = "overdue_value_hours"
MATERIALITY = 0.01  # backup vs buffer differences below 1% of the no-intervention disruption count as "practically tied"
LARGEST_BUFFER = f"buffer_{max(BUFFER_SIZES_EUR) // 1000}k"
PACKAGE = f"backup+{LARGEST_BUFFER}"

LABELS = {
    "none": "No intervention",
    "backup_route": f"Backup provider ({BACKUP_DELAY_H} h activation)",
    "faster_recovery": "Faster recovery (outage halved)",
    "credit_20k": f"Credit line EUR {CREDIT_LIMIT_EUR // 1000}k per participant (on demand)",
    PACKAGE: f"Backup + buffer EUR {max(BUFFER_SIZES_EUR) // 1000}k",
}
for size in BUFFER_SIZES_EUR:
    LABELS[f"buffer_{size // 1000}k"] = f"Liquidity buffer EUR {size // 1000}k per participant"
SINGLES = ("backup_route", "buffer_5k", "buffer_20k", "credit_20k", "faster_recovery")


def interventions_for(base: Scenario, case: Case, duration: int, start_hour: int | None = None) -> dict:
    start = case.start_hour if start_hour is None else start_hour
    disruption = make_disruption(case.service, start, duration, id="D1")
    suggestion = suggest_backup(base, disruption)
    participants = [p.id for p in base.participants]
    backup = make_backup(suggestion.primary_routes, suggestion.backup_route,
                         min(start + BACKUP_DELAY_H, base.horizon_hours - 1), id="backup")
    out: dict = {"none": None, "backup_route": backup}
    for size in BUFFER_SIZES_EUR:
        out[f"buffer_{size // 1000}k"] = make_buffer(participants, base.currency.to_minor(size), 0, id=f"buffer_{size}")
    out["credit_20k"] = make_credit_line(participants, base.currency.to_minor(CREDIT_LIMIT_EUR), 0, id="credit_line")
    reduction = int(duration * RECOVERY_FRACTION)
    if reduction >= 1:
        out["faster_recovery"] = make_faster_recovery("D1", reduction)
    out[PACKAGE] = [backup, out[LARGEST_BUFFER]]
    return out


def _apply(stressed: Scenario, iv) -> Scenario:
    if iv is None:
        return stressed
    return with_intervention(stressed, *(iv if isinstance(iv, list) else [iv]))


def _spec(iv):
    if iv is None:
        return None
    return [x.model_dump(mode="json") for x in iv] if isinstance(iv, list) else iv.model_dump(mode="json")


def run(scenario_path: Path, out_dir: Path) -> dict:
    base = load_scenario(scenario_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, specs = [], []
    for case in CASES:
        for duration in DURATIONS:
            disruption = make_disruption(case.service, case.start_hour, duration, id="D1")
            stressed = replace(base, disruptions=(disruption,), interventions=())
            for name, iv in interventions_for(base, case, duration).items():
                for setting, overrides in SETTINGS.items():
                    scenario = _apply(stressed, iv)
                    if overrides:
                        scenario = with_settings(scenario, **overrides)
                    result = simulate(scenario)
                    m = summarize(result)
                    shares = mechanism_shares(m)
                    row = {
                        "case": case.id, "service": case.service, "start_hour": case.start_hour,
                        "duration_h": duration, "intervention": name, "setting": setting,
                        "overdue_value_hours_minor": m["overdue_value_hours"],
                        "delayed_count": m["delayed_count"], "delayed_value_minor": m["delayed_value"],
                        "unsettled_count": m["unsettled_count"], "unsettled_value_minor": m["unsettled_value"],
                        "peak_overdue_value_minor": m["peak_overdue_value"],
                        "blocked_service_unavailable_count": m["blocked_service_unavailable_count"],
                        "blocked_capacity_count": m["blocked_capacity_count"],
                        "blocked_cash_count": m["blocked_cash_count"],
                        "settled_via_backup_count": m["settled_via_backup_count"],
                        "intervention_funding_minor": m["intervention_funding"],
                        "credit_peak_total_minor": m["credit_peak_total"],
                        "direct_operational_count": m["mechanisms"]["direct_operational"]["count"],
                        "financial_propagation_count": m["mechanisms"]["financial_propagation"]["count"],
                        "mixed_count": m["mechanisms"]["mixed"]["count"],
                        **{f"share_{c}": (round(shares[c], 6) if shares[c] is not None else "") for c in CATEGORIES},
                        "scenario_digest": scenario_digest(scenario),
                        "results_digest": result.digest(),
                    }
                    rows.append(row)
                    specs.append({"case": case.id, "duration_h": duration, "intervention": name, "setting": setting,
                                  "disruption": disruption.model_dump(mode="json"), "intervention_spec": _spec(iv),
                                  "settings": scenario.settings.model_dump(mode="json"),
                                  "scenario_digest": row["scenario_digest"]})

    offsets = []
    for case in CASES:
        for offset in START_OFFSETS:
            start = case.start_hour + offset
            for duration in DURATIONS:
                stressed = replace(base, disruptions=(make_disruption(case.service, start, duration, id="D1"),), interventions=())
                plans = interventions_for(base, case, duration, start_hour=start)
                values = {name: summarize(simulate(_apply(stressed, plans[name])))[METRIC]
                          for name in ("none", "backup_route", LARGEST_BUFFER)}
                offsets.append({"case": case.id, "start_offset_h": offset, "start_hour": start, "duration_h": duration,
                                "stress_value_hours_minor": values["none"],
                                "backup_value_hours_minor": values["backup_route"],
                                "buffer_value_hours_minor": values[LARGEST_BUFFER],
                                "verdict": _compare(values["backup_route"], values[LARGEST_BUFFER], values["none"])})

    backstops = []
    for case in CASES:
        for duration in DURATIONS:
            stressed = replace(base, disruptions=(make_disruption(case.service, case.start_hour, duration, id="D1"),),
                               interventions=())
            b = liquidity_backstop(stressed)
            backstops.append({"case": case.id, "duration_h": duration,
                              "prefunding_needed_minor": b.total_allocation, "participants_funded": len(b.allocation),
                              "credit_line_peaks_minor": b.total_peak, "verified_sufficient": b.verified_sufficient,
                              "all_tight": b.all_tight, "route_limited_count": b.route_limited_count,
                              "route_limited_value_minor": b.route_limited_value, "runs_used": b.runs_used,
                              "allocation": ";".join(f"{p}={v}" for p, v in b.allocation.items())})

    _write_csv(out_dir / "results.csv", rows)
    config = {
        "experiment": "redundancy_vs_liquidity",
        "question": "When does operational redundancy reduce payment disruption more effectively than additional "
                    "liquidity, and when does the ranking reverse?",
        "disclaimer": DISCLAIMER,
        "base_scenario": str(scenario_path.relative_to(ROOT)) if scenario_path.is_relative_to(ROOT) else str(scenario_path),
        "base_scenario_digest": scenario_digest(base),
        "engine_version": ENGINE_VERSION,
        "environment": environment_info(),
        "metric_for_ranking": METRIC,
        "cases": [asdict(c) for c in CASES],
        "durations_h": list(DURATIONS),
        "backup_activation_delay_h": BACKUP_DELAY_H,
        "buffer_sizes_eur_per_participant": list(BUFFER_SIZES_EUR),
        "buffer_recipients": "all participants, funded at hour 0",
        "credit_limit_eur_per_participant": CREDIT_LIMIT_EUR,
        "credit_recipients": "all participants, available from hour 0, drawn on demand, repaid from end-of-hour cash",
        "faster_recovery_fraction": RECOVERY_FRACTION,
        "package": PACKAGE,
        "settings": SETTINGS,
        "start_offsets_h": list(START_OFFSETS),
        "materiality_of_backup_vs_buffer_verdicts": f"differences below {MATERIALITY:.0%} of the no-intervention value count as practically tied",
        "runs": specs,
        "reproduce": "python experiments/redundancy_vs_liquidity.py  (each run's scenario_digest/results_digest must match)",
    }
    (out_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    analysis = analyse(rows, offsets, backstops)
    _write_csv(out_dir / "rankings.csv", analysis["rankings"])
    _write_csv(out_dir / "mechanism_split.csv", analysis["mechanisms"])
    _write_csv(out_dir / "start_offsets.csv", offsets)
    _write_csv(out_dir / "backstop.csv", backstops)
    _write_csv(out_dir / "complementarity.csv", analysis["complementarity"])
    (out_dir / "findings.md").write_text(findings_markdown(analysis, base, config), encoding="utf-8")
    plot(analysis, out_dir / "comparison.png")
    return analysis


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _compare(backup: int, buffer: int, stress: int) -> str:
    """Verdict for backup vs buffer; differences below MATERIALITY x the stress value count as practically tied."""
    if stress == 0:
        return "no disruption"
    if abs(backup - buffer) <= MATERIALITY * stress:
        return "practically tied"
    return "backup better" if backup < buffer else "buffer better"


def analyse(rows: list[dict], offsets: list[dict], backstops: list[dict]) -> dict:
    index = {(r["case"], r["setting"], r["duration_h"], r["intervention"]): r for r in rows}
    names = [n for n in LABELS if n != "none"]
    rankings, mechanisms, reversals, robustness, complementarity = [], [], [], [], []
    for case in CASES:
        for setting in SETTINGS:
            for d in DURATIONS:
                stress = index[(case.id, setting, d, "none")][f"{METRIC}_minor"]
                values = {n: index[(case.id, setting, d, n)][f"{METRIC}_minor"] for n in names
                          if (case.id, setting, d, n) in index}
                singles = {n: v for n, v in values.items() if n in SINGLES}
                order = sorted(singles, key=lambda n: (singles[n], n))
                ranking_text = order[0] if order else ""
                for prev, cur_name in zip(order, order[1:]):
                    ranking_text += (" = " if singles[cur_name] == singles[prev] else " > ") + cur_name
                reductions = {n: (stress - v) / stress * 100 if stress else 0.0 for n, v in values.items()}
                rankings.append({
                    "case": case.id, "setting": setting, "duration_h": d, "stress_value_hours_minor": stress,
                    "ranking": ranking_text,
                    **{f"reduction_pct_{n}": round(reductions.get(n, float("nan")), 1) for n in names},
                    "backup_vs_largest_buffer": _compare(values["backup_route"], values[LARGEST_BUFFER], stress),
                    "credit_equals_largest_buffer": values["credit_20k"] == values[LARGEST_BUFFER],
                })
        for d in DURATIONS:
            r = index[(case.id, "default", d, "none")]
            mechanisms.append({
                "case": case.id, "duration_h": d,
                **{f"share_{c}": r[f"share_{c}"] for c in CATEGORIES},
                "cash_only_share": round(sum(float(r[f"share_{c}"] or 0) for c in ("cash_awaiting_inflows", "cash_other")), 4),
                "direct_operational_count": r["direct_operational_count"],
                "financial_propagation_count": r["financial_propagation_count"],
                "mixed_count": r["mixed_count"], "delayed_count": r["delayed_count"],
            })
            summaries = {"B": {METRIC: r[f"{METRIC}_minor"]},
                         "C:backup": {METRIC: index[(case.id, "default", d, "backup_route")][f"{METRIC}_minor"]},
                         "C:buffer": {METRIC: index[(case.id, "default", d, LARGEST_BUFFER)][f"{METRIC}_minor"]},
                         "P:both": {METRIC: index[(case.id, "default", d, PACKAGE)][f"{METRIC}_minor"]}}
            x = interaction(summaries, METRIC, ["C:backup", "C:buffer"], "P:both")
            complementarity.append({
                "case": case.id, "duration_h": d,
                "backup_pct": round(x["separate_pct"].get("C:backup", 0.0), 1) if x["separate_pct"] else "",
                "buffer_pct": round(x["separate_pct"].get("C:buffer", 0.0), 1) if x["separate_pct"] else "",
                "together_pct": round(x["combined_pct"], 1) if x["combined_pct"] is not None else "",
                "independent_pct": round(x["independent_pct"], 1) if x["independent_pct"] is not None else "",
                "excess_pp": round(x["excess_pp"], 1) if x["excess_pp"] is not None else "",
                "verdict": x["verdict"],
            })
        for size in BUFFER_SIZES_EUR:
            buffer = f"buffer_{size // 1000}k"
            seq = [(d, _compare(index[(case.id, "default", d, "backup_route")][f"{METRIC}_minor"],
                                index[(case.id, "default", d, buffer)][f"{METRIC}_minor"],
                                index[(case.id, "default", d, "none")][f"{METRIC}_minor"])) for d in DURATIONS]
            for (d0, a), (d1, b) in zip(seq, seq[1:]):
                if a != b:
                    reversals.append({"case": case.id, "buffer": buffer, "between_h": (d0, d1), "from": a, "to": b})
        for d in DURATIONS:
            verdicts = {s: _compare(index[(case.id, s, d, "backup_route")][f"{METRIC}_minor"],
                                    index[(case.id, s, d, LARGEST_BUFFER)][f"{METRIC}_minor"],
                                    index[(case.id, s, d, "none")][f"{METRIC}_minor"]) for s in SETTINGS}
            robustness.append({"case": case.id, "duration_h": d, **verdicts, "stable": len(set(verdicts.values())) == 1})
    offset_stability = []
    for case in CASES:
        for d in DURATIONS:
            verdicts = {o["start_offset_h"]: o["verdict"] for o in offsets if o["case"] == case.id and o["duration_h"] == d}
            informative = {k: v for k, v in verdicts.items() if v != "no disruption"}
            offset_stability.append({"case": case.id, "duration_h": d, **{f"offset_{k:+d}": v for k, v in verdicts.items()},
                                     "stable": len(set(informative.values())) <= 1})
    return {"rankings": rankings, "mechanisms": mechanisms, "reversals": reversals, "robustness": robustness,
            "complementarity": complementarity, "offsets": offsets, "offset_stability": offset_stability,
            "backstops": backstops, "index": index, "names": names}


def findings_markdown(a: dict, base: Scenario, config: dict) -> str:
    cur = base.currency
    eur = lambda minor: cur.format(minor, with_code=False)  # noqa: E731
    lines = [
        "# Redundancy vs liquidity: generated findings",
        "",
        f"> {DISCLAIMER} Generated by `experiments/redundancy_vs_liquidity.py`; do not edit by hand.",
        "",
        f"Base scenario `{config['base_scenario']}` (digest `{config['base_scenario_digest'][:16]}…`), engine "
        f"{config['engine_version']}. Ranking metric: overdue value × hours (lower = less disruption). Buffers go to "
        f"all {len(base.participants)} participants at hour 0; the credit line gives every participant an on-demand "
        f"facility of EUR {CREDIT_LIMIT_EUR:,} from hour 0; the backup activates {BACKUP_DELAY_H} h after the outage "
        "starts; faster recovery halves the outage.",
        "",
        "## 1. Which mechanism dominates each case (stress runs, default settings)",
        "",
        "Share of overdue value × hours by blocking category; counts of delayed obligations by mechanism.",
        "",
        "| Case | Outage (h) | Operational only | Operational + cash | Cash, awaiting overdue inflows | Cash, other | Direct / propagated / mixed (count) |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for m in a["mechanisms"]:
        def pct(key: str) -> str:
            value = m[key]
            return f"{float(value) * 100:.0f}%" if value not in ("", None) else "-"
        lines.append(f"| {m['case']} | {m['duration_h']} | {pct('share_operational_only')} | {pct('share_operational_and_cash')} | "
                     f"{pct('share_cash_awaiting_inflows')} | {pct('share_cash_other')} | "
                     f"{m['direct_operational_count']} / {m['financial_propagation_count']} / {m['mixed_count']} |")
    lines += ["", "## 2. Reduction in overdue value × hours versus no intervention (default settings)", ""]
    header = "| Case | Outage (h) | No intervention (EUR·h) | " + " | ".join(LABELS[n] for n in a["names"]) + " |"
    lines += [header, "|---|---:|---:|" + "---:|" * len(a["names"])]
    for r in a["rankings"]:
        if r["setting"] != "default":
            continue
        cells = " | ".join(f"{r[f'reduction_pct_{n}']:.0f}%" for n in a["names"])
        lines.append(f"| {r['case']} | {r['duration_h']} | {eur(r['stress_value_hours_minor'])} | {cells} |")
    same = all(r["credit_equals_largest_buffer"] for r in a["rankings"])
    lines += ["", f"Credit line and EUR {max(BUFFER_SIZES_EUR):,} buffer give identical disruption in "
              + ("every" if same else "**not every**") + " case, duration and setting (a model property: same limit, "
              "same hour). They differ only in the liquidity used; see `results.csv` (`credit_peak_total_minor` vs "
              "`intervention_funding_minor`).", "",
              "## 3. Where redundancy and liquidity swap places (default settings)", ""]
    if a["reversals"]:
        for rv in a["reversals"]:
            lines.append(f"- {rv['case']}, {LABELS[rv['buffer']]}: between {rv['between_h'][0]} h and {rv['between_h'][1]} h "
                         f"the comparison changes from *{rv['from']}* to *{rv['to']}*.")
    else:
        lines.append("- No reversal between the backup route and either buffer size over the tested durations.")
    lines += ["", "Full order of the single interventions per duration (best first):", ""]
    for r in a["rankings"]:
        if r["setting"] == "default":
            lines.append(f"- {r['case']}, {r['duration_h']} h: {r['ranking']}")
    lines += ["", f"## 4. Do the backup and the EUR {max(BUFFER_SIZES_EUR) // 1000}k buffer complement each other?", "",
              "Independence benchmark: together they would remove 1 − (1 − a)(1 − b) if each removed its own share of what "
              "the other leaves.", "",
              "| Case | Outage (h) | Backup alone | Buffer alone | Together | If independent | Difference | Verdict |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
    for c in a["complementarity"]:
        def p(v):
            return "-" if v == "" else f"{v:.0f}%"
        diff = "-" if c["excess_pp"] == "" else f"{c['excess_pp']:+.1f} pp"
        lines.append(f"| {c['case']} | {c['duration_h']} | {p(c['backup_pct'])} | {p(c['buffer_pct'])} | {p(c['together_pct'])} | "
                     f"{p(c['independent_pct'])} | {diff} | {c['verdict']} |")
    lines += ["", f"## 5. Robustness: backup vs {LABELS[LARGEST_BUFFER]} under alternative assumptions", "",
              f"Verdicts treat differences below {MATERIALITY:.0%} of the no-intervention disruption as practically tied.", "",
              "| Case | Outage (h) | " + " | ".join(SETTINGS) + " | Same verdict? |", "|---|---:|" + "---|" * len(SETTINGS) + "---|"]
    for r in a["robustness"]:
        lines.append(f"| {r['case']} | {r['duration_h']} | " + " | ".join(r[s] for s in SETTINGS) + f" | {'yes' if r['stable'] else '**no**'} |")
    unstable = [r for r in a["robustness"] if not r["stable"]]
    lines += ["", f"{len(unstable)} of {len(a['robustness'])} case-duration combinations change verdict under an alternative "
              "ordering or recycling rule.", "",
              f"## 6. Robustness: the same comparison with the outage moved by {', '.join(f'{o:+d}' for o in START_OFFSETS)} h", "",
              "| Case | Outage (h) | " + " | ".join(f"{o:+d} h" for o in START_OFFSETS) + " | Same verdict? |",
              "|---|---:|" + "---|" * len(START_OFFSETS) + "---|"]
    for r in a["offset_stability"]:
        lines.append(f"| {r['case']} | {r['duration_h']} | " + " | ".join(r[f"offset_{o:+d}"] for o in START_OFFSETS)
                     + f" | {'yes' if r['stable'] else '**no**'} |")
    moved = [r for r in a["offset_stability"] if not r["stable"]]
    lines += ["", f"{len(moved)} of {len(a['offset_stability'])} case-duration combinations change verdict when only the start "
              "hour moves (\"no disruption\" cells are ignored).", "",
              "## 7. Exact liquidity backstop of each stressed run (default settings)", "",
              "Smallest hour-0 pre-funding per participant that reproduces the unlimited-liquidity outcome (found by "
              "search, re-checked by simulation), next to the peak draws on an unlimited credit line, and the delays that "
              "no liquidity removes.", "",
              "| Case | Outage (h) | Pre-funding needed (EUR) | Participants | Credit-line peaks (EUR) | Still delayed (route-limited) | Verified |",
              "|---|---:|---:|---:|---:|---:|---|"]
    for b in a["backstops"]:
        verified = "yes" if b["verified_sufficient"] and b["all_tight"] in (True, None) else "partly"
        lines.append(f"| {b['case']} | {b['duration_h']} | {eur(b['prefunding_needed_minor'])} | {b['participants_funded']} | "
                     f"{eur(b['credit_line_peaks_minor'])} | {b['route_limited_count']} | {verified} |")
    lines += ["", "## 8. Caveats", "",
              "- Results are properties of this synthetic network and calendar under the documented rules "
              "(docs/MODEL.md); they are not estimates for any real payment system.",
              "- Overdue value × hours measures how much payment value is late and for how long. It is not economic loss.",
              "- Outages that end near the 72 h horizon leave obligations unsettled; their delay is cut off at the horizon "
              "(right-censoring), which understates long-outage disruption for every intervention.",
              "- The buffer and the credit line are untargeted; the backstop shows how much less a targeted allocation needs.",
              "- The backup's benefit depends on its activation delay, its spare capacity and its independence from the "
              "failed service; all three are fixed here. Case 3 shows what happens when no independent backup exists.", ""]
    return "\n".join(lines)


def plot(a: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = {"backup_route": "#f47230", "faster_recovery": "#f2a600", "credit_20k": "#ee7fac", PACKAGE: "#3d3d3d"}
    buffer_names = [f"buffer_{s // 1000}k" for s in BUFFER_SIZES_EUR]
    for n in buffer_names:
        colours[n] = "#00b894"
    styles = {buffer_names[0]: dict(linestyle=(0, (4, 2)), marker="^", markerfacecolor="white"),
              "credit_20k": dict(linestyle=(0, (1, 1.5)), marker="s", markersize=4),
              PACKAGE: dict(linestyle="-", marker="D", markersize=4)}
    category_colours = {"operational_only": "#6a4ce2", "operational_and_cash": "#ee7fac",
                        "cash_awaiting_inflows": "#008a17", "cash_other": "#8c8c8c"}
    category_labels = {"operational_only": "Operational only", "operational_and_cash": "Operational + cash",
                       "cash_awaiting_inflows": "Cash, awaiting overdue inflows", "cash_other": "Cash, other"}
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9, "axes.edgecolor": "#c2c2c2",
                         "axes.labelcolor": "#666666", "xtick.color": "#666666", "ytick.color": "#666666",
                         "axes.titlesize": 10, "axes.titlecolor": "#191919"})
    fig, axes = plt.subplots(2, len(CASES), figsize=(5.2 * len(CASES), 7.8), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    rankings = [r for r in a["rankings"] if r["setting"] == "default"]
    shown = [n for n in a["names"] if n != "credit_20k"]  # identical to the 20k buffer by construction (noted below)
    for col, case in enumerate(CASES):
        ax = axes[0][col]
        for name in shown:
            ys = [r[f"reduction_pct_{name}"] for r in rankings if r["case"] == case.id]
            ax.plot(DURATIONS, ys, color=colours[name], linewidth=2, label=LABELS[name],
                    **({"marker": "o", "markersize": 5} | styles.get(name, {})))
        ax.set_title(case.label, loc="left")
        ax.set_ylim(-5, 105)
        if col == 0:
            ax.set_ylabel("Reduction in overdue value × hours vs no intervention (%)")
        ax.grid(axis="y", color="#e7e7e7", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.axvline(BACKUP_DELAY_H, color="#c2c2c2", linewidth=1)
        ax.annotate("backup activation delay", (BACKUP_DELAY_H, 101), fontsize=8, color="#666666", ha="left",
                    xytext=(3, 0), textcoords="offset points")
        if case.service == "idp_keygate":
            ax.text(DURATIONS[2], 7, "Backup, buffers and backup + buffer: about 0% at every duration\n"
                    "(the backup route needs KeyGate too)", fontsize=8, color="#666666")
        mech = [m for m in a["mechanisms"] if m["case"] == case.id]
        bottom = [0.0] * len(mech)
        ax2 = axes[1][col]
        for cat in CATEGORIES:
            vals = [float(m[f"share_{cat}"] or 0) * 100 for m in mech]
            ax2.bar([m["duration_h"] for m in mech], vals, bottom=bottom, width=1.4, color=category_colours[cat],
                    label=category_labels[cat], edgecolor="white", linewidth=1)
            bottom = [b + v for b, v in zip(bottom, vals)]
        if col == 0:
            ax2.set_ylabel("Share of overdue value × hours,\nno intervention (%)")
        ax2.set_xlabel("Outage duration (hours)")
        ax2.set_ylim(0, 100)
        ax2.spines[["top", "right"]].set_visible(False)
        ax2.set_xticks(DURATIONS)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=5, frameon=False, bbox_to_anchor=(0.5, 1.0))
    h2, l2 = axes[1][0].get_legend_handles_labels()
    fig.legend(h2, l2, loc="lower center", ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.005))
    fig.text(0.5, 0.035, DISCLAIMER + f" The EUR {CREDIT_LIMIT_EUR // 1000}k credit line coincides with the EUR "
             f"{max(BUFFER_SIZES_EUR) // 1000}k buffer (same limit and hour) and is not drawn separately.",
             ha="center", fontsize=7.5, color="#666666")
    fig.tight_layout(rect=(0, 0.07, 1, 0.94))
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    analysis = run(args.scenario.resolve(), args.out.resolve())
    print(DISCLAIMER)
    print(f"wrote results to {args.out}")
    for m in analysis["mechanisms"]:
        print(f"  {m['case']:<18} {m['duration_h']:>2} h  cash-only share {m['cash_only_share']:.2f}  "
              f"direct/propagated/mixed {m['direct_operational_count']}/{m['financial_propagation_count']}/{m['mixed_count']}")
    for r in analysis["rankings"]:
        if r["setting"] == "default":
            print(f"  {r['case']:<18} {r['duration_h']:>2} h  {r['ranking']}")
    for rv in analysis["reversals"]:
        print(f"  reversal: {rv}")
    for c in analysis["complementarity"]:
        print(f"  {c['case']:<18} {c['duration_h']:>2} h  backup+buffer: {c['verdict']} ({c['excess_pp']} pp)")
    print(f"  robustness (settings): {sum(r['stable'] for r in analysis['robustness'])}/{len(analysis['robustness'])} keep the verdict")
    print(f"  robustness (start offsets): {sum(r['stable'] for r in analysis['offset_stability'])}/"
          f"{len(analysis['offset_stability'])} keep the verdict")


if __name__ == "__main__":
    main()
