"""
One-time import of an OLD SQLite database (bot_database.db) into PostgreSQL.

    DATABASE_URL=postgresql://...  python migrate_sqlite_to_postgres.py [path/to/bot_database.db]

Safe to run more than once (existing rows are kept, nothing is overwritten).
Saved thumbnails / covers / logos that still exist next to the old database
are uploaded too, so they are restored automatically on every deploy.
"""

import asyncio
import os
import sqlite3
import sys

from database import db

BOOL_COLS = {"cover_enabled", "is_banned", "is_active", "active", "wm_enabled", "title_overlay"}
# table -> (conflict target, columns that hold file paths)
TABLES = {
    "users": ("user_id", ("thumbnail_path", "cover_path")),
    "channels": ("channel_id", ("default_thumbnail_path", "channel_cover_path")),
    "bot_settings": ("key", ()),
    "video_settings": ("user_id", ("wm_image_path",)),
    "chat_threads": ("user_id", ()),
    "files_log": ("id", ()),
    "activity_log": ("id", ()),
}


async def main(path: str) -> None:
    if not os.path.exists(path):
        sys.exit(f"SQLite file not found: {path}")
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    await db.init_db()
    pool = db.pool()
    files_to_copy = []
    for table, (key, path_cols) in TABLES.items():
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            print(f"- {table}: not in the old database, skipped")
            continue
        if not rows:
            print(f"- {table}: empty")
            continue
        cols = rows[0].keys()
        pg_cols = {
            r["column_name"]
            for r in await pool.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
            )
        }
        cols = [c for c in cols if c in pg_cols]
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))}) "
            f"ON CONFLICT ({key}) DO NOTHING"
        )
        data = []
        for r in rows:
            vals = [bool(r[c]) if (c in BOOL_COLS and r[c] is not None) else r[c] for c in cols]
            data.append(vals)
            for pc in path_cols:
                if pc in cols and r[pc]:
                    files_to_copy.append(r[pc])
        async with pool.acquire() as con:
            await con.executemany(sql, data)
        print(f"- {table}: {len(rows)} row(s)")
    # keep the id counters ahead of the imported ids
    for table in ("files_log", "activity_log"):
        await pool.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), 1))"
        )
    copied = 0
    for fp in set(files_to_copy):
        if os.path.exists(fp):
            await db.store_file(fp)
            copied += 1
    print(f"- images uploaded to the database: {copied}")
    await db.close_db()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "bot_database.db"))
