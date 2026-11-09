#!/usr/bin/env bash
# ims_v3 launcher — creates a local venv on first run, then starts an app.
#
#   ./run.sh                 -> staff app   (ims_web_app_user.py)
#   ./run.sh admin           -> admin app   (ims_web_app_admin.py)
#   ./run.sh board           -> allergen board (ims_allergen_board.py)
#
# Optional: IMS_DB_PATH=/path/to/inventory.db ./run.sh admin
# (otherwise database_utils.py's default absolute DB path is used).
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating venv in $VENV (first run)..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip >/dev/null
    "$VENV/bin/pip" install -r requirements.txt
fi

case "${1:-user}" in
    user)   APP="ims_web_app_user.py" ;;
    admin)  APP="ims_web_app_admin.py" ;;
    board)  APP="ims_allergen_board.py" ;;
    *)      echo "Unknown app '$1' (expected: user | admin | board)" >&2; exit 1 ;;
esac

exec "$VENV/bin/python" -m streamlit run "$APP" "${@:2}"
