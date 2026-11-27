"""Plain-language answer to the central question, generated deterministically from one comparison.

    If this service becomes unavailable for twelve hours, which payments are delayed, who
    subsequently runs short of cash, and which intervention reduces the disruption most?

Every sentence is assembled from recorded results (no language model, no estimates).
The paragraphs are Markdown and describe one synthetic run under the model's assumptions.
"""

from __future__ import annotations

from typing import Any

from .cascade import Cascade, analyse_cascade
from .compare import Comparison
from .metrics import RANKING_LABELS
from .models import DISCLAIMER, Disruption, Scenario
from .sweep import BackstopResult


def disruption_phrase(d: Disruption, names: dict[str, str]) -> str:
    who = names.get(d.service, d.service)
    if d.duration_hours == 0 and not d.recovery_hours:
        return f"{who} has no outage"
    state = "is unavailable" if d.severity_pct == 100 else f"runs at {100 - d.severity_pct}% capacity"
    text = f"{who} {state} for {d.duration_hours} h from hour {d.start_hour}"
    if d.recovery_hours:
        text += f" (then {d.recovery_hours} h at {d.recovery_capacity_pct}% while recovering)"
    return text


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _names_list(ids: list[str], names: dict[str, str], extra: dict[str, str] | None = None, limit: int = 5) -> str:
    shown = [names.get(i, i) + (f" ({extra[i]})" if extra and i in extra else "") for i in ids[:limit]]
    if len(ids) > limit:
        shown.append(f"{len(ids) - limit} more")
    return _join(shown)


def robustness_paragraph(robust: dict[str, Any]) -> str | None:
    """One Markdown paragraph on whether the leading intervention survives the assumption variants."""
    leaders = robust["configured_best"]
    rows = robust["variants"]
    if not leaders or not robust["reference_stress_value"] or len(rows) < 2:
        return None
    labels = robust["labels"]
    leader = leaders[0]
    s = robust["summary"][leader]
    n = s["tested"]
    tolerance = f"{robust['materiality'] * 100:.0f}%"
    text = (f"**How robust is this ranking?** {labels[leader]} stays the most effective, or within {tolerance} of it, "
            f"in {s['practical_wins']} of the {n} assumption variants tested (the other settlement rules, the outage "
            "earlier, later, shorter or longer, lower or higher opening balances, less throughput).")
    beaten = robust["configured_best_beaten_in"].get(leader, [])
    if beaten:
        cases = [f"{b['label'].lower()} ({_join([labels[w] for w in b['winners']])} does better, by "
                 f"{b['regret_share'] * 100:.0f}% of the configured disruption)" for b in beaten[:3]]
        where = "one variant" if len(beaten) == 1 else f"{len(beaten)} variants"
        text += f" It is beaten in {where}: {'; '.join(cases)}" + (f"; and {len(beaten) - 3} more." if len(beaten) > 3 else ".")
    minimax = robust["minimax_regret"]
    if minimax == leader:
        text += (" Its largest shortfall against the best option in any variant is also the smallest of all options "
                 "(the minimax-regret choice).")
    elif minimax is not None:
        m = robust["summary"][minimax]
        text += (f" The option whose worst shortfall is smallest is {labels[minimax]} "
                 f"({m['max_regret_share'] * 100:.0f}% of the configured disruption at worst, against "
                 f"{s['max_regret_share'] * 100:.0f}% for {labels[leader]}).")
    worse = [k for k, x in robust["summary"].items() if x["worsens_in"]]
    if worse:
        text += " " + _join([f"{labels[k]} makes the disruption worse in {robust['summary'][k]['worsens_in']} of "
                              f"{robust['summary'][k]['tested']}" for k in worse]) + "."
    text += " The variants are not weighted; none is treated as more likely than another."
    return text


def briefing(comparison: Comparison, metric: str = "overdue_value_hours", cascade: Cascade | None = None,
             backstop: BackstopResult | None = None, robust: dict[str, Any] | None = None) -> list[str]:
    """Markdown paragraphs answering the central question for ``comparison`` (B = the stress run)."""
    stress_run = comparison.runs["B"]
    scenario: Scenario = stress_run.scenario
    names = scenario.names()
    money = scenario.currency.format
    A, B = comparison.summaries["A"], comparison.summaries["B"]
    cascade = cascade or analyse_cascade(stress_run)
    c = cascade.summary()
    paragraphs: list[str] = []

    disruptions = [d for d in stress_run.effective_disruptions]
    phrase = _join([disruption_phrase(d, names) for d in disruptions]) or "nothing is disrupted"
    total = B["obligations_count"]
    if B["delayed_count"] == 0:
        paragraphs.append(f"**If** {phrase}, **no payment is delayed**: every one of the {total} obligations settles "
                          "on time, so there is nothing for an intervention to reduce.")
        paragraphs.append(f"_{DISCLAIMER}_")
        return paragraphs
    late, unsettled = B["late_count"], B["unsettled_count"]
    detail = f"{late} settle late" + (f" and {unsettled} are still unsettled when the {scenario.horizon_hours}-hour horizon ends"
                                      if unsettled else "")
    base_text = ("Without the disruption every payment settles on time." if A["delayed_count"] == 0 else
                 f"(Even without the disruption, {A['delayed_count']} are delayed.)")
    paragraphs.append(f"**If** {phrase}, **{B['delayed_count']} of {total} payments are delayed** "
                      f"({money(B['delayed_value'])}): {detail}. {base_text}")

    def verb(n: int) -> str:
        return "is" if n == 1 else "are"

    parts = []
    if c["direct_count"]:
        n = c["direct_count"]
        parts.append(f"{n} ({money(c['direct_value'])}) {verb(n)} held up **directly**: "
                     f"{'its' if n == 1 else 'their'} payment route needs a service that the disruption takes down or "
                     "saturates.")
    if c["propagated_count"]:
        n, depth = c["propagated_count"], c["max_generation"] or 1
        parts.append(f"{n} more ({money(c['propagated_value'])}) {verb(n)} held up **by contagion**: the payer's route "
                     "works, but it is short of cash while waiting for delayed incoming payments, up to "
                     f"{depth} step{'s' if depth != 1 else ''} away from the outage.")
    if c["unlinked_count"]:
        n = c["unlinked_count"]
        parts.append(f"{n} ({money(c['unlinked_value'])}) {verb(n)} delayed by a cash shortfall with no recorded link "
                     "back to the outage.")
    if parts:
        paragraphs.append("Of these, " + " ".join(parts))

    short = cascade.cash_short()
    if short:
        first = {p: f"from hour {cascade.participants[p].first_cash_short_hour}" for p in short}
        ordered = sorted(short, key=lambda p: (cascade.participants[p].first_cash_short_hour, p))
        text = (f"**{len(short)} participant{'s' if len(short) != 1 else ''} run{'s' if len(short) == 1 else ''} short of "
                f"cash**: {_names_list(ordered, names, first)}.")
        indirect = cascade.indirectly_affected()
        if indirect and len(indirect) == len(short):
            text += (" None of them lost a payment route of " + ("its" if len(short) == 1 else "their") + " own: the "
                     "shortfall reached " + ("it" if len(short) == 1 else "them") + " only through delayed incoming money.")
        elif indirect:
            text += (f" {len(indirect)} of them never lost a payment route of their own: the shortfall reached them "
                     "only through delayed incoming money.")
        paragraphs.append(text)
    else:
        paragraphs.append("**No participant runs short of cash**: every delay is operational, so liquidity alone "
                          "cannot shorten any of them.")

    ranking = comparison.ranking(metric)
    if ranking:
        label = RANKING_LABELS.get(metric, metric)

        def item(row: dict[str, Any]) -> str:
            name = comparison.labels[row["run"]].replace("C · ", "").split(" (")[0]
            pct = row["reduction_pct"]
            if pct is None:
                return name
            if row["reduction"] < 0:
                return f"{name} (**{abs(pct):.0f}% higher**)"
            return f"{name} ({pct:.0f}% lower)" if row["reduction"] > 0 else f"{name} (no change)"

        best = ranking[0]
        text = f"**Most effective single intervention in this run** (by {label}): {item(best)}"
        if len(ranking) > 1:
            text += ", then " + _join([item(r) for r in ranking[1:]])
        text += "."
        if best["reduction_pct"] is not None and best["reduction_pct"] <= 0:
            text = (f"**None of the configured interventions reduces the {label}** in this run; see the evidence "
                    "for why (for example a backup that shares the failed dependency).")
        buffer_key = next((k for k, iv in comparison.interventions.items() if iv.type == "liquidity_buffer"), None)
        credit_key = next((k for k, iv in comparison.interventions.items() if iv.type == "credit_line"), None)
        if buffer_key and credit_key:
            sb, sc = comparison.summaries[buffer_key], comparison.summaries[credit_key]
            if sb[metric] == sc[metric] and sc["credit_peak_total"] < sb["intervention_funding"]:
                text += (f" The credit line matches the buffer's result while drawing at most "
                         f"{money(sc['credit_peak_total'])} instead of pre-funding {money(sb['intervention_funding'])}.")
        offset_key = next((k for k, iv in comparison.interventions.items() if iv.type == "offsetting"), None)
        if offset_key:
            so = comparison.summaries[offset_key]
            if so["offsetting_batches"]:
                text += (f" Offsetting settled {so['settled_via_offsetting_count']} queued payment(s) worth "
                         f"{money(so['offsetting_gross_value'])} with only {money(so['offsetting_net_value'])} of cash "
                         "moving net: part of the delay was gridlock, not a lack of money.")
            elif B["blocked_cash_count"]:
                text += (" Offsetting found no gridlock to resolve: every cash-blocked payment needed money that its "
                         "payer did not have, whatever else was settled at the same time.")
        reserve_key = next((k for k, iv in comparison.interventions.items() if iv.type == "priority_reserve"), None)
        if reserve_key:
            sr = comparison.summaries[reserve_key]
            if sr[metric] > B[metric]:
                before, after = B["urgent_delayed_count"], sr["urgent_delayed_count"]
                urgent = (f"it does not reduce the number of delayed priority-1 payments ({after} of {sr['urgent_count']})"
                          if after == before else
                          f"delayed priority-1 payments {'fall' if after < before else 'rise'} from {before} to {after}")
                text += (f" Holding back routine payments to protect payroll and tax makes the {label} **worse**, and "
                         f"{urgent}.")
        packages = comparison.ranking(metric, include_packages=True)
        top = packages[0] if packages else None
        if top and top["run"].startswith("P:") and top["value"] < best["value"]:
            x = comparison.interaction(top["run"], metric)
            text += (f" Combining {comparison.labels[top['run']].replace('C · ', '').lower()} does better still "
                     f"({x['combined_pct']:.0f}% lower; {x['verdict']}).")
        paragraphs.append(text)

    if robust is not None and robust.get("metric") == metric:
        robust_text = robustness_paragraph(robust)
        if robust_text:
            paragraphs.append(robust_text)

    if backstop is not None and backstop.allocation:
        funded = sorted(backstop.allocation, key=lambda p: (-backstop.allocation[p], p))
        paragraphs.append(
            f"**What liquidity can and cannot do:** {money(backstop.total_allocation)} of hour-0 pre-funding, targeted at "
            f"{_names_list(funded, names, {p: money(backstop.allocation[p]) for p in funded})}, already achieves "
            "everything unlimited liquidity would"
            + (f"; {backstop.route_limited_count} obligation(s) ({money(backstop.route_limited_value)}) stay delayed "
               "regardless, because their routes are down or saturated." if backstop.route_limited_count else ".")
        )
    elif backstop is not None and backstop.route_limited_count:
        paragraphs.append("**What liquidity can and cannot do:** liquidity is not what delays payments here; "
                          f"{backstop.route_limited_count} obligation(s) are delayed by unavailable or saturated routes alone.")
    paragraphs.append(f"_{DISCLAIMER} Overdue value is not economic loss, and a temporary shortfall is not insolvency._")
    return paragraphs
