<template>
  <div class="governance-page">
    <section class="governance-hero">
      <div class="hero-copy">
        <div class="hero-eyebrow"><span class="pulse-dot"></span> KNOWLEDGE LIFECYCLE CONTROL</div>
        <h1>让每条知识都有清晰的有效边界</h1>
        <p>统一管理责任人、适用范围、有效期与版本关系；扫描只生成可解释的问题线索，不会自动覆盖原始知识。</p>
        <div class="hero-actions">
          <button class="btn btn-primary" @click="openScan">
            <span aria-hidden="true">⌁</span> 发起治理扫描
          </button>
          <button class="btn btn-ghost" :disabled="refreshing" @click="refreshAll">
            {{ refreshing ? '刷新中…' : '刷新数据' }}
          </button>
        </div>
      </div>

      <div class="lifecycle" aria-label="知识生命周期">
        <div class="lifecycle-line" aria-hidden="true"></div>
        <div v-for="(stage, index) in lifecycleStages" :key="stage.title" class="lifecycle-stage">
          <span class="stage-node" :class="{ active: index < 3 }">{{ index + 1 }}</span>
          <div>
            <strong>{{ stage.title }}</strong>
            <small>{{ stage.desc }}</small>
          </div>
        </div>
      </div>
    </section>

    <section class="stat-grid governance-stats" aria-label="知识治理统计">
      <div class="stat stat-accent">
        <div class="stat-top"><span class="stat-kicker">知识资产</span><span class="stat-symbol">文</span></div>
        <div class="stat-val">{{ stats.documents ?? 0 }}</div>
        <div class="stat-lbl">纳入治理的文档总数</div>
      </div>
      <div class="stat coverage-stat">
        <div class="stat-top"><span class="stat-kicker">元数据覆盖</span><span class="coverage-value">{{ coveragePercent }}%</span></div>
        <div class="coverage-track"><span :style="{ width: coveragePercent + '%' }"></span></div>
        <div class="stat-lbl">{{ stats.governedDocuments ?? 0 }} 份已建立治理档案</div>
      </div>
      <div class="stat">
        <div class="stat-top"><span class="stat-kicker">待处置</span><span class="stat-symbol warning-symbol">!</span></div>
        <div class="stat-val warning-text">{{ unresolvedCount }}</div>
        <div class="stat-lbl">待确认或处理中问题</div>
      </div>
      <div class="stat">
        <div class="stat-top"><span class="stat-kicker">潜在冲突</span><span class="stat-symbol danger-symbol">⇄</span></div>
        <div class="stat-val danger-text">{{ conflictCount }}</div>
        <div class="stat-lbl">相反规定与阈值差异</div>
      </div>
    </section>

    <section v-if="lastScan" class="scan-result" :class="lastScan.mode === 'queued' ? 'queued' : lastScan.mode === 'failed' ? 'failed' : 'complete'">
      <div class="scan-result-icon">{{ lastScan.mode === 'queued' ? '↻' : lastScan.mode === 'failed' ? '!' : '✓' }}</div>
      <div class="scan-result-copy">
        <strong>{{ lastScan.mode === 'queued' ? '扫描任务正在后台执行' : lastScan.mode === 'failed' ? '治理扫描失败' : '治理扫描完成' }}</strong>
        <span v-if="lastScan.mode === 'queued'">
          当前状态 {{ taskStatusLabel(lastScan.task?.status) }}，任务编号 {{ shortId(lastScan.task?.id || lastScan.task?.taskId) }}。
        </span>
        <span v-else-if="lastScan.mode === 'failed'">
          {{ lastScan.error || lastScan.task?.lastError || '请稍后重试，或联系管理员查看任务日志。' }}
        </span>
        <span v-else>
          扫描 {{ lastScan.documentsScanned || 0 }} 份文档，发现 {{ lastScan.findings || 0 }} 个问题；新增 {{ lastScan.created || 0 }} 个，更新 {{ lastScan.updated || 0 }} 个。
        </span>
      </div>
      <button class="icon-btn" title="关闭提示" aria-label="关闭扫描结果" @click="lastScan = null">×</button>
    </section>

    <div class="workspace-tabs" role="tablist" aria-label="治理工作区">
      <button class="workspace-tab" :class="{ active: activeTab === 'documents' }" role="tab" :aria-selected="activeTab === 'documents'" @click="activeTab = 'documents'">
        <span>治理档案</span><b>{{ docs.total || 0 }}</b>
      </button>
      <button class="workspace-tab" :class="{ active: activeTab === 'issues' }" role="tab" :aria-selected="activeTab === 'issues'" @click="activeTab = 'issues'">
        <span>问题工作台</span><b>{{ unresolvedCount }}</b>
      </button>
    </div>

    <section v-show="activeTab === 'documents'" class="card workspace-card" role="tabpanel">
      <div class="card-header workspace-head">
        <div>
          <h2 class="card-title">文档治理档案</h2>
          <div class="card-desc">补录责任人、适用区域、生效与复审策略，决定知识在检索链路中的可用状态。</div>
        </div>
        <div class="toolbar">
          <div class="search-box">
            <span aria-hidden="true">⌕</span>
            <input v-model.trim="docKeyword" aria-label="搜索文档" placeholder="搜索文档名称" @keyup.enter="searchDocuments" />
            <button v-if="docKeyword" aria-label="清空搜索" @click="docKeyword = ''; searchDocuments()">×</button>
          </div>
          <button class="btn btn-ghost btn-sm" @click="searchDocuments">查询</button>
        </div>
      </div>

      <div v-if="selectedDocIds.length" class="selection-bar">
        <span>已选择 <b>{{ selectedDocIds.length }}</b> 份文档</span>
        <button class="btn btn-primary btn-sm" @click="openScan(true)">扫描所选文档</button>
        <button class="btn btn-link btn-sm" @click="selectedDocIds = []">取消选择</button>
      </div>

      <div class="table-wrap governance-table-wrap">
        <table class="tbl governance-table">
          <thead>
            <tr>
              <th class="check-col"><input type="checkbox" :checked="allCurrentDocsSelected" aria-label="选择当前页全部文档" @change="toggleCurrentDocs($event.target.checked)" /></th>
              <th>文档</th>
              <th>治理状态</th>
              <th>责任人与范围</th>
              <th>版本</th>
              <th>待补字段</th>
              <th class="action-col">操作</th>
            </tr>
          </thead>
          <tbody v-if="docsLoading">
            <tr v-for="i in 6" :key="i"><td colspan="7"><div class="skeleton-row"></div></td></tr>
          </tbody>
          <tbody v-else>
            <tr v-for="doc in docs.list" :key="doc.docId">
              <td class="check-col"><input v-model="selectedDocIds" type="checkbox" :value="doc.docId" :aria-label="`选择 ${doc.docName}`" /></td>
              <td>
                <div class="doc-title">{{ doc.docName }}</div>
                <div class="cell-meta"><span>{{ doc.docType || '未分类' }}</span><span>·</span><span>{{ documentStatusLabel(doc.documentStatus) }}</span></div>
              </td>
              <td><span class="badge state-badge" :class="effectiveStateBadge(doc.effectiveState)"><i></i>{{ effectiveStateLabel(doc.effectiveState) }}</span></td>
              <td>
                <div v-if="doc.metadata?.owner" class="owner-line"><span class="owner-avatar">{{ doc.metadata.owner.slice(0, 1) }}</span>{{ doc.metadata.owner }}</div>
                <span v-else class="missing-copy">未指定责任人</span>
                <div class="cell-meta scope-copy">{{ doc.metadata?.applicableRegion || '适用范围待补录' }}</div>
              </td>
              <td>
                <div class="version-line">{{ doc.metadata?.versionLabel || '未标注' }}</div>
                <span class="cell-meta">{{ versionStatusLabel(doc.metadata?.versionStatus) }}</span>
              </td>
              <td>
                <div v-if="doc.missingFields?.length" class="missing-summary">
                  <span class="badge badge-warning">{{ doc.missingFields.length }} 项</span>
                  <span class="missing-preview">{{ doc.missingFields.slice(0, 2).map(missingFieldLabel).join('、') }}<template v-if="doc.missingFields.length > 2">…</template></span>
                </div>
                <span v-else class="badge badge-success">档案完整</span>
              </td>
              <td class="action-col"><button class="btn btn-ghost btn-sm" @click="openProfile(doc)">{{ doc.metadata ? '编辑档案' : '建立档案' }}</button></td>
            </tr>
            <tr v-if="!docs.list?.length"><td colspan="7"><div class="empty-state"><span>◇</span><strong>没有找到文档</strong><p>调整搜索条件，或先在文档管理中上传知识文件。</p></div></td></tr>
          </tbody>
        </table>
      </div>

      <div class="pagination">
        <span>共 {{ docs.total || 0 }} 份</span>
        <button class="btn btn-ghost btn-sm" :disabled="docPage <= 1 || docsLoading" @click="changeDocPage(docPage - 1)">上一页</button>
        <b>{{ docPage }} / {{ docTotalPages }}</b>
        <button class="btn btn-ghost btn-sm" :disabled="docPage >= docTotalPages || docsLoading" @click="changeDocPage(docPage + 1)">下一页</button>
      </div>
    </section>

    <section v-show="activeTab === 'issues'" class="card workspace-card" role="tabpanel">
      <div class="card-header workspace-head issue-head">
        <div>
          <h2 class="card-title">治理问题工作台</h2>
          <div class="card-desc">证据先于结论。冲突扫描仅提供候选片段，最终状态由审核人确认。</div>
        </div>
        <button class="btn btn-ghost btn-sm" :disabled="issuesLoading" @click="loadIssues">{{ issuesLoading ? '加载中…' : '刷新问题' }}</button>
      </div>

      <div class="issue-filters">
        <div class="search-box issue-search">
          <span aria-hidden="true">⌕</span>
          <input v-model.trim="issueFilters.keyword" aria-label="搜索治理问题" placeholder="搜索标题或摘要" @keyup.enter="applyIssueFilters" />
          <button v-if="issueFilters.keyword" aria-label="清空搜索" @click="issueFilters.keyword = ''; applyIssueFilters()">×</button>
        </div>
        <select v-model="issueFilters.status" class="select compact-select" aria-label="按处理状态筛选" @change="applyIssueFilters">
          <option value="">全部状态</option><option value="open">待确认</option><option value="confirmed">已确认</option><option value="resolved">已解决</option><option value="ignored">已忽略</option>
        </select>
        <select v-model="issueFilters.type" class="select compact-select type-select" aria-label="按问题类型筛选" @change="applyIssueFilters">
          <option value="">全部类型</option><option v-for="type in issueTypes" :key="type" :value="type">{{ issueTypeLabel(type) }}</option>
        </select>
        <select v-model="issueFilters.severity" class="select compact-select" aria-label="按严重度筛选" @change="applyIssueFilters">
          <option value="">全部等级</option><option value="critical">严重</option><option value="warning">警告</option><option value="info">提示</option>
        </select>
        <button class="btn btn-ghost btn-sm" @click="resetIssueFilters">重置</button>
      </div>

      <div v-if="issuesLoading" class="issue-skeletons"><div v-for="i in 4" :key="i" class="issue-skeleton"></div></div>
      <div v-else-if="issues.list?.length" class="issue-list">
        <article v-for="issue in issues.list" :key="issue.id" class="issue-card" :class="[`severity-${issue.severity}`, { expanded: isExpanded(issue.id) }]">
          <div class="severity-rail" aria-hidden="true"></div>
          <div class="issue-main">
            <div class="issue-topline">
              <div class="issue-tags">
                <span class="badge" :class="severityBadge(issue.severity)">{{ severityLabel(issue.severity) }}</span>
                <span class="badge badge-neutral">{{ issueTypeLabel(issue.type) }}</span>
                <span class="badge" :class="issueStatusBadge(issue.status)">{{ issueStatusLabel(issue.status) }}</span>
              </div>
              <span class="issue-time">最近发现 {{ formatDate(issue.lastSeenAt) }}</span>
            </div>
            <button class="issue-title-button" :aria-expanded="isExpanded(issue.id)" @click="toggleIssue(issue.id)">
              <span>
                <strong>{{ issue.title }}</strong>
                <small>{{ issue.summary }}</small>
              </span>
              <i aria-hidden="true">⌄</i>
            </button>
            <div class="issue-footline">
              <span>出现 {{ issue.occurrenceCount || 1 }} 次</span>
              <span v-if="issue.reviewer">最近审核：{{ issue.reviewer }} · {{ formatDate(issue.reviewedAt) }}</span>
              <div class="issue-actions">
                <template v-if="['open', 'confirmed'].includes(issue.status)">
                  <button v-if="issue.status === 'open'" class="btn btn-ghost btn-sm" @click="openReview(issue, 'confirmed')">确认问题</button>
                  <button class="btn btn-success btn-sm" @click="openReview(issue, 'resolved')">标记解决</button>
                  <button class="btn btn-ghost btn-sm" @click="openReview(issue, 'ignored')">忽略</button>
                </template>
                <button v-else class="btn btn-ghost btn-sm" @click="openReview(issue, 'open')">重新打开</button>
              </div>
            </div>

            <div v-if="isExpanded(issue.id)" class="evidence-panel">
              <div class="evidence-head">
                <div><span class="evidence-mark">EVIDENCE</span><strong>扫描证据</strong></div>
                <span v-if="issue.evidence?.disclaimer" class="evidence-disclaimer">{{ issue.evidence.disclaimer }}</span>
              </div>

              <template v-if="issue.evidence?.matches?.length">
                <div v-if="issue.evidence.sharedScope?.length" class="scope-tags">
                  <span>共同范围</span><b v-for="scope in issue.evidence.sharedScope" :key="scope">{{ scope }}</b>
                </div>
                <div v-for="(match, matchIndex) in issue.evidence.matches" :key="matchIndex" class="comparison-block">
                  <div class="comparison-index">证据组 {{ matchIndex + 1 }} <span>相似度 {{ formatPercent(match.similarity) }}</span></div>
                  <div class="comparison-grid">
                    <div class="evidence-side left-side">
                      <div class="evidence-doc"><span>A</span><strong>{{ match.left?.docName || '文档 A' }}</strong></div>
                      <small>{{ match.left?.section || '未标注章节' }}</small>
                      <blockquote>{{ match.left?.excerpt || '无摘录' }}</blockquote>
                      <div v-if="match.left?.threshold" class="threshold">阈值 <b>{{ match.left.threshold.value }}{{ match.left.threshold.unit }}</b></div>
                    </div>
                    <div class="compare-divider"><span>VS</span></div>
                    <div class="evidence-side right-side">
                      <div class="evidence-doc"><span>B</span><strong>{{ match.right?.docName || '文档 B' }}</strong></div>
                      <small>{{ match.right?.section || '未标注章节' }}</small>
                      <blockquote>{{ match.right?.excerpt || '无摘录' }}</blockquote>
                      <div v-if="match.right?.threshold" class="threshold">阈值 <b>{{ match.right.threshold.value }}{{ match.right.threshold.unit }}</b></div>
                    </div>
                  </div>
                  <p class="comparison-explain">{{ match.explanation }}</p>
                </div>
              </template>

              <div v-else class="evidence-detail-grid">
                <div v-if="issue.evidence?.docName" class="evidence-detail"><span>文档</span><strong>{{ issue.evidence.docName }}</strong></div>
                <div v-if="issue.evidence?.effectiveAt" class="evidence-detail"><span>计划生效</span><strong>{{ formatDate(issue.evidence.effectiveAt) }}</strong></div>
                <div v-if="issue.evidence?.expiresAt" class="evidence-detail"><span>失效时间</span><strong>{{ formatDate(issue.evidence.expiresAt) }}</strong></div>
                <div v-if="issue.evidence?.nextReviewAt" class="evidence-detail"><span>计划复审</span><strong>{{ formatDate(issue.evidence.nextReviewAt) }}</strong></div>
                <div v-if="issue.evidence?.overdueDays != null" class="evidence-detail"><span>逾期天数</span><strong>{{ issue.evidence.overdueDays }} 天</strong></div>
                <div v-if="issue.evidence?.warningDays != null" class="evidence-detail"><span>预警窗口</span><strong>{{ issue.evidence.warningDays }} 天</strong></div>
                <div v-if="issue.evidence?.missingFields?.length" class="evidence-detail full-detail">
                  <span>缺失字段</span><div class="missing-tags"><b v-for="field in issue.evidence.missingFields" :key="field">{{ missingFieldLabel(field) }}</b></div>
                </div>
                <div v-if="issue.evidence?.explanation" class="evidence-detail full-detail"><span>判断依据</span><p>{{ issue.evidence.explanation }}</p></div>
              </div>

              <div v-if="issue.reviewNote" class="review-note"><span>审核说明</span><p>{{ issue.reviewNote }}</p></div>
            </div>
          </div>
        </article>
      </div>
      <div v-else class="empty-state issue-empty"><span>✓</span><strong>当前筛选下没有治理问题</strong><p>可以调整筛选条件，或发起一次新的知识扫描。</p><button class="btn btn-primary btn-sm" @click="openScan">发起扫描</button></div>

      <div class="pagination">
        <span>共 {{ issues.total || 0 }} 个问题</span>
        <button class="btn btn-ghost btn-sm" :disabled="issuePage <= 1 || issuesLoading" @click="changeIssuePage(issuePage - 1)">上一页</button>
        <b>{{ issuePage }} / {{ issueTotalPages }}</b>
        <button class="btn btn-ghost btn-sm" :disabled="issuePage >= issueTotalPages || issuesLoading" @click="changeIssuePage(issuePage + 1)">下一页</button>
      </div>
    </section>

    <div v-if="profileModal.show" class="modal-bg" @click.self="closeProfile">
      <div class="modal governance-modal profile-modal" role="dialog" aria-modal="true" aria-labelledby="profile-title">
        <div class="modal-head">
          <div><span class="modal-kicker">DOCUMENT PROFILE</span><strong id="profile-title">{{ profileModal.doc?.docName || '治理档案' }}</strong></div>
          <button class="icon-btn" aria-label="关闭治理档案" @click="closeProfile">×</button>
        </div>
        <div v-if="profileModal.loading" class="loading">正在读取治理档案…</div>
        <div v-else-if="profileModal.loadFailed" class="modal-scroll profile-form">
          <div class="form-error">{{ profileError }}</div>
          <div class="modal-actions">
            <button type="button" class="btn btn-ghost" @click="closeProfile">取消</button>
            <button type="button" class="btn btn-primary" @click="openProfile(profileModal.doc)">重新读取</button>
          </div>
        </div>
        <form v-else class="modal-scroll profile-form" @submit.prevent="submitProfile">
          <div class="profile-status-strip">
            <div><span>当前治理状态</span><b class="badge" :class="effectiveStateBadge(profileModal.effectiveState)">{{ effectiveStateLabel(profileModal.effectiveState) }}</b></div>
            <div v-if="profileModal.missingFields.length" class="profile-missing">仍缺 {{ profileModal.missingFields.length }} 项：{{ profileModal.missingFields.map(missingFieldLabel).join('、') }}</div>
            <div v-else class="profile-complete">档案字段完整</div>
          </div>

          <fieldset>
            <legend><span>01</span> 责任归属与适用边界</legend>
            <div class="form-grid">
              <label class="field"><span class="field-label">责任人</span><input v-model.trim="profileForm.owner" class="input" maxlength="64" placeholder="例如：变电检修一班 / 张工" /></label>
              <label class="field"><span class="field-label">适用区域</span><input v-model.trim="profileForm.applicableRegion" class="input" maxlength="256" placeholder="例如：华东区域 220kV 变电站" /></label>
            </div>
          </fieldset>

          <fieldset>
            <legend><span>02</span> 生效与失效策略</legend>
            <div class="form-grid">
              <label class="field"><span class="field-label">生效时间</span><input v-model="profileForm.effectiveAt" class="input" type="datetime-local" /></label>
              <label class="field" :class="{ disabled: profileForm.isPermanent }"><span class="field-label">失效时间</span><input v-model="profileForm.expiresAt" class="input" type="datetime-local" :disabled="profileForm.isPermanent" /></label>
            </div>
            <label class="switch-row"><input v-model="profileForm.isPermanent" type="checkbox" @change="onPermanentChange" /><span class="switch-control"></span><span><strong>永久有效</strong><small>启用后将清空失效时间，仍可按复审周期持续检查。</small></span></label>
          </fieldset>

          <fieldset>
            <legend><span>03</span> 复审机制</legend>
            <div class="form-grid">
              <label class="field"><span class="field-label">复审周期（天）</span><input v-model.number="profileForm.reviewIntervalDays" class="input" type="number" min="1" max="3650" placeholder="例如：365" /></label>
              <label class="field"><span class="field-label">下次复审时间</span><input v-model="profileForm.nextReviewAt" class="input" type="datetime-local" /><small class="field-help">填写周期后，未指定日期时将由系统自动计算。</small></label>
            </div>
          </fieldset>

          <fieldset>
            <legend><span>04</span> 版本身份</legend>
            <div class="form-grid">
              <label class="field"><span class="field-label">版本标识</span><input v-model.trim="profileForm.versionLabel" class="input" maxlength="64" placeholder="例如：Q/GDW 10248.1-2025" /></label>
              <label class="field"><span class="field-label">版本状态</span><select v-model="profileForm.versionStatus" class="select"><option value="">请选择</option><option value="draft">草稿</option><option value="active">现行有效</option><option value="superseded">已被替代</option><option value="withdrawn">已撤回</option></select></label>
            </div>
            <div v-if="['superseded', 'withdrawn'].includes(profileForm.versionStatus)" class="blocking-notice">该状态会阻止文档进入最终检索结果，请确认版本关系无误。</div>
          </fieldset>

          <div v-if="profileError" class="form-error">{{ profileError }}</div>
          <div class="modal-actions">
            <button type="button" class="btn btn-ghost" @click="closeProfile">取消</button>
            <button type="submit" class="btn btn-primary" :disabled="profileModal.saving || profileModal.loadFailed">{{ profileModal.saving ? '保存中…' : '保存治理档案' }}</button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="scanModal.show" class="modal-bg" @click.self="closeScan">
      <div class="modal governance-modal scan-modal" role="dialog" aria-modal="true" aria-labelledby="scan-title">
        <div class="modal-head">
          <div><span class="modal-kicker">GOVERNANCE SCAN</span><strong id="scan-title">发起知识治理扫描</strong></div>
          <button class="icon-btn" aria-label="关闭扫描设置" @click="closeScan">×</button>
        </div>
        <form class="modal-scroll scan-form" @submit.prevent="submitScan">
          <div class="scan-safety"><span>只读扫描</span><p>扫描器只创建问题线索，不修改文档内容、版本状态或检索配置。</p></div>
          <fieldset>
            <legend>扫描范围</legend>
            <label class="radio-card" :class="{ selected: scanForm.scope === 'all' }"><input v-model="scanForm.scope" type="radio" value="all" /><span><strong>全部知识文档</strong><small>按最大文档数，从最近上传的文档开始检查</small></span></label>
            <label class="radio-card" :class="{ selected: scanForm.scope === 'selected', disabled: !selectedDocIds.length }"><input v-model="scanForm.scope" type="radio" value="selected" :disabled="!selectedDocIds.length" /><span><strong>当前选中的 {{ selectedDocIds.length }} 份文档</strong><small>{{ selectedDocIds.length ? '仅检查指定文档，适合定向复核' : '请先在治理档案中选择文档' }}</small></span></label>
          </fieldset>
          <div class="form-grid scan-grid">
            <label class="field"><span class="field-label">到期预警窗口（天）</span><input v-model.number="scanForm.expiryWarningDays" class="input" type="number" min="1" max="365" required /><small class="field-help">进入该窗口的文档会生成“即将失效”问题。</small></label>
            <label class="field"><span class="field-label">最大扫描文档数</span><input v-model.number="scanForm.maxDocuments" class="input" type="number" min="1" max="500" required /></label>
            <label class="field"><span class="field-label">单文档最大分块数</span><input v-model.number="scanForm.maxChunksPerDocument" class="input" type="number" min="1" max="500" required /><small class="field-help">仅影响冲突规则的证据采样范围。</small></label>
          </div>
          <label class="switch-row conflict-switch"><input v-model="scanForm.includeConflicts" type="checkbox" /><span class="switch-control"></span><span><strong>检测相反规定与数值阈值冲突</strong><small>基于共同设备/主题及可解释规则生成候选证据，需人工判定。</small></span></label>
          <div v-if="scanError" class="form-error">{{ scanError }}</div>
          <div class="modal-actions">
            <button type="button" class="btn btn-ghost" @click="closeScan">取消</button>
            <button type="submit" class="btn btn-primary" :disabled="scanModal.running">{{ scanModal.running ? '正在提交…' : '开始扫描' }}</button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="reviewModal.show" class="modal-bg" @click.self="closeReview">
      <div class="modal governance-modal review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title">
        <div class="modal-head">
          <div><span class="modal-kicker">HUMAN REVIEW</span><strong id="review-title">{{ reviewActionLabel(reviewModal.status) }}</strong></div>
          <button class="icon-btn" aria-label="关闭审核" @click="closeReview">×</button>
        </div>
        <form class="modal-scroll review-form" @submit.prevent="submitReview">
          <div class="review-target"><span class="badge" :class="severityBadge(reviewModal.issue?.severity)">{{ severityLabel(reviewModal.issue?.severity) }}</span><strong>{{ reviewModal.issue?.title }}</strong><p>{{ reviewModal.issue?.summary }}</p></div>
          <label class="field"><span class="field-label">审核说明 <b v-if="['resolved', 'ignored'].includes(reviewModal.status)">必填</b></span><textarea v-model.trim="reviewModal.note" class="input review-textarea" maxlength="2000" :placeholder="reviewPlaceholder(reviewModal.status)" :required="['resolved', 'ignored'].includes(reviewModal.status)"></textarea><small class="field-help">{{ reviewModal.note.length }}/2000</small></label>
          <div v-if="reviewError" class="form-error">{{ reviewError }}</div>
          <div class="modal-actions">
            <button type="button" class="btn btn-ghost" @click="closeReview">取消</button>
            <button type="submit" class="btn" :class="reviewModal.status === 'resolved' ? 'btn-success' : 'btn-primary'" :disabled="reviewModal.saving">{{ reviewModal.saving ? '提交中…' : reviewActionLabel(reviewModal.status) }}</button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="toastState.message" class="toast governance-toast" :class="toastState.type" role="status">{{ toastState.message }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  getGovernedDocuments,
  getKnowledgeProfile,
  saveKnowledgeProfile,
  runKnowledgeGovernanceScan,
  getKnowledgeGovernanceScan,
  getKnowledgeIssues,
  getKnowledgeIssueStats,
  reviewKnowledgeIssue,
} from '../api'

const lifecycleStages = [
  { title: '责任归属', desc: '责任人与适用范围' },
  { title: '有效窗口', desc: '生效及失效时间' },
  { title: '定期复审', desc: '周期性校验内容' },
  { title: '版本退出', desc: '替代、撤回可追溯' },
]
const issueTypes = ['metadata_missing', 'not_yet_effective', 'expired', 'expiring', 'review_due', 'conflict_negation', 'conflict_threshold']
const pageSize = 12

const activeTab = ref('documents')
const refreshing = ref(false)
const stats = ref({ documents: 0, governedDocuments: 0, metadataCoverage: 0, byStatus: {}, byType: {} })
const docs = ref({ total: 0, list: [] })
const docsLoading = ref(false)
const docKeyword = ref('')
const docPage = ref(1)
const selectedDocIds = ref([])
const issues = ref({ total: 0, list: [] })
const issuesLoading = ref(false)
const issuePage = ref(1)
const expandedIssueIds = ref(new Set())
const issueFilters = reactive({ keyword: '', status: '', type: '', severity: '' })
const lastScan = ref(null)

const profileModal = reactive({ show: false, loading: false, saving: false, loadFailed: false, doc: null, effectiveState: 'metadata_incomplete', missingFields: [] })
const profileForm = reactive({ owner: '', applicableRegion: '', effectiveAt: '', expiresAt: '', isPermanent: false, reviewIntervalDays: '', nextReviewAt: '', versionLabel: '', versionStatus: '' })
const profileError = ref('')
const scanModal = reactive({ show: false, running: false })
const scanForm = reactive({ scope: 'all', expiryWarningDays: 30, includeConflicts: true, maxDocuments: 100, maxChunksPerDocument: 80 })
const scanError = ref('')
const reviewModal = reactive({ show: false, saving: false, issue: null, status: 'confirmed', note: '' })
const reviewError = ref('')
const toastState = reactive({ message: '', type: 'success' })
let toastTimer = null
let scanPollTimer = null

const coveragePercent = computed(() => Math.round(Math.max(0, Math.min(1, Number(stats.value.metadataCoverage) || 0)) * 100))
const unresolvedCount = computed(() => (stats.value.byStatus?.open || 0) + (stats.value.byStatus?.confirmed || 0))
const conflictCount = computed(() => (stats.value.byType?.conflict_negation || 0) + (stats.value.byType?.conflict_threshold || 0))
const docTotalPages = computed(() => Math.max(1, Math.ceil((docs.value.total || 0) / pageSize)))
const issueTotalPages = computed(() => Math.max(1, Math.ceil((issues.value.total || 0) / pageSize)))
const allCurrentDocsSelected = computed(() => docs.value.list?.length > 0 && docs.value.list.every((doc) => selectedDocIds.value.includes(doc.docId)))

function notify(message, type = 'success') {
  toastState.message = message
  toastState.type = type
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastState.message = '' }, 2600)
}

function unwrapBiz(response) {
  if (!response || response.code !== 200) throw new Error(response?.message || '请求失败')
  return response.data
}

function errorMessage(error, fallback) {
  return error?.response?.data?.message || error?.message || fallback
}

async function loadStats() {
  try {
    const response = await getKnowledgeIssueStats()
    stats.value = { ...stats.value, ...(unwrapBiz(response) || {}) }
  } catch (error) {
    notify(errorMessage(error, '治理统计加载失败'), 'error')
  }
}

async function loadDocuments() {
  docsLoading.value = true
  try {
    const response = await getGovernedDocuments({ keyword: docKeyword.value, page: docPage.value, size: pageSize })
    docs.value = unwrapBiz(response) || { total: 0, list: [] }
    if (docPage.value > docTotalPages.value) {
      docPage.value = docTotalPages.value
      await loadDocuments()
    }
  } catch (error) {
    docs.value = { total: 0, list: [] }
    notify(errorMessage(error, '文档治理档案加载失败'), 'error')
  } finally {
    docsLoading.value = false
  }
}

async function loadIssues() {
  issuesLoading.value = true
  try {
    const response = await getKnowledgeIssues({
      keyword: issueFilters.keyword,
      status: issueFilters.status,
      issueType: issueFilters.type,
      severity: issueFilters.severity,
      page: issuePage.value,
      size: pageSize,
    })
    issues.value = unwrapBiz(response) || { total: 0, list: [] }
    if (issuePage.value > issueTotalPages.value) {
      issuePage.value = issueTotalPages.value
      await loadIssues()
    }
  } catch (error) {
    issues.value = { total: 0, list: [] }
    notify(errorMessage(error, '治理问题加载失败'), 'error')
  } finally {
    issuesLoading.value = false
  }
}

async function refreshAll() {
  refreshing.value = true
  await Promise.allSettled([loadStats(), loadDocuments(), loadIssues()])
  refreshing.value = false
}

function searchDocuments() { docPage.value = 1; loadDocuments() }
function changeDocPage(page) { docPage.value = Math.max(1, Math.min(docTotalPages.value, page)); loadDocuments() }
function changeIssuePage(page) { issuePage.value = Math.max(1, Math.min(issueTotalPages.value, page)); loadIssues() }
function applyIssueFilters() { issuePage.value = 1; loadIssues() }
function resetIssueFilters() {
  Object.assign(issueFilters, { keyword: '', status: '', type: '', severity: '' })
  applyIssueFilters()
}

function toggleCurrentDocs(checked) {
  const currentIds = docs.value.list.map((doc) => doc.docId)
  if (checked) selectedDocIds.value = [...new Set([...selectedDocIds.value, ...currentIds])]
  else selectedDocIds.value = selectedDocIds.value.filter((id) => !currentIds.includes(id))
}

function resetProfileForm() {
  Object.assign(profileForm, { owner: '', applicableRegion: '', effectiveAt: '', expiresAt: '', isPermanent: false, reviewIntervalDays: '', nextReviewAt: '', versionLabel: '', versionStatus: '' })
}

function toLocalInput(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

function toApiDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toISOString()
}

function fillProfileForm(metadata) {
  const meta = metadata || {}
  Object.assign(profileForm, {
    owner: meta.owner || '', applicableRegion: meta.applicableRegion || '',
    effectiveAt: toLocalInput(meta.effectiveAt), expiresAt: toLocalInput(meta.expiresAt),
    isPermanent: Boolean(meta.isPermanent), reviewIntervalDays: meta.reviewIntervalDays ?? '',
    nextReviewAt: toLocalInput(meta.nextReviewAt), versionLabel: meta.versionLabel || '', versionStatus: meta.versionStatus || '',
  })
}

async function openProfile(doc) {
  profileModal.show = true
  profileModal.loading = true
  profileModal.doc = doc
  profileModal.effectiveState = doc.effectiveState || 'metadata_incomplete'
  profileModal.missingFields = [...(doc.missingFields || [])]
  profileModal.loadFailed = false
  profileError.value = ''
  resetProfileForm()
  fillProfileForm(doc.metadata)
  try {
    const response = await getKnowledgeProfile(doc.docId)
    const data = unwrapBiz(response) || {}
    profileModal.doc = { ...doc, ...(data.document || {}) }
    profileModal.effectiveState = data.effectiveState || 'metadata_incomplete'
    profileModal.missingFields = data.missingFields || []
    fillProfileForm(data.metadata)
  } catch (error) {
    profileModal.loadFailed = true
    profileError.value = errorMessage(error, '治理档案读取失败')
  } finally {
    profileModal.loading = false
  }
}

function closeProfile() { if (!profileModal.saving) profileModal.show = false }
function onPermanentChange() { if (profileForm.isPermanent) profileForm.expiresAt = '' }

async function submitProfile() {
  profileError.value = ''
  if (profileModal.loadFailed) {
    profileError.value = '治理档案读取失败，请重新打开后再保存'
    return
  }
  if (profileForm.effectiveAt && profileForm.expiresAt && new Date(profileForm.effectiveAt) > new Date(profileForm.expiresAt)) {
    profileError.value = '生效时间不能晚于失效时间'
    return
  }
  profileModal.saving = true
  try {
    const payload = {
      owner: profileForm.owner || null,
      applicableRegion: profileForm.applicableRegion || null,
      effectiveAt: toApiDate(profileForm.effectiveAt),
      expiresAt: profileForm.isPermanent ? null : toApiDate(profileForm.expiresAt),
      isPermanent: Boolean(profileForm.isPermanent),
      reviewIntervalDays: profileForm.reviewIntervalDays === '' ? null : Number(profileForm.reviewIntervalDays),
      nextReviewAt: toApiDate(profileForm.nextReviewAt),
      versionLabel: profileForm.versionLabel || null,
      versionStatus: profileForm.versionStatus || null,
    }
    const response = await saveKnowledgeProfile(profileModal.doc.docId, payload)
    const data = unwrapBiz(response) || {}
    profileModal.effectiveState = data.effectiveState || profileModal.effectiveState
    profileModal.missingFields = data.missingFields || []
    notify(data.created ? '治理档案已建立' : '治理档案已更新')
    profileModal.show = false
    await Promise.allSettled([loadDocuments(), loadStats()])
  } catch (error) {
    profileError.value = errorMessage(error, '治理档案保存失败')
  } finally {
    profileModal.saving = false
  }
}

function openScan(selectedOnly = false) {
  scanError.value = ''
  scanForm.scope = selectedOnly && selectedDocIds.value.length ? 'selected' : 'all'
  scanModal.show = true
}
function closeScan() { if (!scanModal.running) scanModal.show = false }

function clearScanPolling() {
  if (!scanPollTimer) return
  clearInterval(scanPollTimer)
  scanPollTimer = null
}

function startScanPolling(taskId) {
  clearScanPolling()
  if (!taskId) return
  let ticks = 0
  scanPollTimer = setInterval(async () => {
    ticks += 1
    try {
      const data = unwrapBiz(await getKnowledgeGovernanceScan(taskId))
      lastScan.value = { mode: 'queued', task: data }
      if (!data.done && ticks < 80) return
      clearScanPolling()
      if (!data.done) {
        notify('治理扫描仍在后台执行，可稍后刷新查看', 'error')
      } else if (data.status === 'succeeded') {
        lastScan.value = { mode: 'synchronous', ...(data.result || {}), task: data }
        notify('治理扫描已完成')
        await Promise.allSettled([loadStats(), loadIssues()])
      } else if (['failed', 'dead'].includes(data.status)) {
        lastScan.value = { mode: 'failed', task: data, error: data.lastError || '' }
        notify(data.lastError || '治理扫描执行失败', 'error')
      }
    } catch (error) {
      if (ticks >= 3) {
        clearScanPolling()
        notify(errorMessage(error, '治理扫描状态读取失败'), 'error')
      }
    }
  }, 2500)
}

async function submitScan() {
  scanError.value = ''
  if (scanForm.scope === 'selected' && !selectedDocIds.value.length) {
    scanError.value = '请至少选择一份文档'
    return
  }
  scanModal.running = true
  try {
    const response = await runKnowledgeGovernanceScan({
      expiryWarningDays: Number(scanForm.expiryWarningDays),
      includeConflicts: Boolean(scanForm.includeConflicts),
      maxDocuments: Number(scanForm.maxDocuments),
      maxChunksPerDocument: Number(scanForm.maxChunksPerDocument),
      documentIds: scanForm.scope === 'selected' ? selectedDocIds.value : [],
    })
    lastScan.value = unwrapBiz(response) || { mode: 'queued' }
    scanModal.show = false
    notify(lastScan.value.mode === 'queued' ? '治理扫描已进入任务队列' : '治理扫描已完成')
    const taskId = lastScan.value.task?.taskId || lastScan.value.task?.id
    if (lastScan.value.mode === 'queued') startScanPolling(taskId)
    else await Promise.allSettled([loadStats(), loadIssues()])
  } catch (error) {
    scanError.value = errorMessage(error, '治理扫描提交失败')
  } finally {
    scanModal.running = false
  }
}

function isExpanded(id) { return expandedIssueIds.value.has(id) }
function toggleIssue(id) {
  const next = new Set(expandedIssueIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  expandedIssueIds.value = next
}

function openReview(issue, status) {
  reviewModal.issue = issue
  reviewModal.status = status
  reviewModal.note = ''
  reviewError.value = ''
  reviewModal.show = true
}
function closeReview() { if (!reviewModal.saving) reviewModal.show = false }

async function submitReview() {
  reviewError.value = ''
  if (['resolved', 'ignored'].includes(reviewModal.status) && !reviewModal.note) {
    reviewError.value = '解决或忽略问题时必须填写审核说明'
    return
  }
  reviewModal.saving = true
  try {
    unwrapBiz(await reviewKnowledgeIssue(reviewModal.issue.id, reviewModal.status, reviewModal.note))
    notify(`${reviewActionLabel(reviewModal.status)}成功`)
    reviewModal.show = false
    await Promise.allSettled([loadIssues(), loadStats()])
  } catch (error) {
    reviewError.value = errorMessage(error, '审核状态更新失败')
  } finally {
    reviewModal.saving = false
  }
}

function effectiveStateLabel(state) {
  return ({ metadata_incomplete: '档案不完整', draft: '草稿', active: '现行有效', superseded: '已被替代', withdrawn: '已撤回', not_yet_effective: '尚未生效', expired: '已失效' })[state] || state || '未知'
}
function effectiveStateBadge(state) {
  return ({ active: 'badge-success', draft: 'badge-neutral', metadata_incomplete: 'badge-warning', not_yet_effective: 'badge-info', expired: 'badge-danger', superseded: 'badge-neutral', withdrawn: 'badge-danger' })[state] || 'badge-neutral'
}
function documentStatusLabel(status) { return ({ pending: '待解析', parsed: '已解析', vectorized: '已向量化' })[status] || status || '未知状态' }
function versionStatusLabel(status) { return ({ draft: '草稿', active: '现行有效', superseded: '已被替代', withdrawn: '已撤回' })[status] || '状态待标注' }
function missingFieldLabel(field) {
  return ({ owner: '责任人', applicableRegion: '适用区域', effectiveAt: '生效时间', expiryPolicy: '失效策略', reviewPolicy: '复审策略', versionLabel: '版本标识', versionStatus: '版本状态' })[field] || field
}
function issueTypeLabel(type) {
  return ({ metadata_missing: '元数据缺失', not_yet_effective: '尚未生效', expired: '文档失效', expiring: '即将失效', review_due: '复审逾期', conflict_negation: '相反规定', conflict_threshold: '阈值冲突' })[type] || type || '未知类型'
}
function severityLabel(severity) { return ({ critical: '严重', warning: '警告', info: '提示' })[severity] || severity || '未知' }
function severityBadge(severity) { return ({ critical: 'badge-danger', warning: 'badge-warning', info: 'badge-info' })[severity] || 'badge-neutral' }
function issueStatusLabel(status) { return ({ open: '待确认', confirmed: '已确认', resolved: '已解决', ignored: '已忽略' })[status] || status }
function issueStatusBadge(status) { return ({ open: 'badge-warning', confirmed: 'badge-primary', resolved: 'badge-success', ignored: 'badge-neutral' })[status] || 'badge-neutral' }
function reviewActionLabel(status) { return ({ confirmed: '确认问题', resolved: '标记解决', ignored: '忽略问题', open: '重新打开' })[status] || '更新状态' }
function reviewPlaceholder(status) {
  return ({ confirmed: '可补充初步判断、责任人或后续计划（选填）', resolved: '说明采取了什么措施、依据哪个版本或为何已不再存在', ignored: '说明为何该线索不适用或属于可接受差异', open: '说明重新打开的原因（选填）' })[status] || '填写审核说明'
}
function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ')
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
function formatPercent(value) { return `${Math.round((Number(value) || 0) * 100)}%` }
function shortId(value) { const text = String(value || '待分配'); return text.length > 12 ? `…${text.slice(-10)}` : text }
function taskStatusLabel(status) {
  return ({ queued: '排队中', running: '运行中', failed: '等待重试', dead: '已失败', succeeded: '已完成' })[status] || status || '排队中'
}

function handleEscape(event) {
  if (event.key !== 'Escape') return
  if (reviewModal.show) closeReview()
  else if (scanModal.show) closeScan()
  else if (profileModal.show) closeProfile()
}

onMounted(() => {
  document.addEventListener('keydown', handleEscape)
  refreshAll()
})
onUnmounted(() => {
  document.removeEventListener('keydown', handleEscape)
  clearTimeout(toastTimer)
  clearScanPolling()
})
</script>

<style scoped>
.governance-page { --governance-blue: #2563eb; --governance-cyan: #0891b2; --governance-ink: #172554; padding: 12px; min-width: 0; }

.governance-hero { position: relative; display: grid; grid-template-columns: minmax(360px, 1.05fr) minmax(480px, .95fr); gap: 42px; overflow: hidden; margin-bottom: 14px; padding: 26px 30px; border: 1px solid var(--border); border-radius: var(--radius-xl); background: linear-gradient(125deg, var(--surface) 0%, var(--surface) 58%, var(--primary-soft) 100%); box-shadow: var(--shadow-sm); }
.governance-hero::after { content: ''; position: absolute; width: 280px; height: 280px; right: -130px; top: -180px; border: 1px solid color-mix(in srgb, var(--primary) 22%, transparent); border-radius: 50%; box-shadow: 0 0 0 34px color-mix(in srgb, var(--primary) 3%, transparent), 0 0 0 68px color-mix(in srgb, var(--primary) 2%, transparent); pointer-events: none; }
.hero-copy { position: relative; z-index: 1; }
.hero-eyebrow { display: flex; align-items: center; gap: 8px; color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.pulse-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); box-shadow: 0 0 0 4px var(--success-soft); }
.hero-copy h1 { margin: 9px 0 6px; max-width: 680px; color: var(--text); font-size: clamp(23px, 2.2vw, 32px); line-height: 1.25; letter-spacing: -.025em; }
.hero-copy p { max-width: 680px; margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.75; }
.hero-actions { display: flex; gap: 9px; margin-top: 18px; }

.lifecycle { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(4, 1fr); align-items: start; gap: 10px; padding-top: 18px; }
.lifecycle-line { position: absolute; top: 33px; left: 10%; right: 10%; height: 2px; background: linear-gradient(90deg, var(--primary) 0 70%, var(--border) 70%); }
.lifecycle-stage { position: relative; display: flex; flex-direction: column; align-items: center; gap: 10px; min-width: 0; text-align: center; }
.stage-node { display: grid; place-items: center; width: 31px; height: 31px; z-index: 1; border: 2px solid var(--border); border-radius: 50%; background: var(--surface); color: var(--text-soft); font-size: 11px; font-weight: 800; box-shadow: 0 0 0 5px color-mix(in srgb, var(--surface) 82%, transparent); }
.stage-node.active { border-color: var(--primary); background: var(--primary); color: #fff; }
.lifecycle-stage strong { display: block; color: var(--text); font-size: 12px; }
.lifecycle-stage small { display: block; margin-top: 2px; color: var(--text-soft); font-size: 10px; line-height: 1.35; }

.governance-stats { grid-template-columns: repeat(4, 1fr); }
.governance-stats .stat { min-height: 118px; padding: 16px 18px; }
.stat-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.stat-kicker { color: var(--text-muted); font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.stat-symbol { display: grid; place-items: center; width: 25px; height: 25px; border-radius: 7px; background: var(--primary-soft); color: var(--primary); font-size: 11px; font-weight: 800; }
.warning-symbol { background: var(--warning-soft); color: var(--warning); }.danger-symbol { background: var(--danger-soft); color: var(--danger); }
.governance-stats .stat-val { margin-top: 8px; font-size: 27px; }.warning-text { color: var(--warning); }.danger-text { color: var(--danger); }
.coverage-value { color: var(--primary); font-size: 18px; font-weight: 800; }
.coverage-track { height: 8px; overflow: hidden; margin: 16px 0 12px; border-radius: 999px; background: var(--surface-3); }
.coverage-track span { display: block; height: 100%; min-width: 3px; border-radius: inherit; background: linear-gradient(90deg, var(--primary), var(--accent)); transition: width .35s ease; }

.scan-result { display: flex; align-items: center; gap: 12px; margin: -2px 0 14px; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-sm); }
.scan-result.queued { border-left: 3px solid var(--info); }.scan-result.complete { border-left: 3px solid var(--success); }.scan-result.failed { border-left: 3px solid var(--danger); }
.scan-result-icon { display: grid; place-items: center; width: 31px; height: 31px; flex: 0 0 auto; border-radius: 50%; background: var(--info-soft); color: var(--info); font-weight: 800; }
.scan-result.complete .scan-result-icon { background: var(--success-soft); color: var(--success); }.scan-result.failed .scan-result-icon { background: var(--danger-soft); color: var(--danger); }
.scan-result-copy { display: flex; flex: 1; flex-direction: column; min-width: 0; }.scan-result-copy strong { font-size: 13px; }.scan-result-copy span { color: var(--text-muted); font-size: 12px; }

.workspace-tabs { display: inline-flex; gap: 5px; padding: 4px; margin-bottom: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.workspace-tab { display: flex; align-items: center; gap: 10px; padding: 8px 13px; border: 0; border-radius: 7px; background: transparent; color: var(--text-muted); cursor: pointer; font-family: inherit; font-size: 13px; font-weight: 650; }
.workspace-tab b { min-width: 22px; padding: 1px 6px; border-radius: 999px; background: var(--surface-2); color: var(--text-soft); font-size: 10px; }
.workspace-tab.active { background: var(--primary-soft); color: var(--primary); }.workspace-tab.active b { background: var(--primary); color: #fff; }
.workspace-tab:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

.workspace-card { padding: 0; overflow: hidden; }
.workspace-head { margin: 0; padding: 18px 20px 14px; border-bottom: 1px solid var(--border-soft); }
.workspace-head .card-title { font-size: 16px; }.workspace-head .card-desc { margin-top: 3px; }
.toolbar { display: flex; align-items: center; gap: 8px; }
.search-box { display: flex; align-items: center; width: 250px; height: 35px; padding: 0 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-soft); transition: border-color .15s, box-shadow .15s; }
.search-box:focus-within { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-soft); }
.search-box input { flex: 1; min-width: 0; padding: 0 8px; border: 0; outline: 0; background: transparent; color: var(--text); font-family: inherit; font-size: 12px; }
.search-box input::placeholder { color: var(--text-soft); }.search-box button { padding: 2px; border: 0; background: transparent; color: var(--text-soft); cursor: pointer; }
.selection-bar { display: flex; align-items: center; gap: 8px; padding: 9px 20px; border-bottom: 1px solid var(--primary-soft-2); background: var(--primary-soft); color: var(--primary); font-size: 12px; }.selection-bar span { margin-right: auto; }
.table-wrap { overflow-x: auto; }.governance-table { min-width: 1080px; }.governance-table th { padding-top: 11px; padding-bottom: 11px; }.governance-table td { padding-top: 12px; padding-bottom: 12px; }
.check-col { width: 42px; text-align: center !important; }.action-col { width: 110px; text-align: right !important; }
.doc-title { max-width: 300px; overflow: hidden; color: var(--text); font-size: 13px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.cell-meta { display: flex; align-items: center; gap: 4px; margin-top: 3px; color: var(--text-soft); font-size: 10px; }.scope-copy { max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.state-badge i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.owner-line { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }.owner-avatar { display: grid; place-items: center; width: 21px; height: 21px; border-radius: 6px; background: var(--primary-soft); color: var(--primary); font-size: 10px; }
.missing-copy { color: var(--warning); font-size: 11px; }.version-line { font-size: 12px; font-weight: 650; }.missing-summary { display: flex; align-items: center; gap: 6px; }.missing-preview { max-width: 130px; color: var(--text-muted); font-size: 10px; }
.skeleton-row { height: 26px; border-radius: 5px; background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 50%, var(--surface-2) 75%); background-size: 200% 100%; animation: shimmer 1.3s infinite; }
@keyframes shimmer { to { background-position: -200% 0; } }
.pagination { display: flex; align-items: center; justify-content: flex-end; gap: 9px; padding: 12px 20px; border-top: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; }.pagination span { margin-right: auto; }.pagination b { color: var(--text); font-size: 11px; }
.empty-state { display: flex; align-items: center; flex-direction: column; padding: 52px 16px; text-align: center; }.empty-state > span { display: grid; place-items: center; width: 40px; height: 40px; margin-bottom: 10px; border-radius: 50%; background: var(--surface-2); color: var(--text-soft); font-size: 19px; }.empty-state strong { font-size: 13px; }.empty-state p { margin: 4px 0 0; color: var(--text-soft); font-size: 11px; }

.issue-filters { display: flex; align-items: center; gap: 8px; padding: 12px 20px; border-bottom: 1px solid var(--border-soft); background: var(--surface-2); }.issue-search { flex: 1; max-width: 320px; }.compact-select { width: 120px; padding-top: 7px; padding-bottom: 7px; }.type-select { width: 150px; }
.issue-list { display: flex; flex-direction: column; gap: 10px; padding: 14px; background: var(--bg); }
.issue-card { position: relative; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-sm); }.severity-rail { position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--info); }.severity-warning .severity-rail { background: var(--warning); }.severity-critical .severity-rail { background: var(--danger); }
.issue-main { padding: 14px 16px 13px 18px; }.issue-topline, .issue-footline { display: flex; align-items: center; gap: 12px; }.issue-tags { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }.issue-time { margin-left: auto; color: var(--text-soft); font-size: 10px; }
.issue-title-button { display: flex; align-items: center; justify-content: space-between; gap: 16px; width: 100%; padding: 10px 0 9px; border: 0; background: transparent; color: var(--text); cursor: pointer; text-align: left; font-family: inherit; }.issue-title-button strong { display: block; font-size: 14px; line-height: 1.4; }.issue-title-button small { display: block; margin-top: 3px; color: var(--text-muted); font-size: 11px; font-weight: 400; line-height: 1.6; }.issue-title-button i { color: var(--text-soft); font-size: 20px; font-style: normal; transition: transform .2s; }.issue-card.expanded .issue-title-button i { transform: rotate(180deg); }
.issue-footline { min-height: 29px; color: var(--text-soft); font-size: 10px; }.issue-footline > span + span::before { content: '·'; margin-right: 10px; }.issue-actions { display: flex; gap: 6px; margin-left: auto; }
.evidence-panel { margin-top: 12px; padding-top: 14px; border-top: 1px dashed var(--border); }.evidence-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }.evidence-head > div { display: flex; align-items: center; gap: 8px; }.evidence-head strong { font-size: 12px; }.evidence-mark { padding: 2px 5px; border: 1px solid var(--border); border-radius: 3px; color: var(--primary); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 8px; font-weight: 800; letter-spacing: .12em; }.evidence-disclaimer { max-width: 55%; color: var(--text-soft); font-size: 10px; text-align: right; }
.scope-tags { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; color: var(--text-muted); font-size: 10px; }.scope-tags b, .missing-tags b { padding: 2px 7px; border-radius: 999px; background: var(--primary-soft); color: var(--primary); font-size: 9px; font-weight: 650; }
.comparison-block { overflow: hidden; margin-top: 8px; border: 1px solid var(--border); border-radius: 9px; }.comparison-index { display: flex; justify-content: space-between; padding: 6px 10px; border-bottom: 1px solid var(--border); background: var(--surface-2); color: var(--text-muted); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; }.comparison-grid { display: grid; grid-template-columns: 1fr 42px 1fr; }.evidence-side { min-width: 0; padding: 12px; }.evidence-doc { display: flex; align-items: center; gap: 7px; }.evidence-doc span { display: grid; place-items: center; width: 19px; height: 19px; border-radius: 5px; background: var(--primary-soft); color: var(--primary); font-size: 9px; font-weight: 800; }.right-side .evidence-doc span { background: var(--warning-soft); color: var(--warning); }.evidence-doc strong { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.evidence-side > small { display: block; margin: 5px 0; color: var(--text-soft); font-size: 9px; }.evidence-side blockquote { margin: 0; padding: 8px 9px; border-left: 2px solid var(--primary-soft-2); background: var(--surface-2); color: var(--text-muted); font-size: 10px; line-height: 1.65; }.right-side blockquote { border-left-color: var(--warning); }.compare-divider { display: grid; place-items: center; position: relative; color: var(--text-soft); font-size: 8px; font-weight: 800; }.compare-divider::before { content: ''; position: absolute; top: 0; bottom: 0; left: 50%; border-left: 1px dashed var(--border); }.compare-divider span { z-index: 1; padding: 4px; border-radius: 50%; background: var(--surface); }.threshold { margin-top: 6px; color: var(--text-muted); font-size: 9px; }.threshold b { color: var(--danger); font-size: 11px; }.comparison-explain { margin: 0; padding: 7px 10px; border-top: 1px solid var(--border-soft); background: var(--surface-2); color: var(--text-muted); font-size: 9px; }
.evidence-detail-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }.evidence-detail { padding: 10px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--surface-2); }.evidence-detail > span { display: block; margin-bottom: 3px; color: var(--text-soft); font-size: 9px; }.evidence-detail > strong { font-size: 11px; }.full-detail { grid-column: 1 / -1; }.full-detail p { margin: 0; color: var(--text-muted); font-size: 10px; }.missing-tags { display: flex; flex-wrap: wrap; gap: 5px; }.review-note { margin-top: 9px; padding: 9px 11px; border-left: 2px solid var(--primary); background: var(--primary-soft); }.review-note span { color: var(--primary); font-size: 9px; font-weight: 700; }.review-note p { margin: 2px 0 0; color: var(--text-muted); font-size: 10px; }
.issue-skeletons { display: grid; gap: 10px; padding: 14px; background: var(--bg); }.issue-skeleton { height: 116px; border: 1px solid var(--border); border-radius: var(--radius); background: linear-gradient(90deg, var(--surface) 25%, var(--surface-2) 50%, var(--surface) 75%); background-size: 200% 100%; animation: shimmer 1.3s infinite; }.issue-empty { min-height: 300px; }.issue-empty .btn { margin-top: 14px; }

.governance-modal { height: auto; max-height: calc(100vh - 40px); border: 1px solid var(--border); }.governance-modal .modal-head { flex: 0 0 auto; padding: 16px 20px; }.governance-modal .modal-head > div { display: flex; flex-direction: column; }.governance-modal .modal-head strong { margin-top: 2px; font-size: 15px; }.modal-kicker { color: var(--primary); font-size: 8px; font-weight: 800; letter-spacing: .14em; }.modal-scroll { overflow-y: auto; padding: 18px 20px 20px; }.profile-modal { max-width: 760px; }.scan-modal { max-width: 650px; }.review-modal { max-width: 570px; }
.profile-status-strip { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-2); }.profile-status-strip > div:first-child { display: flex; align-items: center; gap: 8px; }.profile-status-strip > div:first-child > span { color: var(--text-muted); font-size: 10px; }.profile-missing, .profile-complete { margin-left: auto; font-size: 10px; }.profile-missing { color: var(--warning); }.profile-complete { color: var(--success); }
.profile-form fieldset, .scan-form fieldset { margin: 0 0 16px; padding: 0; border: 0; }.profile-form legend, .scan-form legend { width: 100%; margin-bottom: 10px; color: var(--text); font-size: 12px; font-weight: 700; }.profile-form legend span { display: inline-grid; place-items: center; width: 23px; height: 19px; margin-right: 6px; border-radius: 5px; background: var(--primary-soft); color: var(--primary); font-size: 8px; letter-spacing: .04em; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.profile-form .field, .scan-form .field { margin-bottom: 0; }.field.disabled { opacity: .55; }.field-help { display: block; margin-top: 4px; color: var(--text-soft); font-size: 9px; }.field-label b { color: var(--danger); }
.switch-row { display: flex; align-items: flex-start; gap: 9px; margin-top: 9px; cursor: pointer; }.switch-row > input { position: absolute; opacity: 0; pointer-events: none; }.switch-control { position: relative; width: 34px; height: 19px; flex: 0 0 auto; margin-top: 1px; border-radius: 999px; background: var(--surface-3); transition: background .15s; }.switch-control::after { content: ''; position: absolute; top: 3px; left: 3px; width: 13px; height: 13px; border-radius: 50%; background: #fff; box-shadow: var(--shadow-sm); transition: transform .15s; }.switch-row > input:checked + .switch-control { background: var(--primary); }.switch-row > input:checked + .switch-control::after { transform: translateX(15px); }.switch-row > input:focus-visible + .switch-control { box-shadow: 0 0 0 3px var(--primary-soft); }.switch-row strong, .switch-row small { display: block; }.switch-row strong { font-size: 11px; }.switch-row small { margin-top: 1px; color: var(--text-soft); font-size: 9px; }
.blocking-notice, .form-error { margin-top: 9px; padding: 8px 10px; border-radius: 7px; font-size: 10px; }.blocking-notice { border: 1px solid color-mix(in srgb, var(--warning) 30%, var(--border)); background: var(--warning-soft); color: var(--warning); }.form-error { border: 1px solid color-mix(in srgb, var(--danger) 30%, var(--border)); background: var(--danger-soft); color: var(--danger); }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; padding-top: 14px; border-top: 1px solid var(--border); }
.scan-safety { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 16px; padding: 10px 11px; border-radius: 8px; background: var(--success-soft); }.scan-safety span { flex: 0 0 auto; padding: 2px 6px; border-radius: 4px; background: var(--success); color: #fff; font-size: 8px; font-weight: 800; }.scan-safety p { margin: 0; color: var(--success); font-size: 10px; }
.radio-card { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; padding: 10px 11px; border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }.radio-card.selected { border-color: var(--primary); background: var(--primary-soft); }.radio-card.disabled { opacity: .55; cursor: not-allowed; }.radio-card input { accent-color: var(--primary); }.radio-card strong, .radio-card small { display: block; }.radio-card strong { font-size: 11px; }.radio-card small { color: var(--text-soft); font-size: 9px; }.scan-grid { grid-template-columns: repeat(3, 1fr); }.conflict-switch { margin-top: 15px; padding: 11px; border: 1px solid var(--border); border-radius: 8px; }
.review-target { margin-bottom: 15px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }.review-target > strong { display: block; margin-top: 8px; font-size: 12px; }.review-target p { margin: 3px 0 0; color: var(--text-muted); font-size: 10px; line-height: 1.6; }.review-textarea { min-height: 115px; resize: vertical; line-height: 1.65; }
.governance-toast { display: flex; align-items: center; gap: 8px; }.governance-toast.error { background: var(--danger); color: #fff; }.governance-toast.success { background: var(--text); color: var(--surface); }

html.dark .governance-hero { background: linear-gradient(125deg, var(--surface) 0%, var(--surface) 58%, color-mix(in srgb, var(--primary-soft-2) 55%, var(--surface)) 100%); }
html.dark .stage-node { box-shadow: 0 0 0 5px color-mix(in srgb, var(--surface) 90%, transparent); }

@media (max-width: 1180px) {
  .governance-hero { grid-template-columns: 1fr; gap: 18px; }.lifecycle { padding: 8px 10px 0; }.lifecycle-line { top: 23px; }
  .governance-stats { grid-template-columns: repeat(2, 1fr); }.issue-filters { flex-wrap: wrap; }.issue-search { max-width: none; min-width: 260px; }
}
@media (max-width: 760px) {
  .governance-page { padding: 4px; }.governance-hero { padding: 20px 16px; border-radius: var(--radius-lg); }.hero-actions { flex-wrap: wrap; }
  .lifecycle { grid-template-columns: 1fr 1fr; gap: 12px; padding: 4px 0 0; }.lifecycle-line { display: none; }.lifecycle-stage { align-items: flex-start; flex-direction: row; text-align: left; }.stage-node { flex: 0 0 auto; }
  .governance-stats { grid-template-columns: 1fr 1fr; gap: 8px; }.governance-stats .stat { min-height: 105px; padding: 13px; }.governance-stats .stat-val { font-size: 24px; }
  .workspace-tabs { width: 100%; }.workspace-tab { flex: 1; justify-content: center; }.workspace-head { align-items: stretch; }.toolbar, .search-box { width: 100%; }.toolbar .search-box { flex: 1; }
  .selection-bar { flex-wrap: wrap; }.selection-bar span { width: 100%; }.issue-filters { align-items: stretch; flex-direction: column; }.issue-search, .compact-select { width: 100%; max-width: none; }
  .issue-topline { align-items: flex-start; }.issue-time { text-align: right; }.issue-footline { align-items: flex-start; flex-direction: column; }.issue-actions { width: 100%; margin-left: 0; flex-wrap: wrap; }.issue-actions .btn { flex: 1; }
  .evidence-head { flex-direction: column; }.evidence-disclaimer { max-width: none; text-align: left; }.comparison-grid { grid-template-columns: 1fr; }.compare-divider { min-height: 28px; }.compare-divider::before { inset: 50% 0 auto; border-top: 1px dashed var(--border); border-left: 0; }.evidence-detail-grid { grid-template-columns: 1fr 1fr; }
  .form-grid, .scan-grid { grid-template-columns: 1fr; }.profile-status-strip { align-items: flex-start; flex-direction: column; }.profile-missing, .profile-complete { margin-left: 0; }.governance-modal { max-height: calc(100vh - 16px); }
}
@media (max-width: 480px) {
  .governance-stats { grid-template-columns: 1fr; }.lifecycle { grid-template-columns: 1fr; }.scan-result { align-items: flex-start; }.scan-result .icon-btn { width: 28px; height: 28px; }
  .workspace-card { border-radius: 8px; }.workspace-head, .issue-filters { padding-left: 12px; padding-right: 12px; }.issue-list { padding: 8px; }.issue-main { padding: 12px 11px 11px 14px; }.issue-time { display: none; }
  .evidence-detail-grid { grid-template-columns: 1fr; }.pagination { padding: 10px 12px; }.pagination span { display: none; }.pagination { justify-content: center; }
}
@media (prefers-reduced-motion: reduce) {
  .coverage-track span, .issue-title-button i, .switch-control, .switch-control::after { transition: none; }.skeleton-row, .issue-skeleton { animation: none; }
}
</style>
