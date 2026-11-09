#!/usr/bin/env python3
"""
Migration: stop cascade deletes from rewriting history.

order_items and waste_log previously had:
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
so deleting an item silently removed it from every past order and waste log.

This migration:
  1. Adds an `item_name` snapshot column to both tables and backfills it
     from the current items table.
  2. Rebuilds both tables with item_id -> items(id) ON DELETE SET NULL.
  3. Verifies FK integrity afterwards.

Safe to re-run: it is a no-op if the snapshot columns already exist.
Run with: python3 migrate_history_snapshots.py [path-to-db]
"""
import sqlite3
import sys
import shutil
import datetime
import os

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "inventory.db")


def has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def table_sql(conn, table):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


def snapshot_cols_exist(conn):
    return (has_column(conn, "order_items", "item_name")
            and has_column(conn, "waste_log", "item_name"))


def migrate(conn):
    cur = conn.cursor()

    # --- 1. Add + backfill snapshot columns -------------------------------
    if not has_column(conn, "order_items", "item_name"):
        cur.execute("ALTER TABLE order_items ADD COLUMN item_name TEXT")
        print("Added order_items.item_name")
    if not has_column(conn, "waste_log", "item_name"):
        cur.execute("ALTER TABLE waste_log ADD COLUMN item_name TEXT")
        print("Added waste_log.item_name")

    cur.execute("""
        UPDATE order_items SET item_name =
            (SELECT name FROM items WHERE items.id = order_items.item_id)
        WHERE item_name IS NULL
    """)
    cur.execute("""
        UPDATE waste_log SET item_name =
            (SELECT name FROM items WHERE items.id = waste_log.item_id)
        WHERE item_name IS NULL
    """)
    print(f"Backfilled order_items ({cur.rowcount} last batch) and waste_log names")

    # --- 2. Rebuild with ON DELETE SET NULL -------------------------------
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.executescript("""
            BEGIN;

            CREATE TABLE order_items_new (
                order_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                item_id INTEGER,                -- nullable after item deletion
                item_name TEXT,                 -- name snapshot at insert time
                ordered_quantity INTEGER NOT NULL,
                received_quantity INTEGER,
                FOREIGN KEY (order_id) REFERENCES orders (order_id)
                    ON DELETE CASCADE ON UPDATE CASCADE,
                FOREIGN KEY (item_id) REFERENCES items (id)
                    ON DELETE SET NULL ON UPDATE CASCADE
            );

            INSERT INTO order_items_new
                (order_item_id, order_id, item_id, item_name,
                 ordered_quantity, received_quantity)
            SELECT order_item_id, order_id, item_id, item_name,
                   ordered_quantity, received_quantity
            FROM order_items;

            DROP TABLE order_items;
            ALTER TABLE order_items_new RENAME TO order_items;

            CREATE TABLE waste_log_new (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER,                -- nullable after item deletion
                item_name TEXT,                 -- name snapshot at insert time
                event_type TEXT NOT NULL,
                quantity_entered REAL NOT NULL,
                unit_type TEXT NOT NULL,
                calculated_value REAL,
                logged_by TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES items (id)
                    ON DELETE SET NULL ON UPDATE CASCADE
            );

            INSERT INTO waste_log_new
                (log_id, item_id, item_name, event_type, quantity_entered,
                 unit_type, calculated_value, logged_by, timestamp)
            SELECT log_id, item_id, item_name, event_type, quantity_entered,
                   unit_type, calculated_value, logged_by, timestamp
            FROM waste_log;

            DROP TABLE waste_log;
            ALTER TABLE waste_log_new RENAME TO waste_log;

            COMMIT;
        """)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # --- 3. Verify --------------------------------------------------------
    cur.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_order_items_item ON order_items(item_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_waste_log_item ON waste_log(item_id)")

    fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        raise RuntimeError(f"FK violations after migration: {fk_violations}")

    conn.commit()

    # Confirm the new FK actions are in place
    for table in ("order_items", "waste_log"):
        fks = cur.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        item_fk = next(f for f in fks if f[2] == "items")
        # pragma columns: id, seq, table, from, to, on_update, on_delete, match
        assert item_fk[6] == "SET NULL", f"{table}: item FK on_delete is {item_fk[6]}"
        print(f"{table}: item_id FK -> ON DELETE SET NULL  OK")

    n_oi = cur.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
    n_wl = cur.execute("SELECT COUNT(*) FROM waste_log").fetchone()[0]
    print(f"Row counts preserved: order_items={n_oi}, waste_log={n_wl}")


def main():
    print(f"Database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        if snapshot_cols_exist(conn):
            print("Snapshot columns already present — migration already applied. Nothing to do.")
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{DB_PATH}.bak-{stamp}"
        conn.close()
        shutil.copy2(DB_PATH, backup)
        print(f"Backup written: {backup}")
        conn = sqlite3.connect(DB_PATH)
        migrate(conn)
        print("Migration complete.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
