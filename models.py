from pydantic import BaseModel
from typing import Optional


class ItemUpdate(BaseModel):
    # User-data fields
    rating: Optional[int] = None
    favorite: Optional[bool] = None
    notes: Optional[str] = None
    translation_status: Optional[str] = None
    listen_status: Optional[str] = None
    custom_tags: Optional[list[str]] = None
    # Metadata fields (editable on manually-created cards, also usable for
    # hand-correcting scraped entries)
    title: Optional[str] = None
    title_en: Optional[str] = None
    circle: Optional[str] = None
    release_date: Optional[str] = None
    age_rating: Optional[str] = None
    description: Optional[str] = None
    description_en: Optional[str] = None
    seiyuu: Optional[list[str]] = None
    seiyuu_en: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    tags_en: Optional[list[str]] = None
    # Absolute path to an archive on disk — manual drama CDs aren't picked
    # up by the scanner, so the user points the row at a specific file. When
    # set, the backend stats the file and updates total_size / file_count /
    # file_format. Stored in items.files as a single-element JSON list.
    archive_path: Optional[str] = None


class OverrideCodeRequest(BaseModel):
    product_code: str  # New code, e.g. "RJ01234567" or a DLsite URL


class ScanRequest(BaseModel):
    path: Optional[str] = None  # Backward-compatible single path override
    paths: Optional[list[str]] = None  # Preferred multi-path override
    recursive: bool = True


class ScanPathsUpdateRequest(BaseModel):
    paths: list[str]


class GamesScanPathsUpdateRequest(BaseModel):
    """Empty list allowed — the user may not have any games configured yet."""
    paths: list[str]


class TokutenScanPathsUpdateRequest(BaseModel):
    """Empty list allowed — tokutens are optional like games."""
    paths: list[str]


class GameCoverUploadRequest(BaseModel):
    """Mirrors ManualCoverRequest for items. Base64 data URL upload."""
    filename: str
    data_url: str


class GameUpdateRequest(BaseModel):
    """Patch any subset of mutable fields on a game row. Unset keys are left
    alone; null clears scalar fields. JSON-list fields accept Python lists."""
    vndb_id: Optional[str] = None
    title: Optional[str] = None
    title_jp: Optional[str] = None
    title_en: Optional[str] = None
    olang: Optional[str] = None
    developer: Optional[str] = None
    release_date: Optional[str] = None
    cover_url: Optional[str] = None
    cover_local: Optional[str] = None
    description: Optional[str] = None
    is_archive: Optional[bool] = None
    play_status: Optional[str] = None
    personal_rating: Optional[int] = None
    personal_notes: Optional[str] = None
    walkthrough_notes: Optional[str] = None
    favorite: Optional[bool] = None
    aliases: Optional[list[str]] = None
    developers: Optional[list[dict]] = None
    platforms: Optional[list[str]] = None
    platforms_available: Optional[list[str]] = None
    languages: Optional[list[str]] = None
    routes: Optional[list[dict]] = None
    custom_tags: Optional[list[str]] = None
    is_manual: Optional[bool] = None
    vndb_searched: Optional[bool] = None


class FetchMetadataRequest(BaseModel):
    product_codes: Optional[list[str]] = None  # Fetch specific codes, or all if None
    force: bool = False  # Re-fetch even if already have metadata


class ConfirmMatchRequest(BaseModel):
    """Request to confirm or unconfirm a product code match."""
    pass  # No body needed, just a signal


class ManualCoverRequest(BaseModel):
    filename: str
    data_url: str


class BulkIdsRequest(BaseModel):
    item_ids: list[int]


class BulkOverrideEntry(BaseModel):
    item_id: int
    product_code: str


class BulkOverrideRequest(BaseModel):
    overrides: list[BulkOverrideEntry]


class PipelineExtractRequest(BaseModel):
    force: bool = False


class TranscriptSegmentInput(BaseModel):
    segment_index: int
    start_seconds: float
    end_seconds: float
    text: str
    confidence: Optional[float] = None
    meta: Optional[dict] = None


class TranscriptRunCreateRequest(BaseModel):
    language: str = "ja"
    source: str = "manual"
    engine: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    set_active: bool = True
    segments: list[TranscriptSegmentInput]


class TranslationSegmentInput(BaseModel):
    segment_index: int
    text: str
    meta: Optional[dict] = None


class TranslationRunCreateRequest(BaseModel):
    transcript_run_id: int
    target_language: str = "en"
    source: str = "manual"
    engine: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    set_active: bool = True
    segments: list[TranslationSegmentInput]


class SegmentTextUpdateRequest(BaseModel):
    text: str


class PipelineToggleRequest(BaseModel):
    enabled: bool


class AutoTranscribeRequest(BaseModel):
    language: str = "ja"
    model: Optional[str] = None  # Overrides config default
    force: bool = False
    track_ids: Optional[list[int]] = None  # Specific tracks to transcribe, or None for all


class SeiyuuMergeRequest(BaseModel):
    canonical_en: str
    aliases: list[str]
    canonical_jp: Optional[str] = None
    dry_run: bool = True


class AutopilotRequest(BaseModel):
    target_language: str = "en"
    provider: Optional[str] = None
    model: Optional[str] = None
    max_tokens_per_chunk: int = 1000
    max_lines_per_chunk: int = 20
    max_retries_per_chunk: int = 2
    retry_backoff_seconds: float = 1.0
    glossary: Optional[str] = None
    character_memory: Optional[str] = None
    transcribe_language: str = "ja"
    transcribe_model: Optional[str] = None
    skip_stages: Optional[list[str]] = None  # any of: metadata_translate, extract, track_titles_translate, transcribe, track_translate
    force_extract: bool = False
    force_transcribe: bool = False
    force_translate: bool = False


class AutoTranslateRequest(BaseModel):
    transcript_run_id: Optional[int] = None  # Uses active transcript if omitted
    target_language: str = "en"
    provider: str = "gemini"
    model: Optional[str] = None
    max_tokens_per_chunk: int = 1000  # Cost optimized: 100 segments = ~5 API calls
    max_lines_per_chunk: int = 20  # 20 lines per chunk (vs 2-10 for quality)
    max_retries_per_chunk: int = 1  # Only retry once to save quota
    retry_backoff_seconds: float = 0.5  # Faster retries
    set_active: bool = True
    glossary: Optional[str] = None
    character_memory: Optional[str] = None
    # When true, skip queueing if the track already has an active translation
    # run (used by bulk fan-outs so re-running doesn't waste API quota).
    only_if_missing: bool = False


class AiSettingsUpdateRequest(BaseModel):
    translation_provider: Optional[str] = None
    gemini_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    clear_gemini_api_key: bool = False
    openrouter_model: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    clear_openrouter_api_key: bool = False
    chutes_model: Optional[str] = None
    chutes_api_key: Optional[str] = None
    clear_chutes_api_key: bool = False
    openai_compat_model: Optional[str] = None
    openai_compat_api_key: Optional[str] = None
    openai_compat_base_url: Optional[str] = None
    openai_compat_request_format: Optional[str] = None
    clear_openai_compat_api_key: bool = False
    clear_openai_compat_base_url: bool = False


class WhisperSettingsUpdateRequest(BaseModel):
    model: Optional[str] = None
    vad_filter: Optional[bool] = None
    beam_size: Optional[int] = None
    condition_on_previous_text: Optional[bool] = None
    preferred_variant: Optional[str] = None


TOKUTEN_KINDS = {"audio", "book", "image", "misc"}
# "source" filter values exposed in the Tokutens sidebar. Stored as `shop`
# on the tokutens row for legacy reasons. Migration 015 maps the old set
# (animate/stellaworth/getchu/melonbooks/toranoana/amazon_jp/other) onto
# this newer enum; migration 026 adds the metadata-fetch sources
# (gamers/chil_chil/vgmdb).
TOKUTEN_SHOPS = {"dlsite", "booth", "melon", "animate", "stellaworth",
                 "gamers", "chil_chil", "vgmdb", "rejet", "physical", "other"}


class TokutenCreate(BaseModel):
    title: str
    title_en: Optional[str] = None
    kind: str = "audio"
    shop: str = "other"
    shop_other_name: Optional[str] = None
    release_date: Optional[str] = None
    notes: Optional[str] = ""
    source_url: Optional[str] = None
    local_path: Optional[str] = None
    # Independent VNDB id link — stored regardless of whether the matching
    # game exists in the local library. Read-side joins resolve it.
    vndb_id: Optional[str] = None
    # Cast + description (migration 026) — lists are stored as JSON text in
    # the seiyuu/seiyuu_en columns, mirroring items.
    seiyuu: Optional[list[str]] = None
    seiyuu_en: Optional[list[str]] = None
    description: Optional[str] = None
    description_en: Optional[str] = None


class TokutenUpdate(BaseModel):
    title: Optional[str] = None
    title_en: Optional[str] = None
    kind: Optional[str] = None
    shop: Optional[str] = None
    shop_other_name: Optional[str] = None
    release_date: Optional[str] = None
    notes: Optional[str] = None
    source_url: Optional[str] = None
    local_path: Optional[str] = None
    vndb_id: Optional[str] = None
    seiyuu: Optional[list[str]] = None
    seiyuu_en: Optional[list[str]] = None
    description: Optional[str] = None
    description_en: Optional[str] = None


class MetadataFetchRequest(BaseModel):
    url: str


class MetadataSearchRequest(BaseModel):
    query: str
    source: Optional[str] = None  # omit to search all searchable sources


class MetadataApplyRequest(BaseModel):
    """Apply a (possibly user-edited) normalized metadata dict to an item or
    tokuten. `fields` whitelists what gets written — the preview UI sends
    only the checkboxes the user left on."""
    target: str  # 'item' | 'tokuten'
    target_id: int
    metadata: dict
    fields: list[str]


class TokutenScanRequest(BaseModel):
    """Scan a folder of audio files into a new tokuten entry. The folder is
    walked once: audio files become pipeline_tracks, Cover.jpg becomes the
    cover, every other image lands in media_assets as gallery."""
    folder_path: str
    title: str
    title_en: Optional[str] = None
    shop: str = "other"
    shop_other_name: Optional[str] = None
    release_date: Optional[str] = None
    notes: Optional[str] = ""
    source_url: Optional[str] = None
