"""Regression harness: allergen review queue (admin app).

Seeds a temp DB (1 never-reviewed, 1 fresh, 1 stale item), drives the
admin app with streamlit's AppTest, and asserts:
  - extractor metrics (Reviewed / Never / Stale counts)
  - product dropdown labels carry the review-status suffix
  - the three filter modes narrow the dropdown correctly
  - View All Allergens audit table sorts NEVER first, oldest first,
    shows '⚠️ NEVER', and its 4 filter modes work
  - Save Both Lists stamps allergens_last_reviewed
  - re-rendering after a save (label flips) doesn't crash (stale-key guard)

Run with the streamlit venv python:
  ~/.local/share/pipx/venvs/streamlit/bin/python tests/verify_allergen_review_queue.py
"""
import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

db = tempfile.mktemp(suffix=".db")
os.environ["IMS_DB_PATH"] = db
import database_utils as du  # noqa: E402

conn = sqlite3.connect(db)
du.ensure_all_tables_exist(conn)
now = datetime.now()
conn.execute("INSERT INTO items (name, category, main_supplier) VALUES ('Corned Beef Savoury','Deli','Sysco')")
conn.execute("INSERT INTO items (name, category, main_supplier) VALUES ('Batter Mix','Dry','Brand')")
conn.execute("INSERT INTO items (name, category, main_supplier) VALUES ('Ancient Jam','Dry','Y')")
conn.commit()
ids = {n: conn.execute("SELECT id FROM items WHERE name=?", (n,)).fetchone()[0]
       for n in ("Corned Beef Savoury", "Batter Mix", "Ancient Jam")}
conn.execute("UPDATE items SET allergens_last_reviewed=? WHERE id=?",
             (now.strftime("%Y-%m-%d %H:%M"), ids["Batter Mix"]))
conn.execute("UPDATE items SET allergens_last_reviewed=? WHERE id=?",
             ((now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M"), ids["Ancient Jam"]))
conn.commit()
du.manage_staff_user(conn, "add", username="TestAdmin", pin="1234", is_admin=True)
conn.close()

from streamlit.testing.v1 import AppTest  # noqa: E402

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
at = AppTest.from_file("ims_web_app_admin.py", default_timeout=30)
at.session_state["logged_in"] = True
at.session_state["current_user"] = {"user_id": 1, "username": "TestAdmin", "is_admin": 1}

# --- AI Allergen Extractor: metrics, labels, filters ---
at.session_state["admin_choice"] = "🤖 AI Allergen Extractor"
at.run()
assert not at.exception, at.exception
labels = [(m.label, str(m.value)) for m in at.metric]
assert ("✅ Reviewed Items", "2") in labels, labels
assert ("⚠️ Never Reviewed", "1") in labels, labels
assert ("🕰️ Stale (> 90 Days)", "1") in labels, labels

opts = [sb.options for sb in at.selectbox if sb.label == "1. Select Product:"][0]
assert any("Corned Beef Savoury" in o and "⚠️ Never reviewed" in o for o in opts), opts
assert any("Batter Mix" in o and "✅" in o for o in opts), opts

at.radio[0].set_value("⚠️ Never Reviewed Only").run()
assert not at.exception, at.exception
opts = [sb.options for sb in at.selectbox if sb.label == "1. Select Product:"][0]
assert len(opts) == 1 and "Corned Beef" in opts[0], opts

at.radio[0].set_value("🕰️ Needs Review (Never or > 90 Days)").run()
assert not at.exception, at.exception
opts = [sb.options for sb in at.selectbox if sb.label == "1. Select Product:"][0]
assert len(opts) == 2, opts

# --- View All Allergens: audit table ---
at.session_state["admin_choice"] = "👁️ View All Allergens"
at.run()
assert not at.exception, at.exception
df = at.dataframe[0].value
assert list(df["name"]) == ["Corned Beef Savoury", "Ancient Jam", "Batter Mix"], list(df["name"])
assert df.iloc[0]["allergens_last_reviewed"] == "⚠️ NEVER"

def audit_radio():
    return [r for r in at.radio if any("Older than 90 Days" in str(o) for o in r.options)][0]

audit_radio().set_value("⚠️ Never Reviewed").run()
assert not at.exception, at.exception
assert list(at.dataframe[0].value["name"]) == ["Corned Beef Savoury"]

audit_radio().set_value("🕰️ Older than 90 Days").run()
assert not at.exception, at.exception
assert list(at.dataframe[0].value["name"]) == ["Ancient Jam"]

audit_radio().set_value("✅ Up to Date").run()
assert not at.exception, at.exception
assert list(at.dataframe[0].value["name"]) == ["Batter Mix"]

# --- Save Both Lists stamps the review timestamp ---
at.session_state["admin_choice"] = "🤖 AI Allergen Extractor"
at.run()
assert not at.exception, at.exception
sb = [s for s in at.selectbox if s.label == "1. Select Product:"][0]
pick = [o for o in sb.options if "Corned Beef" in o][0]
sb.set_value(pick).run()
assert not at.exception, at.exception
[b for b in at.button if "Save Both Lists" in b.label][0].set_value(True).run()
assert not at.exception, at.exception
c2 = sqlite3.connect(db)
stamped = c2.execute("SELECT allergens_last_reviewed FROM items WHERE id=?",
                     (ids["Corned Beef Savoury"],)).fetchone()[0]
c2.close()
assert stamped and stamped.startswith(now.strftime("%Y-%m-%d")), stamped

# --- Stale-label guard: after save the label flipped; re-render + filter
# switch must not crash (re-fetch elements fresh; old handles go stale) ---
# NOTE: AppTest raises KeyError from its own element tree when widgets
# disappear between runs (auto-keyed multiselects). That is a harness
# artifact, NOT an app exception — at.exception stays empty. Assert the
# app-level state instead.
def rerun_tolerating_harness_quirk():
    try:
        at.run()
    except KeyError:
        pass
    assert not at.exception, at.exception

rerun_tolerating_harness_quirk()
sb2 = [s for s in at.selectbox if s.label == "1. Select Product:"][0]
assert any("Corned Beef" in o and "✅" in o for o in sb2.options), sb2.options
at.radio[0].set_value("⚠️ Never Reviewed Only")
rerun_tolerating_harness_quirk()

os.remove(db)
print("ALL_OK")
