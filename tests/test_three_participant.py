"""The manually verifiable three-participant example (A -> B -> C).

Initial cash A=100, B=0, C=0. O1: A owes B 100 in hour 1. O2: B owes C 100 in hour 2.
Stress: only A's route (provider_a) is unavailable in hours 1 and 2, available from hour 3.
"""

from __future__ import annotations

from cyberliquidity import Mechanism, Status, simulate
from cyberliquidity.variants import baseline, make_buffer, stress, with_intervention, with_settings


def test_baseline_both_payments_settle_on_time(three):
    r = simulate(baseline(three))
    assert r.outcomes["O1"].status is Status.SETTLED_ON_TIME and r.outcomes["O1"].settled_hour == 1
    assert r.outcomes["O2"].status is Status.SETTLED_ON_TIME and r.outcomes["O2"].settled_hour == 2
    assert r.hours[-1].balances == {"A": 0, "B": 0, "C": 100}
    assert all(h.overdue_value == 0 for h in r.hours)
    assert r.blocks == []


def test_stress_operational_block_then_financial_propagation(three):
    r = simulate(stress(three))
    blocks = r.blocks_by_obligation()

    # A cannot pay B during the outage: blocked by the unavailable service only (A has the cash).
    assert [(b.hour, b.reasons) for b in blocks["O1"]] == [(1, ("SERVICE_UNAVAILABLE",)), (2, ("SERVICE_UNAVAILABLE",))]
    assert blocks["O1"][0].route_checks[0]["services"] == ["provider_a"]
    assert blocks["O1"][0].available_cash == 100

    # B cannot pay C because it lacks cash, although B's route is operational.
    [o2_block] = blocks["O2"]
    assert o2_block.hour == 2 and o2_block.reasons == ("INSUFFICIENT_CASH",)
    assert o2_block.route_checks == ({"route": "route_b", "status": "AVAILABLE"},)
    assert o2_block.available_cash == 0 and o2_block.shortfall == 100
    assert o2_block.overdue_inflows == (("O1", 100),)  # B is waiting for A's delayed payment

    # End of hour 2: 200 of overdue obligations (two payments), with only 100 of cash in the system.
    assert (r.hours[1].overdue_count, r.hours[1].overdue_value) == (1, 100)
    assert (r.hours[2].overdue_count, r.hours[2].overdue_value) == (2, 200)

    # Hour 3: A's route is back; A pays B and B immediately pays C (within-step recycling).
    assert r.outcomes["O1"].settled_hour == 3 and r.outcomes["O1"].delay_hours == 2
    assert r.outcomes["O2"].settled_hour == 3 and r.outcomes["O2"].delay_hours == 1
    hour3 = [e for e in r.events if e.type == "SETTLEMENT" and e.hour == 3]
    assert [(e.obligation_id, e.data["sequence_in_hour"]) for e in hour3] == [("O1", 1), ("O2", 2)]
    assert r.hours[3].overdue_value == 0

    # Total cash stays 100 at every hour: nothing is created or lost.
    assert all(sum(h.balances.values()) == 100 for h in r.hours)
    assert r.totals["intervention_funding"] == 0
    assert r.hours[-1].balances == {"A": 0, "B": 0, "C": 100}

    # The two transmission mechanisms are kept distinct.
    assert r.outcomes["O1"].mechanism is Mechanism.DIRECT_OPERATIONAL
    assert r.outcomes["O2"].mechanism is Mechanism.FINANCIAL_PROPAGATION


def test_buffer_lets_b_pay_c_on_time_without_restoring_a(three):
    r = simulate(with_intervention(stress(three), make_buffer(["B"], 100, funding_hour=0, id="buffer_B")))

    assert r.outcomes["O2"].status is Status.SETTLED_ON_TIME and r.outcomes["O2"].settled_hour == 2
    # A's route is still unavailable: the buffer does not restore it.
    assert r.outcomes["O1"].settled_hour == 3
    assert r.outcomes["O1"].reason_hours == {"SERVICE_UNAVAILABLE": 2}

    # The extra funding is explicit, recorded once, and fully accounted for.
    funding = [entry for entry in r.ledger if entry.kind == "intervention_funding"]
    assert len(funding) == 1
    assert (funding[0].credit_account, funding[0].amount, funding[0].hour) == ("B", 100, 0)
    assert funding[0].debit_account == "external:buffer_B"
    assert r.totals["intervention_funding"] == 100
    assert all(sum(h.balances.values()) == 200 for h in r.hours)  # 100 initial + 100 funding
    assert r.hours[-1].balances == {"A": 0, "B": 100, "C": 100}


def test_without_within_step_recycling_b_waits_one_more_hour(three):
    r = simulate(with_settings(stress(three), liquidity_recycling="next_step"))
    assert r.outcomes["O1"].settled_hour == 3
    assert r.outcomes["O2"].settled_hour == 4  # B's receipt in hour 3 is usable from hour 4
    hour3 = [b for b in r.blocks_by_obligation()["O2"] if b.hour == 3][0]
    assert hour3.reasons == ("INSUFFICIENT_CASH",)
    assert (hour3.payer_balance, hour3.available_cash) == (100, 0)
