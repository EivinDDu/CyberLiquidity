# Security policy

CyberLiquidity is a local research simulator for **defensive resilience analysis**. It models outages and
service degradation using fictional organisations and synthetic data. It is not attack tooling, and it must not
be used to scan, probe or test real organisations or systems.

## Reporting a vulnerability

Please report security problems **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Please do not open a public issue for a security problem, and do not include real
personal or confidential data in a report.

Only the latest version on the `main` branch receives fixes.

## What the project does to limit risk

These are hardening measures for a local tool. They are not a security certification, and the app is not
designed to be exposed to untrusted networks.

| Layer | Where | What it does |
|---|---|---|
| Input checks | `src/cyberliquidity/safety.py` | Scenario files are checked for size, UTF-8 encoding, structure (schema, references, no dependency cycles) and complexity before anything is simulated. Problems are explained in plain language, without echoing the file's content. |
| Plain-text fields | `src/cyberliquidity/models.py` | Names and labels cannot contain markup (`< > [ ] \` and backticks), web addresses, or invisible and control characters (for example right-to-left overrides). Lengths are capped. |
| No raw HTML | `app/` | The app never renders raw HTML; text from scenario files is shown as plain text. The HTML report escapes all text. |
| Safe exports | `safety.safe_cell` | Text cells in exported CSV files that a spreadsheet would run as a formula (`=`, `+`, `-`, `@`, tab, carriage return) are prefixed with an apostrophe. |
| Integrity audit | `src/cyberliquidity/audit.py` | Every run shown in the app is re-checked independently: the cash ledger is replayed to every recorded balance, ids and settlement events are checked, totals are compared. The result is shown at the bottom of the page. |
| App configuration | `.streamlit/config.toml` | Binds to `localhost` only, 5 MB upload limit, XSRF protection, no usage statistics, error details kept out of the browser, developer menu hidden, fonts served locally (no internet needed). |
| Error boundaries | `app/ui/layout.py` | An unexpected error in one tab is contained there; the rest of the app keeps working. |
| Continuous integration | `.github/` | Tests (warnings count as failures, including property-based fuzzing of the loader), ruff lint with its security rules, bandit, pip-audit on the pinned dependencies. Actions are pinned to commit SHAs and the token is read-only. Dependabot proposes monthly updates. |
