# UI Redesign Guide

**Date**: 2026-02-15
**Status**: ✅ Complete

## Overview

Complete UI overhaul implementing a modern **card-based collapsible layout** across the entire application. This redesign dramatically improves usability by organizing features into logical, collapsible sections that reduce visual clutter and improve navigation.

---

## What Changed

### 1. Pipeline Tab (Complete Restructure)

**Before**: Single flat panel with all controls visible at once
**After**: 5 organized collapsible cards

#### Card Structure

1. **📦 Item Selection & Extraction**
   - Library item ID input + Load Tracks
   - Extract archive controls (force re-extract checkbox)
   - Bulk queue (comma-separated IDs)
   - Extraction status display
   - **Default**: Open

2. **🎙️ Auto-Transcription (Whisper)**
   - Language selection (ja/en/zh)
   - Model selection (base/small/medium)
   - Track selector with checkboxes (Select All/Clear buttons)
   - Transcribe button with progress tracking
   - Real-time progress bar + current track display
   - Cancel button during transcription
   - **Default**: Open

3. **🎵 Track Selection**
   - Radio button list of all extracted tracks
   - Shows track ID, title/path
   - "Load Runs" button
   - **Default**: Open
   - **Visibility**: Only shown when tracks exist

4. **📝 Transcript & Translation Management**
   - Two-column grid layout
   - **Left column**: Transcript runs list, JSON editor, Create button
   - **Right column**: Translation runs, auto-translate controls, live lines
   - View/Set Active/Delete actions per run
   - **Default**: Open
   - **Visibility**: Only shown when track selected

5. **⚙️ Pipeline Jobs**
   - Status filter dropdown (All/Queued/Running/Completed/Failed)
   - Job list with retry functionality
   - Refresh button
   - **Default**: Collapsed

---

### 2. API Tab (Provider-Based Organization)

**Before**: Flat list of all provider settings
**After**: 3 provider-specific cards + header/footer sections

#### Layout

**Top Section** (Always visible):
- Default translation provider selector (Gemini/OpenRouter/Chutes)

**Collapsible Cards**:

1. **🟢 Gemini Settings**
   - Model input
   - API key input
   - Key status indicator (configured/missing + source)
   - **Default**: Open

2. **🔵 OpenRouter Settings**
   - Model input
   - API key input
   - Key status indicator
   - **Default**: Collapsed

3. **🟣 Chutes Settings**
   - Model input
   - API key input
   - Key status indicator
   - **Default**: Collapsed

**Bottom Section** (Always visible):
- Save/Test/Reload buttons (row 1)
- Clear Gemini/OpenRouter/Chutes key buttons (row 2)
- Success/error messages
- Info note: "💡 Changes apply immediately..."

---

### 3. Left Sidebar (Selective Collapsing)

**Changed sections** (now collapsible):

1. **Filters Section**
   - Search box
   - Seiyuu/Tag/Status/Confidence/Sort selectors
   - Favorite toggle + Clear button
   - **Default**: Open
   - **Header**: Clickable with toggle arrow

2. **Maintenance Section**
   - Duplicate codes/missing covers/stale files stats
   - Refresh/Preview/Cleanup/Rebuild buttons
   - Stale cover sample list
   - Success/error messages
   - **Default**: Collapsed
   - **Header**: Clickable with toggle arrow

3. **Ops / Health Section**
   - Scan/Fetch status
   - Recent jobs count
   - Refresh button
   - Error summary
   - Recent jobs list (last 6)
   - **Default**: Collapsed
   - **Header**: Clickable with toggle arrow

**Unchanged sections** (always visible):
- Title window (Scan/Fetch/Paths buttons)
- Stats bar
- Last fetch summary
- Scan paths panel (conditional)

---

## Technical Implementation

### New CSS Classes

```css
/* Main card system */
.card-section         /* Collapsible card container */
.card-header          /* Clickable header with hover effect */
.card-title           /* Title with emoji + text */
.card-toggle          /* Animated arrow indicator (▼) */
.card-content         /* Collapsible content area */

/* Sidebar variant */
.collapsible-section  /* Sidebar section container */
.section-header       /* Sidebar clickable header */
.section-content      /* Sidebar collapsible content */

/* States */
.collapsed            /* Applied to toggle arrow and content when closed */
```

### CSS Features

- **Smooth animations**: 300ms transitions for expand/collapse
- **Max-height transitions**: `max-height: 1000px` → `max-height: 0`
- **Opacity fade**: `opacity: 1` → `opacity: 0`
- **Arrow rotation**: Toggle arrow rotates when collapsed
- **Hover effects**: Subtle background highlight on headers

### JavaScript State Management

**New reactive refs**:
```javascript
pipelineSectionsOpen: {
  extraction: true,
  transcription: true,
  trackSelection: true,
  transcriptManagement: true,
  jobs: false
}

apiSectionsOpen: {
  gemini: true,
  openrouter: false,
  chutes: false
}

sidebarSectionsOpen: {
  filters: true,
  maintenance: false,
  ops: false
}
```

**New toggle functions**:
- `togglePipelineSection(sectionName)` - Toggle pipeline cards
- `toggleApiSection(sectionName)` - Toggle API provider cards
- `toggleSidebarSection(sectionName)` - Toggle sidebar sections

### HTML Pattern

```html
<div class="card-section">
  <div class="card-header" @click="togglePipelineSection('extraction')">
    <div class="card-title">
      <span>📦</span>
      <span>Item Selection & Extraction</span>
    </div>
    <div class="card-toggle" :class="{ collapsed: !pipelineSectionsOpen.extraction }">▼</div>
  </div>
  <div class="card-content" :class="{ collapsed: !pipelineSectionsOpen.extraction }">
    <!-- Content here -->
  </div>
</div>
```

---

## User Benefits

### Usability Improvements
- ✅ **Reduced clutter** - Advanced features hidden by default
- ✅ **Better focus** - See only what you're working on
- ✅ **Faster navigation** - Logical grouping of related features
- ✅ **Cleaner workflow** - No more endless scrolling
- ✅ **Progressive disclosure** - Expand sections as needed

### Visual Improvements
- ✅ **Clear hierarchy** - Emoji icons + descriptive titles
- ✅ **Smooth animations** - Professional feel
- ✅ **Consistent design** - Same pattern across all tabs
- ✅ **Better spacing** - Proper padding and margins
- ✅ **Responsive layout** - Grid adapts to screen size

### Workflow Improvements
- ✅ **Smart defaults** - Common tasks visible, advanced collapsed
- ✅ **Session persistence** - State maintained during use
- ✅ **Contextual visibility** - Sections appear when relevant (e.g., track selection only shown when tracks exist)

---

## Migration Notes

### Breaking Changes
**None** - All functionality preserved, only presentation changed

### Backwards Compatibility
**Full** - Existing data, API endpoints, and functionality unchanged

### User Adaptation
- **Learning curve**: Minimal - intuitive click-to-expand pattern
- **Muscle memory**: Preserved - all features in same general locations
- **Discoverability**: Improved - clear section headers with icons

---

## File Changes

| File | Lines Changed | Description |
|------|--------------|-------------|
| `static/index.html` | ~200 | Restructured Pipeline/API tabs, sidebar sections |
| `static/css/style.css` | +45 | Added card/section styles |
| `static/js/app.js` | +30 | Added state refs and toggle functions |

---

## Future Enhancements

Potential improvements for future sessions:

1. **Remember section state** - Save open/closed preferences to localStorage
2. **Keyboard shortcuts** - Collapse/expand with hotkeys
3. **Drag to reorder** - Let users customize card order
4. **Minimize all** - Quick collapse all sections button
5. **Section search** - Filter cards by keyword
6. **Custom layouts** - Save/load layout presets

---

## Testing Checklist

- [x] Pipeline cards expand/collapse smoothly
- [x] API provider cards expand/collapse smoothly
- [x] Sidebar sections expand/collapse smoothly
- [x] Toggle arrows rotate correctly
- [x] Hover effects work on all headers
- [x] Default states correct (important sections open)
- [x] Responsive layout works on mobile
- [x] All functionality preserved
- [x] No console errors
- [x] State persists during session

---

## Developer Notes

### Adding New Collapsible Sections

**Pipeline Tab**:
1. Add new key to `pipelineSectionsOpen` ref (default true/false)
2. Use card-section HTML pattern
3. Export new state in return statement

**API Tab**:
1. Add new key to `apiSectionsOpen` ref
2. Use card-section HTML pattern
3. Export new state in return statement

**Sidebar**:
1. Add new key to `sidebarSectionsOpen` ref
2. Use section-header/section-content HTML pattern
3. Export new state in return statement

### CSS Customization

Adjust animation speed:
```css
.card-content {
  transition: max-height 0.3s ease, opacity 0.3s ease;  /* Change 0.3s */
}
```

Adjust max-height for very large sections:
```css
.card-content {
  max-height: 1000px;  /* Increase if content taller than 1000px */
}
```

---

**Implemented by**: Claude Sonnet 4.5
**Session**: UI Overhaul - Card-Based Layout
**Completion Date**: 2026-02-15
