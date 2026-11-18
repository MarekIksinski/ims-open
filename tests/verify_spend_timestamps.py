# Regression: microsecond timestamps must survive the admin app's pandas
# parsing on Spend Analysis. SQLite CURRENT_TIMESTAMP stores second-precision
# strings; explicit Python inserts carry microseconds ('...22:58:38.930330').
# pandas 3's pd.to_datetime without format= raises "unconverted data remains"
# on the microsecond shape — the app now passes format="ISO8601" (mirrored
# here). Seeds BOTH shapes and parses exactly like ims_web_app_admin.py does.
import sqlite3, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils
import pandas as pd

db = os.path.join(tempfile.gettempdir(), 'spend_ts_test.db')
if os.path.exists(db): os.remove(db)
conn = sqlite3.connect(db)
database_utils.ensure_all_tables_exist(conn)
c = conn.cursor()
c.execute("INSERT INTO items (id,name,category,available_quantity,desired_quantity,main_supplier) VALUES (1,'flour','Dry',10,10,'Sup')")
c.execute("INSERT INTO item_prices (item_id,net_price,vat_rate) VALUES (1,5.0,0)")

# shape 1: CURRENT_TIMESTAMP default (second precision)
c.execute("INSERT INTO orders (order_id,supplier_name,status) VALUES (1,'Sup','Delivered')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (1,1,2,2,'flour')")
# shape 2: explicit microsecond timestamp (what the app's own inserts produce)
c.execute("INSERT INTO orders (order_id,supplier_name,status,generation_timestamp) VALUES (2,'Sup','Delivered','2026-01-03 10:00:00.930330')")
c.execute("INSERT INTO order_items (order_id,item_id,ordered_quantity,received_quantity,item_name) VALUES (2,1,3,3,'flour')")
c.execute("INSERT INTO price_history (item_id,net_price,vat_rate,recorded_at,source_order_id) VALUES (1,4.0,0,'2026-01-03 10:00:00.930330',2)")
conn.commit()

# Tab 1 path: fetch_spending_data_over_time -> df['date'] parse
t = database_utils.fetch_spending_data_over_time(conn)
assert len(t) == 2, t
df = pd.DataFrame(t)
df['date'] = pd.to_datetime(df['date'], format="ISO8601")
assert df['date'].notna().all(), df

# Tab 2 path: fetch_item_spending_details -> df_item['date'] parse
d = database_utils.fetch_item_spending_details(conn)
dfi = pd.DataFrame(d)
dfi['date'] = pd.to_datetime(dfi['date'], format="ISO8601")
assert dfi['date'].notna().all(), dfi

# prove the OLD call form actually fails on a MIXED-precision column
# (pandas infers '%Y-%m-%d %H:%M:%S' from the first row, then chokes on
# the microsecond row) — exactly the demo crash.
try:
    pd.to_datetime(['2026-01-03 10:00:00', '2026-01-03 10:00:00.930330'])
    print("NOTE: unformatted parse did not raise on this pandas — guard still correct")
except Exception as e:
    print("old form fails as expected:", type(e).__name__)

print("ALL_OK")
os.remove(db)
