import argparse
import os

from app import backup_bucket, db, ensure_db_initialized
from backup_service import decrypt_backup, parse_backup, restore_database


def main():
    parser = argparse.ArgumentParser(description="Validate or restore an encrypted backup.")
    parser.add_argument("object_name")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirmation")
    args = parser.parse_args()
    if not args.object_name.startswith("database-backups/") or ".." in args.object_name:
        raise SystemExit("Invalid backup object name.")
    encrypted = backup_bucket().blob(args.object_name).download_as_bytes()
    compressed = decrypt_backup(encrypted, os.environ.get("BACKUP_ENCRYPTION_KEY", ""))
    payload = parse_backup(compressed)
    table_count = len(payload["tables"])
    row_count = sum(len(table["rows"]) for table in payload["tables"].values())
    print(f"Backup valid: {table_count} tables, {row_count} rows.")
    if not args.execute:
        print("Dry run only. No database changes were made.")
        return
    if args.confirmation != "PRZYWRÓĆ":
        raise SystemExit("Use --confirmation PRZYWRÓĆ to execute restore.")
    ensure_db_initialized()
    connection = db()
    try:
        restore_database(connection, payload)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print("Restore completed.")


if __name__ == "__main__":
    main()
