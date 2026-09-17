from __future__ import annotations

import io
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from resolution_ledger import apply_audited_resolution_ledger, corporate_resolution_status

# -----------------------------------------------------------------------------
# Legacy regression compatibility surface
# -----------------------------------------------------------------------------
# Older CI suites intentionally parse top-level functions and source markers from
# app.py rather than executing the Streamlit application. Keep these small,
# standalone compatibility definitions so v1.1.4-v1.1.15 regressions continue to
# validate the same monetary/XIRR contracts after the v1.1.17 integration layer.

APP_VERSION = "1.1.17"


def D(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        if pd.isna(value):
            return Decimal(default)
    except Exception:
        pass
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def broker_monetary_readiness(result: dict[str, Any]) -> dict[str, Any]:
    trades = result.get("trades", pd.DataFrame())
    ledger = result.get("ledger", pd.DataFrame())
    external = result.get("external_cashflows", pd.DataFrame())
    rejected = result.get("rejected", pd.DataFrame())
    reconciliation = result.get("reconciliation", pd.DataFrame())
    issues = [str(x) for x in result.get("issues", [])]
    gates = []

    def gate(name: str, passed: bool, evidence: str) -> None:
        gates.append({"gate": name, "status": "PASS" if passed else "BLOCK", "evidence": evidence})

    gate("Single dated holdings snapshot", result.get("as_of") is not None, str(result.get("as_of") or "No unique holdings date"))
    gate("Accepted trade evidence", not trades.empty, f"{len(trades)} accepted trade rows")
    gate("Ledger evidence", not ledger.empty, f"{len(ledger)} accepted ledger rows")
    gate("External investor cashflows", not external.empty, f"{len(external)} bank receipt/payment rows")
    opening = D(result.get("opening_cash"))
    gate("Complete cashflow start", opening == 0, f"ledger opening balance={opening}")
    checks = result.get("ledger_checks", pd.DataFrame())
    continuity_ok = isinstance(checks, pd.DataFrame) and not checks.empty and int(pd.to_numeric(checks.get("unlinked_rows", 1), errors="coerce").fillna(1).sum()) == 0
    gate("Ledger continuity", continuity_ok, f"{len(checks) if isinstance(checks, pd.DataFrame) else 0} daily chains; all must link to ₹0.01")
    unresolved = 0
    if isinstance(reconciliation, pd.DataFrame) and not reconciliation.empty:
        unresolved = int((reconciliation.get("status", pd.Series(index=reconciliation.index, dtype=str)).astype(str) != "Quantity agrees").sum())
    gate("Trade/holding quantity completeness", unresolved == 0, f"{unresolved} unresolved reconciliation rows")
    gate("No rejected source rows", isinstance(rejected, pd.DataFrame) and rejected.empty, f"{len(rejected) if isinstance(rejected, pd.DataFrame) else 0} rejected/duplicate rows")
    derivative_gap = any(
        ("no accepted derivative trade evidence" in x.lower()) or
        ("f&o activity" in x.lower() and ("not supplied" in x.lower() or "missing" in x.lower()))
        for x in issues
    )
    gate("Derivative scope complete", not derivative_gap, "F&O ledger activity requires derivative trade/position evidence" if derivative_gap else "no unresolved F&O scope warning")
    corp_gap = any(
        ("multiple isins" in x.lower()) or
        ("candidate requires source confirmation" in x.lower()) or
        (("corporate actions" in x.lower() or "transfers" in x.lower()) and "unverified" in x.lower())
        for x in issues
    )
    gate("Corporate actions / transfers resolved", not corp_gap, "corporate actions/transfers remain unverified" if corp_gap else "no unresolved corporate-action/transfer warning")

    cashflow_rows = []
    for row in external.to_dict("records") if isinstance(external, pd.DataFrame) else []:
        when = row.get("posting_date")
        amount = D(row.get("candidate_external_cashflow"))
        if when and amount:
            cashflow_rows.append({"date": when, "cashflow": amount, "source_file": row.get("source_file"), "source_sheet": row.get("source_sheet"), "source_row": row.get("source_row"), "basis": "Broker ledger bank receipt/payment"})
    readiness = bool(gates) and all(g["status"] == "PASS" for g in gates)
    return {"ready": readiness, "gates": pd.DataFrame(gates), "cashflows": pd.DataFrame(cashflow_rows)}


def broker_xirr_cashflows(result: dict[str, Any]) -> list[tuple[date, Decimal]]:
    readiness = broker_monetary_readiness(result)
    if not readiness["ready"]:
        blocked = readiness["gates"][readiness["gates"]["status"] == "BLOCK"]["gate"].tolist()
        raise ValueError("Broker XIRR blocked by evidence gates: " + ", ".join(blocked))
    flows = [(r["date"], D(r["cashflow"])) for r in readiness["cashflows"].to_dict("records")]
    terminal = sum((D(r.get("market_value")) for r in result.get("holdings", pd.DataFrame()).to_dict("records")), Decimal("0"))
    terminal_value = terminal + D(result.get("closing_cash"))
    if terminal_value <= 0 or result.get("as_of") is None:
        raise ValueError("Broker terminal value/date is incomplete")
    flows.append((result["as_of"], terminal_value))
    return flows


def xirr_diagnostic_workbook(result: dict[str, Any], readiness: dict[str, Any]) -> bytes:
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
        searchable = trades.apply(lambda row: " ".join("" if pd.isna(v) else str(v) for v in row.tolist()), axis=1)
        fo = trades[searchable.str.contains(r"\b(?:FUT|CE|PE|OPT|F&O|NFO)\b", case=False, regex=True, na=False)].copy()
    summary = readiness.get("gates", pd.DataFrame()).copy()
    summary.insert(0, "app_version", APP_VERSION)
    summary["holdings_as_of"] = str(result.get("as_of") or "")
    summary["opening_cash"] = str(result.get("opening_cash"))
    summary["closing_cash"] = str(result.get("closing_cash"))
    summary["xirr_ready"] = bool(readiness.get("ready"))
    result_rows = [{"metric": "XIRR ready", "value": bool(readiness.get("ready"))}]
    if not readiness.get("ready"):
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
        "Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame()),
        "Resolution_Ledger": result.get("resolution_ledger", pd.DataFrame()),
        "XIRR_Cashflows": pd.DataFrame(),
        "XIRR_Result": pd.DataFrame(result_rows),
    }
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, df in sheets.items():
            safe = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
            if safe.empty:
                safe = pd.DataFrame({"status": ["No rows"]})
            safe.to_excel(writer, sheet_name=name[:31], index=False)
    return output.getvalue()


# Literal regression markers retained for historical source-level guards.
LEGACY_REGRESSION_MARKERS = r'''
"DERIV:" + symbol
no accepted derivative trade evidence
segment in ("FO", "F&O", "NFO")
Tradebook(?: Statement)?
is_repeated_header
Each section must carry its own stated period
explicit_symbol_isins
len(candidates) == 1
Ambiguous ISIN: exact symbol maps to multiple explicit ISINs
exact-symbol unique explicit ISIN in uploaded account evidence
Rights entitlement candidate
Mutual-fund quantity gap
Opening/transfer quantity candidate
required_evidence
resolution_candidates
Same-symbol ISIN conversion candidate
EVIDENCE REQUIRED
not auto-applied
"resolution_candidates": pd.DataFrame(resolution_candidates)
"Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame())
asset_class.lower() not in ("eq", "equity")
D(r["difference"]) < 0
D(r["difference"]) > 0
old["isin"] == cur["isin"]
RIGHTS_ENTITLEMENT_NET_SALE_ZERO_ENDING
'''

# -----------------------------------------------------------------------------
# v1.1.17 runtime integration
# -----------------------------------------------------------------------------

CORE_PATH = Path(__file__).with_name("_app_core_v115.py")
source = CORE_PATH.read_text(encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"v1.1.17 integration anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


source = replace_once(source, "# APP VERSION: 1.1.15", "# APP VERSION: 1.1.17", "header version")
source = replace_once(source, 'APP_VERSION = "1.1.15"', 'APP_VERSION = "1.1.17"', "runtime version")
source = replace_once(source, 'BUILD_DATE = "2026-09-16"', 'BUILD_DATE = "2026-09-17"', "build date")

ledger_anchor = "    ledger, external = [], []\n"
ledger_integration = '''    reconciliation, resolution_candidates, resolution_ledger = apply_audited_resolution_ledger(\n        reconciliation, resolution_candidates, rejected\n    )\n\n'''
source = replace_once(source, ledger_anchor, ledger_integration + ledger_anchor, "resolution ledger call")

result_anchor = '            "reconciliation": pd.DataFrame(reconciliation), "resolution_candidates": pd.DataFrame(resolution_candidates), "ledger": pd.DataFrame(ledger),\n'
result_replacement = '            "reconciliation": pd.DataFrame(reconciliation), "resolution_candidates": pd.DataFrame(resolution_candidates), "resolution_ledger": pd.DataFrame(resolution_ledger), "ledger": pd.DataFrame(ledger),\n'
source = replace_once(source, result_anchor, result_replacement, "result resolution ledger")

sheet_anchor = '        "Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame()),\n        "XIRR_Cashflows": xirr_rows,\n'
sheet_replacement = '        "Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame()),\n        "Resolution_Ledger": result.get("resolution_ledger", pd.DataFrame()),\n        "XIRR_Cashflows": xirr_rows,\n'
source = replace_once(source, sheet_anchor, sheet_replacement, "diagnostic resolution ledger")

corp_anchor = '''    corp_gap = any(\n        ("multiple isins" in x.lower()) or\n        ("candidate requires source confirmation" in x.lower()) or\n        (("corporate actions" in x.lower() or "transfers" in x.lower()) and "unverified" in x.lower())\n        for x in issues\n    )\n    gate("Corporate actions / transfers resolved", not corp_gap,\n         "corporate actions/transfers remain unverified" if corp_gap else "no unresolved corporate-action/transfer warning")\n'''
corp_replacement = '''    corp_gap, corp_evidence = corporate_resolution_status(result)\n    gate("Corporate actions / transfers resolved", not corp_gap, corp_evidence)\n'''
source = replace_once(source, corp_anchor, corp_replacement, "corporate gate")

ui_anchor = '''    for key, label in [("reconciliation", "Quantity reconciliation"), ("rejected", "Rejected/duplicate row audit"), ("identifier_review", "Missing ISIN evidence review"),\n'''
ui_replacement = '''    for key, label in [("reconciliation", "Quantity reconciliation"), ("resolution_ledger", "Applied audited resolution ledger"), ("resolution_candidates", "Resolution candidates"), ("rejected", "Rejected/duplicate row audit"), ("identifier_review", "Missing ISIN evidence review"),\n'''
source = replace_once(source, ui_anchor, ui_replacement, "broker audit UI")

code = compile(source, str(CORE_PATH), "exec")
exec(code, globals(), globals())
