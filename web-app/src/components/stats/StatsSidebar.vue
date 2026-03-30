<template>
  <aside class="stats-sidebar">
    <header class="sidebar-header">
      <h2 class="sidebar-title">数据概览</h2>
      <button
        class="refresh-button"
        @click="handleRefresh"
        :disabled="loading"
        aria-label="刷新数据"
      >
        <n-icon :component="RefreshOutline" :class="{ spinning: loading }" />
      </button>
    </header>

    <div class="stats-grid">
      <StatCard
        title="总书籍数"
        :value="globalStats?.total_books ?? 0"
        icon="📚"
        unit="本"
        :loading="loading"
      />
      <StatCard
        title="总章节数"
        :value="globalStats?.total_chapters ?? 0"
        icon="📄"
        unit="章"
        :loading="loading"
      />
      <StatCard
        title="总字数"
        :value="formatNumber(globalStats?.total_words ?? 0)"
        icon="✍️"
        unit="字"
        :loading="loading"
      />
      <StatCard
        title="各阶段书籍"
        :value="formatStages(globalStats?.books_by_stage ?? {})"
        icon="📊"
        :loading="loading"
      />
    </div>

    <footer class="sidebar-footer">
      <span class="update-time">{{ updateTimeText }}</span>
    </footer>
  </aside>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { NIcon } from 'naive-ui'
import { RefreshOutline } from '@vicons/ionicons5'
import StatCard from './StatCard.vue'
import { useStatsStore } from '@/stores/statsStore'

const statsStore = useStatsStore()
const { globalStats, loading } = storeToRefs(statsStore)

const lastUpdateTime = ref<Date | null>(null)
let updateInterval: number | null = null

onMounted(async () => {
  try {
    await statsStore.loadGlobalStats()
    lastUpdateTime.value = new Date()
  } catch (error) {
    console.error('Failed to load stats:', error)
  }

  // Update time display every minute
  updateInterval = window.setInterval(() => {
    lastUpdateTime.value = new Date()
  }, 60000)
})

onUnmounted(() => {
  if (updateInterval) {
    clearInterval(updateInterval)
  }
})

async function handleRefresh() {
  try {
    await statsStore.loadGlobalStats(true)
    lastUpdateTime.value = new Date()
  } catch (error) {
    console.error('Failed to refresh stats:', error)
    window.$message?.error('刷新失败，请稍后重试')
  }
}

function formatNumber(num: number): string {
  if (num >= 10000) {
    return (num / 10000).toFixed(1) + '万'
  }
  return num.toLocaleString()
}

function formatStages(stages: Record<string, number>): string {
  return Object.entries(stages)
    .map(([stage, count]) => `${stage}:${count}`)
    .join(' ')
}

function formatTime(date: Date | null): string {
  if (!date) return '未更新'

  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMinutes = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)

  if (diffMinutes < 1) {
    return '刚刚'
  } else if (diffMinutes < 60) {
    return `${diffMinutes}分钟前`
  } else if (diffHours < 24) {
    return `${diffHours}小时前`
  } else {
    return date.toLocaleDateString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    })
  }
}

const updateTimeText = computed(() => formatTime(lastUpdateTime.value))
</script>

<style scoped>
.stats-sidebar {
  position: fixed;
  left: 0;
  top: 0;
  width: 280px;
  height: 100vh;
  background: #f5f5f5;
  padding: 24px;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}

.sidebar-title {
  font-size: 20px;
  font-weight: 600;
  color: #111827;
  margin: 0;
}

.refresh-button {
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  transition: background-color 0.2s ease-in-out;
}

.refresh-button:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.05);
}

.refresh-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.refresh-button:focus-visible {
  outline: 2px solid #3b82f6;
  outline-offset: 2px;
}

.refresh-button .n-icon {
  font-size: 20px;
  color: #6b7280;
}

.refresh-button .spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.stats-grid {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1;
}

.sidebar-footer {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid #e5e7eb;
}

.update-time {
  font-size: 12px;
  color: #9ca3af;
}
</style>
