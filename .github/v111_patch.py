from pathlib import Path
import base64, zlib

APP = Path("app.py")
text = APP.read_text()
if '# APP VERSION: 1.1.0' not in text or 'APP_VERSION = "1.1.0"' not in text:
    raise SystemExit("Expected v1.1.0 app baseline not found")
replacement = zlib.decompress(base64.b64decode('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')).decode()
start = text.index("def parse_cas_transactions_layout(")
end = text.index("\n\nSTRICT_NUM_RE", start)
text = text[:start] + replacement + text[end:]
text = text.replace("# APP VERSION: 1.1.0", "# APP VERSION: 1.1.1", 1)
text = text.replace('APP_VERSION = "1.1.0"', 'APP_VERSION = "1.1.1"', 1)
APP.write_text(text)

req = Path("requirements.txt")
r = req.read_text().replace("Requirements version: 1.1.0", "Requirements version: 1.1.1", 1)
req.write_text(r)

readme = Path("README.md")
r = readme.read_text()
r = r.replace("**Repository package version:** 1.1.0", "**Repository package version:** 1.1.1", 1)
r = r.replace("## What v1.1.0 implements", "## What v1.1.1 implements", 1)
marker = "\n## Data-source model\n"
section = '\n### v1.1.1 — transaction text-layer hardening\n\n- assembles dated transaction rows when PDF table cells are split across physical text lines\n- handles split `ISIN :` labels and bare ISIN transaction headers inside validated transaction sections\n- recovers undated MF-folio opening/closing balances used for history-completeness diagnostics\n- keeps depository quantity-only movements separate from MF-folio monetary cash flows\n- preserves the rule that missing trade consideration is never fabricated\n\n'
if "### v1.1.1 — transaction text-layer hardening" not in r:
    if marker not in r: raise SystemExit("README marker not found")
    r = r.replace(marker, section + marker, 1)
readme.write_text("\n".join(line.rstrip() for line in r.splitlines()) + "\n")
