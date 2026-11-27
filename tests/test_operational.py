"""Operational network: state boundaries, required vs alternative dependencies, capacity, backlog."""

from __future__ import annotations

from cyberliquidity import OperationalModel, Reason, Scenario, ServiceState, simulate
from cyberliquidity.variants import make_disruption, stress
from tests.conftest import load, tiny


def _two_cloud_scenario(**overrides) -> Scenario:
    data = tiny(
        services=[
            {"id": "c1", "name": "Cloud 1", "kind": "cloud"},
            {"id": "c2", "name": "Cloud 2", "kind": "cloud"},
            {"id": "needs_both", "name": "Needs both", "kind": "payment_provider", "capacity_per_hour": 10,
             "requires_all": ["c1", "c2"]},
            {"id": "either", "name": "Either cloud", "kind": "payment_provider", "capacity_per_hour": 10,
             "requires_any": [["c1", "c2"]]},
        ],
        routes=[{"id": "r_both", "name": "r_both", "path": ["needs_both"]}, {"id": "r_either", "name": "r_either", "path": ["either"]}],
    )
    data.update(overrides)
    return Scenario.model_validate(data)


def test_outage_start_and_recovery_boundaries():
    s = Scenario.model_validate(tiny(disruptions=[{"id": "D1", "service": "svc", "start_hour": 2, "duration_hours": 3,
                                                   "recovery_hours": 2, "recovery_capacity_pct": 50}]))
    op = OperationalModel(s)
    states = [(op.status_at(h)["svc"].state, op.status_at(h)["svc"].capacity) for h in range(8)]
    assert states[1] == (ServiceState.AVAILABLE, 10)
    assert states[2] == states[3] == states[4] == (ServiceState.UNAVAILABLE, 0)  # hours 2,3,4
    assert states[5] == states[6] == (ServiceState.DEGRADED, 5)  # recovery phase at 50 %
    assert states[7] == (ServiceState.AVAILABLE, 10)


def test_required_dependencies_and_alternatives_behave_differently():
    s = _two_cloud_scenario()
    op = OperationalModel(s, [make_disruption("c1", 0, 5)])
    st = op.status_at(1)
    assert st["needs_both"].state is ServiceState.UNAVAILABLE  # AND: loses c1
    assert st["either"].state is ServiceState.AVAILABLE  # OR: fails over to c2
    both_down = OperationalModel(s, [make_disruption("c1", 0, 5), make_disruption("c2", 0, 5, id="D2")]).status_at(1)
    assert both_down["either"].state is ServiceState.UNAVAILABLE
    assert both_down["either"].causes[0]["mode"] == "any_of"


def test_degradation_propagates_through_the_bottleneck_rule():
    s = _two_cloud_scenario()
    st = OperationalModel(s, [make_disruption("c1", 0, 5, severity_pct=60)]).status_at(1)
    assert (st["c1"].pct, st["needs_both"].pct, st["either"].pct) == (40, 40, 100)
    assert st["needs_both"].state is ServiceState.DEGRADED and st["needs_both"].capacity == 4  # floor(10 * 40 / 100)


def test_shared_capacity_limits_aggregate_throughput():
    obligations = [{"id": f"a{i}", "payer": "P", "recipient": "Q", "amount": 10, "due_hour": 1, "routes": ["r"]} for i in range(3)]
    obligations += [{"id": f"b{i}", "payer": "Q", "recipient": "P", "amount": 10, "due_hour": 1, "routes": ["r"]} for i in range(3)]
    data = tiny(obligations=obligations)
    data["services"][0]["capacity_per_hour"] = 4
    data["participants"][1]["initial_balance"] = 1000
    r = simulate(Scenario.model_validate(data))
    settled_hour_1 = sorted(o.obligation_id for o in r.outcomes.values() if o.settled_hour == 1)
    assert settled_hour_1 == ["a0", "a1", "a2", "b0"]  # 4 in total, not 4 per payer
    for oid in ("b1", "b2"):
        assert r.outcomes[oid].settled_hour == 2
        assert r.blocks_by_obligation()[oid][0].reasons == (Reason.CAPACITY_EXHAUSTED.value,)


def test_backlog_clears_over_several_steps_after_recovery():
    obligations = [{"id": f"o{i}", "payer": "P", "recipient": "Q", "amount": 10, "due_hour": 1, "routes": ["r"]} for i in range(5)]
    data = tiny(obligations=obligations, horizon_hours=8,
                disruptions=[{"id": "D1", "service": "svc", "start_hour": 1, "duration_hours": 3}])
    data["services"][0]["capacity_per_hour"] = 2
    r = simulate(Scenario.model_validate(data))
    assert [h.overdue_count for h in r.hours] == [0, 5, 5, 5, 3, 1, 0, 0]  # recovery at hour 4, 2 per hour
    assert sorted(o.settled_hour for o in r.outcomes.values()) == [4, 4, 5, 5, 6]
    blocked_at_4 = [b for b in r.blocks if b.hour == 4]
    assert blocked_at_4 and all(b.reasons == (Reason.CAPACITY_EXHAUSTED.value,) for b in blocked_at_4)
    blocked_at_2 = [b for b in r.blocks if b.hour == 2]
    assert all(b.reasons == (Reason.SERVICE_UNAVAILABLE.value,) for b in blocked_at_2)


def test_single_points_of_failure_and_common_mode_dependencies():
    op = OperationalModel(load("default_72h.json"))
    assert "idp_keygate" in op.common_mode_failures("alpha_north", "beta_south")
    assert "cloud_nimbus" not in op.single_points_of_failure("beta_south")  # identity provider fails over
    assert set(op.single_points_of_failure("alpha_north")) == {"cloud_nimbus", "idp_keygate", "psp_alpha", "settle_north"}
    footprint = op.failure_footprint("cloud_nimbus")
    assert footprint["unavailable_routes"] == ["alpha_north"]
    assert "idp_keygate" not in footprint["unavailable_services"]


def test_service_state_events_are_recorded_with_causes():
    r = simulate(stress(load("default_72h.json")))
    alpha_events = [e for e in r.events if e.type == "SERVICE_STATE" and e.service_id == "psp_alpha"]
    assert [(e.hour, e.data["state"]) for e in alpha_events] == [(32, "unavailable"), (44, "available")]
    assert alpha_events[0].data["causes"][0]["services"] == ["cloud_nimbus"]
