# Regression harness: log_stock_failure values with PER-BOX net_price
# (item_prices.net_price is the box/case invoice price; losses are
# net_price x boxes, never x pack_size), and fetch_waste_log_report
# filtering out legacy '2Good2Go' rows.
import sqlite3, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils

db = os.path.join(tempfile.gettempdir(), 'stock_failure_test.db')
if os.path.exists(db): os.remove(db)
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys=ON")
database_utils.ensure_all_tables_exist(conn)
conn.commit()

c = conn.cursor()
# item 1: pack 12, net_price (per BOX) 0.50
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (1,'croissants','Bakery',10,10,12,'Sup')")
# item 2: pack NULL -> treated as 1
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,pack_size,main_supplier) VALUES (2,'butter','Dairy',5,5,NULL,'Sup')")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (1,0.50,0)")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (2,3.00,0)")
conn.commit()

ok, msg = database_utils.log_stock_failure(conn, 1, 2, 'tester')
assert ok, msg
row = conn.execute("SELECT available_quantity FROM items WHERE id=1").fetchone()
assert row[0] == 8, row  # 10 - 2 boxes deducted
wh = conn.execute("SELECT calculated_value, unit_type FROM waste_log ORDER BY log_id DESC LIMIT 1").fetchone()
assert abs(wh[0] - 1.0) < 1e-9 and wh[1] == 'Boxes', wh  # 0.50/box * 2 boxes (never x pack_size)

ok, msg = database_utils.log_stock_failure(conn, 2, 1, 'tester')
assert ok, msg
wh = conn.execute("SELECT calculated_value FROM waste_log ORDER BY log_id DESC LIMIT 1").fetchone()
assert abs(wh[0] - 3.0) < 1e-9, wh  # 3.00/box * 1 box

# over-deduct refused
ok, msg = database_utils.log_stock_failure(conn, 2, 99, 'tester')
assert not ok and 'Not enough stock' in msg, msg

# legacy 2Good2Go row must be invisible in the report
conn.execute("INSERT INTO waste_log (item_id,item_name,event_type,quantity_entered,unit_type,calculated_value,logged_by) VALUES (1,'croissants','2Good2Go',5,'Pieces',2.50,'tester')")
conn.commit()
rep = database_utils.fetch_waste_log_report(conn)
assert len(rep) == 2 and all(r['event_type'] == 'Stock Failure' for r in rep), rep

# delete a Stock Failure entry -> stock restored
lid = conn.execute("SELECT log_id FROM waste_log WHERE item_id=1 AND event_type='Stock Failure'").fetchone()[0]
ok, msg = database_utils.delete_waste_log_entry(conn, lid)
assert ok, msg
row = conn.execute("SELECT available_quantity FROM items WHERE id=1").fetchone()
assert row[0] == 10, row  # 2 boxes added back
rep = database_utils.fetch_waste_log_report(conn)
assert len(rep) == 1, rep

print("ALL_OK")
