# CyberLiquidity model note

**Engine version 0.4.0.** This note gives the rules, equations, assumptions, limitations and metric definitions behind every number the application shows. The code is in `src/cyberliquidity/`, and the tests in `tests/` check the rules stated here.

Version 0.4 changes no rule: it is faster and gives exactly the 0.3 results (a regression test checks recorded 0.3 digests), and the backstop stops hopeless trial runs early (7.5, step 5). Version 0.3 added multilateral offsetting (7.8), the priority reserve (7.9) and the robustness check (7.10). Version 0.2 adds committed credit lines (7.4), the exact liquidity backstop (7.5), combinations of interventions (7.6), contagion generations (6.5) and the stress-lab analyses (6.6). Every rule of version 0.1 is unchanged: for scenarios that use only 0.1 features, the 0.2 engine reproduces the 0.1 events and ledger byte for byte, which a regression test checks against recorded 0.1 digests.

> **Synthetic scenario. Results are conditional on model assumptions and are not forecasts of real incidents.**

## 1. Purpose and scope

CyberLiquidity is a deterministic, discrete-time simulator. It traces how an operational service disruption (for example a cloud-region or identity-provider outage) turns into delayed payments, and how those delays travel to other firms through missing cash.

What it is **not**:

- not a forecast and not calibrated to any real market, firm or provider;
- not a risk model: it has no probabilities, expected losses, confidence intervals or return-on-investment figures;
- not a claim of methodological novelty. Payment-system simulators already exist, including central-bank tools such as the Bank of Finland's BoF-PSS2, and so do studies of operational outages and liquidity in payment systems. Positioning this work against that literature is part of the next stage (see `docs/NEXT_STEPS.md`).

## 2. Two networks and the accounting boundary

The model keeps operational dependencies and financial obligations in two separate networks. They connect only through payment routes.

### 2.1 Operational network

- **Service**: a cloud region, identity provider, payment service provider (PSP), settlement institution or payment channel. Optional `capacity_per_hour` is the number of payments the service can process per hour. This capacity is one pool shared by *all* users. A service without a capacity limit only passes its state on to dependents.
- **Dependency edge `A → B`**: *A depends on B*. A failure of B therefore propagates to A, against the arrow. There are two kinds:
  - `requires_all`: A needs every listed service (logical AND);
  - `requires_any` groups: A needs at least one member of each group (logical OR, i.e. failover between independent alternatives). A service that requires two components loses capacity if either fails. A service that can use either of two providers survives the loss of one.
- **Route**: an ordered path of services that must all process a payment, for example PSP Alpha → Settlement Bank North. Settling one payment through a route uses one unit of hourly capacity at every capacity-limited service on the path.
- **Cycles are rejected** at validation with the cycle path in the error message. This is a version-1 simplification (section 9).

### 2.2 Financial network

- **Participant**: a funded firm *inside* the accounting boundary. Its cash account is an integer in minor currency units (cents) and can never go negative.
- **External party**: a counterparty *outside* the boundary (households, employees, tax authority, outside suppliers, lenders, foreign customers). Its cash is not modelled: an external payer is never cash-constrained, but its payment still needs a working route.
- **Obligation**: unique ID, payer, recipient, amount, due hour, priority (1 = most urgent), eligible routes in preference order, and purpose. All obligations are pending at the start. The settlement status is an *output*: on time, late, or unsettled at the horizon.
- Service nodes have no accounts.

### 2.3 Coupling

An obligation can settle in hour *h* only if **one of its eligible routes is usable** *and* **its payer can fund it in full**. This gives two separate transmission mechanisms, and both are recorded:

- **Operational disruption**: the payment cannot settle because a service on its route is unavailable or out of capacity.
- **Financial propagation**: the payer has not received money it was expecting, so it cannot fund a *different* payment, even though that payment's own route works.

### 2.4 Accounting boundary and conservation

Cash moves only through settled obligations and explicitly recorded intervention funding. Every hour the engine checks, and the tests re-check from the ledger, that:

```
sum of participant balances (end of hour h)
  = sum of opening balances
  + external inflows settled up to h      (external payer -> participant)
  - external outflows settled up to h     (participant -> external recipient)
  + intervention funding paid up to h     (recorded as an external source)
  + credit drawn up to h                  (committed credit lines, section 7.4)
  - credit repaid up to h
```

An internal payment is one ledger entry. It debits the payer's account and credits the recipient's account with the same amount.

## 3. Operational state rules

Time is in whole hours. **Hour *h*** is the interval `[h, h+1)` after the scenario start; hour 0 is the first.

**Disruptions.** A disruption of service *s* has `start_hour`, `duration_hours`, `severity_pct` (share of capacity lost) and an optional recovery phase:

```
outage phase    hours [start, start + duration)                     capacity = 100 - severity_pct  (%)
recovery phase  hours [start + duration, start + duration + R)      capacity = recovery_capacity_pct (%)
afterwards                                                         capacity = 100 %
```

`severity_pct = 100` means unavailable. If several disruptions affect one service in the same hour, the lowest capacity applies.

**Effective capacity percentage** (integer arithmetic, dependencies evaluated first):

```
own_s(h) = min over the service's active disruption phases (100 if none)
phi_s(h) = min( own_s(h),
                min over required dependencies d of phi_d(h),
                min over requires_any groups G of ( max over m in G of phi_m(h) ) )
cap_s(h) = floor( capacity_per_hour_s * phi_s(h) / 100 )   if the service has a capacity limit
         = unlimited                                       if it has none and phi_s(h) > 0
         = 0                                               if phi_s(h) = 0
```

The state is **available** if phi = 100, **unavailable** if phi = 0, and **degraded** otherwise. A degraded service whose capacity rounds down to 0 blocks payments with reason "capacity exhausted". Each status records its *binding causes*: the disruption or dependencies whose value equals phi. Explanations follow these causes back to the originating disruption.

## 4. Settlement rules

### 4.1 Step order within hour *h*

1. Evaluate every service's state and capacity for the hour. Remaining capacity starts at `cap_s(h)` and is shared by everyone.
2. Pay intervention funding scheduled for hour *h* into the participants' accounts. It counts as an explicit external inflow and is available immediately.
3. Add obligations whose due hour is *h* to the queue. **No obligation can settle before its due hour.**
4. **Settlement loop.** Sort the queue by the ordering key. Scan from the top and settle the *first* obligation that can settle; then rescan from the top. Stop when a full scan settles nothing. A payer with a committed credit line that is short of cash for a payment whose route is usable draws exactly the shortfall first (section 7.4). A payer under a priority-reserve rule releases a routine payment only from cash it does not need to keep (section 7.9).
4b. **Offsetting** (only with the gridlock-resolution intervention, section 7.8). Look for a set of queued payments that can settle simultaneously and settle it as one batch; then run the settlement loop again, because net proceeds can make further single payments feasible. Repeat until no batch forms.
5. Every obligation still queued is now overdue. Record *every* reason it could not settle (section 4.4).
6. **Credit sweep.** Each participant's end-of-hour cash repays its outstanding credit draws first (participants in ID order).
7. Record end-of-hour balances, credit outstanding, overdue obligations and service usage, then check the invariants (section 5).

*Implementation note.* The engine does not literally rescan the whole queue after each settlement; it uses an equivalent, faster procedure. A settlement only lowers capacity and the payer's cash and credit, and only raises the recipient's cash. A payer's reserve (section 7.9) falls by exactly the protected amount it pays, so its free cash for routine payments does not rise either. So the only queued payments that can newly become feasible are the recipient's own, and under `next_step` recycling not even those. The engine re-checks exactly those payments, in queue order, and otherwise continues the scan. This selects the same payment as a full rescan at every step. The differential tests (section 13) confirm it on 300 random scenarios against a reference model that does rescan literally.

### 4.2 Queue order and selection

- Default ordering key: `(priority, due_hour, obligation_id)`. Alternative: `(due_hour, obligation_id)`, i.e. FIFO that ignores priority.
- **Bypass**: an obligation that cannot settle does not block those behind it. As a result, a lower-priority payment can use cash or capacity while a higher-priority one waits (for example, a fee settles while the same firm's payroll is cash-blocked). Rescanning from the top after each settlement means every settlement is the highest-priority payment that can settle *at that moment*.
- **Route choice**: the first eligible route in preference order that is active in hour *h* and has at least one unit of remaining capacity at every service on its path. The obligation's own routes come first, then any backup routes (section 7.1).
- **Cash condition**: an internal payer needs available cash + undrawn available credit − kept reserve ≥ amount. The credit term is zero unless a committed credit line applies (section 7.4); the reserve term is zero unless the payment is a routine payment of a participant under a priority-reserve rule (section 7.9). External payers have no cash condition.
- **Atomic, full settlement**: there are no partial payments or overdrafts. Intraday credit exists only as the explicit credit-line intervention, and netting only as the explicit offsetting intervention (section 7.8); every draw and every net movement is a ledger entry. Settling one payment debits the payer, credits the recipient, and uses one capacity unit per path service.

### 4.3 Liquidity recycling

With the default `within_step` rule, money received earlier in the hour can fund later payments in the same hour, so long payment chains can clear within one hour. This optimistic simplification ignores processing latency, cut-off times and batch cycles. The alternative `next_step` rule makes cash received in hour *h* usable only from hour *h + 1*; funding is not affected. The experiment uses both (section 10).

### 4.4 Blocking reasons (never collapsed into one)

For every overdue obligation in every hour, each eligible route is checked and recorded:

| Route status | Meaning |
|---|---|
| `ROUTE_NOT_YET_ACTIVE` | A backup route whose activation hour has not been reached. |
| `SERVICE_UNAVAILABLE` | Some service on the path has capacity 0% (the services are listed). |
| `CAPACITY_EXHAUSTED` | Every path service is up, but one has no capacity left this hour. |
| `AVAILABLE` | The route could take this payment. |

The obligation is **operationally blocked** if no route is available; its operational reasons are the set of route statuses. It is **cash-blocked** if the payer is internal and its available cash plus any undrawn available credit, less any reserve it must keep, is below the amount (`INSUFFICIENT_CASH`). Both can apply at once. A cash block also records the shortfall and the payer's **overdue incoming obligations**: payments owed to it that are themselves stuck in the queue. Under a reserve rule it also records the amount kept back; a block is **held by the reserve** when the payer could have funded the payment without the rule.

### 4.5 Deadlines and horizon

A payment due in hour *h* is **on time** if it settles in hour *h* (by the end of hour *h*). Otherwise it is **late** and its delay is `settled_hour - due_hour`. Obligations still unsettled after the last hour are **not discarded**. They are reported as *unsettled at the horizon*, separately from late payments, with their last blocking reasons.

## 5. Invariants (enforced at run time and tested independently)

- A payment settles at most once.
- Cash balances are never negative.
- Every internal settlement moves the same amount out of one account and into another; replaying the ledger reproduces every end-of-hour balance.
- Cash is conserved within the boundary (section 2.4). External flows, intervention funding, credit draws and repayments are explicit ledger entries.
- Outstanding credit never exceeds a facility's limit, is never negative, and always equals draws minus repayments so far.
- Every offsetting batch nets to zero through its clearing account, and each participant's net ledger movement equals its gross obligations in the batch. A payment settled in a batch has exactly one `SETTLEMENT` event like any other.
- A reserve rule never holds back a protected payment.
- Shared capacity is never exceeded, counted across *all* users of a service.
- No payment passes through a route containing an unavailable service, and no payment settles through a backup route before its activation hour.
- Recovery releases the backlog only as fast as capacity allows.
- Every overdue obligation has at least one recorded reason in every overdue hour.
- Identical inputs give identical events and ledger (SHA-256 results digest).
- The engine agrees with an independent reference implementation of sections 3, 4, 7.8 and 7.9 on every settlement, route, balance, blocking reason, credit amount and offsetting batch, across 300 seeded random scenarios (section 13).

## 6. Metric definitions

### 6.1 Per obligation

- **Status**: settled on time / settled late / unsettled at horizon.
- **Delay** (settled only): `settled_hour - due_hour`, in hours.
- **Overdue hours**: the delay, or `horizon - due_hour` if unsettled (right-censored at the horizon).

### 6.2 System-level

- **Overdue at end of hour h**: obligations with `due_hour ≤ h` not settled by the end of hour *h*, by count and value. This is the payment backlog.
- **Peak outstanding overdue value**: the maximum of that value over all hours.
- **Overdue value × hours**: `sum over hours of overdue value = sum over obligations of amount × overdue hours`. A measure of disruption intensity in currency-hours; the default ranking metric.
- **Settlement-delay distribution**: completed payments grouped by delay, with delay 0 meaning on time. Percentiles use the nearest-rank method. The mean delay includes on-time payments at 0.
- **Blocked by**: the number of obligations ever blocked by an unavailable service, by exhausted capacity, or by insufficient cash. An obligation can count under several of these.
- **Backlog recovery**: the overdue count and value at the end of the last outage phase, the first later hour that ends with nothing overdue, and the hours needed.
- **Cash trajectories**: each participant's end-of-hour balance.
- **Priority-1 payments**: how many are delayed, and their overdue value × hours (both are also ranking metrics). In the built-in scenarios these are payroll, tax and loan repayments.
- **Offsetting**: batches formed, payments settled in batches, and the liquidity saved (gross value settled in batches minus the net cash that moved).
- **Reserve**: payments held back at least once although they could have been funded, and their value × hours.

### 6.3 Blocking categories and mechanisms

Each obligation-hour in the backlog falls into exactly one category:

| Category | Rule |
|---|---|
| operational only | operationally blocked, cash sufficient |
| operational and cash | both conditions failed in the same hour |
| cash, awaiting overdue inflows | cash-blocked with a usable route, while payments owed to the payer are overdue (**financial propagation**) |
| cash, other | cash-blocked with a usable route and nothing overdue owed to the payer (e.g. a structural shortfall) |

Over all its blocked hours, each delayed obligation also gets a **mechanism**:

- *direct operational*: never cash-blocked;
- *financial propagation*: only cash blocks, at least one while awaiting overdue inflows;
- *liquidity shortfall (other)*: only cash blocks, never with overdue inflows;
- *mixed*: both kinds occurred.

### 6.4 What the metrics are not

- **Delayed or overdue value is not economic loss.** No penalties, lost sales or financing costs are modelled.
- **A cash shortfall is not insolvency.** A firm can be short in one hour and fully able to pay an hour later.
- **Peak overdue value is not "liquidity required"**: as the hand example in section 12 shows, 200 can be overdue with only 100 of cash in the system. Funding needs are explored only by rerunning: with buffers (section 7.2), and with the exact backstop, which is itself verified by simulation runs (section 7.5).

### 6.5 Contagion generations

Each delayed obligation gets a *kind* and a *step* (generation), computed only from blocking records:

- **direct, step 0**: blocked at least once by an operational reason;
- **propagated, step k ≥ 1**: only ever cash-blocked while its payer was owed overdue incoming payments, and reachable in *k* links from a direct delay. A link *u → v* means *u* was among the payer's overdue inflows when *v* was cash-blocked. The step is the **shortest** such path (breadth-first from all direct delays), so cycles of mutual waiting cannot inflate it;
- **unlinked**: cash-blocked with no recorded path back to a direct delay, for example a shortfall without overdue inflows, or gridlock where two firms wait only for each other.

Derived measures:

- **contagion depth**: the largest step;
- **delayed value by step**;
- per participant: first hour short of cash, hours short, largest single shortfall, whether any of its own payments was route-blocked, the step at which it was reached, and delayed value in and out.

A participant that is short of cash but never had a payment route-blocked is **indirectly affected**: the disruption reached it only through delayed incoming money. Links are recorded dependencies, not counterfactual proofs; compare with the baseline run for that.

### 6.6 Stress-lab analyses

Each analysis is a set of ordinary runs of the same schedule; nothing is sampled or extrapolated.

- **Criticality scan**: the same outage (start, duration, severity) applied to each service in turn, ranked by a chosen metric. Each service also gets its best intervention among a backup suggested for *its* footprint (same activation delay) and the configured buffer, credit line and faster recovery.
- **Duration profile and tolerance**: one service, a list of tested durations, with and without interventions. The *longest tolerated outage* for an outcome is the longest tested duration such that the outcome holds at every tested duration up to it. The first failing duration is the *first breach*, and non-monotone cases are flagged. The true threshold lies between the two tested values; durations are never interpolated.
- **Start profile**: one service and duration, one run per start hour such that the outage ends within the horizon.
- **Impact surface**: a start-hour × duration grid of one metric for the stress run.

## 7. Interventions

Every intervention is compared against the *same* stressed run (B) and changes only what is stated.

### 7.1 Backup payment provider

From `active_from_hour`, every obligation that lists one of the `primary_routes` (optionally only for listed payers) may also use `backup_route`. The primary route stays preferred whenever it is usable. The backup keeps its own dependencies and shares its capacity with its existing users. It is not assumed to be independent: if it depends on the failed service, the dependency graph makes it unusable. The app and the comparison narrative flag this, naming the shared dependency. The app sets the activation hour to the outage start plus a configurable delay.

### 7.2 Finite liquidity buffer

Selected participants receive funding transfers at `funding_hour` (default 0). Each is recorded as an `intervention_funding` ledger entry from an account outside the boundary. Buffers create no unexplained cash and do not touch the operational network.

**Buffer sweep.** The stressed run is repeated for a user-defined grid of amounts per participant. The app reports the **smallest tested buffer that achieves the selected outcome**, which is not a proven minimum: untested amounts are unknown, and outcomes need not be monotonic. A **cash-unconstrained reference run** pre-funds every payer with the full value of its outgoing obligations, so cash can never bind. Whatever is still delayed there is **route-limited**: no amount of liquidity can fix it.

### 7.3 Faster service recovery

The chosen disruption's outage phase is shortened by `duration_reduction_hours`, but never below 0. Any recovery phase follows the shortened outage. Nothing else changes.

### 7.4 Committed credit line

Selected participants get a facility with a `limit`, usable from `available_from_hour`. In the settlement loop, a payment whose route is usable but whose payer's available cash *c* is below the amount *A* is feasible if *c* + undrawn credit ≥ *A*. When it settles, the payer first draws exactly *A − c*: a `credit_draw` ledger entry from the account `credit:<intervention>`, outside the boundary. At the end of every hour, each participant's cash repays its outstanding draws first (`credit_repayment` entries). Draws and repayments are book entries and need no payment route. The model has no fees, collateral, expiry or provider constraints. Metrics: peak draw per participant (at any moment), total drawn, drawn amount outstanding at hour ends (value-hours), and outstanding at the horizon.

**Equivalence with a pre-funded buffer (a model property).** Let *n* be a participant's net position (cash minus credit drawn). With a credit line of limit *L* from hour *X*, the capacity to pay at any check is *n + L*, because a repayment lowers cash and drawn credit by the same amount. A buffer of *L* paid at hour *X* gives the same capacity. So every feasibility check, and hence every settlement, is identical. The two differ only in the liquidity used: the buffer supplies *L* at once, the credit line only what is drawn. A test checks this on the default scenario and on random scenarios.

### 7.5 Exact liquidity backstop

Question: how much hour-0 pre-funding does each participant need so that every payment settles exactly as it would with unlimited liquidity for those participants?

1. Run the stressed scenario once with a **non-binding** credit line for the covered participants (default: everyone with outgoing obligations). Its limit is each participant's total outgoing obligations, which no draw can exceed. Its outcome is the best that liquidity for these participants can achieve. Whatever is still delayed is **route-limited**.
2. **Sufficiency of the peaks.** Pre-funding each participant with its peak draw *P* reproduces that outcome exactly. At every check where the credit run settled a payment, the post-payment position was at least *−P*, so *n + P ≥ A*. A payment the credit run did not settle at a check failed for a route reason, which is unchanged. By induction the two runs settle the same payments in the same order. A cash flag can remain only on hours in which the payment's route was unusable anyway.
3. **Tightness.** With `next_step` recycling, one minor unit less for a participant makes the payment at its peak infeasible at that check. The payer cannot gain usable cash later in the same hour, so a cash block is recorded. With `within_step` recycling the peak can exceed the need: a pre-funded payer can sometimes wait for money arriving later in the same hour, whereas a credit line is drawn the moment the payment is reached in the queue.
4. **Refinement.** Each amount is therefore lowered by binary search, largest first, keeping the outcome identical to the unlimited-liquidity run with no payment of a covered participant held up by cash alone. The search repeats until nothing changes (at most three passes). Every final amount is then re-checked: one minor unit less must change the outcome.

5. **Stopping hopeless trials early (0.4).** Each trial in steps 2 to 4 asks only whether an allocation reproduces the unlimited-liquidity run with no covered payment held up by cash alone. A trial is therefore stopped at the end of the first hour in which it certainly cannot:
   - a payment settles in a different hour than in the unlimited-liquidity run;
   - a payment that run settled in this hour is still queued;
   - a covered participant's payment is blocked by cash alone.

   Each of these makes the final comparison fail, so stopping returns the verdict a full run would. An allocation that was already tried is not run again. Neither shortcut changes which allocations are tried or the result. The reported run count includes every trial started, and `runs_stopped_early` says how many stopped before the horizon.

The result is **locally smallest**: no single participant's amount can be lowered by one minor unit. Another allocation with a smaller total may exist, because outcomes need not be monotone in the amounts. Every claim in the verdict is backed by a simulation run in the same computation, and the number of runs is reported. This is the liquidity the *model* needs in *one* scenario, not an estimate of real liquidity needs and not a recommendation.

### 7.6 Combinations

A package applies several interventions to the same B; each member is also run alone. If member *i* alone removes the fraction *f_i* of B's disruption, the **independence benchmark** for the package is `1 − Π(1 − f_i)`: what they would remove if each removed its own share of whatever disruption the others leave. The package is:

- **complementary** in that run if it removes more than the benchmark by over one percentage point;
- **overlapping** if it removes less (they partly fix the same delays);
- **roughly independent** otherwise.

If a member makes the disruption worse on its own (*f_i* < 0, for example a reserve rule), "overlapping" would be misleading. The verdict is then **more harmful together** (the package removes less than the benchmark) or **less harmful together** (it removes more).

A simple sum of separate reductions is also reported, but it is not used as the benchmark, because two interventions that each remove most of the disruption can never add up.

### 7.7 Ranking

Interventions are ranked by a lower-is-better disruption metric (default: overdue value × hours). Rankings describe one scenario under its declared assumptions. They are not general statements, and no intervention is assumed to be best everywhere. Section 7.10 tests how far a ranking survives changes to those assumptions.

### 7.8 Multilateral offsetting (gridlock resolution)

A liquidity-saving mechanism of the payment system. From `active_from_hour`, after the settlement loop of every hour (step 4b):

1. **Candidates**: every queued payment whose payer is inside the boundary, in queue order. (A payment from an external payer is never cash-blocked, so if it is still queued its route is down.)
2. **Routes**: each candidate gets its first usable route with spare capacity, assigned in queue order against the hour's remaining capacity. A candidate that gets no route is left out of this round.
3. **Coverage**: for each participant touched by the assigned set, its net position *n* (incoming minus outgoing over the set) must satisfy cash + undrawn credit + *n* − *k* ≥ 0, where *k* is the reserve it must keep if it pays a routine payment in the set (section 7.9; *k* is reduced by the protected payments it pays in the set).
4. **Removal**: while some participant is uncovered, remove the **last payment in queue order** of the participant with the **largest uncovered amount** (ties: lowest ID), and repeat from step 2.
5. **Settlement**: if at least two payments remain, they settle together. Net payers draw committed credit for any shortfall, then pay their net amount into the clearing account `clearing:<intervention>` (`offset_debit`); payments to recipients outside the boundary are paid gross from it (`external_outflow`); net receivers are paid out (`offset_credit`). The clearing account is empty again after every batch. Each obligation is discharged in full and gets its own `SETTLEMENT` event, marked with the batch number.
6. Net proceeds count as cash received in that hour, so the `next_step` recycling rule applies to them as to any other receipt.

A single payment never forms a batch: after the settlement loop no payment can settle on its own. **Properties.** (i) Without capacity limits, the batch does not depend on the order of removals: a removal only improves the remover's own position and can only worsen others', so the procedure converges to the largest covered set in which every participant keeps a prefix of its own payments in queue order. With capacity limits, a removal can free capacity for another payment, so the tie-break can matter; a test pins one such case. (ii) The rule is greedy, not optimal: it can miss a settleable set (for example by removing a payment that the participant could afford once another of its payments is removed; the settlement loop after the batch then settles it if possible). (iii) Offsetting is **not guaranteed to help**: settling a batch earlier can move cash away from a participant who needed it for a later payment. In 1 of 300 random test structures it increased overdue value × hours.

This is a simple rule of the kind discussed in the payment-system literature on gridlock resolution (for example M. L. Bech and K. Soramäki, *Gridlock resolution in interbank payment systems*, Bank of Finland Discussion Paper 9/2001); it does not reproduce that paper's algorithm or any particular system's. That study reports the largest benefit when intraday liquidity is scarce. In this model's circular-trade scenario the greedy rule does nothing at the lowest tested balances, because every net payer must cover its net position (`docs/EXPERIMENT.md`). Whether the difference comes from the rule, the network or the data has not been investigated.

### 7.9 Priority reserve (treasury rule)

A behavioural rule for the covered participants (default: all), from `active_from_hour`. **Protected** payments are those with priority ≤ `protected_priority` (default 1, e.g. payroll and tax). At the start of each hour the participant's reserve is the total of its unsettled protected payments due before `hour + lookahead_hours`, overdue ones included. A **routine** payment is feasible only if cash + undrawn credit − reserve ≥ amount; protected payments are never held back by the rule, and paying one lowers the reserve by its amount. The rule uses only the participant's own known payment calendar and adds no money: it decides which payments wait. The payments it holds back are other participants' incoming money, which is why it can increase the disruption for everyone, including, in some settings, the urgent payments it is meant to protect (`docs/EXPERIMENT.md`, second experiment).

The exact liquidity backstop (7.5) rejects a scenario with an applied reserve rule: a reserve counts undrawn credit as cover for upcoming payments, so the credit-line peaks no longer bound the cash a pre-funded buffer needs.

### 7.10 Robustness of a ranking

The comparison is re-run under **one-at-a-time assumption variants**: the other liquidity-recycling rule; the other queue order; every outage 3 h earlier and later; every outage 50% shorter and longer; every opening balance 25% lower and higher; every throughput-limited service 25% slower. Variants that coincide with the configured case are dropped. Interventions are applied as *plans* that follow a moved outage (a backup keeps its activation delay; faster recovery targets the first disruption).

For each variant and intervention the **regret** is how much worse the intervention is than the best one in that variant, in the metric's own units; it is also shown as a share of the configured case's stress disruption so that variants can be compared. Reported: in how many variants each intervention is best or practically tied (regret ≤ 1% of that variant's stress value), its largest regret and where it occurs, the range of its reduction, and in how many variants it makes things worse. The **minimax-regret** intervention has the smallest largest regret. The variants are **not weighted**: none is treated as more likely than another, and nothing here is a probability.

## 8. Explanation traces

Explanations are built only from recorded events; no language model is involved.

- For a delayed obligation, the first blocked hour determines the first link.
- **Operational block**: the unavailable path service, then the binding cause of each failed service down to the originating disruption.
- **Cash-only block**: the payer's shortfall, then the largest overdue incoming payment at that hour, explained recursively (depth limit 8, no repeated obligations).
- Every step cites event IDs (`SERVICE_STATE`, `BLOCKED`, `SETTLEMENT`) that can be looked up in `events.csv`.

An arrow X → Y means *X was recorded as blocking Y*. For cash links this is a recorded dependency, not a counterfactual proof that X alone caused Y. The app shows the same obligation's baseline outcome next to the trace for comparison.

## 9. Simplifications and limitations

1. Synthetic, uncalibrated data. Every name is fictional, and the default calendar was constructed so that both mechanisms appear.
2. One-hour time steps. The order within an hour is a settlement sequence, not clock time.
3. Within-step liquidity recycling (default) is optimistic; see 4.3.
4. Priority ordering with bypass. Real systems may use queues that block or liquidity reservations; one stylised treasury rule that holds back routine payments exists as the explicit reserve intervention (7.9), with perfect knowledge of the firm's own calendar.
5. All-or-nothing settlement: no partial payments, overdrafts, collateral, central-bank liquidity, cut-off times or settlement cycles. Intraday credit exists only as the explicit credit-line intervention, and netting only as the explicit offsetting intervention, which uses one greedy rule (7.8).
6. Capacity counts payments, not value, and every payment uses one unit at each path service. Recipient-side processing appears only when listed in the route.
7. `requires_any` is pure failover to the best member (no load-sharing, no failover delay). Degradation passes through the bottleneck (min) rule.
8. Operational dependency cycles are rejected.
9. External parties have unlimited cash and no behaviour of their own.
10. No behavioural responses: no cancellations, renegotiation, penalties or panic, and no learning. The only change in behaviour is an explicit intervention; hoarding exists only as the reserve rule (7.9).
11. The obligation schedule is fixed. Delays create no new obligations (fees, interest) and remove none.
12. One currency, no FX. Amounts are integers in minor units.
13. Buffers are pre-positioned transfers. Credit lines are drawn on demand and swept at the end of every hour, with no fees, collateral, expiry or provider constraints. Neither models cost or the funder's own constraints.
14. A backup activates at a fixed hour. Detection, decision and onboarding time are folded into that delay.
15. The horizon truncates outcomes: delays are right-censored at the horizon.
16. Deterministic. Uncertainty modelling (for example over outage durations) is deliberately left out until defensible distributions and interpretations exist.
17. Outcomes need not be monotone in liquidity or capacity. Extra cash can change which payments use scarce capacity first. In the experiment, a buffer makes one 36-hour identity outage marginally worse.
18. The exact backstop is locally smallest, not globally (7.5), and is specific to one schedule, ordering and recycling rule.
19. Contagion links are recorded blocking relationships, not causal proofs (6.5).
20. Stress-lab tolerances are bounded by the tested grid; nothing is interpolated (6.6).
21. Offsetting can miss settleable sets and can make a run worse (7.8). The backstop does not support reserve rules (7.9).
22. The robustness check varies one assumption at a time around the configured case; interactions between several changed assumptions are not explored (7.10).

## 10. Confounding assumptions for experiments

Results that compare interventions are sensitive to:

- **Payment ordering**: priority bypass versus FIFO changes who gets scarce cash or capacity first.
- **Within-step recycling**: with recycling, restoring the top of a payment chain clears the whole chain in one hour, which favours operational fixes. Without it, every link adds an hour.
- **Service capacity**: a backup's spare capacity, and the post-recovery throughput, determine how fast backlogs clear.
- **Simulation horizon**: long outages close to the horizon are censored.
- **Calendar timing**: whether payroll falls inside the outage, and an outage start relative to the payment calendar.
- **Buffer design**: size, recipients (targeted or not) and timing.

The research experiment (`docs/EXPERIMENT.md`) reruns its comparison under the alternative ordering and recycling rules, and with the outage start moved by −6 to +6 hours, for this reason. The app and `cyberliquidity robustness` run the general version of this check for any scenario (section 7.10). Its backup-versus-buffer verdicts treat differences below 1% of the no-intervention disruption as practically tied, so that negligible differences do not count as reversals.

## 11. Default synthetic scenario

`scenarios/default_72h.json` is generated by `cyberliquidity generate-default` (`src/cyberliquidity/synthetic.py`). It covers 72 hours and 138 obligations in EUR. The operational side has two cloud regions, Nimbus and Stratus. The identity provider KeyGate is replicated across both regions and fails over between them. PSP Alpha runs on Nimbus, PSP Beta on Stratus, and both use KeyGate. Two settlement banks complete the picture, along with North's on-premises high-value channel, which also uses KeyGate. On the financial side there are twelve fictional businesses:

- three larger firms on PSP Alpha;
- nine smaller, thinly funded firms on PSP Beta, including an intraday bread supply chain: agency → distributor → bakery → mill → farm.

The baseline settles every obligation on time. The default stress is a 12-hour Nimbus Cloud outage from hour 32, 08:00 on day 1.

**Second scenario: circular trade.** `scenarios/cluster_gridlock_48h.json` (`cyberliquidity generate-default --which cluster`, `src/cyberliquidity/synthetic_cluster.py`) covers 48 hours and 45 obligations: eight fictional manufacturing firms on one industrial estate, a payment service provider in one cloud region through which outside customers pay, and one local bank through which the firms pay one another. Birch Assembly Works buys from the other seven and is also their landlord, and the suppliers pay one another round a ring in which each owes the next more than it holds, while its net position over the ring is close to zero. Normally a vehicle maker's payment to Birch at 09:00 starts the chain. A 12-hour outage of the cloud region from hour 32 strands that payment, and the ring locks up although most firms would be net receivers: the case offsetting is designed for. The baseline settles every obligation on time.

## 12. Hand-checkable example

`scenarios/three_participant.json` has opening cash A = 100, B = 0, C = 0. A owes B 100 in hour 1, and B owes C 100 in hour 2. Only A's route is unavailable, in hours 1–2.

| Hour | A → B | B → C | Overdue at end of hour |
|---|---|---|---|
| 1 | route unavailable | not due | 100 |
| 2 | route unavailable | insufficient cash, route usable, awaiting A → B | **200** (with only 100 cash in the system; not a loss of 200) |
| 3 | settles (delay 2 h) | settles in the same hour by recycling (delay 1 h) | 0 |

Total cash stays 100 throughout. Pre-funding B with an explicit, recorded 100 lets B pay C on time in hour 2, while A → B stays blocked until hour 3: the buffer does not restore A's route.

## 13. Reproducibility

- Results digest: SHA-256 over the canonical event and cash ledgers.
- Scenario digest: SHA-256 over the canonical scenario JSON.
- The app's zip export holds each run's exact scenario JSON, its results and ledgers, and a manifest with both digests. Running `cyberliquidity run <run>/scenario.json` reprints the digest for comparison.
- The results digest includes the engine version. `SimulationResult.digest(engine_version=...)` lets the regression tests show that the 0.3 engine reproduces recorded 0.1 digests for 0.1 scenarios and recorded 0.2 digests for scenarios with credit lines.
- **Differential testing**: `tests/reference_model.py` re-implements sections 3, 4, 7.8 and 7.9 independently and naively, with recursive service evaluation, a literal rescan after every settlement, reserves and offsetting positions recomputed from scratch at every check, and plain dictionaries. The engine must match it on 300 seeded random scenarios (`cyberliquidity.generator`). These have random dependency graphs, capacities, schedules, disruptions with degradation and recovery, every intervention type, and both orderings and recycling rules. Three deliberately injected bugs in the 0.2 rules were each caught in 73–219 of the 300 scenarios. Of five injected bugs in the 0.3 rules, four were caught (in 5–56 scenarios each). The fifth changed only the tie-break between equally uncovered participants, which by property (i) of section 7.8 can matter only when capacity binds; a hand-built test covers that case.
