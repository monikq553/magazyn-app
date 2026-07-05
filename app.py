import os
import logging
import math
import secrets
import threading
from flask import Flask, render_template, request, redirect, session, jsonify, Response, make_response
from functools import wraps
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
import json
import io
import pandas as pd
from urllib import request as urlrequest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import firebase_admin
from firebase_admin import credentials, auth, storage
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from backup_service import (
    decrypt_backup,
    encrypt_backup,
    export_database,
    parse_backup,
    restore_database,
)

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
IS_PRODUCTION = os.environ.get("RENDER", "").lower() == "true"
configured_secret_key = os.environ.get("SECRET_KEY")
if IS_PRODUCTION and not configured_secret_key:
    raise RuntimeError("Brak wymaganej zmiennej środowiskowej SECRET_KEY.")
app.secret_key = configured_secret_key or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_NAME="magazyn_csrf_session",
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
    "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", ""),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", ""),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", ""),
    "appId": os.environ.get("FIREBASE_APP_ID", ""),
    "measurementId": os.environ.get("FIREBASE_MEASUREMENT_ID", ""),
}
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
FIREBASE_ADMIN_READY = False
FIREBASE_ADMIN_ERROR = ""
DB_POOL = None
DB_INIT_LOCK = threading.Lock()
DB_INITIALIZED = False
DAILY_BACKUP_LOCK = threading.Lock()
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


if IS_PRODUCTION and get_missing_firebase_web_envs():
    raise RuntimeError(
        "Brak wymaganych zmiennych Firebase Web: "
        + ", ".join(get_missing_firebase_web_envs())
    )
init_firebase_admin()
if IS_PRODUCTION and not FIREBASE_ADMIN_READY:
    raise RuntimeError(FIREBASE_ADMIN_ERROR or "Firebase Admin nie jest skonfigurowany.")


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
        firebase_uid TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        email TEXT UNIQUE NOT NULL,
        role TEXT NOT NULL DEFAULT 'employee',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations(
        name TEXT PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute(
        "SELECT 1 FROM schema_migrations WHERE name='firebase_users_v2'"
    )
    if not cur.fetchone():
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='password'
            )
        """)
        if cur.fetchone()[0]:
            cur.execute("DROP TABLE users")
            cur.execute("""
                CREATE TABLE users(
                    id SERIAL PRIMARY KEY,
                    firebase_uid TEXT UNIQUE NOT NULL,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    email TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL DEFAULT 'employee',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        else:
            # Jednorazowe usunięcie wcześniejszych kont zgodnie z migracją do Firebase.
            cur.execute("DELETE FROM users")
        cur.execute(
            "INSERT INTO schema_migrations(name) VALUES ('firebase_users_v2')"
        )

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
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='users_role_valid') THEN
                ALTER TABLE users
                ADD CONSTRAINT users_role_valid CHECK (role IN ('admin', 'employee', 'warehouse', 'shop', 'accounting'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='users_status_valid') THEN
                ALTER TABLE users
                ADD CONSTRAINT users_status_valid CHECK (status IN ('active', 'blocked'));
            END IF;
        END $$;
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS backup_runs(
        id SERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by TEXT NOT NULL,
        status TEXT NOT NULL,
        object_name TEXT,
        size_bytes BIGINT,
        checksum TEXT,
        error TEXT
    );
    """)
    cur.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_valid")
    cur.execute("""
        ALTER TABLE users
        ADD CONSTRAINT users_role_valid
        CHECK (role IN ('admin', 'employee', 'warehouse', 'shop', 'accounting'))
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_orders(
        id SERIAL PRIMARY KEY,
        order_number TEXT UNIQUE NOT NULL,
        order_date DATE NOT NULL,
        customer_name TEXT NOT NULL,
        delivery_address TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        shipping_cost REAL NOT NULL DEFAULT 0,
        payment_method TEXT,
        payment_status TEXT NOT NULL DEFAULT 'Oczekuje na płatność',
        status TEXT NOT NULL DEFAULT 'Nowe zamówienie',
        sales_document_number TEXT,
        tracking_number TEXT,
        notes TEXT,
        nip TEXT,
        document_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_order_items(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES shop_orders(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
        product_name TEXT NOT NULL,
        qty REAL NOT NULL CHECK (qty > 0),
        price_netto REAL NOT NULL DEFAULT 0,
        price_brutto REAL NOT NULL DEFAULT 0,
        vat REAL NOT NULL DEFAULT 0,
        warehouse TEXT NOT NULL,
        reserved_qty REAL NOT NULL DEFAULT 0,
        issued_qty REAL NOT NULL DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_order_history(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES shop_orders(id) ON DELETE CASCADE,
        user_email TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_notifications(
        id SERIAL PRIMARY KEY,
        order_id INTEGER REFERENCES shop_orders(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        message TEXT NOT NULL,
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_sales_documents(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL UNIQUE REFERENCES shop_orders(id) ON DELETE CASCADE,
        document_number TEXT NOT NULL,
        editable_data JSONB NOT NULL,
        docx BYTEA,
        pdf BYTEA,
        confirmed BOOLEAN NOT NULL DEFAULT FALSE,
        created_by TEXT,
        confirmed_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        confirmed_at TIMESTAMPTZ
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_accounting(
        order_id INTEGER PRIMARY KEY REFERENCES shop_orders(id) ON DELETE CASCADE,
        proforma_issued BOOLEAN NOT NULL DEFAULT FALSE,
        reserved_by_proforma BOOLEAN NOT NULL DEFAULT FALSE,
        waiting_for_payment BOOLEAN NOT NULL DEFAULT FALSE,
        partial_payment BOOLEAN NOT NULL DEFAULT FALSE,
        paid BOOLEAN NOT NULL DEFAULT FALSE,
        payment_method TEXT,
        invoice_issued BOOLEAN NOT NULL DEFAULT FALSE,
        receipt_issued BOOLEAN NOT NULL DEFAULT FALSE,
        invoice_sent BOOLEAN NOT NULL DEFAULT FALSE,
        document_to_warehouse BOOLEAN NOT NULL DEFAULT FALSE,
        ready_to_ship BOOLEAN NOT NULL DEFAULT FALSE,
        settled BOOLEAN NOT NULL DEFAULT FALSE,
        proforma_number TEXT,
        invoice_number TEXT,
        receipt_number TEXT,
        document_issue_date DATE,
        payment_received_date DATE,
        amount_paid REAL NOT NULL DEFAULT 0,
        amount_due REAL NOT NULL DEFAULT 0,
        accounting_notes TEXT,
        salesperson TEXT,
        updated_by TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_shop_accounting_filters ON shop_accounting(payment_method, proforma_number, invoice_number, receipt_number, salesperson)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_shop_orders_search ON shop_orders(lower(order_number), lower(customer_name), lower(status), order_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_shop_items_product ON shop_order_items(product_id)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_doc_photos(
        id SERIAL PRIMARY KEY,
        doc_id INTEGER NOT NULL REFERENCES issue_docs(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        content_type TEXT NOT NULL,
        data BYTEA NOT NULL,
        note TEXT,
        added_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_doc_history(
        id SERIAL PRIMARY KEY,
        doc_id INTEGER NOT NULL REFERENCES issue_docs(id) ON DELETE CASCADE,
        user_email TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_doc_photos_doc ON issue_doc_photos(doc_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_doc_history_doc ON issue_doc_history(doc_id)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
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

ALLOWED_ISSUE_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_ISSUE_PHOTO_BYTES = 5 * 1024 * 1024


def is_issue_document(movement_type, doc_number):
    movement = (movement_type or "").upper()
    number = (doc_number or "").upper()
    return movement in {"WZ", "RW"} or number.startswith("WZ") or number.startswith("RW")


def issue_doc_history(cur, doc_id, action, details=""):
    cur.execute(
        """
        INSERT INTO issue_doc_history(doc_id, user_email, action, details)
        VALUES (%s,%s,%s,%s)
        """,
        (doc_id, session.get("user", "system"), action, details),
    )


def clean_photo_filename(filename):
    filename = (filename or "zdjecie").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    return filename[:180] or "zdjecie"


def save_issue_photos(cur, doc_id, uploads, note=""):
    saved = 0
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        content_type = (upload.mimetype or "").lower()
        if content_type not in ALLOWED_ISSUE_PHOTO_TYPES:
            raise ValueError("Dozwolone są tylko zdjęcia JPG, PNG, WEBP lub GIF.")
        data = upload.read()
        if not data:
            continue
        if len(data) > MAX_ISSUE_PHOTO_BYTES:
            raise ValueError("Jedno zdjęcie może mieć maksymalnie 5 MB.")
        cur.execute(
            """
            INSERT INTO issue_doc_photos(doc_id, filename, content_type, data, note, added_by)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                doc_id,
                clean_photo_filename(upload.filename),
                content_type,
                psycopg2.Binary(data),
                (note or "").strip()[:500],
                session.get("user", "system"),
            ),
        )
        saved += 1
    if saved:
        issue_doc_history(cur, doc_id, "dodano zdjęcia", f"Liczba zdjęć: {saved}")
    return saved

SHOP_ROLES = {"admin", "employee", "warehouse", "shop", "accounting"}
SHOP_STATUS_FLOW = [
    "Nowe zamówienie", "Przyjęte", "Oczekuje na płatność", "Opłacone",
    "Towar zarezerwowany", "Dokument wystawiony", "W trakcie pakowania",
    "Spakowane", "Wysłane", "Dostarczone", "Zakończone", "Anulowane",
]
SHOP_ROLE_LABELS = {
    "admin": "Administrator", "employee": "Pracownik", "warehouse": "Magazynier",
    "shop": "Obsługa sklepu internetowego", "accounting": "Księgowość",
}

ACCOUNTING_PAYMENT_METHODS = ["Przelew", "Gotówka", "Karta", "BLIK", "Autopay", "Pobranie", "Inny"]
ACCOUNTING_BOOL_FIELDS = [
    "proforma_issued", "reserved_by_proforma", "waiting_for_payment", "partial_payment",
    "paid", "invoice_issued", "receipt_issued", "invoice_sent", "document_to_warehouse",
    "ready_to_ship", "settled",
]
ACCOUNTING_FIELD_LABELS = {
    "proforma_issued": "Proforma wystawiona",
    "reserved_by_proforma": "Towar zarezerwowany na podstawie proformy",
    "waiting_for_payment": "Oczekiwanie na płatność",
    "partial_payment": "Płatność częściowa",
    "paid": "Zapłacono",
    "invoice_issued": "Faktura wystawiona",
    "receipt_issued": "Paragon wystawiony",
    "invoice_sent": "Faktura wysłana do klienta",
    "document_to_warehouse": "Dokument przekazany do magazynu",
    "ready_to_ship": "Zamówienie gotowe do wysyłki",
    "settled": "Zamówienie rozliczone",
}


def accounting_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        if session.get('role') not in {'admin', 'accounting'}:
            return "Brak uprawnień", 403
        return f(*args, **kwargs)
    return decorated


def order_can_be_shipped(accounting_row):
    if not accounting_row:
        return False
    payment_method = accounting_row[6]
    paid = bool(accounting_row[5])
    ready_to_ship = bool(accounting_row[11])
    return ready_to_ship and (paid or payment_method == "Pobranie")


def ensure_shop_accounting_row(cur, order_id):
    cur.execute(
        """
        INSERT INTO shop_accounting(order_id, amount_due)
        SELECT o.id, COALESCE(SUM(i.qty*i.price_brutto),0)+o.shipping_cost
        FROM shop_orders o
        LEFT JOIN shop_order_items i ON i.order_id=o.id
        WHERE o.id=%s
        GROUP BY o.id
        ON CONFLICT (order_id) DO NOTHING
        """,
        (order_id,),
    )


def sync_accounting_payment_status(cur, order_id):
    cur.execute("SELECT paid, partial_payment, waiting_for_payment, payment_method FROM shop_accounting WHERE order_id=%s", (order_id,))
    row = cur.fetchone()
    if not row:
        return
    if row[0]:
        status = "Opłacone"
    elif row[1]:
        status = "Płatność częściowa"
    elif row[2]:
        status = "Oczekuje na płatność"
    elif row[3] == "Pobranie":
        status = "Pobranie"
    else:
        status = "Oczekuje na płatność"
    cur.execute("UPDATE shop_orders SET payment_status=%s, payment_method=COALESCE(NULLIF((SELECT payment_method FROM shop_accounting WHERE order_id=%s), ''), payment_method), updated_at=NOW() WHERE id=%s", (status, order_id, order_id))


def current_user_role():
    return session.get("role", "employee")


def can_shop(action):
    role = current_user_role()
    if role == "admin":
        return True
    return action in {
        "shop_edit": {"shop"},
        "warehouse": {"warehouse"},
        "accounting": {"accounting"},
        "view": {"shop", "warehouse", "accounting"},
    }.get(action, set())


def require_shop_permission(action):
    if not can_shop(action):
        return "Brak uprawnień do tej funkcji modułu sklepu internetowego.", 403
    return None


def shop_history(cur, order_id, action, details=""):
    cur.execute(
        """
        INSERT INTO shop_order_history(order_id, user_email, action, details)
        VALUES (%s,%s,%s,%s)
        """,
        (order_id, session.get("user", "system"), action, details),
    )


def create_shop_document_payload(order, items):
    subtotal_net = sum((item[5] or 0) * (item[4] or 0) for item in items)
    subtotal_gross = sum((item[6] or 0) * (item[4] or 0) for item in items)
    shipping = order[8] or 0
    return {
        "document_number": order[11] or f"DS/{order[0]}/{datetime.now().year}",
        "date": order[2],
        "receipt_or_invoice": order[11] or "Do uzupełnienia",
        "seller": "Primadera",
        "buyer": order[3],
        "address": order[4],
        "nip": order[14] or "",
        "items": [
            {
                "name": item[3], "qty": item[4], "net": item[5], "vat": item[7],
                "gross": item[6], "total_gross": (item[6] or 0) * (item[4] or 0),
            }
            for item in items
        ],
        "shipping": shipping,
        "total_net": subtotal_net,
        "total_gross": subtotal_gross + shipping,
        "notes": order[13] or "",
    }


def simple_docx_bytes(payload):
    import zipfile
    from xml.sax.saxutils import escape
    lines = [
        "Dokument sprzedaży", f"Numer: {payload['document_number']}", f"Data: {payload['date']}",
        f"Paragon/Faktura: {payload['receipt_or_invoice']}", f"Sprzedawca: {payload['seller']}",
        f"Nabywca: {payload['buyer']}", f"Adres: {payload['address']}", f"NIP: {payload['nip']}",
        "Produkty:",
    ]
    for item in payload["items"]:
        lines.append(f"{item['name']} | ilość {item['qty']} | netto {item['net']:.2f} | VAT {item['vat']:.0f}% | brutto {item['total_gross']:.2f}")
    lines += [f"Wysyłka: {payload['shipping']:.2f}", f"Razem brutto: {payload['total_gross']:.2f}", f"Uwagi: {payload['notes']}"]
    paragraphs = "".join(f"<w:p><w:r><w:t>{escape(str(line))}</w:t></w:r></w:p>" for line in lines)
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{paragraphs}<w:sectPr/></w:body></w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def shop_pdf_bytes(payload):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [Paragraph("Dokument sprzedaży", styles["Title"]), Spacer(1, 12)]
    for key, label in [("document_number", "Numer"), ("date", "Data"), ("receipt_or_invoice", "Paragon/Faktura"), ("buyer", "Nabywca"), ("address", "Adres"), ("nip", "NIP")]:
        story.append(Paragraph(f"<b>{label}:</b> {payload.get(key) or ''}", styles["Normal"]))
    data = [["Produkt", "Ilość", "Netto", "VAT", "Brutto"]] + [[i["name"], i["qty"], f"{i['net']:.2f}", f"{i['vat']:.0f}%", f"{i['total_gross']:.2f}"] for i in payload["items"]]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.lightgrey), ("GRID", (0,0), (-1,-1), 0.5, colors.grey)]))
    story += [Spacer(1, 12), table, Spacer(1, 12), Paragraph(f"Wysyłka: {payload['shipping']:.2f}", styles["Normal"]), Paragraph(f"Razem brutto: {payload['total_gross']:.2f}", styles["Heading2"]), Paragraph(f"Uwagi: {payload['notes']}", styles["Normal"])]
    doc.build(story)
    return buffer.getvalue()


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
    )


@app.route('/register')
def register():
    return redirect('/login')


@app.route('/auth/session', methods=['POST'])
@limiter.limit("10 per minute")
def create_session():
    payload = request.get_json(silent=True) or {}
    id_token = payload.get("idToken")
    if not id_token:
        return jsonify({"ok": False, "error": "Brak tokenu"}), 400

    if not FIREBASE_ADMIN_READY:
        return jsonify({
            "ok": False,
            "error": FIREBASE_ADMIN_ERROR or "Firebase Admin nie jest skonfigurowany.",
        }), 503
    try:
        decoded = auth.verify_id_token(id_token, check_revoked=True)
        email = (decoded.get("email") or "").strip().lower()
        uid = decoded.get("uid")
        firebase_user = auth.get_user(uid)
        if firebase_user.disabled:
            return jsonify({"ok": False, "error": "Konto jest zablokowane."}), 403
    except Exception:
        return jsonify({"ok": False, "error": "Nieprawidłowy lub wygasły token"}), 401
    if not email or not uid:
        return jsonify({"ok": False, "error": "Brak danych użytkownika"}), 400

    ensure_db_initialized()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, status FROM users WHERE firebase_uid=%s OR lower(email)=lower(%s) FOR UPDATE",
        (uid, email),
    )
    row = cur.fetchone()
    if not row:
        if email not in ADMIN_EMAILS:
            conn.close()
            return jsonify({"ok": False, "error": "Brak konta. Skontaktuj się z administratorem."}), 403
        display_name = (decoded.get("name") or firebase_user.display_name or "").strip()
        first_name, _, last_name = display_name.partition(" ")
        role = "admin"
        cur.execute(
            """
            INSERT INTO users(firebase_uid, first_name, last_name, email, role, status)
            VALUES (%s,%s,%s,%s,'admin','active')
            """,
            (uid, first_name, last_name, email),
        )
    else:
        role, status = row
        if status != "active":
            conn.close()
            return jsonify({"ok": False, "error": "Konto jest zablokowane."}), 403
        cur.execute(
            """
            UPDATE users SET firebase_uid=%s, email=%s, updated_at=NOW()
            WHERE firebase_uid=%s OR lower(email)=lower(%s)
            """,
            (uid, email, uid, email),
        )
    conn.commit()
    conn.close()
    expires = timedelta(days=5)
    try:
        firebase_cookie = auth.create_session_cookie(id_token, expires_in=expires)
    except Exception:
        logger.exception("Firebase session cookie creation failed.")
        return jsonify({"ok": False, "error": "Nie udało się utworzyć bezpiecznej sesji."}), 500
    session['user'] = email
    session['role'] = role
    session['uid'] = uid
    response = make_response(jsonify({"ok": True, "role": role}))
    response.set_cookie(
        "firebase_session",
        firebase_cookie,
        max_age=int(expires.total_seconds()),
        httponly=True,
        secure=os.environ.get("RENDER", "").lower() == "true",
        samesite="Lax",
    )
    return response


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    response = make_response(redirect('/login'))
    response.delete_cookie("firebase_session")
    return response


@app.before_request
def require_login_for_private_app():
    if IS_PRODUCTION and not request.is_secure:
        secure_url = request.url.replace("http://", "https://", 1)
        return redirect(secure_url, code=308)
    public_routes = {"static", "favicon", "health"}
    if request.endpoint in public_routes:
        return None

    def csrf_error():
        if request.is_json or request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Sesja formularza wygasła. Odśwież stronę."}), 400
        return "Sesja formularza wygasła. Odśwież stronę.", 400

    def validate_csrf():
        sent_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not sent_token or not secrets.compare_digest(sent_token, session.get("_csrf_token", "")):
            return csrf_error()
        return None

    if request.endpoint == "create_session":
        return validate_csrf()

    if request.endpoint == "logout":
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return validate_csrf()
        return None

    firebase_cookie = request.cookies.get("firebase_session")
    decoded = None
    if firebase_cookie and FIREBASE_ADMIN_READY:
        try:
            decoded = auth.verify_session_cookie(firebase_cookie, check_revoked=True)
        except Exception:
            decoded = None

    if decoded:
        uid = decoded.get("uid")
        email = (decoded.get("email") or "").strip().lower()
        ensure_db_initialized()
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT role, status FROM users WHERE firebase_uid=%s AND lower(email)=lower(%s)",
            (uid, email),
        )
        account = cur.fetchone()
        conn.close()
        if account and account[1] == "active":
            session["user"] = email
            session["uid"] = uid
            session["role"] = account[0]
            if request.endpoint == "login":
                return redirect("/")
        else:
            decoded = None

    if not decoded:
        session.pop("user", None)
        session.pop("uid", None)
        session.pop("role", None)
        if request.endpoint == "login":
            return None
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"ok": False, "error": "Wymagane logowanie."}), 401
        return redirect("/login")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf_failure = validate_csrf()
        if csrf_failure:
            return csrf_failure
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
        maybe_start_daily_backup()
        return jsonify({"status": "ok"})
    except Exception:
        logger.exception("Health check failed.")
        return jsonify({"status": "error", "database": "unavailable"}), 503


def maybe_start_daily_backup():
    if not os.environ.get("BACKUP_ENCRYPTION_KEY") or not FIREBASE_ADMIN_READY:
        return
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM backup_runs
        WHERE status='completed' AND created_at::date=(NOW() AT TIME ZONE 'UTC')::date
        LIMIT 1
        """
    )
    already_completed = bool(cur.fetchone())
    conn.close()
    if already_completed or not DAILY_BACKUP_LOCK.acquire(blocking=False):
        return

    def run_backup():
        try:
            with app.app_context():
                perform_database_backup("automatic-fallback")
        except Exception:
            logger.exception("Automatic fallback backup failed.")
        finally:
            DAILY_BACKUP_LOCK.release()

    threading.Thread(
        target=run_backup,
        name="daily-database-backup",
        daemon=True,
    ).start()


@app.after_request
def add_private_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.gstatic.com; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data:; "
        "connect-src 'self' https://identitytoolkit.googleapis.com "
        "https://securetoken.googleapis.com https://www.googleapis.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
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
        top_products=top_products,
        max_top_qty=max((row[1] or 0 for row in top_products), default=0),
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
    cur.execute(
        """
        SELECT id, firebase_uid, first_name, last_name, email, role, status
        FROM users ORDER BY lower(last_name), lower(first_name), lower(email)
        """
    )
    users_list = cur.fetchall()
    conn.close()
    return render_template("users.html", users=users_list, role_labels=SHOP_ROLE_LABELS)


@app.route('/add_user', methods=['POST'])
@admin_required
def add_user():
    email = (request.form.get("email") or "").strip().lower()
    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()
    if not email or "@" not in email or len(email) > 254:
        return "Podaj prawidłowy adres e-mail.", 400
    if not first_name or not last_name or len(first_name) > 100 or len(last_name) > 100:
        return "Imię i nazwisko są wymagane (maksymalnie 100 znaków).", 400
    role = request.form.get("role", "employee")
    if role not in SHOP_ROLES:
        return "Nieprawidłowa rola.", 400
    if not FIREBASE_ADMIN_READY:
        return "Firebase Admin nie jest skonfigurowany.", 503

    created_in_firebase = False
    try:
        try:
            firebase_user = auth.get_user_by_email(email)
            if firebase_user.disabled:
                firebase_user = auth.update_user(
                    firebase_user.uid,
                    disabled=False,
                    display_name=f"{first_name} {last_name}",
                )
        except auth.UserNotFoundError:
            firebase_user = auth.create_user(
                email=email,
                display_name=f"{first_name} {last_name}",
                disabled=False,
            )
            created_in_firebase = True
    except Exception:
        logger.exception("Firebase user creation failed.")
        return "Nie udało się utworzyć użytkownika w Firebase.", 502

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO users(firebase_uid, first_name, last_name, email, role, status)
            VALUES (%s,%s,%s,%s,%s,'active')
            ON CONFLICT (email) DO UPDATE SET
                firebase_uid=EXCLUDED.firebase_uid,
                first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,
                role=EXCLUDED.role,
                status='active',
                updated_at=NOW()
            """,
            (firebase_user.uid, first_name, last_name, email, role),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        if created_in_firebase:
            try:
                auth.delete_user(firebase_user.uid)
            except Exception:
                logger.exception("Firebase compensation delete failed.")
        logger.exception("Application user creation failed.")
        return "Nie udało się zapisać użytkownika.", 500
    finally:
        conn.close()
    return redirect('/users')


@app.route('/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT firebase_uid, email, role, status FROM users WHERE id=%s FOR UPDATE",
        (user_id,),
    )
    user = cur.fetchone()
    if not user:
        conn.close()
        return redirect('/users')
    if user[0] == session.get("uid"):
        conn.close()
        return "Nie możesz usunąć aktualnie zalogowanego konta.", 400
    if user[2] == "admin" and user[3] == "active":
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")
        if cur.fetchone()[0] <= 1:
            conn.close()
            return "Nie można usunąć ostatniego administratora.", 400
    try:
        auth.delete_user(user[0])
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        conn.commit()
    except auth.UserNotFoundError:
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("User deletion failed.")
        return "Nie udało się usunąć użytkownika.", 502
    finally:
        conn.close()
    return redirect('/users')


@app.route('/update_user_role/<int:user_id>', methods=['POST'])
@admin_required
def update_user_role(user_id):
    new_role = request.form.get('role', 'employee')
    if new_role not in SHOP_ROLES:
        return redirect('/users')

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role, status FROM users WHERE id=%s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return redirect('/users')

    old_role, status = row
    if old_role == 'admin' and new_role != 'admin' and status == "active":
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")
        admins_count = cur.fetchone()[0]
        if admins_count <= 1:
            conn.close()
            return redirect('/users')

    cur.execute("UPDATE users SET role=%s, updated_at=NOW() WHERE id=%s", (new_role, user_id))
    conn.commit()
    conn.close()
    return redirect('/users')


def send_firebase_password_reset(email):
    endpoint = (
        "https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode"
        f"?key={FIREBASE_CONFIG['apiKey']}"
    )
    payload = json.dumps({"requestType": "PASSWORD_RESET", "email": email}).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=10) as response:
        return response.status == 200


@app.route('/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
@limiter.limit("10 per hour")
def reset_user_password(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return "Nie znaleziono użytkownika.", 404
    try:
        send_firebase_password_reset(row[0])
    except Exception:
        logger.exception("Password reset email failed.")
        return "Nie udało się wysłać wiadomości resetującej hasło.", 502
    return redirect("/users?reset=sent")


@app.route('/users/<int:user_id>/toggle-block', methods=['POST'])
@admin_required
def toggle_user_block(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT firebase_uid, role, status FROM users WHERE id=%s FOR UPDATE",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return "Nie znaleziono użytkownika.", 404
    uid, role, status = row
    if uid == session.get("uid"):
        conn.close()
        return "Nie możesz zablokować własnego konta.", 400
    target_status = "active" if status == "blocked" else "blocked"
    if role == "admin" and status == "active" and target_status == "blocked":
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")
        if cur.fetchone()[0] <= 1:
            conn.close()
            return "Nie można zablokować ostatniego administratora.", 400
    try:
        auth.update_user(uid, disabled=(target_status == "blocked"))
        cur.execute(
            "UPDATE users SET status=%s, updated_at=NOW() WHERE id=%s",
            (target_status, user_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("User block toggle failed.")
        return "Nie udało się zmienić statusu konta.", 502
    finally:
        conn.close()
    return redirect("/users")


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
    product_ids = [row[0] for row in products]
    package_modes = {}
    if product_ids:
        cur.execute(
            """
            SELECT p.id,
                   COALESCE(SUM(CASE WHEN pk.status='active' AND pk.qty>0 THEN pk.qty ELSE 0 END),0) AS numbered_qty
            FROM products p
            LEFT JOIN packages pk ON pk.product_id=p.id AND pk.warehouse=p.warehouse
            WHERE p.id = ANY(%s)
            GROUP BY p.id, p.qty
            """,
            (product_ids,),
        )
        for product_id, numbered_qty in cur.fetchall():
            product = next((row for row in products if row[0] == product_id), None)
            total_qty = product[2] if product else 0
            has_numbered = (numbered_qty or 0) > 0
            has_unnumbered = (total_qty or 0) - (numbered_qty or 0) > 1e-9
            package_modes[product_id] = (
                "mixed" if has_numbered and has_unnumbered else
                "numbered" if has_numbered else
                "unnumbered" if has_unnumbered else
                "empty"
            )
    conn.close()

    return render_template("index.html", products=products, warehouse=name, package_modes=package_modes)


@app.route('/packages/<int:product_id>')
@login_required
def packages_for_product(product_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT qty FROM products WHERE id=%s", (product_id,))
    product = cur.fetchone()
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
    numbered_qty = sum((row[1] or 0) for row in packages if row[2] == 'active')
    unnumbered_qty = max((product[0] if product else 0) - numbered_qty, 0)
    conn.close()
    return render_template("packages.html", packages=packages, unnumbered_qty=unnumbered_qty)


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
    has_package_values = request.form.getlist("has_package_number")
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
        has_package_number = form_value(has_package_values, index) == "1"
        if not issuing and len(package_value) > 100:
            raise ValueError("Numer paczki może mieć maksymalnie 100 znaków.")
        if not issuing and has_package_number and not package_value:
            raise ValueError("Zaznaczono, że towar posiada numer paczki — wpisz numer paczki.")
        if not issuing and not has_package_number:
            package_value = ""
        items.append({
            "product_id": product_id,
            "warehouse": warehouse,
            "qty": qty,
            "package": package_value,
            "has_package_number": has_package_number,
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
                    SELECT COALESCE(SUM(qty),0) FROM packages
                    WHERE product_id=%s AND warehouse=%s AND status='active' AND qty>0
                    """,
                    (item["product_id"], item["warehouse"]),
                )
                numbered_qty = cur.fetchone()[0] or 0
                cur.execute("SELECT qty FROM products WHERE id=%s AND warehouse=%s", (item["product_id"], item["warehouse"]))
                total_qty = (cur.fetchone() or (0,))[0] or 0
                if total_qty - numbered_qty + 1e-9 < item["qty"]:
                    raise ValueError("Brak wystarczającej ilości bez numeru paczki. Wybierz paczkę albo zmniejsz ilość.")

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
        save_issue_photos(cur, doc_id, request.files.getlist("photos"), request.form.get("photo_note"))
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
    issue_document = is_issue_document(doc[7] if len(doc) > 7 else None, doc[5])
    photos = []
    history = []
    if issue_document:
        cur.execute(
            """
            SELECT id, filename, content_type, note, added_by, created_at
            FROM issue_doc_photos
            WHERE doc_id=%s
            ORDER BY created_at DESC, id DESC
            """,
            (id,),
        )
        photos = cur.fetchall()
        cur.execute(
            """
            SELECT user_email, action, details, created_at
            FROM issue_doc_history
            WHERE doc_id=%s
            ORDER BY created_at DESC, id DESC
            """,
            (id,),
        )
        history = cur.fetchall()

    conn.close()

    return render_template(
        "doc_detail.html",
        doc=doc,
        items=items,
        issue_document=issue_document,
        photos=photos,
        history=history,
    )



@app.route('/doc/<int:id>/photos', methods=['POST'])
@login_required
def add_issue_doc_photos(id):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT movement_type, doc_number FROM issue_docs WHERE id=%s", (id,))
        doc = cur.fetchone()
        if not doc:
            return "Nie znaleziono dokumentu.", 404
        if not is_issue_document(doc[0], doc[1]):
            return "Zdjęcia można dodawać tylko do dokumentów WZ/RW.", 400
        saved = save_issue_photos(cur, id, request.files.getlist("photos"), request.form.get("photo_note"))
        if not saved:
            return "Dodaj co najmniej jedno zdjęcie.", 400
        conn.commit()
        cache.clear()
        return redirect(f"/doc/{id}")
    except ValueError as exc:
        conn.rollback()
        return str(exc), 400
    except Exception:
        conn.rollback()
        logger.exception("Issue document photo upload failed.")
        return "Nie udało się zapisać zdjęć.", 500
    finally:
        conn.close()


@app.route('/doc-photo/<int:photo_id>')
@login_required
def view_issue_doc_photo(photo_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT filename, content_type, data FROM issue_doc_photos WHERE id=%s", (photo_id,))
    photo = cur.fetchone()
    conn.close()
    if not photo:
        return "Nie znaleziono zdjęcia.", 404
    return Response(bytes(photo[2]), mimetype=photo[1], headers={"Content-Disposition": f"inline; filename=\"{photo[0]}\""})


@app.route('/doc-photo/<int:photo_id>/delete', methods=['POST'])
@admin_required
def delete_issue_doc_photo(photo_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT doc_id, filename FROM issue_doc_photos WHERE id=%s FOR UPDATE", (photo_id,))
    photo = cur.fetchone()
    if not photo:
        conn.close()
        return "Nie znaleziono zdjęcia.", 404
    cur.execute("DELETE FROM issue_doc_photos WHERE id=%s", (photo_id,))
    issue_doc_history(cur, photo[0], "usunięto zdjęcie", photo[1])
    conn.commit()
    cache.clear()
    conn.close()
    return redirect(f"/doc/{photo[0]}")

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


def backup_bucket():
    bucket_name = FIREBASE_CONFIG.get("storageBucket")
    if not bucket_name:
        raise RuntimeError("Brak FIREBASE_STORAGE_BUCKET.")
    return storage.bucket(bucket_name)


def perform_database_backup(created_by):
    guard_conn = db()
    guard_cur = guard_conn.cursor()
    guard_cur.execute("SELECT pg_try_advisory_lock(67431031)")
    if not guard_cur.fetchone()[0]:
        guard_conn.close()
        raise RuntimeError("Inna kopia zapasowa jest już wykonywana.")
    try:
        return _perform_database_backup(created_by)
    finally:
        try:
            guard_cur.execute("SELECT pg_advisory_unlock(67431031)")
            guard_conn.commit()
        finally:
            guard_conn.close()


def _perform_database_backup(created_by):
    ensure_db_initialized()
    log_conn = db()
    log_cur = log_conn.cursor()
    log_cur.execute(
        """
        INSERT INTO backup_runs(created_by, status)
        VALUES (%s, 'running') RETURNING id
        """,
        (created_by,),
    )
    run_id = log_cur.fetchone()[0]
    log_conn.commit()
    log_conn.close()
    try:
        export_conn = db()
        try:
            compressed, checksum, _ = export_database(export_conn)
        finally:
            export_conn.close()
        encrypted = encrypt_backup(
            compressed,
            os.environ.get("BACKUP_ENCRYPTION_KEY", ""),
        )
        timestamp = datetime.utcnow().strftime("%Y/%m/%Y-%m-%dT%H-%M-%SZ")
        object_name = f"database-backups/{timestamp}-{run_id}.json.gz.enc"
        blob = backup_bucket().blob(object_name)
        blob.metadata = {
            "sha256": checksum,
            "format": "magazyn-app-backup-v1",
            "created-by": created_by[:100],
        }
        blob.upload_from_string(
            encrypted,
            content_type="application/octet-stream",
        )
        update_conn = db()
        update_cur = update_conn.cursor()
        update_cur.execute(
            """
            UPDATE backup_runs
            SET status='completed', object_name=%s, size_bytes=%s, checksum=%s
            WHERE id=%s
            """,
            (object_name, len(encrypted), checksum, run_id),
        )
        update_conn.commit()
        update_conn.close()
        return {
            "id": run_id,
            "object_name": object_name,
            "size_bytes": len(encrypted),
            "checksum": checksum,
        }
    except Exception as exc:
        logger.exception("Database backup failed.")
        try:
            error_conn = db()
            error_cur = error_conn.cursor()
            error_cur.execute(
                "UPDATE backup_runs SET status='failed', error=%s WHERE id=%s",
                (str(exc)[:1000], run_id),
            )
            error_conn.commit()
            error_conn.close()
        except Exception:
            logger.exception("Backup failure logging failed.")
        raise


def valid_backup_object_name(object_name):
    return (
        object_name.startswith("database-backups/")
        and object_name.endswith(".json.gz.enc")
        and ".." not in object_name
        and "\\" not in object_name
    )


@app.route('/admin/backups')
@admin_required
def backups_page():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, created_at, created_by, status, object_name, size_bytes, checksum, error
        FROM backup_runs ORDER BY id DESC LIMIT 100
        """
    )
    runs = cur.fetchall()
    conn.close()
    return render_template(
        "backups.html",
        runs=runs,
        restore_enabled=os.environ.get("ALLOW_BACKUP_RESTORE", "").lower() == "true",
    )


@app.route('/admin/backups/create', methods=['POST'])
@admin_required
@limiter.limit("2 per hour")
def create_manual_backup():
    try:
        perform_database_backup(session.get("user") or "administrator")
    except Exception:
        return "Nie udało się utworzyć kopii zapasowej.", 500
    return redirect("/admin/backups?created=1")


@app.route('/admin/backups/download')
@admin_required
@limiter.limit("20 per hour")
def download_backup():
    object_name = (request.args.get("name") or "").strip()
    if not valid_backup_object_name(object_name):
        return "Nieprawidłowa nazwa kopii.", 400
    try:
        encrypted = backup_bucket().blob(object_name).download_as_bytes()
    except Exception:
        logger.exception("Backup download failed.")
        return "Nie udało się pobrać kopii.", 502
    filename = object_name.rsplit("/", 1)[-1]
    return Response(
        encrypted,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route('/admin/backups/restore', methods=['POST'])
@admin_required
@limiter.limit("1 per hour")
def restore_backup():
    if os.environ.get("ALLOW_BACKUP_RESTORE", "").lower() != "true":
        return "Przywracanie jest wyłączone. Włącz je czasowo w Renderze.", 403
    if request.form.get("confirmation") != "PRZYWRÓĆ":
        return "Wpisz PRZYWRÓĆ, aby potwierdzić.", 400
    object_name = (request.form.get("object_name") or "").strip()
    if not valid_backup_object_name(object_name):
        return "Nieprawidłowa nazwa kopii.", 400
    try:
        encrypted = backup_bucket().blob(object_name).download_as_bytes()
        compressed = decrypt_backup(
            encrypted,
            os.environ.get("BACKUP_ENCRYPTION_KEY", ""),
        )
        payload = parse_backup(compressed)
        restore_conn = db()
        try:
            restore_database(restore_conn, payload)
        except Exception:
            restore_conn.rollback()
            raise
        finally:
            restore_conn.close()
        cache.clear()
    except Exception:
        logger.exception("Database restore failed.")
        return "Nie udało się przywrócić kopii. Baza nie została zmieniona.", 500
    return redirect("/admin/backups?restored=1")


# 📊 HISTORIA
@app.route('/ksiegowosc')
@login_required
@accounting_required
def accounting_dashboard():
    filters = {
        "payment_status": (request.args.get("payment_status") or "").strip(),
        "payment_method": (request.args.get("payment_method") or "").strip(),
        "proforma_status": (request.args.get("proforma_status") or "").strip(),
        "document_status": (request.args.get("document_status") or "").strip(),
        "salesperson": (request.args.get("salesperson") or "").strip(),
        "client": (request.args.get("client") or "").strip(),
        "proforma_number": (request.args.get("proforma_number") or "").strip(),
        "invoice_number": (request.args.get("invoice_number") or "").strip(),
        "receipt_number": (request.args.get("receipt_number") or "").strip(),
        "date": (request.args.get("date") or "").strip(),
    }
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM shop_orders")
    for (order_id,) in cur.fetchall():
        ensure_shop_accounting_row(cur, order_id)
    conn.commit()

    where = []
    params = []
    if filters["payment_status"]:
        where.append("o.payment_status=%s"); params.append(filters["payment_status"])
    if filters["payment_method"]:
        where.append("a.payment_method=%s"); params.append(filters["payment_method"])
    if filters["proforma_status"] == "issued":
        where.append("a.proforma_issued=TRUE")
    elif filters["proforma_status"] == "not_issued":
        where.append("a.proforma_issued=FALSE")
    if filters["document_status"] == "invoice":
        where.append("a.invoice_issued=TRUE")
    elif filters["document_status"] == "receipt":
        where.append("a.receipt_issued=TRUE")
    elif filters["document_status"] == "none":
        where.append("a.invoice_issued=FALSE AND a.receipt_issued=FALSE")
    for key, column in [("salesperson", "a.salesperson"), ("client", "o.customer_name"), ("proforma_number", "a.proforma_number"), ("invoice_number", "a.invoice_number"), ("receipt_number", "a.receipt_number")]:
        if filters[key]:
            where.append(f"lower(COALESCE({column},'')) LIKE %s"); params.append(f"%{filters[key].lower()}%")
    if filters["date"]:
        where.append("o.order_date=%s"); params.append(filters["date"])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base = f"""
        FROM shop_orders o
        JOIN shop_accounting a ON a.order_id=o.id
        {where_sql}
    """
    cur.execute(f"""
        SELECT o.id,o.order_number,o.order_date,o.customer_name,o.payment_status,o.status,
               a.payment_method,a.proforma_number,a.invoice_number,a.receipt_number,
               a.salesperson,a.amount_due,a.amount_paid,a.invoice_issued,a.receipt_issued,
               a.ready_to_ship,a.settled,a.paid,a.partial_payment,a.waiting_for_payment
        {base}
        ORDER BY o.order_date DESC,o.id DESC
    """, tuple(params))
    orders = cur.fetchall()
    cur.execute(f"SELECT COALESCE(SUM(a.amount_due),0), COALESCE(SUM(a.amount_paid),0), COALESCE(SUM(a.amount_due-a.amount_paid),0), COUNT(*), COUNT(*) FILTER (WHERE a.paid), COUNT(*) FILTER (WHERE NOT a.paid), COUNT(*) FILTER (WHERE a.invoice_issued), COUNT(*) FILTER (WHERE NOT a.invoice_issued) {base}", tuple(params))
    totals = cur.fetchone()
    cur.execute(f"SELECT COALESCE(NULLIF(a.salesperson,''),'Nieprzypisany'), COUNT(*), COALESCE(SUM(a.amount_due),0), COALESCE(SUM(a.amount_paid),0) {base} GROUP BY COALESCE(NULLIF(a.salesperson,''),'Nieprzypisany') ORDER BY 1", tuple(params))
    by_salesperson = cur.fetchall()
    cur.execute(f"SELECT COALESCE(NULLIF(a.payment_method,''),'Brak'), COUNT(*), COALESCE(SUM(a.amount_due),0), COALESCE(SUM(a.amount_paid),0) {base} GROUP BY COALESCE(NULLIF(a.payment_method,''),'Brak') ORDER BY 1", tuple(params))
    by_payment = cur.fetchall()
    cur.execute(f"SELECT o.payment_status, COUNT(*), COALESCE(SUM(a.amount_due),0), COALESCE(SUM(a.amount_paid),0) {base} GROUP BY o.payment_status ORDER BY 1", tuple(params))
    by_status = cur.fetchall()
    lists = {
        "waiting": [o for o in orders if o[19] or o[4] == "Oczekuje na płatność"],
        "paid_no_invoice": [o for o in orders if o[17] and not o[13]],
        "invoiced": [o for o in orders if o[13]],
        "ready_warehouse": [o for o in orders if o[15]],
        "unsettled": [o for o in orders if not o[16]],
        "finished": [o for o in orders if o[16] or o[5] in {"Zakończone", "Dostarczone"}],
    }
    conn.close()
    return render_template(
        "accounting.html",
        orders=orders,
        totals=totals,
        by_salesperson=by_salesperson,
        by_payment=by_payment,
        by_status=by_status,
        lists=lists,
        payment_methods=ACCOUNTING_PAYMENT_METHODS,
        filters=filters,
    )


@app.route('/ksiegowosc/orders/<int:order_id>', methods=['POST'])
@login_required
@accounting_required
def update_order_accounting(order_id):
    conn = db(); cur = conn.cursor()
    try:
        ensure_shop_accounting_row(cur, order_id)
        cur.execute("SELECT * FROM shop_accounting WHERE order_id=%s FOR UPDATE", (order_id,))
        before = cur.fetchone()
        if not before:
            return "Nie znaleziono zamówienia.", 404
        bool_values = {field: request.form.get(field) == "on" for field in ACCOUNTING_BOOL_FIELDS}
        payment_method = (request.form.get("payment_method") or "").strip()
        if payment_method and payment_method not in ACCOUNTING_PAYMENT_METHODS:
            raise ValueError("Nieprawidłowy sposób płatności.")
        amount_paid = parse_nonnegative_number(request.form.get("amount_paid"), "Kwota zapłacona")
        amount_due = parse_nonnegative_number(request.form.get("amount_due"), "Kwota pozostała do zapłaty")
        salesperson = (request.form.get("salesperson") or "").strip()[:200]
        cur.execute(
            """
            UPDATE shop_accounting SET
                proforma_issued=%s, reserved_by_proforma=%s, waiting_for_payment=%s,
                partial_payment=%s, paid=%s, payment_method=%s, invoice_issued=%s,
                receipt_issued=%s, invoice_sent=%s, document_to_warehouse=%s,
                ready_to_ship=%s, settled=%s, proforma_number=%s, invoice_number=%s,
                receipt_number=%s, document_issue_date=NULLIF(%s,'')::date,
                payment_received_date=NULLIF(%s,'')::date, amount_paid=%s,
                amount_due=%s, accounting_notes=%s, salesperson=%s,
                updated_by=%s, updated_at=NOW()
            WHERE order_id=%s
            """,
            (
                bool_values["proforma_issued"], bool_values["reserved_by_proforma"],
                bool_values["waiting_for_payment"], bool_values["partial_payment"],
                bool_values["paid"], payment_method or None, bool_values["invoice_issued"],
                bool_values["receipt_issued"], bool_values["invoice_sent"],
                bool_values["document_to_warehouse"], bool_values["ready_to_ship"],
                bool_values["settled"], request.form.get("proforma_number", "").strip(),
                request.form.get("invoice_number", "").strip(), request.form.get("receipt_number", "").strip(),
                request.form.get("document_issue_date", ""), request.form.get("payment_received_date", ""),
                amount_paid, amount_due, request.form.get("accounting_notes", "").strip(),
                salesperson, session.get("user"), order_id,
            ),
        )
        sync_accounting_payment_status(cur, order_id)
        if request.form.get("invoice_number"):
            cur.execute("UPDATE shop_orders SET sales_document_number=%s WHERE id=%s", (request.form.get("invoice_number").strip(), order_id))
        labels = []
        before_bool = dict(zip(ACCOUNTING_BOOL_FIELDS, [before[1], before[2], before[3], before[4], before[5], before[7], before[8], before[9], before[10], before[11], before[12]]))
        for field in ACCOUNTING_BOOL_FIELDS:
            if bool(before_bool[field]) != bool_values[field]:
                labels.append(f"{ACCOUNTING_FIELD_LABELS[field]}: {'tak' if bool_values[field] else 'nie'}")
        if (before[21] or "") != salesperson:
            labels.append(f"Handlowiec: {before[21] or 'brak'} → {salesperson or 'brak'}")
        if labels:
            shop_history(cur, order_id, "zmieniono księgowość", "; ".join(labels))
        conn.commit(); cache.clear(); return redirect(f"/sklep/orders/{order_id}")
    except ValueError as exc:
        conn.rollback(); return str(exc), 400
    except Exception:
        conn.rollback(); logger.exception("Accounting update failed."); return "Nie udało się zapisać księgowości.", 500
    finally:
        conn.close()

@app.route('/sklep')
@login_required
def shop_orders():
    denied = require_shop_permission("view")
    if denied:
        return denied
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    conn = db(); cur = conn.cursor()
    params = []
    where = []
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(o.order_number) LIKE %s OR lower(o.customer_name) LIKE %s OR lower(COALESCE(o.tracking_number,'')) LIKE %s OR lower(COALESCE(o.sales_document_number,'')) LIKE %s OR EXISTS (SELECT 1 FROM shop_order_items i WHERE i.order_id=o.id AND lower(i.product_name) LIKE %s))")
        params += [like]*5
    if status:
        where.append("o.status=%s"); params.append(status)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    cur.execute(f"SELECT o.id,o.order_number,o.order_date,o.customer_name,o.status,o.payment_status,o.sales_document_number,o.tracking_number,COALESCE(SUM(i.qty*i.price_brutto),0)+o.shipping_cost AS total FROM shop_orders o LEFT JOIN shop_order_items i ON i.order_id=o.id{sql_where} GROUP BY o.id ORDER BY o.order_date DESC,o.id DESC", tuple(params))
    orders = cur.fetchall()
    cur.execute("SELECT type,message,created_at FROM shop_notifications WHERE resolved=FALSE ORDER BY created_at DESC LIMIT 8")
    notifications = cur.fetchall()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(i.qty*i.price_brutto),0), COUNT(*) FILTER (WHERE o.status NOT IN ('Zakończone','Anulowane')), COUNT(*) FILTER (WHERE o.status='Zakończone'), COUNT(*) FILTER (WHERE o.status='Anulowane') FROM shop_orders o LEFT JOIN shop_order_items i ON i.order_id=o.id")
    reports = cur.fetchone()
    cur.execute("SELECT id,name,qty,unit,warehouse,price_netto,vat FROM products ORDER BY warehouse,lower(name),id")
    products = cur.fetchall()
    conn.close()
    return render_template("shop.html", orders=orders, notifications=notifications, reports=reports, products=products, statuses=SHOP_STATUS_FLOW, role_labels=SHOP_ROLE_LABELS)


@app.route('/sklep/orders', methods=['POST'])
@login_required
def shop_create_order():
    denied = require_shop_permission("shop_edit")
    if denied: return denied
    product_ids = request.form.getlist("product_id"); qtys = request.form.getlist("qty")
    if not any(product_ids): return "Dodaj co najmniej jeden produkt.", 400
    conn = db(); cur = conn.cursor()
    try:
        order_number = (request.form.get("order_number") or f"SK/{datetime.now().strftime('%Y%m%d%H%M%S')}").strip()
        order_date = normalized_document_date(request.form.get("date"))
        shipping = parse_nonnegative_number(request.form.get("shipping_cost"), "Koszt wysyłki")
        cur.execute("INSERT INTO shop_orders(order_number,order_date,customer_name,delivery_address,phone,email,shipping_cost,payment_method,payment_status,status,sales_document_number,tracking_number,notes,nip,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Nowe zamówienie',%s,%s,%s,%s,%s) RETURNING id", (order_number,order_date,request.form.get("customer_name",""),request.form.get("delivery_address",""),request.form.get("phone",""),request.form.get("email",""),shipping,request.form.get("payment_method",""),request.form.get("payment_status","Oczekuje na płatność"),request.form.get("sales_document_number",""),request.form.get("tracking_number",""),request.form.get("notes",""),request.form.get("nip",""),session.get("user")))
        order_id = cur.fetchone()[0]
        shop_history(cur, order_id, "utworzono zamówienie", order_number)
        lacking = []
        for pid_raw, qty_raw in zip(product_ids, qtys):
            if not pid_raw: continue
            pid = int(pid_raw); qty = parse_positive_number(qty_raw)
            cur.execute("SELECT id,name,qty,warehouse,price_netto,vat FROM products WHERE id=%s FOR UPDATE", (pid,))
            p = cur.fetchone()
            if not p: raise ValueError("Wybrany produkt nie istnieje.")
            available = p[2]
            if available + 1e-9 < qty: lacking.append(f"{p[1]} ({available})")
            reserved = qty if available + 1e-9 >= qty else 0
            cur.execute("INSERT INTO shop_order_items(order_id,product_id,product_name,qty,price_netto,price_brutto,vat,warehouse,reserved_qty) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (order_id,pid,p[1],qty,p[4] or 0,(p[4] or 0)*(1+(p[5] or 0)/100),p[5] or 0,p[3],reserved))
            if reserved:
                shop_history(cur, order_id, "zarezerwowano towar", f"{p[1]} x {qty}")
        if lacking:
            cur.execute("INSERT INTO shop_notifications(order_id,type,message) VALUES (%s,'brak towaru',%s)", (order_id, "Brak towaru: "+", ".join(lacking)))
        else:
            cur.execute("UPDATE shop_orders SET status='Towar zarezerwowany' WHERE id=%s", (order_id,))
            cur.execute("INSERT INTO shop_notifications(order_id,type,message) VALUES (%s,'oczekuje na dokument','Zamówienie oczekuje na dokument sprzedaży')", (order_id,))
        cur.execute("SELECT id,order_number,order_date,customer_name,delivery_address,phone,email,shipping_cost,payment_method,payment_status,status,sales_document_number,tracking_number,notes,nip FROM shop_orders WHERE id=%s", (order_id,))
        order = cur.fetchone(); cur.execute("SELECT id,product_id,product_name,qty,price_netto,price_brutto,vat FROM shop_order_items WHERE order_id=%s", (order_id,)); items=cur.fetchall()
        payload = create_shop_document_payload(order, items)
        cur.execute("INSERT INTO shop_sales_documents(order_id,document_number,editable_data,docx,pdf,created_by) VALUES (%s,%s,%s,%s,%s,%s)", (order_id,payload['document_number'],json.dumps(payload),psycopg2.Binary(simple_docx_bytes(payload)),psycopg2.Binary(shop_pdf_bytes(payload)),session.get('user')))
        ensure_shop_accounting_row(cur, order_id)
        shop_history(cur, order_id, "wygenerowano dokument", payload['document_number'])
        conn.commit(); cache.clear(); return redirect(f"/sklep/orders/{order_id}")
    except ValueError as exc:
        conn.rollback(); return str(exc), 400
    except Exception:
        conn.rollback(); logger.exception("Shop order creation failed"); return "Nie udało się utworzyć zamówienia.", 500
    finally:
        conn.close()


@app.route('/sklep/orders/<int:order_id>')
@login_required
def shop_order_detail(order_id):
    denied = require_shop_permission("view")
    if denied: return denied
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT * FROM shop_orders WHERE id=%s", (order_id,)); order=cur.fetchone()
    cur.execute("SELECT * FROM shop_order_items WHERE order_id=%s ORDER BY id", (order_id,)); items=cur.fetchall()
    cur.execute("SELECT id,document_number,editable_data,confirmed FROM shop_sales_documents WHERE order_id=%s", (order_id,)); document=cur.fetchone()
    ensure_shop_accounting_row(cur, order_id)
    conn.commit()
    cur.execute("SELECT * FROM shop_accounting WHERE order_id=%s", (order_id,)); accounting=cur.fetchone()
    cur.execute("SELECT user_email,action,details,created_at FROM shop_order_history WHERE order_id=%s ORDER BY created_at DESC", (order_id,)); history=cur.fetchall(); conn.close()
    if not order: return "Nie znaleziono zamówienia.", 404
    return render_template("shop_order.html", order=order, items=items, document=document, history=history, statuses=SHOP_STATUS_FLOW, accounting=accounting, payment_methods=ACCOUNTING_PAYMENT_METHODS, can_ship=order_can_be_shipped(accounting))


@app.route('/sklep/orders/<int:order_id>/status', methods=['POST'])
@login_required
def shop_update_status(order_id):
    status = request.form.get("status")
    if status not in SHOP_STATUS_FLOW: return "Nieprawidłowy status.", 400
    action = "warehouse" if status in {"W trakcie pakowania","Spakowane","Wysłane","Dostarczone","Zakończone"} else "shop_edit"
    denied=require_shop_permission(action)
    if denied: return denied
    conn=db(); cur=conn.cursor()
    try:
        if status == "Wysłane":
            ensure_shop_accounting_row(cur, order_id)
            cur.execute("SELECT * FROM shop_accounting WHERE order_id=%s FOR UPDATE", (order_id,))
            accounting = cur.fetchone()
            if not order_can_be_shipped(accounting):
                raise ValueError("Księgowość nie odblokowała wydania: wymagane opłacenie zamówienia albo pobranie oraz status gotowe do wysyłki.")
            cur.execute("SELECT product_id,warehouse,qty,issued_qty FROM shop_order_items WHERE order_id=%s FOR UPDATE", (order_id,))
            for pid, wh, qty, issued in cur.fetchall():
                delta = qty - (issued or 0)
                if delta > 0:
                    cur.execute("UPDATE products SET qty=qty-%s WHERE id=%s AND warehouse=%s AND qty >= %s", (delta,pid,wh,delta))
                    if cur.rowcount != 1: raise ValueError("Brak stanu magazynowego lub próba podwójnej sprzedaży.")
                    cur.execute("UPDATE shop_order_items SET issued_qty=qty WHERE order_id=%s AND product_id=%s", (order_id,pid))
            shop_history(cur, order_id, "wysłano zamówienie", "Towar zdjęty ze stanu")
        cur.execute("UPDATE shop_orders SET status=%s,tracking_number=COALESCE(NULLIF(%s,''),tracking_number),updated_at=NOW() WHERE id=%s", (status,request.form.get('tracking_number',''),order_id))
        shop_history(cur, order_id, "zmieniono status", status)
        if status == "Dokument wystawiony": cur.execute("INSERT INTO shop_notifications(order_id,type,message) VALUES (%s,'gotowe do pakowania','Zamówienie gotowe do pakowania')", (order_id,))
        if status == "Spakowane": cur.execute("INSERT INTO shop_notifications(order_id,type,message) VALUES (%s,'gotowe do wysyłki','Zamówienie gotowe do wysyłki')", (order_id,))
        conn.commit(); cache.clear(); return redirect(f"/sklep/orders/{order_id}")
    except ValueError as exc:
        conn.rollback(); return str(exc), 400
    finally: conn.close()


@app.route('/sklep/orders/<int:order_id>/document', methods=['POST'])
@login_required
def shop_confirm_document(order_id):
    denied=require_shop_permission("accounting")
    if denied: return denied
    number=(request.form.get('sales_document_number') or '').strip()
    if not number: return "Podaj numer faktury lub paragonu.", 400
    conn=db(); cur=conn.cursor()
    cur.execute("UPDATE shop_orders SET sales_document_number=%s,status='Dokument wystawiony',document_confirmed=TRUE WHERE id=%s", (number,order_id))
    cur.execute("UPDATE shop_sales_documents SET document_number=%s,confirmed=TRUE,confirmed_by=%s,confirmed_at=NOW() WHERE order_id=%s", (number,session.get('user'),order_id))
    shop_history(cur, order_id, "wystawiono dokument", number)
    conn.commit(); conn.close(); return redirect(f"/sklep/orders/{order_id}")


@app.route('/sklep/documents/<int:doc_id>/<fmt>')
@login_required
def shop_download_document(doc_id, fmt):
    if fmt not in {"pdf","docx"}: return "Nieprawidłowy format.", 400
    conn=db(); cur=conn.cursor(); cur.execute(f"SELECT document_number,{fmt} FROM shop_sales_documents WHERE id=%s", (doc_id,)); row=cur.fetchone(); conn.close()
    if not row: return "Nie znaleziono dokumentu.", 404
    mimetype = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(bytes(row[1]), mimetype=mimetype, headers={"Content-Disposition": f"attachment; filename={row[0]}.{fmt}"})

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
