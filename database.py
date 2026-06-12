import aiosqlite
import json
import logging
import re
import shutil
from pathlib import Path
from datetime import datetime
from config import (
    DB_PATH,
    SCAN_PATH,
    COVERS_DIR,
    PIPELINE_EXTRACT_DIR,
    ENABLE_PIPELINE,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    CHUTES_API_KEY,
    CHUTES_MODEL,
    OPENAI_COMPAT_API_KEY,
    OPENAI_COMPAT_BASE_URL,
    OPENAI_COMPAT_MODEL,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)
RUNTIME_GEMINI_API_KEY_SETTING = "runtime_gemini_api_key"
RUNTIME_GEMINI_MODEL_SETTING = "runtime_gemini_model"
RUNTIME_OPENROUTER_API_KEY_SETTING = "runtime_openrouter_api_key"
RUNTIME_OPENROUTER_MODEL_SETTING = "runtime_openrouter_model"
RUNTIME_CHUTES_API_KEY_SETTING = "runtime_chutes_api_key"
RUNTIME_CHUTES_MODEL_SETTING = "runtime_chutes_model"
RUNTIME_OPENAI_COMPAT_API_KEY_SETTING = "runtime_openai_compat_api_key"
RUNTIME_OPENAI_COMPAT_MODEL_SETTING = "runtime_openai_compat_model"
RUNTIME_OPENAI_COMPAT_BASE_URL_SETTING = "runtime_openai_compat_base_url"
RUNTIME_OPENAI_COMPAT_REQUEST_FORMAT_SETTING = "runtime_openai_compat_request_format"
RUNTIME_TRANSLATION_PROVIDER_SETTING = "runtime_translation_provider"
RUNTIME_WHISPER_MODEL_SETTING = "runtime_whisper_model"
RUNTIME_WHISPER_VAD_FILTER_SETTING = "runtime_whisper_vad_filter"
RUNTIME_WHISPER_BEAM_SIZE_SETTING = "runtime_whisper_beam_size"
RUNTIME_WHISPER_CONDITION_ON_PREVIOUS_SETTING = "runtime_whisper_condition_on_previous"
RUNTIME_WHISPER_PREFERRED_VARIANT_SETTING = "runtime_whisper_preferred_variant"

SUPPORTED_WHISPER_MODELS = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v1",
    "large-v2",
    "large-v3",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL,
    original_code TEXT,
    title TEXT,
    title_en TEXT,
    circle TEXT,
    description TEXT,
    description_en TEXT,
    cover_url TEXT,
    cover_local TEXT,
    seiyuu TEXT DEFAULT '[]',
    seiyuu_en TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    tags_en TEXT DEFAULT '[]',
    custom_tags TEXT DEFAULT '[]',
    series TEXT,
    release_date TEXT,
    age_rating TEXT,
    file_format TEXT DEFAULT '[]',
    rating INTEGER DEFAULT 0,
    favorite INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    translation_status TEXT DEFAULT 'not_translated',
    listen_status TEXT NOT NULL DEFAULT 'backlog',
    files TEXT DEFAULT '[]',
    file_count INTEGER DEFAULT 1,
    total_size INTEGER DEFAULT 0,
    confidence TEXT DEFAULT 'low',
    metadata_raw TEXT,
    scan_date TEXT,
    metadata_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_code)
);

CREATE TABLE IF NOT EXISTS unmatched_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    scan_date TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date TEXT NOT NULL,
    total_files INTEGER DEFAULT 0,
    matched INTEGER DEFAULT 0,
    unmatched INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,
    updated_items INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_codes (
    code TEXT PRIMARY KEY,
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER DEFAULT 0,
    completed INTEGER DEFAULT 0,
    current TEXT,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 0,
    processed_files INTEGER DEFAULT 0,
    matched INTEGER DEFAULT 0,
    unmatched INTEGER DEFAULT 0,
    stopped INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,
    stopping INTEGER DEFAULT 0,
    errors_json TEXT DEFAULT '[]',
    error_summary_json TEXT DEFAULT '{}',
    result_json TEXT,
    metadata_json TEXT DEFAULT '{}',
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_type_created ON jobs(job_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_created ON job_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS item_seiyuu (
    item_id INTEGER NOT NULL,
    lang TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (item_id, lang, name),
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_item_seiyuu_lang_name ON item_seiyuu(lang, name);
CREATE INDEX IF NOT EXISTS idx_item_seiyuu_item ON item_seiyuu(item_id);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL,
    lang TEXT NOT NULL,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (item_id, lang, source, name),
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_item_tags_lang_source_name ON item_tags(lang, source, name);
CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);

CREATE TABLE IF NOT EXISTS pipeline_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    archive_path TEXT,
    extract_root TEXT,
    track_path TEXT NOT NULL,
    track_index INTEGER DEFAULT 0,
    title TEXT,
    duration_seconds REAL,
    codec TEXT,
    sample_rate INTEGER,
    channels INTEGER,
    status TEXT DEFAULT 'indexed',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(item_id, track_path),
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_tracks_item ON pipeline_tracks(item_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_tracks_status ON pipeline_tracks(status);

CREATE TABLE IF NOT EXISTS pipeline_transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    language TEXT NOT NULL DEFAULT 'ja',
    text TEXT NOT NULL,
    engine TEXT,
    model TEXT,
    prompt_hash TEXT,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(track_id, segment_index, language, engine, model),
    FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_track_idx ON pipeline_transcript_segments(track_id, segment_index);
CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_language ON pipeline_transcript_segments(language);

CREATE TABLE IF NOT EXISTS pipeline_translation_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_segment_id INTEGER NOT NULL,
    target_language TEXT NOT NULL,
    text TEXT NOT NULL,
    engine TEXT,
    model TEXT,
    prompt_hash TEXT,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(transcript_segment_id, target_language, engine, model),
    FOREIGN KEY(transcript_segment_id) REFERENCES pipeline_transcript_segments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_translation_segment ON pipeline_translation_segments(transcript_segment_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_translation_lang ON pipeline_translation_segments(target_language);

CREATE TABLE IF NOT EXISTS pipeline_transcript_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    language TEXT NOT NULL DEFAULT 'ja',
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'completed',
    engine TEXT,
    model TEXT,
    prompt TEXT,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_runs_track ON pipeline_transcript_runs(track_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_transcript_run_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    confidence REAL,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, segment_index),
    FOREIGN KEY(run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_run_segments_run ON pipeline_transcript_run_segments(run_id, segment_index);

CREATE TABLE IF NOT EXISTS pipeline_translation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    transcript_run_id INTEGER NOT NULL,
    target_language TEXT NOT NULL DEFAULT 'en',
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'completed',
    engine TEXT,
    model TEXT,
    prompt TEXT,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE,
    FOREIGN KEY(transcript_run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_translation_runs_track ON pipeline_translation_runs(track_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_translation_runs_transcript ON pipeline_translation_runs(transcript_run_id);

CREATE TABLE IF NOT EXISTS pipeline_translation_run_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, segment_index),
    FOREIGN KEY(run_id) REFERENCES pipeline_translation_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_translation_run_segments_run ON pipeline_translation_run_segments(run_id, segment_index);

CREATE TABLE IF NOT EXISTS pipeline_track_active_outputs (
    track_id INTEGER PRIMARY KEY,
    active_transcript_run_id INTEGER,
    active_translation_run_id INTEGER,
    active_translation_target_language TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE,
    FOREIGN KEY(active_transcript_run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE SET NULL,
    FOREIGN KEY(active_translation_run_id) REFERENCES pipeline_translation_runs(id) ON DELETE SET NULL
);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    product_code,
    title,
    title_en,
    circle,
    seiyuu,
    tags,
    custom_tags,
    description,
    content='items',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, product_code, title, title_en, circle, seiyuu, tags, custom_tags, description)
    VALUES (new.id, new.product_code, new.title, new.title_en, new.circle, new.seiyuu, new.tags, new.custom_tags, new.description);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, product_code, title, title_en, circle, seiyuu, tags, custom_tags, description)
    VALUES ('delete', old.id, old.product_code, old.title, old.title_en, old.circle, old.seiyuu, old.tags, old.custom_tags, old.description);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, product_code, title, title_en, circle, seiyuu, tags, custom_tags, description)
    VALUES ('delete', old.id, old.product_code, old.title, old.title_en, old.circle, old.seiyuu, old.tags, old.custom_tags, old.description);
    INSERT INTO items_fts(rowid, product_code, title, title_en, circle, seiyuu, tags, custom_tags, description)
    VALUES (new.id, new.product_code, new.title, new.title_en, new.circle, new.seiyuu, new.tags, new.custom_tags, new.description);
END;
"""


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


MIGRATION_IDS = [
    "001_add_items_confidence",
    "002_add_items_original_confidence",
    "003_add_items_english_metadata_fields",
    "004_create_normalized_metadata_index_tables",
    "005_backfill_normalized_metadata_indexes",
    "006_add_pipeline_foundation_tables",
    "007_add_pipeline_transcript_translation_runs",
    "008_add_track_summary_json",
    "009_add_track_title_en",
    "010_add_seiyuu_aliases",
    "011_add_tokutens",
    "012_add_items_manual_track_count",
    "013_add_games",
    "014_drop_games_vndb_unique",
    "015_update_tokutens_shop_enum",
    "016_games_wishlist_and_is_manual",
    "017_add_tokutens_vndb_id",
    "018_split_owned_available_platforms",
    "019_add_want_to_play_status",
    "020_add_vndb_searched",
    "021_add_ignored_game_paths",
    "022_add_extra_library_paths",
    "023_add_items_glossary",
    "024_backfill_tokuten_titles_from_items",
    "025_add_items_listen_status",
    "026_tokutens_metadata_sources",
    "027_add_rejet_shop",
    "028_sync_shop_enum_with_sources",
]

# Migrations whose first run should trigger an on-disk backup of library.db.
# Maps migration_id -> backup-file suffix (companion file in DB_PATH parent).
PRE_MIGRATION_BACKUPS = {
    "011_add_tokutens": "pre-tokutens-bak",
    "013_add_games": "pre-games-bak",
    "014_drop_games_vndb_unique": "pre-games-vndb-unique-bak",
    "015_update_tokutens_shop_enum": "pre-tokutens-shop-enum-bak",
    "016_games_wishlist_and_is_manual": "pre-wishlist-and-is-manual-bak",
    "026_tokutens_metadata_sources": "pre-metadata-sources-bak",
}


async def init_db():
    _backup_db_before_migrations()
    db = await get_db()
    try:
        await db.executescript(SCHEMA_SQL)
        await db.executescript(FTS_SQL)
        await apply_migrations(db)
        await recover_interrupted_jobs(db)
        await ensure_default_settings(db)
        await db.commit()
    finally:
        await db.close()


async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    return any(row["name"] == column_name for row in rows)


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return await cursor.fetchone() is not None


async def _is_migration_applied(db: aiosqlite.Connection, migration_id: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ?",
        (migration_id,),
    )
    return await cursor.fetchone() is not None


async def _record_migration(db: aiosqlite.Connection, migration_id: str):
    await db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
        (migration_id, datetime.now().isoformat()),
    )


async def _migration_001_add_items_confidence(db: aiosqlite.Connection):
    if not await _column_exists(db, "items", "confidence"):
        await db.execute("ALTER TABLE items ADD COLUMN confidence TEXT DEFAULT 'low'")


async def _migration_002_add_items_original_confidence(db: aiosqlite.Connection):
    if not await _column_exists(db, "items", "original_confidence"):
        await db.execute("ALTER TABLE items ADD COLUMN original_confidence TEXT")


async def _migration_003_add_items_english_metadata_fields(db: aiosqlite.Connection):
    if not await _column_exists(db, "items", "seiyuu_en"):
        await db.execute("ALTER TABLE items ADD COLUMN seiyuu_en TEXT DEFAULT '[]'")
    if not await _column_exists(db, "items", "tags_en"):
        await db.execute("ALTER TABLE items ADD COLUMN tags_en TEXT DEFAULT '[]'")
    if not await _column_exists(db, "items", "description_en"):
        await db.execute("ALTER TABLE items ADD COLUMN description_en TEXT")


async def _migration_004_create_normalized_metadata_index_tables(db: aiosqlite.Connection):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS item_seiyuu (
            item_id INTEGER NOT NULL,
            lang TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (item_id, lang, name),
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_item_seiyuu_lang_name ON item_seiyuu(lang, name);
        CREATE INDEX IF NOT EXISTS idx_item_seiyuu_item ON item_seiyuu(item_id);

        CREATE TABLE IF NOT EXISTS item_tags (
            item_id INTEGER NOT NULL,
            lang TEXT NOT NULL,
            source TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (item_id, lang, source, name),
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_item_tags_lang_source_name ON item_tags(lang, source, name);
        CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);
        """
    )


def _safe_json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(value)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for entry in raw:
        name = str(entry).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


async def _refresh_metadata_index_for_item(
    db: aiosqlite.Connection,
    item_id: int,
    seiyuu_jp: list[str] | None = None,
    seiyuu_en: list[str] | None = None,
    tags_jp: list[str] | None = None,
    tags_en: list[str] | None = None,
    custom_tags: list[str] | None = None,
):
    await db.execute("DELETE FROM item_seiyuu WHERE item_id = ?", (item_id,))
    await db.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))

    for name in _safe_json_list(seiyuu_jp):
        await db.execute(
            "INSERT OR IGNORE INTO item_seiyuu (item_id, lang, name) VALUES (?, 'jp', ?)",
            (item_id, name),
        )
    for name in _safe_json_list(seiyuu_en):
        await db.execute(
            "INSERT OR IGNORE INTO item_seiyuu (item_id, lang, name) VALUES (?, 'en', ?)",
            (item_id, name),
        )

    for name in _safe_json_list(tags_jp):
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, lang, source, name) VALUES (?, 'jp', 'dlsite', ?)",
            (item_id, name),
        )
    for name in _safe_json_list(tags_en):
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, lang, source, name) VALUES (?, 'en', 'dlsite', ?)",
            (item_id, name),
        )
    for name in _safe_json_list(custom_tags):
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, lang, source, name) VALUES (?, 'all', 'custom', ?)",
            (item_id, name),
        )


async def _migration_005_backfill_normalized_metadata_indexes(db: aiosqlite.Connection):
    if not await _table_exists(db, "item_seiyuu") or not await _table_exists(db, "item_tags"):
        return
    await db.execute("DELETE FROM item_seiyuu")
    await db.execute("DELETE FROM item_tags")
    cursor = await db.execute(
        "SELECT id, seiyuu, seiyuu_en, tags, tags_en, custom_tags FROM items"
    )
    rows = await cursor.fetchall()
    for row in rows:
        await _refresh_metadata_index_for_item(
            db,
            row["id"],
            seiyuu_jp=_safe_json_list(row["seiyuu"]),
            seiyuu_en=_safe_json_list(row["seiyuu_en"]),
            tags_jp=_safe_json_list(row["tags"]),
            tags_en=_safe_json_list(row["tags_en"]),
            custom_tags=_safe_json_list(row["custom_tags"]),
        )


async def _migration_006_add_pipeline_foundation_tables(db: aiosqlite.Connection):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pipeline_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            archive_path TEXT,
            extract_root TEXT,
            track_path TEXT NOT NULL,
            track_index INTEGER DEFAULT 0,
            title TEXT,
            duration_seconds REAL,
            codec TEXT,
            sample_rate INTEGER,
            channels INTEGER,
            status TEXT DEFAULT 'indexed',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(item_id, track_path),
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_tracks_item ON pipeline_tracks(item_id);
        CREATE INDEX IF NOT EXISTS idx_pipeline_tracks_status ON pipeline_tracks(status);

        CREATE TABLE IF NOT EXISTS pipeline_transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            language TEXT NOT NULL DEFAULT 'ja',
            text TEXT NOT NULL,
            engine TEXT,
            model TEXT,
            prompt_hash TEXT,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(track_id, segment_index, language, engine, model),
            FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_track_idx ON pipeline_transcript_segments(track_id, segment_index);
        CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_language ON pipeline_transcript_segments(language);

        CREATE TABLE IF NOT EXISTS pipeline_translation_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_segment_id INTEGER NOT NULL,
            target_language TEXT NOT NULL,
            text TEXT NOT NULL,
            engine TEXT,
            model TEXT,
            prompt_hash TEXT,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(transcript_segment_id, target_language, engine, model),
            FOREIGN KEY(transcript_segment_id) REFERENCES pipeline_transcript_segments(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_translation_segment ON pipeline_translation_segments(transcript_segment_id);
        CREATE INDEX IF NOT EXISTS idx_pipeline_translation_lang ON pipeline_translation_segments(target_language);
        """
    )


async def _migration_007_add_pipeline_transcript_translation_runs(db: aiosqlite.Connection):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pipeline_transcript_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            language TEXT NOT NULL DEFAULT 'ja',
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'completed',
            engine TEXT,
            model TEXT,
            prompt TEXT,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_runs_track ON pipeline_transcript_runs(track_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS pipeline_transcript_run_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            text TEXT NOT NULL,
            confidence REAL,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, segment_index),
            FOREIGN KEY(run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_transcript_run_segments_run ON pipeline_transcript_run_segments(run_id, segment_index);

        CREATE TABLE IF NOT EXISTS pipeline_translation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            transcript_run_id INTEGER NOT NULL,
            target_language TEXT NOT NULL DEFAULT 'en',
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'completed',
            engine TEXT,
            model TEXT,
            prompt TEXT,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE,
            FOREIGN KEY(transcript_run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_translation_runs_track ON pipeline_translation_runs(track_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pipeline_translation_runs_transcript ON pipeline_translation_runs(transcript_run_id);

        CREATE TABLE IF NOT EXISTS pipeline_translation_run_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, segment_index),
            FOREIGN KEY(run_id) REFERENCES pipeline_translation_runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_translation_run_segments_run ON pipeline_translation_run_segments(run_id, segment_index);

        CREATE TABLE IF NOT EXISTS pipeline_track_active_outputs (
            track_id INTEGER PRIMARY KEY,
            active_transcript_run_id INTEGER,
            active_translation_run_id INTEGER,
            active_translation_target_language TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(track_id) REFERENCES pipeline_tracks(id) ON DELETE CASCADE,
            FOREIGN KEY(active_transcript_run_id) REFERENCES pipeline_transcript_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(active_translation_run_id) REFERENCES pipeline_translation_runs(id) ON DELETE SET NULL
        );
        """
    )


async def _migration_008_add_track_summary_json(db: aiosqlite.Connection):
    """Add track_summary_json column to pipeline_tracks for context memory."""
    if not await _table_exists(db, "pipeline_tracks"):
        return
    await db.execute("ALTER TABLE pipeline_tracks ADD COLUMN track_summary_json TEXT")


async def _migration_009_add_track_title_en(db: aiosqlite.Connection):
    """Add title_en column to pipeline_tracks for translated track names."""
    if not await _table_exists(db, "pipeline_tracks"):
        return
    cursor = await db.execute("PRAGMA table_info(pipeline_tracks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "title_en" not in cols:
        await db.execute("ALTER TABLE pipeline_tracks ADD COLUMN title_en TEXT")


async def _migration_012_add_items_manual_track_count(db: aiosqlite.Connection):
    """Manual override for the unique-content track count shown in the Workshop
    item card. Auto-derived from sibling-group clustering (FLAC + MP3 + SFX
    variants of the same audio collapse to one), but some releases need a hand
    correction — that override lives here. NULL means "use the auto value"."""
    if not await _table_exists(db, "items"):
        return
    cursor = await db.execute("PRAGMA table_info(items)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "manual_track_count" not in cols:
        await db.execute("ALTER TABLE items ADD COLUMN manual_track_count INTEGER")


async def _migration_014_drop_games_vndb_unique(db: aiosqlite.Connection):
    """Drop the UNIQUE constraint on games.vndb_id. Two separate game rows
    may legitimately point at the same VNDB entry — e.g. a release with
    multiple disc/route sub-folders where VNDB only lists the parent VN
    (Star-Crossed Myth Department of Wishes / Punishments). SQLite can't
    DROP an inline UNIQUE constraint, so we rebuild the table. Foreign-key
    enforcement is paused for the rebuild so the cascade on game_tokutens
    doesn't kick in when we DROP games."""
    if not await _table_exists(db, "games"):
        return
    # Foreign keys must be toggled OUTSIDE a transaction in SQLite. The
    # caller (apply_migrations) runs migrations inside an implicit
    # transaction, so we COMMIT the current one, toggle, rebuild, toggle
    # back, and re-open the transaction so the outer caller's COMMIT is
    # a no-op.
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE games_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vndb_id TEXT,
                title TEXT NOT NULL,
                title_jp TEXT,
                title_en TEXT,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                olang TEXT,
                developer TEXT,
                developers_json TEXT NOT NULL DEFAULT '[]',
                release_date TEXT,
                cover_url TEXT,
                cover_local TEXT,
                description TEXT,
                platforms_json TEXT NOT NULL DEFAULT '[]',
                languages_json TEXT NOT NULL DEFAULT '[]',
                library_path TEXT,
                is_archive INTEGER NOT NULL DEFAULT 0,
                play_status TEXT NOT NULL DEFAULT 'backlog'
                    CHECK(play_status IN ('backlog','playing','completed','dropped','on_hold')),
                personal_rating INTEGER,
                personal_notes TEXT NOT NULL DEFAULT '',
                walkthrough_notes TEXT NOT NULL DEFAULT '',
                routes_json TEXT NOT NULL DEFAULT '[]',
                favorite INTEGER NOT NULL DEFAULT 0,
                custom_tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO games_new (
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, languages_json, library_path,
                is_archive, play_status, personal_rating, personal_notes,
                walkthrough_notes, routes_json, favorite, custom_tags_json,
                created_at, updated_at
            )
            SELECT
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, languages_json, library_path,
                is_archive, play_status, personal_rating, personal_notes,
                walkthrough_notes, routes_json, favorite, custom_tags_json,
                created_at, updated_at
            FROM games;
            DROP TABLE games;
            ALTER TABLE games_new RENAME TO games;
            CREATE INDEX IF NOT EXISTS idx_games_vndb_id ON games(vndb_id);
            CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
            CREATE INDEX IF NOT EXISTS idx_games_play_status ON games(play_status);
            CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_games_library_path
                ON games(library_path) WHERE library_path IS NOT NULL;
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _migration_013_add_games(db: aiosqlite.Connection):
    """Catalog table for the Games wing. Games are catalog-only (no extraction
    pipeline like drama CDs) — the app just records where the game lives on
    disk plus VNDB metadata and personal tracking. `game_tokutens` is the
    many-to-many junction so a tokuten can ship with several games and a game
    can have several tokutens (bonus discs, store-exclusive sets, etc.).
    Distinct from `items` to keep pipeline_tracks / transcript / translation
    queries from having to filter `kind != 'game'` everywhere."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- VNDB-sourced metadata (nullable for doujin / unlisted entries)
            vndb_id TEXT UNIQUE,
            title TEXT NOT NULL,
            title_jp TEXT,
            title_en TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            olang TEXT,
            developer TEXT,
            developers_json TEXT NOT NULL DEFAULT '[]',
            release_date TEXT,
            cover_url TEXT,
            cover_local TEXT,
            description TEXT,
            platforms_json TEXT NOT NULL DEFAULT '[]',
            languages_json TEXT NOT NULL DEFAULT '[]',
            -- Local file pointer (catalog-only; we don't manage extraction)
            library_path TEXT,
            is_archive INTEGER NOT NULL DEFAULT 0,
            -- Personal tracking
            play_status TEXT NOT NULL DEFAULT 'backlog'
                CHECK(play_status IN ('backlog','playing','completed','dropped','on_hold')),
            personal_rating INTEGER,
            personal_notes TEXT NOT NULL DEFAULT '',
            walkthrough_notes TEXT NOT NULL DEFAULT '',
            routes_json TEXT NOT NULL DEFAULT '[]',
            favorite INTEGER NOT NULL DEFAULT 0,
            custom_tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_games_vndb_id ON games(vndb_id);
        CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
        CREATE INDEX IF NOT EXISTS idx_games_play_status ON games(play_status);
        CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_games_library_path
            ON games(library_path) WHERE library_path IS NOT NULL;

        CREATE TABLE IF NOT EXISTS game_tokutens (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            tokuten_id INTEGER NOT NULL REFERENCES tokutens(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (game_id, tokuten_id)
        );
        CREATE INDEX IF NOT EXISTS idx_game_tokutens_tokuten
            ON game_tokutens(tokuten_id);
        """
    )


async def _migration_011_add_tokutens(db: aiosqlite.Connection):
    """Adds tokutens (game-bonus / community-shared CDs and other bonus
    items), polymorphic media_assets gallery, and an items.kind discriminator
    so audio tokutens can ride the existing pipeline tables. Drama CDs keep
    kind='drama_cd' (default); audio tokutens become kind='tokuten_audio'
    with items.tokuten_id pointing back at the tokutens row."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tokutens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'audio'
                CHECK(kind IN ('audio','book','image','misc')),
            title TEXT NOT NULL,
            title_en TEXT,
            shop TEXT NOT NULL DEFAULT 'other'
                CHECK(shop IN ('animate','stellaworth','getchu','melonbooks',
                               'toranoana','amazon_jp','other')),
            shop_other_name TEXT,
            release_date TEXT,
            notes TEXT DEFAULT '',
            cover_local TEXT,
            source_url TEXT,
            local_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tokutens_kind ON tokutens(kind);
        CREATE INDEX IF NOT EXISTS idx_tokutens_shop ON tokutens(shop);

        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_kind TEXT NOT NULL
                CHECK(parent_kind IN ('item','tokuten','game')),
            parent_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'gallery'
                CHECK(role IN ('cover','tracklist','gallery','other')),
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_assets_parent
            ON media_assets(parent_kind, parent_id, sort_order);
        """
    )
    if not await _column_exists(db, "items", "kind"):
        await db.execute(
            "ALTER TABLE items ADD COLUMN kind TEXT NOT NULL DEFAULT 'drama_cd'"
        )
    if not await _column_exists(db, "items", "tokuten_id"):
        await db.execute(
            "ALTER TABLE items ADD COLUMN tokuten_id INTEGER REFERENCES tokutens(id)"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_tokuten_id ON items(tokuten_id)"
    )


async def _migration_010_add_seiyuu_aliases(db: aiosqlite.Connection):
    """Canonical-name table for seiyuu deduplication. Each row maps an
    alias spelling to a canonical romanization, so the LLM-translated
    seiyuu_en arrays can be normalized at write or read time."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS seiyuu_aliases (
            alias TEXT PRIMARY KEY COLLATE NOCASE,
            canonical_en TEXT NOT NULL,
            canonical_jp TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_seiyuu_aliases_canonical "
        "ON seiyuu_aliases(canonical_en COLLATE NOCASE)"
    )


async def _migration_015_update_tokutens_shop_enum(db: aiosqlite.Connection):
    """Repurpose tokutens.shop as the user-facing 'source' filter. Old enum
    (animate/stellaworth/getchu/melonbooks/toranoana/amazon_jp/other) is
    replaced by (dlsite/booth/melon/animate/stellaworth/physical/other).
    Existing data is remapped — melonbooks→melon; getchu/toranoana/amazon_jp
    collapse to 'other' since the user doesn't shop those anymore."""
    if not await _table_exists(db, "tokutens"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        # Remap existing values to the new enum first so the rebuilt CHECK
        # constraint doesn't reject them on copy.
        await db.execute("UPDATE tokutens SET shop = 'melon' WHERE shop = 'melonbooks'")
        await db.execute(
            "UPDATE tokutens SET shop = 'other' WHERE shop IN ('getchu','toranoana','amazon_jp')"
        )
        await db.executescript(
            """
            CREATE TABLE tokutens_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'audio'
                    CHECK(kind IN ('audio','book','image','misc')),
                title TEXT NOT NULL,
                title_en TEXT,
                shop TEXT NOT NULL DEFAULT 'other'
                    CHECK(shop IN ('dlsite','booth','melon','animate',
                                   'stellaworth','physical','other')),
                shop_other_name TEXT,
                release_date TEXT,
                notes TEXT DEFAULT '',
                cover_local TEXT,
                source_url TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO tokutens_new (
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at
            )
            SELECT
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at
            FROM tokutens;
            DROP TABLE tokutens;
            ALTER TABLE tokutens_new RENAME TO tokutens;
            CREATE INDEX IF NOT EXISTS idx_tokutens_kind ON tokutens(kind);
            CREATE INDEX IF NOT EXISTS idx_tokutens_shop ON tokutens(shop);
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _migration_016_games_wishlist_and_is_manual(db: aiosqlite.Connection):
    """Rebuild games to (a) accept 'wishlist' in the play_status CHECK and
    (b) add an is_manual column. Also tacks is_manual onto items and
    backfills both: items.is_manual=1 where product_code is MAN-/TKT-,
    games.is_manual=1 where library_path is NULL (placeholders from /blank)."""
    if not await _table_exists(db, "games"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE games_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vndb_id TEXT,
                title TEXT NOT NULL,
                title_jp TEXT,
                title_en TEXT,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                olang TEXT,
                developer TEXT,
                developers_json TEXT NOT NULL DEFAULT '[]',
                release_date TEXT,
                cover_url TEXT,
                cover_local TEXT,
                description TEXT,
                platforms_json TEXT NOT NULL DEFAULT '[]',
                languages_json TEXT NOT NULL DEFAULT '[]',
                library_path TEXT,
                is_archive INTEGER NOT NULL DEFAULT 0,
                play_status TEXT NOT NULL DEFAULT 'backlog'
                    CHECK(play_status IN ('backlog','playing','completed',
                                          'dropped','on_hold','wishlist')),
                personal_rating INTEGER,
                personal_notes TEXT NOT NULL DEFAULT '',
                walkthrough_notes TEXT NOT NULL DEFAULT '',
                routes_json TEXT NOT NULL DEFAULT '[]',
                favorite INTEGER NOT NULL DEFAULT 0,
                custom_tags_json TEXT NOT NULL DEFAULT '[]',
                is_manual INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO games_new (
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, languages_json, library_path,
                is_archive, play_status, personal_rating, personal_notes,
                walkthrough_notes, routes_json, favorite, custom_tags_json,
                is_manual, created_at, updated_at
            )
            SELECT
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, languages_json, library_path,
                is_archive, play_status, personal_rating, personal_notes,
                walkthrough_notes, routes_json, favorite, custom_tags_json,
                CASE WHEN library_path IS NULL THEN 1 ELSE 0 END,
                created_at, updated_at
            FROM games;
            DROP TABLE games;
            ALTER TABLE games_new RENAME TO games;
            CREATE INDEX IF NOT EXISTS idx_games_vndb_id ON games(vndb_id);
            CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
            CREATE INDEX IF NOT EXISTS idx_games_play_status ON games(play_status);
            CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
            CREATE INDEX IF NOT EXISTS idx_games_is_manual ON games(is_manual);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_games_library_path
                ON games(library_path) WHERE library_path IS NOT NULL;
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")

    # items.is_manual is a plain ADD COLUMN — no CHECK rewrite needed.
    if not await _column_exists(db, "items", "is_manual"):
        await db.execute(
            "ALTER TABLE items ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
        )
    await db.execute(
        """UPDATE items SET is_manual = 1
           WHERE product_code LIKE 'MAN-%' OR product_code LIKE 'TKT-%'"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_items_is_manual ON items(is_manual)")


async def _migration_023_add_items_glossary(db: aiosqlite.Connection):
    """Per-item translator glossary. Free-form text the user pastes (e.g.
    '智恵 = Tomoe, 茅 = Chigaya') that gets prepended to every translation
    prompt for tracks belonging to this item. Scoped to the item so character
    name mappings from one CD don't leak into another."""
    if not await _column_exists(db, "items", "glossary"):
        await db.execute(
            "ALTER TABLE items ADD COLUMN glossary TEXT NOT NULL DEFAULT ''"
        )


async def _migration_025_add_items_listen_status(db: aiosqlite.Connection):
    """Personal listening-progress tracker for drama CDs — mirrors
    games.play_status. No CHECK constraint (items has none on translation_status
    either); the API layer validates against _DRAMA_CD_LISTEN_STATUSES."""
    if not await _column_exists(db, "items", "listen_status"):
        await db.execute(
            "ALTER TABLE items ADD COLUMN listen_status TEXT NOT NULL DEFAULT 'backlog'"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_listen_status ON items(listen_status)"
    )


async def _migration_024_backfill_tokuten_titles_from_items(db: aiosqlite.Connection):
    """Pre-fix: items PUT didn't mirror title onto the linked tokutens row, so
    manual tokutens edited via the items detail panel kept the
    '[New Tokuten]' placeholder in tokutens.title — visible in the game detail
    panel's linked-tokutens list. Copy items.title onto tokutens.title (and
    title_en) only for rows where the tokuten still has the placeholder."""
    if not await _table_exists(db, "tokutens"):
        return
    await db.execute(
        """UPDATE tokutens
           SET title = (
                   SELECT items.title FROM items
                   WHERE items.tokuten_id = tokutens.id
                     AND items.title IS NOT NULL
                     AND items.title != ''
                     AND items.title != '[New Tokuten]'
                   LIMIT 1
               ),
               updated_at = ?
           WHERE tokutens.title = '[New Tokuten]'
             AND EXISTS (
                   SELECT 1 FROM items
                   WHERE items.tokuten_id = tokutens.id
                     AND items.title IS NOT NULL
                     AND items.title != ''
                     AND items.title != '[New Tokuten]'
               )""",
        (datetime.now().isoformat(),),
    )
    await db.execute(
        """UPDATE tokutens
           SET title_en = (
                   SELECT items.title_en FROM items
                   WHERE items.tokuten_id = tokutens.id
                     AND items.title_en IS NOT NULL
                     AND items.title_en != ''
                   LIMIT 1
               )
           WHERE (tokutens.title_en IS NULL OR tokutens.title_en = '')
             AND EXISTS (
                   SELECT 1 FROM items
                   WHERE items.tokuten_id = tokutens.id
                     AND items.title_en IS NOT NULL
                     AND items.title_en != ''
               )""",
    )


async def _migration_022_add_extra_library_paths(db: aiosqlite.Connection):
    """Same game on multiple platforms / install locations. The primary
    library_path stays as-is (scanner upserts on it); additional paths
    live in extra_library_paths_json as a JSON array of strings. When two
    rows share the same vndb_id, the merge endpoint folds the others'
    paths into the primary's extras."""
    if not await _table_exists(db, "games"):
        return
    if not await _column_exists(db, "games", "extra_library_paths_json"):
        await db.execute(
            "ALTER TABLE games ADD COLUMN extra_library_paths_json TEXT NOT NULL DEFAULT '[]'"
        )


async def _migration_021_add_ignored_game_paths(db: aiosqlite.Connection):
    """Per-path ignore list for games (parallel to ignored_codes for items).
    When the user 'Remove from library' a game, the library_path goes here
    and future scans skip it. Stored lowercased so Windows path comparison
    is case-insensitive."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS ignored_game_paths (
            path_key TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )


async def _migration_020_add_vndb_searched(db: aiosqlite.Connection):
    """Some games legitimately aren't in VNDB (mobile / Switch shovelware /
    very obscure indie titles). After the user goes through the cleanup
    queue and confirms "no VNDB entry, here are the fields manually", the
    row should drop out of the 'unmatched' count — staying in the queue
    forever is just noise. `vndb_searched=1` is the "I reviewed this, no
    VNDB available, move on" flag."""
    if not await _table_exists(db, "games"):
        return
    if not await _column_exists(db, "games", "vndb_searched"):
        await db.execute(
            "ALTER TABLE games ADD COLUMN vndb_searched INTEGER NOT NULL DEFAULT 0"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_games_vndb_searched ON games(vndb_searched)"
    )


async def _migration_019_add_want_to_play_status(db: aiosqlite.Connection):
    """Extend games.play_status CHECK to accept 'want_to_play'. SQLite can't
    ALTER a CHECK in place, so it's another full table rebuild. Same pattern
    as migration 016."""
    if not await _table_exists(db, "games"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE games_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vndb_id TEXT,
                title TEXT NOT NULL,
                title_jp TEXT,
                title_en TEXT,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                olang TEXT,
                developer TEXT,
                developers_json TEXT NOT NULL DEFAULT '[]',
                release_date TEXT,
                cover_url TEXT,
                cover_local TEXT,
                description TEXT,
                platforms_json TEXT NOT NULL DEFAULT '[]',
                platforms_available_json TEXT NOT NULL DEFAULT '[]',
                languages_json TEXT NOT NULL DEFAULT '[]',
                library_path TEXT,
                is_archive INTEGER NOT NULL DEFAULT 0,
                play_status TEXT NOT NULL DEFAULT 'backlog'
                    CHECK(play_status IN ('backlog','playing','completed',
                                          'dropped','on_hold','wishlist',
                                          'want_to_play')),
                personal_rating INTEGER,
                personal_notes TEXT NOT NULL DEFAULT '',
                walkthrough_notes TEXT NOT NULL DEFAULT '',
                routes_json TEXT NOT NULL DEFAULT '[]',
                favorite INTEGER NOT NULL DEFAULT 0,
                custom_tags_json TEXT NOT NULL DEFAULT '[]',
                is_manual INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO games_new (
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, platforms_available_json,
                languages_json, library_path, is_archive, play_status,
                personal_rating, personal_notes, walkthrough_notes, routes_json,
                favorite, custom_tags_json, is_manual, created_at, updated_at
            )
            SELECT
                id, vndb_id, title, title_jp, title_en, aliases_json, olang,
                developer, developers_json, release_date, cover_url, cover_local,
                description, platforms_json, platforms_available_json,
                languages_json, library_path, is_archive, play_status,
                personal_rating, personal_notes, walkthrough_notes, routes_json,
                favorite, custom_tags_json, is_manual, created_at, updated_at
            FROM games;
            DROP TABLE games;
            ALTER TABLE games_new RENAME TO games;
            CREATE INDEX IF NOT EXISTS idx_games_vndb_id ON games(vndb_id);
            CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
            CREATE INDEX IF NOT EXISTS idx_games_play_status ON games(play_status);
            CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
            CREATE INDEX IF NOT EXISTS idx_games_is_manual ON games(is_manual);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_games_library_path
                ON games(library_path) WHERE library_path IS NOT NULL;
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _migration_018_split_owned_available_platforms(db: aiosqlite.Connection):
    """Split the games.platforms_json column into 'owned' (from scanner /
    user files) and 'available' (from VNDB). Pre-split rows stay readable:
    the card pill (owned only) keeps showing what existed before, and the
    detail panel gains the VNDB-derived 'Also available on' line. We
    mirror the current platforms_json onto the new column so VNDB data
    captured before the split isn't lost — user can edit either side."""
    if not await _table_exists(db, "games"):
        return
    if not await _column_exists(db, "games", "platforms_available_json"):
        await db.execute(
            "ALTER TABLE games ADD COLUMN platforms_available_json TEXT NOT NULL DEFAULT '[]'"
        )
    # One-time backfill: copy existing platforms into the new column so the
    # detail-panel 'available' display shows the VNDB list the row already
    # had. The 'owned' column (platforms_json) stays as-is.
    await db.execute(
        """UPDATE games
           SET platforms_available_json = platforms_json
           WHERE platforms_available_json = '[]'
             AND platforms_json != '[]'
             AND platforms_json IS NOT NULL"""
    )


async def _migration_017_add_tokutens_vndb_id(db: aiosqlite.Connection):
    """Tokutens can reference a VNDB game id independently of whether the
    matching game exists in the local library — a bonus disc that ships
    with a VN you don't yet own should still know which VN it belongs to.
    The reverse link is computed at read time by matching games.vndb_id."""
    if not await _table_exists(db, "tokutens"):
        return
    if not await _column_exists(db, "tokutens", "vndb_id"):
        await db.execute("ALTER TABLE tokutens ADD COLUMN vndb_id TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tokutens_vndb_id ON tokutens(vndb_id)"
    )


async def _migration_026_tokutens_metadata_sources(db: aiosqlite.Connection):
    """Metadata-fetch support for tokutens: extend the shop/source enum with
    the scrapable retail/database sites (gamers, chil_chil; vgmdb reserved for
    the future pluggable-fetcher wave) and add cast/description columns
    mirroring items naming (seiyuu/seiyuu_en/description/description_en) so
    tokutens can ride the existing translation machinery. SQLite can't alter a
    CHECK constraint, so the table is rebuilt."""
    if not await _table_exists(db, "tokutens"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE tokutens_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'audio'
                    CHECK(kind IN ('audio','book','image','misc')),
                title TEXT NOT NULL,
                title_en TEXT,
                shop TEXT NOT NULL DEFAULT 'other'
                    CHECK(shop IN ('dlsite','booth','melon','animate',
                                   'stellaworth','gamers','chil_chil','vgmdb',
                                   'physical','other')),
                shop_other_name TEXT,
                release_date TEXT,
                notes TEXT DEFAULT '',
                cover_local TEXT,
                source_url TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                vndb_id TEXT,
                seiyuu TEXT DEFAULT '[]',
                seiyuu_en TEXT DEFAULT '[]',
                description TEXT,
                description_en TEXT
            );
            INSERT INTO tokutens_new (
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id
            )
            SELECT
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id
            FROM tokutens;
            DROP TABLE tokutens;
            ALTER TABLE tokutens_new RENAME TO tokutens;
            CREATE INDEX IF NOT EXISTS idx_tokutens_kind ON tokutens(kind);
            CREATE INDEX IF NOT EXISTS idx_tokutens_shop ON tokutens(shop);
            CREATE INDEX IF NOT EXISTS idx_tokutens_vndb_id ON tokutens(vndb_id);
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _migration_027_add_rejet_shop(db: aiosqlite.Connection):
    """Adds 'rejet' to the tokutens shop/source enum (metadata source for
    Rejet's own works listing). Same rebuild dance as 026 — SQLite CHECK
    constraints can't be altered in place."""
    if not await _table_exists(db, "tokutens"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE tokutens_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'audio'
                    CHECK(kind IN ('audio','book','image','misc')),
                title TEXT NOT NULL,
                title_en TEXT,
                shop TEXT NOT NULL DEFAULT 'other'
                    CHECK(shop IN ('dlsite','booth','melon','animate',
                                   'stellaworth','gamers','chil_chil','vgmdb',
                                   'rejet','physical','other')),
                shop_other_name TEXT,
                release_date TEXT,
                notes TEXT DEFAULT '',
                cover_local TEXT,
                source_url TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                vndb_id TEXT,
                seiyuu TEXT DEFAULT '[]',
                seiyuu_en TEXT DEFAULT '[]',
                description TEXT,
                description_en TEXT
            );
            INSERT INTO tokutens_new (
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id, seiyuu, seiyuu_en,
                description, description_en
            )
            SELECT
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id, seiyuu, seiyuu_en,
                description, description_en
            FROM tokutens;
            DROP TABLE tokutens;
            ALTER TABLE tokutens_new RENAME TO tokutens;
            CREATE INDEX IF NOT EXISTS idx_tokutens_kind ON tokutens(kind);
            CREATE INDEX IF NOT EXISTS idx_tokutens_shop ON tokutens(shop);
            CREATE INDEX IF NOT EXISTS idx_tokutens_vndb_id ON tokutens(vndb_id);
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _migration_028_sync_shop_enum_with_sources(db: aiosqlite.Connection):
    """Bring the tokutens shop CHECK constraint in line with the metadata
    sources / UI shop filter. The newer sources (fanza, toranoana, digiket,
    gyutto, hvdb, pokedora) were wired into the dropdown + filter + the apply
    flow (metadata.py sets shop = source name) but never added to the enum, so
    applying one of their URLs to a tokuten would violate the constraint. Same
    rebuild dance as 026/027 — SQLite CHECK constraints can't be altered in
    place."""
    if not await _table_exists(db, "tokutens"):
        return
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.executescript(
            """
            CREATE TABLE tokutens_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'audio'
                    CHECK(kind IN ('audio','book','image','misc')),
                title TEXT NOT NULL,
                title_en TEXT,
                shop TEXT NOT NULL DEFAULT 'other'
                    CHECK(shop IN ('dlsite','booth','melon','animate',
                                   'stellaworth','gamers','chil_chil','vgmdb',
                                   'rejet','fanza','toranoana','digiket','gyutto',
                                   'hvdb','pokedora','physical','other')),
                shop_other_name TEXT,
                release_date TEXT,
                notes TEXT DEFAULT '',
                cover_local TEXT,
                source_url TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                vndb_id TEXT,
                seiyuu TEXT DEFAULT '[]',
                seiyuu_en TEXT DEFAULT '[]',
                description TEXT,
                description_en TEXT
            );
            INSERT INTO tokutens_new (
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id, seiyuu, seiyuu_en,
                description, description_en
            )
            SELECT
                id, kind, title, title_en, shop, shop_other_name,
                release_date, notes, cover_local, source_url, local_path,
                created_at, updated_at, vndb_id, seiyuu, seiyuu_en,
                description, description_en
            FROM tokutens;
            DROP TABLE tokutens;
            ALTER TABLE tokutens_new RENAME TO tokutens;
            CREATE INDEX IF NOT EXISTS idx_tokutens_kind ON tokutens(kind);
            CREATE INDEX IF NOT EXISTS idx_tokutens_shop ON tokutens(shop);
            CREATE INDEX IF NOT EXISTS idx_tokutens_vndb_id ON tokutens(vndb_id);
            """
        )
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


MIGRATION_HANDLERS = {
    "001_add_items_confidence": _migration_001_add_items_confidence,
    "002_add_items_original_confidence": _migration_002_add_items_original_confidence,
    "003_add_items_english_metadata_fields": _migration_003_add_items_english_metadata_fields,
    "004_create_normalized_metadata_index_tables": _migration_004_create_normalized_metadata_index_tables,
    "005_backfill_normalized_metadata_indexes": _migration_005_backfill_normalized_metadata_indexes,
    "006_add_pipeline_foundation_tables": _migration_006_add_pipeline_foundation_tables,
    "007_add_pipeline_transcript_translation_runs": _migration_007_add_pipeline_transcript_translation_runs,
    "008_add_track_summary_json": _migration_008_add_track_summary_json,
    "009_add_track_title_en": _migration_009_add_track_title_en,
    "010_add_seiyuu_aliases": _migration_010_add_seiyuu_aliases,
    "011_add_tokutens": _migration_011_add_tokutens,
    "012_add_items_manual_track_count": _migration_012_add_items_manual_track_count,
    "013_add_games": _migration_013_add_games,
    "014_drop_games_vndb_unique": _migration_014_drop_games_vndb_unique,
    "015_update_tokutens_shop_enum": _migration_015_update_tokutens_shop_enum,
    "016_games_wishlist_and_is_manual": _migration_016_games_wishlist_and_is_manual,
    "017_add_tokutens_vndb_id": _migration_017_add_tokutens_vndb_id,
    "018_split_owned_available_platforms": _migration_018_split_owned_available_platforms,
    "019_add_want_to_play_status": _migration_019_add_want_to_play_status,
    "020_add_vndb_searched": _migration_020_add_vndb_searched,
    "021_add_ignored_game_paths": _migration_021_add_ignored_game_paths,
    "022_add_extra_library_paths": _migration_022_add_extra_library_paths,
    "023_add_items_glossary": _migration_023_add_items_glossary,
    "024_backfill_tokuten_titles_from_items": _migration_024_backfill_tokuten_titles_from_items,
    "025_add_items_listen_status": _migration_025_add_items_listen_status,
    "026_tokutens_metadata_sources": _migration_026_tokutens_metadata_sources,
    "027_add_rejet_shop": _migration_027_add_rejet_shop,
    "028_sync_shop_enum_with_sources": _migration_028_sync_shop_enum_with_sources,
}


def _backup_db_before_migrations():
    """Copy library.db to its companion .bak files once — a one-shot safety
    snapshot taken the first time the app boots after a new schema-changing
    migration ships. Each suffix is gated by the bak file's existence; if
    the user deletes a bak file after the fact, we don't recreate it. Runs
    synchronously before any aiosqlite connection so it always captures
    pre-migration state."""
    if not DB_PATH.exists():
        return
    for _migration_id, suffix in PRE_MIGRATION_BACKUPS.items():
        backup_path = DB_PATH.with_name(DB_PATH.name + "." + suffix)
        if backup_path.exists():
            continue
        try:
            shutil.copy2(str(DB_PATH), str(backup_path))
            logger.info(f"Backed up library.db to {backup_path.name}")
        except Exception as exc:
            logger.warning(
                f"Could not back up library.db to {backup_path.name}: {exc}"
            )


async def apply_migrations(db: aiosqlite.Connection):
    for migration_id in MIGRATION_IDS:
        if await _is_migration_applied(db, migration_id):
            continue
        handler = MIGRATION_HANDLERS[migration_id]
        await handler(db)
        await _record_migration(db, migration_id)


async def ensure_default_settings(db: aiosqlite.Connection):
    now = datetime.now().isoformat()
    default_pipeline_enabled = "1" if ENABLE_PIPELINE else "0"
    if SCAN_PATH:
        default_scan_paths = json.dumps([SCAN_PATH], ensure_ascii=False)
        await db.execute(
            """INSERT OR IGNORE INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)""",
            ("scan_paths", default_scan_paths, now),
        )
    await db.execute(
        """INSERT OR IGNORE INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)""",
        ("pipeline_enabled", default_pipeline_enabled, now),
    )


async def recover_interrupted_jobs(db: aiosqlite.Connection):
    now = datetime.now().isoformat()
    await db.execute(
        """UPDATE jobs
        SET status = 'interrupted',
            paused = 0,
            stopping = 0,
            stopped = 1,
            error = COALESCE(error, 'Job interrupted (application restart)'),
            finished_at = COALESCE(finished_at, ?),
            updated_at = ?
        WHERE status IN ('running', 'paused', 'stopping')""",
        (now, now),
    )


def _normalize_scan_paths(paths: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for path in paths:
        if not path:
            continue
        clean = str(Path(path).expanduser())
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized


def _normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.strip().upper()
    return normalized or None


async def get_scan_paths() -> list[str]:
    fallback = [SCAN_PATH] if SCAN_PATH else []
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            ("scan_paths",),
        )
        row = await cursor.fetchone()
        if not row:
            return fallback

        try:
            paths = json.loads(row["value"])
        except json.JSONDecodeError:
            return fallback

        if not isinstance(paths, list):
            return fallback

        normalized = _normalize_scan_paths([str(p) for p in paths])
        return normalized or fallback
    finally:
        await db.close()


async def set_scan_paths(paths: list[str]) -> list[str]:
    normalized = _normalize_scan_paths(paths)
    if not normalized:
        raise ValueError("At least one scan path is required")

    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("scan_paths", json.dumps(normalized, ensure_ascii=False), now),
        )
        await db.commit()
        return normalized
    finally:
        await db.close()


async def get_pipeline_enabled() -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            ("pipeline_enabled",),
        )
        row = await cursor.fetchone()
        if not row:
            return bool(ENABLE_PIPELINE)
        value = str(row["value"]).strip().lower()
        return value in {"1", "true", "yes", "on"}
    finally:
        await db.close()


async def set_pipeline_enabled(enabled: bool) -> bool:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        value = "1" if enabled else "0"
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("pipeline_enabled", value, now),
        )
        await db.commit()
        return enabled
    finally:
        await db.close()


async def get_app_setting(key: str) -> str | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (str(key),),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        value = str(row["value"]).strip()
        return value or None
    finally:
        await db.close()


async def set_app_setting(key: str, value: str) -> str:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        clean = str(value).strip()
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (str(key), clean, now),
        )
        await db.commit()
        return clean
    finally:
        await db.close()


async def delete_app_setting(key: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM app_settings WHERE key = ?",
            (str(key),),
        )
        await db.commit()
        return int(cursor.rowcount or 0) > 0
    finally:
        await db.close()


async def get_runtime_gemini_api_key() -> str | None:
    value = await get_app_setting(RUNTIME_GEMINI_API_KEY_SETTING)
    if value:
        return value
    return GEMINI_API_KEY


async def get_runtime_gemini_model() -> str:
    value = await get_app_setting(RUNTIME_GEMINI_MODEL_SETTING)
    if value:
        return value
    return GEMINI_MODEL


async def set_runtime_gemini_api_key(api_key: str) -> bool:
    clean = str(api_key or "").strip()
    if not clean:
        raise ValueError("Gemini API key cannot be empty")
    await set_app_setting(RUNTIME_GEMINI_API_KEY_SETTING, clean)
    return True


async def clear_runtime_gemini_api_key() -> bool:
    return await delete_app_setting(RUNTIME_GEMINI_API_KEY_SETTING)


async def set_runtime_gemini_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise ValueError("Gemini model cannot be empty")
    await set_app_setting(RUNTIME_GEMINI_MODEL_SETTING, clean)
    return clean


async def get_runtime_openrouter_api_key() -> str | None:
    value = await get_app_setting(RUNTIME_OPENROUTER_API_KEY_SETTING)
    if value:
        return value
    return OPENROUTER_API_KEY


async def get_runtime_openrouter_model() -> str:
    value = await get_app_setting(RUNTIME_OPENROUTER_MODEL_SETTING)
    if value:
        return value
    return OPENROUTER_MODEL


async def set_runtime_openrouter_api_key(api_key: str) -> bool:
    clean = str(api_key or "").strip()
    if not clean:
        raise ValueError("OpenRouter API key cannot be empty")
    await set_app_setting(RUNTIME_OPENROUTER_API_KEY_SETTING, clean)
    return True


async def clear_runtime_openrouter_api_key() -> bool:
    return await delete_app_setting(RUNTIME_OPENROUTER_API_KEY_SETTING)


async def set_runtime_openrouter_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise ValueError("OpenRouter model cannot be empty")
    await set_app_setting(RUNTIME_OPENROUTER_MODEL_SETTING, clean)
    return clean


async def get_runtime_chutes_api_key() -> str | None:
    value = await get_app_setting(RUNTIME_CHUTES_API_KEY_SETTING)
    if value:
        return value
    return CHUTES_API_KEY


async def get_runtime_chutes_model() -> str:
    value = await get_app_setting(RUNTIME_CHUTES_MODEL_SETTING)
    if value:
        return value
    return CHUTES_MODEL


async def set_runtime_chutes_api_key(api_key: str) -> bool:
    clean = str(api_key or "").strip()
    if not clean:
        raise ValueError("Chutes API key cannot be empty")
    await set_app_setting(RUNTIME_CHUTES_API_KEY_SETTING, clean)
    return True


async def clear_runtime_chutes_api_key() -> bool:
    return await delete_app_setting(RUNTIME_CHUTES_API_KEY_SETTING)


async def set_runtime_chutes_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise ValueError("Chutes model cannot be empty")
    await set_app_setting(RUNTIME_CHUTES_MODEL_SETTING, clean)
    return clean


SUPPORTED_TRANSLATION_PROVIDERS = {"gemini", "openrouter", "chutes", "openai_compat"}


async def get_runtime_openai_compat_api_key() -> str | None:
    value = await get_app_setting(RUNTIME_OPENAI_COMPAT_API_KEY_SETTING)
    if value:
        return value
    return OPENAI_COMPAT_API_KEY


async def get_runtime_openai_compat_model() -> str:
    value = await get_app_setting(RUNTIME_OPENAI_COMPAT_MODEL_SETTING)
    if value:
        return value
    return OPENAI_COMPAT_MODEL or ""


async def get_runtime_openai_compat_base_url() -> str:
    value = await get_app_setting(RUNTIME_OPENAI_COMPAT_BASE_URL_SETTING)
    if value:
        return value
    return OPENAI_COMPAT_BASE_URL or ""


async def set_runtime_openai_compat_api_key(api_key: str) -> bool:
    clean = str(api_key or "").strip()
    if not clean:
        raise ValueError("OpenAI-compatible API key cannot be empty")
    await set_app_setting(RUNTIME_OPENAI_COMPAT_API_KEY_SETTING, clean)
    return True


async def clear_runtime_openai_compat_api_key() -> bool:
    return await delete_app_setting(RUNTIME_OPENAI_COMPAT_API_KEY_SETTING)


async def set_runtime_openai_compat_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise ValueError("OpenAI-compatible model cannot be empty")
    await set_app_setting(RUNTIME_OPENAI_COMPAT_MODEL_SETTING, clean)
    return clean


async def set_runtime_openai_compat_base_url(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        raise ValueError("OpenAI-compatible base URL cannot be empty")
    if not (clean.startswith("http://") or clean.startswith("https://")):
        raise ValueError("Base URL must start with http:// or https://")
    await set_app_setting(RUNTIME_OPENAI_COMPAT_BASE_URL_SETTING, clean)
    return clean


async def clear_runtime_openai_compat_base_url() -> bool:
    return await delete_app_setting(RUNTIME_OPENAI_COMPAT_BASE_URL_SETTING)


async def get_runtime_openai_compat_request_format() -> str:
    """'openai' (default) or 'anthropic' for proxies that expect /messages."""
    value = await get_app_setting(RUNTIME_OPENAI_COMPAT_REQUEST_FORMAT_SETTING)
    fmt = str(value or "openai").strip().lower()
    if fmt not in {"openai", "anthropic"}:
        return "openai"
    return fmt


async def set_runtime_openai_compat_request_format(fmt: str) -> str:
    clean = str(fmt or "").strip().lower()
    if clean not in {"openai", "anthropic"}:
        raise ValueError("Request format must be 'openai' or 'anthropic'")
    await set_app_setting(RUNTIME_OPENAI_COMPAT_REQUEST_FORMAT_SETTING, clean)
    return clean


async def get_runtime_whisper_model() -> str:
    value = await get_app_setting(RUNTIME_WHISPER_MODEL_SETTING)
    if value:
        return str(value).strip()
    return WHISPER_MODEL or "small"


async def set_runtime_whisper_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise ValueError("Whisper model cannot be empty")
    await set_app_setting(RUNTIME_WHISPER_MODEL_SETTING, clean)
    return clean


async def get_runtime_whisper_vad_filter() -> bool:
    value = await get_app_setting(RUNTIME_WHISPER_VAD_FILTER_SETTING)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def set_runtime_whisper_vad_filter(enabled: bool) -> bool:
    await set_app_setting(RUNTIME_WHISPER_VAD_FILTER_SETTING, "1" if enabled else "0")
    return bool(enabled)


async def get_runtime_whisper_beam_size() -> int:
    value = await get_app_setting(RUNTIME_WHISPER_BEAM_SIZE_SETTING)
    try:
        n = int(str(value).strip()) if value is not None else 5
    except (TypeError, ValueError):
        n = 5
    if n < 1:
        n = 1
    if n > 10:
        n = 10
    return n


async def set_runtime_whisper_beam_size(beam: int) -> int:
    try:
        n = int(beam)
    except (TypeError, ValueError):
        raise ValueError("Beam size must be an integer")
    if n < 1 or n > 10:
        raise ValueError("Beam size must be between 1 and 10")
    await set_app_setting(RUNTIME_WHISPER_BEAM_SIZE_SETTING, str(n))
    return n


async def get_runtime_whisper_condition_on_previous() -> bool:
    value = await get_app_setting(RUNTIME_WHISPER_CONDITION_ON_PREVIOUS_SETTING)
    if value is None:
        return True
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def set_runtime_whisper_condition_on_previous(enabled: bool) -> bool:
    await set_app_setting(RUNTIME_WHISPER_CONDITION_ON_PREVIOUS_SETTING, "1" if enabled else "0")
    return bool(enabled)


async def get_runtime_whisper_preferred_variant() -> str:
    """'sfx' (default — full mix) or 'no-sfx' (voice-only mix when available)."""
    value = await get_app_setting(RUNTIME_WHISPER_PREFERRED_VARIANT_SETTING)
    v = str(value or "sfx").strip().lower()
    return "no-sfx" if v == "no-sfx" else "sfx"


async def set_runtime_whisper_preferred_variant(variant: str) -> str:
    clean = str(variant or "").strip().lower()
    if clean not in {"sfx", "no-sfx"}:
        raise ValueError("Whisper preferred variant must be 'sfx' or 'no-sfx'")
    await set_app_setting(RUNTIME_WHISPER_PREFERRED_VARIANT_SETTING, clean)
    return clean


async def get_runtime_translation_provider() -> str:
    value = await get_app_setting(RUNTIME_TRANSLATION_PROVIDER_SETTING)
    provider = str(value or "gemini").strip().lower()
    if provider not in SUPPORTED_TRANSLATION_PROVIDERS:
        return "gemini"
    return provider


async def set_runtime_translation_provider(provider: str) -> str:
    clean = str(provider or "").strip().lower()
    if clean not in SUPPORTED_TRANSLATION_PROVIDERS:
        raise ValueError(
            "Translation provider must be one of: " + ", ".join(sorted(SUPPORTED_TRANSLATION_PROVIDERS))
        )
    await set_app_setting(RUNTIME_TRANSLATION_PROVIDER_SETTING, clean)
    return clean


def _ignored_path_key(path: str) -> str:
    """Normalize a path for ignored_game_paths comparison. Lowercased
    absolute-as-given (Windows is case-insensitive; on POSIX the user
    probably cares about case but they can re-add if needed)."""
    return str(path or "").strip().rstrip("/\\").lower()


async def get_ignored_game_paths() -> list[dict]:
    """Returns the full list of ignored paths so Settings can display + remove."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT path_key, path, reason, created_at FROM ignored_game_paths ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def get_ignored_game_path_keys() -> set[str]:
    """Lowercased-path set for scanner skip checks."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT path_key FROM ignored_game_paths")
        return {row["path_key"] for row in await cursor.fetchall() if row["path_key"]}
    finally:
        await db.close()


async def add_ignored_game_paths(paths: list[str], reason: str = "") -> list[str]:
    seen = set()
    rows: list[tuple[str, str]] = []
    for raw in paths or []:
        path = str(raw or "").strip()
        if not path:
            continue
        key = _ignored_path_key(path)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append((key, path))
    if not rows:
        return []
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        for key, path in rows:
            await db.execute(
                """INSERT OR IGNORE INTO ignored_game_paths
                       (path_key, path, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (key, path, reason, now),
            )
        await db.commit()
        return [p for _k, p in rows]
    finally:
        await db.close()


async def remove_ignored_game_path(path_key: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM ignored_game_paths WHERE path_key = ?",
            (str(path_key or "").strip().lower(),),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_ignored_codes() -> set[str]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT code FROM ignored_codes")
        rows = await cursor.fetchall()
        codes = set()
        for row in rows:
            code = _normalize_code(row["code"])
            if code:
                codes.add(code)
        return codes
    finally:
        await db.close()


async def add_ignored_codes(codes: list[str], reason: str = "") -> list[str]:
    normalized_codes = []
    seen = set()
    for raw in codes:
        code = _normalize_code(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        normalized_codes.append(code)

    if not normalized_codes:
        return []

    db = await get_db()
    try:
        now = datetime.now().isoformat()
        for code in normalized_codes:
            await db.execute(
                """INSERT OR IGNORE INTO ignored_codes (code, reason, created_at)
                VALUES (?, ?, ?)""",
                (code, reason, now),
            )
        await db.commit()
        return normalized_codes
    finally:
        await db.close()


async def upsert_item(item_data: dict) -> int:
    db = await get_db()
    try:
        # Check if item exists by product_code (primary match)
        cursor = await db.execute(
            "SELECT id, files FROM items WHERE product_code = ?",
            (item_data["product_code"],)
        )
        existing = await cursor.fetchone()

        # IMPORTANT: If same file (original_code) maps to multiple product codes,
        # use the existing entry instead of creating a new duplicate
        if not existing:
            original_code = item_data.get("original_code")
            if original_code:
                cursor = await db.execute(
                    "SELECT id, files FROM items WHERE original_code = ?",
                    (original_code,)
                )
                duplicate = await cursor.fetchone()
                if duplicate:
                    # Same file already exists - use existing entry
                    existing = duplicate

        now = datetime.now().isoformat()

        if existing:
            # Merge file lists
            existing_files = json.loads(existing["files"] or "[]")
            new_files = json.loads(item_data.get("files", "[]"))
            merged_files = sorted(set(existing_files + new_files))

            # Only upgrade confidence, never downgrade (and never overwrite 'verified')
            confidence_sql = ""
            confidence_val = item_data.get("confidence", "low")
            if confidence_val == "high":
                confidence_sql = ", confidence = CASE WHEN confidence = 'verified' THEN 'verified' ELSE ? END"

            update_sql = f"""UPDATE items SET
                    files = ?, file_count = ?, total_size = ?,
                    original_code = COALESCE(original_code, ?),
                    file_format = ?,
                    updated_at = ?
                    {confidence_sql}
                WHERE product_code = ?"""

            params = [
                json.dumps(merged_files),
                len(merged_files),
                item_data.get("total_size", 0),
                item_data.get("original_code"),
                item_data.get("file_format", "[]"),
                now,
            ]
            if confidence_sql:
                params.append(confidence_val)
            params.append(item_data["product_code"])

            await db.execute(update_sql, params)
            await db.commit()
            return existing["id"]
        else:
            cursor = await db.execute(
                """INSERT INTO items
                    (product_code, original_code, files, file_count, total_size,
                     file_format, confidence, scan_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_data["product_code"],
                    item_data.get("original_code"),
                    item_data.get("files", "[]"),
                    item_data.get("file_count", 1),
                    item_data.get("total_size", 0),
                    item_data.get("file_format", "[]"),
                    item_data.get("confidence", "low"),
                    now, now, now,
                )
            )
            await db.commit()
            return cursor.lastrowid
    finally:
        await db.close()


async def update_item_metadata(product_code: str, metadata: dict):
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        seiyuu_jp = _safe_json_list(metadata.get("seiyuu", []))
        seiyuu_en = _safe_json_list(metadata.get("seiyuu_en", []))
        # Reuse romanizations the library already knows. DLsite often gives no
        # English voice-actor spelling, so the scraper stores seiyuu_en as a
        # copy of the JP names; fill any such slot from a name we've already
        # romanized elsewhere (exact JP match) so imports don't lose the
        # romanization we worked out before.
        if seiyuu_jp:
            romaji_map = await _build_seiyuu_romanization_map(db)
            if romaji_map:
                filled_en, _filled = _fill_known_romanizations(seiyuu_jp, seiyuu_en, romaji_map)
                if _filled:
                    seiyuu_en = filled_en
        tags_jp = _safe_json_list(metadata.get("tags", []))
        tags_en = _safe_json_list(metadata.get("tags_en", []))
        await db.execute(
            """UPDATE items SET
                title = ?, title_en = ?, circle = ?, description = ?, description_en = ?,
                cover_url = ?, cover_local = ?, seiyuu = ?, seiyuu_en = ?, tags = ?, tags_en = ?,
                series = ?, release_date = ?, age_rating = ?,
                metadata_raw = ?, metadata_date = ?, updated_at = ?
            WHERE product_code = ?""",
            (
                metadata.get("title"),
                metadata.get("title_en"),
                metadata.get("circle"),
                metadata.get("description"),
                metadata.get("description_en"),
                metadata.get("cover_url"),
                metadata.get("cover_local"),
                json.dumps(seiyuu_jp, ensure_ascii=False),
                json.dumps(seiyuu_en, ensure_ascii=False),
                json.dumps(tags_jp, ensure_ascii=False),
                json.dumps(tags_en, ensure_ascii=False),
                metadata.get("series"),
                metadata.get("release_date"),
                metadata.get("age_rating"),
                json.dumps(metadata.get("raw", {}), ensure_ascii=False),
                now, now,
                product_code,
            )
        )
        cursor = await db.execute(
            "SELECT id, custom_tags FROM items WHERE product_code = ?",
            (product_code,),
        )
        row = await cursor.fetchone()
        if row:
            await _refresh_metadata_index_for_item(
                db,
                row["id"],
                seiyuu_jp=seiyuu_jp,
                seiyuu_en=seiyuu_en,
                tags_jp=tags_jp,
                tags_en=tags_en,
                custom_tags=_safe_json_list(row["custom_tags"]),
            )
        await db.commit()
    finally:
        await db.close()


async def set_item_is_manual(product_code: str, value: bool = True) -> None:
    """Flag (or unflag) an item as a manual / non-DLsite custom entry. Manual items
    are skipped by the DLsite metadata-fetch job (see run_fetch_metadata), so their
    bundled metadata is never clobbered by a doomed DLsite lookup."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE items SET is_manual = ?, updated_at = ? WHERE product_code = ?",
            (1 if value else 0, datetime.now().isoformat(), product_code),
        )
        await db.commit()
    finally:
        await db.close()


def _basename_lower(p) -> str:
    """Separator-agnostic, lowercased basename (items store DLsite files as basenames but
    manual entries store full paths; normalize both for comparison)."""
    return str(p or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


async def get_all_claimed_basenames() -> set[str]:
    """Basenames of every file already claimed by an item OR a tokuten. The scanner uses
    this to keep files that belong to an existing entry (especially manual/custom MAN-*
    items and tokutens) out of the unmatched list, out of the per-scan bundled-package
    peek, and out of the codeless folder/loose-archive import.

    items.files holds two shapes: plain strings (scan/manual flows) and
    {filename, path, size} dicts (tokuten folder-scan flow) — both are handled.
    tokutens.local_path (stub tokutens from the tokuten path scan) is claimed too."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT files FROM items WHERE files IS NOT NULL AND files != '[]'")
        rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT local_path FROM tokutens WHERE local_path IS NOT NULL AND local_path != ''"
        )
        tokuten_paths = [r[0] for r in await cursor.fetchall()]
    finally:
        await db.close()
    out: set[str] = set()
    for row in rows:
        try:
            for f in json.loads(row[0] or "[]"):
                if isinstance(f, dict):
                    f = f.get("path") or f.get("filename")
                if f:
                    out.add(_basename_lower(f))
        except Exception:
            continue
    for p in tokuten_paths:
        out.add(_basename_lower(p))
    return out


async def get_claimed_local_paths() -> set[str]:
    """Lowercased absolute paths owned by tokutens (local_path points at the
    tokuten's folder or archive on disk). The drama-CD scan checks these so a
    folder that IS a tokuten doesn't get re-imported as a manual drama CD."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT local_path FROM tokutens WHERE local_path IS NOT NULL AND local_path != ''"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    out: set[str] = set()
    for (p,) in rows:
        try:
            out.add(str(Path(p).resolve()).lower())
        except OSError:
            out.add(str(p).strip().lower())
    return out


async def get_unmatched_file_keys() -> set[tuple[str, int]]:
    """(filepath_lower, file_size) for every currently-recorded unmatched file. Snapshotted
    before a rescan so an already-seen, package-less archive isn't re-peeked every time -
    only genuinely new files (never seen, not owned by an item) get the bundled-package peek."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT filepath, file_size FROM unmatched_files")
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return {(str(fp or "").lower(), int(sz or 0)) for fp, sz in rows}


async def update_item_user_data(item_id: int, data: dict):
    """Patch any subset of mutable item fields. Covers both pure user-data
    (rating/favorite/notes/custom_tags) and the metadata fields the user can
    hand-edit on a manually-created card (title/circle/release_date/etc., plus
    the seiyuu/tags JSON arrays). The metadata-index tables are refreshed
    whenever any indexed field changes."""
    db = await get_db()
    try:
        fields = []
        values = []
        # translation_status is auto-derived from transcript/translation runs
        # and is not user-editable here.
        scalar_allowed = {
            "rating", "favorite", "notes",
            "title", "title_en", "circle", "release_date", "age_rating",
            "description", "description_en",
            "listen_status",
        }
        json_allowed = {
            "custom_tags", "seiyuu", "seiyuu_en", "tags", "tags_en",
        }
        index_dirty = False
        indexed_overrides: dict[str, list] = {}

        # Virtual field: `archive_path` — an absolute path on disk for a
        # manual item. We translate it into items.files plus derived
        # total_size / file_count / file_format columns. Manual items use this
        # to point at either:
        #   - a .7z/.zip/.rar/.tar archive the scanner wouldn't have picked up
        #     (stored as a single-element files=[abs_path]), or
        #   - a FOLDER of already-extracted loose audio (every audio file
        #     inside is stored as an absolute-path entry, so the extractor
        #     indexes them in place without copying).
        # The extractor's archive resolver treats absolute-path entries in
        # items.files as direct hits.
        if "archive_path" in data and data["archive_path"] is not None:
            raw = str(data["archive_path"]).strip()
            if raw:
                from pathlib import Path as _Path
                p = _Path(raw)
                files_value = [raw]
                total_size = 0
                file_count = 1
                file_format: list[str] = []
                try:
                    if p.is_dir():
                        import asyncio as _asyncio
                        from config import AUDIO_EXTENSIONS as _AUDIO_EXTS

                        def _enum_audio() -> tuple:
                            # Bounded walk: archive_path comes from the client,
                            # so a drive root here must not become a runaway
                            # recursive scan that blocks the event loop.
                            out = []
                            visited = 0
                            for fp in p.rglob("*"):
                                visited += 1
                                if visited > 50000 or len(out) >= 5000:
                                    break
                                if fp.is_file() and fp.suffix.lower() in _AUDIO_EXTS:
                                    out.append(fp)
                            out.sort()
                            size = 0
                            for fp in out:
                                try:
                                    size += fp.stat().st_size
                                except OSError:
                                    pass
                            return out, size

                        audio, total_audio_size = await _asyncio.to_thread(_enum_audio)
                        if audio:
                            files_value = [str(fp) for fp in audio]
                            file_count = len(audio)
                            total_size = total_audio_size
                            file_format = sorted({
                                fp.suffix.lower().lstrip(".") for fp in audio if fp.suffix
                            })
                    elif p.is_file():
                        total_size = p.stat().st_size
                        suf = p.suffix.lower().lstrip(".")
                        if suf:
                            file_format = [suf]
                except Exception:
                    pass
                fields.append("files = ?")
                values.append(json.dumps(files_value, ensure_ascii=False))
                fields.append("file_count = ?")
                values.append(file_count)
                fields.append("total_size = ?")
                values.append(total_size)
                if file_format:
                    fields.append("file_format = ?")
                    values.append(json.dumps(file_format, ensure_ascii=False))
            else:
                # Explicit empty string clears the archive linkage.
                fields.append("files = ?")
                values.append("[]")
                fields.append("file_count = ?")
                values.append(0)
                fields.append("total_size = ?")
                values.append(0)

        for key, val in data.items():
            if key in scalar_allowed:
                if key == "listen_status":
                    if val is None or val == "":
                        val = "backlog"
                    if val not in _DRAMA_CD_LISTEN_STATUSES:
                        raise ValueError(f"Invalid listen_status: {val!r}")
                fields.append(f"{key} = ?")
                values.append(val)
            elif key in json_allowed:
                normalized = _safe_json_list(val)
                fields.append(f"{key} = ?")
                values.append(json.dumps(normalized, ensure_ascii=False))
                indexed_overrides[key] = normalized
                index_dirty = True

        if not fields:
            return

        fields.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        values.append(item_id)

        await db.execute(
            f"UPDATE items SET {', '.join(fields)} WHERE id = ?",
            values
        )
        # Mirror title/title_en onto the linked tokutens row when this item
        # is a tokuten_audio. The reverse mirror (tokutens.title → items.title)
        # already lives in routers/tokutens.py; without this side, edits made
        # via the items detail panel leave tokutens.title stale, which surfaces
        # as "[New Tokuten]" in the game-detail linked-tokutens list.
        if "title" in data or "title_en" in data:
            tk_cursor = await db.execute(
                "SELECT tokuten_id FROM items WHERE id = ?", (item_id,)
            )
            tk_row = await tk_cursor.fetchone()
            if tk_row and tk_row["tokuten_id"]:
                tk_fields = []
                tk_values = []
                if "title" in data:
                    tk_fields.append("title = ?")
                    tk_values.append(data["title"])
                if "title_en" in data:
                    tk_fields.append("title_en = ?")
                    tk_values.append(data["title_en"])
                tk_fields.append("updated_at = ?")
                tk_values.append(datetime.now().isoformat())
                tk_values.append(tk_row["tokuten_id"])
                await db.execute(
                    f"UPDATE tokutens SET {', '.join(tk_fields)} WHERE id = ?",
                    tk_values,
                )
        if index_dirty:
            cursor = await db.execute(
                "SELECT seiyuu, seiyuu_en, tags, tags_en, custom_tags FROM items WHERE id = ?",
                (item_id,),
            )
            row = await cursor.fetchone()
            if row:
                await _refresh_metadata_index_for_item(
                    db,
                    item_id,
                    seiyuu_jp=indexed_overrides.get("seiyuu", _safe_json_list(row["seiyuu"])),
                    seiyuu_en=indexed_overrides.get("seiyuu_en", _safe_json_list(row["seiyuu_en"])),
                    tags_jp=indexed_overrides.get("tags", _safe_json_list(row["tags"])),
                    tags_en=indexed_overrides.get("tags_en", _safe_json_list(row["tags_en"])),
                    custom_tags=indexed_overrides.get("custom_tags", _safe_json_list(row["custom_tags"])),
                )
        await db.commit()
    finally:
        await db.close()


async def set_item_cover(item_id: int, cover_local: str, cover_url: str | None = None) -> dict | None:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """UPDATE items SET
                cover_local = ?, cover_url = ?, updated_at = ?
            WHERE id = ?""",
            (cover_local, cover_url, now, item_id),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def override_product_code(item_id: int, new_code: str) -> dict | None:
    """Change an item's product code, clear its metadata, and mark as verified."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()

        # Get old cover_local path to delete the file
        cursor = await db.execute("SELECT cover_local FROM items WHERE id = ?", (item_id,))
        old_row = await cursor.fetchone()
        old_cover = old_row[0] if old_row else None

        # Check for conflict with existing item
        cursor = await db.execute(
            "SELECT id FROM items WHERE product_code = ? AND id != ?",
            (new_code, item_id)
        )
        if await cursor.fetchone():
            return None  # Conflict

        await db.execute(
            """UPDATE items SET
                product_code = ?, confidence = 'verified',
                title = NULL, title_en = NULL, circle = NULL, description = NULL, description_en = NULL,
                cover_url = NULL, cover_local = NULL, seiyuu = '[]', seiyuu_en = '[]', tags = '[]', tags_en = '[]',
                series = NULL, release_date = NULL, age_rating = NULL,
                metadata_raw = NULL, metadata_date = NULL,
                updated_at = ?
            WHERE id = ?""",
            (new_code, now, item_id)
        )
        cursor = await db.execute("SELECT custom_tags FROM items WHERE id = ?", (item_id,))
        custom_row = await cursor.fetchone()
        await _refresh_metadata_index_for_item(
            db,
            item_id,
            seiyuu_jp=[],
            seiyuu_en=[],
            tags_jp=[],
            tags_en=[],
            custom_tags=_safe_json_list(custom_row["custom_tags"]) if custom_row else [],
        )
        await db.commit()

        # Delete old cover file if it exists
        if old_cover:
            try:
                old_cover_path = COVERS_DIR / Path(old_cover).name
                if old_cover_path.exists():
                    old_cover_path.unlink()
            except Exception as e:
                pass  # Don't fail if file cleanup fails

        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def set_confidence_verified(item_id: int) -> dict | None:
    """Mark an item's product code as verified. Stores original confidence before verification."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()

        # Get current confidence before updating
        cursor = await db.execute(
            "SELECT confidence FROM items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None

        current_confidence = row[0]

        # Only update if not already verified
        if current_confidence != 'verified':
            await db.execute(
                """UPDATE items SET
                    confidence = 'verified', original_confidence = ?,
                    updated_at = ?
                WHERE id = ?""",
                (current_confidence, now, item_id)
            )
            await db.commit()

        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def revert_confidence(item_id: int) -> dict | None:
    """Revert an item's confidence from 'verified' back to its original level."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()

        # Get the original_confidence value
        cursor = await db.execute(
            "SELECT original_confidence FROM items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None  # No original confidence to restore

        original_confidence = row[0]

        # Restore original confidence and clear original_confidence
        await db.execute(
            """UPDATE items SET
                confidence = ?, original_confidence = NULL,
                updated_at = ?
            WHERE id = ?""",
            (original_confidence, now, item_id)
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_all_items(
    sort="scan_date", order="desc", search=None,
    seiyuu=None, tag=None, custom_tag=None,
    translation_status=None, favorite=None,
    has_metadata=None, confidence=None, lang="jp",
    include_tokutens=False, only_tokutens=False,
    is_manual=None,
    listen_status=None, listen_statuses=None,
    tokuten_kind=None, tokuten_source=None,
    limit=500, offset=0
) -> list[dict]:
    db = await get_db()
    try:
        if search:
            # Use FTS
            query = """
                SELECT items.* FROM items
                JOIN items_fts ON items.id = items_fts.rowid
                WHERE items_fts MATCH ?
            """
            params = [search]
        else:
            query = "SELECT * FROM items WHERE 1=1"
            params = []

        # Drama CDs subtab is the default surface; tokutens stay hidden
        # unless the caller asks for them. only_tokutens is the Tokutens
        # subtab's read.
        if only_tokutens:
            query += " AND items.kind = 'tokuten_audio'"
        elif include_tokutens:
            query += " AND items.kind IN ('drama_cd', 'tokuten_audio')"
        else:
            query += " AND items.kind = 'drama_cd'"

        if seiyuu:
            seiyuu_terms = seiyuu if isinstance(seiyuu, list) else [seiyuu]
            seiyuu_terms = [s.strip() for s in seiyuu_terms if s and s.strip()]
            if seiyuu_terms:
                clauses = []
                for _ in seiyuu_terms:
                    if lang == "en":
                        clauses.append(
                            """EXISTS (
                                SELECT 1 FROM item_seiyuu s
                                WHERE s.item_id = items.id
                                  AND s.lang IN ('en', 'jp')
                                  AND s.name LIKE ?
                            )"""
                        )
                    else:
                        clauses.append(
                            """EXISTS (
                                SELECT 1 FROM item_seiyuu s
                                WHERE s.item_id = items.id
                                  AND s.lang = 'jp'
                                  AND s.name LIKE ?
                            )"""
                        )
                # AND between terms — picking "Yuki Kaji" + "Mamoru Miyano"
                # returns CDs that feature BOTH, not either.
                query += " AND (" + " AND ".join(clauses) + ")"
                for term in seiyuu_terms:
                    params.append(f"%{term}%")
        if tag:
            tag_terms = tag if isinstance(tag, list) else [tag]
            tag_terms = [t.strip() for t in tag_terms if t and t.strip()]
            if tag_terms:
                tag_clauses = []
                for _ in tag_terms:
                    if lang == "en":
                        tag_clauses.append(
                            """EXISTS (
                                SELECT 1 FROM item_tags t
                                WHERE t.item_id = items.id
                                  AND (
                                    (t.source = 'dlsite' AND t.lang IN ('en', 'jp'))
                                    OR (t.source = 'custom' AND t.lang = 'all')
                                  )
                                  AND t.name LIKE ?
                            )"""
                        )
                    else:
                        tag_clauses.append(
                            """EXISTS (
                                SELECT 1 FROM item_tags t
                                WHERE t.item_id = items.id
                                  AND (
                                    (t.source = 'dlsite' AND t.lang = 'jp')
                                    OR (t.source = 'custom' AND t.lang = 'all')
                                  )
                                  AND t.name LIKE ?
                            )"""
                        )
                # AND between tags — Binaural + Boy returns CDs tagged with
                # BOTH (intersection), not either (union).
                query += " AND (" + " AND ".join(tag_clauses) + ")"
                for term in tag_terms:
                    params.append(f"%{term}%")
        if custom_tag:
            query += (
                """ AND EXISTS (
                    SELECT 1 FROM item_tags t
                    WHERE t.item_id = items.id
                      AND t.source = 'custom'
                      AND t.lang = 'all'
                      AND t.name LIKE ?
                )"""
            )
            params.append(f"%{custom_tag}%")
        if translation_status:
            query += " AND translation_status = ?"
            params.append(translation_status)
        if favorite is not None:
            query += " AND favorite = ?"
            params.append(1 if favorite else 0)
        if has_metadata is not None:
            if has_metadata:
                query += " AND title IS NOT NULL"
            else:
                query += " AND title IS NULL"
        if confidence:
            query += " AND confidence = ?"
            params.append(confidence)
        if is_manual is True:
            query += " AND items.is_manual = 1"
        elif is_manual is False:
            query += " AND items.is_manual = 0"
        # listen_status: multi-value form takes precedence (for stat-pill
        # filtering), falls back to single-value query param.
        if listen_statuses:
            valid_listen = [s for s in listen_statuses if s in _DRAMA_CD_LISTEN_STATUSES]
            if valid_listen:
                placeholders = ",".join(["?"] * len(valid_listen))
                query += f" AND items.listen_status IN ({placeholders})"
                params.extend(valid_listen)
        elif listen_status and listen_status in _DRAMA_CD_LISTEN_STATUSES:
            query += " AND items.listen_status = ?"
            params.append(listen_status)
        # Tokutens-subtab-only filters. items.tokuten_id is the join key;
        # use an EXISTS so we don't introduce row multiplication and so
        # drama-CD items aren't accidentally matched.
        if tokuten_kind:
            query += """ AND EXISTS (
                SELECT 1 FROM tokutens tk
                WHERE tk.id = items.tokuten_id AND tk.kind = ?
            )"""
            params.append(tokuten_kind)
        if tokuten_source:
            query += """ AND EXISTS (
                SELECT 1 FROM tokutens tk
                WHERE tk.id = items.tokuten_id AND tk.shop = ?
            )"""
            params.append(tokuten_source)

        # Validate and map sort keys
        valid_sorts = {
            "release_date",
            "title",
            "rating",
            "scan_date",
            "created_at",
            "updated_at",
            "product_code",
            "confidence",
            "translation_status",
            "listen_status",
        }
        if sort not in valid_sorts:
            sort = "scan_date"
        order_dir = "DESC" if order == "desc" else "ASC"
        tie_breaker = "id DESC" if order_dir == "DESC" else "id ASC"

        if sort == "title":
            sort_column = "title_en" if lang == "en" else "title"
            query += f" ORDER BY {sort_column} IS NULL, {sort_column} COLLATE NOCASE {order_dir}, {tie_breaker}"
        elif sort == "confidence":
            # verified > high > low for DESC, reversed for ASC
            query += (
                f" ORDER BY CASE confidence "
                f"WHEN 'verified' THEN 3 WHEN 'high' THEN 2 WHEN 'low' THEN 1 ELSE 0 END {order_dir}, "
                f"{tie_breaker}"
            )
        elif sort == "translation_status":
            # translated > transcribed > not_translated for DESC, reversed for ASC
            query += (
                f" ORDER BY CASE translation_status "
                f"WHEN 'translated' THEN 4 "
                f"WHEN 'transcribed' THEN 3 "
                f"WHEN 'extracted' THEN 2 "
                f"WHEN 'not_translated' THEN 1 "
                f"ELSE 0 END {order_dir}, "
                f"{tie_breaker}"
            )
        else:
            query += f" ORDER BY {sort} IS NULL, {sort} {order_dir}, {tie_breaker}"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        # Get total count
        count_query = query.split("ORDER BY")[0].replace("SELECT items.*", "SELECT COUNT(*)", 1).replace("SELECT *", "SELECT COUNT(*)", 1)
        count_params = params[:-2]  # remove limit/offset
        count_cursor = await db.execute(count_query, count_params)
        total = (await count_cursor.fetchone())[0]

        items = [dict(row) for row in rows]
        if lang == "en":
            for item in items:
                if item.get("title_en"):
                    item["title_display"] = item["title_en"]
                else:
                    item["title_display"] = item.get("title")
        else:
            for item in items:
                item["title_display"] = item.get("title")

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        await db.close()


async def get_item(item_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_item_by_product_code(product_code: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM items WHERE product_code = ? LIMIT 1",
            (product_code,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_item_glossary(item_id: int) -> str | None:
    """Return the per-item translator glossary, or None if the item is missing."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT glossary FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return row["glossary"] or ""
    finally:
        await db.close()


async def set_item_glossary(item_id: int, glossary: str) -> bool:
    """Write the per-item translator glossary. Returns False if item missing."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "UPDATE items SET glossary = ?, updated_at = ? WHERE id = ?",
            (str(glossary or ""), now, item_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def set_item_manual_track_count(item_id: int, count: int | None) -> bool:
    """Set or clear the user's manual track-count override. Pass None to
    revert to the auto-derived value (group count)."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "UPDATE items SET manual_track_count = ?, updated_at = ? WHERE id = ?",
            (count, now, item_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def set_item_description_en(item_id: int, description_en: str) -> None:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """UPDATE items
               SET description_en = ?, updated_at = ?
               WHERE id = ?""",
            (description_en, now, item_id),
        )
        await db.commit()
    finally:
        await db.close()


async def set_item_english_text(item_id: int, title_en: str | None, description_en: str | None) -> dict | None:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """UPDATE items
               SET title_en = ?, description_en = ?, updated_at = ?
               WHERE id = ?""",
            (title_en, description_en, now, item_id),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def set_item_english_metadata(
    item_id: int,
    *,
    title_en: str | None,
    description_en: str | None,
    seiyuu_en: list[str] | None = None,
) -> dict | None:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        normalized_seiyuu_en = _safe_json_list(seiyuu_en or [])
        await db.execute(
            """UPDATE items
               SET title_en = ?, description_en = ?, seiyuu_en = ?, updated_at = ?
               WHERE id = ?""",
            (title_en, description_en, json.dumps(normalized_seiyuu_en, ensure_ascii=False), now, item_id),
        )

        cursor = await db.execute(
            "SELECT seiyuu, tags, tags_en, custom_tags FROM items WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        if row:
            await _refresh_metadata_index_for_item(
                db,
                item_id,
                seiyuu_jp=_safe_json_list(row["seiyuu"]),
                seiyuu_en=normalized_seiyuu_en,
                tags_jp=_safe_json_list(row["tags"]),
                tags_en=_safe_json_list(row["tags_en"]),
                custom_tags=_safe_json_list(row["custom_tags"]),
            )

        await db.commit()
        cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        updated = await cursor.fetchone()
        return dict(updated) if updated else None
    finally:
        await db.close()


# CJK detection: Hiragana, Katakana, the kana punctuation block, CJK Unified
# Ideographs (+ Extension A), and the fullwidth/halfwidth forms block. A string
# containing any of these is treated as a raw JP name, not a Latin romanization.
_CJK_RE = re.compile(
    "[　-〿"   # CJK symbols & punctuation (incl. ideographic space)
    "぀-ヿ"    # Hiragana + Katakana
    "㐀-䶿"    # CJK Unified Ideographs Extension A
    "一-鿿"    # CJK Unified Ideographs
    "＀-￯]"   # Halfwidth & Fullwidth Forms
)


def _is_real_romanization(en, jp=None) -> bool:
    """True when ``en`` looks like an actual Latin-script romanization rather
    than a copy of the JP name. The importer stores seiyuu_en as a copy of the
    JP names when DLsite's EN page carries no English voice-actor spelling, so
    we reject anything that still contains CJK characters or simply equals the
    JP counterpart."""
    if not isinstance(en, str):
        return False
    en = en.strip()
    if not en or _CJK_RE.search(en):
        return False
    if jp is not None and isinstance(jp, str) and en == jp.strip():
        return False
    return True


async def _build_seiyuu_romanization_map(db: aiosqlite.Connection) -> dict[str, str]:
    """Map exact JP seiyuu name -> a known EN romanization, harvested from the
    rest of the library. Two sources, in increasing priority:

      1. Positionally-paired ``items.seiyuu`` / ``items.seiyuu_en`` where the EN
         slot is a real romanization. When several romanizations exist for one
         JP name the most frequently used wins (alphabetical tiebreak keeps it
         deterministic).
      2. The user-curated ``seiyuu_aliases`` table (canonical_jp ->
         canonical_en), which always overrides the harvested guess.

    Keys are matched exactly (case-sensitive) so two distinct people can never
    be conflated."""
    counts: dict[str, dict[str, int]] = {}
    cursor = await db.execute(
        """SELECT seiyuu, seiyuu_en FROM items
           WHERE seiyuu IS NOT NULL AND seiyuu_en IS NOT NULL
             AND seiyuu != '[]' AND seiyuu_en != '[]'"""
    )
    for row in await cursor.fetchall():
        try:
            jp = json.loads(row["seiyuu"] or "[]")
            en = json.loads(row["seiyuu_en"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(jp, list) or not isinstance(en, list):
            continue
        if len(jp) != len(en):
            continue  # can't safely pair when lengths differ
        for jp_name, en_name in zip(jp, en):
            if not isinstance(jp_name, str) or not isinstance(en_name, str):
                continue
            jp_name = jp_name.strip()
            en_name = en_name.strip()
            if not jp_name or not _is_real_romanization(en_name, jp_name):
                continue
            bucket = counts.setdefault(jp_name, {})
            bucket[en_name] = bucket.get(en_name, 0) + 1

    mapping: dict[str, str] = {}
    for jp_name, variants in counts.items():
        best = sorted(variants.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        mapping[jp_name] = best

    # Curated aliases override harvested guesses (may not exist on old DBs).
    try:
        cursor = await db.execute(
            "SELECT canonical_jp, canonical_en FROM seiyuu_aliases "
            "WHERE canonical_jp IS NOT NULL AND TRIM(canonical_jp) != ''"
        )
        for row in await cursor.fetchall():
            jp_name = (row["canonical_jp"] or "").strip()
            en_name = (row["canonical_en"] or "").strip()
            if jp_name and _is_real_romanization(en_name, jp_name):
                mapping[jp_name] = en_name
    except Exception:
        pass

    return mapping


def _fill_known_romanizations(seiyuu_jp: list, seiyuu_en, romaji_map: dict) -> tuple[list, int]:
    """Return a seiyuu_en list aligned to ``seiyuu_jp`` where every slot that is
    missing or is just a JP copy gets filled from ``romaji_map`` on an exact JP
    match. Slots that already hold a real romanization are left untouched.
    Returns ``(new_en_list, filled_count)``."""
    seiyuu_en = list(seiyuu_en or [])
    filled = 0
    out: list[str] = []
    for idx, jp_name in enumerate(seiyuu_jp):
        current = seiyuu_en[idx].strip() if idx < len(seiyuu_en) and isinstance(seiyuu_en[idx], str) else ""
        if _is_real_romanization(current, jp_name):
            out.append(current)
            continue
        known = romaji_map.get(jp_name.strip()) if isinstance(jp_name, str) else None
        if known:
            out.append(known)
            filled += 1
        else:
            # Preserve whatever was there (JP copy or empty) so nothing is lost.
            out.append(current or (jp_name if isinstance(jp_name, str) else ""))
    return out, filled


async def backfill_seiyuu_romanizations(dry_run: bool = True) -> dict:
    """Sweep every item and fill seiyuu_en slots that are missing or are JP
    copies using romanizations already known elsewhere in the library (exact
    JP match). When ``dry_run`` is False the changes are written to
    ``items.seiyuu_en`` and the normalized ``item_seiyuu`` rows are refreshed.
    Returns a summary plus a per-item preview."""
    db = await get_db()
    try:
        romaji_map = await _build_seiyuu_romanization_map(db)
        preview: list[dict] = []
        items_changed = 0
        names_filled = 0
        if romaji_map:
            cursor = await db.execute(
                """SELECT id, product_code, title, title_en, seiyuu, seiyuu_en,
                          tags, tags_en, custom_tags
                   FROM items
                   WHERE seiyuu IS NOT NULL AND seiyuu != '[]'"""
            )
            rows = await cursor.fetchall()
            now = datetime.now().isoformat()
            for row in rows:
                seiyuu_jp = _safe_json_list(row["seiyuu"])
                if not seiyuu_jp:
                    continue
                seiyuu_en = _safe_json_list(row["seiyuu_en"])
                new_en, filled = _fill_known_romanizations(seiyuu_jp, seiyuu_en, romaji_map)
                if not filled:
                    continue
                items_changed += 1
                names_filled += filled
                if len(preview) < 200:
                    preview.append({
                        "item_id": row["id"],
                        "product_code": row["product_code"],
                        "title": row["title_en"] or row["title"],
                        "before": seiyuu_en,
                        "after": new_en,
                    })
                if not dry_run:
                    await db.execute(
                        "UPDATE items SET seiyuu_en = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(new_en, ensure_ascii=False), now, row["id"]),
                    )
                    await _refresh_metadata_index_for_item(
                        db,
                        row["id"],
                        seiyuu_jp=seiyuu_jp,
                        seiyuu_en=new_en,
                        tags_jp=_safe_json_list(row["tags"]),
                        tags_en=_safe_json_list(row["tags_en"]),
                        custom_tags=_safe_json_list(row["custom_tags"]),
                    )
            if not dry_run and items_changed:
                await db.commit()
        return {
            "dry_run": dry_run,
            "known_names": len(romaji_map),
            "items_changed": items_changed,
            "names_filled": names_filled,
            "preview": preview,
        }
    finally:
        await db.close()


async def get_seiyuu_inventory() -> dict:
    """Return every distinct EN seiyuu name with use counts, the canonical
    name they currently map to (if any), and the set of JP names that
    appear positionally aligned with each EN name across items.

    JP names come from pairing ``items.seiyuu`` (JP) with ``items.seiyuu_en``
    (EN) at matching indexes — when the two arrays line up, position N is
    the same person. When lengths don't match (rare data-quality issue),
    we skip that item so we don't pair the wrong JP name with an EN one."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT name, COUNT(DISTINCT item_id) AS use_count
               FROM item_seiyuu
               WHERE lang = 'en' AND name IS NOT NULL AND TRIM(name) != ''
               GROUP BY name COLLATE NOCASE
               ORDER BY name COLLATE NOCASE"""
        )
        names = [{"name": row["name"], "use_count": int(row["use_count"])} for row in await cursor.fetchall()]

        # Build EN-name -> set(JP names) by walking items where both arrays
        # are non-empty and the same length.
        cursor = await db.execute(
            """SELECT seiyuu, seiyuu_en FROM items
               WHERE seiyuu IS NOT NULL AND seiyuu_en IS NOT NULL
                 AND seiyuu != '[]' AND seiyuu_en != '[]'"""
        )
        en_to_jp: dict[str, set[str]] = {}
        for row in await cursor.fetchall():
            try:
                jp = json.loads(row["seiyuu"] or "[]")
                en = json.loads(row["seiyuu_en"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(jp, list) or not isinstance(en, list):
                continue
            if len(jp) != len(en):
                continue  # can't safely pair when lengths differ
            for jp_name, en_name in zip(jp, en):
                if not isinstance(jp_name, str) or not isinstance(en_name, str):
                    continue
                jp_name = jp_name.strip()
                en_name = en_name.strip()
                if not jp_name or not en_name:
                    continue
                en_to_jp.setdefault(en_name, set()).add(jp_name)

        cursor = await db.execute("SELECT alias, canonical_en, canonical_jp FROM seiyuu_aliases")
        alias_rows = await cursor.fetchall()
        alias_map = {row["alias"]: {"canonical_en": row["canonical_en"], "canonical_jp": row["canonical_jp"]} for row in alias_rows}

        for entry in names:
            mapped = alias_map.get(entry["name"])
            if mapped:
                entry["canonical_en"] = mapped["canonical_en"]
                entry["canonical_jp"] = mapped["canonical_jp"]
            else:
                entry["canonical_en"] = None
                entry["canonical_jp"] = None
            # Sort JP names for stable display.
            entry["jp_names"] = sorted(en_to_jp.get(entry["name"], set()))
        return {"seiyuu": names, "alias_count": len(alias_map)}
    finally:
        await db.close()


def _normalize_seiyuu_name(name: str) -> str:
    """Fold a romanization variant down to a canonical key for grouping.
    Conservative — we'd rather under-group than wrongly merge two real
    different people. Used by ``suggest_seiyuu_groups``."""
    if not name:
        return ""
    s = name.strip().lower()
    # Strip apostrophes, periods, hyphens that often differ between styles.
    s = re.sub(r"[\.\'’\-]", "", s)
    # Macrons → plain vowels (Ichijō → ichijo).
    s = (s
         .replace("ā", "a").replace("ī", "i").replace("ū", "u")
         .replace("ē", "e").replace("ō", "o"))
    # Romanization-variant folds applied to each whitespace-separated token
    # so we don't fold across name boundaries.
    parts: list[str] = []
    for token in s.split():
        token = re.sub(r"oo|ou|oh", "o", token)
        token = re.sub(r"uu", "u", token)
        token = re.sub(r"ee|ei", "e", token)
        # Collapse any remaining duplicate consonant pairs.
        token = re.sub(r"([bcdfghjklmnpqrstvwxz])\1", r"\1", token)
        parts.append(token)
    # Token-sort so "Hirame Ichijo" and "Ichijo Hirame" land in the same bucket.
    parts.sort()
    return " ".join(p for p in parts if p)


async def suggest_seiyuu_groups() -> list[dict]:
    """Group EN seiyuu names that look like the same person under different
    romanizations. Each member also carries its associated JP names (from
    positional pairing of items.seiyuu / items.seiyuu_en) so the UI can
    show them and the user can verify it's the same person before merging."""
    inventory = await get_seiyuu_inventory()

    buckets: dict[str, list[dict]] = {}
    for entry in inventory.get("seiyuu", []):
        name = entry.get("name") or ""
        key = _normalize_seiyuu_name(name)
        if not key:
            continue
        buckets.setdefault(key, []).append({
            "name": name,
            "use_count": int(entry.get("use_count") or 0),
            "jp_names": list(entry.get("jp_names") or []),
        })

    groups: list[dict] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: -m["use_count"])
        # Aggregate JP names across the whole group so the UI can show
        # "all the JP names this group covers" in one place.
        all_jp: set[str] = set()
        for m in members:
            for jp in m.get("jp_names") or []:
                all_jp.add(jp)
        groups.append({
            "key": key,
            "members": members,
            "total_uses": sum(m["use_count"] for m in members),
            "jp_names": sorted(all_jp),
            # If every JP name is identical across members, flag that as a
            # high-confidence match. Different JP names = different people.
            "jp_consistent": len(all_jp) <= 1,
        })
    groups.sort(key=lambda g: (not g["jp_consistent"], -g["total_uses"]))
    return groups


async def merge_seiyuu_aliases(canonical_en: str, aliases: list[str], canonical_jp: str | None = None, dry_run: bool = True) -> dict:
    """Replace every occurrence of any alias inside items.seiyuu_en with the
    canonical name. Records the alias→canonical mapping in seiyuu_aliases so
    future imports normalize automatically. ``dry_run`` previews the change
    without writing."""
    canonical_en = (canonical_en or "").strip()
    if not canonical_en:
        raise ValueError("canonical_en must be non-empty")
    alias_set = {a.strip() for a in (aliases or []) if a and a.strip() and a.strip() != canonical_en}
    if not alias_set:
        return {"items_touched": 0, "items_changed": 0, "aliases_recorded": 0, "preview": [], "dry_run": dry_run}

    db = await get_db()
    try:
        # Pull every item that mentions any of the aliases in seiyuu_en. SQLite
        # JSON1 is overkill for what is effectively substring matching; fall
        # back to LIKE since aliases are stored as JSON-quoted strings inside
        # an array.
        items_to_check: list[dict] = []
        for alias in alias_set:
            cursor = await db.execute(
                """SELECT id, product_code, title, title_en, seiyuu_en
                   FROM items
                   WHERE seiyuu_en LIKE ?""",
                (f'%"{alias}"%',),
            )
            rows = await cursor.fetchall()
            for row in rows:
                items_to_check.append(dict(row))

        # Dedupe by id, keep the first row we saw per item.
        seen_ids: set[int] = set()
        unique_items: list[dict] = []
        for row in items_to_check:
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            unique_items.append(row)

        items_changed = 0
        preview: list[dict] = []
        now = datetime.now().isoformat()
        for item in unique_items:
            try:
                arr = json.loads(item.get("seiyuu_en") or "[]")
                if not isinstance(arr, list):
                    arr = []
            except (TypeError, ValueError):
                arr = []
            new_arr: list[str] = []
            replaced = False
            for name in arr:
                if not isinstance(name, str):
                    continue
                if name in alias_set:
                    if canonical_en not in new_arr:
                        new_arr.append(canonical_en)
                    replaced = True
                else:
                    if name not in new_arr:
                        new_arr.append(name)
            if not replaced:
                continue  # nothing to do for this item
            items_changed += 1
            preview.append({
                "item_id": int(item["id"]),
                "product_code": item.get("product_code"),
                "title": item.get("title_en") or item.get("title"),
                "before": arr,
                "after": new_arr,
            })
            if not dry_run:
                await db.execute(
                    "UPDATE items SET seiyuu_en = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_arr, ensure_ascii=False), now, int(item["id"])),
                )
                # Update the normalized item_seiyuu rows too so filters stay in sync.
                await db.execute(
                    "DELETE FROM item_seiyuu WHERE item_id = ? AND lang = 'en'",
                    (int(item["id"]),),
                )
                for name in new_arr:
                    await db.execute(
                        "INSERT OR IGNORE INTO item_seiyuu (item_id, lang, name) VALUES (?, 'en', ?)",
                        (int(item["id"]), name),
                    )

        aliases_recorded = 0
        if not dry_run:
            for alias in alias_set:
                await db.execute(
                    """INSERT INTO seiyuu_aliases (alias, canonical_en, canonical_jp, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(alias) DO UPDATE SET
                           canonical_en = excluded.canonical_en,
                           canonical_jp = COALESCE(excluded.canonical_jp, seiyuu_aliases.canonical_jp),
                           updated_at = excluded.updated_at""",
                    (alias, canonical_en, canonical_jp, now, now),
                )
                aliases_recorded += 1
            await db.commit()

        return {
            "dry_run": dry_run,
            "canonical_en": canonical_en,
            "canonical_jp": canonical_jp,
            "aliases": sorted(alias_set),
            "items_touched": len(unique_items),
            "items_changed": items_changed,
            "aliases_recorded": aliases_recorded,
            "preview": preview[:30],
        }
    finally:
        await db.close()


async def delete_seiyuu_alias(alias: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM seiyuu_aliases WHERE alias = ?", (alias,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_unique_seiyuu(lang: str = "jp") -> list[str]:
    db = await get_db()
    try:
        if lang == "en":
            cursor = await db.execute(
                "SELECT DISTINCT name FROM item_seiyuu WHERE lang IN ('en', 'jp') ORDER BY name COLLATE NOCASE"
            )
        else:
            cursor = await db.execute(
                "SELECT DISTINCT name FROM item_seiyuu WHERE lang = 'jp' ORDER BY name COLLATE NOCASE"
            )
        rows = await cursor.fetchall()
        return [row["name"] for row in rows if row["name"]]
    finally:
        await db.close()


async def get_unique_tags(lang: str = "jp") -> dict:
    db = await get_db()
    try:
        if lang == "en":
            dlsite_cursor = await db.execute(
                """SELECT DISTINCT name FROM item_tags
                   WHERE source = 'dlsite' AND lang IN ('en', 'jp')
                   ORDER BY name COLLATE NOCASE"""
            )
        else:
            dlsite_cursor = await db.execute(
                """SELECT DISTINCT name FROM item_tags
                   WHERE source = 'dlsite' AND lang = 'jp'
                   ORDER BY name COLLATE NOCASE"""
            )
        custom_cursor = await db.execute(
            """SELECT DISTINCT name FROM item_tags
               WHERE source = 'custom' AND lang = 'all'
               ORDER BY name COLLATE NOCASE"""
        )
        dlsite_tags = [row["name"] for row in await dlsite_cursor.fetchall() if row["name"]]
        custom_tags = [row["name"] for row in await custom_cursor.fetchall() if row["name"]]
        return {
            "dlsite_tags": dlsite_tags,
            "custom_tags": custom_tags,
        }
    finally:
        await db.close()


async def delete_item_by_id(item_id: int) -> bool:
    """Delete an item by ID. Useful for removing duplicate/orphaned entries.
    For tokuten_audio items, also removes the parent tokutens row and its
    media_assets so deleting from the Library list cleans up fully — there's
    no other entry point for tokuten cleanup now that the dedicated subtab
    is gone."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cover_local, kind, tokuten_id, product_code FROM items WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        if row and row["cover_local"]:
            try:
                cover_path = Path(row["cover_local"])
                if cover_path.name:
                    file_to_delete = COVERS_DIR / cover_path.name
                    if file_to_delete.exists():
                        file_to_delete.unlink()
            except Exception:
                pass  # Ignore file deletion errors

        # Collect this item's extracted-audio folder(s) so we can remove them after the
        # DB rows are gone - otherwise a deleted item leaves orphaned extracted/<CODE>/
        # dirs on disk that a re-add would then "reuse" (stale tracks). Use the paths the
        # tracks actually recorded, plus the canonical extracted/<CODE> location.
        extract_dirs: set[str] = set()
        try:
            ec = await db.execute(
                "SELECT DISTINCT extract_root FROM pipeline_tracks WHERE item_id = ? AND extract_root IS NOT NULL",
                (item_id,),
            )
            for er in await ec.fetchall():
                if er["extract_root"]:
                    extract_dirs.add(er["extract_root"])
        except Exception:
            pass
        code = (row["product_code"] if row else None) or ""
        if code.strip():
            extract_dirs.add(str(PIPELINE_EXTRACT_DIR / code.strip().upper()))

        await db.execute("DELETE FROM items WHERE id = ?", (item_id,))

        if row and row["kind"] == "tokuten_audio" and row["tokuten_id"]:
            tokuten_id = row["tokuten_id"]
            await db.execute(
                "DELETE FROM media_assets WHERE parent_kind = 'tokuten' AND parent_id = ?",
                (tokuten_id,),
            )
            await db.execute("DELETE FROM tokutens WHERE id = ?", (tokuten_id,))

        await db.commit()

        # Remove the extracted audio folder(s) now that the rows are gone. Safety: only
        # delete paths that resolve INSIDE PIPELINE_EXTRACT_DIR, so a malformed
        # extract_root can never rmtree something outside the pipeline workspace.
        try:
            base = PIPELINE_EXTRACT_DIR.resolve()
        except Exception:
            base = PIPELINE_EXTRACT_DIR
        for d in extract_dirs:
            try:
                p = Path(d).resolve()
                if p != base and base in p.parents and p.exists():
                    shutil.rmtree(p, ignore_errors=True)
                    logger.info(f"Removed extracted folder for deleted item {item_id}: {p}")
            except Exception as exc:
                logger.warning(f"Could not remove extracted folder {d} for item {item_id}: {exc}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete item {item_id}: {e}")
        return False
    finally:
        await db.close()


async def get_stats() -> dict:
    """Drama CD stats only. Tokutens have their own breakdown via
    get_tokuten_stats(); games via routers/games.py:/api/games/stats."""
    db = await get_db()
    try:
        total = (await (await db.execute(
            "SELECT COUNT(*) FROM items WHERE kind = 'drama_cd'"
        )).fetchone())[0]
        with_metadata = (await (await db.execute(
            "SELECT COUNT(*) FROM items WHERE kind = 'drama_cd' AND title IS NOT NULL"
        )).fetchone())[0]
        favorites = (await (await db.execute(
            "SELECT COUNT(*) FROM items WHERE kind = 'drama_cd' AND favorite = 1"
        )).fetchone())[0]
        unmatched = (await (await db.execute("SELECT COUNT(*) FROM unmatched_files")).fetchone())[0]

        status_cursor = await db.execute(
            """SELECT translation_status, COUNT(*) as cnt FROM items
               WHERE kind = 'drama_cd' GROUP BY translation_status"""
        )
        statuses = {row["translation_status"]: row["cnt"] for row in await status_cursor.fetchall()}

        listen_cursor = await db.execute(
            """SELECT listen_status, COUNT(*) as cnt FROM items
               WHERE kind = 'drama_cd' GROUP BY listen_status"""
        )
        listen_statuses = {row["listen_status"]: row["cnt"] for row in await listen_cursor.fetchall()}

        return {
            "total_items": total,
            "with_metadata": with_metadata,
            "without_metadata": total - with_metadata,
            "favorites": favorites,
            "unmatched_files": unmatched,
            "translation_statuses": statuses,
            "listen_statuses": listen_statuses,
        }
    finally:
        await db.close()


async def get_tokuten_stats() -> dict:
    """Sidebar stats for the Tokutens subtab: total, favorited count, and
    a per-kind breakdown. Counts are scoped to tokutens that have a paired
    items row — orphan tokutens (left over from cancelled blank-create
    flows) don't show up in the library grid, so they shouldn't inflate
    the stats either. Favorite uses the items row's flag."""
    db = await get_db()
    try:
        # Use the items table as the source of truth for "what's visible".
        cursor = await db.execute(
            "SELECT COUNT(*) AS c FROM items WHERE kind = 'tokuten_audio'"
        )
        total = int((await cursor.fetchone())["c"])

        cursor = await db.execute(
            """SELECT COUNT(*) AS c FROM items
               WHERE kind = 'tokuten_audio' AND favorite = 1"""
        )
        favorited = int((await cursor.fetchone())["c"])

        cursor = await db.execute(
            """SELECT tk.kind AS kind, COUNT(*) AS c
               FROM tokutens tk
               JOIN items it ON it.tokuten_id = tk.id
               WHERE it.kind = 'tokuten_audio'
               GROUP BY tk.kind"""
        )
        rows = await cursor.fetchall()
        by_kind = {r["kind"]: int(r["c"]) for r in rows}

        return {"total": total, "favorited": favorited, "by_kind": by_kind}
    finally:
        await db.close()


async def add_unmatched_file(filename: str, filepath: str, file_size: int = 0):
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT OR IGNORE INTO unmatched_files (filename, filepath, file_size, scan_date)
            VALUES (?, ?, ?, ?)""",
            (filename, filepath, file_size, now)
        )
        await db.commit()
    finally:
        await db.close()


async def get_unmatched_files() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM unmatched_files ORDER BY filename")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def clear_unmatched_files():
    db = await get_db()
    try:
        await db.execute("DELETE FROM unmatched_files")
        await db.commit()
    finally:
        await db.close()


def _json_dumps(value, default):
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False)


async def create_job(job_type: str, status: str = "running", metadata: dict | None = None) -> int:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """INSERT INTO jobs (
                job_type, status, metadata_json, started_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                job_type,
                status,
                _json_dumps(metadata, {}),
                now if status in {"running", "paused", "stopping"} else None,
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def update_job(job_id: int, **fields):
    if not fields:
        return
    db = await get_db()
    try:
        set_parts = []
        values = []
        json_fields = {"errors_json", "error_summary_json", "result_json", "metadata_json"}
        for key, value in fields.items():
            if key in json_fields:
                value = _json_dumps(value, [] if key == "errors_json" else {})
            set_parts.append(f"{key} = ?")
            values.append(value)
        set_parts.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        values.append(job_id)

        # Log if current field is being updated
        if "current" in fields:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[DB UPDATE] Job {job_id}: current='{fields['current']}'")

        await db.execute(f"UPDATE jobs SET {', '.join(set_parts)} WHERE id = ?", values)
        await db.commit()
    finally:
        await db.close()


async def append_job_event(job_id: int, level: str, message: str, data: dict | None = None):
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO job_events (job_id, level, message, data_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (job_id, level, message, _json_dumps(data, {}), now),
        )
        await db.commit()
    finally:
        await db.close()


def _row_to_job_dict(row) -> dict:
    job = dict(row)
    for field, fallback in (
        ("errors_json", []),
        ("error_summary_json", {}),
        ("result_json", {}),
        ("metadata_json", {}),
    ):
        value = job.get(field)
        try:
            job[field] = json.loads(value) if value else fallback
        except Exception:
            job[field] = fallback
    return job


async def get_latest_job(job_type: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE job_type = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (job_type,),
        )
        row = await cursor.fetchone()
        return _row_to_job_dict(row) if row else None
    finally:
        await db.close()


async def get_job(job_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return _row_to_job_dict(row) if row else None
    finally:
        await db.close()


async def get_job_events(job_id: int, limit: int = 20) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, job_id, level, message, data_json, created_at
               FROM job_events
               WHERE job_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (job_id, max(1, min(limit, 100))),
        )
        rows = await cursor.fetchall()
        events = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json") or "{}")
            except Exception:
                item["data"] = {}
            events.append(item)
        events.reverse()
        return events
    finally:
        await db.close()


async def get_recent_jobs(limit: int = 20) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM jobs
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (max(1, min(limit, 100)),),
        )
        rows = await cursor.fetchall()
        return [_row_to_job_dict(row) for row in rows]
    finally:
        await db.close()


async def get_latest_job_for_item(job_type: str, item_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM jobs
               WHERE job_type = ?
               ORDER BY created_at DESC, id DESC
               LIMIT 200""",
            (job_type,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            job = _row_to_job_dict(row)
            metadata = job.get("metadata_json") or {}
            if int(metadata.get("item_id", -1)) == int(item_id):
                return job
        return None
    finally:
        await db.close()


async def replace_pipeline_tracks_for_item(item_id: int, tracks: list[dict]) -> int:
    """Reconcile the on-disk track list with the DB without nuking IDs.

    The previous implementation did DELETE-then-INSERT, which cascaded into
    pipeline_transcript_runs / pipeline_translation_runs / active_outputs
    and **destroyed any existing transcripts/translations** on every
    re-extraction. That also left the UI staring at stale track IDs and
    showing "track not found" until it caught up.

    The new behavior upserts by ``(item_id, track_path)``: existing rows
    keep their id (so transcripts and translations stay attached), missing
    rows get inserted, and only paths that genuinely disappeared from disk
    get deleted (cascading those orphans is the right call — they really
    are gone)."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT id, track_path FROM pipeline_tracks WHERE item_id = ?",
            (item_id,),
        )
        existing_by_path: dict[str, int] = {
            row["track_path"]: int(row["id"]) for row in await cursor.fetchall()
        }
        new_paths: set[str] = {
            t["track_path"] for t in tracks if t.get("track_path")
        }

        # Rename-remap pass: a renamed/moved folder changes every track_path
        # while the filenames stay the same. Without this, those rows would
        # fall through to delete+insert and the deletes would CASCADE away
        # their transcripts/translations even though the audio still exists.
        # Claim each stale row whose filename uniquely matches an incoming
        # path's filename (unique on BOTH sides, so duplicate basenames in
        # different subfolders never cross-wire) and just move it.
        unmatched_new = [p for p in new_paths if p not in existing_by_path]
        unmatched_old = [p for p in existing_by_path if p not in new_paths]
        old_by_name: dict[str, list[str]] = {}
        for p in unmatched_old:
            old_by_name.setdefault(Path(p).name.lower(), []).append(p)
        new_by_name: dict[str, list[str]] = {}
        for p in unmatched_new:
            new_by_name.setdefault(Path(p).name.lower(), []).append(p)
        for name, new_candidates in new_by_name.items():
            old_candidates = old_by_name.get(name) or []
            if len(new_candidates) == 1 and len(old_candidates) == 1:
                old_p, new_p = old_candidates[0], new_candidates[0]
                row_id = existing_by_path.pop(old_p)
                await db.execute(
                    "UPDATE pipeline_tracks SET track_path = ?, updated_at = ? WHERE id = ?",
                    (new_p, now, row_id),
                )
                existing_by_path[new_p] = row_id

        upserted = 0
        for track in tracks:
            tp = track.get("track_path")
            if not tp:
                continue
            existing_id = existing_by_path.get(tp)
            if existing_id is not None:
                # Update only the columns that re-indexing might legitimately
                # refresh (codec/duration/etc. on a re-probe). Title and
                # title_en stay put — track_titles_translate populated them
                # and we shouldn't wipe that on every re-extract.
                await db.execute(
                    """UPDATE pipeline_tracks SET
                          archive_path = ?,
                          extract_root = ?,
                          track_index = ?,
                          duration_seconds = COALESCE(?, duration_seconds),
                          codec = COALESCE(?, codec),
                          sample_rate = COALESCE(?, sample_rate),
                          channels = COALESCE(?, channels),
                          status = ?,
                          error = ?,
                          updated_at = ?
                       WHERE id = ?""",
                    (
                        track.get("archive_path"),
                        track.get("extract_root"),
                        track.get("track_index", 0),
                        track.get("duration_seconds"),
                        track.get("codec"),
                        track.get("sample_rate"),
                        track.get("channels"),
                        track.get("status", "indexed"),
                        track.get("error"),
                        now,
                        existing_id,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO pipeline_tracks (
                        item_id, archive_path, extract_root, track_path, track_index, title,
                        duration_seconds, codec, sample_rate, channels, status, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item_id,
                        track.get("archive_path"),
                        track.get("extract_root"),
                        track.get("track_path"),
                        track.get("track_index", 0),
                        track.get("title"),
                        track.get("duration_seconds"),
                        track.get("codec"),
                        track.get("sample_rate"),
                        track.get("channels"),
                        track.get("status", "indexed"),
                        track.get("error"),
                        now,
                        now,
                    ),
                )
            upserted += 1

        # Remove rows whose audio is no longer on disk. Their transcripts and
        # translations cascade — that's correct, the source file is gone.
        stale_paths = [p for p in existing_by_path if p not in new_paths]
        for stale in stale_paths:
            await db.execute(
                "DELETE FROM pipeline_tracks WHERE item_id = ? AND track_path = ?",
                (item_id, stale),
            )
        await db.commit()
    finally:
        await db.close()
    await recompute_translation_status_for_item(item_id)
    return upserted


async def get_all_pipeline_track_paths() -> list[dict]:
    """Every track's (extract_root, track_path) across ALL items, regardless
    of item kind. Used by the workspace-orphan scan — routing that scan
    through get_all_items() once silently dropped tokuten/manual items
    (default kind filter = drama_cd only), making their extractions look
    orphaned and purgeable."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT extract_root, track_path FROM pipeline_tracks"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_pipeline_tracks(item_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM pipeline_transcript_runs r
                         WHERE r.track_id = t.id) AS transcript_run_count,
                      (SELECT COUNT(*) FROM pipeline_translation_runs r
                         WHERE r.track_id = t.id) AS translation_run_count
               FROM pipeline_tracks t
               WHERE t.item_id = ?
               ORDER BY t.track_index ASC, t.id ASC""",
            (item_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


import re as _grouping_re

# Lower rank = preferred for transcription. Lossless first, then lossy.
_CODEC_RANK = {
    "flac": 0, "wav": 1, "aiff": 2, "aif": 2, "alac": 3,
    "m4a": 4, "ogg": 5, "opus": 6, "mp3": 7, "aac": 8,
}
_DURATION_TOLERANCE_SEC = 2.0  # Allows MP3 encoder padding / silent-frame skew

# Common Japanese drama-CD "alternate mix" tokens. Used in two places:
# 1) as a filename suffix (`Track01_NoSE.flac` → no-sfx variant)
# 2) anywhere in an ancestor folder name (`Disc1_NoSE/Track01.flac` → no-sfx)
_VARIANT_TOKEN = (
    r"no[\s_\-]?se|"
    r"no[\s_\-]?sfx|"
    r"no[\s_\-]?effects?|"
    r"se[\s_\-]?less|"
    r"se[\s_\-]?off|"
    r"voice[\s_\-]?only|"
    r"no[\s_\-]?vocal|"
    r"no[\s_\-]?bgm|"
    r"bgm[\s_\-]?less"
)
_VARIANT_SUFFIX_RE = _grouping_re.compile(
    r"^(.+?)[\s_\-\.]?(" + _VARIANT_TOKEN + r")$",
    _grouping_re.IGNORECASE,
)
# Word-boundary match for ancestor folder names so we catch `Disc1_NoSE`,
# `Voice Only`, `BGMLess`, etc. even when they're not at the very end.
_VARIANT_FOLDER_RE = _grouping_re.compile(
    r"(^|[\s_\-\.])(" + _VARIANT_TOKEN + r")($|[\s_\-\.])",
    _grouping_re.IGNORECASE,
)

# Japanese variant markers — common in drama-CD folder names like
# `03_wav（SE無し）`. Matched as a plain substring; CJK has no spaces and the
# tokens are distinctive enough that no word-boundary check is needed.
_VARIANT_FOLDER_JP_RE = _grouping_re.compile(
    r"SE[\s ]?無し|"
    r"SE[\s ]?なし|"
    r"SE[\s ]?抜き|"
    r"効果音[\s ]?無し|"
    r"効果音[\s ]?なし|"
    r"声のみ|"
    r"ボイスのみ|"
    r"BGM[\s ]?無し|"
    r"BGM[\s ]?なし",
    _grouping_re.IGNORECASE,
)


def _track_filename_stem(track: dict) -> str:
    """Filename stem (no extension) lowercased. Doesn't strip variant suffixes."""
    path = str(track.get("track_path") or "")
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = _grouping_re.sub(r"\.[^.]+$", "", name).strip().lower()
    return stem or f"track-{track.get('id')}"


def _track_ancestor_folders(track: dict) -> list[str]:
    """All folder names along the track's path, lowercased — used for variant detection."""
    path = str(track.get("track_path") or "").replace("\\", "/")
    parts = [p.strip().lower() for p in path.split("/") if p.strip()]
    # Last component is the filename; everything else is folders.
    return parts[:-1] if len(parts) > 1 else []


def _classify_variant(track_or_stem, folders: list[str] | None = None) -> tuple[str, str]:
    """
    Returns (canonical_stem, 'sfx' | 'no-sfx').

    Detection order:
      1. Filename stem suffix (e.g. `Track01_NoSE`).
      2. Any ancestor folder containing a variant token (e.g. `Disc1_NoSE/`).

    Accepts either a track dict (preferred — picks up folders automatically) or
    a bare stem string for backwards-compat callers.
    """
    if isinstance(track_or_stem, dict):
        stem = _track_filename_stem(track_or_stem)
        if folders is None:
            folders = _track_ancestor_folders(track_or_stem)
    else:
        stem = str(track_or_stem or "")
        folders = folders or []

    stem_norm = stem.strip().lower()

    # 1) filename suffix — Latin variant token at the end of the stem
    m = _VARIANT_SUFFIX_RE.match(stem_norm)
    if m:
        canonical = m.group(1).rstrip(" _-.")
        return canonical or stem, "no-sfx"

    # 2) Japanese marker anywhere in the filename stem (e.g. `誰だと思う？（SE無し）`)
    if _VARIANT_FOLDER_JP_RE.search(stem_norm):
        return stem, "no-sfx"

    # 3) folder name anywhere in the path (Latin or JP markers)
    for f in folders:
        if _VARIANT_FOLDER_RE.search(f) or _VARIANT_FOLDER_JP_RE.search(f):
            return stem, "no-sfx"

    return stem, "sfx"


def _track_canonical_key(track: dict) -> str:
    """Canonical group key — variant tokens (filename suffix or folder) stripped."""
    canonical, _ = _classify_variant(track)
    return canonical


def _track_group_key(track: dict) -> str:
    """Backwards-compatible alias for the canonical group key."""
    return _track_canonical_key(track)


def _codec_rank(codec: str | None) -> int:
    return _CODEC_RANK.get(str(codec or "").lower(), 99)


_VARIANT_RANK_SFX_FIRST = {"sfx": 0, "no-sfx": 1}
_VARIANT_RANK_NOSFX_FIRST = {"no-sfx": 0, "sfx": 1}


def _are_likely_siblings(a: str, b: str) -> bool:
    """
    Structural sibling test (no regex / no token list — durable across naming
    conventions, including non-Latin punctuation and CJK).

    Two stems are siblings if any of these hold:
      1. LCP fully spans the shorter stem  → one is prefix of the other
         (e.g. 'Track01' ↔ 'Track01_NoSE', 'tr01_trackname' ↔ 'tr01_trackname（SE無し）')
      2. LCP ends on a non-alphanumeric char
         (e.g. 'track01_fullver' ↔ 'track01_nose' — both diverge after the same '_')
      3. The character right after the LCP (in either stem) is non-alphanumeric
         — covers cases where the divergence point itself is on a punctuation char.

    Rejects 'track01' ↔ 'track010' / 'track02' (digit→digit, no boundary).
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True

    lcp = 0
    while lcp < len(a) and lcp < len(b) and a[lcp] == b[lcp]:
        lcp += 1
    if lcp == 0:
        return False

    short_len = min(len(a), len(b))
    # Rule 1: one stem is a (full) prefix of the other — always siblings.
    if lcp == short_len:
        return True

    # Rules 2 & 3 require substantial overlap (≥ 50% of shorter stem) so a
    # shared category prefix like '【特典】' between unrelated bonus tracks
    # doesn't trigger a false merge.
    if lcp * 2 < short_len:
        return False

    # Rule 2: LCP ends on a non-alphanumeric char (token boundary).
    if not a[lcp - 1].isalnum():
        return True
    # Rule 3: char right after the LCP (in either stem) is non-alphanumeric.
    next_a = a[lcp] if lcp < len(a) else ""
    next_b = b[lcp] if lcp < len(b) else ""
    if next_a and not next_a.isalnum():
        return True
    if next_b and not next_b.isalnum():
        return True
    return False


# Backwards-compat alias: callers that used the old prefix-only test still work,
# they now just get the more permissive structural test.
_is_prefix_sibling = _are_likely_siblings


async def get_pipeline_track_groups(item_id: int, preferred_variant: str = "sfx") -> list[dict]:
    """
    Group tracks that are the same recording in different formats and/or mixes.

    Strategy:
      1. Cluster by duration (±1s). Same recording always has same length to
         within ms — that's the strong signal.
      2. Within each cluster, label variants by regex match against the
         filename suffix or any ancestor folder name. Anything that doesn't
         match a known variant token (`no_se`, `seless`, `voice_only`, ...)
         stays 'sfx'.

    Note: we deliberately do NOT sub-cluster by stem similarity. Drama CDs
    rarely have two unrelated recordings of identical length, and the
    same-recording-different-name case (e.g. `tr07_OP.flac` paired with
    `mp3_seless/tr07.mp3`) needs duration to be the only gate to merge.
    """
    tracks = await get_pipeline_tracks(item_id)

    # --- Step 1: stem-based sibling clustering via union-find ---
    # Duration was unreliable: no-SFX mixes commonly drop SFX-only segments
    # entirely, so the same recording can differ by 5-15s between mixes. Using
    # stem similarity as the primary signal — pairwise _are_likely_siblings,
    # transitively closed via union-find so all reachable siblings cluster.
    n = len(tracks)
    parent = list(range(n))
    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def _union(x: int, y: int) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[rx] = ry

    stems = [_track_filename_stem(t) for t in tracks]
    for i in range(n):
        for j in range(i + 1, n):
            if _are_likely_siblings(stems[i], stems[j]):
                _union(i, j)

    by_root: dict[int, list[dict]] = {}
    for i in range(n):
        by_root.setdefault(_find(i), []).append(tracks[i])
    sub_clusters: list[list[dict]] = list(by_root.values())

    # --- Step 2a: classify variants up front ---
    # The duration gate below is variant-aware, so every track needs its
    # SFX/no-SFX label before bucketing (step 3 reuses these).
    for t in tracks:
        _, _variant_label = _classify_variant(t)
        t["_variant"] = _variant_label

    # --- Step 2b: split each stem-cluster into duration buckets ---
    # Within a stem cluster, SAME-variant tracks more than 2s apart are
    # independent recordings (main track + freetalk + bonus all named
    # "RJ12345*") and split into separate groups. A DIFFERENT-variant track
    # may join a bucket within a looser 30s: circles trim silent/SFX-only
    # segments out of the no-SFX mix, so it commonly runs 2-15s shorter than
    # its SFX sibling — splitting those was the bug, not the feature (the
    # whole point of variants is that they're one group). FLAC/MP3 of the
    # same source still cluster tightly (codec padding is sub-50ms).
    _DURATION_TOLERANCE = 2.0
    _VARIANT_DURATION_TOLERANCE = 30.0
    bucketed_clusters: list[list[dict]] = []
    for sub in sub_clusters:
        buckets: list[tuple[float | None, str, list[dict]]] = []
        for t in sub:
            d = t.get("duration_seconds")
            v = t.get("_variant") or "sfx"
            placed = False
            if isinstance(d, (int, float)):
                for b in buckets:
                    if not isinstance(b[0], (int, float)):
                        continue
                    tol = _DURATION_TOLERANCE if v == b[1] else _VARIANT_DURATION_TOLERANCE
                    if abs(b[0] - d) <= tol:
                        b[2].append(t)
                        placed = True
                        break
            if not placed:
                buckets.append((d, v, [t]))
        bucketed_clusters.extend(b[2] for b in buckets)
    sub_clusters = bucketed_clusters

    # --- Step 3: emit groups (variants already labeled in step 2a) ---
    groups: list[dict] = []
    for sub in sub_clusters:
        stems = [_track_filename_stem(t) for t in sub]

        canonical_stem = min(stems, key=len)
        for t in sub:
            t["_canonical_stem"] = canonical_stem

        variant_rank = (
            _VARIANT_RANK_NOSFX_FIRST if preferred_variant == "no-sfx" else _VARIANT_RANK_SFX_FIRST
        )
        sorted_tracks = sorted(
            sub,
            key=lambda t: (
                variant_rank.get(t.get("_variant") or "sfx", 99),
                _codec_rank(t.get("codec")),
                t.get("id") or 0,
            ),
        )
        preferred = sorted_tracks[0]
        seen_codec, codecs = set(), []
        for tr in sorted_tracks:
            c = str(tr.get("codec") or "").upper() or "?"
            if c not in seen_codec:
                codecs.append(c); seen_codec.add(c)
        seen_variant, variants = set(), []
        for tr in sorted_tracks:
            v = tr.get("_variant") or "sfx"
            if v not in seen_variant:
                variants.append(v); seen_variant.add(v)
        groups.append({
            "group_key": canonical_stem,
            "preferred_track_id": preferred["id"],
            "tracks": sorted_tracks,
            "codecs": codecs,
            "variants": variants,
            "transcript_run_count": sum(int(t.get("transcript_run_count") or 0) for t in sub),
            "translation_run_count": sum(int(t.get("translation_run_count") or 0) for t in sub),
            "min_track_index": min((t.get("track_index") or 0) for t in sub),
        })
    groups.sort(key=lambda g: (g["min_track_index"], g["preferred_track_id"]))
    for g in groups:
        g.pop("min_track_index", None)
    return groups


async def get_pipeline_track(track_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM pipeline_tracks WHERE id = ?", (track_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def set_track_title_en(track_id: int, title_en: str | None) -> bool:
    """Persist a translated track title. Pass None to clear."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE pipeline_tracks SET title_en = ?, updated_at = ? WHERE id = ?",
            (title_en, datetime.now().isoformat(), track_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def bulk_set_track_titles_en(updates: list[tuple[int, str]]) -> int:
    """Batch-update title_en for many tracks at once."""
    if not updates:
        return 0
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        for track_id, title_en in updates:
            await db.execute(
                "UPDATE pipeline_tracks SET title_en = ?, updated_at = ? WHERE id = ?",
                (title_en, now, track_id),
            )
        await db.commit()
        return len(updates)
    finally:
        await db.close()


async def set_track_summary(track_id: int, summary_json: str):
    """Store the track summary JSON for context memory."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE pipeline_tracks SET track_summary_json = ?, updated_at = ? WHERE id = ?",
            (summary_json, datetime.now().isoformat(), track_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_previous_track_summaries(item_id: int, current_track_index: int, limit: int = 2) -> list[dict]:
    """
    Fetch summaries for the previous N tracks (N-1, N-2, ...) based on track_index.
    Returns list of dicts with {track_index, track_id, summary_json}.
    Ordered from most recent to oldest (N-1, N-2, ...).
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, track_index, track_summary_json
               FROM pipeline_tracks
               WHERE item_id = ? AND track_index < ? AND track_summary_json IS NOT NULL
               ORDER BY track_index DESC
               LIMIT ?""",
            (item_id, current_track_index, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "track_id": row["id"],
                "track_index": row["track_index"],
                "summary_json": row["track_summary_json"],
            }
            for row in rows
        ]
    finally:
        await db.close()


def _load_json_field(raw: str | None, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except Exception:
        return fallback


async def recompute_translation_status_for_item(item_id: int) -> str:
    """Derive items.translation_status from existing runs and persist it.

    Order: 'translated' > 'transcribed' > 'extracted' > 'not_translated'.
    'extracted' = audio has been unpacked (pipeline_tracks rows exist) but no
    transcripts yet; useful for filtering CDs you've prepared but haven't
    transcribed."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT 1 FROM pipeline_translation_runs r
               JOIN pipeline_tracks t ON t.id = r.track_id
               WHERE t.item_id = ? LIMIT 1""",
            (item_id,),
        )
        has_translation = await cursor.fetchone() is not None
        if has_translation:
            status = "translated"
        else:
            cursor = await db.execute(
                """SELECT 1 FROM pipeline_transcript_runs r
                   JOIN pipeline_tracks t ON t.id = r.track_id
                   WHERE t.item_id = ? LIMIT 1""",
                (item_id,),
            )
            has_transcript = await cursor.fetchone() is not None
            if has_transcript:
                status = "transcribed"
            else:
                cursor = await db.execute(
                    "SELECT 1 FROM pipeline_tracks WHERE item_id = ? LIMIT 1",
                    (item_id,),
                )
                has_tracks = await cursor.fetchone() is not None
                status = "extracted" if has_tracks else "not_translated"
        await db.execute(
            "UPDATE items SET translation_status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now().isoformat(), item_id),
        )
        await db.commit()
        return status
    finally:
        await db.close()


async def recompute_translation_status_for_track(track_id: int) -> str | None:
    """Look up the item_id for a track and recompute its status. Returns None if track is unknown."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT item_id FROM pipeline_tracks WHERE id = ?", (track_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        return None
    return await recompute_translation_status_for_item(int(row["item_id"]))


async def recompute_all_translation_statuses() -> dict:
    """One-shot backfill: recompute every item's translation_status. Returns counts per status."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM items")
        rows = await cursor.fetchall()
        ids = [int(r["id"]) for r in rows]
    finally:
        await db.close()
    counts = {"translated": 0, "transcribed": 0, "not_translated": 0}
    for item_id in ids:
        status = await recompute_translation_status_for_item(item_id)
        counts[status] = counts.get(status, 0) + 1
    return {"items": len(ids), "counts": counts}


async def create_transcript_run(
    track_id: int,
    language: str,
    source: str,
    engine: str | None,
    model: str | None,
    prompt: str | None,
    segments: list[dict],
    metadata: dict | None = None,
) -> int:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """INSERT INTO pipeline_transcript_runs
               (track_id, language, source, status, engine, model, prompt, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)""",
            (
                track_id,
                language,
                source,
                engine,
                model,
                prompt,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        run_id = cursor.lastrowid
        for seg in segments:
            await db.execute(
                """INSERT INTO pipeline_transcript_run_segments
                   (run_id, segment_index, start_seconds, end_seconds, text, confidence, meta_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    int(seg["segment_index"]),
                    float(seg["start_seconds"]),
                    float(seg["end_seconds"]),
                    str(seg["text"]),
                    seg.get("confidence"),
                    json.dumps(seg.get("meta") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        await db.commit()
    finally:
        await db.close()
    await recompute_translation_status_for_track(track_id)
    return run_id


async def replicate_transcript_run_to_siblings(run_id: int) -> list[int]:
    """
    Copy a transcript run + its segments to every sibling track in the same group
    (matching filename stem + duration ±1s, in the same item). Returns the list
    of newly-created run IDs. Idempotent-ish: if a sibling already has a run
    flagged with the same shared_from_run_id, that sibling is skipped.

    Called automatically after a successful auto-transcription so the user
    doesn't need to re-run whisper on FLAC and MP3 of the same audio.
    """
    db = await get_db()
    try:
        # Load the source run.
        cursor = await db.execute(
            "SELECT * FROM pipeline_transcript_runs WHERE id = ?",
            (run_id,),
        )
        src_run = await cursor.fetchone()
        if not src_run:
            return []
        src = dict(src_run)
        source_track_id = src["track_id"]

        # Find sibling tracks in same group (same item, same stem, dur ±1s).
        cursor = await db.execute(
            "SELECT * FROM pipeline_tracks WHERE id = ?",
            (source_track_id,),
        )
        src_track_row = await cursor.fetchone()
        if not src_track_row:
            return []
        src_track = dict(src_track_row)
        item_id = src_track["item_id"]
        src_stem = _track_filename_stem(src_track)
        src_dur = src_track.get("duration_seconds")

        cursor = await db.execute(
            "SELECT * FROM pipeline_tracks WHERE item_id = ? AND id != ?",
            (item_id, source_track_id),
        )
        all_other_tracks = [dict(r) for r in await cursor.fetchall()]
        siblings = []
        for t in all_other_tracks:
            # Stem similarity AND duration match (±2s) — codec variants (FLAC vs
            # MP3) of the same recording pass; trimmed no-SFX mixes that drop
            # SFX-only segments do NOT. Replicating a transcript across a
            # duration mismatch shifts every segment timestamp by the trimmed
            # amount, which drifts subtitles in the player.
            if not _are_likely_siblings(src_stem, _track_filename_stem(t)):
                continue
            t_dur = t.get("duration_seconds")
            if (
                isinstance(src_dur, (int, float))
                and isinstance(t_dur, (int, float))
                and abs(src_dur - t_dur) > 2.0
            ):
                continue
            siblings.append(t)
        if not siblings:
            return []

        # Load source segments.
        cursor = await db.execute(
            "SELECT * FROM pipeline_transcript_run_segments WHERE run_id = ? ORDER BY segment_index ASC",
            (run_id,),
        )
        src_segments = [dict(r) for r in await cursor.fetchall()]

        new_run_ids: list[int] = []
        now = datetime.now().isoformat()

        async def _ensure_sibling_active(track_id: int, run_id_for_track: int) -> None:
            """Make sure the sibling track points at its copied run as the
            active transcript. Without this, the Player tab loads sibling
            tracks (e.g. the FLAC variant when Whisper ran on the MP3) and
            sees `active_transcript_run_id = NULL` → "No transcript loaded"
            even though the row exists."""
            await db.execute(
                """INSERT INTO pipeline_track_active_outputs
                   (track_id, active_transcript_run_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       active_transcript_run_id = excluded.active_transcript_run_id,
                       updated_at = excluded.updated_at""",
                (track_id, run_id_for_track, now),
            )

        for sib in siblings:
            # Skip duplicating the run if this sibling already has a copy of
            # the same source run, but still ensure that copy is set active —
            # historical data may have the run row without an active pointer.
            cursor = await db.execute(
                """SELECT id FROM pipeline_transcript_runs
                   WHERE track_id = ?
                     AND json_extract(metadata_json, '$.shared_from_run_id') = ?""",
                (sib["id"], run_id),
            )
            existing = await cursor.fetchone()
            if existing:
                await _ensure_sibling_active(sib["id"], int(existing["id"]))
                continue

            # Merge a `shared_from_run_id` flag into the metadata so the UI
            # (and any future re-runs) can tell this is a copy.
            try:
                src_meta = json.loads(src.get("metadata_json") or "{}")
                if not isinstance(src_meta, dict):
                    src_meta = {}
            except (TypeError, ValueError):
                src_meta = {}
            src_meta = dict(src_meta)
            src_meta["shared_from_run_id"] = run_id
            src_meta["shared_from_track_id"] = source_track_id

            cursor = await db.execute(
                """INSERT INTO pipeline_transcript_runs
                   (track_id, language, source, status, engine, model, prompt, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sib["id"],
                    src["language"],
                    src["source"],
                    src["status"],
                    src["engine"],
                    src["model"],
                    src["prompt"],
                    json.dumps(src_meta, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            new_run_id = cursor.lastrowid
            for seg in src_segments:
                await db.execute(
                    """INSERT INTO pipeline_transcript_run_segments
                       (run_id, segment_index, start_seconds, end_seconds, text, confidence, meta_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_run_id,
                        seg["segment_index"],
                        seg["start_seconds"],
                        seg["end_seconds"],
                        seg["text"],
                        seg.get("confidence"),
                        seg.get("meta_json") or "{}",
                        now,
                        now,
                    ),
                )
            new_run_ids.append(new_run_id)
            await _ensure_sibling_active(sib["id"], new_run_id)

        await db.commit()
    finally:
        await db.close()

    # Refresh status flags on every sibling we touched (and the source).
    for sib in siblings:
        await recompute_translation_status_for_track(sib["id"])
    return new_run_ids


async def replicate_translation_run_to_siblings(run_id: int) -> list[int]:
    """Copy a translation run + its segments to every sibling track in the
    same group. Sibling tracks (FLAC/MP3 of the same audio) currently each
    get their own translation run, which doubles or triples LLM cost; this
    function lets one translation cover all variants the way transcripts
    already do.

    The new sibling translation_run is linked to the SIBLING's matching
    transcript_run (the one that was previously replicated from the
    source's transcript), not to the source's transcript_run_id, so each
    variant's row remains internally consistent."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM pipeline_translation_runs WHERE id = ?",
            (run_id,),
        )
        src_run_row = await cursor.fetchone()
        if not src_run_row:
            return []
        src_run = dict(src_run_row)
        source_track_id = src_run["track_id"]
        source_transcript_run_id = src_run["transcript_run_id"]

        cursor = await db.execute(
            "SELECT * FROM pipeline_tracks WHERE id = ?",
            (source_track_id,),
        )
        src_track_row = await cursor.fetchone()
        if not src_track_row:
            return []
        src_track = dict(src_track_row)
        item_id = src_track["item_id"]
        src_stem = _track_filename_stem(src_track)

        cursor = await db.execute(
            "SELECT * FROM pipeline_tracks WHERE item_id = ? AND id != ?",
            (item_id, source_track_id),
        )
        all_other = [dict(r) for r in await cursor.fetchall()]
        src_dur = src_track.get("duration_seconds")
        siblings = []
        for t in all_other:
            if not _are_likely_siblings(src_stem, _track_filename_stem(t)):
                continue
            # Skip cross-duration-bucket siblings — same reasoning as transcript
            # replication: trimmed no-SFX mixes are effectively a different
            # recording even if the filename stems match.
            t_dur = t.get("duration_seconds")
            if (
                isinstance(src_dur, (int, float))
                and isinstance(t_dur, (int, float))
                and abs(src_dur - t_dur) > 2.0
            ):
                continue
            siblings.append(t)
        if not siblings:
            return []

        cursor = await db.execute(
            "SELECT * FROM pipeline_translation_run_segments WHERE run_id = ? ORDER BY segment_index ASC",
            (run_id,),
        )
        src_segments = [dict(r) for r in await cursor.fetchall()]

        new_run_ids: list[int] = []
        now = datetime.now().isoformat()

        async def _ensure_sibling_active_translation(track_id: int, new_run_id: int) -> None:
            """Mirror of the transcript helper — make the sibling's
            active_translation_run_id point at its replicated copy."""
            await db.execute(
                """INSERT INTO pipeline_track_active_outputs
                   (track_id, active_translation_run_id, active_translation_target_language, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       active_translation_run_id = excluded.active_translation_run_id,
                       active_translation_target_language = excluded.active_translation_target_language,
                       updated_at = excluded.updated_at""",
                (track_id, new_run_id, src_run.get("target_language") or "en", now),
            )

        for sib in siblings:
            # Find the sibling's transcript run that was shared from the
            # SAME source transcript the translation is anchored on. Without
            # that, the sibling has no transcript matching the translation
            # segments, so we'd be linking to nothing meaningful.
            cursor = await db.execute(
                """SELECT id FROM pipeline_transcript_runs
                   WHERE track_id = ?
                     AND json_extract(metadata_json, '$.shared_from_run_id') = ?""",
                (sib["id"], source_transcript_run_id),
            )
            sib_transcript = await cursor.fetchone()
            if not sib_transcript:
                continue
            sib_transcript_run_id = int(sib_transcript["id"])

            # Idempotency: skip if this sibling already has a translation
            # copy linked to the same source translation run.
            cursor = await db.execute(
                """SELECT id FROM pipeline_translation_runs
                   WHERE track_id = ?
                     AND json_extract(metadata_json, '$.shared_from_run_id') = ?""",
                (sib["id"], run_id),
            )
            existing = await cursor.fetchone()
            if existing:
                await _ensure_sibling_active_translation(sib["id"], int(existing["id"]))
                continue

            try:
                src_meta = json.loads(src_run.get("metadata_json") or "{}")
                if not isinstance(src_meta, dict):
                    src_meta = {}
            except (TypeError, ValueError):
                src_meta = {}
            sib_meta = dict(src_meta)
            sib_meta["shared_from_run_id"] = run_id
            sib_meta["shared_from_track_id"] = source_track_id

            cursor = await db.execute(
                """INSERT INTO pipeline_translation_runs
                   (track_id, transcript_run_id, target_language, source, status,
                    engine, model, prompt, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sib["id"],
                    sib_transcript_run_id,
                    src_run.get("target_language") or "en",
                    src_run.get("source") or "auto",
                    src_run.get("status") or "completed",
                    src_run.get("engine"),
                    src_run.get("model"),
                    src_run.get("prompt"),
                    json.dumps(sib_meta, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            new_run_id = cursor.lastrowid
            for seg in src_segments:
                await db.execute(
                    """INSERT INTO pipeline_translation_run_segments
                       (run_id, segment_index, text, meta_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        new_run_id,
                        seg["segment_index"],
                        seg["text"],
                        seg.get("meta_json") or "{}",
                        now,
                        now,
                    ),
                )
            new_run_ids.append(new_run_id)
            await _ensure_sibling_active_translation(sib["id"], new_run_id)

        await db.commit()
    finally:
        await db.close()

    for sib in siblings:
        await recompute_translation_status_for_track(sib["id"])
    return new_run_ids


async def list_transcript_runs(track_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM pipeline_transcript_run_segments s WHERE s.run_id = r.id) AS segment_count
               FROM pipeline_transcript_runs r
               WHERE r.track_id = ?
               ORDER BY r.created_at DESC, r.id DESC""",
            (track_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata_json"] = _load_json_field(item.get("metadata_json"), {})
            result.append(item)
        return result
    finally:
        await db.close()


async def get_transcript_run(run_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM pipeline_transcript_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata_json"] = _load_json_field(item.get("metadata_json"), {})
        return item
    finally:
        await db.close()


async def get_transcript_segments(run_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM pipeline_transcript_run_segments
               WHERE run_id = ?
               ORDER BY segment_index ASC, id ASC""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["meta_json"] = _load_json_field(item.get("meta_json"), {})
            result.append(item)
        return result
    finally:
        await db.close()


async def create_translation_run(
    track_id: int,
    transcript_run_id: int,
    target_language: str,
    source: str,
    engine: str | None,
    model: str | None,
    prompt: str | None,
    segments: list[dict],
    metadata: dict | None = None,
) -> int:
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """INSERT INTO pipeline_translation_runs
               (track_id, transcript_run_id, target_language, source, status, engine, model, prompt, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)""",
            (
                track_id,
                transcript_run_id,
                target_language,
                source,
                engine,
                model,
                prompt,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        run_id = cursor.lastrowid
        for seg in segments:
            await db.execute(
                """INSERT INTO pipeline_translation_run_segments
                   (run_id, segment_index, text, meta_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    int(seg["segment_index"]),
                    str(seg["text"]),
                    json.dumps(seg.get("meta") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        await db.commit()
    finally:
        await db.close()
    await recompute_translation_status_for_track(track_id)
    return run_id


async def list_translation_runs(track_id: int, target_language: str | None = None) -> list[dict]:
    db = await get_db()
    try:
        query = (
            """SELECT r.*,
                      (SELECT COUNT(*) FROM pipeline_translation_run_segments s WHERE s.run_id = r.id) AS segment_count
               FROM pipeline_translation_runs r
               WHERE r.track_id = ?"""
        )
        params: list = [track_id]
        if target_language:
            query += " AND r.target_language = ?"
            params.append(target_language)
        query += " ORDER BY r.created_at DESC, r.id DESC"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata_json"] = _load_json_field(item.get("metadata_json"), {})
            result.append(item)
        return result
    finally:
        await db.close()


async def get_translation_run(run_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM pipeline_translation_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata_json"] = _load_json_field(item.get("metadata_json"), {})
        return item
    finally:
        await db.close()


async def get_translation_segments(run_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM pipeline_translation_run_segments
               WHERE run_id = ?
               ORDER BY segment_index ASC, id ASC""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["meta_json"] = _load_json_field(item.get("meta_json"), {})
            result.append(item)
        return result
    finally:
        await db.close()


async def backfill_missing_sibling_translations() -> dict:
    """For every "original" translation run (one that wasn't itself a sibling
    copy), make sure every sibling track has an equivalent copy. Idempotent
    — `replicate_translation_run_to_siblings` skips siblings already done."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id FROM pipeline_translation_runs
               WHERE COALESCE(json_extract(metadata_json, '$.shared_from_run_id'), 0) = 0
               ORDER BY id ASC"""
        )
        original_run_ids = [int(row["id"]) for row in await cursor.fetchall()]
    finally:
        await db.close()

    runs_processed = 0
    new_runs_total = 0
    for run_id in original_run_ids:
        try:
            new_ids = await replicate_translation_run_to_siblings(run_id)
            runs_processed += 1
            new_runs_total += len(new_ids)
        except Exception:
            # One bad run shouldn't stop the whole sweep.
            continue
    return {
        "runs_examined": len(original_run_ids),
        "runs_processed": runs_processed,
        "sibling_runs_created": new_runs_total,
    }


async def backfill_missing_active_transcripts() -> dict:
    """Find tracks that have at least one transcript run but no
    `active_transcript_run_id`, and set the most-recent run as active.
    Returns a summary so the caller can report how many rows were touched.

    Fixes the historical bug where shared (sibling-replicated) transcript
    runs were inserted without writing the `pipeline_track_active_outputs`
    row, so the Player tab couldn't find them."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT t.id AS track_id, MAX(r.id) AS latest_run_id
               FROM pipeline_tracks t
               JOIN pipeline_transcript_runs r ON r.track_id = t.id
               LEFT JOIN pipeline_track_active_outputs ao ON ao.track_id = t.id
               WHERE ao.active_transcript_run_id IS NULL
               GROUP BY t.id"""
        )
        rows = await cursor.fetchall()
        now = datetime.now().isoformat()
        updated_track_ids: list[int] = []
        for row in rows:
            tid = int(row["track_id"])
            run_id = int(row["latest_run_id"])
            await db.execute(
                """INSERT INTO pipeline_track_active_outputs
                   (track_id, active_transcript_run_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       active_transcript_run_id = excluded.active_transcript_run_id,
                       updated_at = excluded.updated_at""",
                (tid, run_id, now),
            )
            updated_track_ids.append(tid)
        await db.commit()
        return {
            "tracks_fixed": len(updated_track_ids),
            "track_ids": updated_track_ids,
        }
    finally:
        await db.close()


async def get_track_active_outputs(track_id: int) -> dict:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM pipeline_track_active_outputs WHERE track_id = ?",
            (track_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "track_id": track_id,
                "active_transcript_run_id": None,
                "active_translation_run_id": None,
                "active_translation_target_language": None,
            }
        return dict(row)
    finally:
        await db.close()


async def set_track_active_transcript(track_id: int, run_id: int) -> bool:
    run = await get_transcript_run(run_id)
    if not run or int(run["track_id"]) != int(track_id):
        return False
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO pipeline_track_active_outputs
               (track_id, active_transcript_run_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(track_id) DO UPDATE SET
                   active_transcript_run_id = excluded.active_transcript_run_id,
                   updated_at = excluded.updated_at""",
            (track_id, run_id, now),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def set_track_active_translation(track_id: int, run_id: int) -> bool:
    run = await get_translation_run(run_id)
    if not run or int(run["track_id"]) != int(track_id):
        return False
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO pipeline_track_active_outputs
               (track_id, active_translation_run_id, active_translation_target_language, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(track_id) DO UPDATE SET
                   active_translation_run_id = excluded.active_translation_run_id,
                   active_translation_target_language = excluded.active_translation_target_language,
                   updated_at = excluded.updated_at""",
            (track_id, run_id, run.get("target_language"), now),
        )
        await db.commit()
        return True
    finally:
        await db.close()


def _cover_name_from_cover_local(cover_local: str | None) -> str | None:
    if not cover_local:
        return None
    try:
        name = Path(cover_local).name
        return name or None
    except Exception:
        return None


async def get_integrity_report(sample_limit: int = 50) -> dict:
    db = await get_db()
    try:
        duplicate_cursor = await db.execute(
            """SELECT original_code, COUNT(*) AS cnt
               FROM items
               WHERE original_code IS NOT NULL AND TRIM(original_code) != ''
               GROUP BY original_code
               HAVING COUNT(*) > 1
               ORDER BY cnt DESC, original_code ASC"""
        )
        duplicate_rows = await duplicate_cursor.fetchall()
        duplicate_original_codes = [
            {"original_code": row["original_code"], "count": row["cnt"]}
            for row in duplicate_rows
        ]

        cover_cursor = await db.execute("SELECT id, product_code, cover_local FROM items WHERE cover_local IS NOT NULL AND TRIM(cover_local) != ''")
        cover_rows = await cover_cursor.fetchall()

        referenced_cover_names = set()
        missing_cover_items = []
        for row in cover_rows:
            cover_name = _cover_name_from_cover_local(row["cover_local"])
            if not cover_name:
                continue
            referenced_cover_names.add(cover_name)
            if not (COVERS_DIR / cover_name).exists():
                missing_cover_items.append(
                    {
                        "item_id": row["id"],
                        "product_code": row["product_code"],
                        "cover_local": row["cover_local"],
                    }
                )

        cover_files = [p.name for p in COVERS_DIR.glob("*") if p.is_file()] if COVERS_DIR.exists() else []
        stale_cover_files = sorted([name for name in cover_files if name not in referenced_cover_names])

        return {
            "duplicate_original_code_groups": len(duplicate_original_codes),
            "duplicate_original_codes": duplicate_original_codes[:sample_limit],
            "missing_cover_items_count": len(missing_cover_items),
            "missing_cover_items": missing_cover_items[:sample_limit],
            "stale_cover_files_count": len(stale_cover_files),
            "stale_cover_files": stale_cover_files[:sample_limit],
            "sample_limit": sample_limit,
        }
    finally:
        await db.close()


async def cleanup_stale_covers(dry_run: bool = True, sample_limit: int = 50) -> dict:
    report = await get_integrity_report(sample_limit=sample_limit)
    stale_files = report.get("stale_cover_files", [])
    deleted = []
    failed = []

    if not dry_run:
        for name in stale_files:
            try:
                path = COVERS_DIR / name
                if path.exists():
                    path.unlink()
                    deleted.append(name)
            except Exception:
                failed.append(name)

    return {
        "dry_run": dry_run,
        "candidate_count": report.get("stale_cover_files_count", 0),
        "candidates": stale_files,
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failed_count": len(failed),
        "failed": failed,
        "sample_limit": sample_limit,
    }


async def rebuild_metadata_indexes(sample_limit: int = 50) -> dict:
    db = await get_db()
    try:
        await db.execute("DELETE FROM item_seiyuu")
        await db.execute("DELETE FROM item_tags")
        cursor = await db.execute(
            "SELECT id, seiyuu, seiyuu_en, tags, tags_en, custom_tags FROM items"
        )
        rows = await cursor.fetchall()
        processed = 0
        for row in rows:
            await _refresh_metadata_index_for_item(
                db,
                row["id"],
                seiyuu_jp=_safe_json_list(row["seiyuu"]),
                seiyuu_en=_safe_json_list(row["seiyuu_en"]),
                tags_jp=_safe_json_list(row["tags"]),
                tags_en=_safe_json_list(row["tags_en"]),
                custom_tags=_safe_json_list(row["custom_tags"]),
            )
            processed += 1
        await db.commit()

        report = await get_integrity_report(sample_limit=sample_limit)
        return {
            "processed_items": processed,
            "post_rebuild_report": report,
        }
    finally:
        await db.close()


# Transcript and Translation Run Deletion
async def delete_transcript_run(run_id: int) -> bool:
    """Delete a transcript run from the database"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT track_id FROM pipeline_transcript_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        track_id = int(row["track_id"]) if row else None
        cursor = await db.execute("DELETE FROM pipeline_transcript_runs WHERE id = ?", (run_id,))
        await db.commit()
        deleted = cursor.rowcount > 0
    finally:
        await db.close()
    if deleted and track_id is not None:
        await recompute_translation_status_for_track(track_id)
    return deleted


async def delete_transcript_segments(run_id: int) -> int:
    """Delete all segments for a transcript run"""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM pipeline_transcript_run_segments WHERE run_id = ?", (run_id,))
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def delete_translation_run(run_id: int) -> bool:
    """Delete a translation run from the database"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT track_id FROM pipeline_translation_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        track_id = int(row["track_id"]) if row else None
        cursor = await db.execute("DELETE FROM pipeline_translation_runs WHERE id = ?", (run_id,))
        await db.commit()
        deleted = cursor.rowcount > 0
    finally:
        await db.close()
    if deleted and track_id is not None:
        await recompute_translation_status_for_track(track_id)
    return deleted


async def delete_translation_segments(run_id: int) -> int:
    """Delete all segments for a translation run"""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM pipeline_translation_run_segments WHERE run_id = ?", (run_id,))
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def update_transcript_segment_text(run_id: int, segment_index: int, text: str) -> dict | None:
    """Edit a single transcript segment's text in place.

    Stamps meta_json.edited so the UI can mark manually-corrected lines.
    Returns the updated row or None if no segment matched.
    """
    now = datetime.now().isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM pipeline_transcript_run_segments WHERE run_id = ? AND segment_index = ?",
            (run_id, segment_index),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        meta = _load_json_field(row["meta_json"], {})
        meta["edited"] = True
        meta["edited_at"] = now
        await db.execute(
            """UPDATE pipeline_transcript_run_segments
               SET text = ?, meta_json = ?, updated_at = ?
               WHERE run_id = ? AND segment_index = ?""",
            (text, json.dumps(meta, ensure_ascii=False), now, run_id, segment_index),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM pipeline_transcript_run_segments WHERE run_id = ? AND segment_index = ?",
            (run_id, segment_index),
        )
        updated = await cursor.fetchone()
        if not updated:
            return None
        out = dict(updated)
        out["meta_json"] = _load_json_field(out.get("meta_json"), {})
        return out
    finally:
        await db.close()


async def update_translation_segment_text(run_id: int, segment_index: int, text: str) -> dict | None:
    """Edit a single translation segment's text in place. See sibling above."""
    now = datetime.now().isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM pipeline_translation_run_segments WHERE run_id = ? AND segment_index = ?",
            (run_id, segment_index),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        meta = _load_json_field(row["meta_json"], {})
        meta["edited"] = True
        meta["edited_at"] = now
        await db.execute(
            """UPDATE pipeline_translation_run_segments
               SET text = ?, meta_json = ?, updated_at = ?
               WHERE run_id = ? AND segment_index = ?""",
            (text, json.dumps(meta, ensure_ascii=False), now, run_id, segment_index),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM pipeline_translation_run_segments WHERE run_id = ? AND segment_index = ?",
            (run_id, segment_index),
        )
        updated = await cursor.fetchone()
        if not updated:
            return None
        out = dict(updated)
        out["meta_json"] = _load_json_field(out.get("meta_json"), {})
        return out
    finally:
        await db.close()


# === Games wing =============================================================
# Catalog table for visual novels / games. Catalog-only — no extraction
# pipeline, no transcripts. Scanned from a separate library path
# (`games_scan_paths`). VNDB metadata + personal tracking (play_status,
# personal_rating, routes_json, notes) live on the row. Tokuten relations are
# many-to-many via the `game_tokutens` junction.

GAMES_SORTABLE_COLUMNS = {
    "created_at",
    "updated_at",
    "title",
    "release_date",
    "play_status",
    "personal_rating",
    "favorite",
}

_GAMES_PLAY_STATUSES = {"backlog", "playing", "completed", "dropped", "on_hold", "wishlist", "want_to_play"}

# Drama-CD personal listen-progress tracker — items.listen_status. Mirrors
# games.play_status; the UI labels 'completed' as "Finished" for audio context.
_DRAMA_CD_LISTEN_STATUSES = {"backlog", "want_to_listen", "listening", "completed", "on_hold", "dropped", "wishlist"}


async def get_tokuten_scan_paths() -> list[str]:
    """Mirrors `get_games_scan_paths()` but for the tokutens library. Returns
    [] when nothing has been configured."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            ("tokuten_scan_paths",),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        try:
            paths = json.loads(row["value"])
        except json.JSONDecodeError:
            return []
        if not isinstance(paths, list):
            return []
        return _normalize_scan_paths([str(p) for p in paths])
    finally:
        await db.close()


async def set_tokuten_scan_paths(paths: list[str]) -> list[str]:
    """Empty list is allowed — the user may not have a tokutens folder set
    up yet but still want to use the rest of the app."""
    normalized = _normalize_scan_paths(paths)
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("tokuten_scan_paths", json.dumps(normalized, ensure_ascii=False), now),
        )
        await db.commit()
        return normalized
    finally:
        await db.close()


async def upsert_tokuten_from_scan(
    *,
    library_path: str,
    title: str,
    is_archive: bool,
    total_size: int = 0,
    file_format: list[str] | None = None,
) -> tuple[int, bool]:
    """Insert a stub tokuten + paired items row for a scanner-discovered
    entry. Returns (tokuten_id, was_created). Idempotent — keyed on the
    items.product_code derived from the absolute path (a stable hash so
    re-scans don't create dupes). Like the games scanner this is catalog-
    only; the user fills in metadata via the detail panel."""
    import hashlib
    abs_path = str(Path(library_path).resolve())
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:12].upper()
    product_code = f"TKS-{digest}"

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, tokuten_id FROM items WHERE product_code = ?",
            (product_code,),
        )
        existing = await cursor.fetchone()
        if existing:
            return int(existing["tokuten_id"] or 0), False

        now = datetime.now().isoformat()
        cursor = await db.execute(
            """INSERT INTO tokutens (kind, title, shop, notes, local_path,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("audio", title, "other", "", abs_path, now, now),
        )
        new_tokuten_id = cursor.lastrowid

        files_json = json.dumps([abs_path], ensure_ascii=False)
        file_format_json = json.dumps(file_format or [], ensure_ascii=False)
        file_count = 1 if is_archive else 0
        await db.execute(
            """INSERT INTO items (
                   product_code, title, kind, tokuten_id, confidence,
                   is_manual, files, file_count, total_size, file_format,
                   scan_date, created_at, updated_at
               ) VALUES (?, ?, 'tokuten_audio', ?, 'verified',
                         0, ?, ?, ?, ?, ?, ?, ?)""",
            (product_code, title, new_tokuten_id, files_json, file_count,
             int(total_size or 0), file_format_json, now, now, now),
        )
        await db.commit()
        return int(new_tokuten_id), True
    finally:
        await db.close()


async def get_games_scan_paths() -> list[str]:
    """Mirrors `get_scan_paths()` but for the games library. Returns [] when
    nothing has been configured — unlike drama CDs there's no env-var fallback."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            ("games_scan_paths",),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        try:
            paths = json.loads(row["value"])
        except json.JSONDecodeError:
            return []
        if not isinstance(paths, list):
            return []
        return _normalize_scan_paths([str(p) for p in paths])
    finally:
        await db.close()


async def set_games_scan_paths(paths: list[str]) -> list[str]:
    """Empty list is allowed (unlike scan_paths) — the user may not have any
    games yet but still want to use the rest of the app."""
    normalized = _normalize_scan_paths(paths)
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("games_scan_paths", json.dumps(normalized, ensure_ascii=False), now),
        )
        await db.commit()
        return normalized
    finally:
        await db.close()


async def upsert_game_from_scan(
    *,
    library_path: str,
    title: str,
    is_archive: bool,
    platforms: list[str] | None = None,
) -> tuple[int, bool]:
    """Insert a stub games row for a scanner-discovered folder/archive.
    Returns (game_id, was_created). Idempotent — keyed on library_path. Only
    populates scanner-derived fields; VNDB metadata + personal tracking are
    set later via update_game().

    `platforms` is a list of VNDB-compatible codes derived from file
    extensions. On INSERT, written as-is. On an existing row, only written
    when the row's current platforms_json is empty / missing — so a VNDB
    match's platforms list never gets clobbered by a re-scan and vice
    versa."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT id, platforms_json FROM games WHERE library_path = ?",
            (library_path,),
        )
        row = await cursor.fetchone()
        if row:
            existing_platforms = _safe_json_list(row["platforms_json"])
            if platforms and not existing_platforms:
                await db.execute(
                    "UPDATE games SET platforms_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(platforms, ensure_ascii=False), now, row["id"]),
                )
                await db.commit()
            return int(row["id"]), False
        # Check if this path already lives in another row's
        # extra_library_paths (post-merge): if so, no new row needed.
        cursor = await db.execute(
            "SELECT id, platforms_json, extra_library_paths_json FROM games WHERE extra_library_paths_json LIKE ?",
            (f'%{json.dumps(library_path, ensure_ascii=False)}%',),
        )
        for extra_row in await cursor.fetchall():
            extras = _safe_json_list(extra_row["extra_library_paths_json"])
            if library_path in extras:
                # Optionally extend platforms if the scanner found new ones
                # not yet present on the primary row.
                if platforms:
                    existing = _safe_json_list(extra_row["platforms_json"])
                    merged = sorted(set(existing) | set(platforms))
                    if merged != existing:
                        await db.execute(
                            "UPDATE games SET platforms_json = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(merged, ensure_ascii=False), now, extra_row["id"]),
                        )
                        await db.commit()
                return int(extra_row["id"]), False
        cursor = await db.execute(
            """INSERT INTO games
                (title, library_path, is_archive, platforms_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (
                title,
                library_path,
                1 if is_archive else 0,
                json.dumps(platforms or [], ensure_ascii=False),
                now,
                now,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid), True
    finally:
        await db.close()


def _row_to_game(row) -> dict:
    return {
        "id": row["id"],
        "vndb_id": row["vndb_id"],
        "title": row["title"],
        "title_jp": row["title_jp"],
        "title_en": row["title_en"],
        "aliases": _safe_json_list(row["aliases_json"]),
        "olang": row["olang"],
        "developer": row["developer"],
        "developers": _safe_json_list(row["developers_json"]),
        "release_date": row["release_date"],
        "cover_url": row["cover_url"],
        "cover_local": row["cover_local"],
        "description": row["description"],
        "platforms": _safe_json_list(row["platforms_json"]),
        "platforms_available": _safe_json_list(row["platforms_available_json"]) if "platforms_available_json" in row.keys() else [],
        "languages": _safe_json_list(row["languages_json"]),
        "library_path": row["library_path"],
        "extra_library_paths": _safe_json_list(row["extra_library_paths_json"]) if "extra_library_paths_json" in row.keys() else [],
        "is_archive": bool(row["is_archive"]),
        "play_status": row["play_status"],
        "personal_rating": row["personal_rating"],
        "personal_notes": row["personal_notes"],
        "walkthrough_notes": row["walkthrough_notes"],
        "routes": _safe_json_list(row["routes_json"]),
        "favorite": bool(row["favorite"]),
        "custom_tags": _safe_json_list(row["custom_tags_json"]),
        "is_manual": bool(row["is_manual"]) if "is_manual" in row.keys() else False,
        "vndb_searched": bool(row["vndb_searched"]) if "vndb_searched" in row.keys() else False,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_games(
    *,
    search: str | None = None,
    play_status: str | None = None,
    play_statuses: list[str] | None = None,
    favorite: bool | None = None,
    is_manual: bool | None = None,
    platform: str | None = None,
    developer: str | None = None,
    custom_tag: str | None = None,
    vndb_id: str | None = None,
    exclude_wishlist: bool = True,
    matched: bool | None = None,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """List games with optional filters. `play_statuses` accepts a list
    (used by the per-status stat-pill filters); `play_status` is the legacy
    single-value form kept for backwards-compat. `exclude_wishlist` defaults
    to True so the main games list hides wishlist rows; the sidebar can flip
    it off to surface them. `matched` filters by vndb_id presence
    (True=has vndb_id, False=unmatched). `platform`, `developer`, `custom_tag`
    do substring matches against the respective fields/JSON lists."""
    sort_col = sort if sort in GAMES_SORTABLE_COLUMNS else "created_at"
    sort_dir = "ASC" if str(order).lower() == "asc" else "DESC"
    where = []
    params: list = []
    if search:
        where.append("(title LIKE ? OR title_jp LIKE ? OR title_en LIKE ? OR vndb_id = ?)")
        like = f"%{search}%"
        params.extend([like, like, like, search])
    if play_statuses:
        valid = [p for p in play_statuses if p in _GAMES_PLAY_STATUSES]
        if valid:
            placeholders = ",".join(["?"] * len(valid))
            where.append(f"play_status IN ({placeholders})")
            params.extend(valid)
    elif play_status and play_status in _GAMES_PLAY_STATUSES:
        where.append("play_status = ?")
        params.append(play_status)
    elif exclude_wishlist:
        # Default surface: hide wishlist rows unless explicitly requested.
        where.append("play_status != 'wishlist'")
    if favorite is True:
        where.append("favorite = 1")
    if is_manual is True:
        where.append("is_manual = 1")
    elif is_manual is False:
        where.append("is_manual = 0")
    if matched is True:
        # "Matched" includes both real VNDB links AND rows the user has
        # manually reviewed (vndb_searched=1 means "I confirmed no VNDB
        # entry exists, fields filled in by hand — done").
        where.append("(vndb_id IS NOT NULL AND vndb_id != '' OR vndb_searched = 1)")
    elif matched is False:
        # "Unmatched" is the cleanup queue's domain: no VNDB link AND not
        # yet manually reviewed.
        where.append("(vndb_id IS NULL OR vndb_id = '') AND vndb_searched = 0")
    if platform:
        where.append("platforms_json LIKE ?")
        params.append(f'%"{platform}"%')
    if developer:
        where.append("developer LIKE ?")
        params.append(f"%{developer}%")
    if custom_tag:
        where.append("custom_tags_json LIKE ?")
        params.append(f'%"{custom_tag}"%')
    if vndb_id:
        where.append("vndb_id = ?")
        params.append(vndb_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT COUNT(*) AS c FROM games {where_sql}",
            params,
        )
        total = (await cursor.fetchone())["c"]

        cursor = await db.execute(
            f"""SELECT * FROM games
                {where_sql}
                ORDER BY {sort_col} {sort_dir}, id DESC
                LIMIT ? OFFSET ?""",
            [*params, int(limit), int(offset)],
        )
        rows = await cursor.fetchall()
        return {
            "total_items": int(total),
            "items": [_row_to_game(r) for r in rows],
            "limit": int(limit),
            "offset": int(offset),
        }
    finally:
        await db.close()


async def get_game(game_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        row = await cursor.fetchone()
        return _row_to_game(row) if row else None
    finally:
        await db.close()


# Fields that are JSON-encoded on disk but accepted as Python lists/dicts in
# update_game(). Each maps the public field name to its DB column.
_GAMES_JSON_FIELDS = {
    "aliases": "aliases_json",
    "developers": "developers_json",
    "platforms": "platforms_json",
    "platforms_available": "platforms_available_json",
    "languages": "languages_json",
    "routes": "routes_json",
    "custom_tags": "custom_tags_json",
    "extra_library_paths": "extra_library_paths_json",
}

# Scalar fields the user / VNDB prefill may set. Excludes id, created_at,
# library_path (set by scanner), and the JSON aliases above.
_GAMES_SCALAR_FIELDS = {
    "vndb_id", "title", "title_jp", "title_en", "olang", "developer",
    "release_date", "cover_url", "cover_local", "description",
    "is_archive", "play_status", "personal_rating",
    "personal_notes", "walkthrough_notes", "favorite", "is_manual",
    "vndb_searched",
}


async def update_game(game_id: int, fields: dict) -> dict | None:
    """Patch any subset of mutable fields on a game row. JSON-list fields
    (aliases/developers/platforms/languages/routes/custom_tags) accept Python
    lists. play_status is validated against the CHECK enum."""
    sets: list[str] = []
    params: list = []
    for key, value in fields.items():
        if key in _GAMES_JSON_FIELDS:
            col = _GAMES_JSON_FIELDS[key]
            sets.append(f"{col} = ?")
            params.append(json.dumps(value if value is not None else [], ensure_ascii=False))
        elif key in _GAMES_SCALAR_FIELDS:
            if key == "play_status" and value not in _GAMES_PLAY_STATUSES:
                raise ValueError(f"Invalid play_status: {value!r}")
            if key in ("is_archive", "favorite", "is_manual", "vndb_searched"):
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return await get_game(game_id)
    sets.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(game_id)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE games SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
    finally:
        await db.close()
    return await get_game(game_id)


async def delete_game(game_id: int) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def find_duplicate_game_vndb_ids() -> list[dict]:
    """Returns groups of game rows that share a vndb_id (and have >1
    member). Each group lists `vndb_id` + a list of game stubs ordered
    such that the chosen "primary" comes first (preference: has cover →
    has play_status non-default → lowest id)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, vndb_id, title, cover_local, library_path, play_status
               FROM games
               WHERE vndb_id IS NOT NULL AND vndb_id != ''
               ORDER BY vndb_id, id"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    by_vndb: dict[str, list[dict]] = {}
    for r in rows:
        by_vndb.setdefault(r["vndb_id"], []).append(r)
    groups = []
    for vndb_id, members in by_vndb.items():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda m: (
                0 if (m.get("cover_local") or "").strip() else 1,
                0 if (m.get("play_status") or "backlog") != "backlog" else 1,
                int(m.get("id") or 0),
            )
        )
        groups.append({"vndb_id": vndb_id, "members": members})
    return groups


async def merge_game_rows(primary_id: int, other_ids: list[int]) -> dict:
    """Fold `other_ids` into `primary_id`: union their library_paths into
    primary.extra_library_paths_json, union platforms_json + custom_tags,
    then delete the others. Personal fields (play_status, rating, notes,
    favorite) stay with primary unchanged — the user's prior choices on
    the primary win. Returns a summary dict."""
    db = await get_db()
    try:
        # Read primary
        cur = await db.execute(
            """SELECT library_path, extra_library_paths_json,
                      platforms_json, platforms_available_json,
                      custom_tags_json, languages_json
               FROM games WHERE id = ?""",
            (primary_id,),
        )
        primary = await cur.fetchone()
        if not primary:
            raise ValueError(f"Primary game {primary_id} not found")
        extras: list[str] = list(_safe_json_list(primary["extra_library_paths_json"]))
        platforms: set[str] = set(_safe_json_list(primary["platforms_json"]))
        platforms_av: set[str] = set(_safe_json_list(primary["platforms_available_json"]))
        custom_tags: set[str] = set(_safe_json_list(primary["custom_tags_json"]))
        languages: set[str] = set(_safe_json_list(primary["languages_json"]))

        merged_count = 0
        for oid in other_ids:
            if int(oid) == int(primary_id):
                continue
            cur = await db.execute(
                """SELECT library_path, extra_library_paths_json,
                          platforms_json, platforms_available_json,
                          custom_tags_json, languages_json
                   FROM games WHERE id = ?""",
                (int(oid),),
            )
            row = await cur.fetchone()
            if not row:
                continue
            if (row["library_path"] or "").strip():
                lp = row["library_path"]
                if lp not in extras and lp != primary["library_path"]:
                    extras.append(lp)
            for ep in _safe_json_list(row["extra_library_paths_json"]):
                if ep not in extras and ep != primary["library_path"]:
                    extras.append(ep)
            platforms |= set(_safe_json_list(row["platforms_json"]))
            platforms_av |= set(_safe_json_list(row["platforms_available_json"]))
            custom_tags |= set(_safe_json_list(row["custom_tags_json"]))
            languages |= set(_safe_json_list(row["languages_json"]))
            await db.execute("DELETE FROM games WHERE id = ?", (int(oid),))
            merged_count += 1

        now = datetime.now().isoformat()
        await db.execute(
            """UPDATE games SET
                   extra_library_paths_json = ?,
                   platforms_json = ?,
                   platforms_available_json = ?,
                   custom_tags_json = ?,
                   languages_json = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                json.dumps(extras, ensure_ascii=False),
                json.dumps(sorted(platforms), ensure_ascii=False),
                json.dumps(sorted(platforms_av), ensure_ascii=False),
                json.dumps(sorted(custom_tags), ensure_ascii=False),
                json.dumps(sorted(languages), ensure_ascii=False),
                now,
                primary_id,
            ),
        )
        await db.commit()
        return {"primary_id": primary_id, "merged": merged_count, "extra_paths": len(extras)}
    finally:
        await db.close()


async def set_game_cover(game_id: int, cover_local: str | None, cover_url: str | None = None) -> dict | None:
    """Write cover_local (and optionally cover_url) on a game row. Called from
    both the manual upload endpoint and the VNDB prefill cover-download path."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        if cover_url is not None:
            await db.execute(
                "UPDATE games SET cover_local = ?, cover_url = ?, updated_at = ? WHERE id = ?",
                (cover_local, cover_url, now, game_id),
            )
        else:
            await db.execute(
                "UPDATE games SET cover_local = ?, updated_at = ? WHERE id = ?",
                (cover_local, now, game_id),
            )
        await db.commit()
    finally:
        await db.close()
    return await get_game(game_id)
