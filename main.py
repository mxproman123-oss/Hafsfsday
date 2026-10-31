import os
import sys
import subprocess
import importlib.util

# ==============================================================================
# 1. BOOTSTRAP & DEPENDENCY INJECTION
# ==============================================================================
def _bootstrap_environment():
    required_packages = {
        "telebot": "pyTelegramBotAPI",
        "cryptography": "cryptography",
        "psutil": "psutil"
    }
    missing = [pkg for mod, pkg in required_packages.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"[*] Installing missing packages: {', '.join(missing)}")
    base_cmd = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    try:
        subprocess.check_call(base_cmd + missing)
    except subprocess.CalledProcessError:
        try:
            subprocess.check_call(base_cmd + ["--break-system-packages"] + missing)
        except Exception as e:
            sys.exit(f"[!] Critical Error during bootstrapping: {e}")

_bootstrap_environment()

import io
import re
import ast
import json
import time
import html
import hmac
import base64
import shutil
import zipfile
import zlib
import sqlite3
import hashlib
import signal
import threading
import secrets
import traceback
from pathlib import Path

import psutil
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

# ==============================================================================
# 2. SYSTEM CONSTANTS & CONFIGURATION
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
VENVS_DIR = BASE_DIR / "libs"
RUN_BASE_DIR = BASE_DIR / "runs"

for _d in (DATA_DIR, VENVS_DIR, RUN_BASE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_d, 0o700 if _d == DATA_DIR else 0o755)
    except Exception:
        pass

def _safe_chmod(path, mode):
    try:
        os.chmod(path, mode)
    except Exception:
        pass

def _load_env_file(filepath: Path):
    if filepath.exists():
        try:
            for line in filepath.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        except Exception:
            pass

_load_env_file(BASE_DIR / ".env")

DEFAULT_BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"
DEFAULT_ADMIN_ID = 0

ENV_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_TOKEN = ENV_TOKEN if ENV_TOKEN else DEFAULT_BOT_TOKEN

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", str(DEFAULT_ADMIN_ID)).replace(" ", "").split(",") if x.isdigit()}
ADMIN_IDS.add(DEFAULT_ADMIN_ID)
ADMIN_IDS.discard(0)

SANDBOX_UID_BASE = int(os.environ.get("SANDBOX_UID_BASE", "0") or 0)
CHILD_MEM_LIMIT_MB = int(os.environ.get("CHILD_MEM_MB", "2048"))
MAX_PROJECT_SIZE_BYTES = 60 * 1024 * 1024  # 60 MB
MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024     # 30 MB
ITEMS_PER_PAGE = 8

SYS_PATH = os.environ.get("PATH", "")
EXTRA_PATHS = "/usr/local/bin:/usr/bin:/bin:/data/data/com.termux/files/usr/bin"
CLEAN_EXEC_PATH = f"{SYS_PATH}:{EXTRA_PATHS}" if SYS_PATH else EXTRA_PATHS

BANNER_HEADER = (
    "╔══════════════════════════════════════╗\n"
    "║        🛠️ <b>PRIVATE BOT MANAGER</b> 🛠️     ║\n"
    "║   <i>Owner-Only Project Control Panel</i>   ║\n"
    "╚══════════════════════════════════════╝"
)
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ==============================================================================
# 3. CRYPTOGRAPHIC ENGINE (AES-256-GCM)
# ==============================================================================
KEY_FILE = DATA_DIR / "master.key"

def _initialize_master_key() -> bytes:
    env_key = os.environ.get("MASTER_KEY", "").strip()
    if env_key:
        try:
            k = base64.urlsafe_b64decode(env_key)
            if len(k) == 32:
                return k
        except Exception:
            pass

    if KEY_FILE.exists():
        try:
            k = base64.urlsafe_b64decode(KEY_FILE.read_text().strip())
            if len(k) == 32:
                return k
        except Exception:
            pass

    raw_key = os.urandom(32)
    KEY_FILE.write_bytes(base64.urlsafe_b64encode(raw_key))
    _safe_chmod(KEY_FILE, 0o600)
    print(f"[!] MASTER KEY GENERATED AT: {KEY_FILE}")
    return raw_key

MASTER_KEY = _initialize_master_key()

def _derive_subkey(label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label).derive(MASTER_KEY)

KEY_FILE_CRYPTO = _derive_subkey(b"vault-host-file")
KEY_META_CRYPTO = _derive_subkey(b"vault-host-meta")
KEY_INDEX_CRYPTO = _derive_subkey(b"vault-host-index")

def aes_encrypt(data: bytes, key: bytes = KEY_META_CRYPTO, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)

def aes_decrypt(blob: bytes, key: bytes = KEY_META_CRYPTO, aad: bytes = b"") -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)

def blind_index(value: str) -> str:
    return hmac.new(KEY_INDEX_CRYPTO, str(value).encode(), hashlib.sha256).hexdigest()

def _derive_file_key(salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"file-encryption").derive(KEY_FILE_CRYPTO)

def encrypt_project_data(raw_data: bytes) -> bytes:
    salt = os.urandom(16)
    compressed = zlib.compress(raw_data, level=6)
    return salt + aes_encrypt(compressed, _derive_file_key(salt), b"project_secure_blob")

def decrypt_project_data(blob: bytes) -> bytes:
    salt, encrypted_data = blob[:16], blob[16:]
    decrypted_compressed = aes_decrypt(encrypted_data, _derive_file_key(salt), b"project_secure_blob")
    return zlib.decompress(decrypted_compressed)

# ==============================================================================
# 4. DATABASE ORM & MANAGEMENT
# ==============================================================================
DB_PATH = DATA_DIR / "vault_core.db"
db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)

for _pragma in ("PRAGMA journal_mode=WAL", "PRAGMA secure_delete=ON", "PRAGMA synchronous=NORMAL"):
    try:
        db_conn.execute(_pragma)
    except Exception:
        pass

db_conn.executescript("""
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_hash TEXT NOT NULL,
    encrypted_meta BLOB NOT NULL,
    encrypted_blob BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    encrypted_data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sys_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")
_safe_chmod(DB_PATH, 0o600)
DB_LOCK = threading.RLock()

def execute_query(sql: str, args=(), fetch_one=False, commit=False):
    with DB_LOCK:
        cursor = db_conn.execute(sql, args)
        if commit:
            db_conn.commit()
        return cursor.fetchone() if fetch_one else cursor.fetchall()

def current_timestamp() -> int:
    return int(time.time())

def escape_html(text: str) -> str:
    return html.escape(str(text))

def json_encrypt(obj: dict, aad: bytes) -> bytes:
    return aes_encrypt(json.dumps(obj, ensure_ascii=False).encode(), KEY_META_CRYPTO, aad)

def json_decrypt(blob: bytes, aad: bytes) -> dict:
    return json.loads(aes_decrypt(blob, KEY_META_CRYPTO, aad).decode())

def get_setting(key: str, default: str = "") -> str:
    row = execute_query("SELECT value FROM sys_settings WHERE key=?", (key,), fetch_one=True)
    return row[0] if row else default

def set_setting(key: str, value: str):
    execute_query("INSERT OR REPLACE INTO sys_settings VALUES(?,?)", (key, str(value)), commit=True)

if not get_setting("max_ram_mb"):
    set_setting("max_ram_mb", str(CHILD_MEM_LIMIT_MB))

# ── Project ORM ──
def pack_virtual_filesystem(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for filename, data in files.items():
            z.writestr(filename, data)
    return encrypt_project_data(buf.getvalue())

def unpack_virtual_filesystem(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(decrypt_project_data(blob))) as z:
        return {info.filename: z.read(info) for info in z.infolist() if not info.is_dir()}

def _normalize_meta(m: dict) -> dict:
    if not m:
        return None
    m.setdefault("owner", 0)
    m.setdefault("name", "Project")
    m.setdefault("main", "main.py")
    m.setdefault("created_at", m.get("created", current_timestamp()))
    m.setdefault("auto_restart", m.get("auto", True))
    m.setdefault("was_running", False)
    m.setdefault("deps_installed", m.get("deps_ok", False))
    m.setdefault("extra_packages", m.get("extra", []))
    m.setdefault("failed_packages", m.get("failed", []))
    m.setdefault("file_count", m.get("nfiles", 0))
    m.setdefault("total_size", m.get("size", 0))
    m.setdefault("env_vars", {})
    return m

def create_project(owner_id: int, name: str, files: dict, main_file: str) -> int:
    meta = {
        "owner": owner_id,
        "name": name,
        "main": main_file,
        "created_at": current_timestamp(),
        "auto_restart": True,
        "was_running": False,
        "deps_installed": False,
        "extra_packages": [],
        "failed_packages": [],
        "file_count": len(files),
        "total_size": sum(len(v) for v in files.values()),
        "env_vars": {}
    }
    with DB_LOCK:
        cursor = db_conn.execute(
            "INSERT INTO projects(owner_hash, encrypted_meta, encrypted_blob) VALUES(?,?,?)",
            (blind_index(owner_id), json_encrypt(meta, b"proj_meta"), pack_virtual_filesystem(files))
        )
        db_conn.commit()
        return cursor.lastrowid

def get_project_meta(pid: int) -> dict:
    row = execute_query("SELECT encrypted_meta FROM projects WHERE id=?", (pid,), fetch_one=True)
    if not row:
        return None
    try:
        return _normalize_meta(json_decrypt(row[0], b"proj_meta"))
    except Exception:
        return None

def update_project_meta(pid: int, **kwargs) -> dict:
    with DB_LOCK:
        meta = get_project_meta(pid)
        if not meta:
            return None
        meta.update(kwargs)
        execute_query(
            "UPDATE projects SET encrypted_meta=? WHERE id=?",
            (json_encrypt(meta, b"proj_meta"), pid),
            commit=True
        )
        return meta

def get_project_files(pid: int) -> dict:
    row = execute_query("SELECT encrypted_blob FROM projects WHERE id=?", (pid,), fetch_one=True)
    if not row:
        return {}
    try:
        return unpack_virtual_filesystem(row[0])
    except Exception:
        return {}

def save_project_files(pid: int, files: dict):
    with DB_LOCK:
        execute_query(
            "UPDATE projects SET encrypted_blob=? WHERE id=?",
            (pack_virtual_filesystem(files), pid),
            commit=True
        )
        update_project_meta(pid, file_count=len(files), total_size=sum(len(v) for v in files.values()))

def get_project_ids(owner_id: int = None) -> list:
    if owner_id is None:
        return [r[0] for r in execute_query("SELECT id FROM projects ORDER BY id DESC")]
    return [r[0] for r in execute_query("SELECT id FROM projects WHERE owner_hash=? ORDER BY id DESC", (blind_index(owner_id),))]

def delete_project(pid: int):
    execute_query("DELETE FROM projects WHERE id=?", (pid,), commit=True)
    shutil.rmtree(VENVS_DIR / str(pid), ignore_errors=True)

def log_audit(admin_id: int, action: str, **kwargs):
    payload = {"timestamp": current_timestamp(), "admin_id": admin_id, "action": action, **kwargs}
    execute_query("INSERT INTO audit_logs(encrypted_data) VALUES(?)", (json_encrypt(payload, b"audit_log"),), commit=True)

# ==============================================================================
# 5. CODE ANALYSIS & DEPENDENCY INFERENCE
# ==============================================================================
COMMON_PACKAGE_ALIASES = {
    "cv2": "opencv-python", "PIL": "Pillow", "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot",
    "bs4": "beautifulsoup4", "yaml": "PyYAML", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "dotenv": "python-dotenv", "Crypto": "pycryptodome", "Cryptodome": "pycryptodomex",
    "dateutil": "python-dateutil", "jwt": "PyJWT", "attr": "attrs", "serial": "pyserial",
    "magic": "python-magic", "docx": "python-docx", "fitz": "PyMuPDF", "pyrogram": "pyrogram tgcrypto",
    "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "nacl": "PyNaCl", "OpenSSL": "pyOpenSSL",
    "yt_dlp": "yt-dlp", "speech_recognition": "SpeechRecognition", "git": "GitPython",
    "googletrans": "googletrans==4.0.0rc1", "pymysql": "PyMySQL", "socks": "PySocks",
    "Levenshtein": "python-Levenshtein", "discord": "discord.py", "google": "google-api-python-client",
    "flask_cors": "flask-cors", "fastapi": "fastapi uvicorn", "aiohttp": "aiohttp", "requests": "requests"
}

STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names) | {
    "os", "sys", "time", "json", "re", "math", "random", "threading", "subprocess", "sqlite3",
    "pathlib", "io", "shutil", "zipfile", "zlib", "hashlib", "hmac", "base64", "secrets", "html",
    "urllib", "http", "asyncio", "logging", "datetime", "collections", "itertools", "functools",
    "typing", "traceback", "signal", "socket", "ssl", "struct", "uuid", "csv", "configparser"
}

PACKAGE_REQ_REGEX = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[A-Za-z0-9,_\-]+\])?"
    r"([=<>!~]=?[A-Za-z0-9.*+!_\-]+(,[=<>!~]=?[A-Za-z0-9.*+!_\-]+)*)?$"
)

def infer_project_requirements(files: dict) -> list:
    local_modules = set()
    for filename in files:
        top_level = filename.split("/")[0]
        local_modules.add(top_level[:-3] if top_level.endswith(".py") else top_level)
        base_stem = os.path.splitext(os.path.basename(filename))[0]
        local_modules.add(base_stem)

    discovered_imports = set()
    for filename, content in files.items():
        if not filename.endswith(".py"):
            continue
        try:
            tree = ast.parse(content.decode("utf-8", "ignore"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        discovered_imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    discovered_imports.add(node.module.split(".")[0])
        except Exception:
            continue

    required_packages = []
    for module in sorted(discovered_imports):
        if module in STDLIB_MODULES or module in local_modules or module.startswith("_"):
            continue
        required_packages += COMMON_PACKAGE_ALIASES.get(module, module).split()
    return list(dict.fromkeys(required_packages))

def extract_requirements_txt(files: dict) -> list:
    raw_reqs = files.get("requirements.txt")
    if not raw_reqs:
        return []
    parsed = []
    for line in raw_reqs.decode("utf-8", "ignore").splitlines():
        clean_line = line.split("#")[0].strip()
        if clean_line and PACKAGE_REQ_REGEX.match(clean_line):
            parsed.append(clean_line)
    return parsed

def determine_entry_point(files: dict) -> str:
    python_files = [f for f in files if f.endswith(".py")]
    if not python_files:
        return None
    standard_names = ["main.py", "bot.py", "app.py", "run.py", "start.py", "index.py", "__main__.py"]
    execution_keywords = (
        "infinity_polling", ".polling(", "run_polling", "run_forever", "start_polling",
        "executor.start", "uvicorn.run", "app.run(", "asyncio.run", "client.run", "bot.run(", "idle()"
    )

    def compute_score(filename: str) -> int:
        code_content = files[filename].decode("utf-8", "ignore")
        score = 0
        base_name = os.path.basename(filename)
        if base_name in standard_names:
            score += 20 - standard_names.index(base_name)
        if any(keyword in code_content for keyword in execution_keywords):
            score += 15
        if "__main__" in code_content:
            score += 5
        return score - (4 * filename.count("/"))

    return max(python_files, key=compute_score)

def validate_and_extract_zip(raw_bytes: bytes) -> dict:
    extracted_files = {}
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
        valid_infos = [info for info in z.infolist() if not info.is_dir()]
        if sum(info.file_size for info in valid_infos) > MAX_PROJECT_SIZE_BYTES or len(valid_infos) > 4000:
            raise ValueError(f"ZIP archive exceeds limits (Max {MAX_PROJECT_SIZE_BYTES // 1024 // 1024}MB).")
        for info in valid_infos:
            clean_name = info.filename.replace("\\", "/").lstrip("/")
            path_parts = clean_name.split("/")
            if ".." in path_parts or path_parts[0] in ("__MACOSX", ".git", ".venv", "venv", "__pycache__"):
                continue
            extracted_files[clean_name] = z.read(info)

    top_level_dirs = {name.split("/")[0] for name in extracted_files}
    if len(top_level_dirs) == 1 and extracted_files and all("/" in name for name in extracted_files):
        prefix = top_level_dirs.pop() + "/"
        extracted_files = {name[len(prefix):]: data for name, data in extracted_files.items() if name[len(prefix):]}
    return extracted_files

# ==============================================================================
# 6. RUNTIME ENGINE & PROCESS MANAGEMENT
# ==============================================================================
ACTIVE_PROCESSES = {}
STARTING_PROCESSES = set()
PROCESS_LOG_CACHE = {}
CRASH_HISTORY = {}
AUTO_HEAL_ATTEMPTS = {}

RUNTIME_LOCK = threading.RLock()
INSTALLATION_SEMAPHORE = threading.Semaphore(2)

def generate_sandbox_credentials(pid: int):
    if SANDBOX_UID_BASE and hasattr(os, "geteuid") and os.geteuid() == 0:
        uid = SANDBOX_UID_BASE + (pid % 60000)
        return {"user": uid, "group": uid, "extra_groups": []}, uid
    return {}, None

def apply_directory_ownership(path: Path, target_uid: int):
    if target_uid is None:
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.lchown(os.path.join(root, name), target_uid, target_uid)
            except Exception:
                pass
    try:
        os.lchown(path, target_uid, target_uid)
    except Exception:
        pass

def execute_pip_install(pid: int, packages: list) -> tuple:
    if not packages:
        return [], []
    target_dir = VENVS_DIR / str(pid)
    target_dir.mkdir(parents=True, exist_ok=True)
    sandbox_kwargs, uid = generate_sandbox_credentials(pid)
    apply_directory_ownership(target_dir, uid)

    env_vars = os.environ.copy()
    env_vars.update({
        "PATH": CLEAN_EXEC_PATH,
        "HOME": str(target_dir),
        "TMPDIR": str(target_dir),
        "PYTHONUNBUFFERED": "1"
    })
    base_pip_cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        "--no-cache-dir", "--upgrade", "--target", str(target_dir)
    ]

    successful, failed = [], []

    def run_sub_install(pkgs):
        try:
            result = subprocess.run(
                base_pip_cmd + pkgs,
                capture_output=True, text=True, timeout=900,
                env=env_vars, **sandbox_kwargs
            )
            return result.returncode == 0
        except Exception:
            return False

    if run_sub_install(packages):
        successful = list(packages)
    else:
        for pkg in packages:
            if run_sub_install([pkg]):
                successful.append(pkg)
            else:
                failed.append(pkg)

    apply_directory_ownership(target_dir, uid)
    return successful, failed

def write_virtual_filesystem(base_directory: Path, files: dict):
    root_resolved = base_directory.resolve()
    for filename, byte_data in files.items():
        file_path = (base_directory / filename).resolve()
        if root_resolved not in file_path.parents and file_path != root_resolved:
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(byte_data)

def collect_virtual_filesystem(base_directory: Path) -> dict:
    collected_files = {}
    total_size = 0
    for root, dirs, files in os.walk(base_directory):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".cache", ".git")]
        for file_name in files:
            file_path = Path(root) / file_name
            if file_name.endswith(".pyc") or file_path.is_symlink():
                continue
            try:
                size = file_path.stat().st_size
                if size > MAX_FILE_SIZE_BYTES or total_size + size > MAX_PROJECT_SIZE_BYTES:
                    continue
                rel_path = str(file_path.relative_to(base_directory)).replace("\\", "/")
                collected_files[rel_path] = file_path.read_bytes()
                total_size += size
            except Exception:
                pass
    return collected_files

def read_tail(filepath: str, max_bytes: int = 4000) -> str:
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(max(0, file_size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""

def synchronize_project_state(pid: int, directory_path: Path):
    try:
        updated_files = collect_virtual_filesystem(directory_path)
        if updated_files:
            save_project_files(pid, updated_files)
    except Exception as e:
        print(f"[!] Sync error for Project {pid}: {e}")

def cleanup_and_finalize_process(pid: int):
    with RUNTIME_LOCK:
        proc_data = ACTIVE_PROCESSES.pop(pid, None)
    if not proc_data:
        return None
    try:
        proc_data["log_file_handle"].close()
    except Exception:
        pass

    PROCESS_LOG_CACHE[pid] = read_tail(proc_data["log_path"], 8000)
    synchronize_project_state(pid, proc_data["run_dir"])
    shutil.rmtree(proc_data["run_dir"], ignore_errors=True)
    try:
        os.remove(proc_data["log_path"])
    except Exception:
        pass
    return proc_data

def _kill_process_tree(pid_num: int, sig):
    try:
        parent = psutil.Process(pid_num)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.send_signal(sig)
            except Exception:
                pass
        parent.send_signal(sig)
    except Exception:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid_num), sig)
    except Exception:
        pass

def force_stop_project(pid: int, triggered_by_user: bool = True) -> bool:
    proc_data = ACTIVE_PROCESSES.get(pid)
    if not proc_data:
        if triggered_by_user:
            update_project_meta(pid, was_running=False)
        return False
    proc_data["is_stopping"] = True
    process = proc_data["process"]

    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        _kill_process_tree(process.pid, sig)
        try:
            process.wait(timeout=4)
            break
        except subprocess.TimeoutExpired:
            continue

    cleanup_and_finalize_process(pid)
    if triggered_by_user:
        update_project_meta(pid, was_running=False)
    return True

# ==============================================================================
# 7. TELEGRAM BOT INITIALIZATION & UI HELPERS
# ==============================================================================
def _init_bot() -> telebot.TeleBot:
    global BOT_TOKEN
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        sys.exit("[!] BOT_TOKEN is not set. Set it in the environment (Render → Environment variables).")
    test_bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=40)
    test_bot.get_me()
    return test_bot

if not ADMIN_IDS:
    sys.exit("[!] ADMIN_IDS is not set. Set it in the environment (Render → Environment variables) to your Telegram user ID.")

bot_app = _init_bot()
USER_STATE_CACHE = {}

class Btn(types.InlineKeyboardButton):
    """Styled Inline Button: primary (blue), success (green), danger (red)."""
    def __init__(self, text: str, callback_data: str = None, style: str = None, url: str = None):
        super().__init__(text, url=url, callback_data=callback_data)
        self._style = style

    def to_dict(self):
        d = super().to_dict()
        if self._style:
            d["style"] = self._style
        return d

def _strip_styles(markup: types.InlineKeyboardMarkup) -> types.InlineKeyboardMarkup:
    if not markup or not getattr(markup, "keyboard", None):
        return markup
    clean_markup = types.InlineKeyboardMarkup()
    for row in markup.keyboard:
        clean_row = []
        for b in row:
            clean_row.append(types.InlineKeyboardButton(
                text=b.text, url=b.url, callback_data=b.callback_data
            ))
        clean_markup.row(*clean_row)
    return clean_markup

def build_keyboard(*rows) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        if row:
            markup.row(*row)
    return markup

def notify_user(chat_id: int, text: str, markup=None):
    try:
        return bot_app.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except ApiTelegramException as e:
        if markup and ("style" in str(e).lower() or "button" in str(e).lower()):
            try:
                return bot_app.send_message(chat_id, text, reply_markup=_strip_styles(markup), disable_web_page_preview=True)
            except Exception:
                pass
        print(f"[!] send_message error to {chat_id}: {e}")
    except Exception as e:
        print(f"[!] send_message error to {chat_id}: {e}")
    return None

def render_view(call: types.CallbackQuery, text: str, markup=None):
    try:
        bot_app.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=markup, disable_web_page_preview=True
        )
    except ApiTelegramException as e:
        err_s = str(e).lower()
        if "not modified" in err_s:
            return
        if markup and ("style" in err_s or "button" in err_s):
            try:
                bot_app.edit_message_text(
                    text, call.message.chat.id, call.message.message_id,
                    reply_markup=_strip_styles(markup), disable_web_page_preview=True
                )
                return
            except Exception:
                pass
        notify_user(call.message.chat.id, text, markup)
    except Exception:
        notify_user(call.message.chat.id, text, markup)

def create_memory_document(filename: str, byte_data: bytes):
    buffer = io.BytesIO(byte_data)
    buffer.name = os.path.basename(filename) or "file.txt"
    return buffer

def format_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"

def format_memory_size(bytes_val: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"

def generate_pagination(prefix: str, current_page: int, total_items: int, extra_suffix: str = "") -> list:
    row = []
    if current_page > 0:
        row.append(Btn("⬅️ Prev", f"{prefix}:{current_page - 1}{extra_suffix}", "primary"))
    if (current_page + 1) * ITEMS_PER_PAGE < total_items:
        row.append(Btn("Next ➡️", f"{prefix}:{current_page + 1}{extra_suffix}", "primary"))
    return row

# ==============================================================================
# 8. ACCESS CONTROL — OWNER ONLY
# ==============================================================================
def enforce_access_control(tg_user: types.User, chat_id: int) -> bool:
    """
    This bot is a PRIVATE, single-owner project manager.
    Only the Telegram user ID(s) listed in ADMIN_IDS may use it at all.
    Everyone else is rejected before any file, command, or button is processed.
    """
    if tg_user.id not in ADMIN_IDS:
        notify_user(
            chat_id,
            "🔒 <b>Private Bot</b>\nThis bot is for its owner's personal use only and is not available to the public."
        )
        return False
    if get_setting("maintenance_mode") == "1":
        notify_user(chat_id, "🛠 <b>Under Maintenance:</b> Please try again shortly.")
        return False
    return True

def can_access_project(pid: int, user_id: int) -> dict:
    meta = get_project_meta(pid)
    if not meta or user_id not in ADMIN_IDS:
        return None
    return meta

# ==============================================================================
# 9. SCREEN VIEWS
# ==============================================================================
def screen_main_dashboard(user_id: int):
    my_pids = get_project_ids(user_id)
    running_my = sum(1 for i in my_pids if i in ACTIVE_PROCESSES)

    text = (
        f"{BANNER_HEADER}\n\n"
        f"👋 Welcome back, Owner.\n"
        f"🆔 <b>Your ID:</b> <code>{user_id}</code>\n\n"
        f"⚡ <b>24/7 Execution</b> with instant deployment\n"
        f"🔐 <b>AES-256-GCM</b> encrypted storage & runtime sync\n"
        f"🧠 <b>Smart AST Scanner</b> auto-installs required libraries\n"
        f"🩹 <b>Self-Healing Engine</b> auto-repairs missing modules\n"
        f"{DIVIDER}\n"
        f"🤖 <b>Your Projects:</b> <b>{len(my_pids)}</b> (🟢 {running_my} Running)"
    )

    rows = [
        [Btn("🚀 Deploy New Project", "up_init", "success")],
        [Btn("📂 My Projects", "my:0", "primary")],
        [Btn("📊 Live Status", "status"), Btn("🆘 Guide & Help", "help")],
        [Btn("⚙️ Settings", "ad_set"), Btn("🧾 Audit Logs", "ad_au:0")],
        [Btn("🗄 Full Backup", "ad_bak", "success"), Btn("🚨 Stop All Bots", "ad_stop_all", "danger")],
    ]
    return text, build_keyboard(*rows)

def screen_project_dashboard(pid: int):
    meta = get_project_meta(pid)
    if not meta:
        return "❌ Project not found.", build_keyboard([Btn("⬅️ Back", "my:0")])

    proc_data = ACTIVE_PROCESSES.get(pid)
    status_str = "🟢 <b>ONLINE (RUNNING)</b>" if proc_data else ("🟡 <b>STARTING...</b>" if pid in STARTING_PROCESSES else "🔴 <b>OFFLINE (STOPPED)</b>")

    text = (
        f"🤖 <b>PROJECT #{pid}: {escape_html(meta['name'])}</b>\n"
        f"{DIVIDER}\n"
        f"📡 <b>Status:</b> {status_str}\n"
    )

    if proc_data:
        try:
            ps_p = psutil.Process(proc_data["process"].pid)
            ram_rss = ps_p.memory_info().rss + sum(c.memory_info().rss for c in ps_p.children(recursive=True))
            cpu_p = ps_p.cpu_percent(interval=0.1)
            text += (
                f"🆔 <b>PID:</b> <code>{proc_data['process'].pid}</code>\n"
                f"⏱ <b>Uptime:</b> {format_uptime(time.time() - proc_data['start_time'])}\n"
                f"🧠 <b>RAM:</b> {format_memory_size(ram_rss)}  •  💻 <b>CPU:</b> {cpu_p:.1f}%\n"
            )
        except Exception:
            text += f"⏱ <b>Uptime:</b> {format_uptime(time.time() - proc_data['start_time'])}\n"

    text += (
        f"🎯 <b>Main File:</b> <code>{escape_html(meta['main'])}</code>\n"
        f"📁 <b>Files:</b> {meta.get('file_count', 0)}  •  💾 <b>Size:</b> {format_memory_size(meta.get('total_size', 0))}\n"
        f"🔑 <b>Env Vars:</b> {len(meta.get('env_vars', {}))} configured\n"
        f"🔁 <b>Auto-Restart:</b> {'✅ ON' if meta.get('auto_restart') else '❌ OFF'}\n"
    )
    if meta.get("failed_packages"):
        text += f"⚠️ <b>Failed Deps:</b> <code>{escape_html(', '.join(meta['failed_packages']))}</code>\n"

    rows = []
    if proc_data:
        rows.append([Btn("⏹ Stop", f"p_stop:{pid}", "danger"), Btn("🔄 Restart", f"p_rst:{pid}", "primary")])
    else:
        rows.append([Btn("▶️ Start", f"p_run:{pid}", "success")])

    rows += [
        [Btn("📜 Live Logs", f"p_log:{pid}", "primary"), Btn("📂 File Manager", f"p_files:{pid}:0", "primary")],
        [Btn("📦 Install Library", f"p_pip:{pid}"), Btn("🔑 Env Variables", f"p_env:{pid}")],
        [Btn(f"🔁 Auto-Restart: {'ON' if meta.get('auto_restart') else 'OFF'}", f"p_auto:{pid}"), Btn("🎯 Main File", f"p_main:{pid}")],
        [Btn("♻️ Upload / Replace Files", f"up_rep:{pid}", "success"), Btn("📥 Download ZIP", f"p_zip:{pid}")],
        [Btn("🧰 Reinstall Deps", f"p_deps:{pid}"), Btn("🗑 Delete Project", f"p_del:{pid}", "danger")],
        [Btn("⬅️ Back to My Projects", "my:0"), Btn("🏠 Main Menu", "home")],
    ]
    return text, build_keyboard(*rows)

def screen_file_manager(pid: int, page: int):
    meta = get_project_meta(pid)
    files_list = sorted(get_project_files(pid).items())
    text = (
        f"📂 <b>File Explorer — {escape_html(meta['name'])} (#{pid})</b>\n"
        f"{DIVIDER}\n"
        f"📄 <b>Total Files:</b> {len(files_list)} ({format_memory_size(meta.get('total_size', 0))})\n"
        f"<i>Select any file below to inspect code, download, or delete:</i>"
    )
    rows = []
    chunk = files_list[page * ITEMS_PER_PAGE:(page + 1) * ITEMS_PER_PAGE]
    for i, (fname, fbytes) in enumerate(chunk):
        idx = page * ITEMS_PER_PAGE + i
        mark = "🎯 " if fname == meta["main"] else "📄 "
        rows.append([Btn(f"{mark}{fname[-32:]} ({format_memory_size(len(fbytes))})", f"p_fread:{pid}:{idx}:{page}")])

    nav = generate_pagination(f"p_files:{pid}", page, len(files_list))
    if nav:
        rows.append(nav)

    rows.append([Btn("📥 Download Full Project ZIP", f"p_zip:{pid}", "success"), Btn("♻️ Upload Files", f"up_rep:{pid}", "primary")])
    rows.append([Btn("⬅️ Back to Project", f"p:{pid}")])
    return text, build_keyboard(*rows)

def screen_env_manager(pid: int, show_unmasked: bool = True):
    meta = get_project_meta(pid)
    env_vars = list(sorted(meta.get("env_vars", {}).items()))
    text = (
        f"🔑 <b>Environment Variables — {escape_html(meta['name'])}</b>\n"
        f"{DIVIDER}\n"
    )
    rows = []
    if env_vars:
        for idx, (k, v) in enumerate(env_vars):
            val_disp = escape_html(v) if show_unmasked else (escape_html(v[:3] + "***" + v[-2:]) if len(v) > 6 else "***")
            text += f"• <code>{escape_html(k)}</code> = <code>{val_disp}</code>\n"
            rows.append([Btn(f"🗑 Remove {k[:22]}", f"p_env_del:{pid}:{idx}", "danger")])
    else:
        text += "<i>No custom environment variables set.</i>\n"

    rows.append([
        Btn("➕ Add KEY=VALUE", f"p_env_add:{pid}", "success"),
        Btn("👁 Show/Hide Values", f"p_env_eye:{pid}:{0 if show_unmasked else 1}", "primary")
    ])
    rows.append([Btn("⬅️ Back to Project", f"p:{pid}")])
    return text, build_keyboard(*rows)

def screen_settings():
    maint = get_setting("maintenance_mode", "0") == "1"
    max_ram = get_setting("max_ram_mb", str(CHILD_MEM_LIMIT_MB))

    text = (
        f"⚙️ <b>SETTINGS</b>\n"
        f"{DIVIDER}\n"
        f"🛠 <b>Maintenance Mode:</b> {'🟢 ACTIVE' if maint else '⚪ INACTIVE'}\n"
        f"🧠 <b>Per-Project RAM Limit:</b> {max_ram} MB\n"
        f"👑 <b>Owner ID(s):</b> {', '.join(str(i) for i in sorted(ADMIN_IDS))}"
    )

    rows = [
        [Btn(f"🛠 Maintenance Mode: {'ON' if maint else 'OFF'}", "ad_tog:maintenance_mode", "danger" if maint else "primary")],
        [Btn("🧠 Set RAM Limit (MB)", "ad_sval:max_ram_mb", "primary")],
        [Btn("⬅️ Back to Main Menu", "home")]
    ]
    return text, build_keyboard(*rows)

# ==============================================================================
# 10. MESSAGE & MEDIA HANDLERS
# ==============================================================================
@bot_app.message_handler(commands=["start", "menu", "dashboard"])
def cmd_start(message: types.Message):
    if not enforce_access_control(message.from_user, message.chat.id):
        return
    USER_STATE_CACHE.pop(message.from_user.id, None)
    t, mk = screen_main_dashboard(message.from_user.id)
    notify_user(message.chat.id, t, mk)

@bot_app.message_handler(content_types=["photo", "video", "audio", "voice", "VideoNote"])
def handle_other_media(message: types.Message):
    if not enforce_access_control(message.from_user, message.chat.id):
        return
    notify_user(message.chat.id, "ℹ️ Send a <code>.py</code> or <code>.zip</code> file to host a project.", build_keyboard([Btn("🏠 Main Menu", "home", "primary")]))

@bot_app.message_handler(content_types=["document"])
def handle_document_upload(message: types.Message):
    if not enforce_access_control(message.from_user, message.chat.id):
        return

    uid = message.from_user.id
    state = USER_STATE_CACHE.get(uid)
    doc = message.document
    filename = os.path.basename(doc.file_name or "uploaded_file.py")

    if not state or state.get("mode") != "upload":
        state = {"mode": "upload", "files": {}}
        USER_STATE_CACHE[uid] = state

    if (doc.file_size or 0) > 20 * 1024 * 1024:
        notify_user(message.chat.id, "❌ File exceeds Telegram's 20MB bot download limit. Compress your project into a smaller ZIP.")
        return

    try:
        file_info = bot_app.get_file(doc.file_id)
        raw_bytes = bot_app.download_file(file_info.file_path)
        if filename.lower().endswith(".zip"):
            added_files = validate_and_extract_zip(raw_bytes)
        else:
            added_files = {filename: raw_bytes}
    except Exception as e:
        notify_user(message.chat.id, f"❌ <b>Could not read file:</b> <code>{escape_html(str(e)[:250])}</code>")
        return

    state["files"].update(added_files)
    total_sz = sum(len(v) for v in state["files"].values())
    if total_sz > MAX_PROJECT_SIZE_BYTES:
        state["files"] = {}
        notify_user(message.chat.id, f"❌ Project size exceeded {format_memory_size(MAX_PROJECT_SIZE_BYTES)}. Upload reset.")
        return

    notify_user(
        message.chat.id,
        f"✅ <b>Staged {len(added_files)} file(s)!</b>\n"
        f"📁 <b>Total Staged Files:</b> {len(state['files'])} ({format_memory_size(total_sz)})\n\n"
        f"<i>Send additional files (like <code>requirements.txt</code> or <code>.env</code>), or click a button below:</i>",
        build_keyboard(
            [Btn("▶️ Deploy & Start Immediately", "up_run", "success")],
            [Btn("💾 Save Project Only", "up_save", "primary"), Btn("❌ Cancel", "home", "danger")]
        )
    )

@bot_app.message_handler(content_types=["text"])
def handle_text_message(message: types.Message):
    if not enforce_access_control(message.from_user, message.chat.id):
        return

    uid = message.from_user.id
    state = USER_STATE_CACHE.get(uid)
    txt = (message.text or "").strip()

    if not state:
        t, mk = screen_main_dashboard(uid)
        notify_user(message.chat.id, t, mk)
        return

    mode = state.get("mode")

    if mode == "pip":
        pid = state["pid"]
        if not can_access_project(pid, uid):
            return
        pkgs = [x.strip() for x in re.split(r"[\s,]+", txt) if x.strip()]
        if not pkgs or not all(PACKAGE_REQ_REGEX.match(x) for x in pkgs):
            notify_user(message.chat.id, "❌ Invalid package format. Example: <code>requests aiohttp pyTelegramBotAPI</code>")
            return
        USER_STATE_CACHE.pop(uid, None)
        wait_msg = notify_user(message.chat.id, f"📦 <b>Installing:</b> <code>{escape_html(', '.join(pkgs))}</code>...")

        def _pip_worker():
            with INSTALLATION_SEMAPHORE:
                ok, bad = execute_pip_install(pid, pkgs)
            mt = get_project_meta(pid)
            if mt:
                update_project_meta(pid, extra_packages=list(dict.fromkeys(mt.get("extra_packages", []) + ok)))
            rep = "📦 <b>Library Installation Result</b>\n"
            if ok:
                rep += f"✅ <b>Installed:</b> <code>{escape_html(', '.join(ok))}</code>\n"
            if bad:
                rep += f"❌ <b>Failed:</b> <code>{escape_html(', '.join(bad))}</code>\n"
            if pid in ACTIVE_PROCESSES:
                rep += "\nℹ️ <i>Restart the bot to apply newly installed libraries.</i>"
            if wait_msg:
                try:
                    bot_app.edit_message_text(rep, message.chat.id, wait_msg.message_id, reply_markup=build_keyboard([Btn("📋 Back to Project", f"p:{pid}", "primary")]))
                    return
                except Exception:
                    pass
            notify_user(message.chat.id, rep, build_keyboard([Btn("📋 Back to Project", f"p:{pid}", "primary")]))

        threading.Thread(target=_pip_worker, daemon=True).start()
        return

    if mode == "env_add":
        pid = state["pid"]
        if not can_access_project(pid, uid):
            return
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip() and "=" in ln]
        if not lines:
            notify_user(message.chat.id, "❌ Invalid format. Send <code>KEY=VALUE</code> (you can send multiple lines).")
            return
        meta = get_project_meta(pid)
        env_vars = meta.get("env_vars", {})
        for ln in lines:
            k, v = ln.split("=", 1)
            env_vars[k.strip()] = v.strip().strip("\"'")
        update_project_meta(pid, env_vars=env_vars)
        USER_STATE_CACHE.pop(uid, None)
        t, mk = screen_env_manager(pid)
        notify_user(message.chat.id, f"✅ Saved <b>{len(lines)}</b> environment variable(s)!\n\n" + t, mk)
        return

    if mode == "set_val":
        key = state["key"]
        USER_STATE_CACHE.pop(uid, None)
        if key == "max_ram_mb" and not txt.isdigit():
            notify_user(message.chat.id, "❌ Please enter a valid number.", build_keyboard([Btn("⚙️ Back to Settings", "ad_set")]))
            return
        set_setting(key, txt)
        log_audit(uid, "update_setting", key=key, value=txt)
        t, mk = screen_settings()
        notify_user(message.chat.id, "✅ <b>Setting updated!</b>\n\n" + t, mk)
        return

# ==============================================================================
# 11. CALLBACK QUERY ROUTER
# ==============================================================================
@bot_app.callback_query_handler(func=lambda c: True)
def on_callback(call: types.CallbackQuery):
    alert_msg = None
    try:
        if not enforce_access_control(call.from_user, call.message.chat.id):
            bot_app.answer_callback_query(call.id)
            return
        alert_msg = route_callback(call)
    except Exception as e:
        traceback.print_exc()
        alert_msg = f"❌ Error: {str(e)[:120]}"
    try:
        bot_app.answer_callback_query(call.id, alert_msg, show_alert=bool(alert_msg))
    except Exception:
        pass

def _finalize_upload(call: types.CallbackQuery, uid: int, auto_start: bool):
    state = USER_STATE_CACHE.get(uid)
    if not state or state.get("mode") != "upload" or not state.get("files"):
        return "⚠️ Please send your .py or .zip file(s) first!"

    files = state["files"]
    if state.get("pid"):
        pid = state["pid"]
        meta = can_access_project(pid, uid)
        if not meta:
            return "⛔ Unauthorized."
        if pid in ACTIVE_PROCESSES:
            force_stop_project(pid, triggered_by_user=False)
        existing_files = get_project_files(pid)
        existing_files.update(files)
        save_project_files(pid, existing_files)
        upd = {"deps_installed": False}
        if meta["main"] not in existing_files:
            upd["main"] = determine_entry_point(existing_files) or meta["main"]
        update_project_meta(pid, **upd)
    else:
        main_file = determine_entry_point(files)
        if not main_file:
            return "❌ No Python (.py) file found in your upload!"
        proj_name = os.path.splitext(os.path.basename(main_file))[0][:30]
        pid = create_project(uid, proj_name, files, main_file)

    USER_STATE_CACHE.pop(uid, None)

    if auto_start:
        spawn_project_async(pid)

    t, mk = screen_project_dashboard(pid)
    prefix = "🚀 <b>Project Saved & Booting Now!</b>\n\n" if auto_start else "✅ <b>Project Encrypted & Saved!</b>\n\n"
    render_view(call, prefix + t, mk)
    return None

def route_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    parts = call.data.split(":")
    act = parts[0]

    if act == "home":
        USER_STATE_CACHE.pop(uid, None)
        t, mk = screen_main_dashboard(uid)
        render_view(call, t, mk)
        return None

    if act == "help":
        text = (
            f"🆘 <b>PRIVATE BOT MANAGER — GUIDE</b>\n"
            f"{DIVIDER}\n"
            f"1️⃣ <b>Deploy Code:</b> Click <b>🚀 Deploy New Project</b> (or directly send any <code>.py</code> or <code>.zip</code> file to this chat).\n"
            f"2️⃣ <b>Auto-Dependencies:</b> Our AST engine automatically detects imports and installs packages via <code>pip</code>.\n"
            f"3️⃣ <b>Environment Variables:</b> Add <code>.env</code> secrets safely inside <b>🔑 Env Variables</b>.\n"
            f"4️⃣ <b>Persistent Storage:</b> SQLite databases and JSON files created by your bot are automatically synced and encrypted."
        )
        render_view(call, text, build_keyboard([Btn("🏠 Back to Main Menu", "home", "primary")]))
        return None

    if act == "status":
        my_pids = get_project_ids(uid)
        run_cnt = sum(1 for i in my_pids if i in ACTIVE_PROCESSES)
        vm = psutil.virtual_memory()
        text = (
            f"📊 <b>SYSTEM & ACCOUNT STATUS</b>\n"
            f"{DIVIDER}\n"
            f"🤖 <b>Your Projects:</b> {len(my_pids)}\n"
            f"🟢 <b>Running:</b> {run_cnt}  •  🔴 <b>Stopped:</b> {len(my_pids) - run_cnt}\n"
            f"🖥 <b>Node Load:</b> CPU {psutil.cpu_percent(0.1)}% | RAM {vm.percent}%\n"
            f"🔐 <b>Encryption Engine:</b> AES-256-GCM Active"
        )
        render_view(call, text, build_keyboard([Btn("🔃 Refresh", "status", "primary"), Btn("🏠 Main Menu", "home")]))
        return None

    # ── Upload Flows ──
    if act == "up_init":
        USER_STATE_CACHE[uid] = {"mode": "upload", "files": {}}
        text = (
            f"🚀 <b>DEPLOY NEW PROJECT</b>\n"
            f"{DIVIDER}\n"
            f"📤 Send your project files now:\n"
            f"• Single or multiple <code>.py</code> files\n"
            f"• Complete project <code>.zip</code> archive\n"
            f"• Optional: <code>requirements.txt</code>\n\n"
            f"<i>Once uploaded, click <b>▶️ Deploy & Start Immediately</b>!</i>"
        )
        render_view(call, text, build_keyboard([Btn("❌ Cancel", "home", "danger")]))
        return None

    if act == "up_rep":
        pid = int(parts[1])
        if not can_access_project(pid, uid):
            return "⛔ Access denied."
        USER_STATE_CACHE[uid] = {"mode": "upload", "files": {}, "pid": pid}
        render_view(
            call,
            f"♻️ <b>UPDATE / ADD FILES — Project #{pid}</b>\n{DIVIDER}\n"
            f"Send the new <code>.py</code> or <code>.zip</code> files now. Matching filenames will be replaced.",
            build_keyboard([Btn("❌ Cancel", f"p:{pid}", "danger")])
        )
        return None

    if act == "up_save":
        return _finalize_upload(call, uid, auto_start=False)

    if act == "up_run":
        return _finalize_upload(call, uid, auto_start=True)

    # ── Project List & Controls ──
    if act == "my":
        page = int(parts[1])
        pids = get_project_ids(uid)
        rows = []
        for pid in pids[page * ITEMS_PER_PAGE:(page + 1) * ITEMS_PER_PAGE]:
            m = get_project_meta(pid)
            if not m:
                continue
            running = pid in ACTIVE_PROCESSES
            rows.append([Btn(f"{'🟢' if running else '🔴'} {m['name']} (#{pid})", f"p:{pid}", "success" if running else None)])
        nav = generate_pagination("my", page, len(pids))
        if nav:
            rows.append(nav)
        rows.append([Btn("🚀 Deploy New Project", "up_init", "success"), Btn("🏠 Main Menu", "home", "primary")])
        header = f"📂 <b>MY PROJECTS ({len(pids)})</b>\n{DIVIDER}" if pids else "📂 <b>You have not deployed any projects yet.</b>"
        render_view(call, header, build_keyboard(*rows))
        return None

    if act in ("p", "p_run", "p_stop", "p_rst", "p_log", "p_pip", "p_auto", "p_deps", "p_main", "p_smain",
               "p_del", "p_delok", "p_zip", "p_files", "p_fread", "p_fdl", "p_fdel", "p_env", "p_env_add", "p_env_del", "p_env_eye"):
        pid = int(parts[1])
        meta = can_access_project(pid, uid)
        if not meta:
            return "⛔ Project not found or access denied."

        if act == "p":
            t, mk = screen_project_dashboard(pid)
            render_view(call, t, mk)
            return None

        if act == "p_run":
            if pid in ACTIVE_PROCESSES or pid in STARTING_PROCESSES:
                return "⚠️ Project is already running!"
            spawn_project_async(pid)
            return "🚀 Starting..."

        if act == "p_stop":
            force_stop_project(pid, triggered_by_user=True)
            t, mk = screen_project_dashboard(pid)
            render_view(call, t, mk)
            return "⏹ Stopped."

        if act == "p_rst":
            force_stop_project(pid, triggered_by_user=False)
            spawn_project_async(pid)
            return "🔄 Restarting..."

        if act == "p_log":
            r = ACTIVE_PROCESSES.get(pid)
            raw_log = read_tail(r["log_path"], 3500) if r else PROCESS_LOG_CACHE.get(pid, "")
            clean_log = escape_html(raw_log[-3200:]) if raw_log.strip() else "(No console logs generated yet)"
            render_view(
                call,
                f"📜 <b>CONSOLE LOGS — {escape_html(meta['name'])} (#{pid})</b>\n{DIVIDER}\n<pre>{clean_log}</pre>",
                build_keyboard(
                    [Btn("🔃 Refresh Logs", f"p_log:{pid}", "primary"), Btn("📥 Download Full Log", f"p_dl_log:{pid}", "success")],
                    [Btn("⬅️ Back to Project", f"p:{pid}")]
                )
            )
            return None

        if act == "p_pip":
            USER_STATE_CACHE[uid] = {"mode": "pip", "pid": pid}
            render_view(
                call,
                f"📦 <b>INSTALL PYTHON LIBRARIES — {escape_html(meta['name'])}</b>\n{DIVIDER}\n"
                f"Send package names separated by spaces:\n<code>pyTelegramBotAPI requests aiohttp</code>",
                build_keyboard([Btn("❌ Cancel", f"p:{pid}", "danger")])
            )
            return None

        if act == "p_auto":
            update_project_meta(pid, auto_restart=not meta.get("auto_restart"))
            t, mk = screen_project_dashboard(pid)
            render_view(call, t, mk)
            return None

        if act == "p_deps":
            if pid in ACTIVE_PROCESSES:
                return "⏹ Stop the bot before resetting dependencies."
            shutil.rmtree(VENVS_DIR / str(pid), ignore_errors=True)
            update_project_meta(pid, deps_installed=False, failed_packages=[])
            AUTO_HEAL_ATTEMPTS.pop(pid, None)
            return "🧰 Dependencies reset! They will be freshly installed on next start."

        if act == "p_main":
            py_files = sorted(n for n in get_project_files(pid) if n.endswith(".py"))[:30]
            rows = [[Btn(("✅ " if n == meta["main"] else "🐍 ") + n, f"p_smain:{pid}:{i}")] for i, n in enumerate(py_files)]
            rows.append([Btn("⬅️ Back to Project", f"p:{pid}")])
            render_view(call, "🎯 <b>Select Entrypoint (.py) File:</b>", build_keyboard(*rows))
            return None

        if act == "p_smain":
            idx = int(parts[2])
            py_files = sorted(n for n in get_project_files(pid) if n.endswith(".py"))
            if 0 <= idx < len(py_files):
                update_project_meta(pid, main=py_files[idx])
            t, mk = screen_project_dashboard(pid)
            render_view(call, t, mk)
            return "✅ Entrypoint updated!"

        if act == "p_zip":
            r = ACTIVE_PROCESSES.get(pid)
            if r:
                synchronize_project_state(pid, r["run_dir"])
            files = get_project_files(pid)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fn, fb in files.items():
                    z.writestr(fn, fb)
            bot_app.send_document(
                chat_id,
                create_memory_document(f"Project_{pid}_{meta['name']}.zip", buf.getvalue()),
                caption=f"📦 <b>Full Project Backup:</b> {escape_html(meta['name'])} (#{pid})"
            )
            return "📥 Sending ZIP archive..."

        if act == "p_files":
            page = int(parts[2])
            t, mk = screen_file_manager(pid, page)
            render_view(call, t, mk)
            return None

        if act == "p_fread":
            idx, page = int(parts[2]), int(parts[3])
            files_list = sorted(get_project_files(pid).items())
            if not (0 <= idx < len(files_list)):
                return "❌ File not found."
            fname, fbytes = files_list[idx]
            try:
                decoded = fbytes.decode("utf-8")
                preview = escape_html(decoded[:3200]) + ("\n\n... [TRUNCATED — DOWNLOAD TO VIEW FULL FILE]" if len(decoded) > 3200 else "")
                body = f"<pre>{preview}</pre>"
            except UnicodeDecodeError:
                body = "<i>(Binary / Non-text file — click Download below to inspect)</i>"

            render_view(
                call,
                f"📄 <b>File:</b> <code>{escape_html(fname)}</code> ({format_memory_size(len(fbytes))})\n{DIVIDER}\n{body}",
                build_keyboard(
                    [Btn("📥 Download File", f"p_fdl:{pid}:{idx}", "success"),
                     Btn("🗑 Delete File", f"p_fdel:{pid}:{idx}:{page}", "danger")],
                    [Btn("⬅️ Back to Files", f"p_files:{pid}:{page}", "primary")]
                )
            )
            return None

        if act == "p_fdl":
            idx = int(parts[2])
            files_list = sorted(get_project_files(pid).items())
            if not (0 <= idx < len(files_list)):
                return "❌ File not found."
            fname, fbytes = files_list[idx]
            bot_app.send_document(chat_id, create_memory_document(fname, fbytes), caption=f"📄 <code>{escape_html(fname)}</code> (Project #{pid})")
            return "📥 File sent!"

        if act == "p_fdel":
            idx, page = int(parts[2]), int(parts[3])
            if pid in ACTIVE_PROCESSES:
                return "⏹ Stop the project before deleting files."
            files_dict = get_project_files(pid)
            files_list = sorted(files_dict.items())
            if 0 <= idx < len(files_list) and len(files_list) > 1:
                fname = files_list[idx][0]
                files_dict.pop(fname, None)
                save_project_files(pid, files_dict)
                t, mk = screen_file_manager(pid, page)
                render_view(call, t, mk)
                return f"🗑 Deleted {fname}"
            return "⚠️ Cannot delete the last remaining file."

        if act == "p_env":
            t, mk = screen_env_manager(pid)
            render_view(call, t, mk)
            return None

        if act == "p_env_eye":
            show_flag = bool(int(parts[2]))
            t, mk = screen_env_manager(pid, show_unmasked=show_flag)
            render_view(call, t, mk)
            return None

        if act == "p_env_add":
            USER_STATE_CACHE[uid] = {"mode": "env_add", "pid": pid}
            render_view(
                call,
                f"🔑 <b>ADD ENVIRONMENT VARIABLES</b>\n{DIVIDER}\n"
                f"Send one or more lines in <code>KEY=VALUE</code> format:\n<pre>BOT_TOKEN=123456:ABC\nADMIN_ID=7813450584</pre>",
                build_keyboard([Btn("❌ Cancel", f"p_env:{pid}", "danger")])
            )
            return None

        if act == "p_env_del":
            idx = int(parts[2])
            env_vars = meta.get("env_vars", {})
            keys = sorted(env_vars.keys())
            if 0 <= idx < len(keys):
                env_vars.pop(keys[idx], None)
                update_project_meta(pid, env_vars=env_vars)
            t, mk = screen_env_manager(pid)
            render_view(call, t, mk)
            return "🗑 Variable removed."

        if act == "p_del":
            render_view(
                call,
                f"⚠️ <b>CONFIRM DELETION</b>\n{DIVIDER}\nAre you sure you want to permanently delete <b>{escape_html(meta['name'])} (#{pid})</b>?",
                build_keyboard([Btn("✅ Yes, Delete Forever", f"p_delok:{pid}", "danger"), Btn("❌ Cancel", f"p:{pid}", "success")])
            )
            return None

        if act == "p_delok":
            force_stop_project(pid)
            delete_project(pid)
            log_audit(uid, "delete_project", pid=pid)
            t, mk = screen_main_dashboard(uid)
            render_view(call, "🗑 <b>Project deleted successfully.</b>\n\n" + t, mk)
            return None

    if act == "p_dl_log":
        pid = int(parts[1])
        meta = can_access_project(pid, uid)
        if not meta:
            return "⛔ Unauthorized."
        r = ACTIVE_PROCESSES.get(pid)
        raw_log = read_tail(r["log_path"], 500 * 1024) if r else PROCESS_LOG_CACHE.get(pid, "")
        if not raw_log.strip():
            return "⚠️ Log is currently empty."
        bot_app.send_document(chat_id, create_memory_document(f"project_{pid}_logs.txt", raw_log.encode("utf-8", "ignore")))
        return "📥 Log file sent!"

    # ── Settings / Admin-style Routes (owner only — enforced above) ──
    if act == "ad_set":
        t, mk = screen_settings()
        render_view(call, t, mk)
        return None

    if act == "ad_tog":
        key = parts[1]
        cur = get_setting(key, "0")
        set_setting(key, "0" if cur == "1" else "1")
        log_audit(uid, "toggle_setting", key=key)
        t, mk = screen_settings()
        render_view(call, t, mk)
        return "✅ Updated!"

    if act == "ad_sval":
        key = parts[1]
        USER_STATE_CACHE[uid] = {"mode": "set_val", "key": key}
        render_view(call, f"✏️ <b>Enter new value for <code>{escape_html(key)}</code>:</b>", build_keyboard([Btn("❌ Cancel", "ad_set", "danger")]))
        return None

    if act == "ad_bak":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if DB_PATH.exists():
                z.write(DB_PATH, "vault_core.db")
            if KEY_FILE.exists():
                z.write(KEY_FILE, "master.key")
        bot_app.send_document(
            chat_id,
            create_memory_document(f"Private_Bot_Backup_{int(time.time())}.zip", buf.getvalue()),
            caption="🗄 <b>Full Backup (Database + Master Key)</b>\n⚠️ <i>Keep this file strictly confidential!</i>"
        )
        log_audit(uid, "export_backup")
        return "📥 Backup sent!"

    if act == "ad_au":
        page = int(parts[1])
        rows_raw = execute_query("SELECT encrypted_data FROM audit_logs ORDER BY id DESC LIMIT 100")
        logs = []
        for (blob,) in rows_raw:
            try:
                x = json_decrypt(blob, b"audit_log")
                ts = time.strftime("%m-%d %H:%M", time.gmtime(x.get("timestamp", 0)))
                extras = " ".join(f"{k}={v}" for k, v in x.items() if k not in ("timestamp", "admin_id", "action"))
                logs.append(f"{ts} | {x.get('action')} | {extras[:45]}")
            except Exception:
                pass
        body = escape_html("\n".join(logs[:25])) if logs else "No audit entries recorded."
        render_view(
            call,
            f"🧾 <b>AUDIT LOGS (Latest 25)</b>\n{DIVIDER}\n<pre>{body}</pre>",
            build_keyboard([Btn("🔃 Refresh", "ad_au:0", "primary"), Btn("🏠 Main Menu", "home")])
        )
        return None

    if act == "ad_stop_all":
        stopped = len(ACTIVE_PROCESSES)
        for pid in list(ACTIVE_PROCESSES):
            force_stop_project(pid)
        log_audit(uid, "stop_all_bots", count=stopped)
        t, mk = screen_main_dashboard(uid)
        render_view(call, t, mk)
        return f"🚨 Stopped all {stopped} running bots."

    return None

# ==============================================================================
# 12. BOOT SEQUENCE & BACKGROUND MONITOR
# ==============================================================================
def spawn_project_async(pid: int, quiet_mode: bool = False):
    threading.Thread(target=initiate_project_boot, args=(pid, quiet_mode), daemon=True).start()

def initiate_project_boot(pid: int, quiet_mode: bool = False):
    with RUNTIME_LOCK:
        if pid in ACTIVE_PROCESSES or pid in STARTING_PROCESSES:
            return
        STARTING_PROCESSES.add(pid)

    owner_id = None
    try:
        meta = get_project_meta(pid)
        if not meta:
            return
        owner_id = meta["owner"]
        project_files = get_project_files(pid)
        if not project_files:
            if not quiet_mode:
                notify_user(owner_id, "❌ <b>Cannot start project:</b> No files found.")
            return

        if not meta.get("deps_installed"):
            status_msg = None
            if not quiet_mode:
                status_msg = notify_user(
                    owner_id,
                    "🧰 <b>Scanning & Installing Dependencies...</b>\n⏳ <i>Please wait while libraries are configured.</i>"
                )
            discovered_reqs = extract_requirements_txt(project_files) or infer_project_requirements(project_files)
            combined_reqs = list(dict.fromkeys(discovered_reqs + meta.get("extra_packages", [])))

            with INSTALLATION_SEMAPHORE:
                installed_ok, installed_failed = execute_pip_install(pid, combined_reqs)

            meta = update_project_meta(pid, deps_installed=True, failed_packages=installed_failed)
            if not quiet_mode:
                result_text = f"✅ <b>Dependencies Ready</b> ({len(installed_ok)} packages configured)"
                if installed_failed:
                    result_text += f"\n⚠️ <b>Skipped/Failed:</b> <code>{escape_html(', '.join(installed_failed))}</code>"
                if status_msg:
                    try:
                        bot_app.edit_message_text(result_text, owner_id, status_msg.message_id)
                    except Exception:
                        notify_user(owner_id, result_text)

        run_hash = secrets.token_hex(4)
        run_directory = RUN_BASE_DIR / f"p_{pid}_{run_hash}"
        run_directory.mkdir(parents=True, exist_ok=True)
        _safe_chmod(run_directory, 0o700)

        write_virtual_filesystem(run_directory, project_files)
        sandbox_kwargs, target_uid = generate_sandbox_credentials(pid)
        apply_directory_ownership(run_directory, target_uid)

        log_filepath = str(RUN_BASE_DIR / f"p_{pid}_{run_hash}.log")
        log_handle = open(log_filepath, "ab")
        _safe_chmod(log_filepath, 0o600)

        venv_path = str(VENVS_DIR / str(pid))
        existing_pypath = os.environ.get("PYTHONPATH", "")
        full_pypath = f"{venv_path}:{existing_pypath}" if existing_pypath else venv_path

        execution_env = os.environ.copy()
        execution_env.update({
            "PATH": CLEAN_EXEC_PATH,
            "HOME": str(run_directory),
            "TMPDIR": str(run_directory),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": full_pypath
        })
        for k, v in meta.get("env_vars", {}).items():
            execution_env[str(k)] = str(v)

        process = subprocess.Popen(
            [sys.executable, "-u", meta["main"]],
            cwd=str(run_directory),
            env=execution_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            **sandbox_kwargs
        )

        with RUNTIME_LOCK:
            ACTIVE_PROCESSES[pid] = {
                "process": process,
                "run_dir": run_directory,
                "log_file_handle": log_handle,
                "log_path": log_filepath,
                "start_time": time.time(),
                "is_stopping": False
            }

        update_project_meta(pid, was_running=True)

        if not quiet_mode:
            notify_user(
                owner_id,
                f"🟢 <b>{escape_html(meta['name'])} is now ONLINE!</b>\n"
                f"🆔 <b>PID:</b> <code>{process.pid}</code>\n"
                f"🎯 <b>Main:</b> <code>{escape_html(meta['main'])}</code>",
                build_keyboard([Btn("📋 Open Project Control", f"p:{pid}", "primary")])
            )
    except Exception as e:
        traceback.print_exc()
        if owner_id:
            notify_user(owner_id, f"❌ <b>Failed to start project #{pid}:</b>\n<pre>{escape_html(str(e)[:600])}</pre>")
    finally:
        with RUNTIME_LOCK:
            STARTING_PROCESSES.discard(pid)

def global_process_monitor():
    last_sync_timestamp = time.time()
    while True:
        time.sleep(4)
        max_ram_bytes = int(get_setting("max_ram_mb", str(CHILD_MEM_LIMIT_MB))) * 1024 * 1024

        for pid, proc_data in list(ACTIVE_PROCESSES.items()):
            try:
                proc = proc_data["process"]
                if proc.poll() is None:
                    try:
                        if os.path.getsize(proc_data["log_path"]) > 8 * 1024 * 1024:
                            tail_data = read_tail(proc_data["log_path"], 300 * 1024)
                            with open(proc_data["log_path"], "r+b") as lf:
                                lf.truncate(0)
                                lf.write(b"--- [LOG ROTATED] ---\n" + tail_data.encode("utf-8", "ignore"))
                    except Exception:
                        pass

                    try:
                        ps_p = psutil.Process(proc.pid)
                        rss = ps_p.memory_info().rss + sum(c.memory_info().rss for c in ps_p.children(recursive=True))
                        if rss > max_ram_bytes:
                            meta = get_project_meta(pid)
                            force_stop_project(pid, triggered_by_user=True)
                            if meta:
                                notify_user(
                                    meta["owner"],
                                    f"🛑 <b>{escape_html(meta['name'])}</b> exceeded the memory limit "
                                    f"({format_memory_size(rss)}) and was stopped."
                                )
                            continue
                    except Exception:
                        pass
                    continue

                if proc_data.get("is_stopping"):
                    continue

                exit_code = proc.returncode
                cleanup_and_finalize_process(pid)
                handle_unexpected_exit(pid, exit_code)
            except Exception as e:
                print(f"[!] Monitor error on PID {pid}: {e}")

        if time.time() - last_sync_timestamp > 450:
            last_sync_timestamp = time.time()
            for pid, proc_data in list(ACTIVE_PROCESSES.items()):
                synchronize_project_state(pid, proc_data["run_dir"])

def handle_unexpected_exit(pid: int, exit_code: int):
    meta = get_project_meta(pid)
    if not meta:
        return
    owner_id = meta["owner"]
    last_log_tail = PROCESS_LOG_CACHE.get(pid, "")

    missing_mod = re.search(r"No module named ['\"]([\w.\-]+)['\"]", last_log_tail)
    if missing_mod and AUTO_HEAL_ATTEMPTS.get(pid, 0) < 3:
        root_module = missing_mod.group(1).split(".")[0]
        mapped_pkg = COMMON_PACKAGE_ALIASES.get(root_module, root_module).split()
        if all(PACKAGE_REQ_REGEX.match(p) for p in mapped_pkg):
            AUTO_HEAL_ATTEMPTS[pid] = AUTO_HEAL_ATTEMPTS.get(pid, 0) + 1
            notify_user(
                owner_id,
                f"🩹 <b>Auto-Heal Triggered:</b> Installing missing module <code>{escape_html(root_module)}</code>..."
            )
            with INSTALLATION_SEMAPHORE:
                ok_pkgs, _ = execute_pip_install(pid, mapped_pkg)
            update_project_meta(pid, extra_packages=list(dict.fromkeys(meta.get("extra_packages", []) + ok_pkgs)))
            spawn_project_async(pid, quiet_mode=True)
            return

    if exit_code == 0:
        update_project_meta(pid, was_running=False)
        notify_user(
            owner_id,
            f"✅ <b>{escape_html(meta['name'])}</b> finished execution cleanly (Exit 0).",
            build_keyboard([Btn("📋 Open Project", f"p:{pid}", "primary")])
        )
        return

    crashes = [t for t in CRASH_HISTORY.get(pid, []) if current_timestamp() - t < 600] + [current_timestamp()]
    CRASH_HISTORY[pid] = crashes
    short_err = escape_html(last_log_tail[-900:]) if last_log_tail.strip() else "Process exited unexpectedly."

    if meta.get("auto_restart") and len(crashes) < 5:
        notify_user(
            owner_id,
            f"💥 <b>{escape_html(meta['name'])}</b> crashed (Exit Code: {exit_code}).\n"
            f"🔄 <i>Auto-restarting in 3 seconds...</i>\n\n<pre>{short_err}</pre>"
        )
        threading.Timer(3.0, spawn_project_async, args=(pid, True)).start()
    else:
        update_project_meta(pid, was_running=False)
        notify_user(
            owner_id,
            f"🛑 <b>{escape_html(meta['name'])}</b> crashed repeatedly and was stopped.\n\n<pre>{short_err}</pre>",
            build_keyboard([Btn("📋 Open Project & Logs", f"p:{pid}", "danger")])
        )

def grace_shutdown(*_):
    for pid in list(ACTIVE_PROCESSES):
        try:
            force_stop_project(pid, triggered_by_user=False)
        except Exception:
            pass
    os._exit(0)

def system_autostart_routine():
    time.sleep(3)
    for pid in get_project_ids():
        try:
            meta = get_project_meta(pid)
            if not meta:
                continue
            if meta.get("was_running") and meta.get("auto_restart"):
                spawn_project_async(pid, quiet_mode=True)
                time.sleep(1.2)
        except Exception as e:
            print(f"[!] Autostart error on PID {pid}: {e}")

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, grace_shutdown)
    signal.signal(signal.SIGINT, grace_shutdown)

    threading.Thread(target=global_process_monitor, daemon=True).start()
    threading.Thread(target=system_autostart_routine, daemon=True).start()

    try:
        me = bot_app.get_me()
        print("\n" + "=" * 54)
        print(f"🛠️ PRIVATE BOT MANAGER ONLINE: @{me.username} (ID: {me.id})")
        print(f"👑 Owner ID(s): {sorted(ADMIN_IDS)}")
        print("=" * 54 + "\n")
    except Exception as e:
        print(f"[!] Startup warning: {e}")

    while True:
        try:
            bot_app.infinity_polling(timeout=40, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            print(f"[!] Polling error: {e}")
            time.sleep(4)
