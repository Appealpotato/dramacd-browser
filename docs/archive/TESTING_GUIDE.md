# Testing Guide - Track Context Memory System

## Prerequisites
- Server running with `DRAMACD_ENABLE_PIPELINE=1`
- Translation provider API key configured (Gemini/OpenRouter/Chutes)
- Multi-track drama CD extracted (at least 2-3 tracks)

## Test Scenario: 3-Track Drama CD

### Step 1: Transcribe All Tracks
1. Go to Pipeline tab
2. Enter item ID for a drama CD
3. Click "Load Tracks"
4. Select all tracks (or use "Select All")
5. Click "Transcribe Selected"
6. Wait for transcription to complete (~2-5 min per track)

**Expected Results**:
- ✅ Each track transcribes successfully
- ✅ Job events show "Generated context summary for Track X"
- ✅ No errors in server logs
- ✅ All tracks have `active_transcript_run_id` set

**Check Database** (optional):
```sql
SELECT id, track_index, title,
       CASE
         WHEN track_summary_json IS NULL THEN 'NO SUMMARY'
         ELSE substr(track_summary_json, 1, 100) || '...'
       END as summary_preview
FROM pipeline_tracks
WHERE item_id = <YOUR_ITEM_ID>
ORDER BY track_index;
```

### Step 2: Translate Track 1 (No Previous Context)
1. Select Track 1
2. Click "Auto-Translate (AI)"
3. Configure settings:
   - Target Language: English
   - Provider: Your configured provider
   - Model: Your preferred model
4. Click "Translate"
5. Wait for completion

**Expected Results**:
- ✅ Translation completes successfully
- ✅ Server logs show: `Fetched 0 previous track summaries for context`
- ✅ Translation prompt includes: `Previous track summaries: (none)`

### Step 3: Translate Track 2 (With Track 1 Context)
1. Select Track 2
2. Click "Auto-Translate (AI)"
3. Same settings as Track 1
4. Click "Translate"
5. Wait for completion

**Expected Results**:
- ✅ Translation completes successfully
- ✅ Server logs show: `Fetched 1 previous track summaries for context`
- ✅ Translation prompt includes: `Previous track context summaries:\nTrack 1: {...}`
- ✅ Translation should maintain slang interpretation from Track 1

**Check Translation Quality**:
- Compare how similar phrases are translated in Track 1 vs Track 2
- Verify emotional tone consistency
- Check if anatomy/arousal language is consistent

### Step 4: Translate Track 3 (With Track 2 + Track 1 Context)
1. Select Track 3
2. Click "Auto-Translate (AI)"
3. Same settings
4. Click "Translate"
5. Wait for completion

**Expected Results**:
- ✅ Translation completes successfully
- ✅ Server logs show: `Fetched 2 previous track summaries for context`
- ✅ Translation prompt includes both Track 2 and Track 1 summaries
- ✅ Translation maintains continuity from previous tracks

## Anatomy Translation Test Cases

### Test Case 1: ぼっき Metaphor Handling
**Scenario**: Track contains ぼっき used metaphorically for female arousal

**Expected Translation** (one of):
- "you're so turned on"
- "you're this aroused"
- "you're swollen with arousal"
- "you're this sensitive"

**NOT Expected**:
- ❌ "you're hard"
- ❌ "erection"
- ❌ Any male-genital interpretation

### Test Case 2: Emotional Continuity
**Scenario**: Track 1 establishes emotional state → Track 2 should reference it

**Check**:
- Track 1 summary should capture emotional state
- Track 2 translation should maintain awareness of Track 1 emotions
- Relationship dynamics should progress naturally

### Test Case 3: Slang Consistency
**Scenario**: Same slang term appears in multiple tracks

**Check**:
- Track summaries note slang interpretation
- Subsequent tracks translate the term consistently
- No contradictory interpretations across tracks

## Troubleshooting

### Summary Generation Failed
**Symptom**: Job event shows "Failed to generate track summary"

**Causes**:
- No API key configured for translation provider
- API rate limit exceeded
- Invalid JSON response from LLM

**Impact**: Translation continues normally, just without previous context

**Fix**:
- Check API key configuration: `GET /api/settings/ai`
- Verify API quota/credits
- Check server logs for error details

### No Previous Summaries Injected
**Symptom**: Server logs show "Fetched 0 previous track summaries" for Track 2+

**Causes**:
- Track 1 summary generation failed
- Incorrect track_index values in database
- Migration 008 didn't run

**Check**:
```sql
-- Check if column exists
PRAGMA table_info(pipeline_tracks);

-- Check track indexes
SELECT id, track_index,
       CASE WHEN track_summary_json IS NULL THEN 'NULL' ELSE 'EXISTS' END
FROM pipeline_tracks
WHERE item_id = <YOUR_ITEM_ID>
ORDER BY track_index;
```

### Translation Uses Wrong Context
**Symptom**: Track 3 references Track 4 summary

**Cause**: track_index values are incorrect

**Fix**:
- Verify track_index values in database
- Re-extract tracks if indexes are wrong
- Track indexes should be sequential (0, 1, 2, ... or 1, 2, 3, ...)

## Success Criteria

✅ **All tracks transcribe successfully**
✅ **Summaries generated for all tracks** (check database or job events)
✅ **Track 2+ translations inject previous summaries** (check server logs)
✅ **Anatomy language translated correctly** (female perspective, no male hallucinations)
✅ **Emotional continuity maintained** across tracks
✅ **Slang interpretation consistent** across tracks
✅ **No translation job failures** due to missing summaries

## Performance Benchmarks

### Transcription Speed (RTX 3060 Ti, Faster Whisper "small")
- Single track (5 min audio): ~30-60 seconds
- Full CD (10 tracks, 50 min total): ~5-10 minutes

### Summary Generation Speed
- Per track: ~3-10 seconds (depends on provider/model)
- Gemini Flash: ~3-5 seconds
- OpenRouter: ~5-10 seconds
- Chutes: ~5-10 seconds

### Translation Speed (depends heavily on provider)
- Per chunk (20 segments): ~5-15 seconds
- Full track (150 segments): ~1-3 minutes
- Provider speed: Gemini Flash > Chutes > OpenRouter

## Reporting Issues

If you encounter issues, provide:
1. Server logs (full output from transcription → translation)
2. Database query results (track summaries, track indexes)
3. Translation quality examples (input Japanese + output English)
4. Provider/model configuration
5. Item ID and track IDs being tested
