#!/usr/bin/env python3
"""
Cleanup script to remove duplicate/orphaned product code entries.

When the same filename (e.g., DMJ-639141) maps to multiple product codes
(e.g., RJ01417184 and RJ639141), this script identifies the entries without
metadata and removes them, keeping the one with metadata.
"""

import asyncio
import aiosqlite
from pathlib import Path

async def cleanup_duplicates():
    db = await aiosqlite.connect('data/library.db')

    try:
        # Find all original codes that map to multiple product codes
        cursor = await db.execute("""
            SELECT original_code, COUNT(*) as count, GROUP_CONCAT(id) as ids
            FROM items
            GROUP BY original_code
            HAVING count > 1
            ORDER BY count DESC
        """)

        rows = await cursor.fetchall()

        if not rows:
            print("No duplicate entries found.")
            return

        print(f"Found {len(rows)} sets of duplicate product codes\n")

        total_deleted = 0

        for orig_code, count, ids_str in rows:
            ids = [int(x) for x in ids_str.split(',')]
            print(f"Processing {orig_code} ({count} entries):")

            # Get details for each ID
            items_info = []
            for item_id in ids:
                cursor2 = await db.execute(
                    "SELECT id, product_code, title, file_count FROM items WHERE id = ?",
                    (item_id,)
                )
                item = await cursor2.fetchone()
                if item:
                    items_info.append(item)

            # Keep the one with metadata (title), delete the others
            with_metadata = [i for i in items_info if i[2]]
            without_metadata = [i for i in items_info if not i[2]]

            if with_metadata and without_metadata:
                print(f"  Keeping: ID {with_metadata[0][0]} ({with_metadata[0][1]}) - HAS METADATA")

                for item in without_metadata:
                    print(f"  Deleting: ID {item[0]} ({item[1]}) - NO METADATA")
                    await db.execute("DELETE FROM items WHERE id = ?", (item[0],))
                    total_deleted += 1
            elif len(items_info) > 1:
                # All have metadata or none have metadata - keep the first, delete the rest
                print(f"  Keeping: ID {items_info[0][0]} ({items_info[0][1]})")

                for item in items_info[1:]:
                    print(f"  Deleting: ID {item[0]} ({item[1]})")
                    await db.execute("DELETE FROM items WHERE id = ?", (item[0],))
                    total_deleted += 1

            print()

        await db.commit()
        print(f"[OK] Deleted {total_deleted} duplicate entries")

    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(cleanup_duplicates())
