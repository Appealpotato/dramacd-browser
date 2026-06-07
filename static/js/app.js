const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue;

// Filter chip component
const FilterChip = {
    props: ['label'],
    emits: ['remove'],
    template: `
        <span class="filter-chip">
            {{ label }}
            <button @click="$emit('remove')">&times;</button>
        </span>
    `
};

const app = createApp({
    components: { FilterChip },

    setup() {
        // State
        const items = ref([]);
        const totalItems = ref(0);
        const loading = ref(false);
        const stats = ref(null);
        const seiyuuList = ref([]);
        const tagList = ref([]);
        const selectedItem = ref(null);
        const searchQuery = ref('');
        const sortBy = ref('created_at|desc');
        const currentLang = ref(localStorage.getItem('dramacd_lang') || 'en');
              watch(currentLang, (val) => {
              localStorage.setItem('dramacd_lang', val);
              });

        // Library grid display mode: 'grid' (cover + meta), 'cover' (cover-only
        // wall with hover tooltip), or 'list' (compact 40px rows). Persisted
        // PER SUBTAB so drama CDs can be 'list' while games is 'grid' etc.
        // The legacy single-key 'dramacd_library_view' is read once as the
        // fallback for any unset subtab so users with existing prefs keep
        // their previous default.
        const _validViewModes = ['grid', 'cover', 'list'];
        const _legacyViewMode = localStorage.getItem('dramacd_library_view');
        const _legacyView = _validViewModes.includes(_legacyViewMode) ? _legacyViewMode : 'grid';
        function _hydrateViewMode(key) {
            const raw = localStorage.getItem(key);
            return _validViewModes.includes(raw) ? raw : _legacyView;
        }
        const libraryViewModeDramaCds = ref(_hydrateViewMode('dramacd_library_view_dramacd'));
        const libraryViewModeGames = ref(_hydrateViewMode('dramacd_library_view_games'));
        const libraryViewModeTokutens = ref(_hydrateViewMode('dramacd_library_view_tokutens'));
        watch(libraryViewModeDramaCds, (v) => localStorage.setItem('dramacd_library_view_dramacd', v));
        watch(libraryViewModeGames, (v) => localStorage.setItem('dramacd_library_view_games', v));
        watch(libraryViewModeTokutens, (v) => localStorage.setItem('dramacd_library_view_tokutens', v));
        // The active view follows the current subtab. Computed get/set so
        // existing `libraryViewMode.value = X` callers (and v-model) write
        // to the right ref. librarySubtab is declared further down but
        // computed getters are lazy — by the time this is read, setup has
        // finished and all refs are live.
        const libraryViewMode = computed({
            get: () => {
                const s = librarySubtab.value;
                if (s === 'games') return libraryViewModeGames.value;
                if (s === 'tokutens') return libraryViewModeTokutens.value;
                return libraryViewModeDramaCds.value;
            },
            set: (val) => {
                if (!_validViewModes.includes(val)) return;
                const s = librarySubtab.value;
                if (s === 'games') libraryViewModeGames.value = val;
                else if (s === 'tokutens') libraryViewModeTokutens.value = val;
                else libraryViewModeDramaCds.value = val;
            },
        });
        function setLibraryViewMode(mode) {
            if (_validViewModes.includes(mode)) libraryViewMode.value = mode;
        }
        const newCustomTag = ref('');
        const selectedSeiyuuOption = ref('');
        const selectedTagOption = ref('');
        const currentOffset = ref(0);
        const pageSize = 100;
        const selectedIds = ref(new Set());
        // Feedback refs that should auto-clear so success/error toasts don't
        // sit around forever after the action completed. Mirrors a normal
        // ref but schedules a clear-to-empty whenever you set a non-empty
        // value. ttl in ms.
        function transientRef(initial = '', ttl = 3000) {
            const r = ref(initial);
            let timer = null;
            watch(r, (val) => {
                if (timer) { clearTimeout(timer); timer = null; }
                if (val) {
                    timer = setTimeout(() => { r.value = ''; }, ttl);
                }
            });
            return r;
        }

        // Mobile-client detection. Used to swap source-rate FLAC for an AAC
        // transcode in the audio player — mobile audio paths typically lock
        // output to 48kHz and accumulate linear drift on 44.1k FLAC over the
        // length of a track. Native FLAC stays the desktop default.
        const _mobileUA = /Android|iPhone|iPad|iPod|Mobile|webOS|BlackBerry/i;
        function isMobileClient() {
            return typeof navigator !== 'undefined' && _mobileUA.test(navigator.userAgent || '');
        }

        const bulkLoading = ref(false);
        const bulkMessage = transientRef('');
        const bulkError = transientRef('');
        const showBulkActions = ref(false);
        const bulkMenuOpen = ref(false);
        const detailKebabOpen = ref(false);
        // Per-seiyuu name swap toggle. A ref(Set) is used (instead of a
        // reactive({})) because Vue tracks `.value` reassignment cleanly,
        // whereas dynamic-key access on a reactive object can miss updates
        // for keys that didn't exist at template-compile time.
        const seiyuuFlippedSet = ref(new Set());

        // Activity drawer + toast state (autopilot pipeline monitor)
        const autopilotJobs = ref([]);                  // array, freshly fetched each poll
        const autopilotPrevStatuses = new Map();        // job_id -> last seen status (for toast triggers)
        const ACTIVITY_DRAWER_OPEN_KEY = 'dramacd.activityDrawerOpen';
        const activityDrawerOpen = ref(false);
        try {
            activityDrawerOpen.value = localStorage.getItem(ACTIVITY_DRAWER_OPEN_KEY) === '1';
        } catch (_) {}
        watch(activityDrawerOpen, (val) => {
            try { localStorage.setItem(ACTIVITY_DRAWER_OPEN_KEY, val ? '1' : '0'); } catch (_) {}
        });
        const activityDrawerAutoOpened = ref(false);    // true when opened by us; we may auto-close
        const expandedActivityJob = ref(null);
        const activityEvents = reactive({});            // job_id -> events array
        const activityEventsLoading = reactive({});     // job_id -> bool
        const autopilotToasts = ref([]);
        let autopilotToastSeq = 0;
        let autopilotPollTimer = null;
        // Item resolution: db.items isn't always in the loaded library cache
        // (e.g. when running pipeline jobs from Workshop) so we fetch missing
        // ones lazily and cache the resolved display title here. Reactive so
        // the drawer re-renders once a fetch completes.
        const activityItemCache = reactive({});         // item_id -> { title, code }
        const activityItemFetchInflight = new Set();    // dedupe concurrent fetches
        // Track resolution mirrors item resolution: per-item dict of track_id -> title.
        const activityTracksCache = reactive({});       // item_id -> { [track_id]: { title, title_en, track_index } }
        const activityTracksFetchInflight = new Set();
        // User-dismissed jobs (drawer hides them locally, server is unaffected).
        // Persisted to localStorage so a refresh doesn't resurrect rows the
        // user already cleared.
        const DISMISSED_ACTIVITY_KEY = 'dramacd.dismissedActivityJobs';
        const dismissedActivityJobs = ref(new Set());
        try {
            const raw = localStorage.getItem(DISMISSED_ACTIVITY_KEY);
            if (raw) {
                const arr = JSON.parse(raw);
                if (Array.isArray(arr)) {
                    dismissedActivityJobs.value = new Set(
                        arr.map(n => Number(n)).filter(n => Number.isFinite(n))
                    );
                }
            }
        } catch (_) {
            // localStorage may be disabled (private mode etc.); silently fall
            // back to in-memory state so the drawer still works.
        }
        watch(dismissedActivityJobs, (val) => {
            try {
                localStorage.setItem(DISMISSED_ACTIVITY_KEY, JSON.stringify(Array.from(val)));
            } catch (_) {}
        });

        // Override state
        const overrideCodeInput = ref('');
        const overrideLoading = ref(false);
        const overrideError = transientRef('');
        const overrideSuccess = transientRef('');

        // Confirm/unconfirm state
        const showOverrideSection = ref(false);
        const showCoverSection = ref(false);
        const confirmLoading = ref(false);
        const unconfirmLoading = ref(false);
        const confirmError = transientRef('');
        const confirmSuccess = transientRef('');

        // Delete state
        const deleteLoading = ref(false);
        const deleteError = transientRef('');
        const coverUploadLoading = ref(false);
        const coverUploadError = transientRef('');
        const coverUploadSuccess = transientRef('');

        // Refresh metadata state
        const refreshMetadataLoading = ref(false);
        const refreshMetadataError = transientRef('');
        const refreshMetadataSuccess = transientRef('');
        const coverFileInput = ref(null);
        const metadataTranslateLoading = ref(false);
        const metadataTranslateError = transientRef('');
        const metadataTranslateSuccess = transientRef('');
        const metadataTranslateStep = ref('');
        const metadataTranslateElapsedSec = ref(0);

        const filters = reactive({
            seiyuu: [],
            tag: [],
            translation_status: '',
            listen_status: '',   // '' = all, or one of backlog/want_to_listen/listening/completed/on_hold/dropped/wishlist
            // Content type discriminator. Defaults to drama_cd so the library
            // opens on the normal 700+ drama-CD list; tokuten/all/game are
            // explicit opt-ins. 'game' shows nothing yet — placeholder for the
            // upcoming Games surface.
            kind: 'drama_cd',
            favorite: false,
            has_metadata: null,  // null = no filter, true = with meta, false = pending
            is_manual: null,     // null/true/false — tri-state (All / Manual / Scanned)
        });

        // Scan/fetch progress
        const scanRunning = ref(false);
        const scanProgress = reactive({ total: 0, processed: 0, current: null, paused: false, stopping: false });
        const fetchRunning = ref(false);
        const fetchProgress = reactive({ total: 0, completed: 0, current: null, paused: false, stopping: false, stopped: false });
        const lastFetchSummary = ref(null);

        // Scan path settings
        const showScanPathsPanel = ref(false);
        const scanPaths = ref([]);
        const scanPathsInput = ref('');
        const scanPathsLoading = ref(false);
        const scanPathsSaving = ref(false);
        const scanPathsError = transientRef('');
        const scanPathsSuccess = transientRef('');

        // === Games wing state (Phase 1) ===
        const gamesScanPaths = ref([]);
        const gamesScanPathsInput = ref('');
        const gamesScanPathsLoading = ref(false);
        const gamesScanPathsSaving = ref(false);
        const gamesScanPathsError = transientRef('');
        const gamesScanPathsSuccess = transientRef('');
        const gamesScanning = ref(false);

        // Tokuten library paths — third member of the scan-paths trio
        // (drama CDs / games / tokutens). Same shape as games scan paths.
        const tokutenScanPaths = ref([]);
        const tokutenScanPathsInput = ref('');
        const tokutenScanPathsLoading = ref(false);
        const tokutenScanPathsSaving = ref(false);
        const tokutenScanPathsError = transientRef('');
        const tokutenScanPathsSuccess = transientRef('');
        const tokutenScanning = ref(false);
        // Library subtab: 'drama_cds' | 'games' | 'tokutens'. Drives both the
        // tab-strip UI and which backend endpoint the list pulls from.
        const _validLibrarySubtabs = ['drama_cds', 'games', 'tokutens'];
        const librarySubtab = ref(
            _validLibrarySubtabs.includes(localStorage.getItem('dramacd_library_subtab'))
                ? localStorage.getItem('dramacd_library_subtab')
                : 'drama_cds'
        );
        watch(librarySubtab, (val) => {
            localStorage.setItem('dramacd_library_subtab', val);
        });
        function setLibrarySubtab(name) {
            if (!_validLibrarySubtabs.includes(name)) return;
            if (librarySubtab.value === name) return;
            // Clear cross-subtab selection so leftover ctrl-click selections
            // from another subtab don't fight the new tab's interactions.
            try {
                if (selectedIds && selectedIds.value) selectedIds.value = new Set();
                if (selectedGameIds && selectedGameIds.value) selectedGameIds.value = new Set();
            } catch (_) {}
            librarySubtab.value = name;
            // Drama CDs / Tokutens both use /api/items with the existing
            // include_tokutens / only_tokutens flags routed via loadItems.
            // Games has its own endpoint. Trigger a refresh for the new tab.
            if (name === 'games') {
                loadGames();
                loadGameStats();
                loadGameDistinctOptions();
                loadDuplicateGameGroups();
            } else if (name === 'tokutens') {
                loadItems();
                loadTokutenStats();
            } else {
                loadItems();
            }
        }

        // === Per-subtab filter state + persistence ===
        // The drama-CD subtab uses the existing `filters` + `searchQuery`. The
        // Games and Tokutens subtabs each get their own reactive set so swapping
        // tabs preserves the inactive tab's filter selection and the persisted
        // localStorage keys stay neatly separated.
        const gameFilters = reactive({
            play_status: '',         // single value (set by sub-pill click)
            platform: '',
            developer: '',
            custom_tag: '',
            favorite: false,
            matched: null,           // null/true/false (All / Matched / Unmatched)
            is_manual: null,         // null/true/false (All / Manual / Scanned)
            include_wishlist: false, // wishlist hidden in main grid by default
        });
        const tokutenFilters = reactive({
            kind: '',
            source: '',
            favorite: false,
            is_manual: null,
        });

        // Search is unified across subtabs — one query (the existing
        // `searchQuery` ref) applies to whichever subtab is currently active.
        // Switching tabs re-runs the active list loader with the same query
        // so the user doesn't need to retype. gameSearchQuery/tokutenSearchQuery
        // are kept as aliases for backwards compat with the rest of the file.
        const gameSearchQuery = searchQuery;
        const tokutenSearchQuery = searchQuery;

        const _FILTER_STORAGE_KEYS = {
            drama_cds: 'dramacd_filters_dramacd',
            games: 'dramacd_filters_games',
            tokutens: 'dramacd_filters_tokutens',
        };

        function _hydrateFiltersFromStorage() {
            try {
                const raw = localStorage.getItem(_FILTER_STORAGE_KEYS.drama_cds);
                if (raw) {
                    const data = JSON.parse(raw);
                    if (Array.isArray(data.seiyuu)) filters.seiyuu = [...data.seiyuu];
                    if (Array.isArray(data.tag)) filters.tag = [...data.tag];
                    if (typeof data.translation_status === 'string') filters.translation_status = data.translation_status;
                    if (typeof data.listen_status === 'string') filters.listen_status = data.listen_status;
                    if (typeof data.favorite === 'boolean') filters.favorite = data.favorite;
                    if (data.has_metadata === true || data.has_metadata === false || data.has_metadata === null) filters.has_metadata = data.has_metadata;
                    if (data.is_manual === true || data.is_manual === false || data.is_manual === null) filters.is_manual = data.is_manual;
                    if (typeof data.searchQuery === 'string') searchQuery.value = data.searchQuery;
                }
            } catch (_) {}
            try {
                const raw = localStorage.getItem(_FILTER_STORAGE_KEYS.games);
                if (raw) {
                    const data = JSON.parse(raw);
                    for (const k of ['play_status', 'platform', 'developer', 'custom_tag']) {
                        if (typeof data[k] === 'string') gameFilters[k] = data[k];
                    }
                    if (typeof data.favorite === 'boolean') gameFilters.favorite = data.favorite;
                    if (typeof data.include_wishlist === 'boolean') gameFilters.include_wishlist = data.include_wishlist;
                    if (data.matched === true || data.matched === false || data.matched === null) gameFilters.matched = data.matched;
                    if (data.is_manual === true || data.is_manual === false || data.is_manual === null) gameFilters.is_manual = data.is_manual;
                    if (typeof data.searchQuery === 'string') gameSearchQuery.value = data.searchQuery;
                }
            } catch (_) {}
            try {
                const raw = localStorage.getItem(_FILTER_STORAGE_KEYS.tokutens);
                if (raw) {
                    const data = JSON.parse(raw);
                    for (const k of ['kind', 'source']) {
                        if (typeof data[k] === 'string') tokutenFilters[k] = data[k];
                    }
                    if (typeof data.favorite === 'boolean') tokutenFilters.favorite = data.favorite;
                    if (data.is_manual === true || data.is_manual === false || data.is_manual === null) tokutenFilters.is_manual = data.is_manual;
                    if (typeof data.searchQuery === 'string') tokutenSearchQuery.value = data.searchQuery;
                }
            } catch (_) {}
        }
        _hydrateFiltersFromStorage();

        // Watchers persist on change. Deep watch picks up nested array writes
        // (filters.seiyuu.push, etc) — the small JSON payload is fine to
        // re-serialize on every keystroke since filter dicts are tiny.
        watch(
            () => ({
                seiyuu: filters.seiyuu,
                tag: filters.tag,
                translation_status: filters.translation_status,
                listen_status: filters.listen_status,
                favorite: filters.favorite,
                has_metadata: filters.has_metadata,
                is_manual: filters.is_manual,
                searchQuery: searchQuery.value,
            }),
            (val) => {
                try { localStorage.setItem(_FILTER_STORAGE_KEYS.drama_cds, JSON.stringify(val)); } catch (_) {}
            },
            { deep: true },
        );
        watch(
            () => ({ ...gameFilters, searchQuery: gameSearchQuery.value }),
            (val) => {
                try { localStorage.setItem(_FILTER_STORAGE_KEYS.games, JSON.stringify(val)); } catch (_) {}
            },
            { deep: true },
        );
        watch(
            () => ({ ...tokutenFilters, searchQuery: tokutenSearchQuery.value }),
            (val) => {
                try { localStorage.setItem(_FILTER_STORAGE_KEYS.tokutens, JSON.stringify(val)); } catch (_) {}
            },
            { deep: true },
        );

        // Unified search input — `currentSearchQuery` dispatches based on the
        // active subtab so the same <input v-model="currentSearchQuery"> swaps
        // its content as the user changes tabs.
        const currentSearchQuery = computed({
            get: () => {
                const s = librarySubtab.value;
                if (s === 'games') return gameSearchQuery.value;
                if (s === 'tokutens') return tokutenSearchQuery.value;
                return searchQuery.value;
            },
            set: (val) => {
                const s = librarySubtab.value;
                if (s === 'games') gameSearchQuery.value = val;
                else if (s === 'tokutens') tokutenSearchQuery.value = val;
                else searchQuery.value = val;
            },
        });

        // Games stats (filled by /api/games/stats) + tokuten stats. The
        // existing `stats` ref stays drama-CD-only; the sidebar swaps panels
        // based on librarySubtab.
        const gameStats = ref(null);
        const tokutenStats = ref(null);

        // Unmatched-games cleanup queue (built out in task #8). Predeclared
        // so applyGameStatFilter's 'unmatched' branch can flip the overlay
        // open without a TDZ reference error.
        const unmatchedQueueOpen = ref(false);
        const unmatchedQueueItems = ref([]);
        const unmatchedQueueIndex = ref(0);
        const unmatchedQueueLoading = ref(false);
        const unmatchedQueueSearch = ref('');
        const unmatchedQueueResults = ref([]);
        const unmatchedQueueSearching = ref(false);
        const unmatchedQueueBusy = ref(false);
        const unmatchedQueueMessage = transientRef('');
        const unmatchedQueueError = transientRef('');
        // Manual-edit pane state. Doujin / unlisted games have no VNDB entry,
        // so the queue needs an in-place manual path. Right pane swaps from
        // VNDB search results to a field-by-field editor + cover upload.
        const unmatchedQueueManualMode = ref(false);
        const unmatchedQueueManualDraft = ref(null);
        const unmatchedQueueCoverInput = ref(null);
        async function loadGameStats() {
            try {
                const resp = await fetch('/api/games/stats');
                if (!resp.ok) return;
                gameStats.value = await resp.json();
            } catch (_) {}
        }
        // Distinct developer / platform / custom-tag values across the whole
        // games table. Used to populate the sidebar dropdowns.
        const gameDistinctOptions = ref({ developers: [], platforms: [], custom_tags: [] });
        async function loadGameDistinctOptions() {
            try {
                const resp = await fetch('/api/games/distinct');
                if (!resp.ok) return;
                gameDistinctOptions.value = await resp.json();
            } catch (_) {}
        }

        // Duplicate-vndb_id detection. Two rows pointing at the same VN are
        // almost certainly the same game on different platforms / install
        // locations — count surfaces as a "Duplicates" stat-pill in the Games
        // sidebar; clicking the pill opens a review modal with a single
        // inline-confirm "Merge all" footer. Loaded with the games list.
        const duplicateGameGroups = ref([]);
        const mergeDuplicatesBusy = ref(false);
        const duplicatesModalOpen = ref(false);
        const pendingMergeAllDuplicates = ref(false);
        async function loadDuplicateGameGroups() {
            try {
                const resp = await fetch('/api/games/duplicates');
                if (!resp.ok) return;
                const data = await resp.json();
                duplicateGameGroups.value = Array.isArray(data.groups) ? data.groups : [];
            } catch (_) {}
        }
        const duplicateRowsCount = computed(() =>
            duplicateGameGroups.value.reduce((acc, g) => acc + Math.max(0, (g.members || []).length - 1), 0)
        );
        function openDuplicatesModal() {
            pendingMergeAllDuplicates.value = false;
            duplicatesModalOpen.value = true;
        }
        function closeDuplicatesModal() {
            duplicatesModalOpen.value = false;
            pendingMergeAllDuplicates.value = false;
        }
        function askMergeAllDuplicates() {
            if (!duplicateGameGroups.value.length) return;
            pendingMergeAllDuplicates.value = true;
        }
        function cancelMergeAllDuplicates() {
            pendingMergeAllDuplicates.value = false;
        }
        async function mergeAllDuplicates() {
            if (!duplicateGameGroups.value.length) return;
            mergeDuplicatesBusy.value = true;
            try {
                const resp = await fetch('/api/games/merge-duplicates', { method: 'POST' });
                if (!resp.ok) {
                    pushToast({ kind: 'failure', title: 'Merge failed', ttl: 4000 });
                    return;
                }
                const data = await resp.json();
                pushToast({
                    kind: 'success',
                    title: 'Duplicates merged',
                    body: `${data.rows_merged} row${data.rows_merged === 1 ? '' : 's'} folded into ${data.groups_processed} primary game${data.groups_processed === 1 ? '' : 's'}`,
                    ttl: 4500,
                });
                await Promise.all([loadGames(), loadGameStats(), loadDuplicateGameGroups(), loadGameDistinctOptions()]);
                closeDuplicatesModal();
            } catch (err) {
                pushToast({ kind: 'failure', title: 'Merge failed', body: String(err.message || err), ttl: 4000 });
            } finally {
                mergeDuplicatesBusy.value = false;
            }
        }
        async function loadTokutenStats() {
            try {
                const resp = await fetch('/api/tokutens/stats');
                if (!resp.ok) return;
                tokutenStats.value = await resp.json();
            } catch (_) {}
        }
        // Games list (separate from `items` since they live in a separate table).
        const gamesItems = ref([]);
        const gamesTotal = ref(0);
        const gamesLoading = ref(false);
        // Games detail-panel state (parallel to selectedItem for drama CDs).
        const selectedGame = ref(null);
        const gameDraft = ref(null);
        const gameEditing = ref(false);
        const gameSavingBusy = ref(false);
        const gameSaveError = transientRef('');

        // Items detail-panel edit mode (drama_cd / tokuten manual editing).
        // detailDraft holds the staged field values; the live selectedItem
        // stays read-only until saveDetailEdit commits.
        const detailEditing = ref(false);
        const detailDraft = ref(null);
        const detailSavingBusy = ref(false);
        const detailSaveError = transientRef('');
        // Set while the native OS file dialog is open for the manual-CD
        // archive_path field. The picker is server-side (FastAPI pops a
        // tkinter dialog in a subprocess) so the user could in theory leave
        // it sitting for minutes; the Browse button disables itself instead
        // of spawning a second dialog.
        const archiveBrowseBusy = ref(false);

        // Tokuten ↔ game cross-link state. When the user opens a tokuten
        // detail panel we lazy-fetch its tokuten row (for vndb_id), then,
        // if vndb_id is set, look for a local game with the same id. The
        // reverse direction (game → linked tokutens) is loaded when the
        // game detail panel opens. Both refs are independent so panel
        // navigation doesn't fight itself.
        const linkedTokutenForItem = ref(null);   // { vndb_id, ... } when current item is a tokuten
        const linkedGameForTokuten = ref(null);   // matching game row or null
        const linkedTokutensForGame = ref([]);    // list of tokutens for the current selectedGame

        // VNDB search state for the games edit panel. vndbResults stores the
        // trimmed candidate summaries returned by /api/games/vndb/search.
        const vndbQuery = ref('');
        const vndbResults = ref([]);
        const vndbSearching = ref(false);
        const vndbSearchError = transientRef('');
        let _vndbSearchTimer = null;

        // External metadata fetch (Gamers / Chil-Chil) for manual items and
        // tokutens. Preview-then-apply: fetch-url/search never write; the
        // preview modal's checkboxes pick which fields POST /api/metadata/apply
        // actually commits, so a fetch can't clobber hand-edited data.
        const metaFetchUrl = ref('');
        const metaFetchBusy = ref(false);
        const metaFetchError = transientRef('', 8000);
        const metaSearchQuery = ref('');
        const metaSearchResults = ref([]);
        const metaSearching = ref(false);
        let _metaSearchTimer = null;
        const metaPreview = ref(null);       // normalized metadata dict from the backend
        const metaPreviewFields = ref({});    // { fieldName: bool } checkbox states
        const metaApplyBusy = ref(false);
        const metaApplyError = transientRef('', 8000);
        // Multi-volume: checked search-result URLs, merged via fetch-multi.
        const metaSelectedUrls = ref([]);
        // Mini cover gallery (media_assets) for the detail panel.
        const galleryOpen = ref(false);
        const galleryMedia = ref([]);
        const galleryLoading = ref(false);
        const galleryBusy = ref(false);
        const galleryError = transientRef('', 6000);

        // Game cover upload state (parallel to coverUploadLoading for items).
        const gameCoverFileInput = ref(null);
        const gameCoverUploadLoading = ref(false);
        const gameCoverUploadError = transientRef('');
        const gameCoverUploadSuccess = transientRef('');

        // Bulk VNDB-match state. Run takes ~1–3 min for a typical library;
        // we keep the in-flight flag so the toolbar button disables.
        const vndbMatchBusy = ref(false);
        const vndbMatchMessage = transientRef('', 8000);
        const vndbMatchError = transientRef('', 8000);
        const scanRecursive = ref(true);
        const maintenanceLoading = ref(false);
        const maintenanceActionLoading = ref(false);
        const maintenancePreview = ref(null);
        const maintenanceMessage = transientRef('');
        const maintenanceError = transientRef('');
        const backfillTranscriptsBusy = ref(false);
        const backfillTranscriptsMessage = transientRef('');
        const backfillTranscriptsError = transientRef('');
        const backfillTranslationsBusy = ref(false);
        const backfillTranslationsMessage = transientRef('', 6000);
        const backfillTranslationsError = transientRef('', 8000);
        const transcriptIoBusy = ref('');
        const transcriptIoMessage = transientRef('');
        const transcriptIoError = transientRef('');
        const transcriptIoSummary = ref(null);
        const transcriptIoReplace = ref(false);
        const transcriptIoAcceptZip = ref(true);
        const transcriptIoFileInput = ref(null);
        const packageBusy = ref(false);
        const packageMessage = transientRef('');
        const packageError = transientRef('');
        const packageIncludeAudio = ref(false);
        const packageAllRuns = ref(false);
        const packagePreservePaths = ref(false);
        const packageIncludeSrt = ref(true);
        const packageIncludeTxt = ref(true);
        const packageIncludeTracklist = ref(true);
        const packageIncludeAllArchiveFiles = ref(false);
        const purgeBusy = ref(false);
        const purgeMessage = transientRef('');
        const purgeError = transientRef('');
        const mojibakeBusy = ref('');
        const mojibakeMessage = transientRef('');
        const mojibakeError = transientRef('');
        const mojibakePreview = ref(null);
        const trackNamesBusy = ref(false);
        const trackNamesMessage = transientRef('');
        const trackNamesError = transientRef('');
        const summariesBusy = ref('');
        const summariesMessage = transientRef('');
        const summariesError = transientRef('');
        const workspaceBusy = ref('');
        const workspaceMessage = transientRef('');
        const workspaceError = transientRef('');
        const workspaceOrphans = ref(null);
        const opsLoading = ref(false);
        const opsScanStatus = ref(null);
        const opsFetchStatus = ref(null);
        const opsErrorsExpanded = ref(false);
        const recentJobs = ref([]);
        // Unmatched-files modal (opened from the Unmatched stat pill).
        const unmatchedFilesPanelOpen = ref(false);
        const unmatchedFilesLoading = ref(false);
        const unmatchedFilesList = ref([]);
        const activeTab = ref('library');

        // === Scroll position retention across tab/subtab switches ===
        // Tabs render via v-if, so a tab's DOM (and its scroll position) is
        // destroyed on switch. Each tab owns one scroll container
        // (.main-content for Library, .pipeline-panel for Atelier/Player/
        // Settings); we snapshot its scrollTop before the swap (pre-flush
        // watcher: old DOM still mounted) and restore after nextTick once
        // the new tab's DOM exists. Keyed by tab — and by subtab within
        // Library so Drama CDs / Tokutens / Games each remember their own
        // spot.
        const _tabScrollMemo = {};
        function _scrollMemoKey(tab, subtab) {
            const t = tab !== undefined ? tab : activeTab.value;
            if (t !== 'library') return t;
            const s = subtab !== undefined ? subtab : librarySubtab.value;
            return 'library:' + s;
        }
        function _activeScrollEl() {
            return document.querySelector('.main-content, .pipeline-panel');
        }
        function _snapshotScroll(key) {
            const el = _activeScrollEl();
            if (el) _tabScrollMemo[key] = el.scrollTop;
        }
        function _restoreScroll(key) {
            const target = _tabScrollMemo[key] || 0;
            const attempt = () => {
                const el = _activeScrollEl();
                if (el) el.scrollTop = target;
                return el ? el.scrollTop : 0;
            };
            nextTick(() => {
                attempt();
                if (target <= 0) return;
                // Content often loads async after a switch (items refetch),
                // so the first attempt can clamp to 0 against a short
                // container. Re-apply a few times; bail once it sticks or
                // the user has started scrolling themselves.
                let tries = 0;
                const timer = setInterval(() => {
                    tries += 1;
                    const el = _activeScrollEl();
                    if (!el) { clearInterval(timer); return; }
                    if (Math.abs(el.scrollTop - target) <= 2 || tries >= 6) {
                        clearInterval(timer);
                        return;
                    }
                    // User grabbed the scrollbar mid-restore — leave it be.
                    if (el.scrollTop > 0 && tries > 1) { clearInterval(timer); return; }
                    el.scrollTop = target;
                }, 150);
            });
        }
        watch(activeTab, (newTab, oldTab) => {
            if (oldTab) _snapshotScroll(_scrollMemoKey(oldTab));
            _restoreScroll(_scrollMemoKey(newTab));
        });
        watch(librarySubtab, (newSub, oldSub) => {
            if (activeTab.value !== 'library') return;
            if (oldSub) _snapshotScroll(_scrollMemoKey('library', oldSub));
            _restoreScroll(_scrollMemoKey('library', newSub));
        });

        // Tokutens (game-bonus / community CDs) live in the same items table
        // as drama CDs. Filtered out by default via filters.kind='drama_cd';
        // user explicitly switches the Type filter to see them.
        const tokutenCreateBusy = ref(false);
        const tokutenCreateError = transientRef('', 5000);
        const addMenuOpen = ref(false);
        const pipelineEnabled = ref(false);
        const pipelineStatus = ref(null);
        const pipelineLoadError = transientRef('');
        const pipelineBusy = ref(false);
        const pipelineSelectedItemId = ref(null);
        const selectedWorkshopItem = ref(null); // Full item data for Workshop display
        const pipelineSectionsOpen = ref({
            extraction: true,
            package: false,
            transcription: true,
            trackSelection: true,
            transcriptManagement: true,
            jobs: false
        });
        const sidebarSectionsOpen = ref({
            filters: true,
            maintenance: false,
            ops: false
        });
        const pipelineTracks = ref([]);
        const pipelineTrackId = ref(null);
        const transcriptRuns = ref([]);
        const translationRuns = ref([]);
        const activeTranscriptRunId = ref(null);
        const activeTranslationRunId = ref(null);
        // Inline delete-confirm pattern: clicking the trash icon flips a card
        // into a ✓ / ✗ pair instead of opening a native confirm() dialog. Only
        // one card per list can be in this pending state at a time.
        const pendingDeleteTranscriptRunId = ref(null);
        const pendingDeleteTranslationRunId = ref(null);
        const pendingCleanupUnusedTranscripts = ref(false);
        const pipelineTranscriptRunId = ref(null);
        // Transient — auto-clears after a few seconds. "Queued extraction"
        // confirmations stay only briefly; the actual progress lives in the
        // Activity drawer as a job row.
        const pipelineActiveSummary = transientRef('', 3500);
        const pipelineForceExtract = ref(false);
        // Inline archive viewer (Workshop Archive panel). Cached per item id
        // so switching back to a CD doesn't re-shell 7z. List comes from
        // GET /api/pipeline/items/{id}/archive-contents.
        const archiveContents = ref(null);
        const archiveContentsLoading = ref(false);
        const archiveContentsCache = new Map();
        // View mode: 'list' (flat, current behavior) or 'grid' (folder
        // navigation with thumbnails for images). Persisted per-user.
        const archiveViewMode = ref('list');
        // Current folder path when navigating the grid view. Stored with a
        // trailing separator for prefix-matching convenience.
        const archiveCurrentPath = ref('');
        // List-view collapsed-folder set. In-memory only; resets when the
        // active CD changes. Folders not in this set are expanded (default).
        const archiveCollapsedFolders = ref(new Set());
        function toggleArchiveFolder(folder) {
            const next = new Set(archiveCollapsedFolders.value);
            if (next.has(folder)) next.delete(folder);
            else next.add(folder);
            archiveCollapsedFolders.value = next;
        }
        function isArchiveFolderCollapsed(folder) {
            return archiveCollapsedFolders.value.has(folder);
        }
        // Inline confirm for the destructive purge-audio icon (no native dialog).
        const pendingPurgeAudio = ref(false);
        // Workshop top search bar: autocomplete over the library to switch the
        // active CD without leaving Workshop. Replaces the old "Library Item
        // ID" number input — typing surfaces up to 10 drama-CD matches.
        const workshopSearchQuery = ref('');
        const workshopSearchResults = ref([]);
        const workshopSearchOpen = ref(false);
        const workshopSearchLoading = ref(false);
        let workshopSearchDebounce = null;
        // Track-count override editing state on the compact item card.
        const manualTrackCountEditing = ref(false);
        const manualTrackCountInput = ref('');
        const selectedTranscriptSegments = ref([]);
        const selectedTranscriptRunId = ref(null);
        const selectedTranscriptCleanText = ref('');
        const selectedTranslationSegments = ref([]);
        const selectedTranslationRunId = ref(null);

        // Shared inline segment editor state. Only one segment is edited at a
        // time across the whole app — Player and Workshop reuse the same buffer
        // so opening a new edit auto-closes any in-progress one.
        // kind: 'transcript' | 'translation'; surface: 'player' | 'workshop'.
        const editingSegment = ref(null);
        const editingSegmentText = ref('');
        const editingSegmentSaving = ref(false);
        const editingSegmentError = ref('');
        const transcribeLanguage = ref('ja');
        const transcribeModel = ref('small');
        const transcriptionInProgress = ref(false);
        const transcriptionStatus = ref(null);
        const transcriptionProgress = ref(null);
        const selectedTracksForTranscription = ref([]);
        const trackCodecFilter = ref('all'); // 'all', 'mp3', 'wav', 'other'
        const autoTranslateTargetLanguage = ref('en');
        const autoTranslateProvider = ref('gemini');
        const autoTranslateModel = ref('gemini-2.0-flash');
        const autoTranslateMaxTokens = ref(1000);
        const autoTranslateMaxLines = ref(20);
        const autoTranslateMaxRetries = ref(2);
        const autoTranslateRetryBackoff = ref(1.0);
        const autoTranslateGlossary = ref('');
        const autoTranslateCharacterMemory = ref('');
        const autoTranslateStatus = ref(null);
        const autoTranslateInProgress = ref(false);
        const autoTranslateProgress = ref(null);
        const autoTranslateLiveLines = ref([]);
        const autoTranslateControlBusy = ref(false);
        const apiSettingsBusy = ref(false);
        const apiSettingsError = transientRef('');
        const apiSettingsSuccess = transientRef('');
        const apiTranslationProvider = ref('gemini');
        const apiSectionsOpen = ref({
            paths: true,
            gamesPaths: false,
            tokutenPaths: false,
            ignoredPaths: false,
            workshop: true,
            theme: false,
            gemini: true,
            openrouter: false,
            chutes: false,
            openai_compat: false,
            translation: false,
            whisper: false,
            seiyuu: false,
            maintenance: false,
            ops: false,
        });

        // Seiyuu deduplication state
        const seiyuuInventory = ref([]);
        const seiyuuSuggestions = ref([]);
        const seiyuuLoading = ref(false);
        const seiyuuFilter = ref('');
        const seiyuuSelected = ref([]);
        const seiyuuCanonical = ref('');
        const seiyuuMergeBusy = ref('');
        const seiyuuMergeMessage = transientRef('', 5000);
        const seiyuuMergeError = transientRef('', 6000);
        const seiyuuMergePreview = ref(null);
        let _seiyuuLoaded = false;
        // Auto-load when the user first opens the seiyuu section so they
        // don't have to click Refresh to see the list.
        watch(() => apiSectionsOpen.value.seiyuu, (open) => {
            if (open && !_seiyuuLoaded) {
                _seiyuuLoaded = true;
                loadSeiyuuInventory();
                loadSeiyuuSuggestions();
            }
        });
        const filteredSeiyuuInventory = computed(() => {
            const q = seiyuuFilter.value.trim().toLowerCase();
            if (!q) return seiyuuInventory.value;
            return seiyuuInventory.value.filter(e =>
                e.name.toLowerCase().includes(q) ||
                (e.canonical_en && e.canonical_en.toLowerCase().includes(q)) ||
                (e.jp_names || []).some(jp => jp.toLowerCase().includes(q))
            );
        });

        function seiyuuJpNamesFor(enName) {
            const entry = seiyuuInventory.value.find(e => e.name === enName);
            return entry ? (entry.jp_names || []) : [];
        }

        // True iff every selected EN variant maps to the same set of JP names
        // (high-confidence "same person"). Used to render the warning when
        // the selected aliases don't actually share a JP source.
        const seiyuuSelectedJpConsistent = computed(() => {
            if (seiyuuSelected.value.length < 2) return true;
            const allJp = new Set();
            for (const name of seiyuuSelected.value) {
                for (const jp of seiyuuJpNamesFor(name)) {
                    allJp.add(jp);
                }
            }
            return allJp.size <= 1;
        });

        async function loadSeiyuuInventory() {
            seiyuuLoading.value = true;
            try {
                const resp = await fetch('/api/seiyuu/inventory');
                if (!resp.ok) throw new Error('failed');
                const data = await resp.json();
                seiyuuInventory.value = Array.isArray(data?.seiyuu) ? data.seiyuu : [];
            } catch (err) {
                seiyuuMergeError.value = 'Failed to load seiyuu inventory';
                console.warn('seiyuu inventory failed:', err);
            } finally {
                seiyuuLoading.value = false;
            }
        }

        async function loadSeiyuuSuggestions() {
            try {
                const resp = await fetch('/api/seiyuu/suggestions');
                if (!resp.ok) return;
                const data = await resp.json();
                seiyuuSuggestions.value = Array.isArray(data?.groups) ? data.groups : [];
            } catch (err) {
                console.warn('seiyuu suggestions failed:', err);
            }
        }

        // Use a suggestion group: pre-select all members and pick the one
        // with highest use_count as the default canonical.
        function useSeiyuuSuggestion(group) {
            if (!group || !group.members?.length) return;
            seiyuuSelected.value = group.members.map(m => m.name);
            // Pick highest-used as canonical default; user can change.
            seiyuuCanonical.value = group.members[0].name;
            seiyuuMergePreview.value = null;
            seiyuuMergeMessage.value = '';
            seiyuuMergeError.value = '';
        }

        async function _runSeiyuuMerge(dryRun) {
            if (!seiyuuCanonical.value) {
                seiyuuMergeError.value = 'Pick a canonical name first.';
                return;
            }
            const aliases = seiyuuSelected.value.filter(n => n !== seiyuuCanonical.value);
            if (!aliases.length) {
                seiyuuMergeError.value = 'Pick at least one alias different from the canonical name';
                return;
            }
            seiyuuMergeBusy.value = dryRun ? 'preview' : 'apply';
            seiyuuMergeMessage.value = '';
            seiyuuMergeError.value = '';
            try {
                const resp = await fetch('/api/seiyuu/merge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        canonical_en: seiyuuCanonical.value,
                        aliases,
                        dry_run: dryRun,
                    }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    seiyuuMergeError.value = data.detail || 'Merge failed';
                    return;
                }
                seiyuuMergePreview.value = data;
                if (dryRun) {
                    seiyuuMergeMessage.value = `Preview: ${data.items_changed} of ${data.items_touched} item(s) would change.`;
                } else {
                    seiyuuMergeMessage.value = `Merged ${aliases.length} alias(es) into "${seiyuuCanonical.value}". ${data.items_changed} item(s) updated.`;
                    seiyuuSelected.value = [];
                    seiyuuCanonical.value = '';
                    await loadSeiyuuInventory();
                    // Refresh the seiyuu filter dropdown elsewhere
                    if (typeof loadSeiyuuList === 'function') {
                        try { await loadSeiyuuList(); } catch (_) {}
                    }
                    // Suggestions also stale after a merge.
                    loadSeiyuuSuggestions();
                }
            } catch (err) {
                seiyuuMergeError.value = `Merge request failed: ${err.message || err}`;
                console.warn('seiyuu merge failed:', err);
            } finally {
                seiyuuMergeBusy.value = '';
            }
        }

        function previewSeiyuuMerge() { return _runSeiyuuMerge(true); }
        function applySeiyuuMerge() { return _runSeiyuuMerge(false); }

        // Backfill romanizations: fill seiyuu_en slots that are still JP copies
        // using names already romanized elsewhere in the library (exact match).
        const seiyuuBackfillBusy = ref('');
        const seiyuuBackfillPreview = ref(null);
        const seiyuuBackfillMessage = ref('');
        const seiyuuBackfillError = ref('');

        async function _runSeiyuuBackfill(dryRun) {
            seiyuuBackfillBusy.value = dryRun ? 'preview' : 'apply';
            seiyuuBackfillMessage.value = '';
            seiyuuBackfillError.value = '';
            try {
                const resp = await fetch('/api/seiyuu/backfill-romanizations?dry_run=' + (dryRun ? 'true' : 'false'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });
                const data = await resp.json();
                if (!resp.ok) {
                    seiyuuBackfillError.value = data.detail || 'Backfill failed';
                    return;
                }
                seiyuuBackfillPreview.value = data;
                if (dryRun) {
                    seiyuuBackfillMessage.value = data.items_changed
                        ? `Preview: ${data.names_filled} name(s) across ${data.items_changed} item(s) would be filled (from ${data.known_names} known).`
                        : `Nothing to fill — no JP-only names match the ${data.known_names} known romanization(s).`;
                } else {
                    seiyuuBackfillMessage.value = `Filled ${data.names_filled} name(s) across ${data.items_changed} item(s).`;
                    // Inventory + filter dropdown are stale after writing.
                    await loadSeiyuuInventory();
                    if (typeof loadSeiyuuList === 'function') {
                        try { await loadSeiyuuList(); } catch (_) {}
                    }
                    if (typeof loadItems === 'function') {
                        try { await loadItems(); } catch (_) {}
                    }
                }
            } catch (err) {
                seiyuuBackfillError.value = `Backfill request failed: ${err.message || err}`;
                console.warn('seiyuu backfill failed:', err);
            } finally {
                seiyuuBackfillBusy.value = '';
            }
        }

        function previewSeiyuuBackfill() { return _runSeiyuuBackfill(true); }
        function applySeiyuuBackfill() { return _runSeiyuuBackfill(false); }
        const apiGeminiModel = ref('gemini-2.0-flash');
        const apiGeminiKeyInput = ref('');
        const apiGeminiHasKey = ref(false);
        const apiGeminiKeySource = ref('env');
        const apiOpenRouterModel = ref('openrouter/auto');
        const apiOpenRouterKeyInput = ref('');
        const apiOpenRouterHasKey = ref(false);
        const apiOpenRouterKeySource = ref('env');
        const apiChutesModel = ref('deepseek-ai/DeepSeek-V3.1');
        const apiChutesKeyInput = ref('');
        const apiChutesHasKey = ref(false);
        const apiChutesKeySource = ref('env');
        const apiOpenAiCompatBaseUrl = ref('');
        const apiOpenAiCompatModel = ref('');
        const apiOpenAiCompatKeyInput = ref('');
        const apiOpenAiCompatHasKey = ref(false);
        const apiOpenAiCompatKeySource = ref('env');
        const apiOpenAiCompatBaseUrlSource = ref('env');
        const apiOpenAiCompatRequestFormat = ref('openai');
        const apiOpenAiCompatModelOptions = ref([]);
        const apiOpenAiCompatModelsBusy = ref(false);
        const apiOpenAiCompatModelsError = transientRef('');
        const SUPPORTED_PROVIDERS = ['gemini', 'openrouter', 'chutes', 'openai_compat'];
        const apiTestBusy = ref(false);
        const apiTestResult = transientRef('', 6000);  // longer — test results often have content worth reading
        const whisperSettings = ref({
            model: 'small',
            vad_filter: false,
            beam_size: 5,
            condition_on_previous_text: true,
            preferred_variant: 'sfx',
        });
        const whisperSupportedModels = ref([
            'tiny', 'base', 'small', 'medium', 'large-v1', 'large-v2', 'large-v3'
        ]);
        const whisperSettingsBusy = ref(false);
        const whisperSettingsError = transientRef('');
        const whisperSettingsSuccess = transientRef('');
        let transcriptionPollInterval = null;
        let autoTranslatePollInterval = null;
        let metadataTranslateTimer = null;

        // Player state
        const playerItemId = ref(null);
        const playerAvailableTracks = ref([]);
        const playerTrackId = ref(null);
        const playerTrackTitle = ref('');
        const playerTrackDuration = ref('');
        const playerIsPlaying = ref(false);
        const playerCurrentTime = ref(0);
        const playerDuration = ref(0);
        const playerProgressPercent = ref(0);
        const playerTranscriptSegments = ref([]);
        const playerTranslationSegments = ref([]);
        const playerTranscriptRunId = ref(null);
        const playerTranslationRunId = ref(null);
        const playerTranscriptRuns = ref([]);   // all transcript runs for the current player track (for the switcher)
        const playerActiveSegmentIndex = ref(-1);
        // Player-only safety: pencil icons stay hidden until the user opts in.
        // Prevents fat-fingering an edit while watching. Persists across reloads.
        const playerEditMode = ref(localStorage.getItem('player_edit_mode') === '1');
        // Glossary textarea collapse state inside the Workshop translate card.
        // Default collapsed since most translate runs don't need a glossary.
        const glossaryExpanded = ref(localStorage.getItem('glossaryExpanded') === '1');
        const playerFollowTranscript = ref(true);
        const playerLastUserScrollTime = ref(0);
        const playerAudioElement = ref(null);

        // Volume + mute — PC-only UI. iOS Safari ignores audio.volume reads
        // and writes (system override), and Android phones have hardware
        // volume buttons that do the same job, so the slider is redundant
        // there. We still track the values so any future preset doesn't
        // start from zero.
        const _storedVolume = parseFloat(localStorage.getItem('player_volume'));
        const playerVolume = ref(
            Number.isFinite(_storedVolume) && _storedVolume >= 0 && _storedVolume <= 1 ? _storedVolume : 1
        );
        const playerMuted = ref(localStorage.getItem('player_muted') === '1');
        const playerShowVolumeControl = ref(!isMobileClient());
        watch(playerVolume, (v) => {
            const clamped = Math.max(0, Math.min(1, Number(v) || 0));
            localStorage.setItem('player_volume', String(clamped));
            if (playerAudioElement.value) playerAudioElement.value.volume = clamped;
        });
        watch(playerMuted, (m) => {
            localStorage.setItem('player_muted', m ? '1' : '0');
            if (playerAudioElement.value) playerAudioElement.value.muted = !!m;
        });
        function toggleMute() {
            playerMuted.value = !playerMuted.value;
        }
        // Apply persisted values when the <audio> element first mounts —
        // Vue assigns the ref after the element renders, so we watch the
        // ref itself rather than calling sync in onMounted (the audio is
        // only mounted when the player tab is visible).
        function _syncPlayerAudioVolume() {
            if (!playerAudioElement.value) return;
            playerAudioElement.value.volume = Math.max(0, Math.min(1, Number(playerVolume.value) || 0));
            playerAudioElement.value.muted = !!playerMuted.value;
        }
        watch(playerAudioElement, (el) => {
            if (el) _syncPlayerAudioVolume();
        });
        const transcriptList = ref(null);
        const transcriptScroll = ref(null);
        const playerManualSeekInProgress = ref(false);
        const itemHasPlayableContent = ref(false);
        const checkingPlayableContent = ref(false);
        const playerTheme = ref('starlit');


        // Persist scan state to localStorage
        watch(scanRunning, (value) => {
            localStorage.setItem('scanRunning', JSON.stringify(value));
        });

        watch(() => scanProgress, (value) => {
            localStorage.setItem('scanProgress', JSON.stringify(value));
        }, { deep: true });

        // Persist fetch state to localStorage
        watch(fetchRunning, (value) => {
            localStorage.setItem('fetchRunning', JSON.stringify(value));
        });

        watch(() => fetchProgress, (value) => {
            localStorage.setItem('fetchProgress', JSON.stringify(value));
        }, { deep: true });

        // Glossary is per-item now (stored in items.glossary). The watcher
        // below debounces writes back to the server; an in-flight load suppresses
        // the watcher via _glossaryLoading so hydrating the field doesn't
        // immediately PUT it back.
        const _glossaryLoading = ref(false);
        const _glossaryItemId = ref(null);
        let _glossarySaveTimer = null;
        watch(autoTranslateGlossary, (value) => {
            if (_glossaryLoading.value) return;
            const itemId = _glossaryItemId.value;
            if (!itemId) return;
            if (_glossarySaveTimer) clearTimeout(_glossarySaveTimer);
            _glossarySaveTimer = setTimeout(() => {
                _glossarySaveTimer = null;
                fetch(`/api/items/${itemId}/glossary`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ glossary: String(value || '') }),
                }).catch((err) => console.warn('Glossary save failed:', err));
            }, 600);
        });

        watch(autoTranslateCharacterMemory, (value) => {
            localStorage.setItem('autoTranslateCharacterMemory', String(value || ''));
        });
        // Player scroll
        // Lyric-stage line class: returns the visual tier based on distance
        // from the active segment. Drives the Spotify-style fade.
        function lyricLineClass(idx) {
            const active = playerActiveSegmentIndex.value;
            if (active < 0) return 'distant';
            const d = idx - active;
            if (d === 0) return 'active';
            const ad = Math.abs(d);
            if (ad === 1) return 'near';
            if (ad === 2) return 'far';
            return 'distant';
        }

        function scrollActiveTranscriptIntoView(idx) {
            const wrapper = document.querySelector('.player-scroll-wrapper');
            if (!wrapper) return;

            const card = wrapper.querySelector(`[data-seg-idx="${idx}"]`);
            if (!card) return;

            const wrapperRect = wrapper.getBoundingClientRect();
            const cardRect = card.getBoundingClientRect();

            const offset = cardRect.top - wrapperRect.top - (wrapperRect.height / 2) + (cardRect.height / 2);

            wrapper.scrollBy({
                top: offset,
                behavior: 'smooth'
            });
    }
        // Player tab auto-load removed — track loading is now explicit only.
        // Player auto-scroll watcher
        watch(playerActiveSegmentIndex, (newIndex) => {
            if (!playerFollowTranscript.value) return;

            nextTick(() => {
                scrollActiveTranscriptIntoView(newIndex);
            });
        });

        // Auto-load transcript/translation runs whenever the user picks a track
        // in the Track Selection list. Replaces the old "Load Runs for Track #N"
        // button — the row click is the action.
        watch(pipelineTrackId, (newId, oldId) => {
            if (!newId || newId === oldId) return;
            // Don't fetch if we're still loading something else; loadPipelineRuns
            // already gates on pipelineBusy and will report nicely.
            loadPipelineRuns().catch((err) => console.warn('auto-load runs failed:', err));
        });

        // Computed
        const hasActiveFilters = computed(() =>
            searchQuery.value || filters.seiyuu.length > 0 || filters.tag.length > 0 ||
            filters.translation_status || filters.listen_status || filters.favorite
        );
        // Selection helpers — subtab-aware. The bulk-actions bar reads
        // these via the same name across all three subtabs, so a count
        // shown in the toolbar always reflects whatever is selected in
        // the active grid. Drama CDs + tokutens share `selectedIds`
        // (both pull from items.value); games has its own `selectedGameIds`.
        const selectedCount = computed(() => (
            librarySubtab.value === 'games'
                ? selectedGameIds.value.size
                : selectedIds.value.size
        ));
        const allVisibleSelected = computed(() => {
            if (librarySubtab.value === 'games') {
                return gamesItems.value.length > 0
                    && gamesItems.value.every(g => selectedGameIds.value.has(g.id));
            }
            return items.value.length > 0
                && items.value.every(i => selectedIds.value.has(i.id));
        });

        const scanPercent = computed(() =>
            scanProgress.total > 0
                ? Math.round((scanProgress.processed / scanProgress.total) * 100)
                : 0
        );

        const fetchPercent = computed(() =>
            fetchProgress.total > 0
                ? Math.round((fetchProgress.completed / fetchProgress.total) * 100)
                : 0
        );


        const failureReasonEntries = computed(() => {
            const summary = lastFetchSummary.value;
            if (!summary || !summary.error_summary) return [];
            return Object.entries(summary.error_summary).sort((a, b) => b[1] - a[1]);
        });
        const filteredPipelineTracks = computed(() => {
            const filter = trackCodecFilter.value;
            const tracks = pipelineTracks.value || [];
            if (filter === 'all') return tracks;

            return tracks.filter(track => {
                const codec = (track.codec || '').toLowerCase();
                if (filter === 'mp3') return codec === 'mp3';
                if (filter === 'wav') return codec === 'wav' || codec.startsWith('pcm_'); // Match all PCM variants (pcm_s16le, pcm_s24le, etc.)
                if (filter === 'other') {
                    return codec !== 'mp3' && codec !== 'wav' && !codec.startsWith('pcm_');
                }
                return true;
            });
        });

        // Tracks with at least one transcript run — drives the Track Selection
        // card so you only see tracks worth managing runs for.
        const transcribedPipelineTracks = computed(() =>
            (pipelineTracks.value || []).filter(t => Number(t.transcript_run_count || 0) > 0)
        );

        // === Track grouping ============================================================
        // Mirror of the backend logic in database.py. FLAC+MP3 of the same audio AND
        // SFX/no-SFX variants of the same recording all collapse into one group.
        const CODEC_RANK = { flac: 0, wav: 1, aiff: 2, aif: 2, alac: 3, m4a: 4, ogg: 5, opus: 6, mp3: 7, aac: 8 };
        const VARIANT_RANK_SFX_FIRST = { 'sfx': 0, 'no-sfx': 1 };
        const VARIANT_RANK_NOSFX_FIRST = { 'no-sfx': 0, 'sfx': 1 };
        const DURATION_TOL = 2.0; // Allows MP3 encoder padding / silent-frame skew
        // Alt-mix variant tokens — matched both at the end of a filename stem
        // and as standalone components in any ancestor folder name.
        const VARIANT_TOKEN = String.raw`no[\s_\-]?se|no[\s_\-]?sfx|no[\s_\-]?effects?|se[\s_\-]?less|se[\s_\-]?off|voice[\s_\-]?only|no[\s_\-]?vocal|no[\s_\-]?bgm|bgm[\s_\-]?less`;
        const VARIANT_SUFFIX_RE = new RegExp(`^(.+?)[\\s_\\-.]?(${VARIANT_TOKEN})$`, 'i');
        const VARIANT_FOLDER_RE = new RegExp(`(^|[\\s_\\-.])(${VARIANT_TOKEN})($|[\\s_\\-.])`, 'i');
        // Japanese variant markers (drama-CD folder names like `03_wav（SE無し）`).
        // Plain substring match — CJK has no word-spaces, tokens are distinctive.
        const VARIANT_FOLDER_JP_RE = /SE[\s ]?無し|SE[\s ]?なし|SE[\s ]?抜き|効果音[\s ]?無し|効果音[\s ]?なし|声のみ|ボイスのみ|BGM[\s ]?無し|BGM[\s ]?なし/i;

        function _filenameStem(track) {
            const path = String(track.track_path || '').replace(/\\/g, '/');
            const name = path.split('/').pop() || '';
            const stem = name.replace(/\.[^.]+$/, '').trim().toLowerCase();
            return stem || `track-${track.id}`;
        }
        function _ancestorFolders(track) {
            const path = String(track.track_path || '').replace(/\\/g, '/');
            const parts = path.split('/').filter(p => p.trim()).map(p => p.trim().toLowerCase());
            return parts.length > 1 ? parts.slice(0, -1) : [];
        }
        function _areLikelySiblings(a, b) {
            // Mirror of Python _are_likely_siblings. Structural test, no regex.
            // Siblings if any of:
            //   1. LCP spans the entire shorter stem
            //   2. LCP ends on a non-alphanumeric character
            //   3. The character right after the LCP (in either stem) is non-alphanumeric
            a = (a || '').trim().toLowerCase();
            b = (b || '').trim().toLowerCase();
            if (!a || !b) return false;
            if (a === b) return true;
            let lcp = 0;
            while (lcp < a.length && lcp < b.length && a[lcp] === b[lcp]) lcp++;
            if (lcp === 0) return false;
            const isAlnum = c => /[a-z0-9]/.test(c);
            const shortLen = Math.min(a.length, b.length);
            // Rule 1: full-prefix match — always siblings.
            if (lcp === shortLen) return true;
            // Rules 2 & 3 need >= 50% overlap so a shared category prefix like
            // '【特典】' between unrelated tracks doesn't trigger a false merge.
            if (lcp * 2 < shortLen) return false;
            // Rule 2: LCP ends on a non-alphanumeric token boundary.
            if (!isAlnum(a[lcp - 1])) return true;
            // Rule 3: char right after the LCP is non-alphanumeric.
            const nextA = lcp < a.length ? a[lcp] : '';
            const nextB = lcp < b.length ? b[lcp] : '';
            if (nextA && !isAlnum(nextA)) return true;
            if (nextB && !isAlnum(nextB)) return true;
            return false;
        }
        function _codecRank(c) {
            return CODEC_RANK[String(c || '').toLowerCase()] ?? 99;
        }
        function _variantRank(v, preferred) {
            const table = preferred === 'no-sfx' ? VARIANT_RANK_NOSFX_FIRST : VARIANT_RANK_SFX_FIRST;
            return table[v] ?? 99;
        }

        function _groupTracks(tracks, preferredVariant = 'sfx') {
            // Step 1: stem-based sibling clustering via union-find. Duration is
            // NOT a gate — no-SFX mixes commonly drop SFX-only segments and end
            // up 5-15s shorter than the SFX version. Stem similarity is the
            // primary signal; union-find transitively closes the relation so
            // all reachable siblings end up in one cluster.
            const n = tracks.length;
            const parent = Array.from({ length: n }, (_, i) => i);
            const find = (x) => {
                while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; }
                return x;
            };
            const union = (x, y) => {
                const rx = find(x), ry = find(y);
                if (rx !== ry) parent[rx] = ry;
            };
            const stems = tracks.map(t => _filenameStem(t));
            for (let i = 0; i < n; i++) {
                for (let j = i + 1; j < n; j++) {
                    if (_areLikelySiblings(stems[i], stems[j])) union(i, j);
                }
            }
            const byRoot = new Map();
            for (let i = 0; i < n; i++) {
                const r = find(i);
                if (!byRoot.has(r)) byRoot.set(r, []);
                byRoot.get(r).push(tracks[i]);
            }
            let subClusters = Array.from(byRoot.values());

            // Step 2b: split each stem-cluster into duration buckets (mirrors
            // backend). Without this, tracks that share a stem prefix but are
            // wildly different recordings (e.g. main track + freetalk + bonus
            // all named "RJ12345*") collapse into a single group and only the
            // preferred one ever appears in the transcription list.
            const bucketed = [];
            for (const sub of subClusters) {
                const buckets = []; // [{ d: number|null, items: track[] }]
                for (const t of sub) {
                    const d = t.duration_seconds;
                    let placed = false;
                    if (typeof d === 'number') {
                        for (const b of buckets) {
                            if (typeof b.d === 'number' && Math.abs(b.d - d) <= DURATION_TOL) {
                                b.items.push(t);
                                placed = true;
                                break;
                            }
                        }
                    }
                    if (!placed) buckets.push({ d, items: [t] });
                }
                for (const b of buckets) bucketed.push(b.items);
            }
            subClusters = bucketed;

            // Step 3: label variants per sub-cluster (regex is now COSMETIC —
            // it decides pill text, not grouping).
            const groups = [];
            for (const sub of subClusters) {
                const stems = sub.map(t => _filenameStem(t));
                for (const t of sub) {
                    const stem = _filenameStem(t);
                    const folders = _ancestorFolders(t);
                    // No-SFX if any signal hits: Latin suffix, JP token in stem,
                    // or Latin/JP token in any ancestor folder.
                    const stemMatch = VARIANT_SUFFIX_RE.test(stem) || VARIANT_FOLDER_JP_RE.test(stem);
                    const folderMatch = folders.some(f => VARIANT_FOLDER_RE.test(f) || VARIANT_FOLDER_JP_RE.test(f));
                    t._variant = (stemMatch || folderMatch) ? 'no-sfx' : 'sfx';
                }
                const canonical = stems.reduce((a, b) => a.length <= b.length ? a : b);
                for (const t of sub) t._canonical_stem = canonical;

                const sorted = [...sub].sort((a, b) =>
                    _variantRank(a._variant, preferredVariant) - _variantRank(b._variant, preferredVariant)
                    || _codecRank(a.codec) - _codecRank(b.codec)
                    || (a.id || 0) - (b.id || 0)
                );
                const preferred = sorted[0];
                const codecs = [], variants = [], seenC = new Set(), seenV = new Set();
                for (const tr of sorted) {
                    const c = String(tr.codec || '?').toUpperCase();
                    if (!seenC.has(c)) { codecs.push(c); seenC.add(c); }
                    const v = tr._variant || 'sfx';
                    if (!seenV.has(v)) { variants.push(v); seenV.add(v); }
                }
                groups.push({
                    group_key: canonical,
                    preferred_track_id: preferred.id,
                    tracks: sorted,
                    title: preferred.title || preferred.track_path?.split(/[\\/]/).pop() || '',
                    title_en: preferred.title_en || '',
                    codecs,
                    variants,
                    duration_seconds: preferred.duration_seconds,
                    track_path: preferred.track_path,
                    // On-disk presence of the playable (preferred) file.
                    // `!== false` so older responses without the field
                    // default to "present" instead of flagging everything.
                    file_exists: preferred.file_exists !== false,
                    transcript_run_count: sub.reduce((s, t) => s + Number(t.transcript_run_count || 0), 0),
                    translation_run_count: sub.reduce((s, t) => s + Number(t.translation_run_count || 0), 0),
                    min_track_index: Math.min(...sub.map(t => Number(t.track_index || 0))),
                });
            }
            groups.sort((a, b) => (a.min_track_index - b.min_track_index) || (a.preferred_track_id - b.preferred_track_id));
            return groups;
        }

        const pipelineTrackGroups = computed(() => _groupTracks(filteredPipelineTracks.value || [], whisperSettings.value.preferred_variant));
        const transcribedTrackGroups = computed(() => _groupTracks(transcribedPipelineTracks.value || [], whisperSettings.value.preferred_variant));
        // Player picker: the available-tracks list grouped by recording so each
        // CD track shows once with codec + variant pickers instead of an entry
        // per file.
        const playerAvailableGroups = computed(() => _groupTracks(playerAvailableTracks.value || [], whisperSettings.value.preferred_variant));
        // Groups whose audio file is gone from disk (DB row survived a
        // workspace cleanup). Drives the "re-extract" banner + row badges.
        const playerMissingAudioCount = computed(() =>
            (playerAvailableGroups.value || []).filter(g => g.file_exists === false).length
        );
        // Atelier CD-card dot: 'present' = all extracted audio on disk,
        // 'missing' = tracks indexed but files gone, 'none' = never extracted.
        const workshopAudioState = computed(() => {
            const tracks = pipelineTracks.value || [];
            if (!tracks.length) return 'none';
            return tracks.every(t => t.file_exists !== false) ? 'present' : 'missing';
        });
        const workshopAudioDotTitle = computed(() => {
            const state = workshopAudioState.value;
            if (state === 'present') return 'Audio extracted';
            if (state === 'missing') return 'Audio files missing on disk — re-extract from Archive';
            return 'Audio not extracted';
        });
        // The group currently loaded in the player (or null) — drives the
        // codec/variant pickers in the header bar above the lyrics.
        const playerActiveGroup = computed(() => {
            const id = playerTrackId.value;
            if (!id) return null;
            for (const g of playerAvailableGroups.value) {
                if (g.tracks.some(t => t.id === id)) return g;
            }
            return null;
        });

        // Selection helpers operating on groups (toggling adds/removes the
        // preferred-codec track id; sibling tracks ride along via DB replication).
        function isGroupSelected(group) {
            return group.tracks.some(t => selectedTracksForTranscription.value.includes(t.id));
        }
        function toggleGroupSelection(group) {
            const ids = new Set(selectedTracksForTranscription.value);
            const groupIds = group.tracks.map(t => t.id);
            const anySelected = groupIds.some(id => ids.has(id));
            if (anySelected) {
                for (const id of groupIds) ids.delete(id);
            } else {
                ids.add(group.preferred_track_id);
            }
            selectedTracksForTranscription.value = Array.from(ids);
        }

        // Helpers
        function parseJson(val) {
            if (!val) return [];
            if (Array.isArray(val)) return val;
            try { return JSON.parse(val); }
            catch { return []; }
        }

        function coverUrl(item) {
            if (!item.cover_local) return '';
            // Add timestamp query parameter to bust browser cache when cover changes
            return '/covers/' + item.cover_local.replace(/^data[\\/]covers[\\/]/, '') + '?v=' + (item.updated_at || '');
        }

        function dlsiteUrl(code) {
            const prefix = code.substring(0, 2).toUpperCase();
            let section = 'maniax';
            if (prefix === 'BJ') section = 'comic';
            else if (prefix === 'VJ') section = 'pro';
            return `https://www.dlsite.com/${section}/work/=/product_id/${code}.html`;
        }
        // True for product codes that look like a real DLsite work id —
        // RJ/BJ/VJ followed by digits. Manual / tokuten synthetic codes
        // (MAN-, TKT-, TKS-) get a `false` here so we don't render a
        // broken "View on DLsite" link for them.
        function isDlsiteCode(code) {
            return typeof code === 'string' && /^(RJ|BJ|VJ)\d+$/i.test(code);
        }
        // VGMdb search URL — preferred over a full API integration. Game
        // audio / tokuten coverage is solid enough that a link-out gives
        // the user the catalog without the scraper / Cloudflare maintenance.
        function vgmdbSearchUrl(query) {
            const q = String(query || '').trim();
            if (!q) return 'https://vgmdb.net/';
            return 'https://vgmdb.net/search?q=' + encodeURIComponent(q);
        }

        // === Platform icons ===
        // Maps VNDB platform codes → human-readable label + inline SVG path.
        // Keeping the SVG markup short so it can be dropped via v-html on a
        // small pill without bloating the page. Unknown codes fall back to
        // the raw code text rendered in the pill.
        const PLATFORM_LABELS = {
            win: 'Windows', mac: 'macOS', lin: 'Linux', web: 'Browser',
            ios: 'iOS', and: 'Android',
            swi: 'Switch', wii: 'Wii', wiu: 'Wii U',
            nds: 'DS', n3d: '3DS', gba: 'GBA', gbc: 'GBC', gb: 'GameBoy',
            nes: 'NES', snes: 'SNES',
            ps1: 'PS1', ps2: 'PS2', ps3: 'PS3', ps4: 'PS4', ps5: 'PS5',
            psp: 'PSP', psv: 'PS Vita',
            xbo: 'Xbox', xb3: 'Xbox 360', xb1: 'Xbox One', xbs: 'Xbox Series',
            drc: 'Dreamcast', sat: 'Saturn', smd: 'Genesis',
            pc8: 'PC-88', pc9: 'PC-98', x68: 'X68000', fmt: 'FM Towns',
        };
        // SVG path data (inside a 24x24 viewBox). Each entry's `paths` is
        // concatenated into a single <svg> wrapper at render time.
        const PLATFORM_ICONS = {
            win:  '<rect x="3" y="4" width="8" height="7"/><rect x="13" y="3" width="8" height="8"/><rect x="3" y="13" width="8" height="7"/><rect x="13" y="13" width="8" height="8"/>',
            mac:  '<path d="M12 22c1.3 0 2.3-1 3.5-1 2.5 0 5-7 5-11.5 0-2.5-2-4-4.5-4-1.7 0-3 1.2-4 1.7C11 6.7 9.7 5.5 8 5.5 5.5 5.5 3.5 7 3.5 9.5 3.5 14 6 21 8.5 21c1.2 0 2.2 1 3.5 1z" fill="currentColor" stroke="none"/><path d="M11 3c.7.7 1.3 1.5 1.3 3"/>',
            lin:  '<ellipse cx="12" cy="14" rx="6" ry="7"/><circle cx="10" cy="10" r="0.8" fill="currentColor"/><circle cx="14" cy="10" r="0.8" fill="currentColor"/><path d="M10 13c1 1 3 1 4 0"/>',
            web:  '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/>',
            ios:  '<rect x="7" y="2" width="10" height="20" rx="2"/><circle cx="12" cy="18.5" r="0.7" fill="currentColor"/>',
            and:  '<rect x="7" y="2" width="10" height="20" rx="2"/><line x1="10" y1="18.5" x2="14" y2="18.5"/>',
            swi:  '<rect x="3" y="3" width="7" height="18" rx="2"/><rect x="14" y="3" width="7" height="18" rx="2"/><circle cx="6.5" cy="9" r="1" fill="currentColor"/><circle cx="17.5" cy="15" r="1" fill="currentColor"/>',
            wii:  '<rect x="9" y="3" width="6" height="18" rx="1"/>',
            wiu:  '<rect x="3" y="6" width="18" height="12" rx="1"/><circle cx="8" cy="12" r="2"/>',
            nds:  '<rect x="3" y="6" width="18" height="12" rx="1"/><line x1="3" y1="12" x2="21" y2="12"/>',
            n3d:  '<rect x="3" y="6" width="18" height="12" rx="1"/><line x1="3" y1="12" x2="21" y2="12"/><circle cx="12" cy="9" r="1.2" fill="currentColor"/>',
            gba:  '<rect x="3" y="7" width="18" height="10" rx="2"/><rect x="6" y="9" width="6" height="6"/>',
            gbc:  '<rect x="5" y="3" width="14" height="18" rx="2"/><rect x="7" y="5" width="10" height="8"/><circle cx="9" cy="17" r="1" fill="currentColor"/><circle cx="15" cy="17" r="1" fill="currentColor"/>',
            gb:   '<rect x="5" y="3" width="14" height="18" rx="2"/><rect x="7" y="5" width="10" height="8"/>',
            nes:  '<rect x="3" y="8" width="18" height="9" rx="1"/><line x1="7" y1="11" x2="7" y2="14"/><line x1="9" y1="11" x2="9" y2="14"/>',
            snes: '<rect x="3" y="8" width="18" height="9" rx="3"/><circle cx="17" cy="12.5" r="1" fill="currentColor"/><circle cx="14" cy="12.5" r="1" fill="currentColor"/>',
            ps1:  '<rect x="3" y="8" width="18" height="9" rx="4"/><text x="12" y="14.5" text-anchor="middle" font-size="6" fill="currentColor" stroke="none">PS</text>',
            ps2:  '<rect x="3" y="8" width="18" height="9" rx="4"/><text x="12" y="14.5" text-anchor="middle" font-size="6" fill="currentColor" stroke="none">PS2</text>',
            ps3:  '<rect x="3" y="8" width="18" height="9" rx="4"/><text x="12" y="14.5" text-anchor="middle" font-size="6" fill="currentColor" stroke="none">PS3</text>',
            ps4:  '<rect x="3" y="8" width="18" height="9" rx="4"/><text x="12" y="14.5" text-anchor="middle" font-size="6" fill="currentColor" stroke="none">PS4</text>',
            ps5:  '<rect x="3" y="8" width="18" height="9" rx="4"/><text x="12" y="14.5" text-anchor="middle" font-size="6" fill="currentColor" stroke="none">PS5</text>',
            psp:  '<rect x="3" y="7" width="18" height="10" rx="2"/><rect x="6" y="9" width="12" height="6"/>',
            psv:  '<rect x="3" y="7" width="18" height="10" rx="2"/><rect x="6" y="9" width="12" height="6"/><circle cx="5" cy="12" r="0.8" fill="currentColor"/><circle cx="19" cy="12" r="0.8" fill="currentColor"/>',
            xbo:  '<circle cx="12" cy="12" r="9"/><path d="M8 8l8 8M16 8l-8 8"/>',
            xb3:  '<circle cx="12" cy="12" r="9"/><path d="M8 8l8 8M16 8l-8 8"/>',
            xb1:  '<circle cx="12" cy="12" r="9"/><path d="M8 8l8 8M16 8l-8 8"/>',
            xbs:  '<circle cx="12" cy="12" r="9"/><path d="M8 8l8 8M16 8l-8 8"/>',
            drc:  '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3" fill="currentColor"/>',
            sat:  '<rect x="3" y="8" width="18" height="9" rx="1"/><circle cx="18" cy="12.5" r="1" fill="currentColor"/>',
            smd:  '<rect x="3" y="8" width="18" height="9" rx="1"/><line x1="7" y1="11" x2="7" y2="14"/>',
        };
        function platformLabel(code) {
            const c = String(code || '').toLowerCase();
            return PLATFORM_LABELS[c] || code;
        }
        function platformIconSvg(code) {
            const c = String(code || '').toLowerCase();
            const paths = PLATFORM_ICONS[c];
            if (!paths) return '';
            return '<svg class="platform-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
        }
        function hasPlatformIcon(code) {
            return !!PLATFORM_ICONS[String(code || '').toLowerCase()];
        }

        // Pretty-print a release_date that DLsite returns as
         // "2022-11-25 00:00:00" — stripping the always-zero time and
         // rendering the date in the user's locale.
        function formatReleaseDate(raw) {
            if (!raw) return '';
            const s = String(raw).trim();
            // Handle both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS".
            const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
            if (!m) return s;
            const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
            if (Number.isNaN(d.getTime())) return s;
            return d.toLocaleDateString(undefined, {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
            });
        }

        // Toggle a single seiyuu name's display between EN and JP. Index-keyed
        // so each entry in a list flips independently. Reassigning the Set
        // is what triggers Vue's reactivity — mutating in place wouldn't.
        function toggleSeiyuuFlip(itemId, idx) {
            const key = `${itemId}::${idx}`;
            const next = new Set(seiyuuFlippedSet.value);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            seiyuuFlippedSet.value = next;
        }

        // Show the Play pill the moment audio has been extracted; the
        // Player itself handles items without transcripts/translations
        // (just plays audio without lyrics). No extra API round-trip
        // needed — translation_status already encodes "audio exists".
        function itemIsPlayable(item) {
            if (!item) return false;
            return ['extracted', 'transcribed', 'translated'].includes(item.translation_status);
        }

        function statusLabel(status) {
            const labels = {
                'not_translated': 'Not Translated',
                'extracted': 'Extracted',
                'transcribed': 'Transcribed',
                'translated': 'Translated',
            };
            return labels[status] || status;
        }

        function confidenceLabel(confidence) {
            const labels = {
                'high': 'High confidence',
                'low': 'Low confidence',
                'verified': 'Verified',
            };
            return labels[confidence] || 'Unknown';
        }

        function confidenceIcon(confidence) {
            const icons = { 'high': '', 'low': '?', 'verified': '\u2713' };
            return icons[confidence] || '?';
        }


        function displayTitle(item) {
            if (!item) return '';
            if (currentLang.value === 'en') {
                return item.title_en || item.title || item.title_display || '';
            }
            return item.title || item.title_en || item.title_display || '';
        }

        function displaySecondaryTitle(item) {
            if (!item) return '';
            if (currentLang.value === 'en') {
                if (item.title && item.title !== displayTitle(item)) return item.title;
                return '';
            }
            if (item.title_en && item.title_en !== displayTitle(item)) return item.title_en;
            return '';
        }

        function displaySeiyuu(item) {
            if (!item) return [];
            const jp = parseJson(item.seiyuu);
            const en = parseJson(item.seiyuu_en);
            if (currentLang.value === 'en') {
                return en.length ? en : jp;
            }
            return jp.length ? jp : en;
        }

        // Language-aware title for game rows. The VNDB schema gives us three
        // candidates: `title` (display title, typically romanized), `title_jp`
        // (original-script), and `title_en` (explicit English entry, often
        // null when no separate EN release exists). Picks the most relevant
        // for the active currentLang and falls through gracefully.
        function displayGameTitle(game) {
            if (!game) return '';
            if (currentLang.value === 'en') {
                return game.title_en || game.title || game.title_jp || '';
            }
            return game.title_jp || game.title || game.title_en || '';
        }
        // The "other" side — used on the detail panel to show the alt-language
        // title underneath the primary. Returns '' when the secondary is
        // missing or identical to the primary (avoids duplicated lines for
        // VNs where title == title_en).
        function displaySecondaryGameTitle(game) {
            if (!game) return '';
            const primary = displayGameTitle(game);
            const candidate = currentLang.value === 'en'
                ? game.title_jp
                : (game.title_en || game.title);
            return candidate && candidate !== primary ? candidate : '';
        }

        // Tooltip lines for the library card in cover-only view. Returns ''
        // (no tooltip) when not in cover mode. The v-app-tooltip directive
        // splits on '\n' to render each line as its own row.
        function cardTooltip(item) {
            if (!item) return '';
            if (libraryViewMode.value !== 'cover') return '';
            const lines = [];
            const t = displayTitle(item) || item.product_code;
            if (t) lines.push(t);
            if (item.circle) lines.push(item.circle);
            if (item.product_code && item.product_code !== t) lines.push(item.product_code);
            return lines.join('\n');
        }

        // Per-position seiyuu name with EN/JP flip support for the detail
        // panel. Falls back to the available language if the requested side
        // is empty. Click toggles via toggleSeiyuuFlip(itemId, idx).
        function displaySeiyuuAt(item, idx) {
            if (!item) return '';
            const jp = parseJson(item.seiyuu);
            const en = parseJson(item.seiyuu_en);
            const flipped = seiyuuFlippedSet.value.has(`${item.id}::${idx}`);
            // Default side follows the global lang setting; flip swaps it.
            const defaultIsEn = currentLang.value === 'en';
            const wantEn = flipped ? !defaultIsEn : defaultIsEn;
            const primary = wantEn ? en : jp;
            const fallback = wantEn ? jp : en;
            return primary[idx] || fallback[idx] || '';
        }

        // For the click hint / tooltip — what would the toggle reveal?
        function alternateSeiyuuAt(item, idx) {
            if (!item) return '';
            const jp = parseJson(item.seiyuu);
            const en = parseJson(item.seiyuu_en);
            const flipped = seiyuuFlippedSet.value.has(`${item.id}::${idx}`);
            const defaultIsEn = currentLang.value === 'en';
            const wantEn = flipped ? !defaultIsEn : defaultIsEn;
            // The "other" side
            const other = wantEn ? jp[idx] : en[idx];
            return other || '';
        }

        function displayTags(item) {
            if (!item) return [];
            const jp = parseJson(item.tags);
            const en = parseJson(item.tags_en);
            if (currentLang.value === 'en') {
                return en.length ? en : jp;
            }
            return jp.length ? jp : en;
        }

        // === BBcode renderer ===
        // VNDB descriptions arrive with BBcode markup ([url], [b], [i],
        // [spoiler]). Parse them into safe HTML — text is HTML-escaped first,
        // then the supported tags are re-introduced. The output is mounted
        // via v-html on description fields. Spoilers are click-to-reveal via
        // an inline class toggle (kept inline since the rendered HTML
        // doesn't get Vue event bindings).
        function _bbcodeEscapeHtml(s) {
            return String(s || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }
        function _bbcodeSafeHref(href) {
            // The href was already HTML-escaped, so we just need to keep
            // out non-http(s) schemes (javascript:, data:, vbscript:). VNDB
            // descriptions only ever link to web URLs.
            const trimmed = String(href || '').trim();
            return /^(?:https?:\/\/|\/|#)/i.test(trimmed) ? trimmed : '#';
        }
        function renderBBcode(text) {
            if (!text) return '';
            let s = _bbcodeEscapeHtml(text);
            // [url=href]text[/url] — link with display text.
            s = s.replace(/\[url=([^\]]+)\]([\s\S]*?)\[\/url\]/gi, (_m, href, inner) =>
                `<a href="${_bbcodeSafeHref(href)}" target="_blank" rel="noopener noreferrer">${inner}</a>`
            );
            // [url]href[/url] — bare link.
            s = s.replace(/\[url\]([\s\S]*?)\[\/url\]/gi, (_m, href) =>
                `<a href="${_bbcodeSafeHref(href)}" target="_blank" rel="noopener noreferrer">${href}</a>`
            );
            s = s.replace(/\[b\]([\s\S]*?)\[\/b\]/gi, '<strong>$1</strong>');
            s = s.replace(/\[i\]([\s\S]*?)\[\/i\]/gi, '<em>$1</em>');
            s = s.replace(
                /\[spoiler\]([\s\S]*?)\[\/spoiler\]/gi,
                '<span class="bbcode-spoiler" onclick="this.classList.toggle(\'bbcode-spoiler-revealed\')">$1</span>'
            );
            // Bare URLs the user pasted without [url] wrapping — link them too.
            s = s.replace(
                /(^|[\s(])((?:https?:\/\/)[^\s<>"']+)/gi,
                (m, prefix, url) => `${prefix}<a href="${_bbcodeSafeHref(url)}" target="_blank" rel="noopener noreferrer">${url}</a>`
            );
            s = s.replace(/\n/g, '<br>');
            return s;
        }

        function displayDescription(item) {
            if (!item) return '';
            if (currentLang.value === 'en') {
                return item.description_en || item.description || '';
            }
            return item.description || item.description_en || '';
        }

        // API calls
        async function loadItems(append = false) {
            // Vue @change handlers pass the event object by default.
            // Only boolean true should trigger append mode ("Load More").
            const isAppend = append === true;

            if (!isAppend) {
                loading.value = true;
                currentOffset.value = 0;
            }

            // Games live in their own table — the games subtab calls
            // loadGames() instead, so loadItems is a no-op when active.
            if (librarySubtab.value === 'games') {
                items.value = [];
                totalItems.value = 0;
                loading.value = false;
                return;
            }

            const [sort, order] = sortBy.value.split('|');
            const params = new URLSearchParams({
                sort, order,
                limit: pageSize.toString(),
                offset: currentOffset.value.toString(),
            });

            // Tokutens subtab has its own filter set (kind, source, favorite,
            // is_manual, search). Drama CDs keep the existing filter surface.
            if (librarySubtab.value === 'tokutens') {
                params.set('only_tokutens', 'true');
                if (tokutenSearchQuery.value) params.set('search', tokutenSearchQuery.value);
                if (tokutenFilters.favorite) params.set('favorite', 'true');
                if (tokutenFilters.is_manual === true) params.set('is_manual', 'true');
                else if (tokutenFilters.is_manual === false) params.set('is_manual', 'false');
                if (tokutenFilters.kind) params.set('tokuten_kind', tokutenFilters.kind);
                if (tokutenFilters.source) params.set('tokuten_source', tokutenFilters.source);
                params.set('lang', currentLang.value);
            } else {
                if (searchQuery.value) params.set('search', searchQuery.value);
                for (const s of filters.seiyuu) {
                    params.append('seiyuu', s);
                }
                for (const t of filters.tag) {
                    params.append('tag', t);
                }
                if (filters.translation_status) params.set('translation_status', filters.translation_status);
                if (filters.listen_status) params.set('listen_status', filters.listen_status);
                if (filters.favorite) params.set('favorite', 'true');
                if (filters.has_metadata === true) params.set('has_metadata', 'true');
                else if (filters.has_metadata === false) params.set('has_metadata', 'false');
                if (filters.is_manual === true) params.set('is_manual', 'true');
                else if (filters.is_manual === false) params.set('is_manual', 'false');
                params.set('lang', currentLang.value);
            }

            try {
                const resp = await fetch('/api/items?' + params);
                const data = await resp.json();

                if (isAppend) {
                    items.value = [...items.value, ...data.items];
                } else {
                    items.value = data.items;
                }
                totalItems.value = data.total;
                bulkMessage.value = '';
                bulkError.value = '';
            } catch (err) {
                console.error('Failed to load items:', err);
            } finally {
                loading.value = false;
            }
        }

        function loadMore() {
            currentOffset.value += pageSize;
            loadItems(true);
        }

        async function createBlankTokuten() {
            // Inline-card pattern: drop a placeholder tokuten + items row into
            // the grid and let the user fill it in via the existing detail
            // panel. No modal, no form — consistent with the rest of the app.
            tokutenCreateBusy.value = true;
            try {
                const resp = await fetch('/api/tokutens', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        title: '[New Tokuten]',
                        kind: 'audio',
                        shop: 'other',
                    }),
                });
                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    throw new Error(detail.detail || ('HTTP ' + resp.status));
                }
                const created = await resp.json();
                if (librarySubtab.value !== 'tokutens') {
                    setLibrarySubtab('tokutens');
                }
                await loadItems();
                // Try to find the paired items row in the just-loaded list.
                // When active filters exclude the new row, fall back to
                // fetching the items row directly so the detail panel still
                // opens — the user clicked "+" expecting to edit it.
                let newItem = items.value.find(i => i.tokuten_id === created.id);
                if (!newItem && created.item_id) {
                    try {
                        const fetchResp = await fetch(`/api/items/${created.item_id}`);
                        if (fetchResp.ok) newItem = await fetchResp.json();
                    } catch (_) { /* fall through */ }
                }
                if (newItem) {
                    openDetail(newItem);
                    startDetailEdit();
                }
            } catch (err) {
                tokutenCreateError.value = 'Add tokuten failed: ' + (err.message || err);
            } finally {
                tokutenCreateBusy.value = false;
            }
        }

        async function createBlankDramaCd() {
            // Drop a placeholder items row and let the user fill it via the
            // detail panel. Backend tags the row with kind='drama_cd' + a
            // synthetic MAN-<hex> product_code + is_manual=1 so it's
            // distinguishable from scanned entries.
            tokutenCreateBusy.value = true;
            try {
                const resp = await fetch('/api/items/blank', { method: 'POST' });
                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    throw new Error(detail.detail || ('HTTP ' + resp.status));
                }
                const created = await resp.json();
                if (librarySubtab.value !== 'drama_cds') {
                    setLibrarySubtab('drama_cds');
                }
                await loadItems();
                // `created` is the full row, so the detail panel can open
                // even when active filters (e.g. favorited-only) would hide
                // the brand-new row from items.value.
                openDetail(created);
                startDetailEdit();
            } catch (err) {
                tokutenCreateError.value = 'Add drama CD failed: ' + (err.message || err);
            } finally {
                tokutenCreateBusy.value = false;
            }
        }

        async function createBlankGame() {
            // Adds a placeholder games row, switches to the Games subtab, and
            // opens the games detail panel in edit mode for immediate filling.
            tokutenCreateBusy.value = true;
            try {
                const resp = await fetch('/api/games/blank', { method: 'POST' });
                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    throw new Error(detail.detail || ('HTTP ' + resp.status));
                }
                const created = await resp.json();
                if (librarySubtab.value !== 'games') {
                    setLibrarySubtab('games');
                }
                await loadGames();
                // Use `created` directly so filters can't block detail open.
                openGameDetail(created);
                startGameEdit();
            } catch (err) {
                tokutenCreateError.value = 'Add game failed: ' + (err.message || err);
            } finally {
                tokutenCreateBusy.value = false;
            }
        }

        // === Bulk add (drama CDs / games / tokutens from a multi-file pick) ===
        // Shared helper: pop the OS multi-select dialog, return picked paths.
        // Returns [] on cancel or any failure (and pushes a toast on failure).
        async function _pickArchivesForBulk(title) {
            try {
                const resp = await fetch('/api/system/pick-files', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        title,
                        filetypes: [
                            ['Archives', '*.7z *.zip *.rar *.tar *.001'],
                            ['All files', '*.*'],
                        ],
                    }),
                });
                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    throw new Error(detail.detail || ('HTTP ' + resp.status));
                }
                const data = await resp.json();
                return Array.isArray(data.paths) ? data.paths : [];
            } catch (err) {
                pushToast({ kind: 'failure', title: 'Bulk add', body: 'Picker failed: ' + (err.message || err), ttl: 5000 });
                return [];
            }
        }
        // Filename → readable default title. Pulls the basename, strips
        // archive extensions (including multi-volume `.part1.rar`).
        function _titleFromArchivePath(p) {
            const base = String(p || '').split(/[\\/]/).pop() || '';
            return _stripArchiveExtension(base) || base || '';
        }
        async function bulkCreateDramaCds() {
            if (tokutenCreateBusy.value) return;
            const paths = await _pickArchivesForBulk('Pick drama CD archives');
            if (paths.length === 0) return;
            tokutenCreateBusy.value = true;
            let ok = 0, fail = 0;
            try {
                for (const p of paths) {
                    try {
                        // 1) Create the placeholder row.
                        const cr = await fetch('/api/items/blank', { method: 'POST' });
                        if (!cr.ok) throw new Error('create failed (' + cr.status + ')');
                        const created = await cr.json();
                        // 2) Patch in title + archive_path. PUT writes both the
                        //    user-data and metadata fields, including the virtual
                        //    archive_path → items.files mapping.
                        const ur = await fetch('/api/items/' + created.id, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                title: _titleFromArchivePath(p) || '[New Drama CD]',
                                archive_path: p,
                            }),
                        });
                        if (!ur.ok) throw new Error('update failed (' + ur.status + ')');
                        ok++;
                    } catch (e) {
                        fail++;
                    }
                }
                if (librarySubtab.value !== 'drama_cds') setLibrarySubtab('drama_cds');
                await loadItems();
                pushToast({
                    kind: fail > 0 ? 'warning' : 'success',
                    title: 'Bulk add',
                    body: `Added ${ok} drama CD${ok === 1 ? '' : 's'}` + (fail > 0 ? ` (${fail} failed)` : ''),
                    ttl: 4500,
                });
            } finally {
                tokutenCreateBusy.value = false;
            }
        }
        async function bulkCreateGames() {
            if (tokutenCreateBusy.value) return;
            const paths = await _pickArchivesForBulk('Pick game files / archives');
            if (paths.length === 0) return;
            tokutenCreateBusy.value = true;
            let ok = 0, fail = 0;
            try {
                for (const p of paths) {
                    try {
                        const cr = await fetch('/api/games/blank', { method: 'POST' });
                        if (!cr.ok) throw new Error('create failed (' + cr.status + ')');
                        const created = await cr.json();
                        const ur = await fetch('/api/games/' + created.id, {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                title: _titleFromArchivePath(p) || '[New Game]',
                                library_path: p,
                            }),
                        });
                        if (!ur.ok) throw new Error('update failed (' + ur.status + ')');
                        ok++;
                    } catch (e) {
                        fail++;
                    }
                }
                if (librarySubtab.value !== 'games') setLibrarySubtab('games');
                await loadGames();
                pushToast({
                    kind: fail > 0 ? 'warning' : 'success',
                    title: 'Bulk add',
                    body: `Added ${ok} game${ok === 1 ? '' : 's'}` + (fail > 0 ? ` (${fail} failed)` : ''),
                    ttl: 4500,
                });
            } finally {
                tokutenCreateBusy.value = false;
            }
        }
        async function bulkCreateTokutens() {
            if (tokutenCreateBusy.value) return;
            const paths = await _pickArchivesForBulk('Pick tokuten archives');
            if (paths.length === 0) return;
            tokutenCreateBusy.value = true;
            let ok = 0, fail = 0;
            try {
                for (const p of paths) {
                    try {
                        const title = _titleFromArchivePath(p) || '[New Tokuten]';
                        const cr = await fetch('/api/tokutens', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ title, kind: 'audio', shop: 'other' }),
                        });
                        if (!cr.ok) throw new Error('create failed (' + cr.status + ')');
                        const created = await cr.json();
                        // Link the archive on the paired items row (is_manual=1
                        // is set by the POST already, so archive_path lands).
                        if (created.item_id) {
                            const ur = await fetch('/api/items/' + created.item_id, {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ title, archive_path: p }),
                            });
                            if (!ur.ok) throw new Error('update failed (' + ur.status + ')');
                        }
                        ok++;
                    } catch (e) {
                        fail++;
                    }
                }
                if (librarySubtab.value !== 'tokutens') setLibrarySubtab('tokutens');
                await loadItems();
                pushToast({
                    kind: fail > 0 ? 'warning' : 'success',
                    title: 'Bulk add',
                    body: `Added ${ok} tokuten${ok === 1 ? '' : 's'}` + (fail > 0 ? ` (${fail} failed)` : ''),
                    ttl: 4500,
                });
            } finally {
                tokutenCreateBusy.value = false;
            }
        }

        // === Items detail-panel edit mode (drama_cd / tokuten) ===
        function startDetailEdit() {
            if (!selectedItem.value) return;
            // Stage editable fields into the draft. seiyuu/tags arrays become
            // comma-separated strings in the input UI (simplest editor — power
            // users can paste comma-lists; we'll add chip editors later if
            // anyone asks). Manual items also expose `archive_path` so the
            // user can point the row at a specific .7z/.zip/.rar on disk —
            // pulled from items.files[0] when present.
            const s = selectedItem.value;
            const filesList = parseJson(s.files);
            const linkedTk = linkedTokutenForItem.value;
            detailDraft.value = {
                title: s.title || '',
                title_en: s.title_en || '',
                circle: s.circle || '',
                release_date: s.release_date || '',
                description: s.description || '',
                description_en: s.description_en || '',
                seiyuu: parseJson(s.seiyuu).join(', '),
                seiyuu_en: parseJson(s.seiyuu_en).join(', '),
                tags: parseJson(s.tags).join(', '),
                tags_en: parseJson(s.tags_en).join(', '),
                archive_path: filesList.length > 0 ? String(filesList[0]) : '',
                // Tokuten-only — staged separately so save can route it to
                // the tokutens PATCH endpoint. Empty string when not editing
                // a tokuten or when tokuten has no vndb link yet.
                tokuten_vndb_id: (s.kind === 'tokuten_audio' && linkedTk) ? (linkedTk.vndb_id || '') : '',
            };
            detailEditing.value = true;
            detailSaveError.value = '';
            // Codeless entries (folder/loose-archive imports, hand-created
            // cards) with no metadata yet: pre-run the multi-source search
            // with the title so matches are already waiting when the panel
            // opens. Hand-edited entries (cast/description present) are
            // assumed matched and left alone.
            const placeholder = !s.title || s.title === '[New Drama CD]' || s.title === '[New Tokuten]';
            const hasMeta = (parseJson(s.seiyuu) || []).length > 0 || !!(s.description || '').trim();
            if (s.is_manual && !placeholder && !hasMeta && !metaSearchQuery.value) {
                metaSearchQuery.value = s.title;
                runMetaSearch(s.title);
            }
        }
        function cancelDetailEdit() {
            detailEditing.value = false;
            detailDraft.value = null;
            detailSaveError.value = '';
        }
        function _csvToArr(s) {
            return String(s || '')
                .split(',')
                .map(x => x.trim())
                .filter(x => x.length > 0);
        }
        // Strip an archive extension (incl. multi-volume `.part1.rar`,
        // `.r00` etc.) from a filename. Used so Browse-picks can prefill the
        // title field with something readable when the title is still the
        // backend's "[New Drama CD]" placeholder.
        function _stripArchiveExtension(name) {
            let out = String(name || '');
            // Strip multi-volume suffixes first: ".part1.rar", ".part01.rar"
            out = out.replace(/\.part\d+\.rar$/i, '');
            // Single-extension archives.
            out = out.replace(/\.(7z|zip|rar|001|tar|gz|bz2)$/i, '');
            return out;
        }
        async function browseArchivePath() {
            if (!detailDraft.value || archiveBrowseBusy.value) return;
            archiveBrowseBusy.value = true;
            try {
                const resp = await fetch('/api/system/pick-file', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        title: 'Pick drama CD archive',
                        filetypes: [
                            ['Archives', '*.7z *.zip *.rar *.tar *.001'],
                            ['All files', '*.*'],
                        ],
                    }),
                });
                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    throw new Error(detail.detail || ('HTTP ' + resp.status));
                }
                const data = await resp.json();
                if (data.cancelled || !data.path) return;
                detailDraft.value.archive_path = data.path;
                // If the title is still the auto-blank placeholder, derive a
                // sensible default from the filename. Don't overwrite a title
                // the user has already typed.
                const currentTitle = (detailDraft.value.title || '').trim();
                if (!currentTitle || currentTitle === '[New Drama CD]') {
                    const parts = data.path.split(/[\\/]/);
                    const base = parts[parts.length - 1] || '';
                    const stripped = _stripArchiveExtension(base);
                    if (stripped) detailDraft.value.title = stripped;
                }
            } catch (err) {
                detailSaveError.value = 'Browse failed: ' + (err.message || err);
            } finally {
                archiveBrowseBusy.value = false;
            }
        }
        async function saveDetailEdit() {
            if (!selectedItem.value || !detailDraft.value) return;
            detailSavingBusy.value = true;
            detailSaveError.value = '';
            const d = detailDraft.value;
            const body = {
                title: d.title,
                title_en: d.title_en || null,
                circle: d.circle || null,
                release_date: d.release_date || null,
                description: d.description || null,
                description_en: d.description_en || null,
                seiyuu: _csvToArr(d.seiyuu),
                seiyuu_en: _csvToArr(d.seiyuu_en),
                tags: _csvToArr(d.tags),
                tags_en: _csvToArr(d.tags_en),
            };
            // Only forward archive_path when it's been touched on a manual
            // row — scanned items have their files set by the scanner and
            // we don't want to clobber that array with an empty string.
            if (selectedItem.value && selectedItem.value.is_manual) {
                body.archive_path = (d.archive_path || '').trim();
            }
            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    detailSaveError.value = err.detail || 'Save failed';
                    return;
                }
                const updated = await resp.json();
                // Refresh both the selected pointer and the in-list row.
                // If the row is currently filtered out of items.value (idx<0)
                // skip the list write — items.value[-1] would silently set a
                // junk property instead of failing loudly.
                selectedItem.value = { ...updated };
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = { ...updated };
                // Tokuten cross-link: vndb_id lives on the tokutens row, so
                // route that field through PATCH /api/tokutens/{id}. Update
                // linkedTokutenForItem locally + refresh the linked game
                // lookup so the read mode pill is correct on close.
                if (updated.kind === 'tokuten_audio' && updated.tokuten_id) {
                    const desired = (d.tokuten_vndb_id || '').trim() || null;
                    const current = (linkedTokutenForItem.value || {}).vndb_id || null;
                    if (desired !== current) {
                        try {
                            const tkResp = await fetch(`/api/tokutens/${updated.tokuten_id}`, {
                                method: 'PATCH',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ vndb_id: desired }),
                            });
                            if (tkResp.ok) {
                                const tkUpdated = await tkResp.json();
                                linkedTokutenForItem.value = tkUpdated;
                                linkedGameForTokuten.value = null;
                                if (tkUpdated.vndb_id) {
                                    _loadLinkedGameForTokuten(tkUpdated.vndb_id);
                                }
                            }
                        } catch (_) {}
                    }
                }
                detailEditing.value = false;
                detailDraft.value = null;
            } catch (err) {
                detailSaveError.value = 'Save failed: ' + (err.message || err);
            } finally {
                detailSavingBusy.value = false;
            }
        }

        // === Games detail panel state + handlers ===
        function openGameDetail(game) {
            selectedGame.value = { ...game };
            gameEditing.value = false;
            gameDraft.value = null;
            gameSaveError.value = '';
            // Cross-link: surface tokutens that share this game's vndb_id.
            linkedTokutensForGame.value = [];
            if (game && game.vndb_id) {
                _loadLinkedTokutensForGame(game.vndb_id);
            }
        }
        async function _loadLinkedTokutensForGame(vndbId) {
            try {
                const resp = await fetch('/api/tokutens?vndb_id=' + encodeURIComponent(vndbId) + '&limit=50');
                if (!resp.ok) return;
                const data = await resp.json();
                linkedTokutensForGame.value = data.tokutens || [];
            } catch (_) {}
        }
        function closeGameDetail() {
            selectedGame.value = null;
            gameEditing.value = false;
            gameDraft.value = null;
            gameSaveError.value = '';
            linkedTokutensForGame.value = [];
        }
        // Switch the visible detail panel from a tokuten to its linked game
        // (called when the user clicks the "linked game" pill on the tokuten
        // panel). Fetches the game freshly so we don't carry stale fields.
        async function openLinkedGame(gameId) {
            if (!gameId) return;
            try {
                const resp = await fetch('/api/games/' + gameId);
                if (!resp.ok) return;
                const game = await resp.json();
                // Switch to Games subtab if the user isn't already on it,
                // so the games detail panel renders against its own state.
                if (librarySubtab.value !== 'games') setLibrarySubtab('games');
                // Close any items detail panel first.
                selectedItem.value = null;
                openGameDetail(game);
            } catch (_) {}
        }
        // Reverse: tokuten link clicked from the game panel — close game
        // panel, switch to Tokutens subtab, open the items detail panel
        // for the paired items row.
        async function openLinkedTokuten(tokuten) {
            if (!tokuten || !tokuten.item_id) return;
            try {
                const resp = await fetch('/api/items/' + tokuten.item_id);
                if (!resp.ok) return;
                const item = await resp.json();
                if (librarySubtab.value !== 'tokutens') setLibrarySubtab('tokutens');
                selectedGame.value = null;
                openDetail(item);
            } catch (_) {}
        }
        function startGameEdit() {
            if (!selectedGame.value) return;
            const g = selectedGame.value;
            gameDraft.value = {
                title: g.title || '',
                title_jp: g.title_jp || '',
                title_en: g.title_en || '',
                developer: g.developer || '',
                vndb_id: g.vndb_id || '',
                release_date: g.release_date || '',
                description: g.description || '',
                library_path: g.library_path || '',
                play_status: g.play_status || 'backlog',
                personal_rating: g.personal_rating ?? '',
                personal_notes: g.personal_notes || '',
                walkthrough_notes: g.walkthrough_notes || '',
                platforms: (g.platforms || []).join(', '),
                platforms_available: (g.platforms_available || []).join(', '),
                languages: (g.languages || []).join(', '),
            };
            gameEditing.value = true;
            gameSaveError.value = '';
        }
        function cancelGameEdit() {
            gameEditing.value = false;
            gameDraft.value = null;
            gameSaveError.value = '';
        }
        async function saveGameEdit() {
            if (!selectedGame.value || !gameDraft.value) return;
            gameSavingBusy.value = true;
            gameSaveError.value = '';
            const d = gameDraft.value;
            const rating = d.personal_rating === '' || d.personal_rating == null
                ? null
                : Number(d.personal_rating);
            const body = {
                title: d.title,
                title_jp: d.title_jp || null,
                title_en: d.title_en || null,
                developer: d.developer || null,
                vndb_id: d.vndb_id || null,
                release_date: d.release_date || null,
                description: d.description || null,
                library_path: d.library_path || null,
                play_status: d.play_status,
                personal_rating: Number.isFinite(rating) ? rating : null,
                personal_notes: d.personal_notes || '',
                walkthrough_notes: d.walkthrough_notes || '',
                platforms: _csvToArr(d.platforms),
                platforms_available: _csvToArr(d.platforms_available),
                languages: _csvToArr(d.languages),
            };
            // Carry cover_url through if the draft has it (VNDB prefill staged
            // it) — the backend triggers the local cover download when this
            // field is present and cover_local is empty.
            if (d.cover_url) body.cover_url = d.cover_url;
            try {
                const resp = await fetch(`/api/games/${selectedGame.value.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    gameSaveError.value = err.detail || 'Save failed';
                    return;
                }
                const updated = await resp.json();
                selectedGame.value = { ...updated };
                const idx = gamesItems.value.findIndex(g => g.id === updated.id);
                if (idx >= 0) gamesItems.value[idx] = { ...updated };
                gameEditing.value = false;
                gameDraft.value = null;
            } catch (err) {
                gameSaveError.value = 'Save failed: ' + (err.message || err);
            } finally {
                gameSavingBusy.value = false;
            }
        }

        // === VNDB search (games edit panel) ===
        function vndbSearchInput(value) {
            vndbQuery.value = value;
            if (_vndbSearchTimer) clearTimeout(_vndbSearchTimer);
            const q = (value || '').trim();
            if (!q) {
                vndbResults.value = [];
                return;
            }
            _vndbSearchTimer = setTimeout(() => runVndbSearch(q), 350);
        }
        async function runVndbSearch(q) {
            vndbSearching.value = true;
            vndbSearchError.value = '';
            try {
                const resp = await fetch('/api/games/vndb/search?q=' + encodeURIComponent(q));
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    vndbSearchError.value = err.detail || 'VNDB search failed';
                    vndbResults.value = [];
                    return;
                }
                const data = await resp.json();
                vndbResults.value = data.results || [];
            } catch (err) {
                vndbSearchError.value = 'VNDB search failed';
                vndbResults.value = [];
            } finally {
                vndbSearching.value = false;
            }
        }
        async function applyVndbResult(candidate) {
            // Fetch the full VN record (search returned the trimmed summary,
            // but we need description / platforms / languages / image url).
            if (!candidate || !candidate.id) return;
            try {
                const resp = await fetch('/api/games/vndb/' + encodeURIComponent(candidate.id));
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    vndbSearchError.value = err.detail || 'VNDB lookup failed';
                    return;
                }
                const fields = await resp.json();
                if (!gameDraft.value) startGameEdit();
                // Merge into the draft. Empty/null values from VNDB don't
                // clobber existing user-entered values — only positive values
                // override.
                const d = gameDraft.value;
                if (fields.vndb_id) d.vndb_id = fields.vndb_id;
                if (fields.title) d.title = fields.title;
                if (fields.title_jp) d.title_jp = fields.title_jp;
                if (fields.title_en) d.title_en = fields.title_en;
                if (fields.developer) d.developer = fields.developer;
                if (fields.release_date) d.release_date = fields.release_date;
                if (fields.description) d.description = fields.description;
                // VNDB platforms now flow into `platforms_available` (the
                // full release list); the user's owned-on column stays
                // untouched so the card pill remains the truth about what
                // they actually have locally.
                if ((fields.platforms_available || []).length) {
                    d.platforms_available = fields.platforms_available.join(', ');
                }
                if ((fields.languages || []).length) d.languages = fields.languages.join(', ');
                // Stash cover_url on the draft so saveGameEdit forwards it
                // and the backend triggers the local download.
                if (fields.cover_url) d.cover_url = fields.cover_url;
                // Collapse the dropdown so it doesn't sit visible after pick.
                vndbResults.value = [];
                vndbQuery.value = '';
            } catch (err) {
                vndbSearchError.value = 'VNDB lookup failed';
                console.error('VNDB lookup failed:', err);
            }
        }

        // Tokuten variant of applyVndbResult — sets the vndb_id, then
        // pulls the cover into the paired items row (cover_local) and
        // inherits the game's release_date when the tokuten doesn't have
        // its own. Tokutens ship with the game so the release_date is
        // almost always shared.
        async function applyVndbResultForTokuten(candidate) {
            if (!candidate || !candidate.id) return;
            if (!detailDraft.value) return;
            detailDraft.value.tokuten_vndb_id = candidate.id;
            vndbResults.value = [];
            vndbQuery.value = '';
            // Inherit release_date when the tokuten doesn't have its own.
            // The full VN record carries the date — fetch it once.
            try {
                const fullResp = await fetch('/api/games/vndb/' + encodeURIComponent(candidate.id));
                if (!fullResp.ok) return;
                const fields = await fullResp.json();
                if (fields.release_date && !(detailDraft.value.release_date || '').trim()) {
                    detailDraft.value.release_date = fields.release_date;
                }
                // Cover: stash the URL so the items PUT save path picks it up.
                // saveDetailEdit doesn't currently forward cover_url, so we
                // fire the upload directly via the existing items cover
                // endpoint — same flow as ManualCoverRequest.
                if (fields.cover_url && selectedItem.value && !selectedItem.value.cover_local) {
                    _fetchAndUploadTokutenCover(selectedItem.value.id, fields.cover_url);
                }
            } catch (err) {
                console.warn('VNDB cover/date fetch for tokuten failed:', err);
            }
        }
        async function _fetchAndUploadTokutenCover(itemId, coverUrl) {
            // Server-side download — VNDB's image CDN doesn't allow CORS, so
            // a browser fetch() against it would error. The backend grabs
            // the image and writes it to COVERS_DIR, then returns the
            // updated row.
            try {
                const resp = await fetch(`/api/items/${itemId}/cover-from-url`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cover_url: coverUrl }),
                });
                if (!resp.ok) return;
                const updated = await resp.json();
                if (selectedItem.value && selectedItem.value.id === itemId) {
                    selectedItem.value = { ...selectedItem.value, cover_local: updated.cover_local };
                }
                // Also patch the in-list row so the card cover refreshes.
                const idx = items.value.findIndex(i => i.id === itemId);
                if (idx >= 0) items.value[idx] = { ...items.value[idx], cover_local: updated.cover_local };
            } catch (err) {
                console.warn('Tokuten cover download failed:', err);
            }
        }

        // === External metadata fetch (Gamers / Chil-Chil) ===
        // Available on manual drama CDs and tokuten cards inside the detail
        // edit panel. Two entry points — paste a product URL, or title-search
        // across all sources — both land in the same preview modal.
        function metaSearchInput(value) {
            metaSearchQuery.value = value;
            if (_metaSearchTimer) clearTimeout(_metaSearchTimer);
            const q = (value || '').trim();
            if (!q) {
                metaSearchResults.value = [];
                return;
            }
            _metaSearchTimer = setTimeout(() => runMetaSearch(q), 400);
        }
        async function runMetaSearch(q) {
            metaSearching.value = true;
            try {
                const resp = await fetch('/api/metadata/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: q }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    metaFetchError.value = err.detail || 'Search failed';
                    metaSearchResults.value = [];
                    return;
                }
                const data = await resp.json();
                metaSearchResults.value = data.results || [];
                if ((data.errors || []).length && !metaSearchResults.value.length) {
                    metaFetchError.value = data.errors.join(' / ');
                }
            } catch (err) {
                metaFetchError.value = 'Search failed';
                metaSearchResults.value = [];
            } finally {
                metaSearching.value = false;
            }
        }
        async function metaFetchFromUrl(url) {
            const target = (url || metaFetchUrl.value || '').trim();
            if (!target || metaFetchBusy.value) return;
            metaFetchBusy.value = true;
            metaFetchError.value = '';
            try {
                const resp = await fetch('/api/metadata/fetch-url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: target }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    metaFetchError.value = err.detail || 'Fetch failed';
                    return;
                }
                const data = await resp.json();
                _openMetaPreview(data.metadata);
                // Collapse search results once a pick lands in the preview.
                metaSearchResults.value = [];
                metaSearchQuery.value = '';
            } catch (err) {
                metaFetchError.value = 'Fetch failed: ' + (err.message || err);
            } finally {
                metaFetchBusy.value = false;
            }
        }
        function metaPickSearchResult(hit) {
            if (!hit || !hit.url) return;
            metaFetchFromUrl(hit.url);
        }
        const META_SOURCE_LABELS = {
            dlsite: 'DLsite', gamers: 'Gamers', chil_chil: 'Chil-Chil', rejet: 'Rejet', vgmdb: 'VGMdb',
        };
        function metaSourceLabel(name) {
            return META_SOURCE_LABELS[name] || name || '?';
        }
        // === Multi-volume selection ===
        // Checkboxes on search results; "Fetch N selected" merges the picked
        // volumes server-side (cast union, common-prefix title, per-volume
        // note lines) into one preview for the single library entry.
        function metaToggleSelected(hit) {
            if (!hit || !hit.url) return;
            const idx = metaSelectedUrls.value.indexOf(hit.url);
            if (idx >= 0) metaSelectedUrls.value.splice(idx, 1);
            else if (metaSelectedUrls.value.length < 20) metaSelectedUrls.value.push(hit.url);
            else metaFetchError.value = 'Max 20 volumes per fetch';
        }
        async function metaFetchSelected() {
            const urls = metaSelectedUrls.value.slice();
            if (!urls.length || metaFetchBusy.value) return;
            metaFetchBusy.value = true;
            metaFetchError.value = '';
            try {
                const resp = await fetch('/api/metadata/fetch-multi', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ urls }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    metaFetchError.value = err.detail || 'Fetch failed';
                    return;
                }
                const data = await resp.json();
                if ((data.errors || []).length) {
                    metaFetchError.value = `${data.errors.length} volume(s) failed; merged the rest`;
                }
                _openMetaPreview(data.metadata);
                metaSelectedUrls.value = [];
                metaSearchResults.value = [];
                metaSearchQuery.value = '';
            } catch (err) {
                metaFetchError.value = 'Fetch failed: ' + (err.message || err);
            } finally {
                metaFetchBusy.value = false;
            }
        }
        function _metaTargetKind() {
            const s = selectedItem.value;
            return (s && s.kind === 'tokuten_audio' && s.tokuten_id) ? 'tokuten' : 'item';
        }
        function _openMetaPreview(meta) {
            if (!meta) return;
            const s = selectedItem.value || {};
            const d = detailDraft.value || {};
            const placeholder = (t) => !t || t === '[New Drama CD]' || t === '[New Tokuten]';
            // Default checkboxes: fill blanks, never default-overwrite data
            // the user already has. source_note is additive so it's always on.
            const fields = {
                title: !!meta.title && placeholder((d.title || s.title || '').trim()),
                release_date: !!meta.release_date && !(d.release_date || s.release_date || '').trim(),
                seiyuu: (meta.seiyuu || []).length > 0 && !(d.seiyuu || '').trim() && !(parseJson(s.seiyuu) || []).length,
                description: !!meta.description && !(d.description || s.description || '').trim(),
                cover: !!meta.cover_url && !s.cover_local,
                source_note: true,
            };
            if (_metaTargetKind() === 'tokuten') {
                fields.shop = true;
                fields.source_url = true;
            } else if ((meta.extra || {}).product_code) {
                // DLsite hit on a manual item: adopting the real code unlocks
                // the full standard pipeline (scan refresh, tags, age rating).
                fields.adopt_code = true;
            }
            metaPreviewFields.value = fields;
            metaPreview.value = meta;
            metaApplyError.value = '';
        }
        function metaClosePreview() {
            metaPreview.value = null;
            metaPreviewFields.value = {};
        }
        function _resetMetaFetchState() {
            metaFetchUrl.value = '';
            metaSearchQuery.value = '';
            metaSearchResults.value = [];
            metaSelectedUrls.value = [];
            metaPreview.value = null;
            metaPreviewFields.value = {};
            galleryOpen.value = false;
            galleryMedia.value = [];
        }

        // === Cover mini-gallery (media_assets) ===
        // Extra volume covers from multi-volume fetches (and tokuten scanner
        // gallery files) live in media_assets; this popover lists them with
        // set-as-cover / remove actions.
        async function openItemGallery() {
            if (!selectedItem.value) return;
            galleryOpen.value = true;
            galleryLoading.value = true;
            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/media`);
                galleryMedia.value = resp.ok ? (await resp.json()).media || [] : [];
            } catch (err) {
                galleryMedia.value = [];
            } finally {
                galleryLoading.value = false;
            }
        }
        function closeItemGallery() {
            galleryOpen.value = false;
        }
        async function setGalleryCover(media) {
            if (!selectedItem.value || !media || galleryBusy.value) return;
            galleryBusy.value = true;
            galleryError.value = '';
            try {
                const resp = await fetch(
                    `/api/items/${selectedItem.value.id}/media/${media.id}/set-cover`,
                    { method: 'POST' },
                );
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    galleryError.value = err.detail || 'Set cover failed';
                    return;
                }
                const updated = await resp.json();
                selectedItem.value = { ...selectedItem.value, ...updated };
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = { ...items.value[idx], ...updated };
                pushToast({ kind: 'success', title: 'Cover updated', ttl: 2500 });
            } catch (err) {
                galleryError.value = 'Set cover failed: ' + (err.message || err);
            } finally {
                galleryBusy.value = false;
            }
        }
        async function removeGalleryMedia(media) {
            if (!selectedItem.value || !media || galleryBusy.value) return;
            galleryBusy.value = true;
            galleryError.value = '';
            try {
                const resp = await fetch(
                    `/api/items/${selectedItem.value.id}/media/${media.id}`,
                    { method: 'DELETE' },
                );
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    galleryError.value = err.detail || 'Remove failed';
                    return;
                }
                galleryMedia.value = galleryMedia.value.filter(m => m.id !== media.id);
            } catch (err) {
                galleryError.value = 'Remove failed: ' + (err.message || err);
            } finally {
                galleryBusy.value = false;
            }
        }
        // Client-side mirror of the backend's provenance stamp, for preview.
        function metaNotePreview(meta) {
            if (!meta) return '';
            const today = new Date().toISOString().slice(0, 10);
            const volumes = (meta.extra || {}).volumes || [];
            if (volumes.length) {
                // Multi-volume: one line per volume, mirroring the backend.
                const lines = volumes.map(v => {
                    const parts = ['[' + (v.source || '?') + ']'];
                    if (v.title) parts.push(v.title);
                    if (v.price) parts.push(v.price);
                    if (v.catalog_number) parts.push('品番 ' + v.catalog_number);
                    if (v.jan) parts.push('JAN ' + v.jan);
                    if (v.release_date) parts.push(v.release_date);
                    if (v.source_url) parts.push(v.source_url);
                    return parts.join(' ・ ');
                });
                lines.push(`(${volumes.length} volumes ・ fetched ${today})`);
                return lines.join('\n');
            }
            const parts = ['[' + (meta.source || '?') + ']'];
            if (meta.price) parts.push(meta.price);
            if (meta.catalog_number) parts.push('品番 ' + meta.catalog_number);
            if (meta.jan) parts.push('JAN ' + meta.jan);
            if (meta.maker) parts.push(meta.maker);
            if (meta.series) parts.push('シリーズ: ' + meta.series);
            if (meta.source_url) parts.push(meta.source_url);
            parts.push('fetched ' + today);
            return parts.join(' ・ ');
        }
        async function metaApply() {
            if (!metaPreview.value || !selectedItem.value || metaApplyBusy.value) return;
            const kind = _metaTargetKind();
            const targetId = kind === 'tokuten' ? selectedItem.value.tokuten_id : selectedItem.value.id;
            const fields = Object.entries(metaPreviewFields.value)
                .filter(([, on]) => on)
                .map(([name]) => name);
            if (!fields.length) {
                metaApplyError.value = 'Nothing selected to apply';
                return;
            }
            metaApplyBusy.value = true;
            metaApplyError.value = '';
            try {
                const resp = await fetch('/api/metadata/apply', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        target: kind,
                        target_id: targetId,
                        metadata: metaPreview.value,
                        fields,
                    }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    metaApplyError.value = err.detail || 'Apply failed';
                    return;
                }
                // Re-pull the items row (apply writes server-side; for
                // tokutens the mirror lands on the paired items row).
                const itemResp = await fetch(`/api/items/${selectedItem.value.id}`);
                if (itemResp.ok) {
                    const updated = await itemResp.json();
                    selectedItem.value = { ...updated };
                    const idx = items.value.findIndex(i => i.id === updated.id);
                    if (idx >= 0) items.value[idx] = { ...updated };
                    // Refresh the staged draft with the applied values so a
                    // later Save doesn't write stale pre-fetch data back.
                    if (detailDraft.value) {
                        const d = detailDraft.value;
                        d.title = updated.title || '';
                        d.release_date = updated.release_date || '';
                        d.seiyuu = parseJson(updated.seiyuu).join(', ');
                        d.description = updated.description || '';
                    }
                }
                metaClosePreview();
                pushToast({ kind: 'success', title: 'Metadata applied', ttl: 3000 });
            } catch (err) {
                metaApplyError.value = 'Apply failed: ' + (err.message || err);
            } finally {
                metaApplyBusy.value = false;
            }
        }

        // === Game manual cover upload ===
        function chooseGameCover() {
            if (gameCoverFileInput.value) {
                gameCoverFileInput.value.click();
            }
        }
        async function uploadGameCover(filename, dataUrl) {
            if (!selectedGame.value || !filename || !dataUrl) return;
            gameCoverUploadLoading.value = true;
            try {
                const resp = await fetch(`/api/games/${selectedGame.value.id}/cover`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename, data_url: dataUrl }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    pushToast({ kind: 'failure', title: 'Cover upload failed', body: err.detail || '', ttl: 4000 });
                    return;
                }
                const updated = await resp.json();
                selectedGame.value = { ...updated };
                const idx = gamesItems.value.findIndex(g => g.id === updated.id);
                if (idx >= 0) gamesItems.value[idx] = { ...updated };
                pushToast({ kind: 'success', title: 'Cover updated', ttl: 2500 });
            } catch (err) {
                pushToast({ kind: 'failure', title: 'Cover upload failed', ttl: 4000 });
            } finally {
                gameCoverUploadLoading.value = false;
            }
        }
        function onGameCoverFileChange(e) {
            const file = e.target && e.target.files ? e.target.files[0] : null;
            if (file) {
                const reader = new FileReader();
                reader.onload = () => uploadGameCover(file.name || 'cover.png', String(reader.result || ''));
                reader.onerror = () => { pushToast({ kind: 'failure', title: 'Failed to read image file', ttl: 4000 }); };
                reader.readAsDataURL(file);
            }
            if (e.target) e.target.value = '';
        }

        async function matchVndbAll() {
            if (vndbMatchBusy.value) return;
            vndbMatchBusy.value = true;
            vndbMatchError.value = '';
            vndbMatchMessage.value = '';
            try {
                const resp = await fetch('/api/games/match-vndb-all', { method: 'POST' });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    vndbMatchError.value = err.detail || 'VNDB bulk match failed';
                    return;
                }
                const data = await resp.json();
                const errCount = (data.errors || []).length;
                vndbMatchMessage.value =
                    `Matched ${data.matched} of ${data.processed}` +
                    (data.skipped_no_hit ? ` · ${data.skipped_no_hit} no hit` : '') +
                    (errCount ? ` · ${errCount} errors` : '');
                // Refresh the games grid so the new covers/titles show up.
                await loadGames();
            } catch (err) {
                vndbMatchError.value = 'VNDB bulk match failed';
                console.error('VNDB bulk match failed:', err);
            } finally {
                vndbMatchBusy.value = false;
            }
        }

        function isSelected(itemId) {
            return selectedIds.value.has(itemId);
        }

        function toggleSelectItem(itemId) {
            const next = new Set(selectedIds.value);
            if (next.has(itemId)) next.delete(itemId);
            else next.add(itemId);
            selectedIds.value = next;
            // Selection no longer auto-jumps to Workshop. Ctrl+click on a card
            // starts selection mode and stays on the library tab; users can
            // explicitly send to Workshop via the bulk-bar actions.
        }

        function toggleSelectAllVisible() {
            if (librarySubtab.value === 'games') {
                const next = new Set(selectedGameIds.value);
                if (allVisibleSelected.value) {
                    for (const g of gamesItems.value) next.delete(g.id);
                } else {
                    for (const g of gamesItems.value) next.add(g.id);
                }
                selectedGameIds.value = next;
                return;
            }
            const next = new Set(selectedIds.value);
            if (allVisibleSelected.value) {
                for (const item of items.value) next.delete(item.id);
            } else {
                for (const item of items.value) next.add(item.id);
            }
            selectedIds.value = next;
        }

        function clearSelection() {
            if (librarySubtab.value === 'games') {
                selectedGameIds.value = new Set();
            } else {
                selectedIds.value = new Set();
            }
            bulkMessage.value = '';
            bulkError.value = '';
        }

        // Bulk plain-delete for games — fires one DELETE per id with
        // ignore_path=false. Non-destructive variant; rows reappear on
        // next scan. Used by the bulk-actions kebab for the "4 identical
        // Hana Awase" scenario.
        async function bulkDeleteGamesSelected() {
            const ids = Array.from(selectedGameIds.value);
            if (!ids.length) return;
            if (!confirm(`Delete ${ids.length} game entr${ids.length === 1 ? 'y' : 'ies'} from the library? Files on disk untouched; will reappear on next scan.`)) return;
            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';
            let ok = 0;
            let failed = 0;
            for (const id of ids) {
                try {
                    const resp = await fetch(`/api/games/${id}?ignore_path=false`, { method: 'DELETE' });
                    if (resp.ok) {
                        ok += 1;
                    } else {
                        failed += 1;
                    }
                } catch (_) {
                    failed += 1;
                }
            }
            // Drop from local list + clear selection + refresh stats.
            const idSet = new Set(ids);
            gamesItems.value = gamesItems.value.filter(g => !idSet.has(g.id));
            gamesTotal.value = Math.max(0, gamesTotal.value - ok);
            selectedGameIds.value = new Set();
            bulkLoading.value = false;
            pushToast({
                kind: failed > 0 ? 'warning' : 'success',
                title: 'Games deleted',
                body: `${ok} removed${failed > 0 ? `, ${failed} failed` : ''}`,
                ttl: 4000,
            });
            loadGameStats();
            loadGameDistinctOptions();
        }

        function applyBulkResult(resultItems) {
            if (!Array.isArray(resultItems)) return;
            for (const row of resultItems) {
                if (!row || row.status !== 'ok' || !row.item) continue;
                const idx = items.value.findIndex(i => i.id === row.item.id);
                if (idx >= 0) items.value[idx] = row.item;
                if (selectedItem.value && selectedItem.value.id === row.item.id) {
                    selectedItem.value = { ...row.item };
                }
            }
        }

        async function runBulkAction(url, body, successLabel) {
            if (selectedCount.value === 0) return;
            bulkLoading.value = true;
            bulkError.value = '';
            bulkMessage.value = '';
            try {
                const resp = await fetch(url, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    bulkError.value = data.detail || `${successLabel} failed`;
                    return;
                }
                applyBulkResult(data.results);
                bulkMessage.value = `${successLabel}: ${data.succeeded}/${data.requested} succeeded`;
                await loadStats();
            } catch (err) {
                bulkError.value = `${successLabel} failed`;
                console.error(`${successLabel} failed:`, err);
            } finally {
                bulkLoading.value = false;
            }
        }

        async function bulkConfirmSelected() {
            const itemIds = Array.from(selectedIds.value);
            await runBulkAction('/api/bulk/items/confirm', { item_ids: itemIds }, 'Bulk confirm');
        }

        async function bulkUnconfirmSelected() {
            const itemIds = Array.from(selectedIds.value);
            await runBulkAction('/api/bulk/items/unconfirm', { item_ids: itemIds }, 'Bulk unconfirm');
        }

        async function bulkOverrideSelected() {
            const selected = items.value.filter(i => selectedIds.value.has(i.id));
            if (!selected.length) return;

            const template = selected.map(i => `${i.id}=${i.product_code}`).join('\n');
            const raw = prompt(
                'Enter overrides as "item_id=RJ/BJ/VJ########" (one per line):',
                template
            );
            if (!raw) return;

            const overrides = [];
            const lines = raw.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
            for (const line of lines) {
                const parts = line.split('=');
                if (parts.length !== 2) continue;
                const itemId = Number(parts[0].trim());
                const code = parts[1].trim();
                if (!Number.isInteger(itemId) || !code) continue;
                overrides.push({ item_id: itemId, product_code: code });
            }
            if (!overrides.length) {
                bulkError.value = 'No valid override lines found.';
                return;
            }
            await runBulkAction('/api/bulk/items/override', { overrides }, 'Bulk override');
        }

        function toggleBulkActions() {
            showBulkActions.value = !showBulkActions.value;
        }

        async function bulkTranslateSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;

            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';

            try {
                // Call translate endpoint for each selected item
                let successCount = 0;
                let failCount = 0;

                for (const itemId of itemIds) {
                    try {
                        const resp = await fetch(`/api/items/${itemId}/translate-metadata`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                        });
                        if (resp.ok) {
                            successCount++;
                        } else {
                            failCount++;
                        }
                    } catch (err) {
                        failCount++;
                    }
                }

                bulkMessage.value = `Translated ${successCount} items${failCount > 0 ? `, ${failCount} failed` : ''}`;
                await loadItems();
            } catch (err) {
                bulkError.value = 'Failed to translate items: ' + err.message;
            } finally {
                bulkLoading.value = false;
            }
        }

        // ---------- Activity drawer + toast helpers ----------
        const AUTOPILOT_STAGE_LABELS = {
            metadata_translate: 'Translating metadata',
            extract: 'Unpacking audio',
            track_titles_translate: 'Translating track titles',
            transcribe: 'Transcribing',
            track_translate: 'Translating',
        };
        const AUTOPILOT_STAGE_ORDER = [
            'metadata_translate',
            'extract',
            'track_titles_translate',
            'transcribe',
            'track_translate',
        ];
        const AUTOPILOT_TOTAL_STAGES = AUTOPILOT_STAGE_ORDER.length;

        // Human-readable label for each pipeline job_type. The drawer shows
        // every pipeline_* job, not just autopilot, so all of these need a
        // friendly name.
        const PIPELINE_JOB_LABELS = {
            pipeline_autopilot: 'Full workflow',
            pipeline_extract: 'Unpacking audio',
            pipeline_transcribe: 'Transcribing',
            pipeline_translate: 'Translating',
        };

        function pipelineJobLabel(job) {
            return PIPELINE_JOB_LABELS[job?.job_type] || (job?.job_type || 'Job');
        }

        // For each autopilot job we may surface the *latest* sub-job that
        // overlaps in time (extraction / transcription / translation that the
        // autopilot itself spawned for the same item). Those sub-jobs render
        // as a nested line inside the parent row instead of as a sibling.
        function findActivityChildJob(parent, all) {
            if (!parent || parent.job_type !== 'pipeline_autopilot') return null;
            const itemId = parent?.metadata_json?.item_id;
            if (itemId == null) return null;
            const parentStart = Date.parse(parent.created_at || '') || 0;
            const parentEnd = parent.finished_at
                ? (Date.parse(parent.finished_at) || Number.MAX_SAFE_INTEGER)
                : Number.MAX_SAFE_INTEGER;
            let best = null;
            let bestT = -1;
            for (const j of all) {
                if (!j || j.job_type === 'pipeline_autopilot') continue;
                if (j?.metadata_json?.item_id !== itemId) continue;
                const t = Date.parse(j.created_at || '') || 0;
                if (t < parentStart || t > parentEnd) continue;
                if (t > bestT) { best = j; bestT = t; }
            }
            return best;
        }

        const nestedChildJobIds = computed(() => {
            const ids = new Set();
            for (const j of autopilotJobs.value) {
                if (j.job_type !== 'pipeline_autopilot') continue;
                const child = findActivityChildJob(j, autopilotJobs.value);
                if (child) ids.add(child.id);
            }
            return ids;
        });

        function activityChildJob(parentJob) {
            return findActivityChildJob(parentJob, autopilotJobs.value);
        }

        function activityChildDisplay(child) {
            if (!child) return '';
            const cur = String(child.current || '').trim();
            const c = Number(child.completed || 0);
            const t = Number(child.total || 0);
            if (cur && t > 0) return `${cur}  (${c}/${t})`;
            if (cur) return cur;
            if (t > 0) return `${c}/${t}`;
            return pipelineJobLabel(child);
        }

        const visibleAutopilotJobs = computed(() =>
            autopilotJobs.value.filter(j =>
                !dismissedActivityJobs.value.has(j.id) &&
                !nestedChildJobIds.value.has(j.id)
            )
        );

        const autopilotActiveCount = computed(() =>
            visibleAutopilotJobs.value.filter(j => j.status === 'running' || j.status === 'queued').length
        );

        const finishedActivityCount = computed(() =>
            visibleAutopilotJobs.value.filter(j => ['completed', 'failed', 'stopped'].includes(j.status)).length
        );

        async function fetchAndCacheTracks(itemId) {
            if (itemId == null) return;
            if (activityTracksCache[itemId]) return;
            if (activityTracksFetchInflight.has(itemId)) return;
            activityTracksFetchInflight.add(itemId);
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/tracks`);
                if (!resp.ok) return;
                const data = await resp.json();
                const map = {};
                for (const t of (data.tracks || [])) {
                    map[t.id] = {
                        title: String(t.title || '').trim(),
                        title_en: String(t.title_en || '').trim(),
                        track_index: t.track_index,
                    };
                }
                activityTracksCache[itemId] = map;
            } catch (_) {
                // best effort
            } finally {
                activityTracksFetchInflight.delete(itemId);
            }
        }

        // Strip the `trackNN_` / `Track NN -` style filename prefix so titles
        // read like prose instead of dumping the original filename. The
        // numeric position is conveyed separately via the track_index pill,
        // so the raw prefix is just noise.
        function _stripTrackNumberPrefix(s) {
            if (!s) return s;
            return s
                .replace(/^\s*track\s*\d+\s*[_\-\.\s:]+/i, '')
                .replace(/^\s*\d+\s*[_\-\.\s:]+/, '')
                .trim();
        }

        function trackDisplayLabel(itemId, trackId) {
            if (itemId == null || trackId == null) return null;
            const map = activityTracksCache[itemId];
            if (!map) {
                fetchAndCacheTracks(itemId);
                return null;
            }
            const t = map[trackId];
            if (!t) return null;
            const idx = Number.isInteger(t.track_index) ? String(t.track_index).padStart(2, '0') : null;
            const rawTitle = (t.title_en || t.title || '').trim();
            const title = _stripTrackNumberPrefix(rawTitle);
            const truncated = title.length > 50 ? title.slice(0, 49) + '…' : title;
            if (idx && truncated) return `${idx} · ${truncated}`;
            if (truncated) return truncated;
            if (idx) return `Track ${idx}`;
            return null;
        }

        async function fetchAndCacheItem(itemId) {
            if (itemId == null) return;
            if (activityItemCache[itemId]) return;
            if (activityItemFetchInflight.has(itemId)) return;
            activityItemFetchInflight.add(itemId);
            try {
                const resp = await fetch(`/api/items/${itemId}`);
                if (!resp.ok) return;
                const data = await resp.json();
                activityItemCache[itemId] = {
                    title: String(data.title_en || data.title || '').trim(),
                    code: String(data.product_code || '').trim(),
                };
            } catch (_) {
                // best effort; we'll fall back to "Item #N"
            } finally {
                activityItemFetchInflight.delete(itemId);
            }
        }

        function activityItemTitle(job) {
            const itemId = job?.metadata_json?.item_id;
            if (itemId == null) return `Job #${job?.id ?? '?'}`;
            const cached = items.value.find(i => i.id === itemId);
            if (cached) {
                const t = String(cached.title_en || cached.title || '').trim();
                const code = String(cached.product_code || '').trim();
                if (t && code) return `${code} — ${t}`;
                return t || code || `Item #${itemId}`;
            }
            const fetched = activityItemCache[itemId];
            if (fetched) {
                if (fetched.title && fetched.code) return `${fetched.code} — ${fetched.title}`;
                return fetched.title || fetched.code || `Item #${itemId}`;
            }
            // Kick off lazy fetch and fall back for now; reactivity will swap
            // the label in once the fetch completes.
            fetchAndCacheItem(itemId);
            return `Item #${itemId}`;
        }

        function dismissActivityJob(jobId) {
            const next = new Set(dismissedActivityJobs.value);
            next.add(jobId);
            dismissedActivityJobs.value = next;
            // If the dismissed row was expanded, collapse it.
            if (expandedActivityJob.value === jobId) expandedActivityJob.value = null;
            // User interacted: cancel any pending auto-close so the drawer
            // stays put after a manual dismiss.
            activityDrawerAutoOpened.value = false;
        }

        function dismissAllFinishedActivity() {
            const next = new Set(dismissedActivityJobs.value);
            for (const j of visibleAutopilotJobs.value) {
                if (['completed', 'failed', 'stopped'].includes(j.status)) {
                    next.add(j.id);
                }
            }
            dismissedActivityJobs.value = next;
            // Clearing finished is an explicit user action — don't let
            // auto-close fire after this.
            activityDrawerAutoOpened.value = false;
        }

        async function restartAutopilotJob(jobId) {
            try {
                const resp = await fetch(`/api/pipeline/jobs/${jobId}/restart`, { method: 'POST' });
                if (!resp.ok) {
                    let msg = `Restart failed (${resp.status})`;
                    try { const d = await resp.json(); if (d.detail) msg = d.detail; } catch (_) {}
                    pushAutopilotToast({ kind: 'fail', title: 'Resume failed', body: msg, ttl: 6000 });
                    return;
                }
                // The old row gets dismissed once the new job appears so the drawer
                // stays uncluttered.
                dismissActivityJob(jobId);
                await loadAutopilotJobs();
            } catch (err) {
                pushAutopilotToast({ kind: 'fail', title: 'Resume failed', body: String(err) });
            }
        }

        function activityStatusLabel(job) {
            const s = String(job?.status || '').toLowerCase();
            if (s === 'queued') return 'Queued';
            if (s === 'running') return 'Running';
            if (s === 'paused') return 'Paused';
            if (s === 'stopping') return 'Stopping';
            if (s === 'stopped') return 'Stopped';
            if (s === 'completed') return 'Done';
            if (s === 'failed') return 'Failed';
            return s ? s[0].toUpperCase() + s.slice(1) : 'Unknown';
        }

        function activityStageDisplay(job) {
            const s = String(job?.status || '').toLowerCase();
            const completed = Number(job?.completed || 0);
            const total = Number(job?.total || 0);
            const current = String(job?.current || '').trim();
            const jobLabel = pipelineJobLabel(job);
            const isAutopilot = job?.job_type === 'pipeline_autopilot';

            if (s === 'queued') return `Queued: ${jobLabel}`;
            if (s === 'completed') return isAutopilot ? 'All stages done' : `${jobLabel}: done`;
            if (s === 'failed') return current ? `Failed during: ${current}` : `${jobLabel} failed`;
            if (s === 'stopped') return current ? `Stopped during: ${current}` : `${jobLabel} stopped`;

            if (isAutopilot) {
                const apTotal = total || AUTOPILOT_TOTAL_STAGES;
                const stageNumber = Math.min(apTotal, completed + 1);
                const label = current || AUTOPILOT_STAGE_LABELS[AUTOPILOT_STAGE_ORDER[completed]] || jobLabel;
                return `Stage ${stageNumber}/${apTotal} · ${label}`;
            }
            // Non-autopilot: show job kind plus current sub-task and progress fraction.
            if (current && total) return `${jobLabel} · ${current} (${completed}/${total})`;
            if (current) return `${jobLabel} · ${current}`;
            if (total) return `${jobLabel} · ${completed}/${total}`;
            return jobLabel;
        }

        function activityStagePercent(job) {
            const total = Number(job?.total || 0);
            const completed = Number(job?.completed || 0);
            const s = String(job?.status || '').toLowerCase();
            if (s === 'completed') return 100;
            if (s === 'queued') return 0;
            const isAutopilot = job?.job_type === 'pipeline_autopilot';
            const denom = total || (isAutopilot ? AUTOPILOT_TOTAL_STAGES : 0);
            if (!denom) return s === 'running' ? 5 : 0; // tiny indeterminate hint
            return Math.max(0, Math.min(100, Math.round((completed / denom) * 100)));
        }

        function activityElapsed(job) {
            const start = job?.started_at || job?.created_at;
            if (!start) return '';
            const startMs = Date.parse(start);
            if (Number.isNaN(startMs)) return '';
            const endMs = job?.finished_at ? Date.parse(job.finished_at) : Date.now();
            const seconds = Math.max(0, Math.round((endMs - startMs) / 1000));
            if (seconds < 60) return `${seconds}s`;
            const m = Math.floor(seconds / 60);
            const s = seconds % 60;
            if (m < 60) return `${m}m ${s}s`;
            const h = Math.floor(m / 60);
            return `${h}h ${m % 60}m`;
        }

        function formatActivityTime(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            if (Number.isNaN(d.getTime())) return '';
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }

        function pushAutopilotToast({ kind = 'info', title, body, ttl = 6000 }) {
            const id = ++autopilotToastSeq;
            autopilotToasts.value = [...autopilotToasts.value, { id, kind, title, body }];
            setTimeout(() => dismissAutopilotToast(id), ttl);
        }
        // Generic alias — the toast stack isn't autopilot-only anymore.
        // Small one-shot ops (translate track names, backfill summaries,
        // sibling replication, etc.) use this to surface their result
        // without parking persistent text under the panel.
        function pushToast(opts) {
            return pushAutopilotToast(opts);
        }

        function dismissAutopilotToast(id) {
            autopilotToasts.value = autopilotToasts.value.filter(t => t.id !== id);
        }

        // Compact a "CODE — full title" string for toast bodies. Keeps the
        // product code intact (it's the load-bearing identifier) and clips
        // the long-ass JP/EN title down to a digestible length. Used only
        // for notifications — drawer rows can show full text.
        function truncateForToast(s, maxTitleChars = 28) {
            if (!s) return s;
            const sep = ' — ';
            const sepIdx = s.indexOf(sep);
            if (sepIdx >= 0) {
                const code = s.slice(0, sepIdx);
                const rest = s.slice(sepIdx + sep.length);
                if (rest.length > maxTitleChars) {
                    return `${code} — ${rest.slice(0, maxTitleChars - 1).trimEnd()}…`;
                }
                return s;
            }
            if (s.length > maxTitleChars + 14) {
                return s.slice(0, maxTitleChars + 13).trimEnd() + '…';
            }
            return s;
        }

        function toastForTerminalStatus(job, prevStatus) {
            const status = String(job?.status || '').toLowerCase();
            const TERMINAL = new Set(['completed', 'failed', 'stopped']);
            if (!TERMINAL.has(status)) return;
            // Skip the first-sight toast: if we've never seen this job before
            // (no prevStatus) AND it already loaded as terminal, the user
            // probably saw it earlier — don't toast historical rows on first
            // poll.
            if (prevStatus === undefined) return;
            if (TERMINAL.has(String(prevStatus || '').toLowerCase())) return;
            const itemTitle = truncateForToast(activityItemTitle(job));
            const jobLabel = pipelineJobLabel(job);

            // Per-track jobs (translate) get the specific track in the body
            // so a flood of bulk translations is still readable.
            const itemId = job?.metadata_json?.item_id;
            const trackId = job?.metadata_json?.track_id;
            let body = itemTitle;
            if (job?.job_type === 'pipeline_translate' && itemId != null && trackId != null) {
                const trackLabel = trackDisplayLabel(itemId, trackId);
                if (trackLabel) {
                    // Track label is already short (`02 · 〈first 50 chars〉`).
                    body = `${itemTitle} · ${trackLabel}`;
                }
            }

            if (status === 'completed') {
                pushAutopilotToast({ kind: 'ok', title: `${jobLabel}: done`, body });
            } else if (status === 'failed') {
                pushAutopilotToast({
                    kind: 'fail',
                    title: `${jobLabel} failed`,
                    body: `${body}${job?.error ? ` — ${String(job.error).slice(0, 80)}` : ''}`,
                    ttl: 9000,
                });
            } else if (status === 'stopped') {
                pushAutopilotToast({ kind: 'warn', title: `${jobLabel} stopped`, body });
            }
        }

        async function loadAutopilotJobs() {
            try {
                // The drawer monitors every pipeline_* job (autopilot, extract,
                // transcribe, translate). Backend already filters non-pipeline
                // jobs out for /api/pipeline/jobs.
                const resp = await fetch('/api/pipeline/jobs?limit=50');
                if (!resp.ok) return;
                const data = await resp.json();
                const all = Array.isArray(data?.jobs) ? data.jobs : [];
                // Sort: active first (running/queued), then most recent
                all.sort((a, b) => {
                    const order = { running: 0, queued: 1, paused: 2, stopping: 3, completed: 4, stopped: 5, failed: 6 };
                    const sa = order[String(a.status || '').toLowerCase()] ?? 99;
                    const sb = order[String(b.status || '').toLowerCase()] ?? 99;
                    if (sa !== sb) return sa - sb;
                    const ta = Date.parse(a.created_at || '') || 0;
                    const tb = Date.parse(b.created_at || '') || 0;
                    return tb - ta;
                });

                // Detect status transitions and remember which item_ids
                // had jobs change state this tick. A subsequent live-refresh
                // pass uses that set to repaint Workshop / Player without
                // re-fetching on every poll.
                const transitionedItemIds = new Set();
                for (const job of all) {
                    const prev = autopilotPrevStatuses.get(job.id);
                    if (prev !== job.status) {
                        toastForTerminalStatus(job, prev);
                        autopilotPrevStatuses.set(job.id, job.status);
                        const jobItemId = job?.metadata_json?.item_id;
                        if (jobItemId != null) transitionedItemIds.add(Number(jobItemId));
                    }
                }

                // Workshop refresh — only if the loaded item had a transition.
                const workshopItemId = pipelineSelectedItemId.value ? Number(pipelineSelectedItemId.value) : null;
                if (workshopItemId && transitionedItemIds.has(workshopItemId) && activeTab.value === 'pipeline') {
                    try {
                        if (typeof loadWorkshopTracksForItem === 'function') {
                            await loadWorkshopTracksForItem();
                        }
                        if (typeof loadPipelineRuns === 'function' && pipelineTrackId.value) {
                            await loadPipelineRuns();
                        }
                        // Archive panel reflects unpacked files on disk; an
                        // extraction transition just changed that, so blow
                        // the cache and re-fetch.
                        archiveContentsCache.delete(workshopItemId);
                        if (typeof loadArchiveContents === 'function') {
                            await loadArchiveContents(workshopItemId);
                        }
                    } catch (_) {}
                }

                // Player refresh — same idea: only if the loaded item had a
                // transition AND the user is on the track-picker (no track
                // playing yet, since we don't want to disturb playback).
                const playerActiveItemId = playerItemId.value ? Number(playerItemId.value) : null;
                if (playerActiveItemId && transitionedItemIds.has(playerActiveItemId)
                    && activeTab.value === 'player' && !playerTrackId.value) {
                    try { await loadPlayerItemTracks(); } catch (_) {}
                }

                // Eagerly resolve item titles + tracks so toasts/rows don't
                // flash "Item #N" or "Track #N".
                for (const job of all) {
                    const itemId = job?.metadata_json?.item_id;
                    if (itemId != null) {
                        if (!activityItemCache[itemId] && !items.value.find(i => i.id === itemId)) {
                            fetchAndCacheItem(itemId);
                        }
                        if (job.job_type === 'pipeline_translate' && !activityTracksCache[itemId]) {
                            fetchAndCacheTracks(itemId);
                        }
                    }
                }

                autopilotJobs.value = all;

                // Auto-close if we auto-opened and nothing is active anymore
                if (activityDrawerAutoOpened.value && autopilotActiveCount.value === 0) {
                    setTimeout(() => {
                        if (autopilotActiveCount.value === 0 && activityDrawerAutoOpened.value) {
                            activityDrawerOpen.value = false;
                            activityDrawerAutoOpened.value = false;
                        }
                    }, 3000);
                }

                // Refresh events for the currently expanded row
                if (expandedActivityJob.value) {
                    loadActivityEvents(expandedActivityJob.value);
                }
            } catch (err) {
                console.warn('Autopilot poll failed:', err);
            }
        }

        function startAutopilotPolling() {
            if (autopilotPollTimer) return;
            // Poll once immediately, then every 3s.
            loadAutopilotJobs();
            autopilotPollTimer = setInterval(loadAutopilotJobs, 3000);
        }

        function toggleActivityDrawer() {
            activityDrawerOpen.value = !activityDrawerOpen.value;
            if (activityDrawerOpen.value) {
                // Manual open clears the auto-open flag so we don't auto-close on user.
                activityDrawerAutoOpened.value = false;
                loadAutopilotJobs();
            }
        }

        async function loadActivityEvents(jobId) {
            if (!jobId) return;
            activityEventsLoading[jobId] = true;
            try {
                const resp = await fetch(`/api/pipeline/jobs/${jobId}/events?limit=80`);
                if (!resp.ok) return;
                const data = await resp.json();
                activityEvents[jobId] = Array.isArray(data?.events) ? data.events : [];
            } catch (err) {
                // Silent — drawer is best-effort
            } finally {
                activityEventsLoading[jobId] = false;
            }
        }

        function toggleActivityRowExpand(jobId) {
            if (expandedActivityJob.value === jobId) {
                expandedActivityJob.value = null;
                return;
            }
            expandedActivityJob.value = jobId;
            loadActivityEvents(jobId);
        }

        async function stopAutopilotJob(jobId) {
            try {
                const resp = await fetch(`/api/pipeline/jobs/${jobId}/stop`, { method: 'POST' });
                if (!resp.ok) return;
                await loadAutopilotJobs();
            } catch (err) {
                console.warn('Stop autopilot job failed:', err);
            }
        }

        async function bulkExtractSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;
            if (!confirm(`Queue extraction for ${itemIds.length} item(s)? Each runs as its own job.`)) return;
            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';
            let ok = 0, failed = 0;
            try {
                for (const itemId of itemIds) {
                    try {
                        const resp = await fetch(`/api/pipeline/items/${itemId}/extract`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ force: false }),
                        });
                        if (resp.ok) ok++; else failed++;
                    } catch { failed++; }
                }
                const parts = [`Extraction queued for ${ok} item(s)`];
                if (failed) parts.push(`${failed} failed`);
                bulkMessage.value = parts.join('; ');
            } finally {
                bulkLoading.value = false;
            }
        }

        async function bulkTranscribeSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;
            if (!confirm(`Queue transcription for every track in ${itemIds.length} item(s)? Already-transcribed tracks are skipped.`)) return;
            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';
            let ok = 0, failed = 0;
            try {
                for (const itemId of itemIds) {
                    try {
                        const resp = await fetch(`/api/pipeline/items/${itemId}/auto-transcribe`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ language: 'ja', force: false }),
                        });
                        if (resp.ok) ok++; else failed++;
                    } catch { failed++; }
                }
                const parts = [`Transcription queued for ${ok} item(s)`];
                if (failed) parts.push(`${failed} failed`);
                bulkMessage.value = parts.join('; ');
            } finally {
                bulkLoading.value = false;
            }
        }

        async function bulkRunAutopilotSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;

            const target = String(autoTranslateTargetLanguage.value || 'en');
            const provider = String(autoTranslateProvider.value || '');
            const msg = `Run full workflow (Translating metadata → Unpacking audio → Translating track titles → Transcribing → Translating, target=${target}${provider ? `, provider=${provider}` : ''}) for ${itemIds.length} item(s)?\nEach item is queued as its own job; up to 2 run at a time.`;
            if (!confirm(msg)) return;

            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';

            let queued = 0;
            let failed = 0;
            const jobIds = [];
            try {
                for (const itemId of itemIds) {
                    try {
                        const resp = await fetch(`/api/pipeline/items/${itemId}/autopilot`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                target_language: target,
                                provider: provider || null,
                                model: String(autoTranslateModel.value || '') || null,
                                max_tokens_per_chunk: Number(autoTranslateMaxTokens.value || 1000),
                                max_lines_per_chunk: Number(autoTranslateMaxLines.value || 20),
                                max_retries_per_chunk: Number(autoTranslateMaxRetries.value || 2),
                                retry_backoff_seconds: Number(autoTranslateRetryBackoff.value || 1.0),
                                glossary: String(autoTranslateGlossary.value || ''),
                                character_memory: String(autoTranslateCharacterMemory.value || ''),
                            }),
                        });
                        if (resp.ok) {
                            const data = await resp.json();
                            if (data && data.job_id) jobIds.push(data.job_id);
                            queued++;
                        } else {
                            failed++;
                        }
                    } catch (err) {
                        failed++;
                    }
                }
                const parts = [`Autopilot queued for ${queued} item(s)`];
                if (failed) parts.push(`${failed} failed to queue`);
                if (jobIds.length) parts.push(`jobs: ${jobIds.join(', ')}`);
                bulkMessage.value = parts.join('; ');

                // Auto-open the activity drawer so user sees jobs start. Track that
                // we auto-opened so we can auto-close once everything finishes.
                if (queued > 0) {
                    activityDrawerOpen.value = true;
                    activityDrawerAutoOpened.value = true;
                    loadAutopilotJobs();
                }
            } finally {
                bulkLoading.value = false;
            }
        }

        async function bulkAutoTranslateSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;

            const target = String(autoTranslateTargetLanguage.value || 'en');
            const provider = String(autoTranslateProvider.value || 'gemini');
            const model = String(autoTranslateModel.value || '');
            if (!confirm(`Queue auto-translation (${provider}, target=${target}) for all tracks in ${itemIds.length} item(s)?\nTracks without an active transcript will be skipped.`)) return;

            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';

            let itemsHandled = 0;
            let itemsFailed = 0;
            let tracksQueued = 0;
            let tracksSkipped = 0;
            let tracksFailed = 0;

            try {
                for (const itemId of itemIds) {
                    let tracks = [];
                    try {
                        const r = await fetch(`/api/pipeline/items/${itemId}/tracks`);
                        if (!r.ok) { itemsFailed++; continue; }
                        const data = await r.json();
                        tracks = Array.isArray(data.tracks) ? data.tracks : [];
                    } catch (err) {
                        itemsFailed++;
                        continue;
                    }
                    if (!tracks.length) { itemsHandled++; continue; }

                    for (const tr of tracks) {
                        const trackId = tr && tr.id;
                        if (!trackId) { tracksFailed++; continue; }
                        try {
                            const resp = await fetch(`/api/pipeline/tracks/${trackId}/auto-translate`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    target_language: target,
                                    provider,
                                    model,
                                    max_tokens_per_chunk: Number(autoTranslateMaxTokens.value || 1000),
                                    max_lines_per_chunk: Number(autoTranslateMaxLines.value || 20),
                                    max_retries_per_chunk: Number(autoTranslateMaxRetries.value || 2),
                                    retry_backoff_seconds: Number(autoTranslateRetryBackoff.value || 1.0),
                                    glossary: String(autoTranslateGlossary.value || ''),
                                    character_memory: String(autoTranslateCharacterMemory.value || ''),
                                    set_active: true,
                                    only_if_missing: true,
                                }),
                            });
                            if (resp.ok) {
                                let body = null;
                                try { body = await resp.json(); } catch (_) {}
                                if (body && body.status === 'skipped') {
                                    tracksSkipped++;
                                } else {
                                    tracksQueued++;
                                }
                            } else if (resp.status === 400) {
                                // Most common case: no active transcript. Treat as skip.
                                tracksSkipped++;
                            } else {
                                tracksFailed++;
                            }
                        } catch (err) {
                            tracksFailed++;
                        }
                    }
                    itemsHandled++;
                }

                const parts = [`Queued ${tracksQueued} track(s) across ${itemsHandled} item(s)`];
                if (tracksSkipped) parts.push(`${tracksSkipped} skipped (no active transcript)`);
                if (tracksFailed) parts.push(`${tracksFailed} track(s) failed`);
                if (itemsFailed) parts.push(`${itemsFailed} item(s) failed to load tracks`);
                bulkMessage.value = parts.join('; ');
            } finally {
                bulkLoading.value = false;
            }
        }

        async function bulkAddCustomTagSelected() {
            const itemIds = Array.from(selectedIds.value);
            if (!itemIds.length) return;

            const raw = prompt(`Custom tag to add to ${itemIds.length} selected item(s):`, '');
            if (raw === null) return;
            const tag = raw.trim();
            if (!tag) return;

            bulkLoading.value = true;
            bulkMessage.value = '';
            bulkError.value = '';

            let updatedCount = 0;
            let alreadyHadCount = 0;
            let failCount = 0;

            try {
                for (const itemId of itemIds) {
                    const item = items.value.find(i => i.id === itemId);
                    if (!item) { failCount++; continue; }

                    const current = parseJson(item.custom_tags);
                    if (current.includes(tag)) {
                        alreadyHadCount++;
                        continue;
                    }
                    const next = [...current, tag];

                    try {
                        const resp = await fetch(`/api/items/${itemId}`, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ custom_tags: next }),
                        });
                        if (!resp.ok) { failCount++; continue; }
                        const updated = await resp.json();
                        const idx = items.value.findIndex(i => i.id === updated.id);
                        if (idx >= 0) items.value[idx] = updated;
                        if (selectedItem.value && selectedItem.value.id === updated.id) {
                            selectedItem.value = { ...updated };
                        }
                        updatedCount++;
                    } catch (err) {
                        failCount++;
                    }
                }

                const parts = [`Added "${tag}" to ${updatedCount} item(s)`];
                if (alreadyHadCount) parts.push(`${alreadyHadCount} already had it`);
                if (failCount) parts.push(`${failCount} failed`);
                bulkMessage.value = parts.join('; ');
            } finally {
                bulkLoading.value = false;
            }
        }

        async function loadStats() {
            try {
                const resp = await fetch('/api/stats');
                stats.value = await resp.json();
            } catch (err) {
                console.error('Failed to load stats:', err);
            }
        }

        async function loadSeiyuuList() {
            try {
                const resp = await fetch('/api/seiyuu?lang=' + encodeURIComponent(currentLang.value));
                seiyuuList.value = await resp.json();
            } catch (err) {
                console.error('Failed to load seiyuu:', err);
            }
        }

        async function loadTagList() {
            try {
                const resp = await fetch('/api/tags?lang=' + encodeURIComponent(currentLang.value));
                const data = await resp.json();
                tagList.value = [...data.dlsite_tags, ...data.custom_tags];
            } catch (err) {
                console.error('Failed to load tags:', err);
            }
        }

        async function loadFilterOptions() {
            await Promise.all([loadSeiyuuList(), loadTagList()]);
        }


        function addSeiyuuFilter() {
            const val = selectedSeiyuuOption.value;
            if (!val) return;
            if (!filters.seiyuu.includes(val)) {
                filters.seiyuu.push(val);
            }
            selectedSeiyuuOption.value = '';
            loadItems();
        }

        function removeSeiyuuFilter(name) {
            filters.seiyuu = filters.seiyuu.filter(s => s !== name);
            loadItems();
        }

        function addTagFilter() {
            const val = selectedTagOption.value;
            if (!val) return;
            if (!filters.tag.includes(val)) {
                filters.tag.push(val);
            }
            selectedTagOption.value = '';
            loadItems();
        }

        function removeTagFilter(tag) {
            filters.tag = filters.tag.filter(t => t !== tag);
            loadItems();
        }

        async function refreshSelectedItem() {
            if (!selectedItem.value || !selectedItem.value.id) return;
            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}`);
                if (!resp.ok) return;
                selectedItem.value = await resp.json();
            } catch (err) {
                console.error('Failed to refresh selected item:', err);
            }
        }

        async function onLanguageChange() {
            // Dispatch to the right loader per subtab so the EN/JP toggle
            // works from any subtab. Filter-option dropdowns + selected-item
            // refresh are drama-CD-specific (seiyuu/tag indexes are items-
            // table-only), so we skip those when on games/tokutens.
            const sub = librarySubtab.value;
            if (sub === 'games') {
                await loadGames();
                return;
            }
            await loadItems();
            await loadFilterOptions();
            await refreshSelectedItem();
        }

        // Search with debounce. Dispatches to the right loader based on the
        // active library subtab so the per-subtab search inputs all hit the
        // right list endpoint.
        let searchTimer = null;
        function debouncedSearch() {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                if (librarySubtab.value === 'games') loadGames();
                else loadItems();
            }, 300);
        }


        function normalizePathList(raw) {
            const seen = new Set();
            const normalized = [];
            for (const line of raw.split(/\r?\n/)) {
                const cleaned = line.trim();
                if (!cleaned) continue;
                const key = cleaned.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                normalized.push(cleaned);
            }
            return normalized;
        }

        async function loadScanPaths() {
            scanPathsLoading.value = true;
            scanPathsError.value = '';
            try {
                const resp = await fetch('/api/scan/paths');
                const data = await resp.json();
                scanPaths.value = Array.isArray(data.paths) ? data.paths : [];
                scanPathsInput.value = scanPaths.value.join('\n');
            } catch (err) {
                scanPathsError.value = 'Failed to load scan paths';
                console.error('Failed to load scan paths:', err);
            } finally {
                scanPathsLoading.value = false;
            }
        }

        async function saveScanPaths() {
            scanPathsSaving.value = true;
            scanPathsError.value = '';
            scanPathsSuccess.value = '';

            const parsed = normalizePathList(scanPathsInput.value);
            if (!parsed.length) {
                scanPathsSaving.value = false;
                scanPathsError.value = 'At least one path is required';
                return;
            }

            try {
                const resp = await fetch('/api/scan/paths', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: parsed }),
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    scanPathsError.value = err.detail || 'Failed to save scan paths';
                    return;
                }

                const data = await resp.json();
                scanPaths.value = Array.isArray(data.paths) ? data.paths : parsed;
                scanPathsInput.value = scanPaths.value.join('\n');
                scanPathsSuccess.value = 'Scan paths saved';
            } catch (err) {
                scanPathsError.value = 'Failed to save scan paths';
                console.error('Failed to save scan paths:', err);
            } finally {
                scanPathsSaving.value = false;
            }
        }

        // === Games wing: scan paths + scanner trigger + list loader ===
        async function loadGamesScanPaths() {
            gamesScanPathsLoading.value = true;
            gamesScanPathsError.value = '';
            try {
                const resp = await fetch('/api/games/scan/paths');
                const data = await resp.json();
                gamesScanPaths.value = Array.isArray(data.paths) ? data.paths : [];
                gamesScanPathsInput.value = gamesScanPaths.value.join('\n');
            } catch (err) {
                gamesScanPathsError.value = 'Failed to load games scan paths';
                console.error('Failed to load games scan paths:', err);
            } finally {
                gamesScanPathsLoading.value = false;
            }
        }

        async function saveGamesScanPaths() {
            gamesScanPathsSaving.value = true;
            gamesScanPathsError.value = '';
            gamesScanPathsSuccess.value = '';
            // Empty input is valid — clears the games library config.
            const parsed = normalizePathList(gamesScanPathsInput.value);
            try {
                const resp = await fetch('/api/games/scan/paths', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: parsed }),
                });
                if (!resp.ok) {
                    const err = await resp.json();
                    gamesScanPathsError.value = err.detail || 'Failed to save games scan paths';
                    return;
                }
                const data = await resp.json();
                gamesScanPaths.value = Array.isArray(data.paths) ? data.paths : parsed;
                gamesScanPathsInput.value = gamesScanPaths.value.join('\n');
                gamesScanPathsSuccess.value = 'Games scan paths saved';
            } catch (err) {
                gamesScanPathsError.value = 'Failed to save games scan paths';
                console.error('Failed to save games scan paths:', err);
            } finally {
                gamesScanPathsSaving.value = false;
            }
        }

        async function scanGames() {
            gamesScanning.value = true;
            gamesScanPathsError.value = '';
            gamesScanPathsSuccess.value = '';
            try {
                const resp = await fetch('/api/games/scan', { method: 'POST' });
                if (!resp.ok) {
                    const err = await resp.json();
                    gamesScanPathsError.value = err.detail || 'Games scan failed';
                    return;
                }
                const data = await resp.json();
                const created = data.created ?? 0;
                const discovered = data.discovered ?? 0;
                gamesScanPathsSuccess.value = `Scan complete: ${created} new, ${discovered} total discovered`;
                if (librarySubtab.value === 'games') {
                    await loadGames();
                }
            } catch (err) {
                gamesScanPathsError.value = 'Games scan failed';
                console.error('Games scan failed:', err);
            } finally {
                gamesScanning.value = false;
            }
        }

        // Tokuten scan paths — same shape as games. Catalog-only walk over
        // configured roots; each top-level folder or archive becomes a stub
        // tokuten + paired items row. Idempotent via a path-derived TKS-
        // synthetic product_code.
        async function loadTokutenScanPaths() {
            tokutenScanPathsLoading.value = true;
            tokutenScanPathsError.value = '';
            try {
                const resp = await fetch('/api/tokutens/scan/paths');
                const data = await resp.json();
                tokutenScanPaths.value = Array.isArray(data.paths) ? data.paths : [];
                tokutenScanPathsInput.value = tokutenScanPaths.value.join('\n');
            } catch (err) {
                tokutenScanPathsError.value = 'Failed to load tokuten scan paths';
                console.error('Failed to load tokuten scan paths:', err);
            } finally {
                tokutenScanPathsLoading.value = false;
            }
        }
        async function saveTokutenScanPaths() {
            tokutenScanPathsSaving.value = true;
            tokutenScanPathsError.value = '';
            tokutenScanPathsSuccess.value = '';
            const parsed = normalizePathList(tokutenScanPathsInput.value);
            try {
                const resp = await fetch('/api/tokutens/scan/paths', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: parsed }),
                });
                if (!resp.ok) {
                    const err = await resp.json();
                    tokutenScanPathsError.value = err.detail || 'Failed to save tokuten scan paths';
                    return;
                }
                const data = await resp.json();
                tokutenScanPaths.value = Array.isArray(data.paths) ? data.paths : parsed;
                tokutenScanPathsInput.value = tokutenScanPaths.value.join('\n');
                tokutenScanPathsSuccess.value = 'Tokuten scan paths saved';
            } catch (err) {
                tokutenScanPathsError.value = 'Failed to save tokuten scan paths';
                console.error('Failed to save tokuten scan paths:', err);
            } finally {
                tokutenScanPathsSaving.value = false;
            }
        }
        async function scanTokutens() {
            tokutenScanning.value = true;
            tokutenScanPathsError.value = '';
            tokutenScanPathsSuccess.value = '';
            try {
                const resp = await fetch('/api/tokutens/scan', { method: 'POST' });
                if (!resp.ok) {
                    const err = await resp.json();
                    tokutenScanPathsError.value = err.detail || 'Tokuten scan failed';
                    return;
                }
                const data = await resp.json();
                const created = data.created ?? 0;
                const discovered = data.discovered ?? 0;
                tokutenScanPathsSuccess.value = `Scan complete: ${created} new, ${discovered} total discovered`;
                if (librarySubtab.value === 'tokutens') {
                    await loadItems();
                    loadTokutenStats();
                }
            } catch (err) {
                tokutenScanPathsError.value = 'Tokuten scan failed';
                console.error('Tokuten scan failed:', err);
            } finally {
                tokutenScanning.value = false;
            }
        }

        async function loadGames() {
            gamesLoading.value = true;
            try {
                const params = new URLSearchParams();
                params.set('limit', '500');
                params.set('offset', '0');
                if (gameSearchQuery.value) params.set('search', gameSearchQuery.value);
                if (gameFilters.favorite) params.set('favorite', 'true');
                if (gameFilters.play_status) params.set('play_status', gameFilters.play_status);
                if (gameFilters.platform) params.set('platform', gameFilters.platform);
                if (gameFilters.developer) params.set('developer', gameFilters.developer);
                if (gameFilters.custom_tag) params.set('custom_tag', gameFilters.custom_tag);
                if (gameFilters.matched === true) params.set('matched', 'true');
                else if (gameFilters.matched === false) params.set('matched', 'false');
                if (gameFilters.is_manual === true) params.set('is_manual', 'true');
                else if (gameFilters.is_manual === false) params.set('is_manual', 'false');
                if (gameFilters.include_wishlist || gameFilters.play_status === 'wishlist') {
                    params.set('include_wishlist', 'true');
                }
                const resp = await fetch('/api/games?' + params.toString());
                const data = await resp.json();
                gamesItems.value = Array.isArray(data.items) ? data.items : [];
                gamesTotal.value = data.total_items || gamesItems.value.length;
            } catch (err) {
                console.error('Failed to load games:', err);
                gamesItems.value = [];
                gamesTotal.value = 0;
            } finally {
                gamesLoading.value = false;
            }
        }

        async function openGameFolder(game) {
            try {
                const resp = await fetch(`/api/games/${game.id}/open-folder`, { method: 'POST' });
                if (!resp.ok) {
                    const err = await resp.json();
                    console.error('Open folder failed:', err);
                }
            } catch (err) {
                console.error('Open folder failed:', err);
            }
        }

        async function toggleGameFavorite(game) {
            const next = !game.favorite;
            // Optimistic update so the heart fills immediately. Revert on failure.
            game.favorite = next;
            try {
                const resp = await fetch(`/api/games/${game.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ favorite: next }),
                });
                if (!resp.ok) {
                    game.favorite = !next;
                }
            } catch (err) {
                console.error('Toggle game favorite failed:', err);
                game.favorite = !next;
            }
        }

        // Play-status quick-dropdown state — `playStatusDropdownFor` is the
        // game id whose dropdown is currently open ('detail' for the detail
        // panel pill), or null when no dropdown is open. The CSS dropdown is
        // a fixed-positioned element absolutely placed next to the pill the
        // user clicked, so we also stash its anchor rect for placement.
        const playStatusDropdownFor = ref(null);
        const playStatusDropdownRect = ref(null);
        const PLAY_STATUS_OPTIONS = ['backlog', 'want_to_play', 'playing', 'completed', 'on_hold', 'dropped', 'wishlist'];
        const PLAY_STATUS_LABELS = {
            backlog: 'Backlog',
            want_to_play: 'Want to play',
            playing: 'Playing',
            completed: 'Completed',
            on_hold: 'On hold',
            dropped: 'Dropped',
            wishlist: 'Wishlist',
        };
        function formatPlayStatus(s) {
            if (!s) return '';
            return PLAY_STATUS_LABELS[s] || String(s).replace(/_/g, ' ');
        }
        function openPlayStatusDropdown(target, game, ev) {
            // `target` is either a game id (number) or the string 'detail'.
            playStatusDropdownFor.value = target;
            if (ev && ev.currentTarget) {
                const r = ev.currentTarget.getBoundingClientRect();
                playStatusDropdownRect.value = {
                    top: r.bottom + 4,
                    left: r.left,
                };
            }
            // Stash the game ref alongside the dropdown state so the menu
            // items can call setGamePlayStatus(game, status) directly.
            _playStatusDropdownGame = game;
        }
        let _playStatusDropdownGame = null;
        function selectPlayStatusOption(status) {
            const g = _playStatusDropdownGame;
            playStatusDropdownFor.value = null;
            _playStatusDropdownGame = null;
            if (!g) return;
            setGamePlayStatus(g, status);
        }
        function closePlayStatusDropdown() {
            playStatusDropdownFor.value = null;
            _playStatusDropdownGame = null;
        }

        // Listen-status quick-dropdown — drama-CD parallel to play-status.
        // Same fixed-positioned popup, same click-outside dismissal.
        const listenStatusDropdownFor = ref(null);
        const listenStatusDropdownRect = ref(null);
        const LISTEN_STATUS_OPTIONS = ['backlog', 'want_to_listen', 'listening', 'completed', 'on_hold', 'dropped', 'wishlist'];
        const LISTEN_STATUS_LABELS = {
            backlog: 'Backlog',
            want_to_listen: 'Want to listen',
            listening: 'Listening',
            completed: 'Finished',
            on_hold: 'On hold',
            dropped: 'Dropped',
            wishlist: 'Wishlist',
        };
        function formatListenStatus(s) {
            if (!s) return formatListenStatus('backlog');
            return LISTEN_STATUS_LABELS[s] || String(s).replace(/_/g, ' ');
        }
        let _listenStatusDropdownItem = null;
        function openListenStatusDropdown(target, item, ev) {
            listenStatusDropdownFor.value = target;
            if (ev && ev.currentTarget) {
                const r = ev.currentTarget.getBoundingClientRect();
                listenStatusDropdownRect.value = {
                    top: r.bottom + 4,
                    left: r.left,
                };
            }
            _listenStatusDropdownItem = item;
        }
        function selectListenStatusOption(status) {
            const it = _listenStatusDropdownItem;
            listenStatusDropdownFor.value = null;
            _listenStatusDropdownItem = null;
            if (!it) return;
            setItemListenStatus(it, status);
        }
        function closeListenStatusDropdown() {
            listenStatusDropdownFor.value = null;
            _listenStatusDropdownItem = null;
        }

        // Patch listen_status on a drama-CD item. Optimistic update with
        // rollback on HTTP failure; also mirrors onto selectedItem so the
        // detail panel reflects the new value immediately.
        async function setItemListenStatus(item, nextStatus) {
            if (!item || !nextStatus) return;
            const prev = item.listen_status || 'backlog';
            if (prev === nextStatus) return;
            item.listen_status = nextStatus;
            try {
                const resp = await fetch(`/api/items/${item.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ listen_status: nextStatus }),
                });
                if (!resp.ok) {
                    item.listen_status = prev;
                    return;
                }
                const updated = await resp.json();
                Object.assign(item, updated);
                if (selectedItem.value && selectedItem.value.id === item.id) {
                    selectedItem.value = { ...selectedItem.value, ...updated };
                }
                // Refresh visible list so a filter-active grid drops/adds
                // the row, and stats sub-pill counts stay accurate.
                if (filters.listen_status) loadItems();
                loadStats();
            } catch (err) {
                item.listen_status = prev;
                console.error('Set listen_status failed:', err);
            }
        }

        // Direct play_status mutation (used by the read-mode dropdown on the
        // game card pill and the detail panel pill — see task #6). Patches
        // the row + refreshes the games-stats counts so the sub-pills stay
        // accurate. Wishlist promotions auto-flip include_wishlist so the
        // row stays visible after the click.
        async function setGamePlayStatus(game, nextStatus) {
            if (!game || !nextStatus) return;
            const prev = game.play_status;
            if (prev === nextStatus) return;
            game.play_status = nextStatus;
            try {
                const resp = await fetch(`/api/games/${game.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ play_status: nextStatus }),
                });
                if (!resp.ok) {
                    game.play_status = prev;
                    return;
                }
                const updated = await resp.json();
                Object.assign(game, updated);
                if (selectedGame.value && selectedGame.value.id === game.id) {
                    selectedGame.value = { ...selectedGame.value, ...updated };
                }
                // Refresh the visible games list so the row drops out (or
                // joins) the current filtered view. e.g. flipping to
                // 'wishlist' hides the row from the default grid because
                // wishlist is excluded unless include_wishlist is on; the
                // play_status filter pills also re-evaluate.
                loadGames();
                loadGameStats();
            } catch (err) {
                game.play_status = prev;
                console.error('Set play_status failed:', err);
            }
        }

        // === Unmatched games cleanup queue (task #8 / 39 unmatched games) ===
        // The Unmatched stat-pill click opens a focused overlay that walks one
        // unmatched game at a time, exposing VNDB search inline so the user
        // can either accept a match, skip, or delete a row that turned out
        // not to be a game.
        async function loadUnmatchedQueue() {
            unmatchedQueueLoading.value = true;
            unmatchedQueueResults.value = [];
            unmatchedQueueSearch.value = '';
            try {
                const resp = await fetch('/api/games?matched=false&limit=2000');
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const data = await resp.json();
                unmatchedQueueItems.value = (data.items || []).filter(
                    g => (g.title || '').trim() && (g.title || '').trim() !== '[New Game]'
                );
                unmatchedQueueIndex.value = 0;
                if (unmatchedQueueItems.value.length) {
                    await runUnmatchedQueueSearch();
                }
            } catch (err) {
                unmatchedQueueError.value = 'Failed to load unmatched games: ' + (err.message || err);
            } finally {
                unmatchedQueueLoading.value = false;
            }
        }
        function closeUnmatchedQueue() {
            unmatchedQueueOpen.value = false;
            unmatchedQueueResults.value = [];
            unmatchedQueueManualMode.value = false;
            unmatchedQueueManualDraft.value = null;
        }
        // Switch the right pane from VNDB search to a manual edit form for
        // the current game. Staging fields from the row itself so the user
        // edits in place (rather than starting empty).
        function startUnmatchedManual() {
            const cur = unmatchedQueueCurrent();
            if (!cur) return;
            unmatchedQueueManualDraft.value = {
                title: cur.title || '',
                title_en: cur.title_en || '',
                title_jp: cur.title_jp || '',
                developer: cur.developer || '',
                release_date: cur.release_date || '',
                description: cur.description || '',
            };
            unmatchedQueueManualMode.value = true;
            unmatchedQueueError.value = '';
        }
        function cancelUnmatchedManual() {
            unmatchedQueueManualMode.value = false;
            unmatchedQueueManualDraft.value = null;
        }
        async function saveUnmatchedManual(advance) {
            const cur = unmatchedQueueCurrent();
            const d = unmatchedQueueManualDraft.value;
            if (!cur || !d) return;
            unmatchedQueueBusy.value = true;
            try {
                const body = {
                    title: d.title || cur.title || '',
                    title_en: d.title_en || null,
                    title_jp: d.title_jp || null,
                    developer: d.developer || null,
                    release_date: d.release_date || null,
                    description: d.description || null,
                    // Mark the row as "I reviewed this, no VNDB entry needed"
                    // so the cleanup queue + Unmatched stat drop it.
                    vndb_searched: true,
                };
                const resp = await fetch(`/api/games/${cur.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    unmatchedQueueError.value = err.detail || 'Save failed';
                    return;
                }
                const updated = await resp.json();
                // Reflect changes locally so the left-pane preview updates if
                // the user navigates Prev/Next afterward.
                Object.assign(cur, updated);
                unmatchedQueueMessage.value = `Saved: ${cur.title || 'game'}`;
                if (advance) {
                    // Drop the row from the queue — the user has decided
                    // there's no VNDB match and the fields are filled in,
                    // so it's "cleaned up" as far as this queue is concerned.
                    const idx = unmatchedQueueIndex.value;
                    unmatchedQueueItems.value.splice(idx, 1);
                    if (unmatchedQueueIndex.value >= unmatchedQueueItems.value.length) {
                        unmatchedQueueIndex.value = Math.max(0, unmatchedQueueItems.value.length - 1);
                    }
                    unmatchedQueueManualMode.value = false;
                    unmatchedQueueManualDraft.value = null;
                    _resetUnmatchedSearchState();
                    await runUnmatchedQueueSearch();
                    loadGames();
                    loadGameStats();
                    loadGameDistinctOptions();
                }
            } catch (err) {
                unmatchedQueueError.value = 'Save failed: ' + (err.message || err);
            } finally {
                unmatchedQueueBusy.value = false;
            }
        }
        // Cover upload inside the cleanup queue — mirrors the games detail
        // panel's chooseGameCover/onGameCoverFileChange pair but scoped to
        // the queue's current row. Read-as-data-URL → PUT /api/games/{id}/cover.
        function chooseUnmatchedCover() {
            if (unmatchedQueueCoverInput.value) {
                unmatchedQueueCoverInput.value.click();
            }
        }
        async function onUnmatchedCoverFileChange(ev) {
            const cur = unmatchedQueueCurrent();
            const file = ev.target.files && ev.target.files[0];
            if (!cur || !file) return;
            try {
                const reader = new FileReader();
                const dataUrl = await new Promise((resolve, reject) => {
                    reader.onload = () => resolve(reader.result);
                    reader.onerror = reject;
                    reader.readAsDataURL(file);
                });
                const resp = await fetch(`/api/games/${cur.id}/cover`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, data_url: dataUrl }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    unmatchedQueueError.value = err.detail || 'Cover upload failed';
                    return;
                }
                const updated = await resp.json();
                Object.assign(cur, updated);
                unmatchedQueueMessage.value = 'Cover uploaded';
            } catch (err) {
                unmatchedQueueError.value = 'Cover upload failed: ' + (err.message || err);
            } finally {
                if (ev.target) ev.target.value = '';
            }
        }
        function unmatchedQueueCurrent() {
            const idx = unmatchedQueueIndex.value;
            const arr = unmatchedQueueItems.value;
            return idx >= 0 && idx < arr.length ? arr[idx] : null;
        }
        let _unmatchedSearchTimer = null;
        function unmatchedQueueSearchInput(value) {
            unmatchedQueueSearch.value = value;
            if (_unmatchedSearchTimer) clearTimeout(_unmatchedSearchTimer);
            const q = (value || '').trim();
            if (!q) {
                unmatchedQueueResults.value = [];
                return;
            }
            _unmatchedSearchTimer = setTimeout(() => runUnmatchedQueueSearch(q), 350);
        }
        async function runUnmatchedQueueSearch(qOverride) {
            // Default: search by the current game's title. Custom search box
            // overrides this (the user often needs to try alternate spellings).
            let q = qOverride;
            if (q == null) q = unmatchedQueueSearch.value;
            if (!q) {
                const cur = unmatchedQueueCurrent();
                q = cur ? (cur.title || '') : '';
            }
            q = (q || '').trim();
            if (!q) {
                unmatchedQueueResults.value = [];
                return;
            }
            unmatchedQueueSearching.value = true;
            try {
                const resp = await fetch('/api/games/vndb/search?q=' + encodeURIComponent(q));
                if (!resp.ok) {
                    unmatchedQueueResults.value = [];
                    return;
                }
                const data = await resp.json();
                unmatchedQueueResults.value = data.results || [];
            } catch (err) {
                unmatchedQueueResults.value = [];
            } finally {
                unmatchedQueueSearching.value = false;
            }
        }
        async function acceptUnmatchedVndbResult(candidate) {
            const cur = unmatchedQueueCurrent();
            if (!cur || !candidate || !candidate.id) return;
            unmatchedQueueBusy.value = true;
            try {
                // Fetch full VN record so we get the cover URL + description.
                const fullResp = await fetch('/api/games/vndb/' + encodeURIComponent(candidate.id));
                if (!fullResp.ok) {
                    unmatchedQueueError.value = 'VNDB lookup failed';
                    return;
                }
                const fields = await fullResp.json();
                // Merge: only positive VNDB values overwrite empty row fields.
                const patch = { vndb_id: fields.vndb_id || candidate.id };
                for (const [k, v] of Object.entries(fields)) {
                    if (k === 'vndb_id') continue;
                    if (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)) continue;
                    if (k === 'cover_url') { patch.cover_url = v; continue; }
                    const existing = cur[k];
                    const empty = existing == null || existing === '' || (Array.isArray(existing) && !existing.length);
                    if (empty) patch[k] = v;
                }
                const patchResp = await fetch(`/api/games/${cur.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(patch),
                });
                if (!patchResp.ok) {
                    const err = await patchResp.json().catch(() => ({}));
                    unmatchedQueueError.value = err.detail || 'Patch failed';
                    return;
                }
                // Remove this row from the queue (it's matched now). Don't
                // bump the index — the next item shifts into the current slot.
                const idx = unmatchedQueueIndex.value;
                unmatchedQueueItems.value.splice(idx, 1);
                if (unmatchedQueueIndex.value >= unmatchedQueueItems.value.length) {
                    unmatchedQueueIndex.value = Math.max(0, unmatchedQueueItems.value.length - 1);
                }
                unmatchedQueueMessage.value = `Matched: ${cur.title}`;
                _resetUnmatchedSearchState();
                await runUnmatchedQueueSearch();
                loadGames();
                loadGameStats();
                loadGameDistinctOptions();
            } catch (err) {
                unmatchedQueueError.value = 'Accept failed: ' + (err.message || err);
            } finally {
                unmatchedQueueBusy.value = false;
            }
        }
        function _resetUnmatchedSearchState() {
            // Wipe the search box + previous results so the auto-search for
            // the newly active row starts from a clean slate. Without this,
            // a custom query typed for game A would persist and override
            // game B's title-based default lookup.
            unmatchedQueueSearch.value = '';
            unmatchedQueueResults.value = [];
        }
        function skipUnmatched() {
            if (unmatchedQueueIndex.value < unmatchedQueueItems.value.length - 1) {
                unmatchedQueueIndex.value += 1;
                unmatchedQueueManualMode.value = false;
                unmatchedQueueManualDraft.value = null;
                _resetUnmatchedSearchState();
                runUnmatchedQueueSearch();
            }
        }
        function prevUnmatched() {
            if (unmatchedQueueIndex.value > 0) {
                unmatchedQueueIndex.value -= 1;
                unmatchedQueueManualMode.value = false;
                unmatchedQueueManualDraft.value = null;
                _resetUnmatchedSearchState();
                runUnmatchedQueueSearch();
            }
        }
        async function deleteUnmatched() {
            const cur = unmatchedQueueCurrent();
            if (!cur) return;
            unmatchedQueueBusy.value = true;
            try {
                const resp = await fetch(`/api/games/${cur.id}`, { method: 'DELETE' });
                if (!resp.ok) {
                    unmatchedQueueError.value = 'Delete failed';
                    return;
                }
                const idx = unmatchedQueueIndex.value;
                unmatchedQueueItems.value.splice(idx, 1);
                if (unmatchedQueueIndex.value >= unmatchedQueueItems.value.length) {
                    unmatchedQueueIndex.value = Math.max(0, unmatchedQueueItems.value.length - 1);
                }
                unmatchedQueueMessage.value = `Deleted: ${cur.title}`;
                _resetUnmatchedSearchState();
                await runUnmatchedQueueSearch();
                loadGames();
                loadGameStats();
            } catch (err) {
                unmatchedQueueError.value = 'Delete failed: ' + (err.message || err);
            } finally {
                unmatchedQueueBusy.value = false;
            }
        }

        async function openScanPathsPanel() {
            showScanPathsPanel.value = !showScanPathsPanel.value;
            scanPathsSuccess.value = '';
            if (showScanPathsPanel.value) {
                await loadScanPaths();
            }
        }

        async function loadMaintenancePreview() {
            maintenanceLoading.value = true;
            maintenanceError.value = '';
            try {
                const resp = await fetch('/api/maintenance/integrity');
                maintenancePreview.value = await resp.json();
            } catch (err) {
                maintenanceError.value = 'Failed to load maintenance preview';
                console.error('Failed to load maintenance preview:', err);
            } finally {
                maintenanceLoading.value = false;
            }
        }

        async function runCleanupStaleCovers(dryRun = true) {
            maintenanceActionLoading.value = true;
            maintenanceError.value = '';
            maintenanceMessage.value = '';
            try {
                const resp = await fetch(`/api/maintenance/cleanup-stale-covers?dry_run=${dryRun ? 'true' : 'false'}`, {
                    method: 'POST',
                });
                const data = await resp.json();
                if (!resp.ok) {
                    maintenanceError.value = data.detail || 'Cleanup failed';
                    return;
                }
                if (dryRun) {
                    maintenanceMessage.value = `Preview: ${data.candidate_count} stale cover file(s) can be removed`;
                } else {
                    maintenanceMessage.value = `Cleanup complete: deleted ${data.deleted_count}, failed ${data.failed_count}`;
                }
                await loadMaintenancePreview();
                await loadStats();
            } catch (err) {
                maintenanceError.value = 'Cleanup failed';
                console.error('Cleanup failed:', err);
            } finally {
                maintenanceActionLoading.value = false;
            }
        }

        async function rebuildMetadataIndexes() {
            maintenanceActionLoading.value = true;
            maintenanceError.value = '';
            maintenanceMessage.value = '';
            try {
                const resp = await fetch('/api/maintenance/rebuild-indexes', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    maintenanceError.value = data.detail || 'Rebuild failed';
                    return;
                }
                maintenanceMessage.value = `Rebuild complete: processed ${data.processed_items} item(s)`;
                maintenancePreview.value = data.post_rebuild_report || maintenancePreview.value;
                await loadStats();
            } catch (err) {
                maintenanceError.value = 'Rebuild failed';
                console.error('Rebuild failed:', err);
            } finally {
                maintenanceActionLoading.value = false;
            }
        }

        async function recomputeTranslationStatus() {
            maintenanceActionLoading.value = true;
            maintenanceError.value = '';
            maintenanceMessage.value = '';
            try {
                const resp = await fetch('/api/maintenance/recompute-translation-status', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    maintenanceError.value = data.detail || 'Recompute failed';
                    return;
                }
                const c = data.counts || {};
                maintenanceMessage.value = `Recomputed ${data.items} item(s): translated=${c.translated || 0}, transcribed=${c.transcribed || 0}, not=${c.not_translated || 0}`;
                await loadItems();
            } catch (err) {
                maintenanceError.value = 'Recompute failed';
                console.error('Recompute failed:', err);
            } finally {
                maintenanceActionLoading.value = false;
            }
        }

        async function backfillActiveTranscripts() {
            backfillTranscriptsBusy.value = true;
            backfillTranscriptsMessage.value = '';
            backfillTranscriptsError.value = '';
            try {
                const resp = await fetch('/api/maintenance/backfill-active-transcripts', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    backfillTranscriptsError.value = data.detail || 'Repair failed';
                    return;
                }
                const n = Number(data.tracks_fixed || 0);
                backfillTranscriptsMessage.value = n
                    ? `Repaired ${n} sibling track(s). The Player should now load their transcripts.`
                    : 'Nothing to repair — every transcribed track already has an active transcript.';
            } catch (err) {
                backfillTranscriptsError.value = 'Repair failed';
                console.error('Backfill active transcripts failed:', err);
            } finally {
                backfillTranscriptsBusy.value = false;
            }
        }

        async function backfillSiblingTranslations() {
            backfillTranslationsBusy.value = true;
            backfillTranslationsMessage.value = '';
            backfillTranslationsError.value = '';
            try {
                const resp = await fetch('/api/maintenance/backfill-sibling-translations', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    backfillTranslationsError.value = data.detail || 'Replicate failed';
                    return;
                }
                const created = Number(data.sibling_runs_created || 0);
                const examined = Number(data.runs_examined || 0);
                backfillTranslationsMessage.value = created
                    ? `Replicated ${created} sibling translation(s) across ${examined} original run(s).`
                    : `Nothing to replicate — every translation already covers its siblings (${examined} original runs examined).`;
            } catch (err) {
                backfillTranslationsError.value = 'Replicate failed';
                console.error('Sibling translations backfill failed:', err);
            } finally {
                backfillTranslationsBusy.value = false;
            }
        }

        async function exportTranscripts() {
            transcriptIoBusy.value = 'export';
            transcriptIoMessage.value = '';
            transcriptIoError.value = '';
            transcriptIoSummary.value = null;
            try {
                const resp = await fetch('/api/pipeline/export');
                if (!resp.ok) {
                    const text = await resp.text();
                    transcriptIoError.value = `Export failed (${resp.status}): ${text.slice(0, 200)}`;
                    return;
                }
                const blob = await resp.blob();
                const disposition = resp.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^"]+)"?/i);
                const filename = match ? match[1] : `dramacd-export-${Date.now()}.json`;
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                transcriptIoMessage.value = `Exported ${filename}`;
            } catch (err) {
                transcriptIoError.value = `Export failed: ${err.message || err}`;
                console.error('Export failed:', err);
            } finally {
                transcriptIoBusy.value = '';
            }
        }

        function triggerImportTranscripts() {
            transcriptIoMessage.value = '';
            transcriptIoError.value = '';
            transcriptIoSummary.value = null;
            const input = transcriptIoFileInput.value;
            if (input) {
                input.value = '';
                input.click();
            }
        }

        async function onImportTranscriptsFile(event) {
            const file = event && event.target && event.target.files && event.target.files[0];
            if (!file) return;
            transcriptIoBusy.value = 'import';
            transcriptIoMessage.value = '';
            transcriptIoError.value = '';
            transcriptIoSummary.value = null;
            try {
                const lowerName = (file.name || '').toLowerCase();
                const isZipName = lowerName.endsWith('.zip');
                const isZipType = (file.type || '').toLowerCase().includes('zip');
                const treatAsZip = (isZipName || isZipType) && transcriptIoAcceptZip.value;
                if ((isZipName || isZipType) && !transcriptIoAcceptZip.value) {
                    transcriptIoError.value = 'This looks like a package zip. Tick "Accept package zip" first, or pick a JSON file.';
                    return;
                }
                const replaceFlag = transcriptIoReplace.value ? 'true' : 'false';
                let resp;
                if (treatAsZip) {
                    const buf = await file.arrayBuffer();
                    resp = await fetch(`/api/pipeline/import-package?replace_existing=${replaceFlag}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/zip' },
                        body: buf,
                    });
                } else {
                    const text = await file.text();
                    resp = await fetch(`/api/pipeline/import?replace_existing=${replaceFlag}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: text,
                    });
                }
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    transcriptIoError.value = data.detail || `Import failed (${resp.status})`;
                    return;
                }
                transcriptIoSummary.value = data.summary || null;
                const s = data.summary || {};
                transcriptIoMessage.value = `Import complete: ${s.transcript_runs_created || 0} transcript run(s), ${s.translation_runs_created || 0} translation run(s) created`;
            } catch (err) {
                transcriptIoError.value = `Import failed: ${err.message || err}`;
                console.error('Import failed:', err);
            } finally {
                transcriptIoBusy.value = '';
            }
        }

        function formatBytes(n) {
            const v = Number(n) || 0;
            if (v < 1024) return `${v} B`;
            if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
            if (v < 1024 * 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`;
            return `${(v / 1024 / 1024 / 1024).toFixed(2)} GB`;
        }

        async function openItemAudioFolder() {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                packageError.value = 'No item selected';
                return;
            }
            packageMessage.value = '';
            packageError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/open-folder`, { method: 'POST' });
                if (!resp.ok) {
                    let msg = `Open failed (${resp.status})`;
                    try {
                        const data = await resp.json();
                        if (data && data.detail) msg = data.detail;
                    } catch (_) {}
                    packageError.value = msg;
                    return;
                }
                const data = await resp.json();
                packageMessage.value = `Opened ${data.path}`;
            } catch (err) {
                packageError.value = `Open failed: ${err.message || err}`;
            }
        }

        // Archive panel export kebab + presets. Hardcoded for now — the
        // long-form Package & Workspace card is still around for power users,
        // but the common cases are one-click via these presets.
        const archiveExportMenuOpen = ref(false);
        function closeArchiveExportMenu() {
            archiveExportMenuOpen.value = false;
        }
        const ARCHIVE_EXPORT_PRESETS = {
            // User's personal config: audio + original folder structure + SRT +
            // tracklist + everything else from the archive (covers / booklets).
            // No TXT because the tracklist already ships a TXT.
            as_release: { runs: 'active', audio: '1', preserve_paths: '1', srt: '1', txt: '0', tracklist: '1', all_files: '1' },
            // Just the subtitle files. No audio, no archive contents.
            subtitles_only: { runs: 'active', audio: '0', preserve_paths: '0', srt: '1', txt: '1', tracklist: '1', all_files: '0' },
            // Everything we know how to ship.
            full_package: { runs: 'active', audio: '1', preserve_paths: '1', srt: '1', txt: '1', tracklist: '1', all_files: '1' },
        };
        async function exportPackagePreset(presetKey) {
            const preset = ARCHIVE_EXPORT_PRESETS[presetKey];
            if (!preset) return;
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                packageError.value = 'No item selected';
                return;
            }
            archiveExportMenuOpen.value = false;
            packageBusy.value = true;
            packageMessage.value = '';
            packageError.value = '';
            try {
                const params = new URLSearchParams(preset);
                const resp = await fetch(`/api/pipeline/items/${itemId}/package.zip?${params}`);
                if (!resp.ok) {
                    const text = await resp.text();
                    packageError.value = `Build failed (${resp.status}): ${text.slice(0, 200)}`;
                    return;
                }
                const blob = await resp.blob();
                const disposition = resp.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^"]+)"?/i);
                const filename = match ? match[1] : `dramacd-package-${itemId}.zip`;
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                packageMessage.value = `Downloaded ${filename} (${formatBytes(blob.size)})`;
                // Backend flags audio that was requested but missing on disk —
                // surface it loudly so a subtitles-only ZIP doesn't pass as full.
                const skipped = parseInt(resp.headers.get('X-DramaCD-Audio-Skipped') || '0', 10);
                if (skipped > 0) {
                    packageError.value = `⚠ ${skipped} audio file${skipped !== 1 ? 's' : ''} missing on disk — skipped. Re-extract from Archive, then export again.`;
                }
            } catch (err) {
                packageError.value = `Download failed: ${err.message || err}`;
                console.error('Package preset export failed:', err);
            } finally {
                packageBusy.value = false;
            }
        }

        async function downloadItemPackage() {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                packageError.value = 'No item selected';
                return;
            }
            packageBusy.value = true;
            packageMessage.value = '';
            packageError.value = '';
            try {
                const params = new URLSearchParams({
                    runs: packageAllRuns.value ? 'all' : 'active',
                    audio: packageIncludeAudio.value ? '1' : '0',
                    preserve_paths: packagePreservePaths.value ? '1' : '0',
                    srt: packageIncludeSrt.value ? '1' : '0',
                    txt: packageIncludeTxt.value ? '1' : '0',
                    tracklist: packageIncludeTracklist.value ? '1' : '0',
                    all_files: packageIncludeAllArchiveFiles.value ? '1' : '0',
                });
                const resp = await fetch(`/api/pipeline/items/${itemId}/package.zip?${params}`);
                if (!resp.ok) {
                    const text = await resp.text();
                    packageError.value = `Build failed (${resp.status}): ${text.slice(0, 200)}`;
                    return;
                }
                const blob = await resp.blob();
                const disposition = resp.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^"]+)"?/i);
                const filename = match ? match[1] : `dramacd-package-${itemId}.zip`;
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                packageMessage.value = `Downloaded ${filename} (${formatBytes(blob.size)})`;
                const skipped = parseInt(resp.headers.get('X-DramaCD-Audio-Skipped') || '0', 10);
                if (skipped > 0) {
                    packageError.value = `⚠ ${skipped} audio file${skipped !== 1 ? 's' : ''} missing on disk — skipped. Re-extract from Archive, then export again.`;
                }
            } catch (err) {
                packageError.value = `Download failed: ${err.message || err}`;
                console.error('Package download failed:', err);
            } finally {
                packageBusy.value = false;
            }
        }

        async function scanMojibake() {
            mojibakeBusy.value = 'scan';
            mojibakeMessage.value = '';
            mojibakeError.value = '';
            try {
                const resp = await fetch('/api/pipeline/maintenance/fix-mojibake?dry_run=true', { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    mojibakeError.value = data.detail || `Scan failed (${resp.status})`;
                    return;
                }
                mojibakePreview.value = data;
                mojibakeMessage.value = `Found ${data.candidates} track title(s) with recoverable mojibake across ${data.items_scanned} items.`;
            } catch (err) {
                mojibakeError.value = `Scan failed: ${err.message || err}`;
            } finally {
                mojibakeBusy.value = '';
            }
        }

        async function fixMojibakePaths() {
            mojibakeBusy.value = 'paths';
            mojibakeMessage.value = '';
            mojibakeError.value = '';
            try {
                // Step 1: dry run for the count.
                const previewResp = await fetch('/api/pipeline/maintenance/fix-mojibake-paths?dry_run=true', { method: 'POST' });
                const preview = await previewResp.json().catch(() => ({}));
                if (!previewResp.ok) {
                    mojibakeError.value = preview.detail || `Scan failed (${previewResp.status})`;
                    return;
                }
                const renames = preview.rename_steps || 0;
                const tracks = preview.track_candidates || 0;
                if (!renames) {
                    mojibakeMessage.value = 'No mojibake paths to fix.';
                    return;
                }
                if (!confirm(`Rename ${renames} on-disk file/folder name(s) and update ${tracks} track path(s) in the DB? Make sure no transcription/translation is currently running.`)) {
                    return;
                }
                // Step 2: apply.
                const resp = await fetch('/api/pipeline/maintenance/fix-mojibake-paths?dry_run=false', { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    mojibakeError.value = data.detail || `Fix failed (${resp.status})`;
                    return;
                }
                const failed = (data.renames_failed && data.renames_failed.length) || 0;
                mojibakeMessage.value = `Renamed ${data.renames_applied}/${data.renames_attempted} on disk; updated ${data.tracks_updated} track row(s); ${failed} rename(s) failed.`;
                if (failed) {
                    console.warn('Rename failures:', data.renames_failed);
                }
            } catch (err) {
                mojibakeError.value = `Path fix failed: ${err.message || err}`;
                console.error('Path fix failed:', err);
            } finally {
                mojibakeBusy.value = '';
            }
        }

        async function fixMojibake() {
            if (!mojibakePreview.value || !mojibakePreview.value.candidates) return;
            if (!confirm(`Apply ${mojibakePreview.value.candidates} title fixes? Track filenames on disk are not changed; only DB titles are updated.`)) return;
            mojibakeBusy.value = 'fix';
            mojibakeMessage.value = '';
            mojibakeError.value = '';
            try {
                const resp = await fetch('/api/pipeline/maintenance/fix-mojibake?dry_run=false', { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    mojibakeError.value = data.detail || `Fix failed (${resp.status})`;
                    return;
                }
                mojibakeMessage.value = `Applied ${data.applied} fix(es).`;
                mojibakePreview.value = null;
                await loadItems();
            } catch (err) {
                mojibakeError.value = `Fix failed: ${err.message || err}`;
            } finally {
                mojibakeBusy.value = '';
            }
        }

        async function translateTrackNames() {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                trackNamesError.value = 'No item selected';
                return;
            }
            trackNamesBusy.value = true;
            trackNamesMessage.value = '';
            trackNamesError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/translate-track-names`, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    trackNamesError.value = data.detail || `Translate failed (${resp.status})`;
                    return;
                }
                pushToast({
                    kind: 'success',
                    title: 'Track names translated',
                    body: `${data.translated_count} / ${data.total} titles via ${data.provider}`,
                    ttl: 4000,
                });
                // Patch local track rows so the UI shows EN titles immediately.
                if (Array.isArray(data.tracks)) {
                    const byId = new Map(data.tracks.map(r => [r.track_id, r.title_en]));
                    pipelineTracks.value = pipelineTracks.value.map(t =>
                        byId.has(t.id) ? { ...t, title_en: byId.get(t.id) } : t
                    );
                }
            } catch (err) {
                trackNamesError.value = `Translate failed: ${err.message || err}`;
                console.error('Translate track names failed:', err);
            } finally {
                trackNamesBusy.value = false;
            }
        }

        async function backfillSummaries(force) {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                summariesError.value = 'No item selected';
                return;
            }
            const mode = force ? 'force' : 'fill';
            summariesBusy.value = mode;
            summariesMessage.value = '';
            summariesError.value = '';
            try {
                const url = `/api/pipeline/items/${itemId}/backfill-summaries?force=${force ? 'true' : 'false'}`;
                const resp = await fetch(url, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    summariesError.value = data.detail || `Backfill failed (${resp.status})`;
                    return;
                }
                const failedCount = (data.failed && data.failed.length) || 0;
                pushToast({
                    kind: failedCount > 0 ? 'warning' : 'success',
                    title: force ? 'Summaries regenerated' : 'Summaries filled',
                    body: `${data.generated} new, ${data.skipped_existing} kept, ${failedCount} failed (of ${data.total})`,
                    ttl: 4000,
                });
            } catch (err) {
                summariesError.value = `Backfill failed: ${err.message || err}`;
                console.error('Backfill summaries failed:', err);
            } finally {
                summariesBusy.value = '';
            }
        }

        async function purgeItemWorkspace() {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) {
                purgeError.value = 'No item selected';
                return;
            }
            if (!confirm('Delete the extracted audio for this item? Transcripts and translations stay; you can re-extract any time.')) {
                return;
            }
            purgeBusy.value = true;
            purgeMessage.value = '';
            purgeError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/purge-workspace`, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    purgeError.value = data.detail || `Purge failed (${resp.status})`;
                    return;
                }
                purgeMessage.value = `Freed ${formatBytes(data.bytes_freed || 0)} across ${data.deleted?.length || 0} folder(s).`;
                if (typeof loadWorkshopTracksForItem === 'function') {
                    try { await loadWorkshopTracksForItem(); } catch (_) {}
                }
            } catch (err) {
                purgeError.value = `Purge failed: ${err.message || err}`;
                console.error('Item workspace purge failed:', err);
            } finally {
                purgeBusy.value = false;
            }
        }

        async function loadWorkspaceOrphans() {
            workspaceBusy.value = 'list';
            workspaceMessage.value = '';
            workspaceError.value = '';
            try {
                const resp = await fetch('/api/pipeline/workspace/orphans');
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    workspaceError.value = data.detail || `Scan failed (${resp.status})`;
                    return;
                }
                workspaceOrphans.value = data;
                workspaceMessage.value = `${data.orphans.length} orphan folder(s); ${formatBytes(data.total_orphan_bytes || 0)} reclaimable.`;
            } catch (err) {
                workspaceError.value = `Scan failed: ${err.message || err}`;
                console.error('Workspace orphan scan failed:', err);
            } finally {
                workspaceBusy.value = '';
            }
        }

        async function purgeWorkspaceOrphans() {
            if (!workspaceOrphans.value || !workspaceOrphans.value.orphans.length) return;
            const count = workspaceOrphans.value.orphans.length;
            const size = formatBytes(workspaceOrphans.value.total_orphan_bytes || 0);
            if (!confirm(`Delete ${count} orphan workspace folder(s) (${size})? This is permanent.`)) {
                return;
            }
            workspaceBusy.value = 'purge';
            workspaceMessage.value = '';
            workspaceError.value = '';
            try {
                const resp = await fetch('/api/pipeline/workspace/purge-orphans', { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    workspaceError.value = data.detail || `Purge failed (${resp.status})`;
                    return;
                }
                workspaceMessage.value = `Deleted ${data.deleted_count} folder(s), freed ${formatBytes(data.bytes_freed || 0)}.`;
                workspaceOrphans.value = null;
            } catch (err) {
                workspaceError.value = `Purge failed: ${err.message || err}`;
                console.error('Workspace orphan purge failed:', err);
            } finally {
                workspaceBusy.value = '';
            }
        }

        async function loadOpsPanel() {
            opsLoading.value = true;
            try {
                const [scanResp, fetchResp, jobsResp] = await Promise.all([
                    fetch('/api/scan/status'),
                    fetch('/api/fetch-metadata/status'),
                    fetch('/api/jobs?limit=8'),
                ]);
                opsScanStatus.value = await scanResp.json();
                opsFetchStatus.value = await fetchResp.json();
                const jobsData = await jobsResp.json();
                recentJobs.value = Array.isArray(jobsData.jobs) ? jobsData.jobs : [];
            } catch (err) {
                console.error('Failed to load ops panel:', err);
            } finally {
                opsLoading.value = false;
            }
        }

        async function loadPipelineStatus() {
            pipelineLoadError.value = '';
            try {
                const resp = await fetch('/api/pipeline/status');
                if (!resp.ok) {
                    pipelineEnabled.value = false;
                    pipelineStatus.value = null;
                    return;
                }
                pipelineStatus.value = await resp.json();
                pipelineEnabled.value = !!pipelineStatus.value.enabled;
            } catch (err) {
                pipelineEnabled.value = false;
                pipelineStatus.value = null;
                pipelineLoadError.value = 'Failed to load pipeline status';
                console.error('Failed to load pipeline status:', err);
            }
        }

        async function toggleWorkshopEnabled() {
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const next = !pipelineEnabled.value;
                const resp = await fetch('/api/pipeline/enabled', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: next }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to toggle pipeline';
                    return;
                }
                pipelineEnabled.value = !!data.enabled;
                await loadPipelineStatus();
                if (pipelineEnabled.value) {
                    pipelineActiveSummary.value = 'Atelier enabled';
                } else {
                    // Switch to Library tab if currently on Workshop
                    if (activeTab.value === 'pipeline') {
                        activeTab.value = 'library';
                    }
                    pipelineTracks.value = [];
                    transcriptRuns.value = [];
                    translationRuns.value = [];
                    selectedTranscriptSegments.value = [];
                    selectedTranscriptRunId.value = null;
                    selectedTranscriptCleanText.value = '';
                    selectedTranslationSegments.value = [];
                    selectedTranslationRunId.value = null;
                    pipelineActiveSummary.value = 'Atelier disabled';
                }
            } catch (err) {
                pipelineLoadError.value = 'Failed to toggle pipeline';
                console.error('Failed to toggle pipeline:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function switchToWorkshopTab() {
            activeTab.value = 'pipeline';
            await loadPipelineStatus();
            // Re-fetch tracks for the focused item so the Track Selection list
            // reflects any transcripts/translations created elsewhere (Player,
            // another session, etc.) since this tab was last opened. Without
            // this the cached transcript_run_count goes stale and the
            // "transcribed" list looks empty even when the data exists.
            if (pipelineSelectedItemId.value) {
                try {
                    await refreshPipelineTrackList();
                    if (pipelineTrackId.value) {
                        try { await loadPipelineRuns(); } catch (_) {}
                    }
                } catch (_) {}
            }
        }

        async function switchToPlayerTab() {
            activeTab.value = 'player';
            // Carry the focused workshop item over to the player. Whenever the
            // workshop's selected CD differs from the player's loaded one, reset
            // the player state and reload the new item's track list — otherwise
            // the previous CD's tracks linger in the available-tracks list.
            const workshopItemId = (selectedWorkshopItem.value && selectedWorkshopItem.value.id)
                || pipelineSelectedItemId.value
                || null;
            if (workshopItemId && workshopItemId !== playerItemId.value) {
                // Different CD than what's currently loaded — drop the previous
                // track + transcript state cleanly so we don't show stale data.
                playerItemId.value = workshopItemId;
                playerTrackId.value = null;
                playerAvailableTracks.value = [];
                playerTranscriptSegments.value = [];
                playerTranslationSegments.value = [];
                playerTrackTitle.value = '';
                playerTrackDuration.value = '';
                try { await loadPlayerItemTracks(); } catch (_) {}
            } else if (workshopItemId && !playerTrackId.value) {
                // Same CD, no track loaded yet — just make sure the available
                // list is populated.
                playerItemId.value = workshopItemId;
                try { await loadPlayerItemTracks(); } catch (_) {}
            }
        }

        // Open a CD in the Player straight from the Library — no Workshop
        // detour. Loads the item's track list; user picks a track to play.
        // The Player itself already tolerates tracks with no transcript /
        // translation (it just renders without lyrics).
        async function openItemInPlayer(item) {
            if (!item || !item.id) return;
            activeTab.value = 'player';
            // Always reset state if this is a different CD than what was
            // previously loaded — otherwise the header keeps showing the
            // previous CD's title/cover while the body shows the new
            // (possibly empty) tracks list.
            if (playerItemId.value !== item.id) {
                playerItemId.value = item.id;
                playerTrackId.value = null;
                playerAvailableTracks.value = [];
                playerTranscriptSegments.value = [];
                playerTranslationSegments.value = [];
                playerTrackTitle.value = '';
                playerTrackDuration.value = '';
            }
            // Keep Workshop's selected-item in sync so the Player's header
            // (which reads from selectedWorkshopItem for cover + metadata)
            // shows the *current* CD, not whatever was last selected in
            // Workshop.
            selectedWorkshopItem.value = item;
            pipelineSelectedItemId.value = item.id;
            try {
                await loadPlayerItemTracks();
            } catch (err) {
                console.warn('Open in Player failed:', err);
            }
        }

        // Open a specific workshop track in the Player tab. Used by the ▶ button
        // on each transcribed-track row — switches tabs AND loads the audio so
        // the user lands on a working player, not just an empty Player tab.
        async function openTrackInPlayer(trackId) {
            playerItemId.value = pipelineSelectedItemId.value;
            activeTab.value = 'player';
            try { await loadPlayerTrack(trackId); } catch (err) { console.warn('open in player failed:', err); }
        }

        async function loadApiSettings() {
            apiSettingsBusy.value = true;
            apiSettingsError.value = '';
            apiSettingsSuccess.value = '';
            try {
                const resp = await fetch('/api/settings/ai');
                const data = await resp.json();
                if (!resp.ok) {
                    apiSettingsError.value = data.detail || 'Failed to load AI settings';
                    return;
                }
                const loadedProvider = String(data.translation_provider || 'gemini').toLowerCase();
                apiTranslationProvider.value = SUPPORTED_PROVIDERS.includes(loadedProvider) ? loadedProvider : 'gemini';
                apiGeminiModel.value = String(data.gemini_model || '').trim() || 'gemini-2.0-flash';
                apiGeminiHasKey.value = !!data.gemini_has_api_key;
                apiGeminiKeySource.value = String(data.gemini_api_key_source || 'env');
                apiOpenRouterModel.value = String(data.openrouter_model || '').trim() || 'openrouter/auto';
                apiOpenRouterHasKey.value = !!data.openrouter_has_api_key;
                apiOpenRouterKeySource.value = String(data.openrouter_api_key_source || 'env');
                apiChutesModel.value = String(data.chutes_model || '').trim() || 'deepseek-ai/DeepSeek-V3.1';
                apiChutesHasKey.value = !!data.chutes_has_api_key;
                apiChutesKeySource.value = String(data.chutes_api_key_source || 'env');
                apiOpenAiCompatBaseUrl.value = String(data.openai_compat_base_url || '').trim();
                apiOpenAiCompatModel.value = String(data.openai_compat_model || '').trim();
                apiOpenAiCompatHasKey.value = !!data.openai_compat_has_api_key;
                apiOpenAiCompatKeySource.value = String(data.openai_compat_api_key_source || 'env');
                apiOpenAiCompatBaseUrlSource.value = String(data.openai_compat_base_url_source || 'env');
                apiOpenAiCompatRequestFormat.value = String(data.openai_compat_request_format || 'openai');
                autoTranslateProvider.value = apiTranslationProvider.value;
                if (!autoTranslateModel.value || autoTranslateModel.value === 'gemini-2.0-flash' || autoTranslateModel.value === 'openrouter/auto') {
                    if (apiTranslationProvider.value === 'openrouter') autoTranslateModel.value = apiOpenRouterModel.value;
                    else if (apiTranslationProvider.value === 'chutes') autoTranslateModel.value = apiChutesModel.value;
                    else if (apiTranslationProvider.value === 'openai_compat') autoTranslateModel.value = apiOpenAiCompatModel.value;
                    else autoTranslateModel.value = apiGeminiModel.value;
                }
                localStorage.setItem('apiTranslationProvider', apiTranslationProvider.value);
                localStorage.setItem('apiGeminiModel', apiGeminiModel.value);
                localStorage.setItem('apiOpenRouterModel', apiOpenRouterModel.value);
                localStorage.setItem('apiChutesModel', apiChutesModel.value);
                localStorage.setItem('apiOpenAiCompatModel', apiOpenAiCompatModel.value);
                localStorage.setItem('apiOpenAiCompatBaseUrl', apiOpenAiCompatBaseUrl.value);
            } catch (err) {
                apiSettingsError.value = 'Failed to load AI settings';
                console.error('Failed to load AI settings:', err);
            } finally {
                apiSettingsBusy.value = false;
            }
        }

        async function saveApiSettings(action = 'save') {
            apiSettingsBusy.value = true;
            apiSettingsError.value = '';
            apiSettingsSuccess.value = '';
            try {
                const payload = {};
                const geminiModel = String(apiGeminiModel.value || '').trim();
                const geminiKey = String(apiGeminiKeyInput.value || '').trim();
                const openRouterModel = String(apiOpenRouterModel.value || '').trim();
                const openRouterKey = String(apiOpenRouterKeyInput.value || '').trim();
                const chutesModel = String(apiChutesModel.value || '').trim();
                const chutesKey = String(apiChutesKeyInput.value || '').trim();
                const compatBaseUrl = String(apiOpenAiCompatBaseUrl.value || '').trim();
                const compatModel = String(apiOpenAiCompatModel.value || '').trim();
                const compatKey = String(apiOpenAiCompatKeyInput.value || '').trim();
                const compatFormat = String(apiOpenAiCompatRequestFormat.value || 'openai').trim().toLowerCase();
                const provider = String(apiTranslationProvider.value || 'gemini').trim().toLowerCase();
                payload.translation_provider = SUPPORTED_PROVIDERS.includes(provider) ? provider : 'gemini';
                if (geminiModel) payload.gemini_model = geminiModel;
                if (geminiKey) payload.gemini_api_key = geminiKey;
                if (openRouterModel) payload.openrouter_model = openRouterModel;
                if (openRouterKey) payload.openrouter_api_key = openRouterKey;
                if (chutesModel) payload.chutes_model = chutesModel;
                if (chutesKey) payload.chutes_api_key = chutesKey;
                if (compatBaseUrl) payload.openai_compat_base_url = compatBaseUrl;
                if (compatModel) payload.openai_compat_model = compatModel;
                if (compatKey) payload.openai_compat_api_key = compatKey;
                if (['openai', 'anthropic'].includes(compatFormat)) payload.openai_compat_request_format = compatFormat;
                if (action === 'clear_gemini') payload.clear_gemini_api_key = true;
                if (action === 'clear_openrouter') payload.clear_openrouter_api_key = true;
                if (action === 'clear_chutes') payload.clear_chutes_api_key = true;
                if (action === 'clear_openai_compat') {
                    payload.clear_openai_compat_api_key = true;
                    payload.clear_openai_compat_base_url = true;
                }
                if (!Object.keys(payload).length) {
                    apiSettingsError.value = 'Enter a model and/or API key first.';
                    return;
                }

                const resp = await fetch('/api/settings/ai', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    apiSettingsError.value = data.detail || 'Failed to save AI settings';
                    return;
                }
                const savedProvider = String(data.translation_provider || provider).toLowerCase();
                apiTranslationProvider.value = SUPPORTED_PROVIDERS.includes(savedProvider) ? savedProvider : 'gemini';
                apiGeminiModel.value = String(data.gemini_model || geminiModel).trim() || apiGeminiModel.value;
                apiGeminiHasKey.value = !!data.gemini_has_api_key;
                apiGeminiKeySource.value = String(data.gemini_api_key_source || apiGeminiKeySource.value || 'env');
                apiOpenRouterModel.value = String(data.openrouter_model || openRouterModel).trim() || apiOpenRouterModel.value;
                apiOpenRouterHasKey.value = !!data.openrouter_has_api_key;
                apiOpenRouterKeySource.value = String(data.openrouter_api_key_source || apiOpenRouterKeySource.value || 'env');
                apiChutesModel.value = String(data.chutes_model || chutesModel).trim() || apiChutesModel.value;
                apiChutesHasKey.value = !!data.chutes_has_api_key;
                apiChutesKeySource.value = String(data.chutes_api_key_source || apiChutesKeySource.value || 'env');
                apiOpenAiCompatBaseUrl.value = String(data.openai_compat_base_url || compatBaseUrl).trim();
                apiOpenAiCompatModel.value = String(data.openai_compat_model || compatModel).trim();
                apiOpenAiCompatHasKey.value = !!data.openai_compat_has_api_key;
                apiOpenAiCompatKeySource.value = String(data.openai_compat_api_key_source || apiOpenAiCompatKeySource.value || 'env');
                apiOpenAiCompatBaseUrlSource.value = String(data.openai_compat_base_url_source || apiOpenAiCompatBaseUrlSource.value || 'env');
                apiOpenAiCompatRequestFormat.value = String(data.openai_compat_request_format || compatFormat || 'openai');
                apiGeminiKeyInput.value = '';
                apiOpenRouterKeyInput.value = '';
                apiChutesKeyInput.value = '';
                apiOpenAiCompatKeyInput.value = '';
                autoTranslateProvider.value = apiTranslationProvider.value;
                if (autoTranslateProvider.value === 'openrouter') {
                    autoTranslateModel.value = apiOpenRouterModel.value;
                } else if (autoTranslateProvider.value === 'chutes') {
                    autoTranslateModel.value = apiChutesModel.value;
                } else if (autoTranslateProvider.value === 'openai_compat') {
                    autoTranslateModel.value = apiOpenAiCompatModel.value;
                } else {
                    autoTranslateModel.value = apiGeminiModel.value;
                }
                localStorage.setItem('apiTranslationProvider', apiTranslationProvider.value);
                localStorage.setItem('apiGeminiModel', apiGeminiModel.value);
                localStorage.setItem('apiOpenRouterModel', apiOpenRouterModel.value);
                localStorage.setItem('apiChutesModel', apiChutesModel.value);
                localStorage.setItem('apiOpenAiCompatModel', apiOpenAiCompatModel.value);
                localStorage.setItem('apiOpenAiCompatBaseUrl', apiOpenAiCompatBaseUrl.value);
                apiSettingsSuccess.value = 'AI settings saved. New requests use them immediately.';
            } catch (err) {
                apiSettingsError.value = 'Failed to save AI settings';
                console.error('Failed to save AI settings:', err);
            } finally {
                apiSettingsBusy.value = false;
            }
        }

        async function fetchOpenAiCompatModels() {
            apiOpenAiCompatModelsBusy.value = true;
            apiOpenAiCompatModelsError.value = '';
            try {
                // Persist the URL/key first so the server-side fetch sees them.
                const baseUrl = String(apiOpenAiCompatBaseUrl.value || '').trim();
                const key = String(apiOpenAiCompatKeyInput.value || '').trim();
                if (baseUrl || key) {
                    const persistPayload = { translation_provider: apiTranslationProvider.value };
                    if (baseUrl) persistPayload.openai_compat_base_url = baseUrl;
                    if (key) persistPayload.openai_compat_api_key = key;
                    await fetch('/api/settings/ai', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(persistPayload),
                    });
                    if (key) apiOpenAiCompatKeyInput.value = '';
                }
                const resp = await fetch('/api/settings/ai/openai-compat-models');
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    apiOpenAiCompatModelsError.value = data.detail || `Fetch failed (${resp.status})`;
                    return;
                }
                apiOpenAiCompatModelOptions.value = Array.isArray(data.models) ? data.models : [];
                if (!apiOpenAiCompatModelOptions.value.length) {
                    apiOpenAiCompatModelsError.value = 'Endpoint returned no models.';
                }
            } catch (err) {
                apiOpenAiCompatModelsError.value = `Fetch failed: ${err.message || err}`;
                console.error('Fetch OpenAI-compat models failed:', err);
            } finally {
                apiOpenAiCompatModelsBusy.value = false;
            }
        }

        async function testApiSettings() {
            apiTestBusy.value = true;
            apiTestResult.value = '';
            try {
                const started = Date.now();
                const resp = await fetch('/api/settings/ai/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });
                const data = await resp.json();
                const elapsedMs = Date.now() - started;
                if (!resp.ok) {
                    apiTestResult.value = `Test failed (${resp.status}): ${data.detail || 'unknown error'}`;
                    return;
                }
                apiTestResult.value = `OK (${elapsedMs}ms) provider=${data.provider} sample="${data.sample_title_en || ''}"`;
            } catch (err) {
                apiTestResult.value = 'Test failed: network/server issue';
                console.error('AI settings test failed:', err);
            } finally {
                apiTestBusy.value = false;
            }
        }

        async function switchToApiTab() {
            activeTab.value = 'api';
            await loadApiSettings();
            await loadWhisperSettings();
            await loadScanPaths();
            await loadGamesScanPaths();
        }

        async function loadWhisperSettings() {
            whisperSettingsBusy.value = true;
            whisperSettingsError.value = '';
            try {
                const resp = await fetch('/api/settings/whisper');
                if (!resp.ok) {
                    whisperSettingsError.value = `Failed to load Whisper settings (${resp.status})`;
                    return;
                }
                const data = await resp.json();
                whisperSettings.value = {
                    model: data.model || 'small',
                    vad_filter: !!data.vad_filter,
                    beam_size: Number(data.beam_size) || 5,
                    condition_on_previous_text: !!data.condition_on_previous_text,
                    preferred_variant: data.preferred_variant === 'no-sfx' ? 'no-sfx' : 'sfx',
                };
                if (Array.isArray(data.supported_models) && data.supported_models.length > 0) {
                    whisperSupportedModels.value = data.supported_models;
                }
            } catch (err) {
                console.error('Failed to load Whisper settings:', err);
                whisperSettingsError.value = 'Failed to load Whisper settings';
            } finally {
                whisperSettingsBusy.value = false;
            }
        }

        async function saveWhisperSettings() {
            whisperSettingsBusy.value = true;
            whisperSettingsError.value = '';
            whisperSettingsSuccess.value = '';
            try {
                const body = {
                    model: whisperSettings.value.model,
                    vad_filter: !!whisperSettings.value.vad_filter,
                    beam_size: Number(whisperSettings.value.beam_size) || 5,
                    condition_on_previous_text: !!whisperSettings.value.condition_on_previous_text,
                    preferred_variant: whisperSettings.value.preferred_variant === 'no-sfx' ? 'no-sfx' : 'sfx',
                };
                const resp = await fetch('/api/settings/whisper', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    whisperSettingsError.value = data.detail || `Save failed (${resp.status})`;
                    return;
                }
                whisperSettings.value = {
                    model: data.model,
                    vad_filter: !!data.vad_filter,
                    beam_size: Number(data.beam_size) || 5,
                    condition_on_previous_text: !!data.condition_on_previous_text,
                    preferred_variant: data.preferred_variant === 'no-sfx' ? 'no-sfx' : 'sfx',
                };
                whisperSettingsSuccess.value = `Saved (${(data.updated_fields || []).join(', ')}). Applies to next transcription job.`;
            } catch (err) {
                console.error('Failed to save Whisper settings:', err);
                whisperSettingsError.value = 'Failed to save Whisper settings';
            } finally {
                whisperSettingsBusy.value = false;
            }
        }

        // Workshop Auto-Load Functions
        function handleWorkshopAutoLoad() {
            const selectedCount = selectedIds.value.size;

            if (selectedCount === 0) {
                // Clear Workshop state when nothing selected
                selectedWorkshopItem.value = null;
                pipelineSelectedItemId.value = null;
                return;
            }

            if (selectedCount === 1) {
                // Single item: load to Workshop and auto-switch tab
                const itemId = Array.from(selectedIds.value)[0];
                const item = items.value.find(i => i.id === itemId);
                if (item) {
                    loadItemToWorkshop(item);
                }
            } else {
                // Multi-select: bulk actions live in the library kebab; just
                // clear the Workshop single-item display so it doesn't show
                // a stale CD that doesn't match the selection.
                selectedWorkshopItem.value = null;
            }
        }

        function effectiveTrackCount(item) {
            // Manual override wins; otherwise the duplicate-deduped group count
            // (so FLAC + MP3 + no-SFX variants of the same audio count once).
            if (!item) return null;
            const manual = item.manual_track_count;
            if (Number.isInteger(manual) && manual >= 0) return manual;
            const groups = pipelineTrackGroups.value || [];
            return groups.length || null;
        }

        function workshopSearchInput(value) {
            workshopSearchQuery.value = value;
            workshopSearchOpen.value = true;
            if (workshopSearchDebounce) clearTimeout(workshopSearchDebounce);
            workshopSearchDebounce = setTimeout(() => runWorkshopSearch(value), 180);
        }

        async function runWorkshopSearch(query) {
            const raw = String(query || '').trim();
            if (!raw) {
                workshopSearchResults.value = [];
                workshopSearchLoading.value = false;
                return;
            }
            // Wrap each token with FTS5 prefix-match `*`. Lets you find
            // partial DLsite codes ("RJ0149" → RJ01494586) and partial words
            // ("mond" → Mondou Ash) without typing the whole thing. Strips
            // FTS-reserved punctuation that would otherwise break the parser.
            const cleaned = raw.replace(/["()]/g, ' ').trim();
            const tokens = cleaned.split(/\s+/).filter(Boolean);
            if (!tokens.length) {
                workshopSearchResults.value = [];
                workshopSearchLoading.value = false;
                return;
            }
            const fts = tokens.map(t => `${t}*`).join(' ');
            workshopSearchLoading.value = true;
            try {
                const resp = await fetch(`/api/items?search=${encodeURIComponent(fts)}&limit=10`);
                if (!resp.ok) {
                    workshopSearchResults.value = [];
                    return;
                }
                const data = await resp.json();
                workshopSearchResults.value = Array.isArray(data.items) ? data.items : (Array.isArray(data) ? data : []);
            } catch (err) {
                console.error('Workshop search failed:', err);
                workshopSearchResults.value = [];
            } finally {
                workshopSearchLoading.value = false;
            }
        }

        function selectWorkshopSearchResult(item) {
            workshopSearchOpen.value = false;
            workshopSearchQuery.value = '';
            workshopSearchResults.value = [];
            if (item) loadItemToWorkshop(item);
        }

        function closeWorkshopSearch() {
            workshopSearchOpen.value = false;
        }

        function startEditTrackCount() {
            const item = selectedWorkshopItem.value;
            if (!item) return;
            const current = effectiveTrackCount(item);
            manualTrackCountInput.value = current == null ? '' : String(current);
            manualTrackCountEditing.value = true;
        }
        function cancelEditTrackCount() {
            manualTrackCountEditing.value = false;
            manualTrackCountInput.value = '';
        }
        async function saveManualTrackCount() {
            const item = selectedWorkshopItem.value;
            if (!item) return;
            const raw = String(manualTrackCountInput.value || '').trim();
            let body;
            if (!raw) {
                body = { count: null };  // clear → revert to auto
            } else {
                const parsed = Number(raw);
                if (!Number.isInteger(parsed) || parsed < 0) {
                    cancelEditTrackCount();
                    return;
                }
                body = { count: parsed };
            }
            try {
                const resp = await fetch(`/api/items/${item.id}/manual-track-count`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await resp.json();
                if (resp.ok) {
                    selectedWorkshopItem.value = { ...item, manual_track_count: data.manual_track_count };
                }
            } catch (err) {
                console.error('Failed to save manual track count:', err);
            } finally {
                cancelEditTrackCount();
            }
        }

        async function loadItemToWorkshop(item) {
            // Store full item data for Workshop display
            selectedWorkshopItem.value = item;
            // Wipe the cached archive listing for this item so a re-send
            // after an extraction completes picks up the freshly unpacked
            // files immediately, instead of showing the stale empty list
            // until the next tab switch.
            archiveContentsCache.delete(item.id);
            pipelineSelectedItemId.value = item.id;

            // Auto-switch to Workshop tab
            activeTab.value = 'pipeline';

            // Load pipeline status if not already loaded
            if (!pipelineStatus.value) {
                await loadPipelineStatus();
            }

            // Auto-load tracks for the selected item
            await loadPipelineTracksForItem();
            // Archive viewer load is wired to a watcher on pipelineSelectedItemId
            // (below) so it fires on both clicks and page-reload rehydration.
        }

        // Trigger the inline archive viewer load whenever the active CD changes —
        // covers both `loadItemToWorkshop` (search click / library send) and the
        // page-reload case where pipelineSelectedItemId rehydrates from
        // localStorage and `loadItemToWorkshop` doesn't run.
        watch(pipelineSelectedItemId, (newId) => {
            // Reset folder state when switching CDs so we don't try to render
            // paths from the previous archive in this one.
            archiveCurrentPath.value = '';
            archiveCollapsedFolders.value = new Set();
            if (!newId) {
                archiveContents.value = null;
                return;
            }
            loadArchiveContents(newId);
        }, { immediate: true });

        // Per-item glossary load. Fires whenever the Workshop's selected item
        // changes (including the initial localStorage rehydrate). _glossaryLoading
        // is true while we set the value so the save-debounce watcher above
        // doesn't immediately PUT the freshly-loaded text back.
        watch(pipelineSelectedItemId, async (newId) => {
            // Drop any pending save from the previous item — its target id is stale.
            if (_glossarySaveTimer) {
                clearTimeout(_glossarySaveTimer);
                _glossarySaveTimer = null;
            }
            if (!newId) {
                _glossaryLoading.value = true;
                _glossaryItemId.value = null;
                autoTranslateGlossary.value = '';
                await nextTick();
                _glossaryLoading.value = false;
                return;
            }
            try {
                const resp = await fetch(`/api/items/${newId}/glossary`);
                if (!resp.ok) {
                    _glossaryLoading.value = true;
                    _glossaryItemId.value = newId;
                    autoTranslateGlossary.value = '';
                    await nextTick();
                    _glossaryLoading.value = false;
                    return;
                }
                const data = await resp.json();
                _glossaryLoading.value = true;
                _glossaryItemId.value = newId;
                autoTranslateGlossary.value = String(data.glossary || '');
                await nextTick();
                _glossaryLoading.value = false;
            } catch (err) {
                console.warn('Failed to load item glossary:', err);
                _glossaryLoading.value = true;
                _glossaryItemId.value = newId;
                autoTranslateGlossary.value = '';
                await nextTick();
                _glossaryLoading.value = false;
            }
        }, { immediate: true });

        async function loadArchiveContents(itemId) {
            if (!itemId) {
                archiveContents.value = null;
                return;
            }
            // Cache hit — just show what we already have.
            if (archiveContentsCache.has(itemId)) {
                archiveContents.value = archiveContentsCache.get(itemId);
                return;
            }
            archiveContents.value = null;
            archiveContentsLoading.value = true;
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/archive-contents`);
                if (!resp.ok) {
                    archiveContents.value = { error: `Couldn't list archive (${resp.status})`, files: [] };
                    return;
                }
                const data = await resp.json();
                archiveContentsCache.set(itemId, data);
                archiveContents.value = data;
            } catch (err) {
                console.error('Failed to load archive contents:', err);
                archiveContents.value = { error: 'Failed to load archive contents', files: [] };
            } finally {
                archiveContentsLoading.value = false;
            }
        }

        // ---- List-view folder grouping ---------------------------------
        // Groups files by their parent folder so the list view shows
        // "Folder header → filenames under it" instead of a flat dump of
        // long paths. The file rows display just the filename (the folder
        // is the header above them).
        const archiveListGroups = computed(() => {
            const list = (archiveContents.value && archiveContents.value.files) || [];
            const groups = new Map();
            for (const f of list) {
                const norm = normalizeArchivePath(f.path);
                const lastSlash = norm.lastIndexOf('/');
                const folder = lastSlash === -1 ? '' : norm.slice(0, lastSlash);
                const name = lastSlash === -1 ? norm : norm.slice(lastSlash + 1);
                if (!groups.has(folder)) groups.set(folder, []);
                groups.get(folder).push({ name, path: norm, size: f.size });
            }
            const result = [];
            const sortedKeys = Array.from(groups.keys()).sort();
            for (const key of sortedKeys) {
                const files = groups.get(key).sort((a, b) => a.name.localeCompare(b.name));
                result.push({ folder: key, files });
            }
            return result;
        });

        // ---- Grid-view folder navigation -------------------------------
        // 7z preserves the archive's native separator (backslash for Windows-
        // created archives, forward slash for *nix-created). Normalize both
        // to '/' so the rest of the UI doesn't have to care.
        function normalizeArchivePath(p) {
            return String(p || '').replace(/\\/g, '/');
        }

        const IMAGE_EXTS = new Set(['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']);
        function isImagePath(path) {
            const dot = path.lastIndexOf('.');
            if (dot < 0) return false;
            return IMAGE_EXTS.has(path.slice(dot).toLowerCase());
        }

        // Entries to show in the grid for the current folder. Splits each
        // file's path relative to the current path, treating the first
        // segment as either a folder (if there are more segments after) or
        // a file (if it's the leaf).
        const archiveGridEntries = computed(() => {
            const list = (archiveContents.value && archiveContents.value.files) || [];
            const prefix = archiveCurrentPath.value;
            const folders = new Map();
            const files = [];
            for (const f of list) {
                const norm = normalizeArchivePath(f.path);
                if (prefix && !norm.startsWith(prefix)) continue;
                const rest = prefix ? norm.slice(prefix.length) : norm;
                if (!rest) continue;
                const idx = rest.indexOf('/');
                if (idx === -1) {
                    files.push({ name: rest, path: norm, size: f.size });
                } else {
                    const folderName = rest.slice(0, idx);
                    const folderPath = prefix + folderName + '/';
                    if (!folders.has(folderPath)) {
                        folders.set(folderPath, { name: folderName, path: folderPath, fileCount: 0, totalSize: 0 });
                    }
                    const entry = folders.get(folderPath);
                    entry.fileCount += 1;
                    entry.totalSize += Number(f.size || 0);
                }
            }
            const folderList = Array.from(folders.values()).sort((a, b) => a.name.localeCompare(b.name));
            files.sort((a, b) => a.name.localeCompare(b.name));
            return { folders: folderList, files };
        });

        const archiveBreadcrumbs = computed(() => {
            const out = [{ label: '/', path: '' }];
            const parts = archiveCurrentPath.value.split('/').filter(Boolean);
            let cumulative = '';
            for (const part of parts) {
                cumulative += part + '/';
                out.push({ label: part, path: cumulative });
            }
            return out;
        });

        function archiveOpenFolder(path) {
            archiveCurrentPath.value = path;
        }

        function archiveThumbUrl(itemId, innerPath) {
            return `/api/pipeline/items/${itemId}/archive-thumb?path=${encodeURIComponent(innerPath)}`;
        }

        function formatArchiveSize(bytes) {
            const n = Number(bytes || 0);
            if (n < 1024) return `${n} B`;
            if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
            if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
            return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
        }

        // Inline-confirm wrappers for the destructive purge-audio icon.
        function askPurgeAudio() { pendingPurgeAudio.value = true; }
        function cancelPurgeAudio() { pendingPurgeAudio.value = false; }
        async function confirmPurgeAudio() {
            pendingPurgeAudio.value = false;
            await purgeItemWorkspace();
        }

        async function loadPipelineTracksForItem() {
            if (!pipelineSelectedItemId.value) {
                pipelineLoadError.value = 'Enter a valid item id';
                return;
            }
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            const previousTrackId = pipelineTrackId.value;
            pipelineTrackId.value = null;
            transcriptRuns.value = [];
            translationRuns.value = [];
            selectedTranscriptSegments.value = [];
            selectedTranscriptRunId.value = null;
            selectedTranscriptCleanText.value = '';
            selectedTranslationSegments.value = [];
            selectedTranslationRunId.value = null;
            try {
                const resp = await fetch(`/api/pipeline/items/${pipelineSelectedItemId.value}/tracks`);
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to load tracks';
                    return;
                }
                pipelineTracks.value = Array.isArray(data.tracks) ? data.tracks : [];
                if (pipelineTracks.value.length) {
                    // Preserve the user's previously-selected track if it's
                    // still in the list (helps with state restore on refresh
                    // and avoids a jarring reset on manual reload).
                    if (previousTrackId && pipelineTracks.value.find(t => t.id === previousTrackId)) {
                        pipelineTrackId.value = previousTrackId;
                    } else {
                        pipelineTrackId.value = pipelineTracks.value[0].id;
                    }
                    // Auto-select one track per group (preferred-codec) so FLAC+MP3
                    // siblings of the same audio don't both get queued for whisper.
                    selectedTracksForTranscription.value = (pipelineTrackGroups.value || []).map(g => g.preferred_track_id);
                } else {
                    // Clear selection if no tracks
                    selectedTracksForTranscription.value = [];
                }
            } catch (err) {
                pipelineLoadError.value = 'Failed to load tracks';
                console.error('Failed to load tracks:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function refreshPipelineTrackList() {
            // Soft refresh: re-fetch the track rows (so transcript_run_count
            // / translation_run_count badges stay current) WITHOUT clobbering
            // the user's current selection, loaded runs, or segment caches.
            // Use this on background events like job completion.
            if (!pipelineSelectedItemId.value) return;
            try {
                const resp = await fetch(`/api/pipeline/items/${pipelineSelectedItemId.value}/tracks`);
                if (!resp.ok) return;
                const data = await resp.json();
                if (Array.isArray(data.tracks)) {
                    pipelineTracks.value = data.tracks;
                }
            } catch (err) {
                console.error('Soft refresh failed:', err);
            }
        }

        function toggleTrackSelection(trackId) {
            const idx = selectedTracksForTranscription.value.indexOf(trackId);
            if (idx >= 0) {
                selectedTracksForTranscription.value.splice(idx, 1);
            } else {
                selectedTracksForTranscription.value.push(trackId);
            }
        }

        function selectAllTracks() {
            if (pipelineTracks.value.length) {
                selectedTracksForTranscription.value = pipelineTracks.value.map(t => t.id);
            }
        }

        function selectTracksByCodec(codec) {
            // Group-aware: select the preferred-codec track of every visible group
            // (so we don't queue duplicate transcriptions of the same audio).
            const groups = pipelineTrackGroups.value || [];
            if (groups.length) {
                selectedTracksForTranscription.value = groups.map(g => g.preferred_track_id);
            }
        }

        function setTrackCodecFilter(filter) {
            trackCodecFilter.value = filter;
        }

        function clearAllTracks() {
            selectedTracksForTranscription.value = [];
        }

        async function loadPipelineRuns() {
            if (!pipelineTrackId.value) {
                pipelineLoadError.value = 'Select a track first';
                return;
            }
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            pipelineActiveSummary.value = '';
            selectedTranscriptSegments.value = [];
            selectedTranslationSegments.value = [];
            pendingDeleteTranscriptRunId.value = null;
            pendingDeleteTranslationRunId.value = null;
            try {
                const [trResp, tlResp] = await Promise.all([
                    fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/transcripts`),
                    fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/translations`),
                ]);
                const trData = await trResp.json();
                const tlData = await tlResp.json();
                if (!trResp.ok) throw new Error(trData.detail || 'Failed to load transcript runs');
                if (!tlResp.ok) throw new Error(tlData.detail || 'Failed to load translation runs');

                transcriptRuns.value = Array.isArray(trData.runs) ? trData.runs : [];
                translationRuns.value = Array.isArray(tlData.runs) ? tlData.runs : [];
                const activeTranscript = trData.active?.active_transcript_run_id;
                const activeTranslation = tlData.active?.active_translation_run_id;
                activeTranscriptRunId.value = activeTranscript || null;
                activeTranslationRunId.value = activeTranslation || null;
                if (activeTranscript) pipelineTranscriptRunId.value = activeTranscript;
                pipelineActiveSummary.value = '';
            } catch (err) {
                pipelineLoadError.value = err.message || 'Failed to load runs';
                console.error('Failed to load runs:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function queueExtractionForItem(itemId, force = false) {
            const numericId = Number(itemId);
            if (!Number.isInteger(numericId) || numericId <= 0) {
                pipelineLoadError.value = `Invalid item id: ${itemId}`;
                return false;
            }
            const resp = await fetch(`/api/pipeline/items/${numericId}/extract`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force: !!force }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                pipelineLoadError.value = data.detail || `Failed to queue extraction for item ${numericId}`;
                return false;
            }
            return true;
        }

        // === Player-tab inline extraction ===
        // "I thought scan would do the trick" — extraction is on-demand by
        // design (disk space), but the Player shouldn't strand the user on
        // an empty state. One button queues the extract job, polls until it
        // lands, then reloads the track list so playback can start.
        const playerExtracting = ref(false);
        const playerExtractStatus = ref('');
        let _playerExtractTimer = null;
        async function extractFromPlayer() {
            if (!playerItemId.value || playerExtracting.value) return;
            const itemId = Number(playerItemId.value);
            playerExtracting.value = true;
            playerExtractStatus.value = 'Queuing…';
            let ok = false;
            try {
                ok = await queueExtractionForItem(itemId, false);
            } catch (err) {
                pipelineLoadError.value = 'Failed to queue extraction';
            }
            if (!ok) {
                playerExtracting.value = false;
                playerExtractStatus.value = '';
                return;
            }
            playerExtractStatus.value = 'Extracting…';
            let polls = 0;
            const poll = async () => {
                // User moved to another CD — stop minding this job.
                if (Number(playerItemId.value) !== itemId) {
                    playerExtracting.value = false;
                    playerExtractStatus.value = '';
                    return;
                }
                polls += 1;
                try {
                    const resp = await fetch(`/api/pipeline/items/${itemId}/extract/status`);
                    if (resp.ok) {
                        const data = await resp.json();
                        const st = data.status;
                        if (st === 'completed') {
                            playerExtracting.value = false;
                            playerExtractStatus.value = '';
                            pushToast({ kind: 'success', title: 'Extraction complete', ttl: 3000 });
                            await loadPlayerItemTracks();
                            return;
                        }
                        if (st === 'failed' || st === 'stopped') {
                            playerExtracting.value = false;
                            playerExtractStatus.value = '';
                            pipelineLoadError.value = (data.job && data.job.error) || 'Extraction failed';
                            return;
                        }
                    }
                } catch (_) { /* transient — keep polling */ }
                if (polls >= 300) { // ~10 min safety cap
                    playerExtracting.value = false;
                    playerExtractStatus.value = '';
                    pipelineLoadError.value = 'Extraction is taking unusually long — check Atelier';
                    return;
                }
                _playerExtractTimer = setTimeout(poll, 2000);
            };
            _playerExtractTimer = setTimeout(poll, 2000);
        }

        async function queueExtractionForCurrentItem() {
            if (!pipelineSelectedItemId.value) {
                pipelineLoadError.value = 'Enter a valid item id';
                return;
            }
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const ok = await queueExtractionForItem(pipelineSelectedItemId.value, pipelineForceExtract.value);
                if (!ok) return;
                pipelineActiveSummary.value = `Queued extraction for item ${pipelineSelectedItemId.value}`;
            } catch (err) {
                pipelineLoadError.value = 'Failed to queue extraction';
                console.error('Failed to queue extraction:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function importSubtitlesForCurrentItem() {
            if (!pipelineSelectedItemId.value) {
                pipelineLoadError.value = 'Enter a valid item id';
                return;
            }
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/items/${pipelineSelectedItemId.value}/import-subtitles`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to import subtitles';
                    return;
                }
                const n = data.imported || 0;
                const skipped = data.skipped_existing || 0;
                const subs = data.tracks_with_subs || 0;
                pipelineActiveSummary.value = n
                    ? `Imported ${n} subtitle transcript(s)` + (skipped ? `, ${skipped} already had one` : '')
                    : (subs ? `No new imports (${skipped} track(s) already transcribed)` : 'No bundled .vtt/.srt found next to the audio');
                if (n) {
                    pushToast({ kind: 'success', title: `Imported ${n} subtitle transcript(s)`, ttl: 4000 });
                }
                if (pipelineTrackId.value) await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to import subtitles';
                console.error('Failed to import subtitles:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function selectPlayerTranscriptRun(runId) {
            // Switch the active transcript for the track currently open in the Player.
            if (!playerTrackId.value || runId === playerTranscriptRunId.value) return;
            try {
                await fetch(`/api/pipeline/tracks/${playerTrackId.value}/active-transcript/${runId}`, { method: 'PUT' });
            } catch (err) {
                console.error('Failed to set active transcript:', err);
            }
            await loadPlayerTrack(playerTrackId.value, runId, playerTranslationRunId.value);
        }

        function describeTranscriptRun(run) {
            // Short label for the switcher: language + where it came from.
            const lang = (run.language || '?').toUpperCase();
            const src = run.source === 'bundled_subtitle' ? 'subtitle'
                : run.source === 'whisper' ? 'whisper'
                : (run.source || 'manual');
            return `${lang} · ${src}` + (run.segment_count ? ` · ${run.segment_count}` : '');
        }



        async function queueAutoTranscription() {
            if (!pipelineSelectedItemId.value) {
                pipelineLoadError.value = 'Select an item first';
                return;
            }
            // Filter out any stale track IDs (tracks that don't exist in current loaded tracks)
            const validTrackIds = pipelineTracks.value.map(t => t.id);
            const filteredSelection = selectedTracksForTranscription.value.filter(id => validTrackIds.includes(id));

            if (filteredSelection.length === 0) {
                pipelineLoadError.value = 'No valid tracks selected. Try reloading tracks.';
                return;
            }

            // Update selection to only valid IDs
            selectedTracksForTranscription.value = filteredSelection;

            transcriptionInProgress.value = true;
            transcriptionStatus.value = null;
            // Initialize progress bar to show immediately (before first poll)
            transcriptionProgress.value = { total: selectedTracksForTranscription.value.length, completed: 0, current: 'Initializing...' };
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/items/${pipelineSelectedItemId.value}/auto-transcribe`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        language: transcribeLanguage.value,
                        force: false,
                        track_ids: selectedTracksForTranscription.value
                    })
                });
                const data = await resp.json();
                if (!resp.ok) {
                    transcriptionInProgress.value = false;
                    pipelineLoadError.value = data.detail || 'Failed to queue transcription';
                    return;
                }
                transcriptionStatus.value = { status: 'queued', job_id: data.job_id };
                pipelineActiveSummary.value = `Transcription queued for ${data.tracks_queued} tracks (job #${data.job_id})`;
                await pollTranscriptionProgress(data.job_id);
            } catch (err) {
                transcriptionInProgress.value = false;
                pipelineLoadError.value = 'Failed to queue transcription';
                console.error('Failed to queue transcription:', err);
            }
        }

        async function pollTranscriptionProgress(jobId) {
            const maxPolls = 1200; // 10 minutes max with 300ms interval
            let pollCount = 0;
            // Clear any existing interval first to avoid duplicate polling
            if (transcriptionPollInterval) {
                clearInterval(transcriptionPollInterval);
            }
            transcriptionPollInterval = setInterval(async () => {
                pollCount++;
                if (pollCount > maxPolls) {
                    clearInterval(transcriptionPollInterval);
                    transcriptionInProgress.value = false;
                    pipelineLoadError.value = 'Transcription timeout';
                    return;
                }
                try {
                    const resp = await fetch(`/api/pipeline/jobs?job_id=${jobId}`);
                    if (!resp.ok) return;
                    const data = await resp.json();
                    const jobs = data.jobs || [];
                    const job = jobs.find(j => j.id === jobId);
                    if (!job) return;

                    // Update progress - backend now updates more frequently with status descriptions
                    const newProgress = {
                        total: job.total || 0,
                        completed: job.completed || 0,
                        current: job.current || ''
                    };

                    // Only update if something changed to reduce DOM updates
                    if (!transcriptionProgress.value ||
                        transcriptionProgress.value.completed !== newProgress.completed ||
                        transcriptionProgress.value.current !== newProgress.current) {
                        transcriptionProgress.value = newProgress;
                    }

                    transcriptionStatus.value = { status: job.status, job_id: jobId };

                    if (job.status === 'completed' || job.status === 'failed') {
                        clearInterval(transcriptionPollInterval);
                        transcriptionInProgress.value = false;
                        // Final update with visual indicator
                        transcriptionProgress.value = {
                            total: job.total || 0,
                            completed: job.completed || 0,
                            current: job.status === 'completed' ? '✓ Complete' : '✗ Failed'
                        };
                        await refreshPipelineTrackList();
                        if (job.status === 'completed') {
                            pipelineActiveSummary.value = `✓ Transcription complete: ${job.completed || 0} / ${job.total || 0} tracks`;
                        } else {
                            pipelineLoadError.value = `✗ Transcription failed: ${job.error || 'Unknown error'}`;
                        }
                    }
                } catch (err) {
                    console.error('Error polling transcription:', err);
                }
            }, 300);  // Poll every 300ms for smoother real-time updates
        }

        function getTranscriptionProgressPercent() {
            if (!transcriptionProgress.value || !transcriptionProgress.value.current) return 0;
            // Extract percentage from current string like "Transcribing: Track (45%)"
            const match = transcriptionProgress.value.current.match(/\((\d+)%\)/);
            if (match) {
                return parseInt(match[1]);
            }
            // Fallback to completed/total if no percentage in current
            if (transcriptionProgress.value.total > 0) {
                return Math.round((transcriptionProgress.value.completed / transcriptionProgress.value.total) * 100);
            }
            return 0;
        }

        async function cancelTranscription() {
            if (!transcriptionStatus.value || !transcriptionStatus.value.job_id) return;
            const jobId = transcriptionStatus.value.job_id;
            try {
                const resp = await fetch(`/api/pipeline/jobs/${jobId}/stop`, {
                    method: 'POST'
                });
                if (!resp.ok) {
                    pipelineLoadError.value = 'Failed to cancel transcription';
                    return;
                }
                // Clear polling interval immediately to prevent further updates
                if (transcriptionPollInterval) {
                    clearInterval(transcriptionPollInterval);
                }
                transcriptionInProgress.value = false;
                pipelineActiveSummary.value = 'Transcription cancelled';
            } catch (err) {
                console.error('Error cancelling transcription:', err);
                pipelineLoadError.value = 'Failed to cancel transcription: ' + err.message;
            }
        }


        async function selectTranscriptRun(runId) {
            // Card-click handler: a single run is the canonical choice for both
            // the Player (active transcript) and the manual translate form
            // (transcript_run_id input). Bifurcating those — as the old
            // "Set Active" + "Use for TL" buttons did — never produced a useful
            // workflow; you always want them locked together.
            pipelineTranscriptRunId.value = runId;
            await setActiveTranscript(runId);
        }

        async function selectTranslationRun(runId) {
            await setActiveTranslation(runId);
        }

        async function setActiveTranscript(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/active-transcript/${runId}`, { method: 'PUT' });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to set active transcript';
                    return;
                }
                await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to set active transcript';
                console.error('Failed to set active transcript:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function setActiveTranslation(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/active-translation/${runId}`, { method: 'PUT' });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to set active translation';
                    return;
                }
                await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to set active translation';
                console.error('Failed to set active translation:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function queueAutoTranslation() {
            if (!pipelineTrackId.value) {
                pipelineLoadError.value = 'Select a track first';
                return;
            }
            if (!pipelineTranscriptRunId.value) {
                pipelineLoadError.value = 'Set transcript run id first';
                return;
            }
            pipelineBusy.value = true;
            autoTranslateInProgress.value = true;
            autoTranslateProgress.value = null;
            autoTranslateLiveLines.value = [];
            autoTranslateControlBusy.value = false;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/auto-translate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        transcript_run_id: pipelineTranscriptRunId.value,
                        target_language: autoTranslateTargetLanguage.value,
                        provider: autoTranslateProvider.value,
                        model: autoTranslateModel.value,
                        max_tokens_per_chunk: Number(autoTranslateMaxTokens.value || 1000),
                        max_lines_per_chunk: Number(autoTranslateMaxLines.value || 20),
                        max_retries_per_chunk: Number(autoTranslateMaxRetries.value || 2),
                        retry_backoff_seconds: Number(autoTranslateRetryBackoff.value || 1.0),
                        glossary: String(autoTranslateGlossary.value || ''),
                        character_memory: String(autoTranslateCharacterMemory.value || ''),
                        set_active: true,
                    }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to queue auto-translation';
                    return;
                }
                autoTranslateStatus.value = {
                    status: 'queued',
                    job_id: data.job_id,
                    segments_queued: data.segments_queued,
                };
                localStorage.setItem('autoTranslateMaxRetries', String(Number(autoTranslateMaxRetries.value || 2)));
                localStorage.setItem('autoTranslateRetryBackoff', String(Number(autoTranslateRetryBackoff.value || 1.0)));
                pipelineActiveSummary.value = `Auto-translation queued (job #${data.job_id})`;
                await pollAutoTranslationProgress(data.job_id);
            } catch (err) {
                autoTranslateInProgress.value = false;
                pipelineLoadError.value = 'Failed to queue auto-translation';
                console.error('Failed to queue auto-translation:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function pollAutoTranslationProgress(jobId) {
            if (autoTranslatePollInterval) {
                clearInterval(autoTranslatePollInterval);
                autoTranslatePollInterval = null;
            }
            const maxPolls = 900; // 15 minutes max
            let pollCount = 0;
            autoTranslatePollInterval = setInterval(async () => {
                pollCount++;
                if (pollCount > maxPolls) {
                    clearInterval(autoTranslatePollInterval);
                    autoTranslatePollInterval = null;
                    autoTranslateInProgress.value = false;
                    pipelineLoadError.value = 'Auto-translation timeout';
                    return;
                }
                try {
                    const [jobResp, eventsResp] = await Promise.all([
                        fetch(`/api/pipeline/jobs?job_id=${jobId}`),
                        fetch(`/api/pipeline/jobs/${jobId}/events?limit=80`),
                    ]);
                    const resp = jobResp;
                    if (!resp.ok) return;
                    const data = await jobResp.json();
                    const jobs = data.jobs || [];
                    const job = jobs.find(j => j.id === jobId);
                    if (!job) return;

                    if (eventsResp.ok) {
                        const eventsData = await eventsResp.json();
                        const events = Array.isArray(eventsData.events) ? eventsData.events : [];
                        const lines = [];
                        for (const ev of events) {
                            const preview = ev && ev.data && Array.isArray(ev.data.preview_lines) ? ev.data.preview_lines : [];
                            for (const line of preview) {
                                const clean = String(line || '').trim();
                                if (!clean) continue;
                                lines.push(clean);
                            }
                        }
                        autoTranslateLiveLines.value = lines.slice(-24);
                    }

                    autoTranslateStatus.value = {
                        status: job.status,
                        job_id: jobId,
                        segments_queued: job.total || autoTranslateStatus.value?.segments_queued || 0,
                        translation_run_id: job.result_json?.translation_run_id || null,
                    };
                    autoTranslateInProgress.value = ["queued", "running", "paused", "stopping"].includes(String(job.status || "").toLowerCase());

                    // Update progress with smooth updates
                    const newProgress = {
                        total: job.total || 0,
                        completed: job.completed || 0,
                        current: job.current || '',
                        error: job.error || '',
                    };

                    // Only update if something changed
                    if (!autoTranslateProgress.value ||
                        autoTranslateProgress.value.completed !== newProgress.completed ||
                        autoTranslateProgress.value.current !== newProgress.current) {
                        autoTranslateProgress.value = newProgress;
                    }

                    if (job.status === 'completed' || job.status === 'failed' || job.status === 'interrupted') {
                        clearInterval(autoTranslatePollInterval);
                        autoTranslatePollInterval = null;
                        autoTranslateInProgress.value = false;
                        // Final update with visual indicator
                        if (job.status === 'completed') {
                            autoTranslateProgress.value = {
                                total: job.total || 0,
                                completed: job.completed || 0,
                                current: '✓ Complete',
                                error: '',
                            };
                        } else if (job.status === 'interrupted') {
                            autoTranslateProgress.value = {
                                total: job.total || 0,
                                completed: job.completed || 0,
                                current: '⏸ Interrupted',
                                error: job.error || '',
                            };
                        } else {
                            autoTranslateProgress.value = {
                                total: job.total || 0,
                                completed: job.completed || 0,
                                current: '✗ Failed',
                                error: job.error || '',
                            };
                        }
                        await loadPipelineRuns();
                        // Soft-refresh just the track list so the 📝 / 🌐 badge
                        // counts update — without resetting the user's current
                        // selection or the runs/segments they're looking at.
                        await refreshPipelineTrackList();
                        if (job.status === 'completed') {
                            const runId = job.result_json?.translation_run_id;
                            if (runId) {
                                pipelineActiveSummary.value = `✓ Auto-translation complete (run #${runId})`;
                            } else {
                                pipelineActiveSummary.value = '✓ Auto-translation complete';
                            }
                        } else if (job.status === 'interrupted') {
                            pipelineLoadError.value = `⏸ Auto-translation interrupted (${job.completed || 0} chunks completed)`;
                        } else {
                            pipelineLoadError.value = `✗ Auto-translation failed: ${job.error || 'Unknown error'}`;
                        }
                    }
                } catch (err) {
                    console.error('Error polling auto-translation progress:', err);
                }
            }, 300);  // Poll every 300ms for smoother real-time updates
        }

        async function controlAutoTranslation(action) {
            const jobId = Number(autoTranslateStatus.value?.job_id || 0);
            if (!jobId) return;
            autoTranslateControlBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/jobs/${jobId}/${action}`, { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || `Failed to ${action} translation`;
                    return;
                }
                autoTranslateStatus.value = { ...(autoTranslateStatus.value || {}), status: data.status || action, job_id: jobId };
            } catch (err) {
                pipelineLoadError.value = `Failed to ${action} translation`;
                console.error(`Failed to ${action} translation:`, err);
            } finally {
                autoTranslateControlBusy.value = false;
            }
        }

        function cleanTranscriptLineForTranslation(raw) {
            let line = String(raw || '').trim();
            if (!line) return '';
            if (/^\s*[0-9\uFF10-\uFF19]+\s*$/.test(line)) return '';
            if (/-->/.test(line)) return '';
            line = line.replace(/<\d{2}:\d{2}:\d{2}(?:[.,]\d{1,3})?>/g, '');
            line = line.replace(/<\/?[^>]+>/g, '');
            line = line.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&');
            line = line.replace(/^\s*[-\u2013\u2014]?\s*(?:[A-Za-z][A-Za-z0-9_ .-]{0,30}|[\u3040-\u30ff\u3400-\u9fff]{1,20})\s*[:\uFF1A]\s*/, '');
            line = line.replace(/\s+/g, ' ').trim();
            if (!line) return '';
            if (/^\s*[\[(\uFF08\u3010].*?[\])\uFF09\u3011]\s*$/.test(line)) return '';
            return line;
        }

        function buildCleanTextFromSegments(segments) {
            if (!Array.isArray(segments) || segments.length === 0) return '';
            const lines = [];
            const seen = new Set();
            for (const seg of segments) {
                const cleaned = cleanTranscriptLineForTranslation(seg && seg.text);
                if (!cleaned) continue;
                if (seen.has(cleaned)) continue;
                seen.add(cleaned);
                lines.push(cleaned);
            }
            return lines.join('\n');
        }

        async function loadTranscriptRunDetail(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/transcripts/${runId}`);
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to load transcript run detail';
                    return;
                }
                selectedTranscriptRunId.value = runId;
                selectedTranscriptSegments.value = Array.isArray(data.segments) ? data.segments : [];
                const cleanFromApi = String(data.clean_source?.text || '');
                selectedTranscriptCleanText.value = cleanFromApi || buildCleanTextFromSegments(selectedTranscriptSegments.value);
            } catch (err) {
                pipelineLoadError.value = 'Failed to load transcript run detail';
                console.error('Failed to load transcript run detail:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function copyCleanTranscriptSource() {
            if (!selectedTranscriptCleanText.value) {
                pipelineLoadError.value = 'Load a transcript run first';
                return;
            }
            pipelineLoadError.value = '';
            const textToCopy = String(selectedTranscriptCleanText.value || '');
            try {
                let copied = false;
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(textToCopy);
                    copied = true;
                } else {
                    const temp = document.createElement('textarea');
                    temp.value = textToCopy;
                    temp.setAttribute('readonly', '');
                    temp.style.position = 'fixed';
                    temp.style.left = '-9999px';
                    temp.style.top = '0';
                    document.body.appendChild(temp);
                    temp.focus();
                    temp.select();
                    copied = document.execCommand('copy');
                    document.body.removeChild(temp);
                }

                if (!copied) {
                    pipelineLoadError.value = 'Copy blocked by browser. Select text in the box and press Ctrl+C.';
                    return;
                }

                pipelineActiveSummary.value = `Copied clean source for run #${selectedTranscriptRunId.value || '-'} (${textToCopy.length} chars)`;
            } catch (err) {
                pipelineLoadError.value = 'Copy failed. Select text in the box and press Ctrl+C.';
                console.error('Clipboard copy failed:', err);
            }
        }

        async function loadTranslationRunDetail(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            pipelineLoadError.value = '';
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/translations/${runId}`);
                const data = await resp.json();
                if (!resp.ok) {
                    pipelineLoadError.value = data.detail || 'Failed to load translation run detail';
                    return;
                }
                selectedTranslationSegments.value = Array.isArray(data.segments) ? data.segments : [];
                selectedTranslationRunId.value = runId;
            } catch (err) {
                pipelineLoadError.value = 'Failed to load translation run detail';
                console.error('Failed to load translation run detail:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        function closeSegmentViewer() {
            selectedTranscriptSegments.value = [];
            selectedTranslationSegments.value = [];
            selectedTranslationRunId.value = null;
            cancelSegmentEdit();
        }

        // ---- Inline segment editor ---------------------------------------
        // Shared across Player and Workshop views — only one segment can be
        // edited at a time. Saves PATCH to the API, then patches the matching
        // segment(s) in any loaded array (player or workshop) so both views
        // stay consistent without re-fetching.
        function startSegmentEdit(surface, kind, idx, currentText) {
            editingSegmentError.value = '';
            editingSegment.value = { surface, kind, idx };
            editingSegmentText.value = String(currentText || '');
        }

        function cancelSegmentEdit() {
            editingSegment.value = null;
            editingSegmentText.value = '';
            editingSegmentSaving.value = false;
            editingSegmentError.value = '';
        }

        function isEditingSegment(surface, kind, idx) {
            const e = editingSegment.value;
            return !!e && e.surface === surface && e.kind === kind && e.idx === idx;
        }

        function _patchLocalSegmentText(arr, segmentIndex, newText) {
            if (!Array.isArray(arr)) return;
            for (let i = 0; i < arr.length; i++) {
                const seg = arr[i];
                if (seg && Number(seg.segment_index) === Number(segmentIndex)) {
                    const meta = Object.assign({}, seg.meta_json || {}, { edited: true });
                    arr[i] = Object.assign({}, seg, { text: newText, meta_json: meta });
                }
            }
        }

        async function saveSegmentEdit() {
            const e = editingSegment.value;
            if (!e) return;
            const text = String(editingSegmentText.value || '');

            // Resolve which (trackId, runId) to PATCH. We accept the edit from
            // either surface and pick the right run id for the kind.
            let trackId = null;
            let runId = null;
            if (e.kind === 'transcript') {
                trackId = e.surface === 'player' ? playerTrackId.value : pipelineTrackId.value;
                runId = e.surface === 'player' ? playerTranscriptRunId.value : selectedTranscriptRunId.value;
            } else {
                trackId = e.surface === 'player' ? playerTrackId.value : pipelineTrackId.value;
                runId = e.surface === 'player' ? playerTranslationRunId.value : selectedTranslationRunId.value;
            }
            if (!trackId || !runId) {
                editingSegmentError.value = 'Missing track or run id';
                return;
            }

            // Look up segment_index from the source array (idx is array index,
            // segment_index is the stable DB key — usually the same but not
            // guaranteed).
            let sourceArr = null;
            if (e.surface === 'player') {
                sourceArr = e.kind === 'transcript'
                    ? playerTranscriptSegments.value
                    : playerTranslationSegments.value;
            } else {
                sourceArr = e.kind === 'transcript'
                    ? selectedTranscriptSegments.value
                    : selectedTranslationSegments.value;
            }
            const seg = sourceArr && sourceArr[e.idx];
            if (!seg) {
                editingSegmentError.value = 'Segment not found';
                return;
            }
            const segmentIndex = Number(seg.segment_index);

            editingSegmentSaving.value = true;
            editingSegmentError.value = '';
            try {
                const url = e.kind === 'transcript'
                    ? `/api/pipeline/tracks/${trackId}/transcripts/${runId}/segments/${segmentIndex}`
                    : `/api/pipeline/tracks/${trackId}/translations/${runId}/segments/${segmentIndex}`;
                const resp = await fetch(url, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    editingSegmentError.value = data.detail || `Save failed (${resp.status})`;
                    editingSegmentSaving.value = false;
                    return;
                }
                // Patch every loaded copy of this segment so both player and
                // workshop reflect the edit (if the same run is open in both).
                if (e.kind === 'transcript') {
                    if (playerTranscriptRunId.value === runId) {
                        _patchLocalSegmentText(playerTranscriptSegments.value, segmentIndex, text);
                    }
                    if (selectedTranscriptRunId.value === runId) {
                        _patchLocalSegmentText(selectedTranscriptSegments.value, segmentIndex, text);
                    }
                } else {
                    if (playerTranslationRunId.value === runId) {
                        _patchLocalSegmentText(playerTranslationSegments.value, segmentIndex, text);
                    }
                    if (selectedTranslationRunId.value === runId) {
                        _patchLocalSegmentText(selectedTranslationSegments.value, segmentIndex, text);
                    }
                }
                cancelSegmentEdit();
            } catch (err) {
                console.error('Failed to save segment edit:', err);
                editingSegmentError.value = 'Save failed';
                editingSegmentSaving.value = false;
            }
        }

        function segmentIsEdited(seg) {
            return !!(seg && seg.meta_json && seg.meta_json.edited);
        }

        function toggleGlossaryExpanded() {
            glossaryExpanded.value = !glossaryExpanded.value;
            localStorage.setItem('glossaryExpanded', glossaryExpanded.value ? '1' : '0');
        }

        function togglePlayerEditMode() {
            playerEditMode.value = !playerEditMode.value;
            localStorage.setItem('player_edit_mode', playerEditMode.value ? '1' : '0');
            // Turning the toggle off mid-edit shouldn't leave a stale buffer
            // hanging on the line, so cancel any in-progress player edit.
            if (!playerEditMode.value && editingSegment.value && editingSegment.value.surface === 'player') {
                cancelSegmentEdit();
            }
        }

        // ---- Inline delete-confirm pattern -------------------------------
        // Clicking the trash icon on a run card flips the actions into a
        // ✓ / ✗ pair (no native dialog). Click ✓ to confirm, ✗ to back out,
        // or click another card to dismiss. Only one pending state per kind
        // at a time so the UI never has two unresolved confirmations open.
        function askDeleteTranscriptRun(runId) {
            pendingDeleteTranscriptRunId.value = runId;
        }
        function cancelDeleteTranscriptRun() {
            pendingDeleteTranscriptRunId.value = null;
        }
        async function confirmDeleteTranscriptRun(runId) {
            pendingDeleteTranscriptRunId.value = null;
            await deleteTranscriptRun(runId);
        }
        function askDeleteTranslationRun(runId) {
            pendingDeleteTranslationRunId.value = runId;
        }
        function cancelDeleteTranslationRun() {
            pendingDeleteTranslationRunId.value = null;
        }
        async function confirmDeleteTranslationRun(runId) {
            pendingDeleteTranslationRunId.value = null;
            await deleteTranslationRun(runId);
        }

        // ---- Relative timestamps -----------------------------------------
        // Under 6h → "X minutes/hours ago"; past that → DD/MM/YYYY HH:MM.
        // Hover tooltip always shows precise DD/MM/YYYY HH:MM:SS. Static at
        // render — refreshes only when the run list reloads.
        function formatRunTimestamp(isoString) {
            if (!isoString) return '';
            const then = new Date(isoString);
            if (isNaN(then.getTime())) return '';
            const diffSec = Math.floor((Date.now() - then.getTime()) / 1000);
            if (diffSec < 30) return 'just now';
            if (diffSec < 60) return `${diffSec} seconds ago`;
            const diffMin = Math.floor(diffSec / 60);
            if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`;
            const diffHrs = Math.floor(diffMin / 60);
            if (diffHrs < 6) return `${diffHrs} hour${diffHrs === 1 ? '' : 's'} ago`;
            return formatRunTimestampShort(then);
        }
        function formatRunTimestampShort(d) {
            const dd = String(d.getDate()).padStart(2, '0');
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const yyyy = d.getFullYear();
            const hh = String(d.getHours()).padStart(2, '0');
            const min = String(d.getMinutes()).padStart(2, '0');
            return `${dd}/${mm}/${yyyy} ${hh}:${min}`;
        }
        function formatRunTimestampPrecise(isoString) {
            if (!isoString) return '';
            const d = new Date(isoString);
            if (isNaN(d.getTime())) return '';
            const dd = String(d.getDate()).padStart(2, '0');
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const yyyy = d.getFullYear();
            const hh = String(d.getHours()).padStart(2, '0');
            const min = String(d.getMinutes()).padStart(2, '0');
            const sec = String(d.getSeconds()).padStart(2, '0');
            return `${dd}/${mm}/${yyyy} ${hh}:${min}:${sec}`;
        }

        function askCleanupUnusedTranscripts() {
            if (!pipelineSelectedItemId.value) return;
            pendingCleanupUnusedTranscripts.value = true;
        }
        function cancelCleanupUnusedTranscripts() {
            pendingCleanupUnusedTranscripts.value = false;
        }
        async function confirmCleanupUnusedTranscripts() {
            pendingCleanupUnusedTranscripts.value = false;
            await deleteRedundantTranscriptRunsForItem();
        }

        async function deleteRedundantTranscriptRunsForItem() {
            const itemId = pipelineSelectedItemId.value;
            if (!itemId) return;
            pipelineBusy.value = true;
            try {
                const resp = await fetch(`/api/pipeline/items/${itemId}/transcripts/redundant`, {
                    method: 'DELETE'
                });
                const data = await resp.json();
                if (!resp.ok) {
                    alert(`Failed: ${data.detail || 'Unknown error'}`);
                    return;
                }
                pipelineLoadError.value = '';
                pushToast({
                    kind: 'success',
                    title: 'Unused transcripts cleaned',
                    body: `${data.deleted_count} run${data.deleted_count === 1 ? '' : 's'} removed`,
                    ttl: 4000,
                });
                await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to clean up unused transcripts';
                console.error('Cleanup failed:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function deleteTranscriptRun(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/transcripts/${runId}`, {
                    method: 'DELETE'
                });
                const data = await resp.json();
                if (!resp.ok) {
                    alert(`Failed to delete: ${data.detail || 'Unknown error'}`);
                    return;
                }
                pipelineLoadError.value = '';
                pushToast({ kind: 'success', title: 'Transcript deleted', ttl: 3000 });
                await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to delete transcript';
                console.error('Delete failed:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        async function deleteTranslationRun(runId) {
            if (!pipelineTrackId.value) return;
            pipelineBusy.value = true;
            try {
                const resp = await fetch(`/api/pipeline/tracks/${pipelineTrackId.value}/translations/${runId}`, {
                    method: 'DELETE'
                });
                const data = await resp.json();
                if (!resp.ok) {
                    alert(`Failed to delete: ${data.detail || 'Unknown error'}`);
                    return;
                }
                pipelineLoadError.value = '';
                pushToast({ kind: 'success', title: 'Translation deleted', ttl: 3000 });
                await loadPipelineRuns();
            } catch (err) {
                pipelineLoadError.value = 'Failed to delete translation';
                console.error('Delete failed:', err);
            } finally {
                pipelineBusy.value = false;
            }
        }

        // Scan
        // Sidebar Scan button — dispatches to the right scanner based on
        // the active library subtab. Drama CDs use the streaming startScan
        // (with progress overlay); games + tokutens use their one-shot
        // catalog walks (results pop into the grid after).
        function scanActiveSubtab() {
            const s = librarySubtab.value;
            if (s === 'games') return scanGames();
            if (s === 'tokutens') return scanTokutens();
            return startScan();
        }
        const anyScanRunning = computed(() =>
            !!scanRunning.value || !!gamesScanning.value || !!tokutenScanning.value
        );
        const activeSubtabLabel = computed(() => {
            const s = librarySubtab.value;
            if (s === 'games') return 'Games';
            if (s === 'tokutens') return 'Tokutens';
            return 'Drama CDs';
        });

        async function startScan() {
            scanRunning.value = true;
            scanProgress.total = 0;
            scanProgress.processed = 0;
            scanProgress.current = null;
            scanProgress.paused = false;
            scanProgress.stopping = false;
            try {
                const requestBody = { recursive: scanRecursive.value };
                if (scanPaths.value.length) {
                    requestBody.paths = scanPaths.value;
                }

                await fetch('/api/scan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody),
                });
                await pollScanStatus();
            } catch (err) {
                console.error('Scan failed:', err);
            }
        }

        async function pollScanStatus() {
            while (true) {
                await new Promise(r => setTimeout(r, 1000));
                try {
                    const resp = await fetch('/api/scan/status');
                    const data = await resp.json();

                    scanProgress.total = data.total_files || 0;
                    scanProgress.processed = data.processed_files || 0;
                    scanProgress.current = data.current || null;
                    scanProgress.paused = !!data.paused;
                    scanProgress.stopping = !!data.stopping;

                    if (!data.running) {
                        scanRunning.value = false;
                        localStorage.removeItem('scanRunning');
                        localStorage.removeItem('scanProgress');
                        await loadItems();
                        await loadStats();
                        break;
                    }
                } catch {
                    break;
                }
            }
        }

        async function pauseScan() {
            if (!scanRunning.value) return;
            try {
                await fetch('/api/scan/pause', { method: 'POST' });
                scanProgress.paused = true;
            } catch (err) {
                console.error('Pause scan failed:', err);
            }
        }

        async function resumeScan() {
            if (!scanRunning.value) return;
            try {
                await fetch('/api/scan/resume', { method: 'POST' });
                scanProgress.paused = false;
            } catch (err) {
                console.error('Resume scan failed:', err);
            }
        }

        async function stopScan() {
            if (!scanRunning.value) return;
            try {
                scanProgress.stopping = true;
                scanProgress.paused = false;
                await fetch('/api/scan/stop', { method: 'POST' });
            } catch (err) {
                console.error('Stop scan failed:', err);
            }
        }

        // Fetch metadata
        async function startFetch() {
            fetchRunning.value = true;
            fetchProgress.total = 0;
            fetchProgress.completed = 0;
            fetchProgress.current = null;
            fetchProgress.paused = false;
            fetchProgress.stopping = false;
            fetchProgress.stopped = false;
            lastFetchSummary.value = null;

            try {
                await fetch('/api/fetch-metadata', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: false }),
                });
                await pollFetchStatus();
            } catch (err) {
                console.error('Fetch failed:', err);
            }
        }

        async function pollFetchStatus() {
            while (true) {
                await new Promise(r => setTimeout(r, 1500));
                try {
                    const resp = await fetch('/api/fetch-metadata/status');
                    const data = await resp.json();
                    fetchProgress.total = data.total;
                    fetchProgress.completed = data.completed;
                    fetchProgress.current = data.current;
                    fetchProgress.paused = !!data.paused;
                    fetchProgress.stopping = !!data.stopping;
                    fetchProgress.stopped = !!data.stopped;
                    lastFetchSummary.value = {
                        total: data.total || 0,
                        completed: data.completed || 0,
                        success: data.success || 0,
                        failed: data.failed || 0,
                        skipped: data.skipped || 0,
                        error_summary: data.error_summary || {},
                        finished_at: data.finished_at || null,
                    };

                    if (!data.running) {
                        fetchRunning.value = false;
                        // Clear localStorage when fetch completes
                        localStorage.removeItem('fetchRunning');
                        localStorage.removeItem('fetchProgress');
                        await loadItems();
                        await loadStats();
                        await loadFilterOptions();
                        break;
                    }
                } catch {
                    break;
                }
            }
        }

        async function pauseFetch() {
            if (!fetchRunning.value) return;
            try {
                await fetch('/api/fetch-metadata/pause', { method: 'POST' });
                fetchProgress.paused = true;
            } catch (err) {
                console.error('Pause fetch failed:', err);
            }
        }

        async function resumeFetch() {
            if (!fetchRunning.value) return;
            try {
                await fetch('/api/fetch-metadata/resume', { method: 'POST' });
                fetchProgress.paused = false;
            } catch (err) {
                console.error('Resume fetch failed:', err);
            }
        }

        async function stopFetch() {
            if (!fetchRunning.value) return;
            try {
                fetchProgress.stopping = true;
                fetchProgress.paused = false;
                await fetch('/api/fetch-metadata/stop', { method: 'POST' });
            } catch (err) {
                console.error('Stop fetch failed:', err);
            }
        }

        // Detail panel
        // Library card click router. Default click opens the detail panel;
        // Ctrl/Cmd+click toggles multi-select instead. Replaces the old
        // corner-checkbox UI with a card-level selected state (pink border + glow).
        // Modifier rules (drama_cd / tokuten subtabs):
        //   - Plain click → opens detail panel (existing behavior).
        //   - Ctrl/Cmd+click → toggles selection (enters multi-select mode).
        //   - Shift+click → range-select from last-clicked to current.
        //   - Once in selection mode, plain click also toggles (no panel).
        const lastClickedItemId = ref(null);
        function onCardClick(item, evt) {
            if (evt && evt.shiftKey && lastClickedItemId.value != null) {
                evt.preventDefault();
                rangeSelectItems(lastClickedItemId.value, item.id);
                lastClickedItemId.value = item.id;
                return;
            }
            if (evt && (evt.ctrlKey || evt.metaKey)) {
                evt.preventDefault();
                toggleSelectItem(item.id);
                lastClickedItemId.value = item.id;
                return;
            }
            // If anything is already selected, treat plain clicks as additional
            // selection toggles instead of opening detail. Click a single
            // selected card to deselect it (and exit selection mode).
            if (selectedIds.value.size > 0) {
                toggleSelectItem(item.id);
                lastClickedItemId.value = item.id;
                return;
            }
            lastClickedItemId.value = item.id;
            openDetail(item);
        }
        function rangeSelectItems(anchorId, targetId) {
            // Build the range between anchorId and targetId from the current
            // items.value ordering (whatever sort/filter is applied). The
            // anchor itself is included; we add to the existing selection
            // rather than replacing — matches how shift-click works in most
            // file managers when already in a multi-select.
            const arr = items.value;
            const ai = arr.findIndex(i => i.id === anchorId);
            const ti = arr.findIndex(i => i.id === targetId);
            if (ai < 0 || ti < 0) return;
            const lo = Math.min(ai, ti);
            const hi = Math.max(ai, ti);
            const next = new Set(selectedIds.value);
            for (let k = lo; k <= hi; k++) next.add(arr[k].id);
            selectedIds.value = next;
        }

        // === Games selection (parallel to selectedIds for drama_cds) ===
        const selectedGameIds = ref(new Set());
        const lastClickedGameId = ref(null);
        function isGameSelected(gameId) {
            return selectedGameIds.value.has(gameId);
        }
        function toggleSelectGame(gameId) {
            const next = new Set(selectedGameIds.value);
            if (next.has(gameId)) next.delete(gameId); else next.add(gameId);
            selectedGameIds.value = next;
        }
        function clearGameSelection() {
            selectedGameIds.value = new Set();
            lastClickedGameId.value = null;
        }
        function rangeSelectGames(anchorId, targetId) {
            const arr = gamesItems.value;
            const ai = arr.findIndex(g => g.id === anchorId);
            const ti = arr.findIndex(g => g.id === targetId);
            if (ai < 0 || ti < 0) return;
            const lo = Math.min(ai, ti);
            const hi = Math.max(ai, ti);
            const next = new Set(selectedGameIds.value);
            for (let k = lo; k <= hi; k++) next.add(arr[k].id);
            selectedGameIds.value = next;
        }
        function onGameCardClick(game, evt) {
            if (evt && evt.shiftKey && lastClickedGameId.value != null) {
                evt.preventDefault();
                rangeSelectGames(lastClickedGameId.value, game.id);
                lastClickedGameId.value = game.id;
                return;
            }
            if (evt && (evt.ctrlKey || evt.metaKey)) {
                evt.preventDefault();
                toggleSelectGame(game.id);
                lastClickedGameId.value = game.id;
                return;
            }
            if (selectedGameIds.value.size > 0) {
                toggleSelectGame(game.id);
                lastClickedGameId.value = game.id;
                return;
            }
            lastClickedGameId.value = game.id;
            openGameDetail(game);
        }

        // === Rubber-band drag selection ===
        // mousedown on empty grid space starts the rectangle. mousemove
        // updates its size + previews intersecting cards. mouseup finalizes
        // the selection. The rectangle is rendered via the .drag-rubberband
        // CSS class positioned absolutely inside the .grid container.
        const dragSelectActive = ref(false);
        const dragSelectRect = ref(null);
        let _dragStart = null;
        let _dragGridEl = null;
        let _dragAdditive = false;
        function onGridMouseDown(evt) {
            // Only start a drag-select if the mousedown landed on the grid
            // background itself (not on a card, button, or pill). The card
            // root handles its own click events; a target deeper than that
            // means the user is interacting with a card.
            if (evt.button !== 0) return;
            if (evt.target.closest('.card')) return;
            if (evt.target.closest('.list-row')) return;
            const gridEl = evt.currentTarget;
            _dragGridEl = gridEl;
            _dragAdditive = evt.ctrlKey || evt.metaKey || evt.shiftKey;
            const rect = gridEl.getBoundingClientRect();
            const x = evt.clientX - rect.left + gridEl.scrollLeft;
            const y = evt.clientY - rect.top + gridEl.scrollTop;
            _dragStart = { x, y };
            dragSelectActive.value = true;
            dragSelectRect.value = { x, y, w: 0, h: 0 };
            window.addEventListener('mousemove', onGridDragMove);
            window.addEventListener('mouseup', onGridDragEnd, { once: true });
        }
        function onGridDragMove(evt) {
            if (!_dragStart || !_dragGridEl) return;
            const rect = _dragGridEl.getBoundingClientRect();
            const x = evt.clientX - rect.left + _dragGridEl.scrollLeft;
            const y = evt.clientY - rect.top + _dragGridEl.scrollTop;
            const nx = Math.min(_dragStart.x, x);
            const ny = Math.min(_dragStart.y, y);
            const nw = Math.abs(x - _dragStart.x);
            const nh = Math.abs(y - _dragStart.y);
            dragSelectRect.value = { x: nx, y: ny, w: nw, h: nh };
        }
        function onGridDragEnd() {
            window.removeEventListener('mousemove', onGridDragMove);
            const rect = dragSelectRect.value;
            dragSelectActive.value = false;
            dragSelectRect.value = null;
            if (!_dragGridEl || !rect || (rect.w < 4 && rect.h < 4)) {
                _dragStart = null;
                _dragGridEl = null;
                return;
            }
            // Determine intersecting card elements and translate to ids.
            const cards = _dragGridEl.querySelectorAll('[data-card-id]');
            const gridRect = _dragGridEl.getBoundingClientRect();
            const dragX = rect.x - _dragGridEl.scrollLeft;
            const dragY = rect.y - _dragGridEl.scrollTop;
            const x2 = dragX + rect.w;
            const y2 = dragY + rect.h;
            const sub = librarySubtab.value;
            const targetSet = (sub === 'games')
                ? new Set(_dragAdditive ? selectedGameIds.value : [])
                : new Set(_dragAdditive ? selectedIds.value : []);
            for (const c of cards) {
                const cr = c.getBoundingClientRect();
                const cx1 = cr.left - gridRect.left;
                const cy1 = cr.top - gridRect.top;
                const cx2 = cx1 + cr.width;
                const cy2 = cy1 + cr.height;
                if (cx2 < dragX || cx1 > x2 || cy2 < dragY || cy1 > y2) continue;
                const id = Number(c.getAttribute('data-card-id'));
                if (!Number.isFinite(id)) continue;
                targetSet.add(id);
            }
            if (sub === 'games') selectedGameIds.value = targetSet;
            else selectedIds.value = targetSet;
            _dragStart = null;
            _dragGridEl = null;
        }

        function openDetail(item) {
            // Reset edit state when switching to a new item. Otherwise the
            // previous item's draft (and the open edit panel) carries over
            // and the user sees the wrong fields on the new selection.
            detailEditing.value = false;
            detailDraft.value = null;
            detailSaveError.value = '';
            _resetMetaFetchState();
            selectedItem.value = { ...item };
            overrideCodeInput.value = '';
            overrideError.value = '';
            overrideSuccess.value = '';
            showOverrideSection.value = false;
            showCoverSection.value = false;
            confirmError.value = '';
            confirmSuccess.value = '';
            confirmLoading.value = false;
            unconfirmLoading.value = false;
            coverUploadLoading.value = false;
            coverUploadError.value = '';
            coverUploadSuccess.value = '';
            metadataTranslateLoading.value = false;
            metadataTranslateError.value = '';
            metadataTranslateSuccess.value = '';
            metadataTranslateStep.value = '';
            metadataTranslateElapsedSec.value = 0;
            if (metadataTranslateTimer) {
                clearInterval(metadataTranslateTimer);
                metadataTranslateTimer = null;
            }

            // Check if item has playable content (tracks with transcripts/translations)
            checkItemPlayableContent(item.id);

            // Tokuten ↔ game cross-link: load the tokuten row (for vndb_id)
            // and, when set, look up the matching local game so the detail
            // panel can render a clickable "linked game" pill.
            linkedTokutenForItem.value = null;
            linkedGameForTokuten.value = null;
            if (item.kind === 'tokuten_audio' && item.tokuten_id) {
                _loadLinkedTokutenForItem(item.tokuten_id);
            }
        }
        async function _loadLinkedTokutenForItem(tokutenId) {
            try {
                const resp = await fetch('/api/tokutens/' + tokutenId);
                if (!resp.ok) return;
                const tk = await resp.json();
                linkedTokutenForItem.value = tk;
                if (tk.vndb_id) {
                    _loadLinkedGameForTokuten(tk.vndb_id);
                }
            } catch (_) {}
        }
        async function _loadLinkedGameForTokuten(vndbId) {
            try {
                const resp = await fetch('/api/games?vndb_id=' + encodeURIComponent(vndbId) + '&limit=1&include_wishlist=true');
                if (!resp.ok) return;
                const data = await resp.json();
                linkedGameForTokuten.value = (data.items && data.items[0]) || null;
            } catch (_) {}
        }

        async function checkItemPlayableContent(itemId) {
            if (!pipelineEnabled.value) {
                itemHasPlayableContent.value = false;
                return;
            }

            try {
                checkingPlayableContent.value = true;
                itemHasPlayableContent.value = false;

                // Load tracks for this item
                const resp = await fetch(`/api/pipeline/items/${itemId}/tracks`);
                if (!resp.ok) {
                    checkingPlayableContent.value = false;
                    return;
                }

                const data = await resp.json();
                const tracks = data.tracks || [];

                // Check if any track has transcripts or translations
                for (const track of tracks) {
                    const [transcriptResp, translationResp] = await Promise.all([
                        fetch(`/api/pipeline/tracks/${track.id}/transcripts`),
                        fetch(`/api/pipeline/tracks/${track.id}/translations`)
                    ]);

                    if (transcriptResp.ok) {
                        const transcriptData = await transcriptResp.json();
                        if (transcriptData.runs && transcriptData.runs.length > 0) {
                            itemHasPlayableContent.value = true;
                            checkingPlayableContent.value = false;
                            return;
                        }
                    }

                    if (translationResp.ok) {
                        const translationData = await translationResp.json();
                        if (translationData.runs && translationData.runs.length > 0) {
                            itemHasPlayableContent.value = true;
                            checkingPlayableContent.value = false;
                            return;
                        }
                    }
                }

                checkingPlayableContent.value = false;
            } catch (err) {
                console.error('Failed to check playable content:', err);
                itemHasPlayableContent.value = false;
                checkingPlayableContent.value = false;
            }
        }

        async function playItemInPlayer() {
            if (!selectedItem.value) return;

            playerItemId.value = selectedItem.value.id;
            selectedWorkshopItem.value = selectedItem.value;
            activeTab.value = 'player';
            closeDetail();

            // Explicitly load tracks (no watcher)
            await loadPlayerItemTracks();
        }

        function closeDetail() {
            if (metadataTranslateTimer) {
                clearInterval(metadataTranslateTimer);
                metadataTranslateTimer = null;
            }
            // Tear down edit-panel state too — a stale draft from this
            // item would otherwise re-render on the next openDetail().
            detailEditing.value = false;
            detailDraft.value = null;
            detailSaveError.value = '';
            _resetMetaFetchState();
            selectedItem.value = null;
        }

        async function translateSelectedMetadata() {
            if (!selectedItem.value) return;
            metadataTranslateLoading.value = true;
            metadataTranslateError.value = '';
            metadataTranslateSuccess.value = '';
            metadataTranslateStep.value = 'Sending request...';
            metadataTranslateElapsedSec.value = 0;
            if (metadataTranslateTimer) {
                clearInterval(metadataTranslateTimer);
                metadataTranslateTimer = null;
            }
            metadataTranslateTimer = setInterval(() => {
                metadataTranslateElapsedSec.value += 1;
            }, 1000);
            try {
                metadataTranslateStep.value = 'Waiting for model response...';
                const resp = await fetch(`/api/items/${selectedItem.value.id}/translate-metadata`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });
                const data = await resp.json();
                if (!resp.ok) {
                    const detail = String(data.detail || '').trim();
                    if (resp.status === 429) {
                        metadataTranslateError.value = `Rate limit/quota hit: ${detail || 'Try again shortly or switch provider/model.'}`;
                    } else if (resp.status >= 500) {
                        metadataTranslateError.value = `Translation service error: ${detail || 'Please retry.'}`;
                    } else {
                        metadataTranslateError.value = detail || 'Metadata translation failed';
                    }
                    return;
                }
                metadataTranslateStep.value = 'Saving translated metadata...';
                const updated = data.item || data;
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };
                const providerUsed = String(data.provider_used || '').trim();
                const fallbackUsed = !!data.fallback_used;
                metadataTranslateSuccess.value = providerUsed
                    ? `EN title/description updated (${providerUsed}${fallbackUsed ? ', fallback used' : ''})`
                    : 'EN title/description updated';
                metadataTranslateStep.value = 'Done';
            } catch (err) {
                metadataTranslateError.value = 'Metadata translation failed (network or server issue)';
                metadataTranslateStep.value = '';
                console.error('Metadata translation failed:', err);
            } finally {
                metadataTranslateLoading.value = false;
                if (metadataTranslateTimer) {
                    clearInterval(metadataTranslateTimer);
                    metadataTranslateTimer = null;
                }
            }
        }

        // User actions
        async function saveItemField(field, value) {
            if (!selectedItem.value) return;
            const body = {};
            body[field] = value;

            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const updated = await resp.json();
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };
            } catch (err) {
                console.error('Save failed:', err);
            }
        }

        function setRating(n) {
            const newRating = selectedItem.value.rating === n ? 0 : n;
            selectedItem.value.rating = newRating;
            saveItemField('rating', newRating);
        }

        async function toggleFavorite(item) {
            const newVal = !item.favorite;
            item.favorite = newVal ? 1 : 0;

            try {
                const resp = await fetch(`/api/items/${item.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ favorite: newVal }),
                });
                const updated = await resp.json();
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                if (selectedItem.value && selectedItem.value.id === updated.id) {
                    selectedItem.value = { ...updated };
                }
            } catch (err) {
                console.error('Toggle favorite failed:', err);
            }
        }

        function saveNotes() {
            saveItemField('notes', selectedItem.value.notes);
        }

        function addCustomTag() {
            const tag = newCustomTag.value.trim();
            if (!tag) return;

            const current = parseJson(selectedItem.value.custom_tags);
            if (current.includes(tag)) {
                newCustomTag.value = '';
                return;
            }

            const updated = [...current, tag];
            selectedItem.value.custom_tags = JSON.stringify(updated);
            newCustomTag.value = '';
            saveItemField('custom_tags', updated);
        }

        function removeCustomTag(idx) {
            const current = parseJson(selectedItem.value.custom_tags);
            current.splice(idx, 1);
            selectedItem.value.custom_tags = JSON.stringify(current);
            saveItemField('custom_tags', current);
        }

        // Override product code
        async function overrideProductCode() {
            if (!selectedItem.value || !overrideCodeInput.value.trim()) return;

            overrideLoading.value = true;
            overrideError.value = '';
            overrideSuccess.value = '';

            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/override-code`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ product_code: overrideCodeInput.value.trim() }),
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    overrideError.value = err.detail || 'Override failed';
                    return;
                }

                const updated = await resp.json();
                // Update in grid
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };

                overrideSuccess.value = `Code changed to ${updated.product_code}. Fetching metadata...`;
                overrideCodeInput.value = '';

                // Poll for metadata to appear (background fetch)
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    try {
                        const itemResp = await fetch(`/api/items/${updated.id}`);
                        const refreshed = await itemResp.json();
                        if (refreshed.title) {
                            // Metadata arrived
                            const idx2 = items.value.findIndex(i => i.id === refreshed.id);
                            if (idx2 >= 0) items.value[idx2] = refreshed;
                            selectedItem.value = { ...refreshed };
                            overrideSuccess.value = `Updated: ${refreshed.title}`;
                            clearInterval(pollInterval);
                        }
                    } catch {}
                    if (attempts > 20) clearInterval(pollInterval);
                }, 2000);

            } catch (err) {
                overrideError.value = 'Network error';
                console.error('Override failed:', err);
            } finally {
                overrideLoading.value = false;
            }
        }

        // Confirm/unconfirm match
        async function confirmMatch() {
            if (!selectedItem.value) return;

            confirmLoading.value = true;
            confirmError.value = '';
            confirmSuccess.value = '';

            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/confirm`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    confirmError.value = err.detail || 'Confirm failed';
                    return;
                }

                const updated = await resp.json();
                // Update in grid
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };

                confirmSuccess.value = 'Match confirmed!';
            } catch (err) {
                confirmError.value = 'Network error';
                console.error('Confirm failed:', err);
            } finally {
                confirmLoading.value = false;
            }
        }

        async function refreshMetadata() {
            if (!selectedItem.value) return;

            refreshMetadataLoading.value = true;
            refreshMetadataError.value = '';
            refreshMetadataSuccess.value = '';

            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/refresh-metadata`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    refreshMetadataError.value = err.detail || 'Metadata refresh failed';
                    return;
                }

                const result = await resp.json();
                refreshMetadataSuccess.value = `Metadata refreshed: ${result.title}`;

                // Reload the item to show updated metadata
                const itemResp = await fetch(`/api/items/${selectedItem.value.id}`);
                if (itemResp.ok) {
                    const updated = await itemResp.json();
                    const idx = items.value.findIndex(i => i.id === updated.id);
                    if (idx >= 0) items.value[idx] = updated;
                    selectedItem.value = { ...updated };
                }
            } catch (err) {
                refreshMetadataError.value = 'Network error';
                console.error('Metadata refresh failed:', err);
            } finally {
                refreshMetadataLoading.value = false;
            }
        }

        async function unconfirmMatch() {
            if (!selectedItem.value) return;

            unconfirmLoading.value = true;
            confirmError.value = '';
            confirmSuccess.value = '';

            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/unconfirm`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    confirmError.value = err.detail || 'Unconfirm failed';
                    return;
                }

                const updated = await resp.json();
                // Update in grid
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };

                confirmSuccess.value = 'Match unconfirmed';
            } catch (err) {
                confirmError.value = 'Network error';
                console.error('Unconfirm failed:', err);
            } finally {
                unconfirmLoading.value = false;
            }
        }

        async function toggleMatchConfidence() {
            if (!selectedItem.value) return;
            if (confirmLoading.value || unconfirmLoading.value) return;
            if (selectedItem.value.confidence === 'verified') {
                await unconfirmMatch();
            } else {
                await confirmMatch();
            }
        }

        function toggleOverrideSection() {
            showOverrideSection.value = !showOverrideSection.value;
        }

        function toggleCoverSection() {
            showCoverSection.value = !showCoverSection.value;
        }

        function togglePipelineSection(sectionName) {
            pipelineSectionsOpen.value[sectionName] = !pipelineSectionsOpen.value[sectionName];
        }

        function toggleWorkshopSection(sectionName) {
            // Alias for togglePipelineSection (workshop rebrand)
            pipelineSectionsOpen.value[sectionName] = !pipelineSectionsOpen.value[sectionName];
        }

        function toggleApiSection(sectionName) {
            apiSectionsOpen.value[sectionName] = !apiSectionsOpen.value[sectionName];
        }

        function toggleSidebarSection(sectionName) {
            sidebarSectionsOpen.value[sectionName] = !sidebarSectionsOpen.value[sectionName];
        }

        function chooseManualCover() {
            if (coverFileInput.value) {
                coverFileInput.value.click();
            }
        }

        async function uploadManualCover(filename, dataUrl) {
            if (!selectedItem.value || !filename || !dataUrl) return;
            coverUploadLoading.value = true;
            try {
                const resp = await fetch(`/api/items/${selectedItem.value.id}/cover`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename, data_url: dataUrl }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    pushToast({ kind: 'failure', title: 'Cover upload failed', body: err.detail || '', ttl: 4000 });
                    return;
                }
                const updated = await resp.json();
                const idx = items.value.findIndex(i => i.id === updated.id);
                if (idx >= 0) items.value[idx] = updated;
                selectedItem.value = { ...updated };
                pushToast({ kind: 'success', title: 'Cover updated', ttl: 2500 });
            } catch (err) {
                pushToast({ kind: 'failure', title: 'Cover upload failed', ttl: 4000 });
                console.error('Cover upload failed:', err);
            } finally {
                coverUploadLoading.value = false;
            }
        }

        function onCoverFileChange(e) {
            const file = e.target && e.target.files ? e.target.files[0] : null;
            if (file) {
                const reader = new FileReader();
                reader.onload = () => {
                    uploadManualCover(file.name || 'cover.png', String(reader.result || ''));
                };
                reader.onerror = () => {
                    pushToast({ kind: 'failure', title: 'Failed to read image file', ttl: 4000 });
                };
                reader.readAsDataURL(file);
            }
            if (e.target) e.target.value = '';
        }

        // Delete item
        async function deleteItem(ignoreCode = false) {
            // Two delete modes:
            //   ignoreCode=false → plain DB delete; file untouched; row will
            //   reappear on next scan if the archive is still under a scan
            //   path. Default — matches the common "I want to re-import"
            //   workflow.
            //   ignoreCode=true  → also adds the product_code to ignored_codes
            //   so scans skip it. Use for "yeet permanently".
            if (!selectedItem.value) return;
            const code = selectedItem.value.product_code;
            const msg = ignoreCode
                ? `Delete ${code} AND ignore its code so future scans do not re-import it?`
                : `Delete ${code} from the library? (File on disk untouched; will reappear on next scan.)`;
            if (!confirm(msg)) return;

            deleteLoading.value = true;
            deleteError.value = '';
            try {
                const resp = await fetch(
                    `/api/items/${selectedItem.value.id}?ignore_code=${ignoreCode ? 'true' : 'false'}`,
                    { method: 'DELETE', headers: { 'Content-Type': 'application/json' } },
                );
                if (!resp.ok) {
                    const err = await resp.json();
                    deleteError.value = err.detail || 'Delete failed';
                    return;
                }
                items.value = items.value.filter(i => i.id !== selectedItem.value.id);
                totalItems.value--;
                closeDetail();
                await loadStats();
                if (librarySubtab.value === 'tokutens') loadTokutenStats();
            } catch (err) {
                deleteError.value = 'Network error';
                console.error('Delete failed:', err);
            } finally {
                deleteLoading.value = false;
            }
        }

        // Two delete modes for games:
        //   ignorePath=false (default) → plain DB delete; file on disk
        //   untouched; will reappear on next scan if still under a scan path.
        //   ignorePath=true  → also writes library_path to ignored_game_paths
        //   so the scanner skips it on future runs. Removable from Settings.
        const deleteGameLoading = ref(false);
        const deleteGameError = transientRef('');
        async function deleteSelectedGame(ignorePath = false) {
            if (!selectedGame.value) return;
            const title = selectedGame.value.title || selectedGame.value.vndb_id || 'game';
            const msg = ignorePath
                ? `Remove "${title}" from the library AND ignore its path so future scans skip it? (File on disk untouched.)`
                : `Delete "${title}" from the library? (File on disk untouched; will reappear on next scan if still under a scan path.)`;
            if (!confirm(msg)) return;
            deleteGameLoading.value = true;
            deleteGameError.value = '';
            try {
                const resp = await fetch(
                    `/api/games/${selectedGame.value.id}?ignore_path=${ignorePath ? 'true' : 'false'}`,
                    { method: 'DELETE' },
                );
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    deleteGameError.value = err.detail || 'Delete failed';
                    return;
                }
                gamesItems.value = gamesItems.value.filter(g => g.id !== selectedGame.value.id);
                gamesTotal.value = Math.max(0, gamesTotal.value - 1);
                closeGameDetail();
                loadGameStats();
                if (ignorePath) loadIgnoredGamePaths();
            } catch (err) {
                deleteGameError.value = 'Network error';
                console.error('Game delete failed:', err);
            } finally {
                deleteGameLoading.value = false;
            }
        }

        // Settings → Ignored game paths management. The list is fetched on
        // demand (Settings card open) so we don't hit the endpoint on every
        // page load.
        const ignoredGamePaths = ref([]);
        const ignoredGamePathsLoading = ref(false);
        const ignoredGamePathsError = transientRef('');
        async function loadIgnoredGamePaths() {
            ignoredGamePathsLoading.value = true;
            try {
                const resp = await fetch('/api/games/ignored-paths');
                if (!resp.ok) return;
                const data = await resp.json();
                ignoredGamePaths.value = Array.isArray(data.paths) ? data.paths : [];
            } catch (err) {
                ignoredGamePathsError.value = 'Failed to load ignored paths';
            } finally {
                ignoredGamePathsLoading.value = false;
            }
        }
        async function unignoreGamePath(pathKey) {
            try {
                const resp = await fetch(
                    `/api/games/ignored-paths/${encodeURIComponent(pathKey)}`,
                    { method: 'DELETE' },
                );
                if (!resp.ok) {
                    ignoredGamePathsError.value = 'Failed to remove';
                    return;
                }
                ignoredGamePaths.value = ignoredGamePaths.value.filter(p => p.path_key !== pathKey);
                pushToast({
                    kind: 'success',
                    title: 'Path un-ignored',
                    body: 'Next scan will pick it back up',
                    ttl: 3000,
                });
            } catch (err) {
                ignoredGamePathsError.value = 'Failed to remove';
            }
        }

        // Filter shortcuts (from detail panel tags)
        function filterBySeiyuu(name) {
            if (!filters.seiyuu.includes(name)) {
                filters.seiyuu.push(name);
            }
            closeDetail();
            loadItems();
        }

        function filterByTag(tag) {
            if (!filters.tag.includes(tag)) {
                filters.tag.push(tag);
            }
            closeDetail();
            loadItems();
        }

        function toggleFavoriteFilter() {
            filters.favorite = !filters.favorite;
            loadItems();
        }

        function clearFilters() {
            // Clear ONLY the active subtab's filter state so swapping tabs
            // doesn't accidentally wipe the other tabs' selections.
            const sub = librarySubtab.value;
            if (sub === 'games') {
                gameSearchQuery.value = '';
                gameFilters.play_status = '';
                gameFilters.platform = '';
                gameFilters.developer = '';
                gameFilters.custom_tag = '';
                gameFilters.favorite = false;
                gameFilters.matched = null;
                gameFilters.is_manual = null;
                gameFilters.include_wishlist = false;
                loadGames();
                return;
            }
            if (sub === 'tokutens') {
                tokutenSearchQuery.value = '';
                tokutenFilters.kind = '';
                tokutenFilters.source = '';
                tokutenFilters.favorite = false;
                tokutenFilters.is_manual = null;
                loadItems();
                return;
            }
            searchQuery.value = '';
            filters.seiyuu = [];
            filters.tag = [];
            selectedSeiyuuOption.value = '';
            selectedTagOption.value = '';
            filters.translation_status = '';
            filters.listen_status = '';
            filters.favorite = false;
            filters.has_metadata = null;
            filters.is_manual = null;
            loadItems();
        }

        // Silent reset — same as clearFilters but skips the loadItems()
        // call, so callers that immediately set a fresh filter can avoid
        // racing two HTTP requests (the cleared one + the filtered one).
        function _resetDramaFiltersSilent() {
            searchQuery.value = '';
            filters.seiyuu = [];
            filters.tag = [];
            selectedSeiyuuOption.value = '';
            selectedTagOption.value = '';
            filters.translation_status = '';
            filters.listen_status = '';
            filters.favorite = false;
            filters.has_metadata = null;
            filters.is_manual = null;
        }

        // Stat-pill click handlers — clicking a stat pill in the sidebar
        // jumps to the matching filtered view in Library so the user can
        // actually see WHICH items each count refers to. The drama-CD
        // version keeps the original semantics; games + tokutens have
        // their own dispatcher below.
        function applyStatFilter(kind) {
            // Reset silently first so the new filter doesn't combine with
            // leftover state, but DON'T fire loadItems yet — we'll fire it
            // exactly once after the new filter is in place to avoid a
            // race where the cleared response overwrites the filtered one.
            _resetDramaFiltersSilent();
            activeTab.value = 'library';
            switch (kind) {
                case 'total':
                    // Nothing else to set — just show all.
                    break;
                case 'favorites':
                    filters.favorite = true;
                    break;
                case 'with_metadata':
                    filters.has_metadata = true;
                    break;
                case 'pending':
                    filters.has_metadata = false;
                    break;
                default:
                    // listen_<status> pills — drill into a single listen_status.
                    if (typeof kind === 'string' && kind.startsWith('listen_')) {
                        const status = kind.slice('listen_'.length);
                        if (LISTEN_STATUS_OPTIONS.includes(status)) {
                            filters.listen_status = status;
                        }
                    }
                    break;
            }
            loadItems();
        }

        function applyGameStatFilter(kind) {
            // Games stat-pill click. Clears existing game filters, applies
            // the matching one, switches to the games subtab. The 'unmatched'
            // click is special — it opens the cleanup queue overlay instead
            // of just filtering, since cleaning the 39 unmatched games is
            // the whole point of that pill.
            clearGameFiltersSilent();
            activeTab.value = 'library';
            if (librarySubtab.value !== 'games') setLibrarySubtab('games');
            switch (kind) {
                case 'total':
                    break;
                case 'matched':
                    gameFilters.matched = true;
                    break;
                case 'unmatched':
                    // Hand-off to the dedicated cleanup queue (defined below).
                    openUnmatchedQueue();
                    return;
                case 'favorited':
                    gameFilters.favorite = true;
                    break;
                case 'manual':
                    gameFilters.is_manual = true;
                    break;
                case 'backlog':
                case 'want_to_play':
                case 'playing':
                case 'completed':
                case 'dropped':
                case 'on_hold':
                case 'wishlist':
                    gameFilters.play_status = kind;
                    if (kind === 'wishlist') gameFilters.include_wishlist = true;
                    break;
            }
            loadGames();
        }

        function clearGameFiltersSilent() {
            gameSearchQuery.value = '';
            gameFilters.play_status = '';
            gameFilters.platform = '';
            gameFilters.developer = '';
            gameFilters.custom_tag = '';
            gameFilters.favorite = false;
            gameFilters.matched = null;
            gameFilters.is_manual = null;
            gameFilters.include_wishlist = false;
        }

        function applyTokutenStatFilter(kind) {
            clearTokutenFiltersSilent();
            activeTab.value = 'library';
            if (librarySubtab.value !== 'tokutens') setLibrarySubtab('tokutens');
            switch (kind) {
                case 'total':
                    break;
                case 'favorited':
                    tokutenFilters.favorite = true;
                    break;
                case 'kind_audio':
                case 'kind_book':
                case 'kind_image':
                case 'kind_misc':
                    tokutenFilters.kind = kind.replace('kind_', '');
                    break;
            }
            loadItems();
        }

        function clearTokutenFiltersSilent() {
            tokutenSearchQuery.value = '';
            tokutenFilters.kind = '';
            tokutenFilters.source = '';
            tokutenFilters.favorite = false;
            tokutenFilters.is_manual = null;
        }

        // is_manual filter dropdown. One shared overlay state since only the
        // active subtab's pill is visible at a time. `isManualDropdownFor`
        // remembers which subtab the dropdown applies to so option-click
        // mutates the right filter.
        const isManualDropdownFor = ref(null);
        const isManualDropdownRect = ref(null);
        function openIsManualDropdown(subtab, ev) {
            isManualDropdownFor.value = subtab;
            if (ev && ev.currentTarget) {
                const r = ev.currentTarget.getBoundingClientRect();
                isManualDropdownRect.value = {
                    top: r.bottom + 4,
                    left: r.left,
                };
            }
        }
        function closeIsManualDropdown() {
            isManualDropdownFor.value = null;
        }
        function selectIsManualOption(value) {
            // `value` ∈ { null, true, false } — matches the API filter shape.
            const sub = isManualDropdownFor.value;
            isManualDropdownFor.value = null;
            if (sub === 'games') {
                gameFilters.is_manual = value;
                loadGames();
            } else if (sub === 'tokutens') {
                tokutenFilters.is_manual = value;
                loadItems();
            } else {
                filters.is_manual = value;
                loadItems();
            }
        }
        function currentIsManualValue() {
            const sub = isManualDropdownFor.value;
            if (sub === 'games') return gameFilters.is_manual;
            if (sub === 'tokutens') return tokutenFilters.is_manual;
            if (sub === 'drama_cds') return filters.is_manual;
            return null;
        }

        // Cleanup queue overlay launcher. Pre-flights the unmatched count
        // and only shows the modal if there's actually work to do — an
        // empty modal saying "nothing to clean up" is just chrome.
        async function openUnmatchedQueue() {
            unmatchedQueueError.value = '';
            unmatchedQueueLoading.value = true;
            try {
                const resp = await fetch('/api/games?matched=false&limit=2000');
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const data = await resp.json();
                const filtered = (data.items || []).filter(
                    g => (g.title || '').trim() && (g.title || '').trim() !== '[New Game]'
                );
                if (filtered.length === 0) {
                    pushToast({
                        kind: 'success',
                        title: 'No unmatched games',
                        body: 'Your library is fully cleaned up.',
                        ttl: 3000,
                    });
                    return;
                }
                unmatchedQueueItems.value = filtered;
                unmatchedQueueIndex.value = 0;
                _resetUnmatchedSearchState();
                unmatchedQueueOpen.value = true;
                await runUnmatchedQueueSearch();
            } catch (err) {
                pushToast({
                    kind: 'failure',
                    title: 'Failed to load unmatched games',
                    body: String(err.message || err),
                    ttl: 4000,
                });
            } finally {
                unmatchedQueueLoading.value = false;
            }
        }

        async function showUnmatchedFiles() {
            unmatchedFilesPanelOpen.value = true;
            unmatchedFilesLoading.value = true;
            try {
                const resp = await fetch('/api/unmatched');
                if (!resp.ok) throw new Error('failed');
                const data = await resp.json();
                unmatchedFilesList.value = Array.isArray(data) ? data : (data.files || []);
            } catch (err) {
                unmatchedFilesList.value = [];
                console.warn('unmatched files fetch failed:', err);
            } finally {
                unmatchedFilesLoading.value = false;
            }
        }

        // Keyboard shortcuts
        function onKeydown(e) {
            if (e.key === 'Escape') {
                // Closing priority — detail panels first, then multi-select.
                if (unmatchedQueueOpen.value) {
                    closeUnmatchedQueue();
                    return;
                }
                if (selectedItem.value) {
                    closeDetail();
                    return;
                }
                if (selectedGame.value) {
                    closeGameDetail();
                    return;
                }
                if (selectedIds.value.size > 0) {
                    clearSelection();
                    return;
                }
                if (selectedGameIds.value.size > 0) {
                    clearGameSelection();
                    return;
                }
                if (playStatusDropdownFor.value !== null) {
                    closePlayStatusDropdown();
                    return;
                }
                if (isManualDropdownFor.value !== null) {
                    closeIsManualDropdown();
                    return;
                }
            }
        }

        // ---------------------------------------------------------------
        // UI state persistence
        // ---------------------------------------------------------------
        // Hydrate a small set of refs from localStorage at setup time and
        // auto-save them on change. Only IDs and the current tab survive a
        // refresh — everything derived (tracks, runs, segments) is re-fetched
        // from the server in onMounted so the data stays fresh.
        function persistRef(key, refObj, opts = {}) {
            const raw = localStorage.getItem(key);
            if (raw !== null) {
                try {
                    if (opts.json) {
                        const stored = JSON.parse(raw);
                        const current = refObj.value;
                        // For plain objects, merge stored over defaults so
                        // newly-introduced keys keep their default value
                        // instead of being silently removed by an older
                        // serialized snapshot.
                        if (
                            stored && typeof stored === 'object' && !Array.isArray(stored) &&
                            current && typeof current === 'object' && !Array.isArray(current)
                        ) {
                            refObj.value = { ...current, ...stored };
                        } else {
                            refObj.value = stored;
                        }
                    } else if (opts.number) {
                        const n = Number(raw);
                        refObj.value = Number.isFinite(n) ? n : null;
                    } else {
                        refObj.value = raw;
                    }
                } catch (_) {
                    /* ignore corrupt values */
                }
            }
            watch(refObj, (v) => {
                if (v === null || v === undefined || v === '') {
                    localStorage.removeItem(key);
                    return;
                }
                try {
                    localStorage.setItem(key, opts.json ? JSON.stringify(v) : String(v));
                } catch (_) {
                    /* quota or serialization error — best-effort */
                }
            }, { deep: !!opts.json });
        }

        persistRef('ui.activeTab', activeTab);
        persistRef('ui.pipelineSelectedItemId', pipelineSelectedItemId, { number: true });
        persistRef('ui.archiveViewMode', archiveViewMode);
        persistRef('ui.selectedWorkshopItem', selectedWorkshopItem, { json: true });
        persistRef('ui.pipelineTrackId', pipelineTrackId, { number: true });
        persistRef('ui.playerItemId', playerItemId, { number: true });
        persistRef('ui.playerTrackId', playerTrackId, { number: true });
        // Collapse / expand state of card sections across the app.
        // Persisted as objects so newly-added sections fall back to the
        // refs' default value rather than being silently absent.
        persistRef('ui.pipelineSectionsOpen', pipelineSectionsOpen, { json: true });
        persistRef('ui.sidebarSectionsOpen', sidebarSectionsOpen, { json: true });
        persistRef('ui.apiSectionsOpen', apiSectionsOpen, { json: true });

        // Init
        // Close any open hover-menu when the user clicks outside it. Listed
        // here (not inside onMounted) so the closure captures the bulkMenuOpen ref.
        function onDocumentClickForMenu(e) {
            const insideMenu = e.target.closest && e.target.closest('.hover-menu');
            if (bulkMenuOpen.value && !insideMenu) {
                bulkMenuOpen.value = false;
            }
            if (detailKebabOpen.value && !insideMenu) {
                detailKebabOpen.value = false;
            }
            if (addMenuOpen.value && !insideMenu) {
                addMenuOpen.value = false;
            }
        }

        // Click-outside collapses the activity drawer. Skips clicks on the
        // toggle button itself (so toggling doesn't immediately re-close).
        function onDocumentClickForActivityDrawer(e) {
            if (!activityDrawerOpen.value) return;
            const t = e.target;
            if (!t || !t.closest) return;
            if (t.closest('.activity-drawer')) return;
            if (t.closest('.activity-toggle')) return;
            activityDrawerOpen.value = false;
            activityDrawerAutoOpened.value = false;
        }

        onMounted(async () => {
            document.addEventListener('keydown', onKeydown);
            document.addEventListener('click', onDocumentClickForMenu);
            document.addEventListener('click', onDocumentClickForActivityDrawer);
            startAutopilotPolling();

            // Show UI immediately with async data loading in background
            // This makes the app feel much faster
            const dataLoadPromises = [
                loadItems(),
                loadStats(),
                loadFilterOptions(),
                loadScanPaths(),
                loadGamesScanPaths(),
                loadTokutenScanPaths(),
                loadMaintenancePreview(),
                loadOpsPanel(),
                loadPipelineStatus(),
                loadApiSettings(),
            ];
            // If persisted subtab is 'games', kick its initial load too.
            if (librarySubtab.value === 'games') {
                dataLoadPromises.push(loadGames());
                dataLoadPromises.push(loadGameStats());
                dataLoadPromises.push(loadGameDistinctOptions());
                dataLoadPromises.push(loadDuplicateGameGroups());
            } else if (librarySubtab.value === 'tokutens') {
                dataLoadPromises.push(loadTokutenStats());
            }

            // Don't wait for all data - start showing UI now
            // Data will populate as it arrives
            Promise.all(dataLoadPromises).catch(err => {
                console.error('Error loading initial data:', err);
            });

            // Initialize player theme from localStorage ASAP
            const savedTheme = localStorage.getItem('playerTheme') || 'starlit';
            setPlayerTheme(savedTheme);

            // Re-hydrate tab-specific data based on the persisted state.
            // Refs were already restored synchronously by persistRef() above;
            // here we re-fetch the derived data (tracks, runs) so the page
            // looks like it did before the refresh.
            (async () => {
                try {
                    if (activeTab.value === 'pipeline' && pipelineSelectedItemId.value) {
                        await loadPipelineTracksForItem();
                        if (pipelineTrackId.value) {
                            try { await loadPipelineRuns(); } catch (_) {}
                        }
                    } else if (activeTab.value === 'player' && playerItemId.value && !playerTrackId.value) {
                        await loadPlayerItemTracks();
                    }
                } catch (err) {
                    console.warn('UI state rehydration failed:', err);
                }
            })();

            // Show the UI immediately - data will load in the background
            // Use nextTick to let Vue finish initial render, then reveal
            nextTick(() => {
                setTimeout(() => {
                    document.getElementById('app').setAttribute('data-loading', 'false');
                }, 0);
            });

            // Restore scan state from localStorage and resume polling in background (don't block UI)
            const savedScanRunning = localStorage.getItem('scanRunning');
            const savedScanProgress = localStorage.getItem('scanProgress');
            if (savedScanRunning === 'true') {
                scanRunning.value = true;
                if (savedScanProgress) {
                    try {
                        const saved = JSON.parse(savedScanProgress);
                        scanProgress.total = saved.total || 0;
                        scanProgress.processed = saved.processed || 0;
                        scanProgress.current = saved.current || null;
                        scanProgress.paused = !!saved.paused;
                        scanProgress.stopping = !!saved.stopping;
                    } catch (e) {
                        console.error('Failed to restore scan progress:', e);
                    }
                }
                pollScanStatus();
            }

            // Restore fetch state from localStorage and resume polling in background (don't block UI)
            const savedFetchRunning = localStorage.getItem('fetchRunning');
            const savedFetchProgress = localStorage.getItem('fetchProgress');
            // Glossary is now per-item; populated when an item is loaded into the Workshop.
            autoTranslateGlossary.value = '';
            // Drop the old global value if it's still hanging around from a prior version.
            try { localStorage.removeItem('autoTranslateGlossary'); } catch (e) {}
            autoTranslateCharacterMemory.value = localStorage.getItem('autoTranslateCharacterMemory') || '';
            autoTranslateMaxRetries.value = Number(localStorage.getItem('autoTranslateMaxRetries') || autoTranslateMaxRetries.value);
            autoTranslateRetryBackoff.value = Number(localStorage.getItem('autoTranslateRetryBackoff') || autoTranslateRetryBackoff.value);
            const savedProvider = localStorage.getItem('apiTranslationProvider');
            apiTranslationProvider.value = SUPPORTED_PROVIDERS.includes(savedProvider) ? savedProvider : 'gemini';
            apiGeminiModel.value = localStorage.getItem('apiGeminiModel') || apiGeminiModel.value;
            apiOpenRouterModel.value = localStorage.getItem('apiOpenRouterModel') || apiOpenRouterModel.value;
            apiChutesModel.value = localStorage.getItem('apiChutesModel') || apiChutesModel.value;
            apiOpenAiCompatModel.value = localStorage.getItem('apiOpenAiCompatModel') || apiOpenAiCompatModel.value;
            apiOpenAiCompatBaseUrl.value = localStorage.getItem('apiOpenAiCompatBaseUrl') || apiOpenAiCompatBaseUrl.value;
            autoTranslateProvider.value = apiTranslationProvider.value;
            if (!autoTranslateModel.value || autoTranslateModel.value === 'gemini-2.0-flash' || autoTranslateModel.value === 'openrouter/auto') {
                if (autoTranslateProvider.value === 'openrouter') autoTranslateModel.value = apiOpenRouterModel.value;
                else if (autoTranslateProvider.value === 'chutes') autoTranslateModel.value = apiChutesModel.value;
                else if (autoTranslateProvider.value === 'openai_compat') autoTranslateModel.value = apiOpenAiCompatModel.value;
                else autoTranslateModel.value = apiGeminiModel.value;
            }

            if (savedFetchRunning === 'true') {
                fetchRunning.value = true;
                if (savedFetchProgress) {
                    try {
                        const saved = JSON.parse(savedFetchProgress);
                        fetchProgress.total = saved.total || 0;
                        fetchProgress.completed = saved.completed || 0;
                        fetchProgress.current = saved.current || null;
                        fetchProgress.paused = !!saved.paused;
                        fetchProgress.stopping = !!saved.stopping;
                        fetchProgress.stopped = !!saved.stopped;
                    } catch (e) {
                        console.error('Failed to restore fetch progress:', e);
                    }
                }
                // Resume polling if fetch was still running (non-blocking)
                pollFetchStatus();
            }

            setInterval(() => {
                loadOpsPanel();
            }, 5000);


        });

        // ====== PLAYER FUNCTIONS ======

        function scrollToPlayerTop() {
            const container = document.querySelector('.player-scroll-wrapper');
            if (container) {
                container.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }

        function jumpToCurrentSegment() {
            if (playerActiveSegmentIndex.value < 0) return;
            const el = document.querySelector('.transcript-card.active');
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        function formatTime(seconds) {
            if (!seconds || isNaN(seconds)) return '0:00';
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        }

        async function loadPlayerItemTracks() {
            if (!playerItemId.value) return;

            try {
                pipelineBusy.value = true;
                pipelineLoadError.value = '';

                const resp = await fetch(`/api/pipeline/items/${playerItemId.value}/tracks`);
                if (!resp.ok) {
                    throw new Error(`Failed to load tracks: ${resp.statusText}`);
                }

                const data = await resp.json();

                // Surface every extracted track. Audio playback doesn't need
                // a transcript or translation — those are optional layers.
                // The transcript/translation counts feed the badge in each
                // row but no longer gate visibility.
                const tracksWithCounts = await Promise.all(
                    data.tracks.map(async (track) => {
                        try {
                            const [transcriptResp, translationResp] = await Promise.all([
                                fetch(`/api/pipeline/tracks/${track.id}/transcripts`),
                                fetch(`/api/pipeline/tracks/${track.id}/translations`)
                            ]);

                            const transcriptData = await transcriptResp.json();
                            const translationData = await translationResp.json();

                            return {
                                ...track,
                                transcript_count: transcriptData.runs?.length || 0,
                                translation_count: translationData.runs?.length || 0
                            };
                        } catch (err) {
                            console.error('Failed to load run counts for track', track.id, err);
                            return { ...track, transcript_count: 0, translation_count: 0 };
                        }
                    })
                );

                playerAvailableTracks.value = tracksWithCounts;

                // The empty-state message lives in the template (see the
                // empty-state block) — keep pipelineLoadError silent here so
                // we don't show a scary red error on a perfectly valid
                // "audio not extracted yet" case.
            } catch (err) {
                console.error('Failed to load player tracks:', err);
                pipelineLoadError.value = err.message || 'Failed to load tracks';
            } finally {
                pipelineBusy.value = false;
            }
        }

        // Safety net for any audio load failure (404 from a deleted file,
        // unsupported codec, transcode error). Without this the <audio>
        // element swallows the error and play just silently does nothing.
        function onPlayerAudioError() {
            // closePlayer() nulls playerTrackId before clearing src — use it
            // to ignore the spurious error fired by the src='' teardown.
            if (!playerTrackId.value) return;
            const el = playerAudioElement.value;
            if (el && !el.currentSrc && !el.getAttribute('src')) return;
            pushToast({
                kind: 'failure',
                title: 'Audio failed to load',
                body: 'The file may be missing on disk — re-extract this CD from Atelier (Archive → Queue extraction).',
                ttl: 7000,
            });
        }

        function closePlayer() {
            playerTrackId.value = null;
            playerTrackTitle.value = '';
            playerTranscriptSegments.value = [];
            playerTranslationSegments.value = [];
            playerIsPlaying.value = false;
            playerCurrentTime.value = 0;
            playerDuration.value = 0;
            playerProgressPercent.value = 0;
            playerActiveSegmentIndex.value = -1;

            if (playerAudioElement.value) {
                playerAudioElement.value.pause();
                playerAudioElement.value.src = '';
            }
        }

        function setPlayerTheme(theme) {
            // Update theme classes on body element (both player and app-wide)
            document.body.classList.remove('player-theme-starlit', 'player-theme-sweet', 'player-theme-eclipse');
            document.body.classList.remove('app-theme-starlit', 'app-theme-sweet', 'app-theme-eclipse');
            document.body.classList.add(`player-theme-${theme}`);
            document.body.classList.add(`app-theme-${theme}`);

            // Save preference to localStorage
            localStorage.setItem('playerTheme', theme);

            playerTheme.value = theme;
        }

        async function loadPlayerTrack(trackId, transcriptRunId = null, translationRunId = null) {
            try {
                pipelineBusy.value = true;

                // Load track metadata
                const trackResp = await fetch(`/api/pipeline/items/${playerItemId.value}/tracks`);
                const trackData = await trackResp.json();
                const track = trackData.tracks.find(t => t.id === trackId);

                if (!track) {
                    alert('Track not found');
                    return;
                }

                // DB row exists but the extracted file is gone — bail with a
                // clear prompt instead of loading a player that can't play.
                if (track.file_exists === false) {
                    pushToast({
                        kind: 'failure',
                        title: 'Audio file not on disk',
                        body: 'Re-extract this CD from Atelier (Archive → Queue extraction), then try again.',
                        ttl: 7000,
                    });
                    return;
                }

                playerTrackId.value = trackId;
                // Respect the user's metadata-language preference. On EN we prefer
                // title_en and fall back to JP; on JP it's the reverse. Final
                // fallback is the raw filename so we always show something.
                const filenameFallback = track.track_path ? track.track_path.split('\\').pop() : '';
                const titleJp = (track.title || '').trim();
                const titleEn = (track.title_en || '').trim();
                playerTrackTitle.value = currentLang.value === 'en'
                    ? (titleEn || titleJp || filenameFallback)
                    : (titleJp || titleEn || filenameFallback);
                playerTrackDuration.value = track.duration_seconds ? `${(track.duration_seconds / 60).toFixed(1)}min` : 'Unknown';

                // Load the transcript run list (for the switcher) + the chosen/active run.
                const trListResp = await fetch(`/api/pipeline/tracks/${trackId}/transcripts`);
                const trListData = await trListResp.json();
                playerTranscriptRuns.value = trListData.runs || [];
                const chosenTranscriptRunId = transcriptRunId || trListData.active?.active_transcript_run_id || null;
                if (chosenTranscriptRunId) {
                    const transcriptResp = await fetch(`/api/pipeline/tracks/${trackId}/transcripts/${chosenTranscriptRunId}`);
                    const transcriptData = await transcriptResp.json();
                    playerTranscriptSegments.value = transcriptData.segments || [];
                    playerTranscriptRunId.value = chosenTranscriptRunId;
                } else {
                    playerTranscriptSegments.value = [];
                    playerTranscriptRunId.value = null;
                }

                // Load translation segments if available
                if (translationRunId) {
                    const translationResp = await fetch(`/api/pipeline/tracks/${trackId}/translations/${translationRunId}`);
                    const translationData = await translationResp.json();
                    playerTranslationSegments.value = translationData.segments || [];
                    playerTranslationRunId.value = translationRunId;
                } else {
                    // Try to load active translation
                    const activeResp = await fetch(`/api/pipeline/tracks/${trackId}/translations`);
                    const activeData = await activeResp.json();
                    const activeTranslationRunId = activeData.active?.active_translation_run_id;

                    if (activeTranslationRunId) {
                        const translationResp = await fetch(`/api/pipeline/tracks/${trackId}/translations/${activeTranslationRunId}`);
                        const translationData = await translationResp.json();
                        playerTranslationSegments.value = translationData.segments || [];
                        playerTranslationRunId.value = activeTranslationRunId;
                    } else {
                        playerTranslationSegments.value = [];
                        playerTranslationRunId.value = null;
                    }
                }

                // Load audio. Mobile devices get an AAC transcode at 48kHz
                // because source-rate FLAC drifts against their output clock
                // over the length of a track (linear resampler drift). Desktop
                // keeps native FLAC for fidelity.
                const audioBase = `/api/pipeline/player/audio/${trackId}`;
                const audioUrl = isMobileClient() ? `${audioBase}?format=aac` : audioBase;
                if (playerAudioElement.value) {
                    playerAudioElement.value.src = audioUrl;
                    playerAudioElement.value.load();
                }

            } catch (err) {
                console.error('Failed to load player track:', err);
                alert('Failed to load track for playback');
            } finally {
                pipelineBusy.value = false;
            }
        }

        // Switch the active audio file within the same group (codec or variant
        // change) while preserving playback time + state. Transcript stays the
        // same since it's replicated across siblings.
        async function switchPlayerVariant(newTrackId) {
            if (!newTrackId || newTrackId === playerTrackId.value) return;
            const audio = playerAudioElement.value;
            const wasPlaying = audio ? !audio.paused : false;
            const savedTime = audio ? audio.currentTime : 0;

            await loadPlayerTrack(newTrackId);

            // Restore position once metadata is available, then resume play.
            const newAudio = playerAudioElement.value;
            if (!newAudio) return;
            const restore = () => {
                if (savedTime > 0 && savedTime < (newAudio.duration || Infinity)) {
                    newAudio.currentTime = savedTime;
                }
                if (wasPlaying) newAudio.play().catch(() => {});
                newAudio.removeEventListener('loadedmetadata', restore);
            };
            if (newAudio.readyState >= 1) restore();
            else newAudio.addEventListener('loadedmetadata', restore);
        }

        function togglePlayPause() {
            if (!playerAudioElement.value) return;

            if (playerIsPlaying.value) {
                playerAudioElement.value.pause();
            } else {
                playerAudioElement.value.play().catch(err => {
                    console.error('Playback failed:', err);
                });
            }
        }

        function seekToPosition(event) {
            if (!playerAudioElement.value || !playerDuration.value) return;

            const rect = event.currentTarget.getBoundingClientRect();
            const clickX = event.clientX - rect.left;
            const percent = clickX / rect.width;
            const newTime = percent * playerDuration.value;

            playerAudioElement.value.currentTime = newTime;
        }

        function jumpToSegment(index) {
            if (!playerAudioElement.value || !playerTranscriptSegments.value[index]) return;

            // When user clicks a card → re-enable auto-follow
            playerFollowTranscript.value = true;

            const segment = playerTranscriptSegments.value[index];

            // Set flag to prevent auto-scroll interference
            playerManualSeekInProgress.value = true;

            playerAudioElement.value.currentTime = segment.start_seconds || 0;

            if (!playerIsPlaying.value) {
                playerAudioElement.value.play().catch(err => {
                    console.error('Playback failed:', err);
                });
            }

            // Clear the flag after a short delay to allow normal auto-scrolling to resume
            setTimeout(() => {
                playerManualSeekInProgress.value = false;
            }, 800);
        }

        function skipToPrevSegment() {
            if (playerActiveSegmentIndex.value > 0) {
                jumpToSegment(playerActiveSegmentIndex.value - 1);
            }
        }

        function skipToNextSegment() {
            if (playerActiveSegmentIndex.value < playerTranscriptSegments.value.length - 1) {
                jumpToSegment(playerActiveSegmentIndex.value + 1);
            }
        }

        const canSkipPrev = computed(() => playerActiveSegmentIndex.value > 0);
        const canSkipNext = computed(() =>
            playerActiveSegmentIndex.value < playerTranscriptSegments.value.length - 1
        );

        function onPlayerTimeUpdate() {
            if (!playerAudioElement.value) return;

            playerCurrentTime.value = playerAudioElement.value.currentTime;

            // Update progress bar
            if (playerDuration.value > 0) {
                playerProgressPercent.value = (playerCurrentTime.value / playerDuration.value) * 100;
            }

            // Update active segment
            const currentTime = playerCurrentTime.value;
            let newActiveIndex = -1;

            for (let i = 0; i < playerTranscriptSegments.value.length; i++) {
                const seg = playerTranscriptSegments.value[i];
                if (currentTime >= (seg.start_seconds || 0) &&
                    currentTime < (seg.end_seconds || Infinity)) {
                    newActiveIndex = i;
                    break;
                }
            }

            // Keep previous segment if no match (prevents gaps in highlighting)
            if (newActiveIndex === -1) {
                newActiveIndex = playerActiveSegmentIndex.value;
            }

            playerActiveSegmentIndex.value = newActiveIndex;
        }

        function onPlayerMetadataLoaded() {
            if (!playerAudioElement.value) return;
            playerDuration.value = playerAudioElement.value.duration;
        }

        function onPlayerEnded() {
            playerIsPlaying.value = false;
            // Optional: auto-advance to next track in future
        }


        return {
            items, totalItems, loading, stats,
            seiyuuList, tagList,
            selectedItem, searchQuery, sortBy, currentLang,
            filters, newCustomTag, selectedSeiyuuOption, selectedTagOption,
            overrideCodeInput, overrideLoading, overrideError, overrideSuccess,
            showOverrideSection, showCoverSection, confirmLoading, unconfirmLoading, confirmError, confirmSuccess,
            refreshMetadataLoading, refreshMetadataError, refreshMetadataSuccess,
            deleteLoading, deleteError,
            coverUploadLoading, coverUploadError, coverUploadSuccess, coverFileInput,
            metadataTranslateLoading, metadataTranslateError, metadataTranslateSuccess,
            metadataTranslateStep, metadataTranslateElapsedSec,
            scanRunning, scanProgress, fetchRunning, fetchProgress, lastFetchSummary,
            showScanPathsPanel, scanPathsInput, scanPathsLoading, scanPathsSaving,
            scanPathsError, scanPathsSuccess, scanRecursive,
            maintenanceLoading, maintenanceActionLoading, maintenancePreview, maintenanceMessage, maintenanceError,
            opsLoading, opsScanStatus, opsFetchStatus, opsErrorsExpanded, recentJobs,
            seiyuuInventory, seiyuuSuggestions, seiyuuLoading, seiyuuFilter, seiyuuSelected, seiyuuCanonical,
            seiyuuMergeBusy, seiyuuMergeMessage, seiyuuMergeError, seiyuuMergePreview,
            filteredSeiyuuInventory, loadSeiyuuInventory, loadSeiyuuSuggestions, useSeiyuuSuggestion,
            previewSeiyuuMerge, applySeiyuuMerge,
            seiyuuBackfillBusy, seiyuuBackfillPreview, seiyuuBackfillMessage, seiyuuBackfillError,
            previewSeiyuuBackfill, applySeiyuuBackfill,
            seiyuuJpNamesFor, seiyuuSelectedJpConsistent,
            activeTab,
            tokutenCreateBusy, tokutenCreateError, createBlankTokuten,
            addMenuOpen,
            pipelineEnabled, pipelineStatus, pipelineLoadError, pipelineBusy,
            pipelineSelectedItemId, selectedWorkshopItem, pipelineTracks, pipelineTrackId, pipelineSectionsOpen, sidebarSectionsOpen,
            workshopAudioState, workshopAudioDotTitle,
            workshopSearchQuery, workshopSearchResults, workshopSearchOpen, workshopSearchLoading,
            workshopSearchInput, selectWorkshopSearchResult, closeWorkshopSearch,
            effectiveTrackCount,
            manualTrackCountEditing, manualTrackCountInput,
            startEditTrackCount, cancelEditTrackCount, saveManualTrackCount,
            transcriptRuns, translationRuns, activeTranscriptRunId, activeTranslationRunId,
            selectTranscriptRun, selectTranslationRun,
            pendingDeleteTranscriptRunId, pendingDeleteTranslationRunId,
            pendingCleanupUnusedTranscripts,
            askDeleteTranscriptRun, cancelDeleteTranscriptRun, confirmDeleteTranscriptRun,
            askDeleteTranslationRun, cancelDeleteTranslationRun, confirmDeleteTranslationRun,
            askCleanupUnusedTranscripts, cancelCleanupUnusedTranscripts, confirmCleanupUnusedTranscripts,
            formatRunTimestamp, formatRunTimestampPrecise,
            pipelineTranscriptRunId,
            pipelineActiveSummary,
            pipelineForceExtract,
            selectedTranscriptSegments, selectedTranslationSegments,
            selectedTranscriptRunId, selectedTranscriptCleanText, trackCodecFilter, filteredPipelineTracks, transcribedPipelineTracks,
            pipelineTrackGroups, transcribedTrackGroups, isGroupSelected, toggleGroupSelection,
            transcribeLanguage, transcribeModel, transcriptionInProgress, transcriptionStatus, transcriptionProgress, selectedTracksForTranscription,
            autoTranslateTargetLanguage, autoTranslateProvider, autoTranslateModel, autoTranslateMaxTokens, autoTranslateMaxLines, autoTranslateStatus,
            autoTranslateMaxRetries, autoTranslateRetryBackoff,
            autoTranslateInProgress, autoTranslateProgress, autoTranslateLiveLines, autoTranslateControlBusy,
            autoTranslateGlossary, autoTranslateCharacterMemory,
            apiSettingsBusy, apiSettingsError, apiSettingsSuccess, apiTranslationProvider,
            apiGeminiModel, apiGeminiKeyInput, apiGeminiHasKey, apiGeminiKeySource,
            apiOpenRouterModel, apiOpenRouterKeyInput, apiOpenRouterHasKey, apiOpenRouterKeySource,
            apiChutesModel, apiChutesKeyInput, apiChutesHasKey, apiChutesKeySource,
            apiOpenAiCompatBaseUrl, apiOpenAiCompatModel, apiOpenAiCompatKeyInput,
            apiOpenAiCompatHasKey, apiOpenAiCompatKeySource, apiOpenAiCompatBaseUrlSource,
            apiOpenAiCompatRequestFormat,
            apiOpenAiCompatModelOptions, apiOpenAiCompatModelsBusy, apiOpenAiCompatModelsError,
            fetchOpenAiCompatModels,
            apiTestBusy, apiTestResult, apiSectionsOpen,
            whisperSettings, whisperSupportedModels, whisperSettingsBusy,
            whisperSettingsError, whisperSettingsSuccess,
            loadWhisperSettings, saveWhisperSettings,
            hasActiveFilters, scanPercent, fetchPercent, failureReasonEntries,
            selectedCount, allVisibleSelected, bulkLoading, bulkMessage, bulkError, bulkMenuOpen,
            parseJson, coverUrl, dlsiteUrl, isDlsiteCode, vgmdbSearchUrl,
            platformLabel, platformIconSvg, hasPlatformIcon,
            statusLabel, displayTitle, displaySecondaryTitle,
            displaySeiyuu, displaySeiyuuAt, alternateSeiyuuAt, toggleSeiyuuFlip, displayTags, displayDescription,
            renderBBcode,
            libraryViewMode, setLibraryViewMode, cardTooltip,
            displayGameTitle, displaySecondaryGameTitle,
            formatReleaseDate, detailKebabOpen, itemIsPlayable,
            confidenceLabel, confidenceIcon,
            isSelected, toggleSelectItem, toggleSelectAllVisible, clearSelection, onCardClick,
            // Games selection + drag-select rubber-band.
            selectedGameIds, isGameSelected, toggleSelectGame, clearGameSelection,
            onGameCardClick, lastClickedItemId, lastClickedGameId,
            dragSelectActive, dragSelectRect, onGridMouseDown,
            bulkDeleteGamesSelected,
            bulkConfirmSelected, bulkUnconfirmSelected, bulkOverrideSelected, toggleBulkActions, bulkTranslateSelected, bulkAddCustomTagSelected, bulkAutoTranslateSelected, bulkRunAutopilotSelected, bulkExtractSelected, bulkTranscribeSelected, showBulkActions,
            autopilotJobs, visibleAutopilotJobs, autopilotActiveCount, finishedActivityCount, activityDrawerOpen, expandedActivityJob, activityEvents, activityEventsLoading, autopilotToasts,
            toggleActivityDrawer, toggleActivityRowExpand, stopAutopilotJob, dismissAutopilotToast,
            dismissActivityJob, dismissAllFinishedActivity, restartAutopilotJob,
            activityItemTitle, activityStatusLabel, activityStageDisplay, activityStagePercent, activityElapsed, formatActivityTime,
            activityChildJob, activityChildDisplay,
            loadItems, loadMore, debouncedSearch, addSeiyuuFilter, addTagFilter, removeSeiyuuFilter, removeTagFilter, onLanguageChange,
            startScan, pauseScan, resumeScan, stopScan, startFetch, pauseFetch, resumeFetch, stopFetch, loadScanPaths, saveScanPaths, openScanPathsPanel,
            scanActiveSubtab, anyScanRunning, activeSubtabLabel,
            librarySubtab, setLibrarySubtab,
            gamesItems, gamesTotal, gamesLoading,
            gamesScanPathsInput, gamesScanPathsLoading, gamesScanPathsSaving, gamesScanPathsError, gamesScanPathsSuccess, gamesScanning,
            loadGames, loadGamesScanPaths, saveGamesScanPaths, scanGames, openGameFolder, toggleGameFavorite,
            tokutenScanPathsInput, tokutenScanPathsLoading, tokutenScanPathsSaving, tokutenScanPathsError, tokutenScanPathsSuccess, tokutenScanning,
            loadTokutenScanPaths, saveTokutenScanPaths, scanTokutens,
            createBlankDramaCd, createBlankGame,
            bulkCreateDramaCds, bulkCreateGames, bulkCreateTokutens,
            detailEditing, detailDraft, detailSavingBusy, detailSaveError,
            startDetailEdit, cancelDetailEdit, saveDetailEdit,
            archiveBrowseBusy, browseArchivePath,
            // Tokuten ↔ game cross-link refs + navigation helpers.
            linkedTokutenForItem, linkedGameForTokuten, linkedTokutensForGame,
            openLinkedGame, openLinkedTokuten,
            selectedGame, gameDraft, gameEditing, gameSavingBusy, gameSaveError,
            openGameDetail, closeGameDetail, startGameEdit, cancelGameEdit, saveGameEdit,
            vndbQuery, vndbResults, vndbSearching, vndbSearchError,
            vndbSearchInput, applyVndbResult, applyVndbResultForTokuten,
            // External metadata fetch (Gamers / Chil-Chil)
            metaFetchUrl, metaFetchBusy, metaFetchError,
            metaSearchQuery, metaSearchResults, metaSearching,
            metaPreview, metaPreviewFields, metaApplyBusy, metaApplyError,
            metaSearchInput, metaFetchFromUrl, metaPickSearchResult,
            metaClosePreview, metaNotePreview, metaApply, metaSourceLabel,
            metaSelectedUrls, metaToggleSelected, metaFetchSelected,
            galleryOpen, galleryMedia, galleryLoading, galleryBusy, galleryError,
            openItemGallery, closeItemGallery, setGalleryCover, removeGalleryMedia,
            gameCoverFileInput, gameCoverUploadLoading, gameCoverUploadError, gameCoverUploadSuccess,
            chooseGameCover, onGameCoverFileChange,
            vndbMatchBusy, vndbMatchMessage, vndbMatchError, matchVndbAll,
            duplicateGameGroups, duplicateRowsCount, mergeDuplicatesBusy,
            duplicatesModalOpen, pendingMergeAllDuplicates,
            loadDuplicateGameGroups, mergeAllDuplicates,
            openDuplicatesModal, closeDuplicatesModal,
            askMergeAllDuplicates, cancelMergeAllDuplicates,
            loadMaintenancePreview, runCleanupStaleCovers, rebuildMetadataIndexes, recomputeTranslationStatus,
            backfillActiveTranscripts, backfillTranscriptsBusy, backfillTranscriptsMessage, backfillTranscriptsError,
            backfillSiblingTranslations, backfillTranslationsBusy, backfillTranslationsMessage, backfillTranslationsError,
            mojibakeBusy, mojibakeMessage, mojibakeError, mojibakePreview, scanMojibake, fixMojibake, fixMojibakePaths,
            transcriptIoBusy, transcriptIoMessage, transcriptIoError, transcriptIoSummary, transcriptIoReplace, transcriptIoAcceptZip, transcriptIoFileInput,
            exportTranscripts, triggerImportTranscripts, onImportTranscriptsFile,
            packageBusy, packageMessage, packageError, packageIncludeAudio, packageAllRuns, packagePreservePaths, packageIncludeSrt, packageIncludeTxt, packageIncludeTracklist, packageIncludeAllArchiveFiles, downloadItemPackage, openItemAudioFolder,
            archiveExportMenuOpen, closeArchiveExportMenu, exportPackagePreset,
            archiveContents, archiveContentsLoading, formatArchiveSize,
            archiveViewMode, archiveCurrentPath, archiveGridEntries, archiveBreadcrumbs, archiveListGroups,
            archiveCollapsedFolders, toggleArchiveFolder, isArchiveFolderCollapsed,
            archiveOpenFolder, archiveThumbUrl, isImagePath, normalizeArchivePath,
            pendingPurgeAudio, askPurgeAudio, cancelPurgeAudio, confirmPurgeAudio,
            purgeBusy, purgeMessage, purgeError, purgeItemWorkspace,
            trackNamesBusy, trackNamesMessage, trackNamesError, translateTrackNames,
            summariesBusy, summariesMessage, summariesError, backfillSummaries,
            workspaceBusy, workspaceMessage, workspaceError, workspaceOrphans,
            loadWorkspaceOrphans, purgeWorkspaceOrphans, formatBytes,
            loadOpsPanel,
            switchToWorkshopTab, switchToApiTab, switchToPlayerTab, openTrackInPlayer, openItemInPlayer, loadApiSettings, saveApiSettings, testApiSettings, loadPipelineTracksForItem, loadItemToWorkshop, handleWorkshopAutoLoad, toggleTrackSelection, selectAllTracks, clearAllTracks, setTrackCodecFilter, selectTracksByCodec, loadPipelineRuns,
            loadWorkshopTracksForItem: loadPipelineTracksForItem,
            loadWorkshopRuns: loadPipelineRuns, // Alias for workshop rebrand
            setActiveTranscript, setActiveTranslation,
            queueAutoTranscription, pollTranscriptionProgress, cancelTranscription, getTranscriptionProgressPercent,
            copyCleanTranscriptSource,
            queueAutoTranslation, pollAutoTranslationProgress, controlAutoTranslation,
            queueExtractionForCurrentItem,
            importSubtitlesForCurrentItem,
            loadTranscriptRunDetail, loadTranslationRunDetail, closeSegmentViewer,
            deleteTranscriptRun, deleteTranslationRun,
            toggleWorkshopEnabled, togglePipelineSection, toggleWorkshopSection, toggleApiSection, toggleSidebarSection,
            openDetail, closeDetail,
            setRating, toggleFavorite,
            saveNotes,
            addCustomTag, removeCustomTag,
            overrideProductCode,
            confirmMatch, unconfirmMatch, refreshMetadata, toggleMatchConfidence, toggleOverrideSection, toggleCoverSection,
            chooseManualCover, onCoverFileChange,
            translateSelectedMetadata,
            deleteItem,
            deleteSelectedGame, deleteGameLoading, deleteGameError,
            ignoredGamePaths, ignoredGamePathsLoading, ignoredGamePathsError,
            loadIgnoredGamePaths, unignoreGamePath,
            filterBySeiyuu, filterByTag,
            toggleFavoriteFilter, clearFilters, applyStatFilter, showUnmatchedFiles,
            unmatchedFilesPanelOpen, unmatchedFilesLoading, unmatchedFilesList,
            // Per-subtab filter state, search input, stats, helpers.
            gameFilters, gameSearchQuery,
            tokutenFilters, tokutenSearchQuery,
            currentSearchQuery,
            gameStats, tokutenStats,
            loadGameStats, loadTokutenStats,
            gameDistinctOptions, loadGameDistinctOptions,
            applyGameStatFilter, applyTokutenStatFilter,
            // is_manual filter dropdown (shared overlay across subtabs).
            isManualDropdownFor, isManualDropdownRect,
            openIsManualDropdown, closeIsManualDropdown,
            selectIsManualOption, currentIsManualValue,
            // Unmatched games cleanup queue.
            unmatchedQueueOpen, unmatchedQueueItems, unmatchedQueueIndex,
            unmatchedQueueLoading, unmatchedQueueSearch, unmatchedQueueResults,
            unmatchedQueueSearching, unmatchedQueueBusy,
            unmatchedQueueMessage, unmatchedQueueError,
            openUnmatchedQueue, closeUnmatchedQueue,
            unmatchedQueueCurrent, unmatchedQueueSearchInput, runUnmatchedQueueSearch,
            acceptUnmatchedVndbResult, skipUnmatched, prevUnmatched, deleteUnmatched,
            // Manual-edit pane inside the cleanup queue (for unlisted games).
            unmatchedQueueManualMode, unmatchedQueueManualDraft, unmatchedQueueCoverInput,
            startUnmatchedManual, cancelUnmatchedManual, saveUnmatchedManual,
            chooseUnmatchedCover, onUnmatchedCoverFileChange,
            // Per-game play_status quick mutation (card pill + detail pill).
            setGamePlayStatus,
            playStatusDropdownFor, playStatusDropdownRect,
            PLAY_STATUS_OPTIONS, PLAY_STATUS_LABELS, formatPlayStatus,
            openPlayStatusDropdown,
            selectPlayStatusOption, closePlayStatusDropdown,
            // Drama-CD listen_status — same dropdown pattern, items-side.
            setItemListenStatus,
            listenStatusDropdownFor, listenStatusDropdownRect,
            LISTEN_STATUS_OPTIONS, LISTEN_STATUS_LABELS, formatListenStatus,
            openListenStatusDropdown,
            selectListenStatusOption, closeListenStatusDropdown,
            // Player
            playerItemId, playerAvailableTracks, playerAvailableGroups, playerActiveGroup, switchPlayerVariant,
            playerExtracting, playerExtractStatus, extractFromPlayer,
            playerMissingAudioCount, onPlayerAudioError,
            playerTrackId, playerTrackTitle, playerTrackDuration, playerIsPlaying,
            playerCurrentTime, playerDuration, playerProgressPercent,
            playerTranscriptSegments, playerTranslationSegments, playerActiveSegmentIndex,
            playerTranscriptRunId, playerTranslationRunId,
            playerTranscriptRuns, selectPlayerTranscriptRun, describeTranscriptRun,
            playerEditMode, togglePlayerEditMode,
            glossaryExpanded, toggleGlossaryExpanded,
            playerAudioElement,
            // Inline segment editor (shared between Player + Workshop).
            editingSegment, editingSegmentText, editingSegmentSaving, editingSegmentError,
            startSegmentEdit, cancelSegmentEdit, saveSegmentEdit, isEditingSegment, segmentIsEdited,
            selectedTranslationRunId,
            transcriptList,
            transcriptScroll,
            scrollActiveTranscriptIntoView, lyricLineClass,
            playerFollowTranscript,
            playerTheme, setPlayerTheme,
            itemHasPlayableContent, checkingPlayableContent,
            canSkipPrev, canSkipNext,
            playerVolume, playerMuted, playerShowVolumeControl, toggleMute,
            formatTime, loadPlayerItemTracks, loadPlayerTrack, closePlayer, togglePlayPause, seekToPosition, jumpToSegment,
            skipToPrevSegment, skipToNextSegment,
            onPlayerTimeUpdate, onPlayerMetadataLoaded, onPlayerEnded,
            scrollToPlayerTop, jumpToCurrentSegment, playItemInPlayer,
        };
    }
});

// Minimal click-outside directive. Used by the Workshop search autocomplete
// dropdown to close itself when the user clicks anywhere else on the page.
app.directive('click-outside', {
    mounted(el, binding) {
        el.__vClickOutsideHandler__ = (event) => {
            if (!(el === event.target || el.contains(event.target))) {
                binding.value(event);
            }
        };
        document.addEventListener('mousedown', el.__vClickOutsideHandler__);
    },
    unmounted(el) {
        document.removeEventListener('mousedown', el.__vClickOutsideHandler__);
        delete el.__vClickOutsideHandler__;
    },
});

// Custom hover tooltip — body-anchored, fades in after a deliberate delay so
// it doesn't fire on accidental cursor passes. Used by cover-only library
// cards where the title/circle/code aren't rendered inline. Pass a string;
// '\n' separates rows. Empty value disables the tooltip entirely.
const APP_TOOLTIP_DELAY_MS = 500;
app.directive('app-tooltip', {
    mounted(el, binding) {
        el.__appTipText__ = binding.value || '';
        el.__appTipTimer__ = null;
        el.__appTipNode__ = null;
        el.__appTipHide__ = () => {
            if (el.__appTipTimer__) { clearTimeout(el.__appTipTimer__); el.__appTipTimer__ = null; }
            if (el.__appTipNode__) { el.__appTipNode__.remove(); el.__appTipNode__ = null; }
        };
        el.__appTipShow__ = () => {
            const text = el.__appTipText__;
            if (!text) return;
            const node = document.createElement('div');
            node.className = 'app-tooltip';
            String(text).split('\n').forEach((line, i) => {
                if (!line) return;
                const row = document.createElement('div');
                row.className = i === 0 ? 'app-tooltip-row primary' : 'app-tooltip-row';
                row.textContent = line;
                node.appendChild(row);
            });
            document.body.appendChild(node);
            const rect = el.getBoundingClientRect();
            const tipW = node.offsetWidth;
            const tipH = node.offsetHeight;
            const margin = 6;
            let top = rect.top - tipH - margin;
            if (top < 8) top = rect.bottom + margin;
            let left = rect.left + rect.width / 2 - tipW / 2;
            left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8));
            node.style.top = `${Math.round(top)}px`;
            node.style.left = `${Math.round(left)}px`;
            requestAnimationFrame(() => node.classList.add('visible'));
            el.__appTipNode__ = node;
        };
        el.__appTipEnter__ = () => {
            if (!el.__appTipText__) return;
            if (el.__appTipTimer__) clearTimeout(el.__appTipTimer__);
            el.__appTipTimer__ = setTimeout(el.__appTipShow__, APP_TOOLTIP_DELAY_MS);
        };
        el.addEventListener('mouseenter', el.__appTipEnter__);
        el.addEventListener('mouseleave', el.__appTipHide__);
        el.addEventListener('click', el.__appTipHide__);
    },
    updated(el, binding) {
        el.__appTipText__ = binding.value || '';
        if (!el.__appTipText__ && el.__appTipHide__) el.__appTipHide__();
    },
    unmounted(el) {
        if (el.__appTipHide__) el.__appTipHide__();
        if (el.__appTipEnter__) el.removeEventListener('mouseenter', el.__appTipEnter__);
        if (el.__appTipHide__) {
            el.removeEventListener('mouseleave', el.__appTipHide__);
            el.removeEventListener('click', el.__appTipHide__);
        }
    },
});

app.mount('#app');
window.app = app;
