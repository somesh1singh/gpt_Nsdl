# =============================================================================
# NSDL CAS Portfolio Intelligence & Advisory System
# APP VERSION: 1.0.2
# BLUEPRINT BASELINE: 1.0
# TARGET PYTHON: 3.14
# BUILD DATE: 2026-09-14
# Entrypoint: app.py
# =============================================================================

from __future__ import annotations

import hashlib
import io
import json
import math
import platform
import re
import sys
from dataclasses import dataclass, asdict, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any, Literal

import fitz  # PyMuPDF
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

APP_VERSION = "1.0.2"
BLUEPRINT_VERSION = "1.0"
TARGET_PYTHON = "3.14"
BUILD_DATE = "2026-09-14"
UPSTREAM_REPO = "https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/main"
getcontext().prec = 40

# -----------------------------------------------------------------------------
# Page configuration
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title=f"NSDL CAS Intelligence v{APP_VERSION}",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def D(value: Any, default: str = "0") -> Decimal:
    """Safe Decimal conversion."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def fmt_inr(value: Any) -> str:
    """Indian-style compact currency formatting without decimals."""
    v = float(D(value))
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 10_000_000:
        return f"{sign}₹{v/10_000_000:.2f} Cr"
    if v >= 100_000:
        return f"{sign}₹{v/100_000:.2f} L"
    return f"{sign}₹{v:,.0f}"


def df_download(label: str, df: pd.DataFrame, filename: str, key: str) -> None:
    if df is None or df.empty:
        return
    st.download_button(
        label,
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
        key=key,
    )


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")
        for c in out.columns
    ]
    return out


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    # fuzzy-ish fallback
    for c in candidates:
        for col in df.columns:
            if c in col:
                return col
    return None


def parse_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", dayfirst=True).dt.date


def runtime_check() -> dict[str, str]:
    return {
        "App": APP_VERSION,
        "Blueprint": BLUEPRINT_VERSION,
        "Python target": TARGET_PYTHON,
        "Python running": platform.python_version(),
        "Streamlit": st.__version__,
        "Pandas": pd.__version__,
        "NumPy": np.__version__,
        "Plotly": getattr(sys.modules.get("plotly"), "__version__", "installed"),
        "PyMuPDF": getattr(fitz, "VersionBind", "installed"),
    }


# -----------------------------------------------------------------------------
# XIRR engine
# -----------------------------------------------------------------------------

def _years(d: date, d0: date) -> Decimal:
    return Decimal((d - d0).days) / Decimal("365.2425")


def xnpv(rate: Decimal, cashflows: list[tuple[date, Decimal]]) -> Decimal:
    if rate <= Decimal("-1"):
        raise ValueError("Rate must be greater than -1.")
    if not cashflows:
        return Decimal("0")
    d0 = min(d for d, _ in cashflows)
    total = Decimal("0")
    for d, cf in cashflows:
        total += cf / ((Decimal("1") + rate) ** _years(d, d0))
    return total


def xirr(
    cashflows: list[tuple[date, Decimal]],
    guess: Decimal = Decimal("0.10"),
    tol: Decimal = Decimal("1e-12"),
    max_iter: int = 100,
) -> Decimal:
    if len(cashflows) < 2:
        raise ValueError("At least two cash flows are required.")
    vals = [cf for _, cf in cashflows]
    if not (any(v < 0 for v in vals) and any(v > 0 for v in vals)):
        raise ValueError("XIRR needs at least one negative and one positive cash flow.")

    d0 = min(d for d, _ in cashflows)
    r = guess

    for _ in range(max_iter):
        f = Decimal("0")
        df = Decimal("0")
        for d, cf in cashflows:
            t = _years(d, d0)
            base = Decimal("1") + r
            f += cf / (base ** t)
            if t != 0:
                df += -t * cf / (base ** (t + Decimal("1")))
        if abs(f) < tol:
            return r
        if df == 0:
            break
        nr = r - f / df
        if nr <= Decimal("-0.999999"):
            nr = (r - Decimal("0.999999")) / Decimal("2")
        if abs(nr - r) < tol:
            return nr
        r = nr

    lo, hi = Decimal("-0.9999"), Decimal("10")
    flo, fhi = xnpv(lo, cashflows), xnpv(hi, cashflows)
    for _ in range(200):
        if flo * fhi <= 0:
            break
        hi *= Decimal("2")
        fhi = xnpv(hi, cashflows)
    if flo * fhi > 0:
        raise ValueError("Could not bracket an XIRR root.")

    for _ in range(250):
        mid = (lo + hi) / Decimal("2")
        fm = xnpv(mid, cashflows)
        if abs(fm) < tol:
            return mid
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / Decimal("2")


# -----------------------------------------------------------------------------
# Tax lot engine
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Lot:
    lot_id: str
    quantity: Decimal
    remaining_quantity: Decimal
    acquire_date: date
    acquire_price: Decimal
    lot_type: str = "BUY"


@dataclass(frozen=True)
class SaleMatch:
    lot_id: str
    quantity: Decimal
    cost_basis: Decimal
    proceeds: Decimal
    gain: Decimal
    holding_days: int
    tax_class: str


@dataclass(frozen=True)
class SaleSimulation:
    matches: list[SaleMatch]
    total_gain: Decimal
    estimated_tax: Decimal
    remaining_lots: list[Lot]


def holding_class(acquire_date: date, sale_date: date, equity: bool = True) -> str:
    days = (sale_date - acquire_date).days
    if equity:
        return "LTCG" if days > 365 else "STCG"
    return "LTCG" if days > 730 else "STCG"


def simulate_sale(
    lots: list[Lot],
    quantity: Decimal,
    sale_price: Decimal,
    sale_date: date,
    method: Literal["FIFO", "LIFO"] = "FIFO",
    stcg_rate: Decimal = Decimal("0.20"),
    ltcg_rate: Decimal = Decimal("0.125"),
    equity: bool = True,
) -> SaleSimulation:
    available = sum((x.remaining_quantity for x in lots), Decimal("0"))
    if quantity <= 0 or quantity > available:
        raise ValueError("Sale quantity must be positive and not exceed available quantity.")

    ordered = sorted(
        lots,
        key=lambda x: (x.acquire_date, x.lot_id),
        reverse=(method == "LIFO"),
    )
    remaining_to_sell = quantity
    updated = {lot.lot_id: lot for lot in lots}
    matches: list[SaleMatch] = []
    tax = Decimal("0")

    for lot in ordered:
        if remaining_to_sell <= 0:
            break
        take = min(lot.remaining_quantity, remaining_to_sell)
        if take <= 0:
            continue
        cost = take * lot.acquire_price
        proceeds = take * sale_price
        gain = proceeds - cost
        cls = holding_class(lot.acquire_date, sale_date, equity=equity)
        rate = ltcg_rate if cls == "LTCG" else stcg_rate
        if gain > 0:
            tax += gain * rate

        matches.append(
            SaleMatch(
                lot_id=lot.lot_id,
                quantity=take,
                cost_basis=cost,
                proceeds=proceeds,
                gain=gain,
                holding_days=(sale_date - lot.acquire_date).days,
                tax_class=cls,
            )
        )
        updated[lot.lot_id] = replace(
            lot,
            remaining_quantity=lot.remaining_quantity - take,
        )
        remaining_to_sell -= take

    return SaleSimulation(
        matches=matches,
        total_gain=sum((m.gain for m in matches), Decimal("0")),
        estimated_tax=tax,
        remaining_lots=list(updated.values()),
    )


def apply_bonus_or_split(
    lots: list[Lot],
    new_units: Decimal,
    old_units: Decimal,
) -> list[Lot]:
    if new_units <= 0 or old_units <= 0:
        raise ValueError("Ratio components must be positive.")
    factor = new_units / old_units
    out = []
    for lot in lots:
        new_qty = lot.quantity * factor
        new_rem = lot.remaining_quantity * factor
        new_price = (lot.quantity * lot.acquire_price) / new_qty
        out.append(
            replace(
                lot,
                quantity=new_qty,
                remaining_quantity=new_rem,
                acquire_price=new_price,
            )
        )
    return out


def harvestable_losses(
    lots: list[Lot],
    market_price: Decimal,
    as_of: date,
    min_loss: Decimal = Decimal("0"),
) -> pd.DataFrame:
    rows = []
    for lot in lots:
        unrealised = lot.remaining_quantity * (market_price - lot.acquire_price)
        if unrealised < -abs(min_loss):
            rows.append(
                {
                    "lot_id": lot.lot_id,
                    "quantity": float(lot.remaining_quantity),
                    "acquire_date": lot.acquire_date,
                    "acquire_price": float(lot.acquire_price),
                    "market_price": float(market_price),
                    "unrealised_loss": float(unrealised),
                    "holding_days": (as_of - lot.acquire_date).days,
                    "tax_class_if_sold": holding_class(lot.acquire_date, as_of),
                }
            )
    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# Attribution / benchmarking / goal / scenario
# -----------------------------------------------------------------------------

def brinson_fachler(df: pd.DataFrame) -> dict[str, Decimal]:
    if df.empty:
        return {
            "allocation": Decimal("0"),
            "selection": Decimal("0"),
            "interaction": Decimal("0"),
        }
    rows = df.copy()
    for c in ["portfolio_weight", "benchmark_weight", "portfolio_return", "benchmark_return"]:
        rows[c] = pd.to_numeric(rows[c], errors="coerce").fillna(0.0)

    bmk_total = sum(
        D(w) * D(r)
        for w, r in zip(rows["benchmark_weight"], rows["benchmark_return"])
    )
    allocation = Decimal("0")
    selection = Decimal("0")
    interaction = Decimal("0")

    for _, r in rows.iterrows():
        pw, bw = D(r["portfolio_weight"]), D(r["benchmark_weight"])
        pr, br = D(r["portfolio_return"]), D(r["benchmark_return"])
        allocation += (pw - bw) * (br - bmk_total)
        selection += bw * (pr - br)
        interaction += (pw - bw) * (pr - br)

    return {
        "allocation": allocation,
        "selection": selection,
        "interaction": interaction,
    }


def required_cagr(current_value: Decimal, target_value: Decimal, years: Decimal) -> Decimal:
    if current_value <= 0 or target_value <= 0 or years <= 0:
        raise ValueError("Current value, target value and years must all be positive.")
    return (target_value / current_value) ** (Decimal("1") / years) - Decimal("1")


def future_value(
    current_value: Decimal,
    annual_return_pct: Decimal,
    years: int,
    annual_contribution: Decimal = Decimal("0"),
) -> Decimal:
    r = annual_return_pct / Decimal("100")
    fv = current_value
    for _ in range(years):
        fv = fv * (Decimal("1") + r) + annual_contribution
    return fv


# -----------------------------------------------------------------------------
# Data quality / reconciliation
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    confidence_penalty: Decimal


def quantity_continuity_issue(
    previous_quantity: Decimal,
    buys: Decimal,
    sells: Decimal,
    corporate_action_delta: Decimal,
    current_quantity: Decimal,
    tolerance: Decimal = Decimal("0.0001"),
) -> QualityIssue | None:
    expected = previous_quantity + buys - sells + corporate_action_delta
    if abs(expected - current_quantity) > tolerance:
        return QualityIssue(
            code="QUANTITY_CONTINUITY_BREAK",
            severity="HIGH",
            message=f"Expected {expected} but observed {current_quantity}.",
            confidence_penalty=Decimal("0.25"),
        )
    return None


def combine_confidence(base: Decimal, issues: list[QualityIssue]) -> Decimal:
    score = base - sum((i.confidence_penalty for i in issues), Decimal("0"))
    return max(Decimal("0"), min(Decimal("1"), score))


# -----------------------------------------------------------------------------
# CAS PDF parsing
# -----------------------------------------------------------------------------

ISIN_RE = re.compile(r"\bIN[A-Z0-9]{10}\b")
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
DATE_RE = re.compile(r"\b(\d{2}[-/]\d{2}[-/]\d{4})\b")


def extract_pdf_text(pdf_bytes: bytes, password: str = "") -> tuple[str, int, bool]:
    """
    Open a PDF, authenticate it when encrypted, and extract text.

    Passwords are used transiently only for PDF authentication and are not
    persisted in session state, logs, exports, or returned values.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    encrypted = bool(doc.needs_pass)

    if encrypted:
        if not password:
            doc.close()
            raise ValueError(
                "This PDF is password protected. Enter the PDF password and try again."
            )

        auth_result = doc.authenticate(password)
        if not auth_result:
            doc.close()
            raise ValueError(
                "The PDF password is incorrect. Please check the password and try again."
            )

    pages = []
    try:
        for page in doc:
            pages.append(page.get_text("text"))
        page_count = len(doc)
    finally:
        doc.close()

    return "\n".join(pages), page_count, encrypted


def parse_cas_pdf(
    pdf_bytes: bytes,
    filename: str,
    password: str = "",
) -> dict[str, Any]:
    text, page_count, encrypted = extract_pdf_text(pdf_bytes, password=password)
    fingerprint = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    holdings = []
    warnings = []
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in raw_lines:
        m = ISIN_RE.search(line)
        if not m:
            continue
        isin = m.group(0)
        suffix = line[m.end():]
        nums = NUMBER_RE.findall(suffix)
        if not nums:
            continue
        try:
            values = [D(x) for x in nums]
            qty = values[-2] if len(values) >= 2 else values[-1]
            market_value = values[-1] if len(values) >= 2 else None
        except Exception:
            continue

        name = line[:m.start()].strip(" -|:") or isin
        holdings.append(
            {
                "file": filename,
                "isin": isin,
                "name": name[:250],
                "quantity_inferred": float(qty),
                "market_value_inferred": None if market_value is None else float(market_value),
                "line_confidence": 0.60,
                "source_line": line[:700],
            }
        )

    all_dates = []
    for raw in DATE_RE.findall(text):
        for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
            try:
                all_dates.append(datetime.strptime(raw, fmt).date())
                break
            except ValueError:
                pass

    confidence = 0.65 if holdings else 0.20
    if not holdings:
        warnings.append(
            "No ISIN-linked holdings were inferred. This CAS layout may need a dedicated parser."
        )
    if "NSDL" not in text.upper():
        warnings.append("The document text did not contain 'NSDL'; verify statement source.")

    return {
        "filename": filename,
        "pages": page_count,
        "encrypted_pdf": encrypted,
        "fingerprint": fingerprint,
        "period_start": min(all_dates) if all_dates else None,
        "period_end": max(all_dates) if all_dates else None,
        "parse_confidence": confidence,
        "warnings": warnings,
        "holdings": pd.DataFrame(holdings),
        "text_preview": text[:10000],
    }


# -----------------------------------------------------------------------------
# Institutional data loader / PIT logic
# -----------------------------------------------------------------------------

UPSTREAM_PATHS = {
    "Shareholding history": "shareholding_history/data/parsed/_flat.csv",
    "Shareholding signals": "shareholding_history/data/parsed/_signals.csv",
    "Index membership": "index_history/data/index_membership_history.csv",
    "F&O membership": "fno_history/data/fno_membership_history.csv",
    "Universe / renames": "_universe.csv",
}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_upstream_csv(relative_path: str) -> pd.DataFrame:
    url = f"{UPSTREAM_REPO}/{relative_path}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return pd.read_csv(io.BytesIO(response.content))


def read_uploaded_csv(uploaded_file) -> pd.DataFrame:
    if uploaded_file is None:
        return pd.DataFrame()
    return pd.read_csv(uploaded_file)


def prepare_shareholding(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str | None]]:
    if df.empty:
        return df, {}
    out = normalize_columns(df)
    mapping = {
        "symbol": find_col(out, ["symbol", "ticker", "nse_symbol"]),
        "period": find_col(out, ["period", "date", "quarter", "as_of"]),
        "promoter": find_col(out, ["promoter_pct", "promoter", "promoter_holding"]),
        "fii": find_col(out, ["fii_pct", "fii", "foreign_institutional"]),
        "dii": find_col(out, ["dii_pct", "dii", "domestic_institutional"]),
        "public": find_col(out, ["public_pct", "public"]),
        "pledge": find_col(out, ["pledge_pct", "pledge", "promoter_pledge"]),
    }
    if mapping["period"]:
        out["_period"] = parse_date_series(out[mapping["period"]])
    if mapping["symbol"]:
        out["_symbol"] = out[mapping["symbol"]].astype(str).str.upper().str.strip()
    return out, mapping


def pit_shareholding(
    df: pd.DataFrame,
    symbol: str,
    as_of: date,
) -> tuple[pd.Series | None, dict[str, Any]]:
    prepared, m = prepare_shareholding(df)
    if prepared.empty or not m.get("symbol") or not m.get("period"):
        return None, m

    s = symbol.upper().strip()
    subset = prepared[(prepared["_symbol"] == s) & prepared["_period"].notna()].copy()
    subset = subset[subset["_period"] <= as_of].sort_values("_period")
    if subset.empty:
        return None, m
    return subset.iloc[-1], m


def four_quarter_smart_money(
    df: pd.DataFrame,
    symbol: str,
    as_of: date,
) -> dict[str, Any]:
    prepared, m = prepare_shareholding(df)
    if prepared.empty or not m.get("symbol") or not m.get("period"):
        return {"available": False, "reason": "Required symbol/period columns not detected."}

    subset = prepared[
        (prepared["_symbol"] == symbol.upper().strip())
        & prepared["_period"].notna()
        & (prepared["_period"] <= as_of)
    ].sort_values("_period")

    if len(subset) < 5 or not m.get("fii") or not m.get("dii"):
        return {"available": False, "reason": "Need at least five quarterly observations plus FII/DII columns."}

    cur = subset.iloc[-1]
    old = subset.iloc[-5]
    fii_delta = D(cur[m["fii"]]) - D(old[m["fii"]])
    dii_delta = D(cur[m["dii"]]) - D(old[m["dii"]])
    return {
        "available": True,
        "latest_period": cur["_period"],
        "old_period": old["_period"],
        "fii_delta_4q": float(fii_delta),
        "dii_delta_4q": float(dii_delta),
        "smart_money_score": float(fii_delta + dii_delta),
    }


def membership_asof(
    df: pd.DataFrame,
    symbol: str,
    as_of: date,
    kind: str,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = normalize_columns(df)
    symbol_col = find_col(out, ["symbol", "ticker", "nse_symbol"])
    if not symbol_col:
        return pd.DataFrame()

    out["_symbol"] = out[symbol_col].astype(str).str.upper().str.strip()
    out = out[out["_symbol"] == symbol.upper().strip()].copy()

    start_col = find_col(out, ["valid_from", "from_date", "start_date", "entry_date", "date"])
    end_col = find_col(out, ["valid_to", "to_date", "end_date", "exit_date"])

    if start_col:
        starts = pd.to_datetime(out[start_col], errors="coerce", dayfirst=True).dt.date
        out = out[(starts.isna()) | (starts <= as_of)]
    if end_col:
        ends = pd.to_datetime(out[end_col], errors="coerce", dayfirst=True).dt.date
        out = out[(ends.isna()) | (ends >= as_of)]

    out.insert(0, "lookup_type", kind)
    return out


# -----------------------------------------------------------------------------
# Advice impact engine
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class AdviceImpact:
    action: str
    expected_delta_xirr_1y: Decimal
    expected_delta_xirr_3y: Decimal
    expected_delta_xirr_5y: Decimal
    tax_cost_today: Decimal
    exit_load_cost: Decimal
    net_expected_benefit_3y: Decimal
    risk_change: str
    confidence: Decimal
    evidence: list[str]
    show_recommendation: bool


def estimate_advice_impact(
    *,
    action: str,
    gross_delta_xirr_1y: Decimal,
    gross_delta_xirr_3y: Decimal,
    gross_delta_xirr_5y: Decimal,
    tax_cost: Decimal,
    exit_load_cost: Decimal,
    portfolio_value: Decimal,
    confidence: Decimal,
    risk_change: str,
    evidence: list[str],
) -> AdviceImpact:
    if portfolio_value <= 0:
        raise ValueError("Portfolio value must be positive.")
    friction = tax_cost + exit_load_cost
    friction_drag_3y = (friction / portfolio_value) / Decimal("3") * Decimal("100")
    net_3y = gross_delta_xirr_3y - friction_drag_3y
    return AdviceImpact(
        action=action,
        expected_delta_xirr_1y=gross_delta_xirr_1y,
        expected_delta_xirr_3y=gross_delta_xirr_3y,
        expected_delta_xirr_5y=gross_delta_xirr_5y,
        tax_cost_today=tax_cost,
        exit_load_cost=exit_load_cost,
        net_expected_benefit_3y=net_3y,
        risk_change=risk_change,
        confidence=max(Decimal("0"), min(Decimal("1"), confidence)),
        evidence=evidence,
        show_recommendation=(net_3y > 0),
    )


# -----------------------------------------------------------------------------
# Session state
# -----------------------------------------------------------------------------

for key, default in {
    "cas_results": [],
    "shareholding_df": pd.DataFrame(),
    "index_df": pd.DataFrame(),
    "fno_df": pd.DataFrame(),
    "signals_df": pd.DataFrame(),
    "universe_df": pd.DataFrame(),
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# -----------------------------------------------------------------------------
# Sidebar
# -----------------------------------------------------------------------------

st.sidebar.title("NSDL CAS Intelligence")
st.sidebar.caption(f"App v{APP_VERSION} · Blueprint v{BLUEPRINT_VERSION}")
section = st.sidebar.radio(
    "Module",
    [
        "Home",
        "CAS Parser & Reconciliation",
        "XIRR & Benchmarking",
        "Tax Lots",
        "Return Attribution",
        "Institutional Intelligence",
        "Advice Impact",
        "Goals & Stress Test",
        "Data Quality",
        "System Status",
    ],
)

with st.sidebar.expander("Runtime"):
    for k, v in runtime_check().items():
        st.write(f"**{k}:** {v}")

if not platform.python_version().startswith(TARGET_PYTHON):
    st.sidebar.warning(
        f"This session is running Python {platform.python_version()}, "
        f"while the deployment target is Python {TARGET_PYTHON}."
    )


# -----------------------------------------------------------------------------
# Home
# -----------------------------------------------------------------------------

if section == "Home":
    st.title("NSDL CAS Portfolio Intelligence & Advisory System")
    st.caption(
        f"Streamlit edition v{APP_VERSION} · Python {TARGET_PYTHON} target · Build {BUILD_DATE}"
    )

    st.info(
        "This build consolidates the supplied technical blueprint into a single Streamlit "
        "application suitable for a three-file GitHub repository."
    )

    cols = st.columns(4)
    cols[0].metric("CAS files parsed", len(st.session_state.cas_results))
    combined_holdings = sum(
        len(x["holdings"]) for x in st.session_state.cas_results
        if isinstance(x.get("holdings"), pd.DataFrame)
    )
    cols[1].metric("Inferred holding rows", combined_holdings)
    cols[2].metric(
        "Institutional data",
        "Loaded" if not st.session_state.shareholding_df.empty else "Not loaded",
    )
    cols[3].metric("App version", APP_VERSION)

    st.subheader("Blueprint coverage")
    coverage = pd.DataFrame(
        [
            ["CAS ingestion / parsing", "Implemented starter", "Heuristic; requires multi-format hardening"],
            ["Multi-CAS reconstruction", "Partial", "Cross-file holdings comparison available"],
            ["Securities master / renames", "Partial", "Upload/fetch universe data; no persistent DB"],
            ["XIRR", "Implemented", "High-precision dated cash-flow engine"],
            ["Tax lots", "Implemented starter", "FIFO/LIFO; configurable rates"],
            ["Return attribution", "Implemented starter", "Waterfall + Brinson-Fachler"],
            ["Institutional overlay", "Implemented", "PIT lookup + 4Q smart-money score"],
            ["Index/F&O history", "Implemented", "As-of lookup when data loaded"],
            ["Advice impact estimator", "Implemented starter", "Tax/cost guardrail + confidence"],
            ["Goal linkage", "Implemented starter", "Required CAGR vs trajectory"],
            ["Scenario / stress test", "Implemented starter", "Base/bull/bear projections"],
            ["Continuous monitoring", "Not persistent", "Requires scheduler/database"],
            ["Security / encrypted storage", "Not in 3-file build", "Production hardening item"],
        ],
        columns=["Area", "Status", "Notes"],
    )
    st.dataframe(coverage, use_container_width=True, hide_index=True)

    st.warning(
        "This is an analytical implementation foundation, not regulated investment advice. "
        "Forward-looking outputs are simulations and depend on data quality."
    )


# -----------------------------------------------------------------------------
# CAS Parser & Reconciliation
# -----------------------------------------------------------------------------

elif section == "CAS Parser & Reconciliation":
    st.title("CAS Parser & Reconciliation")
    st.caption("Upload one or more NSDL CAS PDFs. Files remain in the active Streamlit session.")

    uploads = st.file_uploader(
        "Upload NSDL CAS PDF files",
        type=["pdf"],
        accept_multiple_files=True,
    )

    # Passwords deliberately live only in the widget return values for this run.
    # They are not copied into st.session_state.cas_results.
    pdf_passwords: dict[str, str] = {}
    if uploads:
        st.markdown("#### PDF passwords")
        st.caption(
            "Enter the password only for files that are encrypted. "
            "Leave blank for normal PDFs. Passwords are masked and are not saved in analysis results."
        )
        for idx, f in enumerate(uploads):
            pdf_passwords[f.name] = st.text_input(
                f"Password for {f.name}",
                value="",
                type="password",
                key=f"pdf_password_{idx}_{f.name}",
                help="Used only to unlock this PDF during parsing.",
            )

    if st.button("Parse uploaded CAS", type="primary", disabled=not uploads):
        results = []
        for f in uploads or []:
            try:
                result = parse_cas_pdf(
                    f.getvalue(),
                    f.name,
                    password=pdf_passwords.get(f.name, ""),
                )
                results.append(result)
                if result.get("encrypted_pdf"):
                    st.success(f"{f.name}: encrypted PDF opened successfully.")
            except Exception as exc:
                st.error(f"{f.name}: {exc}")
        st.session_state.cas_results = results

    if st.session_state.cas_results:
        summary_rows = []
        holdings_frames = []
        for r in st.session_state.cas_results:
            summary_rows.append(
                {
                    "file": r["filename"],
                    "pages": r["pages"],
                    "password_protected": bool(r.get("encrypted_pdf", False)),
                    "period_start": r["period_start"],
                    "period_end": r["period_end"],
                    "parse_confidence": r["parse_confidence"],
                    "holdings_rows": len(r["holdings"]),
                    "fingerprint": r["fingerprint"][:16],
                    "warnings": " | ".join(r["warnings"]),
                }
            )
            if not r["holdings"].empty:
                holdings_frames.append(r["holdings"])

        summary_df = pd.DataFrame(summary_rows)
        st.subheader("Statement summary")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        df_download("Download statement summary", summary_df, "cas_statement_summary.csv", "cas_sum")

        if holdings_frames:
            all_holdings = pd.concat(holdings_frames, ignore_index=True)
            st.subheader("Inferred holdings")
            st.warning(
                "Quantity/value columns are inferred heuristically from the source line. "
                "Review before using these values for tax or performance calculations."
            )
            st.dataframe(all_holdings, use_container_width=True, hide_index=True)
            df_download("Download inferred holdings", all_holdings, "cas_inferred_holdings.csv", "cas_hold")

            if len(st.session_state.cas_results) >= 2:
                st.subheader("Cross-CAS quantity comparison")
                pivot = (
                    all_holdings.pivot_table(
                        index=["isin", "name"],
                        columns="file",
                        values="quantity_inferred",
                        aggfunc="sum",
                    )
                    .reset_index()
                )
                st.dataframe(pivot, use_container_width=True, hide_index=True)
                df_download("Download reconciliation view", pivot, "cas_reconciliation.csv", "cas_rec")

        with st.expander("Extracted text preview"):
            chosen = st.selectbox(
                "Statement",
                [r["filename"] for r in st.session_state.cas_results],
            )
            r = next(x for x in st.session_state.cas_results if x["filename"] == chosen)
            st.text_area("Preview", r["text_preview"], height=350)


# -----------------------------------------------------------------------------
# XIRR & Benchmarking
# -----------------------------------------------------------------------------

elif section == "XIRR & Benchmarking":
    st.title("XIRR & Benchmarking")
    st.caption("Enter dated investor cash flows. Contributions/investments are normally negative.")

    sample = pd.DataFrame(
        {
            "date": ["2022-01-01", "2023-01-01", date.today().isoformat()],
            "portfolio_cashflow": [-1000000, -250000, 1600000],
            "benchmark_cashflow": [-1000000, -250000, 1500000],
        }
    )
    edited = st.data_editor(
        sample,
        num_rows="dynamic",
        use_container_width=True,
        key="xirr_editor",
    )

    if st.button("Calculate XIRR", type="primary"):
        try:
            dates = pd.to_datetime(edited["date"], errors="coerce").dt.date
            pflows = [(d, D(v)) for d, v in zip(dates, edited["portfolio_cashflow"]) if pd.notna(d)]
            bflows = [(d, D(v)) for d, v in zip(dates, edited["benchmark_cashflow"]) if pd.notna(d)]

            pxirr = xirr(pflows)
            bxirr = xirr(bflows)
            excess = pxirr - bxirr
            c1, c2, c3 = st.columns(3)
            c1.metric("Portfolio XIRR", f"{float(pxirr)*100:.2f}%")
            c2.metric("Benchmark XIRR", f"{float(bxirr)*100:.2f}%")
            c3.metric("Excess XIRR", f"{float(excess)*100:.2f}%")

            chart_df = pd.DataFrame(
                {
                    "Series": ["Portfolio", "Benchmark", "Excess"],
                    "XIRR %": [float(pxirr)*100, float(bxirr)*100, float(excess)*100],
                }
            )
            st.plotly_chart(
                px.bar(chart_df, x="Series", y="XIRR %", text_auto=".2f"),
                use_container_width=True,
            )
        except Exception as exc:
            st.error(str(exc))


# -----------------------------------------------------------------------------
# Tax Lots
# -----------------------------------------------------------------------------

elif section == "Tax Lots":
    st.title("Tax-Lot Engine")
    st.caption(
        "FIFO/LIFO sale simulation. Tax rates are configurable because statutory rules can change."
    )

    sample_lots = pd.DataFrame(
        {
            "lot_id": ["L1", "L2"],
            "quantity": [100, 50],
            "remaining_quantity": [100, 50],
            "acquire_date": ["2024-01-15", "2026-04-01"],
            "acquire_price": [500.0, 650.0],
            "lot_type": ["BUY", "BUY"],
        }
    )
    lot_df = st.data_editor(sample_lots, num_rows="dynamic", use_container_width=True)

    c1, c2, c3, c4 = st.columns(4)
    sale_qty = c1.number_input("Sale quantity", min_value=0.0, value=50.0)
    sale_price = c2.number_input("Sale price", min_value=0.0, value=800.0)
    sale_date = c3.date_input("Sale date", value=date.today())
    method = c4.selectbox("Matching method", ["FIFO", "LIFO"])

    c5, c6, c7 = st.columns(3)
    stcg_rate = c5.number_input("STCG tax rate %", min_value=0.0, max_value=100.0, value=20.0)
    ltcg_rate = c6.number_input("LTCG tax rate %", min_value=0.0, max_value=100.0, value=12.5)
    market_price = c7.number_input("Current market price", min_value=0.0, value=float(sale_price))

    def lots_from_df(df: pd.DataFrame) -> list[Lot]:
        lots: list[Lot] = []
        for i, row in df.iterrows():
            ad = pd.to_datetime(row["acquire_date"], errors="coerce")
            if pd.isna(ad):
                continue
            lots.append(
                Lot(
                    lot_id=str(row.get("lot_id", f"L{i+1}")),
                    quantity=D(row.get("quantity")),
                    remaining_quantity=D(row.get("remaining_quantity")),
                    acquire_date=ad.date(),
                    acquire_price=D(row.get("acquire_price")),
                    lot_type=str(row.get("lot_type", "BUY")),
                )
            )
        return lots

    if st.button("Simulate sale", type="primary"):
        try:
            lots = lots_from_df(lot_df)
            result = simulate_sale(
                lots=lots,
                quantity=D(sale_qty),
                sale_price=D(sale_price),
                sale_date=sale_date,
                method=method,
                stcg_rate=D(stcg_rate) / D(100),
                ltcg_rate=D(ltcg_rate) / D(100),
            )

            c1, c2 = st.columns(2)
            c1.metric("Total gain/loss", fmt_inr(result.total_gain))
            c2.metric("Estimated tax", fmt_inr(result.estimated_tax))

            matches = pd.DataFrame([asdict(m) for m in result.matches])
            if not matches.empty:
                for c in ["quantity", "cost_basis", "proceeds", "gain"]:
                    matches[c] = matches[c].astype(float)
                st.dataframe(matches, use_container_width=True, hide_index=True)
                df_download("Download sale-lot matches", matches, "tax_lot_sale_matches.csv", "sale_dl")
        except Exception as exc:
            st.error(str(exc))

    st.subheader("Harvestable-loss scan")
    try:
        lots = lots_from_df(lot_df)
        min_loss = st.number_input("Minimum absolute loss to flag (₹)", min_value=0.0, value=1000.0)
        loss_df = harvestable_losses(lots, D(market_price), date.today(), D(min_loss))
        st.dataframe(loss_df, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(str(exc))

    with st.expander("Bonus / split adjustment utility"):
        a, b = st.columns(2)
        new_units = a.number_input("New units in ratio", min_value=0.01, value=2.0)
        old_units = b.number_input("Old units in ratio", min_value=0.01, value=1.0)
        if st.button("Preview adjusted lots"):
            try:
                adjusted = apply_bonus_or_split(
                    lots_from_df(lot_df),
                    D(new_units),
                    D(old_units),
                )
                adf = pd.DataFrame([asdict(x) for x in adjusted])
                for c in ["quantity", "remaining_quantity", "acquire_price"]:
                    adf[c] = adf[c].astype(float)
                st.dataframe(adf, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(str(exc))


# -----------------------------------------------------------------------------
# Return Attribution
# -----------------------------------------------------------------------------

elif section == "Return Attribution":
    st.title("Return Attribution")
    tab1, tab2 = st.tabs(["Waterfall", "Brinson-Fachler"])

    with tab1:
        st.caption("Explain the change from starting value to ending value.")
        cols = st.columns(3)
        starting = cols[0].number_input("Starting value", value=1000000.0)
        contributions = cols[1].number_input("Contributions", value=200000.0)
        withdrawals = cols[2].number_input("Withdrawals", value=0.0)
        cols2 = st.columns(4)
        investment_return = cols2[0].number_input("Investment return", value=120000.0)
        costs = cols2[1].number_input("Costs", value=10000.0)
        taxes = cols2[2].number_input("Taxes", value=5000.0)
        ending = cols2[3].number_input("Ending value", value=1305000.0)

        expected = D(starting) + D(contributions) - D(withdrawals) + D(investment_return) - D(costs) - D(taxes)
        unexplained = D(ending) - expected

        data = pd.DataFrame(
            {
                "Component": [
                    "Starting value", "Contributions", "Withdrawals",
                    "Investment return", "Costs", "Taxes", "Unexplained"
                ],
                "Amount": [
                    float(D(starting)), float(D(contributions)), -float(D(withdrawals)),
                    float(D(investment_return)), -float(D(costs)), -float(D(taxes)),
                    float(unexplained),
                ],
            }
        )
        st.plotly_chart(
            go.Figure(
                go.Waterfall(
                    name="Attribution",
                    orientation="v",
                    measure=["absolute"] + ["relative"] * 6,
                    x=data["Component"],
                    y=data["Amount"],
                    connector={"line": {"width": 1}},
                )
            ),
            use_container_width=True,
        )
        st.metric("Reconciliation gap", fmt_inr(unexplained))

    with tab2:
        st.caption("Weights and returns should be entered as decimals, e.g. 0.25 = 25%.")
        sample = pd.DataFrame(
            {
                "bucket": ["Large Cap", "Mid Cap", "Other"],
                "portfolio_weight": [0.50, 0.30, 0.20],
                "benchmark_weight": [0.60, 0.25, 0.15],
                "portfolio_return": [0.12, 0.18, 0.08],
                "benchmark_return": [0.10, 0.14, 0.07],
            }
        )
        bdf = st.data_editor(sample, num_rows="dynamic", use_container_width=True)
        if st.button("Calculate Brinson attribution"):
            try:
                r = brinson_fachler(bdf)
                result_df = pd.DataFrame(
                    {
                        "Effect": ["Allocation", "Selection", "Interaction", "Total"],
                        "Contribution %": [
                            float(r["allocation"]) * 100,
                            float(r["selection"]) * 100,
                            float(r["interaction"]) * 100,
                            float(sum(r.values())) * 100,
                        ],
                    }
                )
                st.dataframe(result_df, use_container_width=True, hide_index=True)
                st.plotly_chart(
                    px.bar(result_df, x="Effect", y="Contribution %", text_auto=".2f"),
                    use_container_width=True,
                )
            except Exception as exc:
                st.error(str(exc))


# -----------------------------------------------------------------------------
# Institutional Intelligence
# -----------------------------------------------------------------------------

elif section == "Institutional Intelligence":
    st.title("Institutional & Index Intelligence")
    st.caption(
        "Point-in-time institutional analysis based on the blueprint's "
        "aditya-jha/nse-historical-membership integration."
    )

    st.subheader("Load datasets")
    up_cols = st.columns(5)
    upload_map = {}
    for idx, key in enumerate(UPSTREAM_PATHS):
        upload_map[key] = up_cols[idx].file_uploader(
            key,
            type=["csv"],
            key=f"up_{idx}",
        )

    if st.button("Load uploaded CSVs"):
        try:
            st.session_state.shareholding_df = read_uploaded_csv(upload_map["Shareholding history"])
            st.session_state.signals_df = read_uploaded_csv(upload_map["Shareholding signals"])
            st.session_state.index_df = read_uploaded_csv(upload_map["Index membership"])
            st.session_state.fno_df = read_uploaded_csv(upload_map["F&O membership"])
            st.session_state.universe_df = read_uploaded_csv(upload_map["Universe / renames"])
            st.success("Uploaded datasets loaded into this session.")
        except Exception as exc:
            st.error(str(exc))

    if st.button("Fetch available upstream CSVs from GitHub"):
        progress = st.progress(0)
        loaded = []
        failed = []
        keys = list(UPSTREAM_PATHS.items())
        for i, (name, rel) in enumerate(keys, start=1):
            try:
                df = fetch_upstream_csv(rel)
                if name == "Shareholding history":
                    st.session_state.shareholding_df = df
                elif name == "Shareholding signals":
                    st.session_state.signals_df = df
                elif name == "Index membership":
                    st.session_state.index_df = df
                elif name == "F&O membership":
                    st.session_state.fno_df = df
                elif name == "Universe / renames":
                    st.session_state.universe_df = df
                loaded.append(name)
            except Exception as exc:
                failed.append(f"{name}: {exc}")
            progress.progress(i / len(keys))
        if loaded:
            st.success("Loaded: " + ", ".join(loaded))
        if failed:
            st.warning(
                "Some upstream files could not be fetched. The upstream repository layout may "
                "have changed. Use manual CSV upload.\n\n" + "\n".join(failed)
            )

    status_df = pd.DataFrame(
        {
            "Dataset": list(UPSTREAM_PATHS.keys()),
            "Rows loaded": [
                len(st.session_state.shareholding_df),
                len(st.session_state.signals_df),
                len(st.session_state.index_df),
                len(st.session_state.fno_df),
                len(st.session_state.universe_df),
            ],
        }
    )
    st.dataframe(status_df, use_container_width=True, hide_index=True)

    st.subheader("As-of stock intelligence")
    c1, c2 = st.columns(2)
    symbol = c1.text_input("NSE symbol", value="RELIANCE").upper().strip()
    as_of = c2.date_input("As-of date", value=date.today(), key="pit_date")

    if st.button("Run point-in-time lookup", type="primary"):
        if st.session_state.shareholding_df.empty:
            st.error("Load shareholding history first.")
        else:
            row, mapping = pit_shareholding(st.session_state.shareholding_df, symbol, as_of)
            smart = four_quarter_smart_money(st.session_state.shareholding_df, symbol, as_of)

            if row is None:
                st.warning("No shareholding observation found at or before the selected date.")
            else:
                metrics = []
                for display, key in [
                    ("Promoter %", "promoter"),
                    ("FII %", "fii"),
                    ("DII %", "dii"),
                    ("Public %", "public"),
                    ("Pledge %", "pledge"),
                ]:
                    col = mapping.get(key)
                    metrics.append((display, row[col] if col else None))

                cols = st.columns(5)
                for col_ui, (label, value) in zip(cols, metrics):
                    col_ui.metric(label, "N/A" if value is None else str(value))

                st.write("**Observation period:**", row.get("_period"))

            if smart.get("available"):
                c1, c2, c3 = st.columns(3)
                c1.metric("FII Δ 4Q", f"{smart['fii_delta_4q']:.2f} pp")
                c2.metric("DII Δ 4Q", f"{smart['dii_delta_4q']:.2f} pp")
                c3.metric("Smart-money score", f"{smart['smart_money_score']:.2f}")
                if smart["smart_money_score"] > 0:
                    st.success("Net institutional ownership trend is positive over the four-quarter lookback.")
                elif smart["smart_money_score"] < 0:
                    st.warning("Net institutional ownership trend is negative over the four-quarter lookback.")
            else:
                st.info(smart.get("reason", "Smart-money score unavailable."))

            if not st.session_state.index_df.empty:
                idx = membership_asof(st.session_state.index_df, symbol, as_of, "Index")
                st.subheader("Index membership at selected date")
                st.dataframe(idx, use_container_width=True, hide_index=True)

            if not st.session_state.fno_df.empty:
                fno = membership_asof(st.session_state.fno_df, symbol, as_of, "F&O")
                st.subheader("F&O membership at selected date")
                st.dataframe(fno, use_container_width=True, hide_index=True)

    if not st.session_state.shareholding_df.empty:
        with st.expander("Shareholding data sample"):
            st.dataframe(st.session_state.shareholding_df.head(50), use_container_width=True)


# -----------------------------------------------------------------------------
# Advice Impact
# -----------------------------------------------------------------------------

elif section == "Advice Impact":
    st.title("Advice Impact Estimator")
    st.caption(
        "Quantifies an action after estimated tax and exit-load friction. "
        "All forward return inputs are assumptions."
    )

    action = st.text_input("Proposed action", "Switch part of Holding A to Holding B")
    cols = st.columns(3)
    d1 = cols[0].number_input("Gross expected ΔXIRR 1Y (pp)", value=0.50)
    d3 = cols[1].number_input("Gross expected ΔXIRR 3Y (pp)", value=1.40)
    d5 = cols[2].number_input("Gross expected ΔXIRR 5Y (pp)", value=1.20)

    cols2 = st.columns(4)
    portfolio_value = cols2[0].number_input("Portfolio value (₹)", min_value=1.0, value=10000000.0)
    tax_cost = cols2[1].number_input("Tax cost today (₹)", min_value=0.0, value=28500.0)
    exit_load = cols2[2].number_input("Exit load (₹)", min_value=0.0, value=0.0)
    confidence = cols2[3].slider("Confidence", 0.0, 1.0, 0.75, 0.01)

    risk_change = st.selectbox(
        "Risk change",
        ["Lower", "Slightly lower", "Neutral", "Slightly higher", "Higher"],
    )
    evidence_text = st.text_area(
        "Evidence — one item per line",
        "Underperformance versus relevant benchmark\nInstitutional ownership trend\nConcentration reduction",
    )

    if st.button("Estimate impact", type="primary"):
        try:
            result = estimate_advice_impact(
                action=action,
                gross_delta_xirr_1y=D(d1),
                gross_delta_xirr_3y=D(d3),
                gross_delta_xirr_5y=D(d5),
                tax_cost=D(tax_cost),
                exit_load_cost=D(exit_load),
                portfolio_value=D(portfolio_value),
                confidence=D(confidence),
                risk_change=risk_change,
                evidence=[x.strip() for x in evidence_text.splitlines() if x.strip()],
            )
            c1, c2, c3 = st.columns(3)
            c1.metric("Gross ΔXIRR 3Y", f"{float(result.expected_delta_xirr_3y):.2f} pp")
            c2.metric("Net expected benefit 3Y", f"{float(result.net_expected_benefit_3y):.2f} pp")
            c3.metric("Estimated friction", fmt_inr(result.tax_cost_today + result.exit_load_cost))

            if result.show_recommendation:
                st.success("Passes the blueprint guardrail: estimated net benefit remains positive after friction.")
            else:
                st.error("Do-not-trade guardrail triggered: estimated net benefit is not positive after friction.")

            st.json(
                {
                    "action": result.action,
                    "expected_delta_xirr_1y": float(result.expected_delta_xirr_1y),
                    "expected_delta_xirr_3y": float(result.expected_delta_xirr_3y),
                    "expected_delta_xirr_5y": float(result.expected_delta_xirr_5y),
                    "tax_cost_today": float(result.tax_cost_today),
                    "exit_load_cost": float(result.exit_load_cost),
                    "net_expected_benefit_3y": float(result.net_expected_benefit_3y),
                    "risk_change": result.risk_change,
                    "confidence": float(result.confidence),
                    "evidence": result.evidence,
                    "show_recommendation": result.show_recommendation,
                }
            )
        except Exception as exc:
            st.error(str(exc))


# -----------------------------------------------------------------------------
# Goals & Stress Test
# -----------------------------------------------------------------------------

elif section == "Goals & Stress Test":
    st.title("Goal Linkage & Stress Testing")
    tab1, tab2 = st.tabs(["Required XIRR", "Scenario engine"])

    with tab1:
        cols = st.columns(3)
        current = cols[0].number_input("Current portfolio value (₹)", min_value=1.0, value=10000000.0)
        target = cols[1].number_input("Target value (₹)", min_value=1.0, value=30000000.0)
        years = cols[2].number_input("Years to goal", min_value=0.1, value=10.0)

        try:
            req = required_cagr(D(current), D(target), D(years))
            st.metric("Required annual CAGR/XIRR proxy", f"{float(req)*100:.2f}%")
        except Exception as exc:
            st.error(str(exc))

    with tab2:
        cols = st.columns(3)
        base_return = cols[0].number_input("Base annual return %", value=12.0)
        bull_return = cols[1].number_input("Bull annual return %", value=18.0)
        bear_return = cols[2].number_input("Bear annual return %", value=2.0)
        annual_contribution = st.number_input("Annual contribution (₹)", min_value=0.0, value=0.0)
        horizon = st.slider("Projection horizon (years)", 1, 20, 5)

        rows = []
        for y in range(1, horizon + 1):
            rows.append(
                {
                    "Year": y,
                    "Base": float(future_value(D(current), D(base_return), y, D(annual_contribution))),
                    "Bull": float(future_value(D(current), D(bull_return), y, D(annual_contribution))),
                    "Bear": float(future_value(D(current), D(bear_return), y, D(annual_contribution))),
                }
            )
        sdf = pd.DataFrame(rows)
        long = sdf.melt("Year", var_name="Scenario", value_name="Projected value")
        st.plotly_chart(
            px.line(long, x="Year", y="Projected value", color="Scenario", markers=True),
            use_container_width=True,
        )
        st.dataframe(sdf, use_container_width=True, hide_index=True)
        st.caption("Scenario projections are hypothetical and are not forecasts.")


# -----------------------------------------------------------------------------
# Data Quality
# -----------------------------------------------------------------------------

elif section == "Data Quality":
    st.title("Data Quality & Reconciliation")
    st.caption("Check quantity continuity between two portfolio snapshots.")

    cols = st.columns(5)
    prev = cols[0].number_input("Previous quantity", value=100.0)
    buys = cols[1].number_input("Buys", value=20.0)
    sells = cols[2].number_input("Sells", value=10.0)
    ca = cols[3].number_input("Corporate-action delta", value=0.0)
    current = cols[4].number_input("Current quantity", value=110.0)

    if st.button("Run quantity continuity check", type="primary"):
        issue = quantity_continuity_issue(D(prev), D(buys), D(sells), D(ca), D(current))
        issues = [issue] if issue else []
        confidence = combine_confidence(Decimal("1"), issues)

        if issue:
            st.error(issue.message)
            st.json(asdict(issue))
        else:
            st.success("Quantity continuity reconciles within tolerance.")
        st.metric("Confidence after check", f"{float(confidence)*100:.0f}%")

    st.subheader("Current session confidence signals")
    rows = []
    for r in st.session_state.cas_results:
        rows.append(
            {
                "file": r["filename"],
                "parse_confidence": r["parse_confidence"],
                "holding_rows": len(r["holdings"]),
                "warnings": " | ".join(r["warnings"]),
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No CAS files have been parsed in this session yet.")


# -----------------------------------------------------------------------------
# System Status
# -----------------------------------------------------------------------------

elif section == "System Status":
    st.title("System Status")
    st.subheader("Version & runtime")
    runtime_df = pd.DataFrame(runtime_check().items(), columns=["Component", "Version / value"])
    st.dataframe(runtime_df, use_container_width=True, hide_index=True)

    st.info(
        "v1.0.1 hotfix: Streamlit Community Cloud requires the main module to end in lowercase `.py`. "
        "The repository now uses `app.py` and `requirements.txt`."
    )

    st.subheader("Three-file deployment")
    st.code("README.md\nrequirements.txt\napp.py", language="text")

    st.subheader("Implementation maturity")
    status = pd.DataFrame(
        [
            ["Single-file Streamlit UI", "Implemented", APP_VERSION],
            ["Python 3.14 target", "Configured for deployment selection", APP_VERSION],
            ["CAS PDF extraction", "Implemented", APP_VERSION],
            ["Password-protected PDF support", "Implemented", APP_VERSION],
            ["CAS layout hardening", "Partial", APP_VERSION],
            ["Transaction reconstruction", "Not yet reliable", APP_VERSION],
            ["XIRR engine", "Implemented", APP_VERSION],
            ["Tax-lot simulation", "Implemented starter", APP_VERSION],
            ["Return attribution", "Implemented starter", APP_VERSION],
            ["Institutional PIT overlay", "Implemented when data supplied", APP_VERSION],
            ["Index/F&O history", "Implemented when data supplied", APP_VERSION],
            ["Advice impact estimator", "Implemented starter", APP_VERSION],
            ["Goal/scenario engine", "Implemented starter", APP_VERSION],
            ["Persistent database", "Excluded from 3-file build", APP_VERSION],
            ["Encrypted object storage", "Excluded from 3-file build", APP_VERSION],
            ["Authentication", "Excluded from 3-file build", APP_VERSION],
            ["Continuous scheduled monitoring", "Not implemented", APP_VERSION],
        ],
        columns=["Capability", "Status", "Since version"],
    )
    st.dataframe(status, use_container_width=True, hide_index=True)

    st.subheader("Deployment notes")
    st.markdown(
        """
        - Select **Python 3.14** in Streamlit Community Cloud **Advanced settings**.
        - Use `app.py` as the entrypoint.
        - `requirements.txt` pins package versions.
        - Password-protected CAS PDFs are supported; enter the password on the CAS Parser page.
        - If an upstream institutional CSV cannot be fetched, upload it manually in the app.
        - Do not treat parser-inferred quantities as validated until they reconcile with the source CAS.
        """
    )

    st.warning(
        "The three-file constraint intentionally removes database migrations, background workers, "
        "encrypted persistent storage and separate test modules. Those are production-hardening "
        "features, not silently claimed as complete in this Streamlit build."
    )

# Footer
st.divider()
st.caption(
    f"NSDL CAS Portfolio Intelligence & Advisory System · v{APP_VERSION} · "
    f"Blueprint v{BLUEPRINT_VERSION} · Python {TARGET_PYTHON} target"
)
