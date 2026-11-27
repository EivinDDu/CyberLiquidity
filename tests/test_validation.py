"""Invalid scenarios must be rejected with a clear message."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cyberliquidity import Scenario
from tests.conftest import tiny


def _ob(**kw):
    base = {"id": "o", "payer": "P", "recipient": "Q", "amount": 10, "due_hour": 1, "routes": ["r"]}
    base.update(kw)
    return base


CASES = {
    "unknown service in route": (dict(routes=[{"id": "r", "name": "r", "path": ["nope"]}]), "unknown service 'nope'"),
    "unknown route in obligation": (dict(obligations=[_ob(routes=["zzz"])]), "unknown route 'zzz'"),
    "duplicate id across namespaces": (
        dict(participants=[{"id": "svc", "name": "clash", "initial_balance": 0}, {"id": "Q", "name": "Q", "initial_balance": 0}]),
        "Duplicate id 'svc'"),
    "duplicate obligation id": (dict(obligations=[_ob(), _ob()]), "Duplicate obligation id 'o'"),
    "non-positive amount": (dict(obligations=[_ob(amount=0)]), "greater than 0"),
    "negative opening balance": (
        dict(participants=[{"id": "P", "name": "P", "initial_balance": -1}, {"id": "Q", "name": "Q", "initial_balance": 0}]),
        "greater than or equal to 0"),
    "due hour beyond horizon": (dict(obligations=[_ob(due_hour=6)]), "outside the horizon"),
    "payer equals recipient": (dict(obligations=[_ob(recipient="P")]), "payer and recipient are the same"),
    "both parties external": (
        dict(external_parties=[{"id": "X", "name": "X"}, {"id": "Y", "name": "Y"}], obligations=[_ob(payer="X", recipient="Y")]),
        "outside the accounting boundary"),
    "unknown payer": (dict(obligations=[_ob(payer="ghost")]), "unknown payer 'ghost'"),
    "dependency cycle": (
        dict(services=[{"id": "a", "name": "a", "kind": "cloud", "requires_all": ["b"]},
                       {"id": "b", "name": "b", "kind": "cloud", "requires_any": [["a", "svc"]]},
                       {"id": "svc", "name": "s", "kind": "payment_provider", "capacity_per_hour": 1}]),
        "Operational dependency cycle: a -> b -> a"),
    "self dependency": (dict(services=[{"id": "svc", "name": "s", "kind": "cloud", "requires_all": ["svc"]}]), "cannot depend on itself"),
    "single-member alternative group": (
        dict(services=[{"id": "c", "name": "c", "kind": "cloud"},
                       {"id": "svc", "name": "s", "kind": "payment_provider", "requires_any": [["c"]]}]),
        "needs at least two alternatives"),
    "zero capacity": (dict(services=[{"id": "svc", "name": "s", "kind": "payment_provider", "capacity_per_hour": 0}]),
                      "greater than or equal to 1"),
    "severity out of range": (dict(disruptions=[{"id": "D", "service": "svc", "start_hour": 1, "duration_hours": 2, "severity_pct": 101}]),
                              "less than or equal to 100"),
    "disruption of unknown service": (dict(disruptions=[{"id": "D", "service": "x", "start_hour": 1, "duration_hours": 2}]),
                                      "unknown service 'x'"),
    "buffer to an external party": (
        dict(interventions=[{"type": "liquidity_buffer", "id": "b", "allocations": [{"participant": "X", "amount": 5}]}]),
        "not a participant inside the accounting boundary"),
    "backup equal to primary": (
        dict(interventions=[{"type": "backup_route", "id": "b", "primary_routes": ["r"], "backup_route": "r", "active_from_hour": 1}]),
        "also listed as a primary route"),
    "faster recovery of unknown disruption": (
        dict(interventions=[{"type": "faster_recovery", "id": "f", "disruption": "nope", "duration_reduction_hours": 1}]),
        "unknown disruption 'nope'"),
    "unknown field (typo)": (dict(obligations=[_ob(amout=5)]), "Extra inputs are not permitted"),
    "unknown intervention type": (dict(interventions=[{"type": "magic", "id": "m"}]), "magic"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_invalid_scenarios_are_rejected(case):
    overrides, message = CASES[case]
    with pytest.raises(ValidationError) as excinfo:
        Scenario.model_validate(tiny(**overrides))
    assert message in str(excinfo.value)


def test_valid_minimal_scenario_is_accepted():
    s = Scenario.model_validate(tiny(obligations=[_ob()]))
    assert s.synthetic and s.horizon_hours == 6
