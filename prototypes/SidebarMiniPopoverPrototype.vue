<template>
  <aside ref="sidebarRef" class="sb-root" :class="{ 'is-collapsed': isCollapsed }">
    <div class="sb-top">
      <button class="sb-btn sb-collapse-toggle" @click="isCollapsed = !isCollapsed">
        <span class="sb-icon">[]</span>
        <span v-if="!isCollapsed" class="sb-label">Collapse</span>
      </button>
    </div>

    <nav class="sb-groups">
      <button
        v-for="group in groups"
        :key="group.id"
        class="sb-btn sb-group-btn"
        :class="{
          'is-open': !isCollapsed && openGroups.has(group.id),
          'is-active': isCollapsed && activeCollapsedGroup === group.id
        }"
        @click="onGroupClick(group.id)"
      >
        <span class="sb-icon">{{ group.icon }}</span>
        <span v-if="!isCollapsed" class="sb-label">{{ group.label }}</span>
      </button>
    </nav>

    <section v-if="!isCollapsed" class="sb-inline-content">
      <div v-for="group in groups" :key="group.id" class="sb-group-panel" v-show="openGroups.has(group.id)">
        <header class="sb-group-header">{{ group.label }}</header>
        <div v-if="group.id === 'libraryTools'" class="sb-group-body">
          <input v-model="library.search" class="sb-input" placeholder="Search library..." />
          <select v-model="library.filter" class="sb-select">
            <option value="">All Filters</option>
            <option value="seiyuu">Seiyuu</option>
            <option value="tags">Tags</option>
            <option value="status">Status</option>
          </select>
          <select v-model="library.sort" class="sb-select">
            <option value="recent">Sort: Recent</option>
            <option value="title_asc">Sort: Title A-Z</option>
            <option value="title_desc">Sort: Title Z-A</option>
          </select>
          <select v-model="library.action" class="sb-select">
            <option value="">Library Actions</option>
            <option value="scan">Scan</option>
            <option value="fetch">Fetch Metadata</option>
            <option value="bulk_confirm">Bulk Confirm</option>
          </select>
        </div>

        <div v-else-if="group.id === 'workshop'" class="sb-group-body">
          <label class="sb-row">
            <input type="checkbox" v-model="workshop.enabled" />
            <span>Workshop Enabled</span>
          </label>
          <div class="sb-row">Status: <strong>{{ workshop.status }}</strong></div>
          <button class="sb-btn sb-link-btn">View Jobs</button>
        </div>

        <div v-else-if="group.id === 'systemOps'" class="sb-group-body">
          <button class="sb-btn">Maintenance</button>
          <div class="sb-row">Health: {{ systemOps.healthSummary }}</div>
          <div class="sb-row">Last Fetch: {{ systemOps.lastFetchSummary }}</div>
        </div>

        <div v-else-if="group.id === 'libraryPaths'" class="sb-group-body">
          <ul class="sb-list">
            <li v-for="(path, idx) in libraryPaths.paths" :key="idx">{{ path }}</li>
          </ul>
          <label class="sb-row">
            <input type="checkbox" v-model="libraryPaths.recursive" />
            <span>Recursive Scan</span>
          </label>
          <label class="sb-row">
            <input type="checkbox" v-model="libraryPaths.watcherEnabled" />
            <span>Path Watcher</span>
          </label>
        </div>
      </div>
    </section>

    <section
      v-if="isCollapsed && activeCollapsedGroup"
      class="sb-flyout is-mini-popover"
    >
      <header class="sb-group-header">{{ groupLabel(activeCollapsedGroup) }}</header>

      <div class="sb-group-body" v-if="activeCollapsedGroup === 'libraryTools'">
        <input v-model="library.search" class="sb-input" placeholder="Search..." />
        <select v-model="library.filter" class="sb-select">
          <option value="">All Filters</option>
          <option value="seiyuu">Seiyuu</option>
          <option value="tags">Tags</option>
          <option value="status">Status</option>
        </select>
        <select v-model="library.sort" class="sb-select">
          <option value="recent">Recent</option>
          <option value="title_asc">Title A-Z</option>
          <option value="title_desc">Title Z-A</option>
        </select>
        <select v-model="library.action" class="sb-select">
          <option value="">Library Actions</option>
          <option value="scan">Scan</option>
          <option value="fetch">Fetch Metadata</option>
          <option value="bulk_confirm">Bulk Confirm</option>
        </select>
      </div>

      <div class="sb-group-body" v-else-if="activeCollapsedGroup === 'workshop'">
        <label class="sb-row">
          <input type="checkbox" v-model="workshop.enabled" />
          <span>Workshop Enabled</span>
        </label>
        <div class="sb-row">Status: <strong>{{ workshop.status }}</strong></div>
        <button class="sb-btn sb-link-btn">View Jobs</button>
      </div>

      <div class="sb-group-body" v-else-if="activeCollapsedGroup === 'systemOps'">
        <button class="sb-btn">Maintenance</button>
        <div class="sb-row">Health: {{ systemOps.healthSummary }}</div>
        <div class="sb-row">Last Fetch: {{ systemOps.lastFetchSummary }}</div>
      </div>

      <div class="sb-group-body" v-else-if="activeCollapsedGroup === 'libraryPaths'">
        <ul class="sb-list">
          <li v-for="(path, idx) in libraryPaths.paths" :key="idx">{{ path }}</li>
        </ul>
        <label class="sb-row">
          <input type="checkbox" v-model="libraryPaths.recursive" />
          <span>Recursive Scan</span>
        </label>
        <label class="sb-row">
          <input type="checkbox" v-model="libraryPaths.watcherEnabled" />
          <span>Path Watcher</span>
        </label>
      </div>
    </section>

    <div class="sb-bottom">
      <button class="sb-btn sb-settings-btn" @click="toggleSettings">
        <span class="sb-icon">S</span>
        <span v-if="!isCollapsed" class="sb-label">Settings</span>
      </button>
    </div>

    <section v-if="settingsOpen" class="sb-settings-panel">
      <header class="sb-group-header">Settings</header>
      <div class="sb-group-body">
        <div class="sb-row"><strong>Sidebar Collapse Mode</strong></div>
        <label class="sb-row">
          <input type="radio" value="independent" v-model="collapseMode" />
          <span>Independent</span>
        </label>
        <label class="sb-row">
          <input type="radio" value="accordion" v-model="collapseMode" />
          <span>Accordion</span>
        </label>
      </div>
    </section>
  </aside>
</template>

<script setup>
import { ref, reactive, watch, onMounted, onBeforeUnmount } from 'vue'

const groups = [
  { id: 'libraryTools', label: 'Library Tools', icon: 'LT' },
  { id: 'workshop', label: 'Workshop', icon: 'WS' },
  { id: 'systemOps', label: 'System Ops', icon: 'OP' },
  { id: 'libraryPaths', label: 'Library Paths', icon: 'LP' },
]

const isCollapsed = ref(false)
const collapseMode = ref('independent')
const openGroups = ref(new Set(['libraryTools']))
const activeCollapsedGroup = ref(null)
const settingsOpen = ref(false)
const sidebarRef = ref(null)

const library = reactive({ search: '', filter: '', sort: 'recent', action: '' })
const workshop = reactive({ enabled: true, status: 'active' })
const systemOps = reactive({
  healthSummary: 'Scan: idle, Fetch: idle',
  lastFetchSummary: 'Success 12 / Failed 1 / Skipped 4',
})
const libraryPaths = reactive({
  paths: ['G:\\DramaCD\\DL', 'G:\\DramaCD\\DL\\Hirame'],
  recursive: true,
  watcherEnabled: false,
})

function groupLabel(id) {
  return groups.find((g) => g.id === id)?.label || ''
}

function onGroupClick(groupId) {
  settingsOpen.value = false

  if (isCollapsed.value) {
    activeCollapsedGroup.value = activeCollapsedGroup.value === groupId ? null : groupId
    return
  }

  if (collapseMode.value === 'accordion') {
    openGroups.value = openGroups.value.has(groupId) ? new Set() : new Set([groupId])
  } else {
    const next = new Set(openGroups.value)
    if (next.has(groupId)) next.delete(groupId)
    else next.add(groupId)
    openGroups.value = next
  }
}

function toggleSettings() {
  if (isCollapsed.value) activeCollapsedGroup.value = null
  settingsOpen.value = !settingsOpen.value
}

watch(collapseMode, (mode) => {
  if (mode === 'accordion' && openGroups.value.size > 1) {
    const first = openGroups.value.values().next().value
    openGroups.value = first ? new Set([first]) : new Set()
  }
})

watch(isCollapsed, (collapsed) => {
  if (!collapsed) activeCollapsedGroup.value = null
})

function onGlobalClick(e) {
  const root = sidebarRef.value
  if (!root || root.contains(e.target)) return
  if (isCollapsed.value) {
    activeCollapsedGroup.value = null
    settingsOpen.value = false
  }
}

onMounted(() => document.addEventListener('pointerdown', onGlobalClick))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onGlobalClick))
</script>

<style scoped>
.sb-root { position: relative; display: flex; flex-direction: column; width: 280px; min-height: 100vh; }
.sb-root.is-collapsed { width: 64px; }
.sb-top, .sb-bottom { padding: 8px; }
.sb-groups { display: flex; flex-direction: column; gap: 6px; padding: 8px; }
.sb-inline-content { padding: 8px; display: flex; flex-direction: column; gap: 10px; }
.sb-group-panel { border: 1px solid #ddd; border-radius: 8px; }
.sb-group-header { padding: 8px 10px; font-weight: 600; border-bottom: 1px solid #eee; }
.sb-group-body { padding: 10px; display: grid; gap: 8px; }
.sb-btn { display: inline-flex; align-items: center; gap: 8px; padding: 8px; }
.sb-group-btn { width: 100%; justify-content: flex-start; }
.sb-icon { width: 20px; text-align: center; font-size: 11px; }
.sb-input, .sb-select { width: 100%; padding: 6px; }
.sb-row { display: flex; align-items: center; gap: 8px; }
.sb-list { margin: 0; padding-left: 18px; }
.sb-bottom { margin-top: auto; }

.sb-flyout {
  position: absolute;
  top: 8px;
  left: calc(100% + 8px);
  width: 280px;
  border: 1px solid #ddd;
  border-radius: 10px;
  background: #fff;
  z-index: 20;
}

.sb-settings-panel {
  position: absolute;
  bottom: 8px;
  left: calc(100% + 8px);
  width: 280px;
  border: 1px solid #ddd;
  border-radius: 10px;
  background: #fff;
  z-index: 21;
}
</style>
