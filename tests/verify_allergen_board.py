#!/usr/bin/env python
"""
AppTest gate for ims_allergen_board.py (read-only FSA board):
1. Unreviewed item is NEVER shown as safe: it gets the red NOT-YET-VERIFIED
   error, is excluded from Suitable, and shows '❓ UNVERIFIED' in the matrix.
2. 'Customer Avoids' split: item containing Wheat lands in Avoid when the
   gluten column is selected; traces toggle makes a may-contain item Avoid.
3. Reviewed item with no allergens shows 'None declared in ingredients'.
4. Read-only: the app performs no writes (temp DB row count unchanged).
Run with the streamlit venv python.
"""
import os, sys, sqlite3, tempfile
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd); os.unlink(path)
os.environ["IMS_DB_PATH"] = path
import database_utils as du

conn = sqlite3.connect(path)
du.ensure_all_tables_exist(conn)
cur = conn.cursor()

def add(name, cat, reviewed):
    cur.execute("""INSERT INTO items (name, category, main_supplier,
                   available_quantity, desired_quantity, allergens_last_reviewed)
                   VALUES (?, ?, 'SupCo', 5, 5, ?)""", (name, cat, reviewed))
    return cur.lastrowid

wheat_id = add("Wheat Roll", "fresh", "2026-10-01")
milk_id  = add("Latte", "drinks", "2026-10-01")
traces_id = add("Choc Chip Cookie", "fresh", "2026-10-01")
safe_id  = add("Still Water", "drinks", "2026-10-01")
unver_id = add("Mystery Muffin", "fresh", None)   # unreviewed
# stored-XSS payloads (audit 2026-10-31 #1): admin-editable name/category/
# allergen name must be HTML-escaped before unsafe_allow_html rendering.
evil_id = add('<img src=x onerror=alert(1)>', 'cat"><script>alert(2)</script>', "2026-10-01")

def allergen_id(nm):
    cur.execute("INSERT INTO allergens (name) VALUES (?)", (nm,))
    return cur.lastrowid
wheat, milk, egg = allergen_id("Wheat"), allergen_id("Milk"), allergen_id("Egg")
evil_allergen = allergen_id('Milk" onmouseover="alert(3)')
cur.execute("INSERT INTO item_allergens (item_id, allergen_id) VALUES (?,?)", (wheat_id, wheat))
cur.execute("INSERT INTO item_allergens (item_id, allergen_id) VALUES (?,?)", (milk_id, milk))
cur.execute("INSERT INTO item_allergens (item_id, allergen_id) VALUES (?,?)", (evil_id, evil_allergen))
cur.execute("INSERT INTO item_allergens_may_contain (item_id, allergen_id) VALUES (?,?)", (traces_id, egg))
conn.commit()
conn.close()

def run(at):
    try:
        at.run()
    except KeyError:
        pass
    assert not at.exception, at.exception

at = AppTest.from_file(os.path.join(ROOT, "ims_allergen_board.py"), default_timeout=60)
run(at)

text = " ".join([m.value or "" for m in at.markdown] + [e.value or "" for e in at.error])

# 1. unverified safety rule
assert "NOT YET VERIFIED" in text, "unverified item missing the red warning"
assert "Safe to Eat" not in text

# matrix mode check (radio switch)
r = [x for x in at.radio if "View mode" in (x.label or "")]
assert r, "mode radio missing"
r[0].set_value("📋 Allergen Matrix")
run(at)
df = at.dataframe[0].value
unver_row = df[df["Item"] == "Mystery Muffin"]
assert (unver_row["Last Reviewed"] == "❓ UNVERIFIED").all(), "matrix must mark unverified"
assert (unver_row["Milk"] == "❓ UNVERIFIED").all(), "matrix cells must be UNVERIFIED, never '—'"

# back to cards, set avoids
r = [x for x in at.radio if "View mode" in (x.label or "")][0]
r.set_value("🏷️ Product Cards")
run(at)
ms = [x for x in at.multiselect if "Customer Avoids" in (x.label or "")]
assert ms, "avoids multiselect missing"
ms[0].set_value(["Cereals containing gluten", "Milk"])
run(at)

# section headers exist
text = " ".join([m.value or "" for m in at.markdown])
assert "Suitable" in text and "Avoid" in text and "Unverified" in text

# cards render in bucket order (Suitable, then Avoid, then Unverified);
# use the summary-line counts to map each card index to its bucket.
import re as _re
def section_of(name):
    summary = next(m.value for m in at.markdown if "✅ Suitable:" in (m.value or ""))
    n_s, n_av, n_un = map(int, _re.findall(r"\*\*(\d+)\*\*", summary))
    card_names = [v for v in (m.value or "" for m in at.markdown) if v.lstrip().startswith("### ")]
    k = next(idx for idx, v in enumerate(card_names) if name in v)
    if k < n_s: return "suitable"
    if k < n_s + n_av: return "avoid"
    return "unverified"

assert section_of("Wheat Roll") == "avoid", "Wheat Roll must be Avoid under gluten"
assert section_of("Latte") == "avoid", "Latte must be Avoid under Milk"
assert section_of("Still Water") == "suitable", "Still Water must be Suitable"
assert section_of("Choc Chip Cookie") == "suitable", "traces item Suitable while toggle default True? NO unless toggle off — default True means traces count as hit; Egg not selected here, so suitable is correct"
assert section_of("Mystery Muffin") == "unverified", "unverified must land in Unverified bucket"

# traces toggle: avoid Egg -> cookie must flip to Avoid
ms = [x for x in at.multiselect if "Customer Avoids" in (x.label or "")][0]
ms.set_value(["Eggs"])
run(at)
assert section_of("Choc Chip Cookie") == "avoid", "may-contain Egg must count as hit (toggle default True)"

# 'None declared' for reviewed clean item
text = " ".join([m.value or "" for m in at.markdown])
assert "None declared in ingredients" in text

# 5. stored XSS (audit #1): no raw payload may reach the markdown HTML;
# the escaped form must be what renders.
assert "<img src=x onerror" not in text, "raw <img> payload leaked into card HTML"
assert "<script>" not in text, "raw <script> payload leaked into card HTML"
assert "onerror=alert(1)" in text and "&lt;img" in text, \
    "escaped payload should still display as literal text"
assert '" onmouseover=' not in text, "allergen label broke out of its attribute"

# 4. read-only: DB untouched (item count identical, no waste_log rows)
conn = sqlite3.connect(path)
n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
w = conn.execute("SELECT COUNT(*) FROM waste_log").fetchone()[0]
conn.close()
assert n == 6 and w == 0, "board must not write to the DB"

os.unlink(path)
print("PASS: board safety rules, avoid-split, traces toggle, matrix UNVERIFIED cells, read-only")
