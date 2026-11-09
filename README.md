# ims — Stuff Management Thing

A small self-hosted inventory / stock management system for food
businesses, built with Streamlit + SQLite. Designed to run on a LAN
(café, small kitchen, deli counter). AI features are optional and run
against a local [Ollama](https://ollama.com) server — no cloud calls.

Three apps share one SQLite database:

- **`ims_web_app_user.py`** — staff app: inventory & stock checks,
  orders & deliveries (incl. AI-assisted delivery label scanning),
  waste & loss logging.
- **`ims_web_app_admin.py`** — admin panel: inventory/order DB
  management, staff & access, allergen management, waste & recovery,
  reporting & visualisation (plotly), backup/restore, AI Settings.
- **`ims_allergen_board.py`** — read-only customer-facing allergen
  display (no login; UK FSA colour icons; unreviewed items are never
  shown as safe).

## Quickstart

```bash
git clone <this repo> && cd <repo>
./run.sh            # creates .venv on first run, starts the staff app
./run.sh admin      # admin panel
./run.sh board      # customer allergen board
```

First run creates `inventory.db` next to the sources and a default
admin account: username **Admin**, PIN **1234**. Log in and change the
PIN immediately (admin → Manage Users).

Python 3.12+ (verified through 3.14). Dependencies are pinned in
`requirements.txt`.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `IMS_DB_PATH` | SQLite database file | `inventory.db` next to the sources |
| `IMS_OLLAMA_URL` | Ollama base URL | `http://127.0.0.1:11434` |
| `IMS_VISION_MODEL` | model for label/OCR scans | `llama3.2-vision:11b` |
| `IMS_AI_TIMEOUT_S` | seconds per AI call | `180` |
| `IMS_AI_NUM_CTX` | context window (tokens) | `8192` |

Precedence: settings saved in the admin **🧠 AI Settings** page win
over env vars, which win over built-in defaults. The AI Settings page
also lists models detected on the server and has a connection-test
button; changes apply without restart.

## AI features (optional)

All AI runs against your own Ollama server — nothing leaves your
network. If Ollama is not reachable, every AI feature shows a clear
error and the rest of the app works normally.

```bash
ollama pull llama3.2-vision:11b     # or any vision-capable model
```

Uses: delivery-note OCR + item matching (3-pass pipeline with a
Python math gate and an independent verification pass), allergen
label extraction (3-pass + deterministic keyword net), and invoice
price updates. Every screen reports AI failures per stage; AI results
that disagree or look uncertain are flagged 🔍 for human review rather
than applied silently.

## Security model

This is built for a trusted LAN (optionally reached over Tailscale or
similar), not for internet exposure:

- Login is a 4-digit PIN, stored as salted scrypt hashes. Treat the
  PIN as a convenience gate, not a strong secret.
- The allergen board is unauthenticated by design (counter display) —
  keep it on the trusted network segment.
- Do not port-forward any of these apps to the public internet.

## Architecture & behaviour docs

- `WORKFLOWS.md` — per-screen contract of exactly what each screen
  does (session-state keys, pipelines, cross-cutting rules).
- `database_utils.py` — all schema + DB helpers; schema self-migrates
  on app start, so updates need no manual DB step.
- `tests/` — dependency-free regression harnesses:
  `python tests/verify_spend_queries.py` etc. (they import
  `database_utils`, so run them with the venv python:
  `.venv/bin/python tests/<name>.py`).

## Icons

Allergen icons in `PNG/` are the GOV.UK allergen icon set — Crown
copyright, Open Government Licence v3.0. See `PNG/ATTRIBUTION.md`.

## License

MIT (see `LICENSE`), except the `PNG/` icons (OGL v3.0, see above).
