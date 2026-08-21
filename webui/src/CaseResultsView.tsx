import {
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FileText,
  Globe2,
  ListChecks,
  SearchCheck,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { CaseDetail, EvidenceAssetRoute, ToolTrace } from './api'
import { displayUrl } from './url'

type CaseAction = 'supplement' | 'accepted' | 'rejected' | 'insufficient'
type ViewTab = 'overview' | 'sources' | 'differences' | 'technical'
type SourceState = 'included' | 'supplement' | 'excluded' | 'cross_scope'
type SourceItem = {
  url: string
  label: string
  detail: string
  state: SourceState
  kind: string
}

const actionCopy: Record<CaseAction, { label: string; title: string }> = {
  supplement: { label: '要求补充证据', title: '确认记录补证要求？' },
  accepted: { label: '确认符合要求', title: '确认人工通过？' },
  rejected: { label: '确认不符合', title: '确认人工不通过？' },
  insufficient: { label: '证据不足，暂缓结论', title: '确认记录证据不足？' },
}

function formatTime(value: string) {
  return value ? value.replace('T', ' ').slice(0, 19) : '未记录'
}

function traceUrl(trace: ToolTrace): string {
  const values = [trace.input_summary?.url, trace.input_summary?.page_url, trace.output_summary?.source_url]
  return values.find((value): value is string => isWebUrl(value)) || ''
}

function isWebUrl(value: unknown): value is string {
  return typeof value === 'string' && /^https?:\/\//i.test(value)
}

function routeState(route: EvidenceAssetRoute): SourceState {
  if (route.processing_status === 'excluded' || route.route_status === 'excluded') return 'excluded'
  if (route.scope_key && route.route_status === 'cross_scope') return 'cross_scope'
  return route.processing_status === 'processed' ? 'included' : 'supplement'
}

function outcome(detail: CaseDetail, differenceCount: number) {
  if (detail.human_decision === 'accepted') return { label: '人工确认符合要求', tone: 'matched' }
  if (detail.human_decision === 'rejected') return { label: '人工确认不符合', tone: 'differences' }
  if (detail.human_decision === 'insufficient') return { label: '证据不足，暂缓结论', tone: 'differences' }
  if (detail.conclusion_readiness !== 'ready_for_human') return { label: '取证尚未闭环', tone: 'differences' }
  if (differenceCount > 0) return { label: '发现业务差异，等待人工判断', tone: 'differences' }
  return { label: '证据可提交人工确认', tone: 'matched' }
}

function sourceStateLabel(state: SourceState) {
  return state === 'included' ? '比较' : state === 'excluded' ? '排除' : state === 'cross_scope' ? '类别核验' : '补充'
}

function sourceLabel(item: SourceItem) {
  if (item.label) return item.label
  if (item.kind === 'artifact') return '下载的证据文件'
  return '核验来源'
}

function buildSources(detail: CaseDetail): SourceItem[] {
  const initialUrls = new Set([
    ...(detail.m4_evidence?.source_urls || []),
    ...detail.known_urls,
  ])
  const comparisonSources: SourceItem[] = detail.scope_comparisons.flatMap((scope) => scope.source_urls
    .filter(isWebUrl)
    .map((url) => ({
      url,
      label: '最终匹配来源',
      detail: `${scope.role_label || scope.role_type || scope.scope_key || '审查范围'} · 已用于最终名单比较`,
      state: 'included' as const,
      kind: 'comparison',
    })))
  const routedSources: SourceItem[] = detail.evidence_asset_routes
    .filter((route) => routeState(route) !== 'excluded')
    .map((route) => ({
      url: route.url || route.parent_url,
      label: route.label || '已路由证据材料',
      detail: `${route.scope_key || '未归属范围'} · ${route.processing_status || '待处理'}`,
      state: routeState(route),
      kind: route.kind || 'asset',
    }))
    .filter((source) => isWebUrl(source.url))
  const finalUrls = new Set([...comparisonSources, ...routedSources].map((source) => source.url))
  const successfulTraceSources: SourceItem[] = detail.tool_trace
    .filter((trace) => trace.ok && ['fetch_web_page', 'extract_search_document', 'download_evidence', 'extract_web_image', 'ocr_image'].includes(trace.tool_name))
    .map((trace) => ({
      url: traceUrl(trace),
      label: '成功访问的联网来源',
      detail: `${trace.tool_name} · 已成功取证`,
      state: 'included' as const,
      kind: 'trace',
    }))
    .filter((source) => isWebUrl(source.url))
  const replacementTraceSources = successfulTraceSources.filter((source) => !initialUrls.has(source.url))
  const traceFallback = finalUrls.size > 0
    ? []
    : replacementTraceSources.length > 0
      ? replacementTraceSources
      : successfulTraceSources
  const artifactSources: SourceItem[] = detail.artifacts
    .filter((artifact) => isWebUrl(artifact.source_url) && (finalUrls.size === 0 || finalUrls.has(artifact.source_url)))
    .map((artifact) => ({
      url: artifact.source_url,
      label: artifact.file_name || '下载的证据文件',
      detail: `${artifact.content_type || '文件'} · ${(artifact.size_bytes / 1024).toFixed(1)} KB`,
      state: 'included' as const,
      kind: 'artifact',
    }))
  const initialFallback: SourceItem[] = comparisonSources.length || routedSources.length || traceFallback.length || artifactSources.length
    ? []
    : (detail.m4_evidence?.source_urls || []).filter(isWebUrl).map((url) => ({
      url,
      label: '初始提供网址',
      detail: '尚未取得可用于最终比较的替代来源',
      state: 'supplement' as const,
      kind: detail.m4_evidence?.source_kind || 'html',
    }))
  const candidates = [
    ...comparisonSources,
    ...routedSources,
    ...traceFallback,
    ...artifactSources,
    ...initialFallback,
  ]
  const seen = new Set<string>()
  return candidates.filter((item) => {
    if (!isWebUrl(item.url) || seen.has(item.url)) return false
    seen.add(item.url)
    return true
  })
}

export function CaseResultsView({
  caseDetail,
  onAction,
}: {
  caseDetail: CaseDetail
  onAction: (action: CaseAction, text: string) => Promise<void>
}) {
  const [tab, setTab] = useState<ViewTab>('overview')
  const [text, setText] = useState(caseDetail.human_decision_summary || caseDetail.recommendation || '')
  const [pendingAction, setPendingAction] = useState<CaseAction | null>(null)
  const scopes = caseDetail.scope_comparisons || []
  const fallback = caseDetail.comparison
  const metrics = useMemo(() => {
    if (scopes.length > 0) {
      return scopes.reduce((total, scope) => ({
        submitted: total.submitted + scope.submitted_identity_count,
        evidence: total.evidence + scope.evidence_identity_count,
        matched: total.matched + scope.matched_count,
        missing: total.missing + scope.missing.length,
        extra: total.extra + scope.extra.length,
        conflicts: total.conflicts + scope.conflicts.length,
      }), { submitted: 0, evidence: 0, matched: 0, missing: 0, extra: 0, conflicts: 0 })
    }
    return {
      submitted: fallback?.submitted_count ?? caseDetail.m4_evidence?.submitted_count ?? 0,
      evidence: fallback?.evidence_count ?? caseDetail.m4_evidence?.extracted_count ?? 0,
      matched: fallback?.matched_count ?? 0,
      missing: fallback?.missing.length ?? caseDetail.m4_evidence?.missing.length ?? 0,
      extra: fallback?.extra.length ?? caseDetail.m4_evidence?.extra.length ?? 0,
      conflicts: fallback?.contradictions.length ?? 0,
    }
  }, [caseDetail.m4_evidence, fallback, scopes])
  const sources = useMemo(() => buildSources(caseDetail), [caseDetail])
  const differenceCount = metrics.missing + metrics.extra + metrics.conflicts
  const currentOutcome = outcome(caseDetail, differenceCount)
  const latestAttempt = caseDetail.attempts[caseDetail.attempts.length - 1]

  useEffect(() => {
    setTab('overview')
    setText(caseDetail.human_decision_summary || caseDetail.recommendation || '')
    setPendingAction(null)
  }, [caseDetail.case_id, caseDetail.human_decision_summary, caseDetail.recommendation])

  const confirmAction = () => {
    if (!pendingAction || !text.trim()) return
    const action = pendingAction
    setPendingAction(null)
    void onAction(action, text)
  }

  return <section className="case-results-view">
    <header className="case-results-banner">
      <div>
        <span className="acceptance-kicker"><ShieldCheck size={14} /> 实时案件成果 · 案件 #{caseDetail.case_id}</span>
        <h2>{caseDetail.award_name || caseDetail.resource_code}</h2>
        <p>{caseDetail.year || '年份未记录'} · 资源项码 {caseDetail.resource_code || '未记录'} · 批次 #{caseDetail.batch_id} · 更新于 {formatTime(caseDetail.updated_at)}</p>
      </div>
      <div className={`case-results-outcome outcome-${currentOutcome.tone}`}><span>当前结论</span><strong>{currentOutcome.label}</strong><small>{caseDetail.human_decision ? '人工决定已持久化' : caseDetail.status}</small></div>
    </header>

    <div className="case-results-boundary">
      <CircleAlert size={18} />
      <div><strong>{caseDetail.human_decision ? '人工处理记录' : '为什么需要人工处理'}</strong><span>{caseDetail.human_decision_summary || caseDetail.recommendation || '系统不会自动完成业务终审或入库，需由复核人确认当前证据和业务口径。'}</span></div>
      <span>{caseDetail.human_decision || caseDetail.status}</span>
    </div>

    <div className="case-results-metrics" aria-label="案件核对统计">
      <div><span>提交身份</span><strong>{metrics.submitted.toLocaleString()}</strong></div>
      <div><span>来源身份</span><strong>{metrics.evidence.toLocaleString()}</strong></div>
      <div><span>已匹配</span><strong>{metrics.matched.toLocaleString()}</strong></div>
      <div className={differenceCount ? 'metric-attention' : ''}><span>待复核差异</span><strong>{differenceCount.toLocaleString()}</strong><small>缺失 {metrics.missing} · 额外 {metrics.extra} · 冲突 {metrics.conflicts}</small></div>
    </div>

    <div className="case-results-tabs" role="tablist" aria-label="案件成果视图">
      {([
        ['overview', '概览'],
        ['sources', `来源材料（${sources.length}）`],
        ['differences', `名单差异（${differenceCount}）`],
        ['technical', `技术轨迹（${caseDetail.tool_trace.length}）`],
      ] as const).map(([value, label]) => <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? 'is-active' : ''} onClick={() => setTab(value)}>{label}</button>)}
    </div>

    {tab === 'overview' && <>
      <div className="case-results-grid">
        <article className="acceptance-panel">
          <header><div><span>身份比较</span><h3>逐 scope 核对结果</h3></div><ListChecks size={19} /></header>
          {scopes.length > 0 ? <div className="scope-table-wrap"><table className="acceptance-table"><thead><tr><th>审查范围</th><th>提交</th><th>来源</th><th>匹配</th><th>缺失</th><th>额外</th><th>结果</th></tr></thead><tbody>{scopes.map((scope) => {
            const hasDifference = scope.missing.length > 0 || scope.extra.length > 0 || scope.conflicts.length > 0
            return <tr key={scope.scope_id}><td><strong>{scope.role_label || scope.role_type || scope.scope_key}</strong></td><td>{scope.submitted_identity_count.toLocaleString()}</td><td>{scope.evidence_identity_count.toLocaleString()}</td><td className="scope-matched">{scope.matched_count.toLocaleString()}</td><td className={scope.missing.length ? 'scope-difference' : ''}>{scope.missing.length || '-'}</td><td className={scope.extra.length ? 'scope-difference' : ''}>{scope.extra.length || '-'}</td><td><span className={`scope-status ${hasDifference ? 'has-difference' : 'is-matched'}`}>{hasDifference ? '发现差异' : scope.evidence_complete ? '一致' : '证据不完整'}</span></td></tr>
          })}</tbody></table></div> : <div className="case-result-empty"><CheckCircle2 size={18} /><span>当前案件尚未形成逐 scope 比较结果。</span></div>}
        </article>
        <article className="acceptance-panel">
          <header><div><span>执行与 Verifier</span><h3>取证闭环状态</h3></div><ShieldCheck size={19} /></header>
          <dl className="trace-fact-list">
            <div><dt>当前轮次</dt><dd>#{latestAttempt?.sequence || caseDetail.attempt_sequence || 0}</dd></div>
            <div><dt>最终 Verifier</dt><dd>{latestAttempt?.verifier_status === 'persisted' ? <><CheckCircle2 size={14} />已生成</> : '未记录'}</dd></div>
            <div><dt>证据资产</dt><dd>{caseDetail.evidence_workflow.assets.processed} / {caseDetail.evidence_workflow.assets.total}</dd></div>
            <div><dt>搜索轮次</dt><dd>{caseDetail.evidence_progress.search_round}</dd></div>
            <div><dt>工具步骤</dt><dd>{caseDetail.step_count}</dd></div>
            <div><dt>业务状态</dt><dd>{caseDetail.conclusion_readiness === 'ready_for_human' ? '可人工复核' : '证据未闭环'}</dd></div>
          </dl>
        </article>
      </div>
      <CaseReviewPanel caseDetail={caseDetail} text={text} pendingAction={pendingAction} onText={setText} onChoose={setPendingAction} onCancel={() => setPendingAction(null)} onConfirm={confirmAction} />
    </>}

    {tab === 'sources' && <article className="acceptance-panel case-results-panel">
      <header><div><span>实际用于核验的材料</span><h3>最终匹配来源与证据资产</h3></div><Globe2 size={19} /></header>
      <p className="source-panel-note">优先展示进入最终名单比较的链接；原始输入网址仅作为技术追溯记录，不作为人工核验入口。</p>
      <div className="acceptance-source-list">{sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer noopener" title={source.url} aria-label={source.url} className={`acceptance-source source-${source.state}`}><span>{sourceStateLabel(source.state)}</span><div><strong>{sourceLabel(source)}</strong><small>{displayUrl(source.url)} · {source.detail}</small></div><ExternalLink size={15} /></a>)}</div>
      {sources.length === 0 && <div className="case-result-empty"><Globe2 size={18} /><span>当前案件未持久化可展示的来源 URL。</span></div>}
    </article>}

    {tab === 'differences' && <article className="acceptance-panel case-results-panel">
      <header><div><span>可审计问题明细</span><h3>缺失、额外与身份冲突</h3></div><CircleAlert size={19} /></header>
      {scopes.some((scope) => scope.missing.length || scope.extra.length || scope.conflicts.length) ? <div className="difference-detail-list">{scopes.filter((scope) => scope.missing.length || scope.extra.length || scope.conflicts.length).map((scope) => <details key={scope.scope_id} open><summary><div><strong>{scope.role_label || scope.role_type || scope.scope_key}</strong><small>{scope.missing.length} 条缺失 · {scope.extra.length} 条额外 · {scope.conflicts.length} 条冲突</small></div><ChevronRight size={17} /></summary><div className="difference-detail-body"><p>差异来自此 scope 的提交身份与已选用证据身份的确定性比较。</p>{scope.source_urls[0] && <a href={scope.source_urls[0]} target="_blank" rel="noreferrer noopener" title={scope.source_urls[0]} className="difference-source"><ExternalLink size={14} />比较来源<small>{displayUrl(scope.source_urls[0])}</small></a>}<div className="difference-columns"><section><h4>提交有、来源未匹配</h4>{scope.missing.length ? <ul>{scope.missing.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无缺失记录。</p>}</section><section className="extra-list"><h4>来源有、提交未提供</h4>{scope.extra.length ? <ul>{scope.extra.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无额外记录。</p>}</section></div>{scope.conflicts.length > 0 && <div className="case-conflicts"><strong>身份冲突</strong>{scope.conflicts.map((item) => <span key={item}>{item}</span>)}</div>}</div></details>)}</div> : <div className="no-difference-detail"><CheckCircle2 size={18} /><div><strong>未发现身份差异</strong><span>当前已完成的范围内，提交身份与来源身份没有缺失、额外或冲突记录。</span></div></div>}
    </article>}

    {tab === 'technical' && <article className="acceptance-panel case-results-panel">
      <header><div><span>可追溯执行记录</span><h3>attempt、工具调用与搜索候选</h3></div><SearchCheck size={19} /></header>
      <div className="case-technical-summary"><span>attempt {caseDetail.attempts.length}</span><span>Tool Trace {caseDetail.tool_trace.length}</span><span>搜索候选 {caseDetail.evidence_progress.candidates.length}</span><span>发现资产 {caseDetail.artifacts.length}</span></div>
      <div className="case-technical-list">{caseDetail.tool_trace.map((trace, index) => { const url = traceUrl(trace); return <div className="case-technical-step" key={trace.call_id || String(index)}><span className={trace.ok ? 'trace-ok' : 'trace-failed'}>{trace.ok ? '✓' : '!'}</span><div><strong>{index + 1}. {trace.tool_name}</strong><small>{trace.duration_ms} ms · {trace.ok ? '执行成功' : trace.error_code || '执行失败'}</small>{url && <a href={url} target="_blank" rel="noreferrer noopener" title={url}>{displayUrl(url)} <ExternalLink size={12} /></a>}</div></div>})}</div>
      {caseDetail.tool_trace.length === 0 && <div className="case-result-empty"><ListChecks size={18} /><span>暂无可展示的工具调用记录。</span></div>}
    </article>}
  </section>
}

function CaseReviewPanel({
  caseDetail,
  text,
  pendingAction,
  onText,
  onChoose,
  onCancel,
  onConfirm,
}: {
  caseDetail: CaseDetail
  text: string
  pendingAction: CaseAction | null
  onText: (value: string) => void
  onChoose: (action: CaseAction) => void
  onCancel: () => void
  onConfirm: () => void
}) {
  if (caseDetail.human_decision) return <article className="case-review-record"><CheckCircle2 size={18} /><div><strong>人工处理已完成</strong><span>{caseDetail.human_decision_summary || '未填写处理意见'} · {caseDetail.reviewed_by || '复核人未记录'} · {formatTime(caseDetail.reviewed_at)}</span></div></article>
  if (caseDetail.status !== 'waiting_human') return null
  return <article className="case-review-panel"><div><span>人工处理</span><h3>确认业务结论</h3><p>系统结论仅支持人工判断；操作会写入案件与审计记录，不会自动入库。</p></div><textarea value={text} onChange={(event) => onText(event.target.value)} placeholder="填写通过、不通过、补证或暂缓判断的具体原因" /><div className="case-review-actions"><button className="command secondary" disabled={!text.trim()} onClick={() => onChoose('supplement')}>{actionCopy.supplement.label}</button><button className="command success" disabled={!text.trim()} onClick={() => onChoose('accepted')}>{actionCopy.accepted.label}</button><button className="command danger" disabled={!text.trim()} onClick={() => onChoose('rejected')}>{actionCopy.rejected.label}</button><button className="command" disabled={!text.trim()} onClick={() => onChoose('insufficient')}>{actionCopy.insufficient.label}</button></div>{pendingAction && <div className="case-action-confirm"><div><strong>{actionCopy[pendingAction].title}</strong><p>该操作会保留当前证据和 Trace，并记录人工意见。</p></div><div><button className="command" onClick={onCancel}>取消</button><button className="command primary" onClick={onConfirm}>{actionCopy[pendingAction].label}</button></div></div>}</article>
}
