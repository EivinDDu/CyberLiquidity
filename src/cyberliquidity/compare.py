"""Baseline / stress / intervention comparisons with deterministic mechanism narratives.

Rankings are computed for *one* scenario under its declared assumptions; nothing here
claims an intervention is better in general.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .engine import CASH_REASON, OPERATIONAL_REASONS, SimulationResult, Status, simulate
from .metrics import LOWER_IS_BETTER, summarize
from .models import (
    BackupRouteIntervention,
    CreditLineIntervention,
    FasterRecoveryIntervention,
    Intervention,
    LiquidityBufferIntervention,
    OffsettingIntervention,
    PriorityReserveIntervention,
    Scenario,
)
from .operational import OperationalModel
from .variants import baseline, stress, with_intervention

INTERVENTION_LABELS = {
    "backup_route": "Backup payment provider",
    "liquidity_buffer": "Liquidity buffer",
    "faster_recovery": "Faster service recovery",
    "credit_line": "Committed credit line",
    "offsetting": "Multilateral offsetting",
    "priority_reserve": "Payroll-protection reserve",
}


@dataclass
class Comparison:
    base: Scenario
    runs: dict[str, SimulationResult]  # "A", "B", "C:<id>" per intervention, "P:<id>+<id>" per package
    labels: dict[str, str]
    interventions: dict[str, Intervention]
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    effects: dict[str, list[str]] = field(default_factory=dict)
    packages: dict[str, tuple[Intervention, ...]] = field(default_factory=dict)

    def intervention_keys(self) -> list[str]:
        """Runs with exactly one intervention."""
        return [k for k in self.runs if k.startswith("C:")]

    def package_keys(self) -> list[str]:
        return [k for k in self.runs if k.startswith("P:")]

    def ranking(self, metric: str = "overdue_value_hours", include_packages: bool = False) -> list[dict[str, Any]]:
        keys = self.intervention_keys() + (self.package_keys() if include_packages else [])
        return rank(self.summaries, keys, metric, self.labels)

    def interaction(self, package: str, metric: str = "overdue_value_hours") -> dict[str, Any]:
        members = [run_key(iv) for iv in self.packages[package]]
        return interaction(self.summaries, metric, members, package)


def run_key(intervention: Intervention) -> str:
    return f"C:{intervention.id}"


def package_key(interventions: Sequence[Intervention]) -> str:
    return "P:" + "+".join(iv.id for iv in interventions)


def package_label(interventions: Sequence[Intervention]) -> str:
    return "C · " + " + ".join(INTERVENTION_LABELS[iv.type] for iv in interventions)


def compare(scenario: Scenario, interventions: Sequence[Intervention] | None = None,
            packages: Sequence[Sequence[Intervention]] = ()) -> Comparison:
    """Run A (baseline), B (stress), B + each intervention separately, and B + each package.

    A package applies several interventions together; its members are also run alone
    so that the combined effect can be compared with the separate ones.
    """
    chosen = list(scenario.candidate_interventions if interventions is None else interventions)
    for package in packages:
        for iv in package:
            if all(iv.id != c.id for c in chosen):
                chosen.append(iv)
    runs = {"A": simulate(baseline(scenario)), "B": simulate(stress(scenario))}
    labels = {"A": "A · Baseline (no disruption)", "B": "B · Stress (disruption, no intervention)"}
    by_key: dict[str, Intervention] = {}
    stressed = stress(scenario)
    for iv in chosen:
        key = run_key(iv)
        runs[key] = simulate(with_intervention(stressed, iv))
        labels[key] = f"C · {INTERVENTION_LABELS[iv.type]} ({iv.id})"
        by_key[key] = iv
    package_map: dict[str, tuple[Intervention, ...]] = {}
    for package in packages:
        members = tuple(package)
        if len(members) < 2:
            continue
        key = package_key(members)
        runs[key] = simulate(with_intervention(stressed, *members))
        labels[key] = package_label(members)
        package_map[key] = members
    comparison = Comparison(scenario, runs, labels, by_key, packages=package_map)
    comparison.summaries = {k: summarize(r) for k, r in runs.items()}
    for key, iv in by_key.items():
        comparison.effects[key] = describe_effect(runs["B"], runs[key], iv, runs["A"])
    for key in package_map:
        comparison.effects[key] = describe_package(comparison, key)
    return comparison


INDEPENDENCE_TOLERANCE_PP = 1.0  # |excess| up to this many percentage points counts as "roughly independent"


def interaction(summaries: dict[str, dict[str, Any]], metric: str, members: Sequence[str], package: str) -> dict[str, Any]:
    """Combined effect of a package against an independence benchmark (one run, not a general law).

    If intervention i alone removes the fraction f_i of the stress disruption, and the
    interventions acted independently (each removing its own share of whatever disruption
    the others leave), the package would remove ``1 - prod(1 - f_i)``. The package is
    *complementary* in this run if it removes more than that, *overlapping* if it removes
    less (they partly fix the same delays), and *roughly independent* within
    ``INDEPENDENCE_TOLERANCE_PP`` percentage points. If a member makes the disruption
    worse on its own (f_i < 0, e.g. a reserve rule), "overlapping" would be misleading:
    the verdict is then *more harmful together* or *less harmful together*.
    """
    if metric not in LOWER_IS_BETTER:
        raise ValueError(f"metric must be one of {LOWER_IS_BETTER}")
    stress_value = summaries["B"][metric]
    separate = {k: stress_value - summaries[k][metric] for k in members}
    combined = stress_value - summaries[package][metric]
    result: dict[str, Any] = {
        "package": package,
        "members": list(members),
        "stress_value": stress_value,
        "separate_reductions": separate,
        "combined_reduction": combined,
        "sum_of_separate": sum(separate.values()),
        "gain_over_best_single": combined - max(separate.values(), default=0),
    }
    if stress_value == 0:
        return result | {"verdict": "no disruption to reduce", "combined_pct": None, "separate_pct": {},
                         "independent_pct": None, "excess_pp": None}
    fractions = {k: v / stress_value for k, v in separate.items()}
    remaining = 1.0
    for f in fractions.values():
        remaining *= 1 - f
    independent = (1 - remaining) * 100
    combined_pct = combined / stress_value * 100
    excess = combined_pct - independent
    harmful = any(f < 0 for f in fractions.values())
    if excess > INDEPENDENCE_TOLERANCE_PP:
        verdict = "less harmful together" if harmful else "complementary"
    elif excess < -INDEPENDENCE_TOLERANCE_PP:
        verdict = "more harmful together" if harmful else "overlapping"
    else:
        verdict = "roughly independent"
    return result | {"verdict": verdict, "combined_pct": combined_pct,
                     "separate_pct": {k: f * 100 for k, f in fractions.items()},
                     "independent_pct": independent, "excess_pp": excess}


def describe_package(comparison: Comparison, key: str, metric: str = "overdue_value_hours") -> list[str]:
    s, v = comparison.summaries["B"], comparison.summaries[key]
    money = comparison.base.currency.format
    x = comparison.interaction(key, metric)
    names = [INTERVENTION_LABELS[iv.type].lower() for iv in comparison.packages[key]]
    lines = [
        f"Overdue value × hours {money(s['overdue_value_hours'])}·h → {money(v['overdue_value_hours'])}·h "
        f"({_pct_change(s['overdue_value_hours'], v['overdue_value_hours'])}). Delayed obligations "
        f"{s['delayed_count']} → {v['delayed_count']}.",
    ]
    if x["combined_pct"] is None:
        return lines
    separate = ", ".join(f"{p:.0f}%" for p in x["separate_pct"].values())
    lines.append(
        f"Alone they remove {separate} of the stress disruption. Acting independently (each removing its share of "
        f"what the other leaves) they would remove {x['independent_pct']:.0f}% together; they actually remove "
        f"{x['combined_pct']:.0f}%: **{x['verdict']}** in this run ({x['excess_pp']:+.1f} percentage points)."
    )
    if x["verdict"] == "complementary":
        lines.append("Each covers delays the other leaves, for example liquidity bridging the hours before a backup "
                     "is active while the backup restores upstream payments that liquidity cannot.")
    elif x["verdict"] == "overlapping":
        lines.append("They partly remove the same delays, so the second adds less on top of the first than it "
                     "achieves alone.")
    elif x["verdict"] == "more harmful together":
        lines.append("One of them makes the disruption worse on its own, and in combination it cancels more of the "
                     "other's benefit than its own harm alone would suggest.")
    elif x["verdict"] == "less harmful together":
        lines.append("One of them makes the disruption worse on its own; alongside the other it does less damage, "
                     "because the other removes much of what it would make worse.")
    lines.append(f"Gain over the best single intervention: {money(x['gain_over_best_single'])}·h "
                 f"({' and '.join(names)}).")
    return lines


def rank(summaries: dict[str, dict[str, Any]], keys: Sequence[str], metric: str,
         labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Order interventions by a lower-is-better disruption metric (ties share a rank)."""
    if metric not in LOWER_IS_BETTER:
        raise ValueError(f"ranking metric must be one of {LOWER_IS_BETTER}")
    stress_value = summaries["B"][metric]
    ordered = sorted(keys, key=lambda k: (summaries[k][metric], k))
    rows: list[dict[str, Any]] = []
    previous_value, previous_rank = None, 0
    for position, key in enumerate(ordered, start=1):
        value = summaries[key][metric]
        current_rank = previous_rank if value == previous_value else position
        rows.append({
            "rank": current_rank,
            "run": key,
            "label": (labels or {}).get(key, key),
            "value": value,
            "stress_value": stress_value,
            "reduction": stress_value - value,
            "reduction_pct": ((stress_value - value) / stress_value * 100) if stress_value else None,
        })
        previous_value, previous_rank = value, current_rank
    return rows


# ------------------------------------------------------------------ narratives


def _pct_change(before: float, after: float) -> str:
    if before == after:
        return "unchanged"
    if before == 0:
        return "up from zero"
    change = (after - before) / before * 100
    return f"{'down' if change < 0 else 'up'} {abs(change):.0f}%"


def _operational_value_hours(summary: dict[str, Any]) -> int:
    c = summary["category_value_hours"]
    return c["operational_only"] + c["operational_and_cash"]


def _cash_value_hours(summary: dict[str, Any]) -> int:
    c = summary["category_value_hours"]
    return c["cash_awaiting_inflows"] + c["cash_other"] + c["operational_and_cash"]


def describe_effect(stress_run: SimulationResult, variant: SimulationResult, iv: Intervention,
                    baseline_run: SimulationResult | None = None) -> list[str]:
    """Plain-language mechanism statements derived only from the two runs' records."""
    cur = stress_run.scenario.currency
    names = stress_run.scenario.names()
    money = cur.format
    s, v = summarize(stress_run), summarize(variant)
    lines = [
        f"Overdue value × hours {money(s['overdue_value_hours'])}·h → {money(v['overdue_value_hours'])}·h "
        f"({_pct_change(s['overdue_value_hours'], v['overdue_value_hours'])}). "
        f"Delayed obligations {s['delayed_count']} → {v['delayed_count']} "
        f"(unsettled at horizon {s['unsettled_count']} → {v['unsettled_count']}).",
    ]
    op_s, op_v = _operational_value_hours(s), _operational_value_hours(v)
    cash_s, cash_v = _cash_value_hours(s), _cash_value_hours(v)

    if isinstance(iv, CreditLineIntervention):
        peaks = variant.credit_peak
        limits = {f.participant: f.limit for f in iv.facilities}
        users = sorted((p for p, x in peaks.items() if x > 0), key=lambda p: (-peaks[p], p))
        lines.append(
            f"Mechanism: {len(iv.facilities)} participant(s) could draw up to {money(iv.total_limit)} in total from hour "
            f"{iv.available_from_hour}, only when a payment would otherwise fail for lack of cash. "
            f"{len(users)} drew on it; peak usage {money(sum(peaks.values()))} in total "
            f"({money(v['credit_drawn_total'])} drawn over the run, {money(v['credit_outstanding_at_horizon'])} "
            f"still outstanding at the horizon)."
        )
        if users:
            lines.append("Largest peak draws: " + "; ".join(
                f"{names.get(p, p)} {money(peaks[p])} of {money(limits[p])}" for p in users[:4]) + ".")
        lines.append(f"Value-hours blocked by insufficient cash: {money(cash_s)}·h → {money(cash_v)}·h.")
        if op_s:
            suffix = (" A credit line cannot restore an unavailable route, so these are unchanged." if op_v >= op_s else "")
            lines.append(
                f"Value-hours blocked by unavailable services or exhausted capacity: {money(op_s)}·h → {money(op_v)}·h.{suffix}"
            )
        bound = [p for p in users if peaks[p] >= limits[p]]
        if bound:
            lines.append(f"The limit was fully used by {len(bound)} participant(s) "
                         f"({', '.join(names.get(p, p) for p in bound[:4])}); a larger limit could change the result.")

    elif isinstance(iv, LiquidityBufferIntervention):
        lines.append(
            f"Mechanism: {money(iv.total)} of explicitly recorded funding was paid to "
            f"{len(iv.allocations)} participant(s) in hour {iv.funding_hour}; total internal cash is higher by exactly "
            f"this amount. Value-hours blocked by insufficient cash: {money(cash_s)}·h → {money(cash_v)}·h."
        )
        if op_s:
            suffix = (" Additional liquidity cannot restore an unavailable route, so these are unchanged."
                      if op_v >= op_s else "")
            lines.append(
                f"Value-hours blocked by unavailable services or exhausted capacity: {money(op_s)}·h → {money(op_v)}·h.{suffix}"
            )
        for alloc in sorted(iv.allocations, key=lambda a: (-a.amount, a.participant)):
            pid = alloc.participant
            fixed = sorted(
                (o for o in variant.outcomes.values()
                 if o.payer == pid and o.status is Status.SETTLED_ON_TIME and stress_run.outcomes[o.obligation_id].delayed),
                key=lambda o: (-o.amount, o.obligation_id),
            )
            still = [o for o in variant.outcomes.values() if o.payer == pid and o.delayed]
            if not fixed and not still:
                continue
            text = f"{names[pid]} (+{money(alloc.amount)}): "
            if fixed:
                example = fixed[0]
                was = stress_run.outcomes[example.obligation_id]
                was_text = f"{was.delay_hours} h late" if was.delay_hours is not None else "unsettled"
                text += (f"{len(fixed)} payment(s) now on time, e.g. {example.obligation_id} "
                         f"{example.purpose or 'payment'} {money(example.amount)} (under stress: {was_text})")
            else:
                text += "no payment moved to on time"
            if still:
                operational = sum(1 for o in still if any(r in OPERATIONAL_REASONS for r in o.reason_hours))
                cash = sum(1 for o in still if o.reason_hours.get(CASH_REASON))
                text += f"; {len(still)} still delayed ({operational} with an operational reason, {cash} with a cash reason)"
            lines.append(text + ".")
        remaining_operational = [o for o in variant.outcomes.values()
                                 if o.delayed and any(r in OPERATIONAL_REASONS for r in o.reason_hours)]
        if remaining_operational:
            lines.append(
                f"Not addressed by liquidity: {len(remaining_operational)} obligation(s) worth "
                f"{money(sum(o.amount for o in remaining_operational))} were still held at an unavailable or saturated route."
            )

    elif isinstance(iv, BackupRouteIntervention):
        op = OperationalModel(variant.scenario, variant.effective_disruptions)
        backup_name = names.get(iv.backup_route, iv.backup_route)
        lines.append(
            f"Mechanism: from hour {iv.active_from_hour}, payments eligible for "
            f"{', '.join(names.get(r, r) for r in iv.primary_routes)} could reroute via {backup_name}. "
            f"{v['settled_via_backup_count']} payment(s) worth {money(v['settled_via_backup_value'])} settled through the backup."
        )
        windows = [(d.start_hour, d.restored_hour) for d in variant.effective_disruptions if d.duration_hours or d.recovery_hours]
        in_window = [h for h in range(len(variant.service_status))
                     if h >= iv.active_from_hour and any(a <= h < b for a, b in windows)]
        down = [h for h in in_window if not op.route_usable(iv.backup_route, variant.service_status[h])]
        if down:
            roots: set[str] = set()
            for h in down:
                for sid in variant.scenario.routes_by_id()[iv.backup_route].path:
                    if not variant.service_status[h][sid].usable():
                        roots.add(op.cause_chain(sid, h)[-1]["service"])
            lines.append(
                f"The backup route was itself unusable in {len(down)} disrupted hour(s) after activation because it "
                f"depends on {', '.join(names.get(x, x) for x in sorted(roots))}, which failed: it is not independent "
                f"of this disruption."
            )
        path = variant.scenario.routes_by_id()[iv.backup_route].path
        saturated = [h for h in range(iv.active_from_hour, len(variant.hours))
                     if any(variant.service_status[h][sid].capacity is not None
                            and variant.hours[h].service_used.get(sid, 0) >= variant.service_status[h][sid].capacity > 0
                            for sid in path)]
        if saturated:
            lines.append(
                f"The backup's shared capacity was fully used in {len(saturated)} hour(s), so rerouting was "
                f"limited by its throughput (shared with its existing users)."
            )
        ends = [d.end_hour for d in variant.effective_disruptions if d.duration_hours]
        if ends and iv.active_from_hour >= max(ends):
            lines.append("The backup became active only after the outage ended, so it could not reroute anything during it.")
        if cash_s and cash_v < cash_s:
            lines.append(f"Restoring upstream payments also reduced cash-blocked value-hours ({money(cash_s)}·h → {money(cash_v)}·h).")

    elif isinstance(iv, OffsettingIntervention):
        lines.append(
            f"Mechanism: from hour {iv.active_from_hour}, the payment system offset queued payments every hour, "
            "settling a set of payments together whenever every participant's net outflow over the set was covered."
        )
        if v["offsetting_batches"]:
            lines.append(
                f"{v['offsetting_batches']} batch(es) settled {v['settled_via_offsetting_count']} payment(s) worth "
                f"{money(v['offsetting_gross_value'])} while only {money(v['offsetting_net_value'])} of cash changed hands "
                f"net: {money(v['offsetting_liquidity_saved'])} of liquidity was not needed."
            )
            fixed = sorted((o for o in variant.outcomes.values()
                            if o.via_offsetting and o.status is Status.SETTLED_ON_TIME
                            and stress_run.outcomes[o.obligation_id].delayed),
                           key=lambda o: (-o.amount, o.obligation_id))
            if fixed:
                lines.append(f"{len(fixed)} payment(s) that were delayed under stress settled on time inside a batch, "
                             f"e.g. {fixed[0].obligation_id} {fixed[0].purpose or 'payment'} {money(fixed[0].amount)} "
                             f"({names.get(fixed[0].payer, fixed[0].payer)} → {names.get(fixed[0].recipient, fixed[0].recipient)}).")
        else:
            lines.append(
                "No batch could be formed: no set of queued payments was covered by its participants' own cash, so there "
                "was no gridlock to resolve. The delays here need money that someone does not have, or a working route."
            )
        lines.append(f"Value-hours blocked by insufficient cash: {money(cash_s)}·h → {money(cash_v)}·h.")
        if op_s:
            lines.append(f"Value-hours blocked by unavailable services or exhausted capacity: {money(op_s)}·h → "
                         f"{money(op_v)}·h. Offsetting cannot settle a payment whose route is down.")
        if v["overdue_value_hours"] > s["overdue_value_hours"]:
            lines.append("Offsetting made this run worse: settling a batch moved cash between participants in a way "
                         "that left later payments unfunded (settlement is path-dependent in this model).")

    elif isinstance(iv, PriorityReserveIntervention):
        covered = len(iv.participants) if iv.participants is not None else len(variant.scenario.participants)
        lines.append(
            f"Mechanism: {covered} participant(s) kept back the cash needed for their priority ≤ {iv.protected_priority} "
            f"payments due within {iv.lookahead_hours} h, and released other payments only from the cash left over. "
            "No liquidity is added: the rule only decides which payments wait."
        )
        lines.append(
            f"Priority-1 payments delayed: {s['urgent_delayed_count']} → {v['urgent_delayed_count']} "
            f"(overdue value × hours {money(s['urgent_overdue_value_hours'])}·h → {money(v['urgent_overdue_value_hours'])}·h)."
        )
        if v["reserve_held_count"]:
            lines.append(
                f"{v['reserve_held_count']} routine payment(s) worth {money(v['reserve_held_value'])} were held back in at "
                f"least one hour although the payer could have funded them ({money(v['reserve_held_value_hours'])}·h). "
                "Their recipients waited for that money."
            )
        if v["overdue_value_hours"] > s["overdue_value_hours"]:
            lines.append(
                f"Across the network the rule made the disruption worse ({_pct_change(s['overdue_value_hours'], v['overdue_value_hours'])}): "
                f"participants short of cash {s['participants_cash_short_count']} → {v['participants_cash_short_count']}. "
                "The routine payments it held back were other participants' incoming money, so their shortfalls grew."
            )

    elif isinstance(iv, FasterRecoveryIntervention):
        before = {d.id: d for d in stress_run.effective_disruptions}.get(iv.disruption)
        after = {d.id: d for d in variant.effective_disruptions}.get(iv.disruption)
        if before and after:
            lines.append(
                f"Mechanism: the outage of {names.get(after.service, after.service)} was shortened from "
                f"{before.duration_hours} h to {after.duration_hours} h; all other assumptions are unchanged."
            )
        if s["backlog_hours_to_clear"] is not None or v["backlog_hours_to_clear"] is not None:
            lines.append(
                f"Backlog after the outage: {s['backlog_at_outage_end_count']} → {v['backlog_at_outage_end_count']} "
                f"obligations; cleared by end of hour {s['backlog_cleared_hour']} → {v['backlog_cleared_hour']}."
            )
        lines.append(
            f"Value-hours with an operational reason {money(op_s)}·h → {money(op_v)}·h; "
            f"with a cash reason {money(cash_s)}·h → {money(cash_v)}·h."
        )

    if baseline_run is not None:
        b = summarize(baseline_run)
        if v["delayed_count"] <= b["delayed_count"] and v["overdue_value_hours"] <= b["overdue_value_hours"]:
            lines.append("In this run the intervention returns payment timeliness to the baseline level.")
    return lines
