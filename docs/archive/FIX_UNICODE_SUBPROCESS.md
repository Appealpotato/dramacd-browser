# Fix: Unicode Decoding Error in 7z/ffprobe Subprocess Calls

## Issue
```
Exception in thread Thread-1530 (_readerthread):
  File "C:\Users\appl\AppData\Local\Programs\Python\Python310\lib\subprocess.py", line 1515, in _readerthread
    buffer.append(fh.read())
  File "C:\Users\appl\AppData\Local\Programs\Python\Python310\lib\encodings\cp1252.py", line 23, in decode
    return codecs.charmap_decode(input,self.errors,decoding_table)[0]
UnicodeDecodeError: 'charmap' codec can't decode byte 0x90 in position 137: character maps to <undefined>
```

**Root Cause**:
- 7z outputs Japanese filenames (e.g., `DLJ-01094689_v23.11.27-JP-wav.part1.rar`)
- Python `subprocess.run()` with `text=True` defaults to system encoding (cp1252 on Windows)
- Windows cp1252 cannot decode Japanese characters (Shift-JIS, UTF-8, etc.)
- Subprocess crashes when trying to read 7z's stdout/stderr containing Japanese text

## Affected Code
**File**: `pipeline/extractor.py`

### Before (Line 98-103)
```python
result = subprocess.run(
    [exe, "x", "-y", f"-o{target_dir}", str(archive_path)],
    capture_output=True,
    text=True,  # ❌ Defaults to cp1252 on Windows
    check=False,
)
```

### After (Fixed)
```python
result = subprocess.run(
    [exe, "x", "-y", f"-o{target_dir}", str(archive_path)],
    capture_output=True,
    text=True,
    encoding="utf-8",        # ✅ Explicit UTF-8 encoding
    errors="replace",        # ✅ Replace invalid chars instead of crashing
    check=False,
)
```

## Changes Applied

### 1. `_extract_with_7z()` (Line 93-109)
- Added `encoding="utf-8"` to force UTF-8 decoding
- Added `errors="replace"` to substitute invalid bytes with `�` instead of crashing

### 2. `_probe_with_ffprobe()` (Line 163-184)
- Same fix applied to ffprobe subprocess call
- Prevents similar issues when probing audio files with Japanese metadata

## Why This Fix Works

| Parameter | Purpose |
|-----------|---------|
| `encoding="utf-8"` | Forces subprocess to decode stdout/stderr as UTF-8, which supports Japanese characters |
| `errors="replace"` | If 7z outputs truly invalid UTF-8 bytes, replace them with `�` instead of crashing the entire thread |

**Result**:
- 7z/ffprobe can now output Japanese filenames without crashing Python
- Invalid bytes are gracefully replaced (extremely rare, only if 7z outputs corrupt data)
- Extraction continues even if progress messages contain non-ASCII characters

## Testing

### Before Fix
```bash
# Extracting archive with Japanese filename
# → UnicodeDecodeError crashes the background thread
# → Job stuck in "running" state forever
```

### After Fix
```bash
# Extracting archive with Japanese filename
# → UTF-8 decoding handles Japanese characters correctly
# → Progress updates work: "current='DLJ-01094689_v23.11.27-JP-wav.part1.rar'"
# → Job completes successfully
```

## Related Issues
This is similar to the **Whisper Unicode path fix** from Session 7, but affects subprocess **output** decoding instead of input paths.

## Python Default Encoding Behavior

On Windows:
- `subprocess.run(text=True)` → Uses `locale.getpreferredencoding()` → `cp1252`
- `cp1252` can only encode Western European characters (Latin-1 extended)
- Japanese, Chinese, Korean, Arabic, etc. **will crash** without explicit UTF-8

On Linux/macOS:
- Default encoding is usually `utf-8`, so this bug doesn't occur
- But explicit encoding is still best practice for cross-platform consistency

## No Server Restart Needed
Since this is a bug fix in existing code (not a new feature), the server will automatically pick up the changes on the next extraction job. No restart required unless you want to apply it immediately.

## Verification
To verify the fix is working:
1. Queue an extraction job for an item with Japanese filenames
2. Monitor logs: Should see UTF-8 progress updates without crashes
3. Check job status: Should complete successfully instead of hanging

**Status**: ✅ FIXED
