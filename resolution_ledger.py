from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

TOL = Decimal("0.000001")
COMMON_RATIOS = (
    Decimal("0.2"), Decimal("0.25"), Decimal("0.5"), Decimal("1"),
    Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("10"),
)


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


def _ratio_match(value: Decimal) -> Decimal | None:
    return next((r for r in COMMON_RATIOS if abs(value - r) <= TOL), None)


def _is_rights_entitlement_symbol(symbol: str) -> bool:
    return bool(re.search(r"-RE\d*$", symbol.strip().upper()))


def apply_audited_resolution_ledger(
    reconciliation: list[dict[str, Any]],
    resolution_candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply only deterministic, internally auditable quantity resolutions.

    Two conservative rules are supported:

    1. Unique same-symbol ISIN conversion
       - cash equity (EQ/Equity), same symbol, exactly two unresolved rows;
       - exactly one negative and one positive quantity difference;
       - different ISINs and an exact common conversion ratio;
       - the candidate engine independently produced the same pair;
       - no rejected source row exists for that symbol.

    2. Rights-entitlement quantity credit
       - cash equity symbol ends in -RE or -RE<number>;
       - row is explicitly classified as a rights-entitlement candidate;
       - the security is absent from the ending holdings snapshot;
       - accepted trades have a negative net quantity (net sale);
       - the positive reconciliation difference exactly equals that net sale;
       - no rejected source row exists for that symbol.

    Rule 2 does not invent a cashflow or a purchase. It records the minimum non-cash
    entitlement credit mathematically required by accepted net sales and a zero
    ending holding. External rights-issue provenance remains separately described
    as not independently verified.
    """
    rec = [dict(r) for r in reconciliation]
    candidates = [dict(r) for r in resolution_candidates]
    rejected = rejected or []

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
        key = (
            str(cand.get("symbol", "")).strip().upper(),
            str(cand.get("from_isin", "")).strip().upper(),
            str(cand.get("to_isin", "")).strip().upper(),
        )
        candidate_index[key] = cand

    ledger: list[dict[str, Any]] = []

    # Rule 1: unique same-symbol ISIN conversion.
    for (asset_class, symbol), rows in sorted(groups.items()):
        if asset_class.lower() not in ("eq", "equity"):
            continue
        if not symbol or symbol in rejected_symbols or len(rows) != 2:
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
        ratio = destination_qty / source_qty
        matched = _ratio_match(ratio)
        if matched is None:
            continue

        cand = candidate_index.get((symbol, src_isin, dst_isin))
        if cand is None:
            continue
        cand_ratio = _d(cand.get("candidate_ratio"))
        if abs(cand_ratio - matched) > TOL:
            continue

        src_raw = _d(src.get("difference"))
        dst_raw = _d(dst.get("difference"))
        src["resolution_adjustment"] = -src_raw
        dst["resolution_adjustment"] = -dst_raw
        for row in (src, dst):
            row["difference"] = Decimal("0")
            row["status"] = "Quantity agrees"
            row["exception_class"] = "Quantity agrees"
            row["required_evidence"] = ""
            row["resolution_status"] = "RESOLVED_INTERNAL_DETERMINISTIC"
            row["resolution_rule"] = "UNIQUE_SAME_SYMBOL_ISIN_RATIO"
            row["basis"] = (
                str(row.get("basis", ""))
                + "; internally reconciled by unique same-symbol two-ISIN exact-ratio transformation"
            ).strip("; ")

        cand["status"] = "INTERNALLY RECONCILED"
        cand["verification_level"] = "INTERNAL_DETERMINISTIC"
        cand["required_evidence"] = "External corporate-action/transfer evidence still required for provenance"
        cand["basis"] = "Unique same-symbol two-ISIN opposite quantity gaps at an exact common ratio; applied to quantity reconciliation with audit trail"

        ledger.append({
            "asset_class": asset_class,
            "symbol": symbol,
            "from_isin": src_isin,
            "to_isin": dst_isin,
            "source_raw_difference": src_raw,
            "destination_raw_difference": dst_raw,
            "source_quantity": source_qty,
            "destination_quantity": destination_qty,
            "conversion_ratio": matched,
            "rule": "UNIQUE_SAME_SYMBOL_ISIN_RATIO",
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
        if asset_class.lower() not in ("eq", "equity"):
            continue
        if not symbol or symbol in rejected_symbols or not _is_rights_entitlement_symbol(symbol):
            continue
        if str(row.get("exception_class", "")) != "Rights entitlement candidate":
            continue
        if bool(row.get("in_holdings")):
            continue

        net_trade = _d(row.get("net_trade_quantity"))
        reported = _d(row.get("reported_quantity"))
        raw_diff = _d(row.get("difference"))
        if net_trade >= -TOL or abs(reported) > TOL or raw_diff <= TOL:
            continue
        implied_credit = abs(net_trade)
        if abs(raw_diff - implied_credit) > TOL:
            continue

        row["resolution_adjustment"] = -raw_diff
        row["difference"] = Decimal("0")
        row["status"] = "Quantity agrees"
        row["exception_class"] = "Quantity agrees"
        row["required_evidence"] = ""
        row["resolution_status"] = "RESOLVED_INTERNAL_DETERMINISTIC"
        row["resolution_rule"] = "RIGHTS_ENTITLEMENT_NET_SALE_ZERO_ENDING"
        row["basis"] = (
            str(row.get("basis", ""))
            + "; internally reconciled by non-cash rights-entitlement credit equal to accepted net sale with zero ending holding"
        ).strip("; ")

        ledger.append({
            "asset_class": asset_class,
            "symbol": symbol,
            "isin": str(row.get("isin", "")).strip().upper(),
            "source_raw_difference": raw_diff,
            "net_trade_quantity": net_trade,
            "ending_reported_quantity": reported,
            "inferred_entitlement_credit": implied_credit,
            "rule": "RIGHTS_ENTITLEMENT_NET_SALE_ZERO_ENDING",
            "verification_level": "INTERNAL_DETERMINISTIC",
            "quantity_gate_effect": "Rights-entitlement reconciliation row resolved",
            "cashflow_effect": "None; quantity-only non-cash credit",
            "provenance_status": "External rights-issue/allotment record not independently verified",
        })

    return rec, candidates, ledger


def corporate_resolution_status(result: dict[str, Any]) -> tuple[bool, str]:
    """Return corporate/transfer BLOCK state and auditable evidence text."""
    rec = result.get("reconciliation", pd.DataFrame())
    ledger = result.get("resolution_ledger", pd.DataFrame())
    review = result.get("identifier_review", pd.DataFrame())
    issues = [str(x) for x in result.get("issues", [])]

    resolved_symbols: set[str] = set()
    resolved_count = 0
    if isinstance(ledger, pd.DataFrame) and not ledger.empty:
        resolved_count = len(ledger)
        if "symbol" in ledger.columns:
            resolved_symbols = set(ledger["symbol"].astype(str).str.upper().str.strip())

    unresolved_corp_rows = 0
    if isinstance(rec, pd.DataFrame) and not rec.empty:
        status = rec.get("status", pd.Series(index=rec.index, dtype=str)).astype(str)
        unresolved = rec[status != "Quantity agrees"]
        if not unresolved.empty:
            asset = unresolved.get("asset_class", pd.Series(index=unresolved.index, dtype=str)).astype(str).str.lower()
            exc = unresolved.get("exception_class", pd.Series(index=unresolved.index, dtype=str)).astype(str)
            corp_classes = {
                "Rights entitlement candidate",
                "Opening/transfer quantity candidate",
                "Trade/holding quantity gap",
                "Closed/not-in-snapshot quantity gap",
            }
            unresolved_corp_rows = int(((asset.isin(["eq", "equity"])) & exc.isin(corp_classes)).sum())

    unresolved_multi = 0
    for issue in issues:
        low = issue.lower()
        if "multiple isins" not in low:
            continue
        symbol = ""
        if issue.startswith("Symbol ") and " has multiple ISINs" in issue:
            symbol = issue[len("Symbol "):].split(" has multiple ISINs", 1)[0].strip().upper()
        if not symbol or symbol not in resolved_symbols:
            unresolved_multi += 1

    identifier_blocks = 0
    if isinstance(review, pd.DataFrame) and not review.empty:
        if "status" in review.columns:
            identifier_blocks = int(review["status"].astype(str).str.contains("requires source confirmation", case=False, na=False).sum())
        else:
            identifier_blocks = len(review)

    blocked = bool(unresolved_corp_rows or unresolved_multi or identifier_blocks)
    evidence = (
        f"{resolved_count} deterministic resolution-ledger item(s) internally reconciled; "
        f"{unresolved_corp_rows} unresolved equity corporate/transfer quantity row(s); "
        f"{unresolved_multi} unpaired multi-ISIN warning(s); "
        f"{identifier_blocks} identifier confirmation item(s)"
    )
    return blocked, evidence
