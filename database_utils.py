#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
database_utils.py — shared SQLite schema, helpers, settings and AI
settings for all ims_v3 apps.
"""
# database_utils.py
import sqlite3
import logging
import streamlit as st # For caching decorator on connect_to_db
import pandas as pd # dtype checks in sanitize_df_for_csv
from datetime import datetime # For default timestamps in orders
import json
import hashlib
import hmac
import io
import os
import secrets

# --- Configuration ---
# Absolute path so the app always opens THIS database, regardless of the
# working directory it was launched from. Override with IMS_DB_PATH if needed.
DB_NAME = os.environ.get(
    'IMS_DB_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory.db')
)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s')

# --- Database Operation Logging Decorator ---
def log_database_operation(func):
    """
    Decorator for logging database operations and handling exceptions.
    """
    def wrapper(*args, **kwargs):
        func_name = func.__name__
        logging.debug(f"DB_OP: Attempting {func_name} with args: {args}, kwargs: {kwargs}") 
        try:
            result = func(*args, **kwargs)
            logging.debug(f"DB_OP: {func_name} successful.")
            return result
        except sqlite3.Error as e: 
            error_code_info = ""
            if hasattr(e, 'sqlite_errorcode') and hasattr(e, 'sqlite_errorname'):
                error_code_info = f"(SQLite error: {e.sqlite_errorcode} - {e.sqlite_errorname})"
            
            logging.error(f"DB_ERROR in {func_name} {error_code_info}: {e}")
            raise # Re-raise the SQLite error
        except Exception as e:
            logging.error(f"DB_ERROR in {func_name} (General error): {str(e)}")
            raise # Re-raise other exceptions
    return wrapper

# --- Database Connection Management ---
@st.cache_resource # KEEP THIS: Caches the connection object itself, which is correct.
@log_database_operation
def connect_to_db(db_name=DB_NAME):
    """
    Establishes and returns a connection to the SQLite database.
    Enables foreign key support. Cached with st.cache_resource so all
    sessions/reruns in this process share ONE connection (SQLite handles
    the serialization) instead of opening a new one per rerun.
    """
    conn = sqlite3.connect(db_name, check_same_thread=False) 
    conn.execute("PRAGMA foreign_keys = ON;") 
    logging.info(f"Database connection established to {db_name} and foreign keys ON.")
    return conn

# --- Table Creation Functions ---

@log_database_operation
def create_items_table(conn):
    """Creates the items table with an integer ID as primary key."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category TEXT,
            main_supplier TEXT,
            secondary_supplier TEXT,
            available_quantity INTEGER DEFAULT 0,
            desired_quantity INTEGER DEFAULT 0,
            expiry_date TEXT, -- Store as 'YYYY-MM-DD', nullable
            allergens_last_reviewed TEXT -- 'YYYY-MM-DD HH:MM', nullable (NULL = never reviewed)
        )
    ''')
    conn.commit()
    logging.info("Ensured 'items' table exists.")

@log_database_operation
def create_orders_table(conn):
    """Creates the orders table."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            supplier_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Generated', 
            notes TEXT
        )
    ''')
    conn.commit()
    logging.info("Ensured 'orders' table exists.")

@log_database_operation
def create_order_items_table(conn):
    """Creates the order_items linking table."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            order_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            item_id INTEGER, -- nullable: SET NULL when item is deleted
            item_name TEXT, -- name snapshot, survives item deletion
            ordered_quantity INTEGER NOT NULL,
            received_quantity INTEGER,
            FOREIGN KEY (order_id) REFERENCES orders (order_id) ON DELETE CASCADE ON UPDATE CASCADE,
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE SET NULL ON UPDATE CASCADE 
        )
    ''')
    conn.commit()
    logging.info("Ensured 'order_items' table exists.")

@log_database_operation
def create_staff_users_table(conn):
    """Creates the staff_users table for authentication."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS staff_users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_username ON staff_users(username)")
    conn.commit()
    logging.info("Ensured 'staff_users' table exists.")

@log_database_operation
def create_allergens_table(conn):
    """Creates the allergens master table."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS allergens (
            allergen_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    conn.commit()
    logging.info("Ensured 'allergens' table exists.")

@log_database_operation
def create_item_allergens_table(conn):
    """Creates the item_allergens linking table."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_allergens (
            item_id INTEGER NOT NULL, 
            allergen_id INTEGER NOT NULL,
            PRIMARY KEY (item_id, allergen_id), 
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE ON UPDATE CASCADE, 
            FOREIGN KEY (allergen_id) REFERENCES allergens (allergen_id) ON DELETE CASCADE ON UPDATE CASCADE
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_item_allergens_item_id ON item_allergens(item_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_item_allergens_allergen_id ON item_allergens(allergen_id)")
    conn.commit()
    logging.info("Ensured 'item_allergens' table and its indexes exist.")

@log_database_operation
def create_skipped_stock_items_table(conn):
    """Creates the skipped_stock_items table."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS skipped_stock_items (
            skipped_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL, 
            skipped_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved BOOLEAN DEFAULT 0 NOT NULL, 
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE ON UPDATE CASCADE 
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_skipped_resolved ON skipped_stock_items (resolved)")
    conn.commit()
    logging.info("Ensured 'skipped_stock_items' table exists.")

@log_database_operation
def create_item_allergens_may_contain_table(conn):
    """Creates the table for precautionary allergen labeling (May Contain)."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_allergens_may_contain (
            item_id INTEGER NOT NULL,
            allergen_id INTEGER NOT NULL,
            PRIMARY KEY (item_id, allergen_id),
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE ON UPDATE CASCADE,
            FOREIGN KEY (allergen_id) REFERENCES allergens (allergen_id) ON DELETE CASCADE ON UPDATE CASCADE
        )
    ''')
    conn.commit()
    logging.info("Ensured 'item_allergens_may_contain' table exists.")

@log_database_operation
def create_item_prices_table(conn):
    """Creates table for item pricing (Net Price & VAT). One-to-one with items."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_prices (
            item_id INTEGER PRIMARY KEY,
            net_price REAL DEFAULT 0.0,
            vat_rate REAL DEFAULT 0.0, -- Stored as percentage (e.g. 20.0)
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE ON UPDATE CASCADE
        )
    ''')
    conn.commit()
    logging.info("Ensured 'item_prices' table exists.")

@log_database_operation
def create_item_aliases_table(conn):
    """Creates a table to store alternative names (aliases) for items."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_aliases (
            alias_id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            alias_name TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    logging.info("Ensured 'item_aliases' table exists.")

@log_database_operation
def create_price_history_table(conn):
    """Creates a log table to track price changes over time."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            net_price REAL NOT NULL,
            vat_rate REAL DEFAULT 0.0,
            recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            source_order_id INTEGER, -- Links back to the specific delivery
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE,
            FOREIGN KEY (source_order_id) REFERENCES orders (order_id)
        )
    ''')
    conn.commit()
    logging.info("Ensured 'price_history' table exists.")

@log_database_operation
def migrate_add_admin_column(conn):
    """
    Checks if 'is_admin' column exists in staff_users. If not, adds it.
    Sets the default 'Admin' user to True (1), others to False (0).
    """
    if not conn: return
    cursor = conn.cursor()
    try:
        # Attempt to select the column to see if it exists
        cursor.execute("SELECT is_admin FROM staff_users LIMIT 1")
    except sqlite3.OperationalError:
        # If error, column is missing. Add it.
        logging.info("Migrating schema: Adding 'is_admin' column to staff_users.")
        cursor.execute("ALTER TABLE staff_users ADD COLUMN is_admin INTEGER DEFAULT 0")
        
        # Auto-promote the original "Admin" user so you don't get locked out
        cursor.execute("UPDATE staff_users SET is_admin = 1 WHERE username = 'Admin'")
        conn.commit()

@log_database_operation
def migrate_add_pack_size_column(conn):
    """
    Adds 'pack_size' column to items table if it doesn't exist.
    Default is 1 (e.g., 1 Box = 1 Unit).
    """
    if not conn: return
    cursor = conn.cursor()
    try:
        # Try to select the column
        cursor.execute("SELECT pack_size FROM items LIMIT 1")
    except sqlite3.OperationalError:
        logging.info("Migrating schema: Adding 'pack_size' to items table.")
        cursor.execute("ALTER TABLE items ADD COLUMN pack_size INTEGER DEFAULT 1")
        conn.commit()

@log_database_operation
def migrate_add_allergens_last_reviewed_column(conn):
    """
    Adds 'allergens_last_reviewed' column to items table if it doesn't
    exist. Stores 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD'), NULL = never
    reviewed. Stamped by mark_item_allergens_reviewed when allergens are
    saved in the AI Allergen Extractor.
    """
    if not conn: return
    cursor = conn.cursor()
    try:
        # Try to select the column
        cursor.execute("SELECT allergens_last_reviewed FROM items LIMIT 1")
    except sqlite3.OperationalError:
        logging.info("Migrating schema: Adding 'allergens_last_reviewed' to items table.")
        cursor.execute("ALTER TABLE items ADD COLUMN allergens_last_reviewed TEXT")
        conn.commit()

def migrate_add_item_name_snapshots(conn):
    """
    Adds the 'item_name' snapshot column to order_items and waste_log if
    either table predates it (CREATE TABLE IF NOT EXISTS never alters an
    existing table). Backfills existing rows from items.name so past
    order lines / waste entries survive item deletion. Idempotent.
    """
    if not conn: return
    cursor = conn.cursor()
    for table in ("order_items", "waste_log"):
        try:
            cursor.execute(f"SELECT item_name FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            logging.info(f"Migrating schema: Adding 'item_name' to {table} table.")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN item_name TEXT")
            cursor.execute(f"""
                UPDATE {table}
                SET item_name = (SELECT name FROM items WHERE items.id = {table}.item_id)
                WHERE item_name IS NULL
            """)
            conn.commit()

# --- Master Function to Ensure All Tables Exist ---
@log_database_operation
def ensure_all_tables_exist(conn):
    """
    Calls all individual table creation functions to ensure the database schema is set up.
    """
    if not conn:
        logging.error("Cannot ensure tables exist: No database connection.")
        return

    logging.info("Ensuring all database tables exist...")
    create_items_table(conn)
    create_orders_table(conn)
    create_order_items_table(conn)
    create_staff_users_table(conn) 
    create_allergens_table(conn)   
    create_item_allergens_table(conn)
    create_item_allergens_may_contain_table(conn)
    create_item_prices_table(conn)
    create_item_aliases_table(conn)
    create_price_history_table(conn)
    create_skipped_stock_items_table(conn) 
    # pack_size column: migration kept for existing DBs; the field is no
    # longer edited in the UI, but log_stock_failure still reads it.
    migrate_add_pack_size_column(conn)
    create_waste_log_table(conn)
    migrate_add_item_name_snapshots(conn)
    # ---------------------------
    migrate_add_allergens_last_reviewed_column(conn)
    migrate_add_admin_column(conn)
    create_app_settings_table(conn)
    logging.info("All table checks complete.")


# --- AI / OLLAMA SETTINGS (editable from the Admin app) ---
# Precedence: app_settings DB row > environment variable > built-in default.
# Env vars let headless/Docker installs configure AI without opening the UI:
#   IMS_OLLAMA_URL, IMS_VISION_MODEL, IMS_AI_TIMEOUT_S, IMS_AI_NUM_CTX
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_VISION_MODEL = "llama3.2-vision:11b"
DEFAULT_AI_TIMEOUT_S = 180
DEFAULT_AI_NUM_CTX = 8192

@log_database_operation
def create_app_settings_table(conn):
    """Simple key/value store for app settings (AI endpoint, model...)."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    conn.commit()
    logging.info("Ensured 'app_settings' table exists.")

@log_database_operation
def get_setting(_conn, key: str, default: str = None):
    if not _conn: return default
    row = _conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default

@log_database_operation
def set_setting(_conn, key: str, value: str):
    if not _conn: return False
    try:
        _conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        _conn.commit()
        logging.info(f"Setting '{key}' updated.")
        return True
    except sqlite3.Error as e:
        logging.error(f"Error saving setting '{key}': {e}")
        _conn.rollback()
        return False

@log_database_operation
def get_ai_settings(_conn):
    """Returns the AI config dict: DB app_settings row wins, then env var,
    then built-in default."""
    base_url = (get_setting(_conn, "ollama_base_url",
                            os.environ.get("IMS_OLLAMA_URL", DEFAULT_OLLAMA_BASE_URL))
                or "").rstrip("/")
    model = get_setting(_conn, "vision_model",
                        os.environ.get("IMS_VISION_MODEL", DEFAULT_VISION_MODEL))
    timeout_raw = get_setting(_conn, "ai_timeout_s",
                              os.environ.get("IMS_AI_TIMEOUT_S", str(DEFAULT_AI_TIMEOUT_S)))
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        timeout = DEFAULT_AI_TIMEOUT_S
    num_ctx_raw = get_setting(_conn, "ai_num_ctx",
                              os.environ.get("IMS_AI_NUM_CTX", str(DEFAULT_AI_NUM_CTX)))
    try:
        num_ctx = int(num_ctx_raw)
    except (TypeError, ValueError):
        num_ctx = DEFAULT_AI_NUM_CTX
    return {"base_url": base_url, "model": model, "timeout_s": timeout, "num_ctx": num_ctx}

def ollama_list_models(base_url: str, timeout: int = 6):
    """GET /api/tags -> list of model names, or (None, error_message)."""
    import requests
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [m.get("name", "?") for m in r.json().get("models", [])], None
    except requests.exceptions.ConnectionError:
        return None, f"Could not connect to {base_url}"
    except Exception as e:
        return None, str(e)

def ollama_test_connection(base_url: str, model: str, timeout: int = 60):
    """Minimal /api/generate round-trip. Returns (ok, message)."""
    import requests, time
    started = time.time()
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": "Reply with exactly: OK",
                  "stream": False, "options": {"num_predict": 5}},
            timeout=timeout)
        r.raise_for_status()
        reply = r.json().get("response", "").strip()
        return True, f"Model '{model}' responded in {time.time()-started:.1f}s: '{reply[:60]}'"
    except requests.exceptions.ConnectionError:
        return False, f"Could not connect to {base_url} — check the IP and that Ollama is running (ollama serve)."
    except requests.exceptions.Timeout:
        return False, f"Request to {base_url} timed out after {timeout}s."
    except Exception as e:
        return False, str(e)


# --- Data Fetching Functions (CACHING REMOVED to fix stale data) ---

@log_database_operation
def fetch_all_item_names(_conn): 
    """Fetches all item names from the items table, ordered by name."""
    if not _conn: return []
    cursor = _conn.cursor()
    cursor.execute("SELECT name FROM items ORDER BY name")
    items = cursor.fetchall()
    return [item[0] for item in items]

@log_database_operation
def fetch_all_items_with_ids(_conn):
    """Fetches all items as a list of dictionaries (id, name)."""
    if not _conn: return []
    cursor = _conn.cursor()
    cursor.execute("SELECT id, name FROM items ORDER BY name")
    items_data = cursor.fetchall()
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in items_data]

@log_database_operation
def fetch_tables(_conn):
    """Fetches all table names from the database."""
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        return [table[0] for table in tables]
    except sqlite3.Error as e: 
        logging.error(f"Error fetching table names: {e}")
        raise

@log_database_operation
def fetch_table_data(_conn, table_name):
    """Fetches all data and column names from a specific table."""
    if not _conn: return [], [] 
    cursor = _conn.cursor()
    try:
        # Fetch column names
        cursor.execute(f"PRAGMA table_info('{table_name}')") 
        columns_info = cursor.fetchall()
        if not columns_info:
            logging.warning(f"No columns found for table '{table_name}'. Table might not exist or is schema-less.")
            return [], []
        columns = [info[1] for info in columns_info]

        # Fetch data
        cursor.execute(f"SELECT * FROM '{table_name}'") 
        data = cursor.fetchall()
        
        return columns, data
    except sqlite3.Error as e:
        logging.error(f"Error fetching data for table '{table_name}': {e}")
        return [], [] 

@log_database_operation
def update_available_quantity(_conn, item_id: int, new_quantity: int):
    """Updates the available quantity for a specific item using its ID."""
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("UPDATE items SET available_quantity = ? WHERE id = ?", (new_quantity, item_id))
        _conn.commit()
        if cursor.rowcount == 0:
            logging.warning(f"No item found with ID {item_id} during available_quantity update attempt.")
            return False
        logging.info(f"Updated available_quantity for item ID {item_id} to {new_quantity}")
        return True
    except sqlite3.Error as e:
        logging.error(f"DB Error updating available_quantity for item_id {item_id}: {e}")
        raise 

@log_database_operation
def fetch_distinct_categories(_conn):
    """Fetches a list of distinct, non-empty category names from the items table."""
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT category FROM items WHERE category IS NOT NULL AND category != '' ORDER BY category")
        categories = cursor.fetchall()
        return [cat[0] for cat in categories]
    except sqlite3.Error as e:
        logging.error(f"Error fetching distinct categories: {e}")
        raise

@log_database_operation
def fetch_items_with_ids_by_category(_conn, category: str):
    """Fetches items (id, name) for a specific category."""
    if not _conn or not category: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("SELECT id, name FROM items WHERE category = ? ORDER BY name", (category,))
        items_data = cursor.fetchall()
        if not items_data:
            return []
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in items_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching items with IDs for category '{category}': {e}")
        raise

#@log_database_operation
#def search_item_by_id(_conn, item_id: int):
#    """Searches for an item by its ID and returns its details."""
#    if not _conn: return None
#    cursor = _conn.cursor()
#    try:
#        cursor.execute('''
#            SELECT id, name, category, main_supplier, secondary_supplier,
#                   available_quantity, desired_quantity, expiry_date
#            FROM items
#            WHERE id = ?
#        ''', (item_id,))
#        result = cursor.fetchone()
#        if result:
#            columns = [description[0] for description in cursor.description]
#            return dict(zip(columns, result))
#        else:
#            return None
#    except sqlite3.Error as e:
#        logging.error(f"Error searching item by id {item_id}: {e}")
#        raise

@log_database_operation
def search_item_by_id(_conn, item_id: int):
    """Searches for an item by its ID and returns its details."""
    if not _conn: return None
    cursor = _conn.cursor()
    try:
        # pack_size stays here: kept for display/legacy; log_stock_failure
        # no longer uses it (net_price is per box).
        cursor.execute('''
            SELECT id, name, category, main_supplier, secondary_supplier,
                   available_quantity, desired_quantity, expiry_date, pack_size,
                   allergens_last_reviewed
            FROM items
            WHERE id = ?
        ''', (item_id,))
        result = cursor.fetchone()
        if result:
            columns = [description[0] for description in cursor.description]
            return dict(zip(columns, result))
        else:
            return None
    except sqlite3.Error as e:
        logging.error(f"Error searching item by id {item_id}: {e}")
        raise

@log_database_operation
def get_items_to_order(_conn):
    """
    Fetches items whose desired quantity exceeds available quantity —
    filtered STRICTLY by physical stock deficit, never hidden because an
    open order ('Generated' / 'Partially Delivered') exists. A fresh
    Stock Check must always surface the deficit. pending_qty is returned
    as pending_order_quantity for INFORMATIONAL display only (staff sees
    "already on an open order" but the item and its full deficit stay
    visible). pending_qty clamps to 0 per line (MAX) so over-delivery on
    a partially delivered order never credits phantom stock.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            SELECT
                i.id,
                i.name,
                i.available_quantity,
                i.desired_quantity,
                (i.desired_quantity - i.available_quantity) AS deficit_quantity,
                COALESCE(pending.pending_qty, 0) AS pending_order_quantity,
                (i.desired_quantity - i.available_quantity) AS order_quantity,
                i.main_supplier,
                i.secondary_supplier
            FROM items i
            LEFT JOIN (
                SELECT
                    oi.item_id,
                    SUM(MAX(0, oi.ordered_quantity - COALESCE(oi.received_quantity, 0))) AS pending_qty
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.order_id
                WHERE o.status IN ('Generated', 'Partially Delivered')
                  AND oi.item_id IS NOT NULL
                GROUP BY oi.item_id
            ) pending ON i.id = pending.item_id
            WHERE i.desired_quantity > i.available_quantity
              AND (i.desired_quantity - i.available_quantity) > 0
            ORDER BY i.name
        ''')
        results = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in results]
    except sqlite3.Error as e:
        logging.error(f"Error fetching items to order: {e}")
        raise

@log_database_operation
def insert_order(_conn, supplier_name: str, status: str = 'Generated', notes: str = ''):
    """Inserts a new order record and returns the new order_id."""
    if not _conn: return None
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO orders (supplier_name, status, notes, generation_timestamp)
            VALUES (?, ?, ?, ?)
        ''', (supplier_name, status, notes, datetime.now()))
        _conn.commit()
        new_order_id = cursor.lastrowid
        logging.info(f"Inserted new order for supplier '{supplier_name}' with ID: {new_order_id}")
        return new_order_id
    except sqlite3.Error as e:
        logging.error(f"Error inserting order for supplier {supplier_name}: {e}")
        _conn.rollback() 
        raise

@log_database_operation
def insert_order_item(_conn, order_id: int, item_id: int, ordered_quantity: int):
    """Inserts an item line (using item_id) into the order_items table."""
    if not _conn: return False 
    cursor = _conn.cursor()
    try:
        name_row = _conn.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
        cursor.execute('''
            INSERT INTO order_items (order_id, item_id, item_name, ordered_quantity)
            VALUES (?, ?, ?, ?)
        ''', (order_id, item_id, name_row[0] if name_row else None, ordered_quantity))
        logging.info(f"Prepared to insert item ID {item_id} (Qty: {ordered_quantity}) for order ID: {order_id}")
        return True 
    except sqlite3.Error as e:
        logging.error(f"Error inserting order item ID {item_id} for order {order_id}: {e}")
        raise

@log_database_operation
def fetch_open_orders(_conn):
    """
    Fetches orders that are 'Generated' or 'Partially Delivered'.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            SELECT
                order_id,
                supplier_name,
                strftime('%Y-%m-%d %H:%M', generation_timestamp) as generation_time,
                status
            FROM orders
            WHERE status = 'Generated' OR status = 'Partially Delivered'
            ORDER BY generation_timestamp DESC
        ''')
        orders_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in orders_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching open orders: {e}")
        raise

@log_database_operation
def fetch_order_items_for_delivery(_conn, order_id: int):
    """
    Fetches items for a given order_id.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            SELECT
                oi.order_item_id,
                oi.item_id,
                COALESCE(i.name, oi.item_name, '(deleted item)') AS item_name,
                oi.ordered_quantity,
                oi.received_quantity
            FROM order_items oi
            LEFT JOIN items i ON oi.item_id = i.id
            WHERE oi.order_id = ?
            ORDER BY item_name
        ''', (order_id,))
        items_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in items_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching order items for delivery for order_id {order_id}: {e}")
        raise

def fetch_item_order_history(_conn, search_term: str, limit: int = 10):
    """
    Read-only: the last `limit` order appearances for items whose name
    contains search_term (case-insensitive), newest first.
    Uses the item_name snapshot so orders still show deleted items.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            SELECT
                COALESCE(i.name, oi.item_name, '(deleted item)') AS item_name,
                strftime('%Y-%m-%d %H:%M', o.generation_timestamp) AS order_date,
                oi.ordered_quantity,
                o.supplier_name,
                o.status
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            LEFT JOIN items i ON oi.item_id = i.id
            WHERE COALESCE(i.name, oi.item_name) LIKE ?
            ORDER BY o.generation_timestamp DESC
            LIMIT ?
        ''', (f'%{search_term}%', limit))
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Error fetching order history for '{search_term}': {e}")
        raise

@log_database_operation
def update_order_item_received_quantity(_conn, order_item_id: int, new_received_qty: int):
    """
    Updates the received quantity for a specific order_item_id.
    Does NOT commit the transaction; expects calling function to manage transaction.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            UPDATE order_items
            SET received_quantity = ?
            WHERE order_item_id = ?
        ''', (new_received_qty, order_item_id))
        
        if cursor.rowcount == 0:
            logging.warning(f"No order_item found with ID {order_item_id} for received quantity update.")
            return False
        logging.info(f"Set received_quantity to {new_received_qty} for order_item_id {order_item_id} (pending commit).")
        return True
    except sqlite3.Error as e:
        logging.error(f"Error updating received quantity for order_item_id {order_item_id}: {e}")
        raise 

@log_database_operation
def insert_substitution_order_item(_conn, order_id: int, item_id: int, item_name: str, received_qty: int):
    """
    Inserts an order_items row for an item that was NOT on the original order
    (substitution or extra delivered item): ordered_quantity = 0,
    received_quantity = received_qty.
    Does NOT commit the transaction; expects calling function to manage transaction.
    Returns the new order_item_id, or None on failure.
    """
    if not _conn: return None
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO order_items (order_id, item_id, item_name, ordered_quantity, received_quantity)
            VALUES (?, ?, ?, 0, ?)
        ''', (order_id, item_id, item_name, received_qty))
        new_id = cursor.lastrowid
        logging.info(f"Inserted substitution order_item {new_id} (order {order_id}, item {item_id}, recv {received_qty}) (pending commit).")
        return new_id
    except sqlite3.Error as e:
        logging.error(f"Error inserting substitution order item (order {order_id}, item {item_id}): {e}")
        raise

@log_database_operation
def update_order_status_and_notes(_conn, order_id: int, status: str, notes: str = ''):
    """
    Updates the status and notes for a given order_id.
    Does NOT commit the transaction; expects calling function to manage transaction.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        current_notes = cursor.execute("SELECT notes FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        updated_notes = notes 
        if current_notes and current_notes[0]: 
            updated_notes = f"{current_notes[0]}\n---\n{datetime.now().strftime('%Y-%m-%d %H:%M')}: {notes}"
        elif notes: 
             updated_notes = f"{datetime.now().strftime('%Y-%m-%d %H:%M')}: {notes}"
        else: 
            updated_notes = current_notes[0] if current_notes else ''


        cursor.execute('''
            UPDATE orders
            SET status = ?, notes = ?
            WHERE order_id = ?
        ''', (status, updated_notes, order_id))
        
        if cursor.rowcount == 0:
            logging.warning(f"No order found with ID {order_id} for status/notes update.")
            return False
        logging.info(f"Set status to '{status}' for order ID {order_id} (pending commit).")
        return True
    except sqlite3.Error as e:
        logging.error(f"Error updating status/notes for order {order_id}: {e}")
        raise 

@log_database_operation
def fetch_all_orders(_conn):
    """
    Fetches all orders with key details.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT
                order_id,
                strftime('%Y-%m-%d %H:%M', generation_timestamp) as generation_time,
                supplier_name,
                status,
                notes
            FROM orders
            ORDER BY generation_timestamp DESC
        """)
        orders_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in orders_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching all orders: {e}")
        raise

@log_database_operation
def log_skipped_item(_conn, item_id: int):
    """
    Logs an item as skipped in the skipped_stock_items table using its ID.
    Commits immediately.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO skipped_stock_items (item_id, resolved)
            VALUES (?, 0)
        """, (item_id,))
        _conn.commit() 
        logging.info(f"Logged item ID {item_id} as skipped for stock check.")
        return True
    except sqlite3.Error as e:
        logging.error(f"DB Error logging skipped item ID {item_id}: {e}")
        _conn.rollback() 
        raise 

@log_database_operation
def fetch_distinct_suppliers(_conn):
    """
    Fetches a list of distinct, non-empty main_supplier names from the items table.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT DISTINCT main_supplier
            FROM items
            WHERE main_supplier IS NOT NULL AND main_supplier != ''
            ORDER BY main_supplier
        """)
        suppliers = cursor.fetchall()
        return [supplier[0] for supplier in suppliers]
    except sqlite3.Error as e:
        logging.error(f"Error fetching distinct suppliers: {e}")
        raise

@log_database_operation
def fetch_items_with_ids_by_supplier(_conn, supplier_name: str, include_secondary: bool = False):
    """
    Fetches items (id, name, role) for a specific supplier.
    If include_secondary is True, includes items where supplier is secondary.
    """
    if not _conn or not supplier_name: return []
    cursor = _conn.cursor()
    try:
        if include_secondary:
            cursor.execute("""
                SELECT id, name, 
                CASE WHEN main_supplier = ? THEN 'Main' ELSE 'Secondary' END as role
                FROM items
                WHERE main_supplier = ? OR secondary_supplier = ?
                ORDER BY name
            """, (supplier_name, supplier_name, supplier_name))
        else:
            # Default behavior (Main only) to keep compatibility with Stock Check
            cursor.execute("""
                SELECT id, name, 'Main' as role
                FROM items
                WHERE main_supplier = ?
                ORDER BY name
            """, (supplier_name,))
            
        items_data = cursor.fetchall()
        if not items_data:
            return []
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in items_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching items with IDs for supplier '{supplier_name}': {e}")
        raise

@log_database_operation
def fetch_unresolved_skipped_items_details(_conn):
    """
    Fetches details of unresolved skipped stock items.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT
                ssi.skipped_item_id,
                ssi.item_id,
                i.name AS item_name,
                strftime('%Y-%m-%d %H:%M', ssi.skipped_timestamp) as skipped_time
            FROM skipped_stock_items ssi
            JOIN items i ON ssi.item_id = i.id
            WHERE ssi.resolved = 0
            ORDER BY ssi.skipped_timestamp ASC
        """)
        skipped_items_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in skipped_items_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching unresolved skipped items details: {e}")
        raise

@log_database_operation
def mark_skipped_item_resolved(_conn, skipped_item_id: int):
    """
    Marks a specific skipped stock item as resolved.
    Commits the change.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            UPDATE skipped_stock_items
            SET resolved = 1
            WHERE skipped_item_id = ?
        """, (skipped_item_id,))
        _conn.commit() 
        if cursor.rowcount == 0:
            logging.warning(f"No skipped item found with ID {skipped_item_id} to mark as resolved.")
            return False 
        logging.info(f"Marked skipped item ID {skipped_item_id} as resolved.")
        return True
    except sqlite3.Error as e:
        logging.error(f"DB Error marking skipped item ID {skipped_item_id} as resolved: {e}")
        _conn.rollback() 
        raise

@log_database_operation
def add_item_from_csv_import(_conn, name: str, category: str, main_supplier: str, 
                             secondary_supplier: str, available_quantity: int, desired_quantity: int,
                             expiry_date_str: str = None): 
    """
    Inserts a new item into the 'items' table.
    """
    if not _conn:
        logging.error("No database connection provided to add_item_from_csv_import.")
        return False
    cursor = _conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO items (name, category, main_supplier, secondary_supplier, 
                               available_quantity, desired_quantity, expiry_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, category, main_supplier, secondary_supplier, 
              int(available_quantity), int(desired_quantity), expiry_date_str))
        _conn.commit()
        logging.info(f"Successfully added item '{name}' via CSV import.")
        return True
    except sqlite3.IntegrityError as e:
        logging.warning(f"IntegrityError adding item '{name}' via CSV (likely already exists): {e}")
        _conn.rollback() 
        return False 
    except sqlite3.Error as e:
        logging.error(f"SQLite error adding item '{name}' via CSV: {e}")
        _conn.rollback()
        return False
    except ValueError as e: 
        logging.error(f"ValueError adding item '{name}' (likely quantity conversion): {e}")
        _conn.rollback()
        return False

@log_database_operation
def update_item_core_details_by_id(_conn, item_id: int, category: str, main_supplier: str, 
                                   secondary_supplier: str, desired_quantity: int, 
                                   expiry_date_str: str = None):
    """
    Updates core details for a specific item identified by its ID.
    Commits on successful update.
    """
    if not _conn:
        logging.error("No database connection provided to update_item_core_details_by_id.")
        return False
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            UPDATE items
            SET category = ?,
                main_supplier = ?,
                secondary_supplier = ?,
                desired_quantity = ?,
                expiry_date = ?
            WHERE id = ?
        """, (category, main_supplier, secondary_supplier, int(desired_quantity), 
              expiry_date_str, item_id))
        _conn.commit()
        
        if cursor.rowcount == 0:
            logging.warning(f"Attempted to update item ID {item_id}, but no row was found or data was identical. Rows affected: 0.")
        else:
            logging.info(f"Successfully updated core details for item ID {item_id}. Rows affected: {cursor.rowcount}.")
        
        return True 
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating item ID {item_id} core details: {e}")
        _conn.rollback()
        return False
    except ValueError as e: 
        logging.error(f"ValueError updating item ID {item_id} (likely quantity conversion): {e}")
        _conn.rollback()
        return False

def sanitize_df_for_csv(df):
    """Neutralise spreadsheet formula injection (audit 2026-10-31 #4).

    A CSV cell starting with = + - @ (or tab/CR) is EXECUTED by Excel /
    LibreOffice on open, so DB-sourced free text (item names, categories,
    query results) is an injection vector. Prefix such string cells with
    a single quote so they render as literal text. Numeric columns are
    left untouched (a leading '-' on a number is a sign, not a formula).
    Call on any DataFrame before .to_csv() for a download_button."""
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].map(
                lambda v: "'" + v
                if isinstance(v, str) and v[:1] in ('=', '+', '-', '@', '\t', '\r')
                else v)
    return df

@log_database_operation
def fetch_all_items_for_export(_conn):
    """
    Fetches all data fields for all items, ordered by name, for export.
    """
    if not _conn:
        logging.error("No database connection provided to fetch_all_items_for_export.")
        return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                id, 
                name, 
                category, 
                main_supplier, 
                secondary_supplier, 
                available_quantity, 
                desired_quantity, 
                expiry_date, 
                allergens_last_reviewed 
            FROM items 
            ORDER BY name ASC
        """)
        items_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in items_data]
    except sqlite3.Error as e:
        logging.error(f"SQLite error fetching all items for export: {e}")
        return [] 

@log_database_operation
def fetch_items_with_supplier_by_category(_conn, category_name: str):
    """
    Fetches items (id, name, main_supplier) for a specific category.
    """
    if not _conn or not category_name: 
        return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT id, name, main_supplier
            FROM items
            WHERE category = ? AND main_supplier IS NOT NULL AND main_supplier != ''
            ORDER BY name ASC
        """, (category_name,))
        items_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in items_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching items with supplier for category '{category_name}': {e}")
        return []

@log_database_operation
def delete_order_by_id(_conn, order_id: int):
    """
    Deletes an order and its associated order items from the database by its order_id.
    Commits the change.
    """
    if not _conn:
        logging.error("No database connection provided to delete_order_by_id.")
        return False
    cursor = _conn.cursor()
    try:
        cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
        _conn.commit()
        
        if cursor.rowcount > 0:
            logging.info(f"Successfully deleted order ID {order_id} and its items. Rows affected: {cursor.rowcount}.")
            return True
        else:
            logging.warning(f"Attempted to delete order ID {order_id}, but no such order was found.")
            return False 
            
    except sqlite3.Error as e:
        logging.error(f"SQLite error deleting order ID {order_id}: {e}")
        _conn.rollback()
        return False

@log_database_operation
def fetch_all_allergen_details(_conn):
    """
    Fetches all allergens (id and name) from the allergens table.
    """
    if not _conn: 
        return []
    cursor = _conn.cursor()
    try:
        cursor.execute("SELECT allergen_id, name FROM allergens ORDER BY name ASC")
        allergens_data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in allergens_data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching all allergen details: {e}")
        return []

@log_database_operation
def add_new_allergen(_conn, allergen_name: str):
    """
    Adds a new allergen to the 'allergens' table if it doesn't already exist.
    """
    if not _conn or not allergen_name or not allergen_name.strip():
        logging.warning("Cannot add allergen: connection or allergen_name is invalid.")
        return None
    
    processed_name = allergen_name.strip().title() 

    cursor = _conn.cursor()
    try:
        cursor.execute("INSERT INTO allergens (name) VALUES (?)", (processed_name,))
        _conn.commit()
        new_allergen_id = cursor.lastrowid
        logging.info(f"Successfully added new allergen '{processed_name}' with ID {new_allergen_id}.")
        return new_allergen_id
    except sqlite3.IntegrityError:
        _conn.rollback()
        cursor.execute("SELECT allergen_id FROM allergens WHERE name = ?", (processed_name,))
        existing = cursor.fetchone()
        if existing:
            logging.info(f"Allergen '{processed_name}' already exists with ID {existing[0]}.")
            return existing[0]
        else:
            logging.warning(f"IntegrityError for allergen '{processed_name}', but could not retrieve existing ID.")
            return None
    except sqlite3.Error as e:
        logging.error(f"SQLite error adding allergen '{processed_name}': {e}")
        _conn.rollback()
        return None

@log_database_operation
def merge_allergen_ids(_conn, keep_id: int, remove_id: int):
    """
    Merges 'remove_id' into 'keep_id'.
    1. Re-links items from remove_id to keep_id (handling duplicates).
    2. Deletes remove_id from the master table.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        # 1. If an item has BOTH allergens, we just delete the link to the one we are removing
        # (Since it already has the link to the one we are keeping)
        cursor.execute("""
            DELETE FROM item_allergens 
            WHERE allergen_id = ? 
            AND item_id IN (SELECT item_id FROM item_allergens WHERE allergen_id = ?)
        """, (remove_id, keep_id))

        # 2. For items that ONLY have the 'remove_id', switch them to 'keep_id'
        cursor.execute("""
            UPDATE item_allergens 
            SET allergen_id = ? 
            WHERE allergen_id = ?
        """, (keep_id, remove_id))

        # 3. Finally, delete the allergen from the master list
        cursor.execute("DELETE FROM allergens WHERE allergen_id = ?", (remove_id,))
        
        _conn.commit()
        logging.info(f"Merged allergen ID {remove_id} into {keep_id}.")
        return True
    except sqlite3.Error as e:
        logging.error(f"Error merging allergens {remove_id} -> {keep_id}: {e}")
        _conn.rollback()
        return False

@log_database_operation
def fetch_allergens_for_item(_conn, item_id: int):
    """Fetches list of allergen names currently linked to a specific item."""
    if not _conn: return []
    cursor = _conn.cursor()
    cursor.execute("""
        SELECT a.name
        FROM item_allergens ia
        JOIN allergens a ON ia.allergen_id = a.allergen_id
        WHERE ia.item_id = ?
        ORDER BY a.name
    """, (item_id,))
    return [row[0] for row in cursor.fetchall()]

@log_database_operation
def update_item_allergens(_conn, item_id: int, allergen_names_list: list):
    """
    Links a list of allergen names to an item.
    1. Checks if allergen exists in master list (adds it if missing).
    2. Clears OLD links for this item.
    3. Adds NEW links.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        # 1. Resolve IDs for all names, adding new ones if needed
        allergen_ids = []
        for name in allergen_names_list:
            clean_name = name.strip().title()
            if not clean_name: continue
            
            # Check if exists
            cursor.execute("SELECT allergen_id FROM allergens WHERE name = ?", (clean_name,))
            row = cursor.fetchone()
            if row:
                allergen_ids.append(row[0])
            else:
                # Add new master allergen dynamically
                cursor.execute("INSERT INTO allergens (name) VALUES (?)", (clean_name,))
                allergen_ids.append(cursor.lastrowid)
                logging.info(f"Auto-added new master allergen: {clean_name}")

        # 2. Clear existing links for this item (Full Replace Strategy)
        cursor.execute("DELETE FROM item_allergens WHERE item_id = ?", (item_id,))

        # 3. Insert new links
        for aid in set(allergen_ids): # ensure unique IDs only
            cursor.execute("INSERT INTO item_allergens (item_id, allergen_id) VALUES (?, ?)", (item_id, aid))
        
        _conn.commit()
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error updating allergens for item {item_id}: {e}")
        return False

@log_database_operation
def mark_item_allergens_reviewed(_conn, item_id: int):
    """
    Stamps items.allergens_last_reviewed with the current local time
    ('YYYY-MM-DD HH:MM'). Call after saving an item's allergen lists —
    confirming an item as having ZERO allergens counts as a review too.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            UPDATE items
            SET allergens_last_reviewed = strftime('%Y-%m-%d %H:%M', 'now', 'localtime')
            WHERE id = ?
        """, (item_id,))
        _conn.commit()
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error stamping allergens_last_reviewed for item {item_id}: {e}")
        return False

@log_database_operation
def fetch_inventory_with_allergens(_conn):
    """
    Fetches inventory with BOTH 'Contains' and 'May Contain' lists.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        # We use DISTINCT in GROUP_CONCAT to prevent duplicates if joins multiply rows
        cursor.execute("""
            SELECT 
                i.id, 
                i.name, 
                i.category, 
                i.available_quantity,
                i.desired_quantity,
                i.expiry_date,
                i.main_supplier,
                i.allergens_last_reviewed,
                GROUP_CONCAT(DISTINCT a_main.name) as contains_allergens,
                GROUP_CONCAT(DISTINCT a_may.name) as may_contain_allergens
            FROM items i
            LEFT JOIN item_allergens ia ON i.id = ia.item_id
            LEFT JOIN allergens a_main ON ia.allergen_id = a_main.allergen_id
            LEFT JOIN item_allergens_may_contain iamc ON i.id = iamc.item_id
            LEFT JOIN allergens a_may ON iamc.allergen_id = a_may.allergen_id
            GROUP BY i.id
            ORDER BY i.name
        """)
        data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching inventory with full allergens: {e}")
        return []

@log_database_operation
def fetch_may_contain_allergens_for_item(_conn, item_id: int):
    """Fetches list of 'May Contain' allergen names."""
    if not _conn: return []
    cursor = _conn.cursor()
    cursor.execute("""
        SELECT a.name
        FROM item_allergens_may_contain ia
        JOIN allergens a ON ia.allergen_id = a.allergen_id
        WHERE ia.item_id = ?
        ORDER BY a.name
    """, (item_id,))
    return [row[0] for row in cursor.fetchall()]

@log_database_operation
def update_item_may_contain_allergens(_conn, item_id: int, allergen_names_list: list):
    """
    Links 'May Contain' allergens to an item. 
    Almost identical to update_item_allergens but targets the may_contain table.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        allergen_ids = []
        for name in allergen_names_list:
            clean_name = name.strip().title()
            if not clean_name: continue
            
            cursor.execute("SELECT allergen_id FROM allergens WHERE name = ?", (clean_name,))
            row = cursor.fetchone()
            if row:
                allergen_ids.append(row[0])
            else:
                cursor.execute("INSERT INTO allergens (name) VALUES (?)", (clean_name,))
                allergen_ids.append(cursor.lastrowid)

        cursor.execute("DELETE FROM item_allergens_may_contain WHERE item_id = ?", (item_id,))

        for aid in set(allergen_ids):
            cursor.execute("INSERT INTO item_allergens_may_contain (item_id, allergen_id) VALUES (?, ?)", (item_id, aid))
        
        _conn.commit()
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error updating 'may contain' for item {item_id}: {e}")
        return False

@log_database_operation
def update_item_price(_conn, item_id: int, net_price: float, vat_rate: float):
    """Updates or inserts price info for an item using Upsert strategy."""
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        # SQLite ON CONFLICT requires version 3.24+ (Mint 20.3 supports this)
        cursor.execute("""
            INSERT INTO item_prices (item_id, net_price, vat_rate)
            VALUES (?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                net_price = excluded.net_price,
                vat_rate = excluded.vat_rate
        """, (item_id, float(net_price), float(vat_rate)))
        _conn.commit()
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error updating price for item {item_id}: {e}")
        return False

@log_database_operation
def fetch_inventory_with_prices(_conn):
    """
    Fetches inventory items joined with their price data.
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                i.id, 
                i.name, 
                i.available_quantity,
                i.main_supplier,
                COALESCE(ip.net_price, 0.0) as net_price,
                COALESCE(ip.vat_rate, 0.0) as vat_rate
            FROM items i
            LEFT JOIN item_prices ip ON i.id = ip.item_id
            ORDER BY i.name
        """)
        data = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching inventory prices: {e}")
        return []

@log_database_operation
def add_item_alias(_conn, item_id: int, alias_name: str):
    """Learns a new name for an item (e.g., mapping 'Beef Dripping' to 'Fat')."""
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        clean_name = alias_name.strip()
        # Check if already exists to prevent duplicates
        cursor.execute("SELECT alias_id FROM item_aliases WHERE item_id = ? AND alias_name = ?", (item_id, clean_name))
        if cursor.fetchone():
            return True # Already learned
        
        cursor.execute("INSERT INTO item_aliases (item_id, alias_name) VALUES (?, ?)", (item_id, clean_name))
        _conn.commit()
        logging.info(f"Learned alias '{clean_name}' for item ID {item_id}")
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error adding alias: {e}")
        return False

@log_database_operation
def fetch_supplier_items_with_aliases(_conn, supplier_name: str):
    """
    Fetches items for a supplier AND their known aliases.
    Returns list of dicts: {'id': 1, 'name': 'Fat', 'aliases': 'Beef Dripping, Lard'}
    """
    if not _conn: return []
    cursor = _conn.cursor()
    try:
        # Fetch items (Main or Secondary)
        cursor.execute("""
            SELECT i.id, i.name
            FROM items i
            WHERE i.main_supplier = ? OR i.secondary_supplier = ?
        """, (supplier_name, supplier_name))
        items = cursor.fetchall()
        
        result = []
        for item_id, name in items:
            # Fetch aliases for this item
            cursor.execute("SELECT alias_name FROM item_aliases WHERE item_id = ?", (item_id,))
            aliases = [row[0] for row in cursor.fetchall()]
            
            # Combine into a string for the AI context
            alias_str = ", ".join(aliases)
            result.append({
                "id": item_id,
                "name": name,
                "aliases": alias_str
            })
        return result
    except sqlite3.Error as e:
        logging.error(f"Error fetching items with aliases: {e}")
        return []

@log_database_operation
def log_price_history(_conn, item_id: int, net_price: float, vat_rate: float, order_id: int = None):
    """
    Inserts a record into the price history log.
    Does not update the main price table; just records the event.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO price_history (item_id, net_price, vat_rate, source_order_id, recorded_at)
            VALUES (?, ?, ?, ?, ?)
        """, (item_id, float(net_price), float(vat_rate), order_id, datetime.now()))
        _conn.commit()
        return True
    except sqlite3.Error as e:
        _conn.rollback()
        logging.error(f"Error logging price history for item {item_id}: {e}")
        return False

# --- PIN hashing (salted) ---
# Stored format: "scrypt$<salt_hex>$<hash_hex>". Legacy rows stored a bare
# unsalted SHA-256 hex digest; verify_staff_login upgrades them on next login.
_SCRYPT_N = 2**14  # 16 MiB per hash; plenty strong for a 4-digit PIN,
                   # stays under OpenSSL's default scrypt memory limit.

def hash_pin(pin_str, salt_bytes=None):
    """Hashes a PIN with scrypt + per-user random salt."""
    if salt_bytes is None:
        salt_bytes = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin_str.encode(), salt=salt_bytes, n=_SCRYPT_N, r=8, p=1, dklen=32,
        maxmem=64 * 1024 * 1024
    )
    return f"scrypt${salt_bytes.hex()}${digest.hex()}"

def verify_pin_hash(pin_str, stored_hash):
    """Constant-time check of a PIN against a stored hash (new or legacy format)."""
    if stored_hash and stored_hash.startswith("scrypt$"):
        try:
            _, salt_hex, hash_hex = stored_hash.split("$", 2)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
        except ValueError:
            return False
        digest = hashlib.scrypt(pin_str.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1,
                                dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest, expected)
    # Legacy: unsalted SHA-256 hex digest
    legacy = hashlib.sha256(pin_str.encode()).hexdigest()
    return hmac.compare_digest(legacy, stored_hash or "")

@log_database_operation
def ensure_admin_user_exists(conn):
    """
    Checks if staff_users is empty. If so, creates a default Admin.
    Default PIN: 1234
    """
    if not conn: return
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM staff_users")
    count = cursor.fetchone()[0]
    
    if count == 0:
        default_pin_hash = hash_pin("1234")
        cursor.execute('''
            INSERT INTO staff_users (username, hashed_password, is_active)
            VALUES (?, ?, 1)
        ''', ("Admin", default_pin_hash))
        conn.commit()
        logging.info("Table 'staff_users' was empty. Created default user 'Admin' with PIN '1234'.")

@log_database_operation
def verify_staff_login(conn, input_pin):
    if not conn: return None
    pin = str(input_pin).strip()
    cursor = conn.cursor()
    
    # Verify each active user's stored hash (salted scrypt, legacy SHA-256
    # still accepted and upgraded in place on successful login).
    cursor.execute('''
        SELECT user_id, username, is_admin, hashed_password 
        FROM staff_users 
        WHERE is_active = 1
    ''')
    
    for row in cursor.fetchall():
        if verify_pin_hash(pin, row[3]):
            legacy_login = False
            if not row[3].startswith("scrypt$"):
                # Legacy unsalted hash: upgrade now that we know the PIN.
                # Flag the transition so the login UI can show the
                # security banner (audit 2026-10-31 #2).
                cursor.execute(
                    "UPDATE staff_users SET hashed_password = ? WHERE user_id = ?",
                    (hash_pin(pin), row[0])
                )
                conn.commit()
                logging.info(f"Upgraded legacy PIN hash for user '{row[1]}'.")
                legacy_login = True
            return {'user_id': row[0], 'username': row[1], 'is_admin': bool(row[2]),
                    'legacy_login': legacy_login}
    return None

@log_database_operation
def list_legacy_pin_accounts(conn):
    """Usernames still stored with a legacy unsalted SHA-256 PIN hash
    (audit 2026-10-31 #2). Empty list = every account is on scrypt."""
    if not conn: return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT username FROM staff_users
        WHERE hashed_password NOT LIKE 'scrypt$%'
        ORDER BY username
    """)
    return [r[0] for r in cursor.fetchall()]

@log_database_operation
def fetch_all_staff_users(conn):
    if not conn: return []
    cursor = conn.cursor()
    # UPDATED SELECT
    cursor.execute("SELECT user_id, username, is_active, is_admin FROM staff_users ORDER BY username")
    data = cursor.fetchall()
    return [{'user_id': r[0], 'username': r[1], 'is_active': r[2], 'is_admin': r[3]} for r in data]

@log_database_operation
def manage_staff_user(conn, action, username=None, pin=None, user_id=None, is_admin=0):
    """
    Handles Add, Update PIN, Delete, and Role Toggle.
    """
    if not conn: return False
    cursor = conn.cursor()
    
    try:
        if action == 'add':
            if not username or not pin: return False
            hashed = hash_pin(str(pin))
            # Insert with is_admin flag
            cursor.execute("INSERT INTO staff_users (username, hashed_password, is_admin) VALUES (?, ?, ?)", 
                           (username, hashed, int(is_admin)))
            
        elif action == 'update_pin':
            if not user_id or not pin: return False
            hashed = hash_pin(str(pin))
            cursor.execute("UPDATE staff_users SET hashed_password = ? WHERE user_id = ?", (hashed, user_id))
            
        elif action == 'toggle_role':
            # New Action: Flip the admin status
            if not user_id: return False
            cursor.execute("UPDATE staff_users SET is_admin = ? WHERE user_id = ?", (int(is_admin), user_id))

        elif action == 'delete':
            if not user_id: return False
            cursor.execute("DELETE FROM staff_users WHERE user_id = ?", (user_id,))
            
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error managing user ({action}): {e}")
        return False

@log_database_operation
def fetch_spending_data_over_time(conn):
    """
    Fetches total spend per order for all Delivered/Partially Delivered orders.
    Prioritizes historical price (from price_history), falls back to current item_prices.
    Returns: List of dicts [{'date': '2025-01-01', 'total_cost': 150.50}]
    """
    if not conn: return []
    cursor = conn.cursor()
    
    # Logic: 
    # 1. Join Orders -> Order Items.
    # 2. Correlated scalar subquery on price_history (latest history_id per
    #    item+order) — price_history is append-only, so a direct LEFT JOIN
    #    would multiply order_items rows and double-count spend on
    #    multi-stage deliveries. item_prices (current) is the fallback.
    # 3. Multiply Qty * Price.
    
    query = """
        SELECT 
            o.generation_timestamp,
            COALESCE(SUM(COALESCE(oi.received_quantity, 0) * COALESCE(
                (SELECT ph.net_price
                 FROM price_history ph
                 WHERE ph.item_id = oi.item_id AND ph.source_order_id = o.order_id
                 ORDER BY ph.history_id DESC
                 LIMIT 1),
                ip.net_price,
                0
            )), 0.0) as total_cost
        FROM orders o
        JOIN order_items oi ON o.order_id = oi.order_id
        LEFT JOIN item_prices ip ON oi.item_id = ip.item_id
        WHERE o.status IN ('Delivered', 'Partially Delivered')
        GROUP BY o.order_id
        ORDER BY o.generation_timestamp ASC
    """
    
    cursor.execute(query)
    data = cursor.fetchall()
    
    # Return as list of dicts for easy Pandas conversion
    return [{'date': row[0], 'total_cost': row[1]} for row in data]

@log_database_operation
def fetch_item_spending_details(conn):
    """
    Fetches granular spending data: Date, Item Name, Line Total, AND Quantity.
    """
    if not conn: return []
    cursor = conn.cursor()
    
    # Added oi.received_quantity to the SELECT.
    # Scalar subquery (not a LEFT JOIN) on price_history so multiple
    # price_history rows per (item, order) cannot multiply the result set.
    # COALESCE(i.name, oi.item_name) keeps the snapshot name for deleted items.
    query = """
        SELECT 
            o.generation_timestamp,
            COALESCE(i.name, oi.item_name, '(deleted item)') as item_name,
            (COALESCE(oi.received_quantity, 0) * COALESCE(
                (SELECT ph.net_price
                 FROM price_history ph
                 WHERE ph.item_id = oi.item_id AND ph.source_order_id = o.order_id
                 ORDER BY ph.history_id DESC
                 LIMIT 1),
                ip.net_price,
                0
            )) as line_cost,
            COALESCE(oi.received_quantity, 0) as received_quantity
        FROM orders o
        JOIN order_items oi ON o.order_id = oi.order_id
        LEFT JOIN items i ON oi.item_id = i.id
        LEFT JOIN item_prices ip ON oi.item_id = ip.item_id
        WHERE o.status IN ('Delivered', 'Partially Delivered')
          AND COALESCE(oi.received_quantity, 0) > 0
        ORDER BY o.generation_timestamp ASC
    """
    
    cursor.execute(query)
    data = cursor.fetchall()
    
    # Update return dictionary to include quantity
    return [{'date': row[0], 'item_name': row[1], 'cost': row[2], 'qty': row[3]} for row in data]

@log_database_operation
def fetch_spend_by_category(conn):
    """
    Fetches total cumulative spend grouped by Category.
    Used for the 'Spend by Category' pie/donut chart.
    """
    if not conn: return []
    cursor = conn.cursor()
    
    query = """
        SELECT 
            i.category,
            COALESCE(SUM(COALESCE(oi.received_quantity, 0) * COALESCE(
                (SELECT ph.net_price
                 FROM price_history ph
                 WHERE ph.item_id = oi.item_id AND ph.source_order_id = o.order_id
                 ORDER BY ph.history_id DESC
                 LIMIT 1),
                ip.net_price,
                0
            )), 0.0) as total_cat_cost
        FROM orders o
        JOIN order_items oi ON o.order_id = oi.order_id
        LEFT JOIN items i ON oi.item_id = i.id
        LEFT JOIN item_prices ip ON oi.item_id = ip.item_id
        WHERE o.status IN ('Delivered', 'Partially Delivered')
        GROUP BY i.category
        ORDER BY total_cat_cost DESC
    """
    
    cursor.execute(query)
    data = cursor.fetchall()
    
    return [{'category': row[0] or "Uncategorized", 'cost': row[1]} for row in data]

@log_database_operation
def export_database_to_sql(conn):
    """
    Generates a complete SQL dump of the database (schema + data).
    Returns a string.
    """
    if not conn: return None
    
    # iterdump() is a specific SQLite helper that acts like 'mysqldump'
    try:
        dump_lines = conn.iterdump()
        dump_script = "\n".join(dump_lines)
        return dump_script
    except Exception as e:
        logging.error(f"Error exporting database: {e}")
        return None

@log_database_operation
def fully_restore_database_from_sql(conn, sql_script_string):
    """
    DANGEROUS: Wipes the current database and re-builds it from the script.
    """
    if not conn: return False
    cursor = conn.cursor()
    
    try:
        # 1. Safety: Turn off Foreign Keys to prevent locking errors during drop
        cursor.execute("PRAGMA foreign_keys = OFF")
        
        # 2. Wipe the Slate: Drop all tables
        # We fetch all table names first
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall() if row[0] != 'sqlite_sequence']
        
        for table in tables:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            
        # 3. Execute the Restore Script
        cursor.executescript(sql_script_string)
        
        # 4. Cleanup and Re-enable Safety
        cursor.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        
        logging.info("Database successfully restored from backup.")
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"CRITICAL ERROR restoring database: {e}")
        return False

# --- WASTE MANAGEMENT ---

@log_database_operation
def create_waste_log_table(conn):
    """
    Logs Stock Failures (Loss). Legacy rows may still carry the
    retired '2Good2Go' event_type; reports filter them out.
    """
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS waste_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER, -- nullable: SET NULL when item is deleted
            item_name TEXT, -- name snapshot, survives item deletion
            event_type TEXT NOT NULL, -- 'Stock Failure' ('2Good2Go' retired)
            quantity_entered REAL NOT NULL, -- Boxes
            unit_type TEXT NOT NULL, -- 'Pieces' or 'Boxes'
            calculated_value REAL, -- The £ value of this event
            logged_by TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE SET NULL ON UPDATE CASCADE
        )
    ''')
    conn.commit()
    logging.info("Ensured 'waste_log' table exists.")

@log_database_operation
def log_stock_failure(conn, item_id: int, boxes_qty: int, username: str):
    """
    Logs Stock Room Failure. Input is FULL BOXES. DEDUCTS Stock.
    item_prices.net_price is the BOX/CASE unit price from supplier
    invoices, so value lost = net_price * boxes (do NOT multiply by
    pack_size — that inflated losses 20x-60x).
    """
    if not conn: return False, "No Connection"
    cursor = conn.cursor()
    
    try:
        # Check Stock, Pack Size & Price
        cursor.execute("""
            SELECT i.available_quantity, i.name, COALESCE(ip.net_price, 0.0), i.pack_size
            FROM items i 
            LEFT JOIN item_prices ip ON i.id = ip.item_id 
            WHERE i.id = ?
        """, (item_id,))
        row = cursor.fetchone()
        if not row: return False, "Item not found"
        
        curr_stock, name, box_price, pack_size = row
        pack_size = pack_size if pack_size and pack_size > 0 else 1
        
        if curr_stock < boxes_qty:
            return False, f"Not enough stock! (Have {curr_stock}, trying to remove {boxes_qty})"
            
        # Update Stock
        new_qty = curr_stock - boxes_qty
        cursor.execute("UPDATE items SET available_quantity = ? WHERE id = ?", (new_qty, item_id))
        
        # Log Loss — net_price is the price PER BOX (supplier invoice unit),
        # so no pack_size multiplication.
        total_value = box_price * boxes_qty
        cursor.execute('''
            INSERT INTO waste_log (item_id, item_name, event_type, quantity_entered, unit_type, calculated_value, logged_by)
            VALUES (?, ?, 'Stock Failure', ?, 'Boxes', ?, ?)
        ''', (item_id, name, boxes_qty, total_value, username))
        
        conn.commit()
        return True, f"Removed {boxes_qty} boxes of {name}. Value Lost: £{total_value:.2f}"
        
    except sqlite3.Error as e:
        conn.rollback()
        return False, str(e)

@log_database_operation
def fetch_waste_log_report(conn):
    """
    Fetches the waste log with item details for reporting.
    Stock Failures only — legacy '2Good2Go' rows are ignored.
    """
    if not conn: return []
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                w.log_id,
                strftime('%Y-%m-%d %H:%M', w.timestamp) as time_str,
                w.timestamp,
                COALESCE(i.name, w.item_name, '(deleted item)') as item_name,
                i.category,
                w.event_type,        -- 'Stock Failure'
                w.quantity_entered,
                w.unit_type,
                w.calculated_value,
                w.logged_by
            FROM waste_log w
            LEFT JOIN items i ON w.item_id = i.id
            WHERE w.event_type = 'Stock Failure'
            ORDER BY w.timestamp DESC
        """)
        data = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in data]
    except sqlite3.Error as e:
        logging.error(f"Error fetching waste report: {e}")
        return []

@log_database_operation
def update_desired_quantity_only(_conn, item_id: int, new_desired_qty: int):
    """
    Updates ONLY the desired_quantity for a specific item.
    Used for bulk editing in the Admin panel.
    """
    if not _conn: return False
    cursor = _conn.cursor()
    try:
        cursor.execute("UPDATE items SET desired_quantity = ? WHERE id = ?", (int(new_desired_qty), item_id))
        _conn.commit()
        return True
    except sqlite3.Error as e:
        logging.error(f"Error updating desired quantity for item {item_id}: {e}")
        _conn.rollback()
        return False

@log_database_operation
def fetch_waste_log_entry(conn, log_id):
    """Fetches a single waste log entry by ID."""
    if not conn: return None
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.log_id, w.item_id, w.event_type, w.quantity_entered,
               COALESCE(i.name, w.item_name, '(deleted item)')
        FROM waste_log w
        LEFT JOIN items i ON w.item_id = i.id
        WHERE w.log_id = ?
    """, (log_id,))
    row = cursor.fetchone()
    if row:
        return {'log_id': row[0], 'item_id': row[1], 'event_type': row[2], 'quantity': row[3], 'item_name': row[4]}
    return None

@log_database_operation
def delete_waste_log_entry(conn, log_id):
    """
    Deletes a waste log entry.
    CRITICAL: If it was a 'Stock Failure' (Loss), it RE-ADDS the stock to inventory
    to reverse the deduction.
    """
    if not conn: return False, "No connection"
    cursor = conn.cursor()
    
    try:
        # 1. Get details first
        cursor.execute("SELECT item_id, event_type, quantity_entered FROM waste_log WHERE log_id = ?", (log_id,))
        row = cursor.fetchone()
        if not row:
            return False, "Entry not found"
            
        item_id, event_type, qty = row
        
        # 2. If it was a Stock Failure, we must REVERSE the stock deduction
        if event_type == 'Stock Failure':
            cursor.execute("UPDATE items SET available_quantity = available_quantity + ? WHERE id = ?", (qty, item_id))
            logging.info(f"Reversed stock deduction for item {item_id} (Added {qty} back).")
            
        # 3. Delete the log
        cursor.execute("DELETE FROM waste_log WHERE log_id = ?", (log_id,))
        conn.commit()
        return True, "Entry deleted successfully. Stock adjusted if applicable."
        
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error deleting waste log {log_id}: {e}")
        return False, str(e)

if __name__ == '__main__':
    logging.info("Attempting to initialize database schema directly...")
    db_conn = None
    try:
        db_conn = connect_to_db() 
        ensure_all_tables_exist(db_conn)
        logging.info("Database schema initialized/verified successfully.")
    except Exception as e_main:
        logging.error(f"Failed to initialize database schema: {e_main}")
    finally:
        if db_conn:
            db_conn.close()
            logging.info("Database connection closed.")