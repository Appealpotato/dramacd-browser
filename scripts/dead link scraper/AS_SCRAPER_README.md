# anime-sharing.com scraper

Scrapes XenForo thread listings from anime-sharing.com, extracts DLsite product codes from titles, diffs them against `data/library.db`, and writes a CSV of codes you don't have.

## Setup

1. Copy `as_config.example.json` -> `as_config.json` and fill in:
   - `forum_urls` — for this project the only target is `https://www.anime-sharing.com/forums/otome-doujinshi-voices-misc.154/`. Note the path is `/forums/` (plural).
   - `cookie` — open DevTools -> Network -> click any anime-sharing.com request -> Headers -> copy the entire `Cookie:` header value verbatim. Required for role-gated content.
   - `max_pages` — defaults to 42.
   - `delay` — base seconds between requests, default 1.5. Don't go below 1.0.
   - `jitter` — random spread around `delay` as a fraction. Default `0.5` = each sleep is uniformly drawn from `[delay*0.5, delay*1.5]`. Set to `0` to disable jitter.

2. The script uses `httpx`, `beautifulsoup4`, and `lxml` — already in `requirements.txt`.

## Run

```powershell
cd "H:\DRAMACD APP\DRAMACD\dramacd-browser"
python "scripts\dead link scraper\scrape_anime_sharing.py" --config "scripts\dead link scraper\as_config.json"
```

CLI flags override config: `--forum-url`, `--max-pages`, `--delay`, `--jitter`, `--cookie`, `--output`, `--db`, `--include-known`.

You can also pass the cookie via env: `$env:AS_COOKIE = "..."`.

## Output

`as_missing.csv` (or `--output PATH`) with columns:

| column | meaning |
|---|---|
| `code` | normalized product code (RJ/BJ/VJ + digits) |
| `in_db` | 1 if already in `items.product_code` |
| `in_ignored` | 1 if in `ignored_codes` (you've explicitly skipped it) |
| `thread_title` | original AS thread title |
| `thread_url` | direct link |
| `last_post` | ISO timestamp of latest reply |
| `age_days` | days since last post |
| `likely_dead` | 1 if `age_days >= 365` (rough proxy for stale links) |

By default rows where `in_db=1` are filtered out — pass `--include-known` to keep them.

## Notes

- The regex mirrors `scanner.py`: `RJ/BJ/VJ`, plus `DLJ-/DLB-/DMJ-/RE-ESC-/vst` variants normalized to their RJ/BJ equivalents. Bare 6-8 digit runs are only matched as a fallback when no prefixed code is found in the title (avoids grabbing years/episode numbers).
- If a code appears in multiple threads, the script keeps the thread with the most recent last-post (= most likely to still have a live link).
- If pages return 0 threads, the cookie probably expired or the forum URL is wrong — re-export and try again. The script logs `[warn] no threads parsed` in that case.
- `likely_dead` is heuristic only. AS uploads often go down before 1 year; treat the flag as "definitely stale," not "definitely live."
