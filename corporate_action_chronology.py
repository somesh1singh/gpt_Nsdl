from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

TOL = Decimal("0.000001")


def _d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
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
    return None


def parse_split_factor(subject: str) -> Decimal | None:
    """Parse an NSE face-value split subject into the share-count multiplier."""
    text = str(subject or "")
    low = text.lower()
    if not ("split" in low or "sub-division" in low or "subdivision" in low):
        return None
    match = re.search(
        r"from\s+(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)\s*/?-?\s*(?:per\s+share)?\s*to\s+(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        old_value, new_value = Decimal(match.group(1)), Decimal(match.group(2))
    else:
        vals = re.findall(r"(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
        if len(vals) < 2:
            return None
        old_value, new_value = Decimal(vals[0]), Decimal(vals[1])
    if old_value <= 0 or new_value <= 0:
        return None
    factor = old_value / new_value
    integer = factor.to_integral_value()
    if abs(factor - integer) > TOL or integer <= 1:
        return None
    return integer


def _trade_rows(trades: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in trades:
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        if str(row.get("symbol", "")).strip().upper() != symbol:
            continue
        when = _as_date(row.get("trade_date"))
        qty = _d(row.get("signed_quantity"))
        if when is None or abs(qty) <= TOL:
            continue
        rows.append({"date": when, "quantity": qty})
    return sorted(rows, key=lambda r: r["date"])


def _verified_actions(actions: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in actions:
        if str(action.get("symbol", "")).strip().upper() != symbol:
            continue
        if str(action.get("verification_level", "")) != "EXTERNAL_NSE_VERIFIED":
            continue
        when = _as_date(action.get("ex_date")) or _as_date(action.get("record_date")) or _as_date(action.get("effective_date"))
        if when is None:
            continue
        kind = str(action.get("action_type", "")).upper()
        if kind == "BONUS":
            numerator = _d(action.get("bonus_numerator"))
            denominator = _d(action.get("bonus_denominator"))
            if numerator <= 0 or denominator <= 0:
                continue
            out.append({**action, "event_date": when, "event_type": "BONUS", "numerator": numerator, "denominator": denominator})
        elif kind == "SPLIT":
            factor = parse_split_factor(str(action.get("subject", "")))
            if factor is None:
                continue
            out.append({**action, "event_date": when, "event_type": "SPLIT", "split_factor": factor})
        # Rights issues are intentionally evidence-only because subscription/allotment
        # is account-specific and cannot be inferred from the public issue ratio.
    return sorted(out, key=lambda a: (a["event_date"], a["event_type"], str(a.get("subject", ""))))


def reconstruct_quantity(
    symbol: str,
    trades: list[dict[str, Any]],
    corporate_actions: list[dict[str, Any]],
) -> tuple[Decimal, list[dict[str, Any]]] | None:
    """Reconstruct final quantity from accepted trades plus official bonus/split actions."""
    symbol_trades = _trade_rows(trades, symbol)
    actions = _verified_actions(corporate_actions, symbol)
    if not symbol_trades or not actions:
        return None

    events: list[dict[str, Any]] = []
    position = Decimal("0")
    trade_index = 0
    for action in actions:
        when = action["event_date"]
        # Trades on ex-date are post-action, so only strictly earlier trades are
        # eligible for that corporate action in the reconstructed account position.
        while trade_index < len(symbol_trades) and symbol_trades[trade_index]["date"] < when:
            position += symbol_trades[trade_index]["quantity"]
            if position < -TOL:
                return None
            trade_index += 1
        if position <= TOL:
            continue

        pre = position
        if action["event_type"] == "BONUS":
            exact_credit = pre * action["numerator"] / action["denominator"]
            credit = exact_credit.to_integral_value()
            if abs(exact_credit - credit) > TOL or credit <= 0:
                return None
            position += credit
            events.append({
                "date": when,
                "type": "BONUS",
                "subject": action.get("subject"),
                "pre_position": pre,
                "adjustment": credit,
                "post_position": position,
                "ratio": f"{action['numerator']}:{action['denominator']}",
                "source": action.get("source"),
                "source_url": action.get("source_url"),
            })
        elif action["event_type"] == "SPLIT":
            factor = action["split_factor"]
            post = pre * factor
            if abs(post - post.to_integral_value()) > TOL:
                return None
            adjustment = post - pre
            position = post
            events.append({
                "date": when,
                "type": "SPLIT",
                "subject": action.get("subject"),
                "pre_position": pre,
                "adjustment": adjustment,
                "post_position": position,
                "ratio": f"x{factor}",
                "source": action.get("source"),
                "source_url": action.get("source_url"),
            })

    while trade_index < len(symbol_trades):
        position += symbol_trades[trade_index]["quantity"]
        if position < -TOL:
            return None
        trade_index += 1
    return position, events


def apply_verified_quantity_actions(
    reconciliation: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    corporate_actions: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve remaining equity rows only when public actions reconstruct exact ending quantity."""
    rec = [dict(r) for r in reconciliation]
    rejected_symbols = {
        str(r.get("symbol", "")).strip().upper()
        for r in (rejected or [])
        if str(r.get("symbol", "")).strip()
    }
    ledger: list[dict[str, Any]] = []

    for row in rec:
        if row.get("status") == "Quantity agrees":
            continue
        if str(row.get("asset_class", "")).lower() not in ("eq", "equity"):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or symbol in rejected_symbols or re.search(r"-RE\d*$", symbol):
            continue
        rebuilt = reconstruct_quantity(symbol, trades, corporate_actions)
        if rebuilt is None:
            continue
        reconstructed, events = rebuilt
        reported = _d(row.get("reported_quantity")) if bool(row.get("in_holdings")) else Decimal("0")
        if abs(reconstructed - reported) > TOL or not events:
            continue

        raw_diff = _d(row.get("difference"))
        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_EXTERNAL_VERIFIED"
        row["resolution_rule"] = "EXTERNAL_NSE_BONUS_SPLIT_CHRONOLOGY"
        row["basis"] = (
            str(row.get("basis", ""))
            + "; reconciled by official NSE bonus/split evidence and exact accepted-trade chronology"
        ).strip("; ")
        ledger.append({
            "asset_class": row.get("asset_class"),
            "symbol": symbol,
            "isin": str(row.get("isin", "")).strip().upper(),
            "source_raw_difference": raw_diff,
            "net_trade_quantity": _d(row.get("net_trade_quantity")),
            "ending_reported_quantity": reported,
            "reconstructed_quantity": reconstructed,
            "corporate_action_events": len(events),
            "corporate_action_audit": " | ".join(
                f"{e['date']} {e['type']} pre={e['pre_position']} {e['ratio']} adj={e['adjustment']} post={e['post_position']}"
                for e in events
            ),
            "rule": "EXTERNAL_NSE_BONUS_SPLIT_CHRONOLOGY",
            "verification_level": "EXTERNAL_NSE_VERIFIED",
            "quantity_gate_effect": "Reconciliation row resolved",
            "cashflow_effect": "None; corporate-action quantity adjustment only",
            "source": "National Stock Exchange of India corporate actions",
            "source_url": next((e.get("source_url") for e in events if e.get("source_url")), None),
            "provenance_status": "Official NSE bonus/split evidence matched exactly to accepted account trade chronology",
        })
    return rec, ledger
