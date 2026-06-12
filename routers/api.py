import re
import base64
import binascii
import json
import httpx
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Depends
from typing import Optional, List
from models import (
    ItemUpdate,
    OverrideCodeRequest,
    ConfirmMatchRequest,
    ManualCoverRequest,
    BulkIdsRequest,
    BulkOverrideRequest,
    AiSettingsUpdateRequest,
    SeiyuuMergeRequest,
    WhisperSettingsUpdateRequest,
)
from scraper import fetch_metadata_for_code
from config import COVERS_DIR
from auth import require_api_key
import database as db

router = APIRouter(prefix="/api")

# Regex to extract product code from a DLsite URL
DLSITE_URL_PATTERN = re.compile(r'(RJ|BJ|VJ)\d{6,8}', re.IGNORECASE)
ALLOWED_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COVER_BYTES = 8 * 1024 * 1024  # 8MB
EN_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@router.get("/items")
async def list_items(
    sort: str = Query("scan_date", pattern="^(release_date|title|rating|scan_date|created_at|updated_at|product_code|confidence|translation_status|listen_status)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    search: Optional[str] = None,
    seiyuu: Optional[List[str]] = Query(None),
    tag: Optional[List[str]] = Query(None),
    custom_tag: Optional[str] = None,
    translation_status: Optional[str] = None,
    favorite: Optional[bool] = None,
    has_metadata: Optional[bool] = None,
    confidence: Optional[str] = None,
    lang: str = Query("jp", pattern="^(jp|en)$"),
    include_tokutens: bool = Query(False),
    only_tokutens: bool = Query(False),
    is_manual: Optional[bool] = Query(None),
    listen_status: Optional[str] = Query(None, pattern="^(backlog|want_to_listen|listening|completed|on_hold|dropped|wishlist)$"),
    listen_statuses: Optional[List[str]] = Query(None),
    tokuten_kind: Optional[str] = Query(None, pattern="^(audio|book|image|misc)$"),
    tokuten_source: Optional[str] = Query(None, pattern="^(dlsite|booth|melon|animate|stellaworth|gamers|chil_chil|vgmdb|rejet|fanza|toranoana|digiket|gyutto|hvdb|pokedora|physical|other)$"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    return await db.get_all_items(
        sort=sort, order=order, search=search,
        seiyuu=seiyuu, tag=tag, custom_tag=custom_tag,
        translation_status=translation_status,
        favorite=favorite, has_metadata=has_metadata,
        confidence=confidence, lang=lang,
        include_tokutens=include_tokutens,
        only_tokutens=only_tokutens,
        is_manual=is_manual,
        listen_status=listen_status,
        listen_statuses=listen_statuses,
        tokuten_kind=tokuten_kind,
        tokuten_source=tokuten_source,
        limit=limit, offset=offset,
    )


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.post("/items/blank")
async def create_blank_item(_auth=Depends(require_api_key)):
    """Insert a placeholder drama_cd items row the user can then fill in via
    the detail-panel editor. Mirrors the createBlankTokuten flow but with
    kind='drama_cd' and a 'MAN-<hex>' synthetic code so the row never collides
    with a real DLsite scan."""
    from uuid import uuid4
    import datetime as _dt
    from database import get_db
    now = _dt.datetime.now().isoformat()
    synthetic_code = f"MAN-{uuid4().hex[:12].upper()}"
    conn = await get_db()
    try:
        cursor = await conn.execute(
            """INSERT INTO items
                (product_code, title, kind, confidence,
                 files, file_count, total_size, file_format, is_manual,
                 scan_date, created_at, updated_at)
            VALUES (?, ?, 'drama_cd', 'verified',
                    '[]', 0, 0, '[]', 1,
                    ?, ?, ?)""",
            (synthetic_code, "[New Drama CD]", now, now, now),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    finally:
        await conn.close()
    item = await db.get_item(new_id)
    if not item:
        raise HTTPException(status_code=500, detail="Created item could not be re-read")
    return item


@router.post("/system/pick-file")
async def system_pick_file(
    payload: Optional[dict] = None,
    _auth=Depends(require_api_key),
):
    """Pop a native OS file-picker dialog on the host running this server and
    return the absolute path the user picked. Cancellation returns
    `{"path": null, "cancelled": true}`. Used by the manual drama-CD flow so
    the user doesn't have to type or paste an archive path."""
    import asyncio
    from os_utils import pick_file

    body = payload or {}
    title = (body.get("title") or "Pick archive").strip() or "Pick archive"
    initial_dir = body.get("initial_dir") or None
    raw_filetypes = body.get("filetypes")
    if isinstance(raw_filetypes, list) and raw_filetypes:
        filetypes = [tuple(item) for item in raw_filetypes if isinstance(item, (list, tuple)) and len(item) == 2]
    else:
        filetypes = [
            ("Archives", "*.7z *.zip *.rar *.tar *.001"),
            ("All files", "*.*"),
        ]

    try:
        picked = await asyncio.to_thread(
            pick_file,
            title=title,
            initial_dir=initial_dir,
            filetypes=filetypes,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File picker failed: {e}")

    if picked is None:
        return {"path": None, "cancelled": True}
    return {"path": picked, "cancelled": False}


@router.post("/system/pick-folder")
async def system_pick_folder(
    payload: Optional[dict] = None,
    _auth=Depends(require_api_key),
):
    """Pop a native OS folder-picker dialog and return the chosen directory.
    Used by the manual drama-CD flow so a user can point an entry at a folder
    of already-extracted loose audio instead of an archive file. Cancellation
    returns `{"path": null, "cancelled": true}`."""
    import asyncio
    from os_utils import pick_directory

    body = payload or {}
    title = (body.get("title") or "Pick folder").strip() or "Pick folder"
    initial_dir = body.get("initial_dir") or None

    try:
        picked = await asyncio.to_thread(
            pick_directory,
            title=title,
            initial_dir=initial_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Folder picker failed: {e}")

    if picked is None:
        return {"path": None, "cancelled": True}
    return {"path": picked, "cancelled": False}


@router.post("/system/pick-files")
async def system_pick_files(
    payload: Optional[dict] = None,
    _auth=Depends(require_api_key),
):
    """Multi-select variant of `/system/pick-file`. Returns
    `{"paths": [...]}` — empty list on cancel. Used by the bulk add-CD /
    add-game / add-tokuten flows so the user can shift-pick a folder of
    archives in one go."""
    import asyncio
    from os_utils import pick_files

    body = payload or {}
    title = (body.get("title") or "Pick files").strip() or "Pick files"
    initial_dir = body.get("initial_dir") or None
    raw_filetypes = body.get("filetypes")
    if isinstance(raw_filetypes, list) and raw_filetypes:
        filetypes = [tuple(item) for item in raw_filetypes if isinstance(item, (list, tuple)) and len(item) == 2]
    else:
        filetypes = [
            ("Archives", "*.7z *.zip *.rar *.tar *.001"),
            ("All files", "*.*"),
        ]

    try:
        picked = await asyncio.to_thread(
            pick_files,
            title=title,
            initial_dir=initial_dir,
            filetypes=filetypes,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File picker failed: {e}")

    return {"paths": picked}


@router.post("/system/list-addable-children")
async def system_list_addable_children(
    payload: Optional[dict] = None,
    _auth=Depends(require_api_key),
):
    """Enumerate the addable units inside a parent folder for the bulk-add
    flow, so one folder pick can create many entries — archives AND
    already-extracted folders, mixed. For each immediate child:
      - an archive file        → one 'archive' entry,
      - a subfolder with audio  → one 'folder' entry (indexed in place),
      - a subfolder of archives → one 'archive' entry per archive inside.
    Multi-volume RARs collapse to their first part. If the folder itself only
    holds loose audio (a single extracted CD), it returns one 'folder' entry
    for the folder. Returns `{"folder", "entries":[{"path","name","kind"}]}`."""
    from pathlib import Path as _Path
    from config import ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS
    from scanner import get_part_number

    body = payload or {}
    folder = str(body.get("folder") or "").strip()
    if not folder:
        raise HTTPException(status_code=400, detail="No folder provided")
    root = _Path(folder).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="Folder does not exist or is not a directory")

    def _is_continuation(name: str) -> bool:
        # Multi-volume RAR: keep .part1, drop .part2+ (RAR streams the rest).
        pn = get_part_number(name)
        return pn is not None and pn != 1

    def _has_audio(base: _Path) -> bool:
        try:
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    return True
        except OSError:
            pass
        return False

    def _archives_in(base: _Path) -> list:
        out = []
        try:
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS and not _is_continuation(f.name):
                    out.append(f)
        except OSError:
            pass
        return sorted(out, key=lambda p: str(p).lower())

    entries = []
    try:
        children = sorted(root.iterdir(), key=lambda c: c.name.lower())
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read folder: {e}")

    for child in children:
        try:
            if child.is_file():
                if child.suffix.lower() in ARCHIVE_EXTENSIONS and not _is_continuation(child.name):
                    entries.append({"path": str(child), "name": child.name, "kind": "archive"})
            elif child.is_dir():
                if _has_audio(child):
                    entries.append({"path": str(child), "name": child.name, "kind": "folder"})
                else:
                    for a in _archives_in(child):
                        entries.append({"path": str(a), "name": a.name, "kind": "archive"})
        except OSError:
            continue

    # Folder pointed straight at a single extracted CD (loose audio at the top
    # level, no addable children): treat the whole folder as one entry.
    if not entries and _has_audio(root):
        entries.append({"path": str(root), "name": root.name, "kind": "folder"})

    return {"folder": str(root), "entries": entries}


async def _refetch_metadata(item_id: int, product_code: str):
    """Background task: fetch metadata for a single item after code override."""
    import logging
    from config import DLSITE_PROXY_URL
    logger = logging.getLogger(__name__)

    try:
        client_kwargs = {"timeout": 30.0}
        if DLSITE_PROXY_URL:
            client_kwargs["proxies"] = DLSITE_PROXY_URL
        async with httpx.AsyncClient(**client_kwargs) as client:
            metadata, reason = await fetch_metadata_for_code(client, product_code)
            if metadata:
                await db.update_item_metadata(product_code, metadata)
                logger.info(f"Auto-fetched metadata for {product_code} (item {item_id}): {metadata.get('title', 'untitled')}")
            else:
                logger.warning(f"Failed to auto-fetch metadata for {product_code} (item {item_id}): {reason}")
    except Exception as e:
        logger.error(f"Error in background refetch for {product_code} (item {item_id}): {e}")


def _parse_json_payload(raw: str):
    payload = (raw or "").strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        marker_idx = payload.find("\n")
        if marker_idx >= 0:
            payload = payload[marker_idx + 1 :]
    payload = payload.strip()
    if payload.startswith("json"):
        payload = payload[4:].strip()
    if payload.endswith("```"):
        payload = payload[:-3].strip()
    return json.loads(payload)


def _coerce_translation_payload(parsed):
    # Gemini can sometimes return an array instead of an object even when JSON object is requested.
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict) and ("title_en" in entry or "description_en" in entry):
                return entry
    raise HTTPException(status_code=502, detail="Gemini response JSON shape was not a translation object")


def _coerce_name_list(value) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else []
    out = []
    seen = set()
    for entry in raw:
        name = str(entry or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _coerce_notes_list(value) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else []
    notes = []
    seen = set()
    for entry in raw:
        note = str(entry or "").strip()
        if not note:
            continue
        key = note.lower()
        if key in seen:
            continue
        seen.add(key)
        notes.append(note)
        if len(notes) >= 3:
            break
    return notes


def _unpack_metadata_translation_result(result) -> tuple[str, str, list[str], list[str]]:
    if isinstance(result, tuple):
        if len(result) >= 3:
            notes = _coerce_notes_list(result[3]) if len(result) >= 4 else []
            return str(result[0] or "").strip(), str(result[1] or "").strip(), _coerce_name_list(result[2]), notes
        if len(result) == 2:
            return str(result[0] or "").strip(), str(result[1] or "").strip(), [], []
    if isinstance(result, dict):
        return (
            str(result.get("title_en") or "").strip(),
            str(result.get("description_en") or "").strip(),
            _coerce_name_list(result.get("seiyuu_en")),
            _coerce_notes_list(result.get("cultural_notes")),
        )
    raise HTTPException(status_code=502, detail="Translation response had invalid shape")


async def _translate_title_description_with_gemini(title_ja: str, description_ja: str, seiyuu_ja: list[str]) -> tuple[str, str, list[str]]:
    api_key = await db.get_runtime_gemini_api_key()
    model = await db.get_runtime_gemini_model()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DRAMACD_GEMINI_API_KEY")

    prompt = (
        "Translate the following Japanese drama CD metadata to natural, idiomatic English.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "• Return ONLY valid JSON object with keys: title_en, description_en, seiyuu_en, cultural_notes\n"
        "• No markdown, no code blocks, no commentary—just pure JSON.\n\n"
        "TRANSLATION GUIDELINES:\n"
        "• Prioritize natural, idiomatic English over literal translation.\n"
        "• For description_en: Use readable paragraph breaks (blank lines between paragraphs).\n"
        "• Preserve emotional tone and marketing appeal while remaining accurate.\n"
        "• For seiyuu_en: Transliterate or provide standard English stage-name spelling for each voice actor.\n"
        "• If a phrase has no direct equivalent, rewrite it naturally and add a short explanation to cultural_notes.\n"
        "• Avoid awkward literal renderings (example: do NOT output phrases like 'babu-babu erotic little devil').\n\n"
        "SEXUAL CONTENT RULES (if applicable):\n"
        "• When translating erotic drama CDs, the listener/heroine is typically female.\n"
        "• Masculine-coded slang used metaphorically for female arousal should be rendered appropriately:\n"
        "  - \"you're so turned on\" / \"you're swollen with arousal\" / \"you're this sensitive\"\n"
        "• Preserve emotional intensity without embellishing or adding new acts/details not in the original.\n\n"
        "CULTURAL NOTES:\n"
        "• cultural_notes should be a short list (0-3 items) written for readers, plain English.\n"
        "• Only include notes for terms/concepts that truly need explanation for English readers.\n\n"
        f"title_ja: {title_ja}\n"
        f"description_ja: {description_ja}\n"
        f"seiyuu_ja: {json.dumps(seiyuu_ja, ensure_ascii=False)}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            params={"key": api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
            },
        )
    if resp.status_code >= 400:
        detail = f"Gemini API error ({resp.status_code})"
        try:
            err_payload = resp.json()
            err_obj = err_payload.get("error") if isinstance(err_payload, dict) else None
            if isinstance(err_obj, dict):
                msg = str(err_obj.get("message") or "").strip()
                status = str(err_obj.get("status") or "").strip()
                if msg:
                    detail = msg
                elif status:
                    detail = status
        except Exception:
            raw = (resp.text or "").strip()
            if raw:
                detail = raw[:500]

        # Preserve quota/rate-limit semantics for UI handling.
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail=f"Gemini quota/rate limit: {detail}")
        raise HTTPException(status_code=502, detail=f"Gemini upstream error: {detail}")

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise HTTPException(status_code=502, detail="Gemini returned no candidates")
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    if not parts:
        raise HTTPException(status_code=502, detail="Gemini returned empty content")

    try:
        parsed_raw = _parse_json_payload(str(parts[0].get("text") or ""))
    except Exception:
        raise HTTPException(status_code=502, detail="Gemini returned invalid JSON")
    parsed = _coerce_translation_payload(parsed_raw)
    title_en = str(parsed.get("title_en") or "").strip()
    description_en = str(parsed.get("description_en") or "").strip()
    seiyuu_en = _coerce_name_list(parsed.get("seiyuu_en"))
    cultural_notes = _coerce_notes_list(parsed.get("cultural_notes"))
    if not title_en and not description_en and not seiyuu_en and not cultural_notes:
        raise HTTPException(status_code=502, detail="Gemini response missing translated fields")
    return title_en, description_en, seiyuu_en, cultural_notes


async def _translate_title_description_with_openrouter(title_ja: str, description_ja: str, seiyuu_ja: list[str]) -> tuple[str, str, list[str]]:
    api_key = await db.get_runtime_openrouter_api_key()
    model = await db.get_runtime_openrouter_model()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DRAMACD_OPENROUTER_API_KEY")

    prompt = (
        "Translate the following Japanese drama CD metadata to natural, idiomatic English.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "• Return ONLY valid JSON object with keys: title_en, description_en, seiyuu_en, cultural_notes\n"
        "• No markdown, no code blocks, no commentary—just pure JSON.\n\n"
        "TRANSLATION GUIDELINES:\n"
        "• Prioritize natural, idiomatic English over literal translation.\n"
        "• For description_en: Use readable paragraph breaks (blank lines between paragraphs).\n"
        "• Preserve emotional tone and marketing appeal while remaining accurate.\n"
        "• For seiyuu_en: Transliterate or provide standard English stage-name spelling for each voice actor.\n"
        "• If a phrase has no direct equivalent, rewrite it naturally and add a short explanation to cultural_notes.\n"
        "• Avoid awkward literal renderings (example: do NOT output phrases like 'babu-babu erotic little devil').\n\n"
        "SEXUAL CONTENT RULES (if applicable):\n"
        "• When translating erotic drama CDs, the listener/heroine is typically female.\n"
        "• Masculine-coded slang used metaphorically for female arousal should be rendered appropriately:\n"
        "  - \"you're so turned on\" / \"you're swollen with arousal\" / \"you're this sensitive\"\n"
        "• Preserve emotional intensity without embellishing or adding new acts/details not in the original.\n\n"
        "CULTURAL NOTES:\n"
        "• cultural_notes should be a short list (0-3 items) written for readers, plain English.\n"
        "• Only include notes for terms/concepts that truly need explanation for English readers.\n\n"
        f"title_ja: {title_ja}\n"
        f"description_ja: {description_ja}\n"
        f"seiyuu_ja: {json.dumps(seiyuu_ja, ensure_ascii=False)}"
    )

    last_detail = ""
    resp = None
    for use_response_format in (True, False):
        request_json = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if use_response_format:
            request_json["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_json,
            )
        if resp.status_code >= 400:
            break
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            break
        last_detail = str(data)[:300]
        if not use_response_format:
            break

    if resp is None:
        raise HTTPException(status_code=502, detail="OpenRouter request failed before response")
    if resp.status_code >= 400:
        detail = f"OpenRouter API error ({resp.status_code})"
        try:
            err_payload = resp.json()
            err_obj = err_payload.get("error") if isinstance(err_payload, dict) else None
            if isinstance(err_obj, dict):
                msg = str(err_obj.get("message") or "").strip()
                if msg:
                    detail = msg
        except Exception:
            raw = (resp.text or "").strip()
            if raw:
                detail = raw[:500]
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail=f"OpenRouter quota/rate limit: {detail}")
        raise HTTPException(status_code=502, detail=f"OpenRouter upstream error: {detail}")

    data = resp.json()
    err_obj = data.get("error") if isinstance(data, dict) else None
    if isinstance(err_obj, dict):
        msg = str(err_obj.get("message") or "").strip()
        if msg:
            raise HTTPException(status_code=502, detail=f"OpenRouter upstream error: {msg}")
    choices = data.get("choices") or []
    if not choices:
        detail = "OpenRouter returned no choices"
        if last_detail:
            detail = f"{detail}. Response: {last_detail}"
        raise HTTPException(status_code=502, detail=detail)
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, str):
                txt = entry.strip()
            elif isinstance(entry, dict):
                txt = str(entry.get("text") or entry.get("content") or "").strip()
            else:
                txt = ""
            if txt:
                parts.append(txt)
        content = "\n".join(parts)
    content = str(content or "").strip()
    if not content:
        raise HTTPException(status_code=502, detail="OpenRouter returned empty content")

    try:
        parsed_raw = _parse_json_payload(content)
    except Exception:
        raise HTTPException(status_code=502, detail="OpenRouter returned invalid JSON")
    parsed = _coerce_translation_payload(parsed_raw)
    title_en = str(parsed.get("title_en") or "").strip()
    description_en = str(parsed.get("description_en") or "").strip()
    seiyuu_en = _coerce_name_list(parsed.get("seiyuu_en"))
    cultural_notes = _coerce_notes_list(parsed.get("cultural_notes"))
    if not title_en and not description_en and not seiyuu_en and not cultural_notes:
        raise HTTPException(status_code=502, detail="OpenRouter response missing translated fields")
    return title_en, description_en, seiyuu_en, cultural_notes


async def _translate_title_description_with_chutes(title_ja: str, description_ja: str, seiyuu_ja: list[str]) -> tuple[str, str, list[str]]:
    api_key = await db.get_runtime_chutes_api_key()
    model = await db.get_runtime_chutes_model()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DRAMACD_CHUTES_API_KEY")

    prompt = (
        "Translate the following Japanese drama CD metadata to natural, idiomatic English.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "• Return ONLY valid JSON object with keys: title_en, description_en, seiyuu_en, cultural_notes\n"
        "• No markdown, no code blocks, no commentary—just pure JSON.\n\n"
        "TRANSLATION GUIDELINES:\n"
        "• Prioritize natural, idiomatic English over literal translation.\n"
        "• For description_en: Use readable paragraph breaks (blank lines between paragraphs).\n"
        "• Preserve emotional tone and marketing appeal while remaining accurate.\n"
        "• For seiyuu_en: Transliterate or provide standard English stage-name spelling for each voice actor.\n"
        "• If a phrase has no direct equivalent, rewrite it naturally and add a short explanation to cultural_notes.\n"
        "• Avoid awkward literal renderings (example: do NOT output phrases like 'babu-babu erotic little devil').\n\n"
        "SEXUAL CONTENT RULES (if applicable):\n"
        "• When translating erotic drama CDs, the listener/heroine is typically female.\n"
        "• Masculine-coded slang used metaphorically for female arousal should be rendered appropriately:\n"
        "  - \"you're so turned on\" / \"you're swollen with arousal\" / \"you're this sensitive\"\n"
        "• Preserve emotional intensity without embellishing or adding new acts/details not in the original.\n\n"
        "CULTURAL NOTES:\n"
        "• cultural_notes should be a short list (0-3 items) written for readers, plain English.\n"
        "• Only include notes for terms/concepts that truly need explanation for English readers.\n\n"
        f"title_ja: {title_ja}\n"
        f"description_ja: {description_ja}\n"
        f"seiyuu_ja: {json.dumps(seiyuu_ja, ensure_ascii=False)}"
    )

    last_detail = ""
    resp = None
    for use_response_format in (True, False):
        request_json = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if use_response_format:
            request_json["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://llm.chutes.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_json,
            )
        if resp.status_code >= 400:
            break
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            break
        last_detail = str(data)[:300]
        if not use_response_format:
            break

    if resp is None:
        raise HTTPException(status_code=502, detail="Chutes request failed before response")
    if resp.status_code >= 400:
        detail = f"Chutes API error ({resp.status_code})"
        try:
            err_payload = resp.json()
            err_obj = err_payload.get("error") if isinstance(err_payload, dict) else None
            if isinstance(err_obj, dict):
                msg = str(err_obj.get("message") or "").strip()
                if msg:
                    detail = msg
        except Exception:
            raw = (resp.text or "").strip()
            if raw:
                detail = raw[:500]
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail=f"Chutes quota/rate limit: {detail}")
        raise HTTPException(status_code=502, detail=f"Chutes upstream error: {detail}")

    data = resp.json()
    err_obj = data.get("error") if isinstance(data, dict) else None
    if isinstance(err_obj, dict):
        msg = str(err_obj.get("message") or "").strip()
        if msg:
            raise HTTPException(status_code=502, detail=f"Chutes upstream error: {msg}")
    choices = data.get("choices") or []
    if not choices:
        detail = "Chutes returned no choices"
        if last_detail:
            detail = f"{detail}. Response: {last_detail}"
        raise HTTPException(status_code=502, detail=detail)

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, str):
                txt = entry.strip()
            elif isinstance(entry, dict):
                txt = str(entry.get("text") or entry.get("content") or "").strip()
            else:
                txt = ""
            if txt:
                parts.append(txt)
        content = "\n".join(parts)
    content = str(content or "").strip()
    if not content:
        raise HTTPException(status_code=502, detail="Chutes returned empty content")

    try:
        parsed_raw = _parse_json_payload(content)
    except Exception:
        raise HTTPException(status_code=502, detail="Chutes returned invalid JSON")
    parsed = _coerce_translation_payload(parsed_raw)
    title_en = str(parsed.get("title_en") or "").strip()
    description_en = str(parsed.get("description_en") or "").strip()
    seiyuu_en = _coerce_name_list(parsed.get("seiyuu_en"))
    cultural_notes = _coerce_notes_list(parsed.get("cultural_notes"))
    if not title_en and not description_en and not seiyuu_en and not cultural_notes:
        raise HTTPException(status_code=502, detail="Chutes response missing translated fields")
    return title_en, description_en, seiyuu_en, cultural_notes


async def _translate_title_description_with_openai_compat(title_ja: str, description_ja: str, seiyuu_ja: list[str]) -> tuple[str, str, list[str], list[str]]:
    api_key = await db.get_runtime_openai_compat_api_key()
    model = await db.get_runtime_openai_compat_model()
    base_url = await db.get_runtime_openai_compat_base_url()
    request_format = await db.get_runtime_openai_compat_request_format()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing OpenAI-compatible API key")
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing OpenAI-compatible base URL")
    if not model:
        raise HTTPException(status_code=400, detail="Missing OpenAI-compatible model")

    prompt = (
        "Translate the following Japanese drama CD metadata to natural, idiomatic English.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "• Return ONLY valid JSON object with keys: title_en, description_en, seiyuu_en, cultural_notes\n"
        "• No markdown, no code blocks, no commentary—just pure JSON.\n\n"
        f"title_ja: {title_ja}\n"
        f"description_ja: {description_ja}\n"
        f"seiyuu_ja: {json.dumps(seiyuu_ja, ensure_ascii=False)}"
    )

    if request_format == "anthropic":
        from pipeline.anthropic_compat_translator import (
            _normalize_messages_url,
            _supports_temperature,
            ANTHROPIC_VERSION,
            DEFAULT_MAX_TOKENS,
        )
        endpoint = _normalize_messages_url(base_url)
        request_json = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        # Fable 5 / Opus 4.8 / 4.7 reject sampling params with a 400; only send
        # temperature on models that still accept it.
        if _supports_temperature(model):
            request_json["temperature"] = 0.2
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                json=request_json,
            )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Anthropic-compatible upstream error ({resp.status_code}): {resp.text[:300]}",
            )
        data = resp.json()
        content_field = data.get("content")
        content = ""
        if isinstance(content_field, list):
            parts = []
            for entry in content_field:
                if isinstance(entry, str):
                    txt = entry.strip()
                elif isinstance(entry, dict):
                    txt = str(entry.get("text") or entry.get("content") or "").strip()
                else:
                    txt = ""
                if txt:
                    parts.append(txt)
            content = "\n".join(parts).strip()
        elif isinstance(content_field, str):
            content = content_field.strip()
        if not content:
            raise HTTPException(status_code=502, detail=f"Anthropic-compatible returned empty content: {str(data)[:200]}")
    else:
        from pipeline.openrouter_translator import _normalize_chat_completions_url
        endpoint = _normalize_chat_completions_url(base_url)

        resp = None
        for use_response_format in (True, False):
            request_json = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }
            if use_response_format:
                request_json["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_json,
                )
            if resp.status_code >= 400:
                # Anthropic-via-proxy and similar shims reject response_format;
                # retry once without it before giving up.
                if use_response_format:
                    continue
                break
            data = resp.json()
            if data.get("choices"):
                break
            if not use_response_format:
                break

        if resp is None:
            raise HTTPException(status_code=502, detail="OpenAI-compatible request failed before response")
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"OpenAI-compatible upstream error ({resp.status_code}): {resp.text[:300]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise HTTPException(status_code=502, detail="OpenAI-compatible returned no choices")
        message = choices[0].get("message") or {}
        content_field = message.get("content")
        if isinstance(content_field, list):
            parts = []
            for entry in content_field:
                txt = entry.strip() if isinstance(entry, str) else (
                    str(entry.get("text") or entry.get("content") or "").strip() if isinstance(entry, dict) else ""
                )
                if txt:
                    parts.append(txt)
            content = "\n".join(parts)
        else:
            content = str(content_field or "")
        content = content.strip()
        if not content:
            raise HTTPException(status_code=502, detail="OpenAI-compatible returned empty content")

    try:
        parsed_raw = _parse_json_payload(content)
    except Exception:
        raise HTTPException(status_code=502, detail="OpenAI-compatible returned invalid JSON")
    parsed = _coerce_translation_payload(parsed_raw)
    title_en = str(parsed.get("title_en") or "").strip()
    description_en = str(parsed.get("description_en") or "").strip()
    seiyuu_en = _coerce_name_list(parsed.get("seiyuu_en"))
    cultural_notes = _coerce_notes_list(parsed.get("cultural_notes"))
    if not title_en and not description_en and not seiyuu_en and not cultural_notes:
        raise HTTPException(status_code=502, detail="OpenAI-compatible response missing translated fields")
    return title_en, description_en, seiyuu_en, cultural_notes


async def _run_metadata_translation_with_provider(provider: str, title_ja: str, description_ja: str, seiyuu_ja: list[str]):
    chosen = str(provider or "gemini").strip().lower()
    if chosen == "openrouter":
        return await _translate_title_description_with_openrouter(title_ja, description_ja, seiyuu_ja)
    if chosen == "chutes":
        return await _translate_title_description_with_chutes(title_ja, description_ja, seiyuu_ja)
    if chosen == "openai_compat":
        return await _translate_title_description_with_openai_compat(title_ja, description_ja, seiyuu_ja)
    return await _translate_title_description_with_gemini(title_ja, description_ja, seiyuu_ja)


async def _provider_has_key(provider: str) -> bool:
    chosen = str(provider or "gemini").strip().lower()
    if chosen == "openrouter":
        return bool(await db.get_runtime_openrouter_api_key())
    if chosen == "chutes":
        return bool(await db.get_runtime_chutes_api_key())
    if chosen == "openai_compat":
        return bool(
            await db.get_runtime_openai_compat_api_key()
            and await db.get_runtime_openai_compat_base_url()
            and await db.get_runtime_openai_compat_model()
        )
    return bool(await db.get_runtime_gemini_api_key())


async def _llm_one_shot_json(prompt: str) -> tuple[str, str]:
    """Run a single-shot LLM request against the active provider and return
    (raw_text, provider). The caller is responsible for parsing the JSON."""
    provider = await db.get_runtime_translation_provider()

    if provider == "gemini":
        api_key = await db.get_runtime_gemini_api_key()
        model = await db.get_runtime_gemini_model()
        if not api_key:
            raise HTTPException(status_code=400, detail="Missing Gemini API key")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                params={"key": api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
                },
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Gemini upstream error ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise HTTPException(status_code=502, detail="Gemini returned no candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text = str(parts[0].get("text") or "") if parts else ""
        return text, provider

    if provider == "openai_compat":
        api_key = await db.get_runtime_openai_compat_api_key()
        model = await db.get_runtime_openai_compat_model()
        base_url = await db.get_runtime_openai_compat_base_url()
        request_format = await db.get_runtime_openai_compat_request_format()
        if not (api_key and base_url and model):
            raise HTTPException(status_code=400, detail="OpenAI-compatible provider not fully configured")

        if request_format == "anthropic":
            from pipeline.anthropic_compat_translator import (
                _normalize_messages_url,
                _supports_temperature,
                ANTHROPIC_VERSION,
                DEFAULT_MAX_TOKENS,
            )
            endpoint = _normalize_messages_url(base_url)
            anthropic_body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": DEFAULT_MAX_TOKENS,
            }
            # Fable 5 / Opus 4.8 / 4.7 reject sampling params with a 400; only
            # send temperature on models that still accept it.
            if _supports_temperature(model):
                anthropic_body["temperature"] = 0.2
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                    },
                    json=anthropic_body,
                )
            if resp.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Anthropic upstream error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            content = data.get("content")
            if isinstance(content, list):
                parts = []
                for entry in content:
                    if isinstance(entry, dict):
                        txt = str(entry.get("text") or "").strip()
                        if txt:
                            parts.append(txt)
                return ("\n".join(parts), provider)
            if isinstance(content, str):
                return (content, provider)
            raise HTTPException(status_code=502, detail="Anthropic response had no content")

        # OpenAI-format chat completions (with response_format fallback)
        from pipeline.openrouter_translator import _normalize_chat_completions_url
        endpoint = _normalize_chat_completions_url(base_url)
        return await _openai_chat_call(endpoint, api_key, model, prompt), provider

    if provider == "openrouter":
        api_key = await db.get_runtime_openrouter_api_key()
        model = await db.get_runtime_openrouter_model()
        if not api_key:
            raise HTTPException(status_code=400, detail="Missing OpenRouter API key")
        return await _openai_chat_call("https://openrouter.ai/api/v1/chat/completions", api_key, model, prompt), provider

    if provider == "chutes":
        api_key = await db.get_runtime_chutes_api_key()
        model = await db.get_runtime_chutes_model()
        if not api_key:
            raise HTTPException(status_code=400, detail="Missing Chutes API key")
        return await _openai_chat_call("https://llm.chutes.ai/v1/chat/completions", api_key, model, prompt), provider

    raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")


async def _openai_chat_call(endpoint: str, api_key: str, model: str, prompt: str) -> str:
    """OpenAI-style chat completion with response_format fallback. Returns raw text."""
    resp = None
    for use_response_format in (True, False):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if use_response_format:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code >= 400:
            if use_response_format:
                continue
            break
        data = resp.json()
        if data.get("choices"):
            break

    if resp is None or resp.status_code >= 400:
        status = resp.status_code if resp is not None else 0
        text = (resp.text[:300] if resp is not None else "")
        raise HTTPException(status_code=502, detail=f"Upstream error ({status}): {text}")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="Upstream returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, str):
                parts.append(entry.strip())
            elif isinstance(entry, dict):
                txt = str(entry.get("text") or entry.get("content") or "").strip()
                if txt:
                    parts.append(txt)
        return "\n".join(parts).strip()
    return str(content or "").strip()


@router.get("/settings/ai")
async def get_ai_settings():
    runtime_gemini_key = await db.get_app_setting(db.RUNTIME_GEMINI_API_KEY_SETTING)
    runtime_openrouter_key = await db.get_app_setting(db.RUNTIME_OPENROUTER_API_KEY_SETTING)
    runtime_chutes_key = await db.get_app_setting(db.RUNTIME_CHUTES_API_KEY_SETTING)
    runtime_openai_compat_key = await db.get_app_setting(db.RUNTIME_OPENAI_COMPAT_API_KEY_SETTING)
    runtime_openai_compat_url = await db.get_app_setting(db.RUNTIME_OPENAI_COMPAT_BASE_URL_SETTING)
    effective_gemini_key = await db.get_runtime_gemini_api_key()
    effective_openrouter_key = await db.get_runtime_openrouter_api_key()
    effective_chutes_key = await db.get_runtime_chutes_api_key()
    effective_openai_compat_key = await db.get_runtime_openai_compat_api_key()
    effective_openai_compat_url = await db.get_runtime_openai_compat_base_url()
    gemini_model = await db.get_runtime_gemini_model()
    openrouter_model = await db.get_runtime_openrouter_model()
    chutes_model = await db.get_runtime_chutes_model()
    openai_compat_model = await db.get_runtime_openai_compat_model()
    openai_compat_request_format = await db.get_runtime_openai_compat_request_format()
    translation_provider = await db.get_runtime_translation_provider()
    return {
        "translation_provider": translation_provider,
        "gemini_model": gemini_model,
        "gemini_has_api_key": bool(effective_gemini_key),
        "gemini_api_key_source": "runtime" if runtime_gemini_key else "env",
        "openrouter_model": openrouter_model,
        "openrouter_has_api_key": bool(effective_openrouter_key),
        "openrouter_api_key_source": "runtime" if runtime_openrouter_key else "env",
        "chutes_model": chutes_model,
        "chutes_has_api_key": bool(effective_chutes_key),
        "chutes_api_key_source": "runtime" if runtime_chutes_key else "env",
        "openai_compat_model": openai_compat_model,
        "openai_compat_base_url": effective_openai_compat_url,
        "openai_compat_has_api_key": bool(effective_openai_compat_key),
        "openai_compat_api_key_source": "runtime" if runtime_openai_compat_key else "env",
        "openai_compat_base_url_source": "runtime" if runtime_openai_compat_url else "env",
        "openai_compat_request_format": openai_compat_request_format,
    }


@router.get("/settings/ai/openai-compat-models")
async def list_openai_compat_models():
    """Probe the configured base URL's /models endpoint and return model ids."""
    api_key = await db.get_runtime_openai_compat_api_key()
    base_url = await db.get_runtime_openai_compat_base_url()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing OpenAI-compatible API key")
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing OpenAI-compatible base URL")
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    models_url = f"{url}/models"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Models endpoint error ({resp.status_code}): {resp.text[:300]}",
        )
    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Models endpoint returned invalid JSON")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload if isinstance(payload, list) else []
    ids: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            mid = str(row.get("id") or row.get("name") or "").strip()
            if mid:
                ids.append(mid)
        elif isinstance(row, str):
            mid = row.strip()
            if mid:
                ids.append(mid)
    ids.sort()
    return {"models": ids, "count": len(ids), "url": models_url}


@router.post("/settings/ai/test")
async def test_ai_settings(_auth=Depends(require_api_key)):
    provider = await db.get_runtime_translation_provider()
    has_key = await _provider_has_key(provider)
    if not has_key:
        raise HTTPException(status_code=400, detail=f"Missing API key for provider '{provider}'")

    probe_title = "テストタイトル"
    probe_desc = "これは接続テストです。自然な英語に翻訳してください。"
    probe_seiyuu = ["テスト声優"]
    result = await _run_metadata_translation_with_provider(provider, probe_title, probe_desc, probe_seiyuu)
    title_en, description_en, _seiyuu_en, _notes = _unpack_metadata_translation_result(result)
    return {
        "status": "ok",
        "provider": provider,
        "sample_title_en": title_en,
        "sample_description_en": description_en[:120],
    }


@router.put("/settings/ai")
async def update_ai_settings(request: AiSettingsUpdateRequest, _auth=Depends(require_api_key)):
    updated_fields: list[str] = []

    if request.translation_provider is not None:
        provider = str(request.translation_provider).strip().lower()
        if provider not in db.SUPPORTED_TRANSLATION_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail="translation_provider must be one of: " + ", ".join(sorted(db.SUPPORTED_TRANSLATION_PROVIDERS)),
            )
        await db.set_runtime_translation_provider(provider)
        updated_fields.append("translation_provider")

    if request.clear_gemini_api_key:
        await db.clear_runtime_gemini_api_key()
        updated_fields.append("gemini_api_key")

    if request.gemini_api_key is not None:
        clean_key = str(request.gemini_api_key).strip()
        if clean_key:
            await db.set_runtime_gemini_api_key(clean_key)
            updated_fields.append("gemini_api_key")
        elif not request.clear_gemini_api_key:
            raise HTTPException(status_code=400, detail="gemini_api_key cannot be empty")

    if request.gemini_model is not None:
        clean_model = str(request.gemini_model).strip()
        if not clean_model:
            raise HTTPException(status_code=400, detail="gemini_model cannot be empty")
        await db.set_runtime_gemini_model(clean_model)
        updated_fields.append("gemini_model")

    if request.clear_openrouter_api_key:
        await db.clear_runtime_openrouter_api_key()
        updated_fields.append("openrouter_api_key")

    if request.openrouter_api_key is not None:
        clean_key = str(request.openrouter_api_key).strip()
        if clean_key:
            await db.set_runtime_openrouter_api_key(clean_key)
            updated_fields.append("openrouter_api_key")
        elif not request.clear_openrouter_api_key:
            raise HTTPException(status_code=400, detail="openrouter_api_key cannot be empty")

    if request.openrouter_model is not None:
        clean_model = str(request.openrouter_model).strip()
        if not clean_model:
            raise HTTPException(status_code=400, detail="openrouter_model cannot be empty")
        await db.set_runtime_openrouter_model(clean_model)
        updated_fields.append("openrouter_model")

    if request.clear_chutes_api_key:
        await db.clear_runtime_chutes_api_key()
        updated_fields.append("chutes_api_key")

    if request.chutes_api_key is not None:
        clean_key = str(request.chutes_api_key).strip()
        if clean_key:
            await db.set_runtime_chutes_api_key(clean_key)
            updated_fields.append("chutes_api_key")
        elif not request.clear_chutes_api_key:
            raise HTTPException(status_code=400, detail="chutes_api_key cannot be empty")

    if request.chutes_model is not None:
        clean_model = str(request.chutes_model).strip()
        if not clean_model:
            raise HTTPException(status_code=400, detail="chutes_model cannot be empty")
        await db.set_runtime_chutes_model(clean_model)
        updated_fields.append("chutes_model")

    if request.clear_openai_compat_api_key:
        await db.clear_runtime_openai_compat_api_key()
        updated_fields.append("openai_compat_api_key")

    if request.openai_compat_api_key is not None:
        clean_key = str(request.openai_compat_api_key).strip()
        if clean_key:
            await db.set_runtime_openai_compat_api_key(clean_key)
            updated_fields.append("openai_compat_api_key")
        elif not request.clear_openai_compat_api_key:
            raise HTTPException(status_code=400, detail="openai_compat_api_key cannot be empty")

    if request.clear_openai_compat_base_url:
        await db.clear_runtime_openai_compat_base_url()
        updated_fields.append("openai_compat_base_url")

    if request.openai_compat_base_url is not None:
        clean_url = str(request.openai_compat_base_url).strip()
        if clean_url:
            try:
                await db.set_runtime_openai_compat_base_url(clean_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            updated_fields.append("openai_compat_base_url")
        elif not request.clear_openai_compat_base_url:
            raise HTTPException(status_code=400, detail="openai_compat_base_url cannot be empty")

    if request.openai_compat_model is not None:
        clean_model = str(request.openai_compat_model).strip()
        if not clean_model:
            raise HTTPException(status_code=400, detail="openai_compat_model cannot be empty")
        await db.set_runtime_openai_compat_model(clean_model)
        updated_fields.append("openai_compat_model")

    if request.openai_compat_request_format is not None:
        clean_fmt = str(request.openai_compat_request_format).strip().lower()
        if clean_fmt not in {"openai", "anthropic"}:
            raise HTTPException(status_code=400, detail="openai_compat_request_format must be 'openai' or 'anthropic'")
        await db.set_runtime_openai_compat_request_format(clean_fmt)
        updated_fields.append("openai_compat_request_format")

    if not updated_fields:
        raise HTTPException(status_code=400, detail="No settings provided")

    runtime_gemini_key = await db.get_app_setting(db.RUNTIME_GEMINI_API_KEY_SETTING)
    runtime_openrouter_key = await db.get_app_setting(db.RUNTIME_OPENROUTER_API_KEY_SETTING)
    runtime_chutes_key = await db.get_app_setting(db.RUNTIME_CHUTES_API_KEY_SETTING)
    effective_gemini_key = await db.get_runtime_gemini_api_key()
    effective_openrouter_key = await db.get_runtime_openrouter_api_key()
    effective_chutes_key = await db.get_runtime_chutes_api_key()
    gemini_model = await db.get_runtime_gemini_model()
    openrouter_model = await db.get_runtime_openrouter_model()
    chutes_model = await db.get_runtime_chutes_model()
    translation_provider = await db.get_runtime_translation_provider()
    return {
        "status": "updated",
        "updated_fields": updated_fields,
        "translation_provider": translation_provider,
        "gemini_model": gemini_model,
        "gemini_has_api_key": bool(effective_gemini_key),
        "gemini_api_key_source": "runtime" if runtime_gemini_key else "env",
        "openrouter_model": openrouter_model,
        "openrouter_has_api_key": bool(effective_openrouter_key),
        "openrouter_api_key_source": "runtime" if runtime_openrouter_key else "env",
        "chutes_model": chutes_model,
        "chutes_has_api_key": bool(effective_chutes_key),
        "chutes_api_key_source": "runtime" if runtime_chutes_key else "env",
    }


@router.get("/settings/whisper")
async def get_whisper_settings():
    return {
        "model": await db.get_runtime_whisper_model(),
        "vad_filter": await db.get_runtime_whisper_vad_filter(),
        "beam_size": await db.get_runtime_whisper_beam_size(),
        "condition_on_previous_text": await db.get_runtime_whisper_condition_on_previous(),
        "preferred_variant": await db.get_runtime_whisper_preferred_variant(),
        "supported_models": list(db.SUPPORTED_WHISPER_MODELS),
    }


@router.put("/settings/whisper")
async def update_whisper_settings(
    request: WhisperSettingsUpdateRequest,
    _auth=Depends(require_api_key),
):
    updated: list[str] = []
    if request.model is not None:
        try:
            await db.set_runtime_whisper_model(request.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        updated.append("model")
    if request.vad_filter is not None:
        await db.set_runtime_whisper_vad_filter(bool(request.vad_filter))
        updated.append("vad_filter")
    if request.beam_size is not None:
        try:
            await db.set_runtime_whisper_beam_size(request.beam_size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        updated.append("beam_size")
    if request.condition_on_previous_text is not None:
        await db.set_runtime_whisper_condition_on_previous(bool(request.condition_on_previous_text))
        updated.append("condition_on_previous_text")
    if request.preferred_variant is not None:
        try:
            await db.set_runtime_whisper_preferred_variant(request.preferred_variant)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        updated.append("preferred_variant")
    if not updated:
        raise HTTPException(status_code=400, detail="No settings provided")
    return {
        "status": "updated",
        "updated_fields": updated,
        "model": await db.get_runtime_whisper_model(),
        "vad_filter": await db.get_runtime_whisper_vad_filter(),
        "beam_size": await db.get_runtime_whisper_beam_size(),
        "condition_on_previous_text": await db.get_runtime_whisper_condition_on_previous(),
        "preferred_variant": await db.get_runtime_whisper_preferred_variant(),
    }


def _format_english_description(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    # Keep model-provided paragraphs if already present.
    if "\n\n" in raw:
        lines = [line.strip() for line in raw.splitlines()]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return normalized

    # Insert paragraph breaks after every 3-4 sentences for readability.
    sentences = [s.strip() for s in EN_SENTENCE_SPLIT_RE.split(raw) if s.strip()]
    if len(sentences) <= 4:
        return " ".join(sentences)

    paragraphs = []
    i = 0
    while i < len(sentences):
        size = 4
        remaining = len(sentences) - i
        if remaining <= 5:
            size = remaining
        elif remaining % 4 == 1:
            size = 3
        chunk = " ".join(sentences[i : i + size]).strip()
        if chunk:
            paragraphs.append(chunk)
        i += size

    return "\n\n".join(paragraphs).strip()


# More specific routes MUST come before generic {item_id} route
@router.put("/items/{item_id}/override-code")
async def override_code(
    item_id: int,
    request: OverrideCodeRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    """Override the product code for an item, clear metadata, and re-fetch."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Extract product code from URL or raw input
    raw = request.product_code.strip()
    match = DLSITE_URL_PATTERN.search(raw)
    if match:
        new_code = match.group(0).upper()
    else:
        # Try as bare code
        new_code = raw.upper()
        if not re.match(r'^(RJ|BJ|VJ)\d{6,8}$', new_code):
            raise HTTPException(status_code=400, detail="Invalid product code. Use RJ/BJ/VJ followed by 6-8 digits, or paste a DLsite URL.")

    updated = await db.override_product_code(item_id, new_code)
    if updated is None:
        raise HTTPException(status_code=409, detail=f"Product code {new_code} already exists in the library.")

    # Re-fetch metadata in background
    background_tasks.add_task(_refetch_metadata, item_id, new_code)

    return updated


_DLSITE_CODE_RE = re.compile(r'^(RJ|BJ|VJ)\d+$', re.IGNORECASE)


async def _download_item_cover_from_url(item_id: int, product_code: str, cover_url: str) -> str | None:
    """Server-side cover fetch for an items row — mirrors the games path so
    we don't depend on browser fetch() succeeding cross-origin (VNDB's CDN
    historically blocks CORS). Returns the relative cover_local path or
    None on failure."""
    import logging
    logger = logging.getLogger(__name__)
    if not cover_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(cover_url, follow_redirects=True)
            resp.raise_for_status()
            content = resp.content
        if not content or len(content) > MAX_COVER_BYTES:
            return None
        match = re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", cover_url, re.IGNORECASE)
        suffix = f".{match.group(1).lower()}" if match else ".jpg"
        if suffix not in ALLOWED_COVER_EXTENSIONS:
            suffix = ".jpg"
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        safe_code = (product_code or f"item{item_id}").upper()
        filename = f"{safe_code}_remote_{uuid4().hex[:8]}{suffix}"
        target = COVERS_DIR / filename
        target.write_bytes(content)
        cover_local = str(target.relative_to(COVERS_DIR.parent.parent))
        await db.set_item_cover(item_id, cover_local=cover_local, cover_url=cover_url)
        return cover_local
    except Exception as exc:
        logger.warning("Failed to download cover for item %s from %s: %s", item_id, cover_url, exc)
        return None


@router.post("/items/{item_id}/cover-from-url")
async def upload_cover_from_url(item_id: int, payload: dict, _auth=Depends(require_api_key)):
    """Server-side cover fetch — frontend posts {cover_url}, server downloads
    + writes to COVERS_DIR. Used by the VNDB tokuten link flow (browsers
    can't reliably fetch VNDB's CDN cross-origin)."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    cover_url = (payload or {}).get("cover_url") or ""
    if not cover_url:
        raise HTTPException(status_code=400, detail="cover_url is required")
    cover_local = await _download_item_cover_from_url(
        item_id, item.get("product_code") or "", cover_url
    )
    if not cover_local:
        raise HTTPException(status_code=502, detail="Cover download failed")
    return await db.get_item(item_id)


def _media_url(path: str) -> str:
    """media_assets.path → browser URL. Two historical formats coexist:
    'data/covers/X.jpg' (project-root-relative; metadata apply + cover
    uploads) and 'tokutens/5/gallery/x.jpg' (COVERS_DIR-relative; tokuten
    scanner). Both live under data/covers, served by the /covers mount."""
    p = (path or "").replace("\\", "/")
    if p.startswith("data/covers/"):
        p = p[len("data/covers/"):]
    return "/covers/" + p


async def _item_media_rows(conn, item: dict) -> list[dict]:
    """Gallery rows for an items row — its own media plus, for tokuten
    cards, the linked tokutens row's media (the scanner writes there)."""
    parents = [("item", item["id"])]
    if item.get("tokuten_id"):
        parents.append(("tokuten", item["tokuten_id"]))
    out = []
    for parent_kind, parent_id in parents:
        cur = await conn.execute(
            """SELECT id, parent_kind, parent_id, path, role, sort_order
               FROM media_assets
               WHERE parent_kind = ? AND parent_id = ?
               ORDER BY sort_order ASC, id ASC""",
            (parent_kind, parent_id),
        )
        for row in await cur.fetchall():
            entry = dict(row)
            entry["url"] = _media_url(entry["path"])
            out.append(entry)
    return out


@router.get("/items/{item_id}/media")
async def list_item_media(item_id: int):
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    conn = await db.get_db()
    try:
        media = await _item_media_rows(conn, item)
    finally:
        await conn.close()
    return {"media": media}


async def _get_owned_media(conn, item: dict, media_id: int):
    """Fetch a media_assets row only if it belongs to this item (directly
    or via its linked tokuten)."""
    cur = await conn.execute("SELECT * FROM media_assets WHERE id = ?", (media_id,))
    row = await cur.fetchone()
    if not row:
        return None
    owned = (row["parent_kind"] == "item" and row["parent_id"] == item["id"]) or (
        row["parent_kind"] == "tokuten" and row["parent_id"] == item.get("tokuten_id")
    )
    return dict(row) if owned else None


@router.post("/items/{item_id}/media/{media_id}/set-cover")
async def set_cover_from_media(item_id: int, media_id: int, _auth=Depends(require_api_key)):
    """Promote a gallery image to the entry's primary cover. The file
    already lives under data/covers, so this is just a pointer update (the
    old cover file stays on disk and remains reachable via the gallery)."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    conn = await db.get_db()
    try:
        media = await _get_owned_media(conn, item, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Media not found for this item")
        if item.get("tokuten_id"):
            await conn.execute(
                "UPDATE tokutens SET cover_local = ?, updated_at = ? WHERE id = ?",
                (media["path"], datetime.now().isoformat(), item["tokuten_id"]),
            )
            await conn.commit()
    finally:
        await conn.close()
    return await db.set_item_cover(item_id, media["path"])


@router.delete("/items/{item_id}/media/{media_id}")
async def delete_item_media(item_id: int, media_id: int, _auth=Depends(require_api_key)):
    """Remove a gallery row. The image file stays on disk — it may still be
    referenced as a primary cover, and disk is cheap; only the gallery
    listing goes away."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    conn = await db.get_db()
    try:
        media = await _get_owned_media(conn, item, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Media not found for this item")
        await conn.execute("DELETE FROM media_assets WHERE id = ?", (media_id,))
        await conn.commit()
    finally:
        await conn.close()
    return {"deleted": media_id}


@router.post("/items/{item_id}/refresh-metadata")
async def refresh_metadata(item_id: int, _auth=Depends(require_api_key)):
    """Source-aware metadata refresh:
      - Tokuten items with a linked VNDB id → fetch from VNDB.
      - Items with a real DLsite product code (RJ/BJ/VJ) → fetch from DLsite.
      - Anything else (manual / no link) → return a clear error rather than
        pretending to scrape DLsite for a synthetic code.
    Cover download happens inline so the user doesn't need a separate
    "fetch cover" step."""
    import asyncio
    import logging
    from config import DLSITE_PROXY_URL
    import vndb_client
    logger = logging.getLogger(__name__)

    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # --- Tokuten path (linked bonus-CD entries) ---
    if item.get("kind") == "tokuten_audio" and item.get("tokuten_id"):
        # Look up the tokuten row directly so we can read its vndb_id + source path.
        conn = await db.get_db()
        try:
            cur = await conn.execute(
                "SELECT vndb_id, title, local_path FROM tokutens WHERE id = ?",
                (item["tokuten_id"],),
            )
            tk_row = await cur.fetchone()
        finally:
            await conn.close()

        # 1) Prefer a bundled *.package.json in the tokuten's archive/folder (same treatment
        #    as custom drama CDs). The rich metadata lands on the paired item; the fields the
        #    tokuten record itself carries are mirrored so the Tokutens tab reflects it.
        tk_local = (tk_row["local_path"] if tk_row else None) or ""
        if tk_local:
            from pipeline.package_import import find_package_at_path
            payload = await asyncio.to_thread(find_package_at_path, tk_local)
            if payload and item.get("product_code"):
                meta = payload["items"][0]["metadata"]
                await db.update_item_metadata(item["product_code"], meta)
                tk_patch = {k: meta[k] for k in ("title", "title_en", "release_date") if meta.get(k)}
                if tk_patch:
                    import datetime as _dt
                    tk_patch["updated_at"] = _dt.datetime.now().isoformat()
                    conn = await db.get_db()
                    try:
                        sets = ", ".join(f"{k} = ?" for k in tk_patch)
                        await conn.execute(
                            f"UPDATE tokutens SET {sets} WHERE id = ?",
                            list(tk_patch.values()) + [item["tokuten_id"]],
                        )
                        await conn.commit()
                    finally:
                        await conn.close()
                logger.info(f"Refreshed tokuten {item['tokuten_id']} (item {item_id}) from bundled package")
                return {
                    "success": True,
                    "product_code": item.get("product_code"),
                    "title": meta.get("title"),
                    "message": "Metadata refreshed from bundled package",
                }

        # 2) else fall back to VNDB (a linked game).
        tk_vndb = (tk_row["vndb_id"] if tk_row else None) or ""
        if not tk_vndb:
            raise HTTPException(
                status_code=400,
                detail="This tokuten has no linked VNDB id and no bundled .package.json. Edit the tokuten and pick a VNDB game, or bundle a metadata JSON in its archive/folder.",
            )
        try:
            vn = await vndb_client.get_vn(tk_vndb)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"VNDB lookup failed: {exc}") from exc
        if not vn:
            raise HTTPException(status_code=404, detail=f"No VNDB entry for {tk_vndb}")
        fields = vndb_client.vn_to_game_fields(vn)
        # Map VNDB → items columns. VNDB's `developer` doubles as the
        # publishing studio; tokutens carry it in `circle` for parity with
        # drama-CD rows.
        items_patch: dict = {}
        if fields.get("title"):
            items_patch["title"] = fields["title"]
        if fields.get("title_en"):
            items_patch["title_en"] = fields["title_en"]
        if fields.get("description"):
            items_patch["description"] = fields["description"]
        if fields.get("release_date"):
            items_patch["release_date"] = fields["release_date"]
        if fields.get("developer"):
            items_patch["circle"] = fields["developer"]
        if items_patch:
            await db.update_item_user_data(item_id, items_patch)
        cover_url = fields.get("cover_url") or ""
        if cover_url and not item.get("cover_local"):
            await _download_item_cover_from_url(
                item_id, item.get("product_code") or "", cover_url
            )
        return {
            "success": True,
            "vndb_id": tk_vndb,
            "title": fields.get("title"),
            "message": "Tokuten metadata refreshed from VNDB",
        }

    product_code = item.get("product_code") or item.get("original_code")

    # --- Bundled-package path (custom / non-DLsite items) ---
    # A custom item (is_manual / synthetic code) has no DLsite page. If its archive ships a
    # *.package.json, refresh re-reads it and re-applies that metadata - this is what the
    # button should do for non-DLsite entries instead of pretending to scrape DLsite for a
    # synthetic code. Only error if there's genuinely no bundled metadata to defer to.
    if not product_code or not _DLSITE_CODE_RE.match(product_code):
        from pipeline.package_import import find_package_in_archive
        from pipeline.extractor import _resolve_archives_for_item
        scan_paths = await db.get_scan_paths()
        archives = await asyncio.to_thread(_resolve_archives_for_item, item, scan_paths)
        payload = None
        for arc in archives:
            payload = await asyncio.to_thread(find_package_in_archive, arc)
            if payload:
                break
        if payload and item.get("product_code"):
            ip = next((it for it in (payload.get("items") or []) if it.get("metadata")), None)
            meta = ip.get("metadata") if ip else None
            if meta:
                await db.update_item_metadata(item["product_code"], meta)
                logger.info(f"Refreshed metadata for {item['product_code']} (item {item_id}) from bundled package")
                return {
                    "success": True,
                    "product_code": item["product_code"],
                    "title": meta.get("title"),
                    "message": "Metadata refreshed from bundled package",
                }
        raise HTTPException(
            status_code=400,
            detail="No metadata source for this item. Tokutens need a linked VNDB id; drama CDs need a real DLsite code (RJ/BJ/VJ); custom items need a bundled .package.json inside their archive.",
        )

    # --- DLsite path (real product codes only) ---
    try:
        client_kwargs = {"timeout": 30.0}
        if DLSITE_PROXY_URL:
            client_kwargs["proxies"] = DLSITE_PROXY_URL
        async with httpx.AsyncClient(**client_kwargs) as client:
            metadata, reason = await fetch_metadata_for_code(client, product_code)
            if metadata:
                await db.update_item_metadata(product_code, metadata)
                logger.info(f"Force-refreshed metadata for {product_code} (item {item_id}): {metadata.get('title', 'untitled')}")
                return {
                    "success": True,
                    "product_code": product_code,
                    "title": metadata.get("title"),
                    "message": "Metadata refreshed successfully"
                }
            else:
                logger.warning(f"Failed to refresh metadata for {product_code} (item {item_id}): {reason}")
                raise HTTPException(
                    status_code=404,
                    detail=f"Could not fetch metadata from DLsite. Reason: {reason}. Product code may not exist or API may be unavailable."
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="DLsite API timeout")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error: {str(e)}")
    except Exception as e:
        logger.error(f"Error refreshing metadata for item {item_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@router.put("/items/{item_id}/confirm")
async def confirm_match(item_id: int, request: ConfirmMatchRequest, _auth=Depends(require_api_key)):
    """Mark a product code match as verified."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    updated = await db.set_confidence_verified(item_id)
    if updated is None:
        raise HTTPException(status_code=400, detail="Could not confirm match.")

    return updated


@router.put("/items/{item_id}/unconfirm")
async def unconfirm_match(item_id: int, request: ConfirmMatchRequest, _auth=Depends(require_api_key)):
    """Revert a verified match back to its original confidence level."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    updated = await db.revert_confidence(item_id)
    if updated is None:
        raise HTTPException(status_code=400, detail="Cannot unconfirm: no original confidence level found.")

    return updated


@router.get("/items/{item_id}/glossary")
async def get_item_glossary_endpoint(item_id: int):
    """Read the per-item translator glossary. Public read so the Workshop can
    load it without prompting for the API key."""
    glossary = await db.get_item_glossary(item_id)
    if glossary is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"item_id": item_id, "glossary": glossary}


@router.put("/items/{item_id}/glossary")
async def set_item_glossary_endpoint(item_id: int, payload: dict, _auth=Depends(require_api_key)):
    """Write the per-item translator glossary. Accepts ``{"glossary": "..."}``.
    Empty string clears it."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    glossary = payload.get("glossary", "")
    if glossary is None:
        glossary = ""
    if not isinstance(glossary, str):
        raise HTTPException(status_code=400, detail="glossary must be a string")
    ok = await db.set_item_glossary(item_id, glossary)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"item_id": item_id, "glossary": glossary}


@router.patch("/items/{item_id}/manual-track-count")
async def set_item_manual_track_count_endpoint(item_id: int, payload: dict, _auth=Depends(require_api_key)):
    """Set or clear the manual track-count override on an item. Send
    ``{"count": null}`` to revert to the auto value."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    raw = payload.get("count") if isinstance(payload, dict) else None
    count: int | None
    if raw is None:
        count = None
    else:
        try:
            count = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="count must be an integer or null")
        if count < 0:
            raise HTTPException(status_code=400, detail="count must be >= 0")
    await db.set_item_manual_track_count(item_id, count)
    return {"item_id": item_id, "manual_track_count": count}


@router.post("/items/{item_id}/translate-metadata")
async def translate_item_metadata(item_id: int, _auth=Depends(require_api_key)):
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    title_ja = str(item.get("title") or "").strip()
    description_ja = str(item.get("description") or "").strip()
    try:
        seiyuu_ja = json.loads(item.get("seiyuu") or "[]")
        if not isinstance(seiyuu_ja, list):
            seiyuu_ja = []
    except Exception:
        seiyuu_ja = []
    if not title_ja and not description_ja and not seiyuu_ja:
        raise HTTPException(status_code=400, detail="No JP title/description available to translate")

    provider = await db.get_runtime_translation_provider()
    fallback_used = False
    used_provider = provider
    try:
        result = await _run_metadata_translation_with_provider(provider, title_ja, description_ja, seiyuu_ja)
    except HTTPException as exc:
        can_fallback = exc.status_code == 429
        if can_fallback:
            fallback_order = [p for p in ("gemini", "openrouter", "chutes") if p != provider]
            switched = False
            for alt_provider in fallback_order:
                if not await _provider_has_key(alt_provider):
                    continue
                used_provider = alt_provider
                fallback_used = True
                result = await _run_metadata_translation_with_provider(alt_provider, title_ja, description_ja, seiyuu_ja)
                switched = True
                break
            if not switched:
                raise
        else:
            raise
    title_en, description_en, seiyuu_en, cultural_notes = _unpack_metadata_translation_result(result)
    description_en = _format_english_description(description_en)
    if cultural_notes and description_en:
        description_en = (
            f"{description_en}\n\n"
            + "\n".join([f"* Translator note: {note}" for note in cultural_notes])
        ).strip()
    try:
        existing_seiyuu_en = json.loads(item.get("seiyuu_en") or "[]")
        if not isinstance(existing_seiyuu_en, list):
            existing_seiyuu_en = []
    except Exception:
        existing_seiyuu_en = []
    if seiyuu_en:
        updated = await db.set_item_english_metadata(
            item_id,
            title_en=title_en or item.get("title_en"),
            description_en=description_en or item.get("description_en"),
            seiyuu_en=seiyuu_en or existing_seiyuu_en,
        )
    else:
        updated = await db.set_item_english_text(
            item_id,
            title_en=title_en or item.get("title_en"),
            description_en=description_en or item.get("description_en"),
        )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to persist translated metadata")

    return {
        "status": "translated",
        "item_id": item_id,
        "provider_used": used_provider,
        "fallback_used": fallback_used,
        "title_en": updated.get("title_en"),
        "description_en": updated.get("description_en"),
        "seiyuu_en": updated.get("seiyuu_en"),
        "item": updated,
    }


@router.put("/items/{item_id}/cover")
async def upload_cover(item_id: int, request: ManualCoverRequest, _auth=Depends(require_api_key)):
    """Upload a manual cover image for an item."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    original_name = request.filename or "cover"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_COVER_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported image type. Use JPG, PNG, or WEBP.")

    try:
        if "," not in request.data_url:
            raise ValueError("Invalid data URL")
        _prefix, encoded = request.data_url.split(",", 1)
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid cover data.")

    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > MAX_COVER_BYTES:
        raise HTTPException(status_code=400, detail="Cover is too large (max 8MB).")

    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    safe_code = (item.get("product_code") or f"item{item_id}").upper()
    new_filename = f"{safe_code}_manual_{uuid4().hex[:8]}{suffix}"
    target_path = COVERS_DIR / new_filename
    target_path.write_bytes(content)

    old_cover = item.get("cover_local")
    if old_cover:
        try:
            old_cover_path = COVERS_DIR / Path(old_cover).name
            if old_cover_path.exists() and old_cover_path != target_path:
                old_cover_path.unlink()
        except Exception:
            pass

    cover_local = str(target_path.relative_to(COVERS_DIR.parent.parent))
    updated = await db.set_item_cover(item_id, cover_local=cover_local, cover_url=None)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to save cover.")
    return updated


@router.put("/bulk/items/confirm")
async def bulk_confirm_matches(request: BulkIdsRequest, _auth=Depends(require_api_key)):
    item_ids = list(dict.fromkeys(request.item_ids or []))
    if not item_ids:
        raise HTTPException(status_code=400, detail="No item_ids provided.")

    results = []
    success_count = 0
    for item_id in item_ids:
        item = await db.get_item(item_id)
        if not item:
            results.append({"item_id": item_id, "status": "not_found"})
            continue

        updated = await db.set_confidence_verified(item_id)
        if updated is None:
            results.append({"item_id": item_id, "status": "failed"})
            continue

        success_count += 1
        results.append({"item_id": item_id, "status": "ok", "item": updated})

    return {
        "success": True,
        "action": "confirm",
        "requested": len(item_ids),
        "succeeded": success_count,
        "failed": len(item_ids) - success_count,
        "results": results,
    }


@router.put("/bulk/items/unconfirm")
async def bulk_unconfirm_matches(request: BulkIdsRequest, _auth=Depends(require_api_key)):
    item_ids = list(dict.fromkeys(request.item_ids or []))
    if not item_ids:
        raise HTTPException(status_code=400, detail="No item_ids provided.")

    results = []
    success_count = 0
    for item_id in item_ids:
        item = await db.get_item(item_id)
        if not item:
            results.append({"item_id": item_id, "status": "not_found"})
            continue

        updated = await db.revert_confidence(item_id)
        if updated is None:
            results.append({"item_id": item_id, "status": "failed"})
            continue

        success_count += 1
        results.append({"item_id": item_id, "status": "ok", "item": updated})

    return {
        "success": True,
        "action": "unconfirm",
        "requested": len(item_ids),
        "succeeded": success_count,
        "failed": len(item_ids) - success_count,
        "results": results,
    }


@router.put("/bulk/items/override")
async def bulk_override_codes(
    request: BulkOverrideRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    overrides = request.overrides or []
    if not overrides:
        raise HTTPException(status_code=400, detail="No overrides provided.")

    results = []
    success_count = 0

    for entry in overrides:
        item_id = entry.item_id
        raw = entry.product_code.strip()
        match = DLSITE_URL_PATTERN.search(raw)
        new_code = match.group(0).upper() if match else raw.upper()
        if not re.match(r'^(RJ|BJ|VJ)\d{6,8}$', new_code):
            results.append({"item_id": item_id, "status": "invalid_code", "input": raw})
            continue

        item = await db.get_item(item_id)
        if not item:
            results.append({"item_id": item_id, "status": "not_found", "product_code": new_code})
            continue

        updated = await db.override_product_code(item_id, new_code)
        if updated is None:
            results.append({"item_id": item_id, "status": "conflict", "product_code": new_code})
            continue

        success_count += 1
        results.append({"item_id": item_id, "status": "ok", "product_code": new_code, "item": updated})
        background_tasks.add_task(_refetch_metadata, item_id, new_code)

    return {
        "success": True,
        "action": "override",
        "requested": len(overrides),
        "succeeded": success_count,
        "failed": len(overrides) - success_count,
        "results": results,
    }


# Generic item update - MUST come after more specific routes
@router.put("/items/{item_id}")
async def update_item(item_id: int, update: ItemUpdate, _auth=Depends(require_api_key)):
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    data = update.model_dump(exclude_none=True)
    if "favorite" in data:
        data["favorite"] = 1 if data["favorite"] else 0
    await db.update_item_user_data(item_id, data)

    # If the user just pointed a manual item at a folder of already-extracted
    # loose audio, index it in place now (no copy) so it's immediately playable
    # in Atelier without a manual "extract" step — mirrors the on-scan behavior.
    if "archive_path" in data and await db.get_pipeline_enabled():
        refreshed = await db.get_item(item_id)
        try:
            files = json.loads(refreshed.get("files") or "[]")
        except (TypeError, ValueError):
            files = []
        from routers.scan import _auto_index_loose_item
        await _auto_index_loose_item(item_id, files)

    return await db.get_item(item_id)


@router.get("/seiyuu")
async def list_seiyuu(lang: str = Query("jp", pattern="^(jp|en)$")):
    return await db.get_unique_seiyuu(lang=lang)


@router.get("/tags")
async def list_tags(lang: str = Query("jp", pattern="^(jp|en)$")):
    return await db.get_unique_tags(lang=lang)


@router.get("/stats")
async def get_stats():
    return await db.get_stats()


@router.get("/unmatched")
async def list_unmatched():
    return await db.get_unmatched_files()


@router.get("/jobs")
async def list_jobs(limit: int = Query(20, ge=1, le=100)):
    return {"jobs": await db.get_recent_jobs(limit=limit)}


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, ignore_code: bool = Query(True), _auth=Depends(require_api_key)):
    """Delete an item and optionally ignore its code(s) so rescans do not re-import it."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    ignored_codes = []
    if ignore_code:
        codes = [item.get("product_code"), item.get("original_code")]
        ignored_codes = await db.add_ignored_codes(codes, reason="user_deleted")

    success = await db.delete_item_by_id(item_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete item")

    return {
        "success": True,
        "message": f"Deleted item {item_id}",
        "ignored_codes": ignored_codes,
    }


@router.get("/maintenance/integrity")
async def get_integrity(sample_limit: int = Query(50, ge=1, le=500)):
    return await db.get_integrity_report(sample_limit=sample_limit)


@router.post("/maintenance/cleanup-stale-covers")
async def cleanup_stale_covers(
    dry_run: bool = Query(True),
    sample_limit: int = Query(50, ge=1, le=500),
    _auth=Depends(require_api_key),
):
    return await db.cleanup_stale_covers(dry_run=dry_run, sample_limit=sample_limit)


@router.post("/maintenance/rebuild-indexes")
async def rebuild_indexes(
    sample_limit: int = Query(50, ge=1, le=500),
    _auth=Depends(require_api_key),
):
    return await db.rebuild_metadata_indexes(sample_limit=sample_limit)


@router.post("/maintenance/recompute-translation-status")
async def recompute_translation_status(_auth=Depends(require_api_key)):
    return await db.recompute_all_translation_statuses()


@router.get("/seiyuu/inventory")
async def get_seiyuu_inventory():
    """List every distinct EN seiyuu name with use counts and any canonical
    mapping currently registered. Drives the dedup UI."""
    return await db.get_seiyuu_inventory()


@router.get("/seiyuu/suggestions")
async def get_seiyuu_suggestions():
    """Pre-grouped likely-duplicate seiyuu names. Each group has 2+ members
    that normalize to the same canonical key (token-sorted, romanization
    folded). Drives the auto-suggest UI."""
    return {"groups": await db.suggest_seiyuu_groups()}


@router.post("/seiyuu/merge")
async def merge_seiyuu(request: SeiyuuMergeRequest, _auth=Depends(require_api_key)):
    try:
        return await db.merge_seiyuu_aliases(
            canonical_en=request.canonical_en,
            aliases=request.aliases,
            canonical_jp=request.canonical_jp,
            dry_run=request.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/seiyuu/backfill-romanizations")
async def backfill_seiyuu_romanizations(
    dry_run: bool = Query(True),
    _auth=Depends(require_api_key),
):
    """Fill seiyuu_en slots that are missing or are JP-name copies using
    romanizations already known elsewhere in the library (exact JP match).
    ``dry_run=true`` (default) previews the changes without writing."""
    return await db.backfill_seiyuu_romanizations(dry_run=dry_run)


@router.delete("/seiyuu/aliases/{alias}")
async def remove_seiyuu_alias(alias: str, _auth=Depends(require_api_key)):
    removed = await db.delete_seiyuu_alias(alias)
    if not removed:
        raise HTTPException(status_code=404, detail="Alias not found")
    return {"status": "removed", "alias": alias}


@router.post("/maintenance/backfill-sibling-translations")
async def backfill_sibling_translations(_auth=Depends(require_api_key)):
    """Replicate every existing translation run onto sibling tracks (e.g.
    FLAC + MP3 of the same audio) that don't yet have a copy. One-shot fix
    for libraries that translated each variant independently before the
    auto-replication landed."""
    return await db.backfill_missing_sibling_translations()


@router.post("/maintenance/backfill-active-transcripts")
async def backfill_active_transcripts(_auth=Depends(require_api_key)):
    """Repair tracks that have transcript runs but no
    ``active_transcript_run_id`` — typically sibling tracks (FLAC/MP3 of the
    same audio) where the run was replicated but never set active. Without
    this the Player tab loads the sibling and sees "No transcript loaded"
    even though the transcript exists."""
    return await db.backfill_missing_active_transcripts()




