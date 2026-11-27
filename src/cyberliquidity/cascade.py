"""Contagion structure of one run: how far payment delays travel from the disruption.

Built only from the recorded blocking records (docs/MODEL.md section 6.5):

* **direct** (generation 0): a delayed obligation that was blocked at least once by an
  operational reason (unavailable service, exhausted capacity, backup not yet active);
* **propagated** (generation k >= 1): a delayed obligation that was only ever blocked by
  insufficient cash while its payer was owed overdue incoming payments, and that is
  reachable in k such steps from a direct delay;
* **unlinked**: delayed only by insufficient cash with no recorded path back to a direct
  delay (for example a shortfall without overdue inflows).

A link "u -> v" means: when v was cash-blocked, u was among its payer's overdue incoming
payments. It is a recorded dependency, not a counterfactual proof that u alone caused v
(compare against the baseline run for that). Generations are shortest recorded paths,
so cycles of mutual waiting (gridlock) cannot inflate them.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .engine import SimulationResult

KINDS = ("direct", "propagated", "unlinked")
KIND_LABELS = {
    "direct": "Direct: blocked by the operational disruption",
    "propagated": "Propagated: payer short of cash while waiting for delayed inflows",
    "unlinked": "Cash shortfall with no recorded link to the disruption",
}


@dataclass(frozen=True)
class CascadeNode:
    obligation_id: str
    payer: str
    recipient: str
    amount: int
    purpose: str
    kind: str
    generation: int | None
    parents: tuple[str, ...]
    overdue_hours: int
    status: str
    root_services: tuple[str, ...] = ()  # direct delays: services named in the first operational block

    @property
    def value_hours(self) -> int:
        return self.amount * self.overdue_hours


@dataclass(frozen=True)
class ParticipantExposure:
    participant: str
    cash_short: bool
    first_cash_short_hour: int | None
    cash_short_hours: int
    largest_shortfall: int | None
    route_blocked: bool  # at least one of its own payments was held by an operational reason
    indirect: bool  # cash short although none of its payments was ever route-blocked
    generation: int | None  # earliest generation among its delayed outgoing payments
    delayed_out_count: int
    delayed_out_value: int
    direct_out_count: int
    propagated_out_count: int
    delayed_in_count: int
    delayed_in_value: int
    min_balance: int


@dataclass
class Cascade:
    nodes: dict[str, CascadeNode]
    participants: dict[str, ParticipantExposure]
    links: list[dict[str, Any]]  # delayed payments aggregated by (payer, recipient)
    by_generation: list[dict[str, Any]]
    max_generation: int | None
    _children: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def direct(self) -> list[CascadeNode]:
        return [n for n in self.nodes.values() if n.kind == "direct"]

    @property
    def propagated(self) -> list[CascadeNode]:
        return [n for n in self.nodes.values() if n.kind == "propagated"]

    def indirectly_affected(self) -> list[str]:
        """Participants left short of cash although none of their own payment routes failed."""
        return sorted(p for p, x in self.participants.items() if x.indirect)

    def cash_short(self) -> list[str]:
        return sorted(p for p, x in self.participants.items() if x.cash_short)

    def children(self, obligation_id: str) -> list[str]:
        return list(self._children.get(obligation_id, ()))

    def summary(self) -> dict[str, Any]:
        direct, propagated = self.direct, self.propagated
        unlinked = [n for n in self.nodes.values() if n.kind == "unlinked"]
        total_vh = sum(n.value_hours for n in self.nodes.values())
        return {
            "delayed_count": len(self.nodes),
            "direct_count": len(direct),
            "direct_value": sum(n.amount for n in direct),
            "propagated_count": len(propagated),
            "propagated_value": sum(n.amount for n in propagated),
            "unlinked_count": len(unlinked),
            "unlinked_value": sum(n.amount for n in unlinked),
            "max_generation": self.max_generation,
            "propagated_value_hours_share": (sum(n.value_hours for n in propagated) / total_vh) if total_vh else None,
            "cash_short_participants": self.cash_short(),
            "indirectly_affected_participants": self.indirectly_affected(),
        }


def analyse_cascade(result: SimulationResult) -> Cascade:
    blocks = result.blocks_by_obligation()
    obligations = result.scenario.obligations_by_id()
    delayed = [o for o in result.outcomes.values() if o.delayed]

    kind: dict[str, str] = {}
    parents: dict[str, tuple[str, ...]] = {}
    roots: dict[str, tuple[str, ...]] = {}
    for o in delayed:
        records = blocks.get(o.obligation_id, [])
        operational = [b for b in records if b.operational]
        if operational:
            kind[o.obligation_id] = "direct"
            services: list[str] = []
            for check in operational[0].route_checks:
                for sid in check.get("services", ()):
                    if sid not in services:
                        services.append(sid)
            roots[o.obligation_id] = tuple(services)
            parents[o.obligation_id] = ()
            continue
        linked: dict[str, None] = {}
        for b in records:
            if b.cash:
                for inflow_id, _ in b.overdue_inflows:
                    linked.setdefault(inflow_id, None)
        parents[o.obligation_id] = tuple(linked)
        kind[o.obligation_id] = "propagated" if linked else "unlinked"

    children: dict[str, list[str]] = defaultdict(list)
    for child, ps in parents.items():
        for parent in ps:
            children[parent].append(child)

    # Breadth-first from every direct delay: generation = shortest recorded path length.
    generation: dict[str, int] = {}
    queue: deque[str] = deque()
    for oid in sorted(k for k, v in kind.items() if v == "direct"):
        generation[oid] = 0
        queue.append(oid)
    while queue:
        current = queue.popleft()
        for child in sorted(children.get(current, ())):
            if child not in generation and kind.get(child) == "propagated":
                generation[child] = generation[current] + 1
                queue.append(child)

    nodes: dict[str, CascadeNode] = {}
    for o in delayed:
        oid = o.obligation_id
        k = kind[oid]
        if k == "propagated" and oid not in generation:
            k = "unlinked"  # waits only on delays that never trace back to the disruption (e.g. gridlock)
        nodes[oid] = CascadeNode(
            obligation_id=oid, payer=o.payer, recipient=o.recipient, amount=o.amount,
            purpose=obligations[oid].purpose, kind=k, generation=generation.get(oid),
            parents=parents[oid], overdue_hours=o.overdue_hours, status=o.status.value,
            root_services=roots.get(oid, ()),
        )

    max_generation = max((n.generation for n in nodes.values() if n.generation is not None), default=None)
    by_generation = []
    if max_generation is not None:
        for g in range(max_generation + 1):
            group = [n for n in nodes.values() if n.generation == g]
            by_generation.append({"generation": g, "kind": "direct" if g == 0 else "propagated",
                                  "count": len(group), "value": sum(n.amount for n in group),
                                  "value_hours": sum(n.value_hours for n in group),
                                  "payers": sorted({n.payer for n in group})})
    unlinked = [n for n in nodes.values() if n.generation is None]
    if unlinked:
        by_generation.append({"generation": None, "kind": "unlinked", "count": len(unlinked),
                              "value": sum(n.amount for n in unlinked),
                              "value_hours": sum(n.value_hours for n in unlinked),
                              "payers": sorted({n.payer for n in unlinked})})

    exposure: dict[str, ParticipantExposure] = {}
    by_payer: dict[str, list] = defaultdict(list)
    for b in result.blocks:
        by_payer[b.payer].append(b)
    for p in result.scenario.participants:
        records = by_payer.get(p.id, [])
        cash = [b for b in records if b.cash]
        out_nodes = [n for n in nodes.values() if n.payer == p.id]
        in_nodes = [n for n in nodes.values() if n.recipient == p.id]
        route_blocked = any(b.operational for b in records)
        generations = [n.generation for n in out_nodes if n.generation is not None]
        exposure[p.id] = ParticipantExposure(
            participant=p.id,
            cash_short=bool(cash),
            first_cash_short_hour=min((b.hour for b in cash), default=None),
            cash_short_hours=len({b.hour for b in cash}),
            largest_shortfall=max((b.shortfall for b in cash if b.shortfall is not None), default=None),
            route_blocked=route_blocked,
            indirect=bool(cash) and not route_blocked,
            generation=min(generations) if generations else None,
            delayed_out_count=len(out_nodes),
            delayed_out_value=sum(n.amount for n in out_nodes),
            direct_out_count=sum(1 for n in out_nodes if n.kind == "direct"),
            propagated_out_count=sum(1 for n in out_nodes if n.kind == "propagated"),
            delayed_in_count=len(in_nodes),
            delayed_in_value=sum(n.amount for n in in_nodes),
            min_balance=min([result.initial_balances[p.id]] + [h.balances[p.id] for h in result.hours]),
        )

    grouped: dict[tuple[str, str], list[CascadeNode]] = defaultdict(list)
    for n in nodes.values():
        grouped[(n.payer, n.recipient)].append(n)
    links = [
        {
            "payer": payer,
            "recipient": recipient,
            "count": len(group),
            "value": sum(n.amount for n in group),
            "kinds": sorted({n.kind for n in group}, key=KINDS.index),
            "min_generation": min((n.generation for n in group if n.generation is not None), default=None),
            "max_overdue_hours": max(n.overdue_hours for n in group),
            "obligations": sorted(n.obligation_id for n in group),
        }
        for (payer, recipient), group in sorted(grouped.items())
    ]
    return Cascade(nodes, exposure, links, by_generation, max_generation, dict(children))
