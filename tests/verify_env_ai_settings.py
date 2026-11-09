import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database_utils as du

db = os.path.join(tempfile.gettempdir(), 'env_ai_test.db')
if os.path.exists(db): os.remove(db)
conn = sqlite3.connect(db)
du.ensure_all_tables_exist(conn)

# 1. no env, no DB row -> built-in defaults
for k in ("IMS_OLLAMA_URL","IMS_VISION_MODEL","IMS_AI_TIMEOUT_S","IMS_AI_NUM_CTX"):
    os.environ.pop(k, None)
ai = du.get_ai_settings(conn)
assert ai["base_url"] == "http://127.0.0.1:11434", ai
assert ai["model"] == "llama3.2-vision:11b", ai
assert ai["timeout_s"] == 180 and ai["num_ctx"] == 8192, ai

# 2. env set, no DB row -> env wins over built-in
os.environ["IMS_OLLAMA_URL"] = "http://p40.local:11434/"
os.environ["IMS_VISION_MODEL"] = "gemma4:26b"
os.environ["IMS_AI_TIMEOUT_S"] = "240"
os.environ["IMS_AI_NUM_CTX"] = "16384"
ai = du.get_ai_settings(conn)
assert ai["base_url"] == "http://p40.local:11434", ai   # trailing slash stripped
assert ai["model"] == "gemma4:26b" and ai["timeout_s"] == 240 and ai["num_ctx"] == 16384, ai

# 3. DB row beats env
du.set_setting(conn, "ollama_base_url", "http://db-wins:11434")
du.set_setting(conn, "vision_model", "dbmodel:1b")
ai = du.get_ai_settings(conn)
assert ai["base_url"] == "http://db-wins:11434" and ai["model"] == "dbmodel:1b", ai

# 4. garbage env ints fall back to built-in defaults
os.environ["IMS_AI_TIMEOUT_S"] = "notanumber"
ai = du.get_ai_settings(conn)
assert ai["timeout_s"] == 180, ai

os.remove(db)
print("ENV_PRECEDENCE_OK")
