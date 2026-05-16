# Card Theming Fix ✅

## Problem
Card outlines and backgrounds weren't changing between themes.

## Root Cause
The `.card` class in style.css uses CSS variables:
```css
.card {
    background: var(--bg-card);
    border: 1px solid var(--border);
}
```

But these variables weren't being set in the app-themes.css! So they were falling back to the original values defined in style.css.

## Solution
Added `--bg-card` and ensured `--border` is properly set for all three themes in app-themes.css:

### Starlit Theme
- `--bg-card: rgba(255, 255, 255, 0.03)` (semi-transparent white)
- `--border: rgba(244, 234, 247, 0.05)` (purple-tinted borders)

### Sweet Theme
- `--bg-card: rgba(255, 245, 250, 0.65)` (light pink cards)
- `--border: rgba(220, 150, 200, 0.2)` (pink-tinted borders)

### Eclipse Theme
- `--bg-card: #1a212d` (dark blue-gray cards)
- `--border: rgba(156, 163, 175, 0.1)` (subtle gray borders)

## Result
✅ Card backgrounds now change with themes
✅ Card borders now change with themes
✅ Card hover states work (border-color: var(--accent) also responds to theme)

## Files Changed
- `/static/css/app-themes.css` - Added `--bg-card` to all three theme definitions
