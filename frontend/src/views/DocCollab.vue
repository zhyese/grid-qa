<template>
  <div>
    <!-- 文档选择 -->
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">📄 选择文档</h3>
        <span class="hint">批注与会签均挂在所选文档上</span>
      </div>
      <div class="row" style="gap:8px">
        <input class="input" style="max-width:260px" v-model="docKeyword" placeholder="搜索文档名…" @keyup.enter="loadDocs" />
        <button class="btn btn-ghost btn-sm" @click="loadDocs">🔍 搜索</button>
      </div>
      <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap">
        <button v-for="d in docs" :key="d.id" class="btn btn-sm"
                :class="{ 'btn-primary': docId === d.id }" @click="pickDoc(d.id)">
          {{ d.docName?.slice(0, 26) || d.id }}
        </button>
      </div>
    </div>

    <template v-if="docId">
      <div class="tabs">
        <button class="tab" :class="{ active: tab === 'annotations' }" @click="tab = 'annotations'">💬 批注讨论
          <span v-if="annStats" class="badge badge-neutral">{{ annStats.total }}</span>
        </button>
        <button class="tab" :class="{ active: tab === 'signoff' }" @click="loadSignoffs(); tab = 'signoff'">✍️ 电子签批
          <span v-if="signoffs.length" class="badge badge-neutral">{{ signoffs.length }}</span>
        </button>
      </div>

      <!-- 批注 -->
      <div class="card" v-show="tab === 'annotations'">
        <div class="card-header">
          <h3 class="card-title">批注列表</h3>
          <div class="row">
            <select class="select" v-model="annFilter" style="max-width:120px" @change="loadAnnotations">
              <option value="">全部</option><option value="open">待处理</option><option value="resolved">已解决</option>
            </select>
            <button class="btn btn-ghost btn-sm" @click="loadAnnotations">🔄 刷新</button>
          </div>
        </div>

        <div class="field" v-if="canAnnotate" style="border:1px dashed var(--border); border-radius:8px; padding:12px; margin-bottom:14px">
          <div class="row" style="gap:8px">
            <input class="input" style="max-width:110px" type="number" min="0" v-model="annForm.chunkIdx" placeholder="chunk#" />
            <input class="input" style="flex:1" v-model="annForm.quote" placeholder="锚点原文（可选，用于定位/高亮）" />
          </div>
          <div class="row" style="gap:8px; margin-top:8px">
            <input class="input" style="flex:1" v-model="annForm.content" placeholder="批注内容 *（如：此条规程与新版冲突，建议修订）" @keyup.enter="handleAddAnn" />
            <button class="btn btn-primary btn-sm" :disabled="!annForm.content.trim()" @click="handleAddAnn">发布批注</button>
          </div>
        </div>

        <div v-if="!annotations.length" class="empty">暂无批注</div>
        <div v-for="a in annotations" :key="a.id" class="ticket-card" style="cursor:default">
          <div class="tc-header">
            <span class="badge" :class="a.status === 'open' ? 'badge-warning' : 'badge-success'">
              {{ a.status === 'open' ? '待处理' : '已解决' }}</span>
            <span class="tc-title">chunk#{{ a.chunkIdx }} · {{ a.author }}</span>
            <span class="hint" style="margin-left:auto">{{ a.createdAt?.slice(0, 16).replace('T', ' ') }}</span>
          </div>
          <div v-if="a.quote" class="hint" style="border-left:3px solid var(--border); padding:2px 10px; margin:6px 0">
            “{{ a.quote }}”</div>
          <div style="margin:4px 0">{{ a.content }}</div>
          <div v-if="a.replies?.length" style="margin-top:6px">
            <div v-for="(r, i) in a.replies" :key="i" class="hint" style="margin:2px 0">
              ↳ <b>{{ r.author }}</b>：{{ r.content }} <span style="opacity:.6">{{ r.at?.slice(5, 16) }}</span>
            </div>
          </div>
          <div v-if="a.status === 'resolved'" class="hint">由 {{ a.resolvedBy }} 于 {{ a.resolvedAt?.slice(0, 16).replace('T', ' ') }} 解决</div>
          <div class="td-actions" style="margin-top:8px; display:flex; gap:6px" v-if="canAnnotate">
            <input class="input" style="max-width:300px" v-model="replyDraft[a.id]" placeholder="回复…" size="12"
                   @keyup.enter="handleReply(a)" />
            <button class="btn btn-ghost btn-sm" :disabled="!(replyDraft[a.id] || '').trim()" @click="handleReply(a)">回复</button>
            <button v-if="a.status === 'open'" class="btn btn-ghost btn-sm" @click="handleResolve(a, true)">✔ 解决</button>
            <button v-else class="btn btn-ghost btn-sm" @click="handleResolve(a, false)">↩ 重新打开</button>
            <button v-if="auth.role === 'admin' || a.author === auth.username" class="btn btn-danger btn-sm" @click="handleDeleteAnn(a)">🗑</button>
          </div>
        </div>
      </div>

      <!-- 签批 -->
      <div class="card" v-show="tab === 'signoff'">
        <div class="card-header">
          <h3 class="card-title">签批流</h3>
          <button v-if="canSignoff" class="btn btn-primary btn-sm" @click="flowEditor = !flowEditor">
            {{ flowEditor ? '收起' : '＋ 发起会签' }}
          </button>
        </div>

        <!-- 待我签快捷区 -->
        <div v-if="myPending.length" style="margin-bottom:14px; border:1px solid var(--warning, #f39c12); border-radius:8px; padding:10px">
          <div style="font-weight:700; margin-bottom:6px">🔔 轮到我签（{{ myPending.length }}）</div>
          <div v-for="s in myPending" :key="s.id" class="row" style="gap:8px; margin:4px 0; align-items:center">
            <span class="tc-title">{{ s.title }}</span>
            <button class="btn btn-ghost btn-sm" @click="openSignoff(s.id); showSignDialog = 'sign'">去签批</button>
          </div>
        </div>

        <!-- 发起会签 -->
        <div v-if="flowEditor && canSignoff" style="border:1px dashed var(--border); border-radius:8px; padding:12px; margin-bottom:14px">
          <div class="field"><label class="field-label">会签标题（可选）</label>
            <input class="input" v-model="flowForm.title" placeholder="缺省：文档会签·文档名" /></div>
          <label class="field-label">签批节点（顺序会签，1-10 个）</label>
          <div v-for="(n, i) in flowForm.signers" :key="i" class="row" style="gap:8px; margin:6px 0; align-items:center">
            <span class="badge badge-neutral">{{ i + 1 }}</span>
            <select class="select" style="max-width:180px" v-model="n.signer">
              <option value="">选择签批人…</option>
              <option v-for="u in signers" :key="u.signer" :value="u.signer">{{ u.signer }}（{{ u.role }}）</option>
            </select>
            <button class="btn btn-danger btn-sm" v-if="flowForm.signers.length > 1" @click="flowForm.signers.splice(i, 1)">✕</button>
          </div>
          <button class="btn btn-ghost btn-sm" @click="flowForm.signers.push({ signer: '' })">＋ 加节点</button>
          <div style="margin-top:10px">
            <button class="btn btn-primary btn-sm" :disabled="!flowForm.signers.every(n => n.signer)" @click="handleCreateSignoff">
              创建会签草稿
            </button>
          </div>
        </div>

        <div v-if="!signoffs.length" class="empty">暂无签批单</div>
        <div v-for="s in signoffs" :key="s.id" class="ticket-card" @click="openSignoff(s.id)"
             :class="{ active: selectedSignoff?.id === s.id }">
          <div class="tc-header">
            <span class="badge" :class="soBadge(s.status)">{{ soLabel(s.status) }}</span>
            <span class="tc-title">{{ s.title }}</span>
            <span class="hint" style="margin-left:auto">{{ s.createdBy }} · {{ s.createdAt?.slice(0, 16).replace('T', ' ') }}</span>
          </div>
          <div class="tc-body">
            <span v-for="n in s.flow" :key="n.seq" class="badge"
                  :class="n.status === 'signed' ? 'badge-success' : n.status === 'rejected' ? 'badge-danger' : n.seq === s.currentSeq ? 'badge-warning' : 'badge-neutral'">
              {{ n.seq }}.{{ n.signer }}{{ n.status === 'signed' ? '✓' : n.status === 'rejected' ? '✗' : n.seq === s.currentSeq ? '⏳' : '' }}
            </span>
          </div>
        </div>

        <!-- 签批详情 -->
        <div v-if="selectedSignoff" class="ticket-detail" style="margin-top:12px; border-top:1px solid var(--border); padding-top:12px">
          <div class="td-row"><b>状态：</b><span class="badge" :class="soBadge(selectedSignoff.status)">{{ soLabel(selectedSignoff.status) }}</span></div>
          <div class="td-row" v-for="n in selectedSignoff.flow" :key="n.seq">
            <b>节点 {{ n.seq }}（{{ n.signer }}）：</b>
            <span class="badge" :class="n.status === 'signed' ? 'badge-success' : n.status === 'rejected' ? 'badge-danger' : 'badge-neutral'">
              {{ { pending: '待签', signed: '已签', rejected: '已驳回' }[n.status] || n.status }}</span>
            <span class="hint" v-if="n.signedAt"> · {{ n.signedAt.slice(0, 19).replace('T', ' ') }}</span>
            <span class="hint" v-if="n.comment"> · {{ n.comment }}</span>
            <span class="hint" v-if="n.signatureHash"> · 🔗 {{ n.signatureHash.slice(0, 12) }}…</span>
          </div>
          <div class="td-row hint">文档指纹：{{ selectedSignoff.docFingerprint?.slice(0, 24) || '—（提交时固化）' }}…</div>

          <div v-if="verifyResult" class="td-row">
            <span class="badge" :class="verifyResult.valid ? 'badge-success' : 'badge-danger'">
              完整性校验：{{ verifyResult.valid ? '通过（链完整·文档未变更）' : '异常' }}</span>
            <span class="hint" v-if="verifyResult.docChanged"> · 文档在签批后发生变更</span>
            <span class="hint" v-if="!verifyResult.chainOk"> · 签名哈希链断裂</span>
          </div>

          <div class="td-actions" style="margin-top:10px; display:flex; gap:6px; flex-wrap:wrap">
            <button class="btn btn-ghost btn-sm" @click="handleVerify">🔍 完整性校验</button>
            <button v-if="selectedSignoff.status === 'draft' && canSignoff" class="btn btn-primary btn-sm"
                    @click="handleSubmit">📤 提交会签</button>
            <button v-if="canSignMe" class="btn btn-primary btn-sm" @click="showSignDialog = 'sign'">✍️ 签批</button>
            <button v-if="canSignMe" class="btn btn-ghost btn-sm" @click="showSignDialog = 'reject'">✗ 驳回</button>
            <button v-if="canCancel" class="btn btn-danger btn-sm" @click="handleCancel">撤销</button>
          </div>

          <div v-if="selectedSignoff.events?.length" style="margin-top:10px">
            <div class="src-head">审计留痕</div>
            <div v-for="(e, i) in selectedSignoff.events" :key="i" class="hint" style="margin:2px 0">
              {{ e.at?.slice(0, 19).replace('T', ' ') }} · <b>{{ e.actor }}</b> ·
              {{ { create: '创建', submit: '提交', sign: '签批', reject: '驳回', cancel: '取消' }[e.action] || e.action }}
              <span v-if="e.detail"> · {{ e.detail }}</span>
            </div>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="card"><div class="empty">选择上方文档开始协作</div></div>

    <!-- 签批/驳回口令复核弹层 -->
    <div v-if="showSignDialog" class="modal-mask" @click.self="showSignDialog = null">
      <div class="card" style="max-width:420px; margin:12vh auto">
        <h3 class="card-title" style="margin-bottom:10px">
          {{ showSignDialog === 'sign' ? '✍️ 电子签批确认' : '✗ 驳回确认' }}</h3>
        <div class="hint" style="margin-bottom:10px">
          电子签批要求口令复核；签名含时间戳并纳入防篡改哈希链。
        </div>
        <div class="field"><label class="field-label">{{ showSignDialog === 'sign' ? '签批意见（可选）' : '驳回原因 *' }}</label>
          <textarea class="input" rows="2" style="width:100%; resize:vertical; font-family:inherit"
                    v-model="signForm.comment"></textarea></div>
        <div class="field"><label class="field-label">登录口令 *</label>
          <input class="input" type="password" v-model="signForm.password" placeholder="复核登录口令" /></div>
        <div class="row" style="gap:8px; justify-content:flex-end; margin-top:6px">
          <button class="btn btn-ghost btn-sm" @click="showSignDialog = null">取消</button>
          <button class="btn btn-sm" :class="showSignDialog === 'sign' ? 'btn-primary' : 'btn-danger'"
                  :disabled="!signForm.password || (showSignDialog === 'reject' && !signForm.comment.trim())"
                  @click="handleSignAction">
            {{ showSignDialog === 'sign' ? '确认签批' : '确认驳回' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import {
  addAnnotation, cancelSignoff, createSignoff, deleteAnnotation, getAnnotationStats,
  getAnnotations, getMyPendingSignoffs, getSignoff, getSignoffs, listDocs,
  rejectSignoff, replyAnnotation, resolveAnnotation, signSignoff, submitSignoff,
  suggestSigners, verifySignoff,
} from '../api'

const auth = useAuthStore()
const canAnnotate = computed(() => hasPerm(auth.role, 'doc:annotate'))
const canSignoff = computed(() => hasPerm(auth.role, 'doc:signoff'))

const docs = ref([])
const docKeyword = ref('')
const docId = ref('')
const tab = ref('annotations')

const annotations = ref([])
const annStats = ref(null)
const annFilter = ref('')
const annForm = ref({ chunkIdx: 0, quote: '', content: '' })
const replyDraft = ref({})

const signoffs = ref([])
const myPending = ref([])
const signers = ref([])
const flowEditor = ref(false)
const flowForm = ref({ title: '', signers: [{ signer: '' }] })
const selectedSignoff = ref(null)
const verifyResult = ref(null)
const showSignDialog = ref(null)
const signForm = ref({ password: '', comment: '' })

const curNode = computed(() =>
  selectedSignoff.value?.flow?.find(n => n.seq === selectedSignoff.value.currentSeq))
const canSignMe = computed(() =>
  selectedSignoff.value?.status === 'pending' && curNode.value?.signer === auth.username)
const canCancel = computed(() => {
  const s = selectedSignoff.value
  return s && ['draft', 'pending'].includes(s.status) &&
    (canSignoff.value && (auth.role === 'admin' || s.createdBy === auth.username))
})

async function loadDocs() {
  try {
    const res = await listDocs(1, 30, docKeyword.value.trim())
    docs.value = res.data?.list || res.data?.items || []
  } catch (e) { /* 功能未开启/无权限时静默 */ }
}

function pickDoc(id) {
  if (docId.value === id) return
  docId.value = id
  selectedSignoff.value = null
  verifyResult.value = null
  loadAnnotations()
  loadAnnStats()
  loadSignoffs()
}

async function loadAnnotations() {
  if (!docId.value) return
  try {
    const res = await getAnnotations(docId.value, annFilter.value)
    annotations.value = res.data || []
  } catch (e) { /* 同上 */ }
}
async function loadAnnStats() {
  try { annStats.value = (await getAnnotationStats(docId.value)).data } catch (e) { /* 忽略 */ }
}

async function handleAddAnn() {
  if (!annForm.value.content.trim()) return
  try {
    await addAnnotation(docId.value, {
      chunkIdx: Number(annForm.value.chunkIdx) || 0,
      quote: annForm.value.quote.trim(),
      content: annForm.value.content.trim(),
    })
    annForm.value = { chunkIdx: 0, quote: '', content: '' }
    loadAnnotations(); loadAnnStats()
  } catch (e) { /* 拦截器已提示 */ }
}

async function handleReply(a) {
  const text = (replyDraft.value[a.id] || '').trim()
  if (!text) return
  await replyAnnotation(a.id, text)
  replyDraft.value[a.id] = ''
  loadAnnotations()
}

async function handleResolve(a, resolved) {
  await resolveAnnotation(a.id, resolved)
  loadAnnotations(); loadAnnStats()
}

async function handleDeleteAnn(a) {
  if (!confirm('删除该批注？')) return
  await deleteAnnotation(a.id)
  loadAnnotations(); loadAnnStats()
}

async function loadSignoffs() {
  if (!docId.value) return
  try { signoffs.value = (await getSignoffs(docId.value)).data || [] } catch (e) { /* 忽略 */ }
}

async function loadSigners() {
  if (!canSignoff.value) return
  try { signers.value = (await suggestSigners()).data || [] } catch (e) { /* 忽略 */ }
}

async function loadMyPending() {
  try { myPending.value = (await getMyPendingSignoffs()).data || [] } catch (e) { /* 忽略 */ }
}

async function handleCreateSignoff() {
  try {
    await createSignoff(docId.value, {
      title: flowForm.value.title.trim(),
      signers: flowForm.value.signers.map(n => ({ signer: n.signer, role: '' })),
    })
    flowEditor.value = false
    flowForm.value = { title: '', signers: [{ signer: '' }] }
    loadSignoffs()
  } catch (e) { /* 拦截器已提示 */ }
}

async function openSignoff(id) {
  verifyResult.value = null
  try { selectedSignoff.value = (await getSignoff(id)).data } catch (e) { /* 忽略 */ }
}

async function handleSubmit() {
  await submitSignoff(selectedSignoff.value.id)
  openSignoff(selectedSignoff.value.id); loadSignoffs()
}

async function handleSignAction() {
  const id = selectedSignoff.value.id
  try {
    if (showSignDialog.value === 'sign') {
      await signSignoff(id, signForm.value.password, signForm.value.comment.trim())
    } else {
      await rejectSignoff(id, signForm.value.password, signForm.value.comment.trim())
    }
    showSignDialog.value = null
    signForm.value = { password: '', comment: '' }
    openSignoff(id); loadSignoffs(); loadMyPending()
  } catch (e) { /* 口令错误等由拦截器提示，弹层保留 */ }
}

async function handleCancel() {
  if (!confirm('确认撤销该签批单？')) return
  await cancelSignoff(selectedSignoff.value.id)
  openSignoff(selectedSignoff.value.id); loadSignoffs(); loadMyPending()
}

async function handleVerify() {
  try { verifyResult.value = (await verifySignoff(selectedSignoff.value.id)).data } catch (e) { /* 忽略 */ }
}

function soBadge(s) {
  return { draft: 'badge-neutral', pending: 'badge-warning', signed: 'badge-success',
           rejected: 'badge-danger', cancelled: 'badge-neutral' }[s] || 'badge-neutral'
}
function soLabel(s) {
  return { draft: '草稿', pending: '会签中', signed: '已签完成',
           rejected: '已驳回', cancelled: '已取消' }[s] || s
}

onMounted(() => { loadDocs(); loadSigners(); loadMyPending() })
</script>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 100; overflow: auto; }
</style>
