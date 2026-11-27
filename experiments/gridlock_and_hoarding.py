"""Research experiment: payment gridlock and liquidity hoarding after an operational outage.

Two questions, both asked of fully synthetic scenarios:

1. Gridlock. When does a liquidity-saving mechanism (hourly multilateral offsetting of
   queued payments) reduce the payment disruption caused by an outage, and how does that
   depend on how much cash the participants hold and on the settlement rules?
2. Hoarding. If firms hold back routine payments to keep cash for their own urgent ones
   (payroll, tax), are the urgent payments protected, and what does it cost everyone
   else? Does a rule that helps a firm acting alone still help when every firm uses it?

Scenarios: scenarios/cluster_gridlock_48h.json (circular trade on one industrial estate)
and scenarios/default_72h.json (a one-directional supply chain), plus 300 random
structural scenarios from the test generator, to see how often each effect appears
across very different structures. That share describes the generated test structures,
not the likelihood of anything in real payment systems.

Everything is deterministic: there is no sampling and no Monte Carlo. Findings are
properties of this model under these assumptions, not statements about real systems.

    python experiments/gridlock_and_hoarding.py         # writes experiments/output/gridlock_and_hoarding/
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from cyberliquidity import DISCLAIMER, ENGINE_VERSION, simulate
from cyberliquidity.compare import INTERVENTION_LABELS, compare
from cyberliquidity.export import environment_info, load_scenario, scenario_digest
from cyberliquidity.generator import random_scenario
from cyberliquidity.metrics import summarize
from cyberliquidity.models import Scenario
from cyberliquidity.variants import make_disruption, make_offsetting, make_reserve, replace, with_intervention, with_settings

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {"cluster": ROOT / "scenarios" / "cluster_gridlock_48h.json",
             "supply_chain": ROOT / "scenarios" / "default_72h.json"}
DEFAULT_OUT = ROOT / "experiments" / "output" / "gridlock_and_hoarding"

BALANCE_FACTORS = (0.5, 0.75, 0.9, 1.0, 1.25, 1.5, 2.0, 3.0)  # every opening balance scaled by this factor
RECYCLING = ("within_step", "next_step")
DURATIONS = (4, 8, 12, 24)
LOOKAHEADS = (1, 2, 4, 8, 12, 24, 48)
PROTECTED = (1, 2)
INDIVIDUAL_LOOKAHEAD = 24
RANDOM_SEEDS = range(300)
RANDOM_LOOKAHEAD = 12
METRIC = "overdue_value_hours"


def _scaled(base: Scenario, factor: float) -> Scenario:
    return replace(base, participants=tuple(p.model_copy(update={"initial_balance": int(p.initial_balance * factor)})
                                            for p in base.participants))


def _stressed(base: Scenario, duration: int | None = None) -> Scenario:
    d = base.disruptions[0]
    if duration is not None:
        d = make_disruption(d.service, d.start_hour, duration, d.severity_pct, id=d.id, description=d.description)
    return replace(base, disruptions=(d,), interventions=())


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _pct(before: int, after: int) -> float | None:
    return round((before - after) / before * 100, 2) if before else None


def offsetting_grid(bases: dict[str, Scenario]) -> list[dict]:
    rows = []
    for name, base in bases.items():
        for factor in BALANCE_FACTORS:
            for recycling in RECYCLING:
                for duration in DURATIONS:
                    stressed = with_settings(_stressed(_scaled(base, factor), duration), liquidity_recycling=recycling)
                    b = summarize(simulate(stressed))
                    c = summarize(simulate(with_intervention(stressed, make_offsetting())))
                    rows.append({
                        "scenario": name, "balance_factor": factor, "recycling": recycling, "duration_h": duration,
                        "stress_value": b[METRIC], "offsetting_value": c[METRIC],
                        "reduction_pct": _pct(b[METRIC], c[METRIC]), "batches": c["offsetting_batches"],
                        "settled_in_batches": c["settled_via_offsetting_count"],
                        "liquidity_saved": c["offsetting_liquidity_saved"],
                        "stress_cash_blocked_count": b["blocked_cash_count"],
                    })
    return rows


def _urgent(result, participant: str | None = None) -> tuple[int, int]:
    """(delayed priority-1 payments, their overdue value x hours), optionally of one payer only."""
    chosen = [o for o in result.outcomes.values() if o.priority == 1 and (participant is None or o.payer == participant)]
    return sum(1 for o in chosen if o.delayed), sum(o.amount * o.overdue_hours for o in chosen)


def reserve_sweep(bases: dict[str, Scenario]) -> list[dict]:
    rows = []
    for name, base in bases.items():
        for condition in ("baseline", "stress"):
            scenario = _stressed(base) if condition == "stress" else replace(base, disruptions=(), interventions=())
            none = simulate(scenario)
            m0 = summarize(none)
            urgent0 = _urgent(none)
            for protected in PROTECTED:
                for lookahead in LOOKAHEADS:
                    r = simulate(with_intervention(scenario, make_reserve(None, protected, lookahead)))
                    m = summarize(r)
                    urgent = _urgent(r)
                    rows.append({
                        "scenario": name, "condition": condition, "protected_priority": protected, "lookahead_h": lookahead,
                        "value_hours_without_rule": m0[METRIC], "value_hours_with_rule": m[METRIC],
                        "change_pct": None if not m0[METRIC] else round((m[METRIC] - m0[METRIC]) / m0[METRIC] * 100, 2),
                        "delayed_without_rule": m0["delayed_count"], "delayed_with_rule": m["delayed_count"],
                        "urgent_delayed_without_rule": urgent0[0], "urgent_delayed_with_rule": urgent[0],
                        "urgent_value_hours_without_rule": urgent0[1], "urgent_value_hours_with_rule": urgent[1],
                        "cash_short_without_rule": m0["participants_cash_short_count"],
                        "cash_short_with_rule": m["participants_cash_short_count"],
                        "held_back_payments": m["reserve_held_count"],
                    })
    return rows


def reserve_individual(bases: dict[str, Scenario]) -> list[dict]:
    """Each firm applying the rule alone, against every firm applying it (stress run, priority 1)."""
    rows = []
    for name, base in bases.items():
        stressed = _stressed(base)
        none = simulate(stressed)
        everyone = simulate(with_intervention(stressed, make_reserve(None, 1, INDIVIDUAL_LOOKAHEAD)))
        total_none, total_all = summarize(none)[METRIC], summarize(everyone)[METRIC]
        for p in base.participants:
            if not any(o.payer == p.id and o.priority == 1 for o in base.obligations):
                continue
            alone = simulate(with_intervention(stressed, make_reserve([p.id], 1, INDIVIDUAL_LOOKAHEAD)))
            own_none, own_alone, own_all = _urgent(none, p.id), _urgent(alone, p.id), _urgent(everyone, p.id)
            rows.append({
                "scenario": name, "participant": p.id, "name": p.name,
                "own_urgent_value_hours_no_rule": own_none[1], "own_urgent_value_hours_alone": own_alone[1],
                "own_urgent_value_hours_everyone": own_all[1],
                "own_urgent_delayed_no_rule": own_none[0], "own_urgent_delayed_alone": own_alone[0],
                "own_urgent_delayed_everyone": own_all[0],
                "network_value_hours_no_rule": total_none, "network_value_hours_alone": summarize(alone)[METRIC],
                "network_value_hours_everyone": total_all,
            })
    return rows


def random_structures() -> list[dict]:
    rows = []
    for seed in RANDOM_SEEDS:
        s = random_scenario(seed)
        s = replace(s, interventions=())
        b = simulate(s)
        mb = summarize(b)
        o = summarize(simulate(with_intervention(s, make_offsetting())))
        rr = simulate(with_intervention(s, make_reserve(None, 1, RANDOM_LOOKAHEAD)))
        mr = summarize(rr)
        rows.append({
            "seed": seed, "disruptions": len(s.disruptions), "recycling": s.settings.liquidity_recycling,
            "value_hours": mb[METRIC], "value_hours_offsetting": o[METRIC], "batches": o["offsetting_batches"],
            "value_hours_reserve": mr[METRIC], "urgent_delayed": _urgent(b)[0], "urgent_delayed_reserve": _urgent(rr)[0],
            "urgent_value_hours": _urgent(b)[1], "urgent_value_hours_reserve": _urgent(rr)[1],
        })
    return rows


def cluster_pairs(cluster: Scenario) -> list[dict]:
    """Every pair of the cluster's candidate interventions against the independence benchmark (12-hour outage)."""
    ivs = list(cluster.candidate_interventions)
    c = compare(cluster, ivs, packages=[(a, b) for i, a in enumerate(ivs) for b in ivs[i + 1:]])
    rows = []
    for key in c.package_keys():
        x = c.interaction(key, METRIC)
        a, b = c.packages[key]
        rows.append({
            "first": a.id, "second": b.id,
            "label": f"{INTERVENTION_LABELS[a.type]} + {INTERVENTION_LABELS[b.type]}",
            "first_alone_pct": round(x["separate_pct"][f"C:{a.id}"], 2),
            "second_alone_pct": round(x["separate_pct"][f"C:{b.id}"], 2),
            "together_pct": round(x["combined_pct"], 2), "independent_benchmark_pct": round(x["independent_pct"], 2),
            "excess_pp": round(x["excess_pp"], 2), "verdict": x["verdict"],
            "offsetting_batches": c.summaries[key]["offsetting_batches"],
            "held_back_payments": c.summaries[key]["reserve_held_count"],
        })
    return rows


def _sign(before: int, after: int) -> str:
    return "lower" if after < before else ("higher" if after > before else "unchanged")


def analyse(grid: list[dict], sweep: list[dict], individual: list[dict], structures: list[dict],
            pairs: list[dict]) -> dict:
    a: dict = {"pairs": pairs}
    cluster = [r for r in grid if r["scenario"] == "cluster"]
    chain = [r for r in grid if r["scenario"] == "supply_chain"]
    configured = {(r["recycling"]): r for r in cluster if r["balance_factor"] == 1.0 and r["duration_h"] == 12}
    a["cluster_configured"] = configured
    a["chain_batches"] = sum(r["batches"] for r in chain)
    a["chain_runs"] = len(chain)
    by_factor = {}
    for r in cluster:
        if r["duration_h"] == 12:
            by_factor.setdefault(r["recycling"], []).append((r["balance_factor"], r["reduction_pct"], r["batches"]))
    a["cluster_by_factor"] = by_factor
    a["cluster_helped"] = sum(1 for r in cluster if r["reduction_pct"] and r["reduction_pct"] > 0)
    a["cluster_worse"] = sum(1 for r in cluster if r["reduction_pct"] is not None and r["reduction_pct"] < 0)
    a["cluster_runs"] = len(cluster)

    stress_rows = [r for r in sweep if r["condition"] == "stress"]
    a["reserve_worse"] = sum(1 for r in stress_rows if r["value_hours_with_rule"] > r["value_hours_without_rule"])
    a["reserve_stress_runs"] = len(stress_rows)
    a["reserve_urgent"] = {s: Counter_like(_sign(r["urgent_value_hours_without_rule"], r["urgent_value_hours_with_rule"])
                                           for r in stress_rows if r["scenario"] == s) for s in ("cluster", "supply_chain")}
    a["reserve_baseline_harm"] = [r for r in sweep if r["condition"] == "baseline" and r["value_hours_with_rule"] > 0]
    mixed = []  # settings where some look-ahead windows hurt urgent payments and others protect them
    for s in ("cluster", "supply_chain"):
        for protected in PROTECTED:
            series = [r for r in stress_rows if r["scenario"] == s and r["protected_priority"] == protected]
            worst = max(series, key=lambda r: (r["urgent_value_hours_with_rule"], -r["lookahead_h"]))
            best = min(series, key=lambda r: (r["urgent_value_hours_with_rule"], r["lookahead_h"]))
            without = series[0]["urgent_value_hours_without_rule"]
            if worst["urgent_value_hours_with_rule"] > without > best["urgent_value_hours_with_rule"]:
                mixed.append({"scenario": s, "protected_priority": protected, "without": without,
                              "worst_lookahead": worst["lookahead_h"], "worst": worst["urgent_value_hours_with_rule"],
                              "best_lookahead": best["lookahead_h"], "best": best["urgent_value_hours_with_rule"],
                              "best_network_change": best["change_pct"]})
    a["reserve_mixed"] = mixed
    a["reserve_extremes"] = {
        s: {
            "min_change": min((r["change_pct"] for r in stress_rows if r["scenario"] == s), default=None),
            "max_change": max((r["change_pct"] for r in stress_rows if r["scenario"] == s), default=None),
        } for s in ("cluster", "supply_chain")
    }

    fallacy = {}
    for s in ("cluster", "supply_chain"):
        rows = [r for r in individual if r["scenario"] == s]
        helped_alone = [r for r in rows if r["own_urgent_value_hours_alone"] < r["own_urgent_value_hours_no_rule"]]
        hurt_when_all = [r for r in helped_alone if r["own_urgent_value_hours_everyone"] >= r["own_urgent_value_hours_no_rule"]]
        fallacy[s] = {"firms": len(rows), "helped_alone": [r["participant"] for r in helped_alone],
                      "not_helped_when_everyone": [r["participant"] for r in hurt_when_all],
                      "network_no_rule": rows[0]["network_value_hours_no_rule"] if rows else None,
                      "network_everyone": rows[0]["network_value_hours_everyone"] if rows else None}
    a["fallacy"] = fallacy

    n = len(structures)
    a["random_n"] = n
    a["random_offsetting"] = Counter_like(_sign(r["value_hours"], r["value_hours_offsetting"]) for r in structures)
    a["random_offsetting_batches"] = sum(1 for r in structures if r["batches"])
    a["random_reserve_total"] = Counter_like(_sign(r["value_hours"], r["value_hours_reserve"]) for r in structures)
    a["random_reserve_urgent"] = Counter_like(_sign(r["urgent_value_hours"], r["urgent_value_hours_reserve"])
                                              for r in structures)
    both = [r for r in structures if r["value_hours_reserve"] > r["value_hours"]
            and r["urgent_value_hours_reserve"] < r["urgent_value_hours"]]
    a["random_reserve_trade_off"] = len(both)
    return a


def Counter_like(items) -> dict[str, int]:  # noqa: N802 - small ordered tally that serialises as a plain dict
    out = {"lower": 0, "unchanged": 0, "higher": 0}
    for item in items:
        out[item] += 1
    return out


def _money(value: int) -> str:
    return f"EUR {value / 100:,.0f}"


def findings_markdown(a: dict, config: dict) -> str:
    c = a["cluster_configured"]
    lines = [
        "# Findings: payment gridlock and liquidity hoarding",
        "",
        f"_{DISCLAIMER} Generated by `experiments/gridlock_and_hoarding.py` (engine {config['engine_version']}); "
        "every number below comes from the CSV files next to this file._",
        "",
        "Metric: overdue value × hours (the sum over hours of the value of payments that are due but unpaid). "
        "Lower is less disruption; it is not a loss estimate.",
        "",
        "## 1. Gridlock: when does multilateral offsetting help?",
        "",
        f"- **Circular trade (cluster), 12-hour outage, balances as configured:** offsetting removes "
        f"{c['within_step']['reduction_pct']:.0f}% of the disruption with within-step recycling and "
        f"{c['next_step']['reduction_pct']:.0f}% with next-step recycling. In both it settles the suppliers' ring of "
        f"{c['within_step']['settled_in_batches']} payments in one batch, and {_money(c['within_step']['liquidity_saved'])} "
        "of the gross value needs no cash at all.",
        "- **It depends on cash in a non-monotone way** (12-hour outage; reduction by balance factor):",
        "",
        "| Balance factor | " + " | ".join(f"{f:g}×" for f in BALANCE_FACTORS) + " |",
        "|---|" + "---|" * len(BALANCE_FACTORS),
    ]
    for recycling, values in a["cluster_by_factor"].items():
        lines.append(f"| {recycling.replace('_', ' ')} | " + " | ".join(
            "–" if pct is None else f"{pct:.0f}%" for _, pct, _ in sorted(values)) + " |")
    lines += [
        "",
        "  With too little cash, the participants who end the ring as net payers cannot cover even their net "
        "positions: the greedy rule removes their payments, the ring unravels and nothing settles (0%). With ample "
        "cash, firms pay one another directly and there is no gridlock left to resolve. Offsetting helps only "
        "in a middle band.",
        f"- **One-directional supply chain:** across all {a['chain_runs']} tested combinations of balance factor, "
        f"recycling rule and outage duration, offsetting formed {a['chain_batches']} batches. Its delays come from "
        "routes that are down and from cash that is genuinely missing, not from payments waiting for one another.",
        f"- **Across {a['random_n']} random test structures:** offsetting formed at least one batch in "
        f"{a['random_offsetting_batches']}; the disruption was lower in {a['random_offsetting']['lower']}, unchanged in "
        f"{a['random_offsetting']['unchanged']} and higher in {a['random_offsetting']['higher']}. Settlement is path-dependent: "
        "settling a batch early can move cash away from a participant who needed it for a later payment. These "
        "shares describe the generated structures only.",
        "",
        "## 2. Hoarding: does holding cash back for urgent payments help?",
        "",
        f"- **Under stress the rule made the network's disruption worse in {a['reserve_worse']} of "
        f"{a['reserve_stress_runs']} tested settings** (two scenarios × protected priority 1 or 2 × "
        f"{len(LOOKAHEADS)} look-ahead windows). Change against no rule: cluster "
        f"{a['reserve_extremes']['cluster']['min_change']:+.0f}% to {a['reserve_extremes']['cluster']['max_change']:+.0f}%, "
        f"supply chain {a['reserve_extremes']['supply_chain']['min_change']:+.0f}% to "
        f"{a['reserve_extremes']['supply_chain']['max_change']:+.0f}%.",
        f"- **Urgent payments themselves** (overdue value × hours of priority-1 payments, stress): cluster lower in "
        f"{a['reserve_urgent']['cluster']['lower']}, unchanged in {a['reserve_urgent']['cluster']['unchanged']} and "
        f"higher in {a['reserve_urgent']['cluster']['higher']} settings; supply chain lower in "
        f"{a['reserve_urgent']['supply_chain']['lower']}, unchanged in {a['reserve_urgent']['supply_chain']['unchanged']} and "
        f"higher in {a['reserve_urgent']['supply_chain']['higher']}.",
        *[f"- **A longer window is not simply more protection** ({'cluster' if m['scenario'] == 'cluster' else 'supply chain'}, "
          f"priority ≤ {m['protected_priority']}): with a {m['worst_lookahead']}-hour look-ahead the urgent payments are "
          f"worse off than with no rule ({_money(m['worst'])}·h against {_money(m['without'])}·h), while "
          f"{m['best_lookahead']} hours brings them to {_money(m['best'])}·h, at {m['best_network_change']:+.0f}% network "
          "disruption." + (" Whether a firm's own reserve protects its payroll depends on what everyone else holds back."
                           if i == 0 else "")
          for i, m in enumerate(a["reserve_mixed"])],
        f"- **Even without an outage** the rule creates delays in {len(a['reserve_baseline_harm'])} of "
        f"{len(LOOKAHEADS) * len(PROTECTED) * 2} tested settings: firms hold cash back from payments that would "
        "otherwise have settled on time.",
    ]
    harmful = [r for r in a["pairs"] if r["verdict"] == "more harmful together"]
    if harmful:
        def outcome(r: dict) -> str:
            together = (f"ends {abs(r['together_pct']):.0f}% worse than no intervention" if r["together_pct"] < 0
                        else f"removes {r['together_pct']:.0f}%")
            detail = f"{r['held_back_payments']} payments held back although they could be funded"
            if "offsetting" in (r["first"], r["second"]):
                detail += f", {r['offsetting_batches']} offsetting batches"
            return (f"{r['label'].lower()} {together}, against {r['independent_benchmark_pct']:.0f}% removed if they "
                    f"acted independently ({detail})")

        lines.append(
            "- **The rule can cancel other interventions** (cluster, 12-hour outage, the scenario's candidates in pairs): "
            + "; ".join(outcome(r) for r in harmful)
            + ". A buffer's cash is kept back for the recipient's own tax and payroll instead of paying its suppliers, "
            "and offsetting finds no covered set once every firm must also keep its payroll cash.")
    for s, label in (("cluster", "Cluster"), ("supply_chain", "Supply chain")):
        f = a["fallacy"][s]
        lines.append(
            f"- **{label}: one firm alone against everyone** (stress, priority 1, {INDIVIDUAL_LOOKAHEAD}-hour look-ahead): "
            f"{len(f['helped_alone'])} of {f['firms']} firms with urgent payments reduce their own urgent delay by "
            f"applying the rule alone"
            + (f"; of those, {len(f['not_helped_when_everyone'])} "
               f"{'is' if len(f['not_helped_when_everyone']) == 1 else 'are'} no better off than with no rule once "
               "every firm applies it" if f["helped_alone"] else "")
            + f". Network disruption: {_money(f['network_no_rule'])}·h with no rule, "
            f"{_money(f['network_everyone'])}·h when everyone applies it."
        )
    lines += [
        f"- **Across {a['random_n']} random test structures** (priority 1, {RANDOM_LOOKAHEAD}-hour look-ahead, every "
        f"participant): total disruption lower in {a['random_reserve_total']['lower']}, unchanged in "
        f"{a['random_reserve_total']['unchanged']}, higher in {a['random_reserve_total']['higher']}; priority-1 "
        f"overdue value × hours lower in {a['random_reserve_urgent']['lower']}, unchanged in "
        f"{a['random_reserve_urgent']['unchanged']}, higher in {a['random_reserve_urgent']['higher']}. In "
        f"{a['random_reserve_trade_off']} structures the rule protected urgent payments at the cost of more total "
        "disruption.",
        "",
        "## Limits",
        "",
        "- Two hand-built scenarios and generated test structures: none is calibrated to real firms, payment systems "
        "or incidents, and the random structures have no economic meaning.",
        "- Offsetting uses one greedy removal rule (the last payment of the participant with the largest uncovered "
        "position goes first). It does not search for the largest settleable set; an optimising algorithm could "
        "settle more.",
        "- The reserve rule is one stylised treasury behaviour with perfect knowledge of the firm's own calendar. "
        "Firms do not learn, negotiate, borrow or cancel payments.",
        "- Nothing here estimates probabilities, losses or returns; counts over random structures are not likelihoods.",
        "",
    ]
    return "\n".join(lines)


def plot(grid: list[dict], sweep: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9, "axes.edgecolor": "#c2c2c2",
                         "axes.labelcolor": "#666666", "xtick.color": "#666666", "ytick.color": "#666666",
                         "axes.titlesize": 10, "axes.titlecolor": "#191919"})
    blue, orange = "#257de0", "#f47230"  # categorical slots 1 and 2 (validated pair): cluster, supply chain
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.6))

    ax = axes[0]
    position = {f: i for i, f in enumerate(BALANCE_FACTORS)}  # tested grid, evenly spaced
    for recycling, dash, fill in (("within_step", "-", blue), ("next_step", (0, (4, 2)), "white")):
        pts = sorted((r["balance_factor"], r["reduction_pct"] or 0) for r in grid
                     if r["scenario"] == "cluster" and r["recycling"] == recycling and r["duration_h"] == 12)
        ax.plot([position[p[0]] for p in pts], [p[1] for p in pts], color=blue, linewidth=2, linestyle=dash,
                marker="o", markersize=5, markerfacecolor=fill, label=f"Cluster, {recycling.replace('_', '-')} recycling")
    ax.set_xticks(range(len(BALANCE_FACTORS)))
    ax.set_xticklabels([f"{f:g}×" for f in BALANCE_FACTORS])
    ax.set_xlabel("Opening balances (× configured; tested values)")
    ax.set_ylabel("Disruption removed by offsetting (%)")
    ax.set_title("Cluster: offsetting helps only in a middle band of cash", loc="left")
    ax.set_ylim(-5, 105)
    ax.legend(frameon=False, loc="upper right")

    for col, (field, ylabel, title) in enumerate((
            ("change_pct", "Change in network disruption vs no rule (%)", "Reserve rule: network disruption (stress)"),
            ("urgent_value_hours_with_rule", "Priority-1 overdue value × hours (EUR·h)",
             "Reserve rule: urgent payments themselves (stress)")), start=1):
        ax = axes[col]
        for scenario, colour, marker in (("cluster", blue, "o"), ("supply_chain", orange, "s")):
            for protected, dash in ((1, "-"), (2, (0, (4, 2)))):
                pts = [r for r in sweep if r["scenario"] == scenario and r["condition"] == "stress"
                       and r["protected_priority"] == protected]
                ys = [(r[field] / 100 if field.endswith("_with_rule") else r[field]) for r in pts]
                ax.plot([r["lookahead_h"] for r in pts], ys, color=colour, linewidth=2, linestyle=dash, marker=marker,
                        markersize=5, markerfacecolor=colour if protected == 1 else "white",
                        label=f"{'Cluster' if scenario == 'cluster' else 'Supply chain'}, protect priority ≤ {protected}")
            if field.endswith("_with_rule"):
                none = next(r for r in sweep if r["scenario"] == scenario and r["condition"] == "stress")
                ax.axhline(none["urgent_value_hours_without_rule"] / 100, color=colour, linewidth=1, linestyle=":")
        ax.set_xscale("log")
        ax.minorticks_off()
        ax.set_xticks(LOOKAHEADS)
        ax.set_xticklabels([str(x) for x in LOOKAHEADS])
        ax.set_xlabel("Look-ahead window for protected payments (hours)")
        ax.set_ylabel(ylabel)
        ax.set_title(title, loc="left")
        if col == 1:
            ax.axhline(0, color="#8c8c8c", linewidth=1)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%" if v else "0%"))
        else:
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
            ax.text(1, 0.98, "dotted: no rule", transform=ax.transAxes, ha="right", va="top", fontsize=8, color="#666666")
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False, bbox_to_anchor=(0.62, -0.01))
    for ax in axes:
        ax.grid(axis="y", color="#e7e7e7", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    fig.text(0.5, 0.955, DISCLAIMER, ha="center", fontsize=7.5, color="#666666")
    fig.tight_layout(rect=(0, 0.08, 1, 0.95))
    fig.savefig(path, dpi=160)
    plt.close(fig)


def run(out_dir: Path, plot_figure: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    bases = {name: load_scenario(path) for name, path in SCENARIOS.items()}
    grid = offsetting_grid(bases)
    sweep = reserve_sweep(bases)
    individual = reserve_individual(bases)
    structures = random_structures()
    pairs = cluster_pairs(bases["cluster"])
    analysis = analyse(grid, sweep, individual, structures, pairs)
    config = {
        "experiment": "gridlock_and_hoarding",
        "engine_version": ENGINE_VERSION,
        "environment": environment_info(),
        "disclaimer": DISCLAIMER,
        "metric": METRIC,
        "scenarios": {name: {"file": str(path.relative_to(ROOT)), "digest": scenario_digest(bases[name])}
                      for name, path in SCENARIOS.items()},
        "balance_factors": BALANCE_FACTORS, "recycling": RECYCLING, "durations_h": DURATIONS,
        "reserve_lookaheads_h": LOOKAHEADS, "reserve_protected_priorities": PROTECTED,
        "individual_lookahead_h": INDIVIDUAL_LOOKAHEAD, "random_seeds": [RANDOM_SEEDS.start, RANDOM_SEEDS.stop],
        "random_reserve_lookahead_h": RANDOM_LOOKAHEAD,
        "amounts": "integer minor currency units (cents)",
    }
    (out_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    _write_csv(out_dir / "offsetting_grid.csv", grid)
    _write_csv(out_dir / "reserve_sweep.csv", sweep)
    _write_csv(out_dir / "reserve_individual.csv", individual)
    _write_csv(out_dir / "random_structures.csv", structures)
    _write_csv(out_dir / "cluster_pairs.csv", pairs)
    (out_dir / "findings.md").write_text(findings_markdown(analysis, config), encoding="utf-8")
    if plot_figure:
        plot(grid, sweep, out_dir / "gridlock_and_hoarding.png")
    return analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    analysis = run(args.out.resolve())
    print(DISCLAIMER)
    print(f"wrote results to {args.out}")
    c = analysis["cluster_configured"]
    print(f"  cluster, 12 h: offsetting removes {c['within_step']['reduction_pct']}% (within-step), "
          f"{c['next_step']['reduction_pct']}% (next-step)")
    print(f"  supply chain: {analysis['chain_batches']} offsetting batches in {analysis['chain_runs']} runs")
    print(f"  reserve rule worse under stress in {analysis['reserve_worse']}/{analysis['reserve_stress_runs']} settings")
    print(f"  random structures: offsetting {analysis['random_offsetting']}, reserve total {analysis['random_reserve_total']}, "
          f"urgent {analysis['random_reserve_urgent']}")


if __name__ == "__main__":
    main()
