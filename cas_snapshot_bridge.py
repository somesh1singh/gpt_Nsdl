from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import inspect
import tokenize
from typing import Any

import pandas as pd

TOL = Decimal("0.000001")


# Python 3.14 + Streamlit compatibility for dynamically transformed app source.
# Streamlit cache key generation normally falls back when inspect.getsource()
# raises OSError/TypeError, but Python 3.14 can raise tokenize.TokenError when
# a function was created by exec() from source whose line map differs from the
# on-disk file. Translate only that narrow failure into OSError so Streamlit can
# use its documented source-unavailable fallback rather than crashing startup.
def _install_streamlit_exec_inspect_compat() -> None:
    current = inspect.getsource
    if getattr(current, "_gpt_nsdl_tokenerror_compat", False):
        return

    original_getsource = current

    def safe_getsource(obj: Any) -> str:
        try:
            return original_getsource(obj)
        except tokenize.TokenError as exc:
            raise OSError("source unavailable for dynamically transformed function") from exc

    safe_getsource._gpt_nsdl_tokenerror_compat = True  # type: ignore[attr-defined]
    inspect.getsource = safe_getsource


_install_streamlit_exec_inspect_compat()


def _d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        if pd.isna(value):
            return Decimal("0")
    except Exception:
        pass
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()


def apply_cas_snapshot_bridge(
    reconciliation: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    cas_holdings: list[dict[str, Any]],
    cas_date: date,
    broker_as_of: date,
    *,
    cas_filename: str,
    cas_account: str,
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve quantity gaps from a dated CAS account snapshot plus later broker trades.

    The CAS snapshot is treated only as quantity evidence. No trade consideration or
    investor cashflow is inferred. A row is resolved only when:
      * the CAS holdings date is strictly before or equal to the broker holdings date;
      * the same ISIN exists in the selected CAS account snapshot;
      * every accepted post-CAS broker trade for that ISIN is applied chronologically;
      * reconstructed terminal quantity equals the broker-reported terminal quantity;
      * no rejected broker source row exists for the ISIN/symbol.

    This is appropriate for missing pre-tradebook/opening history, including demat
    mutual funds, because it bridges from an independently dated depository snapshot.
    """
    if cas_date is None or broker_as_of is None:
        return [dict(r) for r in reconciliation], []
    if cas_date > broker_as_of:
        return [dict(r) for r in reconciliation], []

    rec = [dict(r) for r in reconciliation]
    rejected = rejected or []
    rejected_isins = {
        str(r.get("isin", "")).strip().upper()
        for r in rejected
        if str(r.get("isin", "")).strip()
    }
    rejected_symbols = {
        str(r.get("symbol", "")).strip().upper()
        for r in rejected
        if str(r.get("symbol", "")).strip()
    }

    snapshot: dict[str, Decimal] = {}
    for row in cas_holdings:
        isin = str(row.get("isin", "")).strip().upper()
        if not isin:
            continue
        qty = _d(row.get("quantity_inferred", row.get("quantity")))
        snapshot[isin] = snapshot.get(isin, Decimal("0")) + qty

    post_trade: dict[str, Decimal] = {}
    post_trade_rows: dict[str, int] = {}
    for row in trades:
        isin = str(row.get("isin", "")).strip().upper()
        when = _as_date(row.get("trade_date"))
        if not isin or when is None or when <= cas_date or when > broker_as_of:
            continue
        qty = _d(row.get("signed_quantity"))
        post_trade[isin] = post_trade.get(isin, Decimal("0")) + qty
        post_trade_rows[isin] = post_trade_rows.get(isin, 0) + 1

    ledger: list[dict[str, Any]] = []
    for row in rec:
        if str(row.get("status", "")) == "Quantity agrees":
            continue
        isin = str(row.get("isin", "")).strip().upper()
        symbol = str(row.get("symbol", "")).strip().upper()
        if not isin or isin not in snapshot:
            continue
        if isin in rejected_isins or (symbol and symbol in rejected_symbols):
            continue

        opening_qty = snapshot[isin]
        later_net = post_trade.get(isin, Decimal("0"))
        expected_terminal = opening_qty + later_net
        reported_terminal = _d(row.get("reported_quantity")) if bool(row.get("in_holdings")) else Decimal("0")
        if expected_terminal < -TOL or abs(expected_terminal - reported_terminal) > TOL:
            continue

        raw_diff = _d(row.get("difference"))
        row["raw_difference"] = row.get("raw_difference", row.get("difference"))
        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_EXTERNAL_CAS_SNAPSHOT"
        row["resolution_rule"] = "CAS_SNAPSHOT_PLUS_POST_SNAPSHOT_BROKER_TRADES"
        row["basis"] = (
            str(row.get("basis", ""))
            + f"; reconciled from CAS snapshot {cas_date.isoformat()} plus accepted broker trades after snapshot"
        ).strip("; ")

        ledger.append({
            "asset_class": row.get("asset_class"),
            "symbol": row.get("symbol"),
            "isin": isin,
            "cas_snapshot_date": cas_date,
            "cas_snapshot_quantity": opening_qty,
            "post_snapshot_trade_rows": post_trade_rows.get(isin, 0),
            "post_snapshot_net_trade_quantity": later_net,
            "reconstructed_terminal_quantity": expected_terminal,
            "broker_reported_terminal_quantity": reported_terminal,
            "source_raw_difference": raw_diff,
            "rule": "CAS_SNAPSHOT_PLUS_POST_SNAPSHOT_BROKER_TRADES",
            "verification_level": "EXTERNAL_CAS_ACCOUNT_SNAPSHOT",
            "quantity_gate_effect": "Reconciliation row resolved",
            "cashflow_effect": "None; quantity evidence only",
            "source": cas_filename,
            "cas_account": cas_account,
            "provenance_status": "Selected CAS account snapshot + accepted post-snapshot broker trades matched exactly",
        })

    return rec, ledger
