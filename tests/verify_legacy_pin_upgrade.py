#!/usr/bin/env python
"""
Gate for audit 2026-10-31 #2 (legacy unsalted SHA-256 PIN hashes):
1. verify_staff_login on a legacy row -> login succeeds, returns
   legacy_login=True, and the row is upgraded to scrypt$ in place.
2. Second login -> legacy_login=False, hash stays scrypt$.
3. list_legacy_pin_accounts: lists legacy rows, empty after conversion.
4. UI: with pin_legacy_notice set, the user app shows the security
   warning banner; without it, no banner.
Run with the streamlit venv python.
"""
import hashlib
import os, sys, sqlite3, tempfile
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd); os.unlink(path)
os.environ["IMS_DB_PATH"] = path
import database_utils as du

conn = sqlite3.connect(path)
du.ensure_all_tables_exist(conn)

# Seed one legacy-hash user (raw unsalted SHA-256 of PIN '2222') + one modern.
cur = conn.cursor()
cur.execute("INSERT INTO staff_users (username, hashed_password, is_active, is_admin) "
            "VALUES ('LegacyUser', ?, 1, 0)", (hashlib.sha256(b"2222").hexdigest(),))
conn.commit()
du.manage_staff_user(conn, 'add', username="AdminA", pin="1111", is_admin=1)

# --- 1. legacy login: succeeds + flag + in-place upgrade ---
u = du.verify_staff_login(conn, "2222")
assert u and u["username"] == "LegacyUser", "legacy login must succeed"
assert u.get("legacy_login") is True, "legacy_login flag must be set on the transition login"
h = conn.execute("SELECT hashed_password FROM staff_users WHERE username='LegacyUser'").fetchone()[0]
assert h.startswith("scrypt$"), f"hash must be upgraded in place, got: {h[:12]}..."

# --- 2. second login: no flag, still scrypt ---
u2 = du.verify_staff_login(conn, "2222")
assert u2 and u2.get("legacy_login") is False, "second login must not flag legacy"
h2 = conn.execute("SELECT hashed_password FROM staff_users WHERE username='LegacyUser'").fetchone()[0]
assert h2.startswith("scrypt$")

# --- 3. list_legacy_pin_accounts ---
cur.execute("INSERT INTO staff_users (username, hashed_password, is_active, is_admin) "
            "VALUES ('Jag', ?, 1, 1)", (hashlib.sha256(b"9999").hexdigest(),))
conn.commit()
legacy = du.list_legacy_pin_accounts(conn)
assert legacy == ["Jag"], f"expected exactly the seeded legacy account, got {legacy}"
assert du.verify_staff_login(conn, "9999")
assert du.list_legacy_pin_accounts(conn) == [], "list must empty out after all convert"
conn.close()

# --- 4. banner rendering in the user app ---
def run(at):
    try:
        at.run()
    except KeyError:
        pass
    assert not at.exception, at.exception

at = AppTest.from_file(os.path.join(ROOT, "ims_web_app_user.py"), default_timeout=60)
at.session_state["logged_in"] = True
at.session_state["current_user"] = {"user_id": 1, "username": "LegacyUser",
                                    "is_admin": False, "legacy_login": False}
at.session_state["pin_legacy_notice"] = True
run(at)
warns = " ".join(w.value or "" for w in at.warning)
assert "legacy format" in warns, f"banner missing with notice set; warnings: {warns!r}"

at2 = AppTest.from_file(os.path.join(ROOT, "ims_web_app_user.py"), default_timeout=60)
at2.session_state["logged_in"] = True
at2.session_state["current_user"] = {"user_id": 1, "username": "LegacyUser",
                                     "is_admin": False, "legacy_login": False}
run(at2)
warns2 = " ".join(w.value or "" for w in at2.warning)
assert "legacy format" not in warns2, "banner must NOT show without the notice"

os.unlink(path)
print("PASS: legacy PIN login flags + upgrades in place, admin legacy list works, banner shows only with notice")
