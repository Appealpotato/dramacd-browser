import asyncio
import json
import logging
from datetime import datetime

import database as db

# Cap concurrent per-track translation jobs across the whole process. Bulk
# fan-outs (e.g. selecting 19 tracks and queueing auto-translate for each)
# would otherwise spawn N concurrent provider calls — blow the rate limit
# and flood the activity drawer. Translations are API-bound, not GPU-bound,
# so 10 is a comfortable ceiling.
MAX_CONCURRENT_TRANSLATIONS = 10
_translation_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSLATIONS)
from pipeline.chutes_translator import ChutesTrackTranslator
from pipeline.gemini_translator import GeminiTrackTranslator, build_token_chunks
from pipeline.openrouter_translator import OpenRouterTrackTranslator
from text_cleaning import build_clean_translation_source

logger = logging.getLogger(__name__)


def _choose_transcript_run_id(metadata: dict, active_outputs: dict) -> int | None:
    explicit = metadata.get("transcript_run_id")
    if explicit:
        return int(explicit)
    active = active_outputs.get("active_transcript_run_id") if active_outputs else None
    return int(active) if active else None


def _build_review_prompt(
    *,
    target_language: str,
    description_context: str,
    glossary: str,
    pairs: list[dict],
) -> str:
    """Second-pass prompt: the model sees JP+EN side by side and returns the
    FINAL text for every segment — corrected where needed, unchanged where
    fine. Same JSON contract as translate_chunk so the existing alignment
    machinery applies."""
    return (
        "You are reviewing a Japanese → " + target_language + " drama CD translation.\n"
        "For EACH segment below you get the Japanese source (ja) and the current translation (tl).\n"
        "Return the FINAL translation text for every segment:\n"
        "• Fix mistranslations, dropped content, wrong speaker tone, or unnatural phrasing.\n"
        "• If the current translation is already good, return it UNCHANGED.\n"
        "• Do not re-style text that is correct; minimal edits only.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "1. Respond ONLY with valid JSON array: [{\"segment_index\":N,\"text\":\"...\"}]\n"
        "2. Preserve segment_index values exactly as provided.\n"
        "3. Return exactly one entry per input segment — never skip any.\n"
        "4. No markdown, no code blocks, no commentary—just pure JSON.\n\n"
        f"Drama description (context): {description_context or '(none)'}\n"
        f"Preferred terms / glossary: {glossary or '(none)'}\n\n"
        f"SEGMENTS TO REVIEW:\n{json.dumps(pairs, ensure_ascii=False)}\n\n"
        "Return the JSON array only:"
    )


def _apply_review_rows(
    expected_indexes: list[int],
    out_rows: list,
    translated_map: dict[int, dict],
) -> int:
    """Merge review output into translated_map. Only segments the reviewer
    actually changed are touched; anything missing/empty keeps the original
    translation (a review failure must never lose first-pass work).
    Returns the number of segments changed."""
    changed = 0
    by_index: dict[int, str] = {}
    for row in out_rows or []:
        if not isinstance(row, dict):
            continue
        try:
            seg_idx = int(row.get("segment_index"))
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if text:
            by_index[seg_idx] = text
    for seg_idx in expected_indexes:
        new_text = by_index.get(seg_idx)
        entry = translated_map.get(seg_idx)
        if not new_text or not entry:
            continue
        if new_text != str(entry.get("text") or ""):
            entry["text"] = new_text
            meta = entry.get("meta") or {}
            meta["reviewed"] = True
            entry["meta"] = meta
            changed += 1
    return changed


import re as _re

_CJK_RE = _re.compile(r"[぀-ヿ一-鿿]")
# Segments likely to contain names/proper nouns: katakana runs (loanword or
# name spellings) or honorific/address markers next to a name.
_PROPER_NOUN_HINT_RE = _re.compile(
    r"[゠-ヿ]{2,}|さん|くん|ちゃん|さま|様|先輩|先生|殿|お兄|お姉"
)
_FEEDBACK_PAIRS_MAX_CHARS = 3000


def _select_feedback_pairs(
    cleaned_segments: list[dict],
    translated_map: dict[int, dict],
    max_chars: int = _FEEDBACK_PAIRS_MAX_CHARS,
) -> list[dict]:
    """Pick JP/EN pairs worth mining for glossary rules: name-bearing
    segments first (katakana runs, honorifics), then any CJK segment, until
    the character budget is spent. Returns [{"ja": ..., "en": ...}]."""
    hinted: list[dict] = []
    plain: list[dict] = []
    for seg in cleaned_segments:
        seg_idx = int(seg["segment_index"])
        ja = str(seg.get("source_text") or "").strip()
        entry = translated_map.get(seg_idx)
        en = str((entry or {}).get("text") or "").strip()
        if not ja or not en or not _CJK_RE.search(ja):
            continue
        pair = {"ja": ja, "en": en}
        (hinted if _PROPER_NOUN_HINT_RE.search(ja) else plain).append(pair)
    selected: list[dict] = []
    used = 0
    for pair in hinted + plain:
        cost = len(pair["ja"]) + len(pair["en"])
        if used + cost > max_chars:
            continue
        selected.append(pair)
        used += cost
    return selected


def _build_feedback_glossary_prompt(pairs: list[dict]) -> str:
    """Post-run prompt: extract the proper-noun mappings the translation
    actually used, so later tracks of the same item reuse them verbatim
    (translation_job re-reads the item glossary at the start of every job)."""
    return (
        "Below are Japanese→English pairs from a drama CD translation you produced.\n"
        "Extract ONLY proper-noun mappings the translation actually used:\n"
        "• Character / person names (e.g. 小霧幸人=Kogiri Yukito)\n"
        "• Place, organization, or series names\n"
        "• Recurring invented terms specific to this work\n"
        "Do NOT include common vocabulary, pronouns, or one-off phrases.\n"
        "Format: one rule per line, exactly '日本語=English', taken verbatim from the pairs.\n"
        "Respond ONLY with valid JSON: {\"glossary\": \"line1\\nline2\"} — use \\n between rules.\n"
        "If nothing qualifies, return {\"glossary\": \"\"}.\n\n"
        f"PAIRS:\n{json.dumps(pairs, ensure_ascii=False)}"
    )


def _coerce_feedback_glossary(parsed) -> str:
    """Normalize the feedback model's output to validated '日本語=English'
    lines: each kept line must contain '=' with a CJK-bearing left side —
    anything else (commentary, common-word pairs, garbage) is dropped."""
    if isinstance(parsed, dict):
        parsed = parsed.get("glossary", "")
    if isinstance(parsed, list):
        parsed = "\n".join(str(x or "").strip() for x in parsed)
    lines = []
    for line in str(parsed or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        left, _, right = line.partition("=")
        if left.strip() and right.strip() and _CJK_RE.search(left):
            lines.append(line)
    return "\n".join(lines)


def _is_retryable_translation_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    retry_markers = (
        " 429",
        "rate limit",
        "quota",
        "no choices",
        "empty content",
        "timeout",
        "temporar",
        "overloaded",
        "503",
        "502",
        "500",
    )
    return any(marker in text for marker in retry_markers)


def _align_chunk_output_rows(expected_indexes: list[int], out_rows: list[dict]) -> tuple[dict[int, dict], str]:
    """
    Try to align model output rows to expected segment indexes.
    Returns (out_map, mode) where mode explains alignment strategy.
    """
    logger.debug(f"[align_chunk_output_rows] Expected indexes: {expected_indexes}")
    logger.debug(f"[align_chunk_output_rows] Raw out_rows count: {len(out_rows)}")
    if out_rows:
        logger.debug(f"[align_chunk_output_rows] First row sample: {json.dumps(out_rows[0], ensure_ascii=False, default=str)[:300]}")

    cleaned_rows = [row for row in out_rows if isinstance(row, dict) and str(row.get("text") or "").strip()]
    logger.debug(f"[align_chunk_output_rows] Cleaned rows count: {len(cleaned_rows)}")

    direct_map: dict[int, dict] = {}
    for row in cleaned_rows:
        if "segment_index" not in row:
            logger.debug(f"[align_chunk_output_rows] Row missing segment_index: {list(row.keys())}")
            continue
        try:
            seg_idx = int(row["segment_index"])
        except Exception as e:
            logger.debug(f"[align_chunk_output_rows] Failed to convert segment_index: {row.get('segment_index')} - {e}")
            continue
        direct_map[seg_idx] = row

    if all(seg_idx in direct_map for seg_idx in expected_indexes):
        logger.debug(f"[align_chunk_output_rows] Using 'direct' alignment mode")
        return direct_map, "direct"

    # Fallback 1: constant offset remap (e.g. model returns 0..N-1 for expected 1..N).
    output_indexes = []
    for row in cleaned_rows:
        if "segment_index" not in row:
            continue
        try:
            output_indexes.append(int(row["segment_index"]))
        except Exception:
            continue
    unique_output = sorted(set(output_indexes))
    if len(unique_output) == len(expected_indexes):
        expected_sorted = sorted(expected_indexes)
        deltas = [expected_sorted[i] - unique_output[i] for i in range(len(expected_sorted))]
        if deltas and all(delta == deltas[0] for delta in deltas):
            delta = deltas[0]
            offset_map: dict[int, dict] = {}
            for row in cleaned_rows:
                if "segment_index" not in row:
                    continue
                try:
                    seg_idx = int(row["segment_index"]) + delta
                except Exception:
                    continue
                if seg_idx in expected_indexes and seg_idx not in offset_map:
                    offset_map[seg_idx] = row
            if all(seg_idx in offset_map for seg_idx in expected_indexes):
                return offset_map, f"offset({delta:+d})"

    # Fallback 2: positional remap by expected order when row counts match.
    if len(cleaned_rows) == len(expected_indexes):
        positional_map: dict[int, dict] = {}
        for i, seg_idx in enumerate(expected_indexes):
            positional_map[seg_idx] = cleaned_rows[i]
        return positional_map, "positional"

    # Fallback 2b: keep valid direct indexes, then fill missing by row order.
    if direct_map:
        remaining_expected = [seg_idx for seg_idx in expected_indexes if seg_idx not in direct_map]
        if remaining_expected:
            remaining_rows = []
            used_expected = set(expected_indexes)
            for row in cleaned_rows:
                try:
                    row_idx = int(row.get("segment_index")) if row.get("segment_index") is not None else None
                except Exception:
                    row_idx = None
                if row_idx in used_expected:
                    continue
                remaining_rows.append(row)
            if len(remaining_rows) >= len(remaining_expected):
                mixed_map = dict(direct_map)
                for i, seg_idx in enumerate(remaining_expected):
                    mixed_map[seg_idx] = remaining_rows[i]
                if all(seg_idx in mixed_map for seg_idx in expected_indexes):
                    return mixed_map, "mixed_positional"

    # Fallback 3: one row containing multiple lines -> split and map positionally.
    if len(cleaned_rows) == 1:
        blob = str(cleaned_rows[0].get("text") or "").strip()
        if blob:
            lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
            if len(lines) == len(expected_indexes):
                split_map: dict[int, dict] = {}
                for i, seg_idx in enumerate(expected_indexes):
                    split_map[seg_idx] = {"segment_index": seg_idx, "text": lines[i], "meta": cleaned_rows[0].get("meta") or {}}
                return split_map, "split_lines"

    # Fallback 4: at least enough rows, ignore broken indexes and map first N by order.
    if len(cleaned_rows) >= len(expected_indexes):
        ordered_map: dict[int, dict] = {}
        for i, seg_idx in enumerate(expected_indexes):
            ordered_map[seg_idx] = cleaned_rows[i]
        logger.info(f"[align_chunk_output_rows] Using 'ordered_prefix' - mapping {len(cleaned_rows)} rows to {len(expected_indexes)} expected indexes")
        return ordered_map, "ordered_prefix"

    # Fallback 5: EMERGENCY - if we have ANY rows at all, use them in order
    if cleaned_rows:
        logger.warning(f"[align_chunk_output_rows] EMERGENCY FALLBACK: {len(cleaned_rows)} rows for {len(expected_indexes)} expected, using positional mapping")
        emergency_map: dict[int, dict] = {}
        for i, seg_idx in enumerate(expected_indexes[:len(cleaned_rows)]):
            emergency_map[seg_idx] = cleaned_rows[i]
        return emergency_map, "emergency_positional"

    logger.error(f"[align_chunk_output_rows] CRITICAL: No rows available for {len(expected_indexes)} expected indexes")
    return direct_map, "partial"


async def _wait_if_paused_or_stopping(job_id: int) -> str:
    while True:
        job = await db.get_job(job_id)
        if not job:
            return "stop"
        status = str(job.get("status") or "").lower()
        stopping = bool(job.get("stopping"))
        paused = bool(job.get("paused")) or status == "paused"
        if status in {"stopping", "stopped", "failed", "interrupted"} or stopping:
            return "stop"
        if paused:
            await db.update_job(job_id, status="paused", paused=1, current="paused")
            await asyncio.sleep(0.8)
            continue
        return "run"


async def run_translation_job(job_id: int):
    """Public entry point — wraps the actual worker in a semaphore so the
    concurrent-translations cap is enforced regardless of who queued the job."""
    job = await db.get_job(job_id)
    if not job or job.get("job_type") != "pipeline_translate":
        return
    if _translation_semaphore.locked():
        await db.append_job_event(
            job_id,
            "info",
            f"Waiting for a translation slot (running {MAX_CONCURRENT_TRANSLATIONS} at a time)",
            {},
        )
    async with _translation_semaphore:
        # Re-fetch in case the job was stopped while we waited.
        job_now = await db.get_job(job_id)
        if not job_now:
            return
        if str(job_now.get("status") or "").lower() in {"stopping", "stopped", "failed"}:
            if str(job_now.get("status") or "").lower() == "stopping":
                await db.update_job(job_id, status="stopped", stopped=1, finished_at=datetime.now().isoformat())
            return
        await _run_translation_job_inner(job_id)


async def _run_translation_job_inner(job_id: int):
    job = await db.get_job(job_id)
    if not job or job.get("job_type") != "pipeline_translate":
        return

    metadata = job.get("metadata_json") or {}
    track_id = int(metadata.get("track_id") or 0)
    item_id = int(metadata.get("item_id") or 0)
    target_language = str(metadata.get("target_language") or "en").strip() or "en"
    provider = str(metadata.get("provider") or "gemini").strip().lower()
    runtime_base_url: str | None = None
    runtime_request_format = "openai"
    if provider == "openrouter":
        runtime_model = await db.get_runtime_openrouter_model()
        runtime_api_key = await db.get_runtime_openrouter_api_key()
    elif provider == "chutes":
        runtime_model = await db.get_runtime_chutes_model()
        runtime_api_key = await db.get_runtime_chutes_api_key()
    elif provider == "openai_compat":
        runtime_model = await db.get_runtime_openai_compat_model()
        runtime_api_key = await db.get_runtime_openai_compat_api_key()
        runtime_base_url = await db.get_runtime_openai_compat_base_url()
        runtime_request_format = await db.get_runtime_openai_compat_request_format()
    else:
        runtime_model = await db.get_runtime_gemini_model()
        runtime_api_key = await db.get_runtime_gemini_api_key()
    model = str(metadata.get("model") or runtime_model).strip() or runtime_model
    # Auto-size chunks when the caller didn't pin them: cloud providers stay
    # at the cost-optimized 1000/20, but a local Ollama backend pays nothing
    # per token — bigger chunks mean more scene continuity per request and
    # far fewer resends of the ~700-token instruction block. 3000/60 stays
    # comfortably inside the 16k num_ctx the ollama format requests.
    _local_backend = provider == "openai_compat" and runtime_request_format == "ollama"
    _auto_tokens, _auto_lines = (3000, 60) if _local_backend else (1000, 20)
    max_tokens = max(200, min(4000, int(metadata.get("max_tokens_per_chunk") or _auto_tokens)))
    max_lines = max(1, min(100, int(metadata.get("max_lines_per_chunk") or _auto_lines)))
    max_retries_per_chunk = max(0, min(6, int(metadata.get("max_retries_per_chunk") or 2)))
    retry_backoff_seconds = max(0.2, min(10.0, float(metadata.get("retry_backoff_seconds") or 1.0)))
    set_active = bool(metadata.get("set_active", True))
    review_pass = bool(metadata.get("review_pass", False))
    glossary_feedback = bool(metadata.get("glossary_feedback", True))
    glossary = str(metadata.get("glossary") or "").strip()
    character_memory = str(metadata.get("character_memory") or "").strip()
    # Glossary precedence: global → item (DB) → per-job. Jobs queued without an
    # explicit glossary (autopilot, bulk fan-outs) still pick up the item's
    # saved rules this way, and feedback-loop additions land mid-run.
    try:
        global_glossary = await db.get_runtime_global_glossary()
    except Exception:
        global_glossary = ""
    item_glossary = ""
    item_character_memory = ""
    if item_id:
        try:
            item_glossary = str(await db.get_item_glossary(item_id) or "").strip()
        except Exception:
            item_glossary = ""
        try:
            item_character_memory = str(await db.get_item_character_memory(item_id) or "").strip()
        except Exception:
            item_character_memory = ""
    glossary = db.merge_glossaries(global_glossary, item_glossary, glossary)
    if not character_memory:
        character_memory = item_character_memory

    if provider not in db.SUPPORTED_TRANSLATION_PROVIDERS:
        await db.update_job(job_id, status="failed", error=f"Unsupported provider: {provider}")
        return
    # OpenAI-compatible endpoints are typically local and unauthenticated
    # (LM Studio, llama.cpp, vLLM, Ollama) — the key is optional regardless
    # of request format; an endpoint that needs auth will 401 with a clearer
    # message than a preemptive block here.
    _key_optional = provider == "openai_compat"
    if not runtime_api_key and not _key_optional:
        missing_key_name = "DRAMACD_GEMINI_API_KEY"
        if provider == "openrouter":
            missing_key_name = "DRAMACD_OPENROUTER_API_KEY"
        elif provider == "chutes":
            missing_key_name = "DRAMACD_CHUTES_API_KEY"
        elif provider == "openai_compat":
            missing_key_name = "OpenAI-compatible API key"
        await db.update_job(
            job_id,
            status="failed",
            error=f"Missing {missing_key_name} for auto-translation",
        )
        return
    if provider == "openai_compat":
        if not runtime_base_url:
            await db.update_job(job_id, status="failed", error="Missing OpenAI-compatible base URL")
            return
        if not runtime_model:
            await db.update_job(job_id, status="failed", error="Missing OpenAI-compatible model")
            return

    track = await db.get_pipeline_track(track_id)
    if not track:
        await db.update_job(job_id, status="failed", error="Track not found")
        return

    # Fetch previous track summaries for context memory (last 2 tracks: N-1, N-2)
    current_track_index = track.get("track_index", 0)
    previous_summaries = await db.get_previous_track_summaries(item_id, current_track_index, limit=2)
    logger.info(f"[translation_job] Fetched {len(previous_summaries)} previous track summaries for context")

    active_outputs = await db.get_track_active_outputs(track_id)
    transcript_run_id = _choose_transcript_run_id(metadata, active_outputs)
    if not transcript_run_id:
        await db.update_job(job_id, status="failed", error="No transcript_run_id provided and no active transcript")
        return

    transcript_run = await db.get_transcript_run(transcript_run_id)
    if not transcript_run or int(transcript_run.get("track_id", -1)) != track_id:
        await db.update_job(job_id, status="failed", error="Transcript run not found for track")
        return

    segments = await db.get_transcript_segments(transcript_run_id)
    if not segments:
        await db.update_job(job_id, status="failed", error="Transcript run has no segments")
        return

    clean_source = build_clean_translation_source(segments)
    cleaned_segments = []
    for seg in clean_source.get("segments", []):
        source_text = str(seg.get("clean_text") or seg.get("text") or "").strip()
        if not source_text:
            continue
        cleaned_segments.append({"segment_index": int(seg["segment_index"]), "source_text": source_text})

    if not cleaned_segments:
        await db.update_job(job_id, status="failed", error="No translatable text after cleaning")
        return

    await db.update_job(
        job_id,
        status="running",
        started_at=job.get("started_at") or datetime.now().isoformat(),
        total=len(cleaned_segments),
        completed=0,
    )
    await db.append_job_event(
        job_id,
        "info",
        "Auto-translation started",
        {
            "track_id": track_id,
            "transcript_run_id": transcript_run_id,
            "target_language": target_language,
            "provider": provider,
                "model": model,
                "max_retries_per_chunk": max_retries_per_chunk,
                "retry_backoff_seconds": retry_backoff_seconds,
            },
        )

    if provider == "openrouter":
        translator = OpenRouterTrackTranslator(api_key=runtime_api_key, model=model)
    elif provider == "chutes":
        translator = ChutesTrackTranslator(api_key=runtime_api_key, model=model)
    elif provider == "openai_compat":
        if runtime_request_format == "anthropic":
            from pipeline.anthropic_compat_translator import AnthropicCompatTrackTranslator
            translator = AnthropicCompatTrackTranslator(
                api_key=runtime_api_key,
                model=model,
                base_url=runtime_base_url,
            )
        elif runtime_request_format == "ollama":
            from pipeline.ollama_translator import OllamaTrackTranslator
            translator = OllamaTrackTranslator(
                model=model,
                base_url=runtime_base_url,
                api_key=runtime_api_key or "",
            )
        else:
            translator = OpenRouterTrackTranslator(
                api_key=runtime_api_key,
                model=model,
                base_url=runtime_base_url,
                provider_label="openai_compat",
            )
    else:
        translator = GeminiTrackTranslator(api_key=runtime_api_key, model=model)
    item = await db.get_item(item_id) if item_id > 0 else None

    description_context = ""
    if item:
        description_context = str(item.get("description_en") or "").strip()
        if not description_context:
            raw_description = str(item.get("description") or "").strip()
            if raw_description:
                try:
                    description_context = await translator.translate_text_to_en(raw_description)
                    await db.set_item_description_en(int(item["id"]), description_context)
                    await db.append_job_event(
                        job_id,
                        "info",
                        "Translated item description for context",
                        {"item_id": int(item["id"])},
                    )
                except Exception as exc:
                    await db.append_job_event(
                        job_id,
                        "warning",
                        "Failed to translate item description; continuing without it",
                        {"error": str(exc)},
                    )

    # Tokuten-specific augmentation: when the tokuten has a linked VNDB id,
    # prepend the game's description to description_context. Tokutens ship
    # with a game and frequently share its world / characters / relationship
    # dynamics — giving the translator that context up-front beats letting
    # it guess from the audio segments alone. Local game row wins; if the
    # user hasn't added the game to their library, fall back to fetching
    # from VNDB directly.
    if item and item.get("kind") == "tokuten_audio" and item.get("tokuten_id"):
        tk_conn = await db.get_db()
        try:
            tk_cur = await tk_conn.execute(
                "SELECT vndb_id FROM tokutens WHERE id = ?",
                (item["tokuten_id"],),
            )
            tk_row = await tk_cur.fetchone()
        finally:
            await tk_conn.close()
        tk_vndb = (tk_row["vndb_id"] if tk_row else None) or ""
        if tk_vndb:
            game_desc = ""
            game_title = ""
            try:
                local_listing = await db.list_games(vndb_id=tk_vndb, limit=1, exclude_wishlist=False)
                local_items = local_listing.get("items") or []
                if local_items:
                    g = local_items[0]
                    game_desc = str(g.get("description") or "").strip()
                    game_title = str(g.get("title_en") or g.get("title") or "").strip()
            except Exception as exc:
                logger.debug(f"[translation_job] Local linked-game lookup failed: {exc}")
            if not game_desc:
                try:
                    import vndb_client as _vndb
                    vn = await _vndb.get_vn(tk_vndb)
                    if vn:
                        game_desc = str(vn.get("description") or "").strip()
                        game_title = str(vn.get("title") or "").strip()
                except Exception as exc:
                    logger.debug(f"[translation_job] VNDB linked-game fetch failed: {exc}")
            if game_desc:
                prefix_lines = []
                if game_title:
                    prefix_lines.append(f"Linked game: {game_title}")
                prefix_lines.append(game_desc)
                prefix = "\n".join(prefix_lines).strip() + "\n\n"
                description_context = (prefix + description_context).strip()
                await db.append_job_event(
                    job_id,
                    "info",
                    "Injected linked-game description into tokuten translation context",
                    {"vndb_id": tk_vndb, "title": game_title or None},
                )

    chunks = build_token_chunks(cleaned_segments, max_tokens=max_tokens, max_lines=max_lines)
    translated_map: dict[int, dict] = {}
    translated_context: list[dict] = []
    processed = 0
    failures = []

    async def _save_partial_run(reason: str) -> int | None:
        """Persist whatever has been translated so far as a (partial)
        translation run. Used on user stop AND on chunk failures — a refusal
        or API error near the end must not throw away every chunk that
        already succeeded."""
        if not translated_map:
            return None
        partial_segments = []
        for seg in segments:
            seg_idx = int(seg["segment_index"])
            row = translated_map.get(seg_idx)
            if row:
                partial_segments.append(row)
        if not partial_segments:
            return None
        try:
            run_id = await db.create_translation_run(
                track_id=track_id,
                transcript_run_id=transcript_run_id,
                target_language=target_language,
                source="auto",
                engine=provider,
                model=model,
                prompt="auto_translate_chunked_with_context",
                segments=partial_segments,
                metadata={
                    "created_via": "auto_translation",
                    "job_id": job_id,
                    "provider": provider,
                    "model": model,
                    "interrupted": True,
                    "interrupt_reason": reason,
                    "segments_translated": len(partial_segments),
                    "total_segments": len(segments),
                },
            )
            await db.set_track_active_translation(track_id, run_id)
            await db.append_job_event(
                job_id,
                "info",
                f"Partial translation saved with {len(partial_segments)} segments ({reason})",
                {"translation_run_id": run_id},
            )
            return run_id
        except Exception as e:
            logger.warning(f"[translation_job] Failed to save partial translation ({reason}): {e}")
            return None

    for idx, chunk in enumerate(chunks, start=1):
        control_state = await _wait_if_paused_or_stopping(job_id)
        if control_state == "stop":
            await _save_partial_run("user stop")
            await db.update_job(
                job_id,
                status="interrupted",
                stopped=1,
                paused=0,
                stopping=0,
                completed=processed,
                finished_at=datetime.now().isoformat(),
                error="Translation job stopped by user",
            )
            await db.append_job_event(job_id, "warning", "Translation stopped by user", {"completed": processed})
            return
        translated_this_chunk = False
        last_error = None
        for attempt in range(max_retries_per_chunk + 1):
            control_state = await _wait_if_paused_or_stopping(job_id)
            if control_state == "stop":
                await _save_partial_run("user stop")
                await db.update_job(
                    job_id,
                    status="interrupted",
                    stopped=1,
                    paused=0,
                    stopping=0,
                    completed=processed,
                    finished_at=datetime.now().isoformat(),
                    error="Translation job stopped by user",
                )
                await db.append_job_event(job_id, "warning", "Translation stopped by user", {"completed": processed})
                return
            try:
                logger.info(f"[translation_job] Chunk {idx}/{len(chunks)}: Translating {len(chunk)} segments with provider={provider}")
                out_rows = await translator.translate_chunk(
                    target_language=target_language,
                    description_context=description_context,
                    chunk_segments=chunk,
                    prior_context=translated_context,
                    glossary=glossary,
                    character_memory=character_memory,
                    previous_summaries=previous_summaries,
                )

                expected_indexes = [int(seg["segment_index"]) for seg in chunk]
                logger.info(f"[translation_job] Chunk {idx}: Received {len(out_rows)} output rows")
                logger.debug(f"[translation_job] Chunk {idx}: Raw output (first 3 rows): {json.dumps([r for r in out_rows[:3]], ensure_ascii=False, default=str)}")

                out_map, align_mode = _align_chunk_output_rows(expected_indexes, out_rows)
                logger.info(f"[translation_job] Chunk {idx}: Alignment mode '{align_mode}' matched {len(out_map)}/{len(expected_indexes)} segments")

                # Check if we got ALL expected segments
                missing = [seg_idx for seg_idx in expected_indexes if seg_idx not in out_map]
                if missing:
                    # If we got at least SOME translations, accept partial result
                    if out_map:
                        coverage_pct = (len(out_map) / len(expected_indexes)) * 100
                        logger.warning(f"[translation_job] Chunk {idx}: Partial response - {len(out_map)}/{len(expected_indexes)} segments ({coverage_pct:.0f}%), missing {missing}")
                        # Update expected_indexes to only what we actually got
                        expected_indexes = [seg_idx for seg_idx in expected_indexes if seg_idx in out_map]
                    else:
                        # No translations at all - hard failure
                        logger.error(f"[translation_job] Chunk {idx}: No segments translated, alignment_mode={align_mode}")
                        raise RuntimeError(f"Model returned no valid segments")
                if align_mode != "direct":
                    await db.append_job_event(
                        job_id,
                        "warning",
                        f"Chunk {idx} used non-direct index alignment",
                        {"alignment_mode": align_mode, "expected_count": len(expected_indexes)},
                    )

                for seg_idx in expected_indexes:
                    row = out_map[seg_idx]
                    translated_map[seg_idx] = {"segment_index": seg_idx, "text": row["text"], "meta": row.get("meta") or {}}
                    translated_context.append({"segment_index": seg_idx, "text": row["text"]})

                processed += len(chunk)
                await db.update_job(job_id, completed=processed, current=f"chunk {idx}/{len(chunks)}")
                await db.append_job_event(
                    job_id,
                    "info",
                    f"Translated chunk {idx}/{len(chunks)}",
                    {
                        "chunk_size": len(chunk),
                        "completed": processed,
                        "total": len(cleaned_segments),
                        "attempt": attempt + 1,
                        "preview_lines": [
                            str(out_map[seg_idx].get("text") or "").strip()
                            for seg_idx in expected_indexes[:3]
                            if str(out_map[seg_idx].get("text") or "").strip()
                        ],
                    },
                )
                translated_this_chunk = True
                break
            except Exception as exc:
                last_error = str(exc)
                can_retry = attempt < max_retries_per_chunk and _is_retryable_translation_error(exc)
                if can_retry:
                    delay = retry_backoff_seconds * (2 ** attempt)
                    await db.append_job_event(
                        job_id,
                        "warning",
                        f"Chunk {idx} attempt {attempt + 1} failed; retrying",
                        {"error": str(exc), "retry_in_seconds": delay, "attempt": attempt + 1},
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        if not translated_this_chunk:
            failures.append({"chunk": idx, "error": last_error or "unknown error"})
            await db.append_job_event(
                job_id,
                "error",
                f"Chunk {idx} failed — continuing with remaining chunks",
                {"error": last_error or "unknown error"},
            )
            # A refusal/parse failure is usually specific to THIS chunk's
            # content — keep going so one bad chunk doesn't sink the track.
            continue

    if failures:
        # Save what DID translate before reporting the failure. Previously a
        # mid-job refusal returned here without saving, discarding every
        # chunk that had already succeeded.
        partial_run_id = await _save_partial_run(
            f"{len(failures)} chunk(s) failed; first error: {str(failures[0]['error'])[:120]}"
        )
        job_kwargs = dict(
            status="interrupted" if partial_run_id else "failed",
            stopped=1,
            completed=processed,
            failed=len(failures),
            errors_json=failures,
            finished_at=datetime.now().isoformat(),
            error=(
                f"{len(failures)}/{len(chunks)} chunk(s) failed"
                + (f" — partial translation saved ({len(translated_map)} segments)" if partial_run_id else "")
                + f": {failures[0]['error']}"
            ),
        )
        if partial_run_id:
            job_kwargs["result_json"] = {
                "track_id": track_id,
                "transcript_run_id": transcript_run_id,
                "translation_run_id": partial_run_id,
                "failed_chunks": [f["chunk"] for f in failures],
            }
        await db.update_job(job_id, **job_kwargs)
        return

    review_changed = 0
    review_failed_chunks = 0
    if review_pass and translated_map:
        from pipeline.json_extract import loads_robust

        await db.append_job_event(
            job_id, "info", "Starting review pass", {"chunks": len(chunks)}
        )
        for idx, chunk in enumerate(chunks, start=1):
            control_state = await _wait_if_paused_or_stopping(job_id)
            if control_state == "stop":
                # First-pass translation is complete and intact — a stop
                # during review just ends the review early and saves what
                # we have, it must NOT discard the run.
                await db.append_job_event(
                    job_id,
                    "warning",
                    "Review pass stopped by user — saving translation with partial review",
                    {"reviewed_chunks": idx - 1, "changed": review_changed},
                )
                break
            expected_indexes = [
                int(seg["segment_index"])
                for seg in chunk
                if int(seg["segment_index"]) in translated_map
            ]
            if not expected_indexes:
                continue
            pairs = [
                {
                    "segment_index": seg_idx,
                    "ja": next(
                        str(seg["source_text"])
                        for seg in chunk
                        if int(seg["segment_index"]) == seg_idx
                    ),
                    "tl": str(translated_map[seg_idx].get("text") or ""),
                }
                for seg_idx in expected_indexes
            ]
            prompt = _build_review_prompt(
                target_language=target_language,
                description_context=description_context,
                glossary=glossary,
                pairs=pairs,
            )
            try:
                raw = await translator._send_text(prompt)
                try:
                    parsed = loads_robust(raw)
                except Exception:
                    parsed = OpenRouterTrackTranslator._extract_json_candidate(raw)
                rows = OpenRouterTrackTranslator._coerce_rows_payload(parsed)
                if not isinstance(rows, list):
                    raise RuntimeError("review output was not a JSON array")
                chunk_changed = _apply_review_rows(expected_indexes, rows, translated_map)
                review_changed += chunk_changed
                await db.update_job(job_id, current=f"review {idx}/{len(chunks)}")
                if chunk_changed:
                    await db.append_job_event(
                        job_id,
                        "info",
                        f"Review chunk {idx}/{len(chunks)}: revised {chunk_changed} segment(s)",
                        {"changed": chunk_changed},
                    )
            except Exception as exc:
                # Review is best-effort: a failed review chunk keeps the
                # first-pass translation for those segments.
                review_failed_chunks += 1
                await db.append_job_event(
                    job_id,
                    "warning",
                    f"Review chunk {idx}/{len(chunks)} failed — keeping first-pass translation",
                    {"error": str(exc)[:300]},
                )
        await db.append_job_event(
            job_id,
            "info",
            f"Review pass finished: {review_changed} segment(s) revised",
            {"changed": review_changed, "failed_chunks": review_failed_chunks},
        )

    translated_segments = []
    for seg in segments:
        seg_idx = int(seg["segment_index"])
        row = translated_map.get(seg_idx)
        if row:
            translated_segments.append(row)

    if not translated_segments:
        await db.update_job(job_id, status="failed", error="No translated segments were produced")
        return

    run_id = await db.create_translation_run(
        track_id=track_id,
        transcript_run_id=transcript_run_id,
        target_language=target_language,
        source="auto",
        engine=provider,
        model=model,
        prompt="auto_translate_chunked_with_context",
        segments=translated_segments,
        metadata={
            "created_via": "auto_translation",
            "job_id": job_id,
            "provider": provider,
            "model": model,
            "description_context_used": bool(description_context),
            "chunk_count": len(chunks),
            "max_tokens_per_chunk": max_tokens,
            "max_lines_per_chunk": max_lines,
            "max_retries_per_chunk": max_retries_per_chunk,
            "retry_backoff_seconds": retry_backoff_seconds,
            "glossary_used": bool(glossary),
            "character_memory_used": bool(character_memory),
            "review_pass": bool(review_pass),
            "review_changed": review_changed,
        },
    )
    if set_active:
        await db.set_track_active_translation(track_id, run_id)

    # Replicate the freshly-finished translation onto every sibling track
    # in the same group (e.g. the FLAC + MP3 of the same audio). Mirrors
    # what whisper_job already does for transcripts — without this every
    # variant re-translates the same audio independently and burns LLM
    # quota for identical output.
    try:
        shared_ids = await db.replicate_translation_run_to_siblings(run_id)
        if shared_ids:
            await db.append_job_event(
                job_id,
                "info",
                f"Shared translation with {len(shared_ids)} sibling track(s)",
                {"source_run_id": run_id, "new_run_ids": shared_ids},
            )
    except Exception as share_err:
        logger.warning(f"[translation_job] Sibling-share failed for run {run_id}: {share_err}")

    # Feedback loop: mine the finished translation for the proper-noun
    # mappings it actually used and merge them into the ITEM glossary, so the
    # next track of this CD starts with them locked in (every job re-reads
    # the item glossary at start). Best-effort — never fails the job.
    glossary_feedback_added = 0
    if glossary_feedback and item_id:
        try:
            from pipeline.json_extract import loads_robust

            pairs = _select_feedback_pairs(cleaned_segments, translated_map)
            if pairs:
                raw = await translator._send_text(_build_feedback_glossary_prompt(pairs))
                try:
                    parsed = loads_robust(raw)
                except Exception:
                    parsed = OpenRouterTrackTranslator._extract_json_candidate(raw)
                suggested = _coerce_feedback_glossary(parsed)
                if suggested:
                    existing = await db.get_item_glossary(item_id) or ""
                    merged = db.merge_glossaries(existing, suggested)
                    existing_lines = {l.strip().lower() for l in existing.splitlines() if l.strip()}
                    added = [l for l in merged.splitlines() if l.strip().lower() not in existing_lines]
                    if added:
                        await db.set_item_glossary(item_id, merged)
                        glossary_feedback_added = len(added)
                        await db.append_job_event(
                            job_id,
                            "info",
                            f"Glossary feedback: +{len(added)} rule(s) saved to item glossary",
                            {"added": added[:20]},
                        )
        except Exception as fb_err:
            await db.append_job_event(
                job_id,
                "warning",
                "Glossary feedback skipped",
                {"error": str(fb_err)[:300]},
            )

    await db.update_job(
        job_id,
        status="completed",
        stopped=1,
        total=len(cleaned_segments),
        completed=len(cleaned_segments),
        success=len(cleaned_segments),
        failed=0,
        result_json={
            "track_id": track_id,
            "transcript_run_id": transcript_run_id,
            "translation_run_id": run_id,
            "target_language": target_language,
            "segments_translated": len(translated_segments),
            "chunk_count": len(chunks),
            "glossary_feedback_added": glossary_feedback_added,
        },
        finished_at=datetime.now().isoformat(),
    )
    await db.append_job_event(
        job_id,
        "info",
        "Auto-translation completed",
        {"translation_run_id": run_id, "segments": len(translated_segments)},
    )
