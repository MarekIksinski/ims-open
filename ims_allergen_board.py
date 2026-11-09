#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ims_allergen_board.py — Standalone FSA Allergen Display (customer-facing)

Read-only Streamlit app: SELECT only, no login, no PIN, no edit path.
Built for the counter: high-contrast cards + official FSA colour icons
from ./PNG/ so the screen can be turned round to the customer.
UK FSA / HSE allergen communication in mind:
- unreviewed items are NEVER shown as safe,
- 'May Contain' (traces) counts as a hit by default.

Run:  streamlit run ims_allergen_board.py
DB:   same SQLite as the staff/admin apps (IMS_DB_PATH overrides).
"""
import base64
import html as _html
import io
import logging
import os

import streamlit as st
import pandas as pd
from PIL import Image

import database_utils

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s')

st.set_page_config(page_title="Allergen Information Guide", layout="wide")

PNG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PNG")

# ---------------------------------------------------------------------------
# 1. Dynamic FSA icon resolver
# ---------------------------------------------------------------------------
# Canonical groups = the 14 UK-declared allergens, keyed to the FSA icon
# filenames in ./PNG/ (matched dynamically by token, never hardcoded paths).
GROUP_TOKENS = {  # group key -> filename tokens (normalized, substring)
    "gluten":     ("gluten", "cereal"),
    "crustaceans": ("crustacean",),
    "eggs":       ("egg",),
    "fish":       ("fish",),
    "lupin":      ("lupin",),
    "milk":       ("milk",),
    "molluscs":   ("mollusc",),
    "mustard":    ("mustard",),
    "peanuts":    ("peanut",),
    "sesame":     ("sesame",),
    "soy":        ("soybean", "soya", "soy"),
    "so2":        ("so2", "sulphur", "sulphit", "sulf"),
    "treenuts":   ("treenut", "tree nut"),
    "celery":     ("celery",),
}

UK14_COLUMNS = [  # display order + official FSA wording (matrix headers)
    ("gluten", "Cereals containing gluten"),
    ("crustaceans", "Crustaceans"),
    ("eggs", "Eggs"),
    ("fish", "Fish"),
    ("lupin", "Lupin"),
    ("milk", "Milk"),
    ("molluscs", "Molluscs"),
    ("mustard", "Mustard"),
    ("peanuts", "Peanuts"),
    ("sesame", "Sesame"),
    ("soy", "Soy"),
    ("so2", "Sulphur dioxide & sulphites"),
    ("treenuts", "Tree nuts"),
    ("celery", "Celery"),
]

# DB allergen name (normalized) -> group key. Covers UK-14 synonyms and the
# specific cereals/nuts/molluscs seen in the ims_v3 allergens table.
_SYNONYMS = {
    # cereals / gluten
    "wheat": "gluten", "barley": "gluten", "rye": "gluten", "oats": "gluten",
    "spelt": "gluten", "kamut": "gluten", "durum": "gluten", "gluten": "gluten",
    "cereals": "gluten", "cereal": "gluten", "cerealscontaininggluten": "gluten",
    "containingglutencereals": "gluten",
    # soy
    "soy": "soy", "soya": "soy", "soyabeans": "soy", "soybean": "soy",
    "soybeans": "soy", "soialecithin": "soy", "lecithinsoya": "soy",
    # sulphur dioxide / sulphites
    "sulphites": "so2", "sulfites": "so2", "sulphurdioxide": "so2",
    "sulfurdioxide": "so2", "so2": "so2", "metabisulphite": "so2",
    "metabisulfite": "so2",
    # milk
    "milk": "milk", "dairy": "milk", "lactose": "milk", "casein": "milk",
    "whey": "milk", "butter": "milk", "cream": "milk", "yoghurt": "milk",
    # eggs
    "egg": "eggs", "eggs": "eggs", "albumen": "eggs", "mayonnaise": "eggs",
    # mustard / celery / sesame
    "mustard": "mustard", "celery": "celery", "celeriac": "celery",
    "sesame": "sesame", "sesameseed": "sesame", "sesameseeds": "sesame",
    "tahini": "sesame",
    # fish
    "fish": "fish", "cod": "fish", "haddock": "fish", "salmon": "fish",
    "tuna": "fish", "anchovy": "fish", "herring": "fish", "mackerel": "fish",
    "smokedfish": "fish", "fishsauce": "fish", "worcestersiresauce": "fish",
    # crustaceans
    "crustaceans": "crustaceans", "crustacean": "crustaceans",
    "prawn": "crustaceans", "prawns": "crustaceans", "crab": "crustaceans",
    "lobster": "crustaceans", "shrimp": "crustaceans", "shrimps": "crustaceans",
    "krill": "crustaceans", "langoustine": "crustaceans", "crawfish": "crustaceans",
    # molluscs
    "molluscs": "molluscs", "mollusc": "molluscs", "snail": "molluscs",
    "snails": "molluscs", "squid": "molluscs", "cuttlefish": "molluscs",
    "octopus": "molluscs", "scallop": "molluscs", "scallops": "molluscs",
    "mussel": "molluscs", "mussels": "molluscs", "clam": "molluscs",
    "clams": "molluscs", "whelk": "molluscs", "whelks": "molluscs",
    "winkle": "molluscs", "winkles": "molluscs", "abalone": "molluscs",
    # peanuts
    "peanut": "peanuts", "peanuts": "peanuts", "groundnut": "peanuts",
    "groundnuts": "peanuts", "sataysauce": "peanuts",
    # tree nuts
    "nuts": "treenuts", "treenuts": "treenuts", "treenut": "treenuts",
    "almond": "treenuts", "almonds": "treenuts", "hazelnut": "treenuts",
    "hazelnuts": "treenuts", "walnut": "treenuts", "walnuts": "treenuts",
    "cashew": "treenuts", "cashews": "treenuts", "pistachio": "treenuts",
    "pistachios": "treenuts", "pecan": "treenuts", "pecans": "treenuts",
    "macadamia": "treenuts", "braziliannut": "treenuts",
    "braziliannuts": "treenuts", "coconut": "treenuts",
    # lupin
    "lupin": "lupin", "lupins": "lupin", "lupini": "lupin",
    "lupinflour": "lupin",
}

# substring fallback for names not in the exact table (order matters:
# 'peanut' is checked before the generic 'nut')
_FALLBACK_TOKENS = [
    ("soy", ("soy",)),
    ("so2", ("sulph", "sulf", "so2")),
    ("gluten", ("gluten", "cereal", "wheat", "barley", "rye", "oat", "spelt", "kamut", "durum")),
    ("milk", ("milk", "dairy", "lactose", "casein", "whey")),
    ("eggs", ("egg",)),
    ("mustard", ("mustard",)),
    ("celery", ("celery", "celeriac")),
    ("sesame", ("sesame", "tahini")),
    ("crustaceans", ("crustacean", "prawn", "crab", "lobster", "shrimp", "krill")),
    ("molluscs", ("mollusc", "snail", "squid", "cuttle", "octopus", "scallop",
                  "mussel", "clam", "whelk", "winkle")),
    ("peanuts", ("peanut", "groundnut")),
    ("treenuts", ("nut", "almond", "hazelnut", "walnut", "cashew", "pistachio",
                  "pecan", "macadamia", "coconut")),
    ("lupin", ("lupin",)),
    ("fish", ("fish", "salmon", "tuna", "cod", "haddock", "anchov", "herring", "mackerel")),
]


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


@st.cache_data(show_spinner=False)
def get_png_map() -> dict:
    """Scan ./PNG/ once and map each FSA icon file to its allergen group."""
    png_map = {}
    if not os.path.isdir(PNG_DIR):
        logging.warning(f"Allergen icon folder not found: {PNG_DIR}")
        return png_map
    for fname in sorted(os.listdir(PNG_DIR)):
        if not fname.lower().endswith(".png"):
            continue
        fkey = _norm(fname)
        for group, tokens in GROUP_TOKENS.items():
            if group in png_map:
                continue  # first match wins; keep deterministic via sorted()
            if any(_norm(t) in fkey for t in tokens):
                png_map[group] = fname
                break
    logging.info(f"Allergen icon map: {len(png_map)}/14 groups from {PNG_DIR}")
    return png_map


def get_allergen_icon_path(allergen_name: str):
    """Resolve a DB allergen name (any UK-14 synonym) to its FSA PNG path."""
    group = map_allergen_to_group(allergen_name)
    fname = get_png_map().get(group) if group else None
    return os.path.join(PNG_DIR, fname) if fname else None


def map_allergen_to_group(allergen_name: str):
    n = _norm(allergen_name)
    if not n:
        return None
    if n in _SYNONYMS:
        return _SYNONYMS[n]
    for group, tokens in _FALLBACK_TOKENS:
        if any(t in n for t in tokens):
            return group
    return None


@st.cache_data(show_spinner=False)
def get_icon_b64(fname: str) -> str:
    """Load an FSA PNG once, downscale to a retina thumbnail, base64 it.

    The official files are ~3700x3700 px (2.9 MB total); embedding them raw
    into every card would crawl. A 200px thumbnail renders crisp at 80-90px.
    """
    path = os.path.join(PNG_DIR, fname)
    try:
        img = Image.open(path)
        img.thumbnail((200, 200), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logging.error(f"Icon load failed for {fname}: {e}")
        return None


def icons_html(names, width: int = 84) -> str:
    """High-contrast row of FSA icons + the EXACT DB allergen label under
    each, so specific detail (e.g. 'Wheat') is never lost behind the
    generic Gluten icon.

    SECURITY: DB-sourced names are interpolated into HTML rendered with
    unsafe_allow_html=True on an UNAUTHENTICATED page — every value goes
    through _html.escape (stored-XSS fix, audit 2026-10-31 #1)."""
    parts = []
    for n in names:
        esc = _html.escape(n, quote=True)
        group = map_allergen_to_group(n)
        fname = get_png_map().get(group) if group else None
        b64 = get_icon_b64(fname) if fname else None
        if b64:
            parts.append(
                f'<div class="alcell"><img src="data:image/png;base64,{b64}" '
                f'width="{width}" height="{width}" alt="{esc}">'
                f'<div class="allbl">{esc}</div></div>')
        else:
            parts.append(
                f'<div class="alcell"><div class="almiss">❓</div>'
                f'<div class="allbl">{esc}</div></div>')
    return f'<div class="alrow">{"".join(parts)}</div>' if parts else ""


CSS = """
<style>
.alrow { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-start; }
.alcell { text-align:center; min-width:90px; }
.alcell img { border-radius:8px; background:#fff; }
.allbl { font-weight:700; font-size:0.95em; margin-top:4px; }
.almiss { font-size:2.2em; }
.banner { background:#0b3d91; color:#fff; padding:16px 20px; border-radius:12px; }
.banner h2 { margin:0 0 6px 0; color:#fff; }
.banner p { margin:0; font-size:1.02em; }
.stamp-ok { color:#1a7f37; font-weight:800; }
.stamp-bad { color:#c62828; font-weight:800; }
.section-avoid { border-left:8px solid #c62828; padding-left:10px; }
.section-safe { border-left:8px solid #1a7f37; padding-left:10px; }
.section-unver { border-left:8px solid #ef6c00; padding-left:10px; }
</style>
"""


# ---------------------------------------------------------------------------
# Data load (SELECT only — no login, no writes)
# ---------------------------------------------------------------------------
conn = database_utils.connect_to_db()
if conn is None:
    st.error("🔴 Cannot open the inventory database. Ask a manager to start the IMS.")
    st.stop()

items_raw = database_utils.fetch_inventory_with_allergens(conn)
if not items_raw:
    st.error("🔴 No items could be loaded from the database.")
    st.stop()


def split_csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


items = []
for it in items_raw:
    contains = split_csv(it.get("contains_allergens"))
    may = split_csv(it.get("may_contain_allergens"))
    reviewed = bool((it.get("allergens_last_reviewed") or "").strip())
    items.append({
        "id": it["id"],
        "name": it["name"],
        "category": it.get("category") or "Uncategorised",
        "contains": contains,
        "may_contain": may,
        "reviewed": reviewed,
        "reviewed_on": it.get("allergens_last_reviewed") or "",
        "contains_groups": {g for g in (map_allergen_to_group(n) for n in contains) if g},
        "may_groups": {g for g in (map_allergen_to_group(n) for n in may) if g},
    })

all_categories = sorted({i["category"] for i in items})
NON_FOOD_HINTS = {"cleaning", "packaging", "paperwork", "non-food", "nonfood", "supplies"}
food_categories = [c for c in all_categories if _norm(c) not in {_norm(x) for x in NON_FOOD_HINTS}]

# ---------------------------------------------------------------------------
# Top compliance banner
# ---------------------------------------------------------------------------
st.markdown(CSS, unsafe_allow_html=True)
st.markdown(
    """
    <div class="banner">
      <h2>🛡️ Allergen Information Guide</h2>
      <p>Please inform a member of staff of any allergies or intolerances before
      ordering. While we take every precaution, all food is prepared in a kitchen
      that handles allergens, so we cannot guarantee any item is 100% free from
      cross-contamination.</p>
    </div>
    """,
    unsafe_allow_html=True,
)
st.caption(f"{len(items)} items · {get_png_map().__len__()}/14 official FSA icons loaded")

# ---------------------------------------------------------------------------
# Quick controls
# ---------------------------------------------------------------------------
c1, c2 = st.columns([2, 3])
with c1:
    search = st.text_input("🔍 Search Item", placeholder="Type to filter…").strip().lower()
with c2:
    cats_sel = st.multiselect("📂 Category", all_categories, default=food_categories)

c3, c4 = st.columns([3, 2])
with c3:
    avoid_labels = st.multiselect(
        "🚫 Customer Avoids (Filter out Allergens)",
        [label for _, label in UK14_COLUMNS],
        help="Select what the customer is allergic to — suitable items are split out instantly.",
    )
with c4:
    traces_hit = st.toggle("Also exclude 'May Contain' (Traces)", value=True,
                           help="On (default): traces count as a hit for maximum customer safety.")
avoid_groups = {g for key, label in UK14_COLUMNS for g in [key] if label in avoid_labels}

mode = st.radio("View mode:", ("🏷️ Product Cards", "📋 Allergen Matrix"), horizontal=True)

# ---------------------------------------------------------------------------
# Filtering + safety-first bucketing
# ---------------------------------------------------------------------------
visible = [i for i in items if (not cats_sel or i["category"] in cats_sel)
           and (not search or search in i["name"].lower())]

unverified = [i for i in visible if not i["reviewed"]]           # NEVER in Suitable
avoiding, suitable = [], []
for i in visible:
    if i["reviewed"]:
        hit = bool(i["contains_groups"] & avoid_groups) or (traces_hit and bool(i["may_groups"] & avoid_groups))
        (avoiding if hit else suitable).append(i)

st.markdown(
    f"###### ✅ Suitable: **{len(suitable)}** &nbsp;·&nbsp; "
    f"❌ Avoid: **{len(avoiding)}** &nbsp;·&nbsp; "
    f"⚠️ Unverified: **{len(unverified)}**")

# ---------------------------------------------------------------------------
# Mode A — visual product cards
# ---------------------------------------------------------------------------
def render_card(i):
    with st.container(border=True):
        # SECURITY: name/category/reviewed_on are admin-editable DB values
        # rendered via unsafe_allow_html — escape them (audit 2026-10-31 #1).
        name = _html.escape(i["name"], quote=True)
        category = _html.escape(i["category"], quote=True)
        stamp = (f'<span class="stamp-ok">✅ Verified: '
                 f'{_html.escape(i["reviewed_on"], quote=True)}</span>'
                 if i["reviewed"] else
                 '<span class="stamp-bad">⚠️ ALLERGEN INFO NOT YET VERIFIED — '
                 'PLEASE CHECK PHYSICAL PACKAGING</span>')
        st.markdown(
            f"### {name} &nbsp; "
            f"<span style='font-size:0.55em;background:#eef2ff;border-radius:8px;"
            f"padding:3px 10px;'>{category}</span><br>{stamp}",
            unsafe_allow_html=True)
        if not i["reviewed"]:
            st.error("⚠️ ALLERGEN INFO NOT YET VERIFIED — PLEASE CHECK PHYSICAL PACKAGING")
            return
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**⚠️ CONTAINS (Ingredients)**", unsafe_allow_html=True)
            if i["contains"]:
                st.markdown(icons_html(i["contains"]), unsafe_allow_html=True)
            else:
                st.markdown("None declared in ingredients")
        with col2:
            st.markdown("**❓ MAY CONTAIN (Precautionary / Traces)**", unsafe_allow_html=True)
            if i["may_contain"]:
                st.markdown(icons_html(i["may_contain"]), unsafe_allow_html=True)
            else:
                st.markdown("None declared")


if mode == "🏷️ Product Cards":
    if not visible:
        st.info("No items match the current filters.")
    if avoid_groups:
        st.markdown('<div class="section-safe"></div>', unsafe_allow_html=True)
        st.subheader(f"✅ Suitable (no selected allergens — Verified) — {len(suitable)}")
        for i in suitable:
            render_card(i)
        st.markdown('<div class="section-avoid"></div>', unsafe_allow_html=True)
        st.subheader(f"❌ Avoid (contains or may contain selected allergens) — {len(avoiding)}")
        for i in avoiding:
            render_card(i)
        st.markdown('<div class="section-unver"></div>', unsafe_allow_html=True)
        st.subheader(f"⚠️ Unverified — Check Label — {len(unverified)}")
        for i in unverified:
            render_card(i)
    else:
        st.subheader(f"✅ Verified — {len(suitable)}")
        for i in suitable:
            render_card(i)
        st.markdown('<div class="section-unver"></div>', unsafe_allow_html=True)
        st.subheader(f"⚠️ Unverified — Check Label — {len(unverified)}")
        for i in unverified:
            render_card(i)

# ---------------------------------------------------------------------------
# Mode B — full FSA allergen matrix
# ---------------------------------------------------------------------------
else:
    rows = []
    for i in visible:
        row = {"Item": i["name"], "Category": i["category"],
               "Last Reviewed": i["reviewed_on"] or "❓ UNVERIFIED"}
        for group, label in UK14_COLUMNS:
            if not i["reviewed"]:
                row[label] = "❓ UNVERIFIED"
            elif group in i["contains_groups"]:
                row[label] = "⚠️ CONTAINS"
            elif group in i["may_groups"]:
                row[label] = "❓ MAY CONTAIN"
            else:
                row[label] = "—"
        rows.append(row)
    df = pd.DataFrame(rows, columns=["Item", "Category", "Last Reviewed"] +
                      [label for _, label in UK14_COLUMNS])
    st.dataframe(df, width="stretch", hide_index=True,
                 column_config={label: st.column_config.Column(width="small")
                                for _, label in UK14_COLUMNS})
    st.caption("⚠️ CONTAINS = declared in ingredients · ❓ MAY CONTAIN = "
               "precautionary traces · ❓ UNVERIFIED = allergen data not yet "
               "reviewed — always check the physical packaging.")

st.sidebar.markdown("---")
st.sidebar.caption("ims_v3 · Read-only display · No login required")
