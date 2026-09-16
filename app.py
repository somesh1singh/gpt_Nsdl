# =============================================================================
# NSDL CAS Portfolio Intelligence & Advisory System
# APP VERSION: 1.1.9
# BLUEPRINT BASELINE: 1.0
# TARGET PYTHON: 3.14
# BUILD DATE: 2026-09-14
# Entrypoint: app.py
# =============================================================================

from __future__ import annotations

import hashlib
import time
import urllib.parse
import io
import json
import math
import platform
import re
import sys
from dataclasses import dataclass, asdict, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any, Literal

import fitz  # PyMuPDF
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

APP_VERSION = "1.1.9"
BLUEPRINT_VERSION = "1.0"
TARGET_PYTHON = "3.14"
BUILD_DATE = "2026-09-16"
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


def xirr_diagnostic_workbook(result: dict[str, Any], readiness: dict[str, Any]) -> bytes:
    """Create a single auditable XLSX pack for resolving broker XIRR blockers."""
    def frame(name: str) -> pd.DataFrame:
        value = result.get(name, pd.DataFrame())
        return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()

    rec = frame("reconciliation")
    unresolved = rec[rec.get("status", pd.Series(index=rec.index, dtype=str)).astype(str) != "Quantity agrees"].copy() if not rec.empty else rec
    rejected = frame("rejected")
    external = readiness.get("cashflows", pd.DataFrame())
    ledger_checks = frame("ledger_checks")

    issue_rows = pd.DataFrame({"issue": [str(x) for x in result.get("issues", [])]})
    ca_mask = issue_rows["issue"].str.contains("corporate|bonus|split|merger|demerger|transfer", case=False, na=False) if not issue_rows.empty else pd.Series(dtype=bool)
    corporate = issue_rows[ca_mask].copy() if not issue_rows.empty else issue_rows

    trades = frame("trades")
    fo = pd.DataFrame()
    if not trades.empty:
        # Coerce every cell explicitly; object columns can retain numeric scalars
        # and pandas agg(" ".join) then raises "expected str instance, float found".
        searchable = trades.apply(lambda row: " ".join("" if pd.isna(v) else str(v) for v in row.tolist()), axis=1)
        fo_mask = searchable.str.contains(r"\b(?:FUT|CE|PE|OPT|F&O|NFO)\b", case=False, regex=True, na=False)
        fo = trades[fo_mask].copy()

    summary = readiness.get("gates", pd.DataFrame()).copy()
    summary.insert(0, "app_version", APP_VERSION)
    summary["holdings_as_of"] = str(result.get("as_of") or "")
    summary["opening_cash"] = str(result.get("opening_cash"))
    summary["closing_cash"] = str(result.get("closing_cash"))
    summary["xirr_ready"] = bool(readiness.get("ready"))

    xirr_rows = pd.DataFrame()
    result_rows = [{"metric": "XIRR ready", "value": bool(readiness.get("ready"))}]
    if readiness.get("ready"):
        flows = broker_xirr_cashflows(result)
        xirr_rows = pd.DataFrame([{"date": d, "cashflow": v} for d, v in flows])
        try:
            result_rows.append({"metric": "Actual portfolio XIRR", "value": f"{float(xirr(flows))*100:.6f}%"})
        except Exception as exc:
            result_rows.append({"metric": "XIRR calculation error", "value": str(exc)})
    else:
        blocked = summary.loc[summary["status"].eq("BLOCK"), "gate"].astype(str).tolist() if not summary.empty else []
        result_rows.append({"metric": "Blocked gates", "value": " | ".join(blocked)})

    sheets = {
        "Summary_Gates": summary,
        "Unresolved_Reconciliation": unresolved,
        "Rejected_Rows": rejected,
        "FO_Reconciliation": fo,
        "Corporate_Actions": corporate,
        "External_Cashflows": external,
        "Ledger_Checks": ledger_checks,
        "Holdings_Reconciliation": rec,
        "XIRR_Cashflows": xirr_rows,
        "XIRR_Result": pd.DataFrame(result_rows),
    }
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, df in sheets.items():
            safe = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
            if safe.empty:
                safe = pd.DataFrame({"status": ["No rows"]})
            safe.to_excel(writer, sheet_name=name[:31], index=False)
            ws = writer.book[name[:31]]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                width = min(max((len(str(c.value)) if c.value is not None else 0) for c in col) + 2, 60)
                ws.column_dimensions[col[0].column_letter].width = max(width, 12)
    return output.getvalue()


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


def india_today() -> date:
    """Return the current calendar date in India, independent of the cloud server timezone."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def runtime_check() -> dict[str, str]:
    return {
        "App": APP_VERSION,
        "Blueprint": BLUEPRINT_VERSION,
        "Python target": TARGET_PYTHON,
        "Python running": platform.python_version(),
        "India date": india_today().isoformat(),
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
# Automated portfolio reconstruction + benchmark simulation
# -----------------------------------------------------------------------------

BENCHMARKS = {
    "NIFTY 50": "^NSEI",
    "NIFTY 500": "^CRSLDX",
    "Gold BeES": "GOLDBEES.NS",
}


def reconstructed_cas_transactions() -> pd.DataFrame:
    frames = []
    for result in st.session_state.get("cas_results", []):
        df = result.get("transactions")
        if isinstance(df, pd.DataFrame) and not df.empty:
            frames.append(df.copy())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "txn_date" in out.columns:
        out["txn_date"] = pd.to_datetime(out["txn_date"], errors="coerce").dt.date
    return out


def reconstructed_transaction_history_diagnostics() -> dict[str, Any]:
    """Aggregate period-level transaction diagnostics across parsed CAS statements."""
    results = st.session_state.get("cas_results", [])
    opening_rows = 0
    opening_nonzero = 0
    malformed = 0
    statements_with_transactions = 0
    for result in results:
        diag = result.get("transaction_diagnostics") or {}
        opening_rows += int(diag.get("opening_balance_rows", 0) or 0)
        opening_nonzero += int(diag.get("opening_balance_nonzero", 0) or 0)
        malformed += int(diag.get("malformed_monetary_rows", 0) or 0)
        tx = result.get("transactions")
        if isinstance(tx, pd.DataFrame) and not tx.empty:
            statements_with_transactions += 1
    return {
        "opening_balance_rows": opening_rows,
        "opening_balance_nonzero": opening_nonzero,
        "malformed_monetary_rows": malformed,
        "statements_with_transactions": statements_with_transactions,
        "history_complete": opening_nonzero == 0 and malformed == 0 and statements_with_transactions > 0,
    }


def transaction_quality_report(tx: pd.DataFrame) -> dict[str, Any]:
    if tx.empty:
        return {
            "valid": False,
            "reason": "No structured transaction rows were reconstructed.",
            "rows": 0,
            "duplicates": 0,
            "low_confidence": 0,
            "narrative_hits": 0,
            "cashflow_rows": 0,
            "external_cashflow_rows": 0,
            "internal_transfer_rows": 0,
            "reversal_review_rows": 0,
            "continuity_anomalies": 0,
            "quantity_only_rows": 0,
            "cashflow_coverage_pct": 0.0,
            "automated_xirr_ready": False,
        }

    source = tx.get("source_line", pd.Series([""] * len(tx))).fillna("").astype(str)
    normalized = source.str.lower().str.replace(r"\s+", " ", regex=True)

    narrative_hits = 0
    for source_text in normalized:
        if any(phrase in source_text for phrase in NON_TRANSACTION_PHRASES):
            narrative_hits += 1

    duplicate_subset = [
        c for c in [
            "isin", "folio", "txn_date", "txn_type", "quantity_inferred",
            "amount_inferred", "source_line"
        ]
        if c in tx.columns
    ]
    duplicates = int(tx.duplicated(subset=duplicate_subset).sum()) if duplicate_subset else 0

    confidence = pd.to_numeric(
        tx.get("transaction_confidence", pd.Series([0] * len(tx))),
        errors="coerce",
    ).fillna(0)
    low_confidence = int((confidence < 0.85).sum())

    source_kind = tx.get("transaction_source", pd.Series([""] * len(tx))).fillna("").astype(str)
    quantity_only_rows = int((source_kind == "DEPOSITORY_QUANTITY_ONLY").sum())
    monetary_rows = int((source_kind == "MF_FOLIO_MONETARY").sum())
    cashflow_coverage_pct = float(monetary_rows / len(tx) * 100) if len(tx) else 0.0

    scope = tx.get("cashflow_scope", pd.Series([""] * len(tx))).fillna("").astype(str)
    external_cashflow_rows = int((scope == "EXTERNAL").sum())
    internal_transfer_rows = int((scope == "INTERNAL").sum())
    reversal_review_rows = int((scope == "REVIEW").sum())

    continuity = pd.to_numeric(
        tx.get("balance_reconciliation_error_pct", pd.Series([None] * len(tx))),
        errors="coerce",
    )
    continuity_anomalies = int((continuity > 1.0).sum())

    valid = narrative_hits == 0 and duplicates == 0 and low_confidence == 0 and continuity_anomalies == 0
    reasons = []
    if narrative_hits:
        reasons.append(f"{narrative_hits} narrative/disclosure row(s)")
    if duplicates:
        reasons.append(f"{duplicates} duplicate row(s)")
    if low_confidence:
        reasons.append(f"{low_confidence} low-confidence row(s)")
    if continuity_anomalies:
        reasons.append(f"{continuity_anomalies} quantity-continuity anomaly row(s) flagged for review")

    # CAS depository rows validate quantity movements but omit trade consideration.
    # Full-portfolio XIRR additionally requires no unresolved reversal rows.
    automated_xirr_ready = (
        valid
        and quantity_only_rows == 0
        and reversal_review_rows == 0
        and external_cashflow_rows > 0
    )

    return {
        "valid": valid,
        "reason": "Structured ledger quality gate passed." if valid else "; ".join(reasons),
        "rows": len(tx),
        "duplicates": duplicates,
        "low_confidence": low_confidence,
        "narrative_hits": narrative_hits,
        "cashflow_rows": monetary_rows,
        "external_cashflow_rows": external_cashflow_rows,
        "internal_transfer_rows": internal_transfer_rows,
        "reversal_review_rows": reversal_review_rows,
        "continuity_anomalies": continuity_anomalies,
        "quantity_only_rows": quantity_only_rows,
        "cashflow_coverage_pct": cashflow_coverage_pct,
        "automated_xirr_ready": automated_xirr_ready,
    }


def latest_cas_result() -> dict[str, Any] | None:
    results = st.session_state.get("cas_results", [])
    if not results:
        return None

    def result_date(r: dict[str, Any]) -> date:
        return (
            r.get("holdings_as_of")
            or r.get("period_end")
            or r.get("period_start")
            or date.min
        )

    return max(results, key=result_date)


def reconstructed_terminal_value() -> Decimal:
    """
    Use only the latest CAS statement.

    Prefer NSDL's exact 'Consolidated Portfolio Value' from that statement.
    Never add market values across several monthly CAS files.
    """
    latest = latest_cas_result()
    if latest is None:
        return Decimal("0")

    statement_value = latest.get("consolidated_portfolio_value")
    if statement_value is not None and D(statement_value) > 0:
        return D(statement_value)

    df = latest.get("holdings")
    if not isinstance(df, pd.DataFrame) or df.empty:
        return Decimal("0")

    if "market_value_inferred" not in df.columns:
        return Decimal("0")

    return sum(
        (D(v) for v in df["market_value_inferred"].dropna().tolist()),
        Decimal("0"),
    )


def reconstructed_portfolio_cashflows(
    terminal_date: date | None = None,
) -> tuple[list[tuple[date, Decimal]], pd.DataFrame, Decimal]:
    tx = reconstructed_cas_transactions()
    terminal_value = reconstructed_terminal_value()

    if tx.empty:
        return [], tx, terminal_value

    flow_col = (
        "portfolio_cashflow_inferred"
        if "portfolio_cashflow_inferred" in tx.columns
        else "cashflow_inferred"
    )
    if flow_col not in tx.columns:
        return [], tx, terminal_value

    clean = tx.dropna(subset=["txn_date", flow_col]).copy()
    if "cashflow_scope" in clean.columns:
        clean = clean[clean["cashflow_scope"].eq("EXTERNAL")].copy()
    else:
        clean = clean[pd.to_numeric(clean[flow_col], errors="coerce").fillna(0).ne(0)].copy()
    if clean.empty:
        return [], clean, terminal_value

    grouped = (
        clean.groupby("txn_date", as_index=False)[flow_col]
        .sum()
        .sort_values("txn_date")
    )
    cashflows = [(d, D(v)) for d, v in zip(grouped["txn_date"], grouped[flow_col])]

    if terminal_date is None:
        terminal_date = max([d for d, _ in cashflows] + [india_today()])

    if terminal_value > 0:
        cashflows.append((terminal_date, terminal_value))

    return cashflows, clean, terminal_value


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_yahoo_history(symbol: str, start: date, end: date) -> pd.DataFrame:
    """
    Fetch daily adjusted/close data from Yahoo's chart endpoint.
    This is a convenience connector, not a guaranteed institutional data source.
    """
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=ZoneInfo("UTC"))
    end_dt = datetime.combine(end + timedelta(days=3), datetime.min.time(), tzinfo=ZoneInfo("UTC"))

    p1 = int(start_dt.timestamp())
    p2 = int(end_dt.timestamp())
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
        f"?period1={p1}&period2={p2}&interval=1d&events=history&includeAdjustedClose=true"
    )

    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()

    result = payload.get("chart", {}).get("result")
    if not result:
        error = payload.get("chart", {}).get("error")
        raise ValueError(f"No Yahoo data returned for {symbol}: {error}")

    result = result[0]
    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    adj = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
    close = quote.get("close", [])
    prices = adj if adj and len(adj) == len(timestamps) else close

    rows = []
    for ts, pxv in zip(timestamps, prices):
        if pxv is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")).date()
        rows.append({"date": dt, "price": float(pxv)})

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No usable price observations returned for {symbol}.")
    return df.drop_duplicates("date").sort_values("date")


def benchmark_price_on_or_after(prices: pd.DataFrame, target: date) -> Decimal | None:
    subset = prices[prices["date"] >= target]
    if subset.empty:
        return None
    return D(subset.iloc[0]["price"])


def benchmark_price_on_or_before(prices: pd.DataFrame, target: date) -> Decimal | None:
    subset = prices[prices["date"] <= target]
    if subset.empty:
        return None
    return D(subset.iloc[-1]["price"])


def simulate_benchmark_from_cashflows(
    transaction_cashflows: list[tuple[date, Decimal]],
    benchmark_symbol: str,
    terminal_date: date,
) -> tuple[list[tuple[date, Decimal]], Decimal, pd.DataFrame]:
    if not transaction_cashflows:
        raise ValueError("No transaction cash flows available for benchmark simulation.")

    start_date = min(d for d, _ in transaction_cashflows)
    prices = fetch_yahoo_history(benchmark_symbol, start_date - timedelta(days=10), terminal_date)
    units = Decimal("0")
    audit_rows = []

    benchmark_flows: list[tuple[date, Decimal]] = []
    for flow_date, cf in sorted(transaction_cashflows, key=lambda x: x[0]):
        pxv = benchmark_price_on_or_after(prices, flow_date)
        if pxv is None or pxv <= 0:
            audit_rows.append({
                "date": flow_date,
                "cashflow": float(cf),
                "price": None,
                "units_after": float(units),
                "status": "No benchmark price",
            })
            continue

        # Negative CF means capital invested -> buy benchmark units.
        # Positive CF means cash withdrawn -> sell benchmark units.
        units_change = (-cf) / pxv
        units += units_change

        benchmark_flows.append((flow_date, cf))
        audit_rows.append({
            "date": flow_date,
            "cashflow": float(cf),
            "price": float(pxv),
            "units_change": float(units_change),
            "units_after": float(units),
            "status": "OK",
        })

    terminal_price = benchmark_price_on_or_before(prices, terminal_date)
    if terminal_price is None or terminal_price <= 0:
        raise ValueError("No benchmark terminal price available.")

    terminal_value = units * terminal_price
    benchmark_flows.append((terminal_date, terminal_value))

    audit_df = pd.DataFrame(audit_rows)
    if not audit_df.empty:
        audit_df["benchmark"] = benchmark_symbol
    return benchmark_flows, terminal_value, audit_df


def cas_xirr_summary(benchmark_name: str) -> dict[str, Any]:
    cashflows, tx, terminal_value = reconstructed_portfolio_cashflows(terminal_date=india_today())

    quality = transaction_quality_report(tx)
    if not quality["valid"]:
        raise ValueError(
            "Automated XIRR blocked by the transaction quality gate: "
            + quality["reason"]
            + ". Review CAS Parser & Reconciliation."
        )

    if not quality["automated_xirr_ready"]:
        raise ValueError(
            "Full-portfolio automated XIRR is intentionally blocked. The CAS contains "
            f"{quality['quantity_only_rows']} depository quantity-only row(s) without trade "
            "consideration. Upload a broker tradebook/ledger or use the manual cashflow "
            "calculator; the app will not invent missing monetary flows."
        )

    history = reconstructed_transaction_history_diagnostics()
    if not history["history_complete"]:
        raise ValueError(
            "Full-portfolio automated XIRR is intentionally blocked because the uploaded CAS "
            f"transaction history begins with {history['opening_balance_nonzero']} non-zero "
            "opening balance(s) and therefore does not contain all historical investment cashflows. "
            "Use a broker ledger/tradebook covering the investment history or the manual calculator."
        )

    if not cashflows:
        raise ValueError("No reconstructed CAS monetary cash flows are available.")

    if terminal_value <= 0:
        raise ValueError(
            "No terminal market value was inferred from the parsed CAS holdings. "
            "Automated portfolio XIRR needs a current/terminal value."
        )

    portfolio_xirr = xirr(cashflows)

    # Benchmark should receive only transaction flows, not the portfolio terminal value.
    txn_flows = [(d, cf) for d, cf in cashflows[:-1]]
    symbol = BENCHMARKS[benchmark_name]
    benchmark_flows, benchmark_terminal, audit = simulate_benchmark_from_cashflows(
        txn_flows,
        symbol,
        india_today(),
    )
    benchmark_xirr_value = xirr(benchmark_flows)

    return {
        "portfolio_xirr": portfolio_xirr,
        "benchmark_xirr": benchmark_xirr_value,
        "excess_xirr": portfolio_xirr - benchmark_xirr_value,
        "portfolio_terminal_value": terminal_value,
        "benchmark_terminal_value": benchmark_terminal,
        "transactions": tx,
        "portfolio_cashflows": pd.DataFrame(
            [{"date": d, "cashflow": float(v)} for d, v in cashflows]
        ),
        "benchmark_cashflows": pd.DataFrame(
            [{"date": d, "cashflow": float(v)} for d, v in benchmark_flows]
        ),
        "benchmark_audit": audit,
        "benchmark_name": benchmark_name,
        "benchmark_symbol": symbol,
    }


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
ISIN_LINE_RE = re.compile(r"^(IN[A-Z0-9]{10})\b\s*(.*)$")


def split_isin_line(line: str) -> tuple[str | None, str]:
    """Return a leading ISIN plus any inline security-description tail."""
    normalized = " ".join(str(line).split()).strip()
    match = ISIN_LINE_RE.match(normalized)
    if not match:
        return None, ""
    return match.group(1), match.group(2).strip()

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


TRANSACTION_KEYWORDS = {
    "BUY": [
        "purchase", "purchased", "buy", "sip", "subscription",
        "switch in", "switch-in", "fresh purchase", "systematic investment"
    ],
    "SELL": [
        "sell", "sold", "redemption", "redeemed", "switch out", "switch-out"
    ],
    "DIVIDEND": [
        "dividend payout", "idcw payout", "income distribution payout"
    ],
    "DIV_REINVEST": [
        "dividend reinvest", "idcw reinvest"
    ],
    "BONUS": ["bonus allotment", "bonus units"],
    "RIGHTS": ["rights allotment", "rights subscription"],
}

# Dates that commonly occur in actual transaction rows.
TRANSACTION_DATE_RE = re.compile(
    r"\b("
    r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}"
    r")\b",
    re.IGNORECASE,
)

# Narrative / scheme-disclosure lines must never be treated as investor transactions.
NON_TRANSACTION_PHRASES = [
    "exit load",
    "entry load",
    "w.e.f",
    "with effect from",
    "if redeemed",
    "if switched",
    "date of allotment",
    "subscription received after",
    "subscription received before",
    "load structure",
    "expense ratio",
    "total expense ratio",
    "ter",
    "scheme information",
    "riskometer",
    "benchmark riskometer",
    "applicable nav",
    "cut-off time",
    "stamp duty",
    "statutory",
    "disclaimer",
    "subject to",
    "terms and conditions",
]


def parse_any_date(raw: str) -> date | None:
    raw = raw.strip()
    formats = (
        "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y",
        "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%d-%B-%y",
        "%d %b %Y", "%d %B %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def is_narrative_non_transaction(line: str) -> bool:
    ll = " ".join(line.lower().split())
    return any(phrase in ll for phrase in NON_TRANSACTION_PHRASES)


def transaction_date_is_row_like(line: str, match: re.Match) -> bool:
    """
    Real CAS transaction rows generally begin with the transaction date or place it
    very near the start of the row. Narrative scheme text can contain many dates much
    later in a sentence, so reject those.
    """
    prefix = line[:match.start()].strip(" |:-\t")
    return len(prefix) <= 18


def classify_transaction_line(line: str) -> str | None:
    ll = " ".join(line.lower().split())

    if is_narrative_non_transaction(line):
        return None

    # Portfolio-XIRR semantics require internal switches and reversals to be
    # distinguished from genuine external purchases/redemptions.
    if "reversal" in ll:
        return "REVERSAL"
    if re.search(r"\bswitch\s*-?\s*in\b", ll):
        return "SWITCH_IN"
    if re.search(r"\bswitch\s*-?\s*out\b", ll):
        return "SWITCH_OUT"

    # More specific phrases must be checked before broader ones.
    for txn_type in ["DIV_REINVEST", "SELL", "BUY", "DIVIDEND", "BONUS", "RIGHTS"]:
        for kw in TRANSACTION_KEYWORDS[txn_type]:
            if kw in ll:
                return txn_type
    return None


def transaction_cashflow_sign(txn_type: str) -> int:
    # Scheme-level direction. Whole-portfolio XIRR uses the separately stored
    # portfolio_cashflow_inferred field so internal switches are not counted.
    if txn_type in {"BUY", "RIGHTS", "SWITCH_IN"}:
        return -1
    if txn_type in {"SELL", "DIVIDEND", "SWITCH_OUT"}:
        return 1
    return 0


def strip_all_date_tokens(line: str) -> str:
    return TRANSACTION_DATE_RE.sub(" ", line)


def infer_transaction_numbers(line: str, date_token: str) -> tuple[Decimal | None, Decimal | None]:
    """
    Conservative fallback for row-like transaction lines.

    All date tokens are removed first so year fragments such as 2024 cannot become
    fake quantities/amounts. Returned amount is always absolute; transaction direction
    is applied separately.
    """
    cleaned = strip_all_date_tokens(line)
    nums = NUMBER_RE.findall(cleaned)
    if not nums:
        return None, None

    values = [abs(D(x)) for x in nums]

    # Reject rows that contain no meaningful monetary/quantity-like number.
    meaningful = [v for v in values if v != 0]
    if not meaningful:
        return None, None

    if len(values) >= 2:
        return values[-2], values[-1]
    return None, values[-1]



TRANSACTION_ISIN_HEADER_RE = re.compile(
    r"^ISIN\s*:\s*(IN[A-Z0-9]{10})\s*(?:[-–—]\s*)?(.*)$",
    re.IGNORECASE,
)
DEPOSITORY_CREDIT_RE = re.compile(r"\b[A-Z0-9]+-CR\b", re.IGNORECASE)
DEPOSITORY_DEBIT_RE = re.compile(r"\b[A-Z0-9]+-DR\b", re.IGNORECASE)


def transaction_numeric_values(block: str) -> list[Decimal]:
    """Return numeric values after removing all date tokens from a transaction block."""
    cleaned = strip_all_date_tokens(block)
    return [abs(D(x)) for x in NUMBER_RE.findall(cleaned)]


def transaction_isin_header(line: str) -> tuple[str | None, str | None]:
    normalized = " ".join(line.split()).strip()
    m = TRANSACTION_ISIN_HEADER_RE.match(normalized)
    if not m:
        return None, None
    isin = m.group(1).upper()
    description = m.group(2).strip(" -–—") or None
    return isin, description


def date_at_row_start(line: str) -> tuple[date | None, str | None]:
    m = TRANSACTION_DATE_RE.search(line)
    if not m or not transaction_date_is_row_like(line, m):
        return None, None
    return parse_any_date(m.group(1)), m.group(1)


def _balance_line(line: str, kind: str) -> Decimal | None:
    ll = " ".join(line.lower().split())
    if kind not in ll:
        return None
    vals = transaction_numeric_values(line)
    return vals[-1] if vals else None


def _mf_monetary_tail(block: str) -> dict[str, Decimal] | None:
    """
    Parse NSDL MF-folio monetary rows.

    In the observed CAS layout the right-most five numbers are:
      Amount, Stamp Duty, NAV, Price, Units.
    Descriptions can contain counters such as 45/927, therefore parsing is from
    the right and is accepted only when Price × Units reconciles to Amount.
    """
    vals = transaction_numeric_values(block)
    if len(vals) < 5:
        return None
    amount, stamp, nav, price, units = vals[-5:]
    if amount <= 0 or nav <= 0 or price <= 0 or units <= 0:
        return None
    err = relative_reconciliation_error(price * units, amount)
    if err > Decimal("0.03"):
        return None
    return {
        "amount": amount,
        "stamp_duty": stamp,
        "nav": nav,
        "price": price,
        "units": units,
        "reconciliation_error": err,
    }


def _transaction_noise_line(line: str) -> bool:
    normalized = " ".join(line.split()).strip().lower()
    if not normalized:
        return True
    if normalized in {
        "summary", "holdings", "transactions", "your account", "about nsdl",
        "national securities depository limited", "summary of transactions of",
        "date transaction details amount", "date transaction particulars credit debit current",
        "balance", "amount", "stamp duty", "nav", "price", "units",
    }:
        return True
    if normalized.startswith("consolidated account statement"):
        return True
    if normalized.startswith("national securities depository limited"):
        return True
    if re.fullmatch(r"\d{1,3}", normalized):
        return True
    return False


def parse_cas_transactions_layout(
    raw_lines: list[str],
    filename: str,
    holding_name_map: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Structured NSDL CAS transaction reconstruction.

    v1.1.2 hardens transaction recovery for the PDF text-layer layout actually
    emitted by NSDL CAS. Transaction table cells may be extracted either as one
    logical line or as several consecutive lines (date, particulars, quantity,
    balance / amount, NAV, units). We therefore assemble a bounded logical row
    beginning at each dated token before interpreting it.

    Two materially different CAS transaction layouts remain separated:
      1. MF Folio rows contain investor consideration, stamp duty, NAV/price and units.
      2. Depository (CDSL/NSDL) rows contain quantity movement and balance only;
         no trade consideration is fabricated.
    """
    holding_name_map = holding_name_map or {}
    rows: list[dict[str, Any]] = []
    current_isin: str | None = None
    current_name: str | None = None
    current_folio: str | None = None
    last_balance: dict[str, Decimal] = {}
    opening_balance_nonzero = 0
    opening_balance_rows = 0
    malformed_monetary_rows = 0
    in_transaction_section = False
    transactions_marker_pending = False

    def normalized_line(idx: int) -> str:
        return " ".join(raw_lines[idx].split()).strip()

    def isin_from_position(idx: int) -> tuple[str | None, str | None, int]:
        """Return ISIN, description, and number of consumed physical lines."""
        line = normalized_line(idx)
        isin, desc = transaction_isin_header(line)
        if isin:
            return isin, desc, 1

        low = line.lower().replace(" ", "")
        if low in {"isin:", "isin"} and idx + 1 < len(raw_lines):
            nxt = normalized_line(idx + 1)
            m = re.match(r"^(IN[A-Z0-9]{10})\b\s*(?:[-–—]\s*)?(.*)$", nxt, re.IGNORECASE)
            if m:
                return m.group(1).upper(), m.group(2).strip(" -–—") or None, 2

        if in_transaction_section:
            m = re.match(r"^(IN[A-Z0-9]{10})\b\s*(?:[-–—]\s*)?(.*)$", line, re.IGNORECASE)
            if m:
                return m.group(1).upper(), m.group(2).strip(" -–—") or None, 1
        return None, None, 0

    def is_section_boundary(line: str) -> bool:
        ll = line.lower()
        return (
            "transactions for the period" in ll
            or "transaction statement for the period" in ll
            or ll.startswith("summary of transactions of")
            or ll.startswith("date transaction particulars")
            or ll.startswith("date transaction details")
        )

    def collect_dated_block(start: int) -> tuple[str, int]:
        """Join split PDF table cells belonging to one dated transaction row."""
        parts = [normalized_line(start)]
        j = start + 1
        while j < len(raw_lines) and j <= start + 16:
            nxt = normalized_line(j)
            if not nxt:
                j += 1
                continue
            next_date, _ = date_at_row_start(nxt)
            next_isin, _, _ = isin_from_position(j)
            low = nxt.lower()
            if (next_date is not None or next_isin or low.startswith("folio no")
                    or "opening balance" in low or "closing balance" in low
                    or is_section_boundary(nxt)):
                break
            if _transaction_noise_line(nxt) or "***end of statement***" in low:
                j += 1
                continue
            if low.startswith("know more about your accounts"):
                break
            parts.append(nxt)
            j += 1
        return " ".join(parts), j

    i = 0
    while i < len(raw_lines):
        line = normalized_line(i)
        low = line.lower()

        if "transactions for the period" in low or "transaction statement for the period" in low:
            in_transaction_section = True
            transactions_marker_pending = False
        elif transactions_marker_pending and low.startswith("for the period"):
            in_transaction_section = True
            transactions_marker_pending = False
        elif low == "transactions":
            transactions_marker_pending = True
        elif transactions_marker_pending and low not in {"", "transactions"}:
            transactions_marker_pending = False

        isin, header_name, consumed = isin_from_position(i)
        if isin:
            current_isin = isin
            current_name = holding_name_map.get(isin) or header_name or isin
            current_folio = None
            i += consumed
            continue

        if low.startswith("folio no"):
            current_folio = line.split("-", 1)[-1].strip() if "-" in line else line
            i += 1
            continue

        # MF-folio opening/closing balances are often undated. Their numeric value
        # can also be extracted on the following physical line.
        if current_isin and ("opening balance" in low or "closing balance" in low):
            bal_kind = "opening balance" if "opening balance" in low else "closing balance"
            bal_block = line
            j = i + 1
            while j < len(raw_lines) and j <= i + 3:
                nxt = normalized_line(j)
                if not nxt:
                    j += 1
                    continue
                if date_at_row_start(nxt)[0] is not None or isin_from_position(j)[0] or nxt.lower().startswith("folio no"):
                    break
                if _transaction_noise_line(nxt):
                    j += 1
                    continue
                bal_block += " " + nxt
                break
            bal = _balance_line(bal_block, bal_kind)
            if bal is not None:
                last_balance[current_isin] = bal
                if bal_kind == "opening balance":
                    opening_balance_rows += 1
                    if bal > 0:
                        opening_balance_nonzero += 1
            i = max(i + 1, j + 1 if j < len(raw_lines) and bal_block != line else i + 1)
            continue

        txn_date, _ = date_at_row_start(line)
        if txn_date is None or not current_isin or not in_transaction_section:
            i += 1
            continue

        block, next_i = collect_dated_block(i)
        block_low = block.lower()

        if "opening balance" in block_low:
            opening = _balance_line(block, "opening balance")
            if opening is not None:
                last_balance[current_isin] = opening
                opening_balance_rows += 1
                if opening > 0:
                    opening_balance_nonzero += 1
            i = max(i + 1, next_i)
            continue
        if "closing balance" in block_low:
            closing = _balance_line(block, "closing balance")
            if closing is not None:
                last_balance[current_isin] = closing
            i = max(i + 1, next_i)
            continue

        is_credit = bool(DEPOSITORY_CREDIT_RE.search(block))
        is_debit = bool(DEPOSITORY_DEBIT_RE.search(block))
        if is_credit or is_debit:
            vals = transaction_numeric_values(block)
            if len(vals) >= 2:
                quantity = vals[-2]
                balance = vals[-1]
                previous = last_balance.get(current_isin)
                bal_err: Decimal | None = None
                if previous is not None:
                    expected = previous + quantity if is_credit else previous - quantity
                    bal_err = relative_reconciliation_error(expected, balance)
                confidence = Decimal("0.98") if bal_err is not None and bal_err <= Decimal("0.001") else Decimal("0.90")
                rows.append({
                    "file": filename,
                    "isin": current_isin,
                    "name": current_name or current_isin,
                    "folio": current_folio,
                    "txn_date": txn_date,
                    "txn_type": "DEMAT_CREDIT" if is_credit else "DEMAT_DEBIT",
                    "quantity_inferred": float(quantity),
                    "amount_inferred": None,
                    "stamp_duty_inferred": None,
                    "nav_inferred": None,
                    "price_inferred": None,
                    "cashflow_inferred": None,
                    "portfolio_cashflow_inferred": None,
                    "cashflow_scope": "UNAVAILABLE_IN_CAS",
                    "cashflow_status": "UNAVAILABLE_IN_CAS",
                    "transaction_source": "DEPOSITORY_QUANTITY_ONLY",
                    "balance_after_inferred": float(balance),
                    "balance_reconciliation_error_pct": None if bal_err is None else float(bal_err * Decimal("100")),
                    "transaction_confidence": float(confidence),
                    "source_line": block[:700],
                })
                last_balance[current_isin] = balance
            i = max(i + 1, next_i)
            continue

        txn_type = classify_transaction_line(block)
        if txn_type and not is_narrative_non_transaction(block):
            parsed_tail = _mf_monetary_tail(block)
            if parsed_tail is not None:
                amount = parsed_tail["amount"]
                stamp = parsed_tail["stamp_duty"]
                sign = transaction_cashflow_sign(txn_type)
                scheme_cashflow = amount * Decimal(sign) if sign else Decimal("0")

                # Whole-portfolio XIRR must contain only money crossing the investor /
                # portfolio boundary. Switches are internal transfers. Reversal rows are
                # retained for audit but are not promoted to external cashflows without
                # corroborating bank/broker evidence. Purchase stamp duty is included in
                # the external outflow because the statement prints it separately.
                if txn_type == "REVERSAL":
                    portfolio_cashflow = None
                    cashflow_scope = "REVIEW"
                    cashflow_status = "REVERSAL_REVIEW"
                elif txn_type in {"SWITCH_IN", "SWITCH_OUT"}:
                    portfolio_cashflow = Decimal("0")
                    cashflow_scope = "INTERNAL"
                    cashflow_status = "INTERNAL_TRANSFER"
                elif sign < 0:
                    portfolio_cashflow = -(amount + stamp)
                    cashflow_scope = "EXTERNAL"
                    cashflow_status = "AVAILABLE_EXTERNAL"
                elif sign > 0:
                    portfolio_cashflow = amount
                    cashflow_scope = "EXTERNAL"
                    cashflow_status = "AVAILABLE_EXTERNAL"
                else:
                    portfolio_cashflow = Decimal("0")
                    cashflow_scope = "INTERNAL"
                    cashflow_status = "NON_EXTERNAL_EVENT"

                rows.append({
                    "file": filename,
                    "isin": current_isin,
                    "name": current_name or current_isin,
                    "folio": current_folio,
                    "txn_date": txn_date,
                    "txn_type": txn_type,
                    "quantity_inferred": float(parsed_tail["units"]),
                    "amount_inferred": float(amount),
                    "stamp_duty_inferred": float(stamp),
                    "nav_inferred": float(parsed_tail["nav"]),
                    "price_inferred": float(parsed_tail["price"]),
                    "cashflow_inferred": float(scheme_cashflow),
                    "portfolio_cashflow_inferred": None if portfolio_cashflow is None else float(portfolio_cashflow),
                    "cashflow_scope": cashflow_scope,
                    "cashflow_status": cashflow_status,
                    "transaction_source": "MF_FOLIO_MONETARY",
                    "balance_after_inferred": None,
                    "balance_reconciliation_error_pct": float(parsed_tail["reconciliation_error"] * Decimal("100")),
                    "transaction_confidence": 0.99,
                    "source_line": block[:700],
                })
            else:
                malformed_monetary_rows += 1

        i = max(i + 1, next_i)

    deduped: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = (
            row.get("isin"), row.get("folio"), row.get("txn_date"), row.get("txn_type"),
            row.get("quantity_inferred"), row.get("amount_inferred"),
            " ".join(str(row.get("source_line", "")).split()).lower(),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    diagnostics = {
        "rows": len(deduped),
        "opening_balance_rows": opening_balance_rows,
        "opening_balance_nonzero": opening_balance_nonzero,
        "malformed_monetary_rows": malformed_monetary_rows,
        "history_complete": opening_balance_nonzero == 0,
    }
    return deduped, diagnostics


STRICT_NUM_RE = re.compile(
    # Plain numbers, western grouping (1,000,000), and Indian grouping
    # (1,00,000 / 36,16,119.95). Optional leading sign is supported.
    r"^[+-]?(?:"
    r"\d+(?:\.\d+)?"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d{1,3}(?:,\d{2})*,\d{3}(?:\.\d+)?"
    r")$"
)
SYMBOL_RE = re.compile(r"^[A-Z0-9&._+\-]+(?:\.NSE|\.BSE)$", re.IGNORECASE)

HOLDING_NOISE = {
    "ISIN", "Stock Symbol", "Company Name", "Face Value in  `",
    "No. of Shares", "Market Price in  `", "Value in  `",
    "ISIN Description", "No. of Units", "NAV in  `",
    "Issuer Name", "Coupon Rate/", "Frequency", "Maturity Date",
    "Face Value Per", "Unit in  `", "Market Price Per", "Unit in",
    "Equity Shares", "ACCOUNT HOLDER",
    "Summary", "Holdings", "Transactions", "Your Account", "About NSDL",
    "National Securities Depository Limited",
}

HOLDING_TERMINATORS = {
    "Sub Total", "Total", "Grand Total",
    "NSDL Demat Account", "CDSL Demat Account",
}


def strict_numeric_token(value: str) -> Decimal | None:
    value = value.strip()
    if not STRICT_NUM_RE.fullmatch(value):
        return None
    try:
        return D(value)
    except Exception:
        return None


def nsdl_date(value: str) -> date | None:
    value = value.strip()
    for fmt in (
        "%d-%b-%Y", "%d-%B-%Y",
        "%d/%m/%Y", "%d-%m-%Y",
        "%d/%m/%y", "%d-%m-%y",
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def extract_statement_period(text: str) -> tuple[date | None, date | None]:
    m = re.search(
        r"Statement\s+for\s+the\s+period\s+from\s+"
        r"(\d{1,2}-[A-Za-z]{3,9}-\d{4})\s+to\s+"
        r"(\d{1,2}-[A-Za-z]{3,9}-\d{4})",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    return nsdl_date(m.group(1)), nsdl_date(m.group(2))


def extract_holdings_as_of(text: str) -> date | None:
    m = re.search(
        r"Holdings\s+as\s+on\s+(\d{1,2}-[A-Za-z]{3,9}-\d{4})",
        text,
        re.IGNORECASE,
    )
    return nsdl_date(m.group(1)) if m else None


def extract_consolidated_portfolio_value(text: str) -> Decimal | None:
    m = re.search(
        r"YOUR\s+CONSOLIDATED\s+PORTFOLIO\s+VALUE\s*[`₹]?\s*"
        r"([\d,]+\.\d{2})",
        text,
        re.IGNORECASE,
    )
    return D(m.group(1)) if m else None


ASSET_COMPOSITION_LABELS = {
    "Equity": "Equities (E)",
    "Mutual Fund (Demat)": "Mutual Funds (M)",
    "SGB": "Sovereign Gold Bonds (SGB)",
    "Mutual Fund Folio": "Mutual Fund Folios (F)",
}


def extract_asset_composition(text: str) -> dict[str, Decimal]:
    """Read NSDL's own portfolio-composition totals for reconciliation."""
    out: dict[str, Decimal] = {}
    for asset_type, label in ASSET_COMPOSITION_LABELS.items():
        pattern = re.escape(label) + r"\\s+([\\d,]+\\.\\d{2})"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            out[asset_type] = D(m.group(1))
    return out


def asset_reconciliation_rows(
    holdings: pd.DataFrame,
    expected: dict[str, Decimal],
) -> list[dict[str, Any]]:
    rows = []
    for asset_type, expected_value in expected.items():
        if holdings is None or holdings.empty or "asset_type" not in holdings.columns:
            parsed = Decimal("0")
        else:
            sub = holdings[holdings["asset_type"] == asset_type]
            parsed = sum(
                (D(x) for x in sub.get("market_value_inferred", pd.Series(dtype=float)).dropna()),
                Decimal("0"),
            )
        diff = parsed - expected_value
        pct = (
            abs(diff) / expected_value * Decimal("100")
            if expected_value != 0 else Decimal("0")
        )
        rows.append(
            {
                "asset_type": asset_type,
                "statement_value": float(expected_value),
                "parsed_value": float(parsed),
                "difference": float(diff),
                "difference_pct": float(pct),
                "status": "PASS" if pct <= Decimal("0.50") else "REVIEW",
            }
        )
    return rows


def holdings_section_start(lines: list[str]) -> int:
    for i, line in enumerate(lines[:-1]):
        if line.strip().lower() == "holdings":
            nxt = lines[i + 1].strip().lower()
            if nxt.startswith("as on"):
                return i + 2
    # Fallback: start from first actual equity/MF table header.
    for i, line in enumerate(lines):
        if line in {"Equities (E)", "Mutual Funds (M)", "Sovereign Gold Bonds (SGB)"}:
            return i
    return 0


def holding_asset_state(line: str) -> str | None:
    normalized = " ".join(line.split()).strip()
    low = normalized.lower()

    if low.startswith("equities") and "(e)" in low:
        return "EQUITY"
    if low.startswith("mutual funds") and "(m)" in low:
        return "MF_DEMAT"
    if low.startswith("sovereign gold bonds") or "(sgb)" in low:
        return "SGB"
    if low.startswith("mutual fund folios"):
        return "MF_FOLIO"
    return None


def detect_account_name(lines: list[str], index: int, current: str | None) -> str | None:
    """
    NSDL and CDSL layouts place ACCOUNT HOLDER / broker name in different orders.
    This helper handles both:
      NSDL Demat Account -> ICICI BANK LIMITED -> ACCOUNT HOLDER
      NSDL Demat Account -> ACCOUNT HOLDER -> ICICI BANK LIMITED
    """
    line = " ".join(lines[index].split()).strip()

    if line not in {"NSDL Demat Account", "CDSL Demat Account"}:
        return current

    candidates = []
    for j in range(index + 1, min(len(lines), index + 6)):
        token = " ".join(lines[j].split()).strip()
        if not token:
            continue
        if token in {
            "ACCOUNT HOLDER", "Equities (E)", "Mutual Funds (M)",
            "Sovereign Gold Bonds (SGB)", "Mutual Fund Folios (F)",
        }:
            continue
        if token.startswith("DP ID") or token.startswith("Client ID"):
            continue
        if "PAN:" in token.upper():
            continue
        if token.startswith("Consolidated Account Statement"):
            continue
        if token.startswith("National Securities Depository Limited"):
            continue
        # Account names are normally textual and often contain LIMITED / SECURITIES / BANK.
        if not ISIN_RE.search(token) and strict_numeric_token(token) is None:
            candidates.append(token)

    if candidates:
        return candidates[0]
    return current


def holding_noise_line(line: str) -> bool:
    stripped = line.strip()
    if stripped in HOLDING_NOISE:
        return True
    if stripped.startswith("Consolidated Account Statement"):
        return True
    if stripped.startswith("National Securities Depository Limited"):
        return True
    if re.fullmatch(r"\d{1,3}", stripped):
        # Page number can appear by itself. Real quantities are retained later
        # inside a bounded ISIN record chunk before this filter is applied.
        return False
    return False


def numeric_tokens_with_index(chunk: list[str]) -> list[tuple[int, Decimal]]:
    out = []
    for idx, token in enumerate(chunk):
        value = strict_numeric_token(token)
        if value is not None:
            out.append((idx, value))
    return out


def relative_reconciliation_error(calculated: Decimal, reported: Decimal) -> Decimal:
    if reported == 0:
        return abs(calculated - reported)
    return abs(calculated - reported) / abs(reported)


def find_equity_quad(chunk: list[str]) -> tuple[list[tuple[int, Decimal]], Decimal] | None:
    """
    Find FaceValue, Quantity, MarketPrice, MarketValue such that
    Quantity × MarketPrice approximately equals MarketValue.
    """
    nums = numeric_tokens_with_index(chunk)
    best = None
    for start in range(max(0, len(nums) - 12), len(nums) - 3):
        seq = nums[start:start + 4]
        if len(seq) < 4:
            continue
        indices = [x[0] for x in seq]
        if max(indices[i + 1] - indices[i] for i in range(3)) > 2:
            continue

        face, qty, price, value = [x[1] for x in seq]
        if qty <= 0 or price <= 0 or value <= 0 or face < 0:
            continue

        err = relative_reconciliation_error(qty * price, value)
        if err <= Decimal("0.02"):
            if best is None or err < best[1]:
                best = (seq, err)
    return best


def find_value_triplet(chunk: list[str]) -> tuple[list[tuple[int, Decimal]], Decimal] | None:
    """
    Find Quantity, NAV/Price, MarketValue where Quantity × Price ≈ Value.
    Useful for demat mutual funds and many MF-folio layouts.
    """
    nums = numeric_tokens_with_index(chunk)
    best = None
    for start in range(max(0, len(nums) - 12), len(nums) - 2):
        seq = nums[start:start + 3]
        if len(seq) < 3:
            continue
        indices = [x[0] for x in seq]
        if max(indices[i + 1] - indices[i] for i in range(2)) > 2:
            continue

        qty, price, value = [x[1] for x in seq]
        if qty <= 0 or price <= 0 or value <= 0:
            continue

        err = relative_reconciliation_error(qty * price, value)
        if err <= Decimal("0.02"):
            if best is None or err < best[1]:
                best = (seq, err)
    return best


def find_cdsl_balance_values(
    chunk: list[str],
) -> tuple[Decimal, Decimal, Decimal, Decimal, int] | None:
    """
    Recover a CDSL balance-table holding from a noisy PDF text chunk.

    Canonical CDSL rows contain 11 numeric fields:
      Current Bal, Free Bal, Lent, Safekeep, Locked, Pledge Setup,
      Pledged, Earmarked, Pledgee, Market Price, Market Value.

    The parser scans for the arithmetic identity Current Balance x Market Price
    ~= Market Value and strongly prefers the canonical 11-field spacing.
    The returned index marks the true balance start so description numbers are
    retained in the security name.
    """
    nums = numeric_tokens_with_index(chunk)
    if len(nums) < 3:
        return None

    candidates: list[
        tuple[tuple[int, Decimal], Decimal, Decimal, Decimal, Decimal, int]
    ] = []

    for start in range(0, max(0, len(nums) - 10)):
        seq = nums[start:start + 11]
        if len(seq) != 11:
            continue
        qty = abs(seq[0][1])
        market_price = abs(seq[9][1])
        market_value = abs(seq[10][1])
        if qty <= 0 or market_price <= 0 or market_value <= 0:
            continue
        err = relative_reconciliation_error(qty * market_price, market_value)
        if err <= Decimal("0.02"):
            candidates.append(
                ((0, err), qty, market_price, market_value, err, seq[0][0])
            )

    for qpos in range(len(nums)):
        qty = abs(nums[qpos][1])
        if qty <= 0:
            continue
        for ppos in range(qpos + 2, min(len(nums) - 1, qpos + 12)):
            market_price = abs(nums[ppos][1])
            market_value = abs(nums[ppos + 1][1])
            if market_price <= 0 or market_value <= 0:
                continue
            err = relative_reconciliation_error(qty * market_price, market_value)
            if err > Decimal("0.02"):
                continue
            spacing_penalty = abs((ppos - qpos) - 9)
            candidates.append(
                (
                    (spacing_penalty + 1, err),
                    qty,
                    market_price,
                    market_value,
                    err,
                    nums[qpos][0],
                )
            )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, qty, market_price, market_value, err, first_balance_idx = candidates[0]
    return qty, market_price, market_value, err, first_balance_idx


def find_mf_folio_values(
    chunk: list[str],
) -> dict[str, Decimal] | None:
    """
    Mutual Fund Folio table layout:
      ... Folio No, Units, Average Cost/Unit, Total Cost,
          Current NAV, Current Value, Unrealised Profit/(Loss)

    Folio number may itself be numeric, so valuation is identified from the six
    right-most numeric fields and validated using both cost and current-value
    arithmetic.
    """
    nums = numeric_tokens_with_index(chunk)
    if len(nums) < 6:
        return None

    # Search from the right because folio numbers/UCCs can also be numeric.
    for end in range(len(nums), 5, -1):
        seq = nums[end - 6:end]
        qty, avg_cost, total_cost, current_nav, current_value, unrealised = [
            x[1] for x in seq
        ]

        qty = abs(qty)
        avg_cost = abs(avg_cost)
        total_cost = abs(total_cost)
        current_nav = abs(current_nav)
        current_value = abs(current_value)

        if min(qty, avg_cost, total_cost, current_nav, current_value) <= 0:
            continue

        cost_err = relative_reconciliation_error(qty * avg_cost, total_cost)
        value_err = relative_reconciliation_error(qty * current_nav, current_value)
        pnl_err = relative_reconciliation_error(
            current_value - total_cost,
            unrealised,
        ) if unrealised != 0 else Decimal("0")

        if cost_err <= Decimal("0.03") and value_err <= Decimal("0.03"):
            return {
                "quantity": qty,
                "average_cost": avg_cost,
                "total_cost": total_cost,
                "current_nav": current_nav,
                "current_value": current_value,
                "unrealised_pl": unrealised,
                "cost_error": cost_err,
                "value_error": value_err,
                "pnl_error": pnl_err,
                "first_numeric_index": seq[0][0],
            }

    return None


def find_sgb_values(
    chunk: list[str],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
    """
    Parse current and historical Sovereign Gold Bond layouts.

    Preferred/current layout after the maturity date:
      units, face value/unit, market price/unit, market value

    Some older CAS statements expose only:
      units, face value/unit, reported value

    For that historical layout, accept the row only when units x face value
    reconciles to the reported value within the existing 2% arithmetic gate.
    The face value is then used as the effective valuation price solely because
    that is the valuation basis printed by the historical statement; no market
    price is invented.
    """
    maturity_idx = next(
        (i for i, token in enumerate(chunk) if nsdl_date(token) is not None),
        None,
    )
    if maturity_idx is None:
        return None

    nums = [
        (i, value)
        for i, value in numeric_tokens_with_index(chunk)
        if i > maturity_idx
    ]

    candidates: list[
        tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal]
    ] = []

    # Preferred modern layout: units, face value, market price, market value.
    for start in range(len(nums) - 3):
        seq = nums[start:start + 4]
        units, face, market_price, market_value = [x[1] for x in seq]
        if units <= 0 or face <= 0 or market_price <= 0 or market_value <= 0:
            continue
        err = relative_reconciliation_error(units * market_price, market_value)
        if err <= Decimal("0.02"):
            candidates.append((0, err, units, face, market_price, market_value))

    # Historical fallback: units, face value, reported value, with no separate
    # market-price field. Require the reported value to reconcile to face value.
    for start in range(len(nums) - 2):
        seq = nums[start:start + 3]
        units, face, market_value = [x[1] for x in seq]
        if units <= 0 or face <= 0 or market_value <= 0:
            continue
        err = relative_reconciliation_error(units * face, market_value)
        if err <= Decimal("0.02"):
            candidates.append((1, err, units, face, face, market_value))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    _, err, units, face, market_price, market_value = candidates[0]
    return units, face, market_price, market_value, err


def clean_record_chunk(chunk: list[str]) -> list[str]:
    out = []
    for token in chunk:
        token = token.strip()
        if not token:
            continue
        if token in HOLDING_NOISE:
            continue
        if token.startswith("Consolidated Account Statement"):
            continue
        if token.startswith("National Securities Depository Limited"):
            continue
        # Page navigation repetition.
        if token in {"Summary", "Holdings", "Transactions", "Your Account", "About NSDL"}:
            continue
        out.append(token)
    return out


def parse_nsdl_holdings_layout(lines: list[str], filename: str) -> list[dict[str, Any]]:
    """
    Parse NSDL CAS holdings across three observed layouts:
      1) NSDL demat equity / demat MF vertical tables.
      2) CDSL balance tables (e.g. Zerodha), with many balance columns.
      3) Mutual Fund Folio valuation tables with cost + current NAV/value.

    Every accepted row is arithmetic-validated before entering analytics.
    v1.1.0 adds inline-ISIN, CDSL numeric-tail, historical SGB, and structured transaction recovery.
    """
    holdings: list[dict[str, Any]] = []
    start_idx = holdings_section_start(lines)
    current_asset: str | None = None
    current_account: str | None = None

    i = start_idx
    while i < len(lines):
        line = lines[i].strip()
        normalized = " ".join(line.split()).strip()

        # Account context may appear before or after ACCOUNT HOLDER.
        current_account = detect_account_name(lines, i, current_account)

        state = holding_asset_state(normalized)
        if state:
            current_asset = state
            if state == "MF_FOLIO":
                current_account = "Mutual Fund Folios"

        if normalized == "ACCOUNT HOLDER":
            # Look both backward and forward for account name.
            back_candidates = []
            for k in range(max(start_idx, i - 3), i):
                token = " ".join(lines[k].split()).strip()
                if (
                    token
                    and token not in HOLDING_NOISE
                    and token not in {"NSDL Demat Account", "CDSL Demat Account"}
                    and not token.startswith("DP ID")
                    and "PAN:" not in token.upper()
                    and strict_numeric_token(token) is None
                ):
                    back_candidates.append(token)
            if back_candidates and back_candidates[-1].upper() not in {"TOTAL", "SUB TOTAL", "GRAND TOTAL"}:
                current_account = back_candidates[-1]
            elif i + 1 < len(lines):
                candidate = " ".join(lines[i + 1].split()).strip()
                if candidate and candidate not in HOLDING_NOISE and "PAN:" not in candidate.upper():
                    current_account = candidate

        isin, inline_tail = split_isin_line(normalized)
        if not isin:
            i += 1
            continue

        # Some CDSL PDF text layers emit ISIN and security description on one line.
        chunk: list[str] = [inline_tail] if inline_tail else []
        j = i + 1

        while j < len(lines) and len(chunk) < 45:
            token = lines[j].strip()
            norm_token = " ".join(token.split()).strip()
            if split_isin_line(norm_token)[0]:
                break
            if norm_token in HOLDING_TERMINATORS:
                break
            if holding_asset_state(norm_token):
                break
            # Stop parsing holdings when transactions section begins.
            if norm_token.lower().startswith("transactions for the period"):
                break
            chunk.append(token)
            j += 1

        chunk = clean_record_chunk(chunk)
        asset = current_asset

        symbol_candidate = next((x for x in chunk if SYMBOL_RE.fullmatch(x)), None)
        if asset is None:
            if isin.startswith("INF"):
                asset = "MF_DEMAT"
            elif symbol_candidate:
                asset = "EQUITY"
            elif isin.startswith("IN00"):
                asset = "SGB"

        row: dict[str, Any] | None = None

        # -----------------------------------------------------------------
        # Mutual Fund Folios: use current NAV/current value, not total cost.
        # -----------------------------------------------------------------
        if asset == "MF_FOLIO":
            folio = find_mf_folio_values(chunk)
            if folio:
                first_numeric_idx = int(folio["first_numeric_index"])
                prefix = chunk[:first_numeric_idx]

                # UCC typically immediately follows ISIN; scheme name follows UCC.
                # Folio number is numeric and therefore excluded from name text.
                textual = [
                    x for x in prefix
                    if strict_numeric_token(x) is None
                    and nsdl_date(x) is None
                    and x.upper() not in {"NOT AVAILABLE", "0"}
                ]
                if textual and re.fullmatch(r"[A-Z0-9/_-]{4,}", textual[0] or ""):
                    textual = textual[1:]
                name = " ".join(textual).strip()

                if name:
                    row = {
                        "file": filename,
                        "account": "Mutual Fund Folios",
                        "asset_type": "Mutual Fund Folio",
                        "isin": isin,
                        "symbol": None,
                        "name": name,
                        "face_value": None,
                        "quantity_inferred": float(folio["quantity"]),
                        "average_cost_inferred": float(folio["average_cost"]),
                        "total_cost_inferred": float(folio["total_cost"]),
                        "market_price_inferred": float(folio["current_nav"]),
                        "market_value_inferred": float(folio["current_value"]),
                        "unrealised_pl_inferred": float(folio["unrealised_pl"]),
                        "reconciliation_error_pct": float(
                            folio["value_error"] * Decimal("100")
                        ),
                        "line_confidence": 0.99,
                        "source_block": " | ".join(chunk[:24]),
                    }

        # -----------------------------------------------------------------
        # Sovereign Gold Bonds
        # -----------------------------------------------------------------
        elif asset == "SGB" or isin.startswith("IN00"):
            values = find_sgb_values(chunk)
            if values:
                units, face, market_price, market_value, err = values
                maturity_idx = next(
                    (k for k, x in enumerate(chunk) if nsdl_date(x) is not None),
                    None,
                )
                description_parts = []
                if maturity_idx is not None:
                    for token in chunk[:maturity_idx]:
                        if strict_numeric_token(token) is None and nsdl_date(token) is None:
                            description_parts.append(token)
                name = " ".join(description_parts).strip() or "Sovereign Gold Bond"

                row = {
                    "file": filename,
                    "account": current_account,
                    "asset_type": "SGB",
                    "isin": isin,
                    "symbol": None,
                    "name": name,
                    "face_value": float(face),
                    "quantity_inferred": float(units),
                    "market_price_inferred": float(market_price),
                    "market_value_inferred": float(market_value),
                    "reconciliation_error_pct": float(err * Decimal("100")),
                    "line_confidence": 0.99 if err <= Decimal("0.002") else 0.95,
                    "source_block": " | ".join(chunk[:18]),
                }

        # -----------------------------------------------------------------
        # Equity. First try NSDL 4-column row; then CDSL balance-table row.
        # -----------------------------------------------------------------
        elif asset == "EQUITY" or symbol_candidate:
            standard = find_equity_quad(chunk)
            cdsl = find_cdsl_balance_values(chunk)

            if standard:
                seq, err = standard
                face, qty, market_price, market_value = [x[1] for x in seq]
                first_numeric_idx = seq[0][0]

                symbol = symbol_candidate
                symbol_idx = chunk.index(symbol) if symbol in chunk else -1
                name_candidates = [
                    x for idx, x in enumerate(chunk[:first_numeric_idx])
                    if idx != symbol_idx
                    and strict_numeric_token(x) is None
                    and nsdl_date(x) is None
                ]
                name = " ".join(name_candidates).strip()

                if name and name.upper() != "ISIN":
                    row = {
                        "file": filename,
                        "account": current_account,
                        "asset_type": "Equity",
                        "isin": isin,
                        "symbol": symbol,
                        "name": name,
                        "face_value": float(face),
                        "quantity_inferred": float(qty),
                        "market_price_inferred": float(market_price),
                        "market_value_inferred": float(market_value),
                        "reconciliation_error_pct": float(err * Decimal("100")),
                        "line_confidence": 0.99 if err <= Decimal("0.001") else 0.95,
                        "source_block": " | ".join(chunk[:18]),
                    }

            elif cdsl:
                qty, market_price, market_value, err, first_numeric_idx = cdsl

                # Use the detected balance-block start rather than the first numeric
                # token, because face-value numbers may occur inside the description.
                name = " ".join(
                    x for x in chunk[:first_numeric_idx]
                    if strict_numeric_token(x) is None and nsdl_date(x) is None
                ).strip()

                if name:
                    row = {
                        "file": filename,
                        "account": current_account,
                        "asset_type": "Equity",
                        "isin": isin,
                        "symbol": None,
                        "name": name,
                        "face_value": None,
                        "quantity_inferred": float(qty),
                        "market_price_inferred": float(market_price),
                        "market_value_inferred": float(market_value),
                        "reconciliation_error_pct": float(err * Decimal("100")),
                        "line_confidence": 0.98 if err <= Decimal("0.002") else 0.94,
                        "source_block": " | ".join(chunk[:24]),
                    }

        # -----------------------------------------------------------------
        # Demat Mutual Funds. CDSL balance tables are common for Zerodha;
        # NSDL demat MF uses Units/NAV/Value triplet.
        # -----------------------------------------------------------------
        elif asset == "MF_DEMAT" or isin.startswith("INF"):
            cdsl = find_cdsl_balance_values(chunk)
            standard = find_value_triplet(chunk)

            if cdsl:
                qty, nav, market_value, err, first_numeric_idx = cdsl
                name = " ".join(
                    x for x in chunk[:first_numeric_idx]
                    if strict_numeric_token(x) is None
                    and nsdl_date(x) is None
                    and x.upper() not in {"NOT AVAILABLE"}
                ).strip()

                if name:
                    row = {
                        "file": filename,
                        "account": current_account,
                        "asset_type": "Mutual Fund (Demat)",
                        "isin": isin,
                        "symbol": None,
                        "name": name,
                        "face_value": None,
                        "quantity_inferred": float(qty),
                        "market_price_inferred": float(nav),
                        "market_value_inferred": float(market_value),
                        "reconciliation_error_pct": float(err * Decimal("100")),
                        "line_confidence": 0.98 if err <= Decimal("0.002") else 0.94,
                        "source_block": " | ".join(chunk[:24]),
                    }

            elif standard:
                seq, err = standard
                qty, nav, market_value = [x[1] for x in seq]
                first_numeric_idx = seq[0][0]
                description_parts = [
                    x for x in chunk[:first_numeric_idx]
                    if strict_numeric_token(x) is None
                    and nsdl_date(x) is None
                    and not x.lower().startswith("folio")
                ]
                name = " ".join(description_parts).strip()

                if name and name.upper() != "ISIN":
                    row = {
                        "file": filename,
                        "account": current_account,
                        "asset_type": "Mutual Fund (Demat)",
                        "isin": isin,
                        "symbol": None,
                        "name": name,
                        "face_value": None,
                        "quantity_inferred": float(qty),
                        "market_price_inferred": float(nav),
                        "market_value_inferred": float(market_value),
                        "reconciliation_error_pct": float(err * Decimal("100")),
                        "line_confidence": 0.98 if err <= Decimal("0.002") else 0.94,
                        "source_block": " | ".join(chunk[:18]),
                    }

        if row is not None:
            holdings.append(row)

        i += 1

    # Exact duplicate rows may appear because PDF text layers repeat headers/records.
    if holdings:
        deduped = []
        seen = set()
        for row in holdings:
            key = (
                row.get("account"),
                row.get("asset_type"),
                row.get("isin"),
                row.get("quantity_inferred"),
                row.get("market_price_inferred"),
                row.get("market_value_inferred"),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(row)
        holdings = deduped

    return holdings


def holding_quality_report(
    holdings: pd.DataFrame,
    statement_total: Decimal | None,
) -> dict[str, Any]:
    if holdings is None or holdings.empty:
        return {
            "valid": False,
            "rows": 0,
            "parsed_value": Decimal("0"),
            "statement_value": statement_total,
            "difference": statement_total,
            "difference_pct": None,
            "bad_name_rows": 0,
            "low_confidence_rows": 0,
            "reason": "No validated holdings rows were parsed.",
        }

    values = pd.to_numeric(
        holdings.get("market_value_inferred", pd.Series(dtype=float)),
        errors="coerce",
    ).fillna(0)
    parsed_value = D(values.sum())

    names = holdings.get("name", pd.Series([""] * len(holdings))).fillna("").astype(str)
    bad_name_rows = int(
        (
            names.str.strip().str.upper().isin({"", "ISIN", "N/A", "NONE"})
            | (names.str.strip().str.len() < 3)
        ).sum()
    )

    confidence = pd.to_numeric(
        holdings.get("line_confidence", pd.Series([0] * len(holdings))),
        errors="coerce",
    ).fillna(0)
    low_confidence_rows = int((confidence < 0.90).sum())

    difference = None
    difference_pct = None
    total_ok = True
    if statement_total is not None and statement_total > 0:
        difference = parsed_value - statement_total
        difference_pct = abs(difference) / statement_total * Decimal("100")
        # Full holdings reconstruction should reconcile very closely to the CAS total.
        total_ok = difference_pct <= Decimal("0.50")

    valid = (
        len(holdings) > 0
        and bad_name_rows == 0
        and low_confidence_rows == 0
        and total_ok
    )

    reasons = []
    if bad_name_rows:
        reasons.append(f"{bad_name_rows} invalid-name row(s)")
    if low_confidence_rows:
        reasons.append(f"{low_confidence_rows} low-confidence row(s)")
    if difference_pct is not None and not total_ok:
        reasons.append(
            f"parsed holdings differ from CAS total by {float(difference_pct):.2f}%"
        )

    return {
        "valid": valid,
        "rows": len(holdings),
        "parsed_value": parsed_value,
        "statement_value": statement_total,
        "difference": difference,
        "difference_pct": difference_pct,
        "bad_name_rows": bad_name_rows,
        "low_confidence_rows": low_confidence_rows,
        "reason": "Holdings quality gate passed." if valid else "; ".join(reasons),
    }


def parse_cas_pdf(
    pdf_bytes: bytes,
    filename: str,
    password: str = "",
) -> dict[str, Any]:
    text, page_count, encrypted = extract_pdf_text(pdf_bytes, password=password)
    fingerprint = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    transactions = []
    warnings = []
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Holdings are parsed from the actual NSDL vertical table layout and validated
    # using Quantity × Price/NAV ≈ Market Value.
    holdings = parse_nsdl_holdings_layout(raw_lines, filename)
    holdings_df = pd.DataFrame(holdings)

    period_start, period_end = extract_statement_period(text)
    holdings_as_of = extract_holdings_as_of(text)
    statement_total = extract_consolidated_portfolio_value(text)
    holdings_quality = holding_quality_report(holdings_df, statement_total)
    asset_composition = extract_asset_composition(text)
    asset_reconciliation = asset_reconciliation_rows(
        holdings_df,
        asset_composition,
    )

    # Use parsed holdings names to preserve security context while scanning
    # transaction sections later in the statement.
    holding_name_map = {}
    if not holdings_df.empty:
        for _, hrow in holdings_df.iterrows():
            holding_name_map.setdefault(str(hrow.get("isin")), str(hrow.get("name")))

    transactions, transaction_diagnostics = parse_cas_transactions_layout(
        raw_lines,
        filename,
        holding_name_map,
    )

    confidence = Decimal("0.20")
    if holdings:
        confidence = Decimal("0.92") if holdings_quality["valid"] else Decimal("0.75")

    if not holdings:
        warnings.append(
            "No validated holdings rows were parsed from the NSDL holdings tables."
        )
    elif not holdings_quality["valid"]:
        warnings.append(
            "Holdings quality gate did not fully reconcile: "
            + holdings_quality["reason"]
        )

    if not transactions:
        warnings.append(
            "No structured transaction rows were reconstructed from this CAS."
        )
    else:
        quantity_only = sum(
            1 for row in transactions
            if row.get("transaction_source") == "DEPOSITORY_QUANTITY_ONLY"
        )
        if quantity_only:
            warnings.append(
                f"{quantity_only} depository transaction row(s) contain quantity/balance only. "
                "CAS does not disclose their trade consideration, so full-portfolio automated XIRR "
                "must remain blocked unless broker tradebook/ledger cashflows are supplied."
            )
    if "NSDL" not in text.upper():
        warnings.append("The document text did not contain 'NSDL'; verify statement source.")

    return {
        "filename": filename,
        "pages": page_count,
        "encrypted_pdf": encrypted,
        "fingerprint": fingerprint,
        "period_start": period_start,
        "period_end": period_end,
        "holdings_as_of": holdings_as_of,
        "consolidated_portfolio_value": (
            None if statement_total is None else float(statement_total)
        ),
        "parse_confidence": float(confidence),
        "asset_composition": {
            k: float(v) for k, v in asset_composition.items()
        },
        "asset_reconciliation": asset_reconciliation,
        "holdings_quality": {
            **holdings_quality,
            "parsed_value": float(holdings_quality["parsed_value"]),
            "statement_value": (
                None if holdings_quality["statement_value"] is None
                else float(holdings_quality["statement_value"])
            ),
            "difference": (
                None if holdings_quality["difference"] is None
                else float(holdings_quality["difference"])
            ),
            "difference_pct": (
                None if holdings_quality["difference_pct"] is None
                else float(holdings_quality["difference_pct"])
            ),
        },
        "warnings": warnings,
        "holdings": holdings_df,
        "transactions": pd.DataFrame(transactions),
        "transaction_diagnostics": transaction_diagnostics,
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
    "Universe": "shareholding_history/data/_universe.csv",
}

SYMBOL_RENAMES_PATH = "index_history/data/manual_overrides/symbol_renames.json"


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_upstream_csv(relative_path: str) -> pd.DataFrame:
    url = f"{UPSTREAM_REPO}/{relative_path}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return pd.read_csv(io.BytesIO(response.content))


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_symbol_renames() -> pd.DataFrame:
    url = f"{UPSTREAM_REPO}/{SYMBOL_RENAMES_PATH}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        # Supports either {old:new} or richer dict structures.
        rows = []
        for old, new in payload.items():
            if isinstance(new, dict):
                row = {"old_symbol": old, **new}
            else:
                row = {"old_symbol": old, "new_symbol": new}
            rows.append(row)
        return pd.DataFrame(rows)

    if isinstance(payload, list):
        return pd.DataFrame(payload)

    return pd.DataFrame()


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
# Zerodha workbook import and evidence-only reconciliation
# -----------------------------------------------------------------------------

def broker_number(value: Any) -> Decimal:
    """Reject missing/non-finite amounts rather than silently turning them into zero."""
    result = Decimal(str(value).strip().replace(",", ""))
    if not result.is_finite():
        raise ValueError("Missing or non-finite number")
    return result


def broker_date(value: Any) -> date:
    """Excel dates or explicit ISO/Indian date strings; never interpret numbers as timestamps."""
    if pd.isna(value):
        raise ValueError("Missing date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                pass
    raise ValueError("Unsupported date; expected an Excel date, YYYY-MM-DD or DD/MM/YYYY")


def broker_report_blocks(raw: pd.DataFrame) -> list[pd.DataFrame]:
    """Combined exports repeat report titles, summaries and tables within a sheet."""
    starts = []
    for index, (_, row) in enumerate(raw.iterrows()):
        if any(re.search(r"^(?:P&L Statement for|Other Debits and Credits for).*from \d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}", str(v).strip()) for v in row if pd.notna(v)):
            starts.append(index)
    if not starts:
        return [raw]
    return [raw.iloc[start:end] for start, end in zip(starts, starts[1:] + [len(raw)])]


def import_zerodha_workbook(content: bytes, filename: str) -> dict[str, Any]:
    """Read supported layouts by header signature; retain workbook/sheet/Excel row."""
    tables = []
    issues = []
    digest = hashlib.sha256(content).hexdigest()
    account_ids = set()
    signatures = [
        ("trades", {"symbol", "isin", "trade_date", "trade_type", "quantity", "price", "trade_id", "exchange", "segment"}),
        ("holdings", {"isin", "quantity_available", "previous_closing_price"}),
        ("ledger", {"particulars", "posting_date", "debit", "credit", "net_balance", "voucher_type"}),
        ("dividends", {"symbol", "ex_date", "qty", "dividend_per_share", "total_dividend"}),
        ("pnl", {"isin", "buy_value", "sell_value", "realized_p_l"}),
        ("pnl_debits", {"particulars", "posting_date", "debit", "credit"}),
    ]
    for sheet, sheet_data in pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl", dtype=object).items():
        for values in sheet_data.itertuples(index=False, name=None):
            for position, value in enumerate(values):
                if str(value).strip().lower() == "client id":
                    following = [str(v).strip().upper() for v in values[position + 1:] if pd.notna(v) and str(v).strip()]
                    if following:
                        account_ids.add(following[0])
        for raw in broker_report_blocks(sheet_data):
            found = None
            for index, (_, row) in enumerate(raw.iterrows()):
                names = [re.sub(r"[^a-z0-9]+", "_", str(v).strip().lower()).strip("_") if pd.notna(v) else "" for v in row]
                for kind, required in signatures:
                    if required <= set(names):
                        found = (index, names, kind)
                        break
                if found:
                    break
            if not found:
                issues.append(f"{filename} / {sheet}: unsupported sheet; no rows imported")
                continue
            index, names, kind = found
            positions = [i for i, name in enumerate(names) if name]
            if len({names[i] for i in positions}) != len(positions):
                raise ValueError(f"{filename} / {sheet}: duplicate column names")
            frame = raw.iloc[index + 1:, positions].copy()
            frame.columns = [names[i] for i in positions]
            frame = frame.dropna(how="all")
            frame["source_row"] = frame.index + 1
            frame["source_file"] = filename
            frame["source_sheet"] = sheet
            frame["source_sha256"] = digest
            frame["asset_class"] = "MF" if sheet.strip().lower() == "mutual funds" else "Equity"
            preamble = " ".join(str(v) for v in raw.iloc[:index].values.ravel() if pd.notna(v))
            snapshot = re.search(r"as on (\d{4}-\d{2}-\d{2})", preamble, re.I)
            period = re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", preamble, re.I)
            frame["report_period_start"] = period[1] if period else None
            frame["report_period_end"] = period[2] if period else None
            tables.append({"kind": kind, "data": frame, "as_of": date.fromisoformat(snapshot[1]) if snapshot else None,
                           "period": (period[1], period[2]) if period else None,
                           "header_row": int(raw.index[index]) + 1, "columns": list(frame.columns)})
    return {"filename": filename, "sha256": digest, "tables": tables, "issues": issues, "account_ids": sorted(account_ids)}


def reconcile_zerodha(workbooks: list[dict[str, Any]]) -> dict[str, Any]:
    """No inferred corporate actions and no automatic XIRR unlock from partial evidence."""
    account_ids = {account for book in workbooks for account in book.get("account_ids", [])}
    if len(account_ids) > 1:
        raise ValueError("Multiple broker Client IDs detected; upload reports for one account only")
    issues, rows, inventory = [], {}, []
    unidentified = [book["filename"] for book in workbooks if not book.get("account_ids")]
    if unidentified:
        issues.append("Account identity absent from report headers: " + ", ".join(unidentified) + "; verify account scope manually")
    seen_files = set()
    snapshots = set()
    seen_reference_tables = {}
    seen_reference_periods = set()
    for book in workbooks:
        if book["sha256"] in seen_files:
            issues.append(f"{book['filename']}: duplicate workbook ignored")
            continue
        seen_files.add(book["sha256"])
        issues.extend(book["issues"])
        for table in book["tables"]:
            kind, frame = table["kind"], table["data"]
            inventory.append({"file": book["filename"], "sheet": frame["source_sheet"].iloc[0] if len(frame) else "",
                              "kind": kind, "rows": len(frame), "header_row": table.get("header_row"),
                              "normalized_columns": ", ".join(table.get("columns", [])), "as_of": table["as_of"], "period": " to ".join(table["period"]) if table["period"] else "Not stated"})
            if kind in ("pnl", "pnl_debits") and not frame.empty:
                columns = sorted(c for c in frame.columns if not c.startswith("source_"))
                def reference_value(value: Any) -> str:
                    if pd.isna(value):
                        return ""
                    if isinstance(value, (int, float, Decimal)):
                        return str(broker_number(value).normalize())
                    return str(value).strip()
                payload = sorted(tuple(reference_value(row[c]) for c in columns) for row in frame.to_dict("records"))
                table_key = (kind, table["period"], tuple(columns), tuple(payload))
                if table_key in seen_reference_tables:
                    inventory[-1]["status"] = "Identical reference section ignored"
                    issues.append(f"{book['filename']}: identical {kind} section for {table['period']} ignored; already supplied by {seen_reference_tables[table_key]}")
                    continue
                seen_reference_tables[table_key] = book["filename"]
                period_key = (kind, table["period"])
                if period_key in seen_reference_periods:
                    issues.append(f"{kind}: differing sections cover the same period {table['period']}; retained for review, do not sum")
                seen_reference_periods.add(period_key)
            inventory[-1]["status"] = "Imported"
            rows.setdefault(kind, []).append(frame)
            if kind == "holdings":
                snapshots.add(table["as_of"])
    tables = {kind: pd.concat(parts, ignore_index=True) for kind, parts in rows.items()}
    if "pnl" in tables:
        reference = tables["pnl"].copy()
        reference["reported_isin"] = reference["isin"]
        reference["identifier_source"] = "isin"
        for index, row in reference.iterrows():
            if pd.isna(row["isin"]) and re.fullmatch(r"IN[A-Z0-9]{10}", str(row.get("symbol", "")).strip().upper()):
                reference.loc[index, "isin"] = str(row["symbol"]).strip().upper()
                reference.loc[index, "identifier_source"] = "symbol (explicit ISIN)"
        count = int((reference["identifier_source"] != "isin").sum())
        if count:
            issues.append(f"{count} P&L reference rows use an explicit ISIN from the symbol field; review their open quantities and valuations")
        tables["pnl"] = reference
    pnl_periods = sorted({(r["report_period_start"], r["report_period_end"]) for r in tables.get("pnl", pd.DataFrame()).to_dict("records") if r["report_period_start"] and r["report_period_end"]})
    seen_pnl_periods = set()
    for part in rows.get("pnl", []):
        if part.empty:
            continue
        period_key = (part.iloc[0]["report_period_start"], part.iloc[0]["report_period_end"])
        if period_key in seen_pnl_periods:
            issues.append("Repeated equity P&L period: upload a corrected report instead of both original and corrected versions")
        seen_pnl_periods.add(period_key)
    for earlier, later in zip(pnl_periods, pnl_periods[1:]):
        if date.fromisoformat(later[0]) > date.fromisoformat(earlier[1]) + timedelta(days=1):
            issues.append(f"Equity P&L coverage gap after {earlier[1]} and before {later[0]}")
        elif later[0] <= earlier[1]:
            issues.append("Equity P&L periods overlap; do not sum overlapping reports")
    for kind in ("trades", "holdings", "ledger", "dividends", "pnl"):
        if kind not in tables:
            issues.append(f"Missing {kind} input")
    as_of = next(iter(snapshots)) if len(snapshots) == 1 else None
    if as_of is None:
        issues.append("Holdings require one explicit common snapshot date")
    trades, holdings, rejected = [], [], []
    trade_keys = {}
    conflicting_trade_keys = set()

    def reject(row: dict, reason: str) -> None:
        rejected.append({**row, "reason": reason})

    for row in tables.get("trades", pd.DataFrame()).to_dict("records"):
        try:
            segment = str(row.get("segment", "")).strip().upper()
            symbol = str(row.get("symbol", "")).strip().upper()
            reported_isin = str(row.get("isin", "")).strip().upper()
            is_derivative = segment in ("FO", "F&O", "NFO")
            if is_derivative:
                if not symbol or symbol in ("NAN", "NONE"):
                    raise ValueError("Derivative trade missing instrument symbol")
                # Exchange-traded derivatives commonly have no equity ISIN. Preserve an
                # auditable instrument identity without fabricating an ISIN.
                isin = "DERIV:" + symbol
                row = {**row, "asset_class": "Derivative"}
            else:
                isin = reported_isin
                if not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
                    raise ValueError("Invalid ISIN")
            side = str(row["trade_type"]).strip().lower()
            qty, price = broker_number(row["quantity"]), broker_number(row["price"])
            when = broker_date(row["trade_date"])
            expected_segment = "MF" if row["asset_class"] == "MF" else ("FO" if is_derivative else "EQ")
            if segment not in ((expected_segment,) if expected_segment != "FO" else ("FO", "F&O", "NFO")):
                raise ValueError("Unsupported trade segment for this sheet")
            if pd.isna(when) or side not in ("buy", "sell") or qty <= 0 or price <= 0:
                raise ValueError("Invalid trade date, side, quantity or price")
            start, end = row.get("report_period_start"), row.get("report_period_end")
            if start and end and not date.fromisoformat(start) <= when <= date.fromisoformat(end):
                raise ValueError("Trade date outside stated report period")
            identity = tuple(str(row[k]).strip() for k in ("exchange", "segment", "trade_id"))
            if any(v in ("", "nan", "None") for v in identity):
                raise ValueError("Missing trade identity")
            key = (when, *identity)
            payload = (isin, side, qty, price, str(row.get("order_id", "")))
            if key in trade_keys:
                if trade_keys[key] != payload or key in conflicting_trade_keys:
                    conflicting_trade_keys.add(key)
                    # Neither version is authoritative when a trade identity conflicts.
                    retained = []
                    for accepted in trades:
                        accepted_key = (accepted["trade_date"], *(str(accepted[k]).strip() for k in ("exchange", "segment", "trade_id")))
                        if accepted_key == key:
                            reject(accepted, "Conflicting trade identity")
                        else:
                            retained.append(accepted)
                    trades = retained
                    raise ValueError("Conflicting trade identity")
                raise ValueError("Duplicate trade identity")
            trade_keys[key] = payload
            row = {**row, "isin": isin, "trade_date": when, "quantity": qty, "price": price,
                   "signed_quantity": qty if side == "buy" else -qty, "gross_consideration": qty * price}
            if as_of is None or when > as_of:
                raise ValueError("Trade cannot be aligned to holdings snapshot")
            trades.append(row)
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            reject(row, str(exc))
    holding_keys = set()
    for row in tables.get("holdings", pd.DataFrame()).to_dict("records"):
        try:
            isin = str(row["isin"]).strip().upper()
            identifier_source = "isin"
            if pd.isna(row["isin"]) and re.fullmatch(r"IN[A-Z0-9]{10}", str(row.get("symbol", "")).strip().upper()):
                isin = str(row["symbol"]).strip().upper()
                identifier_source = "symbol (explicit ISIN)"
            if not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
                raise ValueError("Invalid ISIN")
            key = (row["asset_class"], isin)
            if key in holding_keys:
                raise ValueError("Duplicate holding snapshot/ISIN; select one account snapshot")
            qty, price = broker_number(row["quantity_available"]), broker_number(row["previous_closing_price"])
            if qty < 0 or price < 0:
                raise ValueError("Negative holding quantity/price")
            if qty > 0 and price == 0:
                issues.append(f"{isin}: positive quantity has no reported market value")
            # These balances are not added to available quantity without broker evidence.
            extras = [broker_number(row.get(k, 0)) for k in ("quantity_discrepant", "quantity_pledged_margin", "quantity_pledged_loan")]
            if any(extras):
                issues.append(f"{isin}: discrepant/pledged balances require quantity-definition review")
            holding_keys.add(key)
            holdings.append({**row, "isin": isin, "identifier_source": identifier_source, "quantity_available": qty, "market_value": qty * price})
        except (ValueError, TypeError, InvalidOperation) as exc:
            reject(row, str(exc))
    symbol_isins, isin_symbols = {}, {}
    for row in trades + holdings:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol and symbol != "NAN":
            symbol_isins.setdefault(symbol, set()).add(row["isin"])
            isin_symbols.setdefault(row["isin"], set()).add(symbol)
    for symbol, identifiers in sorted(symbol_isins.items()):
        if len(identifiers) > 1:
            issues.append(f"Symbol {symbol} has multiple ISINs: {', '.join(sorted(identifiers))}; name changes/corporate actions require evidence")
    for isin, symbols in sorted(isin_symbols.items()):
        if len(symbols) > 1:
            issues.append(f"{isin} has multiple reported symbols: {', '.join(sorted(symbols))}; retained by explicit ISIN, no rename inferred")
    identifier_review = []
    for row in rejected:
        if row["reason"] != "Invalid ISIN":
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        candidates = sorted(symbol_isins.get(symbol, set()))
        identifier_review.append({
            "source_file": row.get("source_file"), "source_sheet": row.get("source_sheet"),
            "source_row": row.get("source_row"), "symbol": symbol,
            "reported_isin": row.get("isin"), "trade_date": row.get("trade_date"),
            "candidate_isins": ", ".join(candidates),
            "status": "Candidate requires source confirmation" if candidates else "No candidate in accepted inputs",
            "basis": "Exact symbol in accepted trades/holdings only; not an ISIN assignment. Rejected row remains excluded.",
        })
    totals, held, labels = {}, {}, {}
    for row in trades:
        key = (row["asset_class"], row["isin"])
        totals[key] = totals.get(key, Decimal(0)) + row["signed_quantity"]
        labels[key] = row["symbol"]
    for row in holdings:
        key = (row["asset_class"], row["isin"])
        held[key] = row["quantity_available"]
        labels[key] = row["symbol"]
    reconciliation = []
    for key in sorted(totals.keys() | held.keys()):
        delta = held.get(key, Decimal(0)) - totals.get(key, Decimal(0))
        reconciliation.append({"asset_class": key[0], "isin": key[1], "symbol": labels[key],
            "in_holdings": key in held, "net_trade_quantity": totals.get(key, Decimal(0)),
            "reported_quantity": held.get(key), "difference": delta,
            "status": "Quantity agrees" if abs(delta) <= Decimal("0.000001") else "Unresolved quantity",
            "basis": "Zero opening assumed; missing holdings treated as zero for comparison only"})
    ledger, external = [], []
    previous = None
    opening = closing = None
    previous_date = None
    ledger_parts = rows.get("ledger", [])
    if len(ledger_parts) > 1:
        issues.append("Multiple ledger sheets/uploads: continuity is checked separately; do not combine overlapping accounts")
    for part in ledger_parts:
        previous = previous_date = None
        controls = part["particulars"].astype(str).str.strip().str.lower()
        if (controls == "opening balance").sum() != 1 or (controls == "closing balance").sum() != 1:
            issues.append("Ledger requires exactly one opening and one closing control row per sheet")
        for row in part.to_dict("records"):
            try:
                balance = broker_number(row["net_balance"])
                label = str(row["particulars"]).strip().lower()
                if label == "opening balance":
                    if previous is not None:
                        raise ValueError("Repeated opening balance")
                    opening = previous = balance
                    continue
                if label == "closing balance":
                    closing = balance
                    continue
                debit, credit = broker_number(row["debit"]), broker_number(row["credit"])
                when = broker_date(row["posting_date"])
                if pd.isna(when) or debit < 0 or credit < 0:
                    raise ValueError("Invalid ledger date/debit/credit")
                difference = None if previous is None else balance - (previous + credit - debit)
                if previous_date and when < previous_date:
                    issues.append(f"Ledger row {row['source_row']}: dates out of order")
                previous, previous_date = balance, when
                voucher = str(row["voucher_type"]).strip().lower()
                cashflow = debit - credit if voucher in ("bank receipts", "bank payments") else Decimal(0)
                record = {**row, "posting_date": when, "debit": debit, "credit": credit,
                          "net_balance": balance, "source_order_balance_difference": difference,
                          "candidate_external_cashflow": cashflow}
                ledger.append(record)
                if cashflow:
                    external.append(record)
            except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                reject(row, str(exc))
    if opening is None or closing is None:
        issues.append("Ledger opening/closing control balance missing")
    ledger_checks = []
    if len(ledger_parts) == 1 and opening is not None:
        running = opening
        ledger_frame = pd.DataFrame(ledger)
        if not ledger_frame.empty:
            for when, group in ledger_frame.groupby("posting_date", sort=True):
                pending = group.to_dict("records")
                expected_close = running + sum((r["credit"] - r["debit"] for r in pending), Decimal(0))
                chain_balance = running
                while pending:
                    match = next((i for i, r in enumerate(pending) if abs(chain_balance + r["credit"] - r["debit"] - r["net_balance"]) <= Decimal("0.01")), None)
                    if match is None:
                        break
                    chain_balance = pending.pop(match)["net_balance"]
                ledger_checks.append({"date": when, "opening": running, "expected_close": expected_close,
                                      "unlinked_rows": len(pending), "status": "Balance chain agrees" if not pending else "Unresolved balance chain"})
                running = expected_close
            if any(r["unlinked_rows"] for r in ledger_checks):
                issues.append("Ledger daily balance chains contain unlinked rows; inspect daily controls")
            if closing is None or abs(running - closing) > Decimal("0.01"):
                issues.append("Ledger opening plus credits minus debits does not equal closing")
        if opening != 0:
            issues.append("Nonzero ledger opening: earlier cashflow history required")
    ledger_has_fo = any("F&O" in str(r.get("cost_center", "")) or "F&O" in str(r.get("particulars", "")).upper() for r in ledger)
    accepted_fo = [r for r in trades if str(r.get("asset_class", "")).lower() == "derivative"]
    if ledger_has_fo and not accepted_fo:
        issues.append("Ledger includes F&O activity; no accepted derivative trade evidence was supplied")
    elif ledger_has_fo and accepted_fo:
        issues.append(f"F&O evidence recognized: {len(accepted_fo)} accepted derivative trade rows; open derivative positions still require separate evidence if any exist at holdings date")
    dividends = []
    for row in tables.get("dividends", pd.DataFrame()).to_dict("records"):
        try:
            qty, rate, amount = (broker_number(row[k]) for k in ("qty", "dividend_per_share", "total_dividend"))
            when = broker_date(row["ex_date"])
            if pd.isna(when) or min(qty, rate, amount) < 0 or abs(qty * rate - amount) > Decimal("0.01"):
                raise ValueError("Dividend amount/date fails validation")
            dividends.append({**row, "ex_date": when, "total_dividend": amount,
                              "cashflow_status": "Payment date and receipt unverified; excluded from XIRR"})
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            reject(row, str(exc))
    if rejected:
        issues.append(f"{len(rejected)} rows rejected or duplicated; inspect row audit")
    issues.extend([
        "Net trades assume zero opening positions; corporate actions, transfers and missing history remain unverified.",
        "Dividend ex-dates are not payment dates; payment evidence and ledger overlap must be reconciled.",
        "P&L is a period-limited reference, not additional cashflow or current holdings; charges must not be added again to ledger settlements.",
        "CAS account/date alignment has not been established; broker quantities are not a full CAS reconciliation.",
    ])
    return {"account_ids": sorted(account_ids), "identifier_review": pd.DataFrame(identifier_review), "inventory": pd.DataFrame(inventory), "trades": pd.DataFrame(trades), "holdings": pd.DataFrame(holdings),
            "reconciliation": pd.DataFrame(reconciliation), "ledger": pd.DataFrame(ledger),
            "ledger_checks": pd.DataFrame(ledger_checks),
            "external_cashflows": pd.DataFrame(external), "dividends": pd.DataFrame(dividends),
            "pnl": tables.get("pnl", pd.DataFrame()), "pnl_debits": tables.get("pnl_debits", pd.DataFrame()),
            "rejected": pd.DataFrame(rejected), "issues": issues, "as_of": as_of,
            "opening_cash": opening, "closing_cash": closing, "xirr_ready": False}



def broker_monetary_readiness(result: dict[str, Any]) -> dict[str, Any]:
    """Strict evidence gate for broker-derived cashflows; never infer missing history."""
    trades = result.get("trades", pd.DataFrame())
    ledger = result.get("ledger", pd.DataFrame())
    external = result.get("external_cashflows", pd.DataFrame())
    rejected = result.get("rejected", pd.DataFrame())
    reconciliation = result.get("reconciliation", pd.DataFrame())
    issues = [str(x) for x in result.get("issues", [])]
    gates = []

    def gate(name: str, passed: bool, evidence: str) -> None:
        gates.append({"gate": name, "status": "PASS" if passed else "BLOCK", "evidence": evidence})

    gate("Single dated holdings snapshot", result.get("as_of") is not None,
         str(result.get("as_of") or "No unique holdings date"))
    gate("Accepted trade evidence", not trades.empty, f"{len(trades)} accepted trade rows")
    gate("Ledger evidence", not ledger.empty, f"{len(ledger)} accepted ledger rows")
    gate("External investor cashflows", not external.empty, f"{len(external)} bank receipt/payment rows")
    opening = result.get("opening_cash")
    gate("Complete cashflow start", opening is not None and D(opening) == 0,
         f"ledger opening balance={opening}" if opening is not None else "opening balance unavailable")
    ledger_checks = result.get("ledger_checks", pd.DataFrame())
    ledger_ok = (not ledger_checks.empty and "unlinked_rows" in ledger_checks.columns and
                 pd.to_numeric(ledger_checks["unlinked_rows"], errors="coerce").fillna(1).eq(0).all())
    gate("Ledger continuity", bool(ledger_ok),
         f"{len(ledger_checks)} daily chains; all must link to ₹0.01" if not ledger_checks.empty else "no daily chain evidence")
    unresolved_qty = (len(reconciliation) if reconciliation.empty else
                      int((reconciliation.get("status", pd.Series(dtype=str)) != "Quantity agrees").sum()))
    gate("Trade/holding quantity completeness", not reconciliation.empty and unresolved_qty == 0,
         f"{unresolved_qty} unresolved reconciliation rows")
    gate("No rejected source rows", rejected.empty, f"{len(rejected)} rejected/duplicate rows")
    derivative_gap = any(
        ("no accepted derivative trade evidence" in x.lower()) or
        ("f&o activity" in x.lower() and ("not supplied" in x.lower() or "missing" in x.lower()))
        for x in issues
    )
    gate("Derivative scope complete", not derivative_gap,
         "F&O ledger activity requires derivative trade/position evidence" if derivative_gap else "no unresolved F&O scope warning")
    corp_gap = any(
        ("multiple isins" in x.lower()) or
        ("candidate requires source confirmation" in x.lower()) or
        (("corporate actions" in x.lower() or "transfers" in x.lower()) and "unverified" in x.lower())
        for x in issues
    )
    gate("Corporate actions / transfers resolved", not corp_gap,
         "corporate actions/transfers remain unverified" if corp_gap else "no unresolved corporate-action/transfer warning")

    cashflow_rows = []
    for row in external.to_dict("records"):
        when = row.get("posting_date")
        amount = D(row.get("candidate_external_cashflow"))
        if when and amount:
            cashflow_rows.append({
                "date": when, "cashflow": amount,
                "source_file": row.get("source_file"), "source_sheet": row.get("source_sheet"),
                "source_row": row.get("source_row"), "basis": "Broker ledger bank receipt/payment",
            })
    readiness = bool(gates) and all(g["status"] == "PASS" for g in gates)
    return {"ready": readiness, "gates": pd.DataFrame(gates), "cashflows": pd.DataFrame(cashflow_rows)}


def broker_xirr_cashflows(result: dict[str, Any]) -> list[tuple[date, Decimal]]:
    """Build broker XIRR flows only after every monetary-completeness gate passes."""
    readiness = broker_monetary_readiness(result)
    if not readiness["ready"]:
        blocked = readiness["gates"][readiness["gates"]["status"] == "BLOCK"]["gate"].tolist()
        raise ValueError("Broker XIRR blocked by evidence gates: " + ", ".join(blocked))
    flows = [(r["date"], D(r["cashflow"])) for r in readiness["cashflows"].to_dict("records")]
    terminal = sum((D(r.get("market_value")) for r in result.get("holdings", pd.DataFrame()).to_dict("records")), Decimal("0"))
    terminal_value = terminal + D(result.get("closing_cash"))
    if terminal_value <= 0 or result.get("as_of") is None:
        raise ValueError("Broker terminal valuation is unavailable or non-positive")
    flows.append((result["as_of"], terminal_value))
    return flows


def compare_broker_cas_snapshot(broker: pd.DataFrame, cas: pd.DataFrame,
                                broker_date: date | None, cas_date: date | None) -> pd.DataFrame:
    """The caller selects one CAS statement and one broker account, never all accounts."""
    if broker_date is None or cas_date is None or broker_date != cas_date:
        raise ValueError("CAS and broker snapshot dates must match; no cross-date comparison is inferred")
    if broker.empty or cas.empty:
        raise ValueError("Both selected snapshots must contain holdings")
    if not {"isin", "quantity_inferred"} <= set(cas.columns):
        raise ValueError("CAS holdings lack ISIN/quantity fields")
    sides = []
    for frame, quantity in ((broker, "quantity_available"), (cas, "quantity_inferred")):
        values = {}
        for row in frame.to_dict("records"):
            isin = str(row["isin"]).strip().upper()
            if not re.fullmatch(r"IN[A-Z0-9]{10}", isin) or isin in values:
                raise ValueError("Invalid or repeated ISIN in selected snapshot; review account scope")
            values[isin] = broker_number(row[quantity])
            if values[isin] < 0:
                raise ValueError("Negative snapshot quantity requires review")
        sides.append(values)
    left, right = sides
    return pd.DataFrame([{"isin": isin, "broker_quantity": left.get(isin), "cas_quantity": right.get(isin),
        "difference": left.get(isin, Decimal(0)) - right.get(isin, Decimal(0)),
        "status": ("Missing from one snapshot" if isin not in left or isin not in right else
                   "Quantity agrees" if abs(left[isin] - right[isin]) <= Decimal("0.000001") else "Unresolved quantity")}
        for isin in sorted(left.keys() | right.keys())])


def import_contract_notes(content: bytes, filename: str) -> dict[str, Any]:
    """Strict supported contract-note schema; settlement dates come only from notes."""
    records, excluded, accounts = [], [], set()
    digest = hashlib.sha256(content).hexdigest()
    for sheet, raw in pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object, engine="openpyxl").items():
        def field(label: str) -> str:
            matches = []
            for values in raw.itertuples(index=False, name=None):
                for i, value in enumerate(values):
                    if str(value).strip().rstrip(":.").lower() == label:
                        following = [str(v).strip() for v in values[i + 1:] if pd.notna(v) and str(v).strip()]
                        if following:
                            matches.append(following[0])
            if len(matches) != 1:
                raise ValueError(f"{sheet}: missing or ambiguous {label}")
            return matches[0]
        account = field("ucc of client").upper()
        if not re.fullmatch(r"[A-Z0-9]+", account):
            raise ValueError(f"{sheet}: invalid client identifier")
        accounts.add(account)
        when = datetime.strptime(field("trade date"), "%d-%m-%Y").date()
        # NCL-Cash is explicitly the settlement column for the supported equity layout.
        cash = [(i, j) for i, row in raw.iterrows() for j, v in enumerate(row) if str(v).strip() == "NCL-Cash" and i < 10]
        if len(cash) != 1:
            raise ValueError(f"{sheet}: ambiguous cash settlement column")
        settlement_rows = [i for i, row in raw.iterrows() if any(str(v).strip().lower().rstrip(".") == "settlement date" for v in row)]
        if len(settlement_rows) != 1:
            raise ValueError(f"{sheet}: missing settlement date")
        settled = datetime.strptime(str(raw.iloc[settlement_rows[0], cash[0][1]]).strip(), "%d/%m/%Y").date()
        if settled < when:
            raise ValueError(f"{sheet}: settlement precedes trade")
        headers = []
        for i, row in raw.iterrows():
            names = [re.sub(r"[^a-z0-9]+", "_", str(v).strip().lower()).strip("_") if pd.notna(v) else "" for v in row]
            if {"trade_no", "quantity", "exchange", "buy_b_sell_s", "security_contract_description"} <= set(names):
                headers.append((i, names))
        if len(headers) != 1:
            raise ValueError(f"{sheet}: missing or ambiguous trade header")
        index, names = headers[0]
        required = {"trade_no", "quantity", "exchange", "buy_b_sell_s", "security_contract_description", "gross_rate_trade_price_per_unit_rs", "net_total_before_levies_rs"}
        if not required <= set(names) or len([n for n in names if n]) != len(set(n for n in names if n)):
            raise ValueError(f"{sheet}: unsupported or duplicate columns")
        for i, values in raw.iloc[index + 1:].iterrows():
            row = dict(zip(names, values))
            side = str(row.get("buy_b_sell_s", "")).strip()
            if side not in ("B", "S"):
                # A nonblank trade number must never disappear as a footer.
                if pd.notna(row.get("trade_no")):
                    raise ValueError(f"{sheet} row {i+1}: unsupported trade side")
                continue
            description = str(row["security_contract_description"]).strip()
            provenance = {"source_file": filename, "source_sha256": digest, "source_sheet": sheet, "source_row": i + 1}
            isin = re.search(r"/(IN[A-Z0-9]{10})$", description)
            if not isin:
                if re.fullmatch(r"[A-Z0-9]+(?:CE|PE|FUT)", description):
                    excluded.append({**provenance, "description": description, "reason": "Derivative excluded from equity quantity bridge"})
                    continue
                raise ValueError(f"{sheet} row {i+1}: missing equity ISIN")
            qty = broker_number(row["quantity"])
            price = broker_number(row["gross_rate_trade_price_per_unit_rs"])
            amount = broker_number(row["net_total_before_levies_rs"])
            identity = str(row["trade_no"]).strip()
            exchange = str(row["exchange"]).strip().upper()
            if qty <= 0 or price <= 0 or identity in ("", "nan", "None") or exchange not in ("NSE", "BSE"):
                raise ValueError(f"{sheet} row {i+1}: invalid trade fields")
            if abs(amount - qty * price * (-1 if side == "B" else 1)) > Decimal("0.01"):
                raise ValueError(f"{sheet} row {i+1}: gross consideration arithmetic mismatch")
            records.append({**provenance, "account_id": account, "trade_date": when, "settlement_date": settled,
                            "trade_id": identity, "exchange": exchange, "isin": isin[1], "side": side,
                            "quantity": qty, "price": price})
    if len(accounts) != 1 or not records:
        raise ValueError("Contract notes require one account and at least one equity trade")
    frame = pd.DataFrame(records)
    if frame.duplicated(["trade_date", "exchange", "trade_id"]).any():
        raise ValueError("Repeated contract-note trade identity; duplicates or conflicts require review")
    return {"trades": frame, "excluded": pd.DataFrame(excluded), "account_id": next(iter(accounts))}


def reconcile_contract_settlements(notes: dict[str, Any], broker: dict[str, Any], cas: pd.DataFrame,
                                   cas_date: date, expected_account: str, cas_quality_valid: bool) -> dict[str, Any]:
    """Documentary quantity bridge only; never promotes broker data to XIRR-ready."""
    closing = broker.get("as_of")
    if not cas_quality_valid or cas_date is None or closing is None or cas_date >= closing:
        raise ValueError("Passed CAS holdings quality and an earlier CAS date are required")
    if notes["account_id"] != expected_account.strip().upper():
        raise ValueError("Contract-note account does not match the confirmed broker account")
    for frame, quantity in ((broker["holdings"], "quantity_available"), (cas, "quantity_inferred")):
        if frame.empty or not {"isin", quantity} <= set(frame.columns):
            raise ValueError("Both account snapshots require ISIN and quantity")
        identities = set()
        for row in frame.to_dict("records"):
            isin = str(row["isin"]).strip().upper()
            if not re.fullmatch(r"IN[A-Z0-9]{10}", isin) or isin in identities or broker_number(row[quantity]) < 0:
                raise ValueError("Invalid, repeated or negative snapshot holding")
            identities.add(isin)
    if broker.get("account_ids") and set(broker["account_ids"]) != {notes["account_id"]}:
        raise ValueError("Broker reports and contract notes belong to different accounts")
    contracts = notes["trades"]
    begin = min(contracts.trade_date)
    if begin > cas_date:
        raise ValueError("Contract-note coverage must start on or before the CAS date")
    trades = broker["trades"]
    if trades.empty:
        raise ValueError("Accepted broker trades are required")
    rejected = broker.get("rejected", pd.DataFrame())
    for row in rejected.to_dict("records"):
        if "trade_date" not in row or pd.isna(row.get("trade_date")):
            continue
        try:
            when = broker_date(row["trade_date"])
        except ValueError:
            raise ValueError("Rejected trade has an unresolvable date; coverage is uncertain")
        if begin <= when <= closing:
            raise ValueError("Rejected/duplicate broker trades occur inside contract-note coverage")
    relevant = trades[(trades.trade_date >= begin) & (trades.trade_date <= closing)]
    if (relevant.asset_class != "Equity").any():
        raise ValueError("MF trades in this window need separate settlement evidence")
    def key(row: dict) -> tuple:
        return row["trade_date"], str(row["exchange"]).strip(), str(row["trade_id"]).strip()
    lookup = {key(row): row for row in relevant.to_dict("records")}
    if len(lookup) != len(relevant):
        raise ValueError("Ambiguous broker trade identities")
    matched, seen, movements = [], set(), {}
    for row in contracts[contracts.trade_date <= closing].to_dict("records"):
        identity = key(row)
        original = lookup.get(identity)
        if original is None:
            raise ValueError("Contract-note trade missing from accepted broker tradebook")
        if row["isin"] != original["isin"] or row["quantity"] != original["quantity"] or row["side"] != ("B" if original["signed_quantity"] > 0 else "S"):
            raise ValueError("Contract note and tradebook disagree on ISIN, side or quantity")
        seen.add(identity)
        difference = row["price"] - original["price"]
        matched.append({**row, "tradebook_price": original["price"], "price_difference": difference,
                        "price_status": "Exact" if difference == 0 else "Price discrepancy; monetary review required"})
        if cas_date < row["settlement_date"] <= closing:
            movements[row["isin"]] = movements.get(row["isin"], Decimal(0)) + row["quantity"] * (1 if row["side"] == "B" else -1)
    if seen != set(lookup):
        raise ValueError("Contract notes do not cover every broker trade in the comparison window")
    starts = {r["isin"].strip().upper(): broker_number(r["quantity_inferred"]) for r in cas.to_dict("records")}
    ends = {r["isin"]: broker_number(r["quantity_available"]) for r in broker["holdings"].to_dict("records")}
    comparison = []
    for isin in sorted(starts.keys() | ends.keys() | movements.keys()):
        expected = starts.get(isin, Decimal(0)) + movements.get(isin, Decimal(0))
        difference = ends.get(isin, Decimal(0)) - expected
        comparison.append({"isin": isin, "cas_quantity": starts.get(isin, Decimal(0)), "settled_net_quantity": movements.get(isin, Decimal(0)),
                           "expected_quantity": expected, "broker_quantity": ends.get(isin, Decimal(0)), "difference": difference,
                           "status": "Quantity agrees" if expected >= 0 and abs(difference) <= Decimal("0.000001") else "Unresolved exception"})
    return {"comparison": pd.DataFrame(comparison), "matches": pd.DataFrame(matched), "excluded": notes["excluded"],
            "later_notes": contracts[contracts.trade_date > closing], "xirr_ready": False}


def render_broker_imports() -> None:
    st.title("Broker Imports & Reconciliation")
    st.caption("Zerodha Excel reports · Select files for one account and one holdings date. Files stay in this session.")
    uploads = st.file_uploader("Tradebooks, holdings, ledger, dividends and P&L", type=["xlsx"], accept_multiple_files=True, key="broker_uploads")
    # Streamlit removes uploader widget values when its page is not rendered. Persist
    # the imported/reconciled result independently so navigation does not erase it.
    current_fingerprint = tuple((f.name, hashlib.sha256(f.getvalue()).hexdigest()) for f in (uploads or []))
    if uploads and st.session_state.get("broker_fingerprint") not in (None, current_fingerprint):
        st.info("A different broker file set is selected. Click Import and reconcile to replace the frozen session dataset.")
    if st.button("Import and reconcile", disabled=not uploads):
        try:
            books = [import_zerodha_workbook(f.getvalue(), f.name) for f in uploads]
            new_result = reconcile_zerodha(books)
            st.session_state.broker_result = new_result
            st.session_state.broker_fingerprint = current_fingerprint
            st.session_state.broker_file_names = [f.name for f in uploads]
        except Exception as exc:
            st.error(f"Import failed: {exc}")
    elif not uploads and st.session_state.get("broker_result") is not None:
        names = st.session_state.get("broker_file_names", [])
        st.success(f"Using frozen broker dataset from this session ({len(names)} files). Return here only to replace it.")
    result = st.session_state.get("broker_result")
    if result is None:
        return
    readiness = broker_monetary_readiness(result)
    st.subheader(f"v{APP_VERSION} Monetary completeness & XIRR readiness")
    st.dataframe(readiness["gates"], use_container_width=True, hide_index=True)
    if readiness["ready"]:
        st.success("All current broker monetary-evidence gates pass. XIRR may be calculated from the gated broker cashflows.")
        try:
            xirr_flows = broker_xirr_cashflows(result)
            actual_xirr = xirr(xirr_flows)
            terminal_holdings = sum((D(r.get("market_value")) for r in result.get("holdings", pd.DataFrame()).to_dict("records")), Decimal("0"))
            terminal_cash = D(result.get("closing_cash"))
            terminal_value = terminal_holdings + terminal_cash
            c1, c2, c3 = st.columns(3)
            c1.metric("Actual portfolio XIRR", f"{float(actual_xirr) * 100:.2f}%")
            c2.metric("Terminal holdings value", fmt_inr(terminal_holdings))
            c3.metric("Closing cash", fmt_inr(terminal_cash))
            st.caption(f"Evidence-gated money-weighted return through {result['as_of']}. Terminal portfolio value: {fmt_inr(terminal_value)}.")
            xirr_audit = pd.DataFrame([{
                "date": when, "cashflow": amount,
                "basis": "Terminal holdings + closing cash" if i == len(xirr_flows) - 1 else "External investor cashflow"
            } for i, (when, amount) in enumerate(xirr_flows)])
            with st.expander("XIRR evidence report"):
                st.dataframe(xirr_audit.astype(str), use_container_width=True, hide_index=True)
                df_download("Download XIRR evidence report", xirr_audit, "actual_portfolio_xirr_evidence.csv", "broker_xirr_evidence_csv")
        except Exception as exc:
            st.error(f"XIRR calculation failed after readiness gate: {exc}")
    else:
        st.error("Broker XIRR remains blocked. The table above identifies the exact evidence gaps; no missing cashflow is inferred.")
    if not readiness["cashflows"].empty:
        with st.expander("Auditable external cashflow candidates"):
            st.dataframe(readiness["cashflows"].astype(str), use_container_width=True, hide_index=True)
            df_download("Download monetary cashflow audit", readiness["cashflows"], "broker_monetary_cashflows.csv", "broker_money_csv")
    try:
        diagnostic_bytes = xirr_diagnostic_workbook(result, readiness)
        st.download_button(
            "Download XIRR Reconciliation Diagnostic.xlsx",
            data=diagnostic_bytes,
            file_name="XIRR_Reconciliation_Diagnostic.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="xirr_reconciliation_diagnostic_xlsx",
        )
        st.caption("Diagnostic export includes gate evidence, unresolved reconciliation rows, rejected rows, F&O candidates, corporate-action issues, external cashflows, ledger checks and XIRR evidence.")
    except Exception as exc:
        st.warning(f"Diagnostic workbook could not be generated: {exc}")

    st.write(f"Holdings snapshot: {result['as_of'] or 'Unresolved'}")
    st.dataframe(result["inventory"], use_container_width=True)
    rec = result["reconciliation"]
    if not rec.empty:
        current = rec[rec["in_holdings"]]
        cols = st.columns(3)
        cols[0].metric("Imported trades", len(result["trades"]))
        cols[1].metric("Current quantities agree", int((current["status"] == "Quantity agrees").sum()))
        cols[2].metric("Unresolved ISINs (including historical)", int((rec["status"] != "Quantity agrees").sum()))
    for issue in result["issues"]:
        st.warning(issue)
    st.caption("Quantity agreement is a comparison under a zero-opening assumption, not proof of complete history. No adjustments are invented.")
    cas_results = st.session_state.get("cas_results", [])
    with st.expander("Compare with a CAS account snapshot"):
        if not cas_results:
            st.info("Parse a CAS statement first. Comparison requires the same holdings date and the corresponding broker account.")
        else:
            selected = st.selectbox("CAS statement", range(len(cas_results)), format_func=lambda i: cas_results[i]["filename"], key="broker_cas_statement")
            statement = cas_results[selected]
            cas_holdings = statement.get("holdings", pd.DataFrame())
            if not statement.get("holdings_quality", {}).get("valid", False):
                st.warning("CAS holdings quality gate has not passed; comparison is blocked")
            elif "account" in cas_holdings.columns:
                accounts = sorted(cas_holdings["account"].dropna().unique())
                account = st.selectbox("Corresponding Zerodha account in CAS", [None] + accounts, key="broker_cas_account")
                if account is not None:
                    try:
                        comparison = compare_broker_cas_snapshot(result["holdings"], cas_holdings[cas_holdings["account"] == account], result["as_of"], statement.get("holdings_as_of"))
                        st.dataframe(comparison.astype(str), use_container_width=True)
                        df_download("Download CAS comparison", comparison, "zerodha_cas_comparison.csv", "broker_cas_csv")
                        st.caption("This compares quantities only. It does not validate consideration, transfers, corporate actions or XIRR completeness.")
                    except (ValueError, InvalidOperation) as exc:
                        st.warning(str(exc))
                    st.markdown("#### Reconcile using documented settlements")
                    contract_upload = st.file_uploader("Contract-note workbook", type=["xlsx"], key="contract_notes_upload")
                    confirmed_account = st.text_input("Broker trading account (UCC)", key="contract_account")
                    confirmed = st.checkbox("I confirm this CAS account and these broker reports belong to the entered trading account", key="contract_account_confirm")
                    if st.button("Check documented settlements", disabled=contract_upload is None or not confirmed or not confirmed_account.strip()):
                        try:
                            notes = import_contract_notes(contract_upload.getvalue(), contract_upload.name)
                            settlement = reconcile_contract_settlements(notes, result, cas_holdings[cas_holdings["account"] == account],
                                statement.get("holdings_as_of"), confirmed_account, bool(statement.get("holdings_quality", {}).get("valid")))
                            comparison = settlement["comparison"]
                            agrees = int((comparison["status"] == "Quantity agrees").sum())
                            st.info(f"Documented settlement quantities agree for {agrees} of {len(comparison)} ISINs.")
                            st.caption("Quantity comparison only. Earlier unsettled positions, depository delivery, transfers and cashflow completeness are not independently verified. XIRR remains blocked.")
                            for name, table in settlement.items():
                                if isinstance(table, pd.DataFrame):
                                    st.write(name.replace("_", " ").title())
                                    st.dataframe(table.astype(str), use_container_width=True)
                                    df_download("Download " + name.replace("_", " "), table, "settlement_" + name + ".csv", "settlement_" + name)
                        except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
                            st.error(f"Settlement check blocked: {exc}")
            else:
                st.warning("CAS account identifiers are unavailable; select a statement with identifiable accounts")
    for key, label in [("reconciliation", "Quantity reconciliation"), ("rejected", "Rejected/duplicate row audit"), ("identifier_review", "Missing ISIN evidence review"),
                       ("trades", "Trades (gross consideration, before charges)"), ("holdings", "Holdings"),
                       ("ledger", "Ledger (original row order)"), ("ledger_checks", "Daily ledger balance controls"), ("external_cashflows", "Candidate bank cashflows (investor sign)"),
                       ("dividends", "Dividend reference"), ("pnl", "P&L reference"), ("pnl_debits", "P&L debit/credit reference")]:
        with st.expander(label, expanded=key in ("reconciliation", "rejected")):
            st.dataframe(result[key].astype(str), use_container_width=True)
            df_download("Download CSV", result[key], f"zerodha_{key}.csv", f"broker_{key}_csv")



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
    "renames_df": pd.DataFrame(),
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
        "Broker Imports & Reconciliation",
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
            ["CAS ingestion / parsing", "Implemented starter", "NSDL + CDSL + MF-folio holdings parser with arithmetic validation"],
            ["Multi-CAS reconstruction", "Partial", "Compact ISIN-based reconciliation; transaction reconstruction still under hardening"],
            ["Securities master / renames", "Partial", "Upload/fetch universe data; no persistent DB"],
            ["XIRR", "Implemented", "Manual + automated CAS reconstructed cash-flow engine"],
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
                    "holdings_as_of": r.get("holdings_as_of"),
                    "portfolio_value": r.get("consolidated_portfolio_value"),
                    "parsed_holdings_value": r.get("holdings_quality", {}).get("parsed_value"),
                    "holdings_difference_pct": r.get("holdings_quality", {}).get("difference_pct"),
                    "holdings_quality": (
                        "PASS" if r.get("holdings_quality", {}).get("valid") else "REVIEW"
                    ),
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

        asset_rows = []
        for r in st.session_state.cas_results:
            for item in r.get("asset_reconciliation", []):
                asset_rows.append({"file": r["filename"], **item})

        if asset_rows:
            asset_rec_df = pd.DataFrame(asset_rows)
            st.subheader("Asset-class reconciliation")
            st.caption(
                "Parsed holdings are compared directly with NSDL's own portfolio-composition "
                "totals. A PASS means the asset-class value is within 0.5%."
            )
            st.dataframe(asset_rec_df, use_container_width=True, hide_index=True)
            df_download(
                "Download asset-class reconciliation",
                asset_rec_df,
                "cas_asset_reconciliation.csv",
                "cas_asset_rec",
            )

        if holdings_frames:
            all_holdings = pd.concat(holdings_frames, ignore_index=True)
            st.subheader("Parsed holdings")
            st.caption(
                "v1.0.6 uses the observed NSDL holdings-table layout and accepts rows only when "
                "quantity × market price/NAV reconciles to the reported market value."
            )
            st.dataframe(all_holdings, use_container_width=True, hide_index=True)
            df_download("Download inferred holdings", all_holdings, "cas_inferred_holdings.csv", "cas_hold")

            if len(st.session_state.cas_results) >= 2:
                st.subheader("Cross-CAS quantity comparison")
                # Compact reconciliation: one genuine security row per asset_type + ISIN.
                # Do NOT pivot on symbol/name simultaneously with dropna=False because pandas can
                # create a Cartesian product of all symbols × names × ISINs.
                compact = (
                    all_holdings
                    .groupby(["file", "asset_type", "isin"], as_index=False, dropna=False)
                    .agg(
                        symbol=("symbol", lambda s: next(
                            (str(x) for x in s if pd.notna(x) and str(x).strip()),
                            ""
                        )),
                        name=("name", lambda s: next(
                            (str(x) for x in s if pd.notna(x) and str(x).strip()),
                            ""
                        )),
                        quantity=("quantity_inferred", "sum"),
                        market_value=("market_value_inferred", "sum"),
                    )
                )

                metadata = (
                    compact.sort_values("file")
                    .groupby(["asset_type", "isin"], as_index=False)
                    .agg(
                        symbol=("symbol", lambda s: next((x for x in s if x), "")),
                        name=("name", lambda s: next((x for x in s if x), "")),
                    )
                )

                qty_pivot = compact.pivot(
                    index=["asset_type", "isin"],
                    columns="file",
                    values="quantity",
                ).reset_index()

                pivot = metadata.merge(
                    qty_pivot,
                    on=["asset_type", "isin"],
                    how="left",
                )
                st.dataframe(pivot, use_container_width=True, hide_index=True)
                df_download("Download reconciliation view", pivot, "cas_reconciliation.csv", "cas_rec")

        tx_frames = [
            r["transactions"] for r in st.session_state.cas_results
            if isinstance(r.get("transactions"), pd.DataFrame) and not r["transactions"].empty
        ]
        if tx_frames:
            all_tx = pd.concat(tx_frames, ignore_index=True)
            st.subheader("Structured CAS transaction ledger")
            st.info(
                "v1.1.2 separates external investor cashflows, internal switches, reversals, and depository "
                "quantity movements. Depository CAS rows do not disclose trade consideration; "
                "those rows are preserved but never converted into invented cashflows."
            )
            st.dataframe(all_tx, use_container_width=True, hide_index=True)

            q = transaction_quality_report(all_tx)
            qc1, qc2, qc3, qc4 = st.columns(4)
            qc1.metric("Ledger rows", q["rows"])
            qc2.metric("Monetary rows", q["cashflow_rows"])
            qc3.metric("Quantity-only rows", q["quantity_only_rows"])
            qc4.metric("Cashflow coverage", f"{q['cashflow_coverage_pct']:.1f}%")
            qx1, qx2, qx3, qx4 = st.columns(4)
            qx1.metric("External cashflow rows", q.get("external_cashflow_rows", 0))
            qx2.metric("Internal switch/event rows", q.get("internal_transfer_rows", 0))
            qx3.metric("Reversal rows to review", q.get("reversal_review_rows", 0))
            qx4.metric("Continuity anomalies", q.get("continuity_anomalies", 0))

            if not q["valid"]:
                st.error("Transaction ledger quality gate failed: " + q["reason"])
            elif q["automated_xirr_ready"]:
                st.success("Transaction ledger is monetary-complete for the reconstructed rows.")
            else:
                st.warning(
                    "Ledger parsing passed, but full-portfolio automated XIRR is not monetary-complete. "
                    "A broker tradebook/ledger is required for consideration missing from depository CAS rows."
                )

            df_download(
                "Download inferred transactions",
                all_tx,
                "cas_inferred_transactions.csv",
                "cas_tx_download",
            )
        else:
            st.info(
                "No dated transaction rows were inferred from the uploaded CAS files. "
                "You can still use the manual XIRR calculator."
            )

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

elif section == "Broker Imports & Reconciliation":
    render_broker_imports()


elif section == "XIRR & Benchmarking":
    st.title("XIRR & Benchmarking")
    st.caption(
        "v1.1.0 separates validated transaction-ledger reconstruction from monetary completeness; full XIRR is blocked when CAS omits trade consideration."
    )

    auto_tab, manual_tab = st.tabs(["Automated from CAS", "Manual calculator"])

    with auto_tab:
        tx = reconstructed_cas_transactions()
        terminal_value = reconstructed_terminal_value()

        tx_quality = transaction_quality_report(tx)
        history_quality = reconstructed_transaction_history_diagnostics()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("CAS ledger rows", len(tx))
        c2.metric("Monetary rows", tx_quality["cashflow_rows"])
        c3.metric("Quantity-only", tx_quality["quantity_only_rows"])
        c4.metric("Non-zero openings", history_quality["opening_balance_nonzero"])

        if not tx.empty and not tx_quality["valid"]:
            st.error("Transaction ledger quality gate: " + tx_quality["reason"])
        elif not tx.empty and (not tx_quality["automated_xirr_ready"] or not history_quality["history_complete"]):
            st.warning(
                "Full-portfolio automated XIRR is blocked because CAS does not provide a complete "
                "monetary history: depository rows may omit trade consideration and non-zero opening "
                "balances prove earlier cashflows exist outside the uploaded period. Upload broker "
                "tradebook/ledger history or use the Manual calculator. No missing cashflow will be invented."
            )

        if tx.empty:
            st.info(
                "Upload and parse one or more CAS PDFs first. If the parser cannot infer transaction "
                "rows, use the Manual calculator tab."
            )
        else:
            st.warning(
                "The structured CAS ledger can be validated even when full monetary history is incomplete. "
                "Performance calculation remains disabled until monetary completeness is satisfied."
            )

            benchmark_name = st.selectbox(
                "Benchmark",
                list(BENCHMARKS.keys()),
                index=1,
                help="NIFTY 50 (^NSEI), NIFTY 500 (^CRSLDX), or Gold BeES (GOLDBEES.NS).",
            )

            if st.button("Calculate automated CAS XIRR", type="primary"):
                try:
                    result = cas_xirr_summary(benchmark_name)

                    c1, c2, c3 = st.columns(3)
                    c1.metric("Portfolio XIRR", f"{float(result['portfolio_xirr'])*100:.2f}%")
                    c2.metric(
                        f"{result['benchmark_name']} XIRR",
                        f"{float(result['benchmark_xirr'])*100:.2f}%"
                    )
                    c3.metric("Excess XIRR", f"{float(result['excess_xirr'])*100:.2f}%")

                    values = pd.DataFrame(
                        {
                            "Series": ["Portfolio", result["benchmark_name"], "Excess"],
                            "XIRR %": [
                                float(result["portfolio_xirr"]) * 100,
                                float(result["benchmark_xirr"]) * 100,
                                float(result["excess_xirr"]) * 100,
                            ],
                        }
                    )
                    st.plotly_chart(
                        px.bar(values, x="Series", y="XIRR %", text_auto=".2f"),
                        use_container_width=True,
                    )

                    terminal_df = pd.DataFrame(
                        [
                            {
                                "Series": "Portfolio",
                                "Terminal value": float(result["portfolio_terminal_value"]),
                            },
                            {
                                "Series": result["benchmark_name"],
                                "Terminal value": float(result["benchmark_terminal_value"]),
                            },
                        ]
                    )
                    st.subheader("Terminal-value comparison")
                    st.dataframe(terminal_df, use_container_width=True, hide_index=True)

                    with st.expander("Reconstructed portfolio cash flows"):
                        st.dataframe(
                            result["portfolio_cashflows"],
                            use_container_width=True,
                            hide_index=True,
                        )
                        df_download(
                            "Download reconstructed portfolio cash flows",
                            result["portfolio_cashflows"],
                            "reconstructed_portfolio_cashflows.csv",
                            "auto_pf_cashflows_dl",
                        )

                    with st.expander("Benchmark simulation audit"):
                        st.caption(
                            f"{result['benchmark_name']} uses Yahoo symbol "
                            f"`{result['benchmark_symbol']}`. Each portfolio cash-flow date is "
                            "mapped to the next available benchmark trading-day price."
                        )
                        st.dataframe(
                            result["benchmark_audit"],
                            use_container_width=True,
                            hide_index=True,
                        )
                        df_download(
                            "Download benchmark audit",
                            result["benchmark_audit"],
                            "benchmark_simulation_audit.csv",
                            "benchmark_audit_dl",
                        )

                except Exception as exc:
                    st.error(str(exc))
                    st.info(
                        "If this error is due to incomplete CAS extraction, inspect CAS Parser & "
                        "Reconciliation and use the Manual calculator until the statement-specific "
                        "parser is hardened."
                    )

    with manual_tab:
        st.caption(
            "Enter dated investor cash flows. Contributions/investments are normally negative."
        )

        sample = pd.DataFrame(
            {
                "date": ["2022-01-01", "2023-01-01", india_today().isoformat()],
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

        if st.button("Calculate manual XIRR", type="primary"):
            try:
                dates = pd.to_datetime(edited["date"], errors="coerce").dt.date
                pflows = [
                    (d, D(v))
                    for d, v in zip(dates, edited["portfolio_cashflow"])
                    if pd.notna(d)
                ]
                bflows = [
                    (d, D(v))
                    for d, v in zip(dates, edited["benchmark_cashflow"])
                    if pd.notna(d)
                ]

                pxirr_value = xirr(pflows)
                bxirr_value = xirr(bflows)
                excess = pxirr_value - bxirr_value

                c1, c2, c3 = st.columns(3)
                c1.metric("Portfolio XIRR", f"{float(pxirr_value)*100:.2f}%")
                c2.metric("Benchmark XIRR", f"{float(bxirr_value)*100:.2f}%")
                c3.metric("Excess XIRR", f"{float(excess)*100:.2f}%")

                chart_df = pd.DataFrame(
                    {
                        "Series": ["Portfolio", "Benchmark", "Excess"],
                        "XIRR %": [
                            float(pxirr_value)*100,
                            float(bxirr_value)*100,
                            float(excess)*100,
                        ],
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
    sale_date = c3.date_input("Sale date", value=india_today())
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
        loss_df = harvestable_losses(lots, D(market_price), india_today(), D(min_loss))
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
            st.session_state.universe_df = read_uploaded_csv(upload_map["Universe"])
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
                elif name == "Universe":
                    st.session_state.universe_df = df
                loaded.append(name)
            except Exception as exc:
                failed.append(f"{name}: {exc}")
            progress.progress(i / len(keys))

        # Symbol renames are JSON in the upstream repository, not part of the universe CSV.
        try:
            st.session_state.renames_df = fetch_symbol_renames()
            if not st.session_state.renames_df.empty:
                loaded.append("Symbol renames")
        except Exception as exc:
            failed.append(f"Symbol renames: {exc}")

        if loaded:
            st.success("Loaded: " + ", ".join(loaded))
        if failed:
            st.warning(
                "Some upstream files could not be fetched. Use manual upload where available.\n\n"
                + "\n".join(failed)
            )

    status_df = pd.DataFrame(
        {
            "Dataset": list(UPSTREAM_PATHS.keys()) + ["Symbol renames"],
            "Rows loaded": [
                len(st.session_state.shareholding_df),
                len(st.session_state.signals_df),
                len(st.session_state.index_df),
                len(st.session_state.fno_df),
                len(st.session_state.universe_df),
                len(st.session_state.renames_df),
            ],
        }
    )
    st.dataframe(status_df, use_container_width=True, hide_index=True)

    st.subheader("As-of stock intelligence")
    c1, c2 = st.columns(2)
    symbol = c1.text_input("NSE symbol", value="RELIANCE").upper().strip()
    as_of = c2.date_input("As-of date", value=india_today(), key="pit_date")

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

            if not st.session_state.renames_df.empty:
                rdf = normalize_columns(st.session_state.renames_df)
                candidate_cols = [c for c in rdf.columns if "symbol" in c]
                if candidate_cols:
                    mask = pd.Series(False, index=rdf.index)
                    for c in candidate_cols:
                        mask = mask | (
                            rdf[c].astype(str).str.upper().str.strip() == symbol.upper().strip()
                        )
                    matches = rdf[mask]
                    if not matches.empty:
                        st.subheader("Symbol rename history")
                        st.dataframe(matches, use_container_width=True, hide_index=True)

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
            ["India timezone date handling", "Implemented", APP_VERSION],
            ["Universe + symbol rename upstream paths", "Corrected", APP_VERSION],
            ["CAS layout hardening", "Partial", APP_VERSION],
            ["Transaction reconstruction", "Not yet reliable", APP_VERSION],
            ["XIRR engine", "Implemented", APP_VERSION],
            ["Tax-lot simulation", "Implemented starter", APP_VERSION],
            ["Return attribution", "Implemented starter", APP_VERSION],
            ["Institutional PIT overlay", "Implemented when data supplied", APP_VERSION],
            ["Index/F&O history", "Implemented when data supplied", APP_VERSION],
            ["Advice impact estimator", "Implemented starter", APP_VERSION],
            ["NSDL/CDSL/MF-folio holdings parsers", "Implemented", APP_VERSION],
            ["Indian-number parsing (e.g. 36,16,119.95)", "Implemented", APP_VERSION],
            ["Compact cross-CAS reconciliation", "Implemented", APP_VERSION],
            ["Inline ISIN + security-description row recovery", "Implemented", APP_VERSION],
            ["CDSL numeric-tail/page-number recovery", "Implemented", APP_VERSION],
            ["Historical SGB face-value reconciliation", "Implemented", APP_VERSION],
            ["Structured MF-folio monetary transaction parser", "Implemented", APP_VERSION],
            ["Depository quantity/balance transaction parser", "Implemented", APP_VERSION],
            ["Cashflow-completeness XIRR gate", "Implemented", APP_VERSION],
            ["Holdings arithmetic/reconciliation quality gate", "Implemented", APP_VERSION],
            ["Latest-statement terminal value selection", "Implemented", APP_VERSION],
            ["Automated CAS XIRR", "Implemented starter with parser quality gate", APP_VERSION],
            ["NIFTY 50 / NIFTY 500 / Gold benchmark simulation", "Implemented starter", APP_VERSION],
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