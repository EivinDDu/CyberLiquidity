"""Backup routes, liquidity buffers, faster recovery and the buffer sweep."""

from __future__ import annotations

from cyberliquidity import Mechanism, OperationalModel, Reason, Scenario, Status, simulate
from cyberliquidity.compare import describe_effect
from cyberliquidity.sweep import buffer_sweep
from cyberliquidity.variants import make_backup, make_faster_recovery, stress, with_intervention
from tests.conftest import tiny

SU, CAP, NA, CASH = (Reason.SERVICE_UNAVAILABLE.value, Reason.CAPACITY_EXHAUSTED.value,
                     Reason.ROUTE_NOT_YET_ACTIVE.value, Reason.INSUFFICIENT_CASH.value)


def _two_provider(shared_identity: bool, backup_capacity: int = 2) -> Scenario:
    """Provider A (primary) and provider B (backup); optionally both need the same identity service."""
    services = [
        {"id": "cloud_a", "name": "Cloud A", "kind": "cloud"},
        {"id": "cloud_b", "name": "Cloud B", "kind": "cloud"},
        {"id": "idp", "name": "Identity", "kind": "identity"},
        {"id": "prov_a", "name": "Provider A", "kind": "payment_provider", "capacity_per_hour": 10,
         "requires_all": ["cloud_a", "idp"] if shared_identity else ["cloud_a"]},
        {"id": "prov_b", "name": "Provider B", "kind": "payment_provider", "capacity_per_hour": backup_capacity,
         "requires_all": ["cloud_b", "idp"] if shared_identity else ["cloud_b"]},
    ]
    obligations = [{"id": f"o{i}", "payer": "P", "recipient": "Q", "amount": 10, "due_hour": 1, "routes": ["ra"]} for i in range(4)]
    obligations.append({"id": "late", "payer": "P", "recipient": "Q", "amount": 10, "due_hour": 7, "routes": ["ra"]})
    disrupted = "idp" if shared_identity else "cloud_a"
    return Scenario.model_validate(tiny(
        horizon_hours=10, services=services,
        routes=[{"id": "ra", "name": "Route A", "path": ["prov_a"]}, {"id": "rb", "name": "Route B", "path": ["prov_b"]}],
        obligations=obligations,
        disruptions=[{"id": "D1", "service": disrupted, "start_hour": 1, "duration_hours": 5}],
    ))


def test_independent_backup_reroutes_after_activation_within_its_capacity():
    s = _two_provider(shared_identity=False, backup_capacity=2)
    r = simulate(with_intervention(s, make_backup(["ra"], "rb", active_from_hour=3)))
    blocks = r.blocks_by_obligation()
    assert blocks["o0"][0].reasons == tuple(sorted((NA, SU)))  # hour 1: primary down, backup not active yet
    assert sorted((o.obligation_id, o.settled_hour) for o in r.outcomes.values() if o.via_backup) == [
        ("o0", 3), ("o1", 3), ("o2", 4), ("o3", 4)]  # 2 per hour: the backup's own capacity
    assert all(e.hour >= 3 for e in r.events if e.type == "SETTLEMENT" and e.data["via_backup"])
    assert r.outcomes["late"].route_used == "ra" and not r.outcomes["late"].via_backup  # primary preferred once back
    assert [b.reasons for b in blocks["o2"] if b.hour == 3] == [(CAP, SU)]


def test_backup_sharing_the_failed_dependency_does_not_help():
    s = _two_provider(shared_identity=True)
    assert "idp" in OperationalModel(s).common_mode_failures("ra", "rb")
    without = simulate(stress(s))
    with_backup = simulate(with_intervention(s, make_backup(["ra"], "rb", active_from_hour=3)))
    assert {o.settled_hour for o in with_backup.outcomes.values() if o.due_hour == 1} == {6}
    assert with_backup.digest() != without.digest()  # different route eligibility is recorded ...
    assert [o.settled_hour for o in with_backup.outcomes.values()] == [o.settled_hour for o in without.outcomes.values()]
    assert not any(o.via_backup for o in with_backup.outcomes.values())  # ... but nothing reroutes
    lines = describe_effect(without, with_backup, with_backup.scenario.interventions[0])
    assert any("not independent" in line for line in lines)


def test_faster_recovery_changes_only_the_outage_duration():
    s = _two_provider(shared_identity=False)
    r = simulate(with_intervention(s, make_faster_recovery("D1", 3)))
    assert [d.duration_hours for d in r.effective_disruptions] == [2]
    assert r.scenario.obligations == s.obligations and r.scenario.services == s.services
    assert {o.settled_hour for o in r.outcomes.values() if o.due_hour == 1} == {3}  # unavailable hours 1-2 only
    assert [d.duration_hours for d in simulate(stress(s)).effective_disruptions] == [5]


def test_insufficient_cash_stays_insufficient_without_new_funding():
    data = tiny(obligations=[{"id": "o", "payer": "Q", "recipient": "P", "amount": 100, "due_hour": 1, "routes": ["r"]}])
    data["participants"][1]["initial_balance"] = 50
    r = simulate(Scenario.model_validate(data))
    o = r.outcomes["o"]
    assert o.status is Status.UNSETTLED_AT_HORIZON and o.mechanism is Mechanism.LIQUIDITY_OTHER
    assert [b.reasons for b in r.blocks] == [(CASH,)] * 5  # hours 1-5
    assert {b.shortfall for b in r.blocks} == {50}
    assert [e.type for e in r.events if e.obligation_id == "o"][-1] == "UNSETTLED_AT_HORIZON"
    assert r.hours[-1].balances["Q"] == 50  # no cash appeared from anywhere


def test_buffer_sweep_reports_smallest_tested_buffer(three):
    sweep = buffer_sweep(stress(three), ["B"], [0, 50, 100, 150], outcome="selected_payers_on_time")
    assert [(p.amount_each, p.achieved) for p in sweep.points] == [(0, False), (50, False), (100, True), (150, True)]
    assert sweep.smallest_achieving.amount_each == 100
    assert "Not a proven minimum" in sweep.verdict(str)


def test_buffer_sweep_flags_outcomes_liquidity_cannot_reach(three):
    sweep = buffer_sweep(stress(three), ["B"], [0, 100, 1000], outcome="all_on_time")
    assert sweep.smallest_achieving is None and not sweep.reference_achieves
    assert [x["obligation_id"] for x in sweep.route_limited] == ["O1"]  # A's route is down whatever the cash
    assert sweep.route_limited[0]["operational_reasons"] == [SU]
    assert "not achievable by liquidity" in sweep.verdict(str)
