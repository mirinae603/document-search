"""
migrate_add_tags.py
-------------------
One-time migration: adds client_tag, project_tag, domain_tag columns
to your existing chunks (and documents) LanceDB tables.

Existing rows get empty string "" for all tag columns — making them
invisible to scoped queries (which require client_tag IS NOT NULL AND != '').

USAGE:
    python migrate_add_tags.py
    python migrate_add_tags.py --lancedb-path ./my_custom_lancedb_path
"""

import os
import sys
import argparse
import lancedb
import pyarrow as pa

def migrate(lancedb_path: str):
    print(f"Connecting to LanceDB at: {lancedb_path}")
    db = lancedb.connect(lancedb_path)
    tables = db.table_names()
    print(f"Found tables: {tables}")

    TAG_FIELDS = ["client_tag", "project_tag", "domain_tag"]

    # ── Migrate chunks table ──────────────────────────────────────────────────
    if "chunks" in tables:
        print("\n[chunks] Starting migration...")
        table = db.open_table("chunks")
        
        # Read all existing data
        existing = table.to_pandas()
        print(f"  Rows found: {len(existing)}")

        # Check which tag columns are already present
        existing_cols = list(existing.columns)
        missing_tags = [f for f in TAG_FIELDS if f not in existing_cols]

        if not missing_tags:
            print("  [chunks] All tag columns already present. Skipping.")
        else:
            print(f"  Adding columns: {missing_tags}")
            for col in missing_tags:
                existing[col] = ""  # Empty string = excluded from scoped queries

            # Drop and recreate table with new schema
            db.drop_table("chunks")
            db.create_table("chunks", data=existing)
            print(f"  [chunks] Migration complete. {len(existing)} rows updated.")
    else:
        print("\n[chunks] Table not found — skipping.")

    # ── Migrate documents table ───────────────────────────────────────────────
    if "documents" in tables:
        print("\n[documents] Starting migration...")
        table = db.open_table("documents")
        existing = table.to_pandas()
        print(f"  Rows found: {len(existing)}")

        existing_cols = list(existing.columns)
        missing_tags = [f for f in TAG_FIELDS if f not in existing_cols]

        if not missing_tags:
            print("  [documents] All tag columns already present. Skipping.")
        else:
            print(f"  Adding columns: {missing_tags}")
            for col in missing_tags:
                existing[col] = ""

            db.drop_table("documents")
            db.create_table("documents", data=existing)
            print(f"  [documents] Migration complete. {len(existing)} rows updated.")
    else:
        print("\n[documents] Table not found — skipping.")

    print("\nMigration done.")
    print("Existing docs have empty tags → excluded from scoped queries (as configured).")
    print("Use POST /scoped/ingest to add new documents with tags.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add tag columns to LanceDB tables")
    parser.add_argument(
        "--lancedb-path",
        default=os.getenv("LANCEDB_PATH", "./lancedb_data"),
        help="Path to your LanceDB directory"
    )
    args = parser.parse_args()
    migrate(args.lancedb_path)