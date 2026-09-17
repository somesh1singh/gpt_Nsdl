from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import requests

TOL = Decimal("0.000001")
COMMON_RATIOS = (
    Decimal("0.2"), Decimal("0.25"), Decimal("0.5"), Decimal("1"),
    Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("10"),
)
NSE_BASE = "https://www.nseindia.com"
NSE_ACTIONS_URL = f"{NSE_BASE}/api/corporates-corporateActions"
NSE_ACTIONS_PAGE = f"{NSE_BASE}/companies-listing/corporate-filings-actions"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_ACTIONS_PAGE,
}
_NSE_ACTION_CACHE: dict[tuple[tuple[str, ...], str, str], list[dict[str, Any]]] = {}


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
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
        return None if pd.isna(parsed) else parsed.date()
    except Exception:
        return None


def _ratio_match(value: Decimal) -> Decimal | None:
    return next((r for r in COMMON_RATIOS if abs(value - r) <= TOL), None)


def _is_rights_entitlement_symbol(symbol: str) -> bool:
    return bool(re.search(r"-RE\d*$", symbol.strip().upper()))


def _parse_bonus_ratio(subject: str) -> tuple[Decimal, Decimal] | None:
    text = str(subject or "")
    if "bonus" not in text.lower():
        return None
    match = re.search(r"(?<!\d)(\d+)\s*:\s*(\d+)(?!\d)", text)
    if not match:
        return None
    numerator, denominator = Decimal(match.group(1)), Decimal(match.group(2))
    if numerator <= 0 or denominator <= 0:
        return None
    return numerator, denominator


def fetch_nse_corporate_actions(
    symbols: list[str],
    start_date: date | None,
    end_date: date | None,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Fetch and normalize official NSE corporate-action evidence.

    Network failure is deliberately non-fatal: an unavailable NSE feed returns an
    empty/partial evidence list, so reconciliation remains blocked rather than
    inventing a corporate action. Only bonus actions are normalized for automatic
    quantity resolution in v1.1.18; rights/splits/dividends are retained as evidence
    but are not auto-applied by the ledger.
    """
    normalized_symbols = tuple(sorted({str(s).strip().upper() for s in symbols if str(s).strip()}))
    if not normalized_symbols:
        return []
    start = start_date or date(2000, 1, 1)
    end = end_date or date.today()
    key = (normalized_symbols, start.isoformat(), end.isoformat())
    if key in _NSE_ACTION_CACHE:
        return [dict(r) for r in _NSE_ACTION_CACHE[key]]

    evidence: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get(NSE_BASE, timeout=timeout)
    except Exception:
        _NSE_ACTION_CACHE[key] = []
        return []

    for symbol in normalized_symbols:
        try:
            response = session.get(
                NSE_ACTIONS_URL,
                params={"index": "equities", "symbol": symbol},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                continue
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                row_symbol = str(raw.get("symbol") or "").strip().upper()
                if row_symbol != symbol:
                    continue
                ex_date = _as_date(raw.get("exDate"))
                record_date = _as_date(raw.get("recDate") or raw.get("recordDate"))
                effective = ex_date or record_date
                if effective is None or effective < start or effective > end:
                    continue
                subject = str(raw.get("subject") or "").strip()
                bonus_ratio = _parse_bonus_ratio(subject)
                low = subject.lower()
                if bonus_ratio:
                    action_type = "BONUS"
                    numerator, denominator = bonus_ratio
                elif "right" in low:
                    action_type = "RIGHTS"
                    numerator = denominator = None
                elif "split" in low or "sub-division" in low or "subdivision" in low:
                    action_type = "SPLIT"
                    numerator = denominator = None
                else:
                    action_type = "OTHER"
                    numerator = denominator = None
                evidence.append({
                    "symbol": row_symbol,
                    "company": str(raw.get("comp") or "").strip(),
                    "series": str(raw.get("series") or "").strip(),
                    "subject": subject,
                    "action_type": action_type,
                    "ex_date": ex_date,
                    "record_date": record_date,
                    "effective_date": effective,
                    "bonus_numerator": numerator,
                    "bonus_denominator": denominator,
                    "verification_level": "EXTERNAL_NSE_VERIFIED",
                    "source": "National Stock Exchange of India corporate actions",
                    "source_url": NSE_ACTIONS_PAGE,
                })
        except Exception:
            continue

    evidence.sort(key=lambda r: (r.get("effective_date") or date.max, r.get("symbol", ""), r.get("subject", "")))
    _NSE_ACTION_CACHE[key] = [dict(r) for r in evidence]
    return evidence


def _trade_rows_for_symbol(trades: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    out = []
    for row in trades:
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        if str(row.get("symbol", "")).strip().upper() != symbol:
            continue
        when = _as_date(row.get("trade_date"))
        qty = _d(row.get("signed_quantity"))
        if when is None or abs(qty) <= TOL:
            continue
        out.append({"trade_date": when, "signed_quantity": qty})
    return sorted(out, key=lambda r: r["trade_date"])


def _bonus_chronology(
    symbol: str,
    trades: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> tuple[Decimal, list[dict[str, Any]]] | None:
    symbol_trades = _trade_rows_for_symbol(trades, symbol)
    bonuses = [
        dict(a) for a in actions
        if str(a.get("symbol", "")).strip().upper() == symbol
        and str(a.get("action_type", "")).upper() == "BONUS"
        and str(a.get("verification_level", "")) == "EXTERNAL_NSE_VERIFIED"
        and _as_date(a.get("effective_date") or a.get("ex_date") or a.get("record_date")) is not None
        and _d(a.get("bonus_numerator")) > 0
        and _d(a.get("bonus_denominator")) > 0
    ]
    if not symbol_trades or not bonuses:
        return None
    bonuses.sort(key=lambda a: _as_date(a.get("effective_date") or a.get("ex_date") or a.get("record_date")) or date.max)

    position = Decimal("0")
    credits: list[dict[str, Any]] = []
    trade_index = 0
    for action in bonuses:
        effective = _as_date(action.get("ex_date")) or _as_date(action.get("record_date")) or _as_date(action.get("effective_date"))
        if effective is None:
            continue
        while trade_index < len(symbol_trades) and symbol_trades[trade_index]["trade_date"] < effective:
            position += symbol_trades[trade_index]["signed_quantity"]
            if position < -TOL:
                return None
            trade_index += 1
        if position <= TOL:
            continue
        numerator = _d(action.get("bonus_numerator"))
        denominator = _d(action.get("bonus_denominator"))
        exact_credit = position * numerator / denominator
        integer_credit = exact_credit.to_integral_value()
        if abs(exact_credit - integer_credit) > TOL:
            return None
        credit = integer_credit
        if credit <= 0:
            continue
        credits.append({
            "effective_date": effective,
            "record_date": _as_date(action.get("record_date")),
            "ex_date": _as_date(action.get("ex_date")),
            "subject": action.get("subject"),
            "pre_action_position": position,
            "bonus_numerator": numerator,
            "bonus_denominator": denominator,
            "bonus_credit": credit,
            "source": action.get("source"),
            "source_url": action.get("source_url"),
        })
        position += credit

    while trade_index < len(symbol_trades):
        position += symbol_trades[trade_index]["signed_quantity"]
        if position < -TOL:
            return None
        trade_index += 1
    return position, credits


def apply_audited_resolution_ledger(
    reconciliation: list[dict[str, Any]],
    resolution_candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]] | None = None,
    corporate_actions: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply only deterministic or externally verified quantity resolutions."""
    rec = [dict(r) for r in reconciliation]
    candidates = [dict(r) for r in resolution_candidates]
    rejected = rejected or []
    trades = trades or []
    corporate_actions = corporate_actions or []

    rejected_symbols = {
        str(r.get("symbol", "")).strip().upper()
        for r in rejected
        if str(r.get("symbol", "")).strip()
    }
    for row in rec:
        row.setdefault("raw_difference", row.get("difference"))
        row.setdefault("resolution_adjustment", Decimal("0"))
        row.setdefault("resolution_status", "UNRESOLVED" if row.get("status") != "Quantity agrees" else "NOT_REQUIRED")
        row.setdefault("resolution_rule", "")

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rec:
        if row.get("status") == "Quantity agrees":
            continue
        key = (str(row.get("asset_class", "")), str(row.get("symbol", "")).strip().upper())
        groups.setdefault(key, []).append(row)

    candidate_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cand in candidates:
        candidate_index[(
            str(cand.get("symbol", "")).strip().upper(),
            str(cand.get("from_isin", "")).strip().upper(),
            str(cand.get("to_isin", "")).strip().upper(),
        )] = cand

    ledger: list[dict[str, Any]] = []

    # Rule 1: unique same-symbol ISIN conversion.
    for (asset_class, symbol), rows in sorted(groups.items()):
        if asset_class.lower() not in ("eq", "equity") or not symbol or symbol in rejected_symbols or len(rows) != 2:
            continue
        negative = [r for r in rows if _d(r.get("difference")) < -TOL]
        positive = [r for r in rows if _d(r.get("difference")) > TOL]
        if len(negative) != 1 or len(positive) != 1:
            continue
        src, dst = negative[0], positive[0]
        src_isin = str(src.get("isin", "")).strip().upper()
        dst_isin = str(dst.get("isin", "")).strip().upper()
        if not src_isin or not dst_isin or src_isin == dst_isin:
            continue
        source_qty = abs(_d(src.get("difference")))
        destination_qty = _d(dst.get("difference"))
        if source_qty <= TOL or destination_qty <= TOL:
            continue
        matched = _ratio_match(destination_qty / source_qty)
        if matched is None:
            continue
        cand = candidate_index.get((symbol, src_isin, dst_isin))
        if cand is None or abs(_d(cand.get("candidate_ratio")) - matched) > TOL:
            continue
        src_raw, dst_raw = _d(src.get("difference")), _d(dst.get("difference"))
        src["resolution_adjustment"], dst["resolution_adjustment"] = -src_raw, -dst_raw
        for row in (src, dst):
            row["difference"] = Decimal("0")
            row["status"] = "Quantity agrees"
            row["exception_class"] = "Quantity agrees"
            row["required_evidence"] = ""
            row["resolution_status"] = "RESOLVED_INTERNAL_DETERMINISTIC"
            row["resolution_rule"] = "UNIQUE_SAME_SYMBOL_ISIN_RATIO"
            row["basis"] = (str(row.get("basis", "")) + "; internally reconciled by unique same-symbol two-ISIN exact-ratio transformation").strip("; ")
        cand["status"] = "INTERNALLY RECONCILED"
        cand["verification_level"] = "INTERNAL_DETERMINISTIC"
        cand["required_evidence"] = "External corporate-action/transfer evidence still required for provenance"
        cand["basis"] = "Unique same-symbol two-ISIN opposite quantity gaps at an exact common ratio; applied to quantity reconciliation with audit trail"
        ledger.append({
            "asset_class": asset_class, "symbol": symbol,
            "from_isin": src_isin, "to_isin": dst_isin,
            "source_raw_difference": src_raw, "destination_raw_difference": dst_raw,
            "source_quantity": source_qty, "destination_quantity": destination_qty,
            "conversion_ratio": matched, "rule": "UNIQUE_SAME_SYMBOL_ISIN_RATIO",
            "verification_level": "INTERNAL_DETERMINISTIC",
            "quantity_gate_effect": "Both paired reconciliation rows resolved",
            "provenance_status": "External corporate-action/transfer evidence not independently verified",
        })

    # Rule 2: rights-entitlement credit implied by accepted net sale and zero ending holding.
    for row in rec:
        if row.get("status") == "Quantity agrees":
            continue
        asset_class = str(row.get("asset_class", ""))
        symbol = str(row.get("symbol", "")).strip().upper()
        if asset_class.lower() not in ("eq", "equity") or not symbol or symbol in rejected_symbols or not _is_rights_entitlement_symbol(symbol):
            continue
        if str(row.get("exception_class", "")) != "Rights entitlement candidate" or bool(row.get("in_holdings")):
            continue
        net_trade, reported, raw_diff = _d(row.get("net_trade_quantity")), _d(row.get("reported_quantity")), _d(row.get("difference"))
        if net_trade >= -TOL or abs(reported) > TOL or raw_diff <= TOL or abs(raw_diff - abs(net_trade)) > TOL:
            continue
        implied_credit = abs(net_trade)
        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_INTERNAL_DETERMINISTIC"
        row["resolution_rule"] = "RIGHTS_ENTITLEMENT_NET_SALE_ZERO_ENDING"
        row["basis"] = (str(row.get("basis", "")) + "; internally reconciled by non-cash rights-entitlement credit equal to accepted net sale with zero ending holding").strip("; ")
        ledger.append({
            "asset_class": asset_class, "symbol": symbol, "isin": str(row.get("isin", "")).strip().upper(),
            "source_raw_difference": raw_diff, "net_trade_quantity": net_trade,
            "ending_reported_quantity": reported, "inferred_entitlement_credit": implied_credit,
            "rule": "RIGHTS_ENTITLEMENT_NET_SALE_ZERO_ENDING", "verification_level": "INTERNAL_DETERMINISTIC",
            "quantity_gate_effect": "Rights-entitlement reconciliation row resolved",
            "cashflow_effect": "None; quantity-only non-cash credit",
            "provenance_status": "External rights-issue/allotment record not independently verified",
        })

    # Rule 3: official NSE bonus action + exact accepted-trade chronology.
    for row in rec:
        if row.get("status") == "Quantity agrees":
            continue
        asset_class = str(row.get("asset_class", ""))
        symbol = str(row.get("symbol", "")).strip().upper()
        raw_diff = _d(row.get("difference"))
        reported = _d(row.get("reported_quantity"))
        net_trade = _d(row.get("net_trade_quantity"))
        if (
            asset_class.lower() not in ("eq", "equity") or not symbol or symbol in rejected_symbols
            or not bool(row.get("in_holdings")) or raw_diff <= TOL
            or str(row.get("exception_class", "")) != "Trade/holding quantity gap"
        ):
            continue
        chronology = _bonus_chronology(symbol, trades, corporate_actions)
        if chronology is None:
            continue
        reconstructed, credits = chronology
        total_credit = sum((_d(c.get("bonus_credit")) for c in credits), Decimal("0"))
        if not credits or abs(reconstructed - reported) > TOL or abs(total_credit - raw_diff) > TOL:
            continue
        if abs(net_trade + total_credit - reported) > TOL:
            continue
        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_EXTERNAL_VERIFIED"
        row["resolution_rule"] = "EXTERNAL_NSE_BONUS_CHRONOLOGY"
        row["basis"] = (str(row.get("basis", "")) + "; reconciled by official NSE bonus evidence and exact accepted-trade chronology").strip("; ")
        ledger.append({
            "asset_class": asset_class, "symbol": symbol, "isin": str(row.get("isin", "")).strip().upper(),
            "source_raw_difference": raw_diff, "net_trade_quantity": net_trade,
            "ending_reported_quantity": reported, "total_bonus_credit": total_credit,
            "bonus_events": len(credits), "bonus_event_audit": " | ".join(
                f"{c['effective_date']} pre={c['pre_action_position']} ratio={c['bonus_numerator']}:{c['bonus_denominator']} credit={c['bonus_credit']}"
                for c in credits
            ),
            "rule": "EXTERNAL_NSE_BONUS_CHRONOLOGY", "verification_level": "EXTERNAL_NSE_VERIFIED",
            "quantity_gate_effect": "Trade/holding reconciliation row resolved",
            "cashflow_effect": "None; bonus quantity-only non-cash credit",
            "source": "National Stock Exchange of India corporate actions",
            "source_url": NSE_ACTIONS_PAGE,
            "provenance_status": "Official NSE corporate-action evidence matched to accepted account trade chronology",
        })

    return rec, candidates, ledger


def corporate_resolution_status(result: dict[str, Any]) -> tuple[bool, str]:
    """Return corporate/transfer BLOCK state from actual unresolved evidence."""
    rec = result.get("reconciliation", pd.DataFrame())
    ledger = result.get("resolution_ledger", pd.DataFrame())
    review = result.get("identifier_review", pd.DataFrame())
    issues = [str(x) for x in result.get("issues", [])]

    resolved_count = len(ledger) if isinstance(ledger, pd.DataFrame) and not ledger.empty else 0
    unresolved_symbols: set[str] = set()
    unresolved_corp_rows = 0
    if isinstance(rec, pd.DataFrame) and not rec.empty:
        status = rec.get("status", pd.Series(index=rec.index, dtype=str)).astype(str)
        unresolved = rec[status != "Quantity agrees"]
        if not unresolved.empty:
            if "symbol" in unresolved.columns:
                unresolved_symbols = set(unresolved["symbol"].astype(str).str.upper().str.strip())
            asset = unresolved.get("asset_class", pd.Series(index=unresolved.index, dtype=str)).astype(str).str.lower()
            exc = unresolved.get("exception_class", pd.Series(index=unresolved.index, dtype=str)).astype(str)
            corp_classes = {
                "Rights entitlement candidate", "Opening/transfer quantity candidate",
                "Trade/holding quantity gap", "Closed/not-in-snapshot quantity gap",
            }
            unresolved_corp_rows = int(((asset.isin(["eq", "equity"])) & exc.isin(corp_classes)).sum())

    unresolved_multi = 0
    historical_multi_nonblocking = 0
    for issue in issues:
        if "multiple isins" not in issue.lower():
            continue
        symbol = ""
        if issue.startswith("Symbol ") and " has multiple ISINs" in issue:
            symbol = issue[len("Symbol "):].split(" has multiple ISINs", 1)[0].strip().upper()
        if not symbol or symbol in unresolved_symbols:
            unresolved_multi += 1
        else:
            historical_multi_nonblocking += 1

    identifier_blocks = 0
    if isinstance(review, pd.DataFrame) and not review.empty:
        if "status" in review.columns:
            identifier_blocks = int(review["status"].astype(str).str.contains("requires source confirmation", case=False, na=False).sum())
        else:
            identifier_blocks = len(review)

    blocked = bool(unresolved_corp_rows or unresolved_multi or identifier_blocks)
    evidence = (
        f"{resolved_count} resolution-ledger item(s) reconciled; "
        f"{unresolved_corp_rows} unresolved equity corporate/transfer quantity row(s); "
        f"{unresolved_multi} blocking multi-ISIN warning(s); "
        f"{historical_multi_nonblocking} historical multi-ISIN warning(s) non-blocking because quantities agree; "
        f"{identifier_blocks} identifier confirmation item(s)"
    )
    return blocked, evidence
