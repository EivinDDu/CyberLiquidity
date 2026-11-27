# Changelog

All results are synthetic and conditional on the model's assumptions (docs/MODEL.md).

## 0.4.0 (2026-11-27)

### Engine: faster, with the same results
- **Same results as 0.3.** Events, ledger, balances and outcomes are unchanged for every scenario. Only the engine version stamped into new result digests differs. How this was checked:
  - 114 recorded 0.3 result digests are reproduced byte for byte (`tests/golden_v03_digests.json`);
  - during development, 368 complete results (14 parts each) and 142 backstop answers were compared with the 0.3 code;
  - both experiments were regenerated: every simulated number is the same, only the digests and the backstop's run counts changed.
- **Speed** (`benchmarks/bench_engine.py`; medians of three interleaved rounds of five runs, Python 3.13.7, Apple M4):

  | Workload | 0.3 | 0.4 | Change |
  |---|---:|---:|---:|
  | One stress run (default scenario) | 3.2 ms | 2.3 ms | −28% |
  | Compare all interventions and pairs (default / cluster) | 24.5 / 36.4 ms | 17.1 / 25.7 ms | −30% / −29% |
  | Exact liquidity backstop (default) | 285.6 ms | 102.0 ms | −64% |
  | Robustness check, 10 variants (default / cluster) | 121.4 / 120.6 ms | 87.7 / 85.8 ms | −28% / −29% |
  | Six random networks (40 firms, 800 payments) | 1,081.6 ms | 682.8 ms | −37% |
  | **All seven workloads** | **1,673.4 ms** | **1,003.4 ms** | **−40%** |

  Timings depend on the machine; the benchmark script reproduces them.
- How: each hour's route checks are computed once and shared; the offsetting search is incremental (it rebuilds only when an earlier payment in the set caused a capacity failure); service status is cached per set of active disruptions; block flags are filled in once; unnecessary queue sorts are skipped.
- **Liquidity backstop** (model note 7.5, step 5): a trial run stops at the end of the first hour in which it can no longer reproduce the target outcome, and an allocation that was already tried is not run again. Neither changes the answer.
  - New field `runs_stopped_early`.
  - `runs_used` is lower. Default scenario: 93 → 90, and 86 of the 90 stopped early. Circular-trade cluster: 101 → 98. Three-participant check: 13 → 11.
- The engine no longer uses `assert` for an internal check; it raises `InvariantViolation` instead, so the check also runs under `python -O`.

### Protective layers
- **Input checks** (`cyberliquidity.safety`). Scenario files must pass four checks before anything is simulated: size, UTF-8 encoding, structure and complexity.
  - Problems are listed in plain language and never echo the file's content.
  - The app accepts smaller scenarios than the command line. `--allow-large` lifts the command-line limits.
- **Plain-text fields.** Names, labels and descriptions cannot contain invisible or control characters (such as right-to-left overrides). Names and labels also reject markup (`< > [ ] \` and backticks) and web addresses. Lengths are capped.
- **CSV exports neutralise spreadsheet formulas.** A text cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe; numbers are left alone.
- **Integrity audit** (`cyberliquidity.audit`). Every run shown in the app is re-checked independently, and the result appears at the bottom of the page. The audit:
  - replays the cash ledger to every recorded balance;
  - checks event and ledger numbering and settlement events;
  - compares the final totals.
- **The app never renders raw HTML.** Text taken from a scenario file is shown as plain text.
- **Each tab is contained.** An unexpected error is reported in its own tab, and the rest of the app keeps working; details go to the terminal only.
- **Hardened app configuration**: localhost only, 5 MB upload limit, explicit XSRF protection, no usage statistics, short error messages in the browser, developer menu hidden.
- **CI**:
  - third-party actions pinned to commit SHAs, with a read-only token;
  - a lint and security job (ruff with its security rules, bandit, pip-audit);
  - the test suite treats any warning as a failure;
  - Dependabot proposes monthly updates.
- New `SECURITY.md` explains how to report a problem privately.

### App: cleaner, easier to use
- **New look**:
  - the Inter typeface, self-hosted (SIL Open Font License, `app/static/fonts/`), so no internet connection is needed;
  - light and dark themes, with text contrast checked against WCAG AA;
  - a logo and Material icons;
  - bordered metric tiles with shorter values (three significant digits).
- **Clearer structure**:
  - the sidebar runs in four numbered steps: Scenario → Disruption (run B) → Response (run C) → Ranking;
  - the header has *Guide* and *Export* menus and short facts about the scenario;
  - the synthetic-data notice stays visible on every page.
- **Help for new users**:
  - a four-step quick start (Overview tab and Guide menu);
  - a plain-language glossary of 20 terms (Guide menu and Model tab);
  - scenario names instead of file names, and plainer control labels.
- **Accessible numbers**: every bar and line chart has *Show the numbers as a table*, read from the chart itself.
- **Readable errors**: problems with an uploaded or edited scenario are listed in plain language.

### Tests and tooling
- 486 tests, run on Python 3.13.7 and 3.11.13. New in this release:
  - property-based fuzzing of the loader, the audit and the CSV guard (Hypothesis);
  - the input-check and audit tests;
  - the 0.3 golden digests;
  - tests that each speed-up gives exactly the slow path's answer;
  - tests of the app's error boundary and of the integrity footer.
- `requirements-dev.txt` holds the pinned development tools. New `make` targets: `dev`, `lint`, `audit` and `bench`.

## 0.3.0 (2026-11-27)

### Model and engine
- **Multilateral offsetting** (`offsetting` intervention, model note 7.8): after each hour's settlement loop, a covered set of queued payments settles together.
  - Greedy removal: the last payment of the participant with the largest uncovered net position goes first.
  - Every obligation is still discharged in full with its own `SETTLEMENT` event, but cash moves net through a clearing account outside the boundary (`offset_debit`, `offset_credit`) that is empty after every batch.
  - Net payers can draw committed credit; batch proceeds follow the recycling rule.
  - The settlement loop runs again after each batch.
- **Priority reserve** (`priority_reserve` intervention, model note 7.9): a treasury rule under which covered participants release routine payments only from cash, plus undrawn credit, that they need not keep for their protected payments due within a look-ahead window.
  - Blocks record the reserve kept; a block is *held by the reserve* when the payer could otherwise have paid.
- The settlement loop's exact-equivalent speed-up still holds with reserves (a protected payment lowers cash and reserve equally). It is now a separate method that offsetting reuses.
- **Version-pinned regression**: scenarios with credit lines reproduce the recorded 0.2 digests byte for byte (`tests/golden_v02_digests.json`), in addition to the 0.1 digests.
- The liquidity backstop rejects scenarios with an applied reserve rule, because its sufficiency argument does not cover them.

### Analysis
- **Ranking robustness** (`analysis.robustness`, model note 7.10):
  - the comparison is re-run under ten one-at-a-time assumption variants (other recycling and queue rules, outage ±3 h, ±50% duration, balances ±25%, throughput −25%);
  - interventions are applied as plans that follow the moved outage;
  - reported: wins, practical ties, the largest regret and the range of reduction per intervention, and the minimax-regret choice;
  - no variant is weighted.
- New metrics: delayed priority-1 payments and their overdue value × hours (both also ranking metrics), payments settled by offsetting, liquidity saved by offsetting, and payments held back by a reserve.
- Combinations with a harmful member are labelled *more harmful together* or *less harmful together*, instead of the misleading *overlapping* or *complementary*.
- The briefing adds a robustness paragraph, states when an intervention makes things worse, explains whether offsetting found gridlock, and states what the reserve rule does to urgent payments.
- Explanation traces say when a payment was held back by the payer's reserve rule rather than by a lack of cash.

### Scenarios
- **Circular-trade cluster** (`scenarios/cluster_gridlock_48h.json`, generated by `synthetic_cluster.py`): eight fictional firms whose supplier ring locks up when an outside customer's payment is stuck in an outage. Its candidates include all six intervention types.

### App
- Two new interventions in the sidebar, all six compared side by side, and every pair optionally.
- **How robust is the ranking?** A diverging grid of every intervention under every variant (values printed in each cell, best starred), with a minimax-regret table. The same check feeds "The answer" and the HTML report.
- The run palette extends to seven validated categorical slots, checked for colour-vision deficiency in light and dark mode. Pair charts allow negative effects.
- Fixed: the HTML report labelled and formatted every ranking metric as money value-hours, even when the ranking was by a count.

### CLI
- `robustness` (table, CSV export and the robustness paragraph); `brief` now includes robustness; `generate-default --which cluster`.

### Experiment 2: gridlock and hoarding (new)
- `experiments/gridlock_and_hoarding.py` runs 1,257 deterministic runs in about 5 s:
  - an offsetting grid over balance levels, recycling rules and outage lengths;
  - a reserve-rule sweep over look-ahead windows and protected priorities, with and without the outage;
  - one firm alone against every firm;
  - pair interactions;
  - 300 random test structures.
- A test checks that the committed outputs are reproduced byte for byte.
- Experiment 1 is unchanged under engine 0.3 (only the version-stamped results digests differ).

### Tests and tooling
- 308 → 389 tests:
  - the reference model implements offsetting and reserves independently, and the differential test compares offsetting batches too (mutation-checked: 4 of 5 injected bugs caught; the fifth is a tie-break that provably cannot matter without capacity limits, covered by a hand-built test);
  - hand examples for every offsetting and reserve rule;
  - invariant tests over 31 scenario variants;
  - robustness, report and app tests.
- GitHub Actions workflow runs the test suite on Python 3.11, 3.12 and 3.13.

## 0.2.0 (2026-11-26)

### Model and engine
- **Committed credit line** (`credit_line` intervention):
  - drawn on demand, only for exactly the shortfall of a payment whose route works;
  - repaid from end-of-hour cash;
  - every draw and repayment is a ledger entry with an account outside the boundary;
  - new invariants: outstanding credit stays within [0, limit] and reconciles with draws and repayments.
- **Version-aware results digest**: `SimulationResult.digest(engine_version=...)`. Scenarios that use only 0.1 features reproduce the recorded 0.1 digests byte for byte (regression test).
- **Exact-equivalent faster settlement loop**: after a settlement, only the recipient's own queued payments are re-checked. The same payments settle in the same order, verified by digests and differential tests, and a 300-participant, 10,000-obligation, 168-hour network runs 2.1× faster.

### Analysis
- **Contagion generations** (`cascade.py`): each delayed payment is classified as direct, propagated (at step *k*) or unlinked. The module also reports contagion depth, value by step, and per-participant exposure, including participants that ran short of cash although their own routes never failed.
- **Exact liquidity backstop** (`sweep.liquidity_backstop`): the smallest hour-0 pre-funding per participant that reproduces the unlimited-liquidity outcome. Proven sufficient (the peaks of a non-binding credit line), refined by binary search, and each amount re-checked by simulation.
- **Combinations**: any set of interventions can be run together, and is compared with the independence benchmark `1 − Π(1 − f_i)`.
- **Stress lab** (`analysis.py`):
  - criticality scan across services, with the best intervention per service;
  - duration profile with the longest tolerated outage;
  - start-time profile;
  - start × duration impact surface.
- **Briefing** (`briefing.py`): a deterministic plain-language answer to the central question.
- **Random structural scenarios** (`generator.py`) for fuzzing and scale.

### App
- New tabs:
  - **Contagion**: contagion map, steps and participant exposure;
  - **Stress lab**: runs only when opened, and keeps its settings;
  - **Scenario editor**: validated edits, session only, downloadable.
- **The answer** at the top of the Overview.
- Committed credit line as a fourth intervention; every pair of interventions; up to two concurrent disruptions.
- **Briefing report (.html)**: one self-contained, offline file with the answer, charts, tables, assumptions and digests.
- Native deep links (`?tab=…`).
- Cached computations.
- The credit-line colour (palette slot 5) was re-validated for colour-vision deficiency in light and dark mode.

### CLI
- New commands: `brief`, `backstop`, `criticality`, `profile` and `generate`.

### Experiment (v2)
- New: the common-mode case (identity-provider outage), the credit line, the backup + buffer combination, a robustness check shifting the outage by ±3 and ±6 h, the exact backstop for every stressed run, and a 1% materiality threshold for verdicts.
- New outputs: `complementarity.csv`, `start_offsets.csv` and `backstop.csv`.

### Tests and tooling
- 188 → 308 tests:
  - differential tests against an independent reference model on 300 random scenarios (mutation-checked);
  - the backstop theorems on random scenarios;
  - credit line ≡ buffer equivalence;
  - golden 0.1 digests;
  - tests for every app tab and the editor;
  - report tests.
- `start.command` (double-click launcher) and a `Makefile`.

## 0.1.0 (2026-11-26)

First version:
- the two-network engine;
- baseline, stress and three interventions;
- the Streamlit app with five views;
- the CLI;
- the redundancy-vs-liquidity experiment;
- 188 tests.
