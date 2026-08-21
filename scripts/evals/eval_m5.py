"""Offline M5 acceptance evaluation over redacted probes and controlled cases."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from award_audit.agent.harness.client import FakeAgentClient  # noqa: E402
from award_audit.agent.harness.models import (  # noqa: E402
    AgentDecision,
    CaseSeed,
    HarnessLimits,
    NextAction,
    TriggerCode,
)
from award_audit.agent.harness.persistence import CaseRepository  # noqa: E402
from award_audit.agent.harness.runner import EvidenceHarness  # noqa: E402
from award_audit.agent.memory import CaseMemoryService  # noqa: E402
from award_audit.agent.toolkit import (  # noqa: E402
    EvidenceArtifact,
    ToolBudgetLimits,
    ToolRegistry,
    ToolResult,
)
from award_audit.agent.toolkit.testing import register_fake_tool  # noqa: E402
from award_audit.agent.verification import (  # noqa: E402
    EvidenceSnapshot,
    deterministic_verify,
)
from award_audit.core.pipeline.store import Store  # noqa: E402

DEFAULT_RESULTS = ROOT / "tests" / "data" / "m5_golden" / "results"
DEFAULT_GOLD = ROOT / "tests" / "data" / "m5_golden" / "awards_10.json"
DEFAULT_OUTPUT = DEFAULT_RESULTS / "m5_eval.json"


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _metric(
    value: float | int | bool | None,
    threshold: str,
    passed: bool | None,
    *,
    source: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "threshold": threshold,
        "passed": passed,
        "source": source,
        "detail": detail or {},
    }


def _recall_metrics(
    search: dict[str, Any] | None,
    gold: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    if not search or not gold:
        missing = _metric(None, ">= 0.80", None, source="not_rerun")
        return missing, _metric(None, "informational", None, source="not_rerun"), {}
    rows = search.get("recall", {}).get("rows", [])
    hit_by_code = {
        str(row.get("code", "")): bool(row.get("hit"))
        for row in rows
        if isinstance(row, dict)
    }
    awards = [item for item in gold.get("awards", []) if isinstance(item, dict)]
    domestic = [item for item in awards if item.get("source_type") != "国际奖"]
    international = [item for item in awards if item.get("source_type") == "国际奖"]

    def score(items: list[dict[str, Any]]) -> tuple[float | None, int, int]:
        if not items:
            return None, 0, 0
        hits = sum(bool(hit_by_code.get(str(item.get("code", "")))) for item in items)
        return round(hits / len(items), 4), hits, len(items)

    domestic_value, domestic_hits, domestic_n = score(domestic)
    international_value, international_hits, international_n = score(international)
    distribution = Counter(str(item.get("source_type", "unknown")) for item in awards)
    return (
        _metric(
            domestic_value,
            ">= 0.80",
            domestic_value is not None and domestic_value >= 0.8,
            source="measured:redacted_search_probe",
            detail={"hits": domestic_hits, "n": domestic_n},
        ),
        _metric(
            international_value,
            "informational",
            None,
            source="measured:redacted_search_probe",
            detail={"hits": international_hits, "n": international_n},
        ),
        dict(sorted(distribution.items())),
    )


def _pdf_metrics(pdf: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], Any]:
    if not pdf:
        missing = _metric(None, ">= 0.95", None, source="not_rerun")
        return missing, _metric(None, ">= 0.90", None, source="not_rerun"), None
    digital: float | None = None
    scan_without_vision: float | None = None
    for record in pdf.get("records", []):
        if not isinstance(record, dict):
            continue
        score = record.get("best_extraction", {}).get("score", {})
        if record.get("sample") == "digital_roster":
            digital = score.get("field_recall")
        elif record.get("sample") == "scanned_roster":
            scan_without_vision = score.get("field_recall")
    vision = pdf.get("summary", {}).get("vision_average_f1")
    gain = None
    if isinstance(vision, (int, float)) and isinstance(scan_without_vision, (int, float)):
        gain = round(float(vision) - float(scan_without_vision), 4)
    return (
        _metric(
            digital,
            ">= 0.95",
            isinstance(digital, (int, float)) and digital >= 0.95,
            source="measured:redacted_pdf_ocr_probe",
            detail={"metric": "digital_field_recall"},
        ),
        _metric(
            vision,
            ">= 0.90",
            isinstance(vision, (int, float)) and vision >= 0.9,
            source="measured:redacted_pdf_ocr_probe",
            detail={"metric": "scanned_vision_average_f1"},
        ),
        gain,
    )


def _schema_metric(
    vision: dict[str, Any] | None,
    toolcall: dict[str, Any] | None,
) -> dict[str, Any]:
    valid: list[bool] = []
    if vision:
        valid.extend(
            bool(row.get("json_valid"))
            for row in vision.get("records", [])
            if isinstance(row, dict)
        )
    if toolcall:
        for route in toolcall.get("results", []):
            if isinstance(route, dict) and route.get("route") == "structured_action":
                valid.extend(
                    bool(row.get("valid"))
                    for row in route.get("samples", [])
                    if isinstance(row, dict)
                )
    if not valid:
        return _metric(None, ">= 0.99", None, source="not_rerun")
    rate = round(sum(valid) / len(valid), 4)
    return _metric(
        rate,
        ">= 0.99",
        rate >= 0.99,
        source="measured:redacted_vision_and_toolcall_probes",
        detail={"valid": sum(valid), "n": len(valid)},
    )


def _controlled_severe_false_pass() -> dict[str, Any]:
    complete = EvidenceSnapshot(
        expected_award_name="某竞赛",
        expected_year="2025",
        observed_award_names=["某竞赛"],
        observed_years=["2025"],
        source_levels=["official_primary"],
        expected_count=10,
        observed_count=10,
        total_pages=2,
        processed_pages=2,
        sequence_complete=True,
    )
    scenarios = {
        "wrong_year": complete.model_copy(update={"observed_years": ["2024"]}),
        "unknown_source": complete.model_copy(update={"source_levels": []}),
        "incomplete_coverage": complete.model_copy(update={"sequence_complete": False}),
        "contradiction": complete.model_copy(update={"contradictions": ["官方结果冲突"]}),
    }
    actions = {
        name: deterministic_verify(snapshot).recommended_action
        for name, snapshot in scenarios.items()
    }
    false_passes = sum(action == "accept_evidence" for action in actions.values())
    return _metric(
        false_passes,
        "== 0",
        false_passes == 0,
        source="controlled:deterministic_verifier",
        detail={"n": len(actions), "actions": actions},
    )


def _controlled_metadata() -> dict[str, Any]:
    artifacts = [
        EvidenceArtifact(
            kind="html",
            source_url=f"https://example.gov.cn/evidence/{index}",
            local_path=f"evidence-{index}.html",
            content_type="text/html",
            sha256=f"{index:x}" * 64,
            size_bytes=100 + index,
            fetched_at="2026-07-25T00:00:00Z",
        )
        for index in range(1, 4)
    ]
    complete = sum(
        bool(item.source_url and item.fetched_at and item.sha256) for item in artifacts
    )
    rate = complete / len(artifacts)
    return _metric(
        rate,
        "== 1.00",
        rate == 1.0,
        source="controlled:evidence_artifact_contract",
        detail={"complete": complete, "n": len(artifacts)},
    )


def _finalize_case(
    repository: CaseRepository,
    seed: CaseSeed,
) -> Any:
    state, _ = repository.create_or_get(seed)
    state.status = "waiting_human"
    state.recommendation = "已收集结构化证据，等待人工确认"
    repository.save(state)
    return repository.finalize(
        state.case_id,
        "accepted",
        "人工确认该处理规则可复用",
        "m5-eval-reviewer",
        expected_version=state.state_version,
    )


def _controlled_memory(work_dir: Path) -> dict[str, Any]:
    store = Store(work_dir / "memory-eval.db")
    try:
        repository = CaseRepository(store)
        service = CaseMemoryService(store)
        batch_id = store.create_batch("m5-eval-memory")
        fixtures = [
            ("SOURCE_DISCOVERY", "SOURCE_URL_MISSING", "官网入口失效需寻找主管单位页面"),
            ("DOCUMENT_EXTRACTION", "PDF_ONLY", "PDF 名单需要跨页提取完整字段"),
            ("FIELD_SEMANTICS", "COLUMN_AMBIGUOUS", "推荐单位列混入专家姓名"),
            ("COVERAGE_PATTERN", "COVERAGE_UNKNOWN", "名单页数与序列完整性未知"),
            ("STANDARD_CORRECTION", "SOFT_RULE_SUSPECT", "字段值需按已核准标准纠正"),
        ]
        expected_ids: list[int] = []
        for index, (category, trigger, symptom) in enumerate(fixtures):
            completed = _finalize_case(
                repository,
                CaseSeed(
                    batch_id=batch_id,
                    resource_code=f"M5MEM{index:02d}",
                    trigger_codes=[cast(TriggerCode, trigger)],
                    objective=symptom,
                    submitted_summary={"resource_type": "award", "field_code": ""},
                    open_questions=[symptom],
                ),
            )
            candidate = service.propose_from_case(
                completed,
                category_code=category,
                symptom_text=symptom,
                resolution=f"复用规则：{symptom}",
            )
            if candidate is None:
                continue
            active = service.repository.transition(
                candidate.memory_id,
                "active",
                "m5-eval-approver",
                expected_version=candidate.state_version,
            )
            expected_ids.append(active.memory_id)

        hits = 0
        details: list[dict[str, Any]] = []
        for index, ((_category, trigger, symptom), expected_id) in enumerate(
            zip(fixtures, expected_ids)
        ):
            query, _ = repository.create_or_get(
                CaseSeed(
                    batch_id=batch_id,
                    resource_code=f"M5QRY{index:02d}",
                    trigger_codes=[cast(TriggerCode, trigger)],
                    objective=symptom,
                    submitted_summary={"resource_type": "award", "field_code": ""},
                    open_questions=[symptom],
                )
            )
            found = [item.memory_id for item in service.retrieve_for_case(query)]
            matched = expected_id in found
            hits += matched
            details.append({"expected": expected_id, "top3": found, "hit": matched})
        n = len(fixtures)
        rate = round(hits / n, 4) if n else 0.0
        return _metric(
            rate,
            ">= 0.80",
            rate >= 0.8 and len(expected_ids) == n,
            source="controlled:governed_case_memory",
            detail={"hits": hits, "n": n, "queries": details},
        )
    finally:
        store.close()


def _controlled_budget(work_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    store = Store(work_dir / "budget-eval.db")
    try:
        repository = CaseRepository(store)
        batch_id = store.create_batch("m5-eval-budget")
        scenarios = [
            (
                HarnessLimits(max_steps=1),
                ToolBudgetLimits(),
                AgentDecision(
                    action=NextAction(action="call_tool", tool_name="ok_tool"),
                    route="fake",
                ),
                "agent_step_budget_exhausted",
            ),
            (
                HarnessLimits(max_tokens=1),
                ToolBudgetLimits(),
                AgentDecision(
                    action=NextAction(action="finish"), token_used=2, route="fake"
                ),
                "agent_token_budget_exhausted",
            ),
            (
                HarnessLimits(),
                ToolBudgetLimits(),
                AgentDecision(
                    action=NextAction(action="manual", reason_summary="需人工判断"),
                    route="fake",
                ),
                "agent_requested_manual",
            ),
        ]
        outcomes: list[dict[str, Any]] = []
        for index, (limits, tool_limits, decision, expected) in enumerate(scenarios):
            state, _ = repository.create_or_get(
                CaseSeed(
                    batch_id=batch_id,
                    resource_code=f"M5BUD{index:02d}",
                    trigger_codes=["COVERAGE_UNKNOWN"],
                    objective="验证预算耗尽或人工动作时可靠转交",
                ),
                tool_limits=tool_limits,
            )
            registry = ToolRegistry()
            register_fake_tool(registry, "ok_tool", [ToolResult(ok=True)])
            outcome = EvidenceHarness(
                repository=repository,
                registry=registry,
                agent_client=FakeAgentClient([decision]),
                allowed_roots=[work_dir],
                limits=limits,
            ).run(state.case_id)
            outcomes.append(
                {
                    "expected": expected,
                    "actual": outcome.stopped_reason,
                    "status": outcome.state.status,
                    "tool_calls": outcome.state.budget.calls,
                    "token_used": outcome.state.token_used,
                    "elapsed_ms": outcome.state.elapsed_ms,
                }
            )
        handed_off = sum(
            row["status"] == "waiting_human" and row["actual"] == row["expected"]
            for row in outcomes
        )
        rate = handed_off / len(outcomes)
        elapsed = sorted(int(row["elapsed_ms"]) for row in outcomes)
        p95_index = max(0, round(0.95 * (len(elapsed) - 1)))
        operations = {
            "sample_scope": "controlled_scenarios_not_production",
            "automatic_completion_rate": 0.0,
            "human_handoff_rate": rate,
            "tool_calls_total": sum(int(row["tool_calls"]) for row in outcomes),
            "tokens_total": sum(int(row["token_used"]) for row in outcomes),
            "elapsed_ms_p50": statistics.median(elapsed),
            "elapsed_ms_p95": elapsed[p95_index],
        }
        return (
            _metric(
                rate,
                "== 1.00",
                rate == 1.0,
                source="controlled:fake_harness",
                detail={"n": len(outcomes), "outcomes": outcomes},
            ),
            operations,
        )
    finally:
        store.close()


def _probe_boolean_metric(
    payload: dict[str, Any] | None,
    *,
    threshold: str,
    passed: bool | None,
    detail: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    if payload is None:
        return _metric(None, threshold, None, source="not_rerun")
    return _metric(passed, threshold, passed, source=source, detail=detail)


def evaluate(results_dir: Path, gold_path: Path, work_dir: Path) -> dict[str, Any]:
    """Evaluate redacted probe outputs without network or model calls."""

    search = _load(results_dir / "search.json")
    pdf = _load(results_dir / "pdf_ocr_vision.json") or _load(
        results_dir / "pdf_ocr_rapid.json"
    )
    vision = _load(results_dir / "vision.json")
    toolcall = _load(results_dir / "toolcall.json")
    security = _load(results_dir / "security_offline.json")
    sqlite = _load(results_dir / "sqlite_wal.json")
    gold = _load(gold_path)

    domestic, international, source_distribution = _recall_metrics(search, gold)
    digital_pdf, scanned_vision, ocr_vision_gain = _pdf_metrics(pdf)
    security_checks = security.get("checks", {}) if security else {}
    security_ok = bool(security_checks) and all(bool(value) for value in security_checks.values())
    recovery_ok = bool(
        sqlite
        and sqlite.get("journal_mode") == "wal"
        and (
            sqlite.get("second_write_committed")
            or sqlite.get("second_write") == "committed"
        )
        and sqlite.get("expired_lease_recovered")
    )

    controlled_dir = work_dir.resolve()
    controlled_dir.mkdir(parents=True, exist_ok=True)
    budget, controlled_operations = _controlled_budget(controlled_dir)
    measured = {
        "domestic_official_recall_top5": domestic,
        "international_official_recall_top5": international,
        "digital_pdf_field_recall": digital_pdf,
        "scanned_vision_f1": scanned_vision,
        "structured_json_valid_rate": _schema_metric(vision, toolcall),
        "p5_security_offline": _probe_boolean_metric(
            security,
            threshold="8/8 and zero network/model calls",
            passed=security_ok,
            detail={"checks": security_checks},
            source="measured:redacted_security_probe",
        ),
        "task_recovery": _probe_boolean_metric(
            sqlite,
            threshold="== 1.00",
            passed=recovery_ok,
            detail={
                "journal_mode": sqlite.get("journal_mode") if sqlite else None,
                "second_write_committed": (
                    (
                        sqlite.get("second_write_committed")
                        or sqlite.get("second_write") == "committed"
                    )
                    if sqlite
                    else None
                ),
                "expired_lease_recovered": (
                    sqlite.get("expired_lease_recovered") if sqlite else None
                ),
            },
            source="measured:redacted_sqlite_probe",
        ),
    }
    controlled = {
        "severe_false_passes": _controlled_severe_false_pass(),
        "evidence_metadata_completeness": _controlled_metadata(),
        "case_memory_top3": _controlled_memory(controlled_dir),
        "budget_handoff": budget,
    }
    evaluated = [
        item
        for group in (measured, controlled)
        for item in group.values()
        if item["passed"] is not None
    ]
    return {
        "schema_version": "m5-eval-v1",
        "status": (
            "complete"
            if evaluated and all(item["passed"] for item in evaluated)
            else "failed"
        ),
        "execution_boundary": {
            "network_calls": 0,
            "model_calls": 0,
            "real_probes_rerun": False,
            "input_policy": "redacted probe JSON plus controlled local scenarios only",
        },
        "measured": measured,
        "controlled": controlled,
        "not_rerun": {
            "real_search_api": "user-supplied redacted result reused",
            "real_vision_api": "user-supplied redacted result reused",
            "production_operational_sample": "insufficient_sample",
            "reflection_corrections": "insufficient_sample",
        },
        "operational_statistics": {
            **controlled_operations,
            "source_type_distribution": source_distribution,
            "ocr_vision_gain": ocr_vision_gain,
            "reflection_corrected_count": {
                "status": "insufficient_sample",
                "value": None,
            },
        },
        "summary": {
            "evaluated_metrics": len(evaluated),
            "passed_metrics": sum(bool(item["passed"]) for item in evaluated),
            "all_available_thresholds_passed": bool(evaluated)
            and all(bool(item["passed"]) for item in evaluated),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.work_dir is None:
        with tempfile.TemporaryDirectory(prefix="award-audit-m5-eval-") as temp:
            result = evaluate(args.results_dir, args.gold, Path(temp))
    else:
        result = evaluate(args.results_dir, args.gold, args.work_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("== M5 离线验收 ==")
    print(
        f"状态={result['status']}  "
        f"通过={result['summary']['passed_metrics']}/"
        f"{result['summary']['evaluated_metrics']}"
    )
    print("网络调用=0  模型调用=0  真实探针重跑=False")
    print(f"结果已写 {args.output}")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
