from app import perform_database_backup


if __name__ == "__main__":
    result = perform_database_backup("render-cron")
    print(
        f"Backup completed: id={result['id']}, "
        f"size={result['size_bytes']}, sha256={result['checksum']}"
    )
