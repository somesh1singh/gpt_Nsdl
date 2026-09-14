# NSDL CAS Portfolio Intelligence & Advisory System

**Repository package version:** 1.0.6  
**Blueprint implementation baseline:** 1.0  
**Target Python:** 3.14  
**Entrypoint:** `app.py`  
**UI:** Streamlit  
**Build date:** 2026-09-14

This repository intentionally contains **only three files**. For Streamlit/Linux deployment the executable and dependency filenames must use lowercase extensions:

```text
README.md
requirements.txt
app.py
```

It consolidates the earlier multi-file implementation into one Streamlit application so it can be uploaded directly to GitHub and tested on Streamlit Community Cloud.

## What v1.0.6 implements

The app follows the supplied NSDL CAS Portfolio Intelligence & Advisory System blueprint and includes:

- NSDL CAS PDF upload and heuristic text extraction
- Password-protected/encrypted PDF support with secure password prompts
- document fingerprinting and parse confidence
- ISIN-based holding extraction
- multi-CAS reconciliation workspace
- security/symbol canonicalisation workspace
- XIRR engine using dated cash flows
- Automated XIRR reconstruction from heuristically parsed CAS transactions
- Automatic benchmark simulation for NIFTY 50, NIFTY 500 and Gold BeES
- tax-lot engine with FIFO and LIFO sale simulation
- equity holding-period classification
- realised gain and estimated-tax preview
- bonus/split cost-basis adjustment utility
- harvestable-loss detection
- return-waterfall attribution
- Brinson-Fachler attribution
- benchmark XIRR comparison
- point-in-time FII/DII/promoter shareholding lookup
- four-quarter FII/DII delta and smart-money score
- historical index-membership lookup
- historical F&O-membership lookup
- institutional-divergence screening
- advice impact estimator with tax/cost guardrails
- data-quality and quantity-continuity checks
- goal-based required-return calculator
- scenario/stress projection
- downloadable analysis tables
- implementation/status page inside the app

## Data-source model

The blueprint references `aditya-jha/nse-historical-membership`. Because this GitHub package is constrained to three files, the repository's CSV data is **not bundled**. The Streamlit app instead supports:

1. direct upload of the relevant CSV files in the Institutional Intelligence tab; and
2. best-effort fetching from the upstream public GitHub repository at runtime.

Expected upstream paths:

```text
shareholding_history/data/parsed/_flat.csv
shareholding_history/data/parsed/_signals.csv
index_history/data/index_membership_history.csv
fno_history/data/fno_membership_history.csv
shareholding_history/data/_universe.csv
index_history/data/manual_overrides/symbol_renames.json
```

If upstream structures change, the app will fail gracefully and allow manual CSV upload instead.

## Python 3.14 deployment

Streamlit Community Cloud lets you select the Python version during deployment.

When creating the app:

1. Push these three files to the root of a GitHub repository.
2. Open Streamlit Community Cloud.
3. Click **Create app**.
4. Select your GitHub repository.
5. Set the entrypoint to `app.py`. Streamlit rejects an uppercase `.PY` extension.
6. Open **Advanced settings**.
7. Select **Python 3.14**.
8. Deploy.

If you later need to change the Python runtime, Streamlit Community Cloud requires redeployment.

## Recommended GitHub layout

```text
your-repository/
├── APP.PY
├── README.MD
└── REQUIREMENTS.TXT
```

No additional source files are required for v1.0.6.

## Local test

With Python 3.14:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r REQUIREMENTS.TXT
streamlit run app.py
```

## Important implementation notes

### CAS parsing

The parser in v1.0.6 is a **defensive heuristic parser**, not yet a guaranteed parser for every historical NSDL CAS layout. It extracts text from PDFs, identifies ISIN-linked lines, attempts to infer quantities and market values, and exposes confidence/warnings instead of silently fabricating data.

For industry-grade use, parser hardening should be performed against several real CAS files spanning different statement formats and years.

### Tax calculations

The tax-lot module provides an analytical estimate and supports configurable STCG/LTCG rates in the UI. It should not be treated as a tax filing engine. Indian tax rules change by asset type and financial year, and grandfathering/indexation/special cases need dedicated validation.

### Forward-looking estimates

The Advice Impact and Scenario modules are simulations. Expected XIRR is not a promise of future performance. The app shows confidence and allows a "do nothing" baseline.

### Privacy

Uploaded files are processed in the running Streamlit session. This three-file Community Cloud build does not implement encrypted persistent object storage, user authentication, database isolation, or field-level PAN encryption. Those remain production-hardening items from the blueprint.

Do not deploy sensitive personal CAS statements to a public/shared app unless you understand the hosting and access implications.

## Version history

### v1.0.6 — 2026-09-14

- converted the earlier multi-file backend implementation into a single Streamlit app
- changed deployment target to Python 3.14
- preserved only `README.MD`, `requirements.txt`, and `app.py`
- added in-app version display
- added dependency/version diagnostics
- added institutional intelligence upload/fetch workflow
- added portfolio analytics, tax lots, attribution, benchmarking, scenario and advice modules
- added graceful handling for incomplete source data
- added CSV download exports for analysis tables

## Next version candidates

- hardened CAS format detection and transaction extraction
- corporate-action automation for merger/demerger/rights/spinoff
- AMFI NAV connector
- NSE/BSE price-history connector
- category/risk-matched benchmark engine
- persistent portfolio/session store
- user authentication
- encrypted private storage
- audit log and recommendation evidence pack
- closed-loop recommendation tracking
- full Indian tax-year rules
- richer goal planning and staged rebalancing


### v1.0.6 — Streamlit filename hotfix

- Renamed `APP.PY` → `app.py` because Streamlit requires a lowercase `.py` extension.
- Renamed `REQUIREMENTS.TXT` → `requirements.txt` so Streamlit Community Cloud reliably detects dependencies.
- Renamed `README.MD` → `README.md` for standard GitHub naming.
- No analytical logic removed.


### v1.0.6 — Password-protected CAS PDF support

- Added support for encrypted/password-protected PDF statements.
- After a PDF is uploaded, the CAS Parser page asks for a password for each uploaded PDF.
- The password field uses masked input.
- Passwords are used only to authenticate the PDF during parsing and are **not** stored in session state, exports, logs, or analysis tables.
- If a PDF is not encrypted, the password field may be left blank.
- If a password is missing or incorrect, the app now shows a specific message instead of a generic parsing failure.


### v1.0.6 — Institutional path + India-date fixes

- Corrected the upstream universe path to `shareholding_history/data/_universe.csv`.
- Added the separate upstream `symbol_renames.json` mapping.
- Added symbol-rename display in Institutional Intelligence.
- Replaced server-UTC `date.today()` defaults with India calendar date (`Asia/Kolkata`) so cloud deployment does not show the previous date during early-morning IST usage.
- Preserved Python 3.14 target and password-protected PDF support.


### v1.0.6 — Automated CAS XIRR & benchmark reconstruction

- Added stateful heuristic transaction extraction from CAS PDFs.
- CAS Parser now displays and exports inferred transaction rows with source lines and confidence.
- Added an **Automated from CAS** tab under XIRR & Benchmarking.
- Automatically aggregates inferred dated transaction cash flows and appends inferred terminal portfolio value.
- Added benchmark simulation using the same dated flows against:
  - NIFTY 50 (`^NSEI`)
  - NIFTY 500 (`^CRSLDX`)
  - Gold BeES (`GOLDBEES.NS`)
- Added terminal-value comparison and benchmark price-mapping audit.
- Retained the manual XIRR calculator as a fallback.
- No claim is made that generic transaction parsing is fully validated; statement-specific hardening remains required.


### v1.0.6 — Transaction parser false-positive fix

This version was created after reviewing an exported `cas_inferred_transactions.csv`
where the parser incorrectly treated an **Exit Load** disclosure sentence as a SELL transaction.

Fixes:
- Blocks scheme narrative/disclosure text such as `Exit Load`, `W.E.F`, `if redeemed`,
  `date of allotment`, `subscription received after`, TER, cut-off time, etc.
- Requires transaction dates to occur at/near the start of a row.
- Supports numeric and month-name date forms while removing all date tokens before
  inferring quantity/amount.
- Uses absolute inferred amounts and applies the cash-flow sign separately.
- Deduplicates repeated PDF text-layer transaction rows.
- Raises transaction confidence for rows that pass the stricter pattern.
- Adds a transaction quality gate showing narrative hits, duplicates and low-confidence rows.
- **Blocks automated XIRR** when the transaction quality gate fails.
- Manual XIRR remains available as fallback.


### v1.0.6 — NSDL holdings-layout parser hardening

This version was built from the actual extracted August-2026 NSDL CAS layout supplied for testing.

Key changes:

- Replaced the old "last numbers on the ISIN line" holdings heuristic.
- Added NSDL vertical-table parsing for:
  - Equity: `ISIN → Stock Symbol → Company Name → Face Value → Shares → Market Price → Value`
  - Demat Mutual Fund: `ISIN → Description → Units → NAV → Value`
  - Sovereign Gold Bond: issuer/series + maturity + units + face value + market price + value
  - Generic MF-folio triplet parsing where quantity × NAV reconciles to reported value.
- Every accepted equity/MF/SGB row must reconcile `quantity × price/NAV ≈ market value`.
- Added real company/scheme names, symbols, asset type, market price, account and row confidence.
- Added a holdings quality gate comparing parsed market value with NSDL's stated Consolidated Portfolio Value.
- Fixed statement dates so an SGB maturity date cannot accidentally become the CAS `period_end`.
- Fixed terminal value logic: only the **latest CAS** is used. Monthly CAS values are no longer added together.
- When available, terminal value uses NSDL's exact `YOUR CONSOLIDATED PORTFOLIO VALUE`.
- Automated XIRR remains unavailable when no genuine dated transaction rows are reconstructed.
