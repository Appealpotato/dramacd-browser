# Changelog

## 2026-05-12 — Games wing polish round (migrations 015–022)

### Migrations
- **015** `015_update_tokutens_shop_enum` — rebuilds `tokutens` with the new `shop` CHECK enum (`dlsite/booth/melon/animate/stellaworth/physical/other`). Data remap: `melonbooks→melon`; `getchu/toranoana/amazon_jp→other`. Pre-migration backup: `pre-tokutens-shop-enum-bak`.
- **016** `016_games_wishlist_and_is_manual` — full `games` rebuild to accept `'wishlist'` in `play_status` CHECK and add `is_manual INTEGER NOT NULL DEFAULT 0`. `items.is_manual` added via ALTER. Backfill marks games with NULL `library_path` and items with `product_code LIKE 'MAN-%' OR 'TKT-%'` as manual. Pre-migration backup: `pre-wishlist-and-is-manual-bak`.
- **017** `017_add_tokutens_vndb_id` — `tokutens.vndb_id` + index. Independent VNDB link (no FK to games — can reference a game you don't own locally).
- **018** `018_split_owned_available_platforms` — `games.platforms_available_json` (JSON array). One-time backfill copies `platforms_json` into the new column so existing VNDB data survives. Going forward: scanner writes `platforms_json` (owned), VNDB writes `platforms_available_json` (full release set).
- **019** `019_add_want_to_play_status` — full `games` rebuild widening `play_status` CHECK to include `want_to_play`. Uses explicit-column INSERT because 018's ALTER appended `platforms_available_json` at the end, so SELECT * wouldn't align with the freshly-CREATEd new table.
- **020** `020_add_vndb_searched` — `games.vndb_searched INTEGER NOT NULL DEFAULT 0` + index. "User reviewed this, no VNDB entry exists, fields hand-filled" flag. Counts as matched for the `matched=true/false` filter and the Unmatched stat.
- **021** `021_add_ignored_game_paths` — new table `ignored_game_paths(path_key PRIMARY KEY, path, reason, created_at)`. Mirrors `ignored_codes` for drama CDs. Used by `DELETE /api/games/{id}?ignore_path=true`.
- **022** `022_add_extra_library_paths` — `games.extra_library_paths_json` (JSON array). Holds additional library paths from merged-duplicate rows.

### New endpoints
- **`GET /api/games/stats`** — `{total (excludes wishlist), total_with_wishlist, matched, unmatched, favorited, manual, by_status}`.
- **`GET /api/games/distinct`** — `{developers, platforms, custom_tags}` sorted unique values for sidebar filter dropdowns.
- **`GET /api/games/duplicates`** — groups of rows sharing a `vndb_id` (>1 member each), best-candidate primary first.
- **`POST /api/games/merge-duplicates`** — folds each group's non-primaries into the primary. Paths → `extra_library_paths_json`; `platforms_json` / `platforms_available_json` / `custom_tags_json` / `languages_json` union; personal fields stay with primary.
- **`GET /api/games/ignored-paths`** + **`DELETE /api/games/ignored-paths/{path_key:path}`** — surface and un-ignore.
- **`DELETE /api/games/{id}?ignore_path=true|false`** — plain delete (default) vs delete + add to `ignored_game_paths`.
- **`GET /api/tokutens/stats`** — `{total, favorited, by_kind}`. Counts via `items.tokuten_id` so orphan tokutens don't inflate.
- **`GET/PUT /api/tokutens/scan/paths`** + **`POST /api/tokutens/scan`** — third member of the scan-paths trio. Stub rows keyed on `TKS-<sha1(abs_path)>[:12]`.
- **`POST /api/items/{id}/cover-from-url`** — server-side cover fetch. Bypasses browser CORS (VNDB CDN blocks fetch from origin). Used by the tokuten VNDB link flow.
- **`POST /api/items/{id}/refresh-metadata`** — source-aware now. Tokuten with linked vndb_id → VNDB. Real DLsite code (RJ/BJ/VJ regex via `_DLSITE_CODE_RE`) → DLsite. Else → clear error.

### Frontend
- **Per-subtab sidebar filters + stats**: Drama CDs / Games / Tokutens each have their own filter set and stat panel, persisted to separate localStorage keys. Search is unified across subtabs (typing once filters whichever subtab is active).
- **Wishlist + Want-to-play states** added to play_status. Wishlist excluded from main grid + Total stat by default. `formatPlayStatus(s)` handles display (DB keys with underscores → user-friendly labels like "On hold" / "Want to play").
- **Play-status quick-dropdown** on card pill + detail panel pill — click to change without entering edit mode.
- **is_manual filter** as a click-to-open dropdown per subtab (All / Manual only / Scanned only).
- **Platform icons** via `platformIconSvg(code)` + `PLATFORM_LABELS`. Card pill shows owned-only icons; detail panel shows owned + "Also available on" rows.
- **Tokuten ↔ game cross-link**: tokuten edit panel has a VNDB search bar (top of form), linked-game pill in read mode (clickable when local), and the game detail panel shows linked tokutens. Tokuten VNDB match also fetches cover + inherits release_date.
- **BBcode rendering** in description fields via `renderBBcode(text)` — supports `[url]/[b]/[i]/[spoiler]` + bare http(s) URLs. HTML-escapes first, then re-introduces safe tags.
- **VGMdb search link-out** ("Search VGMdb ↗") on both items + games detail action rows. No API integration — just a link.
- **Unmatched-games cleanup queue overlay**: walks unmatched rows one at a time, VNDB search auto-populated with title, "No VNDB entry — edit manually" path sets `vndb_searched=1` so the row drops out of the queue + Unmatched stat permanently.
- **Duplicate-vndb_id merge banner**: detects rows sharing `vndb_id` and offers one-click bulk merge.
- **"Delete entry" / "Yeet permanently" / "Remove from library"** split — items have plain + DLsite ignore variants; games have plain + path ignore variants. Bulk delete entries for games via the bulk-actions kebab.
- **Settings UI** for ignored game paths (lazy-loaded list with un-ignore per row).
- **Player volume control** (PC-only — iOS Safari ignores `audio.volume` writes; Android has hardware buttons). Slider + dynamic speaker-icon mute toggle. Persisted to localStorage.
- **Per-subtab view-mode persistence** (grid / cover / list). Drama CDs can be 'list' while games is 'grid'.
- **EN/JP metadata toggle** shared across all three subtabs (was drama-CD-only).
- **Sidebar Scan button** dispatches to the right scanner based on active subtab ("Scan Drama CDs" / "Scan Games" / "Scan Tokutens").
- **Cover Art popup eliminated**: kebab "Replace cover" fires the OS file dialog directly. Success/failure surface via toasts.
- **Transient toasts** for small jobs (track-name translate, backfill summaries, cleanup unused transcripts, cover upload, merge duplicates, etc.). `pushToast({kind, title, body, ttl})` is the canonical helper. `pipelineActiveSummary` was made transient too — no more persistent "Queued extraction for item X" green text.
- **Atelier instant refresh**: archive panel re-fetches immediately on item load + after extraction transitions (was needing a tab switch).
- **Multi-select + drag-rubberband** on library grid (Ctrl/Cmd+click toggles, shift+click range, drag draws box). Works in all three view modes; rubber-band rectangle only renders in grid/cover.
- **Scanner extensions**: PLATFORM_BY_EXTENSION now covers Switch compressed formats (.nsz/.xcz), 3DS variants (.3dsx, .cci), PSP (.pbp, .cso), Vita (.vpk). `detect_platforms` walks up to 3 levels deep so multi-disc layouts auto-detect their platform. Scanner reads `ignored_game_paths` and skips matching entries; `upsert_game_from_scan` recognizes paths in `extra_library_paths_json` so merged duplicates don't recreate.

### Pipeline
- **Linked-game description as translation context**: `pipeline/translation_job.py` augments `description_context` for tokutens. When the tokuten has a linked vndb_id, the job prepends the game's description (local game row first, then direct VNDB fetch as fallback) so the translator has world/character context up-front.
- **Manual archive path on items.files**: `pipeline/extractor.py:_resolve_archives_for_item` now recognizes absolute-path entries — if `items.files` contains a string that's an absolute path and exists, it's used directly without walking scan paths. Lets manual drama CDs / tokutens point at archives outside scan_paths.

## 2026-05-11 — Workshop refactor, archive viewer, sibling-dup bugfix

### Sibling duplicate-transcript bug fix
- **Autopilot stage 4** was queueing every track that lacked an active transcript — including sibling variants (FLAC + MP3 + SFX/no-SFX of the same audio). Whisper ran N times on the same recording per group, each producing a slightly different segment count, and `replicate_transcript_run_to_siblings` kept overwriting actives mid-flight. Translation snapshotted an early `transcript_run_id` and ended up pinned to a run whose segment count no longer matched what the Player loaded → visible JP/EN drift.
- Stage 4 now uses `get_pipeline_track_groups(preferred_variant="no-sfx")` and only queues the *preferred* track per sibling group. `whisper_job.py` per-track loop also skips a track if a sibling has already populated its active transcript via replication.
- `get_pipeline_track_groups` sub-buckets each stem cluster by duration (±2s). Some circles trim silent/SFX-only segments in no-SFX mixes (5-15s shorter); their segment timings don't transfer to the longer SFX mix, so trimmed and full versions are now separate groups.
- `replicate_transcript_run_to_siblings` and `replicate_translation_run_to_siblings` are duration-gated to match.

### New endpoints
- **`PATCH /api/items/{id}/manual-track-count`** — `{count: <int>|null}`. Override the auto-derived group count shown in the Workshop CD card; null reverts to auto. Backed by migration 012 (`items.manual_track_count INTEGER NULL`).
- **`DELETE /api/pipeline/tracks/{id}/transcripts/redundant`** — drop all transcript runs on a track except the active one and the one anchoring the active translation. FK cascade removes dependent translations + segments.
- **`DELETE /api/pipeline/items/{id}/transcripts/redundant`** — same cleanup fanned out across every track in the item.
- **`GET /api/pipeline/items/{id}/archive-contents`** — inline archive viewer. Calls `7z l -slt -sccUTF-8` and returns `{archives, files: [{path, size, archive}]}`. Multi-archive items return the union; multi-volume RARs collapse to first part.
- **`GET /api/pipeline/items/{id}/archive-thumb?path=<rel>`** — extracts a single image from the source archive via `7z e -so`, thumbnails with Pillow to 240×240 JPEG, caches at `data/pipeline/archive-thumbs/{item_id}/{sha1(path)}.jpg`.

### Workshop layout refactor
- **Top search bar** — persistent autocomplete over `/api/items?search=<fts>&limit=10`. Each token wraps with FTS5 `*` for prefix matching: `RJ0149` → `RJ01494586`, `mond` → `Mondou Ash`. Drama_cd only.
- **Compact CD card** — cover (128px) + title + Circle / Voice Actors / DLsite / track count, plain-text vertical layout. Pencil icon opens an inline track-count override (writes to `manual_track_count`). Removed: blue "Selected:" banner, "Mode/Archive support" header, Library Item ID number input. Uses `.workshop-cd*` class prefix (not `.workshop-item-card*`) to avoid the `[class*="item-card"]` theme wildcard.
- **Archive panel** (renamed from "Item Selection & Extraction") — icon-only header: list/grid view toggle, extract, force-toggle, open folder, purge audio (inline ✓/✗ confirm), export kebab with 3 hardcoded presets (AS Release / Subtitles Only / Full Package, mapped to existing `/package.zip` query params). Body is the inline **archive viewer**:
  - **List view**: collapsible folder groups (leaf-only folder names, file count badge, chevron rotation).
  - **Grid view**: file-explorer style with breadcrumbs and a CSS grid of tiles. Image files lazy-load thumbnails from the archive-thumb endpoint.
- **Transcript & Translation Management** — old View / Set Active / Use for TL / Delete button soup replaced with single-click card selection (sets active + binds as translation source). Lucide eye + trash icons; trash uses inline ✓/✗ confirm. Run IDs are hidden — cards show `language · N segments · model · relative time` (precise timestamp on hover).
- **Track Selection panel** — header gained translate-track-names + fill-missing-summaries + regenerate-all-summaries icons.
- **Translation Settings card in Settings tab** — max-tokens/lines/retries/backoff moved out of Workshop.
- **Removed**: Package & Workspace card (Archive's export kebab replaces it), Workshop Jobs card (Activity drawer covers it), "Status: completed (job #X)" status line, `pipelineActiveSummary` active-id green pill, Bulk Queue input, "Refresh tracks" button in Player, Glossary/Character-memory textareas in the translate panel.

### Library bulk kebab
- The 4 pipeline actions collapsed into an icon row at the bottom of the menu — extract / transcribe / translate / full workflow. New `bulkExtractSelected` and `bulkTranscribeSelected` handlers added.

### Player tab
- Title display respects the `currentLang` toggle — primary in active language, secondary line below in the other language. Active track title (`playerTrackTitle`) follows the same rule.

### UI polish
- **Inline-confirm pattern, no native `confirm()` dialogs** — trash icons flip into ✓/✗ on click. Used on transcript/translation deletes, "Clean up unused transcripts", purge audio.
- **Number-input spinners yeeted app-wide** — `input[type="number"] { appearance: textfield; }` + `::-webkit-*-spin-button { appearance: none }`.
- **Windows asyncio noise suppressed** — `_quiet_proactor_connection_reset` exception handler installed in lifespan startup filters `ConnectionResetError` from `_call_connection_lost` (mobile-disconnect post-close cleanup), everything else surfaces normally.

## 2026-05 — Track grouping, OpenAI-compat, UI overhaul

### Track grouping (FLAC/MP3 + SFX/no-SFX collapse)
- New endpoint `GET /api/pipeline/items/{id}/track-groups`. Backend cluster logic in `database.py`:
  - `_are_likely_siblings(a, b)` — structural (no regex token list): LCP spans shorter, OR LCP ends at non-alphanumeric, OR next char after LCP is non-alphanumeric. Substantial-overlap floor (≥ 50%) blocks shared category prefixes like `【特典】` from false-merging unrelated tracks.
  - Union-find pass over all tracks in an item produces transitively-closed sibling groups.
  - Duration is no longer a grouping gate — no-SFX mixes commonly trim 5–15s of SFX-only segments, so durations diverge between mixes.
  - Variant labeling is regex-driven (Latin + Japanese tokens) and purely cosmetic — drives whether the row shows an SFX/no-SFX pill, never affects which tracks cluster.
- `replicate_transcript_run_to_siblings(run_id)` — after every successful Whisper run, the run + all segments are copied to every sibling track in the group. Idempotent (won't double-copy), tagged in metadata with `shared_from_run_id`. Whisper job hook automates this.
- Frontend `_groupTracks()` and `_areLikelySiblings()` mirror the backend logic exactly. Workshop transcription list, transcribed list, and Player available-tracks list all render groups.

### OpenAI-compatible translation provider
- New runtime provider with configurable `base_url`, `api_key`, `model`. Settings in **API → OpenAI-compatible Settings**.
- Two `request_format` sub-modes:
  - `openai` — `POST {base}/chat/completions`, standard chat completions schema.
  - `anthropic` — `POST {base}/messages`, top-level `system`, `x-api-key` header, `anthropic-version: 2023-06-01`. Targets Claude reverse proxies (matches SillyTavern's contract).
- Model picker can fetch from `{base}/models` (`GET /api/settings/ai/openai-compat-models`) or accept manual entry.
- Anthropic-mode prompt caching wired up via `cache_control` on content blocks. Up to 4 cache breakpoints per request, marker-based splitting between stable context and per-chunk variable.
- New module `pipeline/anthropic_compat_translator.py`. `pipeline/openrouter_translator.py` extended with the `openai_compat` branch. `pipeline/track_summarizer.py` now speaks both formats.

### Whisper runtime settings
- New endpoint `GET|PUT /api/settings/whisper`.
- DB-persisted: `model`, `vad_filter`, `beam_size`, `condition_on_previous_text`, `preferred_variant` (sfx | no-sfx).
- UI panel in API tab. Per-call request body still wins over runtime settings if a model is explicitly supplied.
- `WhisperTranscriber` accepts `vad_filter`, `beam_size`, `condition_on_previous_text` parameters.
- Workshop's per-job model dropdown removed — runtime panel is the single source of truth.

### Translation status & related fixes
- `translation_status` now derived from actual run data (`recompute_translation_status_for_*` helpers) — no manual toggle.
- Migration 009: `pipeline_tracks.title_en` column for translated track titles.
- `POST /api/pipeline/items/{id}/translate-track-names` translates each track's title in batch (uses item's `title` + `description_en` for context).
- `POST /api/pipeline/items/{id}/backfill-summaries` for retroactive summary generation, including OpenAI-compat support.

### Mojibake fix utilities
- `_decode_zip_filename` falls back to `errors="replace"` for malformed CP932.
- New `try_recover_mojibake(s)` (`pipeline/extractor.py`) — CJK heuristic + high-byte count comparison via `s.encode('cp437').decode('cp932')` round-trip.
- Sidebar Maintenance:
  - `Scan Mojibake` / `Fix N titles` — DB-only title repair.
  - `Fix folder/file names on disk` — deepest-first rename of CP932-mojibaked filesystem entries (`POST /api/pipeline/maintenance/fix-mojibake-paths`).

### Player redesign
- Spotify-lyrics-style three-zone layout (`player-stage-*` CSS classes): top bar (back / title / resume-follow), centered scrollable lyric stage, bottom controls (slim progress + prev/play/next).
- Active line highlighted with accent color + soft glow; surrounding lines fade by tier (`active`, `near`, `far`, `distant`).
- Spacer blocks (40vh / 35vh on mobile) so the first/last lines can scroll to viewport center.
- All glyph buttons replaced with inline Lucide SVGs (back, crosshair-resume, skip-back, play/pause, skip-forward).
- Picker pills under the title when the loaded track's group has multiple codecs OR variants. Clicking a picker pill calls `switchPlayerVariant(newId)` which preserves `currentTime` and `paused` state across the swap.

### UI overhaul (sidebar / workshop / library)
- Custom-styled checkboxes & radios — `appearance: none` + pure CSS pseudo-element rendering. White centered square (checkbox) or dot (radio) on accent-pink fill when checked. No native OS controls anywhere.
- Library card multi-select: corner checkbox removed. Card-level selected state (pink border + soft glow). Ctrl/Cmd+click toggles. Plain click on a selected card deselects (exits selection mode).
- Hover-revealed **Send to Workshop** button bottom-right of each library card (Lucide wrench icon, accent-pink hover state, `backdrop-filter: blur` for legibility on busy cover art).
- Bulk-bar in library tab: counter on left, icon-only Select All / Clear / kebab on right. Kebab dropdown ("More actions") hosts `Send to Workshop` (single-select only) and `Translate metadata`. Click-toggle + outside-click close.
- Workshop transcription card: the per-job model dropdown deleted. Track Selection (no-checkbox version): row is the click target, hover-tinted bg, pink-tinted bg when selected, no border. Transcribed-row click auto-loads transcript/translation runs (replaces the "Load Runs for Track #N" button).
- Sidebar collapsible sections use `interpolate-size: allow-keywords` for `height: auto ↔ 0` animation — no more max-height dead-pixel sweep lag.
- Sidebar `.control-column` pinned to `calc(100vh - 30px)` with thin custom scrollbar.
- Global thin-scrollbar styling (Firefox `scrollbar-width` + Chromium `::-webkit-scrollbar*`) applied to all scroll containers.
- Lucide-style icons throughout (icon-only buttons via `.icon-btn` class, 14px SVGs, currentColor stroke for hover-color flow).
- All cache-busting versions on `style.css?v=` and `app.js?v=` so refreshes always pick up the latest.

### Misc bug fixes
- `pipelineSelectedItemId`, `playerItemId`, `playerTrackId`, `pipelineTrackId`, collapsed/expanded card sections persisted across reloads via localStorage.
- `switchToWorkshopTab` now soft-refreshes track list + reloads runs for the focused track. Fixes case where transcripts created elsewhere weren't visible.
- `switchToPlayerTab` resets player state cleanly when the focused workshop CD differs from the loaded one. Fixes "previous CD's tracks linger" bug.
- "Translate Titles" button in library tab moved into the kebab menu's "Translate metadata" item.
- Removed "Load Runs for Track #N" button — track row click auto-fires `loadPipelineRuns()` via watcher.
- Library card click handler now dispatches: plain click → openDetail (current behavior); ctrl/cmd-click → toggle selection; click-while-selection-active → toggle (deselect to exit selection mode).
- Read-only player endpoints (`list_item_tracks`, `list_track_transcript_runs`, segment fetches, `serve_track_audio`) ungated from the pipeline-enabled flag, so the Player tab works even when Workshop is off.
- "Cards overlap when expanded" — sidebar `.control-column` sets `flex-shrink: 0` on direct children to stop flex squishing each section to fit, which was making content visually bleed across section boundaries.

### DLsite upload-template script
- New standalone `scripts/dlsite_template.py` outputs a BBCode upload post for a given product code:
  - Cover, English title (preferred from `title_en`, falls back to JP), Original title, Format (MP3+FLAC), Voice Actor(s) (JA only), Release date, Developer/Publisher, DLsite URL, Tags (comma-separated), Download placeholders, EN/JA descriptions.
- Pulls EN title/description/seiyuu from local `data/library.db` first, falls back to scraper.
- Writes both thread title and BBCode body to `{CODE}.txt`.

---

## Session 8 (2026-02-15) — Track Context Memory System

### New
- **Track Context Memory** — auto-generates structured per-track summaries after transcription (scene, listener state, partner state, relationship dynamics, slang notes). Stored in `pipeline_tracks.track_summary_json`.
- When translating track N, summaries from N-1 and N-2 are injected into the translator prompt for continuity.
- Migration 008: `track_summary_json TEXT` column.
- New `pipeline/track_summarizer.py`.

### Translator rules
- All translators carry **SEXUAL CONTENT & ANATOMY RULES**: female listener perspective enforcement, masculine slang (`ぼっき`) → female-arousal phrasing, no hallucinated acts/fluids, emotional intensity preserved.

---

## Session 7 (2026-02-14) — Faster Whisper migration

### Breaking
- Migrated `openai-whisper` → `faster-whisper`. Run `pip install -r requirements.txt`.

### Performance
- 4-5× faster transcription. Real per-segment progress (no fake percentages). int8 on CPU, float16 on CUDA. Built-in VAD.

### Technical
- `pipeline/transcriber.py` rewritten around `faster_whisper.WhisperModel`.
- `main.py` sets `KMP_DUPLICATE_LIB_OK=TRUE` for Windows OpenMP coexistence.

### Removed
- Console-output UI panel (no longer needed once real progress callbacks landed).

---

## Session 6 & earlier — Auto-transcription foundation

- Local Whisper with GPU acceleration.
- Unicode path handling via temp-file copying.
- Track selection UI.
- Real-time progress polling.
- Transcript_run creation + activation.

---

## Upgrade notes

### Existing install → 2026-05
- `pip install -r requirements.txt` (no new deps, but pinning may have moved).
- Restart server. Migrations are automatic.
- Hard-refresh the browser once to bust cached CSS/JS.
- If you want the OpenAI-compatible provider, fill in **API → OpenAI-compatible Settings** (URL, key, model, request format) — no env var required.
- Whisper runtime panel ships with sane defaults; no action needed unless you want to flip preferences (e.g., switch to `large-v2` or enable VAD).

### From Session 8 → 2026-05
- The big behavioral change: per-job Whisper model dropdown removed from Workshop. Use **API → Whisper Settings** instead.
- Existing transcripts are not retroactively replicated to siblings. Use `POST /api/pipeline/maintenance/fix-mojibake` and re-run transcription on a single sibling per group; new run replicates to others.
