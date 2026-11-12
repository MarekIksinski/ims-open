#!/usr/bin/env python
"""
Regression harness for the 'Generate Orders' reordering fixes.

Asserts:
1. get_items_to_order filters STRICTLY by physical deficit — an item
   fully covered by an open ('Generated'/'Partially Delivered') order
   still appears, with pending_order_quantity informational and
   order_quantity = desired - available (never hidden/zeroed).
2. log_stock_failure values loss as net_price (per BOX) * boxes —
   NOT multiplied by pack_size.

Run with the streamlit venv python (database_utils imports streamlit):
  ~/.local/share/pipx/venvs/streamlit/bin/python tests/verify_order_and_loss_semantics.py
"""
import os, sys, tempfile, sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils as du

def main():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    du.ensure_all_tables_exist(conn)
    cur = conn.cursor()

    # --- seed items -----------------------------------------------------
    # A: deficit fully covered by an open order (the old bug hid it)
    # B: partial open order, still a deficit
    # C: no open order, plain deficit
    # D: no deficit (must NOT appear)
    # E: over-delivered open order (pending clamps to 0, no deficit -> absent)
    def add_item(name, avail, desired, pack):
        cur.execute("""INSERT INTO items (name, category, main_supplier,
                       available_quantity, desired_quantity, pack_size)
                       VALUES (?, 'Cat', 'SupCo', ?, ?, ?)""",
                    (name, avail, desired, pack))
        return cur.lastrowid

    a = add_item("A", 5, 10, 24)
    b = add_item("B", 3, 10, 40)
    c = add_item("C", 0, 4, 12)
    d = add_item("D", 20, 10, 6)
    e = add_item("E", 12, 10, 24)

    def add_order(status, lines):
        cur.execute("INSERT INTO orders (supplier_name, status, generation_timestamp) VALUES ('SupCo', ?, datetime('now'))", (status,))
        oid = cur.lastrowid
        for item_id, ordered, received in lines:
            cur.execute("INSERT INTO order_items (order_id, item_id, ordered_quantity, received_quantity) VALUES (?, ?, ?, ?)",
                        (oid, item_id, ordered, received))
        return oid

    add_order('Generated',          [(a, 5, 0)])    # A fully covered by open order
    add_order('Partially Delivered',[(b, 5, 2)])    # B: pending 3
    add_order('Delivered',          [(e, 50, 50)])  # E closed order; plus over-delivery below
    add_order('Partially Delivered',[(e, 4, 8)])    # over-delivered -> pending clamps 0

    # --- 1. get_items_to_order -----------------------------------------
    rows = {r['name']: r for r in du.get_items_to_order(conn)}

    assert 'A' in rows, "A hidden despite open order — the original bug"
    assert rows['A']['order_quantity'] == 5, f"A order_quantity {rows['A']['order_quantity']} != 5"
    assert rows['A']['deficit_quantity'] == 5
    assert rows['A']['pending_order_quantity'] == 5, "A pending not informational 5"

    assert 'B' in rows and rows['B']['order_quantity'] == 7, "B order_quantity must be physical deficit 7"
    assert rows['B']['pending_order_quantity'] == 3, "B pending must be MAX(0,5-2)=3"

    assert 'C' in rows and rows['C']['order_quantity'] == 4 and rows['C']['pending_order_quantity'] == 0

    assert 'D' not in rows, "D has no deficit and must not appear"
    assert 'E' not in rows, "E has no deficit; over-delivery must not create one"

    # --- 2. log_stock_failure ------------------------------------------
    cur.execute("INSERT INTO item_prices (item_id, net_price, vat_rate) VALUES (?, 20.00, 0.0)", (a,))
    conn.commit()
    ok, msg = du.log_stock_failure(conn, a, 2, 'tester')
    assert ok, f"log_stock_failure failed: {msg}"
    cur.execute("SELECT calculated_value FROM waste_log ORDER BY log_id DESC LIMIT 1")
    val = cur.fetchone()[0]
    assert abs(val - 40.0) < 1e-9, f"loss value {val} != net_price(20) * 2 boxes (pack_size 24 must NOT multiply)"
    cur.execute("SELECT available_quantity FROM items WHERE id = ?", (a,))
    assert cur.fetchone()[0] == 3, "stock must deduct by boxes"

    conn.close()
    print("PASS: get_items_to_order physical-deficit semantics + log_stock_failure box-price semantics")

if __name__ == "__main__":
    main()
