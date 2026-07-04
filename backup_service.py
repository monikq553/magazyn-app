import base64
import gzip
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from psycopg2 import sql


BACKUP_TABLES = (
    "schema_migrations",
    "users",
    "products",
    "packages",
    "issue_docs",
    "issue_items",
    "costs",
    "system_settings",
    "backup_runs",
)
BACKUP_FORMAT = "magazyn-app-backup-v1"


def json_value(value):
    if isinstance(value, (datetime, date)):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    return value


def object_hook(value):
    value_type = value.get("__type__")
    if value_type == "datetime":
        return value["value"]
    if value_type == "decimal":
        return value["value"]
    if value_type == "bytes":
        return base64.b64decode(value["value"])
    return value


def export_database(connection):
    cursor = connection.cursor()
    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    cursor.execute("SELECT current_database()")
    database_name = cursor.fetchone()[0]
    tables = {}
    for table_name in BACKUP_TABLES:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        columns = [row[0] for row in cursor.fetchall()]
        if not columns:
            continue
        order_clause = sql.SQL(" ORDER BY id") if "id" in columns else sql.SQL("")
        cursor.execute(
            sql.SQL("SELECT * FROM {}").format(sql.Identifier(table_name))
            + order_clause
        )
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        tables[table_name] = {"columns": columns, "rows": rows}
    connection.rollback()
    payload = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "database": database_name,
        "tables": tables,
    }
    raw_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=json_value,
    ).encode("utf-8")
    compressed = gzip.compress(raw_json, compresslevel=9)
    return compressed, hashlib.sha256(compressed).hexdigest(), payload


def encrypt_backup(compressed_backup, encryption_key):
    if not encryption_key:
        raise ValueError("Brak BACKUP_ENCRYPTION_KEY.")
    return Fernet(encryption_key.encode("ascii")).encrypt(compressed_backup)


def decrypt_backup(encrypted_backup, encryption_key):
    if not encryption_key:
        raise ValueError("Brak BACKUP_ENCRYPTION_KEY.")
    try:
        return Fernet(encryption_key.encode("ascii")).decrypt(encrypted_backup)
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Nie można odszyfrować kopii zapasowej.") from exc


def parse_backup(compressed_backup):
    try:
        raw_json = gzip.decompress(compressed_backup)
        payload = json.loads(raw_json.decode("utf-8"), object_hook=object_hook)
    except Exception as exc:
        raise ValueError("Plik kopii zapasowej jest uszkodzony.") from exc
    if payload.get("format") != BACKUP_FORMAT:
        raise ValueError("Nieobsługiwany format kopii zapasowej.")
    if not isinstance(payload.get("tables"), dict):
        raise ValueError("Kopia nie zawiera tabel.")
    missing = {"users", "products", "packages", "issue_docs", "issue_items"} - set(
        payload["tables"]
    )
    if missing:
        raise ValueError("Kopia jest niekompletna: " + ", ".join(sorted(missing)))
    return payload


def restore_database(connection, payload):
    cursor = connection.cursor()
    cursor.execute("SELECT pg_advisory_xact_lock(67431030)")
    present_tables = [
        table_name for table_name in BACKUP_TABLES if table_name in payload["tables"]
    ]
    cursor.execute(
        sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in present_tables)
        )
    )
    insert_order = (
        "schema_migrations",
        "users",
        "products",
        "issue_docs",
        "costs",
        "system_settings",
        "packages",
        "issue_items",
        "backup_runs",
    )
    for table_name in insert_order:
        table = payload["tables"].get(table_name)
        if not table:
            continue
        columns = table["columns"]
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table_name),
            sql.SQL(", ").join(sql.Identifier(name) for name in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        for row in table["rows"]:
            cursor.execute(query, [row.get(column) for column in columns])
        if "id" in columns:
            cursor.execute(
                sql.SQL(
                    """
                    SELECT setval(
                        pg_get_serial_sequence(%s, 'id'),
                        GREATEST(COALESCE(MAX(id), 0), 1),
                        COALESCE(MAX(id), 0) > 0
                    ) FROM {}
                    """
                ).format(sql.Identifier(table_name)),
                (table_name,),
            )
    connection.commit()
