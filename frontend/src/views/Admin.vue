<template>
  <div class="page">
    <header class="topbar">
      <span>系统管理</span>
      <nav>
        <router-link to="/chat">问答</router-link> |
        <router-link to="/documents">文档</router-link> |
        <router-link to="/dashboard">统计</router-link> |
        <router-link to="/admin">管理</router-link> |
        <a href="#" @click.prevent="logout">退出({{ auth.username }})</a>
      </nav>
    </header>
    <div class="admin-wrap">
      <!-- 反馈管理：坏 case 看板 + 一键回流 golden（反馈→评测闭环）-->
      <div class="card">
        <h3>反馈管理（坏 case 看板，共 {{ feedbacks.total }} 条）
          <button class="tab-btn" :class="{on: fbFilter==='dislike'}" @click="loadFeedbacks('dislike')">只看👎坏case</button>
          <button class="tab-btn" :class="{on: fbFilter==='like'}" @click="loadFeedbacks('like')">只看👍</button>
          <button class="tab-btn" :class="{on: fbFilter===''}" @click="loadFeedbacks('')">全部</button>
        </h3>
        <p class="tip">dislike 自动异步跑 LLM-judge 打质量分；确认是坏 case 后「标为 golden」→ 自动写入 golden 集 → CI 门禁永久覆盖。</p>
        <table>
          <thead><tr><th>问题</th><th>反馈</th><th>judge幻觉</th><th>理由/纠错</th><th>用户</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="f in feedbacks.list" :key="f.id">
              <td class="wrap">{{ f.query }}</td>
              <td>{{ f.feedback === 'like' ? '👍' : '👎' }}</td>
              <td><span :class="judgeClass(f.judgeHalluc)">{{ f.judgeHalluc != null ? (f.judgeHalluc * 100).toFixed(0) + '%' : '待评' }}</span></td>
              <td class="wrap">{{ f.reason || '—' }}</td>
              <td>{{ f.username || '—' }}</td>
              <td class="nowrap">{{ f.createdAt }}</td>
              <td><button class="secondary" @click="markGolden(f)">标为 golden</button></td>
            </tr>
            <tr v-if="!feedbacks.list.length"><td colspan="7" class="empty-row">暂无反馈</td></tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <h3>操作日志（共 {{ logs.total }} 条）</h3>
        <table>
          <thead><tr><th>用户</th><th>类型</th><th>内容</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="l in logs.list" :key="l.id">
              <td>{{ l.operateUser }}</td><td>{{ l.operateType }}</td>
              <td class="wrap">{{ l.content }}</td><td class="nowrap">{{ l.operateTime }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>Milvus 索引配置</h3>
        <input v-model="milvus.indexType" placeholder="indexType" />
        <input v-model="milvus.nprobe" placeholder="nprobe" />
        <input v-model="milvus.nlist" placeholder="nlist" />
        <button @click="saveMilvus">保存</button>
      </div>
      <div class="card">
        <h3>模型参数配置</h3>
        <input v-model="model.modelType" placeholder="modelType" />
        <input v-model="model.temperature" placeholder="temperature" />
        <button @click="saveModel">保存</button>
      </div>
    </div>
    <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getLogs, configMilvus, configModel, getFeedbacks, markFeedbackGolden } from '../api'

const auth = useAuthStore()
const router = useRouter()
const logs = ref({ total: 0, list: [] })
const feedbacks = ref({ total: 0, list: [] })
const fbFilter = ref('dislike')
const milvus = reactive({ indexType: 'IVF_FLAT', nprobe: 16, nlist: 1024 })
const model = reactive({ modelType: 'deepseek', temperature: 0.2 })
const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1600) }

async function loadLogs() { logs.value = (await getLogs({ page: 1, size: 20 })).data }
async function loadFeedbacks(fb = 'dislike') {
  fbFilter.value = fb
  try { feedbacks.value = (await getFeedbacks({ feedback: fb, page: 1, size: 30 })).data } catch (e) { toast('加载反馈失败') }
}
async function markGolden(f) {
  try {
    const r = (await markFeedbackGolden(f.id)).data
    toast(r.added ? `已加入 golden 集（当前 ${r.total} 条）` : `未加入：${r.reason || '已存在'}`)
  } catch (e) { toast('操作失败') }
}
function judgeClass(h) {
  if (h == null) return 'judge-pending'
  if (h >= 0.5) return 'judge-high'
  if (h >= 0.2) return 'judge-mid'
  return 'judge-low'
}
async function saveMilvus() {
  await configMilvus(milvus.indexType, { nprobe: Number(milvus.nprobe), nlist: Number(milvus.nlist) })
  toast('已保存')
}
async function saveModel() {
  await configModel(model.modelType, { temperature: Number(model.temperature) })
  toast('已保存')
}
function logout() { auth.logout(); router.push('/login') }
onMounted(() => { loadLogs(); loadFeedbacks('dislike') })
</script>

<style scoped>
.admin-wrap { max-width: 1000px; margin: 20px auto; padding: 0 16px; }
h3 { margin: 0 0 12px; color: #1e3a8a; }
.tip { color: #94a3b8; font-size: 12px; margin: -6px 0 10px; }
input { margin-right: 8px; width: 130px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 13px; }
th { background: #f1f5f9; }
td.wrap { max-width: 260px; white-space: normal; word-break: break-all; }
td.nowrap { white-space: nowrap; }
.empty-row { text-align: center; color: #94a3b8; padding: 20px; }
.tab-btn { font-size: 12px; padding: 3px 10px; margin-left: 6px; background: #e2e8f0; color: #475569; border: none; border-radius: 12px; cursor: pointer; }
.tab-btn.on { background: #2563eb; color: #fff; }
.judge-high { color: #dc2626; font-weight: 600; }
.judge-mid { color: #d97706; }
.judge-low { color: #16a34a; }
.judge-pending { color: #94a3b8; }
button { cursor: pointer; border: none; padding: 5px 12px; border-radius: 4px; background: #2563eb; color: #fff; }
button.secondary { background: #64748b; }
.toast { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 8px 18px; border-radius: 6px; z-index: 999; font-size: 13px; }
</style>
