#!/usr/bin/env python
"""
AppTest gate for the Backup & Restore DANGER ZONE (audit 2026-10-31 #3):
the restore confirm must require an ADMIN PIN — a valid non-admin staff
PIN must be rejected and the DB must survive untouched.

1. Staff PIN + uploaded .sql -> "non-admin" error, DB tables intact.
2. Admin PIN + uploaded .sql -> restore executes (DB replaced by the dump).
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
du.manage_staff_user(conn, 'add', username="AdminA", pin="1111", is_admin=1)
du.manage_staff_user(conn, 'add', username="StaffS", pin="2222", is_admin=0)
conn.execute("INSERT INTO items (name, category, main_supplier, available_quantity, desired_quantity) "
             "VALUES ('Canary', 'fresh', 'SupCo', 1, 1)")
conn.commit()
conn.close()

# A trivially valid backup dump: one table, one row.
DUMP = "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);\nINSERT INTO items (name) VALUES ('FromBackup');\n"

def run(at):
    try:
        at.run()
    except KeyError:
        pass
    assert not at.exception, at.exception

at = AppTest.from_file(os.path.join(ROOT, "ims_web_app_admin.py"), default_timeout=60)
at.session_state["logged_in"] = True
at.session_state["current_user"] = {"user_id": 1, "username": "AdminA", "is_admin": True}
at.session_state["admin_choice"] = "Backup & Restore"
run(at)

up = at.file_uploader
assert up, "file uploader missing on Backup & Restore screen"
up[0].upload("backup.sql", DUMP.encode(), "application/sql")
run(at)

pin = at.text_input
pin = [p for p in at.text_input if "restore_pin" in (p.key or "")]
assert pin, "restore PIN input missing"

btn = [b for b in at.button if "EXECUTE RESTORE" in (b.label or "")]
assert btn, "EXECUTE RESTORE button missing"

# --- 1. staff PIN must be refused, DB untouched ---
pin[0].set_value("2222")
run(at)
btn = [b for b in at.button if "EXECUTE RESTORE" in (b.label or "")]
btn[0].set_value(True).run()
errs = " ".join(e.value or "" for e in at.error)
assert "non-admin" in errs, f"staff PIN must be refused as non-admin; errors were: {errs!r}"
conn = sqlite3.connect(path)
names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
canary = conn.execute("SELECT COUNT(*) FROM items WHERE name='Canary'").fetchone()[0]
conn.close()
assert "orders" in names and canary == 1, "staff-PIN restore must NOT touch the DB"

# --- 2. admin PIN executes the restore ---
pin = [p for p in at.text_input if "restore_pin" in (p.key or "")]
pin[0].set_value("1111")
run(at)
btn = [b for b in at.button if "EXECUTE RESTORE" in (b.label or "")]
btn[0].set_value(True).run()
succ = " ".join(s.value or "" for s in at.success)
assert "Restore Complete" in succ, f"admin PIN must execute restore; successes were: {succ!r}"
conn = sqlite3.connect(path)
row = conn.execute("SELECT name FROM items").fetchall()
conn.close()
assert row == [("FromBackup",)], "restore must have replaced the DB with the dump"

os.unlink(path)
print("PASS: restore DANGER ZONE requires an admin PIN (staff PIN refused, DB intact; admin PIN restores)")
