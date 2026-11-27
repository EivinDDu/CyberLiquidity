"""Independent integrity audit of a finished run (defence in depth for the engine's own checks).

The engine already stops with ``InvariantViolation`` if cash is not conserved, a balance
goes negative, a credit limit is exceeded or a service processes more than its capacity.
``audit(result)`` re-checks a *finished* result from the outside, using only the
recorded ledger and events, so a display or export never rests on an unchecked number:

* **ledger replay**: starting from the opening balances, applying every cash movement in
  order reproduces each entry's recorded balances and every end-of-hour balance;
* **numbering**: event and ledger ids run 1, 2, 3, ... and every ledger entry points to
  an earlier event in the same hour;
* **settlements**: each settled obligation has exactly one settlement event, in the
  hour the outcome reports, and unsettled ones have none;
* **totals**: the reported final cash equals the replayed cash.

It is linear in the size of the result (a few milliseconds for the built-in scenarios).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .engine import SimulationResult


@dataclass(frozen=True)
class AuditReport:
    checks: tuple[tuple[str, bool, str], ...]  # (check, passed, detail)
    entries: int = 0
    events: int = 0
    failures: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def summary(self) -> str:
        if self.ok:
            return (f"Integrity audit passed: {self.entries:,} cash movements replay to every recorded balance; "
                    f"{self.events:,} events are complete and consistent.")
        return "Integrity audit FAILED: " + "; ".join(self.failures[:5])


def audit(result: SimulationResult) -> AuditReport:
    problems: dict[str, list[str]] = {"ledger replay": [], "numbering": [], "settlements": [], "totals": []}
    balances = dict(result.initial_balances)
    hours = result.hours
    events = result.events

    for index, event in enumerate(events, start=1):
        if event.event_id != index:
            problems["numbering"].append(f"event {index} has id {event.event_id}")
            break

    position = 0
    ledger = result.ledger
    for hour, snapshot in enumerate(hours):
        while position < len(ledger) and ledger[position].hour == hour:
            entry = ledger[position]
            position += 1
            if entry.entry_id != position:
                problems["numbering"].append(f"ledger entry {position} has id {entry.entry_id}")
            if not 1 <= entry.event_id <= len(events) or events[entry.event_id - 1].hour != hour:
                problems["numbering"].append(f"ledger entry {entry.entry_id} points to event {entry.event_id}")
            if entry.amount <= 0:
                problems["ledger replay"].append(f"entry {entry.entry_id} moves a non-positive amount")
            if entry.debit_account in balances:
                balances[entry.debit_account] -= entry.amount
                if balances[entry.debit_account] != entry.debit_balance_after:
                    problems["ledger replay"].append(f"entry {entry.entry_id}: payer balance does not replay")
            if entry.credit_account in balances:
                balances[entry.credit_account] += entry.amount
                if balances[entry.credit_account] != entry.credit_balance_after:
                    problems["ledger replay"].append(f"entry {entry.entry_id}: recipient balance does not replay")
        if balances != snapshot.balances:
            problems["ledger replay"].append(f"balances at the end of hour {hour} do not replay")
        if any(value < 0 for value in balances.values()):
            problems["ledger replay"].append(f"a balance is negative at the end of hour {hour}")
    if position != len(ledger):
        problems["ledger replay"].append(f"{len(ledger) - position} ledger entries are outside the horizon or out of order")

    settlements = Counter(e.obligation_id for e in events if e.type == "SETTLEMENT")
    for oid, outcome in result.outcomes.items():
        count = settlements.get(oid, 0)
        if outcome.settled_hour is None:
            if count:
                problems["settlements"].append(f"{oid} is unsettled but has a settlement event")
        elif count != 1:
            problems["settlements"].append(f"{oid} has {count} settlement events")
        elif events[outcome.settlement_event_id - 1].hour != outcome.settled_hour:
            problems["settlements"].append(f"{oid} settled in a different hour than its event")
    unknown = set(settlements) - set(result.outcomes)
    if unknown:
        problems["settlements"].append(f"settlement events for unknown obligations: {sorted(unknown)[:3]}")

    if sum(balances.values()) != result.totals["final_internal_cash"]:
        problems["totals"].append("reported final cash differs from the replayed cash")

    checks = tuple((name, not found, "; ".join(found[:3])) for name, found in problems.items())
    failures = tuple(f"{name}: {found[0]}" for name, found in problems.items() if found)
    return AuditReport(checks=checks, entries=len(ledger), events=len(events), failures=failures)
