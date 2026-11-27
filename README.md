# CyberLiquidity

A deterministic research simulator for one question:

> *If this service becomes unavailable for twelve hours, which payments are delayed, who subsequently runs short of cash, and which intervention reduces the disruption most?*

CyberLiquidity keeps two networks apart and couples them explicitly:
- an **operational** network of cloud, identity, payment-provider and settlement services with typed dependencies and shared capacity;
- a **financial** network of funded participants and their payment obligations.

Each payment depends on an operational route. A delay can therefore come from an **operational disruption** (the route is down or saturated) or from **financial propagation** (the payer did not receive expected money). The tool records both separately, explains every delay from recorded events, traces how far the delays travel, and compares resilience interventions, alone and in combination, on the same stressed run. It also tests whether the recommended intervention stays best when the assumptions change.

> **Synthetic scenario. Results are conditional on model assumptions and are not forecasts of real incidents.**
> All organisations and data are fictional and labelled synthetic. The model is not calibrated to any real market and makes no claim of novelty, regulatory compliance, production security or predictive validity.

Intended first users: treasury teams, operational-resilience teams and researchers.

## Open the app

**Easiest (macOS):** open the `CyberLiquidity` folder in Finder and double-click **`start.command`**. A Terminal window opens and your browser shows the app at **http://localhost:8501**. Keep the Terminal window open while you use the app; close it, or press Ctrl+C in it, to stop.

- The first start creates `.venv` and installs the pinned dependencies, which needs internet once and takes a minute or two. Later starts are immediate.
- If macOS says it cannot verify the file, right-click `start.command`, choose **Open**, and then **Open** again.

**From Terminal** (after cloning the repository):

```bash
cd CyberLiquidity
./start.command            # or: make app
```

Everything runs on your computer. The app listens only on `localhost`; nothing is sent anywhere, and no account, API key or internet connection is needed after installation.

## What is new in v0.3

See [CHANGELOG.md](CHANGELOG.md) for details.

- **How robust is the answer?** The comparison is re-run under ten one-at-a-time changes to the assumptions (settlement rules, outage timing and length, balances, throughput). The app shows a grid of every intervention's effect in every variant and names the minimax-regret choice; "The answer" says in one paragraph whether the recommendation survives. No variant is weighted, and nothing is a probability.
- **Multilateral offsetting (gridlock resolution)**: a liquidity-saving mechanism that settles queued payments together when every participant's net position is covered, with net cash movements through a clearing account.
- **Payroll-protection reserve**: a treasury rule in which firms hold back routine payments to keep cash for their own urgent ones. It is a behavioural rule, not added money, and in these scenarios it usually makes the network's disruption worse.
- **A second built-in scenario**, a circular-trade cluster in which an outage causes real gridlock.
- **Experiment 2: gridlock and hoarding.** Offsetting helps only in a middle band of cash and never in the one-directional supply chain. Hoarding that helps one firm alone stops helping once every firm does it, and it can cancel a liquidity buffer entirely.
- **Verification**: the reference model covers both new rules (300 random scenarios, mutation-checked), and v0.2 results are reproduced byte for byte.

**v0.2** added the plain-language answer, contagion analysis, committed credit lines, the exact liquidity backstop, intervention pairs, the stress lab, the scenario editor, the HTML report and differential testing:

- **The answer, in plain words**: a briefing at the top of the app, and `cyberliquidity brief`, answers the central question from recorded results.
- **Contagion analysis**: how many steps each delay is from the outage, a contagion map, and who ran short of cash without ever losing a payment route.
- **Committed credit lines**: a fourth intervention, drawn on demand with every draw and repayment in the ledger. In this model it settles exactly like an equal-sized pre-funded buffer, but uses only the liquidity actually needed.
- **Exact liquidity backstop**: the smallest pre-funding per participant that reproduces the unlimited-liquidity outcome, found by search and re-checked by simulation (no more guessing on a grid).
- **Combinations**: every pair of interventions is run together and compared with an independence benchmark (complementary, overlapping or roughly independent).
- **Stress lab**:
  - which service matters most;
  - the longest outage that keeps payroll and tax payments on time;
  - the worst start hour;
  - a start × duration grid.
- **App additions**: concurrent disruptions, a validated **scenario editor**, and a one-file **HTML briefing report** that works offline.
- **Verification**: the engine is checked against an independent reference implementation on 300 random scenarios, and reproduces version 0.1's results byte for byte. It is also 2× faster on large networks.
- **Experiment v2**: a third (common-mode) case, the credit line and a combination, start-time robustness and the exact backstop.

## Quick start (manual setup)

Tested with Python 3.13.7 on macOS. The package declares Python 3.11 or later, but other versions were not tested.

```bash
cd ~/CyberLiquidity
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt     # pinned direct dependencies
python -m pip install -e .                    # the cyberliquidity package (src/)
python -m pytest                              # run the test suite
streamlit run app/streamlit_app.py            # opens http://localhost:8501
```

`requirements-lock.txt` pins the complete tested environment (`pip install -r requirements-lock.txt`).

- **Dependencies:** the engine itself depends only on Pydantic. Streamlit, Plotly and pandas serve the interface, Matplotlib draws the experiment figure, and pytest runs the tests.
- **Nothing external is needed:** no cloud services, paid APIs, authentication, language model or private data.
- **Local only:** the bundled `.streamlit/config.toml` binds the app to `localhost` and turns off Streamlit's usage statistics.

`make help` lists shortcuts (`make app`, `make test`, `make experiment`, `make brief`).

## What you can do

### The app

**Sidebar controls:**
- the scenario (bundled, uploaded, or your edited version);
- the disruption: service, start hour, duration, severity in % of capacity lost, an optional recovery ramp, and up to two concurrent disruptions;
- the intervention shown as run C, with the parameters of all six interventions;
- whether to run every pair of interventions;
- the ranking metric;
- downloads.

The app always runs **A** (baseline, no disruption), **B** (stress), **B + each intervention**, and optionally **B + each pair**.

| View | Shows |
|---|---|
| **Overview** | **The answer** (plain-language briefing), scenario facts, headline comparisons of A, B and C, overdue value over time, delay distribution, blocking-reason split, full metrics table, key assumptions |
| **Contagion** | How far the disruption travels: payments held up directly vs by contagion, contagion depth, a top-to-bottom contagion map, delayed value by step, per-participant exposure (who ran short of cash, from when, reached at which step, and whether only through delayed incoming money) |
| **Network** | Operational dependencies, financial obligations and the payment-routing coupling as separate layers at any hour; service and participant tables; single-point-of-failure analysis |
| **Timeline** | Backlog (value and count), credit outstanding, overdue obligations by blocking reason, service-capacity heatmap, throughput against capacity, cash balances by participant (small multiples), hourly table |
| **Interventions & evidence** | <ul><li>The six interventions against the same B, with mechanism statements built from the records</li><li>**How robust is the ranking?** Every intervention under ten assumption variants, with the minimax-regret choice</li><li>Combinations against an independence benchmark</li><li>The **exact liquidity backstop**</li><li>The liquidity-buffer sweep (smallest *tested* buffer)</li><li>A per-payment trace with cited event IDs ("why was this payment delayed?")</li><li>Obligations unsettled at the horizon</li></ul> |
| **Stress lab** | <ol><li>Which service matters most (the same outage applied to every service, with the best intervention for each)</li><li>How long the outage can last before an outcome breaks (longest tolerated outage per intervention)</li><li>The worst start hour, plus an optional start × duration grid</li></ol>Runs only when you open the tab. |
| **Scenario editor** | Edit opening balances, service capacities and the payment schedule (add or delete payments), or scale and shift them globally. Edits are validated before use, live only in your session, and can be downloaded as JSON. |
| **Model & assumptions** | `docs/MODEL.md` and the exact scenario JSON simulated |

**Downloads:**
- **Briefing report (.html)**: one self-contained file with the answer, charts, tables, assumptions and reproduction digests. Share it with someone who does not run the app; it opens offline in any browser.
- **All runs (.zip)**: every run's exact `scenario.json`, the full event ledger (`events.csv`), cash ledger, outcomes, blocking records, time series and a `manifest.json` with SHA-256 digests.

Deep links to a tab work, for example `http://localhost:8501/?tab=Stress+lab`.

### The command line

```bash
cyberliquidity brief scenarios/default_72h.json          # the plain-language answer
cyberliquidity robustness scenarios/cluster_gridlock_48h.json   # does the best intervention stay best?
cyberliquidity run scenarios/default_72h.json --out runs/default        # prints the results digest
cyberliquidity compare scenarios/default_72h.json --out runs/compare    # A, B and B + each candidate intervention
cyberliquidity backstop scenarios/default_72h.json       # exact liquidity backstop per participant
cyberliquidity criticality scenarios/default_72h.json --start 32 --duration 12
cyberliquidity profile scenarios/default_72h.json --service cloud_nimbus --start 32 --outcome priority_on_time
cyberliquidity sweep scenarios/default_72h.json --participants oakridge,silverfern,thistle,copperleaf --grid 0,5000,10000,20000,30000
cyberliquidity validate scenarios/default_72h.json
cyberliquidity generate --seed 7 --participants 40 --obligations 800 --out runs/random_7.json   # random structural scenario
cyberliquidity generate-default --out scenarios/default_72h.json        # regenerate the synthetic fixture
cyberliquidity generate-default --which cluster                         # regenerate the circular-trade fixture
cyberliquidity schema --out docs/scenario.schema.json
```

**Reproducing an exported run:** `cyberliquidity run <run folder>/scenario.json` must print the `results_digest` recorded in the bundle's `manifest.json`.

### The research experiments

```bash
python experiments/redundancy_vs_liquidity.py     # experiment 1, about 11 s
python experiments/gridlock_and_hoarding.py       # experiment 2, about 5 s
```

**Experiment 1** compares three cases:
- direct operational blockage;
- delayed incoming cash;
- a common-mode identity-provider outage.

Each case is run at eight outage durations with seven intervention settings, with the payment schedule held constant. It then checks robustness under alternative ordering and recycling rules and under shifted outage start times, and computes the exact liquidity backstop of every stressed run. It runs 1,837 deterministic simulations.

**Experiment 2** asks when multilateral offsetting resolves gridlock after an outage, and whether firms that hold cash back for payroll protect it, and at what cost to everyone else. It uses both built-in scenarios and 300 random test structures, and runs 1,257 deterministic simulations.

Outputs go to `experiments/output/`; the write-up of both is in [docs/EXPERIMENT.md](docs/EXPERIMENT.md).

## Project layout

```
src/cyberliquidity/     engine package (UI-independent)
  models.py             validated scenario model (Pydantic): services, routes, participants, obligations, disruptions, interventions
  operational.py        service state, capacity, dependency propagation, single points of failure
  engine.py             hourly settlement engine, credit lines, offsetting, reserve rules, ledgers, blocking reasons, invariant checks
  metrics.py            metric definitions and export tables
  compare.py            A/B/C comparisons, combinations, independence benchmark, mechanism statements
  cascade.py            contagion generations and participant exposure
  analysis.py           stress lab (criticality scan, duration/start profiles, impact surface) and ranking robustness
  briefing.py           plain-language answer generated from recorded results
  explain.py            deterministic explanation traces from events
  sweep.py              buffer grid search, cash-unconstrained reference, exact liquidity backstop
  variants.py           scenario variants and intervention builders
  generator.py          seeded random scenarios (testing and scale)
  export.py             JSON/CSV exports, reproducible zip bundles, digests
  synthetic.py          generator of the default synthetic scenario
  synthetic_cluster.py  generator of the circular-trade (gridlock) scenario
  cli.py                command-line interface
app/                    Streamlit interface (streamlit_app.py); charts, graphs, theme and HTML report in ui/
scenarios/              default_72h.json (12 firms, 72 h), cluster_gridlock_48h.json (8 firms, 48 h), three_participant.json (hand example)
experiments/            redundancy_vs_liquidity.py, gridlock_and_hoarding.py and their committed outputs
docs/                   MODEL.md, EXPERIMENT.md, NEXT_STEPS.md, scenario.schema.json
tests/                  unit, invariant, differential (reference model), integration, experiment, report and app tests
start.command           double-click launcher (macOS); Makefile has the same shortcuts
```

## The model in brief

Full rules, equations, metric definitions and proofs: [docs/MODEL.md](docs/MODEL.md).

- **Time and money**: one-hour steps; integer minor currency units; one configurable currency, no FX. A payment due in hour *h* is on time if it settles by the end of hour *h*; it cannot settle earlier.
- **Operational network**: an edge `A → B` means A depends on B. `requires_all` means every listed service is needed; a `requires_any` group needs only one of its members (failover). Capacity is the bottleneck of the service's own state and its dependencies, and each service has one capacity pool shared by all users. Dependency cycles are rejected in this version.
- **Each hour**:
  1. update service states;
  2. apply scheduled intervention funding;
  3. queue newly due obligations;
  4. settle in order of `(priority, due hour, id)`, as if rescanning from the top after every settlement, so that cash received can fund further payments in the same hour (within-step recycling). A payer with a credit line draws exactly any shortfall; a payer under a reserve rule releases routine payments only from cash it does not need to keep;
  5. with offsetting switched on, settle covered sets of queued payments together, then repeat step 4;
  6. record every reason each remaining obligation is blocked;
  7. repay credit from end-of-hour cash.
- **Settlement** is atomic and in full: no partial payments or overdrafts. Intraday credit exists only as the explicit credit-line intervention, and netting only as the explicit offsetting intervention.
- **Invariants**, checked by the engine and re-checked by the tests:
  - a payment settles at most once;
  - no negative balances;
  - equal debit and credit;
  - cash conserved within the boundary, with external flows, funding and credit recorded;
  - credit within its limits;
  - every offsetting batch nets to zero;
  - shared capacity respected;
  - no bypassing an unavailable route;
  - unsettled obligations stay visible.
- **Not computed**: economic loss, insolvency, "liquidity required" read from overdue amounts, probabilities, expected losses, confidence intervals or ROI.

## Scenario files

A scenario is a single JSON document (schema: `docs/scenario.schema.json`) containing:
- `currency` and `horizon_hours`;
- `services` (with `capacity_per_hour`, `requires_all` and `requires_any`), `routes` (`path`), `participants` (`initial_balance`), `external_parties` and `obligations` (`payer`, `recipient`, `amount`, `due_hour`, `priority`, `routes`);
- `disruptions`, plus `interventions` applied in the run: `backup_route`, `liquidity_buffer`, `faster_recovery`, `credit_line`, `offsetting` or `priority_reserve`;
- `candidate_interventions` used as comparison presets;
- `settings` (ordering and recycling).

Validation rejects, with clear messages:
- unknown references and duplicate IDs;
- amounts that are not positive;
- times outside the horizon;
- payments from a party to itself, or between two external parties;
- dependency cycles;
- buffers, credit lines or reserve rules for parties outside the boundary, overlapping credit lines or reserve rules, and more than one offsetting intervention;
- unknown fields.

## Tests

`python -m pytest` runs 389 tests, among them:
- the three-participant hand example and hand examples for credit lines, offsetting (cycles, net settlement through the clearing account, the removal rule and its tie-break, capacity, next-step recycling, credit draws) and the reserve rule (holding back, look-ahead window, undrawn credit);
- invariant tests over 31 scenario variants, six of them random, including offsetting batches that must net to zero;
- **differential tests**: the engine against an independent reference model on 300 random scenarios, offsetting batches included (mutation-checked: deliberately injected bugs are caught);
- checks that version 0.1's and version 0.2's recorded results digests are reproduced byte for byte;
- the robustness check (variants, regret, minimax), and both experiments: a fresh run must reproduce the committed outputs byte for byte;
- the backstop theorems (sufficiency always; tightness under next-step recycling) on random scenarios;
- credit line ≡ buffer equivalence;
- contagion generations, including gridlock;
- the stress lab, combinations and the briefing text;
- operational rules, interventions and validation errors;
- determinism, explanations and exports, the CLI including the new commands, and experiment reproducibility from `config.json`;
- the HTML report;
- headless smoke tests of every app tab, the scenario editor and concurrent disruptions via `streamlit.testing`.

## Not implemented yet (deliberately out of scope)

- Operational dependency cycles (rejected), partial payments, liquidity-saving mechanisms other than the greedy offsetting rule, credit-line fees, collateral and expiry, cut-off times and settlement cycles.
- Behavioural responses other than the stylised reserve rule (cancellations, penalties, learning), and multiple currencies.
- Optional cost assumptions for interventions. No costs are modelled; if added, they must be labelled as user-provided or illustrative.
- An HTTP API (the engine is UI-independent, so an API is a thin layer) and importers from treasury or ERP systems.
- Uncertainty or Monte Carlo modelling. It is intentionally absent until defensible distributions exist.
- Real data, and the security controls real data would need. See [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) for the data, domain review and partner validation required before institutional use.
