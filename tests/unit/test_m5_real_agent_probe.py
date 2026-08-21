"""Offline safety tests for the submission-14 real Agent acceptance logic.

逻辑已从 scripts/probe_m5_real_agent.py 提炼进 award_audit.agent.harness.acceptance；
本测试改测其正式落点（探针壳只是委托 acceptance）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from award_audit.agent.harness import acceptance
from award_audit.agent.harness.models import CaseSeed, LlmTurnUsage
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.toolkit import EvidenceArtifact, ToolObservation
from award_audit.agent.verification import VerificationReport, VerifierCallUsage
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.engine import BatchResult, FileResult
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry
from award_audit.core.reference.resource_map import ResourceMapEntry
from award_audit.core.reference.template_registry import build_template_spec


def _module():  # noqa: ANN202  兼容旧写法：逻辑正式落点是 acceptance
    return acceptance


def test_real_agent_probe_requires_explicit_api_confirmation(monkeypatch) -> None:  # noqa: ANN001
    probe = _module()
    monkeypatch.setattr(
        probe.config,
        "load_env",
        lambda: (_ for _ in ()).throw(AssertionError("must not load configuration")),
    )
    cfg = probe.AcceptanceConfig()
    with pytest.raises(ValueError, match="confirm-real-api"):
        probe.run(cfg)


def test_acceptance_defaults_to_e2e_mode() -> None:
    assert acceptance.AcceptanceConfig().mode == "e2e"


def test_dry_check_runs_real_prepare_and_offline_precheck(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    source = tmp_path / "submission"
    source.mkdir()
    submission = source / "case.xlsx"
    submission.write_bytes(b"fixture")
    table_code = "CON_GG_XK_RCPY_GXDJSCGR"
    imported = ImportedFile(
        batch=source.name,
        path=str(submission),
        file_name=submission.name,
        claimed_table_code=table_code,
        award_name="文件奖项",
        year="2026",
        sheet_name="数据",
        header_codes=["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"],
        header_names=["资源项码", "资源项", "项目名称", "获奖人", "年度"],
        rows=[["04050088", "文件奖项", "年度人物", "张三", "2026"]],
    )
    spec = build_template_spec(
        table_code, imported.sheet_name, imported.header_codes, imported.header_names
    )
    ledger = {"04050088": LedgerEntry(
        resource_code="04050088",
        resource_name="文件奖项",
        expected_count=1,
        collect_url="https://official.example/derived",
    )}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "submission_dir": str(source),
        "cases": [{
            "id": "SXX", "file": submission.name, "resource_code": "04050088",
            "award_name": "文件奖项", "year": "2026", "submitted_rows": 1,
        }],
    }, ensure_ascii=False), "utf-8")
    calls = {"prepare": 0, "precheck": 0}

    def fake_prepare(folder, store, **kwargs):  # noqa: ANN001, ANN202
        calls["prepare"] += 1
        assert folder == source and kwargs["imported_files"] == [imported]
        return SimpleNamespace(
            batch_id=1,
            imported_files=(imported,),
            registry={table_code: spec},
            ledger=ledger,
            result=BatchResult(batch=source.name, files=[FileResult(
                file=submission.name,
                claimed_table_code=table_code,
                n_rows=1,
                issues=[],
            )]),
        )

    def fake_precheck(files, current_ledger, prober):  # noqa: ANN001, ANN202
        calls["precheck"] += 1
        assert files == [imported] and current_ledger == ledger and prober is None
        return SimpleNamespace(candidate_targets=[SimpleNamespace(
            resource_code="04050088", year="2026"
        )], issues=[])

    monkeypatch.setattr(acceptance, "import_file", lambda *_args: imported)
    monkeypatch.setattr(acceptance, "load_template_registry", lambda: {table_code: spec})
    monkeypatch.setattr(acceptance, "load_resource_map", lambda: {})
    monkeypatch.setattr(acceptance, "load_ledger", lambda: ledger)
    monkeypatch.setattr(acceptance, "prepare_review_batch", fake_prepare, raising=False)
    monkeypatch.setattr(
        acceptance,
        "l5_precheck",
        SimpleNamespace(run_batch=fake_precheck),
        raising=False,
    )

    result = acceptance.dry_check(acceptance.AcceptanceConfig(
        manifest=manifest, submission_dir=str(source), output=None
    ), printer=lambda _message: None)

    assert calls == {"prepare": 1, "precheck": 1}
    assert result["probe_status"] == "not_checked"
    assert result["local_stage"] == "done"


def test_run_dispatches_explicit_acceptance_modes(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        acceptance, "_run_e2e", lambda _cfg, _printer: {"selected": "e2e"}
    )
    monkeypatch.setattr(
        acceptance,
        "_run_m5_regression",
        lambda _cfg, _printer: {"selected": "m5_regression"},
    )
    assert acceptance.run(acceptance.AcceptanceConfig())["selected"] == "e2e"
    assert acceptance.run(acceptance.AcceptanceConfig(
        mode="m5_regression"
    ))["selected"] == "m5_regression"


def test_e2e_runs_prepare_then_m4_and_only_diverted_case_enters_m5(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    source = tmp_path / "submission"
    source.mkdir()
    paths = [source / "case-a.xlsx", source / "case-b.xlsx"]
    for path in paths:
        path.write_bytes(b"fixture")
    table_code = "CON_GG_XK_RCPY_GXDJSCGR"
    codes = ["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"]
    names = ["资源项码", "资源项", "项目名称", "获奖人", "年度"]
    imported_files = [
        ImportedFile(
            batch=source.name,
            path=str(path),
            file_name=path.name,
            claimed_table_code=table_code,
            award_name=f"奖项-{index}",
            year="2026",
            sheet_name="数据",
            header_codes=codes,
            header_names=names,
            rows=[[f"0405008{index}", f"奖项-{index}", "项目", "张三", "2026"]],
        )
        for index, path in enumerate(paths, start=1)
    ]
    spec = build_template_spec(table_code, "数据", codes, names)
    registry = {table_code: spec}
    ledger = {
        item.first_zylbm: LedgerEntry(
            resource_code=item.first_zylbm,
            resource_name=item.award_name,
            expected_count=1,
            collect_url=f"https://official.example/{index}",
        )
        for index, item in enumerate(imported_files, start=1)
    }
    resource_map = {
        item.first_zylbm: ResourceMapEntry(
            resource_code=item.first_zylbm,
            resource_name=item.award_name,
            table_code=table_code,
        )
        for item in imported_files
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "submission_dir": str(source),
        "cases": [
            {
                "id": f"S0{index}", "file": item.file_name,
                "resource_code": item.first_zylbm, "award_name": item.award_name,
                "year": item.year, "submitted_rows": 1,
            }
            for index, item in enumerate(imported_files, start=1)
        ],
    }, ensure_ascii=False), "utf-8")
    by_name = {item.file_name: item for item in imported_files}
    calls: list[str] = []

    monkeypatch.setattr(acceptance.config, "load_env", lambda: None)
    monkeypatch.setattr(acceptance.llm_module, "_provider", lambda: "fake")
    monkeypatch.setattr(
        acceptance.llm_module, "LlmClient", lambda: SimpleNamespace(model="fake-model")
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(acceptance, "load_template_registry", lambda: registry)
    monkeypatch.setattr(acceptance, "load_resource_map", lambda: resource_map)
    monkeypatch.setattr(acceptance, "load_ledger", lambda: ledger)
    monkeypatch.setattr(
        acceptance,
        "import_file",
        lambda path, _batch: by_name[path.name],
    )

    def fake_prepare(folder, store, **kwargs):  # noqa: ANN001, ANN202
        calls.append("prepare")
        batch_id = store.create_batch(folder.name)
        return SimpleNamespace(
            batch_id=batch_id,
            imported_files=tuple(kwargs["imported_files"]),
            registry=registry,
            ledger=ledger,
            result=BatchResult(batch=folder.name, files=[
                FileResult(
                    file=item.file_name,
                    claimed_table_code=table_code,
                    n_rows=1,
                    issues=[],
                )
                for item in imported_files
            ]),
        )

    def fake_audit(store, prepared, **kwargs):  # noqa: ANN001, ANN202
        calls.append("m4")
        assert kwargs["prober"] is acceptance.l5_precheck.default_prober
        assert kwargs["approve"] is None and kwargs["use_corpus"] is False
        repository = CaseRepository(store)
        state, _ = repository.create_or_get(CaseSeed(
            batch_id=prepared.batch_id,
            resource_code=imported_files[0].first_zylbm,
            award_name=imported_files[0].award_name,
            year="2026",
            trigger_codes=["COVERAGE_UNKNOWN"],
            objective="核验",
        ))
        state.status = "waiting_human"
        repository.save(state)
        second = imported_files[1]
        claim = store.claim_stage_item(
            prepared.batch_id, second.first_zylbm, second.year, worker="fake-m4"
        )
        assert claim is not None
        result_id = store.add_audit_results(prepared.batch_id, [{
            "resource_code": second.first_zylbm,
            "award_name": second.award_name,
            "year": second.year,
            "verdict": "一致",
            "confidence": "high",
            "reason_codes": [],
        }])[0]
        store.finish_stage_item(
            prepared.batch_id,
            second.first_zylbm,
            second.year,
            status="done",
            current_result_id=result_id,
            worker="fake-m4",
            expected_version=int(claim["state_version"]),
        )
        return SimpleNamespace(
            status="done",
            precheck=SimpleNamespace(passable_targets=[1, 2]),
            bridge=SimpleNamespace(case_ids=[state.case_id]),
        )

    def fake_m5(db_path, batch_id, **kwargs):  # noqa: ANN001, ANN202
        calls.append("m5")
        reopened = Store(db_path)
        try:
            row = reopened.list_audit_cases(batch_id=batch_id)[0]
            return [{
                "case_id": int(row["id"]),
                "status": "waiting_human",
                "stopped_reason": "recommendation_ready",
            }]
        finally:
            reopened.close()

    monkeypatch.setattr(acceptance, "prepare_review_batch", fake_prepare)
    monkeypatch.setattr(acceptance, "run_audit_stage", fake_audit)
    monkeypatch.setattr(acceptance, "run_queued_review_cases", fake_m5)
    result = acceptance._run_e2e(acceptance.AcceptanceConfig(
        manifest=manifest,
        submission_dir=str(source),
        evidence_dir=tmp_path / "evidence",
        confirm_real_api=True,
        output=None,
    ), printer=lambda _message: None)

    assert calls == ["prepare", "m4", "m5"]
    assert result["routing"] == {"m4_only": 1, "m5_entered": 1}
    assert [row["route"] for row in result["cases"]] == ["m5", "m4"]


def test_case_selection_keeps_requested_years_separate() -> None:
    probe = _module()
    manifest = json.loads(probe.DEFAULT_MANIFEST.read_text("utf-8"))
    selected = probe._selected_cases("S03,S04", manifest)
    assert [(case["resource_code"], case["year"]) for case in selected] == [
        ("04030060", "2023"),
        ("04030060", "2025"),
    ]
    with pytest.raises(ValueError, match="unknown case ids"):
        probe._selected_cases("S99", manifest)


def test_acceptance_manifest_cannot_override_generic_case_context(
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    probe = _module()
    table_code = "CON_GG_XK_RCPY_GXDJSCGR"
    submitted = ImportedFile(
        batch="new-batch",
        path=str(tmp_path / "new.xlsx"),
        file_name="new.xlsx",
        claimed_table_code=table_code,
        award_name="文件推导奖项",
        year="2026",
        sheet_name="数据",
        header_codes=["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"],
        header_names=["资源项码", "资源项", "项目名称", "获奖人", "年度"],
        rows=[["04050088", "文件推导奖项", "年度人物", "张三", "2026"]],
    )
    spec = build_template_spec(
        table_code,
        submitted.sheet_name,
        submitted.header_codes,
        submitted.header_names,
    )
    ledger = LedgerEntry(
        resource_code="04050088",
        resource_name="台账推导奖项",
        expected_count=1,
        collect_url="https://official.example/derived",
    )
    monkeypatch.setattr(probe, "import_file", lambda *_args: submitted, raising=False)
    monkeypatch.setattr(
        probe, "load_template_registry", lambda: {table_code: spec}, raising=False
    )
    monkeypatch.setattr(
        probe, "load_ledger", lambda: {ledger.resource_code: ledger}, raising=False
    )
    manifest_case = {
        "id": "SXX",
        "file": "new.xlsx",
        "resource_code": "WRONG",
        "award_name": "manifest错误奖项",
        "year": "1900",
        "submitted_rows": 999,
        "reference_rows": 999,
        "match_fields": ["WRONG_FIELD"],
        "urls": ["https://wrong.example/manifest"],
    }

    seed = probe._case_seed(1, manifest_case, tmp_path)

    assert seed.resource_code == "04050088"
    assert seed.award_name == "文件推导奖项"
    assert seed.year == "2026"
    assert seed.submitted_summary["submitted_rows"] == 1
    assert seed.submitted_summary["match_fields"] == ["XRYXM"]
    assert seed.known_urls == ["https://official.example/derived"]


def test_probe_result_excludes_paths_rosters_and_recommendation_text() -> None:
    probe = _module()
    observation = ToolObservation(
        call_id="call-1",
        tool_name="download_evidence",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        input_summary={"path": "C:/private/roster.xlsx"},
        output_summary={
            "text": "张三",
            "verification_facts": {
                "source_level": "publisher_secondary",
                "coverage_complete": True,
                "missing_items": ["张三"],
            },
        },
        ok=True,
    )
    artifact = EvidenceArtifact(
        kind="xlsx",
        source_url="https://example.gov.cn/list.xlsx",
        local_path="C:/private/roster.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256="a" * 64,
        size_bytes=123,
        fetched_at="2026-01-01T00:00:01Z",
    )
    state = SimpleNamespace(
        status="waiting_human",
        resource_code="04030060",
        year="2025",
        confidence="medium",
        reason_codes=["agent_recommendation_ready"],
        step_count=2,
        token_used=100,
        llm_usage=[LlmTurnUsage(
            step=1,
            route="native",
            provider_usage_reported=True,
            total_tokens=100,
            input_tokens=80,
            output_tokens=20,
            cached_input_tokens=40,
            cache_detail_reported=True,
            prompt_chars=1200,
            tool_schema_chars=400,
        )],
        verifier_llm_usage=[VerifierCallUsage(
            route="native",
            provider_usage_reported=True,
            total_tokens=30,
            input_tokens=25,
            output_tokens=5,
        )],
        elapsed_ms=2000,
        tool_trace=[observation],
        artifacts=[artifact],
        recommendation="张三需要人工确认",
        last_error="",
        latest_verification=VerificationReport(
            target_match="yes",
            year_match="yes",
            source_authority="secondary",
            coverage_complete="yes",
            recommended_action="accept_evidence",
            reason_codes=["secondary_source_only"],
            deterministic_action="accept_evidence",
            model_used=True,
        ),
    )
    result = probe._redacted_result(
        "S04", SimpleNamespace(state=state, stopped_reason="recommendation_ready")
    )
    payload = json.dumps(result, ensure_ascii=False)
    assert "C:/private" not in payload and "张三" not in payload
    assert result["recommendation_present"] is True
    assert result["artifacts"][0]["source_host"] == "example.gov.cn"
    assert result["llm_usage"]["cached_input_ratio"] == 0.5
    assert result["llm_usage"]["scope"] == "agent_planner_only"
    assert result["llm_usage"]["cache_detail_complete"] is True
    assert result["llm_usage"]["provider_usage_complete"] is True
    assert result["llm_usage"]["token_used_is_lower_bound"] is False
    assert result["verifier_llm_usage"]["total_tokens"] == 30
    assert result["verifier_llm_usage"]["provider_usage_complete"] is True
    assert result["verification"] == {
        "recommended_action": "accept_evidence",
        "target_match": "yes",
        "year_match": "yes",
        "source_authority": "secondary",
        "coverage_complete": "yes",
        "deterministic_action": "accept_evidence",
        "model_used": True,
        "reason_codes": ["secondary_source_only"],
        "missing_evidence_count": 0,
    }
    assert result["tool_calls"][0]["verification_facts"] == {
        "source_level": "publisher_secondary",
        "coverage_complete": True,
        "missing_items_count": 1,
    }

    exhausted = probe._redacted_result(
        "S04", SimpleNamespace(state=state, stopped_reason="agent_token_budget_exhausted")
    )
    assert exhausted["probe_ok"] is False


def test_recovery_uses_persisted_case_without_loading_real_configuration(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    probe = _module()
    db_path = tmp_path / "probe.db"
    store = Store(db_path)
    batch_id = store.create_batch("recovery")
    repository = CaseRepository(store)
    state, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="02050015",
        award_name="最美教师",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="recover",
    ))
    report = VerificationReport(
        target_match="yes",
        year_match="yes",
        source_authority="secondary",
        coverage_complete="yes",
        recommended_action="accept_evidence",
        reason_codes=["secondary_source_only"],
        deterministic_action="accept_evidence",
        model_used=True,
    )
    trace = ToolObservation(
        call_id="call-1",
        tool_name="fetch_web_page",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        ok=True,
    )
    state.status = "waiting_human"
    state.reason_codes = ["secondary_source_only", "agent_recommendation_ready"]
    state.recommendation = "ready"
    state.latest_verification = report
    repository.save(state, traces=[trace], verifications=[report])
    store.close()
    monkeypatch.setattr(
        probe.config,
        "load_env",
        lambda: (_ for _ in ()).throw(AssertionError("must not load configuration")),
    )
    cfg = probe.AcceptanceConfig(recover_db=db_path, recover_label="S02")
    result = probe.recover(cfg)
    assert result["summary"]["status"] == "complete"
    assert result["summary"]["verification_cases"] == 1
    assert result["cases"][0]["verification"]["model_used"] is True
