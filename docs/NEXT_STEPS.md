# Next-stage plan

Version 0.3 is a tested, runnable prototype on synthetic data. Its engine is checked against an independent reference model. Version 0.2 added credit lines, an exact liquidity backstop, contagion analysis, a stress lab, a scenario editor and shareable reports. Version 0.3 adds multilateral offsetting, a stylised hoarding rule and a robustness check for rankings. Before any institution relies on it, the model needs real inputs, expert review and external validation. Engineering comes after those, not before.

## 1. Data needed (none of it is in the prototype)

| Data | Why it matters | Typical source (to be agreed with partners) |
|---|---|---|
| Payment schedules: amounts, timing, priorities, counterparties | Drive every result; the synthetic calendar was constructed by hand | Treasury management systems, ERP payables and receivables, payroll calendars |
| Intraday liquidity positions and credit facilities | Decide whether a delay propagates | Treasury and bank account data, credit-line terms |
| Service dependency maps, including fourth parties | Single points of failure and the independence of backups | ICT third-party registers, configuration-management databases (CMDB), vendor attestations |
| Throughput limits and recovery profiles | Backlog clearing; the value of backups | Provider SLAs, capacity tests, incident post-mortems |
| Settlement rules: cut-offs, cycles, queuing, netting | The hourly, gross, all-or-nothing rules are simplifications | Rulebooks of the payment and settlement systems used |
| Historical incident timelines | Scenario design and back-testing, not probabilities | Internal incident records, published post-incident reports |

Real data must stay local, be minimised and pseudonymised, and be governed by the partner's data-protection and confidentiality rules. The prototype has no authentication, encryption or access control and must not hold real data as it stands.

## 2. Domain review

- **Treasury and payment operations:**
  - Are the queue discipline, the bypass rule and within-step recycling realistic for the systems in scope?
  - Which outcome metrics do treasurers act on?
- **Operational resilience and IT risk:**
  - Are the dependency semantics (AND, failover OR, bottleneck degradation) adequate?
  - Is a fixed backup activation delay realistic?
- **Payment-system and market-infrastructure experts:** settlement windows, liquidity-saving mechanisms, central-bank facilities.
- **Literature review:** position the work against existing payment-system simulators, for example central-bank tools such as the Bank of Finland's BoF-PSS2, and studies of operational outages and liquidity in payment systems. Novelty must not be claimed until this review is done.
- **Regulatory context:** institutional use would need assessment against the applicable operational-resilience and ICT-risk frameworks (for example the EU's DORA). The prototype makes no claim of compliance.

## 3. Partner validation

1. **Face validity.** Walk partners through the hand example and two default scenarios, and record where the model's behaviour differs from their experience.
2. **Replication of past incidents.** For incidents with known timelines, check whether the model reproduces the observed backlogs and payment delays given the actual schedule and outage profile. Report the mismatches.
3. **Sensitivity analysis.** Systematically vary ordering, recycling, capacity, horizon and calendar alignment, and publish which conclusions survive. Version 0.3 automates the one-at-a-time version of this (model note, section 7.10); varying several assumptions together, and the horizon, remain to be done.
4. **Calibration protocol.** Fix any parameters estimated from data before the validation runs, to avoid fitting to the outcome.
5. **Independent review** of the code and the model note before any decision use.

## 4. Engineering roadmap (after the validation work is under way)

Done in v0.2:

- committed credit lines;
- an in-app scenario editor with validation;
- an exact-equivalent faster settlement loop (2.1× on 10,000 obligations), with identical digests;
- differential testing against a reference model.

Done in v0.3:

- multilateral offsetting as a switchable liquidity-saving mechanism, with a clearing account and its own invariants;
- a treasury behaviour rule (holding back routine payments to protect payroll);
- a robustness check for intervention rankings (one-at-a-time variants, minimax regret);
- a second built-in scenario with real gridlock, and a second experiment;
- a continuous-integration workflow on two Python versions.

Still to do:

- An HTTP API around the engine (it already has no interface dependency), and importers from treasury or ERP exports into the schema (`docs/scenario.schema.json`).
- **Model extensions, each switchable and tested against the current rules:**
  - cut-off times and finer time steps;
  - partial payments, bilateral offsetting and an *optimising* multilateral algorithm (the present rule is greedy), credit-line fees, collateral and expiry;
  - richer treasury behaviour: reprioritising, borrowing or cancelling, and rules that react to what others do;
  - operational dependency cycles (fixed-point evaluation);
  - load-sharing alternatives and failover delays;
  - recipient-side processing.
- **Scale:** the settlement loop now re-checks only the payments a settlement can unblock. Recording every blocking reason every hour now dominates run time: about 3 s for 300 participants, 10,000 obligations and 168 hours. A compact block encoding would be next, again verified by identical digests and the differential tests.
- **Backstop:** a global (not only locally smallest) liquidity allocation, for example by integer programming on the recorded constraints. The present search is verified, but greedy.
- **Uncertainty:** only when distributions for outage durations, timing or schedules can be justified from data. Probabilistic results would then be reported as distributions conditional on those inputs, never as point "expected losses".
- **Security**, before real data is used: authentication, role-based access, audit logging, encrypted storage, dependency pinning and vulnerability scanning, and a threat model for the deployment.
