import os
import logging
import math
import secrets
import threading
from flask import Flask, render_template, request, redirect, session, jsonify, Response
from functools import wraps
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from werkzeug.security import generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime
import json
import io
import pandas as pd
from urllib import request as urlrequest
from urllib import error as urlerror
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import firebase_admin
from firebase_admin import credentials, auth
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER", "").lower() == "true",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
INVESTMENT_WAREHOUSE = "Inwestycja Suwaj"
MAIN_WAREHOUSE = "Drewno"
WAREHOUSES = (
    "Drewno",
    "Farby",
    "Śruby i wkręty",
    "Pellet",
    "Golden Oak",
    "Inne",
    INVESTMENT_WAREHOUSE,
)
UNITS = {"m2", "m3", "szt", "l", "opak", "kg"}
FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", "AIzaSyDeQD7CKOFY-GHbjz_Sn9WNgjnQQquBYAU"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", "magazyn-app-8cab2.firebaseapp.com"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", "magazyn-app-8cab2"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", "magazyn-app-8cab2.firebasestorage.app"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "808429208979"),
    "appId": os.environ.get("FIREBASE_APP_ID", "1:808429208979:web:b64c24422cce1989051466"),
    "measurementId": os.environ.get("FIREBASE_MEASUREMENT_ID", "G-SDGZD53J7L"),
}
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
FIREBASE_ADMIN_READY = False
FIREBASE_ADMIN_ERROR = ""
DB_POOL = None
DB_INIT_LOCK = threading.Lock()
DB_INITIALIZED = False
cache = Cache(config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 60})
cache.init_app(app)
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
limiter.init_app(app)


class PooledConn:
    def __init__(self, raw_conn):
        self._raw = raw_conn

    def __getattr__(self, item):
        return getattr(self._raw, item)

    def close(self):
        global DB_POOL
        if DB_POOL:
            DB_POOL.putconn(self._raw)
        else:
            self._raw.close()


def init_db_pool():
    global DB_POOL
    if DB_POOL:
        return
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Brak wymaganej zmiennej środowiskowej DATABASE_URL.")
    pool_size = max(2, int(os.environ.get("DB_POOL_SIZE", "5")))
    candidates = [dsn]
    try:
        parsed = urlsplit(dsn)
        hostname = parsed.hostname or ""
        if os.environ.get("RENDER") and hostname.endswith("-postgres.render.com"):
            internal_hostname = hostname.split(".", 1)[0]
            userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
            port = f":{parsed.port}" if parsed.port else ""
            internal_netloc = f"{userinfo}@{internal_hostname}{port}" if userinfo else f"{internal_hostname}{port}"
            internal_query = urlencode(
                [(key, value) for key, value in parse_qsl(parsed.query) if key.lower() != "sslmode"]
            )
            internal_dsn = urlunsplit(
                (parsed.scheme, internal_netloc, parsed.path, internal_query, parsed.fragment)
            )
            candidates.insert(0, internal_dsn)
    except ValueError:
        logger.warning("DATABASE_URL is not a standard URL; using it without normalization.")

    last_error = None
    for candidate in candidates:
        try:
            DB_POOL = ThreadedConnectionPool(
                minconn=1,
                maxconn=pool_size,
                dsn=candidate,
                connect_timeout=10,
                application_name="magazyn-app",
            )
            break
        except psycopg2.OperationalError as exc:
            last_error = exc
            logger.warning("Database connection candidate failed; trying fallback if available.")
    if DB_POOL is None:
        raise last_error or RuntimeError("Nie udało się utworzyć puli połączeń z bazą.")
    logger.info("DB pool initialized.")


def init_firebase_admin():
    global FIREBASE_ADMIN_READY, FIREBASE_ADMIN_ERROR
    if firebase_admin._apps:
        FIREBASE_ADMIN_READY = True
        return
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        FIREBASE_ADMIN_READY = False
        FIREBASE_ADMIN_ERROR = "Brak FIREBASE_SERVICE_ACCOUNT_JSON w zmiennych środowiskowych."
        logger.error("Firebase Admin init failed: FIREBASE_SERVICE_ACCOUNT_JSON is missing.")
        return
    try:
        # Read strictly from env and parse JSON payload (Render-compatible).
        normalized_raw = raw.strip()
        if (normalized_raw.startswith("'") and normalized_raw.endswith("'")) or (
            normalized_raw.startswith('"') and normalized_raw.endswith('"')
        ):
            normalized_raw = normalized_raw[1:-1]

        try:
            service_account = json.loads(normalized_raw)
        except json.JSONDecodeError:
            # Some dashboards escape JSON one level too deep; unescape and try again.
            service_account = json.loads(bytes(normalized_raw, "utf-8").decode("unicode_escape"))

        # If private_key is escaped (\\n), normalize to real newlines.
        private_key = service_account.get("private_key")
        if isinstance(private_key, str):
            service_account["private_key"] = private_key.replace("\\n", "\n")

        cred = credentials.Certificate(service_account)
        firebase_admin.initialize_app(cred)
        FIREBASE_ADMIN_READY = True
        FIREBASE_ADMIN_ERROR = ""
        logger.info("Firebase Admin initialized successfully.")
    except Exception:
        FIREBASE_ADMIN_READY = False
        FIREBASE_ADMIN_ERROR = "Nieprawidłowy FIREBASE_SERVICE_ACCOUNT_JSON."
        logger.exception("Firebase Admin init failed: invalid FIREBASE_SERVICE_ACCOUNT_JSON.")


def get_missing_firebase_web_envs():
    key_map = {
        "FIREBASE_API_KEY": "apiKey",
        "FIREBASE_AUTH_DOMAIN": "authDomain",
        "FIREBASE_PROJECT_ID": "projectId",
        "FIREBASE_STORAGE_BUCKET": "storageBucket",
        "FIREBASE_MESSAGING_SENDER_ID": "messagingSenderId",
        "FIREBASE_APP_ID": "appId",
        "FIREBASE_MEASUREMENT_ID": "measurementId",
    }
    return [env_key for env_key, cfg_key in key_map.items() if not FIREBASE_CONFIG.get(cfg_key)]


def verify_id_token_with_firebase_rest(id_token):
    api_key = FIREBASE_CONFIG.get("apiKey")
    if not api_key:
        raise ValueError("Brak FIREBASE_API_KEY do fallback verify.")
    endpoint = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}"
    payload = json.dumps({"idToken": id_token}).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError:
        raise ValueError("Nieprawidłowy token")
    users = data.get("users") or []
    if not users:
        raise ValueError("Brak użytkownika w tokenie")
    user = users[0]
    return {
        "email": (user.get("email") or "").lower(),
        "uid": user.get("localId") or ""
    }


init_firebase_admin()


# 🔥 DB
def db():
    init_db_pool()
    raw = DB_POOL.getconn()
    raw.autocommit = False
    with raw.cursor() as cur:
        cur.execute("SET statement_timeout = 15000")
    return PooledConn(raw)


# 🔥 INIT DB
def run_db_migrations():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(67431029)")

    # USERS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    );
    """)

    # PRODUCTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id SERIAL PRIMARY KEY,
        name TEXT,
        qty REAL,
        unit TEXT,
        warehouse TEXT,
        price_netto REAL DEFAULT 0,
        vat REAL DEFAULT 0
    );
    """)

    # PACKAGES
    cur.execute("""
    CREATE TABLE IF NOT EXISTS packages(
        id SERIAL PRIMARY KEY,
        product_id INTEGER,
        number TEXT,
        qty REAL,
        warehouse TEXT
    );
    """)

    # DOCS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_docs(
        id SERIAL PRIMARY KEY,
        date TEXT,
        kontrahent TEXT,
        warehouse TEXT,
        image TEXT,
        doc_number TEXT
    );
    """)

    # COSTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS costs(
        id SERIAL PRIMARY KEY,
        name TEXT,
        amount REAL,
        date TEXT,
        warehouse_source BOOLEAN DEFAULT FALSE,
        description TEXT
    );
    """)

    # ITEMS (bez kombinowania w środku)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_items(
        id SERIAL PRIMARY KEY,
        doc_id INTEGER,
        product_id INTEGER,
        qty REAL,
        warehouse TEXT,
        package_id INTEGER,
        price_netto REAL DEFAULT 0,
        price_brutto REAL DEFAULT 0
    );
    """)

    # 🔥 KLUCZOWE — aktualizacja starej bazy
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS package_id INTEGER;
    """)
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS warehouse TEXT;
    """)
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS price_netto REAL DEFAULT 0;
    """)
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS price_brutto REAL DEFAULT 0;
    """)
    cur.execute("""
    ALTER TABLE packages
    ADD COLUMN IF NOT EXISTS warehouse TEXT;
    """)
    cur.execute("""
    ALTER TABLE packages
    ADD COLUMN IF NOT EXISTS number TEXT;
    """)

    # migracja kompatybilności: stare bazy mogły mieć kolumnę package_number zamiast number
    cur.execute("""
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'packages' AND column_name = 'package_number'
    )
    """)
    has_package_number = cur.fetchone()[0]
    if has_package_number:
        cur.execute("""
        UPDATE packages
        SET number = COALESCE(number, package_number::TEXT)
        WHERE number IS NULL
        """)

    # aktualizacja ról adminów z env (bez kasowania użytkowników)
    for admin_email in ADMIN_EMAILS:
        cur.execute("""
            INSERT INTO users(username, password, role)
            VALUES (%s,%s,%s)
            ON CONFLICT (username) DO UPDATE SET role='admin'
        """, (admin_email, generate_password_hash("firebase-managed"), "admin"))

    cur.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS initial_qty REAL")
    cur.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")
    cur.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE issue_docs ADD COLUMN IF NOT EXISTS movement_type TEXT")
    cur.execute("ALTER TABLE issue_docs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cur.execute("ALTER TABLE issue_docs ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE issue_docs ADD COLUMN IF NOT EXISTS voided_by TEXT")
    cur.execute("ALTER TABLE issue_items ADD COLUMN IF NOT EXISTS package_number TEXT")
    cur.execute("ALTER TABLE costs ADD COLUMN IF NOT EXISTS source_warehouse TEXT")
    cur.execute("UPDATE products SET qty=0 WHERE qty IS NULL OR qty < 0 OR qty::text='NaN'")
    cur.execute("UPDATE packages SET qty=0 WHERE qty IS NULL OR qty < 0 OR qty::text='NaN'")
    cur.execute("UPDATE packages SET initial_qty=qty WHERE initial_qty IS NULL")
    cur.execute("""
        UPDATE packages
        SET status=CASE WHEN qty <= 0 THEN 'issued' ELSE 'active' END,
            archived_at=CASE WHEN qty <= 0 THEN COALESCE(archived_at, NOW()) ELSE archived_at END
        WHERE status IS NULL OR status NOT IN ('active', 'issued', 'cancelled')
           OR (qty <= 0 AND status='active')
    """)
    cur.execute("""
        UPDATE issue_docs
        SET movement_type=CASE
            WHEN COALESCE(doc_number, '') LIKE 'PZ%' THEN 'PZ'
            WHEN COALESCE(doc_number, '') LIKE 'WZ%' THEN 'WZ'
            ELSE movement_type
        END
        WHERE movement_type IS NULL
    """)
    cur.execute("""
        UPDATE issue_items i SET package_number=p.number
        FROM packages p
        WHERE i.package_id=p.id AND i.package_number IS NULL
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_warehouse_name ON products(warehouse, lower(name))")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_packages_product_active ON packages(product_id, warehouse, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_packages_number ON packages(warehouse, lower(number))")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_items_doc ON issue_items(doc_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_docs_date ON issue_docs(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_docs_number ON issue_docs(lower(doc_number))")
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='products_qty_nonnegative') THEN
                ALTER TABLE products
                ADD CONSTRAINT products_qty_nonnegative CHECK (qty >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='packages_qty_nonnegative') THEN
                ALTER TABLE packages
                ADD CONSTRAINT packages_qty_nonnegative CHECK (qty >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='issue_items_qty_positive') THEN
                ALTER TABLE issue_items
                ADD CONSTRAINT issue_items_qty_positive CHECK (qty > 0) NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='products_qty_finite_nonnegative') THEN
                ALTER TABLE products
                ADD CONSTRAINT products_qty_finite_nonnegative
                CHECK (qty >= 0 AND qty::text <> 'NaN') NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='packages_qty_finite_nonnegative') THEN
                ALTER TABLE packages
                ADD CONSTRAINT packages_qty_finite_nonnegative
                CHECK (qty >= 0 AND qty::text <> 'NaN') NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='packages_status_valid') THEN
                ALTER TABLE packages
                ADD CONSTRAINT packages_status_valid
                CHECK (status IN ('active', 'issued', 'cancelled')) NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='packages_product_fk') THEN
                ALTER TABLE packages
                ADD CONSTRAINT packages_product_fk
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='issue_items_doc_fk') THEN
                ALTER TABLE issue_items
                ADD CONSTRAINT issue_items_doc_fk
                FOREIGN KEY (doc_id) REFERENCES issue_docs(id) ON DELETE RESTRICT NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='issue_items_product_fk') THEN
                ALTER TABLE issue_items
                ADD CONSTRAINT issue_items_product_fk
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='issue_items_package_fk') THEN
                ALTER TABLE issue_items
                ADD CONSTRAINT issue_items_package_fk
                FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE SET NULL NOT VALID;
            END IF;
        END $$;
    """)

    conn.commit()
    conn.close()


def ensure_db_initialized():
    global DB_INITIALIZED, DB_POOL
    if DB_INITIALIZED:
        return
    with DB_INIT_LOCK:
        if DB_INITIALIZED:
            return
        try:
            run_db_migrations()
            DB_INITIALIZED = True
        except Exception:
            logger.exception("Database initialization failed.")
            if DB_POOL:
                DB_POOL.closeall()
                DB_POOL = None
            raise


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def parse_positive_number(value, field_name="Ilość"):
    try:
        number = float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name}: podaj prawidłową liczbę.")
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} musi być większa od zera.")
    return number


def parse_nonnegative_number(value, field_name):
    if value in (None, ""):
        return 0.0
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name}: podaj prawidłową liczbę.")
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} nie może być ujemna.")
    return number


def close_with_rollback(conn):
    conn.rollback()

# 🔒 LOGIN REQUIRED
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        if session.get('role') != 'admin':
            return "Brak uprawnień", 403
        return f(*args, **kwargs)
    return decorated


# 🔐 LOGIN
@app.route('/login')
def login():
    if 'user' in session:
        return redirect('/')
    return render_template(
        "login.html",
        firebase_config=FIREBASE_CONFIG,
        firebase_admin_ready=FIREBASE_ADMIN_READY,
        firebase_admin_error=FIREBASE_ADMIN_ERROR,
        missing_firebase_web_envs=get_missing_firebase_web_envs(),
        firebase_rest_fallback=not FIREBASE_ADMIN_READY and bool(FIREBASE_CONFIG.get("apiKey"))
    )


@app.route('/register')
def register():
    return redirect('/login')


@app.route('/auth/session', methods=['POST'])
@limiter.limit("20 per minute")
def create_session():
    payload = request.get_json(silent=True) or {}
    id_token = payload.get("idToken")
    if not id_token:
        return jsonify({"ok": False, "error": "Brak tokenu"}), 400

    if FIREBASE_ADMIN_READY:
        try:
            decoded = auth.verify_id_token(id_token)
            email = (decoded.get("email") or "").lower()
            uid = decoded.get("uid")
        except Exception:
            return jsonify({"ok": False, "error": "Nieprawidłowy token"}), 401
    else:
        try:
            decoded = verify_id_token_with_firebase_rest(id_token)
            email = decoded.get("email")
            uid = decoded.get("uid")
        except Exception:
            return jsonify({"ok": False, "error": FIREBASE_ADMIN_ERROR or "Nie udało się zweryfikować tokenu."}), 503
    if not email or not uid:
        return jsonify({"ok": False, "error": "Brak danych użytkownika"}), 400

    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        return jsonify({"ok": False, "error": "E-mail poza listą dozwolonych użytkowników"}), 403

    ensure_db_initialized()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE username=%s", (email,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        can_autoprovision = (
            users_count == 0
            or email in ADMIN_EMAILS
            or email in ALLOWED_EMAILS
        )
        if not can_autoprovision:
            conn.close()
            return jsonify({"ok": False, "error": "Brak konta. Skontaktuj się z administratorem."}), 403

        role = "admin" if (users_count == 0 or email in ADMIN_EMAILS) else "employee"
        cur.execute(
            "INSERT INTO users(username, password, role) VALUES (%s,%s,%s)",
            (email, generate_password_hash(uid), role)
        )
    else:
        role = row[0] or ("admin" if email in ADMIN_EMAILS else "employee")
    cur.execute("UPDATE users SET password=%s WHERE username=%s", (generate_password_hash(uid), email))
    conn.commit()
    conn.close()

    session['user'] = email
    session['role'] = role
    session['uid'] = uid
    return jsonify({"ok": True, "role": role})


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect('/login')


@app.before_request
def require_login_for_private_app():
    allowed_routes = {"login", "create_session", "logout", "static", "favicon", "health"}
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        sent_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not sent_token or not secrets.compare_digest(sent_token, session.get("_csrf_token", "")):
            if request.is_json:
                return jsonify({"ok": False, "error": "Sesja formularza wygasła. Odśwież stronę."}), 400
            return "Sesja formularza wygasła. Odśwież stronę.", 400
    if request.endpoint in allowed_routes:
        return None
    if 'user' not in session:
        return redirect('/login')
    ensure_db_initialized()
    return None


@app.route('/favicon.ico')
def favicon():
    return Response(status=204)


@app.route('/health')
def health():
    try:
        ensure_db_initialized()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception:
        logger.exception("Health check failed.")
        return jsonify({"status": "error", "database": "unavailable"}), 503


@app.after_request
def add_private_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route('/')
@login_required
def home():
    return render_template("home.html")


@app.route('/dashboard', endpoint='dashboard_page_view')
@login_required
def dashboard_page():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM products")
    total_products, total_qty = cur.fetchone()
    cur.execute("SELECT name, qty FROM products ORDER BY qty DESC LIMIT 10")
    top_products = cur.fetchall()
    conn.close()
    return render_template(
        "dashboard.html",
        total_products=total_products,
        total_qty=round(total_qty or 0, 3),
        names=json.dumps([row[0] for row in top_products]),
        qtys=json.dumps([row[1] for row in top_products]),
    )


@app.route('/magazyny')
@login_required
@cache.cached(timeout=30)
def magazyny():
    return render_template("magazyny.html", warehouses=WAREHOUSES)


@app.route('/users')
@admin_required
def users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY id")
    users_list = cur.fetchall()
    conn.close()
    return render_template("users.html", users=users_list)


@app.route('/add_user', methods=['POST'])
@admin_required
def add_user():
    conn = db()
    cur = conn.cursor()
    username = (request.form.get('username') or '').strip().lower()
    if not username or "@" not in username or len(username) > 254:
        conn.close()
        return "Podaj prawidłowy adres e-mail.", 400
    role = request.form.get('role', 'employee')
    if role not in ('admin', 'employee'):
        role = 'employee'
    cur.execute(
        "INSERT INTO users(username, password, role) VALUES (%s,%s,%s) ON CONFLICT (username) DO NOTHING",
        (
            username,
            generate_password_hash("firebase-managed"),
            role
        )
    )
    conn.commit()
    conn.close()
    return redirect('/users')


@app.route('/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users WHERE id=%s FOR UPDATE", (user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return redirect('/users')
    if user[0].lower() == (session.get("user") or "").lower():
        conn.close()
        return "Nie możesz usunąć aktualnie zalogowanego konta.", 400
    if user[1] == "admin":
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
        if cur.fetchone()[0] <= 1:
            conn.close()
            return "Nie można usunąć ostatniego administratora.", 400
    cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    conn.commit()
    conn.close()
    return redirect('/users')


@app.route('/update_user_role/<int:user_id>', methods=['POST'])
@admin_required
def update_user_role(user_id):
    new_role = request.form.get('role', 'employee')
    if new_role not in ('admin', 'employee'):
        return redirect('/users')

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return redirect('/users')

    old_role = row[0] or 'employee'
    if old_role == 'admin' and new_role != 'admin':
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
        admins_count = cur.fetchone()[0]
        if admins_count <= 1:
            conn.close()
            return redirect('/users')

    cur.execute("UPDATE users SET role=%s WHERE id=%s", (new_role, user_id))
    conn.commit()
    conn.close()
    return redirect('/users')


@app.route('/magazyn/<name>')
@login_required
def magazyn(name):
    if name != "Wszystko" and name not in WAREHOUSES:
        return "Nie znaleziono magazynu.", 404
    conn = db()
    cur = conn.cursor()

    if name == "Wszystko":
        cur.execute("SELECT * FROM products ORDER BY warehouse, lower(name), id")
    else:
        cur.execute("SELECT * FROM products WHERE warehouse=%s ORDER BY lower(name), id", (name,))

    products = cur.fetchall()
    conn.close()

    return render_template("index.html", products=products, warehouse=name)


@app.route('/packages/<int:product_id>')
@login_required
def packages_for_product(product_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT number, qty, status
        FROM packages
        WHERE product_id=%s
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC
        """,
        (product_id,),
    )
    packages = cur.fetchall()
    conn.close()
    return render_template("packages.html", packages=packages)


@app.route('/api/packages/lookup')
@login_required
def package_lookup():
    number = (request.args.get("number") or "").strip()
    warehouse = (request.args.get("warehouse") or "").strip()
    if not number:
        return jsonify({"ok": False, "error": "Podaj numer paczki."}), 400
    conn = db()
    cur = conn.cursor()
    query = """
        SELECT pk.id, pk.number, pk.qty, pk.warehouse, pk.status,
               p.id, p.name, p.unit, p.price_netto, p.vat
        FROM packages pk
        JOIN products p ON p.id=pk.product_id
        WHERE lower(pk.number)=lower(%s) AND pk.status='active' AND pk.qty>0
    """
    params = [number]
    if warehouse:
        query += " AND pk.warehouse=%s"
        params.append(warehouse)
    query += " ORDER BY CASE WHEN pk.status='active' THEN 0 ELSE 1 END, pk.id DESC LIMIT 2"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return jsonify({"ok": False, "error": "Nie znaleziono paczki."}), 404
    if len(rows) > 1 and not warehouse:
        return jsonify({"ok": False, "error": "Numer występuje w kilku magazynach. Wybierz magazyn."}), 409
    row = rows[0]
    return jsonify({
        "ok": True,
        "package": {
            "id": row[0], "number": row[1], "qty": row[2], "warehouse": row[3],
            "status": row[4], "product_id": row[5], "product": row[6],
            "unit": row[7], "price_netto": row[8], "vat": row[9],
        },
    })


@app.route('/add_product', methods=['POST'])
@login_required
def add_product():
    name = (request.form.get("name") or "").strip()
    unit = (request.form.get("unit") or "").strip()
    warehouse = (request.form.get("warehouse") or "").strip()
    if not name or len(name) > 200:
        return "Nazwa produktu jest wymagana i może mieć maksymalnie 200 znaków.", 400
    if unit not in UNITS:
        return "Wybierz prawidłową jednostkę.", 400
    if warehouse not in WAREHOUSES:
        return "Wybierz prawidłowy magazyn.", 400
    try:
        price_netto = parse_nonnegative_number(request.form.get("price_netto"), "Cena netto")
        vat = parse_nonnegative_number(request.form.get("vat"), "VAT")
    except ValueError as exc:
        return str(exc), 400
    if vat > 100:
        return "VAT nie może przekraczać 100%.", 400

    conn = db()
    cur = conn.cursor()
    try:
        lock_key = f"product:{warehouse}:{name.casefold()}"
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
        cur.execute(
            "SELECT id FROM products WHERE warehouse=%s AND lower(name)=lower(%s) LIMIT 1",
            (warehouse, name),
        )
        if cur.fetchone():
            raise ValueError("Taki produkt już istnieje w tym magazynie.")
        cur.execute(
            """
            INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
            VALUES (%s,0,%s,%s,%s,%s)
            """,
            (name, unit, warehouse, price_netto, vat),
        )
        conn.commit()
        cache.clear()
        return redirect(f"/magazyn/{warehouse}")
    except ValueError as exc:
        conn.rollback()
        return str(exc), 409
    except Exception:
        conn.rollback()
        logger.exception("Product creation failed.")
        return "Nie udało się dodać produktu.", 500
    finally:
        conn.close()


@app.route('/delete_product/<int:product_id>', methods=['POST'])
@admin_required
def delete_product(product_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM issue_items WHERE product_id=%s", (product_id,))
    linked = cur.fetchone()[0]
    if linked > 0:
        conn.close()
        return "Nie można usunąć produktu użytego w dokumentach.", 400

    cur.execute("DELETE FROM packages WHERE product_id=%s", (product_id,))
    cur.execute("DELETE FROM products WHERE id=%s", (product_id,))
    conn.commit()
    conn.close()
    return ("", 204)


@app.route('/delete_selected', methods=['POST'])
@admin_required
def delete_selected():
    selected = request.form.getlist("selected")
    if not selected:
        return redirect(request.referrer or '/magazyny')

    conn = db()
    cur = conn.cursor()
    for pid_raw in selected:
        try:
            pid = int(pid_raw)
        except ValueError:
            continue

        cur.execute("SELECT COUNT(*) FROM issue_items WHERE product_id=%s", (pid,))
        linked = cur.fetchone()[0]
        if linked > 0:
            continue

        cur.execute("DELETE FROM packages WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM products WHERE id=%s", (pid,))

    conn.commit()
    conn.close()
    return redirect(request.referrer or '/magazyny')


@app.route('/costs')
@login_required
def costs():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, amount, date, warehouse_source, description, source_warehouse "
        "FROM costs ORDER BY date DESC, id DESC"
    )
    costs_rows = cur.fetchall()
    conn.close()
    return render_template("costs.html", costs=costs_rows, warehouses=WAREHOUSES)


def process_cost():
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    source_warehouse = (request.form.get("source_warehouse") or "").strip()
    warehouse_source = request.form.get("warehouse_source") == "on"
    try:
        amount = parse_positive_number(request.form.get("amount"), "Kwota / ilość")
        date = normalized_document_date(request.form.get("date"))
    except ValueError as exc:
        return str(exc), 400
    if not name:
        return "Nazwa jest wymagana.", 400
    if warehouse_source and source_warehouse not in WAREHOUSES:
        return "Wybierz prawidłowy magazyn źródłowy.", 400

    conn = db()
    cur = conn.cursor()
    try:
        if warehouse_source:
            cur.execute(
                """
                SELECT id, qty FROM products
                WHERE lower(name)=lower(%s) AND warehouse=%s
                ORDER BY id LIMIT 1 FOR UPDATE
                """,
                (name, source_warehouse),
            )
            product = cur.fetchone()
            if not product:
                raise ValueError(f"Brak produktu '{name}' w magazynie {source_warehouse}.")
            cur.execute(
                "SELECT 1 FROM packages WHERE product_id=%s AND status='active' AND qty>0 LIMIT 1",
                (product[0],),
            )
            if cur.fetchone():
                raise ValueError("Ten produkt jest ewidencjonowany w paczkach. Użyj formularza wydania.")
            cur.execute(
                "UPDATE products SET qty=qty-%s WHERE id=%s AND qty >= %s",
                (amount, product[0], amount),
            )
            if cur.rowcount != 1:
                raise ValueError(f"Brak wystarczającej ilości w magazynie {source_warehouse}.")
        cur.execute(
            """
            INSERT INTO costs(name, amount, date, warehouse_source, description, source_warehouse)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (name, amount, date, warehouse_source, description, source_warehouse or None),
        )
        conn.commit()
        cache.clear()
        return redirect("/costs")
    except ValueError as exc:
        conn.rollback()
        return str(exc), 400
    except Exception:
        conn.rollback()
        logger.exception("Cost creation failed.")
        return "Nie udało się zapisać kosztu.", 500
    finally:
        conn.close()


@app.route('/add_cost', methods=['POST'])
@login_required
def add_cost():
    return process_cost()

    name = (request.form.get('name') or '').strip()
    description = (request.form.get('description') or '').strip()
    date = request.form.get('date') or datetime.now().strftime("%Y-%m-%d")
    warehouse_source = request.form.get('warehouse_source') == 'on'
    source_warehouse = (request.form.get('source_warehouse') or '').strip()
    try:
        amount = float((request.form.get('amount') or '0').replace(",", "."))
    except Exception:
        return "Nieprawidłowa kwota/ilość.", 400

    if not name or amount <= 0:
        return "Nazwa i kwota/ilość są wymagane.", 400

    conn = db()
    cur = conn.cursor()
    try:
        if warehouse_source:
            if source_warehouse not in WAREHOUSES:
                return "Wybierz prawidłowy magazyn źródłowy.", 400
            cur.execute(
                "SELECT id, qty FROM products WHERE name=%s AND warehouse=%s ORDER BY id LIMIT 1 FOR UPDATE",
                (name, source_warehouse)
            )
            product = cur.fetchone()
            if not product:
                conn.close()
                return f"Brak produktu '{name}' w magazynie głównym ({MAIN_WAREHOUSE}).", 400
            if product[1] < amount:
                conn.close()
                return "Brak wystarczającej ilości w magazynie głównym.", 400

            cur.execute(
                "SELECT 1 FROM packages WHERE product_id=%s AND status='active' AND qty>0 LIMIT 1",
                (product[0],)
            )
            if cur.fetchone():
                return "Ten produkt jest ewidencjonowany w paczkach. Użyj formularza wydania.", 400
            cur.execute(
                "UPDATE products SET qty=qty-%s WHERE id=%s AND qty >= %s",
                (amount, product[0], amount)
            )

        cur.execute(
            """
            INSERT INTO costs(name, amount, date, warehouse_source, description, source_warehouse)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (name, amount, date, warehouse_source, description, source_warehouse or None)
        )
        conn.commit()
        cache.clear()
    except Exception:
        conn.rollback()
        return "Błąd podczas zapisu kosztu.", 400
    finally:
        conn.close()

    return redirect('/costs')


def form_value(values, index, default=""):
    return values[index] if index < len(values) else default


def normalized_document_date(value):
    value = (value or "").strip() or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Nieprawidłowa data dokumentu.")
    return value


def collect_document_items(forced_warehouse=None, issuing=False):
    product_ids = request.form.getlist("product_id")
    quantities = request.form.getlist("qty")
    warehouses = request.form.getlist("warehouse")
    package_values = request.form.getlist("package_id" if issuing else "package_number")
    netto_values = request.form.getlist("price_netto")
    brutto_values = request.form.getlist("price_brutto")
    items = []
    for index, raw_product_id in enumerate(product_ids):
        raw_product_id = (raw_product_id or "").strip()
        if not raw_product_id:
            continue
        try:
            product_id = int(raw_product_id)
        except ValueError:
            raise ValueError("Wybrano nieprawidłowy produkt.")
        warehouse = forced_warehouse or form_value(warehouses, index).strip()
        if warehouse not in WAREHOUSES:
            raise ValueError("Wybierz prawidłowy magazyn dla każdej pozycji.")
        qty = parse_positive_number(form_value(quantities, index))
        price_netto = parse_nonnegative_number(form_value(netto_values, index), "Cena netto")
        price_brutto = parse_nonnegative_number(form_value(brutto_values, index), "Cena brutto")
        package_value = form_value(package_values, index).strip()
        if not issuing and len(package_value) > 100:
            raise ValueError("Numer paczki może mieć maksymalnie 100 znaków.")
        items.append({
            "product_id": product_id,
            "warehouse": warehouse,
            "qty": qty,
            "package": package_value,
            "price_netto": price_netto,
            "price_brutto": price_brutto,
        })
    if not items:
        raise ValueError("Dodaj co najmniej jedną kompletną pozycję dokumentu.")
    return items


def create_receipt(forced_warehouse=None):
    try:
        items = collect_document_items(forced_warehouse=forced_warehouse, issuing=False)
        items.sort(key=lambda item: (item["warehouse"], item["product_id"], item["package"].casefold()))
        date = normalized_document_date(request.form.get("date"))
    except ValueError as exc:
        return str(exc), 400
    contractor = (request.form.get("kontrahent") or "").strip()
    if not contractor:
        return "Dostawca jest wymagany.", 400
    if len(contractor) > 200:
        return "Nazwa dostawcy może mieć maksymalnie 200 znaków.", 400

    conn = db()
    cur = conn.cursor()
    try:
        resolved = []
        for item in items:
            cur.execute(
                "SELECT name, unit, price_netto, vat FROM products WHERE id=%s",
                (item["product_id"],),
            )
            source = cur.fetchone()
            if not source:
                raise ValueError("Wybrany produkt już nie istnieje.")
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"product:{item['warehouse']}:{source[0].casefold()}",),
            )
            cur.execute(
                """
                SELECT id FROM products
                WHERE warehouse=%s AND lower(name)=lower(%s)
                ORDER BY id LIMIT 1 FOR UPDATE
                """,
                (item["warehouse"], source[0]),
            )
            target = cur.fetchone()
            target_id = target[0] if target else None
            if item["package"]:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"package:{item['warehouse']}:{item['package'].casefold()}",),
                )
                cur.execute(
                    """
                    SELECT 1 FROM packages
                    WHERE warehouse=%s AND lower(number)=lower(%s)
                    LIMIT 1 FOR UPDATE
                    """,
                    (item["warehouse"], item["package"]),
                )
                if cur.fetchone():
                    raise ValueError(
                        f"Paczka {item['package']} już istnieje w magazynie {item['warehouse']}."
                    )
            resolved.append((item, source, target_id))

        cur.execute(
            """
            INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number, movement_type)
            VALUES (%s,%s,%s,%s,%s,'PZ') RETURNING id
            """,
            (date, contractor, forced_warehouse or "", "", ""),
        )
        doc_id = cur.fetchone()[0]
        requested_number = (request.form.get("doc_number") or "").strip()
        if len(requested_number) > 100:
            raise ValueError("Numer dokumentu może mieć maksymalnie 100 znaków.")
        if requested_number:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"document:{requested_number.casefold()}",),
            )
            cur.execute(
                "SELECT 1 FROM issue_docs WHERE id<>%s AND lower(doc_number)=lower(%s) LIMIT 1",
                (doc_id, requested_number),
            )
            if cur.fetchone():
                raise ValueError("Dokument o takim numerze już istnieje.")
        prefix = "PZ-IS" if forced_warehouse == INVESTMENT_WAREHOUSE else "PZ"
        doc_number = requested_number or f"{prefix}/{doc_id}/{datetime.now().year}"
        cur.execute("UPDATE issue_docs SET doc_number=%s WHERE id=%s", (doc_number, doc_id))

        for item, source, target_id in resolved:
            if target_id:
                cur.execute(
                    "UPDATE products SET qty=qty+%s, price_netto=%s WHERE id=%s",
                    (item["qty"], item["price_netto"], target_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (source[0], item["qty"], source[1], item["warehouse"],
                     item["price_netto"] or source[2] or 0, source[3] or 0),
                )
                target_id = cur.fetchone()[0]

            package_id = None
            if item["package"]:
                cur.execute(
                    """
                    INSERT INTO packages(product_id, number, qty, warehouse, initial_qty, status)
                    VALUES (%s,%s,%s,%s,%s,'active') RETURNING id
                    """,
                    (target_id, item["package"], item["qty"], item["warehouse"], item["qty"]),
                )
                package_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO issue_items(
                    doc_id, product_id, qty, warehouse, package_id, package_number,
                    price_netto, price_brutto
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (doc_id, target_id, item["qty"], item["warehouse"], package_id,
                 item["package"] or None, item["price_netto"], item["price_brutto"]),
            )
        conn.commit()
        cache.clear()
        return redirect(f"/doc/{doc_id}")
    except ValueError as exc:
        close_with_rollback(conn)
        return str(exc), 400
    except Exception:
        close_with_rollback(conn)
        logger.exception("Receipt creation failed.")
        return "Nie udało się zapisać przyjęcia. Żadne stany nie zostały zmienione.", 500
    finally:
        conn.close()


def create_issue(forced_warehouse=None):
    try:
        items = collect_document_items(forced_warehouse=forced_warehouse, issuing=True)
        items.sort(
            key=lambda item: (
                item["warehouse"],
                item["product_id"],
                int(item["package"]) if item["package"].isdigit() else 0,
            )
        )
        date = normalized_document_date(request.form.get("date"))
    except ValueError as exc:
        return str(exc), 400
    contractor = (request.form.get("kontrahent") or "").strip()
    if not contractor:
        return "Kontrahent jest wymagany.", 400
    if len(contractor) > 200:
        return "Nazwa kontrahenta może mieć maksymalnie 200 znaków.", 400
    movement_type = (request.form.get("movement_type") or "WZ").strip().upper()
    if movement_type not in {"WZ", "RW"}:
        return "Nieprawidłowy typ dokumentu wydania.", 400

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number, movement_type)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (date, contractor, forced_warehouse or "", "", "", movement_type),
        )
        doc_id = cur.fetchone()[0]
        requested_number = (request.form.get("doc_number") or "").strip()
        if len(requested_number) > 100:
            raise ValueError("Numer dokumentu może mieć maksymalnie 100 znaków.")
        if requested_number:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"document:{requested_number.casefold()}",),
            )
            cur.execute(
                "SELECT 1 FROM issue_docs WHERE id<>%s AND lower(doc_number)=lower(%s) LIMIT 1",
                (doc_id, requested_number),
            )
            if cur.fetchone():
                raise ValueError("Dokument o takim numerze już istnieje.")
        prefix = f"{movement_type}-IS" if forced_warehouse == INVESTMENT_WAREHOUSE else movement_type
        doc_number = requested_number or f"{prefix}/{doc_id}/{datetime.now().year}"
        cur.execute("UPDATE issue_docs SET doc_number=%s WHERE id=%s", (doc_number, doc_id))

        for item in items:
            cur.execute(
                """
                SELECT id FROM products
                WHERE id=%s AND warehouse=%s
                FOR UPDATE
                """,
                (item["product_id"], item["warehouse"]),
            )
            if not cur.fetchone():
                raise ValueError("Produkt nie istnieje w wybranym magazynie.")

            package_id = None
            package_number = None
            if item["package"]:
                try:
                    package_id = int(item["package"])
                except ValueError:
                    raise ValueError("Wybrano nieprawidłową paczkę.")
                cur.execute(
                    """
                    SELECT number, qty FROM packages
                    WHERE id=%s AND product_id=%s AND warehouse=%s AND status='active'
                    FOR UPDATE
                    """,
                    (package_id, item["product_id"], item["warehouse"]),
                )
                package = cur.fetchone()
                if not package:
                    raise ValueError("Paczka nie należy do wybranego produktu lub magazynu.")
                package_number = package[0]
                if package[1] + 1e-9 < item["qty"]:
                    raise ValueError(
                        f"W paczce {package_number} jest tylko {package[1]}."
                    )
                cur.execute(
                    """
                    UPDATE packages
                    SET qty=qty-%s,
                        status=CASE WHEN qty-%s <= 0 THEN 'issued' ELSE 'active' END,
                        archived_at=CASE WHEN qty-%s <= 0 THEN NOW() ELSE NULL END
                    WHERE id=%s AND qty >= %s
                    """,
                    (item["qty"], item["qty"], item["qty"], package_id, item["qty"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("Stan paczki zmienił się. Odśwież stronę i spróbuj ponownie.")
            else:
                cur.execute(
                    """
                    SELECT 1 FROM packages
                    WHERE product_id=%s AND warehouse=%s AND status='active' AND qty>0
                    LIMIT 1
                    """,
                    (item["product_id"], item["warehouse"]),
                )
                if cur.fetchone():
                    raise ValueError("Ten produkt jest ewidencjonowany w paczkach. Wybierz numer paczki.")

            cur.execute(
                """
                UPDATE products SET qty=qty-%s
                WHERE id=%s AND warehouse=%s AND qty >= %s
                """,
                (item["qty"], item["product_id"], item["warehouse"], item["qty"]),
            )
            if cur.rowcount != 1:
                raise ValueError(f"Brak wystarczającego stanu w magazynie {item['warehouse']}.")
            cur.execute(
                """
                INSERT INTO issue_items(
                    doc_id, product_id, qty, warehouse, package_id, package_number,
                    price_netto, price_brutto
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (doc_id, item["product_id"], item["qty"], item["warehouse"], package_id,
                 package_number, item["price_netto"], item["price_brutto"]),
            )
        conn.commit()
        cache.clear()
        return redirect(f"/doc/{doc_id}")
    except ValueError as exc:
        close_with_rollback(conn)
        return str(exc), 400
    except Exception:
        close_with_rollback(conn)
        logger.exception("Issue creation failed.")
        return "Nie udało się zapisać wydania. Żadne stany nie zostały zmienione.", 500
    finally:
        conn.close()


# 📥 PRZYJĘCIE
@app.route('/przyjecie')
@login_required
def przyjecie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products ORDER BY warehouse, lower(name), id")
    products = cur.fetchall()

    conn.close()

    return render_template("przyjecie.html", products=products)


# 📥 ZAPIS PRZYJĘCIA
@app.route('/receive_doc', methods=['POST'])
@login_required
def receive_doc():
    return create_receipt()

    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, request.form.get('kontrahent'), "PZ", "", "PZ"))

    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')
    package_numbers = request.form.getlist('package_number')
    prices_netto = request.form.getlist('price_netto')
    prices_brutto = request.form.getlist('price_brutto')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        wh = warehouses[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        if qty <= 0:
            continue
        try:
            price_netto = float(prices_netto[i].replace(",", "."))
        except:
            price_netto = 0
        try:
            price_brutto = float(prices_brutto[i].replace(",", "."))
        except:
            price_brutto = 0

        # ✅ stan +
        cur.execute("""
            UPDATE products 
            SET qty = qty + %s 
            WHERE id=%s AND warehouse=%s
        """, (qty, pid, wh))

        # zapis pozycji
        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, price_netto, price_brutto)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, wh, price_netto, price_brutto))

        # pakiet
        if package_numbers[i]:
            cur.execute("""
                INSERT INTO packages(product_id, number, qty, warehouse)
                VALUES (%s,%s,%s,%s)
            """, (pid, package_numbers[i], qty, wh))

    conn.commit()
    conn.close()

    return redirect('/historia')


def process_excel_import(upload):
    if not upload or not upload.filename:
        return "Brak pliku do importu.", 400
    if not upload.filename.lower().endswith(".xlsx"):
        return "Dozwolony jest tylko plik .xlsx.", 400
    try:
        dataframe = pd.read_excel(io.BytesIO(upload.read()), engine="openpyxl")
    except Exception:
        return "Nie udało się odczytać pliku Excel.", 400
    required = {"name", "qty", "unit", "warehouse"}
    if not required.issubset(dataframe.columns):
        return f"Brak wymaganych kolumn: {', '.join(sorted(required))}.", 400

    rows = []
    try:
        for row_number, row in dataframe.iterrows():
            raw_name = row.get("name", "")
            raw_unit = row.get("unit", "")
            raw_warehouse = row.get("warehouse", "")
            name = "" if pd.isna(raw_name) else str(raw_name).strip()
            unit = "" if pd.isna(raw_unit) else str(raw_unit).strip()
            warehouse = "" if pd.isna(raw_warehouse) else str(raw_warehouse).strip()
            if not name or len(name) > 200 or unit not in UNITS or warehouse not in WAREHOUSES:
                raise ValueError(
                    f"Wiersz {row_number + 2}: nieprawidłowa nazwa, jednostka lub magazyn."
                )
            qty = parse_positive_number(row.get("qty"), f"Wiersz {row_number + 2}, ilość")
            raw_package_number = row.get("package_number", "")
            package_number = (
                "" if pd.isna(raw_package_number) else str(raw_package_number).strip()
            )
            if len(package_number) > 100:
                raise ValueError(f"Wiersz {row_number + 2}: numer paczki jest za długi.")
            rows.append((name, qty, unit, warehouse, package_number))
    except ValueError as exc:
        return str(exc), 400
    if not rows:
        return "Plik nie zawiera żadnych pozycji.", 400
    rows.sort(key=lambda row: (row[3], row[0].casefold(), row[4].casefold()))

    conn = db()
    cur = conn.cursor()
    try:
        date = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            """
            INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number, movement_type)
            VALUES (%s,%s,%s,%s,%s,'PZ') RETURNING id
            """,
            (date, f"Import: {upload.filename}", "", "", ""),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE issue_docs SET doc_number=%s WHERE id=%s",
            (f"PZ-IMPORT/{doc_id}/{datetime.now().year}", doc_id),
        )
        for name, qty, unit, warehouse, package_number in rows:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"product:{warehouse}:{name.casefold()}",),
            )
            cur.execute(
                """
                SELECT id FROM products
                WHERE warehouse=%s AND lower(name)=lower(%s)
                ORDER BY id LIMIT 1 FOR UPDATE
                """,
                (warehouse, name),
            )
            existing = cur.fetchone()
            if existing:
                product_id = existing[0]
                cur.execute(
                    "UPDATE products SET qty=qty+%s, unit=%s WHERE id=%s",
                    (qty, unit, product_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
                    VALUES (%s,%s,%s,%s,0,23) RETURNING id
                    """,
                    (name, qty, unit, warehouse),
                )
                product_id = cur.fetchone()[0]
            package_id = None
            if package_number:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"package:{warehouse}:{package_number.casefold()}",),
                )
                cur.execute(
                    """
                    SELECT 1 FROM packages
                    WHERE warehouse=%s AND lower(number)=lower(%s)
                    """,
                    (warehouse, package_number),
                )
                if cur.fetchone():
                    raise ValueError(f"Paczka {package_number} już istnieje w magazynie {warehouse}.")
                cur.execute(
                    """
                    INSERT INTO packages(product_id, number, qty, warehouse, initial_qty, status)
                    VALUES (%s,%s,%s,%s,%s,'active') RETURNING id
                    """,
                    (product_id, package_number, qty, warehouse, qty),
                )
                package_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO issue_items(doc_id, product_id, qty, warehouse, package_id, package_number)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (doc_id, product_id, qty, warehouse, package_id, package_number or None),
            )
        conn.commit()
        cache.clear()
        return redirect(f"/doc/{doc_id}")
    except ValueError as exc:
        conn.rollback()
        return str(exc), 400
    except Exception:
        conn.rollback()
        logger.exception("Excel import failed.")
        return "Błąd importu. Żadne dane nie zostały zapisane.", 500
    finally:
        conn.close()


@app.route('/import_excel', methods=['POST'])
@login_required
def import_excel():
    file = request.files.get("excel_file")
    return process_excel_import(file)

    if not file or not file.filename:
        return "Brak pliku do importu.", 400
    if not file.filename.lower().endswith(".xlsx"):
        return "Dozwolony jest tylko plik .xlsx", 400

    try:
        file_bytes = file.read()
        df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    except Exception:
        return "Nie udało się odczytać pliku Excel.", 400

    required_columns = ["name", "qty", "unit", "warehouse"]
    if not all(col in df.columns for col in required_columns):
        return f"Brak wymaganych kolumn: {', '.join(required_columns)}", 400

    conn = db()
    cur = conn.cursor()
    try:
        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            unit = str(row.get("unit", "")).strip()
            warehouse = str(row.get("warehouse", "")).strip()
            if not name or not unit or not warehouse:
                continue

            try:
                qty = float(str(row.get("qty", "0")).replace(",", "."))
            except Exception:
                continue
            if qty <= 0:
                continue

            cur.execute(
                "SELECT id FROM products WHERE name=%s AND warehouse=%s LIMIT 1",
                (name, warehouse)
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE products SET qty = qty + %s, unit=%s WHERE id=%s",
                    (qty, unit, existing[0])
                )
            else:
                cur.execute(
                    """
                    INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (name, qty, unit, warehouse, 0, 23)
                )

        conn.commit()
    except Exception:
        conn.rollback()
        return "Błąd importu. Sprawdź dane w pliku.", 400
    finally:
        conn.close()

    return redirect('/magazyny')


# 📤 WYDANIE
@app.route('/wydanie')
@login_required
def wydanie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products ORDER BY warehouse, lower(name), id")
    products = cur.fetchall()

    conn.close()

    return render_template("wydanie.html", products=products)


@app.route('/inwestycja-suwaj', endpoint='inwestycja_suwaj_page_view')
@login_required
def inwestycja_suwaj():
    return render_template("inwestycja_suwaj.html", warehouse=INVESTMENT_WAREHOUSE)


@app.route('/inwestycja-suwaj/magazyn')
@login_required
def inwestycja_suwaj_magazyn():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    products = cur.fetchall()
    conn.close()
    return render_template("index.html", products=products, warehouse=INVESTMENT_WAREHOUSE)


@app.route('/inwestycja-suwaj/przyjecie')
@login_required
def inwestycja_suwaj_przyjecie():
    conn = db()
    cur = conn.cursor()

    # pokazujemy wszystkie produkty, żeby można było dodać nowy asortyment do magazynu inwestycji
    cur.execute("SELECT * FROM products ORDER BY warehouse, lower(name), id")
    products = cur.fetchall()

    conn.close()

    return render_template(
        "przyjecie.html",
        products=products,
        forced_warehouse=INVESTMENT_WAREHOUSE,
        form_action="/inwestycja-suwaj/receive_doc",
        page_title="📥 Przyjęcie (PZ) – Inwestycja Suwaj"
    )


@app.route('/inwestycja-suwaj/receive_doc', methods=['POST'])
@login_required
def inwestycja_suwaj_receive_doc():
    return create_receipt(INVESTMENT_WAREHOUSE)

    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    num = cur.fetchone()[0] + 1
    doc_number = f"PZ-IS/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, INVESTMENT_WAREHOUSE, "", doc_number))
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    package_numbers = request.form.getlist('package_number')
    prices_netto = request.form.getlist('price_netto')
    prices_brutto = request.form.getlist('price_brutto')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue
        pid = int(product_ids[i])
        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0
        if qty <= 0:
            continue
        try:
            price_netto = float(prices_netto[i].replace(",", "."))
        except:
            price_netto = 0
        try:
            price_brutto = float(prices_brutto[i].replace(",", "."))
        except:
            price_brutto = 0

        cur.execute("""
            UPDATE products
            SET qty = qty + %s
            WHERE id=%s AND warehouse=%s
        """, (qty, pid, INVESTMENT_WAREHOUSE))
        updated = cur.rowcount

        # jeśli produktu nie ma jeszcze w magazynie inwestycji, sklonuj kartotekę i dodaj stan
        if updated == 0:
            cur.execute("""
                SELECT name, unit, price_netto, vat
                FROM products
                WHERE id=%s
            """, (pid,))
            source = cur.fetchone()

            if source:
                cur.execute("""
                    INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (source[0], qty, source[1], INVESTMENT_WAREHOUSE, source[2], source[3]))
                pid = cur.fetchone()[0]
            else:
                continue

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, price_netto, price_brutto)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, INVESTMENT_WAREHOUSE, price_netto, price_brutto))

        if i < len(package_numbers) and package_numbers[i]:
            cur.execute("""
                INSERT INTO packages(product_id, number, qty, warehouse)
                VALUES (%s,%s,%s,%s)
            """, (pid, package_numbers[i], qty, INVESTMENT_WAREHOUSE))

    conn.commit()
    conn.close()

    return redirect('/inwestycja-suwaj/magazyn')


@app.route('/inwestycja-suwaj/wydanie')
@login_required
def inwestycja_suwaj_wydanie():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE warehouse=%s ORDER BY lower(name), id",
        (INVESTMENT_WAREHOUSE,),
    )
    products = cur.fetchall()
    conn.close()

    return render_template(
        "wydanie.html",
        products=products,
        forced_warehouse=INVESTMENT_WAREHOUSE,
        form_action="/inwestycja-suwaj/issue_doc",
        page_title="📄 Wydanie (WZ) – Inwestycja Suwaj"
    )


@app.route('/inwestycja-suwaj/issue_doc', methods=['POST'])
@login_required
def inwestycja_suwaj_issue_doc():
    return create_issue(INVESTMENT_WAREHOUSE)

    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    num = cur.fetchone()[0] + 1
    doc_number = f"WZ-IS/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, INVESTMENT_WAREHOUSE, "", doc_number))
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    package_ids = request.form.getlist('package_id')
    prices_netto = request.form.getlist('price_netto')
    prices_brutto = request.form.getlist('price_brutto')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0
        if qty <= 0:
            continue
        try:
            price_netto = float(prices_netto[i].replace(",", "."))
        except:
            price_netto = 0
        try:
            price_brutto = float(prices_brutto[i].replace(",", "."))
        except:
            price_brutto = 0

        pkg = package_ids[i] if package_ids[i] else None
        if pkg:
            pkg = int(pkg)
            cur.execute("""
                UPDATE packages
                SET qty = qty - %s
                WHERE id=%s AND (warehouse=%s OR warehouse IS NULL) AND qty >= %s
            """, (qty, pkg, INVESTMENT_WAREHOUSE, qty))
            if cur.rowcount == 0:
                conn.rollback()
                conn.close()
                return "Brak w paczce"

        cur.execute("""
            UPDATE products
            SET qty = qty - %s
            WHERE id=%s AND warehouse=%s AND qty >= %s
        """, (qty, pid, INVESTMENT_WAREHOUSE, qty))

        if cur.rowcount == 0:
            conn.rollback()
            conn.close()
            return f"Brak stanu w magazynie {INVESTMENT_WAREHOUSE}"

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, package_id, price_netto, price_brutto)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, INVESTMENT_WAREHOUSE, pkg, price_netto, price_brutto))

    cur.execute("DELETE FROM packages WHERE qty <= 0")
    conn.commit()
    conn.close()

    return redirect(f"/doc/{doc_id}")


# 📤 ZAPIS WYDANIA (PRO)
@app.route('/issue_doc', methods=['POST'])
@login_required
def issue_doc():
    return create_issue()

    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs")
    num = cur.fetchone()[0] + 1
    doc_number = f"WZ/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, "", "", doc_number))

    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')
    package_ids = request.form.getlist('package_id')
    prices_netto = request.form.getlist('price_netto')
    prices_brutto = request.form.getlist('price_brutto')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        wh = warehouses[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        if qty <= 0:
            continue
        try:
            price_netto = float(prices_netto[i].replace(",", "."))
        except:
            price_netto = 0
        try:
            price_brutto = float(prices_brutto[i].replace(",", "."))
        except:
            price_brutto = 0

        pkg = package_ids[i] if package_ids[i] else None

        # pakiet
        if pkg:
            pkg = int(pkg)

            cur.execute("""
                UPDATE packages
                SET qty = qty - %s
                WHERE id=%s AND warehouse=%s AND qty >= %s
            """, (qty, pkg, wh, qty))
            if cur.rowcount == 0:
                conn.rollback()
                conn.close()
                return "Brak w paczce"

        # ✅ stan -
        cur.execute("""
            UPDATE products 
            SET qty = qty - %s 
            WHERE id=%s AND warehouse=%s AND qty >= %s
        """, (qty, pid, wh, qty))
        if cur.rowcount == 0:
            conn.rollback()
            conn.close()
            return f"Brak stanu w magazynie {wh}"

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, package_id, price_netto, price_brutto)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, wh, pkg, price_netto, price_brutto))

    # czyść puste paczki
    cur.execute("DELETE FROM packages WHERE qty <= 0")

    conn.commit()
    conn.close()

    return redirect(f"/doc/{doc_id}")


# 📄 SZCZEGÓŁ
@app.route('/doc/<int:id>')
@login_required
def doc_detail(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM issue_docs WHERE id=%s", (id,))
    doc = cur.fetchone()
    if not doc:
        conn.close()
        return "Nie znaleziono dokumentu.", 404

    cur.execute("""
        SELECT p.name, i.qty, COALESCE(i.warehouse, p.warehouse),
               COALESCE(i.package_number, pk.number)
        FROM issue_items i
        JOIN products p ON p.id = i.product_id
        LEFT JOIN packages pk ON pk.id = i.package_id
        WHERE i.doc_id=%s
    """, (id,))
    items = cur.fetchall()

    conn.close()

    return render_template("doc_detail.html", doc=doc, items=items)


@app.route('/doc/<int:id>/edit', methods=['POST'])
@login_required
def edit_doc(id):
    contractor = (request.form.get("kontrahent") or "").strip()
    try:
        date = normalized_document_date(request.form.get("date"))
    except ValueError as exc:
        return str(exc), 400
    if not contractor:
        return "Kontrahent jest wymagany.", 400
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE issue_docs
        SET kontrahent=%s, date=%s
        WHERE id=%s AND voided_at IS NULL
    """, (contractor, date, id))
    conn.commit()
    conn.close()
    cache.clear()
    return redirect(f"/doc/{id}")


def void_document(document_id):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT movement_type, doc_number, voided_at
            FROM issue_docs WHERE id=%s FOR UPDATE
            """,
            (document_id,),
        )
        document = cur.fetchone()
        if not document:
            return redirect("/historia")
        if document[2]:
            return redirect(f"/doc/{document_id}")
        movement_type = document[0] or (
            "WZ" if str(document[1] or "").startswith("WZ") else "PZ"
        )
        cur.execute(
            """
            SELECT product_id, qty, warehouse, package_id
            FROM issue_items WHERE doc_id=%s ORDER BY id FOR UPDATE
            """,
            (document_id,),
        )
        items = cur.fetchall()
        if movement_type in {"WZ", "RW"}:
            for product_id, qty, warehouse, package_id in items:
                cur.execute(
                    "UPDATE products SET qty=qty+%s WHERE id=%s AND warehouse=%s",
                    (qty, product_id, warehouse),
                )
                if package_id:
                    cur.execute(
                        """
                        UPDATE packages
                        SET qty=qty+%s, status='active', archived_at=NULL
                        WHERE id=%s
                        """,
                        (qty, package_id),
                    )
        else:
            for product_id, qty, warehouse, package_id in items:
                cur.execute(
                    """
                    UPDATE products SET qty=qty-%s
                    WHERE id=%s AND warehouse=%s AND qty >= %s
                    """,
                    (qty, product_id, warehouse, qty),
                )
                if cur.rowcount != 1:
                    raise ValueError("Nie można anulować przyjęcia: część towaru została już wydana.")
                if package_id:
                    cur.execute(
                        "SELECT qty FROM packages WHERE id=%s FOR UPDATE",
                        (package_id,),
                    )
                    package = cur.fetchone()
                    if not package or package[0] + 1e-9 < qty:
                        raise ValueError("Nie można anulować przyjęcia: paczka została już częściowo wydana.")
                    cur.execute(
                        """
                        UPDATE packages
                        SET qty=0, status='cancelled', archived_at=NOW()
                        WHERE id=%s
                        """,
                        (package_id,),
                    )
        cur.execute(
            "UPDATE issue_docs SET voided_at=NOW(), voided_by=%s WHERE id=%s",
            (session.get("user"), document_id),
        )
        conn.commit()
        cache.clear()
        return redirect(f"/doc/{document_id}")
    except ValueError as exc:
        conn.rollback()
        return str(exc), 409
    except Exception:
        conn.rollback()
        logger.exception("Document cancellation failed.")
        return "Nie udało się anulować dokumentu.", 500
    finally:
        conn.close()


@app.route('/doc/<int:id>/delete', methods=['POST'])
@admin_required
def delete_doc(id):
    return void_document(id)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, date, kontrahent, warehouse, image, doc_number FROM issue_docs WHERE id=%s", (id,))
    doc = cur.fetchone()
    if not doc:
        conn.close()
        return redirect('/historia')

    cur.execute("""
        SELECT product_id, qty, warehouse, package_id
        FROM issue_items
        WHERE doc_id=%s
    """, (id,))
    items = cur.fetchall()

    is_issue_doc = str(doc[5] or "").startswith("WZ")

    for product_id, qty, warehouse, package_id in items:
        wh = warehouse or ""
        sign = 1 if is_issue_doc else -1
        cur.execute("""
            UPDATE products
            SET qty = qty + %s
            WHERE id=%s AND warehouse=%s
        """, (sign * qty, product_id, wh))

        if package_id and is_issue_doc:
            cur.execute("UPDATE packages SET qty = qty + %s WHERE id=%s", (qty, package_id))

    cur.execute("DELETE FROM issue_items WHERE doc_id=%s", (id,))
    cur.execute("DELETE FROM issue_docs WHERE id=%s", (id,))
    cur.execute("DELETE FROM packages WHERE qty <= 0")
    conn.commit()
    conn.close()
    return redirect('/historia')


# 📊 HISTORIA
@app.route('/historia')
@login_required
@cache.cached(timeout=30, query_string=True)
def historia():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM issue_docs ORDER BY id DESC")
    docs = cur.fetchall()
    conn.close()

    days = {}
    for d in docs:
        days.setdefault(d[1], []).append(d)

    return render_template("historia.html", days=days)


@app.route('/report')
@login_required
@cache.cached(timeout=30, query_string=True)
def report():
    selected_date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.date, COALESCE(d.kontrahent, ''), COALESCE(p.name, ''), i.qty, COALESCE(i.warehouse, p.warehouse, '')
        FROM issue_docs d
        JOIN issue_items i ON i.doc_id = d.id
        LEFT JOIN products p ON p.id = i.product_id
        WHERE d.date = %s AND d.voided_at IS NULL
        ORDER BY d.id DESC, i.id ASC
        """,
        (selected_date,)
    )
    operations = cur.fetchall()
    conn.close()
    return render_template("report.html", selected_date=selected_date, operations=operations)


@app.route('/report/pdf')
@login_required
def report_pdf():
    selected_date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.date, COALESCE(d.kontrahent, ''), COALESCE(p.name, ''), i.qty, COALESCE(i.warehouse, p.warehouse, '')
        FROM issue_docs d
        JOIN issue_items i ON i.doc_id = d.id
        LEFT JOIN products p ON p.id = i.product_id
        WHERE d.date = %s AND d.voided_at IS NULL
        ORDER BY d.id DESC, i.id ASC
        """,
        (selected_date,)
    )
    operations = cur.fetchall()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("Raport dzienny magazynu", styles["Title"]),
        Spacer(1, 8),
        Paragraph(f"Data: {selected_date}", styles["Normal"]),
        Spacer(1, 12),
    ]

    data = [["Data", "Kontrahent", "Produkt", "Ilość", "Magazyn"]]
    for row in operations:
        data.append([str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4])])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FFC067")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (3, 1), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=raport-{selected_date}.pdf"}
    )


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
