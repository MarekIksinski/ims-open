# Verify the spend-query fixes against a temp SQLite DB built with the
# project's own ensure_all_tables_exist, seeded with the multiplication case.
import sqlite3, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils

db = os.path.join(tempfile.gettempdir(), 'spend_fix_test.db')
if os.path.exists(db): os.remove(db)
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys=ON")
database_utils.ensure_all_tables_exist(conn)
conn.commit()

c = conn.cursor()
# items: 1=flour (has category), 2=beef dripping; item 3 will be DELETED
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,main_supplier) VALUES (1,'flour','Dry',10,10,'Sup')")
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,main_supplier) VALUES (2,'beef dripping','Fat',10,10,'Sup')")
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,main_supplier) VALUES (3,'old item','Cat3',0,0,'Sup')")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (1,5.0,0)")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (2,20.0,0)")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (3,7.0,0)")

# order 1: Delivered; item1 received 10. TWO price_history rows for (item1, order1)
# (multi-stage delivery) -> old JOIN would double-count.
c.execute("INSERT INTO orders (order_id,supplier_name,status,generation_timestamp) VALUES (1,'Sup','Delivered','2026-01-01 10:00:00')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (1,1,10,10,'flour')")
c.execute("INSERT INTO price_history (item_id,net_price,source_order_id) VALUES (1,4.00,1)")   # older
c.execute("INSERT INTO price_history (item_id,net_price,source_order_id) VALUES (1,4.50,1)")   # newer -> wins
# expected order1 total: 10 * 4.50 = 45.00 (NOT 85 or 90)

# order 2: Partially Delivered; item2 received NULL (unreceived), item3 deleted.
c.execute("INSERT INTO orders (order_id,supplier_name,status,generation_timestamp) VALUES (2,'Sup','Partially Delivered','2026-01-02 10:00:00')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (2,2,5,NULL,'beef dripping')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (2,3,4,3,'old item')")
c.execute("DELETE FROM items WHERE id=3")   # snapshot name must survive
conn.commit()

t = database_utils.fetch_spending_data_over_time(conn)
print("over_time:", t)
assert abs(t[0]['total_cost'] - 45.0) < 1e-9, t
# NULL-received line -> 0; deleted item: SET NULL kills the price lookup -> 0
assert abs(t[1]['total_cost'] - 0.0) < 1e-9, t

d = database_utils.fetch_item_spending_details(conn)
print("details:", d)
assert [r['item_name'] for r in d] == ['flour', 'old item'], d  # snapshot name, no NULL row for unreceived
assert abs(d[0]['cost'] - 45.0) < 1e-9 and d[0]['qty'] == 10
assert abs(d[1]['cost'] - 0.0) < 1e-9 and d[1]['qty'] == 3  # snapshot name kept, price lookup dead

cat = database_utils.fetch_spend_by_category(conn)
print("by_category:", cat)
byc = {r['category']: r['cost'] for r in cat}
assert abs(byc['Dry'] - 45.0) < 1e-9, cat
assert abs(byc['Fat'] - 0.0) < 1e-9, cat      # NULL qty -> 0, not None
# deleted item's category is gone -> grouped as 'Uncategorized'
assert abs(byc['Uncategorized'] - 0.0) < 1e-9, cat
print("ALL_OK")
