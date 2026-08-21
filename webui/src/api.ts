export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
  reviewer = '',
): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (reviewer) headers.set('X-Reviewer', encodeURIComponent(reviewer))
  const response = await fetch(path, { ...options, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const message = payload.message || payload.detail || `请求失败 (${response.status})`
    throw new ApiError(response.status, typeof message === 'string' ? message : JSON.stringify(message))
  }
  return response.json() as Promise<T>
}

export type Batch = {
  id: number
  name: string
  status: string
  n_files: number
  n_rows: number
  imported_at: string
  issue_counts: Record<string, number>
  case_counts: Record<string, number>
  l5_count: number
  stages: Record<'local' | 'm4' | 'm5', BatchStage>
  promotion_readiness: PromotionReadiness
}

export type BatchStage = {
  status: 'pending' | 'running' | 'done' | 'failed' | 'partial'
  attempt: number
  error_code: string
  item_counts?: Record<string, number>
  case_counts?: Record<string, number>
  required?: boolean
}

export type PromotionReadiness = {
  can_promote: boolean
  reasons: string[]
}

export type AuditTarget = {
  resource_code: string
  year: string
  award_name: string
  urls: string[]
  domains: string[]
  submitted_count: number
  probe_status: 'not_checked' | 'passable'
}

export type AuditPreview = {
  batch_id: number
  candidate_targets: AuditTarget[]
  issues: Array<Record<string, unknown>>
  probe_status: 'not_checked'
  preview_digest: string
}

export type M4CaseBinding = {
  case_id: number
  case_status: string
  origin_m4_result_id: number
  is_current: boolean
}

export type M4ResultItem = {
  stage_item_id: number
  resource_code: string
  year: string
  stage_status: string
  attempt: number
  stage_error_code: string
  stage_error_message: string
  current_result_id: number
  history_count: number
  award_name?: string
  verdict?: string
  confidence?: string
  triage?: string
  review_status?: string
  identity_version?: string
  source_kind?: string
  source_url?: string
  source_urls?: string[]
  found_assets?: string[]
  page_year?: string
  extracted_count?: number
  submitted_count?: number
  missing?: string[]
  extra?: string[]
  reason_codes?: string[]
  notes?: string
  created_at?: string
  binding?: M4CaseBinding | null
}

export type M4Results = {
  batch_id: number
  history_count: number
  items: M4ResultItem[]
}

export type Issue = {
  staging_id: number
  batch_id: number
  file: string
  sheet: string
  row_no: number
  resource_code: string
  rule_id: string
  severity: string
  message: string
  field_code?: string
  suggestion?: string
}

export type AuditCase = {
  case_id: number
  batch_id: number
  resource_code: string
  award_name: string
  year: string
  trigger_codes: string[]
  status: string
  confidence: string
  step_count: number
  token_used: number
  elapsed_ms: number
  reflection_count: number
  recommendation: string
  human_decision: string
  human_decision_summary: string
  reviewed_by: string
  reviewed_at: string
  state_version: number
  updated_at: string
}

export type Artifact = {
  artifact_id: number
  kind: string
  source_url: string
  file_name: string
  content_type: string
  sha256: string
  size_bytes: number
  fetched_at: string
  metadata: Record<string, unknown>
  preview_url: string
}

export type CaseDetail = AuditCase & {
  active_attempt_id: number
  attempt_sequence: number
  origin_m4_result_id: number
  m4_evidence?: {
    result_id: number
    verdict: string
    confidence: string
    source_kind: string
    source_urls: string[]
    found_assets: string[]
    extracted_count: number
    submitted_count: number
    missing: string[]
    extra: string[]
    reason_codes: string[]
    notes: string
  } | null
  objective: string
  submitted_summary: Record<string, unknown>
  known_urls: string[]
  open_questions: string[]
  reason_codes: string[]
  budget: { calls: number; searches: number; limits: Record<string, number> }
  tool_trace: ToolTrace[]
  artifacts: Artifact[]
  latest_verification?: VerificationReport
  retrieved_memories: Array<Record<string, unknown>>
  evidence_progress: EvidenceProgress
  attempts: AuditAttempt[]
  evidence_workflow: EvidenceWorkflow
  evidence_groups: EvidenceGroup[]
  evidence_asset_routes: EvidenceAssetRoute[]
  submission_conservation: SubmissionConservation
  comparison?: EvidenceComparison | null
  scopes: AuditScope[]
  scope_comparisons: ScopeComparison[]
  conclusion_readiness: 'ready_for_human' | 'incomplete'
}

export type SubmissionConservation = {
  total_rows: number
  assigned_rows: number
  ambiguous_rows: number
  unassigned_rows: number
  unresolved_rows: Array<Record<string, unknown>>
  closed: boolean
}

export type EvidenceAssetRoute = {
  route_id: number
  asset_id: number
  scope_id?: number | null
  scope_key: string
  role_type: string
  url: string
  parent_url: string
  label: string
  kind: string
  subunit_type: 'document' | 'section' | 'sheet' | 'image_batch'
  selector: Record<string, unknown>
  identity_fields: string[][]
  route_source: 'exact_rule' | 'llm' | 'human'
  confidence: number
  route_status: string
  processing_status: string
  reason: string
  blockers: string[]
}

export type AuditAttempt = {
  attempt_id: number
  sequence: number
  kind: 'initial' | 'supplement' | 'resume' | 'legacy'
  supplement_request: string
  status: string
  phase: string
  budget_limits: Record<string, unknown>
  budget_usage: Record<string, unknown>
  step_count: number
  token_used: number
  elapsed_ms: number
  stop_reason: string
  verifier_status: string
  conclusion_readiness: string
  blockers: string[]
  started_at: string
  finished_at: string
}

export type EvidenceWorkflow = {
  assets: { total: number; processed: number; failed: number; excluded: number; pending: number }
  sources: Record<string, number>
  blockers: string[]
  ledger_closed: boolean
  groups_collecting: number
  routes: Record<string, number>
  row_conservation: SubmissionConservation
}

export type AuditScope = {
  scope_id: number
  scope_key: string
  role_type: string
  role_label: string
  required: boolean
  business_scope: Record<string, unknown>
  submitted_row_count: number
  submitted_identity_count: number
  unidentified_row_count: number
  status: string
  blockers: string[]
}

export type ScopeComparison = {
  scope_id: number
  scope_key: string
  role_type: string
  role_label: string
  required: boolean
  status: string
  evidence_complete: boolean
  comparison_result: 'matched' | 'differences_found' | 'conflict' | 'not_compared'
  submitted_row_count: number
  submitted_identity_count: number
  evidence_identity_count: number
  matched_count: number
  missing: string[]
  extra: string[]
  conflicts: string[]
  identity_conflicts: IdentityFieldConflict[]
  semantic_identity_decisions: SemanticIdentityDecision[]
  comparison_differences: ComparisonDifference[]
  source_urls: string[]
  blockers: string[]
  verifier: Record<string, unknown>
}

export type SemanticIdentityDecision = {
  candidate_id: string
  submitted: string
  source: string
  decision: 'same_identity' | 'field_conflict' | 'different' | 'uncertain'
  confidence: number
  reason: string
  source_url: string
  source_anchor: string
}

export type IdentityFieldConflict = {
  submitted: string
  source: string
  fields: string
  reason: string
  source_url: string
}

export type ComparisonDifference = {
  difference_type: 'field_conflict' | 'missing_from_source' | 'extra_in_source'
  submitted: string
  source: string
  fields: string
  reason: string
  source_urls: string[]
}

export type EvidenceGroup = {
  group_id: number
  attempt_id: number
  group_key: string
  scope_id?: number | null
  scope_key: string
  role_type: string
  parent_url: string
  title: string
  scope: Record<string, unknown>
  status: string
  expected_assets: number
  terminal_assets: number
  extracted_count: number
}

export type EvidenceComparison = {
  comparison_id: number
  attempt_id: number
  group_id?: number | null
  status: string
  submitted_count: number
  evidence_count: number
  matched_count: number
  missing: string[]
  extra: string[]
  contradictions: string[]
  blockers: string[]
}

export type EvidenceCandidate = {
  url: string
  source_level: string
  provider: string
  rank: number
  title: string
  query: string
  status: 'pending' | 'succeeded' | 'failed' | 'skipped'
  attempts: number
  status_reason: string
  relevance: 'relevant' | 'unreviewed' | 'excluded'
  relevance_score: number
}

export type EvidenceProgress = {
  phase: string
  candidates: EvidenceCandidate[]
  search_round: number
  source_failures: number
  successful_sources: number
}

export type SupplementRequest = {
  code: string
  question: string
  suggested_tools: string[]
}

export type VerificationReport = {
  target_match: string
  year_match: string
  source_authority: string
  coverage_complete: string
  contradictions: string[]
  missing_evidence: string[]
  supplement_requests: SupplementRequest[]
  recommended_action: string
  reason_codes: string[]
  deterministic_action: string
  model_used: boolean
}

export type ToolTrace = {
  call_id: string
  tool_name: string
  started_at: string
  finished_at: string
  duration_ms: number
  input_summary: Record<string, unknown>
  output_summary: Record<string, unknown>
  ok: boolean
  error_code: string
}

export type Memory = {
  memory_id: number
  status: string
  category_code: string
  resource_type: string
  field_code: string
  symptom_text: string
  resolution: string
  occurrence_count: number
  source_case_ids: number[]
  approved_by: string
  merged_into_id?: number
  state_version: number
}

export type Job = {
  job_id: number
  kind: string
  batch_id?: number
  case_id?: number
  status: string
  progress: number
  progress_message: string
  error_code: string
  state_version: number
  created_by: string
  updated_at: string
}
