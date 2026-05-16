# Metadata Refresh Feature + Improved Override Logging

## Problem
When using "Override Product Code" to manually correct an entry, the background metadata fetch was failing silently due to:
1. Geoblocking (though DLsite API should bypass this)
2. No user feedback when the fetch failed
3. No logging to diagnose issues

## Solution

### 1. New Manual Refresh Endpoint (`POST /api/items/{item_id}/refresh-metadata`)
- **Immediate feedback** (not background task)
- Returns success/failure with clear error messages
- Accessible via new "Refresh Metadata" button in item detail panel

### 2. Improved Background Refetch Logging
Enhanced `_refetch_metadata()` in `routers/api.py`:
- Now logs **success** with title confirmation
- Logs **failures** with reason (not_found, timeout, network_error, etc.)
- Catches exceptions and logs them for debugging

### 3. Frontend Button
Added "Refresh Metadata" button in item detail panel:
- Blue primary button (stands out from secondary buttons)
- Shows "Refreshing..." state while loading
- Displays success/error messages below the button
- Tooltip: "Force-refresh metadata from DLsite API (bypasses geoblocking)"

---

## How DLsite Metadata Fetching Works

### API-First Approach (Already Implemented)
The scraper **already prioritizes the DLsite API**, which bypasses geoblocking:

1. **Try DLsite API** (`/api/=/product.json?workno=RJ...`)
   - Works from all regions (no geoblocking)
   - Returns clean JSON data
   - Tries multiple site sections (maniax, home, girls, etc.)

2. **Try alternate prefixes** (RJ → BJ/VJ, etc.)
   - If RJ123456 not found, tries BJ123456, VJ123456

3. **Scrape HTML page** (only if API succeeds)
   - Enriches metadata with seiyuu, series, richer description
   - This step CAN be geoblocked, but it's **optional enrichment**
   - Base metadata (title, circle, tags, cover) comes from API

### Why "Override Code" Was Failing
The background task (`_refetch_metadata`) was silently swallowing errors. If the API fetch failed for ANY reason (not just geoblocking), you'd see no feedback.

---

## Usage Guide

### Scenario 1: Override Product Code (Automated)
**Old Behavior**:
1. Click "Show Correction" → Enter new code → Save
2. Background fetch happens silently
3. If it fails → no feedback, item stays blank

**New Behavior**:
1. Click "Show Correction" → Enter new code → Save
2. Background fetch happens
3. **Check server logs** for success/failure:
   ```
   [INFO] Auto-fetched metadata for RJ123456 (item 42): Sample Title
   # OR
   [WARNING] Failed to auto-fetch metadata for RJ123456 (item 42): not_found
   ```

### Scenario 2: Manual Refresh (New Feature)
**Best for**:
- Override failed silently and item is still blank
- You want immediate feedback
- Need to force-refresh stale metadata

**Steps**:
1. Open item detail panel (click card)
2. Click **"Refresh Metadata"** button
3. Wait 2-5 seconds
4. **See immediate result**:
   - ✅ Success: `Metadata refreshed: [Title]` (green)
   - ❌ Failure: `Could not fetch metadata from DLsite. Reason: not_found` (orange)

---

## API Response Format

DLsite API returns:
```json
[{
  "work_name": "ドスケベ悪魔とえっちな契約",
  "maker_name": "HoneyParfum (ハニパル)",
  "work_image": "//img.dlsite.jp/modpub/images2/work/doujin/RJ01287000/RJ01286723_img_main.jpg",
  "regist_date": "2024-11-15 00:00:00",
  "age_category": 3,
  "genres": [
    {"name": "連続絶頂"},
    {"name": "サキュバス/淫魔"}
  ]
}]
```

Our parser extracts:
- `title` (work_name)
- `circle` (maker_name)
- `cover_url` (work_image with `https:` prefix)
- `release_date` (regist_date)
- `age_rating` (mapped from age_category: 1=ALL, 2=R15, 3=R18)
- `tags` (from genres array)

---

## Error Reasons

| Reason | Meaning |
|--------|---------|
| `not_found` | Product code doesn't exist on DLsite (typo or delisted) |
| `timeout` | DLsite API took too long to respond (network issue) |
| `network_error` | Connection failed (check internet) |
| `server_error` | DLsite returned 5xx error (their servers down) |
| `rate_limited` | Too many requests (429), automatically retried |
| `parse_error` | API returned invalid JSON (rare) |
| `access_denied` | 401/403 (shouldn't happen with API, but possible with HTML) |

---

## Testing the Fix

### Test 1: Override Code with Immediate Refresh
1. Pick an item with blank metadata
2. Click "Show Correction"
3. Enter a valid product code (e.g., `RJ01286723`)
4. Click "Save Override"
5. **Check server logs** for `[INFO] Auto-fetched metadata for...`
6. If background fetch failed, click **"Refresh Metadata"** button
7. Should see green success message with title

### Test 2: Invalid Product Code
1. Override with fake code (e.g., `RJ99999999`)
2. Click "Refresh Metadata"
3. Should see error: `Reason: not_found`

### Test 3: Verify API Bypasses Geoblocking
Run from terminal:
```bash
curl "https://www.dlsite.com/maniax/api/=/product.json?workno=RJ01286723"
```
Should return JSON even if you're geoblocked from HTML pages.

---

## Files Modified

| File | Changes |
|------|---------|
| `routers/api.py` | Added `/refresh-metadata` endpoint, improved `_refetch_metadata()` logging |
| `static/js/app.js` | Added `refreshMetadata()` function + state variables |
| `static/index.html` | Added "Refresh Metadata" button in detail panel |

---

## Future Improvements

1. **Bulk Refresh**: Add button to refresh metadata for all items with missing data
2. **Retry Queue**: If API fails, queue for automatic retry later
3. **Cache API Responses**: Store API JSON to avoid re-fetching on every override
4. **Offline Mode**: Use cached metadata when DLsite is unreachable

---

## Status
✅ **READY TO TEST** - Refresh your browser and try the new "Refresh Metadata" button!
