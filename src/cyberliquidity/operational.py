"""Operational network: hourly service state, shared capacity and dependency analysis.

Rule (all quantities are integer percentages, see docs/MODEL.md section 3):

    own_s(h)  = min over disruption phases of s active in hour h of the phase capacity
                (100 when none is active)
    phi_s(h)  = min( own_s(h),
                     min over required dependencies d of phi_d(h),
                     min over requires_any groups G of max over m in G of phi_m(h) )
    cap_s(h)  = floor(capacity_per_hour_s * phi_s(h) / 100)   (0 when phi = 0;
                unlimited when the service has no capacity limit and phi > 0)

State: available if phi = 100, unavailable if phi = 0, degraded otherwise. Each
status records its *binding* causes (the constraints equal to phi), which the
explanation traces follow back to the originating disruption.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .graphs import topological_order
from .models import Disruption, Scenario


class ServiceState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def state_for(pct: int) -> ServiceState:
    if pct >= 100:
        return ServiceState.AVAILABLE
    if pct <= 0:
        return ServiceState.UNAVAILABLE
    return ServiceState.DEGRADED


@dataclass(frozen=True)
class ServiceStatus:
    service_id: str
    pct: int
    own_pct: int
    capacity: int | None  # effective payments/hour this hour; None = not throughput-limited
    nominal_capacity: int | None
    state: ServiceState
    causes: tuple[dict[str, Any], ...]  # binding constraints; empty when fully available

    def usable(self) -> bool:
        return self.pct > 0 and (self.capacity is None or self.capacity > 0)


class OperationalModel:
    """Evaluates the operational network for a scenario and a set of disruptions."""

    def __init__(self, scenario: Scenario, disruptions: Iterable[Disruption] | None = None):
        self.scenario = scenario
        self.services = scenario.services_by_id()
        self.routes = scenario.routes_by_id()
        self.groups = {sid: svc.dependency_groups() for sid, svc in self.services.items()}
        self.order = topological_order({sid: svc.dependency_ids() for sid, svc in self.services.items()})
        self.disruptions = tuple(scenario.disruptions if disruptions is None else disruptions)
        self._by_service: dict[str, list[Disruption]] = defaultdict(list)
        for d in self.disruptions:
            self._by_service[d.service].append(d)

    # ------------------------------------------------------------------ state

    def own_status(self, service_id: str, hour: int | None) -> tuple[int, list[dict[str, Any]]]:
        """Intrinsic capacity percentage from the service's own disruptions."""
        if hour is None:
            return 100, []
        pct = 100
        active: list[dict[str, Any]] = []
        for d in self._by_service.get(service_id, ()):
            phase = d.phase_at(hour)
            if phase is None:
                continue
            phase_pct, phase_name = phase
            active.append({"type": "disruption", "disruption": d.id, "phase": phase_name, "pct": phase_pct})
            pct = min(pct, phase_pct)
        return pct, [c for c in active if c["pct"] == pct] if pct < 100 else []

    def status_at(self, hour: int | None, overrides: Mapping[str, int] | None = None) -> dict[str, ServiceStatus]:
        """Effective status of every service in ``hour``.

        ``hour=None`` ignores disruptions (normal operation). ``overrides`` forces the
        *own* capacity percentage of given services, for hypothetical failure analysis.
        """
        out: dict[str, ServiceStatus] = {}
        for sid in self.order:
            if overrides and sid in overrides:
                own_pct = int(overrides[sid])
                own_causes = [{"type": "hypothetical", "pct": own_pct}] if own_pct < 100 else []
            else:
                own_pct, own_causes = self.own_status(sid, hour)
            pct = own_pct
            group_values = []
            for group in self.groups[sid]:
                value = max(out[m].pct for m in group)
                group_values.append((group, value))
                pct = min(pct, value)
            causes: list[dict[str, Any]] = []
            if pct < 100:
                if own_pct == pct:
                    causes.extend(own_causes)
                for group, value in group_values:
                    if value == pct:
                        causes.append(
                            {
                                "type": "dependency",
                                "mode": "required" if len(group) == 1 else "any_of",
                                "services": list(group),
                                "pct": value,
                            }
                        )
            nominal = self.services[sid].capacity_per_hour
            if pct <= 0:
                capacity: int | None = 0
            elif nominal is None:
                capacity = None
            else:
                capacity = nominal * pct // 100
            out[sid] = ServiceStatus(sid, pct, own_pct, capacity, nominal, state_for(pct), tuple(causes))
        return out

    def timeline(self, horizon: int) -> list[dict[str, ServiceStatus]]:
        return [self.status_at(h) for h in range(horizon)]

    # ------------------------------------------------------------ route checks

    def route_usable(self, route_id: str, status: Mapping[str, ServiceStatus]) -> bool:
        """True if every service on the route path is usable (ignores capacity already used)."""
        return all(status[sid].usable() for sid in self.routes[route_id].path)

    def route_nominal_capacity(self, route_id: str) -> int | None:
        caps = [self.services[sid].capacity_per_hour for sid in self.routes[route_id].path]
        limited = [c for c in caps if c is not None]
        return min(limited) if limited else None

    # ------------------------------------------------------- static analysis

    def failure_footprint(self, service_id: str, severity_pct: int = 100) -> dict[str, Any]:
        """Services and routes affected if ``service_id`` alone lost ``severity_pct`` of capacity."""
        status = self.status_at(None, overrides={service_id: 100 - severity_pct})
        unavailable = sorted(sid for sid, st in status.items() if st.state is ServiceState.UNAVAILABLE)
        degraded = sorted(sid for sid, st in status.items() if st.state is ServiceState.DEGRADED)
        routes_down = sorted(rid for rid in self.routes if not self.route_usable(rid, status))
        routes_degraded = sorted(
            rid
            for rid in self.routes
            if rid not in routes_down and any(status[s].state is ServiceState.DEGRADED for s in self.routes[rid].path)
        )
        return {
            "service": service_id,
            "severity_pct": severity_pct,
            "unavailable_services": unavailable,
            "degraded_services": degraded,
            "unavailable_routes": routes_down,
            "degraded_routes": routes_degraded,
        }

    def single_points_of_failure(self, route_id: str) -> list[str]:
        """Services whose failure *alone* makes the route unusable (includes the path itself)."""
        result = []
        for sid in self.order:
            status = self.status_at(None, overrides={sid: 0})
            if not self.route_usable(route_id, status):
                result.append(sid)
        return sorted(result)

    def common_mode_failures(self, route_a: str, route_b: str) -> list[str]:
        """Single services whose failure disables both routes (backup is not independent of these)."""
        return sorted(set(self.single_points_of_failure(route_a)) & set(self.single_points_of_failure(route_b)))

    def cause_chain(self, service_id: str, hour: int) -> list[dict[str, Any]]:
        """Follow binding causes from a service down to the originating disruption(s).

        Returns links ordered from ``service_id`` to the root. For an any-of group the
        first member (sorted) at the binding level is followed.
        """
        status = self.status_at(hour)
        links: list[dict[str, Any]] = []
        current: str | None = service_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            st = status[current]
            link: dict[str, Any] = {"service": current, "state": st.state.value, "pct": st.pct, "capacity": st.capacity}
            disruptions = [c for c in st.causes if c["type"] == "disruption"]
            dependencies = [c for c in st.causes if c["type"] == "dependency"]
            if disruptions:
                link["disruptions"] = [c["disruption"] for c in disruptions]
                link["phase"] = disruptions[0]["phase"]
                links.append(link)
                break
            if not dependencies:
                links.append(link)
                break
            cause = dependencies[0]
            link["via"] = cause
            links.append(link)
            current = next(m for m in sorted(cause["services"]) if status[m].pct == cause["pct"])
        return links
