# ims_v3 — Workflow Documentation

Everything the app does, per screen. Two Streamlit apps share one SQLite
database (`database_utils.py` holds all DB helpers and the schema):

- **Staff app** — `ims_web_app_user.py` (PIN login, staff users)
- **Admin app** — `ims_web_app_admin.py` (PIN login, admin users)

Both apps gate on login first: 4-digit PIN, verified against salted
scrypt hashes (`scrypt$<salt>$<hash>`; legacy SHA-256 hashes upgrade
automatically on next successful login — audit 2026-10-31 #2, soft
path 2026-11-07: that transition login returns `legacy_login: True`,
both apps then show a persistent 'PIN was stored insecurely, treat it
as compromised' banner until logout, and the admin panel lists any
accounts still on legacy hashes via
`database_utils.list_legacy_pin_accounts()` until all convert.
Regression: `tests/verify_legacy_pin_upgrade.py`.

A third standalone app exists: **FSA Allergen Board** —
`ims_allergen_board.py` (see §3).

---

## 1. Staff app (`ims_web_app_user.py`)

Sidebar groups: Inventory & Stock / Orders & Deliveries / Waste & Loss.

### 1.1 📊 Stuff We Have (inventory view)
Read-only stock table: name, qty, ⚠️ Contains / ❓ May Contain allergens,
category, expiry date. A "Quick view other tables" selectbox shows raw
`orders` / `order_items` rows for reference.

### 1.2 🔄 Update Available Stuff (quick qty edit)
Three-step drill-down: pick a **category button** → pick an **item** →
enter the new quantity → **Update**. Writes `available_quantity` directly.
Category/item selection persists in session state (`ua_cat`, `ua_item`).

### 1.3 🔍 Stock Check (counting wizard)
Pick a scope — **All Items / By Category / By Supplier** — and Start.
Items are walked one at a time (alphabetical) with a progress bar:
- **Confirm & Next** — writes the counted "Actual Qty" to stock.
- **Skip** — logs the item to `skipped_items` (unresolved) and moves on.
- **Quit** — ends the check early; progress is kept until Finish.
Items deleted mid-check are skipped with a warning. State: `sc_list`,
`sc_idx`, `sc_active`.

### 1.4 📊 Resolve Skipped Items
Lists unresolved skips from a Stock Check. Pick one, enter the correct
quantity → **Resolve** updates stock and marks the skip resolved. Items
deleted since the check show an error and cannot be resolved by qty.

### 1.5 📝 Stuff We Want (generate orders)
`get_items_to_order` filters STRICTLY by physical deficit: any item with
`desired_quantity > available_quantity` appears, NEVER hidden because an
open order (`status IN ('Generated', 'Partially Delivered')`) exists —
a fresh Stock Check must always resurface the deficit. Per-line pending
on open orders = `MAX(0, ordered − received)` is returned as
`pending_order_quantity` for INFORMATIONAL display only: rows with
pending stock show a `⚠️ Note: N already on an open order` warning. The
default order qty is the raw physical shortfall,
`desired − available` (pack rounding was retired with the pack_size UI).
Flow:
1. Items are grouped into one form **per main supplier**.
2. Adjust quantities → **Generate Order** → inserts an `orders` row
   (status Open) + `order_items` rows, commits.
3. A plain-text order sheet (Supplier / Date / Name: Qty lines) is kept
   in session memory (`generated_orders_map`) with a **Download Order
   Text** button per supplier.
4. "Start New Order for <supplier>" or the global **Reset All Order
   Forms** button clears the memory and shows pending items again.

**Stale-widget rule:** `invalidate_order_form_state()` (clears
`generated_orders_map` + every `ord_*` widget key) is called whenever
stock changes elsewhere — Stock Check **Confirm & Next** / **Finish**,
Update Available Stuff **Update**, Resolve Skipped **Resolve**, Log
Stock Loss success — so `st.number_input` never keeps a pre-stock-check
quantity via session_state.

### 1.6 🚚 Stuff We Got (receive delivery — AI 3-pass scan + confirm)
The core receiving workflow. Pick an open order, then:

**AI Auto-Fill expander** — upload one or more invoice photos, hit
**🚀 Scan Invoice**. Each page runs the 3-pass pipeline (accuracy over
speed; every call reads live AI settings from the `app_settings` table):

- **Pass 1 — pure vision OCR** (`ocr_invoice_page`): the image only, no
  DB items or aliases in the prompt, so the model transcribes what is
  actually printed. Returns per row: `invoice_desc` (exact Product
  column text), `qty` (Qty column — never pack sizes like 2x5kg),
  `unit_price` (Unit Price, pre-VAT), `goods_value` (line net),
  `vat_rate` (% as float). Temperature 0, JSON mode.
- **Python math check**: any row where `qty × unit_price` differs from
  `goods_value` by > £0.05 is flagged. A zero `unit_price` is derived as
  `goods_value / qty`; remaining mismatches trigger **one retry OCR** at
  temperature 0.2 whose prompt lists the offending rows.
- **Pass 2 — text-only semantic matching** (`match_invoice_lines`): no
  image. Two context blocks — EXPECTED ON THIS ORDER vs OTHER SUPPLIER
  CATALOG ITEMS (id, name, aliases as *Past Brand/Pack Examples*) — plus
  the numbered OCR lines. Any brand/pack of the same product type
  matches (beef dripping of any brand → "fat" item). Returns
  `line_index → id | null`.
- **Pass 3 — independent verification** (`verify_matches`): no image.
  Shows the pairings (`Line N: Invoice "…" → DB ID x ("name")`) against
  the full item list and hunts for swaps/contradictions. Pass 3 ids are
  final. Any Pass2↔Pass3 disagreement, missing verified entry, or
  `confidence: "low"` sets `needs_review` on the result.

Results accumulate across pages into `delivery_ai_results` (qty sums,
latest price wins) split into:
- **matched** — ids on this order; and
- **unexpected** — `id: null` or a catalog item not on this order.

After the loop the scan handler explicitly assigns the form widget state
(`q_/p_/v_` per order item, `notes_<oid>`), clears stale unexpected-row
widget keys, stores a summary in `delivery_scan_msg` and `st.rerun()`s,
so the form immediately shows scanned values.

**Confirmation form** — one row per ordered line: label with
Ord/Rec counts, a badge (`✅ Match`, `⚠️ Short/Over`, `❌ Not found on
invoice`, plus `· 🔍 Verify match` when the 3-pass check flagged it) and
the invoice text as a caption; editable **Recv Qty / Net Price / VAT**
inputs. Below it, the **Unexpected / Substituted Items** section: tick
**Include?**, map to any inventory item, adjust qty/price/VAT (mapping
applies to this delivery only — staff never save aliases).

On **Confirm Delivery & Update Prices** (one transaction):
1. For each row with qty > 0: add to stock, bump `received_quantity`.
2. For each row with price > 0: update the item's current price and log
   to `price_history` (tagged with the order id).
3. Included unexpected items: add stock; if the target is on the order,
   bump its received qty (re-read fresh from DB); otherwise insert a
   substitution `order_items` row (`ordered_quantity = 0` + name
   snapshot). Prices handled as in step 2.
4. Delivery notes: user notes + substitution block + a discrepancy
   summary computed **after** mapped items are folded in (so
   substitutions don't show as "Missing").
5. Status: `Delivered` if every line has received ≥ ordered, else
   `Partially Delivered`. Commit, clear scan state, rerun.

Scan failures are shown per page (`(OCR)/(Match)/(Verify)` tagged) and
suppress badges/auto-notes for that order.

### 1.7 📦 View Order History
Read-only: all orders table + per-order line items via selectbox.

### 1.8 📉 Log Stock Loss
Stock Room Failure (Loss) only — 2Good2Go was retired. Pick a category
and item (`Name (Stock: N)` labels), enter **how many FULL BOXES lost**
and hit **🚨 Deduct Stock & Log Loss**: deducts stock (refuses if more
than available) and logs the event with
`value = net_price × boxes` — `item_prices.net_price` is the BOX/CASE
unit price from supplier invoices, NOT per piece (multiplying by
`pack_size` inflated losses 20x–60x; fixed 2026-10). `pack_size` is no
longer read for this calculation. On success,
`invalidate_order_form_state()` clears stale 'Stuff We Want' widget
state. Entries carry the logged-in username.

---

## 2. Admin app (`ims_web_app_admin.py`)

Sidebar groups: Inventory DB Mgmt / Order DB Mgmt / Staff & Access /
Allergen Management / Waste & Stock Loss / Reporting / System Backup.

### 2.1 📊 View Full Stock
Full inventory dataframe (all columns incl. prices, desired qty,
suppliers, allergens).

### 2.2 🔍 Search Items
Search by name; detail view of the matching item(s).

### 2.3 ➕ Add New Item
Form: name, category, supplier(s), pack size, desired/available qty,
etc. Inserts into `items`.

### 2.4 ✏️ Edit Item Details
Pick item → edit category/suppliers/desired qty/expiry → save
(the Pack Size field was removed; `pack_size` remains read-only in the
DB for stock-failure valuation).

### 2.5 🎯 Batch Set Desired Stock
Filter by category (or All), edit desired quantities in a grid,
**Save Changes** writes only the changed rows
(`update_desired_quantity_only`).

### 2.6 💾 Export Stock Data / 🚚 Import Stock Data (CSV)
CSV download of the stock table; CSV upload to bulk-create items.
Every exported CSV goes through `database_utils.sanitize_df_for_csv`
(audit 2026-10-31 #4, fixed 2026-11-07): string cells starting with
`= + - @` TAB or CR get a leading `'` so Excel/LibreOffice render them
as text instead of executing them; numeric columns are untouched.
Regression:
`tests/verify_csv_sanitization.py`.

### 2.7 💰 Price & Value
- Dashboard metrics: total stock value net/gross, items priced.
- **📄 Update Prices from Invoice (AI)**: now runs the same **3-pass
  pipeline** as the staff delivery scan (see 1.6) — pure-vision OCR with
  math check + one retry, text-only semantic matching against the
  supplier catalog (aliases as *Past Brand/Pack Examples*), then an
  independent Pass-3 swap check whose ids are final. Multi-page uploads
  supported; rows with low confidence or Pass2↔Pass3 disagreement show
  a `🔍 Verify match` badge in the review table. **💾 Process Updates**
  writes current prices, logs `price_history`, and optionally saves
  learned aliases (admin-only).
- **Manual Price Editor** per item (net price + VAT).
- **Full Valuation Table** (net/gross per item and totals).

### 2.8 View Order History / 🛒 Create Custom Order / Delete-Cancel Order
History viewer; hand-built order (supplier → category → item + qty,
add rows, notes, **Generate Orders**); delete or cancel an order
(cancel keeps history, delete removes it — line items survive deletes
via the `item_name` snapshot + `SET NULL` schema).

### 2.9 👥 Manage Users
Add staff (username + 4-digit PIN), reset PINs, delete users. PINs are
stored hashed; see README auth note.

### 2.10 🧠 AI Settings
Edits the `app_settings` key/value table: Ollama base URL, vision
model, timeout, context window (`ai_num_ctx`, 2048–32768 tokens,
default 8192 — passed as `options.num_ctx` on every Ollama call so
long catalogs/schemas are not silently truncated to Ollama's 2048
default; invalid stored values fall back to the default).
Precedence: DB row > env var > built-in default. Env vars
`IMS_OLLAMA_URL`, `IMS_VISION_MODEL`, `IMS_AI_TIMEOUT_S`,
`IMS_AI_NUM_CTX` configure AI without opening the UI (headless/
Docker); the page shows which env defaults are active and saving
here overrides them. Built-in defaults are the public ones
(`http://127.0.0.1:11434`, `llama3.2-vision:11b`) — local setups
pin their model via env or this page. Harness
`tests/verify_env_ai_settings.py`.
Live model list from `/api/tags` and a connection-test
button. Every AI call site re-reads settings per call — changes apply
without restart.

### 2.11 🤖 AI Allergen Extractor / 👁️ View All Allergens
Upload a product label photo → **3-pass pipeline** (same philosophy as
the invoice scan, §1.6): Pass 1 pure-vision label read (ingredients +
Contains statement + precautionary warning transcribed, bold/underlined
allergen words preserved as `**word**` markers, plus a `bold_words`
list — NO master list in the prompt), Pass 2 text-only normalisation of
each phrase to canonical master allergen names (every ingredient
compound scanned: Wheat Flour→Wheat, Whey→Milk, Albumen→Eggs,
Sulphites/E220-E228 etc.), Pass 3 independent verification against the
transcription (full UK-14 scan, bolded words must appear; its lists
are FINAL). Low confidence or Pass-3 corrections flag `🔍`.
**Deterministic safety net**: a word-boundary regex keyword scan
(`deterministic_allergen_scan`) runs over the transcription itself —
ingredients+Contains merged, precautionary separate — and UNIONS any
missed allergen into the lists (names snapped to the master table's
spelling, e.g. Soya/Soy, Egg/Eggs, Tree Nuts→Nuts); anything it adds
forces `🔍` and is named in the scan message. Phrases matching no
master name are listed for the admin. Review the prefilled
multiselects → **Save Both Lists** writes to the item + master
allergen tables. Master list page shows all known allergens.
**Review timestamp**: `items.allergens_last_reviewed` (TEXT,
`'YYYY-MM-DD HH:MM'` local time, NULL = never reviewed; migration
`migrate_add_allergens_last_reviewed_column`). The extractor shows
`Last Reviewed:` under the current allergen captions and
**Save Both Lists** stamps it via `mark_item_allergens_reviewed`
(even a zero-allergen confirmation counts as a review). It surfaces
in 🔍 Search Items ('Never' default), the staff Stuff We Have table
(🕒 Allergens Reviewed column, 'Never' default) and the CSV export.
**Review queue**: the extractor opens with 3 metrics (✅ Reviewed /
⚠️ Never Reviewed / 🕰️ Stale > 90 days, buckets via
`_allergen_review_bucket` + `ALLERGEN_STALE_DAYS`) and a horizontal
radio filtering the product dropdown (All / Never Only / Needs Review);
dropdown labels carry the status suffix (`— ⚠️ Never reviewed` or
`— ✅ YYYY-MM-DD`). The selectbox is keyed (`allergen_prod_select`) and
its stale value is popped when the new option list changes, so label
flips and filter switches can't crash it. View All Allergens is now
two tabs: 📋 Inventory Allergen Review Status (audit dataframe from
`fetch_inventory_with_allergens`, filter radio All / ⚠️ Never Reviewed
/ 🕰️ Older than 90 Days / ✅ Up to Date, sorted NEVER first then
oldest timestamps first, blank shown as `⚠️ NEVER`) + the master
allergen list (add/merge unchanged, inside the tab).

### 2.12 📊 Waste Analysis (❄️ Stock Loss & Waste Dashboard)
Stock Failures only — legacy `2Good2Go` rows are filtered out in SQL.
KPIs: Total Value Lost (£), Total Boxes Lost, Total Incidents Logged.
Charts tab: Loss by Category donut + Top Lost Items by Value bar
(plotly). Log tab: transaction table + delete an entry (deleting a
Stock Failure re-adds the boxes to inventory).

### 2.13 📊 Spend Analysis
Tabs: 💰 Total Spend, 🧀 Spend by Item (monthly side-by-side),
📂 Spend by Category — built from orders/price history. The spend
queries pick at most ONE `price_history` row per (item, order) via a
correlated subquery (latest `history_id`) — a direct JOIN would
double-count multi-stage deliveries — fall back to current
`item_prices`, treat NULL `received_quantity` as 0, and use the
`order_items.item_name` snapshot for deleted items.

### 2.14 💾 Backup & Restore
**Export**: generates a full SQL dump (`smt_backup_<ts>.sql`) for
download. **Import (Restore)**: DANGER ZONE — upload a `.sql` dump and
**EXECUTE RESTORE** wipes current data and replays the backup. The
confirm PIN must belong to an **admin** account (audit 2026-10-31 #3,
fixed 2026-11-07): `verify_staff_login()` result is checked for
`is_admin` before restoring — any staff PIN is refused with a
non-admin error and the DB is untouched. Regression:
`tests/verify_restore_admin_pin.py` (staff PIN refused + canary row
survives; admin PIN executes the restore).

---

## 3. FSA Allergen Board (`ims_allergen_board.py`)

Standalone, customer-facing, **strictly read-only**: no login/PIN, no
form writes — the only DB call is
`database_utils.fetch_inventory_with_allergens(conn)`. Built to be
turned round to the customer at the counter (UK FSA / HSE allergen
communication). Shares the same DB (`IMS_DB_PATH` override applies).

- **Top banner (permanent):** "🛡️ Allergen Information Guide" + the FSA
  cross-contamination notice (kitchen handles allergens; nothing
  guaranteed 100% free).
- **FSA icons:** official colour PNGs in `./PNG/` (tracked in git),
  resolved dynamically by filename token (never hardcoded paths):
  `get_png_map()` scans the folder once; `get_allergen_icon_path()` /
  `map_allergen_to_group()` map any DB allergen name (UK-14 synonyms +
  specific cereals/nuts/molluscs) to one of the 14 groups; icons are
  downscaled to 200px thumbnails and base64-cached (`@st.cache_data`) —
  the originals are ~3700×3700 px. Cards always show the EXACT DB label
  under the icon (e.g. "Wheat" under the Gluten icon).
- **CRITICAL safety rule:** `allergens_last_reviewed` empty ⇒ item is
  NEVER shown as safe/no-allergens. It gets the red "⚠️ ALLERGEN INFO
  NOT YET VERIFIED — PLEASE CHECK PHYSICAL PACKAGING" badge, is excluded
  from ✅ Suitable, and every matrix cell reads "❓ UNVERIFIED".
- **Controls:** search box, category multiselect (defaults to food
  categories; cleaning/packaging/paperwork unchecked), "🚫 Customer
  Avoids" UK-14 multiselect + "Also exclude 'May Contain' (Traces)"
  toggle (default **True** — traces count as a hit). Items split into
  ✅ Suitable (verified, no hit) / ❌ Avoid / ⚠️ Unverified.
- **XSS-safe rendering (audit 2026-10-31 #1, fixed 2026-11-07):** every
  DB-sourced value (item name, category, allergen labels, review date)
  interpolated into the card/icon HTML goes through
  `html.escape(value, quote=True)` before `st.markdown(...,
  unsafe_allow_html=True)` — names are admin-editable and the board is
  unauthenticated, so unescaped values = stored XSS on the counter
  display. Covered sites: `icons_html` (alt attr + label div, both
  branches), `render_card` (heading, category badge, verified stamp).
  Regression: `tests/verify_allergen_board.py` seeds `<img onerror>` /
  `<script>` / attribute-breakout payloads and asserts only the escaped
  form reaches the rendered markdown.
- **Mode A — Product cards (default):** name + category badge,
  verification stamp (`✅ Verified: <date>` or red warning), side-by-side
  ⚠️ CONTAINS / ❓ MAY CONTAIN icon rows ("None declared" when empty).
- **Mode B — FSA matrix:** items × 14 UK allergen columns + Last
  Reviewed; cells `⚠️ CONTAINS` / `❓ MAY CONTAIN` / `—` / `❓ UNVERIFIED`
  (EHO-friendly whole-menu view).
- Regression harness: `tests/verify_allergen_board.py` (AppTest) asserts
  the unverified rule, avoid-split, traces toggle, matrix cells, and
  that the board performs zero DB writes.

---

## 4. Cross-cutting behaviour

- **One shared DB connection** (`@st.cache_resource`,
  `check_same_thread=False`); helpers never commit — callers manage
  transactions (`conn.commit()` / `conn.rollback()` on error).
- **History survival**: `order_items` and `waste_log` keep an
  `item_name` snapshot and `item_id → items(id) ON DELETE SET NULL`, so
  deleting an item never erases past orders or waste entries.
- **AI contract**: every Ollama call returns `(data, error_msg)` —
  failures (HTTP, timeout, connection, JSON parse) are logged and shown
  in the UI, never silently turned into an empty scan.
- **Widget-state rule**: values computed by the AI scan are assigned
  into `st.session_state` for every form key, then `st.rerun()` —
  Streamlit keeps existing widget state over `value=` defaults, so
  assigning (not popping) is what makes scanned values appear.
- **Permission split**: alias creation, user management, restores and
  bulk edits are admin-only; the staff app never calls those helpers.

Roadmap and change history: see `TODO.md`. Architecture/run/auth notes:
see `README.md`.
