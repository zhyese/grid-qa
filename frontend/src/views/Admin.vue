<template>
  <div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="loadFeedbacks(fbFilter); tab = 'feedback'" v-if="can('feedback:read')">🛡️ 反馈管理</button>
      <button class="tab" :class="{ active: tab === 'log' }" @click="tab = 'log'">📋 操作日志</button>
      <button class="tab" :class="{ active: tab === 'alert' }" @click="loadAlerts(); tab = 'alert'" v-if="can('alert:read')">🚨 告警 <span v-if="alerts.total" class="badge badge-danger">{{ alerts.total }}</span></button>
      <button class="tab" :class="{ active: tab === 'config' }" @click="tab = 'config'" v-if="can('system:config')">⚙️ 系统配置</button>
      <button class="tab" :class="{ active: tab === 'optimizer' }" @click="loadOptimizer(); tab = 'optimizer'" v-if="can('optimizer:manage')">📈 优化建议</button>
      <button class="tab" :class="{ active: tab === 'rewrite' }" @click="loadRewrite(); tab = 'rewrite'" v-if="can('optimizer:manage')">🔧 Query改写</button>
      <button class="tab" :class="{ active: tab === 'evidence' }" @click="loadEvidenceGaps(); tab = 'evidence'" v-if="can('evidence:manage')">📝 证据补全</button>
      <button class="tab" :class="{ active: tab === 'cost' }" @click="loadCostReport(); tab = 'cost'" v-if="can('metric:read')">💰 成本</button>
      <button class="tab" :class="{ active: tab === 'quality' }" @click="loadQuality(); tab = 'quality'" v-if="can('system:config')">📚 知识库质量</button>
      <button class="tab" :class="{ active: tab === 'eval' }" @click="loadEval(); tab = 'eval'" v-if="can('metric:read')">📊 评测趋势</button>
      <button class="tab" :class="{ active: tab === 'abtest' }" @click="loadABTest(); tab = 'abtest'" v-if="can('system:config')">🧪 A/B测试</button>
      <button class="tab" :class="{ active: tab === 'tune' }" @click="loadTuneReport(); tab = 'tune'" v-if="can('system:config')">🎯 检索调参</button>
      <button class="tab" :class="{ active: tab === 'users' }" @click="loadUsers(); tab = 'users'" v-if="can('user:manage')">👥 用户管理</button>
      <button class="tab" :class="{ active: tab === 'persona' }" @click="loadPersonas(); tab = 'persona'" v-if="can('system:config')">🧩 Persona</button>
      <button class="tab" :class="{ active: tab === 'backup' }" @click="loadBackups(); tab = 'backup'" v-if="can('system:config')">💾 备份恢复</button>
      <button class="tab" :class="{ active: tab === 'terms' }" @click="loadTerms(); tab = 'terms'" v-if="can('system:config')">📖 词表管理</button>
      <button class="tab" :class="{ active: tab === 'srules' }" @click="loadSrules(); tab = 'srules'" v-if="can('system:config')">🏷️ 语义规则</button>
      <button class="tab" :class="{ active: tab === 'prompt' }" @click="loadPrompt(); tab = 'prompt'" v-if="can('system:config')">📝 Prompt模板</button>
      <button class="tab" :class="{ active: tab === 'memory' }" @click="loadMemories(); tab = 'memory'" v-if="can('system:config')">🧠 记忆</button>
    </div>

    <!-- 反馈管理 -->
    <div class="card" v-show="tab === 'persona'">
      <div class="card-header">
        <h3 class="card-title">🧩 AI 助手配置</h3>
        <button class="btn btn-ghost btn-sm" @click="loadPersonas">🔄 刷新</button>
      </div>
      <p class="hint" style="margin-top:0;line-height:1.7">
        <b>在这里调整每个 AI 助手的「角色设定」，保存后立即生效，无需改代码重新部署。</b><br/>
        可调：① <b>角色指令</b>——告诉 AI 怎么回答（如「回答简洁、突出安全风险」）；② <b>可用工具</b>——它能查哪些资料（运维规程/知识图谱/历史案例/操作票）；③ <b>参数</b>——思考几轮、回答风格、输出格式。<br/>
        操作：选一个助手 → 改设定 → 保存。勾「启用」才生效；删除则恢复出厂默认。
      </p>
      <div style="margin:6px 0 10px"><span class="muted">内置助手（点对应名即可调整）：</span>
        <span v-for="c in personas.codePersonas" :key="c" class="badge badge-neutral" style="margin:0 4px;cursor:pointer" @click="editPersona({name:c,systemPrompt:'',allowedTools:'',maxIter:null,temperature:null,maxTokens:null,outputFormat:'',enabled:true})">{{ personaLabel(c) }}</span>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:8px 0">
        <input class="input" v-model="personaForm.name" placeholder="助手名：qa / diagnose / alert" style="flex:1;min-width:180px" />
        <label class="ws-toggle" title="AI 最多自主思考几轮（默认 6）">思考轮数<input class="input" v-model.number="personaForm.maxIter" type="number" placeholder="6" style="width:70px" /></label>
        <label class="ws-toggle" title="回答创意度 0~1，越高越发散（默认 0.2）">风格<input class="input" v-model.number="personaForm.temperature" type="number" step="0.1" placeholder="0.2" style="width:64px" /></label>
        <select class="select" v-model="personaForm.outputFormat" style="width:auto"><option value="">默认格式</option><option value="text">自然语言</option><option value="json">结构化 JSON</option></select>
        <select class="select" v-model="personaForm.fallbackKey" style="width:auto" title="仅自定义 persona(code无)需选 fallback 降级映射；覆盖 code 的无需选"><option value="">fallback(覆盖code无需)</option><option value="qa">降级走qa</option><option value="diagnose">降级走diagnose</option><option value="alert">降级走alert</option><option value="none">无降级</option></select>
        <label class="ws-toggle"><input type="checkbox" v-model="personaForm.enabled" /> 启用</label>
        <button class="btn btn-primary btn-sm" @click="savePersona">💾 保存</button>
      </div>
      <textarea class="input edit-area" v-model="personaForm.systemPrompt" placeholder="角色指令：告诉这个 AI 助手它是谁、怎么回答。例如「你是电网运维专家，回答要简洁专业，突出安全风险，分点说明」（留空 = 用内置默认指令）" rows="3" style="margin:6px 0"></textarea>
      <input class="input" v-model="personaForm.allowedTools" placeholder='可用工具（JSON 数组，留空=用内置）。如 [&quot;search_regulation&quot;,&quot;query_equipment_graph&quot;,&quot;search_similar_case&quot;]' style="margin:6px 0" />
      <div style="overflow-x:auto;margin-top:8px">
        <div class="muted" style="margin-bottom:4px;font-size:12px">已保存的自定义配置（启用 = 覆盖内置；删除 = 恢复内置）：</div>
        <table class="tbl">
          <thead><tr><th>助手</th><th>状态</th><th>角色指令(摘要)</th><th>工具</th><th>参数</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="p in personas.configs" :key="p.id">
              <td>{{ personaLabel(p.name) }}</td>
              <td><span class="badge" :class="p.enabled ? 'badge-success' : 'badge-neutral'">{{ p.enabled ? '✅ 启用' : '⏸ 停用' }}</span></td>
              <td class="muted" style="max-width:240px">{{ (p.systemPrompt || '').slice(0, 60) }}</td>
              <td class="muted">{{ p.allowedTools || '内置' }}</td>
              <td class="muted">{{ p.maxIter || '默认' }}轮 / 风格{{ p.temperature ?? '默认' }} / {{ p.outputFormat || '默认' }}</td>
              <td><button class="btn btn-ghost btn-sm" @click="testPersona(p.name)">🧪 测试</button> <button class="btn btn-ghost btn-sm" @click="editPersona(p)">编辑</button> <button class="btn btn-danger btn-sm" @click="removePersona(p.name)">删除</button></td>
            </tr>
            <tr v-if="!personas.configs.length"><td colspan="6" class="empty">还没有自定义配置（全部用内置默认）。点上方内置助手名，或直接改下方表单试试 →</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" v-show="tab === 'users'">
      <div class="card-header">
        <h3 class="card-title">👥 用户管理</h3>
        <button class="btn btn-ghost btn-sm" @click="loadUsers">🔄 刷新</button>
      </div>
      <p class="hint" style="margin-top:0;line-height:1.7">
        <b>角色</b>：admin（全权）/ editor（文档全权+问答+图谱编辑）/ operator（问答+读文档）/ auditor（全只读）。<br/>
        <b>部门</b>：用于文档级 ACL——文档可限定某部门可见，跨部门互不可见（空=公开）。
      </p>
      <div style="overflow-x:auto;margin-top:8px">
        <table class="tbl">
          <thead><tr><th>用户</th><th>角色</th><th>部门</th><th>租户</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="u in users.list" :key="u.userId">
              <td>{{ u.username }}</td>
              <td>
                <select class="select" v-model="u.role" style="width:auto">
                  <option value="admin">admin</option>
                  <option value="editor">editor</option>
                  <option value="operator">operator</option>
                  <option value="auditor">auditor</option>
                </select>
              </td>
              <td><input class="input" v-model="u.dept" placeholder="如:调度/检修" style="width:120px" /></td>
              <td class="muted">{{ u.tenantId }}</td>
              <td><span class="badge" :class="u.status === 'inactive' ? 'badge-danger' : 'badge-success'">{{ u.status === 'inactive' ? '已禁用' : '正常' }}</span></td>
              <td class="muted">{{ u.createdAt }}</td>
              <td style="white-space:nowrap">
                <button class="btn btn-primary btn-sm" @click="saveUser(u)">💾</button>
                <button class="btn btn-ghost btn-sm" @click="toggleUserStatus(u)" :title="u.status === 'inactive' ? '启用' : '禁用'">{{ u.status === 'inactive' ? '启用' : '禁用' }}</button>
                <button class="btn btn-ghost btn-sm" @click="resetUserPwd(u)">改密</button>
                <button class="btn btn-danger btn-sm" @click="removeUser(u)">删除</button>
              </td>
            </tr>
            <tr v-if="!users.list || !users.list.length"><td colspan="7" class="empty">暂无用户</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" v-show="tab === 'feedback'">
      <div class="card-header">
        <h3 class="card-title">坏 case 看板 <span class="badge badge-neutral">{{ feedbacks.total }}</span></h3>
        <div class="row">
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === 'dislike' }" @click="loadFeedbacks('dislike')">只看👎</button>
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === 'like' }" @click="loadFeedbacks('like')">只看👍</button>
          <button class="btn btn-ghost btn-sm" :class="{ 'btn-primary': fbFilter === '' }" @click="loadFeedbacks('')">全部</button>
        </div>
      </div>
      <p class="hint" style="margin-top:0">dislike 自动异步跑 LLM-judge 打质量分 + 检索质量评估；确认坏 case 后「标为 golden」→ 自动写入 golden 集 → CI 门禁永久覆盖。</p>
      <!-- 检索→回答一致性矩阵 -->
      <div v-if="fbStats?.consistencyMatrix" style="margin-bottom:10px; padding:10px; background:var(--surface-2); border-radius:8px; font-size:12px">
        <strong style="font-size:13px">📊 检索→回答 一致性矩阵</strong>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:6px">
          <div class="badge badge-success" style="justify-content:center">检索好 + 回答好 ✅ {{ fbStats.consistencyMatrix.retrieval_good_answer_good }}</div>
          <div class="badge badge-warning" style="justify-content:center">检索好 + 回答差 🔧 {{ fbStats.consistencyMatrix.retrieval_good_answer_bad }}</div>
          <div class="badge badge-danger" style="justify-content:center">检索差 + 回答好 ⚠️ 编造 {{ fbStats.consistencyMatrix.retrieval_poor_answer_good }}</div>
          <div class="badge badge-danger" style="justify-content:center">检索差 + 回答差 ❌ 根因 {{ fbStats.consistencyMatrix.retrieval_poor_answer_bad }}</div>
        </div>
        <div v-if="fbStats.consistencyMatrix.retrieval_poor_answer_good_queries?.length" style="margin-top:6px; color:var(--danger)">
          ⚠️ 疑似 LLM 编造 case：<span v-for="q in fbStats.consistencyMatrix.retrieval_poor_answer_good_queries" :key="q" class="chip" style="color:var(--danger)">{{ q }}</span>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>问题</th><th>反馈</th><th>检索质量</th><th>judge幻觉</th><th>理由</th><th>用户</th><th>时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="f in feedbacks.list" :key="f.id">
              <td style="max-width:220px">{{ f.query }}</td>
              <td>{{ f.feedback === 'like' ? '👍' : '👎' }}</td>
              <td><span :class="retrievalBadge(f.retrievalQuality)">{{ retrievalLabel(f.retrievalQuality) }}</span></td>
              <td><span :class="judgeBadge(f.judgeHalluc)">{{ f.judgeHalluc != null ? (f.judgeHalluc * 100).toFixed(0) + '%' : '待评' }}</span></td>
              <td class="muted" style="max-width:160px">{{ f.reason || '—' }}</td>
              <td>{{ f.username || '—' }}</td>
              <td class="muted">{{ f.createdAt }}</td>
              <td><button v-if="can('feedback:manage')" class="btn btn-link btn-sm" @click="markGolden(f)">标为 golden</button></td>
            </tr>
            <tr v-if="!feedbacks.list.length"><td colspan="8" class="empty">暂无反馈</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 操作日志 -->
    <div class="card" v-show="tab === 'log'">
      <div class="card-header">
        <h3 class="card-title">操作日志 <span class="badge badge-neutral">{{ logs.total }}</span></h3>
        <div v-if="archiveStat" class="row" style="gap:10px;align-items:center">
          <span class="hint" style="margin:0">归档：共{{ archiveStat.total }}条 · 最早{{ archiveStat.oldest || '-' }} · 超期待归档<b style="color:var(--danger)">{{ archiveStat.pendingArchive }}</b>（保留{{ archiveStat.retentionDays }}天，自动每日）</span>
          <button class="btn btn-ghost btn-sm" @click="doArchiveLogs">📦 立即归档</button>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>用户</th><th>类型</th><th>内容</th><th>时间</th></tr></thead>
          <tbody><tr v-for="l in logs.list" :key="l.id"><td>{{ l.operateUser }}</td><td><span class="badge badge-neutral">{{ l.operateType }}</span></td><td>{{ l.content }}</td><td class="muted">{{ l.operateTime }}</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- 告警（Grafana alerting → webhook 落库） -->
    <div class="card" v-show="tab === 'alert'">
      <div class="card-header">
        <h3 class="card-title">🚨 告警 <span class="badge badge-neutral">{{ alerts.total }}</span></h3>
        <button class="btn btn-ghost btn-sm" @click="loadAlerts">🔄 刷新</button>
      </div>
      <p class="hint" style="margin-top:0">Grafana 告警规则（组件下线/降级激增/幻觉率/安全命中）触发后经 webhook 回调落库，在此实时可见。规则在 Grafana「Alerting」页可查可改。</p>
      <div style="overflow-x:auto">
        <table class="tbl">
          <thead><tr><th>级别</th><th>告警</th><th>来源</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="a in alerts.list" :key="a.id">
              <td><span class="badge" :class="sevBadge(a.content)">{{ sevOf(a.content) }}</span></td>
              <td>{{ a.content.replace(/^\[(info|warning|critical)\]\s*/, '') }}</td>
              <td class="muted">{{ a.operateUser }}</td>
              <td class="muted">{{ a.operateTime }}</td>
            </tr>
            <tr v-if="!alerts.list.length"><td colspan="4" class="empty">暂无告警（系统正常）</td></tr>
          </tbody>
        </table>
      </div>

      <!-- S3 告警自动处置（ALERT_PERSONA 自动调工具分析） -->
      <div class="disposal-section" style="margin-top:18px;border-top:1px dashed var(--border);padding-top:12px">
        <div class="card-header" style="margin-bottom:8px">
          <h4 class="card-title" style="font-size:14px;margin:0">🤖 自动处置 <span class="badge badge-neutral">{{ disposals.total }}</span></h4>
          <button class="btn btn-ghost btn-sm" @click="loadDisposals">🔄 刷新</button>
        </div>
        <p class="hint" style="margin:0 0 10px">手动触发或 Grafana 告警进来后，AI 自动调工具(规程/图谱/案例/操作票)分析 → 生成诊断/处置/操作票草案。</p>
        <template v-if="can('alert:manage')">
          <div class="disp-form" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <select class="select" v-model="dispForm.severity" style="width:auto">
              <option value="info">info</option><option value="warning">warning</option><option value="critical">critical</option>
            </select>
            <input class="input" v-model="dispForm.title" placeholder="告警标题（如：主变油温高）" style="flex:1;min-width:160px" />
            <button class="btn btn-primary btn-sm" @click="doDispose">🤖 触发处置</button>
          </div>
          <input class="input" v-model="dispForm.summary" placeholder="告警详情描述（可选，英文/中文均可）" style="margin:6px 0" />
        </template>
        <div style="overflow-x:auto;margin-top:8px">
          <table class="tbl">
            <thead><tr><th>状态</th><th>告警</th><th>处置概述</th><th>操作票</th><th>来源</th><th>时间</th><th v-if="can('alert:manage')">操作</th></tr></thead>
            <tbody>
              <tr v-for="d in disposals.list" :key="d.id">
                <td><span class="badge" :class="dispStatusBadge(d.status)">{{ dispStatusLabel(d.status) }}</span></td>
                <td><span class="badge" :class="{ 'badge-danger': d.severity==='critical', 'badge-success': d.severity==='info', 'badge-neutral': d.severity==='warning' }">{{ d.severity }}</span> {{ d.title || '(无标题)' }}</td>
                <td class="muted" style="max-width:260px">{{ (d.handling || parseDispDiag(d.diagnosis)?.summary || '—').slice(0, 90) }}</td>
                <td class="muted">{{ (() => { const t = parseDispDiag(d.ticketDraft); return t && t.steps ? `${t.device || ''}·${t.steps.length}步` : '—' })() }}</td>
                <td class="muted">{{ d.source }}</td>
                <td class="muted">{{ d.createdAt }}</td>
                <td style="white-space:nowrap" v-if="can('alert:manage')">
                  <button v-if="['proposed','disposed'].includes(d.status)" class="btn btn-primary btn-sm" @click="doConfirmDisp(d)">确认</button>
                  <button v-if="['proposed','disposed','confirmed'].includes(d.status)" class="btn btn-ghost btn-sm" @click="doRejectDisp(d)">驳回</button>
                  <button v-if="d.status==='confirmed'" class="btn btn-accent btn-sm" @click="doToTicket(d)">转两票</button>
                  <button v-if="!['ticketed','closed'].includes(d.status)" class="btn btn-ghost btn-sm" @click="doCloseDisp(d)">关闭</button>
                  <span v-if="d.ticketId" class="muted" style="margin-left:44px">票{{ String(d.ticketId).slice(-6) }}</span>
                </td>
              </tr>
              <tr v-if="!disposals.list.length"><td colspan="7" class="empty">暂无处置记录（点「触发处置」试试）</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 系统配置 -->
    <div v-show="tab === 'config'">
      <!-- provider 连通性 -->
      <div class="card">
        <div class="card-header">
          <h3 class="card-title">Provider 连通性</h3>
          <button class="btn btn-primary btn-sm" :disabled="healthLoading" @click="loadHealth">{{ healthLoading ? '探测中...' : '🧪 测试连通' }}</button>
        </div>
        <p class="hint" style="margin-top:0">主动 ping LLM/Embedding provider，抓欠费/配额/key 失效/网络问题（消耗少量 token）。</p>
        <div class="config-grid" v-if="health">
          <div class="stat stat-accent">
            <div class="stat-val" :style="{ color: health.llm.status === 'ok' ? 'var(--success)' : 'var(--danger)' }">{{ health.llm.status === 'ok' ? '正常' : '异常' }}</div>
            <div class="stat-lbl">LLM：{{ health.llm.provider || '—' }}<br><span class="muted" style="font-size:11px">{{ health.llm.detail || health.llm.error || '' }}</span></div>
          </div>
          <div class="stat stat-accent">
            <div class="stat-val" :style="{ color: health.embedding.status === 'ok' ? 'var(--success)' : 'var(--danger)' }">{{ health.embedding.status === 'ok' ? '正常' : '异常' }}</div>
            <div class="stat-lbl">Embedding：{{ health.embedding.provider || '—' }}<br><span class="muted" style="font-size:11px">{{ health.embedding.detail || health.embedding.error || '' }}</span></div>
          </div>
        </div>
        <div v-else class="hint">点「测试连通」探测当前 provider（结果不会自动刷新）。</div>
      </div>

      <div class="config-grid">
        <div class="card">
          <div class="card-header"><h3 class="card-title">Milvus 索引配置</h3><span v-if="configLoaded" class="badge badge-success">已读取线上值</span></div>
          <div class="field"><label class="field-label">indexType</label><input class="input" v-model="milvus.indexType" /></div>
          <div class="field"><label class="field-label">M（HNSW 建索引参数）</label><input class="input" v-model="milvus.M" /></div>
          <div class="field"><label class="field-label">efConstruction（建索引参数）</label><input class="input" v-model="milvus.efConstruction" /></div>
          <div class="field"><label class="field-label">ef（查询参数 · 运行时即时生效）</label><input class="input" v-model="milvus.ef" /><span class="hint">↑ef 召回↑延迟↑，可实时调</span></div>
          <button class="btn btn-primary" @click="saveMilvus">保存</button>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">模型参数配置</h3><span v-if="configLoaded" class="badge badge-success">已读取线上值</span></div>
          <div class="field"><label class="field-label">modelType</label><input class="input" v-model="model.modelType" /></div>
          <div class="field"><label class="field-label">temperature（主答案 · 运行时即时生效）</label><input class="input" v-model="model.temperature" /></div>
          <div class="field"><label class="field-label">max_tokens</label><input class="input" v-model="model.max_tokens" /></div>
          <button class="btn btn-primary" @click="saveModel">保存</button>
        </div>
      </div>

      <!-- BM25 重建 -->
      <div class="card">
        <div class="card-header"><h3 class="card-title">BM25 索引</h3><button class="btn btn-ghost btn-sm" :disabled="bm25Loading" @click="handleRebuildBm25">{{ bm25Loading ? '重建中...' : '🔄 全量重建' }}</button></div>
        <p class="hint" style="margin-top:0">新文档默认增量进内存；进程重启/异常后点此兜底全量重建。</p>
      </div>
    </div><!-- /config tab -->

      <!-- 优化建议 -->
      <div class="card" v-show="tab === 'optimizer'">
        <div class="card-header">
          <h3 class="card-title">📈 反馈驱动优化建议</h3>
          <div style="display:flex; gap:6px">
            <button class="btn btn-primary btn-sm" :disabled="optLoading" @click="generateOptimizer">{{ optLoading ? '分析中…' : '🔄 重新分析' }}</button>
            <button class="btn btn-ghost btn-sm" :disabled="tuneLoading" @click="tuneCache">{{ tuneLoading ? '调优中…' : '🎚️ 缓存调优' }}</button>
          </div>
        </div>
        <p class="hint" style="margin-top:0">基于用户反馈自动分析知识盲区、缓存策略和检索质量，生成可执行优化建议。</p>
        <div v-if="optimizer" style="margin-top:8px">
          <div class="opt-meta" style="margin-bottom:8px; font-size:12px; color:var(--text-muted)">
            分析时间：{{ optimizer.generatedAt || '未生成' }} · 总 dislike {{ optimizer.totalDislike }} · 近7天 {{ optimizer.recentDislike }} · 缓存命中率 {{ optimizer.cacheHitRate != null ? (optimizer.cacheHitRate * 100).toFixed(0) + '%' : '—' }}
          </div>
          <div v-if="tuneResult" class="opt-card" style="margin-bottom:10px; border-left-color:var(--accent)">
            <div class="opt-header"><span class="badge badge-info">缓存调优</span><strong class="opt-title">已应用黑名单 {{ tuneResult.appliedBlacklist }} 条</strong></div>
            <div v-if="tuneResult.blacklisted?.length" class="opt-detail">🚫 高频坏答案禁缓存：{{ tuneResult.blacklisted.join('、') }}</div>
            <div v-if="tuneResult.extended?.length" class="opt-detail">⏱️ TTL 延长候选：{{ tuneResult.extended.map(e => e.query).join('、') }}</div>
          </div>
          <div class="opt-card" style="margin-bottom:10px">
            <div class="opt-header"><span class="badge badge-danger">🚫 黑名单</span><strong class="opt-title">缓存黑名单管理</strong></div>
            <div class="opt-detail" style="margin-top:4px">黑名单内的 query 强制跳过所有缓存层(redis+mysql+semantic)，每次重走 LLM。来源：dislike 累计达标自动入库 + 此处手动添加。</div>
            <div style="display:flex; gap:6px; margin:8px 0">
              <input v-model="blInput" placeholder="输入要拉黑的 query，回车加入" @keyup.enter="addBlacklist" style="flex:1; padding:5px 8px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--text)" />
              <button class="btn btn-primary btn-sm" @click="addBlacklist">加入</button>
              <button class="btn btn-ghost btn-sm" @click="loadBlacklist">刷新</button>
            </div>
            <div v-if="blacklist.length">
              <span v-for="q in blacklist" :key="q" class="badge badge-danger" style="margin:3px; cursor:pointer; display:inline-block" @click="removeBlacklist(q)" title="点击移除">{{ q }} ✕</span>
            </div>
            <div v-else class="hint" style="font-size:12px; margin-top:4px">黑名单为空</div>
          </div>
          <div v-if="!optimizer.suggestions?.length" class="empty">暂无优化建议（数据积累中）</div>
          <div class="opt-card" v-for="(s, i) in optimizer.suggestions" :key="i" :class="'sev-' + s.severity">
            <div class="opt-header">
              <span class="badge" :class="severityBadge(s.severity)">{{ {high:'高优',medium:'中优',low:'低优'}[s.severity] || s.severity }}</span>
              <span class="opt-type">{{ typeLabel(s.type) }}</span>
              <strong class="opt-title">{{ s.title }}</strong>
            </div>
            <div class="opt-detail">{{ s.detail }}</div>
            <div class="opt-actions" v-if="s.actions?.length">
              <div class="opt-action" v-for="(a, j) in s.actions" :key="j">{{ a }}</div>
            </div>
          </div>
        </div>
        <div v-else class="hint" style="margin-top:12px">点「重新分析」生成优化建议报告。</div>
      </div>

      <!-- Query 改写质量评估 -->
      <div class="card" v-show="tab === 'rewrite'">
        <div class="card-header">
          <h3 class="card-title">🔧 Query 改写质量评估</h3>
          <select v-model="rwPeriod" @change="loadRewrite" class="btn btn-ghost btn-sm">
            <option value="today">今天</option><option value="7d">近7天</option>
          </select>
        </div>
        <div v-if="rwStats" style="display:flex; gap:16px; flex-wrap:wrap; margin:8px 0">
          <div class="stat"><small>总改写</small><b> {{ rwStats.total }} </b></div>
          <div class="stat"><small>采纳率</small><b> {{ (rwStats.adoptedRate * 100).toFixed(0) }}% </b></div>
          <div class="stat"><small>否决率</small><b> {{ ((1 - rwStats.adoptedRate) * 100).toFixed(0) }}% </b></div>
          <div class="stat"><small>缓存命中</small><b> {{ (rwStats.cacheHitRate * 100).toFixed(0) }}% </b></div>
        </div>
        <div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 0">
          <div ref="rwPieEl" style="width:48%; height:280px"></div>
          <div ref="rwScatterEl" style="width:48%; height:280px"></div>
        </div>
        <div ref="rwTrendEl" style="width:100%; height:260px; margin-bottom:12px"></div>
        <div class="card-header"><h4 class="card-title">改写明细</h4>
          <button class="btn btn-ghost btn-sm" @click="loadRewriteEvents">🔄 刷新</button>
        </div>
        <table class="tbl" v-if="rwEvents.length">
          <thead><tr><th>时间</th><th>策略</th><th>原 query</th><th>改写</th><th>采纳</th><th>分数(原→新)</th></tr></thead>
          <tbody>
            <tr v-for="(e, i) in rwEvents" :key="i">
              <td>{{ e.ts }}</td><td>{{ e.strategy }}</td>
              <td>{{ (e.original || '').slice(0, 30) }}</td><td>{{ (e.rewritten || '').slice(0, 30) }}</td>
              <td><span :class="e.improved ? 'badge badge-success' : 'badge badge-neutral'">{{ e.improved ? '✓' : '✗' }}</span></td>
              <td>{{ e.origScore != null ? e.origScore.toFixed(2) : '-' }} → {{ e.newScore != null ? e.newScore.toFixed(2) : '-' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">暂无改写事件（先在 Chat 问几个口语化问题积累数据）</div>
      </div>

      <!-- 证据补全 -->
      <div class="card" v-show="tab === 'evidence'">
        <div class="card-header">
          <h3 class="card-title">📝 证据补全（medium/refused 人工兜底回流）</h3>
          <button class="btn btn-ghost btn-sm" @click="retagFaq" title="补全旧FAQ的设备标签（从答案提取）">🏷️ 补设备标签</button>
          <select v-model="egFilter" @change="loadEvidenceGaps" class="btn btn-ghost btn-sm">
            <option value="">全部</option><option value="pending">待处理</option>
            <option value="ai_drafted">已续写</option><option value="synced">已同步</option>
            <option value="ignored">已忽略</option>
          </select>
        </div>
        <div v-if="!egList.length" class="empty">暂无证据补全记录（medium/refused 自动收集 + Chat 上报）</div>
        <div class="opt-card" v-for="g in egList" :key="g.id" :class="g.confidence==='refused'?'sev-high':'sev-medium'">
          <div class="opt-header">
            <select v-model="g.confidence" @change="saveConf(g)" class="badge" :class="confBadgeClass(g.confidence)" title="点击标注 confidence">
              <option value="sufficient">证据充足</option>
              <option value="medium">证据有限</option>
              <option value="refused">证据不足</option>
              <option value="outdated">证据过时</option>
              <option value="conflict">证据冲突</option>
              <option value="verify">需人工核实</option>
              <option value="expired">已过保</option>
            </select>
            <span class="badge" :class="{pending:'badge-info',ai_drafted:'badge-warning',synced:'badge-success',ignored:'badge-neutral'}[g.status]">{{ {pending:'待处理',ai_drafted:'已续写',synced:'已同步',ignored:'已忽略'}[g.status] }}</span>
            <strong class="opt-title">{{ g.query }}</strong>
          </div>
          <div class="opt-detail">原答案：{{ (g.originalAnswer || '').slice(0, 80) }}</div>
          <AgentTrace :steps="g.agentSteps" />
          <div v-if="g.aiDraft" class="opt-detail" style="margin-top:6px">
            <b>AI草稿{{ g.deepStreaming ? '（生成中…）' : '（点击下方文本可直接编辑）' }}：</b>
            <div v-if="g._editing" style="margin-top:4px">
              <textarea v-model="g.aiDraft" rows="8" class="input" style="width:100%;font-size:12px"></textarea>
              <div style="margin-top:4px"><button class="btn btn-success btn-sm" @click="saveAiDraftInline(g)">💾 保存草稿</button> <button class="btn btn-ghost btn-sm" @click="g._editing = false">取消</button></div>
            </div>
            <div v-else @click="!g.deepStreaming && (g._editing = true)" style="cursor:text;white-space:pre-wrap;margin-top:4px;padding:8px 10px;background:var(--surface);border-radius:6px;min-height:50px;max-height:240px;overflow-y:auto;border:1px dashed var(--border)" title="点击编辑">{{ g.aiDraft }}</div>
          </div>
          <div v-if="g.status==='synced'" class="opt-detail" style="color:var(--success)">最终：{{ (g.finalAnswer || '').slice(0, 100) }}</div>
          <div style="margin-top:6px">
            <button v-if="g.status==='pending'" class="btn btn-primary btn-sm" @click="egDraft(g)">🤖 AI续写</button>
            <button v-if="g.status==='pending' || g.status==='ai_drafted'" class="btn btn-ghost btn-sm" @click="egDeepDraft(g)" title="用 Agent 引擎多轮调工具交叉验证，比续写更深（复用深度思考同款引擎）">🧠 深度补全</button>
            <button v-if="g.status!=='synced' && g.status!=='ignored'" class="btn btn-ghost btn-sm" @click="egEdit(g)">✏️ 人工编辑</button>
            <button v-if="g.status==='ai_drafted' || g.status==='confirmed'" class="btn btn-success btn-sm" @click="egConfirm(g)">✓ 确认同步</button>
            <button v-if="g.status!=='synced' && g.status!=='ignored'" class="btn btn-ghost btn-sm" @click="egIgnore(g)">忽略</button>
          </div>
        </div>
      </div>

      <!-- 证据补全：人工编辑弹窗 -->
      <div class="modal-overlay" v-if="egEditing" @click.self="egEditing = null">
        <div class="modal" style="max-width:680px; height:auto; max-height:88vh; overflow-y:auto">
          <div class="modal-head">
            ✏️ 人工编辑最终答案
            <a @click="egEditing = null" style="cursor:pointer">✕</a>
          </div>
          <div style="padding:14px 18px; display:flex; flex-direction:column; gap:12px">
            <div>
              <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">问题</div>
              <div style="font-weight:600">{{ egEditing.query }}</div>
            </div>
            <div v-if="egEditing.originalAnswer">
              <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">原答案（{{ egEditing.confidence === 'refused' ? '证据不足' : '证据有限' }}）</div>
              <div style="background:var(--surface-2);padding:8px 10px;border-radius:6px;font-size:13px;max-height:80px;overflow-y:auto;color:var(--text-muted)">{{ egEditing.originalAnswer }}</div>
            </div>
            <div v-if="egEditing.aiDraft">
              <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">AI 草稿（参考，可复制）</div>
              <div style="background:var(--surface-2);padding:8px 10px;border-radius:6px;font-size:13px;max-height:100px;overflow-y:auto;border-left:3px solid var(--accent)">{{ egEditing.aiDraft }}</div>
            </div>
            <div>
              <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">最终答案（编辑后点「保存」，再「确认同步」入库）</div>
              <textarea v-model="egEditText" rows="8" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px;font-family:inherit;resize:vertical" placeholder="编辑最终答案（保存后状态变为「已确认」，再点确认同步入库）"></textarea>
            </div>
          </div>
          <div style="padding:10px 18px;display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--border)">
            <button class="btn btn-ghost btn-sm" @click="egEditing = null">取消</button>
            <button class="btn btn-primary btn-sm" :disabled="egSaving" @click="egSave">{{ egSaving ? '保存中…' : '💾 保存（待同步）' }}</button>
          </div>
        </div>
      </div>

      <!-- 成本 -->
      <div class="card" v-show="tab === 'cost'">
        <div class="card-header"><h3 class="card-title">💰 LLM 成本报告</h3><button class="btn btn-ghost btn-sm" @click="loadCostReport">🔄 刷新</button></div>
        <div v-if="costReport">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ costReport.todayTokens?.toLocaleString() || 0 }}</div><div class="stat-lbl">今日 Token</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ costReport.monthTokens?.toLocaleString() || 0 }}</div><div class="stat-lbl">本月 Token</div></div>
            <div class="stat stat-accent"><div class="stat-val">¥{{ costReport.todayByModel?.reduce((s,m)=>s+m.cost,0).toFixed(4) || '0' }}</div><div class="stat-lbl">今日费用</div></div>
          </div>
          <div v-if="costReport.todayByModel?.length" class="src-head">今日各模型消耗</div>
          <div class="cause" v-for="m in costReport.todayByModel" :key="m.model" style="justify-content:space-between">
            <span><b>{{ m.model }}</b></span><span>{{ m.tokens?.toLocaleString() }} tokens · ¥{{ m.cost?.toFixed(4) }}</span>
          </div>
          <div v-if="costReport.topUsers?.length" class="src-head" style="margin-top:10px">本月用户排行 Top-10</div>
          <div class="cause" v-for="(u, i) in costReport.topUsers" :key="i" style="justify-content:space-between">
            <span>{{ i+1 }}. {{ u.username }}</span><span>{{ u.tokens?.toLocaleString() }} tokens</span>
          </div>
          <div class="hint" style="margin-top:8px">用户配额：{{ costReport.userQuota?.toLocaleString() }} · 租户配额：{{ costReport.tenantQuota?.toLocaleString() }}</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- 知识库质量 -->
      <div class="card" v-show="tab === 'quality'">
        <div class="card-header"><h3 class="card-title">📚 知识库质量</h3><button class="btn btn-ghost btn-sm" @click="loadQuality">🔄 刷新</button></div>
        <div v-if="quality">
          <div class="stats-grid" style="margin-bottom:14px;grid-template-columns:repeat(5,1fr)">
            <div class="stat stat-accent"><div class="stat-val" :style="{ color: gradeColor(quality.overallGrade) }">{{ quality.overallGrade || '?' }}</div><div class="stat-lbl">综合评级</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ ((quality.qualityScore||0)*100).toFixed(0) }}%</div><div class="stat-lbl">分块质量</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ ((quality.coverageRate||0)*100).toFixed(0) }}%</div><div class="stat-lbl">向量化覆盖</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ quality.docCount }}</div><div class="stat-lbl">文档总数</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ quality.chunkCount }}</div><div class="stat-lbl">分块总数</div></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">
            <div>
              <div class="src-head">文档类型分布</div>
              <div ref="qualityPieEl" style="height:260px"></div>
            </div>
            <div>
              <div class="src-head">分块质量</div>
              <div ref="qualityPieScoreEl" style="height:260px"></div>
            </div>
            <div>
              <div class="src-head">向量化覆盖</div>
              <div ref="qualityPieCovEl" style="height:260px"></div>
            </div>
          </div>
          <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:8px">
            <span class="hint">重复率 {{ ((quality.dupRate||0)*100).toFixed(1) }}%</span>
            <span class="hint">过短块 {{ quality.tooShortChunks }}</span>
            <span class="hint">过长块 {{ quality.tooLongChunks }}</span>
          </div>
          <div v-if="quality.gaps?.length" class="src-head" style="margin-top:8px;color:var(--warning)">⚠ 知识盲区</div>
          <div class="cause" v-for="g in quality.gaps" :key="g.term"><span>{{ g.suggestion }}</span></div>
          <div style="margin-top:16px">
            <div class="src-head">质量三维度对比（横向柱图 %）</div>
            <div ref="qualityBarEl" style="height:240px"></div>
          </div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- 评测趋势 -->
      <div class="card" v-show="tab === 'eval'">
        <div class="card-header"><h3 class="card-title">📊 检索质量评测趋势</h3><button class="btn btn-ghost btn-sm" @click="loadEval">🔄 刷新</button></div>
        <div v-if="evalTrend">
          <div class="stats-grid" style="margin-bottom:14px;grid-template-columns:repeat(4,1fr)">
            <div class="stat stat-accent ring-stat"><div class="ring" :style="{ background: `conic-gradient(var(--primary) 0 ${((evalTrend.latestOverall||0)*100).toFixed(1)}%, var(--surface-2) ${((evalTrend.latestOverall||0)*100).toFixed(1)}% 100%)` }"><div class="ring-hole"><div class="ring-val">{{ ((evalTrend.latestOverall||0)*100).toFixed(1) }}%</div><div class="ring-lbl">最新综合</div></div></div></div>
            <div class="stat stat-accent ring-stat"><div class="ring" :style="{ background: `conic-gradient(#6c5ce7 0 ${parseFloat(evalAvg())||0}%, var(--surface-2) ${parseFloat(evalAvg())||0}% 100%)` }"><div class="ring-hole"><div class="ring-val">{{ evalAvg() }}</div><div class="ring-lbl">区间均分</div></div></div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ evalTrend.trends?.length || 0 }}</div><div class="stat-lbl">采样天数</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ evalSamples() }}</div><div class="stat-lbl">采样总数</div></div>
          </div>
          <div class="src-head">近 {{ evalTrend.days }} 天质量趋势（综合 / 相关性 / 忠实度）</div>
          <div ref="evalLineEl" style="height:340px"></div>
          <div v-if="!evalTrend.trends?.length" class="empty" style="margin-top:8px">暂无评测数据（需用户问答触发采样跑 LLM Judge 后才有趋势）</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>

      <!-- A/B 测试 -->
      <div class="card" v-show="tab === 'abtest'">
        <div class="card-header"><h3 class="card-title">🧪 路由 A/B 测试</h3><button class="btn btn-ghost btn-sm" @click="loadABTest">🔄 刷新</button></div>
        <div v-if="abConfig">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ abConfig.enabled ? '✅ 开' : '❌ 关' }}</div><div class="stat-lbl">路由总开关</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ abConfig.abTestRatio < 1 ? ((1 - abConfig.abTestRatio) * 100).toFixed(0) + '%' : '全量' }}</div><div class="stat-lbl">B 组流量</div></div>
          </div>
          <div class="cause" style="justify-content:space-between"><span>Sparse 最大长度</span><span>{{ abConfig.sparseMaxLen }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>Dense 最小长度</span><span>{{ abConfig.denseMinLen }}</span></div>
          <div class="cause" style="justify-content:space-between"><span>最低置信度</span><span>{{ abConfig.minConfidence }}</span></div>
          <div class="hint" style="margin-top:8px">B 组走 hybrid 全链路，A 组走智能路由。对比两组延迟和检索质量。</div>
        </div>
        <div v-else class="hint" style="margin-top:8px">加载中...</div>
      </div>
      <!-- 检索调参（只建议模式） -->
      <div class="card" v-show="tab === 'tune'">
        <div class="card-header">
          <h3 class="card-title">🎯 检索参数调参建议</h3>
          <button class="btn btn-primary btn-sm" @click="runTuneScan" :disabled="tuneRunning">{{ tuneRunning ? '扫描中…' : '🔄 重新扫描' }}</button>
        </div>
        <p class="hint" style="margin-top:0">跑 golden 集扰动扫描 → 产出参数建议。<b>只建议不自动应用</b>，点「复制 .env 行」贴到 .env 重启生效。</p>
        <div v-if="tuneReport && tuneReport.baseline">
          <div class="stats-grid" style="margin-bottom:10px">
            <div class="stat stat-accent"><div class="stat-val">{{ tuneReport.baseline.recall?.toFixed(3) }}</div><div class="stat-lbl">baseline recall</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ tuneReport.baseline.mrr?.toFixed(3) }}</div><div class="stat-lbl">MRR</div></div>
            <div class="stat stat-accent"><div class="stat-val">{{ tuneReport.baseline.ndcg?.toFixed(3) }}</div><div class="stat-lbl">nDCG</div></div>
            <div class="stat"><div class="stat-val">{{ ((tuneReport.baseline.noResultRate || 0) * 100).toFixed(1) }}%</div><div class="stat-lbl">无结果率</div></div>
          </div>
          <div v-if="tuneReport.incomplete" class="cause" style="color:#e53935">⚠ {{ tuneReport.note }}</div>
          <h4 style="margin:12px 0 6px">💡 建议（{{ tuneReport.suggestions?.length || 0 }} 条）</h4>
          <div style="overflow-x:auto">
            <table class="tbl">
              <thead><tr><th>参数</th><th>当前</th><th>建议</th><th>recall 提升</th><th>置信度</th><th>理由</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="s in tuneReport.suggestions" :key="s.param">
                  <td><b>{{ s.param }}</b></td>
                  <td class="muted">{{ s.current }}</td>
                  <td><b style="color:var(--primary)">{{ s.suggested }}</b></td>
                  <td>+{{ (s.delta * 100).toFixed(1) }}%</td>
                  <td><span class="badge" :class="{'badge-success':s.confidence==='high','badge-warning':s.confidence==='medium','badge-danger':s.confidence==='low'}">{{ s.confidence }}</span></td>
                  <td class="muted" style="max-width:240px">{{ s.reason }}</td>
                  <td><button class="btn btn-ghost btn-sm" @click="copyEnv(s)">复制 .env 行</button></td>
                </tr>
                <tr v-if="!tuneReport.suggestions?.length"><td colspan="7" class="empty">暂无建议（提升均 &lt; margin，或样本不足）</td></tr>
              </tbody>
            </table>
          </div>
          <h4 style="margin:12px 0 6px">🔀 开关 A/B（关闭后 recall 变化）</h4>
          <div style="overflow-x:auto">
            <table class="tbl">
              <thead><tr><th>开关</th><th>关闭后 recall</th><th>Δ recall</th><th>结论</th></tr></thead>
              <tbody>
                <tr v-for="sw in tuneReport.switches" :key="sw.switch">
                  <td><b>{{ sw.switch }}</b></td>
                  <td>{{ sw.recall?.toFixed(3) }}</td>
                  <td :style="{color: sw.delta < 0 ? '#e53935' : ''}">{{ (sw.delta * 100).toFixed(1) }}%</td>
                  <td class="muted">{{ sw.delta < -0.02 ? '⚠ 该开关不应关闭' : '关闭影响小' }}</td>
                </tr>
                <tr v-if="!tuneReport.switches?.length"><td colspan="4" class="empty">无开关数据</td></tr>
              </tbody>
            </table>
          </div>
          <div class="hint" style="margin-top:8px">扫描 {{ tuneReport.evalCount }} 次评测 · 耗时 {{ tuneReport.duration }}s · {{ tuneReport.runAt }}</div>
        </div>
        <div v-else-if="tuneReport && tuneReport.error" class="cause" style="color:#e53935">⚠ {{ tuneReport.error }}</div>
        <div v-else class="hint" style="margin-top:8px">暂无报告，点「重新扫描」生成</div>
      </div>
      <!-- 三合一备份恢复（MySQL+Redis+Milvus） -->
      <div class="card" v-show="tab === 'backup'">
        <div class="card-header">
          <h3 class="card-title">💾 三合一备份恢复（MySQL+Redis+Milvus）</h3>
          <button class="btn btn-primary btn-sm" @click="doBackupAll" :disabled="backupLoading">{{ backupLoading ? '备份中…' : '🛡️ 一键全量备份' }}</button>
        </div>
        <p class="hint" style="margin-top:0;line-height:1.7">
          一键备份 <b>MySQL(元数据) + Redis(全量缓存/配置) + Milvus(向量)</b>，含 manifest 元信息包。<br/>
          ⚠ <b>一键恢复会全量覆盖三者</b>。系统每 <b>3 小时</b>自动全量备份一次（后台定时）。
        </p>
        <div style="overflow-x:auto;margin-top:8px">
          <table class="tbl">
            <thead><tr><th>备份时间</th><th>MySQL</th><th>Redis</th><th>Milvus</th><th>总大小</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="b in backups" :key="b.ts">
                <td>{{ b.createdAt }}<br/><span class="muted" style="font-size:11px">{{ b.ts }}</span></td>
                <td class="muted">{{ b.meta?.mysqlTables || 0 }}表/{{ b.meta?.mysqlRows || 0 }}行</td>
                <td class="muted">{{ b.meta?.redisKeys || 0 }} key</td>
                <td class="muted">{{ b.meta?.milvusVectors || 0 }} 向量</td>
                <td class="muted">{{ (b.totalSize / 1024).toFixed(1) }} KB</td>
                <td style="white-space:nowrap">
                  <button class="btn btn-primary btn-sm" @click="doRestoreAll(b.ts)">一键恢复</button>
                  <button class="btn btn-danger btn-sm" @click="doDeleteBackupAll(b.ts)">删除</button>
                </td>
              </tr>
              <tr v-if="!backups.length"><td colspan="6" class="empty">暂无备份（点「一键全量备份」生成；或等待 3h 自动备份）</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <!-- 词表管理 -->
      <div class="card" v-show="tab === 'terms'">
        <div class="card-header"><h3 class="card-title">📖 词表管理 <span class="badge badge-neutral">{{ termsList.length }}</span></h3></div>
        <p class="hint" style="margin-top:0">术语归一化词表（别名→标准词）。改后立即生效（清缓存），检索/问答 query 都会经过归一化。</p>
        <div class="row" style="gap:8px;margin:8px 0">
          <input class="input" v-model="termForm.alias" placeholder="别名/错别字（如：主变气）" style="flex:1" />
          <input class="input" v-model="termForm.standard" placeholder="标准词（如：主变压器）" style="flex:1" />
          <button class="btn btn-primary btn-sm" @click="doAddTerm">＋ 新增</button>
        </div>
        <div style="overflow-x:auto">
          <table class="tbl">
            <thead><tr><th>别名</th><th>标准词</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="t in termsList" :key="t.alias"><td>{{ t.alias }}</td><td>{{ t.standard }}</td><td><button class="btn btn-danger btn-sm" @click="doDeleteTerm(t.alias)">删除</button></td></tr>
              <tr v-if="!termsList.length"><td colspan="3" class="empty">暂无词条</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <!-- Prompt 模板管理 -->
      <div class="card" v-show="tab === 'prompt'">
        <div class="card-header">
          <h3 class="card-title">📝 System Prompt 模板</h3>
          <div class="row" style="gap:8px">
            <button class="btn btn-ghost btn-sm" @click="promptText = promptDefault; toast('已重置为默认（未保存）')">↩ 恢复默认</button>
            <button class="btn btn-primary btn-sm" @click="savePrompt">💾 保存覆盖</button>
          </div>
        </div>
        <p class="hint" style="margin-top:0">电网运维问答的 system prompt。改后<b>即改即生效</b>（下次问答即用，热读缓存）。空保存=恢复 code 默认。</p>
        <textarea class="input" v-model="promptText" rows="14" style="width:100%;font-family:inherit;resize:vertical;margin-top:8px" placeholder="留空保存即恢复默认"></textarea>
      </div>
      <!-- 语义增强规则 -->
      <div class="card" v-show="tab === 'srules'">
        <div class="card-header"><h3 class="card-title">🏷️ 语义增强规则 <span class="badge badge-neutral">{{ srules.length }}</span></h3></div>
        <p class="hint" style="margin-top:0">自定义「维度→关键词→标签」规则，给文档/分块文本打语义维度（如 作业场景=倒闸操作、安全等级=高风险）。</p>
        <div class="row" style="gap:8px;margin:8px 0;flex-wrap:wrap">
          <input class="input" v-model="srForm.dimension" placeholder="维度（作业场景/安全等级）" style="width:150px" />
          <input class="input" v-model="srForm.tag" placeholder="标签（倒闸操作/高风险）" style="width:150px" />
          <input class="input" v-model="srForm.kws" placeholder="关键词（逗号分隔）" style="flex:1;min-width:200px" />
          <button class="btn btn-primary btn-sm" @click="doAddSrule">＋ 新增</button>
        </div>
        <div style="overflow-x:auto">
          <table class="tbl">
            <thead><tr><th>维度</th><th>标签</th><th>关键词</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in srules" :key="i"><td>{{ r.dimension }}</td><td>{{ r.tag }}</td><td class="muted">{{ (r.keywords||[]).join('、') }}</td><td><button class="btn btn-danger btn-sm" @click="doDelSrule(i)">删除</button></td></tr>
              <tr v-if="!srules.length"><td colspan="4" class="empty">暂无规则</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- N1 记忆管理 -->
      <div class="card" v-show="tab === 'memory'">
        <div class="card-header">
          <h3 class="card-title">🧠 Agent 长期记忆</h3>
          <button class="btn btn-ghost btn-sm" @click="loadMemories">🔄 刷新</button>
        </div>
        <p class="hint" style="margin-top:0;line-height:1.7">
          Agent 从对话中自动抽取的结构化事实（用户偏好/诊断结论/待确认项），用于跨会话上下文延续。<br/>
          <b>只读查看 + 软删除</b>（删除后保留30天审计日志）。容量上限 {{ memoryStats.capacity || 500 }} 条/用户。
        </p>
        <div v-if="memoryStats.total !== undefined" class="stats-grid" style="margin-bottom:10px">
          <div class="stat stat-accent"><div class="stat-val">{{ memoryStats.total || 0 }}</div><div class="stat-lbl">总记忆</div></div>
          <div class="stat stat-accent"><div class="stat-val">{{ memoryStats.active || 0 }}</div><div class="stat-lbl">活跃</div></div>
          <div class="stat"><div class="stat-val">{{ memoryStats.deleted || 0 }}</div><div class="stat-lbl">已删除</div></div>
          <div class="stat"><div class="stat-val">{{ memoryStats.users || 0 }}</div><div class="stat-lbl">用户数</div></div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;margin:8px 0">
          <input class="input" v-model="memoryFilter" placeholder="按用户名筛选（空=全部）" @keyup.enter="loadMemories" style="flex:1;max-width:240px" />
          <button class="btn btn-primary btn-sm" @click="loadMemories">🔍 查询</button>
        </div>
        <div style="overflow-x:auto;margin-top:8px">
          <table class="tbl">
            <thead><tr><th>用户</th><th>事实</th><th>实体</th><th>分类</th><th>权重</th><th>命中</th><th>创建时间</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="m in memories.list" :key="m.factId">
                <td>{{ m.userId }}</td>
                <td style="max-width:300px">{{ m.factText }}</td>
                <td class="muted">{{ m.entity || '-' }}</td>
                <td><span class="badge" :class="{ 'badge-info': m.category === 'preference', 'badge-warning': m.category === 'diagnosis', 'badge-neutral': m.category === 'pending' }">{{ {preference:'偏好', diagnosis:'诊断', pending:'待确认'}[m.category] || m.category }}</span></td>
                <td class="muted">{{ m.weight?.toFixed(2) }}</td>
                <td class="muted">{{ m.hitCount }}</td>
                <td class="muted">{{ m.createdAt }}</td>
                <td><span class="badge" :class="m.deleted ? 'badge-danger' : 'badge-success'">{{ m.deleted ? '已删除' : '活跃' }}</span></td>
                <td><button v-if="!m.deleted" class="btn btn-danger btn-sm" @click="doDeleteMemory(m)">删除</button></td>
              </tr>
              <tr v-if="!memories.list?.length"><td colspan="9" class="empty">暂无记忆数据</td></tr>
            </tbody>
          </table>
        </div>
        <div class="hint" style="margin-top:8px" v-if="memories.total">{{ memories.total }} 条记忆 · 第 {{ memoryPage }} 页</div>
      </div>

      <div class="toast" v-if="toastMsg">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick, computed } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'
import AgentTrace from '../components/AgentTrace.vue'
import * as echarts from 'echarts/core'
import { PieChart, BarChart, ScatterChart, LineChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getLogs, getAlerts, configMilvus, configModel, getMilvusConfig, getModelConfig, getProviderHealth, rebuildBm25, getFeedbacks, markFeedbackGolden, getFeedbackStats, alertDispose, getAlertDisposals, getPersonas, upsertPersona, deletePersona, agentRun, getUsers, updateUserRole, updateUserStatus, deleteUser, resetUserPassword, backupDB, listBackups, restoreDB, removeBackup, getLogArchiveStats, archiveLogs, getTerms, addTerm, deleteTerm, getPromptConfig, updatePromptConfig, getSemanticRules, addSemanticRule, deleteSemanticRule, getRetrievalTuneReport, runRetrievalTune, confirmDisposal, rejectDisposal, disposalToTicket, closeDisposal, backupAll, restoreAllBackup, listManifestBackups, deleteManifestBackup, getMemories, deleteMemory, getMemoryStats } from '../api'
import request from '../api/request'

echarts.use([PieChart, BarChart, ScatterChart, LineChart, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const auth = useAuthStore()
const can = (p) => hasPerm(auth.role, p)   // RBAC：Tab 显隐（后端 require_perm 为真相之源，此处仅 UI 提前隐藏）

const tab = ref('feedback')
const logs = ref({ total: 0, list: [] })
const alerts = ref({ total: 0, list: [] })
const disposals = ref({ total: 0, list: [] })          // S3 告警自动处置记录
const tuneReport = ref(null)                           // 检索调参报告（只建议）
const tuneRunning = ref(false)
const dispForm = reactive({ severity: 'critical', title: '', summary: '' })
const personas = ref({ codePersonas: [], configs: [] })  // S5 persona 配置
const users = ref({ total: 0, list: [] })  // RBAC 用户管理
const backups = ref([])                     // 数据备份列表
const backupLoading = ref(false)
const archiveStat = ref(null)               // 日志归档统计
const termsList = ref([])                   // 词表
const termForm = reactive({ alias: '', standard: '' })
const srules = ref([])
const srForm = reactive({ dimension: '', tag: '', kws: '' })
const promptText = ref('')
const promptDefault = ref('')
// N1 记忆管理
const memories = ref({ total: 0, list: [] })
const memoryStats = ref({})
const memoryFilter = ref('')
const memoryPage = ref(1)
const personaForm = reactive({ name: '', systemPrompt: '', allowedTools: '', maxIter: null, temperature: null, maxTokens: null, outputFormat: '', fallbackKey: '', enabled: true })
const feedbacks = ref({ total: 0, list: [] })
const fbStats = ref(null)
const fbFilter = ref('dislike')
const milvus = reactive({ indexType: 'HNSW', M: 16, efConstruction: 200, ef: 64 })
const model = reactive({ modelType: 'deepseek', temperature: 0.2, max_tokens: 2048 })
const health = ref(null)
const healthLoading = ref(false)
const bm25Loading = ref(false)
const configLoaded = ref(false)
const toastMsg = ref('')
let toastTimer = null
function toast(m) { toastMsg.value = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg.value = ''), 1600) }
function judgeBadge(h) {
  if (h == null) return 'badge badge-neutral'
  if (h >= 0.5) return 'badge badge-danger'
  if (h >= 0.2) return 'badge badge-warning'
  return 'badge badge-success'
}
function retrievalLabel(q) {
  if (q === 'good') return '✅ 好'
  if (q === 'partial') return '⚠️ 部分'
  if (q === 'poor') return '❌ 差'
  return '待评'
}
function retrievalBadge(q) {
  if (q === 'good') return 'badge badge-success'
  if (q === 'partial') return 'badge badge-warning'
  if (q === 'poor') return 'badge badge-danger'
  return 'badge badge-neutral'
}

async function loadLogs() { logs.value = (await getLogs({ page: 1, size: 20 })).data }
async function loadAlerts() { try { alerts.value = (await getAlerts({ page: 1, size: 30 })).data } catch (e) { toast('加载告警失败') } loadDisposals() }
async function loadDisposals() { try { const r = await getAlertDisposals({ page: 1, size: 20 }); disposals.value = (r && r.data) ? r.data : { total: 0, list: [] } } catch (e) {} }
function dispStatusLabel(s) {
  return { pending: '⏳处理中', proposed: '🟡待确认', confirmed: '✅已确认', rejected: '❌已驳回', ticketed: '🎫已转两票', closed: '⚪已关闭', disposed: '✅已处置' }[s] || s
}
function dispStatusBadge(s) {
  return { proposed: 'badge-warning', confirmed: 'badge-success', rejected: 'badge-danger', ticketed: 'badge-accent', closed: 'badge-neutral', disposed: 'badge-success', pending: 'badge-neutral' }[s] || 'badge-neutral'
}
async function doConfirmDisp(d) { try { await confirmDisposal(d.id); toast('已确认'); loadDisposals() } catch (e) { toast('操作失败') } }
async function doRejectDisp(d) { const note = prompt('驳回理由：'); if (note === null) return; try { await rejectDisposal(d.id, note); toast('已驳回'); loadDisposals() } catch (e) { toast('操作失败') } }
async function doToTicket(d) { try { await disposalToTicket(d.id); toast('已转两票草稿，请到两票管理提交审核'); loadDisposals() } catch (e) { toast('操作失败') } }
async function doCloseDisp(d) { try { await closeDisposal(d.id); toast('已关闭'); loadDisposals() } catch (e) { toast('操作失败') } }
async function doDispose() {
  if (!dispForm.summary.trim() && !dispForm.title.trim()) { toast('请填告警标题或描述'); return }
  try {
    const r = (await alertDispose(dispForm.severity, dispForm.title, dispForm.summary)).data
    toast(`已触发处置 #${r.id}（后台 AI 分析中，稍后刷新查看）`)
    dispForm.title = ''; dispForm.summary = ''
    setTimeout(loadDisposals, 300)
  } catch (e) { toast('触发失败') }
}
function parseDispDiag(d) { try { return JSON.parse(d) } catch { return null } }
async function loadPersonas() { try { personas.value = (await getPersonas()).data } catch (e) { toast('加载persona失败') } }
async function loadTuneReport() { try { tuneReport.value = (await getRetrievalTuneReport()).data } catch (e) { /* silent */ } }
async function runTuneScan() {
  tuneRunning.value = true
  try { const r = await runRetrievalTune(); toast(r.message || '扫描已启动') } catch (e) { toast('启动扫描失败') }
  setTimeout(() => { loadTuneReport(); tuneRunning.value = false }, 5000)
}
function copyEnv(s) {
  const line = `${s.param}=${s.suggested}`
  if (navigator.clipboard) navigator.clipboard.writeText(line).then(() => toast('已复制：' + line)).catch(() => toast(line))
  else toast(line)
}
async function loadUsers() { try { users.value = (await getUsers()).data } catch (e) { toast('加载用户失败') } }
async function saveUser(u) {
  try { await updateUserRole(u.userId, u.role, u.dept); toast(`已更新 ${u.username} → ${u.role}/${u.dept || '-'}`) }
  catch (e) { toast('更新失败') }
}
async function toggleUserStatus(u) {
  const next = u.status === 'inactive' ? 'active' : 'inactive'
  try { await updateUserStatus(u.userId, next); u.status = next; toast(`${u.username} 已${next === 'inactive' ? '禁用' : '启用'}`) }
  catch (e) { toast('操作失败') }
}
async function resetUserPwd(u) {
  const pwd = prompt(`重置 ${u.username} 的密码（至少6位）`)
  if (pwd == null) return
  if (pwd.length < 6) { toast('密码至少 6 位'); return }
  try { await resetUserPassword(u.userId, pwd); toast(`${u.username} 密码已重置`) } catch (e) { toast('重置失败') }
}
async function removeUser(u) {
  if (!confirm(`确认删除用户 ${u.username}？此操作不可恢复。`)) return
  try { await deleteUser(u.userId); toast(`已删除 ${u.username}`); loadUsers() } catch (e) { toast('删除失败（自己或最后一个管理员？）') }
}
async function loadBackups() { try { backups.value = (await listManifestBackups()).data || [] } catch (e) { toast('加载备份列表失败') } }
async function doBackupAll() {
  backupLoading.value = true
  try { const r = await backupAll(); toast(`三合一备份成功：MySQL ${r.data.meta.mysqlRows}行 / Redis ${r.data.meta.redisKeys}key / Milvus ${r.data.meta.milvusVectors}向量`); await loadBackups() }
  catch (e) { toast('备份失败') } finally { backupLoading.value = false }
}
async function doRestoreAll(ts) {
  if (!confirm(`确认从备份 ${ts} 一键恢复？⚠ MySQL+Redis+Milvus 当前数据将全量覆盖！`)) return
  try { await restoreAllBackup(ts); toast('三合一恢复完成'); await loadBackups() } catch (e) { toast('恢复失败') }
}
async function doDeleteBackupAll(ts) {
  if (!confirm(`删除备份 ${ts}？（manifest + 3 数据文件）`)) return
  try { await deleteManifestBackup(ts); toast('已删除'); await loadBackups() } catch (e) { toast('删除失败') }
}
async function loadArchiveStats() { try { archiveStat.value = (await getLogArchiveStats()).data } catch (e) { /* 非管理员静默 */ } }
async function loadTerms() { try { termsList.value = (await getTerms()).data || [] } catch (e) { /* silent */ } }
async function doAddTerm() {
  if (!termForm.alias.trim() || !termForm.standard.trim()) { toast('别名和标准词都必填'); return }
  try { await addTerm(termForm.alias.trim(), termForm.standard.trim()); toast('已保存（缓存已清）'); termForm.alias = ''; termForm.standard = ''; await loadTerms() }
  catch (e) { toast('保存失败') }
}
async function doDeleteTerm(alias) { if (!confirm(`删除词条「${alias}」？`)) return; try { await deleteTerm(alias); toast('已删除'); await loadTerms() } catch (e) { toast('删除失败') } }
async function loadSrules() { try { srules.value = (await getSemanticRules()).data || [] } catch (e) { /* silent */ } }
async function doAddSrule() {
  const kws = srForm.kws.split(',').map(s => s.trim()).filter(Boolean)
  if (!srForm.dimension.trim() || !srForm.tag.trim() || !kws.length) { toast('维度/标签/关键词都必填'); return }
  try { await addSemanticRule(srForm.dimension.trim(), srForm.tag.trim(), kws); toast('已保存'); srForm.dimension = srForm.tag = srForm.kws = ''; await loadSrules() } catch (e) { toast('保存失败') }
}
async function doDelSrule(idx) { if (!confirm('删除该规则？')) return; try { await deleteSemanticRule(idx); toast('已删除'); await loadSrules() } catch (e) { toast('删除失败') } }
async function loadPrompt() { try { const d = (await getPromptConfig()).data; promptDefault.value = d.default || ''; promptText.value = d.systemPrompt || '' } catch (e) { /* silent */ } }
async function savePrompt() { try { await updatePromptConfig(promptText.value); toast('Prompt 已保存（下次问答生效）') } catch (e) { toast('保存失败') } }

// N1 记忆管理
async function loadMemories() {
  try {
    const params = { page: memoryPage.value, size: 20 }
    if (memoryFilter.value.trim()) params.userId = memoryFilter.value.trim()
    const r = await getMemories(params)
    memories.value = r.data || { total: 0, list: [] }
    const s = await getMemoryStats()
    memoryStats.value = { ...(s.data || {}), capacity: 500 }
  } catch (e) { toast('加载记忆失败') }
}
async function doDeleteMemory(m) {
  if (!confirm(`删除记忆「${m.factText}」？（软删除，保留30天审计）`)) return
  try { await deleteMemory(m.factId); toast('已删除'); loadMemories() } catch (e) { toast('删除失败') }
}
async function doArchiveLogs() {
  const days = archiveStat.value?.retentionDays || 90
  if (!confirm(`归档超过 ${days} 天的日志？导出 jsonl 后从库删除。`)) return
  try { const r = await archiveLogs(); toast(`已归档 ${r.data.archived} 条 → ${r.data.file || '无'}`); await loadArchiveStats(); loadLogs() }
  catch (e) { toast('归档失败') }
}
function editPersona(p) { Object.assign(personaForm, { name: p.name, systemPrompt: p.systemPrompt || '', allowedTools: p.allowedTools || '', maxIter: p.maxIter, temperature: p.temperature, maxTokens: p.maxTokens, outputFormat: p.outputFormat || '', fallbackKey: p.fallbackKey || '', enabled: p.enabled }) }
async function savePersona() {
  if (!personaForm.name.trim()) { toast('请填 persona 名'); return }
  try { await upsertPersona({ ...personaForm }); toast('保存成功（DB 覆盖已生效）'); loadPersonas() } catch (e) { toast('保存失败') }
}
async function removePersona(name) { try { await deletePersona(name); toast('已删除（恢复内置默认）'); loadPersonas() } catch (e) { toast('删除失败') } }
async function testPersona(name) {
  const q = window.prompt('测试 persona "' + name + '"，输入问题：')
  if (!q) return
  toast('运行中（agent 多轮调工具）…')
  try {
    const r = await agentRun(name, q)
    const d = r.data || {}
    alert('persona: ' + d.persona + '\n轮数: ' + d.iterations + ' | 工具: ' + (d.toolsUsed || []).join(',') + ' | 降级: ' + d.degraded + '\n\n答案:\n' + (typeof d.answer === 'string' ? d.answer : JSON.stringify(d.answer, null, 2)))
  } catch (e) { toast('运行失败') }
}
function personaLabel(c) {
  const map = { qa: '💬 问答助手', diagnose: '🔬 诊断助手', alert: '🚨 告警处置助手' }
  return map[c] ? `${c} · ${map[c]}` : c
}
const sevOf = (c = '') => { const m = c.match(/^\[(info|warning|critical)\]/i); return m ? m[1].toLowerCase() : 'info' }
const sevBadge = (c = '') => ({ critical: 'badge badge-danger', warning: 'badge badge-warning', info: 'badge badge-info' }[sevOf(c)] || 'badge badge-neutral')
async function loadFeedbacks(fb = 'dislike') { fbFilter.value = fb; try { feedbacks.value = (await getFeedbacks({ feedback: fb, page: 1, size: 30 })).data } catch (e) { toast('加载反馈失败') } }
async function loadFbStats() { try { fbStats.value = (await getFeedbackStats()).data } catch (e) { /* silent */ } }
async function markGolden(f) { try { const r = (await markFeedbackGolden(f.id)).data; toast(r.added ? `已加入 golden 集（共 ${r.total} 条）` : `未加入：${r.reason || '已存在'}`) } catch (e) { toast('操作失败') } }
async function saveMilvus() { await configMilvus(milvus.indexType, { M: Number(milvus.M), efConstruction: Number(milvus.efConstruction), ef: Number(milvus.ef) }); toast('Milvus 已保存（ef 即时生效）') }
async function saveModel() { await configModel(model.modelType, { temperature: Number(model.temperature), max_tokens: Number(model.max_tokens) }); toast('模型参数已保存（temperature 即时生效）') }
async function loadConfig() {
  try {
    const mv = (await getMilvusConfig()).data || {}
    const md = (await getModelConfig()).data || {}
    const mp = mv.param || {}
    milvus.indexType = mv.indexType || 'HNSW'
    milvus.M = mp.M ?? 16
    milvus.efConstruction = mp.efConstruction ?? 200
    milvus.ef = mp.ef ?? 64
    const pp = md.param || {}
    model.modelType = md.modelType || 'deepseek'
    model.temperature = pp.temperature ?? 0.2
    model.max_tokens = pp.max_tokens ?? 2048
    configLoaded.value = true
  } catch (e) { toast('读取线上配置失败') }
}
async function loadHealth() {
  healthLoading.value = true
  try { health.value = (await getProviderHealth()).data } catch (e) { toast('探测失败') } finally { healthLoading.value = false }
}
async function handleRebuildBm25() {
  bm25Loading.value = true
  try { const r = (await rebuildBm25()).data; toast(`BM25 重建完成（${r.chunks} 个分块）`) } catch (e) { toast('重建失败') } finally { bm25Loading.value = false }
}
const optimizer = ref(null)
const optLoading = ref(false)
async function loadOptimizer() {
  optLoading.value = true
  try {
    const r = await request.get('/system/optimizer/report')
    optimizer.value = r.data || null
    loadBlacklist()
  } catch (e) { /* silent */ } finally { optLoading.value = false }
}
async function generateOptimizer() {
  optLoading.value = true
  try {
    const r = await request.post('/system/optimizer/generate')
    optimizer.value = r.data || null
    toast('优化建议已生成')
  } catch (e) { toast('生成失败') } finally { optLoading.value = false }
}
const tuneResult = ref(null)
const tuneLoading = ref(false)
async function tuneCache() {
  tuneLoading.value = true
  try {
    const r = await request.post('/system/optimizer/tune-cache')
    tuneResult.value = r.data || null
    toast(`已应用黑名单 ${(r.data || {}).appliedBlacklist || 0} 条`)
    loadBlacklist()
  } catch (e) { toast('调优失败') } finally { tuneLoading.value = false }
}
const blacklist = ref([])
const blInput = ref('')
async function loadBlacklist() {
  try { blacklist.value = (await request.get('/system/optimizer/blacklist')).data || [] } catch (e) { /* silent */ }
}
async function addBlacklist() {
  const q = blInput.value.trim()
  if (!q) return
  try {
    await request.post('/system/optimizer/blacklist', null, { params: { query: q } })
    blInput.value = ''
    await loadBlacklist()
    toast('已加入黑名单')
  } catch (e) { toast('加入失败') }
}
async function removeBlacklist(q) {
  try {
    await request.delete('/system/optimizer/blacklist', { params: { query: q } })
    await loadBlacklist()
    toast('已移出黑名单')
  } catch (e) { toast('移除失败') }
}
const rwStats = ref(null); const rwEvents = ref([]); const rwPeriod = ref('today')
const rwPieEl = ref(null); const rwScatterEl = ref(null); const rwTrendEl = ref(null)
const egList = ref([]); const egFilter = ref('pending')
async function loadEvidenceGaps() {
  try {
    const d = (await request.get('/system/evidence-gap', { params: { status: egFilter.value || undefined, size: 50 } })).data
    egList.value = (d && d.list) || []
  } catch (e) { toast('加载失败') }
}
async function egDraft(g) {
  try { const r = await request.post(`/system/evidence-gap/${g.id}/ai-draft`); g.aiDraft = (r.data || {}).aiDraft || ''; g.status = 'ai_drafted'; toast('AI续写完成') }
  catch (e) { toast('续写失败') }
}
async function egDeepDraft(g) {
  g.agentSteps = []; g.deepStreaming = true; g._traceOpen = true
  const auth = useAuthStore()
  try {
    const resp = await fetch(`/api/system/evidence-gap/${g.id}/deep-draft`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}) },
      body: JSON.stringify({ modelType: null })
    })
    if (!resp.ok) throw new Error('流式失败')
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n'); buf = lines.pop()
      for (const line of lines) {
        const s = line.trim()
        if (!s.startsWith('data:')) continue
        const payload = s.slice(5).trim()
        if (payload === '[DONE]') continue
        try {
          const ev = JSON.parse(payload)
          if (ev.type === 'tool_step') { g.agentSteps.push(ev.step || {}) }
          else if (ev.type === 'token') { g.aiDraft = ev.content || ''; g.status = 'ai_drafted' }
          else if (ev.type === 'done') { toast(ev.error ? ('失败: ' + ev.error) : '深度补全完成') }
        } catch (e) {}
      }
    }
  } catch (e) { toast('深度补全失败') }
  finally { g.deepStreaming = false }
}
async function saveAiDraftInline(g) {
  const t = g.aiDraft || ''
  g._editing = false
  try { await request.put(`/system/evidence-gap/${g.id}/ai-draft`, { aiDraft: t }); toast('草稿已保存') }
  catch (e) { toast('保存失败') }
}
function confBadgeClass(c) {
  return { sufficient: 'badge-success', medium: 'badge-warning', refused: 'badge-danger',
           outdated: 'badge-neutral', conflict: 'badge-danger', verify: 'badge-info', expired: 'badge-neutral' }[c] || 'badge-neutral'
}
async function saveConf(g) {
  try { await request.put(`/system/evidence-gap/${g.id}/confidence`, { confidence: g.confidence }); toast('标注已保存') }
  catch (e) { toast('保存失败') }
}
async function retagFaq() {
  try { const r = await request.post('/system/evidence-gap/retag'); toast(`已补全 ${r.data.updated} 条设备标签`) }
  catch (e) { toast('补全失败') }
}
const egEditing = ref(null); const egEditText = ref(''); const egSaving = ref(false)
function egEdit(g) {
  egEditing.value = { id: g.id, query: g.query, confidence: g.confidence,
                      originalAnswer: g.originalAnswer || '', aiDraft: g.aiDraft || '' }
  egEditText.value = g.finalAnswer || g.aiDraft || g.originalAnswer || ''
}
async function egSave() {
  if (!egEditText.value.trim()) { toast('答案不能为空'); return }
  egSaving.value = true
  try {
    const id = egEditing.value.id
    await request.post(`/system/evidence-gap/${id}/edit`, { finalAnswer: egEditText.value })
    const g = egList.value.find(x => x.id === id)
    if (g) { g.finalAnswer = egEditText.value; g.status = 'confirmed' }
    egEditing.value = null
    toast('已保存，点「✓ 确认同步」入库')
  } catch (e) { toast('保存失败') } finally { egSaving.value = false }
}
async function egConfirm(g) {
  try {
    const r = await request.post(`/system/evidence-gap/${g.id}/confirm`)
    if ((r.data || {}).ok) { g.status = 'synced'; toast('已确认并同步入库') }
    else { toast('同步失败: ' + ((r.data || {}).msg || '')) }
  } catch (e) { toast('确认失败') }
}
async function egIgnore(g) {
  try { await request.post(`/system/evidence-gap/${g.id}/ignore`); g.status = 'ignored'; toast('已忽略') }
  catch (e) { toast('失败') }
}
async function loadRewrite() {
  try {
    rwStats.value = (await request.get('/system/optimizer/rewrite-stats', { params: { period: rwPeriod.value } })).data
    await loadRewriteEvents()
    await nextTick()  // 等 DOM ref 就位再渲染图表
    renderRwCharts()
  } catch (e) { toast('加载失败') }
}
async function loadRewriteEvents() {
  try {
    const d = (await request.get('/system/optimizer/rewrite-events', { params: { size: 50 } })).data
    rwEvents.value = (d && d.list) || []
  } catch (e) { rwEvents.value = [] }
}
function renderRwCharts() {
  if (!rwStats.value) return
  const bs = rwStats.value.byStrategy || {}
  if (rwPieEl.value) {
    echarts.init(rwPieEl.value).setOption({
      title: { text: '策略分布', left: 'center', textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'item' },
      series: [{ type: 'pie', radius: ['40%', '70%'], data: Object.entries(bs).map(([k, v]) => ({ name: k, value: v.count })) }]
    })
  }
  if (rwScatterEl.value) {
    echarts.init(rwScatterEl.value).setOption({
      title: { text: '改写前后分数（对角线上方=改进）', left: 'center', textStyle: { fontSize: 13 } },
      xAxis: { name: '原分数', type: 'value' }, yAxis: { name: '新分数', type: 'value' },
      tooltip: { trigger: 'item' },
      series: [{ type: 'scatter', symbolSize: 6, data: rwEvents.value.map(e => [e.origScore || 0, e.newScore || 0]), itemStyle: { color: '#3b82f6' } }]
    })
  }
  if (rwTrendEl.value && rwStats.value.daily && rwStats.value.daily.length) {
    const daily = rwStats.value.daily
    echarts.init(rwTrendEl.value).setOption({
      title: { text: '采纳率 / 否决率 / 缓存命中率 趋势', left: 'center', textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'axis' },
      legend: { data: ['采纳率', '否决率', '缓存命中率'], bottom: 0 },
      grid: { left: 40, right: 20, top: 40, bottom: 40 },
      xAxis: { type: 'category', data: daily.map(d => d.date) },
      yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' } },
      series: [
        { name: '采纳率', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, itemStyle: { color: '#22c55e' }, data: daily.map(d => +(d.adoptedRate * 100).toFixed(1)) },
        { name: '否决率', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, itemStyle: { color: '#ef4444' }, data: daily.map(d => +(d.rejectedRate * 100).toFixed(1)) },
        { name: '缓存命中率', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, itemStyle: { color: '#3b82f6' }, data: daily.map(d => +(d.cacheHitRate * 100).toFixed(1)) },
      ]
    })
  }
}
function typeLabel(t) {
  return { retrieval: '检索优化', knowledge_gap: '知识盲区', cache: '缓存策略', trend: '趋势预警', hallucination: '编造风险' }[t] || t
}
function severityBadge(s) {
  return { high: 'badge badge-danger', medium: 'badge badge-warning', low: 'badge badge-info' }[s] || 'badge badge-neutral'
}
const costReport = ref(null); const quality = ref(null); const evalTrend = ref(null); const abConfig = ref(null); const qualityPieEl = ref(null); const qualityPieScoreEl = ref(null); const qualityPieCovEl = ref(null); const qualityBarEl = ref(null); const evalLineEl = ref(null)
async function loadCostReport() { try { costReport.value = (await request.get('/system/cost/report', { params: { period: 'today' } })).data } catch (e) { toast('加载失败') } }
async function loadQuality() {
  try {
    quality.value = (await request.get('/system/knowledge/quality')).data
    await nextTick(); renderQualityCharts()
  } catch (e) { toast('加载失败') }
}
function gradeColor(g) { return ({ S: '#ffd700', A: '#f59e0b', B: '#10b981', C: '#ef4444', D: '#6b7280' })[g] || '#94a3b8' }
function renderQualityCharts() {
  if (!quality.value) return
  const q = quality.value
  if (qualityPieEl.value) {
    echarts.init(qualityPieEl.value).setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c} 份 ({d}%)' },
      legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
      color: ['#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4'],
      series: [{ type: 'pie', radius: ['40%', '68%'], center: ['50%', '45%'],
        data: Object.entries(q.docTypeDistribution || {}).map(([name, value]) => ({ name, value })),
        label: { color: '#cbd5e1', formatter: '{b}\n{c} 份' },
        itemStyle: { borderColor: '#1e293b', borderWidth: 2 },
        emphasis: { scale: true, scaleSize: 8, itemStyle: { shadowBlur: 14, shadowColor: 'rgba(0,0,0,.4)' } },
      }],
    })
  }
  const pie2 = (el, val, title, color) => {
    if (!el.value) return
    echarts.init(el.value).setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c}%' },
      series: [{ type: 'pie', radius: ['60%', '80%'], center: ['50%', '50%'],
        data: [{ name: title, value: val, itemStyle: { color } }, { name: '未达成', value: 100 - val, itemStyle: { color: 'rgba(148,163,184,.16)' } }],
        label: { show: true, position: 'center', formatter: `{a|${val}%}\n{b|${title}}`,
          rich: { a: { fontSize: 24, color: '#e2e8f0', fontWeight: 700, lineHeight: 30 }, b: { fontSize: 12, color: '#94a3b8' } } },
        emphasis: { scale: true, scaleSize: 12, itemStyle: { shadowBlur: 18, shadowColor: 'rgba(0,0,0,.5)' } },
      }],
    })
  }
  pie2(qualityPieScoreEl, Math.round((q.qualityScore || 0) * 100), '分块质量', '#10b981')
  pie2(qualityPieCovEl, Math.round((q.coverageRate || 0) * 100), '向量化覆盖', '#3b82f6')
  if (qualityBarEl.value) {
    echarts.init(qualityBarEl.value).setOption({
      tooltip: { formatter: '{b}: {c}%' },
      grid: { left: 72, right: 40, top: 16, bottom: 28 },
      xAxis: { type: 'value', max: 100, axisLabel: { color: '#94a3b8', formatter: '{value}%' }, splitLine: { lineStyle: { color: 'rgba(148,163,184,.15)' } } },
      yAxis: { type: 'category', data: ['向量化覆盖', '分块质量', '去重率'], axisLabel: { color: '#cbd5e1' }, axisLine: { lineStyle: { color: 'rgba(148,163,184,.3)' } } },
      series: [{ type: 'bar', barWidth: 16,
        data: [
          { value: Math.round((q.coverageRate || 0) * 100), itemStyle: { color: '#3b82f6' } },
          { value: Math.round((q.qualityScore || 0) * 100), itemStyle: { color: '#10b981' } },
          { value: Math.round((1 - (q.dupRate || 0)) * 100), itemStyle: { color: '#f59e0b' } },
        ],
        label: { show: true, position: 'right', formatter: '{c}%', color: '#cbd5e1' },
        emphasis: { focus: 'self', itemStyle: { shadowBlur: 12, shadowColor: 'rgba(255,255,255,.4)' } },
      }],
    })
  }
}
async function loadEval() {
  try {
    evalTrend.value = (await request.get('/system/eval/trends', { params: { days: 7 } })).data
    await nextTick(); renderEvalChart()
  } catch (e) { toast('加载失败') }
}
function evalSamples() { return (evalTrend.value?.trends || []).reduce((s, x) => s + (x.samples || 0), 0) }
function evalAvg() {
  const t = evalTrend.value?.trends || []
  if (!t.length) return '-'
  return (t.reduce((s, x) => s + (x.overall || 0), 0) / t.length * 100).toFixed(1) + '%'
}
function renderEvalChart() {
  const t = evalTrend.value
  if (!t || !evalLineEl.value) return
  const arr = t.trends || []
  const pct = (xs) => xs.map(x => Math.round((x || 0) * 100))
  echarts.init(evalLineEl.value).setOption({
    tooltip: { trigger: 'axis', formatter: (p) => p.map(i => `${i.marker}${i.seriesName}: ${i.value}%`).join('<br/>') },
    legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
    grid: { left: 44, right: 24, top: 24, bottom: 40 },
    xAxis: { type: 'category', data: arr.map(x => x.date), boundaryGap: false, axisLabel: { color: '#94a3b8' }, axisLine: { lineStyle: { color: 'rgba(148,163,184,.3)' } } },
    yAxis: { type: 'value', max: 100, axisLabel: { color: '#94a3b8', formatter: '{value}%' }, splitLine: { lineStyle: { color: 'rgba(148,163,184,.15)' } } },
    series: [
      { name: '综合', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, data: pct(arr.map(x => x.overall)), itemStyle: { color: '#3b82f6' }, areaStyle: { opacity: 0.15 } },
      { name: '相关性', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, data: pct(arr.map(x => x.relevance)), itemStyle: { color: '#10b981' } },
      { name: '忠实度', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6, data: pct(arr.map(x => x.faithfulness)), itemStyle: { color: '#f59e0b' } },
    ],
  })
}
async function loadABTest() { try { abConfig.value = (await request.get('/system/routing/config')).data } catch (e) { toast('加载失败') } }
onMounted(() => {
  loadLogs(); loadFeedbacks('dislike'); loadFbStats()
  if (can('alert:read')) { loadAlerts(); connectAlertsWs() }   // 审计员/管理员：告警实时推送
  if (can('system:config')) { loadConfig(); loadArchiveStats() }  // 仅管理员（系统配置/日志归档）
})
onUnmounted(() => { try { alertWs && alertWs.close() } catch (e) {} })
let alertWs = null
function connectAlertsWs() {
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    alertWs = new WebSocket(`${proto}://${location.host}/api/system/ws/alerts?token=${auth.token}`)
    alertWs.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.type === 'alert') { toast(`🚨 新告警：${d.title}`); loadAlerts() }
      } catch (_) {}
    }
  } catch (e) {}
}
</script>

<style scoped>
.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .config-grid { grid-template-columns: 1fr } }
.opt-card { background: var(--surface-2); padding: 10px 12px; border-radius: var(--radius-sm); margin-bottom: 8px; border-left: 3px solid var(--border); }
.opt-card.sev-high { border-left-color: var(--danger); }
.opt-card.sev-medium { border-left-color: var(--warning); }
.opt-card.sev-low { border-left-color: var(--info); }
.opt-header { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-bottom: 4px; flex-wrap: wrap; }
.opt-type { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.opt-detail { font-size: 12px; color: var(--text); margin-bottom: 4px; line-height: 1.5; }
.ring-stat { display: flex; align-items: center; justify-content: center; }
.ring { width: 98px; height: 98px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }
.ring-hole { width: 74px; height: 74px; border-radius: 50%; background: var(--surface); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; }
.ring-val { font-size: 17px; font-weight: 700; color: var(--primary); }
.ring-lbl { font-size: 11px; color: var(--text-muted); }
.opt-actions { display: flex; flex-direction: column; gap: 2px; }
.opt-action { font-size: 12px; color: var(--text-muted); padding-left: 8px; border-left: 2px solid var(--border); margin: 1px 0; }
</style>
