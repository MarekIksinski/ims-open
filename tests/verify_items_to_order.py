# Regression harness: get_items_to_order must net out quantities already
# pending on open orders ('Generated'/'Partially Delivered') so staff are
# never prompted to re-order stock that is already on its way.
# Seeds a temp SQLite DB built with the project's own ensure_all_tables_exist.
import sqlite3, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils

db = os.path.join(tempfile.gettempdir(), 'items_to_order_test.db')
if os.path.exists(db): os.remove(db)
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys=ON")
database_utils.ensure_all_tables_exist(conn)
conn.commit()

c = conn.cursor()
# item 1: flour, deficit 10, pending 4 on a Generated order -> net 6
c.execute("INSERT INTO items (id,name,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (1,'flour',5,15,6,'SupA')")
# item 2: milk, deficit 10, fully pending (10 on open orders) -> must NOT appear
c.execute("INSERT INTO items (id,name,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (2,'milk',0,10,1,'SupA')")
# item 3: fat, deficit 5, over-delivered on a Partially Delivered order (ordered 2, received 5) -> pending clamps to 0 -> net 5
c.execute("INSERT INTO items (id,name,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (3,'fat',3,8,1,'SupB')")
# item 4: beans, deficit 4 but 4 pending on a DELIVERED order -> delivered orders don't count; still ordered
c.execute("INSERT INTO items (id,name,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (4,'beans',0,4,1,'SupB')")

c.execute("INSERT INTO orders (order_id,supplier_name,status) VALUES (1,'SupA','Generated')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (1,1,4,0,'flour')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (1,2,10,0,'milk')")
c.execute("INSERT INTO orders (order_id,supplier_name,status) VALUES (2,'SupB','Partially Delivered')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (2,3,2,5,'fat')")
c.execute("INSERT INTO orders (order_id,supplier_name,status) VALUES (3,'SupB','Delivered')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (3,4,4,4,'beans')")
conn.commit()

rows = database_utils.get_items_to_order(conn)
by_name = {r['name']: r for r in rows}
print(rows)

assert set(by_name) == {'flour', 'fat', 'beans'}, by_name  # milk fully pending -> gone

f = by_name['flour']
assert f['pending_order_quantity'] == 4, f
assert f['deficit_quantity'] == 6, f          # 15 - 5 - 4
assert f['order_quantity'] == 6, f            # raw net deficit (pack rounding retired)
assert 'pack_size' not in f, f

assert by_name['fat']['pending_order_quantity'] == 0, by_name['fat']   # MAX(0, 2-5) clamp
assert by_name['fat']['order_quantity'] == 5, by_name['fat']

assert by_name['beans']['pending_order_quantity'] == 0, by_name['beans']  # Delivered != open
assert by_name['beans']['order_quantity'] == 4, by_name['beans']

print("ALL_OK")
