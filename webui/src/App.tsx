import {
  Activity,
  AlertTriangle,
  Archive,
  BrainCircuit,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  DatabaseZap,
  ExternalLink,
  FileSearch,
  FolderInput,
  Globe2,
  History,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Paperclip,
  Play,
  RefreshCw,
  Search,
  SearchCheck,
  Send,
  ShieldCheck,
  UserRound,
  X,
  XCircle,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  ApiError,
  type Artifact,
  type AuditCase,
  type AuditPreview,
  type Batch,
  type CaseDetail,
  type ComparisonDifference,
  type Issue,
  type IdentityFieldConflict,
  type Job,
  type M4Results,
  type Memory,
  type SemanticIdentityDecision,
  type ToolTrace,
  type VerificationReport,
} from './api'
import { AcceptanceBench } from './AcceptanceBench'
import { CaseResultsView } from './CaseResultsView'
import { displayUrl } from './url'

type View = 'batches' | 'issues' | 'cases' | 'memories' | 'acceptance'
type Toast = { tone: 'success' | 'error' | 'conflict'; message: string } | null
type RuntimeInfo = {
  ok: boolean
  environment: 'development' | 'acceptance' | 'production'
  database: string
}

const environmentLabels: Record<RuntimeInfo['environment'], string> = {
  development: '开发',
  acceptance: '验收',
  production: '正式',
}

const statusLabels: Record<string, string> = {
  queued: '排队中',
  running: '处理中',
  waiting_human: '待人工',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  candidate: '候选',
  active: '生效',
  deprecated: '失效',
  merged: '已合并',
  accepted: '通过',
  rejected: '打回',
  insufficient: '证据不足',
  pending: '待开始',
  done: '已完成',
  partial: '部分完成',
  evidence_incomplete: '取证不完整',
  evidence_complete_differences: '取证完成，发现业务差异，待人工复核',
  evidence_complete_matched: '取证完成，未发现业务差异，待人工复核',
}

const toolLabels: Record<string, string> = {
  parse_spreadsheet: '读取提交名单',
  fetch_web_page: '核验网页',
  verify_page_image_roster: '识别网页名单图片',
  search_official_award: '搜索官方来源',
  extract_search_document: '提取网页正文',
  collect_spreadsheet_attachments: '汇总网页附件',
  download_evidence: '下载证据文件',
  inspect_pdf: '检查 PDF 文件',
  extract_pdf_text: '提取 PDF 文字与表格',
  parse_pdf_text: '读取 PDF 文字',
  render_pdf_pages: '渲染 PDF 页面',
  ocr_image: '本地 OCR 识别',
  vision_extract_roster: '视觉模型结构化名单',
  ocr_pdf_pages: '识别扫描 PDF',
  review_asset_relations: '模型判断资产与业务范围',
  review_identity_candidates: '模型裁决身份候选',
  'langgraph:prepare_case': 'Graph · 准备案件',
  'langgraph:retrieve_memory': 'Graph · 检索案例记忆',
  'langgraph:semantic_plan': 'Graph · 模型规划',
  'langgraph:execute_tool': 'Graph · 执行工具',
  'langgraph:observe': 'Graph · 记录观察',
  'langgraph:assess_extraction_quality': 'Graph · 评估抽取质量',
  'langgraph:semantic_route_assets': 'Graph · 语义路由资产',
  'langgraph:build_exact_matches_and_candidates': 'Graph · 精确匹配与候选生成',
  'langgraph:semantic_adjudicate_identities': 'Graph · 身份语义消歧',
  'langgraph:deterministic_verify': 'Graph · 确定性校验',
  'langgraph:persist': 'Graph · 持久化结果',
  'langgraph:waiting_human': 'Graph · 等待人工复核',
}

const reasonLabels: Record<string, string> = {
  target_mismatch: '奖项名称不一致',
  year_mismatch: '年份不一致',
  secondary_source_only: '只有次级来源，需人工确认',
  source_authority_unknown: '来源权威性无法确认',
  coverage_incomplete: '名单覆盖不足',
  evidence_conflict: '官方来源与提交材料口径冲突',
  agent_requested_manual: '系统已转入人工复核',
  zero_overlap: '来源名单与提交名单没有匹配记录',
  count_mismatch: '名单数量与业务口径不一致',
  repeated_tool_call_blocked: '候选来源处理完毕后停止重复操作',
  repeated_tool_call_redirected_to_candidate: '重复操作已自动改为核验下一候选来源',
  official_search_candidates_ready: '已找到候选官方来源',
  secondary_evidence_requires_official_corroboration: '次级来源完整，已继续核验官方来源',
  pending_page_images_processed_before_recovery: '已先识别给定网页中的名单图片',
  verifier_requires_manual: '系统要求人工作最终判断',
  year_match: '名单年份一致',
  observed_award_names_empty: '来源中未识别到目标奖项名称',
}

const candidateStatusLabels: Record<string, string> = {
  pending: '尚未访问',
  succeeded: '已访问并取得结果',
  failed: '访问失败',
  skipped: '已跳过',
}

const sourceLabels: Record<string, string> = {
  official: '官方主来源',
  official_primary: '官方主来源',
  official_secondary: '官方关联来源',
  institutional_secondary: '机构次级来源',
  publisher_secondary: '媒体或发布者次级来源',
  secondary: '次级来源',
  unknown: '来源级别未知',
}

const decisionLabels: Record<string, string> = {
  accepted: '人工确认符合要求',
  rejected: '人工确认不符合要求',
  insufficient: '证据不足，暂无法判断',
}

function Status({ value }: { value: string }) {
  return <span className={`status status-${value}`}>{statusLabels[value] || value}</span>
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function matchLabel(value: unknown) {
  if (value === true || value === 'yes') return '一致'
  if (value === false || value === 'no') return '不一致'
  return '尚未确认'
}

function actionLabel(value: unknown) {
  if (value === 'accept_evidence') return '证据可提交人工确认'
  if (value === 'supplement') return '需要补充证据'
  return '需要人工判断'
}

function traceFacts(trace: ToolTrace): Record<string, unknown> {
  return asRecord(trace.output_summary?.verification_facts)
}

function traceUrl(trace: ToolTrace): string {
  const inputUrl = trace.input_summary?.url
  const pageUrl = trace.input_summary?.page_url
  const outputUrl = trace.output_summary?.source_url
  const value = typeof inputUrl === 'string'
    ? inputUrl
    : typeof pageUrl === 'string'
      ? pageUrl
      : typeof outputUrl === 'string'
        ? outputUrl
        : ''
  return /^https?:\/\//i.test(value) ? value : ''
}

function fileName(value: string): string {
  return value.split(/[\\/]/).filter(Boolean).at(-1) || value
}

function displayIdentityPart(value: string, index: number): string {
  return value.split(';').map((part) => part.trim()).filter(Boolean)[index] || '-'
}

function isFieldConflict(value: string): boolean {
  return value.startsWith('identity_field_conflict:')
}

function scopeDisplayName(scopeKey: string, fallback: string): string {
  const match = scopeKey.match(/(?:^|\u001f|u001f)scope:XMLB=([^\u001f]+?)(?=u001f|$)/)
  if (!match?.[1]) return fallback
  return match[1].trim().replace(/项目$/, '') || fallback
}

function traceAttachments(trace: ToolTrace) {
  const facts = traceFacts(trace)
  const names = asStrings(facts.attachment_names)
  const urls = asStrings(facts.attachment_urls)
  return names.map((name, index) => ({ name, url: urls[index] || '' }))
}

function sourceLabel(value: unknown) {
  return sourceLabels[String(value || '')] || '来源级别未记录'
}

function reasonLabel(code: string) {
  return reasonLabels[code] || '其他待核验事项'
}

function problemLabel(problem: string) {
  if (problem === 'Automatic evidence processing stopped before evidence acceptance.') {
    return '系统在证据达到可采纳条件前停止了自动取证，尚未形成可用于终审的证据链。'
  }
  if (problem.includes('observed_award_names') && problem.includes('zero_overlap')) {
    return '来源中未识别到目标奖项名称，且来源名单与提交名单没有匹配记录，无法证明该来源属于本案。'
  }
  return problem
}

function traceFindings(trace: ToolTrace): string[] {
  const facts = traceFacts(trace)
  const findings: string[] = []
  if ('award_name_match' in facts) findings.push(`奖项名称：${matchLabel(facts.award_name_match)}`)
  if ('year_match' in facts) findings.push(`年份：${matchLabel(facts.year_match)}`)
  const observed = typeof facts.observed_count === 'number' ? facts.observed_count : null
  const expected = typeof facts.expected_count === 'number' ? facts.expected_count : null
  const coverageLabel = facts.next_evidence_stage === 'spreadsheet_processing'
    ? '网页正文覆盖'
    : '名单覆盖'
  if (observed !== null && expected !== null) {
    findings.push(`${coverageLabel}：${observed} / ${expected}`)
  } else if ('coverage_complete' in facts) {
    findings.push(`${coverageLabel}：${matchLabel(facts.coverage_complete)}`)
  }
  if ('source_level' in facts) findings.push(sourceLabel(facts.source_level))
  if (typeof facts.candidate_count === 'number') findings.push(`找到 ${facts.candidate_count} 个候选网址`)
  if (facts.relationship_confirmed === true) findings.push('姓名与群体名称对应关系：已取得补证')
  return findings
}

function traceRelationship(trace: ToolTrace) {
  const facts = traceFacts(trace)
  return {
    confirmed: facts.relationship_confirmed === true,
    terms: asStrings(facts.relationship_terms),
    summary: typeof facts.relationship_summary === 'string' ? facts.relationship_summary : '',
  }
}

function traceGroupedMatches(trace: ToolTrace): string[][] {
  return asStrings(traceFacts(trace).split_matched_items)
    .map((item) => item.split(/[;；、，,]+/).map((name) => name.trim()).filter(Boolean))
    .filter((names) => names.length > 1)
}

function traceDifferences(trace: ToolTrace) {
  const facts = traceFacts(trace)
  return {
    missing: asStrings(facts.missing_items),
    extra: asStrings(facts.extra_items),
    unresolved: asStrings(facts.unresolved_items),
    note: typeof facts.comparison_note === 'string' ? facts.comparison_note : '',
  }
}

function traceProblem(trace: ToolTrace): string {
  if (!trace.ok && trace.error_code === 'TOOL_BUDGET_EXCEEDED') {
    return '本步骤因自动取证预算达到上限而停止，不能据此判断该来源或提交材料不合格。'
  }
  if (!trace.ok) return `本步骤失败：${trace.error_code || '未取得有效结果'}`
  const facts = traceFacts(trace)
  if (facts.relationship_confirmed === true) return '该来源支持姓名与群体名额的对应关系；最终业务口径仍需人工确认。'
  if (facts.award_name_match === false) return '网页标题或正文与本案奖项不一致。'
  if (facts.year_match === false) return '网页内容与本案年份不一致。'
  if (facts.next_evidence_stage === 'spreadsheet_processing') {
    const observed = typeof facts.observed_count === 'number' ? facts.observed_count : '?'
    const expected = typeof facts.expected_count === 'number' ? facts.expected_count : '?'
    const count = typeof facts.attachment_count === 'number' ? facts.attachment_count : '?'
    return `网页正文覆盖 ${observed} / ${expected} 条；已发现 ${count} 个附件，附件核验完成前不能判定来源整体未覆盖。`
  }
  if (facts.coverage_complete === false) {
    const observed = typeof facts.observed_count === 'number' ? facts.observed_count : '?'
    const expected = typeof facts.expected_count === 'number' ? facts.expected_count : '?'
    return `该来源只覆盖 ${observed} / ${expected} 条，不能证明名单完整。`
  }
  if (facts.coverage_complete === true) return '该来源中的提交名单覆盖完整。'
  return ''
}

function Empty({ label }: { label: string }) {
  return (
    <div className="empty-state">
      <FileSearch size={24} />
      <span>{label}</span>
    </div>
  )
}

function IdentityConflictTable({ scope }: {
  scope: {
    role_label: string
    scope_key: string
    identity_conflicts: IdentityFieldConflict[]
  }
}) {
  const scopeName = scopeDisplayName(scope.scope_key, scope.role_label)
  return <section className="identity-conflict-table">
    <div className="identity-conflict-heading">
      <strong>编号字段冲突</strong>
      <span>题名一致，但提交编号与官网编号不同；这不是“来源未匹配”。</span>
    </div>
    <div className="identity-conflict-scroll">
      <table>
        <thead><tr><th>奖项</th><th>提交编号</th><th>官网编号</th><th>共同题名</th><th>官网来源</th></tr></thead>
        <tbody>{scope.identity_conflicts.map((conflict, index) => {
          const submittedTitle = displayIdentityPart(conflict.submitted, 1)
          const sourceTitle = displayIdentityPart(conflict.source, 1)
          const title = submittedTitle !== '-' ? submittedTitle : sourceTitle
          return <tr key={`${conflict.submitted}-${conflict.source}-${index}`}>
            <td>{scopeName}</td>
            <td>{displayIdentityPart(conflict.submitted, 0)}</td>
            <td>{displayIdentityPart(conflict.source, 0)}</td>
            <td>{title}</td>
            <td>{/^https?:\/\//i.test(conflict.source_url) ? <a href={conflict.source_url} target="_blank" rel="noreferrer noopener">查看官网 <ExternalLink size={13} /></a> : '-'}</td>
          </tr>
        })}</tbody>
      </table>
    </div>
  </section>
}

function SemanticIdentityTable({ decisions }: { decisions: SemanticIdentityDecision[] }) {
  return <section className="identity-conflict-table semantic-identity-table">
    <div className="identity-conflict-heading">
      <strong>身份语义消歧</strong>
      <span>本地仅生成候选；以下结论由模型判断并通过一对一与来源锚点校验。</span>
    </div>
    <div className="identity-conflict-scroll"><table>
      <thead><tr><th>提交身份</th><th>官网身份</th><th>结论</th><th>置信度</th><th>理由与锚点</th></tr></thead>
      <tbody>{decisions.map((decision) => <tr key={decision.candidate_id}>
        <td>{decision.submitted}</td>
        <td>{decision.source}</td>
        <td>{decision.decision === 'same_identity' ? '同一身份' : decision.decision === 'field_conflict' ? '字段冲突' : decision.decision === 'different' ? '不同身份' : '无法确认'}</td>
        <td>{Math.round(decision.confidence * 100)}%</td>
        <td><span>{decision.reason}</span><small>{decision.source_anchor || '未记录锚点'}</small>{/^https?:\/\//i.test(decision.source_url) && <a href={decision.source_url} target="_blank" rel="noreferrer noopener">查看来源 <ExternalLink size={13} /></a>}</td>
      </tr>)}</tbody>
    </table></div>
  </section>
}

function comparisonDifferenceLabel(value: ComparisonDifference['difference_type']) {
  if (value === 'field_conflict') return '字段冲突'
  if (value === 'missing_from_source') return '提交有，官网未找到'
  return '官网有，提交未提供'
}

function ScopeDifferenceTable({ scope }: {
  scope: {
    role_label: string
    scope_key: string
    comparison_differences: ComparisonDifference[]
  }
}) {
  const scopeName = scopeDisplayName(scope.scope_key, scope.role_label)
  return <section className="identity-conflict-table scope-difference-table">
    <div className="identity-conflict-heading">
      <strong>逐条差异</strong>
      <span>字段冲突会并列双方身份；无法可靠配对的缺失或多出记录不会强行配对。</span>
    </div>
    <div className="identity-conflict-scroll">
      <table>
        <thead><tr><th>审核范围</th><th>差异类型</th><th>提交身份</th><th>官网身份</th><th>冲突字段</th><th>官网来源</th></tr></thead>
        <tbody>{scope.comparison_differences.map((difference, index) => <tr key={`${difference.difference_type}-${difference.submitted}-${difference.source}-${index}`}>
          <td>{scopeName}</td>
          <td>{comparisonDifferenceLabel(difference.difference_type)}</td>
          <td>{difference.submitted || '提交未提供此身份'}</td>
          <td>{difference.source || '官网未找到此身份'}</td>
          <td>{difference.difference_type === 'field_conflict' ? difference.fields : '-'}</td>
          <td>{difference.source_urls.length > 0 ? <div className="difference-source-links">{difference.source_urls.map((url, sourceIndex) => <a href={url} target="_blank" rel="noreferrer noopener" key={`${url}-${sourceIndex}`}>官网来源 {sourceIndex + 1}<ExternalLink size={13} /></a>)}</div> : '-'}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>
}

function formatTime(value: string) {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 19)
}

function App() {
  const [view, setView] = useState<View>('batches')
  const [reviewer, setReviewer] = useState(() => localStorage.getItem('award-reviewer') || '')
  const [batches, setBatches] = useState<Batch[]>([])
  const [issues, setIssues] = useState<Issue[]>([])
  const [cases, setCases] = useState<AuditCase[]>([])
  const [memories, setMemories] = useState<Memory[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState<Toast>(null)
  const [eventsOnline, setEventsOnline] = useState(false)
  const [selectedCase, setSelectedCase] = useState<CaseDetail | null>(null)
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null)
  const [jobsOpen, setJobsOpen] = useState(false)
  const [auditPreview, setAuditPreview] = useState<{
    batch: Batch
    preview: AuditPreview
  } | null>(null)
  const [m4Results, setM4Results] = useState<{
    batch: Batch
    results: M4Results
  } | null>(null)
  const [loadingM4BatchId, setLoadingM4BatchId] = useState<number | null>(null)
  const [previewingBatchId, setPreviewingBatchId] = useState<number | null>(null)
  const [confirmingAudit, setConfirmingAudit] = useState(false)
  const [importFiles, setImportFiles] = useState<File[]>([])
  const [importInputKey, setImportInputKey] = useState(0)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [filters, setFilters] = useState({ severity: '', file: '', resource: '', field: '' })

  const notify = useCallback((next: Toast) => {
    setToast(next)
    window.setTimeout(() => setToast(null), 4200)
  }, [])

  const handleError = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 409) {
      notify({ tone: 'conflict', message: '数据已被其他复核人更新，请刷新后重试。' })
    } else {
      notify({ tone: 'error', message: error instanceof Error ? error.message : '请求失败' })
    }
  }, [notify])

  const loadAll = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const [batchData, issueData, caseData, memoryData, jobData, runtimeData] = await Promise.all([
        api<{ batches: Batch[] }>('/api/batches'),
        api<{ issues: Issue[] }>('/api/issues'),
        api<{ cases: AuditCase[] }>('/api/audit-cases'),
        api<{ memories: Memory[] }>('/api/memories'),
        api<{ jobs: Job[] }>('/api/jobs'),
        api<RuntimeInfo>('/api/health'),
      ])
      setBatches(batchData.batches)
      setIssues(issueData.issues)
      setCases(caseData.cases)
      setMemories(memoryData.memories)
      setJobs(jobData.jobs)
      setRuntime(runtimeData)
    } catch (error) {
      handleError(error)
    } finally {
      setLoading(false)
    }
  }, [handleError])

  useEffect(() => {
    if (view === 'acceptance') {
      setLoading(false)
      return
    }
    void loadAll()
  }, [loadAll, view])

  useEffect(() => {
    if (view === 'acceptance') return undefined
    const events = new EventSource('/api/events')
    events.onopen = () => setEventsOnline(true)
    events.onerror = () => setEventsOnline(false)
    events.onmessage = () => void loadAll(true)
    const refresh = () => void loadAll(true)
    ;['job.queued', 'job.running', 'job.progress', 'job.completed', 'job.failed', 'human.action']
      .forEach((name) => events.addEventListener(name, refresh))
    return () => events.close()
  }, [loadAll, view])

  useEffect(() => {
    localStorage.setItem('award-reviewer', reviewer)
  }, [reviewer])

  const requireReviewer = () => {
    if (reviewer.trim()) return true
    notify({ tone: 'error', message: '请先填写复核人。' })
    return false
  }

  const openCase = async (caseId: number) => {
    try {
      const data = await api<{ case: CaseDetail }>(`/api/audit-cases/${caseId}`)
      setSelectedCase(data.case)
      setSelectedArtifact(null)
    } catch (error) { handleError(error) }
  }

  const previewAudit = async (batch: Batch) => {
    if (!requireReviewer()) return
    setPreviewingBatchId(batch.id)
    try {
      const preview = await api<AuditPreview>(
        `/api/batches/${batch.id}/audit/preview`, { method: 'POST' }, reviewer,
      )
      setAuditPreview({ batch, preview })
    } catch (error) {
      handleError(error)
    } finally {
      setPreviewingBatchId(null)
    }
  }

  const openM4Results = async (batch: Batch) => {
    setLoadingM4BatchId(batch.id)
    try {
      const results = await api<M4Results>(`/api/batches/${batch.id}/audit-results`)
      setM4Results({ batch, results })
    } catch (error) {
      handleError(error)
    } finally {
      setLoadingM4BatchId(null)
    }
  }

  const confirmAudit = async () => {
    if (!auditPreview || !requireReviewer()) return
    setConfirmingAudit(true)
    try {
      await api(`/api/batches/${auditPreview.batch.id}/audit`, {
        method: 'POST',
        body: JSON.stringify({ preview_digest: auditPreview.preview.preview_digest }),
      }, reviewer)
      setAuditPreview(null)
      notify({ tone: 'success', message: '联网核对任务已进入持久队列。' })
      setJobsOpen(true)
      await loadAll(true)
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setAuditPreview(null)
        notify({ tone: 'conflict', message: '预览内容已变化，请重新预览后确认。' })
      } else {
        handleError(error)
      }
    } finally {
      setConfirmingAudit(false)
    }
  }

  const startReview = async (batchId: number) => {
    if (!requireReviewer()) return
    try {
      await api(`/api/batches/${batchId}/review`, { method: 'POST' }, reviewer)
      notify({ tone: 'success', message: 'M5 语义路由与本地比较任务已进入持久队列。' })
      setJobsOpen(true)
      await loadAll(true)
    } catch (error) { handleError(error) }
  }

  const importBatch = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!requireReviewer() || importFiles.length === 0) return
    const body = new FormData()
    importFiles.forEach((file) => body.append('files', file, file.name))
    try {
      await api('/api/batches/upload', {
        method: 'POST',
        body,
      }, reviewer)
      notify({ tone: 'success', message: '导入任务已开始，完成后可在批次列表启动审核。' })
      setImportFiles([])
      setImportInputKey((value) => value + 1)
      setJobsOpen(true)
      await loadAll(true)
    } catch (error) { handleError(error) }
  }

  const promote = async (batch: Batch) => {
    if (!requireReviewer()) return
    try {
      await api(`/api/batches/${batch.id}/promote`, {
        method: 'POST', body: JSON.stringify({ expected_status: batch.status }),
      }, reviewer)
      notify({ tone: 'success', message: '入库闸门执行完成。' })
      await loadAll(true)
    } catch (error) { handleError(error) }
  }

  const submitCaseAction = async (
    action: 'supplement' | 'accepted' | 'rejected' | 'insufficient',
    text: string,
  ) => {
    if (!selectedCase || !requireReviewer() || !text.trim()) return
    const caseId = selectedCase.case_id
    try {
      if (action === 'supplement') {
        await api(`/api/audit-cases/${selectedCase.case_id}/supplement`, {
          method: 'POST',
          body: JSON.stringify({ request: text, expected_version: selectedCase.state_version }),
        }, reviewer)
      } else {
        await api(`/api/audit-cases/${selectedCase.case_id}/review`, {
          method: 'POST',
          body: JSON.stringify({
            decision: action, summary: text, expected_version: selectedCase.state_version,
          }),
        }, reviewer)
      }
      const messages = {
        supplement: '补证要求已记录，案件将重新进入取证流程。',
        accepted: '已记录“符合要求”，这是人工终审结论。',
        rejected: '已记录“不符合要求”，这是人工终审结论。',
        insufficient: '已记录“证据不足”，未发送给其他人员或系统。',
      }
      notify({ tone: 'success', message: messages[action] })
      await loadAll(true)
      await openCase(caseId)
    } catch (error) { handleError(error) }
  }

  const memoryAction = async (memory: Memory, action: 'approve' | 'deprecate' | 'merge', target?: number) => {
    if (!requireReviewer()) return
    try {
      await api(`/api/memories/${memory.memory_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ expected_version: memory.state_version, merged_into_id: target || null }),
      }, reviewer)
      notify({ tone: 'success', message: '案例记忆状态已更新。' })
      await loadAll(true)
    } catch (error) { handleError(error) }
  }

  const cancelJob = async (job: Job) => {
    if (!requireReviewer()) return
    try {
      await api(`/api/jobs/${job.job_id}/cancel`, {
        method: 'POST', body: JSON.stringify({ expected_version: job.state_version }),
      }, reviewer)
      notify({ tone: 'success', message: '任务已取消。' })
      await loadAll(true)
    } catch (error) { handleError(error) }
  }

  const filteredIssues = useMemo(() => issues.filter((issue) =>
    (!filters.severity || issue.severity === filters.severity)
    && (!filters.file || issue.file.toLowerCase().includes(filters.file.toLowerCase()))
    && (!filters.resource || issue.resource_code.includes(filters.resource))
    && (!filters.field || (issue.field_code || '').includes(filters.field)),
  ), [issues, filters])

  const totals = useMemo(() => ({
    rows: batches.reduce((sum, item) => sum + item.n_rows, 0),
    blocker: batches.reduce((sum, item) => sum + (item.issue_counts.blocker || 0), 0),
    waiting: cases.filter((item) => item.status === 'waiting_human').length,
    activeMemory: memories.filter((item) => item.status === 'active').length,
  }), [batches, cases, memories])

  const orderedCases = useMemo(() => [...cases].sort((left, right) => {
    const timeDifference = Date.parse(right.updated_at || '') - Date.parse(left.updated_at || '')
    return Number.isFinite(timeDifference) && timeDifference !== 0
      ? timeDifference
      : right.case_id - left.case_id
  }), [cases])

  useEffect(() => {
    if (view === 'cases' && !selectedCase && orderedCases.length > 0) {
      void openCase(orderedCases[0].case_id)
    }
  }, [view, selectedCase, orderedCases])

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark"><ShieldCheck size={22} /><span>评奖审查台</span></div>
        <nav aria-label="主导航">
          {([
            ['batches', LayoutDashboard, '批次'],
            ['issues', ListChecks, '问题'],
            ['cases', FileSearch, '疑难案件'],
            ['memories', BrainCircuit, '案例记忆'],
          ] as const).map(([key, Icon, label]) => (
            <button key={key} className={view === key ? 'nav-active' : ''} onClick={() => setView(key)}>
              <Icon size={18} /><span>{label}</span>
            </button>
          ))}
          <button className={view === 'acceptance' ? 'nav-active' : ''} onClick={() => setView('acceptance')}>
            <ShieldCheck size={18} /><span>验收成果</span>
          </button>
        </nav>
        <div className="sidebar-foot">
          <span className={`connection ${eventsOnline ? 'online' : ''}`} />
          {eventsOnline ? '事件流已连接' : '事件流重连中'}
        </div>
      </aside>

      <main className={view === 'acceptance' ? 'acceptance-main' : ''}>
        <header className="topbar">
          <div>
            <h1>{view === 'batches' ? '批次总览' : view === 'issues' ? '问题队列' : view === 'cases' ? '疑难审核' : '案例记忆'}</h1>
            <p>{view === 'batches' ? '确定性核查、疑难任务与入库闸门' : view === 'issues' ? '按定位与严重度复核提交问题' : view === 'cases' ? '证据、Tool 时间线与人工终审' : '候选审批、适用范围与来源追溯'}</p>
          </div>
          <div className="top-actions">
            {runtime && <div className={`runtime-badge runtime-${runtime.environment}`} title="当前运行环境与数据库">
              <DatabaseZap size={16} />
              <span><strong>{environmentLabels[runtime.environment]}</strong><small>{runtime.database}</small></span>
            </div>}
            <label className="reviewer-field">
              <UserRound size={16} />
              <input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="复核人" aria-label="复核人" />
            </label>
            <button className="icon-button" title="任务活动" onClick={() => setJobsOpen(true)}>
              <Activity size={18} /><span className="job-count">{jobs.filter((job) => ['queued', 'running'].includes(job.status)).length}</span>
            </button>
            <button className="icon-button" title="刷新" onClick={() => void loadAll()}><RefreshCw size={18} /></button>
          </div>
        </header>

        {view === 'acceptance' && <AcceptanceBench />}

        {view === 'batches' && (
          <>
            <section className="metric-strip">
              <div><span>数据行</span><strong>{totals.rows}</strong></div>
              <div><span>严重问题</span><strong className="text-danger">{totals.blocker}</strong></div>
              <div><span>待人工案件</span><strong className="text-warning">{totals.waiting}</strong></div>
              <div><span>生效记忆</span><strong className="text-success">{totals.activeMemory}</strong></div>
            </section>
            <section className="workspace-section">
              <div className="section-heading batch-heading">
                <div><h2>批次</h2><span>{batches.length} 个</span></div>
                <form className="batch-import" onSubmit={(event) => void importBatch(event)}>
                  <label className="file-picker" htmlFor="batch-files">
                    <FolderInput size={16} />选择文件
                  </label>
                  <input
                    accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    id="batch-files"
                    key={importInputKey}
                    multiple
                    onChange={(event) => setImportFiles(Array.from(event.target.files || []))}
                    type="file"
                  />
                  <span className="selected-files" title={importFiles.map((file) => file.name).join('\n')}>
                    {importFiles.length > 0 ? `已选 ${importFiles.length} 个文件` : '未选择文件'}
                  </span>
                  <button className="command primary" disabled={importFiles.length === 0} type="submit">
                    <FolderInput size={16} />导入批次
                  </button>
                </form>
              </div>
              <div className="table-wrap">
                <table className="batch-table">
                  <thead><tr><th>批次</th><th>审核进度</th><th>文件 / 行</th><th>问题</th><th>疑难案件</th><th>入库状态</th><th>操作</th></tr></thead>
                  <tbody>{batches.map((batch) => {
                    const m4Status = batch.stages.m4.status
                    const m5Status = batch.stages.m5.status
                    const activeStageJob = jobs.some((job) => (
                      job.batch_id === batch.id
                      && ['audit_batch', 'review_batch'].includes(job.kind)
                      && ['queued', 'running'].includes(job.status)
                    ))
                    const canAudit = batch.stages.local.status === 'done'
                      && !['running', 'done'].includes(m4Status)
                      && !activeStageJob
                    const canReview = ['done', 'partial'].includes(m4Status)
                      && m5Status !== 'running'
                      && (batch.case_counts.queued || 0) > 0
                      && !activeStageJob
                    const hasM4Activity = m4Status !== 'pending'
                      || Object.values(batch.stages.m4.item_counts || {}).some((count) => count > 0)
                    return <tr key={batch.id}>
                      <td><strong>{batch.name}</strong><small>#{batch.id}</small></td>
                      <td><BatchStageStepper batch={batch} /></td>
                      <td>{batch.n_files} / {batch.n_rows}</td>
                      <td><span className="text-danger">{batch.issue_counts.blocker || 0}</span> · {batch.issue_counts.format || 0} · <span className="text-warning">{batch.issue_counts.review || 0}</span></td>
                      <td>{batch.case_counts.total || 0} <small>待人 {batch.case_counts.waiting_human || 0}</small></td>
                      <td>
                        <Status value={batch.status} />
                        <small>{batch.promotion_readiness.can_promote ? '门禁已满足' : `${batch.promotion_readiness.reasons.length} 项未满足`}</small>
                      </td>
                      <td className="batch-actions">
                        <button
                          className="command"
                          title="查看 M4 当前结果、来源附件和 M5 绑定"
                          data-testid={`m4-results-${batch.id}`}
                          disabled={!hasM4Activity || loadingM4BatchId === batch.id}
                          onClick={() => void openM4Results(batch)}
                        >
                          {loadingM4BatchId === batch.id ? <LoaderCircle className="spin" size={15} /> : <ListChecks size={15} />}
                          M4 结果
                        </button>
                        <button
                          className="command secondary"
                          title="预览 M4 官方来源核对目标"
                          disabled={!canAudit || previewingBatchId === batch.id}
                          onClick={() => void previewAudit(batch)}
                        >
                          {previewingBatchId === batch.id ? <LoaderCircle className="spin" size={15} /> : <SearchCheck size={15} />}
                          联网核对
                        </button>
                        <button
                          className="command"
                          title="启动 M5 语义路由与本地比较"
                          disabled={!canReview}
                          onClick={() => void startReview(batch.id)}
                        ><Play size={15} />M5 审查</button>
                        <button
                          className="command success"
                          title={batch.promotion_readiness.reasons.join('；') || '执行入库闸门'}
                          disabled={!batch.promotion_readiness.can_promote || activeStageJob}
                          onClick={() => void promote(batch)}
                        ><DatabaseZap size={15} />入库</button>
                      </td>
                    </tr>
                  })}</tbody>
                </table>
                {!loading && batches.length === 0 && <Empty label="暂无批次" />}
              </div>
            </section>
          </>
        )}

        {view === 'issues' && (
          <section className="workspace-section">
            <div className="filterbar">
              <Search size={17} />
              <select value={filters.severity} onChange={(event) => setFilters({ ...filters, severity: event.target.value })} aria-label="严重度">
                <option value="">全部严重度</option><option value="blocker">严重</option><option value="format">格式</option><option value="review">待复核</option>
              </select>
              <input placeholder="文件" value={filters.file} onChange={(event) => setFilters({ ...filters, file: event.target.value })} />
              <input placeholder="资源项码" value={filters.resource} onChange={(event) => setFilters({ ...filters, resource: event.target.value })} />
              <input placeholder="字段" value={filters.field} onChange={(event) => setFilters({ ...filters, field: event.target.value })} />
              <span>{filteredIssues.length} 条</span>
            </div>
            <div className="table-wrap">
              <table><thead><tr><th>级别</th><th>规则</th><th>文件定位</th><th>资源项 / 字段</th><th>问题</th><th>建议</th></tr></thead>
                <tbody>{filteredIssues.map((issue, index) => <tr key={`${issue.staging_id}-${issue.rule_id}-${index}`}>
                  <td><Status value={issue.severity} /></td><td>{issue.rule_id}</td>
                  <td><strong>{issue.file}</strong><small>{issue.sheet} · 第 {issue.row_no} 行</small></td>
                  <td>{issue.resource_code}<small>{issue.field_code || '-'}</small></td>
                  <td className="wide-cell">{issue.message}</td><td>{issue.suggestion || '-'}</td>
                </tr>)}</tbody>
              </table>
              {!loading && filteredIssues.length === 0 && <Empty label="没有符合条件的问题" />}
            </div>
          </section>
        )}

        {view === 'cases' && (
          <section className="workspace-section case-workspace-section">
            <div className="case-master-detail">
              <section className="case-list-panel" aria-label="疑难案件列表">
                <div className="case-list-heading"><div><h2>疑难案件</h2><span>{cases.length} 个案件 · 按最近更新</span></div><RefreshCw size={16} /></div>
                <div className="case-list-filter">全部状态 <ChevronRight size={14} /></div>
                <div className="case-list-items">
                  {orderedCases.map((item) => <button
                    key={item.case_id}
                    type="button"
                    className={`case-list-item${selectedCase?.case_id === item.case_id ? ' case-list-item-selected' : ''}`}
                    onClick={() => void openCase(item.case_id)}
                  >
                    <div className="case-list-item-head"><strong>{item.award_name || item.resource_code}</strong><Status value={item.human_decision || item.status} /></div>
                    <small>案件 #{item.case_id} · {item.year || '年份未记录'} · {formatTime(item.updated_at)}</small>
                    <div className="case-list-item-stats"><span>匹配 <b>{item.human_decision === 'accepted' ? '已通过' : item.status === 'completed' ? '已完成' : item.status === 'waiting_human' ? '待人工' : item.status}</b></span><span>步骤 <b>{item.step_count}</b></span></div>
                    <p>{item.human_decision_summary || item.recommendation || item.trigger_codes.map(reasonLabel).join(' · ') || '暂无摘要'}</p>
                  </button>)}
                  {!loading && cases.length === 0 && <Empty label="暂无疑难案件" />}
                </div>
              </section>
              <section className="case-detail-panel" aria-label="当前案件成果详情">
                {selectedCase
                  ? <CaseResultsView caseDetail={selectedCase} onAction={submitCaseAction} />
                  : <div className="case-detail-empty"><FileSearch size={32} /><strong>选择一个疑难案件</strong><span>左侧列表保留所有案件，右侧展示选中案件的审核成果、证据和人工处理。</span></div>}
              </section>
            </div>
          </section>
        )}

        {view === 'memories' && (
          <section className="workspace-section">
            <div className="section-heading"><h2>案例记忆</h2><span>{memories.length} 条</span></div>
            <div className="table-wrap"><table><thead><tr><th>状态</th><th>分类</th><th>症状</th><th>处理方法</th><th>范围</th><th>出现 / 来源</th><th>审批</th><th></th></tr></thead>
              <tbody>{memories.map((memory) => <MemoryRow key={memory.memory_id} memory={memory} memories={memories} onAction={memoryAction} />)}</tbody></table>
              {!loading && memories.length === 0 && <Empty label="暂无案例记忆" />}
            </div>
          </section>
        )}

        {loading && <div className="loading"><LoaderCircle className="spin" size={20} />加载中</div>}
      </main>

      {selectedCase && view !== 'cases' && <CaseDrawer caseDetail={selectedCase} artifact={selectedArtifact} onArtifact={setSelectedArtifact} onClose={() => setSelectedCase(null)} onAction={submitCaseAction} />}
      {auditPreview && (
        <AuditPreviewDialog
          batch={auditPreview.batch}
          preview={auditPreview.preview}
          confirming={confirmingAudit}
          onClose={() => setAuditPreview(null)}
          onConfirm={confirmAudit}
        />
      )}
      {m4Results && (
        <M4ResultsDialog
          batch={m4Results.batch}
          results={m4Results.results}
          onClose={() => setM4Results(null)}
          onOpenCase={(caseId) => {
            setM4Results(null)
            setView('cases')
            void openCase(caseId)
          }}
        />
      )}
      {jobsOpen && <JobsDrawer jobs={jobs} onCancel={cancelJob} onClose={() => setJobsOpen(false)} />}
      {toast && <div className={`toast toast-${toast.tone}`}>{toast.tone === 'success' ? <Check size={18} /> : toast.tone === 'conflict' ? <History size={18} /> : <CircleAlert size={18} />}{toast.message}</div>}
    </div>
  )
}

function BatchStageStepper({ batch }: { batch: Batch }) {
  const stages = [
    { key: 'local', label: '本地检查', value: batch.stages.local },
    { key: 'm4', label: '联网核对', value: batch.stages.m4 },
    { key: 'm5', label: '深度取证', value: batch.stages.m5 },
  ] as const
  return (
    <ol className="batch-stepper" aria-label={`${batch.name}审核进度`}>
      {stages.map(({ key, label, value }) => {
        const notRequired = key === 'm5'
          && batch.stages.m4.status === 'done'
          && value.required === false
        const displayStatus = notRequired
          ? '无需进入'
          : key === 'm5' && value.status === 'done'
            ? '取证完成，待人工复核'
            : key === 'm5' && value.status === 'partial'
              ? '取证不完整'
              : statusLabels[value.status] || value.status
        return (
          <li key={key} className={`batch-step batch-step-${value.status} ${notRequired ? 'batch-step-muted' : ''}`}>
            <span className="batch-step-dot">
              {value.status === 'done' || notRequired
                ? <Check size={11} />
                : value.status === 'running'
                  ? <LoaderCircle className="spin" size={11} />
                  : value.status === 'failed'
                    ? <X size={11} />
                    : null}
            </span>
            <span><strong>{label}</strong><small>{displayStatus}</small></span>
          </li>
        )
      })}
    </ol>
  )
}

function M4ResultsDialog({
  batch,
  results,
  onClose,
  onOpenCase,
}: {
  batch: Batch
  results: M4Results
  onClose: () => void
  onOpenCase: (caseId: number) => void
}) {
  return <div className="modal-backdrop">
    <section
      className="audit-preview-dialog m4-results-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="m4-results-title"
      data-testid="m4-results-dialog"
    >
      <header>
        <div><span className="dialog-kicker">M4 联网核对结果</span><h2 id="m4-results-title">{batch.name}</h2></div>
        <button className="icon-button" title="关闭" onClick={onClose}><X size={19} /></button>
      </header>
      <div className="preview-summary">
        <div><span>当前资源项</span><strong>{results.items.length}</strong></div>
        <div><span>历史结果</span><strong>{results.history_count}</strong></div>
        <div><span>展示口径</span><strong>仅当前结果</strong></div>
      </div>
      <div className="m4-result-list">
        {results.items.map((item) => {
          const sources = Array.from(new Set([
            ...(item.source_urls || []),
            ...(item.source_url ? [item.source_url] : []),
          ]))
          return <article key={item.stage_item_id}>
            <div className="m4-result-heading">
              <div><strong>{item.award_name || item.resource_code}</strong><span>{item.year || '年份未记录'} · {item.resource_code}</span></div>
              <Status value={item.stage_status} />
            </div>
            {item.current_result_id > 0 ? <>
              <div className="m4-result-facts">
                <div><span>名单抽取 / 提交</span><strong>{item.extracted_count || 0} / {item.submitted_count || 0}</strong></div>
                <div><span>M4 结论</span><strong>{item.verdict || '未记录'}</strong></div>
                <div><span>来源形式</span><strong>{item.source_kind || '未记录'}</strong></div>
                <div><span>身份规则</span><strong>{item.identity_version || '未记录'}</strong></div>
              </div>
              {sources.length > 0 && <div className="m4-result-sources"><strong>实际核对来源</strong>{sources.map((url) => <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url} key={url}><Globe2 size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>)}</div>}
              {(item.found_assets || []).length > 0 && <div className="m4-result-sources"><strong>发现的网页附件</strong>{item.found_assets?.map((url) => <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url} key={url}><Paperclip size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>)}</div>}
              {((item.missing || []).length > 0 || (item.extra || []).length > 0) && <div className="m4-result-differences">
                <div><strong>提交有、来源未找到</strong><div>{item.missing?.map((value) => <span key={`m4-missing-${value}`}>{value}</span>)}</div></div>
                <div><strong>来源有、提交未提供</strong><div>{item.extra?.map((value) => <span key={`m4-extra-${value}`}>{value}</span>)}</div></div>
              </div>}
              {item.notes && <p className="m4-result-notes">{item.notes}</p>}
              <div className={`m4-binding ${item.binding?.is_current ? 'binding-current' : 'binding-stale'}`}>
                <div><strong>{item.binding?.is_current ? '已绑定当前 M4 结果' : item.binding ? '案件绑定已过期' : '未进入 M5'}</strong><span>当前结果 #{item.current_result_id} · 本资源项历史 {item.history_count} 条</span></div>
                {item.binding && <button className="command secondary" onClick={() => onOpenCase(item.binding!.case_id)}>案件 #{item.binding.case_id}<ChevronRight size={15} /></button>}
              </div>
            </> : <div className="m4-result-empty"><CircleAlert size={17} /><span>{item.stage_error_message || item.stage_error_code || '该资源项尚未形成当前 M4 结果。'}</span></div>}
          </article>
        })}
        {results.items.length === 0 && <Empty label="M4 尚未产生资源项结果" />}
      </div>
      <footer><button className="command" onClick={onClose}>关闭</button></footer>
    </section>
  </div>
}

function AuditPreviewDialog({
  batch,
  preview,
  confirming,
  onClose,
  onConfirm,
}: {
  batch: Batch
  preview: AuditPreview
  confirming: boolean
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="audit-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="audit-preview-title">
        <header>
          <div>
            <span className="dialog-kicker">M4 联网核对</span>
            <h2 id="audit-preview-title">{batch.name}</h2>
          </div>
          <button className="icon-button" title="关闭预览" onClick={onClose}><X size={18} /></button>
        </header>
        <div className="preview-summary">
          <div><span>候选目标</span><strong>{preview.candidate_targets.length}</strong></div>
          <div><span>本地问题</span><strong>{preview.issues.length}</strong></div>
          <div><span>联网状态</span><strong>尚未核验</strong></div>
        </div>
        <div className="preview-targets">
          {preview.candidate_targets.map((target) => (
            <article key={`${target.resource_code}-${target.year}`}>
              <div className="preview-target-main">
                <strong>{target.award_name || target.resource_code}</strong>
                <span>{target.year || '年份未提供'} · {target.resource_code}</span>
              </div>
              <div className="preview-target-count"><strong>{target.submitted_count}</strong><span>提交记录</span></div>
              <div className="preview-target-source">
                <span>{target.domains.join('、') || '未配置来源域名'}</span>
                <small>{target.urls.length} 个候选网址 · 尚未联网核验</small>
              </div>
            </article>
          ))}
          {preview.candidate_targets.length === 0 && <Empty label="没有可启动的联网核对目标" />}
        </div>
        <footer>
          <button className="command" disabled={confirming} onClick={onClose}>取消</button>
          <button
            className="command primary"
            disabled={confirming || preview.candidate_targets.length === 0}
            onClick={() => void onConfirm()}
          >
            {confirming ? <LoaderCircle className="spin" size={16} /> : <SearchCheck size={16} />}
            确认并启动联网核对
          </button>
        </footer>
      </section>
    </div>
  )
}

function MemoryRow({ memory, memories, onAction }: { memory: Memory; memories: Memory[]; onAction: (memory: Memory, action: 'approve' | 'deprecate' | 'merge', target?: number) => Promise<void> }) {
  const [target, setTarget] = useState('')
  return <tr>
    <td><Status value={memory.status} /></td><td>{memory.category_code}</td>
    <td className="wide-cell">{memory.symptom_text}</td><td className="wide-cell">{memory.resolution}</td>
    <td>{memory.resource_type || '通用'}<small>{memory.field_code || '全部字段'}</small></td>
    <td>{memory.occurrence_count}<small>{memory.source_case_ids.map((id) => `#${id}`).join(', ')}</small></td>
    <td>{memory.approved_by || '-'}</td>
    <td className="row-actions">
      {memory.status === 'candidate' && <button className="icon-button success" title="批准候选" onClick={() => void onAction(memory, 'approve')}><Check size={17} /></button>}
      {['candidate', 'active'].includes(memory.status) && <button className="icon-button danger" title="标记失效" onClick={() => void onAction(memory, 'deprecate')}><Archive size={17} /></button>}
      {['candidate', 'active'].includes(memory.status) && <div className="merge-control">
        <select value={target} onChange={(event) => setTarget(event.target.value)} aria-label="合并目标"><option value="">合并到</option>{memories.filter((item) => item.status === 'active' && item.memory_id !== memory.memory_id).map((item) => <option key={item.memory_id} value={item.memory_id}>#{item.memory_id}</option>)}</select>
        <button className="icon-button" title="合并" disabled={!target} onClick={() => void onAction(memory, 'merge', Number(target))}><ChevronRight size={17} /></button>
      </div>}
    </td>
  </tr>
}

type CaseAction = 'supplement' | 'accepted' | 'rejected' | 'insufficient'

const actionConfirmations: Record<CaseAction, { title: string; detail: string; confirm: string }> = {
  supplement: {
    title: '确认要求补充证据？',
    detail: '系统会创建新的取证轮次并重置本轮预算；已成功处理的证据继续复用，当前意见作为补证要求保存。',
    confirm: '提交补证要求',
  },
  accepted: {
    title: '确认本案符合要求？',
    detail: '这会记录为人工终审通过并结束当前审查。系统不会自动执行正式入库。',
    confirm: '提交终审通过',
  },
  rejected: {
    title: '确认本案不符合要求？',
    detail: '这会记录为人工终审不通过并结束当前审查，请确保处理意见说明具体原因。',
    confirm: '提交终审不通过',
  },
  insufficient: {
    title: '确认暂时无法判断？',
    detail: '这会把案件记录为“证据不足”并结束当前自动审查。它不会发送给其他人员或外部系统。',
    confirm: '提交证据不足记录',
  },
}

function CaseDrawer({ caseDetail, artifact, onArtifact, onClose, onAction, inline = false }: { caseDetail: CaseDetail; artifact: Artifact | null; onArtifact: (artifact: Artifact | null) => void; onClose: () => void; onAction: (action: CaseAction, text: string) => Promise<void>; inline?: boolean }) {
  const [text, setText] = useState(caseDetail.human_decision_summary || caseDetail.recommendation || '')
  const [pendingAction, setPendingAction] = useState<CaseAction | null>(null)
  const verification: VerificationReport | undefined = caseDetail.latest_verification
  const submittedRows = caseDetail.submitted_summary?.submitted_rows
  const referenceRows = caseDetail.submitted_summary?.reference_rows
  const scopeFacts = caseDetail.tool_trace
    .map(traceFacts)
    .reverse()
    .find((facts) => typeof facts.expected_count === 'number') || {}
  const targetScopeRows = typeof scopeFacts.expected_count === 'number'
    ? scopeFacts.expected_count
    : referenceRows
  const pageTotalRows = typeof scopeFacts.page_total_count === 'number'
    ? scopeFacts.page_total_count
    : undefined
  const tracedUrls = new Set(caseDetail.tool_trace.map(traceUrl).filter(Boolean))
  const providedUrls = caseDetail.known_urls.filter((url) => /^https?:\/\//i.test(url) && !tracedUrls.has(url))
  const candidates = caseDetail.evidence_progress?.candidates || []
  const latestAttempt = caseDetail.attempts?.[caseDetail.attempts.length - 1]
  const workflow = caseDetail.evidence_workflow
  const comparison = caseDetail.comparison
  const scopeComparisons = caseDetail.scope_comparisons || []
  const hasBusinessDifferences = scopeComparisons.some((item) =>
    item.comparison_result === 'differences_found' || item.comparison_result === 'conflict')
  const finalStatus = caseDetail.human_decision || (
    caseDetail.status === 'running'
      ? 'running'
      : caseDetail.status === 'waiting_human' && caseDetail.conclusion_readiness !== 'ready_for_human'
        ? 'evidence_incomplete'
        : caseDetail.status === 'waiting_human'
          ? hasBusinessDifferences ? 'evidence_complete_differences' : 'evidence_complete_matched'
          : caseDetail.status
  )
  const localProblems = Array.isArray(caseDetail.submitted_summary?.local_issues)
    ? caseDetail.submitted_summary.local_issues.map((item) => {
      const issue = asRecord(item)
      const message = typeof issue.message === 'string' ? issue.message : ''
      const sourceFile = typeof issue.file === 'string' ? fileName(issue.file) : ''
      return message ? `${sourceFile ? `${sourceFile}：` : ''}${message}` : ''
    }).filter(Boolean)
    : []
  const problems = [
    ...localProblems,
    ...asStrings(verification?.contradictions).map(problemLabel),
    ...asStrings(verification?.missing_evidence).map(problemLabel),
    ...(verification?.supplement_requests || []).map((item) => problemLabel(item.question)),
    ...(workflow?.blockers || []).map(problemLabel),
    ...(comparison?.blockers || []).map(problemLabel),
    ...scopeComparisons.flatMap((item) => item.blockers.map(problemLabel)),
  ]
  if (verification?.coverage_complete === 'no' && problems.length === 0) problems.push('公开来源没有覆盖业务要求的全部名单。')
  if (verification?.source_authority === 'secondary') problems.push('当前有效证据来自次级来源，仍需人工确认其权威性。')
  const uniqueProblems = [...new Set(problems)]
  const verdict = caseDetail.human_decision
    ? decisionLabels[caseDetail.human_decision] || '人工处理已完成'
    : actionLabel(verification?.recommended_action)
  const verdictDetail = caseDetail.human_decision
    ? '当前状态来自人工终审记录，不代表案件被发送给其他人员或外部系统。'
    : verification?.coverage_complete === 'no'
      ? '系统确认了奖项或年份，但公开来源未覆盖完整业务口径，因此不能自动通过。'
      : verification?.source_authority === 'secondary'
        ? '名单内容可以核对，但来源不是官方主发布页，因此需要人工确认。'
        : '系统无法仅凭当前证据作出最终业务决定。'

  return <div className={`overlay${inline ? ' case-inline-overlay' : ''}`} role="dialog" aria-modal={!inline || undefined} aria-label="疑难案件详情">
    {!inline && <button className="overlay-scrim" aria-label="关闭" onClick={onClose} />}
    <aside className={`drawer case-drawer${inline ? ' case-inline-drawer' : ''}`}>
      <div className="drawer-head"><div><span className="eyebrow">案件 #{caseDetail.case_id}</span><h2>{caseDetail.award_name || caseDetail.resource_code}</h2><p>{caseDetail.year} 年 · 资源项码 {caseDetail.resource_code}</p></div><button className="icon-button" title="关闭" onClick={onClose}><X size={19} /></button></div>
      <div className="case-summary"><Status value={finalStatus} /><span>{caseDetail.step_count} 个审查步骤</span><span>{caseDetail.budget?.calls || 0} 次证据工具</span><span>耗时 {(caseDetail.elapsed_ms / 1000).toFixed(1)} 秒</span></div>
      <div className="drawer-scroll">
        <section className="detail-band verdict-band">
          <div className="verdict-heading"><span className="verdict-icon"><AlertTriangle size={20} /></span><div><span>当前审查结论</span><h3>{verdict}</h3></div></div>
          <p>{verdictDetail}</p>
          {typeof submittedRows === 'number' && typeof targetScopeRows === 'number' && <div className="scope-comparison"><span>提交名单 <strong>{submittedRows} 条</strong></span><ChevronRight size={18} /><span>目标分组核验口径 <strong>{targetScopeRows} 条</strong></span>{typeof pageTotalRows === 'number' && pageTotalRows !== targetScopeRows && <span className="mixed-page-total">页面混合总口径 {pageTotalRows} 条，包含其他分组</span>}</div>}
          {verification && <div className="business-checks">
            <div><span>奖项名称</span><strong>{matchLabel(verification.target_match)}</strong></div>
            <div><span>名单年份</span><strong>{matchLabel(verification.year_match)}</strong></div>
            <div><span>名单覆盖</span><strong>{verification.coverage_complete === 'yes' ? '完整' : verification.coverage_complete === 'no' ? '不完整' : '尚未确认'}</strong></div>
            <div><span>来源级别</span><strong>{sourceLabel(verification.source_authority)}</strong></div>
          </div>}
        </section>

        <section className="detail-band">
          <div className="section-title-row"><div><h3>取证闭环状态</h3><p>执行轮次、证据资产和最终核验分别记录</p></div><ListChecks size={20} /></div>
          <div className="m4-result-facts">
            <div><span>当前轮次</span><strong>#{latestAttempt?.sequence || caseDetail.attempt_sequence || 0}</strong></div>
            <div><span>轮次结果</span><strong>{latestAttempt?.conclusion_readiness === 'ready_for_human' ? '可人工复核' : '取证不完整'}</strong></div>
            <div><span>Verifier</span><strong>{latestAttempt?.verifier_status === 'persisted' ? '已生成' : '缺失'}</strong></div>
            <div><span>资产处理</span><strong>{workflow?.assets.processed || 0} / {workflow?.assets.total || 0}</strong></div>
          </div>
          {workflow && <div className="reason-list">
            <span>待处理 {workflow.assets.pending}</span>
            <span>失败 {workflow.assets.failed}</span>
            <span>排除 {workflow.assets.excluded}</span>
            <span>证据组 {caseDetail.evidence_groups?.length || 0}</span>
            <span>路由待确认 {Math.max(0, (workflow.routes?.total || 0) - (workflow.routes?.routed || 0))}</span>
          </div>}
          {(caseDetail.attempts || []).length > 0 && <details className="technical-details"><summary>查看执行轮次、预算与终止原因</summary><div>{caseDetail.attempts.map((attempt) => <p key={attempt.attempt_id}>#{attempt.sequence} · {attempt.kind === 'supplement' ? '补证' : attempt.kind === 'legacy' ? '历史执行' : '初始取证'} · {attempt.status} · {attempt.stop_reason || '尚未结束'} · Verifier {attempt.verifier_status} · {attempt.step_count} 步 · {attempt.elapsed_ms} ms · 调用 {Number(attempt.budget_usage?.calls || 0)} / {Number(attempt.budget_limits?.max_calls || 0)}</p>)}</div></details>}
        </section>

        <section className="detail-band">
          <div className="section-title-row"><div><h3>提交行与资产路由</h3><p>每一行和每个附件都必须有明确业务范围或阻塞原因</p></div><ListChecks size={20} /></div>
          <div className="m4-result-facts">
            <div><span>提交总行</span><strong>{caseDetail.submission_conservation?.total_rows || 0}</strong></div>
            <div><span>已归属</span><strong>{caseDetail.submission_conservation?.assigned_rows || 0}</strong></div>
            <div><span>歧义行</span><strong>{caseDetail.submission_conservation?.ambiguous_rows || 0}</strong></div>
            <div><span>未归属</span><strong>{caseDetail.submission_conservation?.unassigned_rows || 0}</strong></div>
          </div>
          {(caseDetail.evidence_asset_routes || []).length > 0 && <details className="technical-details"><summary>查看资产到业务范围的路由</summary><div>
            {caseDetail.evidence_asset_routes.map((route) => <p key={route.route_id}>
              {route.label || route.url} · {route.scope_key || '尚未归属'} · {route.subunit_type} · {route.processing_status} · {route.route_source} {Math.round(route.confidence * 100)}%
            </p>)}
          </div></details>}
        </section>

        {scopeComparisons.length > 0 && <section className="detail-band">
          <div className="section-title-row"><div><h3>逐角色名单核对</h3><p>原始行数与去重后的业务身份分别计数，差异不会触发无界补搜</p></div><ListChecks size={20} /></div>
          <div className="scope-result-list">
            {scopeComparisons.map((item) => <div className="scope-result" key={item.scope_id}>
              <div className="scope-result-head"><div><strong>{item.role_label || item.role_type}</strong><small>{item.required ? '必审范围' : '适用范围'} · {item.evidence_complete ? '证据完整' : '证据不完整'}</small></div><span>{item.comparison_result === 'matched' ? '未发现差异' : item.comparison_result === 'differences_found' ? '发现业务差异' : item.comparison_result === 'conflict' ? '存在冲突' : '尚未比较'}</span></div>
              <div className="m4-result-facts">
                <div><span>提交原始行</span><strong>{item.submitted_row_count}</strong></div>
                <div><span>提交唯一身份</span><strong>{item.submitted_identity_count}</strong></div>
                <div><span>证据唯一身份</span><strong>{item.evidence_identity_count}</strong></div>
                <div><span>匹配</span><strong>{item.matched_count}</strong></div>
              </div>
              {(item.comparison_differences || []).length === 0 && (item.missing.length > 0 || item.extra.length > 0 || item.conflicts.length > 0) && <div className="comparison-differences">
                {item.missing.length > 0 && <div><strong>提交有、来源未找到</strong><div>{item.missing.map((value) => <span key={`${item.scope_id}-missing-${value}`}>{value}</span>)}</div></div>}
                {item.extra.length > 0 && <div><strong>来源有、提交未提供</strong><div>{item.extra.map((value) => <span key={`${item.scope_id}-extra-${value}`}>{value}</span>)}</div></div>}
                {item.conflicts.filter((value) => !isFieldConflict(value)).length > 0 && <div><strong>身份冲突</strong><div>{item.conflicts.filter((value) => !isFieldConflict(value)).map((value) => <span key={`${item.scope_id}-conflict-${value}`}>{value}</span>)}</div></div>}
              </div>}
              {(item.comparison_differences || []).length > 0
                ? <ScopeDifferenceTable scope={item} />
                : (item.identity_conflicts || []).length > 0 && <IdentityConflictTable scope={item} />}
              {(item.semantic_identity_decisions || []).length > 0 && <SemanticIdentityTable decisions={item.semantic_identity_decisions} />}
            </div>)}
          </div>
        </section>}

        {comparison && scopeComparisons.length === 0 && <section className="detail-band">
          <div className="section-title-row"><div><h3>完整名单差异</h3><p>来自独立比较账本，不受 Tool Trace 展示截断影响</p></div><SearchCheck size={20} /></div>
          <div className="m4-result-facts">
            <div><span>提交 / 来源</span><strong>{comparison.submitted_count} / {comparison.evidence_count}</strong></div>
            <div><span>匹配</span><strong>{comparison.matched_count}</strong></div>
            <div><span>缺失</span><strong>{comparison.missing.length}</strong></div>
            <div><span>多出</span><strong>{comparison.extra.length}</strong></div>
          </div>
          {(comparison.missing.length > 0 || comparison.extra.length > 0) && <div className="comparison-differences">
            {comparison.missing.length > 0 && <div><strong>提交有、来源未找到</strong><div>{comparison.missing.map((item) => <span key={`ledger-missing-${item}`}>{item}</span>)}</div></div>}
            {comparison.extra.length > 0 && <div><strong>来源有、提交未提供</strong><div>{comparison.extra.map((item) => <span key={`ledger-extra-${item}`}>{item}</span>)}</div></div>}
          </div>}
        </section>}

        {caseDetail.m4_evidence && <section className="detail-band m4-handoff-band">
          <div className="section-title-row"><div><h3>M4 → M5 证据交接</h3><p>案件固定读取当前 M4 结果，历史结果不会覆盖本案</p></div><ShieldCheck size={20} /></div>
          <div className="m4-binding binding-current">
            <div><strong>已绑定当前 M4 结果</strong><span>结果 #{caseDetail.m4_evidence.result_id} · 案件记录 #{caseDetail.origin_m4_result_id}</span></div>
          </div>
          <div className="m4-result-facts">
            <div><span>名单抽取 / 提交</span><strong>{caseDetail.m4_evidence.extracted_count} / {caseDetail.m4_evidence.submitted_count}</strong></div>
            <div><span>M4 结论</span><strong>{caseDetail.m4_evidence.verdict}</strong></div>
            <div><span>可信度</span><strong>{caseDetail.m4_evidence.confidence}</strong></div>
            <div><span>来源形式</span><strong>{caseDetail.m4_evidence.source_kind}</strong></div>
          </div>
          {caseDetail.m4_evidence.source_urls.map((url) => <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url} key={`m4-source-${url}`}><Globe2 size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>)}
          {caseDetail.m4_evidence.found_assets.map((url) => <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url} key={`m4-asset-${url}`}><Paperclip size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>)}
        </section>}

        <section className="detail-band">
          <h3>具体问题</h3>
          {uniqueProblems.length > 0 ? <div className="issue-list">{uniqueProblems.map((item, index) => <div key={`${item}-${index}`}><CircleAlert size={17} /><span>{item}</span></div>)}</div> : <p className="muted-copy">当前没有记录具体缺证项，但仍需人工确认来源和业务口径。</p>}
          {verification && <div className="reason-list">{verification.reason_codes.map((code) => <span key={code}>{reasonLabel(code)}</span>)}</div>}
        </section>

        <section className="detail-band">
          <div className="section-title-row"><div><h3>本案检索的案例记忆</h3><p>历史处理方法只作为规划参考，当前证据仍独立核验</p></div><BrainCircuit size={20} /></div>
          {caseDetail.retrieved_memories.length > 0 ? <div className="memory-hit-list">{caseDetail.retrieved_memories.map((memory, index) => <div key={String(memory.memory_id || index)}><strong>{String(memory.symptom_text || `记忆 #${memory.memory_id || index + 1}`)}</strong><p>{String(memory.resolution || '')}</p><small>{String(memory.warning || '历史案例不是当前事实，必须重新核验证据。')}</small></div>)}</div> : <Empty label="本次未检索到适用的生效记忆" />}
        </section>

        <section className="detail-band">
          <div className="section-title-row"><div><h3>核验来源与过程</h3><p>按实际执行顺序列出访问来源和判断结果</p></div><SearchCheck size={20} /></div>
          {providedUrls.length > 0 && <div className="provided-sources"><strong>案件提供的待核验网址</strong><p>以下网址尚未出现在实际 Tool 步骤中，不能视为已核验证据。</p>{providedUrls.map((url) => <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url} key={url}><Globe2 size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>)}</div>}
          {candidates.length > 0 && <div className="candidate-sources"><div><strong>搜索候选网址</strong><span>{candidates.length} 个线索 · {caseDetail.evidence_progress.search_round} 轮搜索</span></div><p>候选网址只是搜索线索；只有标记为“已访问并取得结果”的网址才进入证据核验。</p><div>{candidates.map((candidate) => <div className="candidate-row" key={candidate.url}><div className="candidate-heading"><strong>{candidate.title || `候选来源 ${candidate.rank || ''}`}</strong><span className={`candidate-status candidate-${candidate.status}`}>{candidateStatusLabels[candidate.status] || candidate.status}</span></div><a className="source-link" href={candidate.url} target="_blank" rel="noreferrer noopener" title={candidate.url} aria-label={candidate.url}><Globe2 size={15} /><span>{displayUrl(candidate.url)}</span><ExternalLink size={14} /></a><small>{sourceLabel(candidate.source_level)} · 搜索服务 {candidate.provider || '未记录'} · 排名 {candidate.rank || '未记录'}</small>{candidate.query && <div className="candidate-query"><SearchCheck size={13} /><span>搜索词：{candidate.query}</span></div>}{candidate.status_reason && <p className="candidate-reason">{candidate.status_reason}</p>}</div>)}</div></div>}
          <div className="process-list">{caseDetail.tool_trace.length ? caseDetail.tool_trace.map((trace, index) => {
            const url = traceUrl(trace)
            const findings = traceFindings(trace)
            const differences = traceDifferences(trace)
            const attachments = traceAttachments(trace)
            const problem = traceProblem(trace)
            const relationship = traceRelationship(trace)
            const groupedMatches = traceGroupedMatches(trace)
            const localFile = typeof trace.input_summary?.path === 'string'
              ? trace.input_summary.path
              : typeof trace.input_summary?.file === 'string'
                ? trace.input_summary.file
                : ''
            const comparisonFiles = asStrings(trace.input_summary?.submitted_paths)
            const comparisonFile = typeof trace.input_summary?.submitted_path === 'string'
              ? trace.input_summary.submitted_path
              : ''
            if (comparisonFiles.length === 0 && comparisonFile) comparisonFiles.push(comparisonFile)
            const imageUrls = asStrings(trace.input_summary?.image_urls)
            return <div className="process-step" key={trace.call_id || String(index)}>
              <div className={`step-marker ${trace.ok ? 'step-ok' : 'step-fail'}`}>{trace.ok ? <Check size={15} /> : <X size={15} />}</div>
              <div className="step-body">
                <div className="step-head"><div><span>步骤 {index + 1}</span><strong>{toolLabels[trace.tool_name] || trace.tool_name}</strong></div><small>{trace.duration_ms} ms · {trace.ok ? '执行成功' : '执行失败'}</small></div>
                {url && <a className="source-link" href={url} target="_blank" rel="noreferrer noopener" title={url} aria-label={url}><Globe2 size={15} /><span>{displayUrl(url)}</span><ExternalLink size={14} /></a>}
                {!url && localFile && <div className="local-file"><FileSearch size={15} /><span>{fileName(localFile)}</span></div>}
                {attachments.length > 0 && <div className="discovered-attachments"><strong><Paperclip size={14} />发现网页附件</strong>{attachments.map((item, attachmentIndex) => item.url && /^https?:\/\//i.test(item.url)
                  ? <a href={item.url} target="_blank" rel="noreferrer noopener" key={`${item.url}-${attachmentIndex}`}><span>{item.name}</span><ExternalLink size={13} /></a>
                  : <div key={`${item.name}-${attachmentIndex}`}>{item.name}</div>)}</div>}
                {comparisonFiles.length > 0 && <div className="comparison-baseline"><FileSearch size={14} /><span>名单比对基准（{comparisonFiles.length} 个文件）：{comparisonFiles.map(fileName).join('、')}</span></div>}
                {imageUrls.length > 0 && <div className="image-count">待识别网页图片：{imageUrls.length} 张</div>}
                {findings.length > 0 && <div className="finding-list">{findings.map((finding) => <span key={finding}>{finding}</span>)}</div>}
                {relationship.confirmed && <div className="relationship-evidence"><strong><CheckCircle2 size={15} />找到对应关系证据</strong><p>{relationship.summary || '该来源同时出现差异姓名和群体名称。'}</p><div>{relationship.terms.map((term) => <span key={term}>{term}</span>)}</div></div>}
                {groupedMatches.length > 0 && <div className="grouped-match-evidence"><strong><CheckCircle2 size={15} />联合记录如何计数</strong>{groupedMatches.map((names) => <div key={names.join('|')}><span>{names.join('、')}</span><small>提交文件中的同一条记录，来源中均已找到；按 1 条联合记录计数。</small></div>)}<p>姓名均已匹配，不属于“未找到”。联合名额的业务口径仍需人工确认。</p></div>}
                {(differences.missing.length > 0 || differences.extra.length > 0 || differences.unresolved.length > 0) && <div className="comparison-differences">
                  {differences.note && <p className="comparison-note"><CircleAlert size={15} />{differences.note}</p>}
                  {differences.missing.length > 0 && <div><strong>提交名单有，该来源未找到</strong><div>{differences.missing.map((item) => <span key={`missing-${item}`}>{item}</span>)}</div></div>}
                  {differences.extra.length > 0 && <div><strong>该来源有，提交名单未提供</strong><div>{differences.extra.map((item) => <span key={`extra-${item}`}>{item}</span>)}</div></div>}
                  {differences.unresolved.length > 0 && <div className="unresolved"><strong>{traceFacts(trace).next_evidence_stage === 'spreadsheet_processing' ? '等待附件继续核验' : '等待图片或附件继续核验'}</strong><div>{differences.unresolved.map((item) => <span key={`unresolved-${item}`}>{item}</span>)}</div></div>}
                </div>}
                {problem && <p className={trace.ok ? 'step-problem' : 'step-error'}>{problem}</p>}
              </div>
            </div>
          }) : <Empty label="暂无核验步骤" />}</div>
        </section>

        {caseDetail.artifacts.length > 0 && <section className="detail-band"><h3>下载的证据文件</h3><div className="artifact-list">{caseDetail.artifacts.map((item) => <button key={item.artifact_id} className={artifact?.artifact_id === item.artifact_id ? 'artifact-active' : ''} onClick={() => onArtifact(item)}><FileSearch size={18} /><span><strong>{item.file_name}</strong><small>{item.content_type} · {(item.size_bytes / 1024).toFixed(1)} KB</small></span><ChevronRight size={16} /></button>)}</div>
          {artifact && <div className="preview"><div className="preview-head"><div><strong>{artifact.file_name}</strong><small>SHA-256 {artifact.sha256.slice(0, 16)}… · {formatTime(artifact.fetched_at)}</small></div><a className="icon-button" href={artifact.preview_url} target="_blank" rel="noreferrer noopener" title="新窗口打开"><ExternalLink size={17} /></a></div>{artifact.content_type.startsWith('image/') ? <img src={artifact.preview_url} alt={artifact.file_name} /> : artifact.content_type === 'application/pdf' ? <iframe src={artifact.preview_url} title={artifact.file_name} /> : <a href={artifact.preview_url}>下载证据文件</a>}</div>}
        </section>}

        {caseDetail.human_decision && <section className="detail-band human-record"><div className="section-title-row"><div><h3>人工处理记录</h3><p>{decisionLabels[caseDetail.human_decision] || caseDetail.human_decision}</p></div><CheckCircle2 size={21} /></div><dl><div><dt>复核人</dt><dd>{caseDetail.reviewed_by || '未记录'}</dd></div><div><dt>处理时间</dt><dd>{formatTime(caseDetail.reviewed_at)}</dd></div></dl><p>{caseDetail.human_decision_summary || '未填写处理意见'}</p><div className="record-note">该决定仅记录在本地审查系统中，没有转交给其他人员或外部平台。</div></section>}

        <details className="technical-details"><summary>查看技术记录</summary><div><p>自动取证步数：{caseDetail.step_count}；模型 Token：{caseDetail.token_used}；Reflection：{caseDetail.reflection_count}/1。</p><div className="reason-list">{caseDetail.reason_codes.map((code) => <span key={code}>{reasonLabel(code)}</span>)}</div></div></details>
      </div>

      {caseDetail.status === 'waiting_human' && !caseDetail.human_decision && <div className="review-bar"><label htmlFor={`case-review-${caseDetail.case_id}`}>处理意见</label><textarea id={`case-review-${caseDetail.case_id}`} value={text} onChange={(event) => setText(event.target.value)} placeholder="说明通过、不通过、补证或暂缓判断的具体原因" /><div><button className="command secondary" disabled={!text.trim()} onClick={() => setPendingAction('supplement')}><Send size={16} />要求补充证据</button><button className="command success" disabled={!text.trim()} onClick={() => setPendingAction('accepted')}><Check size={16} />确认符合要求</button><button className="command danger" disabled={!text.trim()} onClick={() => setPendingAction('rejected')}><XCircle size={16} />确认不符合</button><button className="command" disabled={!text.trim()} onClick={() => setPendingAction('insufficient')}><CircleAlert size={16} />证据不足，暂缓结论</button></div>
        {pendingAction && <div className="decision-confirm" role="alertdialog" aria-label="确认人工决定"><div><strong>{actionConfirmations[pendingAction].title}</strong><p>{actionConfirmations[pendingAction].detail}</p></div><div><button className="command" onClick={() => setPendingAction(null)}>取消</button><button className="command primary" onClick={() => { const action = pendingAction; setPendingAction(null); void onAction(action, text) }}>{actionConfirmations[pendingAction].confirm}</button></div></div>}
      </div>}
    </aside>
  </div>
}

function JobsDrawer({ jobs, onCancel, onClose }: { jobs: Job[]; onCancel: (job: Job) => Promise<void>; onClose: () => void }) {
  return <div className="overlay" role="dialog" aria-modal="true" aria-label="任务活动"><button className="overlay-scrim" aria-label="关闭" onClick={onClose} /><aside className="drawer jobs-drawer"><div className="drawer-head"><div><span className="eyebrow">持久队列</span><h2>任务活动</h2></div><button className="icon-button" title="关闭" onClick={onClose}><X size={19} /></button></div><div className="drawer-scroll jobs-list">{jobs.map((job) => <div className="job-row" key={job.job_id}><div className="job-title"><span>#{job.job_id} · {job.kind}</span><div className="job-state"><Status value={job.status} />{['queued', 'waiting_human'].includes(job.status) && <button className="icon-button danger" title="取消任务" onClick={() => void onCancel(job)}><XCircle size={16} /></button>}</div></div><div className="progress-track"><span style={{ width: `${job.progress}%` }} /></div><p>{job.progress_message || job.error_code || '等待执行'}</p><small>{job.created_by} · {formatTime(job.updated_at)}</small></div>)}{jobs.length === 0 && <Empty label="暂无任务" />}</div></aside></div>
}

export default App
