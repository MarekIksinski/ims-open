#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ims_v3 admin panel — inventory DB management, staff & access, allergens,
reporting, backup, AI settings.
"""
import logging
import os
import sqlite3
import json
import base64
import re
import requests
from datetime import datetime
import streamlit as st
import plotly.express as px
import pandas as pd
import database_utils
import altair as alt

# --- OLLAMA CONFIGURATION ---
# Endpoint/model/timeouts are stored in the app_settings DB table and
# edited live from the admin 'AI Settings' page. Defaults: database_utils. 
 

# --- HELPER FUNCTIONS ---

def streamlit_image_to_base64(uploaded_image_file):
    """Converts a Streamlit UploadedFile object (image) to a base64 encoded string."""
    if uploaded_image_file is None:
        return None
    try:
        img_bytes = uploaded_image_file.getvalue()
        return base64.b64encode(img_bytes).decode('utf-8')
    except Exception as e:
        st.error(f"Error encoding uploaded image: {e}")
        logging.error(f"Error encoding Streamlit uploaded image: {e}")
        return None

# ============ 3-PASS ALLERGEN PIPELINE (accuracy over speed) ============
# Same philosophy as the invoice pipeline: Pass 1 reads the label with NO
# master allergen list in the prompt (so the model cannot hallucinate DB
# allergens over what is actually printed). Pass 2 normalises the raw
# printed phrases to canonical master names, text-only. Pass 3
# independently re-checks the result against the transcription and its
# lists are FINAL.

def ocr_allergen_label(base64_img):
    """
    PASS 1 — pure vision label read. NO master allergen list in this
    prompt. Returns (data, error_msg) with:
      {'label_text': {'ingredients', 'allergy_statement', 'precautionary_statement'},
       'raw_contains': [...], 'raw_may_contain': [...]}
    raw_* entries keep the LABEL's wording (e.g. 'WHEY (MILK)', 'SOYA').
    """
    if not base64_img:
        return None, "No image data."
    prompt = """
    Read the product label in this image.

    STEP 1 — transcribe EXACTLY what is printed (empty string if absent):
    - "ingredients": the full Ingredients list, as printed.
    - "allergy_statement": the emphasized 'Contains: ...' statement.
    - "precautionary_statement": 'May contain ...' / 'Made in a factory
      that handles ...' / 'Not suitable for ...' warnings.
    When transcribing, PRESERVE any bold, underlined or highlighted
    allergen words by wrapping them in double asterisks, e.g.
    "**Wheat** Flour", "**Soya**", "**Mustard**". Bold formatting is the
    label's way of marking allergens — losing it loses allergens.

    STEP 2 — from the transcribed text ONLY, list the allergens:
    - "raw_contains": allergens present in the ingredients or the
      'Contains' statement. Scan EVERY ingredient word and compound
      (e.g. "Wheat Flour" -> Wheat, "Whey" -> Milk, "Spices (Mustard)"
      -> Mustard), not just bolded or parenthesised ones.
    - "raw_may_contain": allergens mentioned ONLY in the precautionary
      warning (traces / factory cross-contamination).
    Use the wording printed on the label (e.g. 'WHEY (MILK)', 'SOYA',
    'NUTS'). Do NOT invent allergens that are not printed somewhere on
    the label. An allergen belongs to at most one list.

    STEP 3 — "bold_words": every word/phrase that is visually bolded,
    underlined or highlighted anywhere on the label (usually the allergen
    declarations).

    RETURN JSON ONLY:
    {
        "label_text": {"ingredients": "...", "allergy_statement": "...", "precautionary_statement": "..."},
        "raw_contains": ["printed phrase", ...],
        "raw_may_contain": ["printed phrase", ...],
        "bold_words": ["printed emphasized word", ...]
    }
    """
    raw_text, err = _ollama_generate(prompt, images=[base64_img], temperature=0.0, tag="ocr_allergen_label")
    if err:
        return None, err
    data, err = _parse_json_response(raw_text, "ocr_allergen_label")
    if err:
        return None, err
    if not isinstance(data, dict) or "label_text" not in data:
        return None, "AI returned no label transcription (empty result)."
    data.setdefault("raw_contains", [])
    data.setdefault("raw_may_contain", [])
    data.setdefault("bold_words", [])
    return data, None

def match_allergens(pass1, master_names):
    """
    PASS 2 — text-only normalisation (no image). Maps each raw printed
    phrase to a canonical master allergen name (or null when no master
    name fits but the phrase is still an allergen).
    Returns (result, error_msg) with
      result = {'contains': [{'phrase','allergen'|None}],
                'may_contain': [{'phrase','allergen'|None}]}
    """
    def clean(lst):
        return [str(x).strip() for x in (lst or []) if str(x).strip()]

    master_str = "\n".join(f"- {n}" for n in master_names)
    contains_str = "\n".join(f"- {p}" for p in clean(pass1.get("raw_contains")))
    may_str = "\n".join(f"- {p}" for p in clean(pass1.get("raw_may_contain")))

    prompt = f"""
    You are normalising allergen phrases from a product label to our
    standard allergen names.

    STANDARD NAMES (use these whenever one fits):
    ---
    {master_str}
    ---

    PHRASES PRINTED ON THE LABEL — 'contains' group:
    ---
    {contains_str}
    ---

    PHRASES PRINTED ON THE LABEL — 'may contain' group:
    ---
    {may_str}
    ---

    TASK: for EACH phrase, give the canonical standard name it refers to
    (case-insensitive, e.g. 'WHEY (MILK)' -> "Milk", 'SOYA' -> "Soy",
    'GLUTEN/BARLEY' -> "Gluten"), or null if NO standard name fits.
    Use EXACTLY one of the STANDARD NAMES above (they include close
    variants such as Soya/Soy and Egg/Eggs — pick the one that fits the
    phrase's wording).

    Scan EVERY ingredient word and compound, not just bolded or
    parenthesised ones. Common derivations:
    - "Wheat Flour"/"Semolina"/"Spelt"/"Durum" -> Wheat (or Gluten if
      that reads better)
    - "Barley Malt"/"Rye"/"Oats" -> Barley/Rye/Oats (or Gluten)
    - "Whey"/"Casein"/"Butter"/"Cheese"/"Lactose" -> Milk
    - "Albumen"/"Albumin"/"Meringue" -> Eggs (or Egg)
    - "Soya Lecithin"/"Edamame"/"Tofu" -> Soya (or Soy)
    - "Sulphur Dioxide"/"Sulphites"/"E220-E228" -> Sulphites
    - "Mustard"/"Celery"/"Celeriac"/"Sesame"/"Tahini"/"Lupin" as printed

    Keep each phrase in the group it was given in — do not move phrases
    between groups.

    RETURN JSON ONLY:
    {{
        "contains": [{{"phrase": "WHEY (MILK)", "allergen": "Milk"}},
                     {{"phrase": "SOME EXOTIC", "allergen": null}}],
        "may_contain": [{{"phrase": "TRACE OF NUTS", "allergen": "Nuts"}}]
    }}
    """
    raw_text, err = _ollama_generate(prompt, temperature=0.0, tag="match_allergens")
    if err:
        return None, err
    data, err = _parse_json_response(raw_text, "match_allergens")
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "AI returned no allergen matches (empty result)."
    data.setdefault("contains", [])
    data.setdefault("may_contain", [])
    return data, None

def verify_allergens(pass1, matched, master_names=None):
    """
    PASS 3 — independent verification (no image). Re-reads the
    transcription against the proposed canonical lists, hunts for missed
    allergens and contains/may-contain swaps. Its lists are FINAL.
    Returns (result, error_msg) with
      result = {'contains': [...names], 'may_contain': [...names],
                'confidence': 'high'|'low', 'notes': str}
    """
    lt = pass1.get("label_text") or {}
    def names(group):
        out = []
        for m in matched.get(group, []):
            a = m.get("allergen") if isinstance(m, dict) else None
            if a:
                out.append(str(a))
        return sorted(set(out))

    master_str = ", ".join(master_names) if master_names else "(none)"

    prompt = f"""
    Verify proposed allergen lists against the label transcription.

    STANDARD ALLERGEN NAMES (use these exact spellings):
    {master_str}

    LABEL TRANSCRIPTION:
    INGREDIENTS: {lt.get('ingredients', '')}
    CONTAINS STATEMENT: {lt.get('allergy_statement', '')}
    PRECAUTIONARY WARNING: {lt.get('precautionary_statement', '')}

    PROPOSED 'contains': {names('contains')}
    PROPOSED 'may contain': {names('may contain')}

    CHECK carefully:
    - an allergen in the INGREDIENTS or 'Contains' statement that is
      missing from 'contains';
    - a traces-only allergen (precautionary warning only) wrongly placed
      in 'contains' — it belongs in 'may contain';
    - an allergen from the 'Contains' statement wrongly demoted to
      'may contain';
    - wrong standard-name mappings.
    SCAN EVERY ingredient word and compound against all 14 EU/UK
    declared allergens (celery, cereals/gluten, crustaceans, eggs, fish,
    lupin, milk, molluscs, mustard, nuts/peanuts, sesame, soy, sulphites)
    — derivations count: "Wheat Flour" -> Wheat, "Whey"/"Casein" -> Milk,
    "Albumen" -> Eggs, "Soya Lecithin" -> Soya, "Barley Malt" -> Barley,
    "Sulphur Dioxide"/E220-E228 -> Sulphites. Words bolded with ** in the
    transcription are allergen declarations by the label itself and MUST
    appear in a list.
    Correct the lists if needed; set confidence to "low" if the label
    text is ambiguous or unreadable. Lists may contain the same allergen
    in both groups only if the label says so.

    RETURN JSON ONLY:
    {{"contains": ["Milk", "Wheat"], "may_contain": ["Nuts"],
      "confidence": "high", "notes": "short reason for any change"}}
    """
    raw_text, err = _ollama_generate(prompt, temperature=0.0, tag="verify_allergens")
    if err:
        return None, err
    data, err = _parse_json_response(raw_text, "verify_allergens")
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "AI returned no verification result (empty result)."
    return data, None

# ---- Deterministic allergen safety net ----
# NEVER rely solely on the LLM once the text is already transcribed:
# word-boundary regex over the transcription unions anything the AI
# missed. Mappings are canonical UK 14 allergen names; each is then
# snapped to the closest existing master-allergen name so the UI
# multiselect pre-selects cleanly.
ALLERGEN_KEYWORD_MAP = {
    # canonical -> keywords (regex-safe, matched case-insensitively)
    'Wheat':   ['wheat', 'spelt', 'khorasan', 'kamut', 'durum', 'semolina'],
    'Gluten':  ['gluten'],
    'Barley':  ['barley', 'malt extract', 'malted'],
    'Rye':     ['rye'],
    'Oats':    ['oats'],
    'Cereals Containing Gluten': ['cereals containing gluten'],
    'Soy':     ['soya', 'soy', 'soybean', 'soyabeans', 'edamame', 'tofu'],
    'Milk':    ['milk', 'butter', 'cream', 'cheese', 'whey', 'casein',
                'caseinate', 'lactose', 'yoghurt', 'yogurt', 'ghee', 'buttermilk'],
    'Eggs':    ['egg', 'eggs', 'albumen', 'albumin', 'meringue', 'mayonnaise'],
    'Mustard': ['mustard'],
    'Celery':  ['celery', 'celeriac'],
    'Sesame':  ['sesame', 'tahini'],
    'Fish':    ['fish', 'cod', 'haddock', 'salmon', 'tuna', 'anchovy',
                'anchovies', 'pollock', 'plaice'],
    'Crustaceans': ['crustacean', 'crustaceans', 'prawn', 'prawns', 'shrimp',
                    'crab', 'lobster', 'crayfish', 'scampi'],
    'Molluscs': ['mollusc', 'molluscs', 'mussel', 'mussels', 'oyster',
                 'oysters', 'squid', 'calamari', 'scallop', 'scallops',
                 'clam', 'clams', 'whelk', 'whelks'],
    'Peanuts': ['peanut', 'peanuts', 'groundnut', 'groundnuts', 'arachis'],
    'Tree Nuts': ['almond', 'almonds', 'hazelnut', 'hazelnuts', 'walnut',
                  'walnuts', 'cashew', 'cashews', 'pecan', 'pecans',
                  'brazil nut', 'brazil nuts', 'pistachio', 'pistachios',
                  'macadamia', 'tree nut', 'tree nuts', 'nuts'],
    'Lupin':   ['lupin', 'lupins', 'lupine', 'lupines'],
    'Sulphites': ['sulphite', 'sulphites', 'sulfite', 'sulfites',
                  'sulphur dioxide', 'sulfur dioxide',
                  'e220', 'e221', 'e222', 'e223', 'e224', 'e226', 'e227', 'e228'],
}

CANONICAL_ALIASES = {
    # canonical -> master-name spellings to try, in order of preference
    'Tree Nuts': ['Tree Nuts', 'Nuts'],
    'Sulphites': ['Sulphites', 'Sulfites'],
    'Molluscs':  ['Molluscs', 'Mollusc'],
    'Eggs':      ['Eggs', 'Egg'],
    'Soy':       ['Soy', 'Soya'],
    'Gluten':    ['Gluten', 'Cereals Containing Gluten'],
}

def _snap_to_master(canonical, master_allergens):
    """Snap a canonical name to the closest existing master name.
    Tries the canonical and its aliases; prefers exact-insensitive match,
    then a singular/plural or shared-prefix variant (Soya<->Soy,
    Egg<->Eggs, Mollusc<->Molluscs); if nothing fits, keeps the canonical
    name (it becomes a new master entry on save)."""
    lowered = {m.lower(): m for m in master_allergens}
    for cand in [canonical] + CANONICAL_ALIASES.get(canonical, []):
        c = cand.lower()
        if c in lowered:
            return lowered[c]
        for variant in (c + 's', c.rstrip('s')):
            if variant in lowered:
                return lowered[variant]
        for m in master_allergens:
            ml = m.lower()
            if len(c) >= 4 and (ml.startswith(c[:4]) or c.startswith(ml[:4])):
                return m
    return canonical

def deterministic_allergen_scan(text, master_allergens):
    """Regex keyword scan of already-transcribed label text. Returns the
    set of master-snapped canonical allergen names found. Word-boundary,
    case-insensitive; '*' from bold markers stripped."""
    found = set()
    if not text:
        return found
    clean = text.replace('*', ' ')
    for canonical, keywords in ALLERGEN_KEYWORD_MAP.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', clean, re.IGNORECASE):
                found.add(_snap_to_master(canonical, master_allergens))
                break
    return found

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
    Every call re-reads live AI settings (model, url, timeout) from the
    app_settings table.
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
# Same pipeline as the staff app's delivery scan (ims_web_app_user.py),
# adapted for the admin price sweep: no order context, just the supplier
# catalog. Pass 1 reads the invoice with NO database context at all so the
# vision model cannot hallucinate DB aliases over the real invoice text.
# Pass 2 matches the extracted text to DB ids without the image. Pass 3
# independently re-checks the matches for swaps.

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

def match_invoice_lines(ocr_lines, supplier_catalog):
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

    catalog_str = "\n".join(fmt(i) for i in supplier_catalog)
    ocr_str = "\n".join(
        f"Line {idx}: \"{ln.get('invoice_desc', '')}\" (qty {ln.get('qty', 0)}, unit price {ln.get('unit_price', 0)})"
        for idx, ln in enumerate(ocr_lines)
    )

    prompt = f"""
    You are matching invoice lines to database items.

    SUPPLIER CATALOG ITEMS:
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

def verify_matches(ocr_lines, proposed_matches, supplier_catalog):
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

    items_str = "\n".join(fmt(i) for i in supplier_catalog)
    name_by_id = {i['id']: i['name'] for i in supplier_catalog}

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

# --- STREAMLIT APP START ---

st.set_page_config(page_title="IMS (Admin)", layout="wide")

# Establish Database Connection using database_utils
conn = database_utils.connect_to_db() 
if conn:
    database_utils.ensure_all_tables_exist(conn) 
    # Safety: Ensure Admin exists if this is the first time running the system
    database_utils.ensure_admin_user_exists(conn)
else:
    st.error("🔴 Major database connection issue. Admin app cannot start.")
    st.stop() 

# --- AUTHENTICATION & SESSION STATE ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'current_user' not in st.session_state:
    st.session_state.current_user = None

def login():
    """Admin Login Screen with Role Check"""
    st.markdown("## 🔒 Admin Access")
    st.write("Please enter your Admin PIN.")
    
    with st.form("admin_login_form"):
        pin_input = st.text_input("PIN", type="password", max_chars=4, placeholder="****")
        submitted = st.form_submit_button("Login")
        
        if submitted:
            user = database_utils.verify_staff_login(conn, pin_input)
            if user:
                # CHECK PERMISSION HERE
                if user['is_admin']:
                    st.session_state.logged_in = True
                    st.session_state.current_user = user
                    if user.get('legacy_login'):
                        # Audit #2: hash was legacy at login time (now
                        # upgraded) — nag loudly until logout.
                        st.session_state.pin_legacy_notice = True
                    st.success(f"Welcome, {user['username']}!")
                    st.rerun()
                else:
                    st.error("⛔ Access Denied: This account is Staff only.")
            else:
                st.error("❌ Incorrect PIN")

def logout():
    st.session_state.logged_in = False
    st.session_state.current_user = None
    st.session_state.pop('pin_legacy_notice', None)
    st.rerun()

# --- LOGIN GATE ---
if not st.session_state.logged_in:
    _, col2, _ = st.columns([1, 1, 1])
    with col2:
        login()
    st.stop()  # Stops app execution here if not logged in

# =========================================================
#  🏁 MAIN APP STARTS HERE (Only runs if logged_in == True)
# =========================================================

st.title(f"📦 Admin Panel (User: {st.session_state.current_user['username']})")

if st.session_state.get('pin_legacy_notice'):
    st.warning("⚠️ Your PIN was stored in an insecure legacy format. It has "
               "just been upgraded to a secure hash — but treat the old PIN as "
               "compromised: change it on the Users screen if anyone else "
               "knows it.", icon="🔐")

# Admin visibility (audit 2026-10-31 #2): accounts still on legacy hashes.
_legacy_accounts = database_utils.list_legacy_pin_accounts(conn)
if _legacy_accounts:
    st.error("🔐 These accounts still have an insecure legacy PIN hash and "
             "must log in once to upgrade them: "
             + ", ".join(_legacy_accounts)
             + ". Until then, a stolen database file would reveal their PINs.")

# --- SIDEBAR NAVIGATION ---
with st.sidebar:
    st.write(f"👤 **{st.session_state.current_user['username']}**")
    if st.button("🔒 Logout", type="primary"):
        logout()
    st.divider()
    
st.sidebar.title("Admin Navigation")

with st.sidebar.expander("📝 Inventory DB Mgmt", expanded=True): 
    if st.button("📊 View Full Stock", key="nav_view_stock", use_container_width=True):
        st.session_state.admin_choice = "📊 View Full Stock"
        st.rerun()
    if st.button("🔍 Search Items", key="nav_search", use_container_width=True):
        st.session_state.admin_choice = "🔍 Search Items"
        st.rerun()
    if st.button("➕ Add New Item", key="nav_add", use_container_width=True):
        st.session_state.admin_choice = "➕ Add New Item"
        st.rerun()
    if st.button("✏️ Edit Item Details", key="nav_edit_item", use_container_width=True):
        st.session_state.admin_choice = "✏️ Edit Item Details"
        st.rerun()
    if st.button("🎯 Batch Set Desired Stock", key="nav_batch_desired", use_container_width=True):
        st.session_state.admin_choice = "🎯 Batch Set Desired Stock"
        st.rerun()
    if st.button("💾 Export Stock Data", key="nav_export", use_container_width=True):
        st.session_state.admin_choice = "💾 Export Stock Data"
        st.rerun()
    if st.button("🚚 Import Stock Data (CSV)", key="nav_import_csv", use_container_width=True):
        st.session_state.admin_choice = "🚚 Import Stock Data (CSV)"
        st.rerun()
    if st.button("💰 Price & Value", key="nav_prices", use_container_width=True):
        st.session_state.admin_choice = "💰 Price & Value"
        st.rerun()

with st.sidebar.expander("📋 Order DB Mgmt", expanded=True):
     if st.button("View Order History", key="nav_view_orders", use_container_width=True):
        st.session_state.admin_choice = "View Order History"
        st.rerun()
     if st.button("🛒 Create Custom Order", key="nav_create_order", use_container_width=True): 
        st.session_state.admin_choice = "🛒 Create Custom Order"
        st.rerun()
     if st.button("Delete/Cancel Order", key="nav_delete_orders", use_container_width=True):
        st.session_state.admin_choice = "Delete/Cancel Order"
        st.rerun()

with st.sidebar.expander("👥 Staff & Access", expanded=True): 
    if st.button("Manage Users", key="nav_users", use_container_width=True):
        st.session_state.admin_choice = "Manage Users"
        st.rerun()
    if st.button("🧠 AI Settings", key="nav_ai_settings", use_container_width=True):
        st.session_state.admin_choice = "🧠 AI Settings"
        st.rerun()

with st.sidebar.expander("🏷️ Allergen Management", expanded=True): 
    if st.button("🤖 AI Allergen Extractor", key="nav_ai_allergen", use_container_width=True):
        st.session_state.admin_choice = "🤖 AI Allergen Extractor"
        st.rerun()
    if st.button("👁️ View All Allergens", key="nav_view_allergens", use_container_width=True):
        st.session_state.admin_choice = "👁️ View All Allergens"
        st.rerun()

with st.sidebar.expander("♻️ Waste & Stock Loss", expanded=True):
    if st.button("📊 Waste Analysis", key="nav_waste_analysis", use_container_width=True):
        st.session_state.admin_choice = "📊 Waste Analysis"
        st.rerun()
    
with st.sidebar.expander("📈 Reporting & Visualisation", expanded=True):
    if st.button("📊 Spend Analysis", key="nav_spend", use_container_width=True):
        st.session_state.admin_choice = "📊 Spend Analysis"
        st.rerun()

with st.sidebar.expander("💾 System Backup", expanded=False):
    if st.button("Backup & Restore", key="nav_backup", use_container_width=True):
        st.session_state.admin_choice = "Backup & Restore"
        st.rerun()
        
# --- MAIN PAGE CONTENT ---

if 'admin_choice' not in st.session_state:
    st.session_state.admin_choice = "📊 View Full Stock" 

choice = st.session_state.admin_choice

@st.cache_data 
def get_admin_items_for_selection(_conn):
    """
    Fetches all items (id, name) from database_utils for selectbox display.
    """
    if not _conn: return [], {} 
    items_list_of_dicts = database_utils.fetch_all_items_with_ids(_conn) 
    if not items_list_of_dicts: return [], {}

    sorted_items = sorted(items_list_of_dicts, key=lambda x: x['name'])
    selectbox_options_map = {
        f"{item['name']} (ID: {item['id']})": item['id'] 
        for item in sorted_items
    }
    return list(selectbox_options_map.keys()), selectbox_options_map


ALLERGEN_STALE_DAYS = 90

def _allergen_review_bucket(last_reviewed):
    """
    Classify an items.allergens_last_reviewed value:
    'never' (NULL/empty/unparseable), 'stale' (> ALLERGEN_STALE_DAYS days),
    or 'fresh'. Shared by the AI Allergen Extractor filter and the
    Inventory Allergen Review Status audit table.
    """
    if not last_reviewed:
        return 'never'
    s = str(last_reviewed).strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s, fmt)
            return 'stale' if (datetime.now() - dt).days > ALLERGEN_STALE_DAYS else 'fresh'
        except ValueError:
            continue
    return 'never'  # unparseable -> treat as needing review


# --- VIEW SECTIONS ---

if choice == "📊 View Full Stock":
    st.header("📊 Display Inventory")
    tables = database_utils.fetch_tables(conn)

    if not tables:
        st.warning("No tables found in the database.")
    else:
        if 'items' in tables:
            st.subheader("Contents of Table: `items`")
            columns_items, data_items = database_utils.fetch_table_data(conn, 'items')
            if columns_items and data_items:
                df_items = pd.DataFrame(data_items, columns=columns_items)
                st.dataframe(df_items, use_container_width=True, hide_index=True)
            else:
                st.info("Table `items` is empty.")
        else:
            st.warning("The main 'items' table was not found.")

        # Quick view for other tables
        other_tables = [t for t in tables if t in ['orders', 'order_items']] 
        if other_tables:
            st.divider()
            st.subheader("Other Relevant Data")
            selected_other = st.selectbox("Quick view for:", other_tables, index=None, key="staff_other_tables_select")
            if selected_other:
                cols_other, data_other = database_utils.fetch_table_data(conn, selected_other)
                if cols_other and data_other:
                    st.dataframe(pd.DataFrame(data_other, columns=cols_other), use_container_width=True, hide_index=True)
                else:
                    st.info(f"Table `{selected_other}` is empty.")

elif choice == "💰 Price & Value":
    st.header("💰 Price Management & Stock Valuation")

    # --- Fetch Data ---
    price_data = database_utils.fetch_inventory_with_prices(conn)
    
    if not price_data:
        st.info("No items found.")
    else:
        df = pd.DataFrame(price_data)
        
        # Calculations for Dashboard
        df['gross_unit_price'] = df['net_price'] * (1 + (df['vat_rate'] / 100))
        df['total_stock_value_net'] = df['net_price'] * df['available_quantity']
        df['total_stock_value_gross'] = df['gross_unit_price'] * df['available_quantity']
        
        total_net = df['total_stock_value_net'].sum()
        total_gross = df['total_stock_value_gross'].sum()
        
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Stock Value (Net)", f"£{total_net:,.2f}")
        m2.metric("Total Stock Value (Gross)", f"£{total_gross:,.2f}")
        m3.metric("Items Priced", f"{len(df[df['net_price'] > 0])} / {len(df)}")
        
        st.divider()

        # --- SECTION: AI INVOICE SCANNER (3-pass pipeline) ---
        with st.expander("📄 Update Prices from Invoice (AI)", expanded=True):
            st.info("Upload invoice photo(s). The 3-pass pipeline transcribes the invoice, "
                    "matches lines to your catalog, then independently re-checks every match "
                    "(accuracy over speed).")

            suppliers = database_utils.fetch_distinct_suppliers(conn)
            sel_supplier = st.selectbox("1. Select Supplier (Filters Items)", suppliers, index=None)
            up_files = st.file_uploader("2. Upload Invoice Image(s)", accept_multiple_files=True,
                                        type=['jpg', 'png', 'jpeg'])

            # Session state for AI results
            if 'invoice_matches' not in st.session_state: st.session_state.invoice_matches = []

            if st.button("🚀 Scan Invoice") and up_files and sel_supplier:
                # Catalog context for Pass 2/3 (aliases included)
                context_items = database_utils.fetch_supplier_items_with_aliases(conn, sel_supplier)

                if not context_items:
                    st.error(f"No items found for supplier '{sel_supplier}'.")
                else:
                    st.session_state.invoice_matches = []  # Reset
                    scan_errors = []
                    results = []

                    progress = st.progress(0)
                    for idx, file in enumerate(up_files):
                        b64 = streamlit_image_to_base64(file)

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
                        matches, err_msg = match_invoice_lines(ocr_lines, context_items)
                        if err_msg:
                            scan_errors.append(f"Page {idx + 1} (Match): {err_msg}")
                            progress.progress((idx + 1) / len(up_files))
                            continue

                        # --- Pass 3: independent verification / swap check ---
                        verified, v_err = verify_matches(ocr_lines, matches, context_items)
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

                        # One row per invoice line for the review table
                        for m in matches:
                            li_idx = m.get('line_index')
                            if not (isinstance(li_idx, int) and 0 <= li_idx < len(ocr_lines)):
                                continue
                            ln = ocr_lines[li_idx]
                            results.append({
                                'db_id': m.get('id'),
                                'invoice_desc': ln.get('invoice_desc', '') or 'Unknown',
                                'net_price': float(ln.get('unit_price', 0.0) or 0.0),
                                'vat_rate': float(ln.get('vat_rate', 0.0) or 0.0),
                                'needs_review': bool(needs_review_map.get(li_idx)),
                            })

                        progress.progress((idx + 1) / len(up_files))

                    for e in scan_errors:
                        st.error(e)

                    # Clear stale review-widget state from a previous scan, then
                    # assign the new values into session_state (Streamlit keeps
                    # existing widget state over value= defaults).
                    for k in [k for k in list(st.session_state.keys())
                              if isinstance(k, str) and k.startswith(("save_", "sel_", "learn_", "np_", "nv_"))]:
                        st.session_state.pop(k, None)
                    for i, r in enumerate(results):
                        st.session_state[f"save_{i}"] = r['db_id'] is not None
                        st.session_state[f"np_{i}"] = r['net_price']
                        st.session_state[f"nv_{i}"] = r['vat_rate']

                    st.session_state.invoice_matches = results
                    if results:
                        review = sum(1 for r in results if r['needs_review'])
                        msg = f"Scanned {len(results)} invoice lines."
                        if review:
                            msg += f" {review} flagged 🔍 Verify match."
                        st.session_state.invoice_scan_msg = msg
                    elif not scan_errors:
                        st.session_state.invoice_scan_msg = "AI found no invoice lines on the uploaded page(s)."
                    st.rerun()

            if st.session_state.get('invoice_scan_msg'):
                st.success(st.session_state.invoice_scan_msg)

            # --- DISPLAY & SAVE RESULTS ---
            if st.session_state.invoice_matches:
                st.write("### Review Matches")
                st.caption("If AI missed a match, select it manually. Check 'Learn?' to save that name for next time. "
                           "Rows flagged 🔍 had low confidence or a Pass-2/Pass-3 disagreement — check the mapping.")

                updates_to_process = []
                aliases_to_learn = []

                # Header row
                cols = st.columns([0.5, 2.5, 2.5, 0.5, 1.2, 1.2])
                cols[0].write("**Save?**")
                cols[1].write("**Invoice Text**")
                cols[2].write("**DB Match**")
                cols[3].write("**Learn?**")
                cols[4].write("**Net £**")
                cols[5].write("**VAT %**")

                # Prepare dropdown options from current price data
                # We need a map of ID -> Name
                db_item_opts = {f"{x['name']} (ID: {x['id']})": x['id'] for x in price_data}
                db_item_opts["❌ No Match"] = None

                for idx, match in enumerate(st.session_state.invoice_matches):
                    # Data from AI
                    ai_db_id = match.get('db_id')
                    inv_desc = match.get('invoice_desc', 'Unknown')
                    price = float(match.get('net_price', 0.0))
                    vat = float(match.get('vat_rate', 0.0))

                    c1, c2, c3, c4, c5, c6 = st.columns([0.5, 2.5, 2.5, 0.5, 1.2, 1.2])

                    # 1. Save Checkbox
                    do_save = c1.checkbox("##", value=(ai_db_id is not None), key=f"save_{idx}", label_visibility="hidden")

                    # 2. Invoice Text (Read-only) + review badge
                    badge = " 🔍 Verify match" if match.get('needs_review') else ""
                    c2.markdown(f"{inv_desc}{badge}")

                    # 3. DB Match Dropdown (Allow user to correct AI)
                    # Find current index in dropdown
                    curr_index = None
                    if ai_db_id:
                        for i, (k, v) in enumerate(db_item_opts.items()):
                            if v == ai_db_id:
                                curr_index = i
                                break

                    selected_label = c3.selectbox("##", list(db_item_opts.keys()), index=curr_index, key=f"sel_{idx}", label_visibility="hidden")
                    selected_id = db_item_opts[selected_label]

                    # 4. Learn Checkbox (Only show if manually matched or AI matched)
                    # If user links "Beef Dripping" -> "Fat", they can check this.
                    do_learn = False
                    if selected_id:
                         do_learn = c4.checkbox("##", key=f"learn_{idx}", label_visibility="hidden", help=f"Teach AI that '{inv_desc}' = '{selected_label}'")

                    # 5/6. Price/VAT inputs
                    new_p = c5.number_input("£", value=price, step=0.01, key=f"np_{idx}", label_visibility="hidden")
                    new_v = c6.number_input("%", value=vat, step=0.5, key=f"nv_{idx}", label_visibility="hidden")
                    
                    # Collect Logic
                    if do_save and selected_id:
                        updates_to_process.append((selected_id, new_p, new_v))
                        if do_learn:
                            aliases_to_learn.append((selected_id, inv_desc))

                st.divider()
                if st.button(f"💾 Process Updates", type="primary"):
                    # 1. Save Aliases
                    learned_count = 0
                    for item_id, alias_name in aliases_to_learn:
                        if database_utils.add_item_alias(conn, item_id, alias_name):
                            learned_count += 1
                    
                    # 2. Update Prices
                    price_count = 0
                    for uid, up, uv in updates_to_process:
                        if database_utils.update_item_price(conn, uid, up, uv):
                            price_count += 1
                    
                    msg = f"Updated {price_count} prices."
                    if learned_count > 0:
                        msg += f" Learned {learned_count} new item names!"
                    
                    st.success(msg)
                    st.session_state.invoice_matches = []
                    st.session_state.invoice_scan_msg = ""
                    st.rerun()

        st.divider()
        
        # --- EDITOR (Manual) ---
        st.subheader("Manual Price Editor")
        item_opts = {f"{row['name']} (Curr: £{row['net_price']:.2f})": row for index, row in df.iterrows()}
        selected_key = st.selectbox("Select Item to Edit:", options=list(item_opts.keys()), index=None)
        
        if selected_key:
            item = item_opts[selected_key]
            with st.form("price_editor"):
                c1, c2 = st.columns(2)
                new_net = c1.number_input("Net Unit Price (£)", min_value=0.0, value=float(item['net_price']), step=0.01)
                new_vat = c2.number_input("VAT Rate (%)", min_value=0.0, value=float(item['vat_rate']), step=0.5)
                
                calc_gross = new_net * (1 + (new_vat/100))
                st.caption(f"Calculated Gross Price: **£{calc_gross:.2f}**")
                
                if st.form_submit_button("💾 Save Price"):
                    if database_utils.update_item_price(conn, item['id'], new_net, new_vat):
                        st.success(f"Updated price for '{item['name']}'")
                        st.rerun()
                    else:
                        st.error("Failed to update price.")

        # --- FULL TABLE ---
        st.subheader("Full Valuation Table")
        display_df = df[['name', 'available_quantity', 'net_price', 'vat_rate', 'gross_unit_price', 'total_stock_value_net']].copy()
        display_df.columns = ['Item', 'Qty', 'Net (£)', 'VAT (%)', 'Gross (£)', 'Total Net (£)']
        
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Net (£)": st.column_config.NumberColumn(format="£%.2f"),
                "Gross (£)": st.column_config.NumberColumn(format="£%.2f"),
                "Total Net (£)": st.column_config.NumberColumn(format="£%.2f"),
                "VAT (%)": st.column_config.NumberColumn(format="%.1f%%"),
            }
        )

elif choice == "➕ Add New Item":
    st.header("➕ Add New Item")
    with st.form("new_item_form_v2", clear_on_submit=True):
        name = st.text_input("Item Name*", key="add_name_v2")
        category = st.text_input("Category", key="add_cat_v2")
        main_supplier = st.text_input("Main Supplier", key="add_main_v2")
        secondary_supplier = st.text_input("Secondary Supplier", key="add_sec_v2")
        col1, col2 = st.columns(2)
        available_quantity = st.number_input("Initial Available Qty*", min_value=0, step=1, value=0)
        desired_quantity = st.number_input("Initial Desired Qty*", min_value=0, step=1, value=0)
        expiry_date_input = st.date_input("Expiry Date (Optional)", value=None, min_value=datetime.today().date())

        if st.form_submit_button("Add Item to Inventory"):
            if not name.strip():
                 st.warning("Item Name is required.")
            else:
                expiry_str = expiry_date_input.strftime('%Y-%m-%d') if expiry_date_input else None
                success = database_utils.add_item_from_csv_import(
                    conn, name.strip(), category.strip(), main_supplier.strip(),
                    secondary_supplier.strip(), int(available_quantity), int(desired_quantity), expiry_str
                )
                if success:
                    st.success(f"Item '{name}' added successfully!")
                    get_admin_items_for_selection.clear()
                else:
                    st.error(f"Failed to add item '{name}'. It might already exist.")

elif choice == "🔍 Search Items":
    st.header("🔍 Search and View Item Details")
    item_display_list, item_display_to_id_map = get_admin_items_for_selection(conn) 

    if not item_display_list:
        st.warning("No items found.")
    else:
        selected_display = st.selectbox("Select Item:", item_display_list, index=None, placeholder="Choose an item...")
        if selected_display:
            item_id = item_display_to_id_map[selected_display]
            item_details = database_utils.search_item_by_id(conn, item_id) 
            if item_details:
                st.subheader(f"Details for: {item_details['name']} (ID: {item_details['id']})")
                col1, col2 = st.columns(2)
                with col1:
                    st.metric("Available Quantity", item_details.get('available_quantity', 'N/A'))
                    st.text_input("Category", value=item_details.get('category', 'N/A'), disabled=True)
                    st.text_input("Main Supplier", value=item_details.get('main_supplier', 'N/A'), disabled=True)
                with col2:
                    st.metric("Desired Quantity", item_details.get('desired_quantity', 'N/A'))
                    st.text_input("Secondary Supplier", value=item_details.get('secondary_supplier', 'N/A'), disabled=True)
                    st.text_input("Expiry Date", value=item_details.get('expiry_date', 'N/A'), disabled=True)
                    st.text_input("Allergens Last Reviewed", value=item_details.get('allergens_last_reviewed') or 'Never', disabled=True)
            else:
                 st.error("Could not find details for this item.")

elif choice == "💾 Export Stock Data":
    st.header("💾 Export Full Stock to CSV")
    if st.button("Generate and Prepare Download"):
        items_list = database_utils.fetch_all_items_for_export(conn)
        if items_list:
            df_stock = pd.DataFrame(items_list)
            if not df_stock.empty:
                col_order = ['id', 'name', 'category', 'main_supplier', 'secondary_supplier', 
                             'available_quantity', 'desired_quantity', 'expiry_date',
                             'allergens_last_reviewed']
                df_stock = df_stock.reindex(columns=col_order).fillna('')
                # formula-injection neutralisation before CSV (audit #4)
                df_stock = database_utils.sanitize_df_for_csv(df_stock)
                csv_data = df_stock.to_csv(index=False).encode('utf-8')
                file_name = f'stock_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
                st.download_button("📥 Download Stock Data (.csv)", data=csv_data, file_name=file_name, mime='text/csv')
            else:
                st.info("DataFrame was empty.")
        else:
            st.error("No stock data fetched.")

elif choice == "🚚 Import Stock Data (CSV)":
    st.header("🚚 Import Item Data from CSV")
    st.write("Expected Columns: `name`, `category`, `main_supplier`, `secondary_supplier`, `available_quantity`, `desired_quantity`.")
    uploaded_file = st.file_uploader("Choose CSV file", type="csv")

    if uploaded_file is not None:
        try:
            df_new = pd.read_csv(uploaded_file)
            st.dataframe(df_new.head())
            if st.button("🚀 Start Import"):
                success_count = 0
                failure_count = 0
                errors = []
                
                if 'name' not in df_new.columns:
                    st.error("CSV missing required column: 'name'")
                else:
                    with st.spinner("Importing..."):
                        for idx, row in df_new.iterrows():
                            try:
                                name_val = str(row['name']).strip() if pd.notna(row['name']) else None
                                if not name_val:
                                    failure_count += 1
                                    continue
                                
                                # Safe extraction
                                cat = str(row.get('category', '')).strip()
                                main_sup = str(row.get('main_supplier', '')).strip()
                                sec_sup = str(row.get('secondary_supplier', '')).strip()
                                avail = int(pd.to_numeric(row.get('available_quantity', 0), errors='coerce') or 0)
                                desire = int(pd.to_numeric(row.get('desired_quantity', 0), errors='coerce') or 0)

                                if database_utils.add_item_from_csv_import(conn, name_val, cat, main_sup, sec_sup, avail, desire):
                                    success_count += 1
                                else:
                                    failure_count += 1
                                    errors.append(f"Row {idx+2}: Failed to insert '{name_val}' (check duplicates).")
                            except Exception as e:
                                failure_count += 1
                                errors.append(f"Row {idx+2}: Error - {e}")
                    
                    st.success(f"Imported: {success_count}. Failed: {failure_count}.")
                    if errors:
                        with st.expander("View Errors"):
                            for e in errors: st.write(e)
                    get_admin_items_for_selection.clear()
        except Exception as e:
            st.error(f"Error processing CSV: {e}")

elif choice == "✏️ Edit Item Details":
     st.header("✏️ Edit Item Details")
     item_display_list, item_display_to_id_map = get_admin_items_for_selection(conn)

     if not item_display_list:
        st.warning("No items found.")
     else:
        selected_display = st.selectbox("Select Item:", item_display_list, index=None, placeholder="Choose an item...")
        if selected_display:
            item_id = item_display_to_id_map[selected_display]
            current_data = database_utils.search_item_by_id(conn, item_id)

            if current_data:
                st.subheader(f"Editing: {current_data['name']} (ID: {item_id})")
                with st.form(key=f"edit_form_{item_id}"):
                    # Existing fields
                    new_cat = st.text_input("Category", value=current_data.get('category', ''))
                    new_main = st.text_input("Main Supplier", value=current_data.get('main_supplier', ''))
                    new_sec = st.text_input("Secondary Supplier", value=current_data.get('secondary_supplier', ''))
                    
                    new_des = st.number_input("Desired Quantity", min_value=0, value=int(current_data.get('desired_quantity', 0)))
                    
                    curr_date = None
                    if current_data.get('expiry_date'):
                        try: curr_date = datetime.strptime(current_data['expiry_date'], '%Y-%m-%d').date()
                        except ValueError: pass
                    
                    new_date = st.date_input("Expiry Date", value=curr_date)
                    
                    if st.form_submit_button("💾 Save Changes"):
                        date_str = new_date.strftime('%Y-%m-%d') if new_date else None
                        
                        # Update Core
                        ok_core = database_utils.update_item_core_details_by_id(conn, item_id, new_cat.strip(), new_main.strip(), new_sec.strip(), int(new_des), date_str)
                        
                        if ok_core:
                            st.success("Updated successfully!")
                            get_admin_items_for_selection.clear()
                        else:
                            st.error("Update failed.")

elif choice == "🎯 Batch Set Desired Stock":
    st.header("🎯 Batch Update Desired Stock Levels")
    st.info("Edit the 'Desired Qty' column directly in the table below, then click 'Save Changes'.")

    # 1. Fetch Data
    # We fetch all items. We can use the existing export function as it gets the columns we need.
    all_items = database_utils.fetch_all_items_for_export(conn)
    
    if not all_items:
        st.warning("No items found in database.")
    else:
        df = pd.DataFrame(all_items)
        
        # 2. Filter Options (Optional, helps if you have hundreds of items)
        cats = sorted(list(set(df['category'].dropna().unique())))
        cats.insert(0, "All Categories")
        
        col_filter, col_save = st.columns([1, 1])
        with col_filter:
            selected_cat = st.selectbox("Filter by Category:", cats)
        
        # Apply Filter
        if selected_cat != "All Categories":
            df_display = df[df['category'] == selected_cat].copy()
        else:
            df_display = df.copy()

        # 3. Prepare DataFrame for Editor
        # We only want to show relevant columns. 
        # We MUST keep 'id' to identify the row, but we can hide it or disable it.
        df_editor = df_display[['id', 'name', 'category', 'available_quantity', 'desired_quantity']]

        # 4. The Editable Dataframe
        edited_df = st.data_editor(
            df_editor,
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "name": st.column_config.TextColumn("Item Name", disabled=True),
                "category": st.column_config.TextColumn("Category", disabled=True),
                "available_quantity": st.column_config.NumberColumn("Current Stock", disabled=True),
                "desired_quantity": st.column_config.NumberColumn(
                    "🎯 Desired Qty", 
                    min_value=0, 
                    step=1,
                    help="Target stock level. Orders are generated to reach this number."
                ),
            },
            hide_index=True,
            use_container_width=True,
            key="desired_qty_editor" # Unique key ensures state is kept
        )

        # 5. Save Logic
        # We compare the edited dataframe (edited_df) against the original (df_display)
        # to find what changed.
        with col_save:
            st.write("###") # Spacer
            if st.button("💾 Save Changes", type="primary", use_container_width=True):
                changes_count = 0
                
                # Convert both to dictionaries for faster lookup/comparison
                # (Iterating rows is fast enough for <10,000 items)
                with st.spinner("Saving updates..."):
                    for index, row in edited_df.iterrows():
                        item_id = row['id']
                        new_qty = row['desired_quantity']
                        
                        # Find original value to see if it changed
                        original_row = df_display[df_display['id'] == item_id]
                        if not original_row.empty:
                            old_qty = original_row.iloc[0]['desired_quantity']
                            
                            if int(new_qty) != int(old_qty):
                                # It changed! Update DB.
                                success = database_utils.update_desired_quantity_only(conn, item_id, new_qty)
                                if success:
                                    changes_count += 1
                
                if changes_count > 0:
                    st.success(f"✅ Successfully updated targets for {changes_count} items!")
                    # Clear cache to force refresh on next load
                    st.cache_data.clear()
                    # Rerun to show new values as 'original'
                    st.rerun()
                else:
                    st.info("No changes detected.")

elif choice == "View Order History":
     st.header("📋 View Order History")
     all_orders = database_utils.fetch_all_orders(conn)
     if not all_orders:
        st.info("No order history found.")
     else:
        st.dataframe(pd.DataFrame(all_orders), use_container_width=True, hide_index=True)
        st.divider()
        st.subheader("View Items for a Specific Order")
        
        order_options = {f"ID: {o['order_id']} - {o['supplier_name']} ({o['generation_time']})": o['order_id'] for o in all_orders}
        selected_key = st.selectbox("Select Order:", list(order_options.keys()), index=None)
        
        if selected_key:
            items = database_utils.fetch_order_items_for_delivery(conn, order_options[selected_key])
            if items:
                st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)
            else:
                st.info("No items found for this order.")

elif choice == "🛒 Create Custom Order":
    st.header("🛒 Create Custom Order")

    if 'admin_custom_order_items' not in st.session_state: st.session_state.admin_custom_order_items = []
    
    st.subheader("1. Add Items")
    filter_mode = st.radio("Filter by:", ("Supplier", "Category"), horizontal=True)
    
    col1, col2 = st.columns(2)
    items_for_select = []
    item_map = {}
    
    with col1:
        if filter_mode == "Supplier":
            suppliers = database_utils.fetch_distinct_suppliers(conn)
            sel_filter = st.selectbox("Select Supplier", suppliers, index=None)
            if sel_filter:
                # MODIFIED: Now fetching secondary items too
                items = database_utils.fetch_items_with_ids_by_supplier(conn, sel_filter, include_secondary=True)
                for i in items:
                    d_name = f"{i['name']} (ID: {i['id']})"
                    # Add visual indicator if it's a secondary item
                    if i.get('role') == 'Secondary':
                        d_name += " (Secondary)"
                    
                    items_for_select.append(d_name)
                    item_map[d_name] = {'id': i['id'], 'name': i['name'], 'sup': sel_filter}
        else:
            cats = database_utils.fetch_distinct_categories(conn)
            sel_filter = st.selectbox("Select Category", cats, index=None)
            if sel_filter:
                items = database_utils.fetch_items_with_supplier_by_category(conn, sel_filter)
                for i in items:
                    d_name = f"{i['name']} (ID: {i['id']})"
                    items_for_select.append(d_name)
                    item_map[d_name] = {'id': i['id'], 'name': i['name'], 'sup': i['main_supplier']}
    
    with col2:
        sel_item = st.selectbox("Item", sorted(items_for_select), index=None, disabled=not items_for_select)
        qty = st.number_input("Qty", min_value=1, value=1, disabled=not items_for_select)
        
        if st.button("➕ Add"):
            if sel_item:
                data = item_map[sel_item]
                st.session_state.admin_custom_order_items.append({'item_id': data['id'], 'name': data['name'], 'quantity': qty, 'supplier': data['sup']})
                st.success(f"Added {data['name']}")
                st.rerun()

    st.subheader("2. Current List")
    if st.session_state.admin_custom_order_items:
        st.dataframe(pd.DataFrame(st.session_state.admin_custom_order_items), use_container_width=True)
        if st.button("Clear List"):
            st.session_state.admin_custom_order_items = []
            st.rerun()

    st.subheader("3. Generate")
    notes = st.text_area("Order Notes")
    if st.button("✅ Generate Orders", type="primary", disabled=not st.session_state.admin_custom_order_items):
        grouped = {}
        for i in st.session_state.admin_custom_order_items:
            grouped.setdefault(i['supplier'], []).append(i)
        
        success_count = 0
        for sup, items in grouped.items():
            try:
                oid = database_utils.insert_order(conn, sup, notes=notes)
                if oid:
                    for i in items:
                        database_utils.insert_order_item(conn, oid, i['item_id'], i['quantity'])
                    conn.commit()
                    success_count += 1
            except Exception as e:
                conn.rollback()
                st.error(f"Failed for {sup}: {e}")
        
        if success_count:
            st.success(f"Generated {success_count} orders.")
            st.session_state.admin_custom_order_items = []
            st.rerun()

elif choice == "Delete/Cancel Order":
     st.header("🗑️ Delete or Cancel Order")
     all_orders = database_utils.fetch_all_orders(conn)
     
     if all_orders:
        opts = {f"ID: {o['order_id']} - {o['supplier_name']} ({o['status']})": o['order_id'] for o in all_orders}
        sel_key = st.selectbox("Choose Order:", list(opts.keys()), index=None)
        
        if sel_key:
            oid = opts[sel_key]
            order_data = next(o for o in all_orders if o['order_id'] == oid)
            st.write(f"**Status:** {order_data['status']}")
            
            if order_data['status'] not in ['Cancelled', 'Delivered']:
                if st.button(f"Cancel Order {oid}"):
                    if database_utils.update_order_status_and_notes(conn, oid, "Cancelled", "Cancelled by Admin"):
                        conn.commit()
                        st.success("Cancelled.")
                        st.rerun()
            
            st.divider()
            st.warning("Deleting is permanent.")
            if st.checkbox(f"Confirm delete {oid}"):
                if st.button("Permanently Delete", type="primary"):
                    if database_utils.delete_order_by_id(conn, oid):
                        st.success("Deleted.")
                        st.rerun()

elif choice == "Manage Users":
    st.header("👥 Manage Staff Access")
    
    users = database_utils.fetch_all_staff_users(conn)
    if users:
        df_users = pd.DataFrame(users)
        st.dataframe(
            df_users[['username', 'is_admin', 'is_active']], 
            use_container_width=True, 
            hide_index=True,
            column_config={
                "is_admin": st.column_config.CheckboxColumn("Admin Access?", disabled=True),
                "is_active": st.column_config.CheckboxColumn("Active?", disabled=True)
            }
        )
    
    st.divider()
    col_add, col_manage = st.columns(2)
    
    # --- ADD NEW USER ---
    with col_add:
        st.subheader("➕ Add New Staff")
        with st.form("add_user_form"):
            new_user_name = st.text_input("Username")
            new_user_pin = st.text_input("Set PIN (4 digits)", max_chars=4)
            # New Checkbox
            is_admin_check = st.checkbox("Grant Admin Access?", help="Allows access to this Admin Panel")
            
            if st.form_submit_button("Create User"):
                if len(new_user_pin) != 4 or not new_user_pin.isdigit():
                    st.error("PIN must be 4 digits.")
                elif not new_user_name:
                    st.error("Username is required.")
                else:
                    if database_utils.manage_staff_user(conn, 'add', username=new_user_name, pin=new_user_pin, is_admin=is_admin_check):
                        st.success(f"User '{new_user_name}' created!")
                        st.rerun()
                    else:
                        st.error("Failed. Username might exist.")

    # --- MANAGE EXISTING ---
    with col_manage:
        st.subheader("⚙️ Edit User")
        
        user_opts = {f"{u['username']} (Admin: {'✅' if u['is_admin'] else '❌'})": u for u in users}
        selected_user_key = st.selectbox("Select Staff Member:", list(user_opts.keys()), index=None)
        
        if selected_user_key:
            target_user = user_opts[selected_user_key]
            
            # 1. Promote/Demote
            st.write("**Access Level:**")
            col_role1, col_role2 = st.columns(2)
            if target_user['is_admin']:
                if col_role1.button("📉 Demote to Staff"):
                    # Safety: Don't demote yourself
                    if target_user['username'] == st.session_state.current_user['username']:
                        st.error("You cannot remove your own Admin rights.")
                    else:
                        database_utils.manage_staff_user(conn, 'toggle_role', user_id=target_user['user_id'], is_admin=0)
                        st.rerun()
            else:
                if col_role2.button("📈 Promote to Admin"):
                    database_utils.manage_staff_user(conn, 'toggle_role', user_id=target_user['user_id'], is_admin=1)
                    st.rerun()

            st.divider()

            # 2. Reset PIN
            with st.expander("🔑 Reset PIN"):
                new_pin_reset = st.text_input("New PIN", max_chars=4, key="reset_pin_input")
                if st.button("Update PIN"):
                    if len(new_pin_reset) == 4 and new_pin_reset.isdigit():
                        database_utils.manage_staff_user(conn, 'update_pin', user_id=target_user['user_id'], pin=new_pin_reset)
                        st.success("PIN Updated.")
                    else:
                        st.error("Invalid PIN.")

            # 3. Delete
            with st.expander("🗑️ Delete User"):
                if target_user['username'] == st.session_state.current_user['username']:
                    st.info("You cannot delete yourself.")
                else:
                    if st.button("Confirm Delete", type="primary"):
                        database_utils.manage_staff_user(conn, 'delete', user_id=target_user['user_id'])
                        st.success("Deleted.")
                        st.rerun()
                        

elif choice == "🤖 AI Allergen Extractor":
    st.header("🏷️ Product Allergen Scanner (Contains & May Contain)")
    
    # 1. Select Product (with allergen-review status filter)
    if 'ai_scan_results' not in st.session_state: st.session_state.ai_scan_results = {} # Changed to dict

    inv_all = database_utils.fetch_inventory_with_allergens(conn)
    buckets = {it['id']: _allergen_review_bucket(it.get('allergens_last_reviewed')) for it in inv_all}
    n_never = sum(1 for b in buckets.values() if b == 'never')
    n_stale = sum(1 for b in buckets.values() if b == 'stale')
    n_reviewed = sum(1 for b in buckets.values() if b in ('fresh', 'stale'))

    m1, m2, m3 = st.columns(3)
    m1.metric("✅ Reviewed Items", n_reviewed)
    m2.metric("⚠️ Never Reviewed", n_never)
    m3.metric("🕰️ Stale (> 90 Days)", n_stale)

    filter_status = st.radio("Filter Product List:",
                             ["All Items", "⚠️ Never Reviewed Only",
                              "🕰️ Needs Review (Never or > 90 Days)"],
                             horizontal=True)

    # Build dropdown options: label carries the review status at a glance.
    item_map = {}
    item_display_list = []
    for it in sorted(inv_all, key=lambda x: x['name']):
        b = buckets[it['id']]
        if filter_status == "⚠️ Never Reviewed Only" and b != 'never':
            continue
        if filter_status == "🕰️ Needs Review (Never or > 90 Days)" and b == 'fresh':
            continue
        last_rev = (it.get('allergens_last_reviewed') or '').strip()
        if b == 'never':
            suffix = " — ⚠️ Never reviewed"
        else:
            suffix = f" — ✅ {last_rev[:10]}"
        label = f"{it['name']} (ID: {it['id']}){suffix}"
        item_display_list.append(label)
        item_map[label] = it['id']

    if not item_display_list:
        st.info("No items match this filter — everything here is up to date. 🎉")

    # Labels change after a save (⚠️ -> ✅) and the filter can drop the
    # current pick; a stale widget value not in the new options list would
    # crash the selectbox, so drop it before rendering.
    if st.session_state.get("allergen_prod_select") not in item_display_list:
        st.session_state.pop("allergen_prod_select", None)

    selected_display = st.selectbox("1. Select Product:", item_display_list,
                                    index=None, key="allergen_prod_select")
    
    selected_item_id = None
    curr_contains = []
    curr_may_contain = []

    if selected_display:
        selected_item_id = item_map[selected_display]
        # Fetch existing data
        curr_contains = database_utils.fetch_allergens_for_item(conn, selected_item_id)
        curr_may_contain = database_utils.fetch_may_contain_allergens_for_item(conn, selected_item_id)
        
        st.caption(f"**Current Main:** {', '.join(curr_contains) if curr_contains else 'None'}")
        st.caption(f"**Current May Contain:** {', '.join(curr_may_contain) if curr_may_contain else 'None'}")
        item_rev = database_utils.search_item_by_id(conn, selected_item_id)
        last_rev = (item_rev or {}).get('allergens_last_reviewed')
        st.caption(f"**Last Reviewed:** {last_rev if last_rev else '⚠️ Never reviewed'}")

    st.divider()

    # 2. Upload & Scan (3-pass pipeline)
    img = st.file_uploader("2. Upload Label", type=["png", "jpg", "webp"])

    if img and st.button("🧪 Analyze Label"):
        b64 = streamlit_image_to_base64(img)
        master_names = [a['name'] for a in database_utils.fetch_all_allergen_details(conn)]

        # Pass 1: pure vision label read (no master list in the prompt)
        with st.spinner("Pass 1/3 — reading the label..."):
            pass1, err = ocr_allergen_label(b64)
        if err:
            st.error(f"Label read failed: {err}")
            pass1 = None

        matched = None
        if pass1:
            # Pass 2: text-only normalisation to canonical master names
            with st.spinner("Pass 2/3 — matching to standard allergen names..."):
                matched, err = match_allergens(pass1, master_names)
            if err:
                st.error(f"Name matching failed: {err}")

        verified = None
        needs_review = False
        if pass1 and matched:
            # Pass 3: independent verification (its lists are FINAL)
            with st.spinner("Pass 3/3 — verifying against the label text..."):
                verified, err = verify_allergens(pass1, matched, master_names)
            if err:
                st.error(f"Verification failed ({err}) — using unverified matches.")
                needs_review = True
            else:
                # Review flag: low confidence, or Pass 3 changed the lists
                def canon(group):
                    return sorted({str(m['allergen']) for m in matched.get(group, [])
                                   if isinstance(m, dict) and m.get('allergen')})
                if str(verified.get('confidence', '')).lower() == 'low':
                    needs_review = True
                if sorted(v.lower() for v in verified.get('contains', [])) != sorted(x.lower() for x in canon('contains')) or \
                   sorted(v.lower() for v in verified.get('may_contain', [])) != sorted(x.lower() for x in canon('may_contain')):
                    needs_review = True

        if pass1 and matched and verified:
            # Deterministic safety net: regex-scan the transcription itself
            # and UNION anything the AI missed. A regex hit on already
            # transcribed text is ground truth — never rely solely on the LLM.
            lt = pass1.get("label_text") or {}
            ing_text = f"{lt.get('ingredients', '')} {lt.get('allergy_statement', '')}"
            det_contains = deterministic_allergen_scan(ing_text, master_names)
            det_may = deterministic_allergen_scan(lt.get('precautionary_statement', ''), master_names)

            ai_contains = [a.strip().title() for a in verified.get("contains", [])]
            ai_may = [a.strip().title() for a in verified.get("may_contain", [])]
            extra_c = sorted(det_contains - {x for x in ai_contains if x.lower() in {y.lower() for y in det_contains}})
            extra_m = sorted(det_may - {x for x in ai_may if x.lower() in {y.lower() for y in det_may}})
            if extra_c or extra_m:
                needs_review = True  # AI missed printed allergens — flag loudly
            final_contains = sorted(set(ai_contains) | det_contains, key=str.lower)
            final_may = sorted(set(ai_may) | det_may, key=str.lower)

            # Unmatched phrases stay visible so admin can add master names
            unmatched = [m['phrase'] for grp in ('contains', 'may_contain')
                         for m in matched.get(grp, [])
                         if isinstance(m, dict) and not m.get('allergen')]
            st.session_state.ai_scan_results = {
                "contains": final_contains,
                "may_contain": final_may,
                "unmatched": unmatched,
                "needs_review": needs_review,
                "label_text": lt,
                "safety_net_additions": extra_c + extra_m,
            }
            msg = "3-pass analysis complete."
            if extra_c or extra_m:
                msg += f" 🛡️ Safety net added missed allergens: {', '.join(extra_c + extra_m)}."
            if needs_review:
                msg += " 🔍 Low confidence or Pass-3 corrections — check the lists."
            st.session_state.allergen_scan_msg = msg
            st.rerun()

    if st.session_state.get('allergen_scan_msg'):
        scan = st.session_state.ai_scan_results
        if scan.get('needs_review'):
            st.warning(st.session_state.allergen_scan_msg)
        else:
            st.success(st.session_state.allergen_scan_msg)
        lt = scan.get('label_text') or {}
        with st.expander("📝 What the AI read on the label"):
            st.markdown(f"**Ingredients:** {lt.get('ingredients') or '—'}")
            st.markdown(f"**Contains statement:** {lt.get('allergy_statement') or '—'}")
            st.markdown(f"**Precautionary warning:** {lt.get('precautionary_statement') or '—'}")
        if scan.get('unmatched'):
            st.info("Not matched to a standard name (add it under View All Allergens if needed): "
                    + ", ".join(scan['unmatched']))

    # 3. Review & Save
    if selected_item_id:
        st.subheader("3. Confirm & Save")
        
        all_master_algs = [a['name'] for a in database_utils.fetch_all_allergen_details(conn)]
        
        # --- BOX 1: CONTAINS ---
        ai_contains = st.session_state.ai_scan_results.get("contains", [])
        # Merge options
        opts_contains = sorted(list(set(all_master_algs + ai_contains + curr_contains)))
        # Default selection (AI wins if present, else DB)
        def_contains = ai_contains if ai_contains else curr_contains
        # Ensure default is in options
        def_contains = [x for x in def_contains if x in opts_contains] + [x for x in def_contains if x not in opts_contains]
        
        final_contains = st.multiselect("⚠️ Contains (Main):", options=opts_contains, default=def_contains)

        # --- BOX 2: MAY CONTAIN ---
        ai_may = st.session_state.ai_scan_results.get("may_contain", [])
        opts_may = sorted(list(set(all_master_algs + ai_may + curr_may_contain)))
        def_may = ai_may if ai_may else curr_may_contain
        def_may = [x for x in def_may if x in opts_may] + [x for x in def_may if x not in opts_may]
        
        final_may = st.multiselect("❓ May Contain (Traces):", options=opts_may, default=def_may)

        if st.button("💾 Save Both Lists", type="primary"):
            ok1 = database_utils.update_item_allergens(conn, selected_item_id, final_contains)
            ok2 = database_utils.update_item_may_contain_allergens(conn, selected_item_id, final_may)
            
            if ok1 and ok2:
                # Saving counts as a review — even zero-allergen confirmations.
                database_utils.mark_item_allergens_reviewed(conn, selected_item_id)
                st.success(f"Updated allergens for '{selected_display}'!")
                st.session_state.ai_scan_results = {}
                st.session_state.allergen_scan_msg = ""
                st.rerun()
            else:
                st.error("Database update failed.")

elif choice == "👁️ View All Allergens":
    tab_audit, tab_master = st.tabs(["📋 Inventory Allergen Review Status",
                                     "👁️ Master Allergen List"])

    # --- Inventory Allergen Review Status (audit / review queue) ---
    with tab_audit:
        audit_rows = database_utils.fetch_inventory_with_allergens(conn)
        if not audit_rows:
            st.info("No items found.")
        else:
            df_audit = pd.DataFrame(audit_rows)
            df_audit['allergens_last_reviewed'] = df_audit['allergens_last_reviewed'].fillna('')
            df_audit['contains_allergens'] = df_audit['contains_allergens'].fillna('')
            df_audit['may_contain_allergens'] = df_audit['may_contain_allergens'].fillna('')
            df_audit['_bucket'] = df_audit['allergens_last_reviewed'].map(_allergen_review_bucket)

            audit_filter = st.radio("Filter:",
                                    ["All Items", "⚠️ Never Reviewed",
                                     "🕰️ Older than 90 Days", "✅ Up to Date"],
                                    horizontal=True, key="audit_filter")
            bucket_map = {"⚠️ Never Reviewed": 'never',
                          "🕰️ Older than 90 Days": 'stale',
                          "✅ Up to Date": 'fresh'}
            if audit_filter in bucket_map:
                df_audit = df_audit[df_audit['_bucket'] == bucket_map[audit_filter]]

            # Review queue order: NEVER first, then oldest timestamps first.
            df_audit['_sort_key'] = df_audit['allergens_last_reviewed'].map(
                lambda v: v if v else '0000-00-00')
            df_audit = df_audit.sort_values(['_sort_key', 'name'])
            df_audit['allergens_last_reviewed'] = df_audit['allergens_last_reviewed'].replace('', '⚠️ NEVER')

            if df_audit.empty:
                st.info("No items match this filter.")
            else:
                st.dataframe(
                    df_audit[['name', 'category', 'main_supplier',
                              'contains_allergens', 'may_contain_allergens',
                              'allergens_last_reviewed']],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "name": "Item Name",
                        "category": "Category",
                        "main_supplier": "Supplier",
                        "contains_allergens": "⚠️ Contains",
                        "may_contain_allergens": "❓ May Contain",
                        "allergens_last_reviewed": st.column_config.TextColumn(
                            "🕒 Last Reviewed",
                            help="When this product's allergen label was last checked"
                        ),
                    }
                )

    # --- Master Allergen List ---
    with tab_master:
        # --- Display Current List ---
        allergens = database_utils.fetch_all_allergen_details(conn)
        if allergens:
            df_alg = pd.DataFrame(allergens)
            # Show ID clearly so user knows what to merge
            st.dataframe(df_alg, use_container_width=True, hide_index=True)
        else:
            st.info("No allergens found.")

        st.divider()

        # --- Add New ---
        with st.expander("➕ Add New Allergen", expanded=False):
            with st.form("add_alg"):
                new_alg = st.text_input("New Allergen Name")
                if st.form_submit_button("Add"):
                    if database_utils.add_new_allergen(conn, new_alg):
                        st.success("Added.")
                        st.rerun()

        # --- Merge / Cleanup Tool ---
        with st.expander("🛠️ Cleanup / Merge Duplicates", expanded=True):
            st.write("Use this to merge duplicates (e.g., merge 'Egg' into 'Eggs').")

            if not allergens:
                st.warning("No data to merge.")
            else:
                # Create a dictionary for the dropdowns
                alg_opts = {f"{a['name']} (ID: {a['allergen_id']})": a['allergen_id'] for a in allergens}

                col1, col2 = st.columns(2)
                with col1:
                    keep_sel = st.selectbox("Keep this one (Official):", options=list(alg_opts.keys()), key="merge_keep")
                with col2:
                    remove_sel = st.selectbox("Remove this duplicate:", options=list(alg_opts.keys()), key="merge_remove")

                if st.button("🔀 Merge Allergens", type="primary"):
                    if keep_sel and remove_sel:
                        keep_id = alg_opts[keep_sel]
                        remove_id = alg_opts[remove_sel]

                        if keep_id == remove_id:
                            st.error("Cannot merge an allergen into itself.")
                        else:
                            # Perform the merge using the new utility function
                            if database_utils.merge_allergen_ids(conn, keep_id, remove_id):
                                st.success(f"Successfully merged '{remove_sel}' into '{keep_sel}'.")
                                st.rerun()
                            else:
                                st.error("Merge failed. Check logs.")
                            
elif choice == "📊 Waste Analysis":
    st.header("❄️ Stock Loss & Waste Dashboard")
    
    # 1. Fetch Data (Stock Failures only — legacy 2Good2Go rows filtered in SQL)
    raw_data = database_utils.fetch_waste_log_report(conn)
    
    if not raw_data:
        st.info("No stock losses logged yet.")
    else:
        df = pd.DataFrame(raw_data)
        
        # 2. KPIs (Key Performance Indicators)
        col1, col2, col3 = st.columns(3)
        col1.metric("💸 Total Value Lost", f"£{df['calculated_value'].sum():,.2f}", delta="-Lost", delta_color="inverse")
        col2.metric("📦 Total Boxes Lost", f"{int(df['quantity_entered'].sum()):,}")
        col3.metric("🧾 Total Incidents Logged", f"{len(df):,}")
        
        st.divider()
        
        # 3. Visuals
        tab1, tab2 = st.tabs(["📈 Charts", "📝 Detailed Log"])
        
        with tab1:
            c1, c2 = st.columns(2)
            
            with c1:
                st.subheader("Loss by Category")
                if not df.empty:
                    # Donut chart showing where the losses are happening
                    fig_cat = px.pie(df, names='category', values='calculated_value', hole=0.4, title="Value Lost by Category")
                    st.plotly_chart(fig_cat, use_container_width=True)
            
            with c2:
                st.subheader("Top Lost Items by Value (£)")
                if not df.empty:
                    # Bar chart of the items costing the most when lost
                    by_item = (df.groupby('item_name', dropna=False)['calculated_value']
                               .sum().sort_values(ascending=False).head(10).reset_index())
                    fig_bar = px.bar(
                        by_item, x='calculated_value', y='item_name', orientation='h',
                        color_discrete_map={None: '#e74c3c'},
                        title="Top 10 Items by Value Lost"
                    ).update_yaxis(categoryorder="total ascending")
                    st.plotly_chart(fig_bar, use_container_width=True)

        with tab2:
            st.subheader("Transaction Log & Management")
            # Clean up dataframe for display
            display_df = df[['log_id', 'time_str', 'event_type', 'item_name', 'quantity_entered', 'unit_type', 'calculated_value', 'logged_by']].copy()
            display_df.columns = ['ID', 'Time', 'Type', 'Item', 'Qty', 'Unit', 'Value (£)', 'Staff']
            
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Value (£)": st.column_config.NumberColumn(format="£%.2f"),
                    "Type": st.column_config.TextColumn(width="medium"),
                }
            )

            # --- DELETE FUNCTIONALITY ---
            st.divider()
            st.write("### 🗑️ Delete Erroneous Entry")
            st.info("Deleting an entry will remove it from reports. If you are deleting a 'Stock Failure', the lost stock will be automatically added back to inventory.")

            if not raw_data:
                st.caption("No entries to delete.")
            else:
                # Create a list for the dropdown
                # We sort by ID descending so the newest (the mistake) is at the top
                sorted_rows = sorted(raw_data, key=lambda x: x['log_id'], reverse=True)
                opts = {f"ID {r['log_id']}: {r['item_name']} ({r['event_type']} - £{r['calculated_value']:.2f})": r['log_id'] for r in sorted_rows}
                
                sel_delete = st.selectbox("Select Entry to Delete:", list(opts.keys()), index=None)
                
                if sel_delete:
                    log_id_to_del = opts[sel_delete]
                    if st.button(f"🔴 Confirm Delete (ID: {log_id_to_del})", type="primary"):
                        success, msg = database_utils.delete_waste_log_entry(conn, log_id_to_del)
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

elif choice == "📊 Spend Analysis":
    st.header("📊 Spend Analysis")
    
    # Updated to 3 Tabs
    tab1, tab2, tab3 = st.tabs(["💰 Total Spend", "🧀 Spend by Item", "📂 Spend by Category"])
    
    # --- TAB 1: TOTAL OVERVIEW ---
    with tab1:
        raw_data = database_utils.fetch_spending_data_over_time(conn)
        if not raw_data:
            st.info("No delivered orders found yet.")
        else:
            df = pd.DataFrame(raw_data)
            df['date'] = pd.to_datetime(df['date'])
            
            col_ctrl1, col_ctrl2 = st.columns([1, 3])
            with col_ctrl1:
                time_frame = st.radio("Group By:", ["Weekly", "Monthly"], horizontal=True, key="time_frame_overview")
                
            if time_frame == "Weekly":
                chart_data = df.resample('W-MON', on='date')['total_cost'].sum().reset_index()
                chart_data['date_str'] = chart_data['date'].dt.strftime('%Y-%m-%d (W%W)')
            else:
                chart_data = df.resample('ME', on='date')['total_cost'].sum().reset_index()
                chart_data['date_str'] = chart_data['date'].dt.strftime('%b %Y')

            # Chronological row order — the X-axis labels below are text
            # categories, so without this sort Plotly would order them
            # alphabetically ("2026-April" before "2026-August"...).
            chart_data = chart_data.sort_values('date')

            total_spend = chart_data['total_cost'].sum()
            avg_spend = chart_data['total_cost'].mean()
            
            m1, m2 = st.columns(2)
            m1.metric("Total Spend", f"£{total_spend:,.2f}")
            m2.metric("Average Spend", f"£{avg_spend:,.2f}")
            
            fig_total = px.bar(
                chart_data,
                x='date_str',
                y='total_cost',
                labels={'date_str': 'Period', 'total_cost': 'Total Spend (£)'},
                text_auto='.2s'
            )
            fig_total.update_traces(
                marker_color='#4CAF50',
                hovertemplate='<b>%{x}</b><br>Total Spend: £%{y:,.2f}<extra></extra>'
            )
            fig_total.update_layout(
                xaxis={'categoryorder': 'array', 'categoryarray': chart_data['date_str'].tolist(), 'title': None},
                yaxis_title='Total Spend (£)',
                height=420
            )
            st.plotly_chart(fig_total, use_container_width=True)

    # --- TAB 2: ITEM BREAKDOWN (Plotly Grouped Bar) ---
    with tab2:
        st.subheader("Monthly Spend per Item (Side-by-Side)")
        item_data = database_utils.fetch_item_spending_details(conn)
        
        if not item_data:
            st.info("No item data available.")
        else:
            df_item = pd.DataFrame(item_data)
            df_item['date'] = pd.to_datetime(df_item['date'])
            df_item['Month'] = df_item['date'].dt.strftime('%Y-%m')

            # Calculate Defaults (Top 5 by Volume)
            df_totals = df_item.groupby('item_name')[['qty', 'cost']].sum()
            if not df_totals.empty:
                top_5_by_volume = df_totals.sort_values('qty', ascending=False).head(5).index.tolist()
                all_items = sorted(df_totals.index.tolist())
            else:
                top_5_by_volume = []
                all_items = []

            st.caption("Select items to compare. Defaults to your Top 5 'Most Ordered' items.")
            selected_items = st.multiselect("Filter Items:", options=all_items, default=top_5_by_volume)

            if selected_items:
                df_chart = df_item[df_item['item_name'].isin(selected_items)].copy()
                df_chart = df_chart.groupby(['Month', 'item_name'], as_index=False)['cost'].sum()
                df_chart = df_chart.sort_values('Month')

                fig = px.bar(
                    df_chart, x='Month', y='cost', color='item_name', barmode='group',
                    text_auto='.2s', labels={'cost': 'Cost (£)', 'item_name': 'Item', 'Month': 'Month'},
                    # Lock X order to chronological month sequence regardless of
                    # which item appears first (Month is a text category here).
                    category_orders={'Month': sorted(df_chart['Month'].unique())}
                )
                fig.update_traces(hovertemplate='<b>%{x}</b><br>%{fullData.name}: £%{y:,.2f}<extra></extra>')
                fig.update_layout(xaxis_title=None, yaxis_title="Total Cost (£)", legend_title_text="Items", height=500)
                st.plotly_chart(fig, use_container_width=True)

    # --- TAB 3: CATEGORY SPEND (New Circular Chart) ---
    with tab3:
        st.subheader("Total Spend by Category")
        
        cat_data = database_utils.fetch_spend_by_category(conn)
        
        if not cat_data:
            st.info("No category data available.")
        else:
            df_cat = pd.DataFrame(cat_data)
            
            # Create Donut Chart
            # hole=0.4 makes it a "Donut" (easier to read than Pie)
            fig_cat = px.pie(
                df_cat, 
                values='cost', 
                names='category', 
                hole=0.4,
                title="Cumulative Spend Distribution"
            )
            
            # Update labels to show Percentage AND Value
            fig_cat.update_traces(textposition='inside', textinfo='percent+label')
            fig_cat.update_layout(
                annotations=[dict(text='Total', x=0.5, y=0.5, font_size=20, showarrow=False)]
            )
            
            c1, c2 = st.columns([2, 1])
            
            with c1:
                st.plotly_chart(fig_cat, use_container_width=True)
            
            with c2:
                st.write("**breakdown:**")
                st.dataframe(
                    df_cat.style.format({"cost": "£{:.2f}"}), 
                    use_container_width=True,
                    hide_index=True
                )

elif choice == "🧠 AI Settings":
    st.header("🧠 AI Settings (Ollama)")
    st.caption("Endpoint, model and timeout used by ALL AI features in both the Staff and Admin apps. Changes apply immediately — no restart.")
    env_set = [k for k in ("IMS_OLLAMA_URL", "IMS_VISION_MODEL", "IMS_AI_TIMEOUT_S", "IMS_AI_NUM_CTX") if os.environ.get(k)]
    if env_set:
        st.caption(f"Environment defaults active for: {', '.join(env_set)} (saving here overrides them).")

    ai = database_utils.get_ai_settings(conn)

    with st.form("ai_settings_form"):
        new_url = st.text_input("Ollama base URL (no /api/generate)", value=ai["base_url"])
        new_timeout = st.number_input("Timeout per AI call (seconds)", min_value=10, max_value=600, value=ai["timeout_s"], step=10)
        new_num_ctx = st.number_input("Context Window (num_ctx tokens)", min_value=2048, max_value=32768, value=ai["num_ctx"], step=1024)

        # Live model list from the server, if reachable
        models, models_err = database_utils.ollama_list_models(new_url)
        if models:
            model_options = sorted(models)
            try:
                default_idx = model_options.index(ai["model"])
            except ValueError:
                default_idx = 0
            new_model = st.selectbox("Model (detected on server)", model_options, index=default_idx)
        else:
            st.warning(f"Could not list models: {models_err}")
            new_model = st.text_input("Model (type manually)", value=ai["model"])

        c_save, c_test = st.columns(2)
        saved = c_save.form_submit_button("💾 Save settings", type="primary")
        tested = c_test.form_submit_button("🔌 Test connection")

    if saved:
        ok1 = database_utils.set_setting(conn, "ollama_base_url", new_url.strip())
        ok2 = database_utils.set_setting(conn, "vision_model", new_model.strip())
        ok3 = database_utils.set_setting(conn, "ai_timeout_s", int(new_timeout))
        ok4 = database_utils.set_setting(conn, "ai_num_ctx", int(new_num_ctx))
        if ok1 and ok2 and ok3 and ok4:
            st.success("Saved. Both apps now use these settings.")
        else:
            st.error("Some settings failed to save — check the server log.")
    if tested:
        with st.spinner(f"Testing {new_model} at {new_url}..."):
            ok, msg = database_utils.ollama_test_connection(new_url, new_model, timeout=min(60, int(new_timeout)))
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    st.divider()
    st.markdown(
        f"**Currently active:** `{ai['base_url']}` | model `{ai['model']}` | timeout {ai['timeout_s']}s | context {ai['num_ctx']} tokens")

elif choice == "Backup & Restore":
    st.header("💾 System Backup & Restore")
    
    tab_backup, tab_restore = st.tabs(["📤 Export (Backup)", "📥 Import (Restore)"])
    
    # --- EXPORT ---
    with tab_backup:
        st.info("Download a full copy of your database (Items, Orders, Users, History).")
        
        if st.button("Generate SQL Dump"):
            with st.spinner("Generating backup..."):
                sql_script = database_utils.export_database_to_sql(conn)
                
                if sql_script:
                    # Create timestamped filename
                    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
                    filename = f"ims_backup_{ts}.sql"
                    
                    # Convert to bytes for download
                    b_data = sql_script.encode('utf-8')
                    
                    st.download_button(
                        label="📥 Click to Download Backup File",
                        data=b_data,
                        file_name=filename,
                        mime="application/sql"
                    )
                    st.success("Backup generated! Click above to save.")
                else:
                    st.error("Failed to generate backup.")

    # --- RESTORE ---
    with tab_restore:
        st.error("⚠️ DANGER ZONE: Restoring will WIPE all current data and replace it with the backup.")
        
        # 1. Upload
        uploaded_file = st.file_uploader("Upload .sql Backup File", type=["sql"])
        
        # 2. Security Check (Require PIN again for this destructive action)
        confirm_pin = st.text_input("Confirm Admin PIN to proceed:", type="password", max_chars=4, key="restore_pin")
        
        if st.button("🔴 EXECUTE RESTORE", type="primary"):
            if not uploaded_file:
                st.warning("Please upload a file first.")
            elif not confirm_pin:
                st.warning("Please enter your PIN.")
            else:
                # Check PIN — and the confirmed user MUST be an admin
                # (audit 2026-10-31 #3: any staff PIN used to pass this,
                # letting a staff member wipe/restore the whole DB).
                user = database_utils.verify_staff_login(conn, confirm_pin)
                if user and user['is_admin']:
                    # Proceed
                    try:
                        string_data = uploaded_file.getvalue().decode("utf-8")
                        with st.spinner("Restoring database... please wait..."):
                            if database_utils.fully_restore_database_from_sql(conn, string_data):
                                st.success("✅ Restore Complete! The system has been reset to the backup state.")
                                st.balloons()
                            else:
                                st.error("❌ Restore Failed. Check logs. Your old data might still be partially there or wiped.")
                    except Exception as e:
                        st.error(f"Error reading file: {e}")
                else:
                    st.error("❌ Incorrect PIN, or that PIN belongs to a non-admin account. Access Denied.")

# --- FOOTER ---
st.sidebar.markdown("---")
st.sidebar.info("ims_v3 · open source (MIT)")
