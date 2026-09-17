from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any

import pandas as pd

TOL = Decimal("0.000001")
UCC_FILENAME_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2,4}\d{3,6})(?![A-Z0-9])")


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


def infer_consistent_ucc_from_filenames(
    filenames: list[str] | tuple[str, ...],
    *,
    min_votes: int = 2,
) -> str | None:
    """Return one repeated broker UCC token from independent filenames.

    This is deliberately only a provisional identity source. The historical
    holdings bridge still requires the dated Zerodha workbook itself to expose
    the same Client ID before any quantity row can be resolved.
    """
    counts: dict[str, int] = {}
    for filename in filenames or []:
        matches = set(UCC_FILENAME_RE.findall(str(filename).upper()))
        for candidate in matches:
            counts[candidate] = counts.get(candidate, 0) + 1

    winners = sorted(candidate for candidate, votes in counts.items() if votes >= min_votes)
    return winners[0] if len(winners) == 1 else None


def bootstrap_session_account_id_from_filenames() -> str | None:
    """Populate missing broker account identity only from repeated filename evidence.

    Streamlit broker imports sometimes omit Client ID from the parsed report
    headers even though multiple Zerodha exports carry the same UCC in their
    filenames. When that happens, store the repeated token as a *provisional*
    account id. Resolution remains blocked unless a dated historical Zerodha
    holdings workbook contains the exact same embedded Client ID.
    """
    try:
        import streamlit as st
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx() is None:
            return None
        result = st.session_state.get("broker_result")
        if not isinstance(result, dict) or result.get("account_ids"):
            return None
        candidate = infer_consistent_ucc_from_filenames(
            st.session_state.get("broker_file_names", []) or []
        )
        if not candidate:
            return None

        result["account_ids"] = [candidate]
        result["account_id_provenance"] = "CONSISTENT_BROKER_FILENAMES_PENDING_HISTORICAL_HEADER_MATCH"
        issues = list(result.get("issues", []) or [])
        note = (
            f"Broker Client ID candidate {candidate} derived from at least two consistent broker filenames; "
            "historical holdings resolution still requires the same embedded Client ID in the dated Zerodha workbook."
        )
        if note not in issues:
            issues.append(note)
        result["issues"] = issues
        st.session_state.broker_result = result
        return candidate
    except Exception:
        # Identity fallback must never prevent the application from starting.
        return None


# Initial import-time attempt. In Streamlit this may occur before broker files are
# loaded, so plan_historical_holdings_dates() repeats the attempt on every rerun.
bootstrap_session_account_id_from_filenames()


def plan_historical_holdings_dates(
    reconciliation: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build an evidence request for unresolved equity quantities.

    The suggested date is one calendar day before the earliest accepted trade in
    the supplied broker history for the ISIN. If that day is not selectable in
    Zerodha Console (for example a weekend), the user can choose the nearest
    earlier available date. The bridge itself does not rely on the suggestion;
    it validates any uploaded dated snapshot mathematically.
    """
    # The planner is executed on every Streamlit rerun immediately before the
    # historical bridge button. Refresh provisional account identity here because
    # the module itself may have been imported before broker files were loaded.
    bootstrap_session_account_id_from_filenames()

    by_isin: dict[str, list[date]] = {}
    for row in trades:
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        isin = str(row.get("isin", "")).strip().upper()
        when = _as_date(row.get("trade_date"))
        if isin and when is not None:
            by_isin.setdefault(isin, []).append(when)

    plan: list[dict[str, Any]] = []
    for row in reconciliation:
        if str(row.get("status", "")) == "Quantity agrees":
            continue
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        isin = str(row.get("isin", "")).strip().upper()
        dates = sorted(by_isin.get(isin, []))
        if not dates:
            continue
        first_trade = dates[0]
        plan.append({
            "symbol": row.get("symbol"),
            "isin": isin,
            "exception_class": row.get("exception_class"),
            "net_trade_quantity": row.get("net_trade_quantity"),
            "first_accepted_trade_date": first_trade,
            "last_accepted_trade_date": dates[-1],
            "suggested_snapshot_date": first_trade - timedelta(days=1),
            "required_evidence": "Zerodha historical holdings XLSX for this account dated before the first accepted trade; nearest earlier available date is acceptable",
        })
    return plan


def apply_historical_holdings_bridge(
    reconciliation: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    broker_as_of: date,
    *,
    current_account_ids: list[str] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve quantity gaps from dated Zerodha holdings snapshots.

    Each snapshot item must contain: as_of, filename, account_ids and holdings.
    `holdings` is a list of rows from the broker holdings parser. The rule is
    quantity-only: snapshot quantity + accepted trades strictly after snapshot
    through the current holdings date must equal the current terminal quantity.
    No purchase, sale consideration, corporate action or investor cashflow is
    invented. Rejected source evidence blocks resolution for the affected ISIN.
    """
    if broker_as_of is None:
        return [dict(r) for r in reconciliation], []

    current_ids = {str(x).strip().upper() for x in (current_account_ids or []) if str(x).strip()}
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

    normalized: list[dict[str, Any]] = []
    for snap in snapshots:
        snap_date = _as_date(snap.get("as_of"))
        if snap_date is None or snap_date > broker_as_of:
            continue
        snap_ids = {str(x).strip().upper() for x in snap.get("account_ids", []) if str(x).strip()}
        if current_ids:
            if not snap_ids:
                raise ValueError(f"{snap.get('filename', 'Historical holdings')}: Client ID is missing")
            if snap_ids != current_ids:
                raise ValueError(f"{snap.get('filename', 'Historical holdings')}: Client ID does not match current broker evidence")
        quantities: dict[str, Decimal] = {}
        for row in snap.get("holdings", []):
            if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
                continue
            isin = str(row.get("isin", "")).strip().upper()
            if not isin:
                continue
            qty = _d(row.get("quantity_available", row.get("quantity")))
            if qty < 0:
                raise ValueError(f"{snap.get('filename', 'Historical holdings')}: negative holding quantity")
            if isin in quantities:
                raise ValueError(f"{snap.get('filename', 'Historical holdings')}: duplicate ISIN {isin}")
            quantities[isin] = qty
        normalized.append({
            "as_of": snap_date,
            "filename": str(snap.get("filename", "Historical holdings")),
            "account_ids": sorted(snap_ids),
            "quantities": quantities,
        })

    normalized.sort(key=lambda s: s["as_of"], reverse=True)
    rec = [dict(r) for r in reconciliation]
    ledger: list[dict[str, Any]] = []

    trade_rows: dict[str, list[dict[str, Any]]] = {}
    for row in trades:
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        isin = str(row.get("isin", "")).strip().upper()
        when = _as_date(row.get("trade_date"))
        if isin and when is not None:
            trade_rows.setdefault(isin, []).append(row)

    for row in rec:
        if str(row.get("status", "")) == "Quantity agrees":
            continue
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        isin = str(row.get("isin", "")).strip().upper()
        symbol = str(row.get("symbol", "")).strip().upper()
        if not isin or isin in rejected_isins or (symbol and symbol in rejected_symbols):
            continue

        reported_terminal = _d(row.get("reported_quantity")) if bool(row.get("in_holdings")) else Decimal("0")
        selected: dict[str, Any] | None = None
        for snap in normalized:
            if isin not in snap["quantities"]:
                continue
            opening_qty = snap["quantities"][isin]
            post_rows = []
            later_net = Decimal("0")
            for trade in trade_rows.get(isin, []):
                when = _as_date(trade.get("trade_date"))
                if when is None or when <= snap["as_of"] or when > broker_as_of:
                    continue
                later_net += _d(trade.get("signed_quantity"))
                post_rows.append(trade)
            expected_terminal = opening_qty + later_net
            if expected_terminal < -TOL or abs(expected_terminal - reported_terminal) > TOL:
                continue
            selected = {
                "snapshot": snap,
                "opening_qty": opening_qty,
                "later_net": later_net,
                "post_rows": len(post_rows),
                "expected_terminal": expected_terminal,
            }
            break

        if selected is None:
            continue

        raw_diff = _d(row.get("difference"))
        snap = selected["snapshot"]
        row["raw_difference"] = row.get("raw_difference", row.get("difference"))
        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_EXTERNAL_HISTORICAL_HOLDINGS"
        row["resolution_rule"] = "HISTORICAL_HOLDINGS_PLUS_POST_SNAPSHOT_TRADES"
        row["basis"] = (
            str(row.get("basis", ""))
            + f"; reconciled from Zerodha holdings snapshot {snap['as_of'].isoformat()} plus accepted broker trades after snapshot"
        ).strip("; ")

        ledger.append({
            "asset_class": row.get("asset_class"),
            "symbol": row.get("symbol"),
            "isin": isin,
            "historical_snapshot_date": snap["as_of"],
            "historical_snapshot_quantity": selected["opening_qty"],
            "post_snapshot_trade_rows": selected["post_rows"],
            "post_snapshot_net_trade_quantity": selected["later_net"],
            "reconstructed_terminal_quantity": selected["expected_terminal"],
            "broker_reported_terminal_quantity": reported_terminal,
            "source_raw_difference": raw_diff,
            "rule": "HISTORICAL_HOLDINGS_PLUS_POST_SNAPSHOT_TRADES",
            "verification_level": "EXTERNAL_ZERODHA_HOLDINGS_SNAPSHOT",
            "quantity_gate_effect": "Reconciliation row resolved",
            "cashflow_effect": "None; quantity evidence only",
            "source": snap["filename"],
            "account_ids": ", ".join(snap["account_ids"]),
            "provenance_status": "Dated Zerodha holdings snapshot + accepted post-snapshot broker trades matched exactly",
        })

    return rec, ledger
