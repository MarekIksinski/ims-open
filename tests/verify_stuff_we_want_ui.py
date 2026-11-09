#!/usr/bin/env python
"""
AppTest gate for the reordering workflow fixes:
1. 'Stuff We Want' shows an item that is fully covered by an open order
   (it must NOT be hidden), with the 'already on an open order' warning,
   and the order-qty widget defaulted to the physical deficit.
2. A Stock Check 'Confirm & Next' clears stale 'ord_*' widget keys and
   generated_orders_map (invalidate_order_form_state).
Run with the streamlit venv python.
"""
import os, sys, tempfile, sqlite3
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd); os.unlink(path)
os.environ["IMS_DB_PATH"] = path
import database_utils as du

conn = sqlite3.connect(path)
du.ensure_all_tables_exist(conn)
cur = conn.cursor()
cur.execute("INSERT INTO items (name, category, main_supplier, available_quantity, desired_quantity, pack_size) VALUES ('Croissants','Bakery','SupCo',5,10,24)")
iid = cur.lastrowid
cur.execute("INSERT INTO orders (supplier_name, status, generation_timestamp) VALUES ('SupCo','Generated',datetime('now'))")
cur.execute("INSERT INTO order_items (order_id, item_id, ordered_quantity, received_quantity) VALUES (?, ?, 5, 0)", (cur.lastrowid, iid))
conn.commit(); conn.close()

def run(at):
    # AppTest's element tree can KeyError when widgets disappear between
    # reruns (harness artifact) — at.exception stays empty in that case.
    try:
        at.run()
    except KeyError:
        pass
    assert not at.exception, at.exception

# --- 1. Stuff We Want rendering --------------------------------------
at = AppTest.from_file(os.path.join(ROOT, "ims_web_app_user.py"), default_timeout=30)
at.session_state["logged_in"] = True
at.session_state["current_user"] = {"username": "tester", "is_admin": False}
at.session_state["staff_choice"] = "📝 Stuff We Want"
run(at)

assert any("Croissants" in (m.value or "") for m in at.markdown), "Croissants hidden despite fully-covered open order"
assert any("already on an open order" in (w.value or "") for w in at.warning), "pending warning missing"
oi = [x for x in at.number_input if x.key == f"ord_{iid}"]
assert oi and oi[0].value == 5, f"order qty default {oi[0].value if oi else None} != physical deficit 5"

# --- 2. Stock Check Confirm & Next clears stale order-form state ------
at2 = AppTest.from_file(os.path.join(ROOT, "ims_web_app_user.py"), default_timeout=30)
at2.session_state["logged_in"] = True
at2.session_state["current_user"] = {"username": "tester", "is_admin": False}
at2.session_state["staff_choice"] = "🔍 Stock Check"
at2.session_state["sc_active"] = True
at2.session_state["sc_list"] = [{"id": iid, "name": "Croissants"}]
at2.session_state["sc_idx"] = 0
at2.session_state["ord_123"] = 99          # stale widget from a prior visit
at2.session_state["generated_orders_map"] = {"SupCo": {"text": "x", "oid": 1}}
run(at2)

btn = [b for b in at2.button if b.key == f"sc_confirm_{iid}"]
assert btn, "Confirm & Next button not found"
btn[0].set_value(True)
run(at2)

assert "ord_123" not in at2.session_state, "stale ord_* key survived a stock change"
assert ("generated_orders_map" not in at2.session_state) or (at2.session_state["generated_orders_map"] == {}), "generated_orders_map not cleared"
assert at2.session_state["sc_idx"] == 1, "stock check index should have advanced"

os.unlink(path)
print("PASS: open-order item visible with warning + deficit default; stock change clears ord_* state")
