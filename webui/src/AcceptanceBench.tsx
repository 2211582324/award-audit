import {
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  Globe2,
  SearchX,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useState } from 'react'
import acceptanceDetails from './acceptanceDetails.public.json'
import { displayUrl } from './url'

type Scope = {
  label: string
  submitted: number
  evidence: number
  matched: number
  missing: number
  extra: number
  categoryConflicts?: number
}

type ComparisonDetail = {
  scopeLabel: string
  sourceAsset: string
  sourceUrl: string
  identityRule: string
  missing: string[]
  extra: string[]
  relatedOutOfScope?: Array<{
    identity: string
    source_url: string
    source_label: string
    reason: string
  }>
}

type Asset = {
  label: string
  kind: 'xlsx' | 'pdf' | 'html'
  state: 'included' | 'cross_scope' | 'excluded' | 'supplement'
  detail: string
  url: string
}

type AcceptanceCase = {
  id: string
  name: string
  resourceCode: string
  source: string
  database: string
  status: 'matched' | 'differences' | 'accepted'
  humanReview: string
  trace: { requests: number; assets: number; assessments: number }
  assets: Asset[]
  scopes: Scope[]
  comparisonDetails: ComparisonDetail[]
  review: string
}

const attachmentUrl = (id: string) => `https://cpipc.acge.org.cn/sysFile/downFile.do?fileId=${id}`
const sinossPdf = (id: string) => `https://www.sinoss.net/upload/resources/file/2025/09/25/${id}.pdf`

const acceptanceCases: AcceptanceCase[] = [
  {
    id: 's03',
    name: '全国农科研究生志愿服务技能大赛',
    resourceCode: '04030060 · 2023',
    source: '提交-14 · 四份官方 XLSX + 一份 2025 PDF',
    database: 'm5-p41-s03-20260807-233850',
    status: 'matched',
    humanReview: '比较链路已经闭环，但案件仍保持 waiting_human：系统不自动终审、不自动入库，等待人工确认业务结论。',
    trace: { requests: 5, assets: 7, assessments: 12 },
    assets: [
      { label: '一等奖获奖团队名单', kind: 'xlsx', state: 'included', detail: '技术类、教育类、管理类', url: attachmentUrl('ace2c07cc46d4af29d9c10fda061b473') },
      { label: '二等奖获奖团队名单', kind: 'xlsx', state: 'included', detail: '技术类、教育类、管理类', url: attachmentUrl('a3061576552e439d94f0a5f168468230') },
      { label: '三等奖获奖团队名单', kind: 'xlsx', state: 'included', detail: '技术类、教育类、管理类', url: attachmentUrl('475bffce3db64ff684747fb1798a068c') },
      { label: '优秀组织单位名单', kind: 'xlsx', state: 'included', detail: '组织单位 scope', url: attachmentUrl('dc77d4881b0f468e92818c79ada38bae') },
      { label: '第二届获奖名单', kind: 'pdf', state: 'excluded', detail: '2025 · 独立届次', url: attachmentUrl('5b4684c500bd478a88e35f3b65b882db') },
      { label: '2023 公示页面', kind: 'html', state: 'supplement', detail: '同届说明页', url: 'https://cpipc.acge.org.cn/cw/detail/2c90800c7a132603017a139d4bfb06db/2c9080158aee323f018bc7aa37fa6a9d' },
      { label: '2025 公示页面', kind: 'html', state: 'supplement', detail: '第二届附件来源', url: 'https://cpipc.acge.org.cn/cw/detail/2c90800c7a132603017a139d4bfb06db/2c908018998135ac019984e57b410fcc' },
    ],
    scopes: [
      { label: '优秀组织单位', submitted: 28, evidence: 28, matched: 28, missing: 0, extra: 0 },
      { label: '技术类团队', submitted: 168, evidence: 168, matched: 168, missing: 0, extra: 0 },
      { label: '教育类团队', submitted: 70, evidence: 70, matched: 70, missing: 0, extra: 0 },
      { label: '管理类团队', submitted: 41, evidence: 41, matched: 41, missing: 0, extra: 0 },
    ],
    comparisonDetails: [],
    review: '2025 第二届 PDF 与提交基准的 2023 第一届不是同一届次，排除理由已经与公示页标题核对。',
  },
  {
    id: 's15',
    name: '教育部人文社会科学研究一般项目',
    resourceCode: '05040003 · 2025',
    source: '提交-15 · 登记页发现六份官方 PDF',
    database: 'm5-p42-submission15-20260807-231850',
    status: 'differences',
    humanReview: '发现 212 条业务差异（209 条缺失、3 条额外），案件保持 waiting_human，必须由人工核实名单口径。',
    trace: { requests: 3, assets: 7, assessments: 7 },
    assets: [
      { label: '规划 / 青年 / 自筹项目名单', kind: 'pdf', state: 'included', detail: '177 页 · 三个 scope', url: sinossPdf('46270') },
      { label: '专项任务：理论体系研究', kind: 'pdf', state: 'included', detail: '理论体系 scope', url: sinossPdf('46274') },
      { label: '专项任务：高校辅导员研究', kind: 'pdf', state: 'included', detail: '辅导员 scope', url: sinossPdf('46275') },
      { label: '西部和边疆地区项目', kind: 'pdf', state: 'excluded', detail: '独立类别', url: sinossPdf('46271') },
      { label: '新疆项目 / 西藏项目', kind: 'pdf', state: 'excluded', detail: '独立类别', url: sinossPdf('46272') },
      { label: '新疆项目 / 西藏项目（补充）', kind: 'pdf', state: 'excluded', detail: '独立类别', url: sinossPdf('46273') },
      { label: '2025 公示登记页', kind: 'html', state: 'supplement', detail: '附件目录与公告上下文', url: 'https://www.sinoss.net/c/2025-09-25/659407.shtml' },
    ],
    scopes: [
      { label: '专项任务：理论体系研究', submitted: 36, evidence: 36, matched: 36, missing: 0, extra: 0 },
      { label: '专项任务：高校辅导员研究', submitted: 212, evidence: 209, matched: 206, missing: 6, extra: 3 },
      { label: '自筹经费项目', submitted: 16, evidence: 16, matched: 16, missing: 0, extra: 0 },
      { label: '规划基金项目', submitted: 1259, evidence: 1183, matched: 1183, missing: 76, extra: 0 },
      { label: '青年基金项目', submitted: 1747, evidence: 1620, matched: 1620, missing: 127, extra: 0 },
    ],
    comparisonDetails: acceptanceDetails.s15 as ComparisonDetail[],
    review: '差异已持久化为业务复核项，系统没有自动消解、终审或 promote。',
  },
]

const iconFor = (kind: Asset['kind']) => kind === 'xlsx'
  ? FileSpreadsheet
  : kind === 'pdf'
    ? FileText
    : Globe2

export function AcceptanceBench() {
  const [selectedId, setSelectedId] = useState('s15')
  const selected = acceptanceCases.find((item) => item.id === selectedId) ?? acceptanceCases[0]
  const current = selected.id === 's15'
      ? {
          ...selected,
          database: 'm5-p42-submission15-scopehierarchy-final-20260809-123421',
          status: 'accepted',
          humanReview: '本次真实复验确认：提交侧按上位项目类别（BZ）和项目子类别（XMLB）重建为 10 个 scope。六份官方 PDF 均由 ReviewAgent 选用；3,270 条提交身份与 3,270 条来源身份逐 scope 全部匹配。案件仍保留 waiting_human，未自动 promote。',
          assets: selected.assets.map((asset) => (
            ['46271', '46272', '46273'].some((id) => asset.url.includes(id))
              ? { ...asset, state: 'included' as const, detail: asset.url.includes('46271') ? '西部和边疆：规划基金 74、青年基金 115' : asset.url.includes('46272') ? '新疆：规划基金 2、青年基金 11' : '西藏：青年基金 1' }
              : asset
          )),
          scopes: [
            { label: '专项任务：理论体系研究', submitted: 36, evidence: 36, matched: 36, missing: 0, extra: 0 },
            { label: '专项任务：高校辅导员研究', submitted: 212, evidence: 212, matched: 212, missing: 0, extra: 0 },
            { label: '普通项目：自筹经费', submitted: 16, evidence: 16, matched: 16, missing: 0, extra: 0 },
            { label: '新疆项目：规划基金', submitted: 2, evidence: 2, matched: 2, missing: 0, extra: 0 },
            { label: '西部和边疆项目：规划基金', submitted: 74, evidence: 74, matched: 74, missing: 0, extra: 0 },
            { label: '普通项目：规划基金', submitted: 1183, evidence: 1183, matched: 1183, missing: 0, extra: 0 },
            { label: '新疆项目：青年基金', submitted: 11, evidence: 11, matched: 11, missing: 0, extra: 0 },
            { label: '西藏项目：青年基金', submitted: 1, evidence: 1, matched: 1, missing: 0, extra: 0 },
            { label: '西部和边疆项目：青年基金', submitted: 115, evidence: 115, matched: 115, missing: 0, extra: 0 },
            { label: '普通项目：青年基金', submitted: 1620, evidence: 1620, matched: 1620, missing: 0, extra: 0 },
          ] as Scope[],
          comparisonDetails: [],
        }
    : selected
  const total = current.scopes.reduce((sum, item) => sum + item.submitted, 0)
  const evidence = current.scopes.reduce((sum, item) => sum + item.evidence, 0)
  const matched = current.scopes.reduce((sum, item) => sum + item.matched, 0)
  const missing = current.scopes.reduce((sum, item) => sum + item.missing, 0)
  const extra = current.scopes.reduce((sum, item) => sum + item.extra, 0)
  const categoryConflicts = current.scopes.reduce(
    (sum, item) => sum + (item.categoryConflicts ?? 0), 0,
  )

  return (
    <section className="acceptance-bench">
      <div className="acceptance-banner">
        <div className="acceptance-banner-main">
          <span className="acceptance-kicker"><ShieldCheck size={14} /> M5 最小 MVP · 只读验收快照</span>
          <h2>两类真实材料已完成审查闭环</h2>
          <p>资产关系、身份比较、差异持久化和材料复核均来自隔离时间戳库。</p>
        </div>
        <div className="acceptance-gate">
          <span>全量 M5</span>
          <strong>NO-GO</strong>
          <small>未执行 promote</small>
        </div>
      </div>

      <div className="acceptance-overview" aria-label="验收概览">
        <div><span>真实案件</span><strong>2</strong><small>Excel 与多 PDF</small></div>
        <div><span>待人工复核</span><strong>2</strong><small>均为 waiting_human</small></div>
        <div><span>已完成 scope</span><strong>14</strong><small>两份验收快照均持久化比较结果</small></div>
        <div><span>自动入库</span><strong>0</strong><small>保留人工边界</small></div>
      </div>

      <div className="acceptance-case-tabs" role="tablist" aria-label="待人工复核案件">
        {acceptanceCases.map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={item.id === current.id}
            className={item.id === current.id ? 'is-active' : ''}
            onClick={() => setSelectedId(item.id)}
          >
            <span className={`case-tab-mark case-tab-${item.status}`}>
              {item.status === 'matched' || item.status === 'accepted' ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}
            </span>
            <span><strong>{item.name}</strong><small>{item.resourceCode} · waiting_human</small></span>
            <ChevronRight size={17} />
          </button>
        ))}
      </div>

      <div className="acceptance-case-head">
        <div>
          <span className="acceptance-kicker">待人工复核案件</span>
          <h3>{current.name}</h3>
          <p>{current.source}</p>
        </div>
        <div className={`acceptance-outcome acceptance-outcome-${current.status}`}>
          {current.status === 'matched' || current.status === 'accepted' ? <CheckCircle2 size={18} /> : <CircleAlert size={18} />}
          <span>{current.status === 'matched'
            ? '比较完成，无差异'
            : current.status === 'accepted'
              ? '比较完成，等待人工终审边界'
              : '比较完成，待复核差异'}</span>
        </div>
      </div>

      <div className={`acceptance-human-boundary ${current.status === 'accepted' ? 'accepted-boundary' : ''}`}>
        <CircleAlert size={18} />
        <div><strong>为什么仍是疑难案件</strong><span>{current.humanReview}</span></div>
        <span className="waiting-state">waiting_human</span>
      </div>

      <div className="acceptance-metrics">
        <div><span>提交身份</span><strong>{total.toLocaleString()}</strong></div>
        <div><span>来源身份</span><strong>{evidence.toLocaleString()}</strong></div>
        <div><span>已匹配</span><strong>{matched.toLocaleString()}</strong></div>
        <div className={missing || extra || categoryConflicts ? 'metric-attention' : ''}><span>待复核差异</span><strong>{(missing + extra + categoryConflicts).toLocaleString()}</strong></div>
      </div>

      <div className="acceptance-grid">
        <article className="acceptance-panel asset-panel">
          <header><div><span>语义资产路由</span><h3>选用、排除与补充</h3></div><Sparkles size={19} /></header>
          <div className="asset-route-list">
            {current.assets.map((asset) => {
              const Icon = iconFor(asset.kind)
              return <div className="asset-route" key={`${asset.label}-${asset.url}`}>
                <span className={`asset-icon asset-${asset.kind}`}><Icon size={16} /></span>
                <div><strong>{asset.label}</strong><small>{asset.detail}</small></div>
                <span className={`route-state route-${asset.state}`}>
                  {asset.state === 'included' ? '选用' : asset.state === 'cross_scope' ? '跨类别核验' : asset.state === 'excluded' ? '排除' : '补充'}
                </span>
              </div>
            })}
          </div>
        </article>

        <article className="acceptance-panel trace-panel">
          <header><div><span>真实执行 Trace</span><h3>协议与边界</h3></div><ShieldCheck size={19} /></header>
          <dl className="trace-fact-list">
            <div><dt>Prompt</dt><dd>review-agent-v2</dd></div>
            <div><dt>Validation</dt><dd><CheckCircle2 size={14} /> accepted</dd></div>
            <div><dt>请求轮次</dt><dd>{current.trace.requests}</dd></div>
            <div><dt>发现资产</dt><dd>{current.trace.assets}</dd></div>
            <div><dt>关系判断</dt><dd>{current.trace.assessments}</dd></div>
            <div><dt>验收数据库</dt><dd className="trace-db">{current.database}</dd></div>
          </dl>
          <div className="read-only-notice"><ShieldCheck size={15} /> 仅展示已持久化结果，不提供终审或入库操作。</div>
        </article>
      </div>

      <div className="acceptance-grid acceptance-audit-grid">
        <article className="acceptance-panel source-panel">
          <header><div><span>实际比较来源</span><h3>已访问 URL</h3></div><Globe2 size={19} /></header>
          <p className="source-panel-note">以下是本案使用的登记页和附件 URL。选用材料进入本地身份比较；补充与排除材料仅用于届次和上下文判断。</p>
          <div className="acceptance-source-list">
            {current.assets.map((asset) => (
              <a key={`source-${asset.url}`} href={asset.url} target="_blank" rel="noreferrer" title={asset.url} aria-label={asset.url} className={`acceptance-source source-${asset.state}`}>
                <span>{asset.state === 'included' ? '比较' : asset.state === 'cross_scope' ? '类别核验' : asset.state === 'excluded' ? '排除依据' : '上下文'}</span>
                <div><strong>{asset.label}</strong><small>{displayUrl(asset.url)}</small></div>
                <ExternalLink size={15} aria-label={`打开 ${asset.label}`} />
              </a>
            ))}
          </div>
        </article>

        <article className="acceptance-panel network-panel">
          <header><div><span>网络边界</span><h3>未检索其他网址</h3></div><SearchX size={19} /></header>
          <div className="network-result"><SearchX size={24} /><div><strong>开放搜索：否</strong><span>仅访问登记 URL 及其页面发现的附件 URL。</span></div></div>
          <dl className="network-facts">
            <div><dt>searches</dt><dd>0 / 3</dd></div>
            <div><dt>candidate_urls</dt><dd>0</dd></div>
            <div><dt>补充候选</dt><dd>无</dd></div>
          </dl>
          <p>Agent 未调用搜索工具，也没有向外部网站扩展候选来源。页面内附件发现属于登记来源的 M4 资产发现，不属于联网检索。</p>
        </article>
      </div>

      <article className="acceptance-panel flow-panel">
        <header><div><span>比较过程</span><h3>从登记 URL 到逐 scope 结果</h3></div><ArrowRight size={19} /></header>
        <ol className="acceptance-flow">
          <li><span>1</span><div><strong>M4 资产发现</strong><p>访问登记页，发现附件，下载并解析；每份可用材料保留 URL、SHA-256、本地路径和解析 metadata。</p></div></li>
          <li><span>2</span><div><strong>ReviewAgent 语义路由</strong><p><code>review-agent-v2</code> 在受限资产索引与读取工具内判断材料与 scope 的关系，返回选用、跨类别核验、排除或补充。</p></div></li>
          <li><span>3</span><div><strong>本地身份比较</strong><p>读取选用资产并交叉核验同案官方独立类别。跨类别命中不增加 matched，而是从 missing 移入类别冲突；其余记录按 scope 身份字段计算 evidence、matched、missing、extra。</p></div></li>
          <li><span>4</span><div><strong>Verifier 与持久化</strong><p>Verifier 接受协议结果后写入逐 scope 比较与差异；案件停在 <code>waiting_human</code>，不自动 promote。</p></div></li>
        </ol>
      </article>

      <article className="acceptance-panel local-review-panel">
        <header><div><span>提交侧本地审查</span><h3>L1-L4：不调用网络，不调用 LLM</h3></div><ShieldCheck size={19} /></header>
        <div className="local-rule-grid">
          <div><span>L1</span><strong>字段格式</strong><p>逐行检查必填值、资源项码、姓名空格与引号、多值分隔符、年份、等级等确定性格式规则。</p></div>
          <div><span>L2</span><strong>文件内一致性</strong><p>核对文件名与表内资源项、年份是否一致，并确认同一文件没有混入多个资源项码。</p></div>
          <div><span>L3</span><strong>批次查全</strong><p>按资源项码汇总同一批次的所有文件行数，与采集清单的应采数量比较；无清单基准时不硬判。</p></div>
          <div><span>L4</span><strong>去重与身份完整性</strong><p>按模板身份方案构造业务键，检查文件内重复、正式库跨批次重复，以及主身份相同但校验字段冲突。</p></div>
        </div>
        <div className="identity-explainer">
          <div><span>identity-v2</span><strong>最终身份如何匹配</strong></div>
          <ol>
            <li>模板为每类数据定义有序的主身份字段组合。系统从第一组字段齐全的组合构造身份，而不是让模型猜测。</li>
            <li>文本做保守规范化：去除空白和标点、统一大小写；保留原始展示值，便于人工回看。</li>
            <li>按 `audit_scope` 把提交身份和 M5 选中的本地证据身份分到同一业务范围。S03 的来源例子是团队名称与组别、组织单位；项目案使用该项目 scope 的身份方案。</li>
            <li>在同一个 scope 内做集合比较：提交集合与来源集合的交集为已匹配，提交减来源为缺失，来源减提交为额外；同主身份的冲突字段单列为冲突。</li>
          </ol>
        </div>
      </article>

      <article className="acceptance-panel scope-panel">
        <header><div><span>身份比较</span><h3>逐 scope 结果</h3></div><span className="scope-total">{current.scopes.length} 个 scope</span></header>
        <div className="scope-table-wrap">
          <table className="acceptance-table">
            <thead><tr><th>审查 scope</th><th>提交身份</th><th>来源身份</th><th>已匹配</th><th>缺失</th><th>额外</th><th>结果</th></tr></thead>
            <tbody>{current.scopes.map((scope) => {
              const hasConflict = (scope.categoryConflicts ?? 0) > 0
              const hasDifference = scope.missing > 0 || scope.extra > 0 || hasConflict
              return <tr key={scope.label}>
                <td><strong>{scope.label}</strong></td>
                <td>{scope.submitted.toLocaleString()}</td>
                <td>{scope.evidence.toLocaleString()}</td>
                <td className="scope-matched">{scope.matched.toLocaleString()}</td>
                <td className={scope.missing ? 'scope-difference' : ''}>{scope.missing || '-'}</td>
                <td className={scope.extra ? 'scope-difference' : ''}>{scope.extra || '-'}</td>
                <td><span className={`scope-status ${hasDifference ? 'has-difference' : 'is-matched'}`}>{hasConflict ? `类别冲突 (${scope.categoryConflicts})` : hasDifference ? '发现差异' : '一致'}</span></td>
              </tr>
            })}</tbody>
          </table>
        </div>
      </article>

      <article className="acceptance-panel difference-panel">
        <header><div><span>可审计问题明细</span><h3>缺失、额外与来源定位</h3></div><CircleAlert size={19} /></header>
        {current.comparisonDetails.length ? (
          <div className="difference-detail-list">
            {current.comparisonDetails.map((detail) => (
              <details key={detail.scopeLabel} open={detail.scopeLabel === '专项任务：高校辅导员研究'}>
                <summary>
                  <div><strong>{detail.scopeLabel}</strong><small>{detail.missing.length} 条缺失 · {detail.extra.length} 条额外{detail.relatedOutOfScope?.length ? ` · ${detail.relatedOutOfScope.length} 条类别冲突` : ''}</small></div>
                  <ChevronRight size={17} />
                </summary>
                <div className="difference-detail-body">
                  <p>{detail.identityRule}</p>
                  <a href={detail.sourceUrl} target="_blank" rel="noreferrer" title={detail.sourceUrl} aria-label={detail.sourceUrl} className="difference-source"><ExternalLink size={14} /> 比较来源：{detail.sourceAsset}<small>{displayUrl(detail.sourceUrl)}</small></a>
                  <div className="difference-columns">
                    <section><h4>提交有、来源未匹配</h4><ul>{detail.missing.map((item) => <li key={item}>{item}</li>)}</ul></section>
                    <section className="extra-list"><h4>来源有、提交未匹配</h4>{detail.extra.length ? <ul>{detail.extra.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无额外记录。</p>}</section>
                  </div>
                  {detail.relatedOutOfScope?.length ? <section className="related-out-of-scope-list"><h4>同案官方来源已找到，但项目类别不一致</h4><ul>{detail.relatedOutOfScope.map((item) => <li key={`${item.identity}-${item.source_url}`}><strong>{item.identity}</strong><a href={item.source_url} target="_blank" rel="noreferrer" title={item.source_url} aria-label={item.source_url}>{item.source_label}</a><small>{item.reason}</small></li>)}</ul></section> : null}
                </div>
              </details>
            ))}
          </div>
        ) : <div className="no-difference-detail"><CheckCircle2 size={18} /><div><strong>未发现身份差异</strong><span>四个 scope 均完成本地比较：307 项提交身份全部在选用的官方 XLSX 中匹配。</span></div></div>}
      </article>

      <div className="acceptance-review-note">
        <span className="review-note-icon"><FileText size={18} /></span>
        <div><strong>材料复核记录</strong><p>{current.review}</p></div>
        <ArrowRight size={18} />
        <span>案件保持 waiting_human</span>
      </div>
    </section>
  )
}
