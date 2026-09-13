# NSDL CAS Portfolio Intelligence & Advisory System

**Repository package version:** 1.0.1  
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

## What v1.0.1 implements

The app follows the supplied NSDL CAS Portfolio Intelligence & Advisory System blueprint and includes:

- NSDL CAS PDF upload and heuristic text extraction
- document fingerprinting and parse confidence
- ISIN-based holding extraction
- multi-CAS reconciliation workspace
- security/symbol canonicalisation workspace
- XIRR engine using dated cash flows
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
_universe.csv
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

No additional source files are required for v1.0.1.

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

The parser in v1.0.1 is a **defensive heuristic parser**, not yet a guaranteed parser for every historical NSDL CAS layout. It extracts text from PDFs, identifies ISIN-linked lines, attempts to infer quantities and market values, and exposes confidence/warnings instead of silently fabricating data.

For industry-grade use, parser hardening should be performed against several real CAS files spanning different statement formats and years.

### Tax calculations

The tax-lot module provides an analytical estimate and supports configurable STCG/LTCG rates in the UI. It should not be treated as a tax filing engine. Indian tax rules change by asset type and financial year, and grandfathering/indexation/special cases need dedicated validation.

### Forward-looking estimates

The Advice Impact and Scenario modules are simulations. Expected XIRR is not a promise of future performance. The app shows confidence and allows a "do nothing" baseline.

### Privacy

Uploaded files are processed in the running Streamlit session. This three-file Community Cloud build does not implement encrypted persistent object storage, user authentication, database isolation, or field-level PAN encryption. Those remain production-hardening items from the blueprint.

Do not deploy sensitive personal CAS statements to a public/shared app unless you understand the hosting and access implications.

## Version history

### v1.0.1 — 2026-09-14

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


### v1.0.1 — Streamlit filename hotfix

- Renamed `APP.PY` → `app.py` because Streamlit requires a lowercase `.py` extension.
- Renamed `REQUIREMENTS.TXT` → `requirements.txt` so Streamlit Community Cloud reliably detects dependencies.
- Renamed `README.MD` → `README.md` for standard GitHub naming.
- No analytical logic removed.
