<template>
  <div class="home">
    <StatsSidebar />
    <div class="home-content">
      <div class="home-bg" aria-hidden="true" />
      <div class="container">
      <header class="header">
        <h1 class="title">书稿工作台</h1>
        <p class="subtitle">从一句梗概到完整书稿，结构规划与校阅一站完成</p>
      </header>

      <n-card class="create-card" :bordered="false" content-style="padding: 24px 26px;">
        <n-space vertical :size="20">
          <div class="create-header">
            <h3 class="create-title">新建书目</h3>
            <n-button text type="primary" @click="showAdvanced = !showAdvanced">
              {{ showAdvanced ? '收起' : '高级设置' }}
            </n-button>
          </div>

          <n-input
            v-model:value="newBook.premise"
            type="textarea"
            placeholder="描述你想写的故事…&#10;&#10;例如：程序员穿越成状元，用工程思维整顿吏治。"
            :rows="5"
            :disabled="creating"
            size="large"
            class="premise-input"
          />

          <div v-show="showAdvanced" class="advanced-settings">
            <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
              <n-gi>
                <n-form-item label="书名">
                  <n-input v-model:value="newBook.title" placeholder="留空则从梗概自动截取" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item label="类型">
                  <n-select v-model:value="newBook.genre" :options="genreOptions" placeholder="选择类型" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item label="章节数">
                  <n-input-number v-model:value="newBook.chapters" :min="1" :max="100" class="w-full" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item label="每章字数">
                  <n-input-number v-model:value="newBook.words" :min="500" :max="10000" :step="500" class="w-full" />
                </n-form-item>
              </n-gi>
            </n-grid>
          </div>

          <n-space justify="end">
            <n-button
              type="primary"
              size="large"
              round
              :loading="creating"
              :disabled="!newBook.premise.trim()"
              @click="handleCreate"
            >
              <template #icon>
                <n-icon><IconSpark /></n-icon>
              </template>
              建档并进入工作台
            </n-button>
          </n-space>
        </n-space>
      </n-card>

      <section class="books-section">
        <div class="section-header">
          <h2 class="section-title">我的书目</h2>
          <n-input
            v-model:value="searchQuery"
            placeholder="搜索书名或类型…"
            clearable
            round
            class="search-input"
          >
            <template #prefix>
              <n-icon><IconSearch /></n-icon>
            </template>
          </n-input>
        </div>

        <div v-if="loading" class="loading-state">
          <n-spin size="large" />
          <p>加载中…</p>
        </div>

        <div v-else-if="filteredBooks.length === 0" class="empty-state">
          <n-empty description="还没有书目，创建第一个吧" size="large" />
        </div>

        <n-grid v-else :cols="3" :x-gap="20" :y-gap="20" responsive="screen">
          <n-gi v-for="(book, idx) in filteredBooks" :key="book.slug">
            <n-card
              class="book-card"
              hoverable
              :style="{ animationDelay: `${idx * 0.04}s` }"
            >
              <div class="book-content" @click="navigateToBook(book.slug)">
                <div class="book-header">
                  <h3 class="book-title">{{ book.title }}</h3>
                  <n-space :size="6" align="center" @click.stop>
                    <n-tag :type="getStageType(book.stage)" size="small" round>
                      {{ book.stage_label }}
                    </n-tag>
                    <n-popconfirm
                      positive-text="删除"
                      negative-text="取消"
                      @positive-click="() => handleDeleteBook(book.slug)"
                    >
                      <template #trigger>
                        <n-button
                          quaternary
                          circle
                          size="small"
                          type="error"
                          :loading="deletingSlug === book.slug"
                          aria-label="删除书目"
                        >
                          <template #icon>
                            <n-icon><IconTrash /></n-icon>
                          </template>
                        </n-button>
                      </template>
                      将删除「{{ book.title }}」及本地全部章节与设定，且不可恢复。确定删除吗？
                    </n-popconfirm>
                  </n-space>
                </div>
                <div class="book-meta">
                  <n-tag size="small" :bordered="false" round>
                    {{ book.genre || '未分类' }}
                  </n-tag>
                </div>
              </div>
            </n-card>
          </n-gi>
        </n-grid>
      </section>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { h, ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { bookApi } from '../api/book'
import StatsSidebar from '@/components/stats/StatsSidebar.vue'

const IconSpark = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', {
      fill: 'currentColor',
      d: 'M13 2L3 14h8l-1 8 10-12h-8l1-8z',
    })
  )

const IconSearch = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', {
      fill: 'currentColor',
      d: 'M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
    })
  )

const IconTrash = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', {
      fill: 'currentColor',
      d: 'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
    })
  )

const router = useRouter()
const message = useMessage()

const showAdvanced = ref(false)
const creating = ref(false)
const loading = ref(false)
const books = ref<any[]>([])
const searchQuery = ref('')
const deletingSlug = ref<string | null>(null)

const newBook = ref({
  title: '',
  premise: '',
  genre: '',
  chapters: 5,
  words: 2500,
})

const genreOptions = [
  { label: '玄幻', value: '玄幻' },
  { label: '都市', value: '都市' },
  { label: '科幻', value: '科幻' },
  { label: '历史', value: '历史' },
  { label: '武侠', value: '武侠' },
  { label: '仙侠', value: '仙侠' },
  { label: '奇幻', value: '奇幻' },
  { label: '游戏', value: '游戏' },
  { label: '悬疑', value: '悬疑' },
  { label: '其他', value: '其他' },
]

const filteredBooks = computed(() => {
  if (!searchQuery.value.trim()) {
    return books.value
  }
  const query = searchQuery.value.toLowerCase()
  return books.value.filter(
    book =>
      book.title.toLowerCase().includes(query) ||
      (book.genre && book.genre.toLowerCase().includes(query))
  )
})

const fetchBooks = async () => {
  loading.value = true
  try {
    books.value = await bookApi.getList()
  } catch {
    message.error('加载失败')
  } finally {
    loading.value = false
  }
}

const handleCreate = async () => {
  if (!newBook.value.premise.trim()) {
    message.warning('请输入故事创意')
    return
  }

  creating.value = true
  try {
    const payload: any = {
      title: newBook.value.title || newBook.value.premise.substring(0, 20),
      premise: newBook.value.premise,
    }

    if (showAdvanced.value) {
      if (newBook.value.genre) payload.genre = newBook.value.genre
      if (newBook.value.chapters) payload.chapters = newBook.value.chapters
      if (newBook.value.words) payload.words = newBook.value.words
    }

    const result = await bookApi.create(payload)
    message.success('创建成功')
    router.push(`/book/${result.slug}/workbench`)
  } catch (error: any) {
    message.error(error.response?.data?.detail || '创建失败')
  } finally {
    creating.value = false
  }
}

const navigateToBook = (slug: string) => {
  router.push(`/book/${slug}/workbench`)
}

const handleDeleteBook = async (slug: string) => {
  deletingSlug.value = slug
  try {
    await bookApi.deleteBook(slug)
    message.success('书目已删除')
    books.value = books.value.filter(b => b.slug !== slug)
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    message.error(typeof detail === 'string' ? detail : '删除失败')
  } finally {
    deletingSlug.value = null
  }
}

const getStageType = (stage: string) => {
  const map: Record<string, any> = {
    plan: 'info',
    write: 'warning',
    done: 'success',
  }
  return map[stage] || 'default'
}

onMounted(() => {
  fetchBooks()
})
</script>

<style scoped>
.home {
  display: flex;
  min-height: 100vh;
}

.home-content {
  flex: 1;
  margin-left: 280px;
  padding: 24px;
  position: relative;
  overflow: hidden;
}

.home-bg {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 110% 80% at 50% -30%, rgba(99, 102, 241, 0.35), transparent 55%),
    radial-gradient(ellipse 60% 50% at 100% 20%, rgba(14, 165, 233, 0.18), transparent 45%),
    radial-gradient(ellipse 50% 40% at 0% 60%, rgba(167, 139, 250, 0.2), transparent 50%),
    linear-gradient(180deg, #e8ecf8 0%, #f0f2f8 45%, #eef1f7 100%);
  z-index: 0;
}

.container {
  position: relative;
  z-index: 1;
  max-width: 1120px;
  margin: 0 auto;
}

.header {
  text-align: center;
  margin-bottom: 40px;
  animation: fade-up 0.55s ease both;
}

.title {
  font-size: clamp(2rem, 4vw, 2.75rem);
  font-weight: 700;
  margin: 0 0 12px;
  letter-spacing: -0.03em;
  color: #0f172a;
}

.subtitle {
  font-size: 1.05rem;
  color: #475569;
  margin: 0;
  font-weight: 400;
}

.create-card {
  margin-bottom: 36px;
  border-radius: var(--app-radius-lg, 16px);
  box-shadow: var(--app-shadow-hover);
  animation: fade-up 0.55s ease 0.08s both;
}

.create-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.create-title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
}

.premise-input :deep(textarea) {
  font-size: 15px;
  line-height: 1.55;
}

.advanced-settings {
  padding: 16px;
  background: rgba(79, 70, 229, 0.04);
  border-radius: 12px;
  border: 1px solid var(--app-border);
}

.w-full {
  width: 100%;
}

.books-section {
  background: var(--app-surface, #fff);
  border-radius: var(--app-radius-lg, 16px);
  padding: 28px 28px 32px;
  box-shadow: var(--app-shadow);
  animation: fade-up 0.55s ease 0.14s both;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}

.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #0f172a;
}

.search-input {
  width: min(100%, 280px);
}

.loading-state,
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 72px 20px;
  color: #64748b;
}

.loading-state p {
  margin-top: 16px;
  font-size: 14px;
}

.book-card {
  cursor: pointer;
  border-radius: 14px;
  height: 100%;
  transition:
    transform var(--app-transition),
    box-shadow var(--app-transition);
  animation: fade-up 0.45s ease both;
}

.book-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--app-shadow-hover);
}

.book-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.book-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
}

.book-header :deep(.n-space) {
  flex-shrink: 0;
}

.book-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  line-height: 1.4;
}

.book-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

@keyframes fade-up {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
