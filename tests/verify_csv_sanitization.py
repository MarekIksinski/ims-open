#!/usr/bin/env python
"""
Unit gate for database_utils.sanitize_df_for_csv (audit 2026-10-31 #4,
spreadsheet formula injection in CSV exports).

Asserts: =, +, -, @, tab and CR prefixed string cells gain a leading
apostrophe; clean strings, numbers, None/NaN and numeric columns (incl.
negative numbers) are untouched; pandas-3 'str' dtype columns are
covered (the object-dtype trap).
Run with the streamlit venv python.
"""
import os, sys
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import database_utils as du

df = pd.DataFrame([
    {"name": "=HYPERLINK(\"http://evil\")", "cat": "fresh", "qty": 5},
    {"name": "+SUM(1,2)", "cat": "@SUM(1,2)", "qty": -3},
    {"name": "-4+1", "cat": "\tTabbed", "qty": None},
    {"name": "\rCRLFbreak", "cat": None, "qty": 0},
    {"name": "Normal Name", "cat": "", "qty": 1},
])
out = du.sanitize_df_for_csv(df.copy())

# every dangerous prefix must gain the apostrophe
assert out["name"].tolist() == [
    "'=HYPERLINK(\"http://evil\")", "'+SUM(1,2)", "'-4+1", "'\rCRLFbreak", "Normal Name",
], f"names not sanitised as expected: {out['name'].tolist()!r}"
assert out["cat"].tolist()[1] == "'@SUM(1,2)"
assert out["cat"].tolist()[2] == "'\tTabbed"

# clean values untouched
assert out["cat"].tolist()[0] == "fresh" and out["cat"].tolist()[4] == ""
assert out["name"].tolist()[4] == "Normal Name"

# numbers untouched (incl. negative — a sign is not a formula)
assert list(out["qty"]) == [5, -3, None, 0, 1] or (
    out["qty"].fillna("N").tolist() == [5, -3, "N", 0, 1]), \
    f"numeric column must be untouched: {out['qty'].tolist()!r}"
assert du.pd.api.types.is_numeric_dtype(out["qty"]), "qty must stay numeric"

# the exported CSV must never START a row with a dangerous cell
csv_lines = out.to_csv(index=False).splitlines()[1:]
for line in csv_lines:
    assert line[:1] not in ("=", "+", "@", "\t", "\r"), \
        f"row starts with an unsanitized dangerous cell: {line!r}"

print("PASS: csv sanitizer prefixes =+-@/tab/CR cells, spares numbers+clean text, pandas3 str dtype covered")
