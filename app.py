from __future__ import annotations

from pathlib import Path

from resolution_ledger import apply_audited_resolution_ledger, corporate_resolution_status

CORE_PATH = Path(__file__).with_name("_app_core_v115.py")
source = CORE_PATH.read_text(encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"v1.1.16 integration anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


source = replace_once(source, "# APP VERSION: 1.1.15", "# APP VERSION: 1.1.16", "header version")
source = replace_once(source, 'APP_VERSION = "1.1.15"', 'APP_VERSION = "1.1.16"', "runtime version")
source = replace_once(source, 'BUILD_DATE = "2026-09-16"', 'BUILD_DATE = "2026-09-17"', "build date")

# Apply the audited resolution ledger after v1.1.15 candidate generation and before
# monetary reconciliation gates consume the reconciliation table.
ledger_anchor = "    ledger, external = [], []\n"
ledger_integration = '''    reconciliation, resolution_candidates, resolution_ledger = apply_audited_resolution_ledger(\n        reconciliation, resolution_candidates, rejected\n    )\n\n'''
source = replace_once(source, ledger_anchor, ledger_integration + ledger_anchor, "resolution ledger call")

# Persist the audited ledger in the broker result object.
result_anchor = '            "reconciliation": pd.DataFrame(reconciliation), "resolution_candidates": pd.DataFrame(resolution_candidates), "ledger": pd.DataFrame(ledger),\n'
result_replacement = '            "reconciliation": pd.DataFrame(reconciliation), "resolution_candidates": pd.DataFrame(resolution_candidates), "resolution_ledger": pd.DataFrame(resolution_ledger), "ledger": pd.DataFrame(ledger),\n'
source = replace_once(source, result_anchor, result_replacement, "result resolution ledger")

# Export both candidate detection and applied audited transformations.
sheet_anchor = '        "Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame()),\n        "XIRR_Cashflows": xirr_rows,\n'
sheet_replacement = '        "Resolution_Candidates": result.get("resolution_candidates", pd.DataFrame()),\n        "Resolution_Ledger": result.get("resolution_ledger", pd.DataFrame()),\n        "XIRR_Cashflows": xirr_rows,\n'
source = replace_once(source, sheet_anchor, sheet_replacement, "diagnostic resolution ledger")

# Replace the warning-string corporate-action gate with a data-derived gate that
# acknowledges internally reconciled transformations while preserving BLOCK for
# remaining equity corporate/transfer exceptions or identifier ambiguity.
corp_anchor = '''    corp_gap = any(\n        ("multiple isins" in x.lower()) or\n        ("candidate requires source confirmation" in x.lower()) or\n        (("corporate actions" in x.lower() or "transfers" in x.lower()) and "unverified" in x.lower())\n        for x in issues\n    )\n    gate("Corporate actions / transfers resolved", not corp_gap,\n         "corporate actions/transfers remain unverified" if corp_gap else "no unresolved corporate-action/transfer warning")\n'''
corp_replacement = '''    corp_gap, corp_evidence = corporate_resolution_status(result)\n    gate("Corporate actions / transfers resolved", not corp_gap, corp_evidence)\n'''
source = replace_once(source, corp_anchor, corp_replacement, "corporate gate")

# Make the audit trail visible in the Broker Imports page as well as XLSX export.
ui_anchor = '''    for key, label in [("reconciliation", "Quantity reconciliation"), ("rejected", "Rejected/duplicate row audit"), ("identifier_review", "Missing ISIN evidence review"),\n'''
ui_replacement = '''    for key, label in [("reconciliation", "Quantity reconciliation"), ("resolution_ledger", "Applied audited resolution ledger"), ("resolution_candidates", "Resolution candidates"), ("rejected", "Rejected/duplicate row audit"), ("identifier_review", "Missing ISIN evidence review"),\n'''
source = replace_once(source, ui_anchor, ui_replacement, "broker audit UI")

# Compile the integrated source before execution so broken anchors never silently
# degrade the financial logic.
code = compile(source, str(CORE_PATH), "exec")
exec(code, globals(), globals())
