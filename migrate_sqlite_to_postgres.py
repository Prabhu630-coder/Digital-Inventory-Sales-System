import sqlite3
from app import init_db, get_db, USE_POSTGRES, DB_PATH

if not USE_POSTGRES:
    raise SystemExit("Set DATABASE_URL first.")

# Open SQLite source database
src = sqlite3.connect(DB_PATH)
src.row_factory = sqlite3.Row

# Create PostgreSQL tables
init_db()

# Open PostgreSQL destination
dst = get_db()

# Tables to migrate
tables = [
    "settings",
    "users",
    "products",
    "customers",
    "sales",
    "sale_items"
]

# Migrate settings first
rows = src.execute("SELECT * FROM settings").fetchall()

if rows:
    dst.execute("DELETE FROM settings")

    for row in rows:
        columns = list(row.keys())
        names = ",".join(columns)
        placeholders = ",".join(["?"] * len(columns))

        dst.execute(
            f"INSERT INTO settings ({names}) VALUES ({placeholders})",
            tuple(row)
        )

# Tables with auto-generated IDs
id_tables = [
    "users",
    "products",
    "customers",
    "sales",
    "sale_items"
]

for table in id_tables:

    rows = src.execute(f"SELECT * FROM {table}").fetchall()

    if not rows:
        continue

    # Clear destination
    dst.execute(f"DELETE FROM {table}")

    for row in rows:
        columns = list(row.keys())

        # Remove SQLite's ID.
        # PostgreSQL will generate a new ID.
        data_columns = [col for col in columns if col != "id"]

        names = ",".join(data_columns)
        placeholders = ",".join(["?"] * len(data_columns))

        values = tuple(row[col] for col in data_columns)

        dst.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            values
        )

dst.commit()

# Reset PostgreSQL sequences so future INSERTs continue correctly
for table in id_tables:
    try:
        dst.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                true
            )
            """
        )
    except Exception:
        pass

dst.commit()

dst.close()
src.close()

print("SQLite data copied to PostgreSQL successfully.")