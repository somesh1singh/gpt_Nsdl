from pathlib import Path
import re

app_path = Path('app.py')
text = app_path.read_text()
text = text.replace('# APP VERSION: 1.0.9', '# APP VERSION: 1.0.10')
text = text.replace('APP_VERSION = "1.0.9"', 'APP_VERSION = "1.0.10"')

pattern = re.compile(
    r'def find_sgb_values\(\n'
    r'    chunk: list\[str\],\n'
    r'\) -> tuple\[Decimal, Decimal, Decimal, Decimal, Decimal\] \| None:\n'
    r'.*?\n\n\ndef clean_record_chunk',
    re.S,
)

replacement = '''def find_sgb_values(
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

    # Always prefer a genuine market-price layout over the historical fallback,
    # then select the candidate with the smallest arithmetic error.
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, err, units, face, market_price, market_value = candidates[0]
    return units, face, market_price, market_value, err


def clean_record_chunk'''

text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f'SGB function replacement count={count}')

text = text.replace(
    '    v1.0.9 also recovers CDSL rows contaminated by description numbers or page numbers.\n',
    '    v1.0.10 also supports arithmetic-validated historical SGB face-value layouts.\n',
    1,
)

status_anchor = '            ["CDSL numeric-tail/page-number recovery", "Implemented", APP_VERSION],\n'
status_add = status_anchor + '            ["Historical SGB face-value reconciliation", "Implemented", APP_VERSION],\n'
if status_anchor in text and 'Historical SGB face-value reconciliation' not in text:
    text = text.replace(status_anchor, status_add, 1)

app_path.write_text(text)

req_path = Path('requirements.txt')
req_text = req_path.read_text().replace('Requirements version: 1.0.9', 'Requirements version: 1.0.10')
req_path.write_text(req_text)

readme_path = Path('README.md')
readme = readme_path.read_text()
readme = readme.replace('**Repository package version:** 1.0.9', '**Repository package version:** 1.0.10')
readme = readme.replace('## What v1.0.9 implements', '## What v1.0.10 implements')
readme = readme.replace('No additional source files are required for v1.0.9.', 'No additional source files are required for v1.0.10.')
readme = readme.replace('The parser in v1.0.9 is a **defensive heuristic parser**', 'The parser in v1.0.10 is a **defensive heuristic parser**')
marker = '\n## Next version candidates\n'
section = '''
### v1.0.10 — historical SGB reconciliation cleanup

- adds a conservative historical SGB layout fallback for statements that contain units, face value and reported value but no separate market-price field
- requires `units × face value ≈ reported value` within the existing 2% arithmetic quality gate
- does not invent a historical market price; face value is used only when that is the valuation basis printed by the statement
- preserves the modern SGB parser as the preferred path whenever a genuine market price is present
- targets the repeated ₹9,464.01 residual observed in the Mar-2024 and Mar-2025 validation exports

'''
if '### v1.0.10 — historical SGB reconciliation cleanup' not in readme:
    if marker not in readme:
        raise SystemExit('README next-version marker not found')
    readme = readme.replace(marker, section + marker, 1)
# Remove trailing whitespace so CI validation is deterministic.
readme = '\n'.join(line.rstrip() for line in readme.splitlines()) + '\n'
readme_path.write_text(readme)
