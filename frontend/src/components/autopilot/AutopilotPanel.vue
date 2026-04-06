<template>
  <div class="autopilot-panel">
    <!-- 状态头 -->
    <div class="ap-header">
      <span class="ap-dot" :class="dotClass"></span>
      <span class="ap-title">全托管驾驶</span>
      <span class="ap-stage-tag" :class="stageTagClass">{{ stageLabel }}</span>
    </div>

    <!-- 进度条 -->
    <n-progress
      type="line"
      :percentage="progressPct"
      :color="progressColor"
      indicator-placement="inside"
      :height="14"
      style="margin: 4px 0"
    />

    <!-- 数据格 -->
    <div class="ap-grid">
      <div class="ap-cell">
        <div class="label">完稿 / 书稿</div>
        <div class="value">
          {{ status?.completed_chapters || 0 }} / {{ status?.manuscript_chapters ?? status?.completed_chapters ?? 0 }} / {{ status?.target_chapters || '-' }}
        </div>
      </div>
      <div class="ap-cell">
        <div class="label">总字数</div>
        <div class="value">{{ formatWords(status?.total_words) }}</div>
      </div>
      <div class="ap-cell">
        <div class="label">当前幕 / 节拍</div>
        <div class="value">
          第 {{ (status?.current_act || 0) + 1 }} 幕
          <span v-if="isWriting">· {{ beatLabel }}</span>
        </div>
      </div>
      <div class="ap-cell">
        <div class="label">上章张力</div>
        <div class="value" :style="{ color: tensionColor }">{{ tensionLabel }}</div>
      </div>
    </div>

    <!-- 单本挂起 / 失败计数过高：与监控大盘「熔断保护 → 重置」同源接口 -->
    <n-alert v-if="needsRecovery" type="error" :show-icon="true" style="margin: 4px 0; font-size: 12px">
      <div class="recovery-hint">
        <p v-if="status?.autopilot_status === 'error'">
          本书已因<strong>连续失败</strong>被标为<strong>异常挂起</strong>（守护进程会停止处理本书）。
        </p>
        <p v-else>
          已连续失败 <strong>{{ status?.consecutive_error_count || 0 }}</strong> 次（达到 3 次会挂起）。
        </p>
        <p class="recovery-sub">
          全局 LLM 熔断在守护进程内，无法在此直接展示。下方按钮与「监控大盘 → 熔断保护 → 重置」相同：清零计数并解除异常，然后可重新点「启动全托管」。
        </p>
        <n-button
          size="small"
          type="primary"
          secondary
          :loading="toggling"
          @click="clearCircuitBreaker"
        >
          解除挂起并清零计数
        </n-button>
      </div>
    </n-alert>

    <!-- 审阅等待：宏观规划完成后、或某一幕「首次」生成章节规划后各需确认一次；确认后同幕不会反复要求审批 -->
    <n-alert v-if="needsReview" type="warning" :show-icon="true" style="margin: 4px 0; font-size: 12px">
      <strong>待审阅确认</strong>：请在侧栏查看刚生成的大纲/结构，确认后点
      <strong>「确认大纲，继续写作」</strong>。
      宏观规划完成后会停一次；之后每一幕<strong>仅在首次生成该幕章节规划</strong>时再停一次，不会无限循环。
    </n-alert>

    <!-- 实时日志流 -->
    <RealtimeLogStream v-if="isRunning" :novel-id="novelId" />

    <!-- 操作按钮 -->
    <n-space justify="end" size="small">
      <n-button v-if="needsReview" type="warning" size="small" :loading="toggling" @click="resume">
        确认大纲，继续写作
      </n-button>
      <n-button v-if="!isRunning && !needsReview" type="primary" size="small" :loading="toggling" @click="openStartModal">
        🚀 启动全托管
      </n-button>
      <n-button v-if="isRunning" type="error" ghost size="small" :loading="toggling" @click="stop">
        ⏹ 停止
      </n-button>
    </n-space>

    <!-- 启动配置弹窗 -->
    <n-modal v-model:show="showStartModal" title="自动驾驶配置" preset="dialog" positive-text="启动" @positive-click="start">
      <n-space vertical :size="12" style="width: 100%">
        <n-alert type="info" :show-icon="true" style="font-size: 12px">
          <strong>前置条件</strong>：本机需运行<strong>自动驾驶守护进程</strong>（如
          <code style="font-size: 11px">python scripts/start_daemon.py</code>
          ），否则数据库状态不会推进。若本书曾异常挂起，请先点「解除挂起并清零计数」或监控大盘「重置」。
        </n-alert>
        <n-form>
          <n-form-item label="本次最多生成章节数（成本控制）">
            <n-input-number v-model:value="startConfig.max_auto_chapters" :min="1" :max="200" />
          </n-form-item>
        </n-form>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useMessage } from 'naive-ui'
import RealtimeLogStream from './RealtimeLogStream.vue'

const props = defineProps({ novelId: String })
const emit = defineEmits(['status-change'])
const message = useMessage()

const status = ref(null)
const toggling = ref(false)
const showStartModal = ref(false)
const startConfig = ref({ max_auto_chapters: 50 })
let eventSource = null

// 计算属性
const isRunning  = computed(() => status.value?.autopilot_status === 'running')
const needsReview = computed(() => status.value?.needs_review === true)
const isWriting  = computed(() => status.value?.current_stage === 'writing')
/** 需人工解除：异常挂起，或连续失败已达阈值 */
const needsRecovery = computed(
  () =>
    status.value?.autopilot_status === 'error' ||
    (status.value?.consecutive_error_count || 0) >= 3
)
/** 无完稿时用语稿章节进度条，避免规划落库后仍显示 0% */
const progressPct = computed(() => {
  const s = status.value
  if (!s) return 0
  const done = s.completed_chapters || 0
  const ms = s.manuscript_chapters ?? 0
  if (done > 0) return s.progress_pct ?? 0
  if (ms > 0 && s.progress_pct_manuscript != null) return s.progress_pct_manuscript
  return s.progress_pct ?? 0
})
const progressColor = computed(() => {
  if (needsRecovery.value) return '#d03050'
  if (needsReview.value) return '#f0a020'
  return '#18a058'
})

const dotClass = computed(() => ({
  'dot-running': isRunning.value && !needsReview.value,
  'dot-review':  needsReview.value,
  'dot-error':   status.value?.autopilot_status === 'error',
  'dot-stopped': !isRunning.value && !needsReview.value,
}))

const stageLabel = computed(() => {
  const m = {
    macro_planning: '宏观规划', act_planning: '幕级规划',
    writing: '撰写中', auditing: '审计中',
    paused_for_review: '待审阅', completed: '已完成',
  }
  return m[status.value?.current_stage] || '待机'
})

const stageTagClass = computed(() => ({
  'tag-active':  isRunning.value && !needsReview.value,
  'tag-review':  needsReview.value,
  'tag-idle':    !isRunning.value && !needsReview.value,
}))

const beatLabel = computed(() => {
  const b = status.value?.current_beat_index || 0
  return b === 0 ? '准备' : `节拍 ${b}`
})

const tensionLabel = computed(() => {
  const t = status.value?.last_chapter_tension || 0
  if (t >= 8) return `🔥 高潮 (${t}/10)`
  if (t >= 5) return `⚡ 冲突 (${t}/10)`
  return `🌊 平缓 (${t}/10)`
})

const tensionColor = computed(() => {
  const t = status.value?.last_chapter_tension || 0
  return t >= 8 ? '#d03050' : t >= 5 ? '#f0a020' : '#18a058'
})

// 格式化
function formatWords(n) {
  if (!n) return '0'
  return n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(n)
}

// API 调用
const base = () => `/api/v1/autopilot/${props.novelId}`

async function fetchStatus() {
  const res = await fetch(`${base()}/status`)
  if (res.ok) {
    status.value = await res.json()
    emit('status-change', status.value)
  }
}

function connectSSE() {
  if (eventSource) eventSource.close()
  eventSource = new EventSource(`${base()}/events`)
  eventSource.onmessage = (e) => {
    status.value = JSON.parse(e.data)
    emit('status-change', status.value)
  }
  eventSource.onerror = () => {
    eventSource.close()
    setTimeout(connectSSE, 5000)
  }
}

function openStartModal() { showStartModal.value = true }

async function start() {
  toggling.value = true
  const res = await fetch(`${base()}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(startConfig.value)
  })
  if (res.ok) message.success('自动驾驶已启动')
  else message.error('启动失败')
  await fetchStatus()
  toggling.value = false
}

async function stop() {
  toggling.value = true
  await fetch(`${base()}/stop`, { method: 'POST' })
  message.info('已停止')
  await fetchStatus()
  toggling.value = false
}

async function resume() {
  toggling.value = true
  const res = await fetch(`${base()}/resume`, { method: 'POST' })
  if (res.ok) message.success('已确认大纲，开始写作')
  else { const e = await res.json(); message.error(e.detail || '恢复失败') }
  await fetchStatus()
  toggling.value = false
}

async function clearCircuitBreaker() {
  toggling.value = true
  try {
    const res = await fetch(`${base()}/circuit-breaker/reset`, { method: 'POST' })
    if (res.ok) {
      message.success('已解除挂起并清零失败计数，可重新启动全托管')
      await fetchStatus()
    } else {
      message.error('操作失败，请确认后端已更新并稍后重试')
    }
  } finally {
    toggling.value = false
  }
}

onMounted(() => { fetchStatus(); connectSSE() })
onUnmounted(() => eventSource?.close())
</script>

<style scoped>
.autopilot-panel {
  background: linear-gradient(135deg, rgba(24, 160, 88, 0.05) 0%, rgba(24, 160, 88, 0.02) 100%);
  border: 1px solid rgba(24, 160, 88, 0.15);
  border-radius: 12px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  transition: all 0.3s ease;
}

.autopilot-panel:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  border-color: rgba(24, 160, 88, 0.25);
}

.ap-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ap-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  box-shadow: 0 0 8px currentColor;
}

.dot-running {
  background: #18a058;
  animation: pulse 1.4s ease-in-out infinite;
}

.dot-review {
  background: #f0a020;
  animation: pulse 0.8s ease-in-out infinite;
}

.dot-error {
  background: #d03050;
}

.dot-stopped {
  background: #999;
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(0.9);
  }
}

.ap-title {
  font-weight: 600;
  color: var(--n-text-color);
  font-size: 15px;
  letter-spacing: 0.3px;
}

.ap-stage-tag {
  margin-left: auto;
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 12px;
  font-weight: 500;
  letter-spacing: 0.2px;
}

.tag-active {
  background: rgba(24, 160, 88, 0.15);
  color: #18a058;
}

.tag-review {
  background: rgba(240, 160, 32, 0.15);
  color: #f0a020;
}

.tag-idle {
  background: rgba(100, 100, 100, 0.1);
  color: #999;
}

.ap-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  padding: 4px 0;
}

.ap-cell {
  text-align: center;
  padding: 6px 4px;
  min-width: 0;
  background: rgba(255, 255, 255, 0.4);
  border-radius: 8px;
  transition: background 0.2s ease;
}

.ap-cell:hover {
  background: rgba(255, 255, 255, 0.6);
}

.ap-cell .label {
  font-size: 10px;
  color: var(--n-text-color-3);
  margin-bottom: 2px;
  font-weight: 500;
  line-height: 1.25;
}

.ap-cell .value {
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text-color);
  font-variant-numeric: tabular-nums;
  line-height: 1.3;
  word-break: break-word;
}

@media (max-width: 720px) {
  .ap-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.recovery-hint p {
  margin: 0 0 6px;
  line-height: 1.5;
}

.recovery-sub {
  font-size: 11px;
  opacity: 0.95;
  margin-bottom: 8px !important;
}
</style>
