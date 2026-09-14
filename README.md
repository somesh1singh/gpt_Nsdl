# NSDL CAS Portfolio Intelligence & Advisory System

**Repository package version:** 1.1.1
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

## What v1.1.1 implements

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

### v1.1.1 — transaction text-layer hardening

- assembles dated transaction rows when PDF table cells are split across physical text lines
- handles split `ISIN :` labels and bare ISIN transaction headers inside validated transaction sections
- recovers undated MF-folio opening/closing balances used for history-completeness diagnostics
- keeps depository quantity-only movements separate from MF-folio monetary cash flows
- preserves the rule that missing trade consideration is never fabricated


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

No additional source files are required for v1.1.0.

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

The parser in v1.1.0 is a **defensive structured/heuristic parser**, not yet a guaranteed parser for every historical NSDL CAS layout. It extracts text from PDFs, identifies ISIN-linked lines, attempts to infer quantities and market values, and exposes confidence/warnings instead of silently fabricating data.

For industry-grade use, parser hardening should be performed against several real CAS files spanning different statement formats and years.

### Tax calculations

The tax-lot module provides an analytical estimate and supports configurable STCG/LTCG rates in the UI. It should not be treated as a tax filing engine. Indian tax rules change by asset type and financial year, and grandfathering/indexation/special cases need dedicated validation.

### Forward-looking estimates

The Advice Impact and Scenario modules are simulations. Expected XIRR is not a promise of future performance. The app shows confidence and allows a "do nothing" baseline.

### Privacy

Uploaded files are processed in the running Streamlit session. This three-file Community Cloud build does not implement encrypted persistent object storage, user authentication, database isolation, or field-level PAN encryption. Those remain production-hardening items from the blueprint.

Do not deploy sensitive personal CAS statements to a public/shared app unless you understand the hosting and access implications.

## Version history

### v1.0.7 — 2026-09-14

- converted the earlier multi-file backend implementation into a single Streamlit app
- changed deployment target to Python 3.14
- preserved only `README.MD`, `requirements.txt`, and `app.py`
- added in-app version display
- added dependency/version diagnostics
- added institutional intelligence upload/fetch workflow
- added portfolio analytics, tax lots, attribution, benchmarking, scenario and advice modules
- added graceful handling for incomplete source data
- added CSV download exports for analysis tables


### v1.0.8 — CDSL reconciliation hardening

- recovers holdings when an ISIN and security description share one PDF-extracted line
- stops record scanning when the next line starts with another ISIN
- avoids nearby Total/Sub Total labels replacing the account name
- retains arithmetic validation and the strict transaction quality gate

### v1.0.9 — CDSL numeric-tail and page-break hardening

- replaces the brittle first-number/last-two-number CDSL assumption with arithmetic candidate scanning
- prefers the canonical 11 numeric CDSL balance fields and validates `quantity × price ≈ value`
- ignores security-description face-value numbers when locating the true holding quantity
- ignores trailing PDF page-number contamination after a CDSL holding row
- carries the detected balance-start index into equity and demat-MF name extraction

### v1.0.10 — historical SGB reconciliation cleanup

- adds a conservative historical SGB layout fallback for statements that contain units, face value and reported value but no separate market-price field
- requires `units × face value ≈ reported value` within the existing 2% arithmetic quality gate
- does not invent a historical market price; face value is used only when that is the valuation basis printed by the statement
- preserves the modern SGB parser as the preferred path whenever a genuine market price is present
- targets the repeated ₹9,464.01 residual observed in the Mar-2024 and Mar-2025 validation exports


### v1.1.0 — structured CAS transaction-ledger reconstruction

- parses real MF-folio transaction rows using the printed monetary columns: amount, stamp duty, NAV, price and units
- supports wrapped SIP/systematic-investment rows and validates `price × units ≈ amount` before accepting a monetary transaction
- parses CDSL/NSDL depository credit/debit rows as quantity/balance movements and reconciles them against opening/current balances where available
- explicitly marks depository rows as `UNAVAILABLE_IN_CAS` for monetary cashflow because CAS does not print trade consideration in those rows
- ignores opening/closing balances as transactions and retains them only for ledger reconciliation
- adds cashflow-coverage diagnostics and separates ledger validity from XIRR readiness
- blocks full-portfolio automated XIRR when CAS monetary consideration is incomplete instead of mixing partial MF cashflows with whole-portfolio terminal value
- keeps manual XIRR available and identifies broker tradebook/ledger as the required source for missing depository consideration
- retains the v1.0.10 historical-SGB fallback and all v1.0.9 CDSL holdings fixes


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


### v1.0.7 — Streamlit filename hotfix

- Renamed `APP.PY` → `app.py` because Streamlit requires a lowercase `.py` extension.
- Renamed `REQUIREMENTS.TXT` → `requirements.txt` so Streamlit Community Cloud reliably detects dependencies.
- Renamed `README.MD` → `README.md` for standard GitHub naming.
- No analytical logic removed.


### v1.0.7 — Password-protected CAS PDF support

- Added support for encrypted/password-protected PDF statements.
- After a PDF is uploaded, the CAS Parser page asks for a password for each uploaded PDF.
- The password field uses masked input.
- Passwords are used only to authenticate the PDF during parsing and are **not** stored in session state, exports, logs, or analysis tables.
- If a PDF is not encrypted, the password field may be left blank.
- If a password is missing or incorrect, the app now shows a specific message instead of a generic parsing failure.


### v1.0.7 — Institutional path + India-date fixes

- Corrected the upstream universe path to `shareholding_history/data/_universe.csv`.
- Added the separate upstream `symbol_renames.json` mapping.
- Added symbol-rename display in Institutional Intelligence.
- Replaced server-UTC `date.today()` defaults with India calendar date (`Asia/Kolkata`) so cloud deployment does not show the previous date during early-morning IST usage.
- Preserved Python 3.14 target and password-protected PDF support.


### v1.0.7 — Automated CAS XIRR & benchmark reconstruction

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


### v1.0.7 — Transaction parser false-positive fix

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


### v1.0.7 — NSDL holdings-layout parser hardening

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


### v1.0.7 — Full holdings/reconciliation correction

Built from the v1.0.6 exports plus the observed NSDL/CDSL and Mutual Fund Folio layouts.

Key corrections:

- Indian-number parser now accepts values such as `1,10,967.50`, `1,36,980.00`,
  `17,88,268.86` and `36,16,119.95`.
- Added CDSL balance-table parser for Zerodha holdings:
  `Current Balance ... Market Price ... Value`.
- Added Mutual Fund Folio parser that distinguishes:
  `Units → Average Cost → Total Cost → Current NAV → Current Value → Unrealised P/L`.
  The parser now uses **Current NAV and Current Value**, not historical cost.
- `Mutual Fund Folios (F)` is treated as its own asset class and account.
- Added robust account-name detection for both NSDL and CDSL table orderings.
- Added NSDL portfolio-composition asset-class reconciliation.
- Fixed the cross-CAS reconciliation Cartesian-product bug that previously expanded
  a small holding set into hundreds of thousands of meaningless rows.
- Cross-CAS output is now compact and ISIN-based.
- Existing password-PDF support, India-date handling, transaction quality gate,
  institutional intelligence and latest-CAS terminal-value rules are retained.

Acceptance target for this revision:
- each uploaded CAS should reconcile its parsed Equity / Demat MF / SGB / MF Folio
  market values to the NSDL statement totals within a small tolerance;
- cross-CAS reconciliation should contain one meaningful row per security rather
  than a Cartesian product.
