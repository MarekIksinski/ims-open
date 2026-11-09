#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ims_v3 staff app — inventory & stock checks, orders & deliveries,
waste & loss logging.
"""
import logging
import sqlite3
from datetime import datetime
import streamlit as st
import pandas as pd
import database_utils
import requests
import json
import re
import base64

# AI endpoint/model/timeouts live in the app_settings DB table
# (Admin app -> AI Settings page). Defaults are in database_utils.

# --- STREAMLIT CONFIG ---
st.set_page_config(page_title="SMT (Staff)", layout="wide") 

# --- DATABASE CONNECTION ---
conn = database_utils.connect_to_db() 
if conn:
    database_utils.ensure_all_tables_exist(conn)
    # NEW: Ensure we have at least one user so you don't get locked out
    database_utils.ensure_admin_user_exists(conn)
else:
    st.error("🔴 Major database connection issue. App cannot start.")
    st.stop()

# --- AUTHENTICATION & SESSION STATE ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'current_user' not in st.session_state:
    st.session_state.current_user = None

def login():
    """Simple Till-Style Login Screen"""
    st.markdown("## 🔒 Staff Access")
    st.write("Please enter your 4-digit PIN.")
    
    # Using a form so hitting 'Enter' submits the PIN
    with st.form("login_form"):
        pin_input = st.text_input("PIN", type="password", max_chars=4, placeholder="****")
        submitted = st.form_submit_button("Login")
        
        if submitted:
            user = database_utils.verify_staff_login(conn, pin_input)
            if user:
                st.session_state.logged_in = True
                st.session_state.current_user = user
                if user.get('legacy_login'):
                    # Audit #2: hash was legacy at login time (now upgraded) —
                    # nag loudly until logout.
                    st.session_state.pin_legacy_notice = True
                st.success(f"Welcome, {user['username']}!")
                st.rerun()
            else:
                st.error("❌ Incorrect PIN")

def logout():
    st.session_state.logged_in = False
    st.session_state.current_user = None
    st.session_state.pop('pin_legacy_notice', None)
    st.rerun()

# --- LOGIN GATE ---
if not st.session_state.logged_in:
    # Use columns to center the login box neatly
    _, col2, _ = st.columns([1, 1, 1])
    with col2:
        login()
    st.stop()  # <--- CRITICAL: Stops the rest of the app from loading if not logged in

# =========================================================
#  🏁 MAIN APP STARTS HERE (Only runs if logged_in == True)
# =========================================================

st.title(f"📦 Stuff Management Thingi (User: {st.session_state.current_user['username']})") 

if st.session_state.get('pin_legacy_notice'):
    st.warning("⚠️ Your PIN was stored in an insecure legacy format. It has "
               "just been upgraded to a secure hash — but treat the old PIN as "
               "compromised: if anyone else knows it, ask an admin to change "
               "it on the Users screen.", icon="🔐")

# --- SIDEBAR NAVIGATION (UPDATED) ---
with st.sidebar:
    st.write(f"👤 **{st.session_state.current_user['username']}**")
    if st.button("🔒 Logout", type="primary"):
        logout()
    st.divider()

# Initialize the choice in session state if it doesn't exist
if 'staff_choice' not in st.session_state:
    st.session_state.staff_choice = "📊 Stuff We Have"

st.sidebar.title("Staff Menu")

# Group 1: Inventory
with st.sidebar.expander("📦 Inventory & Stock", expanded=True):
    if st.button("📊 Stuff We Have", use_container_width=True):
        st.session_state.staff_choice = "📊 Stuff We Have"
        st.rerun()
        
    if st.button("🔄 Update Available Stuff", use_container_width=True):
        st.session_state.staff_choice = "🔄 Update Available Stuff"
        st.rerun()
        
    if st.button("🔍 Stock Check", use_container_width=True):
        st.session_state.staff_choice = "🔍 Stock Check"
        st.rerun()
        
    if st.button("📊 Resolve Skipped Items", use_container_width=True):
        st.session_state.staff_choice = "📊 Resolve Skipped Items"
        st.rerun()

# Group 2: Deliveries & Orders
with st.sidebar.expander("🚚 Orders & Deliveries", expanded=True):
    if st.button("📝 Stuff We Want", use_container_width=True):
        st.session_state.staff_choice = "📝 Stuff We Want"
        st.rerun()
        
    if st.button("🚚 Stuff We Got", use_container_width=True):
        st.session_state.staff_choice = "🚚 Stuff We Got"
        st.rerun()
        
    if st.button("📦 View Order History", use_container_width=True):
        st.session_state.staff_choice = "📦 View Order History"
        st.rerun()

    if st.button("🕘 Item Order History", use_container_width=True):
        st.session_state.staff_choice = "🕘 Item Order History"
        st.rerun()

# Group 3: Waste
with st.sidebar.expander("♻️ Waste & Loss", expanded=True):
    if st.button("📉 Log Stock Loss", use_container_width=True):
        st.session_state.staff_choice = "📉 Log Stock Loss"
        st.rerun()

# Set the variable 'choice' that the rest of the script uses
choice = st.session_state.staff_choice

def invalidate_order_form_state():
    """
    Drop stale 'Stuff We Want' widget state whenever stock changes
    (Stock Check, Update Available Stuff, Resolve Skipped, Log Stock
    Loss): st.number_input keeps session_state over value= defaults,
    so old order quantities would otherwise survive a stock change.
    """
    st.session_state.generated_orders_map = {}
    for k in [k for k in list(st.session_state.keys()) if isinstance(k, str) and k.startswith("ord_")]:
        st.session_state.pop(k, None)

# --- AI HELPERS ---
def image_to_base64(uploaded_file):
    try:
        return base64.b64encode(uploaded_file.getvalue()).decode('utf-8')
    except Exception as e:
        return None

def extract_json_from_response(text):
    code_block_match = re.search(r'```json\n(.*?)\n```', text, re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1)
    else:
        start = text.find('{')
        end = text.rfind('}') + 1
        if start != -1 and end != -1:
            text = text[start:end]
    return text

def _ollama_generate(prompt, images=None, temperature=0.0, tag="ollama"):
    """
    Shared Ollama /api/generate call. Returns (raw_text, error_msg).
    Never swallows failures into an empty result — the caller shows
    error_msg in the UI so a dead server is not mistaken for 'nothing found'.
    """
    ai = database_utils.get_ai_settings(conn)
    payload = {
        "model": ai["model"],
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": ai["num_ctx"]}
    }
    if images:
        payload["images"] = images
    try:
        response = requests.post(f"{ai['base_url']}/api/generate", json=payload, timeout=ai["timeout_s"])
        if response.status_code != 200:
            err = f"Ollama returned HTTP {response.status_code}: {response.text[:200]}"
            logging.error(f"{tag}: {err}")
            return "", err
        result = response.json()
        raw_text = result.get('response', '')
        if not raw_text and result.get('error'):
            err = str(result['error'])
            logging.error(f"{tag}: Ollama error: {err}")
            return "", err
        return raw_text, None
    except requests.exceptions.ConnectionError as e:
        err = f"Cannot reach Ollama at {ai['base_url']} — is it running?"
        logging.error(f"{tag}: {err} ({e})")
        return "", err
    except requests.exceptions.Timeout:
        err = f"Ollama timed out after {ai['timeout_s']}s."
        logging.error(f"{tag}: {err}")
        return "", err
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logging.error(f"{tag}: unexpected error: {err}")
        return "", err

def _parse_json_response(raw_text, tag):
    """Extract + json.loads the model output. Returns (data, error_msg)."""
    try:
        return json.loads(extract_json_from_response(raw_text)), None
    except (json.JSONDecodeError, ValueError) as e:
        err = f"Could not parse AI response as JSON: {e}"
        logging.error(f"{tag}: {err}")
        return None, err

# ============ 3-PASS INVOICE PIPELINE (accuracy over speed) ============
# Pass 1 reads the invoice with NO database context at all (no item lists,
# no aliases) so the vision model cannot hallucinate DB aliases over the
# real invoice text. Pass 2 matches the extracted text to DB ids without
# the image. Pass 3 independently re-checks the matches for swaps.

def ocr_invoice_page(base64_img, extra_note="", temperature=0.0):
    """
    PASS 1 — pure vision OCR. NO database items or aliases in this prompt.
    Returns (lines, error_msg) where each line is:
      {'invoice_desc', 'qty', 'unit_price', 'goods_value', 'vat_rate'}
    """
    prompt = f"""
    Read the invoice/delivery-note table in this image row by row.
    Table columns are typically: No. / Code, Product, Qty, Unit Price,
    Goods Value, Vat Amt., VAT %, Total.

    For EACH row return:
    - "invoice_desc": the EXACT text from the Product/Description column
      of the invoice. Transcribe only what is printed on the document.
    - "qty": the integer from the 'Qty' column. NEVER use pack sizes that
      appear inside the description like 2x5kg or 24x110g.
    - "unit_price": the float from the 'Unit Price' column — the price of
      1 unit/case BEFORE VAT (NOT 'Goods Value' or 'Total').
    - "goods_value": the line total BEFORE VAT (the 'Goods Value' / Net
      column).
    - "vat_rate": the numeric percentage from the 'VAT' column
      (e.g. "0%" -> 0.0, "20%" -> 20.0).
    {extra_note}
    RETURN JSON ONLY:
    {{
        "lines": [
            {{"invoice_desc": "EXACT INVOICE TEXT", "qty": 10, "unit_price": 20.40, "goods_value": 204.00, "vat_rate": 0.0}}
        ]
    }}
    """
    raw_text, err = _ollama_generate(prompt, images=[base64_img], temperature=temperature, tag="ocr_invoice_page")
    if err:
        return [], err
    data, err = _parse_json_response(raw_text, "ocr_invoice_page")
    if err:
        return [], err
    lines = data.get("lines", []) if isinstance(data, dict) else []
    if not lines:
        return [], "AI returned no invoice lines (empty result)."
    return lines, None

def math_check_problems(lines):
    """
    Python-side sanity check on Pass 1 rows. Returns a list of
    human-readable problems (empty list = all rows consistent).
    """
    problems = []
    for ln in lines:
        try:
            qty = float(ln.get('qty', 0) or 0)
            unit_price = float(ln.get('unit_price', 0) or 0)
            goods_value = float(ln.get('goods_value', 0) or 0)
        except (TypeError, ValueError):
            problems.append(f"'{ln.get('invoice_desc', '?')}': non-numeric numbers")
            continue
        if qty > 0 and goods_value > 0 and abs((qty * unit_price) - goods_value) > 0.05:
            problems.append(
                f"'{ln.get('invoice_desc', '?')}': qty {ln.get('qty')} x unit_price "
                f"{ln.get('unit_price')} = {qty * unit_price:.2f}, but goods_value says {goods_value}"
            )
    return problems

def apply_math_fixes(lines):
    """
    If unit_price is 0 but goods_value > 0 (qty > 0), derive
    unit_price = goods_value / qty. Mutates and returns the lines.
    """
    for ln in lines:
        try:
            qty = float(ln.get('qty', 0) or 0)
            unit_price = float(ln.get('unit_price', 0) or 0)
            goods_value = float(ln.get('goods_value', 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and unit_price == 0 and goods_value > 0:
            ln['unit_price'] = round(goods_value / qty, 2)
    return lines

def match_invoice_lines(ocr_lines, ordered_items, supplier_catalog):
    """
    PASS 2 — text-only semantic matching (no image). Maps each
    line_index to a DB id (or None). Returns (matches, error_msg)
    with matches = [{'line_index': int, 'id': int|None}].
    """
    def fmt(i):
        line = f"ID: {i['id']} | Name: {i['name']}"
        if i.get('aliases'):
            line += f" | Past Brand/Pack Examples: {i['aliases']}"
        return line

    expected_str = "\n".join(fmt(i) for i in ordered_items)
    catalog_str = "\n".join(fmt(i) for i in supplier_catalog)
    ocr_str = "\n".join(
        f"Line {idx}: \"{ln.get('invoice_desc', '')}\" (qty {ln.get('qty', 0)}, unit price {ln.get('unit_price', 0)})"
        for idx, ln in enumerate(ocr_lines)
    )

    prompt = f"""
    You are matching invoice lines to database items.

    EXPECTED ON THIS ORDER:
    ---
    {expected_str}
    ---

    OTHER SUPPLIER CATALOG ITEMS:
    ---
    {catalog_str}
    ---

    INVOICE LINES (read from the document):
    ---
    {ocr_str}
    ---

    TASK: map each Line <line_index> to the id of the database item it
    actually is (or null if no item matches).

    IMPORTANT — 'Past Brand/Pack Examples' are ONLY examples of brands
    and pack sizes bought previously for that generic item. ANY brand or
    pack size of the same product type matches, e.g.:
    - "2x5kg Q Toreno Beef Dripping" matches the item whose example is
      "12.5kg Ct.Olenex Beef Dripping" (both are beef dripping / fat).
    - "Golden Batter Flour" matches the item "batter mix".
    Judge by product TYPE, not exact brand/size wording.

    RETURN JSON ONLY:
    {{
        "matches": [
            {{"line_index": 0, "id": 101}},
            {{"line_index": 1, "id": null}}
        ]
    }}
    """
    raw_text, err = _ollama_generate(prompt, temperature=0.0, tag="match_invoice_lines")
    if err:
        return [], err
    data, err = _parse_json_response(raw_text, "match_invoice_lines")
    if err:
        return [], err
    matches = data.get("matches", []) if isinstance(data, dict) else []
    return matches, None

def verify_matches(ocr_lines, proposed_matches, ordered_items, supplier_catalog):
    """
    PASS 3 — independent verification of Pass 2 (no image). Looks for
    swapped items / contradictions and re-states each mapping with a
    confidence. Returns (verified, error_msg) with verified =
    [{'line_index', 'id', 'confidence'}].
    """
    def fmt(i):
        line = f"ID: {i['id']} | Name: {i['name']}"
        if i.get('aliases'):
            line += f" | Past Brand/Pack Examples: {i['aliases']}"
        return line

    all_items = list(ordered_items) + list(supplier_catalog)
    items_str = "\n".join(fmt(i) for i in all_items)
    name_by_id = {i['id']: i['name'] for i in all_items}

    paired = []
    for m in proposed_matches:
        idx = m.get('line_index')
        desc = ''
        if isinstance(idx, int) and 0 <= idx < len(ocr_lines):
            desc = ocr_lines[idx].get('invoice_desc', '')
        mid = m.get('id')
        if mid is None:
            paired.append(f"Line {idx}: Invoice \"{desc}\" -> Matched to NO database item (null)")
        else:
            paired.append(f"Line {idx}: Invoice \"{desc}\" -> Matched to DB ID {mid} (\"{name_by_id.get(mid, 'unknown item')}\")")
    paired_str = "\n".join(paired)

    prompt = f"""
    Review these proposed invoice-to-database matches.

    AVAILABLE DATABASE ITEMS:
    ---
    {items_str}
    ---

    PROPOSED MATCHES:
    ---
    {paired_str}
    ---

    Check carefully for swapped items or contradictions (for example,
    'Beef Dripping' is fat, never batter mix; 'Batter Flour' is batter
    mix, never fat). If a match is wrong, correct the id (or set it to
    null if it does not match any database item). If no database item
    fits, null is correct.

    RETURN JSON ONLY:
    {{"verified": [{{"line_index": 0, "id": 101, "confidence": "high"}}]}}
    where confidence is "high" or "low".
    """
    raw_text, err = _ollama_generate(prompt, temperature=0.0, tag="verify_matches")
    if err:
        return [], err
    data, err = _parse_json_response(raw_text, "verify_matches")
    if err:
        return [], err
    verified = data.get("verified", []) if isinstance(data, dict) else []
    return verified, None

def build_discrepancy_notes(line_items, ai_results):
    """One-line summary per ordered item whose scanned qty differs from expected remaining."""
    lines = []
    for i in line_items:
        exp = max(0, i['ordered_quantity'] - (i['received_quantity'] or 0))
        got = ai_results.get(i['item_id'], {}).get('qty', 0)
        if got == 0 and exp > 0:
            lines.append(f"Missing: {i['item_name']} (Exp {exp}, Got 0)")
        elif got != exp:
            kind = "Short" if got < exp else "Over"
            lines.append(f"{kind}: {i['item_name']} (Exp {exp}, Got {got})")
    if not lines:
        return ""
    return "Delivery discrepancies:\n" + "\n".join(lines)

# --- HELPER ---
@st.cache_data
def get_item_id_name_map(_conn):
    """Fetches all items for selectbox display."""
    if not _conn: return [], {}
    items_list = database_utils.fetch_all_items_with_ids(_conn)
    if not items_list: return [], {}
    
    options_map = {f"{item['name']} (ID: {item['id']})": item['id'] for item in items_list}
    return list(options_map.keys()), options_map

# --- MAIN LOGIC ---

if choice == "📊 Stuff We Have": 
    st.header("📊 Current Inventory")
    
    # Use the new optimized query that includes allergens
    inventory_data = database_utils.fetch_inventory_with_allergens(conn)
    
    if inventory_data:
        df = pd.DataFrame(inventory_data)
        
        # Replace None with empty string for cleaner display
        df['contains_allergens'] = df['contains_allergens'].fillna('')
        df['may_contain_allergens'] = df['may_contain_allergens'].fillna('')
        df['allergens_last_reviewed'] = df['allergens_last_reviewed'].fillna('Never')

        cols = ['name', 'available_quantity', 'contains_allergens', 'may_contain_allergens', 'allergens_last_reviewed', 'category', 'expiry_date']
        # Filter existing columns
        existing_cols = [c for c in cols if c in df.columns]
        
        st.dataframe(
            df[existing_cols], 
            use_container_width=True, 
            hide_index=True,
            column_config={
                "contains_allergens": st.column_config.TextColumn(
                    "⚠️ Contains", 
                    width="medium",
                    help="Allergens explicitly listed in ingredients"
                ),
                "may_contain_allergens": st.column_config.TextColumn(
                    "❓ May Contain", 
                    width="medium",
                    help="Precautionary/Factory traces"
                ),
                "allergens_last_reviewed": st.column_config.TextColumn(
                    "🕒 Allergens Reviewed",
                    width="small",
                    help="When this product's allergen label was last checked"
                ),
                "available_quantity": st.column_config.NumberColumn("Qty", format="%d")
            }
        )
    else:
        st.info("Inventory is empty.")

    # Quick view for orders (kept from previous version)
    tables = database_utils.fetch_tables(conn)
    other = [t for t in tables if t in ['orders', 'order_items']]
    if other:
        st.divider()
        sel = st.selectbox("Quick view other tables:", other, index=None)
        if sel:
            c, d = database_utils.fetch_table_data(conn, sel)
            if c and d:
                st.dataframe(pd.DataFrame(d, columns=c), use_container_width=True, hide_index=True)

elif choice == "🔄 Update Available Stuff": 
    st.header("🔄 Update Available Quantity")

    if 'ua_cat' not in st.session_state: st.session_state.ua_cat = None
    if 'ua_item' not in st.session_state: st.session_state.ua_item = None

    # 1. Category
    cats = database_utils.fetch_distinct_categories(conn)
    if not cats:
        st.warning("No categories found.")
    else:
        cols = st.columns(4)
        for idx, c in enumerate(cats):
            if cols[idx % 4].button(c, key=f"btn_{c}", use_container_width=True):
                st.session_state.ua_cat = c
                st.session_state.ua_item = None
                st.rerun()

        if st.session_state.ua_cat:
            st.caption(f"Selected: **{st.session_state.ua_cat}**")
            if st.button("Clear Selection"):
                st.session_state.ua_cat = None
                st.rerun()

            # 2. Item
            items = database_utils.fetch_items_with_ids_by_category(conn, st.session_state.ua_cat)
            opts = {f"{i['name']} (ID: {i['id']})": i['id'] for i in items}
            
            sel_display = st.selectbox("Select Item:", list(opts.keys()), index=None)
            
            if sel_display:
                iid = opts[sel_display]
                data = database_utils.search_item_by_id(conn, iid)
                if data:
                    st.write(f"Current Qty: **{data['available_quantity']}**")
                    new_qty = st.number_input("New Qty:", min_value=0, step=1, key=f"qty_{iid}")
                    
                    if st.button("Update", type="primary"):
                        if database_utils.update_available_quantity(conn, iid, int(new_qty)):
                            invalidate_order_form_state()
                            st.success("Updated!")
                            st.rerun()

elif choice == "📉 Log Stock Loss":
    st.header("❄️ Log Stock Room Failure (Loss)")
    
    st.info("Log FULL BOXES lost in the stock room (e.g. freezer breakdown). This DEDUCTS stock and tracks the financial loss.")

    # 1. Select Item
    cats = database_utils.fetch_distinct_categories(conn)
    sel_cat = st.selectbox("Category", cats, index=None)
    
    item_map = {}
    if sel_cat:
        items = database_utils.fetch_items_with_ids_by_category(conn, sel_cat)
        # Fetch Pack Size for display so staff know what a "box" is
        for i in items:
            details = database_utils.search_item_by_id(conn, i['id'])
            if not details:
                continue  # item was deleted between fetch and lookup
            # Create a label like: "Croissants (Stock: 5)"
            label = f"{i['name']} (Stock: {details['available_quantity']})"
            item_map[label] = details
    
    st.divider()

    # 2. The Form
    with st.form("waste_entry_form"):
        sel_label = st.selectbox("Select Item", list(item_map.keys()), disabled=not item_map)
        
        st.markdown("### ❄️ Stock Failure Entry")
        qty_input = st.number_input("How many FULL BOXES lost?", min_value=1, step=1, help="Full units to remove from stock")
        confirm_label = "🚨 Deduct Stock & Log Loss"

        if st.form_submit_button(confirm_label):
            if not sel_label:
                st.error("Please select an item first.")
            else:
                item_data = item_map[sel_label]
                username = st.session_state.current_user['username']
                
                # Log Boxes (Deduct Stock)
                ok, msg = database_utils.log_stock_failure(conn, item_data['id'], int(qty_input), username)
                if ok:
                    invalidate_order_form_state()
                    st.warning(msg) # Warning color for loss
                    st.rerun() # Rerun to update the stock count in the dropdown
                else:
                    st.error(msg)

elif choice == "📝 Stuff We Want": 
    st.header("📝 Generate Orders")

    if 'generated_orders_map' not in st.session_state:
        st.session_state.generated_orders_map = {} 

    # Safety Button: If logic gets stuck, this clears the memory manually
    if st.session_state.generated_orders_map:
        if st.button("🔄 Reset All Order Forms (Show All Pending Items)"):
            st.session_state.generated_orders_map = {}
            st.rerun()

    items_to_order = database_utils.get_items_to_order(conn)

    if not items_to_order:
        st.success("✅ No items need ordering.")
    else:
        by_supplier = {}
        for i in items_to_order:
            sup = i['main_supplier'] or "Unknown"
            by_supplier.setdefault(sup, []).append(i)

        for sup, items in by_supplier.items():
            safe_sup = "".join(c if c.isalnum() else "_" for c in sup)
            clean_sup_key = sup.strip() # Ensure clean key matching
            
            # CHECK: Is this supplier in the session memory?
            if clean_sup_key in st.session_state.generated_orders_map:
                order_info = st.session_state.generated_orders_map[clean_sup_key]
                
                with st.expander(f"Supplier: {sup} ✅ (Order Generated)", expanded=False):
                    st.success(f"Order #{order_info['oid']} has been generated for {sup}.")
                    
                    st.download_button(
                        label="Download Order Text",
                        data=order_info['text'],
                        file_name=f"Order_{order_info['oid']}_{safe_sup}.txt",
                        mime="text/plain"
                    )
                    
                    if st.button(f"Start New Order for {sup}", key=f"reset_{safe_sup}"):
                        del st.session_state.generated_orders_map[clean_sup_key]
                        st.rerun()

            else:
                with st.expander(f"Supplier: {sup}", expanded=True):
                    with st.form(f"form_{safe_sup}"):
                        final_data = []
                        for i in items:
                            pending = i.get('pending_order_quantity', 0)
                            if pending > 0:
                                st.warning(f"⚠️ Note: {pending} already on an open order")
                            st.write(f"**{i['name']}** (Current: {i['available_quantity']}, need: {i['deficit_quantity']})")
                            qty = st.number_input(f"Order Qty for {i['name']}", value=i['order_quantity'], min_value=0, key=f"ord_{i['id']}")
                            final_data.append({'id': i['id'], 'qty': qty, 'name': i['name']})
                            st.write("---")
                        
                        if st.form_submit_button("Generate Order"):
                            items_for_db = [x for x in final_data if x['qty'] > 0]
                            if items_for_db:
                                try:
                                    oid = database_utils.insert_order(conn, sup)
                                    for x in items_for_db:
                                        database_utils.insert_order_item(conn, oid, x['id'], x['qty'])
                                    conn.commit()
                                    
                                    # TEXT GENERATION
                                    curr_date = datetime.now().strftime('%Y-%m-%d')
                                    txt = f"Supplier: {sup}\nDate: {curr_date}\n\n"
                                    for x in items_for_db:
                                        txt += f"{x['name']}: {x['qty']}\n"
                                    
                                    # Save to session using clean key
                                    st.session_state.generated_orders_map[clean_sup_key] = {'text': txt, 'oid': oid}
                                    
                                    st.success(f"Order {oid} generated!")
                                    st.rerun() 
                                except Exception as e:
                                    conn.rollback()
                                    st.error(f"Error: {e}")
                            else:
                                st.warning("No items selected (all quantities were 0).")

elif choice == "🚚 Stuff We Got":
    st.header("🚚 Receive Goods (+ Update Prices)")
    
    # Session state for AI results (stores dict: {id: {'qty': 0, 'price': 0.0, 'vat': 0.0, 'invoice_desc': ''}})
    if 'delivery_ai_results' not in st.session_state: st.session_state.delivery_ai_results = {}
    if 'delivery_unexpected_items' not in st.session_state: st.session_state.delivery_unexpected_items = []
    # Which order the last scan belonged to (badges/auto-notes only apply to that order)
    if 'delivery_scan_oid' not in st.session_state: st.session_state.delivery_scan_oid = None
    if 'delivery_notes_auto' not in st.session_state: st.session_state.delivery_notes_auto = ''
    if 'delivery_scan_msg' not in st.session_state: st.session_state.delivery_scan_msg = ''

    open_orders = database_utils.fetch_open_orders(conn)

    if not open_orders:
        st.info("No open orders.")
    else:
        opts = {f"ID: {o['order_id']} - {o['supplier_name']} ({o['status']})": o['order_id'] for o in open_orders}
        sel_key = st.selectbox("Select Order:", list(opts.keys()), index=None)

        if sel_key:
            oid = opts[sel_key]
            
            # 1. Fetch Order Data
            order_info = next(o for o in open_orders if o['order_id'] == oid)
            supplier_name = order_info['supplier_name']
            
            # 2. Fetch Line Items
            line_items = database_utils.fetch_order_items_for_delivery(conn, oid)
            # Defined OUTSIDE the scan block: needed both during "Scan Invoice"
            # and later at "Confirm Delivery" (scan ends with st.rerun(), so a
            # definition inside the button block would be gone on submit).
            order_id_set = {i['item_id'] for i in line_items}

            # --- AI SCANNER ---
            with st.expander("📸 AI Auto-Fill (Qty & Prices)", expanded=True):
                st.info(f"Upload invoice from **{supplier_name}**. AI will match items, count quantities, and extract prices.")
                up_files = st.file_uploader("Invoice Photos", accept_multiple_files=True, type=['jpg', 'png', 'jpeg'])
                
                if st.button("🚀 Scan Invoice") and up_files:
                    catalog_data = database_utils.fetch_supplier_items_with_aliases(conn, supplier_name)

                    # Section 1: items on THIS order (ids from line_items + their aliases)
                    ordered_ids = {c['id']: c for c in catalog_data}
                    ordered_section = []
                    for li in line_items:
                        cat = ordered_ids.get(li['item_id'])
                        ordered_section.append({
                            'id': li['item_id'],
                            'name': li['item_name'],
                            'ordered_quantity': li['ordered_quantity'],
                            'aliases': cat['aliases'] if cat else ''
                        })
                    # Section 2: supplier catalog items NOT on this order
                    catalog_section = [c for c in catalog_data if c['id'] not in order_id_set]

                    st.session_state.delivery_ai_results = {} # Reset
                    st.session_state.delivery_unexpected_items = []  # Reset

                    # Clear stale unexpected-item widget state from a previous scan
                    for k in [k for k in list(st.session_state.keys())
                              if isinstance(k, str) and k.startswith((f"inc_{oid}_", f"umap_{oid}_",
                                                                      f"uq_{oid}_", f"up_{oid}_", f"uv_{oid}_"))]:
                        st.session_state.pop(k, None)

                    progress = st.progress(0)
                    scan_errors = []
                    for idx, file in enumerate(up_files):
                        b64 = image_to_base64(file)

                        # --- Pass 1: pure vision OCR (no DB context) ---
                        ocr_lines, err_msg = ocr_invoice_page(b64)
                        if not err_msg:
                            problems = math_check_problems(ocr_lines)
                            if problems:
                                # Derive missing unit prices from goods_value/qty
                                apply_math_fixes(ocr_lines)
                                remaining = math_check_problems(ocr_lines)
                                if remaining:
                                    # One retry re-reading the numbers carefully
                                    note = ("RE-READ the numbers carefully: for these rows "
                                            "qty * unit_price did NOT equal goods_value:\n"
                                            + "\n".join(f"- {p}" for p in remaining)
                                            + "\nReturn ALL lines again, corrected.")
                                    retry_lines, retry_err = ocr_invoice_page(b64, extra_note=note, temperature=0.2)
                                    if not retry_err and retry_lines:
                                        apply_math_fixes(retry_lines)
                                        ocr_lines = retry_lines
                                    # If the retry also failed, keep pass-1 numbers.

                        if err_msg:
                            scan_errors.append(f"Page {idx + 1} (OCR): {err_msg}")
                            progress.progress((idx + 1) / len(up_files))
                            continue

                        # --- Pass 2: text-only semantic matching ---
                        matches, err_msg = match_invoice_lines(ocr_lines, ordered_section, catalog_section)
                        if err_msg:
                            scan_errors.append(f"Page {idx + 1} (Match): {err_msg}")
                            progress.progress((idx + 1) / len(up_files))
                            continue

                        # --- Pass 3: independent verification / swap check ---
                        verified, v_err = verify_matches(ocr_lines, matches, ordered_section, catalog_section)
                        needs_review_map = {}
                        if v_err:
                            scan_errors.append(f"Page {idx + 1} (Verify): {v_err}")
                            # Matching result stands but is unverified — flag it.
                            for m in matches:
                                needs_review_map[m.get('line_index')] = True
                        else:
                            ver_by_idx = {v.get('line_index'): v for v in verified if isinstance(v, dict)}
                            for m in matches:
                                li_idx = m.get('line_index')
                                v = ver_by_idx.get(li_idx)
                                if v is None:
                                    needs_review_map[li_idx] = True
                                elif v.get('id') != m.get('id') or str(v.get('confidence', '')).lower() == 'low':
                                    needs_review_map[li_idx] = True
                            # Pass 3 is the final authority on ids
                            merged = []
                            for m in matches:
                                li_idx = m.get('line_index')
                                if li_idx in ver_by_idx:
                                    merged.append({'line_index': li_idx,
                                                   'id': ver_by_idx[li_idx].get('id', m.get('id'))})
                                else:
                                    merged.append(m)
                            matches = merged

                        # Accumulate Qty, update Price/VAT; split matched vs unexpected
                        for m in matches:
                            li_idx = m.get('line_index')
                            if not (isinstance(li_idx, int) and 0 <= li_idx < len(ocr_lines)):
                                continue
                            ln = ocr_lines[li_idx]
                            iid = m.get('id')
                            qty = ln.get('qty', 0) or 0
                            price = ln.get('unit_price', 0.0) or 0.0
                            vat = ln.get('vat_rate', 0.0) or 0.0
                            inv_desc = ln.get('invoice_desc', '') or ''
                            needs_review = bool(needs_review_map.get(li_idx))

                            if iid and iid in order_id_set:
                                if iid not in st.session_state.delivery_ai_results:
                                    st.session_state.delivery_ai_results[iid] = {'qty': 0, 'price': 0.0, 'vat': 0.0, 'invoice_desc': '', 'needs_review': False}

                                # Add Qty (multi-page accumulation)
                                st.session_state.delivery_ai_results[iid]['qty'] += qty

                                # Overwrite Price/VAT/desc if found (latest page wins)
                                if price > 0: st.session_state.delivery_ai_results[iid]['price'] = price
                                st.session_state.delivery_ai_results[iid]['vat'] = vat
                                if inv_desc: st.session_state.delivery_ai_results[iid]['invoice_desc'] = inv_desc
                                if needs_review: st.session_state.delivery_ai_results[iid]['needs_review'] = True
                            else:
                                # id is null OR matched a catalog item not on this order
                                st.session_state.delivery_unexpected_items.append({
                                    'id': iid if iid else None,
                                    'invoice_desc': inv_desc,
                                    'qty': qty, 'price': price, 'vat': vat,
                                    'needs_review': needs_review
                                })

                        progress.progress((idx + 1) / len(up_files))

                    # Explicitly set widget state so the form renders fresh with AI values
                    # (popping keys alone does not update already-initialized widgets).
                    for li in line_items:
                        oi_id = li['order_item_id']
                        ai_d = st.session_state.delivery_ai_results.get(li['item_id'], {})
                        st.session_state[f"q_{oi_id}"] = int(ai_d.get('qty', 0) or 0)
                        st.session_state[f"p_{oi_id}"] = float(ai_d.get('price', 0.0) or 0.0)
                        st.session_state[f"v_{oi_id}"] = float(ai_d.get('vat', 0.0) or 0.0)

                    if scan_errors:
                        st.session_state.delivery_scan_msg = "AI Scan Error:\n" + "\n".join(scan_errors)
                        # Do NOT set delivery_scan_oid: no badges / auto-notes when the scan failed,
                        # so items aren't falsely badged "Not found on invoice".
                        st.session_state.delivery_scan_oid = None
                        st.session_state.delivery_notes_auto = ''
                    else:
                        matched = len(st.session_state.delivery_ai_results)
                        extra = len(st.session_state.delivery_unexpected_items)
                        review = sum(1 for v in st.session_state.delivery_ai_results.values() if v.get('needs_review'))
                        summary = f"Scanned {len(up_files)} pages — {matched} matched, {extra} unexpected."
                        if review:
                            summary += f" {review} flagged 🔍 Verify match."
                        summary += " Review below."
                        extras_txt = ""
                        if extra:
                            extras_txt = "\n" + "\n".join(
                                f"Not on this order: “{u['invoice_desc'] or ('ID ' + str(u['id']))}” — qty {u['qty']}, £{u['price']:.2f} (not applied)"
                                for u in st.session_state.delivery_unexpected_items
                            )
                        st.session_state.delivery_scan_msg = summary + extras_txt

                        # Discrepancy auto-notes for this order's scan
                        st.session_state.delivery_scan_oid = oid
                        st.session_state.delivery_notes_auto = build_discrepancy_notes(line_items, st.session_state.delivery_ai_results)

                    # Explicitly (re)set the notes widget value for the fresh render
                    st.session_state[f"notes_{oid}"] = st.session_state.delivery_notes_auto

                    st.rerun()
            
            # --- CONFIRMATION FORM ---
            st.divider()
            if st.session_state.delivery_scan_msg:
                if st.session_state.delivery_scan_msg.startswith("AI Scan Error"):
                    st.error(st.session_state.delivery_scan_msg)
                else:
                    st.success(st.session_state.delivery_scan_msg)
                st.session_state.delivery_scan_msg = ''  # show once
            st.subheader("Confirm Delivery & Prices")
            
            with st.form(f"recv_{oid}"):
                updates = [] 
                
                # Header
                h1, h2, h3, h4 = st.columns([2.5, 1, 1, 0.8])
                h1.write("**Item Details**")
                h2.write("**Recv Qty**")
                h3.write("**Net Price (£)**")
                h4.write("**VAT (%)**")

                for i in line_items:
                    # Logic
                    iid = i['item_id']
                    db_rem = max(0, i['ordered_quantity'] - (i['received_quantity'] or 0))
                    
                    # AI Data
                    ai_data = st.session_state.delivery_ai_results.get(iid, {})
                    ai_qty = ai_data.get('qty', 0)
                    ai_price = ai_data.get('price', 0.0)
                    ai_vat = ai_data.get('vat', 0.0)
                    ai_desc = ai_data.get('invoice_desc', '')
                    ai_review = bool(ai_data.get('needs_review'))
                    scan_ran = st.session_state.delivery_scan_oid == oid
                    
                    # Defaults: Use AI if available, else DB defaults
                    def_qty = ai_qty if ai_qty > 0 else 0
                    
                    c1, c2, c3, c4 = st.columns([2.5, 1, 1, 0.8])
                    
                    # Label + discrepancy badge (only after a scan of THIS order)
                    label = f"{i['item_name']} (Ord: {i['ordered_quantity']}, Rec: {i['received_quantity']})"
                    badge = None
                    if scan_ran:
                        if ai_qty == 0:
                            badge = "❌ Not found on invoice"
                        elif ai_qty == db_rem:
                            badge = "✅ Match"
                        elif ai_qty < db_rem:
                            badge = f"⚠️ Short (-{db_rem - ai_qty})"
                        else:
                            badge = f"⚠️ Over (+{ai_qty - db_rem})"
                        # Pass 2/3 disagreement or low confidence
                        if ai_review:
                            badge = f"{badge} · 🔍 Verify match"
                    if badge:
                        c1.markdown(f"{label}  \n**{badge}**")
                        if ai_desc:
                            c1.caption(f"Invoice: {ai_desc}")
                    else:
                        c1.text(label)
                    
                    # Inputs (values come from session_state, set at scan or initialised here)
                    q_key, p_key, v_key = f"q_{i['order_item_id']}", f"p_{i['order_item_id']}", f"v_{i['order_item_id']}"
                    if q_key not in st.session_state: st.session_state[q_key] = def_qty
                    if p_key not in st.session_state: st.session_state[p_key] = float(ai_price)
                    if v_key not in st.session_state: st.session_state[v_key] = float(ai_vat)
                    val_q = c2.number_input("Qty", min_value=0, key=q_key, label_visibility="collapsed")
                    val_p = c3.number_input("£", min_value=0.0, step=0.01, key=p_key, label_visibility="collapsed")
                    val_v = c4.number_input("%", min_value=0.0, step=0.5, key=v_key, label_visibility="collapsed")
                    
                    updates.append((i, val_q, val_p, val_v))

                # --- Unexpected / substituted items on the invoice ---
                # Staff may map them to an order item or any inventory item.
                # Alias saving is Admin-only: no add_item_alias here, ever.
                unexpected_rows = []
                unexpected = st.session_state.delivery_unexpected_items
                if unexpected and st.session_state.delivery_scan_oid == oid:
                    st.divider()
                    st.markdown("#### ⚠️ Unexpected / Substituted Items on Invoice")
                    st.caption("Tick **Include?** and map to the right item. Mapping applies to this delivery only — no aliases are saved.")
                    inv_opts, inv_map = get_item_id_name_map(conn)
                    for uidx, u in enumerate(unexpected):
                        desc = u['invoice_desc'] or f"(no description, ID {u['id']})"
                        matched_hint = ""
                        preselect_idx = None
                        if u.get('id'):
                            for oi_pos, lbl in enumerate(inv_opts):
                                if inv_map[lbl] == u['id']:
                                    preselect_idx = oi_pos
                                    matched_hint = f" — AI matched: {lbl}"
                                    break
                        uc1, uc2, uc3, uc4, uc5 = st.columns([0.8, 3.2, 1, 1, 0.8])
                        include = uc1.checkbox("Include?", key=f"inc_{oid}_{uidx}", value=False)
                        review_hint = "  🔍 Verify match" if u.get('needs_review') else ""
                        uc2.markdown(f"**“{desc}”**{matched_hint}{review_hint}")
                        map_label = uc2.selectbox(
                            "Map to item", inv_opts,
                            index=preselect_idx,
                            key=f"umap_{oid}_{uidx}",
                            placeholder="Pick the inventory item...",
                            label_visibility="collapsed"
                        )
                        uq = uc3.number_input("Qty", min_value=0, value=int(u['qty']), key=f"uq_{oid}_{uidx}", label_visibility="collapsed")
                        up = uc4.number_input("£", min_value=0.0, value=float(u['price']), step=0.01, key=f"up_{oid}_{uidx}", label_visibility="collapsed")
                        uv = uc5.number_input("%", min_value=0.0, value=float(u['vat']), step=0.5, key=f"uv_{oid}_{uidx}", label_visibility="collapsed")
                        unexpected_rows.append((desc, include, map_label, uq, up, uv))

                auto_notes = st.session_state.delivery_notes_auto if st.session_state.delivery_scan_oid == oid else ''
                notes_key = f"notes_{oid}"
                if auto_notes and notes_key not in st.session_state:
                    st.session_state[notes_key] = auto_notes
                notes = st.text_area("Delivery Notes", key=notes_key)

                if st.form_submit_button("Confirm Delivery & Update Prices", type="primary"):
                    try:
                        qty_updates = 0
                        price_updates = 0
                        
                        for i, u_qty, u_price, u_vat in updates:
                            # 1. Update Stock Logic
                            if u_qty > 0:
                                qty_updates += 1
                                stock_row = database_utils.search_item_by_id(conn, i['item_id'])
                                if not stock_row:
                                    st.warning(f"Item '{i.get('item_name', i['item_id'])}' no longer exists — stock update skipped.")
                                    continue
                                curr_stock = stock_row['available_quantity']
                                database_utils.update_available_quantity(conn, i['item_id'], curr_stock + u_qty)
                                new_rec = (i['received_quantity'] or 0) + u_qty
                                database_utils.update_order_item_received_quantity(conn, i['order_item_id'], new_rec)
                            
                            # 2. Price Logic (Update Current AND Log History)
                            if u_price > 0:
                                # A. Update the "Current Price" for Valuation
                                database_utils.update_item_price(conn, i['item_id'], u_price, u_vat)
                                
                                # B. Log to History (New!)
                                # We pass 'oid' so we know exactly which invoice gave us this price
                                database_utils.log_price_history(conn, i['item_id'], u_price, u_vat, order_id=oid)
                                price_updates += 1

                        # 3. Unexpected / substituted items included by staff.
                        #    NOTE: no alias saving here — aliases are Admin-only.
                        sub_notes = []
                        # Effective received qty per ordered item: main form values
                        # plus any unexpected items mapped onto that same item.
                        eff_qty = {i['item_id']: q for i, q, _, _ in updates}
                        for desc, include, map_label, uq, up, uv in unexpected_rows:
                            if not include or not map_label or uq <= 0:
                                continue
                            target_id = inv_map[map_label]
                            stock_row = database_utils.search_item_by_id(conn, target_id)
                            if not stock_row:
                                st.warning(f"Mapped item for “{desc}” no longer exists — skipped.")
                                continue
                            # Stock: add received qty
                            database_utils.update_available_quantity(conn, target_id, stock_row['available_quantity'] + uq)
                            qty_updates += 1

                            if target_id in order_id_set:
                                # Already on the order: bump that row's received_quantity.
                                # Re-read fresh (the main loop above may have updated it this submit).
                                oi_row = next(x for x in line_items if x['item_id'] == target_id)
                                fresh_rows = database_utils.fetch_order_items_for_delivery(conn, oid)
                                cur_rec = next(x for x in fresh_rows if x['order_item_id'] == oi_row['order_item_id'])['received_quantity'] or 0
                                database_utils.update_order_item_received_quantity(conn, oi_row['order_item_id'], cur_rec + uq)
                                eff_qty[target_id] = eff_qty.get(target_id, 0) + uq
                                sub_notes.append(f"Sub/Extra added: {stock_row['name']} (Qty {uq}) — matched to ordered item")
                            else:
                                # Substitution/extra not on the order: new order_items row
                                database_utils.insert_substitution_order_item(conn, oid, target_id, stock_row['name'], uq)
                                sub_notes.append(f"Sub/Extra added: {stock_row['name']} (Qty {uq}) — invoice: “{desc}”")

                            # Price
                            if up > 0:
                                database_utils.update_item_price(conn, target_id, up, uv)
                                database_utils.log_price_history(conn, target_id, up, uv, order_id=oid)
                                price_updates += 1

                        if sub_notes:
                            disc_block = "Substitutions / extras:\n" + "\n".join(sub_notes)
                            notes = f"{notes}\n\n{disc_block}".strip() if notes else disc_block

                        # Fresh discrepancy summary — computed AFTER mapped unexpected
                        # items are folded in, so substitutions don't show as "Missing".
                        # First strip the stale pre-scan auto-notes block: it was
                        # pre-filled into the notes box before mapping, and would
                        # otherwise linger (e.g. "Missing: kids fish box (Exp 1, Got 0)"
                        # after an unexpected item was mapped onto that item).
                        old_auto = (st.session_state.get('delivery_notes_auto') or '').strip()
                        if old_auto and old_auto in notes:
                            notes = notes.replace(old_auto, '').strip()
                        disc = build_discrepancy_notes(line_items, {iid: {'qty': q} for iid, q in eff_qty.items()})
                        if disc and disc not in notes:
                            notes = f"{notes}\n\n{disc}".strip() if notes else disc

                        # Determine status
                        final_items = database_utils.fetch_order_items_for_delivery(conn, oid)
                        all_done = all((x['received_quantity'] or 0) >= x['ordered_quantity'] for x in final_items)
                        status = "Delivered" if all_done else "Partially Delivered"
                        
                        database_utils.update_order_status_and_notes(conn, oid, status, notes)
                        conn.commit()

                        # Clear memory
                        sup_clean = supplier_name.strip()
                        if 'generated_orders_map' in st.session_state:
                            if sup_clean in st.session_state.generated_orders_map:
                                del st.session_state.generated_orders_map[sup_clean]
                        
                        st.session_state.delivery_ai_results = {}
                        st.session_state.delivery_unexpected_items = []
                        st.session_state.delivery_scan_oid = None
                        st.session_state.delivery_notes_auto = ''
                        st.session_state.pop(f"notes_{oid}", None)
                        st.success(f"Processed: {qty_updates} Stock Updates, {price_updates} Price Logs. Order marked as {status}.")
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(f"Error: {e}")

elif choice == "📦 View Order History":
    st.header("📦 History")
    all_orders = database_utils.fetch_all_orders(conn)
    if all_orders:
        st.dataframe(pd.DataFrame(all_orders), use_container_width=True, hide_index=True)
        
        opts = {f"ID: {o['order_id']} - {o['supplier_name']}": o['order_id'] for o in all_orders}
        sel = st.selectbox("View Items:", list(opts.keys()), index=None)
        if sel:
            items = database_utils.fetch_order_items_for_delivery(conn, opts[sel])
            st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)

elif choice == "🕘 Item Order History":
    st.header("🕘 Item Order History")
    st.caption("Search an item name (or part of it) to see when it was last ordered — newest first.")

    term = st.text_input("🔍 Item name:", key="ioh_term").strip()
    if term:
        history = database_utils.fetch_item_order_history(conn, term, limit=20)
        if not history:
            st.info(f"No order history found for items matching '{term}'.")
        else:
            st.success(f"Found {len(history)} recent record(s) matching '{term}':")
            df_hist = pd.DataFrame(history)
            df_hist = df_hist[['order_date', 'item_name', 'ordered_quantity', 'supplier_name', 'status']]
            df_hist.columns = ['Date', 'Item Name', 'Qty', 'Supplier', 'Status']
            st.dataframe(df_hist, use_container_width=True, hide_index=True)
    else:
        st.info("Type an item name above to search its order history.")

elif choice == "🔍 Stock Check":
    st.header("🔍 Stock Check")

    if 'sc_list' not in st.session_state: st.session_state.sc_list = []
    if 'sc_idx' not in st.session_state: st.session_state.sc_idx = 0
    if 'sc_active' not in st.session_state: st.session_state.sc_active = False

    if not st.session_state.sc_active:
        mode = st.radio("Mode:", ("All Items", "By Category", "By Supplier"), horizontal=True)
        start = False
        if mode == "All Items":
            if st.button("Start All"):
                items = database_utils.fetch_all_items_with_ids(conn)
                start = True
        elif mode == "By Category":
            c = st.selectbox("Category", database_utils.fetch_distinct_categories(conn))
            if st.button("Start Category") and c:
                items = database_utils.fetch_items_with_ids_by_category(conn, c)
                start = True
        else:
            s = st.selectbox("Supplier", database_utils.fetch_distinct_suppliers(conn))
            if st.button("Start Supplier") and s:
                items = database_utils.fetch_items_with_ids_by_supplier(conn, s)
                start = True
        
        if start:
            if items:
                st.session_state.sc_list = sorted(items, key=lambda x: x['name'])
                st.session_state.sc_idx = 0
                st.session_state.sc_active = True
                st.rerun()
            else:
                st.warning("No items found.")
    
    else:
        # Check in progress
        if st.session_state.sc_idx >= len(st.session_state.sc_list):
            st.success("Check Complete!")
            if st.button("Finish"):
                st.session_state.sc_active = False
                invalidate_order_form_state()
                st.rerun()
        else:
            curr = st.session_state.sc_list[st.session_state.sc_idx]
            st.progress((st.session_state.sc_idx) / len(st.session_state.sc_list))
            st.subheader(f"Checking: {curr['name']}")
            
            # Fetch fresh data
            db_data = database_utils.search_item_by_id(conn, curr['id'])
            if not db_data:
                # Item was deleted while the check was in progress — skip it.
                st.warning(f"Item '{curr['name']}' no longer exists — skipped.")
                st.session_state.sc_idx += 1
                st.rerun()
            st.metric("System Qty", db_data['available_quantity'])
            
            # FIXED: Added unique keys to widgets based on item ID
            real = st.number_input("Actual Qty", min_value=0, value=db_data['available_quantity'], key=f"sc_qty_{curr['id']}")
            
            c1, c2, c3 = st.columns(3)
            # FIXED: Added unique keys to buttons
            if c1.button("Confirm & Next", type="primary", key=f"sc_confirm_{curr['id']}"):
                database_utils.update_available_quantity(conn, curr['id'], real)
                invalidate_order_form_state()
                st.session_state.sc_idx += 1
                st.rerun()
            
            if c2.button("Skip", key=f"sc_skip_{curr['id']}"):
                database_utils.log_skipped_item(conn, curr['id'])
                st.session_state.sc_idx += 1
                st.rerun()
            
            if c3.button("Quit", key="sc_quit"):
                st.session_state.sc_active = False
                st.rerun()

elif choice == "📊 Resolve Skipped Items":
    st.header("📊 Resolve Skipped")
    skipped = database_utils.fetch_unresolved_skipped_items_details(conn)
    
    if not skipped:
        st.success("No skipped items.")
    else:
        st.dataframe(pd.DataFrame(skipped), use_container_width=True)
        
        opts = {f"{s['item_name']} (Skipped: {s['skipped_time']})": s for s in skipped}
        sel_key = st.selectbox("Resolve:", list(opts.keys()), index=None)
        
        if sel_key:
            data = opts[sel_key]
            curr = database_utils.search_item_by_id(conn, data['item_id'])
            if not curr:
                st.error("This item no longer exists in the inventory — it can't be resolved by quantity update.")
            else:
                st.write(f"Current System Qty: {curr['available_quantity']}")
                new_q = st.number_input("Correct Qty", min_value=0, value=curr['available_quantity'])

                if st.button("Resolve"):
                    database_utils.update_available_quantity(conn, data['item_id'], new_q)
                    database_utils.mark_skipped_item_resolved(conn, data['skipped_item_id'])
                    invalidate_order_form_state()
                    st.success("Resolved!")
                    st.rerun()

# --- FOOTER ---
st.sidebar.markdown("---")
st.sidebar.info("ims_v3 · open source (MIT)")
