"""Liquidity diagnostics: the buffer grid search and the exact liquidity backstop.

``buffer_sweep`` reruns a stressed scenario with each buffer size in a user-supplied
grid and reports the *smallest tested* buffer that achieves a selected outcome. This is
not a proven minimum: a smaller untested amount might also work, and outcomes need not
be monotone in the buffer size. A cash-unconstrained reference run identifies delays
that no amount of liquidity can remove because the payment route itself is unavailable.

``liquidity_backstop`` answers one narrower question exactly: which hour-0 pre-funding
per participant makes cash non-binding for it, so that insufficient cash *alone* never
holds up any of its payments? It runs the stressed scenario once with a non-binding,
on-demand credit line and reads each participant's peak draw. docs/MODEL.md section 7.5
shows that pre-funding exactly these peaks reproduces the credit run (sufficiency). With
``next_step`` recycling the peaks are also the smallest such amounts: one minor unit less
for any participant brings back a cash block for it. With ``within_step`` recycling they
are an upper bound, because a payer can sometimes wait for money arriving later in the
same hour. Both properties are re-checked by extra simulation runs every time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .engine import OPERATIONAL_REASONS, BlockRecord, SimulationResult, Status, simulate, simulate_until
from .metrics import summarize
from .models import (
    BufferAllocation,
    CreditLineIntervention,
    LiquidityBufferIntervention,
    Obligation,
    PriorityReserveIntervention,
    Scenario,
)
from .variants import cash_unconstrained, make_buffer, make_credit_line, outgoing_totals, with_intervention

OUTCOMES = {
    "no_cash_blocking": "No obligation is ever blocked by insufficient cash",
    "selected_payers_on_time": "Every obligation paid by the buffered participants settles on time",
    "priority_on_time": "Every priority-1 obligation (e.g. payroll, tax) settles on time",
    "all_on_time": "Every obligation settles on time",
    "no_unsettled_at_horizon": "No obligation is still unsettled at the horizon",
}


def outcome_achieved(result: SimulationResult, outcome: str, participants: Sequence[str]) -> bool:
    outcomes = result.outcomes.values()
    if outcome == "no_cash_blocking":
        return not any(b.cash for b in result.blocks)
    if outcome == "selected_payers_on_time":
        chosen = set(participants)
        return all(o.status is Status.SETTLED_ON_TIME for o in outcomes if o.payer in chosen)
    if outcome == "priority_on_time":
        return all(o.status is Status.SETTLED_ON_TIME for o in outcomes if o.priority == 1)
    if outcome == "all_on_time":
        return all(o.status is Status.SETTLED_ON_TIME for o in outcomes)
    if outcome == "no_unsettled_at_horizon":
        return all(o.status is not Status.UNSETTLED_AT_HORIZON for o in outcomes)
    raise ValueError(f"unknown outcome '{outcome}'; choose from {sorted(OUTCOMES)}")


@dataclass(frozen=True)
class SweepPoint:
    amount_each: int
    total_supplied: int
    achieved: bool
    summary: dict[str, Any]


@dataclass(frozen=True)
class SweepResult:
    outcome: str
    outcome_label: str
    participants: tuple[str, ...]
    points: tuple[SweepPoint, ...]
    smallest_achieving: SweepPoint | None
    reference_achieves: bool  # outcome reached when cash can never bind
    route_limited: tuple[dict[str, Any], ...]  # still delayed with cash non-binding
    reference_summary: dict[str, Any]

    def verdict(self, money) -> str:
        if self.smallest_achieving is not None:
            p = self.smallest_achieving
            return (f"Smallest tested buffer achieving the outcome: {money(p.amount_each)} per participant "
                    f"({money(p.total_supplied)} in total). Not a proven minimum - only grid values were tested.")
        if not self.reference_achieves:
            return ("No tested buffer achieves the outcome, and it is not achievable by liquidity at all: even with cash "
                    f"made non-binding for every participant, {len(self.route_limited)} obligation(s) remain delayed "
                    "because their payment routes are unavailable or saturated.")
        return ("No tested buffer achieves the outcome, although it is achievable when cash is non-binding for every "
                "participant: extend the grid or buffer other participants.")


def buffer_sweep(stressed: Scenario, participants: Sequence[str], grid: Sequence[int],
                 outcome: str = "no_cash_blocking", funding_hour: int = 0) -> SweepResult:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome '{outcome}'")
    chosen = tuple(dict.fromkeys(participants))
    if not chosen:
        raise ValueError("select at least one participant to buffer")
    points = []
    for amount in sorted({int(a) for a in grid if int(a) >= 0}):
        scenario = stressed if amount == 0 else with_intervention(
            stressed, make_buffer(chosen, amount, funding_hour=funding_hour, id="sweep_buffer"))
        result = simulate(scenario)
        points.append(SweepPoint(amount, amount * len(chosen), outcome_achieved(result, outcome, chosen), summarize(result)))
    smallest = next((p for p in points if p.achieved), None)

    reference = simulate(cash_unconstrained(stressed))
    route_limited = tuple(
        {
            "obligation_id": o.obligation_id,
            "payer": o.payer,
            "recipient": o.recipient,
            "amount": o.amount,
            "due_hour": o.due_hour,
            "status": o.status.value,
            "delay_hours": o.delay_hours,
            "operational_reasons": sorted(r for r in o.reason_hours if r in OPERATIONAL_REASONS),
        }
        for o in sorted(reference.outcomes.values(), key=lambda o: (-o.amount, o.obligation_id))
        if o.delayed
    )
    return SweepResult(
        outcome=outcome,
        outcome_label=OUTCOMES[outcome],
        participants=chosen,
        points=tuple(points),
        smallest_achieving=smallest,
        reference_achieves=outcome_achieved(reference, outcome, chosen),
        route_limited=route_limited,
        reference_summary=summarize(reference),
    )


# ------------------------------------------------------------ exact backstop

BACKSTOP_ID = "backstop_credit"


@dataclass(frozen=True)
class BackstopResult:
    participants: tuple[str, ...]  # covered by the diagnostic (non-binding) credit line
    rows: tuple[dict[str, Any], ...]  # one per participant that drew on the credit line, largest allocation first
    peaks: dict[str, int]  # peak draw per participant on the non-binding credit line
    allocation: dict[str, int]  # smallest pre-funding found per participant (verified, see verdict)
    credit_summary: dict[str, Any]
    stress_summary: dict[str, Any]
    verified_sufficient: bool  # the allocation reproduces the unlimited-liquidity outcome
    tight: tuple[str, ...]  # one minor unit less for this participant changes the outcome
    not_tight: tuple[str, ...]
    route_limited_count: int  # still delayed with unlimited liquidity: liquidity cannot fix these
    route_limited_value: int
    runs_used: int  # simulation runs started (a repeated trial reuses its earlier run)
    recycling: str = "within_step"
    runs_stopped_early: int = 0  # trial runs abandoned once their outcome could no longer match

    @property
    def total_peak(self) -> int:
        return sum(self.peaks.values())

    @property
    def total_allocation(self) -> int:
        return sum(self.allocation.values())

    @property
    def all_tight(self) -> bool | None:
        return None if not (self.tight or self.not_tight) else not self.not_tight

    def verdict(self, money) -> str:
        funded = [p for p, v in self.allocation.items() if v > 0]
        if not funded:
            text = ("No pre-funding is needed: this run already reaches the outcome it would have with unlimited "
                    "liquidity, so liquidity is not what delays its payments.")
            if self.route_limited_count:
                text += (f" {self.route_limited_count} obligation(s) ({money(self.route_limited_value)}) are delayed by "
                         "unavailable or saturated routes alone.")
            return text
        text = (f"{len(funded)} participant(s) need {money(self.total_allocation)} of hour-0 pre-funding in total so that "
                "every payment settles exactly as it would with unlimited liquidity for them.")
        if self.verified_sufficient and self.all_tight:
            text += (" Found by search and re-checked by simulation: one minor unit less for any of them changes the "
                     "outcome (a locally smallest allocation; it is not a forecast of real liquidity needs).")
        elif self.verified_sufficient:
            text += (f" Re-checked by simulation; for {len(self.not_tight)} participant(s) a slightly smaller amount "
                     "might also work (outcomes need not be monotone in the amount).")
        if self.total_peak != self.total_allocation:
            text += (f" Peak draws on an unlimited credit line total {money(self.total_peak)}: a credit line is drawn the "
                     "moment a payment is reached in the queue, while pre-funded cash can wait for money arriving "
                     "later in the same hour.")
        if self.route_limited_count:
            text += (f" {self.route_limited_count} obligation(s) ({money(self.route_limited_value)}) stay delayed regardless, "
                     "because their routes are unavailable or saturated.")
        return text


def _allocation_scenario(stressed: Scenario, amounts: dict[str, int], id: str) -> Scenario:
    allocations = tuple(BufferAllocation(participant=p, amount=a) for p, a in sorted(amounts.items()) if a > 0)
    if not allocations:
        return stressed
    return with_intervention(stressed, LiquidityBufferIntervention(
        id=id, allocations=allocations, funding_hour=0,
        funder="Diagnostic pre-funding (outside the accounting boundary)",
        description="Diagnostic: pre-funding found by the liquidity backstop analysis."))


def liquidity_backstop(stressed: Scenario, participants: Sequence[str] | None = None,
                       refine: bool = True, max_passes: int = 3) -> BackstopResult:
    """Smallest hour-0 pre-funding per participant that reproduces the unlimited-liquidity outcome.

    1. Run ``stressed`` with a non-binding on-demand credit line for the covered
       participants (default: everyone with outgoing obligations). Its outcome is the best
       that liquidity for these participants can achieve.
    2. Pre-funding each participant with its peak draw reproduces that outcome exactly
       (docs/MODEL.md section 7.5); this is re-checked by a run.
    3. With ``refine``, each amount is lowered by binary search, keeping the outcome
       identical, and every final amount is re-checked: one minor unit less must change
       the outcome. The result is locally smallest, not necessarily the smallest total.

    Each trial asks only "does this allocation reproduce the credit run?", so a trial run
    is abandoned at the end of the first hour in which the answer is certainly no: a
    payment settles in a different hour than in the credit run, a payment the credit run
    settled in that hour is still queued, or a covered participant is blocked by cash
    alone. A trial whose allocation was already tried reuses that answer. Neither changes
    which allocations are tried or the result; both only save simulation time.
    """
    if any(isinstance(iv, CreditLineIntervention) for iv in stressed.interventions):
        raise ValueError("the backstop diagnostic needs a scenario without an applied credit line")
    if any(isinstance(iv, PriorityReserveIntervention) for iv in stressed.interventions):
        # A reserve rule counts undrawn credit as cover for upcoming protected payments, so a
        # non-binding credit line's peak draws do not bound the cash a pre-funded buffer needs.
        raise ValueError("the backstop diagnostic does not support an applied priority-reserve rule "
                         "(its sufficiency argument assumes payments need only their own amount in cash)")
    limits = outgoing_totals(stressed)
    if participants is not None:
        chosen = set(participants)
        limits = {p: v for p, v in limits.items() if p in chosen}
    recycling = stressed.settings.liquidity_recycling
    stress_result = simulate(stressed)
    runs = 1
    if not limits:
        summary = summarize(stress_result)
        return BackstopResult((), (), {}, {}, summary, summary, True, (), (), 0, 0, runs, recycling)
    credit_run = simulate(with_intervention(stressed, make_credit_line(
        limits, available_from_hour=0, id=BACKSTOP_ID,
        provider="Diagnostic credit line (non-binding limit)",
        description="Diagnostic only: limits equal each participant's total outgoing obligations.")))
    runs += 1
    peaks = {p: v for p, v in credit_run.credit_peak.items() if v > 0}
    covered = set(limits)
    target = {k: (o.status, o.settled_hour) for k, o in credit_run.outcomes.items()}
    target_hour = {k: o.settled_hour for k, o in credit_run.outcomes.items()}
    stopped = 0
    tried: dict[tuple[tuple[str, int], ...], bool] = {}

    def cannot_match(hour: int, settled: list[Obligation], blocked: list[BlockRecord]) -> bool:
        # Each condition makes the final comparison below fail, so stopping here is exact.
        if any(target_hour[ob.id] != hour for ob in settled):
            return True
        return any(target_hour[b.obligation_id] == hour or (b.cash and not b.operational and b.payer in covered)
                   for b in blocked)

    def sufficient(amounts: dict[str, int]) -> bool:
        nonlocal runs, stopped
        key = tuple(sorted((p, a) for p, a in amounts.items() if a > 0))  # zero amounts fund nothing
        if key in tried:
            return tried[key]
        runs += 1
        result = simulate_until(_allocation_scenario(stressed, amounts, "backstop_prefunding"), cannot_match)
        if result is None:
            stopped += 1
            ok = False
        elif any((o.status, o.settled_hour) != target[k] for k, o in result.outcomes.items()):
            ok = False
        else:
            ok = not any(b.cash and not b.operational and b.payer in covered for b in result.blocks)
        tried[key] = ok
        return ok

    allocation = dict(peaks)
    verified = sufficient(allocation) if peaks else True
    if refine and verified:
        for _ in range(max(1, max_passes)):
            changed = False
            for p in sorted(allocation, key=lambda x: (-allocation[x], x)):
                if allocation[p] == 0:
                    continue
                trial = dict(allocation, **{p: 0})
                if sufficient(trial):
                    allocation[p], changed = 0, True
                    continue
                lo, hi = 0, allocation[p]  # insufficient at lo, sufficient at hi
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if sufficient(dict(allocation, **{p: mid})):
                        hi = mid
                    else:
                        lo = mid
                if hi != allocation[p]:
                    allocation[p], changed = hi, True
            if not changed:
                break
        verified = sufficient(allocation)

    tight, not_tight = [], []
    for p in sorted(allocation):
        if allocation[p] > 0:
            (tight if not sufficient(dict(allocation, **{p: allocation[p] - 1})) else not_tight).append(p)

    first_draw: dict[str, int] = {}
    draws: dict[str, int] = {}
    for e in credit_run.events:
        if e.type == "CREDIT_DRAW":
            first_draw.setdefault(e.participant_id, e.hour)
            draws[e.participant_id] = draws.get(e.participant_id, 0) + 1
    rows = tuple(
        {
            "participant": p,
            "allocation": allocation.get(p, 0),
            "peak_draw": peaks[p],
            "first_draw_hour": first_draw.get(p),
            "draws": draws.get(p, 0),
            "stress_cash_blocked_hours": len({b.hour for b in stress_result.blocks if b.payer == p and b.cash}),
            "tight": (p in tight) if allocation.get(p, 0) > 0 else None,
        }
        for p in sorted(peaks, key=lambda p: (-allocation.get(p, 0), -peaks[p], p))
    )
    still = [o for o in credit_run.outcomes.values() if o.delayed]
    return BackstopResult(
        participants=tuple(sorted(covered)),
        rows=rows,
        peaks=dict(sorted(peaks.items())),
        allocation={p: v for p, v in sorted(allocation.items()) if v > 0},
        credit_summary=summarize(credit_run),
        stress_summary=summarize(stress_result),
        verified_sufficient=verified,
        tight=tuple(tight),
        not_tight=tuple(not_tight),
        route_limited_count=len(still),
        route_limited_value=sum(o.amount for o in still),
        runs_used=runs,
        recycling=recycling,
        runs_stopped_early=stopped,
    )
