"""M5 统一验收：e2e 完整决策链与 m5_regression 兼容回归模式。

这不是新探针，而是把原 ``scripts/probe_m5_real_agent.py`` 的已验证逻辑提炼进包，
供 CLI（``award-audit audit --m5``）与旧探针薄壳共用。e2e 默认先跑 L0-L4 与 M4，
仅把实际分流目标送入 M5；M4 高置信目标按设计跳过 M5，仍等待人工结论。

安全门：真实 API 只在 ``confirm_real_api=True`` 时发生（约 10 万 token）。默认走
``dry_check``——不调 API，用内存库真实执行 L0-L4 与离线预检。
脱敏产出不含 API Key、名单正文、模型原文与本地绝对路径。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from award_audit.agent import llm as llm_module
from award_audit.agent.harness.models import CaseSeed, HarnessLimits, HarnessOutcome
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import build_default_harness
from award_audit.agent.integration import seed_from_evidence_report
from award_audit.agent.review_workflow import (
    prepare_review_batch,
    run_audit_stage,
    run_queued_review_cases,
)
from award_audit.agent.toolkit import ToolBudgetLimits
from award_audit.core import config
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline.checks import l5_precheck
from award_audit.core.pipeline.importer import import_file
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry, load_ledger
from award_audit.core.reference.resource_map import load_resource_map
from award_audit.core.reference.template_registry import load_template_registry

DEFAULT_MANIFEST = config.PROJECT_ROOT / "tests" / "data" / "m5_real" / "submission14_manifest.json"
DEFAULT_EVIDENCE_DIR = config.PROJECT_ROOT / "out" / "m5_real_submission14" / "agent_evidence"
DEFAULT_OUTPUT = (
    config.PROJECT_ROOT / "tests" / "data" / "m5_real" / "results" / "submission14_agent_smoke.json"
)

Printer = Callable[[str], None]


# 一次验收运行的全部输入（CLI 与旧探针壳共用；真跑仅当 confirm_real_api=True）
@dataclass
class AcceptanceConfig:
    mode: Literal["e2e", "m5_regression"] = "e2e"
    cases: str = "all"
    manifest: Path = DEFAULT_MANIFEST
    submission_dir: str = ""
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR
    output: Path | None = None
    max_steps: int = 12
    max_tokens: int = 50_000
    max_tool_calls: int = 10
    confirm_real_api: bool = False
    recover_db: Path | None = None
    recover_label: str = "RECOVERED_CASE"


# 当前 UTC 时间（脱敏产出的生成时间戳）
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 把相对路径按项目根解析为绝对路径（manifest 里的 ../评奖信息核查/… 即相对项目根）
def _resolve_input(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (config.PROJECT_ROOT / path).resolve(strict=False)


# 取 URL 主机名并去掉 www. 前缀（脱敏用，只留来源域名不留完整 URL）
def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# 从 manifest 选出要跑的案例（"all" 或逗号分隔的 case id）
def _selected_cases(raw: str, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    cases = list(manifest["cases"])
    if raw.strip().lower() == "all":
        return cases
    requested = [item.strip().upper() for item in raw.split(",") if item.strip()]
    by_id = {str(case["id"]).upper(): case for case in cases}
    unknown = [case_id for case_id in requested if case_id not in by_id]
    if unknown:
        raise ValueError(f"unknown case ids: {','.join(unknown)}")
    if not requested:
        raise ValueError("at least one case id is required")
    return [by_id[case_id] for case_id in requested]


# manifest 只选择文件；运行上下文统一从提交文件、模板和采集清单推导。
def _case_seed(
    batch_id: int,
    case: dict[str, Any],
    submission_dir: Path,
    *,
    registry: Mapping[str, TemplateSpec] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
) -> CaseSeed:
    submission_file = (submission_dir / str(case["file"])).resolve()
    imported = import_file(submission_file, submission_dir.name)
    resource_code = imported.first_zylbm.strip()
    seed = seed_from_evidence_report(
        batch_id,
        {
            "resource_code": resource_code,
            "award_name": imported.award_name,
            "year": imported.year,
            "verdict": "无法核对",
            "confidence": "low",
            "source_kind": "none",
            "submitted_count": imported.n_rows,
            "extracted_count": 0,
            "missing": [],
            "extra": [],
            "reason_codes": ["evidence_review_not_started"],
        },
        imported_files=[imported],
        registry=registry or load_template_registry(),
        ledger=ledger or load_ledger(),
    )
    if seed is None:
        raise ValueError("submission file cannot produce a generic M5 case")
    return seed


def _manifest_mismatches(seed: CaseSeed, case: Mapping[str, Any]) -> list[str]:
    expected = {
        "resource_code": str(case.get("resource_code", "")),
        "award_name": str(case.get("award_name", "")),
        "year": str(case.get("year", "")),
        "submitted_rows": case.get("submitted_rows"),
        "match_fields": case.get("match_fields"),
    }
    actual = {
        "resource_code": seed.resource_code,
        "award_name": seed.award_name,
        "year": seed.year,
        "submitted_rows": seed.submitted_summary.get("submitted_rows"),
        "match_fields": seed.submitted_summary.get("match_fields"),
    }
    return [
        key
        for key, value in expected.items()
        if value not in (None, "") and value != actual[key]
    ]


_SAFE_FACT_KEYS = {
    "attachment_count",
    "award_name_match",
    "award_name_match_mode",
    "candidate_count",
    "coverage_complete",
    "expected_count",
    "manual_required",
    "next_evidence_stage",
    "observed_count",
    "observed_year",
    "official_candidate_count",
    "provider",
    "related_candidate_count",
    "relationship_confirmed",
    "scan_detected",
    "source_level",
    "strategy",
    "unqualified_candidate_count",
    "year_conflict_count",
    "year_match",
}


def _redacted_verification_facts(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    redacted: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if key in _SAFE_FACT_KEYS and (
            item is None or isinstance(item, (bool, int, float, str))
        ):
            redacted[key] = item
        elif isinstance(item, (list, tuple, set)):
            redacted[f"{key}_count"] = len(item)
    return redacted


# 把一案 HarnessOutcome 脱敏为可落盘的结构（不含名单正文、Key、模型原文、本地绝对路径）
def _redacted_result(case_id: str, outcome: Any) -> dict[str, Any]:
    state = outcome.state
    traces = [
        {
            "tool": item.tool_name,
            "ok": item.ok,
            "error_code": item.error_code,
            "duration_ms": item.duration_ms,
            "verification_facts": _redacted_verification_facts(
                item.output_summary.get("verification_facts", {})
            ),
        }
        for item in state.tool_trace
    ]
    artifacts = [
        {
            "kind": item.kind,
            "source_host": _host(item.source_url),
            "size_bytes": item.size_bytes,
            "sha256_prefix": item.sha256[:12],
        }
        for item in state.artifacts
    ]
    verification = state.latest_verification
    usage_turns = [
        item.model_dump(mode="json") for item in getattr(state, "llm_usage", [])
    ]
    input_tokens = sum(int(item["input_tokens"]) for item in usage_turns)
    cached_tokens = sum(int(item["cached_input_tokens"]) for item in usage_turns)
    cache_detail_turns = sum(
        bool(item["cache_detail_reported"]) for item in usage_turns
    )
    cache_detail_complete = bool(usage_turns) and cache_detail_turns == len(usage_turns)
    provider_usage_turns = sum(
        bool(item["provider_usage_reported"]) for item in usage_turns
    )
    provider_usage_complete = bool(usage_turns) and provider_usage_turns == len(usage_turns)
    verifier_usage = [
        item.model_dump(mode="json")
        for item in getattr(state, "verifier_llm_usage", [])
    ]
    accepted_stops = {
        "recommendation_ready",
        "verifier_requires_manual",
        "reflection_exhausted",
        "agent_requested_manual",
    }
    probe_ok = (
        state.status == "waiting_human"
        and bool(traces)
        and outcome.stopped_reason in accepted_stops
    )
    return {
        "id": case_id,
        "resource_code": state.resource_code,
        "year": state.year,
        "status": state.status,
        "stopped_reason": outcome.stopped_reason,
        "probe_ok": probe_ok,
        "confidence": state.confidence,
        "reason_codes": state.reason_codes,
        "step_count": state.step_count,
        "token_used": state.token_used,
        "llm_usage": {
            "scope": "agent_planner_only",
            "turns": usage_turns,
            "failed_turns": sum(item["outcome"] == "failed" for item in usage_turns),
            "provider_usage_turns": provider_usage_turns,
            "provider_usage_complete": provider_usage_complete,
            "token_used_is_lower_bound": not provider_usage_complete,
            "input_tokens": input_tokens,
            "output_tokens": sum(int(item["output_tokens"]) for item in usage_turns),
            "cached_input_tokens": cached_tokens,
            "reasoning_output_tokens": sum(
                int(item["reasoning_output_tokens"]) for item in usage_turns
            ),
            "cache_detail_turns": cache_detail_turns,
            "cache_detail_complete": cache_detail_complete,
            "cached_input_ratio": (
                round(cached_tokens / input_tokens, 4)
                if input_tokens and cache_detail_complete
                else None
            ),
            "prompt_chars_total": sum(int(item["prompt_chars"]) for item in usage_turns),
            "tool_schema_chars_total": sum(
                int(item["tool_schema_chars"]) for item in usage_turns
            ),
        },
        "elapsed_ms": state.elapsed_ms,
        "verifier_llm_usage": {
            "scope": "model_verifier_only",
            "calls": verifier_usage,
            "total_tokens": sum(int(item["total_tokens"]) for item in verifier_usage),
            "provider_usage_complete": bool(verifier_usage) and all(
                bool(item["provider_usage_reported"]) for item in verifier_usage
            ),
            "token_used_is_lower_bound": not verifier_usage or not all(
                bool(item["provider_usage_reported"]) for item in verifier_usage
            ),
        },
        "tool_calls": traces,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "recommendation_present": bool(state.recommendation),
        "last_error_code": state.last_error[:100],
        "last_error_detail": getattr(state, "last_error_detail", "")[:100],
        "verification": (
            {
                "recommended_action": verification.recommended_action,
                "target_match": verification.target_match,
                "year_match": verification.year_match,
                "source_authority": verification.source_authority,
                "coverage_complete": verification.coverage_complete,
                "deterministic_action": verification.deterministic_action,
                "model_used": verification.model_used,
                "reason_codes": verification.reason_codes,
                "missing_evidence_count": len(verification.missing_evidence),
            }
            if verification is not None
            else None
        ),
    }


# 汇总多案脱敏结果为整批产出（供 run/recover 复用）
def _summarize(
    results: list[dict[str, Any]],
    *,
    probe: str,
    configuration: dict[str, Any],
    require_verification: bool,
) -> dict[str, Any]:
    passed = sum(bool(row.get("probe_ok")) for row in results)
    verification_cases = sum(row.get("verification") is not None for row in results)
    budget_exhausted_cases = sum(
        "budget_exhausted" in str(row.get("stopped_reason", "")) for row in results
    )
    complete = passed == len(results) and (verification_cases >= 1 or not require_verification)
    return {
        "schema_version": 1,
        "probe": probe,
        "generated_at": _utc_now(),
        "contains_raw_roster": False,
        "contains_api_key": False,
        "configuration": configuration,
        "summary": {
            "status": "complete" if complete else "partial",
            "cases_ok": passed,
            "cases_total": len(results),
            "tool_calls_total": sum(len(row.get("tool_calls", [])) for row in results),
            "artifacts_total": sum(int(row.get("artifact_count", 0)) for row in results),
            "verification_cases": verification_cases,
            "verifier_tokens_total": sum(
                int(row.get("verifier_llm_usage", {}).get("total_tokens", 0))
                for row in results
            ),
            "budget_exhausted_cases": budget_exhausted_cases,
            "year_isolation_ok": len(
                {(row.get("resource_code"), row.get("year")) for row in results}
            )
            == len(results),
        },
        "cases": results,
    }


# 干跑校验：不调 API；用内存库真实执行导入、L0-L4 与离线预检。
def dry_check(cfg: AcceptanceConfig, printer: Printer = print) -> dict[str, Any]:
    manifest = json.loads(cfg.manifest.resolve().read_text("utf-8"))
    cases = _selected_cases(cfg.cases, manifest)
    submission_dir = _resolve_input(cfg.submission_dir or manifest["submission_dir"])
    registry = load_template_registry()
    resource_map = load_resource_map()
    ledger = load_ledger()
    imported_files = [
        import_file(submission_dir / str(case["file"]), submission_dir.name)
        for case in cases
    ]
    store = Store(":memory:")
    try:
        prepared = prepare_review_batch(
            submission_dir,
            store,
            imported_files=imported_files,
            registry=registry,
            resource_map=resource_map,
            ledger=ledger,
        )
        precheck = l5_precheck.run_batch(
            list(prepared.imported_files), prepared.ledger, prober=None
        )
        local = store.get_batch_stage_run(prepared.batch_id, "local")
        local_stage = str(local["status"]) if local is not None else "done"
    finally:
        store.close()
    candidate_keys = {
        (str(target.resource_code), str(target.year))
        for target in precheck.candidate_targets
    }
    plan: list[dict[str, Any]] = []
    seeds_ok = 0
    for case in cases:
        case_id = str(case["id"])
        present = (submission_dir / str(case["file"])).is_file()
        try:
            seed = _case_seed(
                1,
                case,
                submission_dir,
                registry=registry,
                ledger=ledger,
            )
            seed_ok = True
            seeds_ok += 1
            mismatches = _manifest_mismatches(seed, case)
        except Exception as exc:  # noqa: BLE001  干跑要看清哪条案例组装失败
            seed_ok = False
            mismatches = []
            printer(f"[{case_id}] seed 组装失败：{type(exc).__name__}")
        plan.append(
            {
                "id": case_id,
                "award_name": seed.award_name if seed_ok else "",
                "year": seed.year if seed_ok else "",
                "resource_code": seed.resource_code if seed_ok else "",
                "known_urls": len(seed.known_urls) if seed_ok else 0,
                "file_present": present,
                "seed_ok": seed_ok,
                "manifest_mismatches": mismatches,
                "candidate_target": (
                    (seed.resource_code, seed.year) in candidate_keys if seed_ok else False
                ),
                "probe_status": "not_checked",
            }
        )
    return {
        "schema_version": 1,
        "probe": "m5_real_submission14_dry",
        "generated_at": _utc_now(),
        "mode": "dry",
        "acceptance_mode": cfg.mode,
        "manifest": str(cfg.manifest),
        "submission_dir_exists": submission_dir.is_dir(),
        "cases_total": len(cases),
        "seeds_ok": seeds_ok,
        "files_present": sum(1 for p in plan if p["file_present"]),
        "local_stage": local_stage,
        "local_issue_count": prepared.result.total_issues,
        "candidate_targets": len(precheck.candidate_targets),
        "probe_status": "not_checked",
        "plan": plan,
        "hint": "加 --confirm-real-api 才会真的调用 API（约 10 万 token）。",
    }


# M5 回归模式：跳过 M4，按 manifest 派生合成 seed，强制选中案例进入 Harness。
def _run_m5_regression(
    cfg: AcceptanceConfig, printer: Printer = print
) -> dict[str, Any]:
    if not cfg.confirm_real_api:
        raise ValueError("real API use requires --confirm-real-api")

    config.load_env()
    provider = llm_module._provider()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError("configured API key is missing")
    client_metadata = llm_module.LlmClient()

    manifest = json.loads(cfg.manifest.resolve().read_text("utf-8"))
    cases = _selected_cases(cfg.cases, manifest)
    submission_dir = _resolve_input(cfg.submission_dir or manifest["submission_dir"])
    if not submission_dir.is_dir():
        raise FileNotFoundError("submission directory not found")
    missing = [case["id"] for case in cases if not (submission_dir / case["file"]).is_file()]
    if missing:
        raise FileNotFoundError(f"submission files missing for: {','.join(missing)}")

    evidence_dir = cfg.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    registry = load_template_registry()
    ledger = load_ledger()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    db_path = evidence_dir.parent / f"agent_probe_{stamp}.db"
    store = Store(db_path)
    results: list[dict[str, Any]] = []
    try:
        batch_id = store.create_batch(f"m5-real-agent-{stamp}", source="probe")
        repository = CaseRepository(store)
        harness = build_default_harness(
            store,
            allowed_roots=[evidence_dir, submission_dir],
            limits=HarnessLimits(max_steps=cfg.max_steps, max_tokens=cfg.max_tokens),
        )
        for case in cases:
            case_id = str(case["id"])
            printer(f"[{case_id}] Agent 核验开始：{case['award_name']} {case['year']}")
            stage = "case_setup"
            try:
                state, _created = repository.create_or_get(
                    _case_seed(
                        batch_id,
                        case,
                        submission_dir,
                        registry=registry,
                        ledger=ledger,
                    ),
                    tool_limits=ToolBudgetLimits(max_calls=cfg.max_tool_calls),
                )
                stage = "harness_run"
                outcome = harness.run(state.case_id)
                stage = "redaction"
                row = _redacted_result(case_id, outcome)
                detail = (
                    f" detail={row['last_error_detail']}"
                    if row["last_error_detail"]
                    else ""
                )
                printer(
                    f"[{case_id}] status={row['status']} stop={row['stopped_reason']} "
                    f"tools={len(row['tool_calls'])} tokens={row['token_used']} "
                    f"verifier_tokens={row['verifier_llm_usage']['total_tokens']} "
                    f"ok={row['probe_ok']}{detail}"
                )
            except Exception as exc:  # noqa: BLE001  单案异常不杀整批
                row = {
                    "id": case_id,
                    "resource_code": str(case["resource_code"]),
                    "year": str(case["year"]),
                    "probe_ok": False,
                    "error_type": type(exc).__name__,
                    "error_stage": stage,
                }
                printer(f"[{case_id}] failed={type(exc).__name__} stage={stage}")
            results.append(row)
    finally:
        store.close()

    summary = _summarize(
        results,
        probe="m5_real_submission14_m5_regression",
        configuration={
            "mode": "m5_regression",
            "provider": provider,
            "model": client_metadata.model,
            "base_url_configured": bool(
                os.environ.get("AWARD_AUDIT_BASE_URL")
                or os.environ.get("ANTHROPIC_BASE_URL")
            ),
            "cases_requested": [str(case["id"]) for case in cases],
            "max_steps": cfg.max_steps,
            "max_tokens": cfg.max_tokens,
            "max_tool_calls": cfg.max_tool_calls,
        },
        require_verification=True,
    )
    summary["routing"] = {"m4_only": 0, "m5_entered": len(results)}
    return summary


def _normalized_code(value: object) -> str:
    text = str(value or "").strip()
    return text.zfill(8) if text.isdigit() else text.casefold()


def _m4_only_result(
    case_id: str,
    imported: Any,
    stage_item: Any,
    audit_result: Any,
) -> dict[str, Any]:
    done = stage_item is not None and str(stage_item["status"]) == "done"
    return {
        "id": case_id,
        "resource_code": _normalized_code(imported.first_zylbm),
        "year": imported.year,
        "route": "m4",
        "m5_entered": False,
        "status": "m4_completed" if done else "m4_unresolved",
        "stopped_reason": (
            "m4_high_confidence_no_m5" if done else "m4_did_not_reach_case"
        ),
        "probe_ok": done,
        "confidence": str(audit_result["confidence"]) if audit_result is not None else "",
        "reason_codes": (
            json.loads(str(audit_result["reason_codes_json"]))
            if audit_result is not None else []
        ),
        "step_count": 0,
        "token_used": 0,
        "elapsed_ms": 0,
        "tool_calls": [],
        "artifact_count": 0,
        "artifacts": [],
        "recommendation_present": False,
        "last_error_code": "",
        "last_error_detail": "",
        "verification": None,
        "llm_usage": {"turns": []},
        "verifier_llm_usage": {"calls": [], "total_tokens": 0},
    }


def _run_e2e(cfg: AcceptanceConfig, printer: Printer = print) -> dict[str, Any]:
    if not cfg.confirm_real_api:
        raise ValueError("real API use requires --confirm-real-api")

    config.load_env()
    provider = llm_module._provider()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError("configured API key is missing")
    client_metadata = llm_module.LlmClient()
    manifest = json.loads(cfg.manifest.resolve().read_text("utf-8"))
    cases = _selected_cases(cfg.cases, manifest)
    submission_dir = _resolve_input(cfg.submission_dir or manifest["submission_dir"])
    if not submission_dir.is_dir():
        raise FileNotFoundError("submission directory not found")
    missing = [case["id"] for case in cases if not (submission_dir / case["file"]).is_file()]
    if missing:
        raise FileNotFoundError(f"submission files missing for: {','.join(missing)}")

    evidence_dir = cfg.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    registry = load_template_registry()
    resource_map = load_resource_map()
    ledger = load_ledger()
    imported_files = [
        import_file(submission_dir / str(case["file"]), submission_dir.name)
        for case in cases
    ]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    db_path = evidence_dir.parent / f"agent_e2e_{stamp}.db"
    store = Store(db_path)
    try:
        prepared = prepare_review_batch(
            submission_dir,
            store,
            imported_files=imported_files,
            registry=registry,
            resource_map=resource_map,
            ledger=ledger,
        )
        printer(
            f"E2E 本地检查完成：files={len(prepared.imported_files)} "
            f"issues={prepared.result.total_issues}"
        )
        audit = run_audit_stage(
            store,
            prepared,
            prober=l5_precheck.default_prober,
            approve=None,
            workdir=evidence_dir,
            use_corpus=False,
            tool_limits=ToolBudgetLimits(max_calls=cfg.max_tool_calls),
        )
        printer(
            f"E2E M4 收口：status={audit.status} "
            f"targets={len(audit.precheck.passable_targets)} "
            f"m5_cases={len(audit.bridge.case_ids)}"
        )

        m5_outcomes: dict[int, str] = {}
        if audit.bridge.case_ids:
            for item in run_queued_review_cases(
                db_path,
                prepared.batch_id,
                evidence_roots=[evidence_dir, submission_dir],
            ):
                m5_outcomes[int(str(item["case_id"]))] = str(item["stopped_reason"])

        repository = CaseRepository(store)
        case_rows = store.list_audit_cases(batch_id=prepared.batch_id)
        by_target = {
            (_normalized_code(row["resource_code"]), str(row["year"])): row
            for row in case_rows
        }
        results: list[dict[str, Any]] = []
        for manifest_case, imported in zip(cases, imported_files, strict=True):
            case_id = str(manifest_case["id"])
            key = (_normalized_code(imported.first_zylbm), imported.year)
            case_row = by_target.get(key)
            if case_row is not None:
                persisted_id = int(case_row["id"])
                state = repository.load(persisted_id)
                stopped_reason = m5_outcomes.get(
                    persisted_id, _recovered_stop_reason(state)
                )
                result = _redacted_result(
                    case_id,
                    HarnessOutcome(state=state, stopped_reason=stopped_reason),
                )
                result["route"] = "m5"
                result["m5_entered"] = True
                printer(
                    f"[{case_id}] 已分流 M5：status={result['status']} "
                    f"stop={result['stopped_reason']}"
                )
            else:
                stage_item = store.get_stage_item(
                    prepared.batch_id, key[0], key[1]
                )
                audit_result = None
                if stage_item is not None and stage_item["current_result_id"] is not None:
                    audit_result = store.conn.execute(
                        "SELECT * FROM audit_result WHERE id=?",
                        (int(stage_item["current_result_id"]),),
                    ).fetchone()
                result = _m4_only_result(
                    case_id, imported, stage_item, audit_result
                )
                printer(
                    f"[{case_id}] M4 高置信核对完成；按设计无需进入 M5，"
                    "仍等待 M4 人工结论。"
                )
            results.append(result)
    finally:
        store.close()

    summary = _summarize(
        results,
        probe="m5_real_submission14_e2e",
        configuration={
            "mode": "e2e",
            "provider": provider,
            "model": client_metadata.model,
            "base_url_configured": bool(
                os.environ.get("AWARD_AUDIT_BASE_URL")
                or os.environ.get("ANTHROPIC_BASE_URL")
            ),
            "cases_requested": [str(case["id"]) for case in cases],
            "max_steps": cfg.max_steps,
            "max_tokens": cfg.max_tokens,
            "max_tool_calls": cfg.max_tool_calls,
        },
        require_verification=False,
    )
    summary["routing"] = {
        "m4_only": sum(row.get("route") == "m4" for row in results),
        "m5_entered": sum(row.get("route") == "m5" for row in results),
    }
    return summary


def run(cfg: AcceptanceConfig, printer: Printer = print) -> dict[str, Any]:
    if cfg.mode == "e2e":
        return _run_e2e(cfg, printer)
    if cfg.mode == "m5_regression":
        return _run_m5_regression(cfg, printer)
    raise ValueError(f"unsupported acceptance mode: {cfg.mode}")


# 从 reason_codes 反推停止原因（恢复模式用，旧库未持久化 stopped_reason）
def _recovered_stop_reason(state: Any) -> str:
    mapping = (
        ("agent_recommendation_ready", "recommendation_ready"),
        ("verifier_requires_manual", "verifier_requires_manual"),
        ("reflection_exhausted", "reflection_exhausted"),
        ("agent_requested_manual", "agent_requested_manual"),
        ("verifier_error", "verifier_error"),
    )
    for code, reason in mapping:
        if code in state.reason_codes:
            return reason
    return "awaiting_human_action"


# 恢复模式：从一次已完成但报告失败的单案库重建脱敏结果，不重调 API
def recover(cfg: AcceptanceConfig, printer: Printer = print) -> dict[str, Any]:
    if cfg.recover_db is None:
        raise ValueError("recover requires recover_db")
    db_path = cfg.recover_db.resolve()
    if not db_path.is_file() or db_path.suffix.lower() not in {".db", ".sqlite3"}:
        raise ValueError("--recover-db must point to an existing SQLite database")
    store = Store(db_path)
    try:
        rows = store.list_audit_cases()
        if len(rows) != 1:
            raise ValueError("recovery currently requires a single-case probe database")
        state = CaseRepository(store).load(int(rows[0]["id"]))
        stopped_reason = _recovered_stop_reason(state)
        row = _redacted_result(
            cfg.recover_label,
            HarnessOutcome(state=state, stopped_reason=stopped_reason),
        )
    finally:
        store.close()
    result = _summarize(
        [row],
        probe="m5_real_submission14_agent_recovery",
        configuration={
            "recovered_from_db": True,
            "cases_requested": [cfg.recover_label],
        },
        require_verification=False,
    )
    result["recovery_limitations"] = {
        "llm_turn_usage_may_be_missing": not bool(row["llm_usage"]["turns"]),
        "verifier_usage_may_be_missing": not bool(row["verifier_llm_usage"]["calls"]),
    }
    return result


# 打印干跑计划表（一眼看清六案文件在位/映射成立）
def _print_dry_plan(result: dict[str, Any], printer: Printer) -> None:
    printer(
        f"\n干跑校验（不调 API）：manifest {result['manifest']}"
        f"\n提交目录存在：{result['submission_dir_exists']}\n"
    )
    printer(f"  {'案':<5}{'奖项/年度':<40}{'资源项码':<12}{'URL':<5}{'文件在位':<8}{'seed'}")
    for p in result["plan"]:
        award = f"{p['award_name']}/{p['year']}"
        printer(
            f"  {p['id']:<5}{award[:38]:<40}{p['resource_code']:<12}"
            f"{p['known_urls']:<5}{'是' if p['file_present'] else '否':<8}"
            f"{'ok' if p['seed_ok'] else 'FAIL'}"
        )
    printer(
        f"\n合计：案例 {result['cases_total']} ｜ seed 成立 {result['seeds_ok']} ｜ "
        f"文件在位 {result['files_present']}/{result['cases_total']}"
    )
    printer(
        f"本地阶段：{result['local_stage']} ｜ 离线候选目标："
        f"{result['candidate_targets']} ｜ probe_status={result['probe_status']}"
    )
    printer(f"提示：{result['hint']}")


# 顶层分派：recover_db → 恢复；未确认真跑 → 干跑；确认 → 真跑。返回 (结果, 退出码)
def execute(cfg: AcceptanceConfig, printer: Printer = print) -> tuple[dict[str, Any], int]:
    if cfg.recover_db is not None:
        result = recover(cfg, printer)
        mode = "recover"
    elif not cfg.confirm_real_api:
        result = dry_check(cfg, printer)
        mode = "dry"
    else:
        result = run(cfg, printer)
        mode = "run"

    if cfg.output is not None:
        cfg.output.parent.mkdir(parents=True, exist_ok=True)
        cfg.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        printer(f"脱敏结果已写 {cfg.output}")

    if mode == "dry":
        _print_dry_plan(result, printer)
        ok = (
            result["seeds_ok"] == result["cases_total"]
            and result["files_present"] == result["cases_total"]
            and result["local_stage"] == "done"
            and all(not row["manifest_mismatches"] for row in result["plan"])
        )
        return result, (0 if ok else 2)

    summary = result["summary"]
    routing = result.get("routing")
    if isinstance(routing, dict):
        printer(
            f"路由：M4 直接收口 {routing.get('m4_only', 0)} 案；"
            f"进入 M5 深度取证 {routing.get('m5_entered', 0)} 案。"
        )
    printer(
        f"完成：status={summary['status']} cases={summary['cases_ok']}/"
        f"{summary['cases_total']} tools={summary['tool_calls_total']}"
    )
    return result, (0 if summary["status"] == "complete" else 1)
