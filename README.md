# dramacd-browser

Local browser for drama CD archives with DLsite metadata, on-device transcription, and AI translation.

## What it does

- **Library** — scans your archive folder, fetches DLsite metadata, lets you browse with seiyuu/tag/format filters, multi-select cards.
- **Workshop** — extracts archives (zip/rar/7z), transcribes audio with Faster Whisper, runs LLM translation (Gemini / OpenRouter / Chutes / any OpenAI-compatible endpoint, including Claude proxies that speak the Anthropic Messages format).
- **Player** — Spotify-style synchronized lyrics player. Picks up the active transcript + translation for a track and follows along with playback. Live codec/variant switching during playback.
- **Track grouping** — FLAC, MP3, WAV, AIFF, ... of the same recording, plus SFX vs no-SFX/voice-only mixes, all collapse into one row. Transcript runs replicate automatically to every sibling so you never re-run Whisper on the MP3 of something you already did in FLAC.

## Stack

- Python 3.10+ / FastAPI / aiosqlite
- Vue 3 (CDN, no build step)
- Faster Whisper (GPU/CPU)
- ffmpeg + ffprobe for audio I/O
- 7z for rar/7z archives

## Run

```bash
pip install -r requirements.txt
python main.py
# → open http://localhost:8080
```

Pipeline features (extraction, transcription, translation, player) are gated behind a runtime toggle in the UI sidebar. Set `DRAMACD_ENABLE_PIPELINE=1` to start with them on, or flip the switch in the sidebar.

## Configuration

### Required for full functionality
| Variable | Purpose |
|---|---|
| `DRAMACD_FFMPEG_PATH` | absolute path to `ffmpeg.exe` (Windows) — required for transcription |
| `DRAMACD_FFPROBE_PATH` | absolute path to `ffprobe.exe` — for richer audio metadata |
| `DRAMACD_7Z_PATH` | absolute path to `7z` — required for rar/7z extraction |

### Server / scan
| Variable | Default | Purpose |
|---|---|---|
| `DRAMACD_PORT` | `8080` | web server port |
| `DRAMACD_SCAN_PATH` | `G:\DramaCD\DL` | fallback default scan path on first run |
| `DRAMACD_API_KEY` | — | optional API key for mutating endpoints |
| `DRAMACD_ENABLE_PIPELINE` | `0` | start with pipeline features enabled |

### Whisper (env defaults — runtime UI overrides them)
| Variable | Default | Purpose |
|---|---|---|
| `DRAMACD_WHISPER_MODEL` | `small` | faster-whisper model id |
| `DRAMACD_WHISPER_DEVICE` | auto-detect | `cuda` / `cpu` |

The UI's **API → Whisper Settings** panel persists overrides to the DB at runtime: model, VAD filter, beam size, condition-on-previous-text, and **preferred variant** (SFX vs no-SFX) for queueing. Restarts not required.

### Translation providers (env defaults — runtime UI overrides them)
| Variable | Default |
|---|---|
| `DRAMACD_GEMINI_API_KEY` / `DRAMACD_GEMINI_MODEL` | — / `gemini-2.0-flash` |
| `DRAMACD_OPENROUTER_API_KEY` / `DRAMACD_OPENROUTER_MODEL` | — / `openrouter/auto` |
| `DRAMACD_CHUTES_API_KEY` / `DRAMACD_CHUTES_MODEL` | — / `deepseek-ai/DeepSeek-V3.1` |
| `DRAMACD_OPENAI_COMPAT_API_KEY` / `DRAMACD_OPENAI_COMPAT_MODEL` / `DRAMACD_OPENAI_COMPAT_BASE_URL` | — | OpenAI-compatible (Together, vLLM, LM Studio, custom proxies, etc.) |

The **OpenAI-compatible provider** has two request-format sub-modes:
- `openai` — standard `POST {base}/chat/completions`
- `anthropic` — `POST {base}/messages` with `x-api-key` header. Use this for Claude reverse proxies (matches SillyTavern's "Claude reverse proxy" contract). Prompt caching is wired up automatically — up to 4 cache breakpoints per request.

API keys can be sent via header (`X-API-Key`) or query (`?api_key=`).

## API surface

Every mutating endpoint accepts the API key. `?dry_run=true` previews destructive operations.

### Scan / fetch
- `POST /api/scan` — body `{paths: [...], recursive: bool}` (or single `path:`)
- `POST /api/scan/{pause,resume,stop}`, `GET /api/scan/status`
- `GET|PUT /api/scan/paths`
- `POST /api/fetch-metadata`, `POST /api/fetch-metadata/{pause,resume,stop}`, `GET /api/fetch-metadata/status`

### Items
- `GET /api/items` (with filter params), `GET /api/items/{id}`
- `PUT /api/items/{id}`, `DELETE /api/items/{id}`
- `PUT /api/items/{id}/override-code`
- `POST /api/items/{id}/refresh-metadata`
- `POST /api/items/{id}/translate-metadata` — translates title/description/seiyuu via active provider
- `PUT /api/items/{id}/{confirm,unconfirm,cover}`
- `PUT /api/bulk/items/{confirm,unconfirm,override}`

### Settings
- `GET|PUT /api/settings/ai` — translation providers, model ids, key status, OpenAI-compat URL/format
- `POST /api/settings/ai/test` — quick round-trip translation probe
- `GET /api/settings/ai/openai-compat-models` — fetch model list from configured base URL
- `GET|PUT /api/settings/whisper` — model, VAD, beam size, condition-on-previous, preferred-variant

### Maintenance
- `GET /api/maintenance/integrity`
- `POST /api/maintenance/cleanup-stale-covers?dry_run=`
- `POST /api/maintenance/rebuild-indexes`
- `POST /api/maintenance/recompute-translation-status`
- `GET /api/jobs?limit=&status=`

### Pipeline (gated by enable flag)
- `GET /api/pipeline/status`, `PUT /api/pipeline/enabled`
- `POST /api/pipeline/items/{id}/extract`, `GET /api/pipeline/items/{id}/extract/status`
- `GET /api/pipeline/items/{id}/tracks` — flat track list
- **`GET /api/pipeline/items/{id}/track-groups`** — tracks collapsed into recording-groups (codec + variant siblings)
- **`GET /api/pipeline/items/{id}/archive-contents`** — read-only list of files inside the source archive(s), used by the Workshop Archive panel's inline viewer
- **`GET /api/pipeline/items/{id}/archive-thumb?path=<inner-path>`** — extracts a single image from the archive and serves a Pillow-thumbnailed JPEG (cached on disk under `data/pipeline/archive-thumbs/`)
- `POST /api/pipeline/items/{id}/autopilot` — full per-CD pipeline (metadata → extract → titles → transcribe → translate)
- `POST /api/pipeline/items/{id}/auto-transcribe` — body `{track_ids?, language, force}` (model now read from runtime settings)
- `POST /api/pipeline/items/{id}/translate-track-names`
- `POST /api/pipeline/items/{id}/backfill-summaries`
- `POST /api/pipeline/tracks/{id}/auto-translate`
- Versioned runs: `POST|GET /api/pipeline/tracks/{id}/{transcripts,translations}`, single-run GET, `PUT .../active-{transcript,translation}/{run_id}`
- **Redundant-run cleanup**: `DELETE /api/pipeline/tracks/{id}/transcripts/redundant` (per-track), `DELETE /api/pipeline/items/{id}/transcripts/redundant` (per-item). Keeps active transcript + transcript anchoring the active translation; FK cascade drops dependent translations.
- `GET /api/pipeline/jobs?limit=&status=`
- `GET /api/pipeline/player/audio/{track_id}` — audio stream for player
- `GET /api/pipeline/items/{id}/package.zip` — bundled SRT/TXT/tracklist/audio/all-archive-files via query params
- Cleanup: `POST /api/pipeline/workspace/{orphans,purge-orphans}`, `POST /api/pipeline/maintenance/{fix-mojibake,fix-mojibake-paths}`

### Items
- `GET /api/items?search=&seiyuu=&tag=&favorite=&...` — library listing with FTS5 search across `product_code, title, title_en, circle, seiyuu, tags, custom_tags, description`. Workshop's autocomplete bar appends `*` per token for prefix matching.
- `GET|POST|DELETE /api/items/{id}` — single item CRUD
- **`PATCH /api/items/{id}/manual-track-count`** — `{count: <int>|null}`. Manual override for the auto-derived sibling-group count shown in the Workshop CD card; null reverts to auto.

## Track grouping

Same-recording siblings collapse into one row in both Workshop and Player. The grouping rule is intentionally structural (no hardcoded naming convention list):

1. **Stem-based union-find.** Tracks whose filename stems pass `_are_likely_siblings` end up in the same group. Two stems are siblings if any of:
   - One is a (full) prefix of the other (e.g. `Track01` ↔ `Track01_NoSE`).
   - LCP ends on a non-alphanumeric character (e.g. `track01_fullver` ↔ `track01_nose` — both diverge after `_`).
   - The character right after the LCP (in either stem) is non-alphanumeric — covers any punctuation, including JP `（）`, `【】`, etc.
   - Substantial-overlap floor (≥ 50% of the shorter stem) prevents a shared category prefix like `【特典】` from falsely merging unrelated bonus tracks.
2. **Variant labels (cosmetic).** A regex matches Latin (`no_se`, `seless`, `voiceonly`, `bgmless`, ...) AND Japanese (`SE無し`, `効果音なし`, `声のみ`, `BGM無し`, ...) tokens in either filename suffix or any ancestor folder. When matched, the row gets a `no-SFX` pill; otherwise it stays `SFX`. Labeling is independent of grouping — adding new tokens improves pill text, never affects whether tracks group.

When a transcription run completes, the new run + all its segments **replicate to every sibling track in the group** so MP3 of an audio you transcribed in FLAC inherits the same transcript. Replication is idempotent (won't double-copy) and metadata-tagged with `shared_from_run_id` for traceability.

## Whisper notes

- `large-v2` is the recommended model for Japanese drama CDs — `large-v3` hallucinates more on whispered/breathy speech.
- Enable **VAD filter** if you're seeing timestamp drift or false dialogue on silence-heavy tracks.
- Drop **beam size** to 1–3 if Whisper is producing repeated/looping output.
- Turn **condition-on-previous-text** OFF if errors compound over long tracks (drift).

## Translation continuity

After each transcription, a small structured "track summary" is generated by the active LLM (scene, listener state, partner state, relationship dynamics, slang notes). When translating track N, the previous 1–2 track summaries are injected into the translator prompt to keep voice/anatomy/slang interpretations consistent across the disc.

All translators (Gemini, OpenRouter, Chutes, OpenAI-compat) carry the same set of anatomy-accuracy rules: female listener perspective, masculine-coded slang (`ぼっき`, etc.) maps to female-arousal phrasing, no hallucinated acts/fluids, emotional intensity preserved.

## UI

Three tabs in the workspace, plus an API tab for settings:

- **Library** — card grid. Click a card → detail pane. Ctrl/Cmd+click → toggle multi-select (selected cards get a pink-bordered glow). Hover-revealed Send-to-Workshop button bottom-right of each card. Kebab menu in the bulk bar for batch actions; bottom of the menu is a 4-icon row for pipeline shortcuts (extract / transcribe / translate / full workflow).
- **Workshop** — sticks one item in focus.
  - **Top**: persistent autocomplete search bar (FTS5 prefix-match across title/code/seiyuu/tags) → compact CD card (cover + title + circle + voice actors + DLsite code + track count with manual override pencil).
  - **Archive panel**: icon-only header (list/grid view toggle, extract, force re-extract toggle, open folder, purge audio with inline confirm, export kebab with 3 hardcoded presets — AS Release / Subtitles Only / Full Package). Body is an inline archive viewer with collapsible folder grouping (list view) or breadcrumbed file-explorer style with image thumbnails (grid view).
  - **Transcription**: track grid with select-all/clear and language picker in the header row.
  - **Track Selection**: header icons for translate track names, fill missing summaries, regenerate all summaries.
  - **Transcript & Translation Management**: cards for each transcript/translation run. Click a card to select (sets active + binds as TL source). Lucide eye/trash icons; trash uses an inline ✓/✗ confirm. Run IDs hidden — cards show `language · N segments · model · relative time`.
  - **Active job state** lives in the Activity drawer (header bell icon), not in a separate panel.
- **Player** — Spotify-lyrics-style stage. Active line is large + accent-colored, surrounding lines fade with distance. Bottom: thin progress bar + prev/play/next. Top: track title with codec/variant pickers when alternates exist (click `MP3` to swap format mid-playback, or `no-SFX` to swap mix — playback position is preserved). Title respects the metadata-language toggle, with the other language as a secondary line.
- **Settings** — provider settings (Gemini, OpenRouter, Chutes, OpenAI-compat), Translation Settings (chunk tokens/lines/retries/backoff), Whisper runtime, Maintenance, Ops.

## Troubleshooting

| Symptom | Check |
|---|---|
| Scan returns `missing_paths` | path exists + readable |
| GPU not detected | `python -c "import torch; print(torch.cuda.is_available())"` |
| Faster Whisper import error | `pip install faster-whisper` completed |
| Unicode path errors during transcription | app auto-copies to ASCII-named temp file; check `data/pipeline/extracted/...` paths exist |
| Mojibake in track titles | sidebar maintenance → `Scan Mojibake` → `Fix titles` (or `Fix folder/file names on disk` for filesystem-level) |
| Workshop track list looks stale | switching tabs now auto-refreshes; if still stale, click the item again to re-load |
| Player shows previous CD's tracks | fixed — `switchToPlayerTab` now resets state when the workshop's focused CD differs |
| Frontend changes don't appear | hard refresh (Ctrl+Shift+R) — `style.css` and `app.js` are cache-busted via `?v=` query string |

## Reset

- Delete `data/library.db` for a full reset (loses metadata, transcripts, translations, settings).
- Delete `data/pipeline/extracted/` to reclaim extracted audio without losing DB state.
- Workspace orphans are listed in sidebar Maintenance → "Find orphan folders" → "Purge orphans".
