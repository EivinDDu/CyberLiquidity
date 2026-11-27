"""Graphviz DOT builders for the two networks and their coupling.

The operational and financial networks are drawn as separate views with distinct
node shapes: services are rounded boxes, payment routes are arrow-shaped nodes,
participants are filled ellipses and external parties are dashed ellipses. State is
shown with a status colour *and* an icon/label, never colour alone.
"""

from __future__ import annotations

from collections import defaultdict

from cyberliquidity.engine import SimulationResult
from cyberliquidity.models import Scenario
from cyberliquidity.operational import ServiceStatus

from .theme import STATUS_ICON, Palette

_FONT = "Helvetica"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _label(*lines: str) -> str:
    return '"' + "\\n".join(_esc(line) for line in lines if line) + '"'


def _header(pal: Palette, rankdir: str) -> list[str]:
    return [
        "digraph G {",
        f'graph [rankdir={rankdir}, bgcolor="transparent", fontname="{_FONT}", nodesep=0.35, ranksep=0.6, pad=0.25];',
        f'node [fontname="{_FONT}", fontsize=11, fontcolor="{pal.text}", color="{pal.axis}", penwidth=1.4, margin="0.12,0.06"];',
        f'edge [fontname="{_FONT}", fontsize=9, fontcolor="{pal.text2}", color="{pal.text2}", arrowsize=0.6, penwidth=1.0];',
    ]


def _service_node(sid: str, name: str, st: ServiceStatus, used: int, disrupted: bool, pal: Palette) -> str:
    state = st.state.value
    status_line = f"{STATUS_ICON[state]} {state}" + (f" ({st.pct}%)" if state == "degraded" else "")
    if st.nominal_capacity is None:
        cap_line = ""
    elif st.capacity == 0:
        cap_line = "no capacity this hour"
    else:
        cap_line = f"{used} of {st.capacity} payments/h used"
    extra = "◆ directly disrupted" if disrupted else ""
    return (f'"{_esc(sid)}" [label={_label(name, status_line, cap_line, extra)}, shape=box, style="rounded,filled", '
            f'fillcolor="{pal.status_fill[state]}", color="{pal.status[state]}", '
            f'penwidth={2.4 if state != "available" else 1.4}, peripheries={2 if disrupted else 1}];')


def operational_dot(scenario: Scenario, status: dict[str, ServiceStatus], used: dict[str, int],
                    disrupted: set[str], pal: Palette) -> str:
    """Operational dependencies: edge A -> B means 'A depends on B' (failure travels against the arrow)."""
    lines = _header(pal, "TB")
    route_ids = []
    for route in scenario.routes:
        usable = all(status[s].usable() for s in route.path)
        state = "available" if usable else "unavailable"
        icon = STATUS_ICON[state]
        route_ids.append(route.id)
        lines.append(
            f'"{_esc(route.id)}" [label={_label(route.name, f"{icon} route " + ("usable" if usable else "not usable"))}, '
            f'shape=cds, style=filled, fillcolor="{pal.status_fill["neutral"]}", color="{pal.status[state]}", penwidth=1.6];'
        )
    lines.append("{rank=source; " + " ".join(f'"{_esc(r)}"' for r in route_ids) + "}")
    for svc in scenario.services:
        lines.append(_service_node(svc.id, svc.name, status[svc.id], used.get(svc.id, 0), svc.id in disrupted, pal))
    for route in scenario.routes:
        for step, sid in enumerate(route.path, start=1):
            lines.append(f'"{_esc(route.id)}" -> "{_esc(sid)}" [label="path {step}", color="{pal.axis}", fontcolor="{pal.text2}"];')
    for svc in scenario.services:
        for dep in svc.requires_all:
            lines.append(f'"{_esc(svc.id)}" -> "{_esc(dep)}" [label="requires"];')
        for group in svc.requires_any:
            for dep in group:
                lines.append(f'"{_esc(svc.id)}" -> "{_esc(dep)}" [label="any of", style=dashed];')
    lines.append("}")
    return "\n".join(lines)


def financial_dot(result: SimulationResult, hour: int, pal: Palette, only_delayed: bool = True) -> str:
    """Financial obligations aggregated by payer -> recipient over the horizon, coloured by state at ``hour``."""
    s = result.scenario
    internal = s.internal_ids()
    money = s.currency.format
    blocks_now = {b.obligation_id: b for b in result.blocks if b.hour == hour}
    pairs: dict[tuple[str, str], list] = defaultdict(list)
    for ob in s.obligations:
        pairs[(ob.payer, ob.recipient)].append(ob)

    lines = _header(pal, "LR")
    shown: set[str] = set(internal)
    edges = []
    max_value = max((sum(o.amount for o in obs) for obs in pairs.values()), default=1)
    for (payer, recipient), obs in sorted(pairs.items()):
        delayed = [o for o in obs if result.outcomes[o.id].delayed]
        if only_delayed and not delayed:
            continue
        now = [blocks_now[o.id] for o in obs if o.id in blocks_now]
        value = sum(o.amount for o in obs)
        if any(b.operational for b in now):
            colour, note = pal.status["unavailable"], f"✕ {len(now)} overdue now"
        elif now:
            colour, note = pal.status["degraded"], f"⚠ {len(now)} overdue now (cash)"
        elif delayed:
            colour, note = pal.text2, f"{len(delayed)} delayed during run"
        else:
            colour, note = pal.axis, ""
        width = 1.0 + 3.0 * value / max_value
        label = _label(f"{money(value)} · {len(obs)} payment(s)", note)
        edges.append(f'"{_esc(payer)}" -> "{_esc(recipient)}" [label={label}, color="{colour}", penwidth={width:.2f}];')
        shown.update((payer, recipient))

    balances = result.hours[hour].balances
    for p in s.participants:
        out_now = [b for b in blocks_now.values() if b.payer == p.id]
        cash = sum(1 for b in out_now if b.cash)
        route = sum(1 for b in out_now if b.operational)
        if cash:
            colour, fill, flag = pal.status["degraded"], pal.status_fill["degraded"], f"⚠ {cash} payment(s) cash-blocked"
        elif route:
            colour, fill, flag = pal.status["unavailable"], pal.status_fill["unavailable"], f"✕ {route} payment(s) route-blocked"
        else:
            colour, fill, flag = pal.axis, pal.status_fill["neutral"], ""
        extra = f"✕ {route} route-blocked" if cash and route else ""
        lines.append(
            f'"{_esc(p.id)}" [label={_label(p.name, "balance " + money(balances[p.id]), flag, extra)}, shape=ellipse, '
            f'style=filled, fillcolor="{fill}", color="{colour}", penwidth={2.2 if flag else 1.2}];'
        )
    for e in s.external_parties:
        if e.id in shown:
            lines.append(f'"{_esc(e.id)}" [label={_label(e.name, "outside boundary")}, shape=ellipse, style=dashed, '
                         f'fontcolor="{pal.text2}", color="{pal.axis}"];')
    lines.extend(edges)
    lines.append("}")
    return "\n".join(lines)


def routing_dot(result: SimulationResult, hour: int, pal: Palette) -> str:
    """Coupling view: which participants use which payment routes, and which services each route needs."""
    s = result.scenario
    status = result.service_status[hour]
    internal = s.internal_ids()
    usage: dict[tuple[str, str, bool], int] = defaultdict(int)
    for ob in s.obligations:
        party = ob.payer if ob.payer in internal else ob.recipient
        for route_id, _active_from in result.eligible_routes[ob.id]:
            usage[(party, route_id, route_id not in ob.routes)] += 1
    lines = _header(pal, "LR")
    for p in s.participants:
        lines.append(f'"{_esc(p.id)}" [label={_label(p.name)}, shape=ellipse, style=filled, '
                     f'fillcolor="{pal.status_fill["neutral"]}", color="{pal.axis}"];')
    for route in s.routes:
        usable = all(status[x].usable() for x in route.path)
        state = "available" if usable else "unavailable"
        lines.append(f'"{_esc(route.id)}" [label={_label(route.name, STATUS_ICON[state] + (" usable" if usable else " not usable"))}, '
                     f'shape=cds, style=filled, fillcolor="{pal.status_fill["neutral"]}", color="{pal.status[state]}", penwidth=1.6];')
    used = result.hours[hour].service_used
    for svc in s.services:
        if any(svc.id in r.path for r in s.routes):
            lines.append(_service_node(svc.id, svc.name, status[svc.id], used.get(svc.id, 0), False, pal))
    for (party, route_id, is_backup), count in sorted(usage.items()):
        style = f', style=dashed, label="backup ({count})"' if is_backup else f', label="{count}"'
        lines.append(f'"{_esc(party)}" -> "{_esc(route_id)}" [color="{pal.text2}"{style}];')
    for route in s.routes:
        for step, sid in enumerate(route.path, start=1):
            lines.append(f'"{_esc(route.id)}" -> "{_esc(sid)}" [label="path {step}", color="{pal.axis}"];')
    lines.append("}")
    return "\n".join(lines)


def _compact(minor: int, currency) -> str:
    value = currency.to_major(minor)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= size:
            return f"{value / size:.{0 if abs(value) >= 100 * size else 1}f}{suffix} {currency.code}"
    return f"{value:,.0f} {currency.code}"


def cascade_dot(cascade, result: SimulationResult, pal: Palette) -> str:
    """Contagion map, top to bottom: disruption -> payers hit directly -> recipients left short of cash -> ...

    Edges are delayed payments aggregated by payer -> recipient and coloured by how they were
    held up (direct = operational colour, propagated = cash-awaiting-inflows colour); labels
    give value and count, tooltips the details. Participants that ran short of cash carry a
    warning outline and the step at which the disruption reached them.
    """
    from .theme import KIND_CATEGORY

    s = result.scenario
    names = s.names()
    money = s.currency.format
    internal = s.internal_ids()
    lines = _header(pal, "TB")
    lines[1] = lines[1].replace("nodesep=0.35, ranksep=0.6", "nodesep=0.45, ranksep=0.55")

    disruptions = [d for d in result.effective_disruptions if d.duration_hours or d.recovery_hours]
    for d in disruptions:
        state = "unavailable" if d.severity_pct == 100 else "degraded"
        what = "unavailable" if d.severity_pct == 100 else f"at {100 - d.severity_pct}% capacity"
        lines.append(f'"root:{_esc(d.id)}" [label={_label(f"◆ {d.id}: {names.get(d.service, d.service)}", f"{what}, hours {d.start_hour}–{max(d.start_hour, d.end_hour - 1)}")}, '
                     f'shape=box, style="rounded,filled", fillcolor="{pal.status_fill[state]}", color="{pal.status[state]}", penwidth=2.4];')
    if len(disruptions) > 1:
        lines.append("{rank=source; " + " ".join(f'"root:{_esc(d.id)}"' for d in disruptions) + "}")

    involved: set[str] = set()
    for link in cascade.links:
        involved.update((link["payer"], link["recipient"]))
    for pid in sorted(involved):
        x = cascade.participants.get(pid)
        if pid not in internal:
            lines.append(f'"{_esc(pid)}" [label={_label(names.get(pid, pid), "outside boundary")}, shape=ellipse, '
                         f'style=dashed, fontcolor="{pal.text2}", color="{pal.axis}"];')
            continue
        if x and x.cash_short:
            step = f" · step {x.generation}" if x.generation is not None else ""
            note = f"⚠ short of cash from h{x.first_cash_short_hour}{step}"
            extra = "own routes worked" if x.indirect else "✕ also route-blocked"
            colour, fill, width = pal.status["degraded"], pal.status_fill["degraded"], 2.4
        elif x and x.route_blocked:
            note, extra = "✕ payments route-blocked", ""
            colour, fill, width = pal.status["unavailable"], pal.status_fill["unavailable"], 2.0
        else:
            note, extra = "✓ never short of cash", ""
            colour, fill, width = pal.axis, pal.status_fill["neutral"], 1.2
        lines.append(f'"{_esc(pid)}" [label={_label(names.get(pid, pid), note, extra)}, shape=ellipse, style=filled, '
                     f'fillcolor="{fill}", color="{colour}", penwidth={width}];')

    direct_by_payer: dict[str, list] = defaultdict(list)
    for n in cascade.direct:
        direct_by_payer[n.payer].append(n)
    roots = [f"root:{d.id}" for d in disruptions]
    for payer, nodes in sorted(direct_by_payer.items()):
        value = sum(n.amount for n in nodes)
        label = _label(f"{len(nodes)} blocked · {_compact(value, s.currency)}")
        tip = f"{len(nodes)} payment(s) by {names.get(payer, payer)} held at an unavailable or saturated route ({money(value)})"
        for root in roots:
            lines.append(f'"{_esc(root)}" -> "{_esc(payer)}" [label={label}, tooltip="{_esc(tip)}", '
                         f'color="{pal.categories["operational_only"]}", penwidth=1.6, style=dashed];')

    max_value = max((link["value"] for link in cascade.links), default=1)
    for link in cascade.links:
        kind = "propagated" if "propagated" in link["kinds"] else link["kinds"][0]
        colour = pal.categories[KIND_CATEGORY[kind]]
        step = f"step {link['min_generation']}" if link["min_generation"] is not None else "unlinked"
        label = _label(f"{_compact(link['value'], s.currency)} · {link['count']}")
        tip = (f"{names.get(link['payer'], link['payer'])} → {names.get(link['recipient'], link['recipient'])}: "
               f"{link['count']} delayed payment(s), {money(link['value'])}, {step}, up to {link['max_overdue_hours']} h overdue "
               f"({', '.join(link['obligations'][:6])})")
        width = 1.2 + 3.0 * link["value"] / max_value
        lines.append(f'"{_esc(link["payer"])}" -> "{_esc(link["recipient"])}" [label={label}, tooltip="{_esc(tip)}", '
                     f'color="{colour}", penwidth={width:.2f}];')
    lines.append("}")
    return "\n".join(lines)
