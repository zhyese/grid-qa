<template>
  <div style="max-width:520px;margin:0 auto">
    <div class="card">
      <div class="card-header"><h3 class="card-title">👤 个人资料</h3></div>
      <div v-if="p" style="display:flex;flex-direction:column;gap:14px;margin-top:8px">
        <div class="cause" style="justify-content:space-between"><span>用户名</span><b>{{ p.username }}</b></div>
        <div class="cause" style="justify-content:space-between"><span>角色</span><span class="badge badge-neutral">{{ p.role }}</span></div>
        <div class="cause" style="justify-content:space-between"><span>租户</span><span class="muted">{{ p.tenantId }}</span></div>
        <div class="cause" style="justify-content:space-between"><span>状态</span><span class="badge" :class="p.status==='inactive'?'badge-danger':'badge-success'">{{ p.status==='inactive'?'已禁用':'正常' }}</span></div>
        <div class="cause" style="justify-content:space-between"><span>注册时间</span><span class="muted">{{ p.createdAt }}</span></div>
        <div>
          <label class="hint">部门（影响可见文档范围，空=公开）</label>
          <div class="row" style="gap:8px;margin-top:6px">
            <input class="input" v-model="dept" placeholder="如：调度/检修" />
            <button class="btn btn-primary" @click="saveDept">保存部门</button>
          </div>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-header"><h3 class="card-title">🔑 修改密码</h3></div>
      <div style="display:flex;flex-direction:column;gap:10px;margin-top:8px">
        <input class="input" type="password" v-model="pwd.old" placeholder="旧密码" />
        <input class="input" type="password" v-model="pwd.new1" placeholder="新密码（至少6位）" />
        <input class="input" type="password" v-model="pwd.new2" placeholder="确认新密码" />
        <button class="btn btn-primary" @click="savePwd">修改密码</button>
      </div>
    </div>
    <div class="toast" v-if="msg">{{ msg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getProfile, updateProfile, changePassword } from '../api'
const p = ref(null)
const dept = ref('')
const pwd = reactive({ old: '', new1: '', new2: '' })
const msg = ref('')
let t = null
function toast(m) { msg.value = m; clearTimeout(t); t = setTimeout(() => msg.value = '', 1800) }
async function load() { try { p.value = (await getProfile()).data; dept.value = p.value.dept || '' } catch (e) { toast('加载失败') } }
async function saveDept() { try { await updateProfile(dept.value); toast('部门已更新'); load() } catch (e) { toast('保存失败') } }
async function savePwd() {
  if (!pwd.old) { toast('请填旧密码'); return }
  if (pwd.new1.length < 6) { toast('新密码至少6位'); return }
  if (pwd.new1 !== pwd.new2) { toast('两次新密码不一致'); return }
  try { await changePassword(pwd.old, pwd.new1); toast('密码已修改'); pwd.old = pwd.new1 = pwd.new2 = '' }
  catch (e) { toast('修改失败（旧密码错误？）') }
}
onMounted(load)
</script>
