# Track Context Memory System

## Overview

Implemented a per-track context memory system that maintains continuity across drama CD tracks during translation. After each track is transcribed, the system generates a structured summary that captures emotional state, relationship dynamics, and slang interpretation. These summaries are then injected into subsequent track translations to maintain consistency.

## How It Works

### 1. **Summary Generation (After Transcription)**
When a track completes transcription:
- Calls the LLM with the full transcript + drama description
- Generates a structured JSON summary (200-400 tokens):
```json
{
  "track_number": 1,
  "scene_summary": "1-3 sentence summary",
  "listener_state": "female; emotional + physical state",
  "boyfriend_state": "emotional + behavioral state",
  "relationship_state": "brief dynamic description",
  "important_terms": {
    "ぼっき": "used metaphorically for female arousal"
  }
}
```
- Stores in `pipeline_tracks.track_summary_json`
- Uses the same provider/model/API key as translation

### 2. **Summary Injection (During Translation)**
When translating track N:
- Fetches summaries for tracks N-1 and N-2 (whichever exist)
- Injects them into **every chunk** translation prompt:
```
Previous track context summaries:
Track 2: {...}
Track 1: {...}
```
- Provides multi-track continuity without bloating the prompt

### 3. **Context Limit: 2 Tracks**
- Only fetches the **last 2 previous tracks** (N-1, N-2)
- Prevents summary drift and keeps prompts compact
- Balances continuity vs. token cost

## Implementation Details

### Database Changes
- **Migration 008**: Added `track_summary_json TEXT` column to `pipeline_tracks`
- **New Functions**:
  - `set_track_summary(track_id, summary_json)` - Store summary
  - `get_previous_track_summaries(item_id, current_track_index, limit=2)` - Fetch previous summaries

### New Module: `pipeline/track_summarizer.py`
- `TrackSummarizer` class supports Gemini, OpenRouter, and Chutes
- Generates structured summaries with validation
- Graceful failure: If summary generation fails, stores `None` and continues

### Modified Files

#### `pipeline/whisper_job.py`
- After transcription completes and transcript_run is created
- Generates track summary using TrackSummarizer
- Stores summary in database via `set_track_summary()`
- Logs success/failure but doesn't block transcription job

#### `pipeline/translation_job.py`
- Fetches `previous_summaries` for current track
- Passes `previous_summaries=` to `translator.translate_chunk()`

#### All 3 Translators (`gemini_translator.py`, `openrouter_translator.py`, `chutes_translator.py`)
- Added `previous_summaries: list[dict] = None` parameter to `translate_chunk()`
- Injects summaries into prompt between "CONTEXT:" and other context fields
- Format:
```
Previous track context summaries:
Track 2: {...}
Track 1: {...}

(or)

Previous track summaries: (none)
```

## Workflow

### First Track (Track 1)
1. Transcribe → Generate summary → Store in DB
2. Translate Track 1 → No previous summaries → `(none)`

### Second Track (Track 2)
1. Transcribe → Generate summary → Store in DB
2. Translate Track 2 → Inject Track 1 summary

### Third Track (Track 3)
1. Transcribe → Generate summary → Store in DB
2. Translate Track 3 → Inject Track 2 + Track 1 summaries

### Fourth Track (Track 4)
1. Transcribe → Generate summary → Store in DB
2. Translate Track 4 → Inject Track 3 + Track 2 summaries (Track 1 dropped)

## Benefits

✅ **Emotional Continuity** - Tracks state transitions between scenes
✅ **Slang Consistency** - Prevents re-interpreting ぼっき differently in Track 3 vs Track 1
✅ **Relationship Arc** - Maintains awareness of evolving dynamics
✅ **Anatomy Awareness** - Reinforces female listener perspective across tracks
✅ **No Bloat** - Only 2 summaries = ~600-800 tokens max
✅ **Graceful Degradation** - Translation continues even if summary generation fails

## Configuration

No new environment variables needed. Uses existing translation provider settings:
- `DRAMACD_GEMINI_API_KEY` / `DRAMACD_GEMINI_MODEL`
- `DRAMACD_OPENROUTER_API_KEY` / `DRAMACD_OPENROUTER_MODEL`
- `DRAMACD_CHUTES_API_KEY` / `DRAMACD_CHUTES_MODEL`
- `DRAMACD_TRANSLATION_PROVIDER` (gemini/openrouter/chutes)

## Testing

1. **Restart server** - Migration 008 will add `track_summary_json` column
2. **Transcribe a multi-track drama CD** (e.g., 3-5 tracks)
3. **Check job events** - Should see "Generated context summary for Track X" events
4. **Translate Track 2+** - Should see previous summaries in translation prompts (check logs)
5. **Verify consistency** - Check if slang/emotional tone stays consistent across tracks

## Error Handling

- If summary generation fails → Logs warning, stores `None`, continues transcription
- If no previous summaries exist → Injects `"(none)"`
- If summary JSON is invalid → Catches exception, stores `None`
- Translation never fails due to missing summaries
