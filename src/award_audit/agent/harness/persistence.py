"""Typed persistence adapter over the additive M5.4 Store migration."""

from __future__ import annotations

from award_audit.agent.harness.models import AuditCaseState, CaseSeed
from award_audit.agent.toolkit.contracts import (
    EvidenceArtifact,
    ToolBudgetLimits,
    ToolBudgetState,
    ToolObservation,
    ToolResult,
)
from award_audit.agent.verification.models import VerificationReport
from award_audit.core.pipeline.store import Store


class CaseRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create_or_get(
        self,
        seed: CaseSeed,
        *,
        tool_limits: ToolBudgetLimits | None = None,
    ) -> tuple[AuditCaseState, bool]:
        state = AuditCaseState.from_seed(
            seed,
            ToolBudgetState(limits=tool_limits or ToolBudgetLimits()),
        )
        case_id, created = self.store.create_or_get_audit_case(
            state.model_dump(mode="json")
        )
        if not created:
            return self.load(case_id), False
        state.case_id = case_id
        return state, True

    def load(self, case_id: int) -> AuditCaseState:
        snapshot = self.store.get_audit_case_snapshot(case_id)
        if snapshot is None:
            raise KeyError(f"audit case not found: {case_id}")
        return AuditCaseState.model_validate(snapshot)

    def save(
        self,
        state: AuditCaseState,
        *,
        traces: list[ToolObservation] | None = None,
        artifacts: list[EvidenceArtifact] | None = None,
        verifications: list[VerificationReport] | None = None,
    ) -> None:
        version = self.store.save_audit_case_execution(
            state.case_id,
            state.model_dump(mode="json"),
            expected_version=state.state_version,
            attempt_id=state.active_attempt_id or None,
            traces=[item.model_dump(mode="json") for item in traces or []],
            artifacts=[item.model_dump(mode="json") for item in artifacts or []],
            verification_reports=[
                item.model_dump(mode="json") for item in verifications or []
            ],
        )
        state.state_version = version
        asset_records: list[dict[str, object]] = []
        bound_pages = set(
            state.m4_evidence.source_urls if state.m4_evidence is not None else []
        )
        if state.m4_evidence is not None:
            for item in state.m4_evidence.assets:
                payload = item.model_dump(mode="json")
                payload["metadata"] = {
                    **payload.get("metadata", {}),
                    "m4_verified_parent_bound": bool(
                        item.parent_url and item.parent_url in bound_pages
                    ),
                }
                asset_records.append(payload)
        failed_media = set(state.evidence_progress.media_failed_urls)
        failed_attachments = set(state.evidence_progress.failed_attachment_urls)
        failed_assets = failed_media | failed_attachments
        for url in [
            *state.evidence_progress.pending_attachment_urls,
            *state.evidence_progress.failed_attachment_urls,
            *state.evidence_progress.pending_media_urls,
        ]:
            parent = (
                state.evidence_progress.pending_attachment_parent_urls.get(url)
                or state.evidence_progress.pending_media_parent_urls.get(url, "")
            )
            asset_records.append({
                "url": url,
                "parent_url": parent,
                "kind": (
                    "image"
                    if url in state.evidence_progress.pending_media_urls
                    else "attachment"
                ),
                "status": "failed" if url in failed_assets else "discovered",
                "error_code": (
                    "MEDIA_PROCESSING_FAILED" if url in failed_media
                    else "ATTACHMENT_PROCESSING_FAILED" if url in failed_attachments
                    else ""
                ),
            })
        if state.active_attempt_id:
            summary = state.submitted_summary
            scope = {
                "year": state.year,
                "edition": summary.get("edition") or summary.get("session") or "",
                "stage": summary.get("stage") or summary.get("round") or "",
                "category": summary.get("category") or summary.get("award_level") or "",
                "track": summary.get("track") or summary.get("group") or "",
            }
            parsed_paths = {
                str(trace.input_summary.get("path", ""))
                for trace in state.tool_trace
                if trace.tool_name in {"extract_pdf_text", "parse_spreadsheet"}
                and trace.ok and trace.input_summary.get("path")
            }
            pending_urls = {
                *state.evidence_progress.pending_attachment_urls,
                *state.evidence_progress.pending_media_urls,
            }
            artifact_payloads: list[dict[str, object]] = []
            for artifact in state.artifacts:
                payload = artifact.model_dump(mode="json")
                metadata = payload.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                parent_url = str(metadata.get("page_url", ""))
                if bound_pages:
                    metadata = {
                        **metadata,
                        "m4_verified_parent_bound": bool(
                            parent_url and parent_url in bound_pages
                        ),
                    }
                payload["metadata"] = metadata
                kind = artifact.kind.casefold()
                explicit_routes = artifact.metadata.get("routes", [])
                all_routes_excluded = bool(
                    isinstance(explicit_routes, list)
                    and explicit_routes
                    and all(
                        isinstance(route, dict)
                        and route.get("route_status") == "excluded"
                        for route in explicit_routes
                    )
                )
                processed = (
                    artifact.local_path in parsed_paths
                    or (kind in {"xlsx", "xls"} and any(
                        trace.tool_name == "collect_spreadsheet_attachments" and trace.ok
                        for trace in state.tool_trace
                    ))
                    or (kind in {"png", "jpeg", "jpg", "gif", "webp"}
                        and artifact.source_url not in pending_urls
                        and artifact.source_url not in failed_media)
                )
                payload["status"] = (
                    "failed"
                    if artifact.source_url in failed_media
                    else "excluded" if all_routes_excluded
                    else "processed" if processed else "downloaded"
                )
                if artifact.source_url in failed_media:
                    payload["error_code"] = "MEDIA_PROCESSING_FAILED"
                artifact_payloads.append(payload)
            self.store.sync_evidence_ledger(
                state.case_id,
                state.active_attempt_id,
                known_urls=state.known_urls,
                candidates=[
                    item.model_dump(mode="json")
                    for item in state.evidence_progress.candidates
                ],
                asset_records=asset_records,
                artifacts=artifact_payloads,
                scope=scope,
            )

    def start_attempt(
        self,
        state: AuditCaseState,
        *,
        kind: str,
        supplement_request: str,
    ) -> None:
        attempt = self.store.start_audit_attempt(
            state.case_id,
            kind=kind,
            supplement_request=supplement_request,
            budget_limits=state.budget.limits.model_dump(mode="json"),
        )
        state.active_attempt_id = int(attempt["attempt_id"])
        state.attempt_sequence = int(attempt["sequence"])
        role_scopes = state.submitted_summary.get("role_scopes", [])
        if not role_scopes:
            role_scopes = [{
                "scope_key": "work_or_project:legacy-default",
                "role_type": "work_or_project",
                "role_label": "主审核范围",
                "required": True,
                "profile": {
                    "primary_alternatives": state.submitted_summary.get(
                        "identity_primary_alternatives", []
                    ),
                    "scope_fields": state.submitted_summary.get(
                        "identity_scope_fields", []
                    ),
                },
                "business_scope": {"year": state.year},
                "submitted_row_count": int(
                    state.submitted_summary.get("submitted_rows", 0) or 0
                ),
                "submitted_identity_count": int(
                    state.submitted_summary.get("expected_scope_count", 0) or 0
                ),
                "submitted_identities": {},
            }]
        persisted_scopes = self.store.sync_audit_scopes(
            state.case_id,
            role_scopes,
            identity_version=str(
                state.submitted_summary.get("identity_version", "identity-v2")
            ),
        )
        scope_ids = {
            item["scope_key"]: item["scope_id"] for item in persisted_scopes
        }
        state.submitted_summary["role_scopes"] = [
            {**scope, "scope_id": scope_ids.get(str(scope.get("scope_key", "")), 0)}
            for scope in role_scopes
        ]
        row_assignments = state.submitted_summary.get("row_assignments", [])
        if isinstance(row_assignments, list):
            self.store.sync_scope_assignments(
                state.case_id,
                [item for item in row_assignments if isinstance(item, dict)],
            )

    def finish_attempt(
        self,
        state: AuditCaseState,
        *,
        stopped_reason: str,
        failed: bool = False,
    ) -> None:
        if not state.active_attempt_id:
            return
        summary = self.store.evidence_workflow_summary(
            state.case_id, attempt_id=state.active_attempt_id
        )
        verifier_persisted = state.latest_verification is not None
        scopes = self.store.list_audit_scopes(state.case_id)
        applicable_scopes = [
            item for item in scopes
            if item["required"] or item["submitted_identity_count"] > 0
        ]
        comparisons = self.store.list_scope_comparisons(
            state.case_id, attempt_id=state.active_attempt_id
        )
        comparison_persisted = bool(applicable_scopes) and (
            len(comparisons) == len(applicable_scopes)
        )
        blockers = list(summary["blockers"])
        if not verifier_persisted:
            blockers.append("verifier_missing")
        if not comparison_persisted:
            blockers.append("comparison_missing")
        for comparison in comparisons:
            if comparison["status"] != "complete":
                blockers.extend(comparison["blockers"] or [
                    f"scope_{comparison['scope_id']}_comparison_incomplete"
                ])
            if not comparison["evidence_complete"]:
                blockers.append(f"scope_{comparison['scope_id']}_evidence_incomplete")
            if not comparison["verifier"]:
                blockers.append(f"scope_{comparison['scope_id']}_verifier_missing")
        budget_stop = stopped_reason in {
            "agent_token_budget_exhausted", "tool_budget_exhausted",
            "wall_time_budget_exhausted", "pdf_page_budget_exhausted",
            "vision_budget_exhausted",
        }
        if budget_stop:
            blockers.append(stopped_reason)
        ready = (
            verifier_persisted
            and comparison_persisted
            and bool(summary["ledger_closed"])
            and not blockers
        )
        self.store.finish_audit_attempt(
            state.active_attempt_id,
            status="failed" if failed else ("succeeded" if ready else "incomplete"),
            phase=state.evidence_progress.phase,
            budget_usage=state.budget.model_dump(mode="json"),
            step_count=state.step_count,
            token_used=state.token_used,
            elapsed_ms=state.elapsed_ms,
            stop_reason=stopped_reason,
            verifier_status="persisted" if verifier_persisted else "missing",
            conclusion_readiness="ready_for_human" if ready else "incomplete",
            blockers=blockers,
        )

    def record_comparison(
        self,
        state: AuditCaseState,
        tool_results: list[ToolResult],
        verification: VerificationReport,
    ) -> None:
        if not state.active_attempt_id:
            return
        facts = [
            fact.model_dump(mode="json")
            for result in tool_results
            if "search_results_are_leads_not_evidence" not in result.warnings
            for fact in result.evidence_facts
        ]
        grouped: dict[tuple[int, str], dict[str, object]] = {}
        for result in tool_results:
            group = str(result.data.get("evidence_group", "")).strip()
            scope_id = int(result.data.get("scope_id", 0) or 0)
            role_type = str(result.data.get("role_type", ""))
            hashes = result.data.get("matched_identity_hashes")
            identities = result.data.get("submitted_identity_items")
            if (
                not group
                or result.data.get("document_complete") is not True
                or not isinstance(hashes, list)
                or not isinstance(identities, dict)
            ):
                continue
            aggregate = grouped.setdefault((scope_id, group), {
                "identities": {}, "matched": set(), "source_levels": [],
                "role_type": role_type,
            })
            identity_map = aggregate["identities"]
            matched_hashes = aggregate["matched"]
            source_levels = aggregate["source_levels"]
            if isinstance(identity_map, dict):
                identity_map.update({
                    str(key): str(value)
                    for key, value in identities.items()
                    if str(key) and str(value)
                })
            if isinstance(matched_hashes, set):
                matched_hashes.update(str(item) for item in hashes if str(item))
            if isinstance(source_levels, list):
                source_levels.extend(
                    fact.source_level for fact in result.evidence_facts if fact.is_evidence
                )
        for (scope_id, group), aggregate in grouped.items():
            identity_map = aggregate["identities"]
            matched_hashes = aggregate["matched"]
            if not isinstance(identity_map, dict) or not isinstance(matched_hashes, set):
                continue
            matched = [
                display for identity_hash, display in identity_map.items()
                if identity_hash in matched_hashes
            ]
            missing = [
                display for identity_hash, display in identity_map.items()
                if identity_hash not in matched_hashes
            ]
            levels = aggregate["source_levels"]
            source_level = (
                next((str(item) for item in levels if str(item) == "official_primary"), "")
                if isinstance(levels, list) else ""
            ) or (str(levels[0]) if isinstance(levels, list) and levels else "unknown")
            facts.append({
                "status": "complete" if not missing else "partial",
                "document_complete": True,
                "source_url": group,
                "scope_id": scope_id,
                "role_type": str(aggregate.get("role_type", "")),
                "source_level": source_level,
                "expected_count": len(identity_map),
                "observed_count": len(matched_hashes),
                "matched_items": matched,
                "missing_items": missing,
                "missing_item_count": len(missing),
                "coverage_complete": not missing,
                "extraction_method": "grouped_attachment_identity_union",
            })
        self.store.record_evidence_comparison(
            state.case_id,
            state.active_attempt_id,
            facts=facts,
            fallback_missing=verification.missing_evidence,
            fallback_contradictions=verification.contradictions,
        )
        self.store.record_scope_comparisons(
            state.case_id,
            state.active_attempt_id,
            facts=facts,
            verifier=verification.model_dump(mode="json"),
        )

    def request_supplement(
        self,
        case_id: int,
        request: str,
        *,
        expected_version: int,
    ) -> AuditCaseState:
        self.store.request_audit_case_supplement(
            case_id, request, expected_version=expected_version
        )
        return self.load(case_id)

    def finalize(
        self,
        case_id: int,
        decision: str,
        summary: str,
        reviewer: str,
        *,
        expected_version: int,
    ) -> AuditCaseState:
        self.store.finalize_audit_case(
            case_id,
            decision,
            summary,
            reviewer,
            expected_version=expected_version,
        )
        return self.load(case_id)
