# Session 8 Summary - Track Context Memory System

## What Was Built

Implemented a complete **per-track context memory system** that maintains continuity across drama CD tracks during translation.

### The Problem
When translating multi-track drama CDs, each track was translated independently with no awareness of:
- Previous track emotional states
- Established slang interpretations (especially anatomy/arousal language)
- Character relationship dynamics
- Story progression

This led to inconsistent translations where the same term might be interpreted differently across tracks.

### The Solution

**Automatic Summary Generation**:
- After each track completes transcription, call the LLM to generate a structured JSON summary (200-400 tokens)
- Summary captures: scene events, listener state (female), boyfriend state, relationship dynamics, important slang notes
- Store in `pipeline_tracks.track_summary_json`

**Context Injection During Translation**:
- When translating track N, fetch summaries for tracks N-1 and N-2
- Inject them into **every chunk** of the translation prompt
- Provides multi-track continuity without bloating prompts

**Limit: 2 Tracks**:
- Only inject the last 2 previous tracks (N-1, N-2)
- Prevents summary drift and keeps prompts compact
- Ideal balance of continuity vs. token cost (~600-800 tokens)

## Files Created

1. **`pipeline/track_summarizer.py`** (252 lines)
   - `TrackSummarizer` class with Gemini/OpenRouter/Chutes support
   - Generates structured summaries with validation
   - Graceful failure handling

2. **`TRACK_CONTEXT_MEMORY.md`** (Complete system documentation)
3. **`CHANGELOG.md`** (Version history and upgrade guide)
4. **`TESTING_GUIDE.md`** (Testing scenarios and success criteria)

## Files Modified

1. **`database.py`**
   - Migration 008: Added `track_summary_json TEXT` column
   - New function: `set_track_summary(track_id, summary_json)`
   - New function: `get_previous_track_summaries(item_id, current_track_index, limit=2)`

2. **`pipeline/whisper_job.py`**
   - After transcription completes, generates summary via `TrackSummarizer`
   - Stores summary in database
   - Logs success/failure but never blocks transcription

3. **`pipeline/translation_job.py`**
   - Fetches previous 2 track summaries before translation
   - Passes `previous_summaries=` to translator

4. **All 3 Translators** (`gemini_translator.py`, `openrouter_translator.py`, `chutes_translator.py`)
   - Added `previous_summaries: list[dict] = None` parameter to `translate_chunk()`
   - Injects summaries into prompt between drama description and glossary

5. **`README.md`**
   - Added Faster Whisper to tech stack
   - Documented auto-transcription feature
   - Documented track context memory system
   - Added sexual content translation rules
   - Updated troubleshooting section

6. **`MEMORY.md`**
   - Added Session 8 documentation
   - Updated Session 7 status (Faster Whisper migration)
   - Cleaned up deprecated console output notes

## Bonus: Sexual Content Translation Rules

Added strict anatomical accuracy rules to all translators:
- **Female listener perspective**: All arousal language translated for female anatomy
- **Metaphor handling**: ぼっき → "you're aroused" (NOT "you're hard")
- **No hallucinations**: Avoid adding acts/fluids not in Japanese
- **Emotional preservation**: Maintain intensity without embellishment

## How It Works (Example Workflow)

```
Track 1:
  Transcribe → Generate summary → Store in DB
  Translate → No previous summaries (inject "none")

Track 2:
  Transcribe → Generate summary → Store in DB
  Translate → Inject Track 1 summary

Track 3:
  Transcribe → Generate summary → Store in DB
  Translate → Inject Track 2 + Track 1 summaries

Track 4:
  Transcribe → Generate summary → Store in DB
  Translate → Inject Track 3 + Track 2 summaries (Track 1 dropped)
```

## Benefits

✅ **Emotional Continuity** - Tracks state transitions between scenes
✅ **Slang Consistency** - Prevents re-interpreting terms differently
✅ **Relationship Arc** - Maintains awareness of evolving dynamics
✅ **Anatomy Awareness** - Reinforces female listener perspective
✅ **No Bloat** - Only 2 summaries = ~600-800 tokens max
✅ **Graceful Degradation** - Translation continues even if summary fails

## Testing

See `TESTING_GUIDE.md` for complete testing scenarios.

**Quick Test**:
1. Transcribe a 3-track drama CD
2. Check job events for "Generated context summary" messages
3. Translate Track 2 → Should see Track 1 summary in logs
4. Translate Track 3 → Should see Track 2 + Track 1 summaries in logs
5. Verify anatomy language consistency across tracks

## Configuration

No new environment variables needed. Uses existing:
- `DRAMACD_GEMINI_API_KEY` / `DRAMACD_GEMINI_MODEL`
- `DRAMACD_OPENROUTER_API_KEY` / `DRAMACD_OPENROUTER_MODEL`
- `DRAMACD_CHUTES_API_KEY` / `DRAMACD_CHUTES_MODEL`
- `DRAMACD_TRANSLATION_PROVIDER`

## Next Steps (Future Sessions)

1. **UI Redesign** - Card-based layout for Pipeline tab
2. **Track/Item Name Translation** - Auto-translate track titles
3. **Bulk Translation** - Translate multiple tracks at once
4. **Enhanced Progress** - Better visualization of translation progress

---

**Status**: ✅ **READY FOR TESTING**

Restart the server and test with a multi-track drama CD!
