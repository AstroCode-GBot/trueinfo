import os
import re
import json
import hmac
import hashlib
import secrets
import sqlite3
import threading
import time
import urllib.parse
import base64
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, abort
)
from dotenv import load_dotenv
import telebot
import jwt
from jwt import PyJWKClient

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_UID = int(os.getenv("ADMIN_UID", "0") or 0)

BOT_USERNAME = os.getenv("BOT_USERNAME", "TrueInfoTBot").lstrip("@")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
TELEGRAM_CLIENT_ID = os.getenv("TELEGRAM_CLIENT_ID", "").strip()
TELEGRAM_CLIENT_SECRET = os.getenv("TELEGRAM_CLIENT_SECRET", "").strip()
TELEGRAM_OIDC_AUTH = "https://oauth.telegram.org/auth"
TELEGRAM_OIDC_TOKEN = "https://oauth.telegram.org/token"
TELEGRAM_OIDC_ISSUER = "https://oauth.telegram.org"
TELEGRAM_OIDC_JWKS = "https://oauth.telegram.org/.well-known/jwks.json"

TRUE_API_URL = os.getenv(
    "TRUE_API_URL",
    "https://darktoolshub.site/bot/trueapi.php?num={number}"
)
TG_TO_PHONE_API = os.getenv(
    "TG_TO_PHONE_API",
    "https://telegram-tophone.smallgmrzk.workers.dev/api/developer/"
    "Cyb3rB4nn3r/fast?key={key}&userid={userid}"
)
TG_TO_PHONE_KEY = os.getenv("TG_TO_PHONE_KEY", "")

SMS_API_URL = os.getenv(
    "SMS_API_URL",
    "http://kalyanking.baribd.eu.cc/co.php?number={number}&msg={message}"
)

APP_SECRET = os.getenv("APP_SECRET") or secrets.token_hex(32)
DB_PATH = os.getenv("DB_PATH", "trueinfo.db")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))
MAX_LOOKUPS_PER_MIN = int(os.getenv("MAX_LOOKUPS_PER_MIN", "10"))
PREMIUM_LOOKUPS_PER_DAY = int(os.getenv("PREMIUM_LOOKUPS_PER_DAY", "100"))
SMS_PER_DAY = int(os.getenv("SMS_PER_DAY", "5"))

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("COOKIE_SECURE", "1") == "1")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None
bot_thread = None

db_lock = threading.RLock()
rate_lock = threading.RLock()
rate_buckets = {}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            photo_url TEXT,
            is_premium INTEGER NOT NULL DEFAULT 0,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_platform TEXT,
            website_logins INTEGER NOT NULL DEFAULT 0,
            bot_uses INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS lookups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            source TEXT NOT NULL,
            query_value TEXT NOT NULL,
            result_json TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sms_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            number TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            response TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lookup_tg ON lookups(tg_id);
        CREATE INDEX IF NOT EXISTS idx_lookup_created ON lookups(created_at);
        """)

        defaults = {
            "bot_enabled": "1",
            "popup_enabled": "0",
            "popup_notice": "",
        }
        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)",
                (k, v)
            )
        con.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def setting(key, default=None):
    with db_lock, db() as con:
        row = con.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db_lock, db() as con:
        con.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        con.commit()


def upsert_user(tg_id, username="", first_name="", last_name="",
                photo_url="", platform="web"):
    ts = now_iso()
    with db_lock, db() as con:
        existing = con.execute(
            "SELECT * FROM users WHERE tg_id=?", (int(tg_id),)
        ).fetchone()
        if existing:
            con.execute("""
                UPDATE users
                SET username=?, first_name=?, last_name=?, photo_url=?,
                    last_seen=?, last_platform=?,
                    website_logins=website_logins + ?
                WHERE tg_id=?
            """, (
                username or existing["username"],
                first_name or existing["first_name"],
                last_name or existing["last_name"],
                photo_url or existing["photo_url"],
                ts, platform,
                1 if platform == "web" else 0,
                int(tg_id)
            ))
        else:
            con.execute("""
                INSERT INTO users
                (tg_id, username, first_name, last_name, photo_url,
                 first_seen, last_seen, last_platform, website_logins, bot_uses)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                int(tg_id), username, first_name, last_name, photo_url,
                ts, ts, platform, 1 if platform == "web" else 0, 
                1 if platform == "bot" else 0
            ))
        con.commit()


def touch_bot_user(tg_user):
    upsert_user(
        tg_user.id,
        tg_user.username or "",
        tg_user.first_name or "",
        tg_user.last_name or "",
        getattr(tg_user, "photo_url", "") or "",
        "bot"
    )


def get_user(tg_id):
    with db_lock, db() as con:
        return con.execute(
            "SELECT * FROM users WHERE tg_id=?", (int(tg_id),)
        ).fetchone()


def is_blocked(tg_id):
    row = get_user(tg_id)
    return bool(row and row["is_blocked"])


def is_premium(tg_id):
    row = get_user(tg_id)
    return bool(row and row["is_premium"])


def save_lookup(tg_id, source, query_value, result, success):
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO lookups
            (tg_id, source, query_value, result_json, success, created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            tg_id,
            source,
            query_value,
            json.dumps(result, ensure_ascii=False, default=str),
            1 if success else 0,
            now_iso()
        ))
        con.commit()


def save_sms(tg_id, number, message, status, response):
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO sms_logs
            (tg_id, number, message, status, response, created_at)
            VALUES(?,?,?,?,?,?)
        """, (tg_id, number, message, status, response[:4000], now_iso()))
        con.commit()


def clean_phone(value):
    value = (value or "").strip()
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00880"):
        digits = "880" + digits[5:]
    elif digits.startswith("01"):
        digits = "880" + digits[1:]
    if not digits.startswith("880"):
        raise ValueError("বাংলাদেশের নম্বর দিন, যেমন 01XXXXXXXXX")
    if len(digits) != 13 or not digits.startswith("8801"):
        raise ValueError("সঠিক ১১-সংখ্যার বাংলাদেশি মোবাইল নম্বর দিন")
    return "+" + digits


def normalize_for_external_api(phone):
    return phone.replace(" ", "")


def safe_json_response(resp):
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:8000]}


def external_true_lookup(phone):
    url = TRUE_API_URL.format(
        number=urllib.parse.quote(normalize_for_external_api(phone), safe="")
    )
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return safe_json_response(resp)


def external_tg_to_phone(tg_id):
    if not TG_TO_PHONE_KEY:
        raise RuntimeError("TG_TO_PHONE_KEY সেট করা হয়নি")
    url = TG_TO_PHONE_API.format(
        key=urllib.parse.quote(TG_TO_PHONE_KEY, safe=""),
        userid=urllib.parse.quote(str(tg_id), safe="")
    )
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return safe_json_response(resp)


def external_sms(number, message):
    url = SMS_API_URL.format(
        number=urllib.parse.quote(number, safe=""),
        message=urllib.parse.quote(message, safe="")
    )
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return safe_json_response(resp)


def rate_limit(key, limit, window=60):
    now = time.time()
    with rate_lock:
        bucket = rate_buckets.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def daily_count(table, tg_id):
    day = datetime.now(timezone.utc).date().isoformat()
    with db_lock, db() as con:
        if table == "lookups":
            row = con.execute(
                "SELECT COUNT(*) c FROM lookups "
                "WHERE tg_id=? AND created_at LIKE ?",
                (tg_id, day + "%")
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) c FROM sms_logs "
                "WHERE tg_id=? AND created_at LIKE ?",
                (tg_id, day + "%")
            ).fetchone()
        return int(row["c"])


def current_tg_id():
    return int(session["tg_id"]) if session.get("tg_id") else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_tg_id():
            return jsonify({"ok": False, "error": "login_required"}), 401
        row = get_user(current_tg_id())
        if not row or row["is_blocked"]:
            session.clear()
            return jsonify({"ok": False, "error": "blocked"}), 403
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        tg_id = current_tg_id()
        if not tg_id:
            return redirect(url_for("telegram_login_start", next=request.path))
        if tg_id != ADMIN_UID:
            return abort(403)
        return fn(*args, **kwargs)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@app.route("/")
def home():
    return render_template(
        "index.html",
        user=get_user(current_tg_id()) if current_tg_id() else None,
        bot_username=BOT_USERNAME,
        popup_enabled=setting("popup_enabled", "0") == "1",
        popup_notice=setting("popup_notice", "")
    )


@app.route("/auth/telegram/start")
def telegram_login_start():
    if not TELEGRAM_CLIENT_ID or not TELEGRAM_CLIENT_SECRET:
        return "Telegram Login is not configured. Add TELEGRAM_CLIENT_ID and TELEGRAM_CLIENT_SECRET.", 503

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    session["tg_oauth_state"] = state
    session["tg_oauth_verifier"] = verifier
    redirect_uri = PUBLIC_URL + "/auth/telegram/callback"

    params = {
        "client_id": TELEGRAM_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(
        TELEGRAM_OIDC_AUTH + "?" + urllib.parse.urlencode(params)
    )


@app.route("/auth/telegram/callback")
def telegram_login_callback():
    error = request.args.get("error")
    if error:
        return redirect(url_for("home"))

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = session.pop("tg_oauth_state", None)
    verifier = session.pop("tg_oauth_verifier", None)

    if not code or not verifier or not expected_state or not hmac.compare_digest(
        state, expected_state
    ):
        return "Invalid Telegram login state.", 400

    redirect_uri = PUBLIC_URL + "/auth/telegram/callback"

    try:
        token_resp = requests.post(
            TELEGRAM_OIDC_TOKEN,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": TELEGRAM_CLIENT_ID,
                "code_verifier": verifier,
            },
            auth=(TELEGRAM_CLIENT_ID, TELEGRAM_CLIENT_SECRET),
            timeout=REQUEST_TIMEOUT,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        id_token = token_data["id_token"]

        jwks_client = PyJWKClient(TELEGRAM_OIDC_JWKS)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)

        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=TELEGRAM_CLIENT_ID,
            issuer=TELEGRAM_OIDC_ISSUER,
        )

        tg_id = int(claims.get("id") or claims.get("sub"))
        name = claims.get("name", "")
        first_name = claims.get(
            "given_name",
            name.split(" ", 1)[0] if name else ""
        )
        last_name = claims.get("family_name", "")
        username = claims.get("preferred_username", "")
        photo_url = claims.get("picture", "")

        upsert_user(
            tg_id, username, first_name, last_name, photo_url, "web"
        )
        row = get_user(tg_id)
        if row["is_blocked"]:
            session.clear()
            return "Your account is blocked.", 403

        session["tg_id"] = tg_id
        session["login_method"] = "telegram_oidc"
        return redirect(url_for("home"))
    except Exception:
        return "Telegram login verification failed.", 400


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/status")
def status():
    tg_id = current_tg_id()
    row = get_user(tg_id) if tg_id else None
    return jsonify({
        "ok": True,
        "logged_in": bool(row),
        "premium": bool(row and row["is_premium"]),
        "admin": bool(tg_id and tg_id == ADMIN_UID),
        "blocked": bool(row and row["is_blocked"]),
        "bot_enabled": setting("bot_enabled", "1") == "1"
    })


@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    tg_id = current_tg_id()
    if tg_id and is_blocked(tg_id):
        return jsonify({"ok": False, "error": "Your account is blocked"}), 403

    if not rate_limit(f"lookup:{request.remote_addr}", MAX_LOOKUPS_PER_MIN):
        return jsonify({"ok": False, "error": "Too many requests. Try again later."}), 429

    try:
        phone = clean_phone((request.get_json(silent=True) or {}).get("number"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        data = external_true_lookup(phone)
        save_lookup(tg_id, "website", phone, data, True)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        save_lookup(tg_id, "website", phone, {"error": str(e)}, False)
        return jsonify({"ok": False, "error": "Lookup API failed"}), 502


@app.route("/api/premium/tg-to-phone", methods=["POST"])
@login_required
def api_tg_to_phone():
    tg_id = current_tg_id()
    if not is_premium(tg_id):
        return jsonify({"ok": False, "error": "Premium required"}), 403

    if daily_count("lookups", tg_id) >= PREMIUM_LOOKUPS_PER_DAY:
        return jsonify({"ok": False, "error": "Daily premium limit reached"}), 429

    body = request.get_json(silent=True) or {}
    target = str(body.get("userid", "")).strip()
    if not target.isdigit() or len(target) > 20:
        return jsonify({"ok": False, "error": "Invalid Telegram user ID"}), 400

    try:
        data = external_tg_to_phone(target)
        save_lookup(tg_id, "tg-to-phone", target, data, True)
        return jsonify({"ok": True, "data": data})
    except Exception:
        save_lookup(tg_id, "tg-to-phone", target, {"error": "API failed"}, False)
        return jsonify({"ok": False, "error": "Premium API failed"}), 502


@app.route("/api/premium/sms", methods=["POST"])
@login_required
def api_sms():
    tg_id = current_tg_id()
    if not is_premium(tg_id):
        return jsonify({"ok": False, "error": "Premium required"}), 403

    if daily_count("sms", tg_id) >= SMS_PER_DAY:
        return jsonify({"ok": False, "error": "Daily SMS limit reached"}), 429

    body = request.get_json(silent=True) or {}
    number = body.get("number", "")
    message = body.get("message", "").strip()

    try:
        number = clean_phone(number)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not message or len(message) > 300:
        return jsonify({"ok": False, "error": "Message must be 1-300 characters"}), 400

    if not rate_limit(f"sms:{tg_id}", 2, 60):
        return jsonify({"ok": False, "error": "Please wait before sending another SMS"}), 429

    try:
        result = external_sms(number, message)
        save_sms(tg_id, number, message, "success", json.dumps(result, ensure_ascii=False))
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        save_sms(tg_id, number, message, "failed", str(e))
        return jsonify({"ok": False, "error": "SMS API failed"}), 502


@app.route("/api/history")
@login_required
def api_history():
    tg_id = current_tg_id()
    with db_lock, db() as con:
        rows = con.execute("""
            SELECT id, source, query_value, result_json, success, created_at
            FROM lookups WHERE tg_id=?
            ORDER BY id DESC LIMIT 100
        """, (tg_id,)).fetchall()
    return jsonify({
        "ok": True,
        "items": [dict(r) for r in rows]
    })


@app.route("/adm")
@admin_required
def admin():
    with db_lock, db() as con:
        stats = {
            "users": con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
            "logged_users": con.execute(
                "SELECT COUNT(*) c FROM users WHERE website_logins > 0"
            ).fetchone()["c"],
            "lookups": con.execute("SELECT COUNT(*) c FROM lookups").fetchone()["c"],
            "premium": con.execute(
                "SELECT COUNT(*) c FROM users WHERE is_premium=1"
            ).fetchone()["c"],
        }
    return render_template(
        "admin.html",
        stats=stats,
        bot_enabled=setting("bot_enabled", "1") == "1",
        popup_enabled=setting("popup_enabled", "0") == "1",
        popup_notice=setting("popup_notice", "")
    )


@app.route("/adm/api/users")
@admin_required
def admin_users():
    q = (request.args.get("q") or "").strip()
    with db_lock, db() as con:
        if q:
            rows = con.execute("""
                SELECT * FROM users
                WHERE CAST(tg_id AS TEXT) LIKE ?
                   OR username LIKE ?
                   OR first_name LIKE ?
                ORDER BY last_seen DESC LIMIT 300
            """, (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM users ORDER BY last_seen DESC LIMIT 300"
            ).fetchall()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@app.route("/adm/api/user/<int:tg_id>/history")
@admin_required
def admin_user_history(tg_id):
    with db_lock, db() as con:
        lookups = con.execute("""
            SELECT * FROM lookups WHERE tg_id=?
            ORDER BY id DESC LIMIT 200
        """, (tg_id,)).fetchall()
        sms = con.execute("""
            SELECT * FROM sms_logs WHERE tg_id=?
            ORDER BY id DESC LIMIT 100
        """, (tg_id,)).fetchall()
    return jsonify({
        "ok": True,
        "lookups": [dict(x) for x in lookups],
        "sms": [dict(x) for x in sms]
    })


@app.route("/adm/api/user/<int:tg_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(tg_id):
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in {"block", "unblock", "premium_on", "premium_off"}:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    with db_lock, db() as con:
        if action == "block":
            con.execute("UPDATE users SET is_blocked=1 WHERE tg_id=?", (tg_id,))
        elif action == "unblock":
            con.execute("UPDATE users SET is_blocked=0 WHERE tg_id=?", (tg_id,))
        elif action == "premium_on":
            con.execute("UPDATE users SET is_premium=1 WHERE tg_id=?", (tg_id,))
        elif action == "premium_off":
            con.execute("UPDATE users SET is_premium=0 WHERE tg_id=?", (tg_id,))
        con.commit()
    return jsonify({"ok": True})


@app.route("/adm/api/settings", methods=["POST"])
@admin_required
def admin_settings():
    body = request.get_json(silent=True) or {}
    for key in ("bot_enabled", "popup_enabled"):
        if key in body:
            set_setting(key, "1" if body[key] else "0")
    if "popup_notice" in body:
        set_setting("popup_notice", str(body["popup_notice"])[:1000])
    return jsonify({"ok": True})


@app.route("/adm/api/broadcast", methods=["POST"])
@admin_required
def admin_broadcast():
    if not bot:
        return jsonify({"ok": False, "error": "Bot is not configured"}), 500

    text = str((request.get_json(silent=True) or {}).get("message", "")).strip()
    if not text or len(text) > 4000:
        return jsonify({"ok": False, "error": "Message must be 1-4000 chars"}), 400

    with db_lock, db() as con:
        users = con.execute(
            "SELECT tg_id FROM users WHERE is_blocked=0"
        ).fetchall()

    sent = failed = 0
    for row in users:
        try:
            bot.send_message(int(row["tg_id"]), text)
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    return jsonify({"ok": True, "sent": sent, "failed": failed})


def bot_guard(message):
    if setting("bot_enabled", "1") != "1" and message.from_user.id != ADMIN_UID:
        bot.reply_to(message, "⚠️ Bot is currently disabled.")
        return False

    if is_blocked(message.from_user.id):
        bot.reply_to(message, "⛔ Your account is blocked.")
        return False
    return True


if bot:

    @bot.message_handler(commands=["start"])
    def cmd_start(message):
        touch_bot_user(message.from_user)
        if not bot_guard(message):
            return
        bot.reply_to(
            message,
            f"👋 <b>Welcome to TrueInfo</b>\n\n"
            f"🔎 Use <code>/lookup 01XXXXXXXXX</code> to search.\n"
            f"⭐ Premium: <code>/premium</code>\n"
            f"📜 History: <code>/history</code>\n\n"
            f"🌐 {PUBLIC_URL or 'Open the website from your deployment URL'}"
        )

    @bot.message_handler(commands=["lookup"])
    def cmd_lookup(message):
        touch_bot_user(message.from_user)
        if not bot_guard(message):
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: <code>/lookup 01XXXXXXXXX</code>")
            return
        try:
            phone = clean_phone(parts[1])
        except ValueError as e:
            bot.reply_to(message, f"❌ {e}")
            return

        if not rate_limit(f"bot:{message.from_user.id}", MAX_LOOKUPS_PER_MIN):
            bot.reply_to(message, "⏳ Too many requests. Try again later.")
            return

        try:
            data = external_true_lookup(phone)
            save_lookup(message.from_user.id, "telegram", phone, data, True)
            pretty = json.dumps(data, ensure_ascii=False, indent=2)[:3800]
            bot.reply_to(
                message,
                f"✅ <b>Lookup completed</b>\n<pre>{pretty}</pre>"
            )
        except Exception:
            save_lookup(message.from_user.id, "telegram", phone,
                        {"error": "API failed"}, False)
            bot.reply_to(message, "❌ Lookup API failed.")

    @bot.message_handler(commands=["history"])
    def cmd_history(message):
        touch_bot_user(message.from_user)
        if not bot_guard(message):
            return
        with db_lock, db() as con:
            rows = con.execute("""
                SELECT source, query_value, created_at
                FROM lookups WHERE tg_id=?
                ORDER BY id DESC LIMIT 20
            """, (message.from_user.id,)).fetchall()
        if not rows:
            bot.reply_to(message, "📭 No lookup history yet.")
            return
        lines = ["📜 <b>Your recent history</b>"]
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {row['query_value']} — {row['source']} — {row['created_at']}"
            )
        bot.reply_to(message, "\n".join(lines))

    @bot.message_handler(commands=["premium"])
    def cmd_premium(message):
        touch_bot_user(message.from_user)
        if not bot_guard(message):
            return
        if is_premium(message.from_user.id):
            bot.reply_to(
                message,
                "⭐ <b>Premium active</b>\n"
                "Use the website for premium tools."
            )
        else:
            bot.reply_to(
                message,
                "⭐ Premium features are available after admin activation."
            )

    @bot.message_handler(commands=["admin"])
    def cmd_admin(message):
        if message.from_user.id != ADMIN_UID:
            return
        bot.reply_to(
            message,
            f"🛠 <b>Admin</b>\n"
            f"Website: {PUBLIC_URL}/adm"
        )

    @bot.message_handler(commands=["bot_on"])
    def cmd_bot_on(message):
        if message.from_user.id != ADMIN_UID:
            return
        set_setting("bot_enabled", "1")
        bot.reply_to(message, "✅ Bot enabled.")

    @bot.message_handler(commands=["bot_off"])
    def cmd_bot_off(message):
        if message.from_user.id != ADMIN_UID:
            return
        set_setting("bot_enabled", "0")
        bot.reply_to(message, "⛔ Bot disabled.")

    @bot.message_handler(commands=["broadcast"])
    def cmd_broadcast(message):
        if message.from_user.id != ADMIN_UID:
            return
        text = message.text.split(maxsplit=1)
        if len(text) < 2:
            bot.reply_to(message, "Usage: <code>/broadcast your message</code>")
            return
        with db_lock, db() as con:
            users = con.execute(
                "SELECT tg_id FROM users WHERE is_blocked=0"
            ).fetchall()
        sent = failed = 0
        for row in users:
            try:
                bot.send_message(int(row["tg_id"]), text[1])
                sent += 1
                time.sleep(0.05)
            except Exception:
                failed += 1
        bot.reply_to(message, f"📣 Sent: {sent}\n❌ Failed: {failed}")

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        touch_bot_user(message.from_user)
        bot.reply_to(
            message,
            "ℹ️ <b>TrueInfo</b>\n\n"
            "/lookup 01XXXXXXXXX\n"
            "/history\n"
            "/premium\n"
            "/help"
        )


def start_bot():
    if not bot:
        return
    try:
        bot.remove_webhook()
    except Exception:
        pass
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=20,
                long_polling_timeout=20
            )
        except Exception:
            time.sleep(5)


init_db()

if bot and os.getenv("ENABLE_BOT_POLLING", "1") == "1":
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
