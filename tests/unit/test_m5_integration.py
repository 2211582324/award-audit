from __future__ import annotations

from pathlib import Path

import pytest

from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.integration import (
    case_report_rows,
    derive_audit_case_input,
    ensure_imported_review_cases,
    ensure_review_cases,
    seed_from_evidence_report,
)
from award_audit.agent.toolkit.contracts import EvidenceArtifact
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.checks.l5_precheck import SearchHandoff
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry
from award_audit.core.reference.template_registry import build_template_spec


def _report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "resource_code": "04050014",
        "award_name": "示例奖",
        "year": "2026",
        "verdict": "无法核对",
        "confidence": "low",
        "source_kind": "none",
        "source_url": "https://official.example/page",
        "source_urls": ["https://official.example/page"],
        "found_assets": [],
        "submitted_count": 4,
        "extracted_count": 0,
        "missing": [],
        "extra": [],
        "reason_codes": ["coverage_unknown"],
    }
    report.update(overrides)
    return report


def _imported_file(
    path: Path,
    *,
    year: str = "2026",
    person: str = "张三",
) -> ImportedFile:
    return ImportedFile(
        batch="新文件批次",
        path=str(path),
        file_name=f"CON_GG_XK_RCPY_GXDJSCGR-通用示例奖-{year}.xlsx",
        claimed_table_code="CON_GG_XK_RCPY_GXDJSCGR",
        award_name="通用示例奖",
        year=year,
        sheet_name="数据",
        header_codes=["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"],
        header_names=["资源项码", "资源项", "项目名称", "获奖人", "年度"],
        rows=[["04050014", "通用示例奖", "年度人物", person, year]],
    )


@pytest.mark.parametrize(
    ("overrides", "trigger"),
    [
        ({"reason_codes": ["evidence_conflict"]}, "EVIDENCE_CONFLICT"),
        ({"reason_codes": ["year_mismatch"]}, "PAGE_TARGET_UNCERTAIN"),
        ({"reason_codes": ["zero_overlap"]}, "ZERO_OVERLAP"),
        ({"source_kind": "image"}, "IMAGE_ONLY"),
        ({"found_assets": ["https://official.example/list.png"]}, "IMAGE_ONLY"),
        ({"found_assets": ["https://official.example/list.pdf"]}, "PDF_ONLY"),
        (
            {
                "reason_codes": ["pdf_only"],
                "found_assets": [
                    "https://official.example/decorative.jpg",
                    "https://official.example/list.pdf",
                ],
            },
            "PDF_ONLY",
        ),
        (
            {
                "found_assets": [
                    "https://official.example/decorative.jpg",
                    "https://official.example/list.pdf",
                ],
            },
            "PDF_ONLY",
        ),
        ({"reason_codes": ["coverage_unknown"]}, "COVERAGE_UNKNOWN"),
    ],
)
def test_evidence_report_mapping_is_deterministic(
    overrides: dict[str, object], trigger: str
) -> None:
    seed = seed_from_evidence_report(1, _report(**overrides))
    assert seed is not None and seed.trigger_codes == [trigger]
    assert "missing_count" in seed.submitted_summary
    assert all(url.startswith("https://") for url in seed.known_urls)


def test_auto_pass_and_missing_resource_code_do_not_create_case() -> None:
    assert seed_from_evidence_report(
        1, _report(verdict="一致", confidence="high")
    ) is None
    assert seed_from_evidence_report(1, _report(resource_code="")) is None


def test_numeric_resource_code_is_restored_to_eight_digits() -> None:
    seed = seed_from_evidence_report(1, _report(resource_code="2040005"))
    assert seed is not None and seed.resource_code == "02040005"


def test_bridge_propagates_primary_and_secondary_official_domains() -> None:
    seed = seed_from_evidence_report(1, _report(
        source_url="https://award.example.gov.cn/notice",
        source_urls=["https://mirror.example.org/list"],
    ))

    assert seed is not None
    assert seed.submitted_summary["official_domains"] == ["award.example.gov.cn"]
    assert seed.submitted_summary["official_secondary_domains"] == [
        "mirror.example.org"
    ]


def test_bridge_uses_first_bound_url_when_source_url_is_omitted() -> None:
    seed = seed_from_evidence_report(1, _report(
        source_url="",
        source_urls=["https://cpipc.acge.org.cn/results/2025"],
    ))

    assert seed is not None
    assert seed.submitted_summary["official_domains"] == ["cpipc.acge.org.cn"]


def test_generic_bridge_derives_complete_case_context_without_manifest(
    tmp_path: Path,
) -> None:
    submission = tmp_path / "unknown-submission.xlsx"
    submission.touch()
    imported = _imported_file(submission)
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )
    ledger = LedgerEntry(
        resource_code="04050014",
        resource_name="通用示例奖",
        expected_count=25,
        collect_url="https://official.example/award-list",
        source="示例主管方",
    )

    seed = seed_from_evidence_report(
        7,
        _report(
            award_name="通用示例奖",
            submitted_count=1,
            extracted_count=1,
            missing=["来源侧新增对象"],
            extra=["提交侧待核对象"],
        ),
        imported_files=[imported],
        registry={imported.claimed_table_code: spec},
        ledger={ledger.resource_code: ledger},
    )

    assert seed is not None
    assert seed.submitted_summary["submission_file"] == str(submission)
    assert seed.submitted_summary["submission_files"] == [str(submission)]
    assert seed.submitted_summary["table_code"] == imported.claimed_table_code
    assert seed.submitted_summary["match_fields"] == ["XRYXM"]
    assert seed.submitted_summary["identity_primary_alternatives"] == [["XRYXM"]]
    assert seed.submitted_summary["attachment_match_fields"] == ["XRYXM"]
    assert seed.submitted_summary["match_combine"] == "first"
    assert seed.submitted_summary["submitted_rows"] == 1
    assert seed.submitted_summary["expected_scope_count"] == 1
    assert seed.submitted_summary["ledger_expected_count"] == 25
    assert seed.submitted_summary["source_only_items"] == ["来源侧新增对象"]
    assert seed.submitted_summary["submitted_only_items"] == ["提交侧待核对象"]
    assert seed.known_urls == [
        "https://official.example/page",
        "https://official.example/award-list",
    ]


def test_generic_bridge_keeps_all_same_resource_files_and_isolates_year(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "part-a.xlsx"
    second_path = tmp_path / "part-b.xlsx"
    other_year_path = tmp_path / "part-c.xlsx"
    for path in (first_path, second_path, other_year_path):
        path.touch()
    first = _imported_file(first_path, person="张三")
    second = _imported_file(second_path, person="李四")
    other_year = _imported_file(other_year_path, year="2025", person="王五")
    spec = build_template_spec(
        first.claimed_table_code,
        first.sheet_name,
        first.header_codes,
        first.header_names,
    )

    seed = seed_from_evidence_report(
        8,
        _report(award_name="通用示例奖", submitted_count=2),
        imported_files=[first, second, other_year],
        registry={first.claimed_table_code: spec},
        ledger={},
    )

    assert seed is not None
    assert seed.submitted_summary["submission_files"] == [
        str(first_path),
        str(second_path),
    ]
    assert seed.submitted_summary["submitted_rows"] == 2
    assert str(other_year_path) not in seed.submitted_summary["submission_files"]


def test_new_import_creates_generic_cases_without_acceptance_manifest(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "new-part-a.xlsx"
    second_path = tmp_path / "new-part-b.xlsx"
    blocked_path = tmp_path / "invalid.xlsx"
    for path in (first_path, second_path, blocked_path):
        path.touch()
    first = _imported_file(first_path, person="甲")
    second = _imported_file(second_path, person="乙")
    blocked = _imported_file(blocked_path, year="2025", person="丙")
    spec = build_template_spec(
        first.claimed_table_code,
        first.sheet_name,
        first.header_codes,
        first.header_names,
    )
    ledger = LedgerEntry(
        resource_code="04050014",
        resource_name="通用示例奖",
        expected_count=25,
        collect_url="https://official.example/generic-list",
        source="示例主管方",
    )
    store = Store(tmp_path / "generic-import.db")
    batch_id = store.create_batch("全新导入批次")

    result = ensure_imported_review_cases(
        store,
        batch_id,
        imported_files=[first, second, blocked],
        eligible_files=[first.file_name, second.file_name],
        registry={first.claimed_table_code: spec},
        ledger={ledger.resource_code: ledger},
    )

    assert result.created == 1
    state = CaseRepository(store).load(result.case_ids[0])
    assert state.resource_code == "04050014"
    assert state.year == "2026"
    assert state.known_urls == ["https://official.example/generic-list"]
    assert state.submitted_summary["submission_files"] == [
        str(first_path),
        str(second_path),
    ]
    assert state.submitted_summary["match_fields"] == ["XRYXM"]
    assert state.submitted_summary["submitted_rows"] == 2
    assert state.submitted_summary["expected_scope_count"] == 2
    assert state.submitted_summary["ledger_expected_count"] == 25
    assert str(blocked_path) not in state.submitted_summary["submission_files"]


@pytest.mark.parametrize(
    ("table_code", "codes", "expected_fields", "expected_attachment_fields"),
    [
        (
            "CON_GG_XK_RCPY_DXPM",
            ["ZYLBM", "ZYLB", "XDWMC", "FBND"],
            ["XDWMC"],
            ["XDWMC"],
        ),
        (
            "CON_GG_XK_RCPY_RZXX",
            ["ZYLBM", "ZYLB", "XDWMC", "TGRZZY", "QSSJ"],
            ["XDWMC", "TGRZZY"],
            ["XDWMC", "TGRZZY", "QSSJ"],
        ),
        (
            "CON_GG_XK_RCPY_XSJSHJ",
            ["ZYLBM", "ZYLB", "CSDWMC", "XRYXM", "XCSDW", "HJNF"],
            ["CSDWMC"],
            ["CSDWMC", "XCSDW"],
        ),
    ],
)
def test_generic_builder_derives_non_roster_match_profiles(
    tmp_path: Path,
    table_code: str,
    codes: list[str],
    expected_fields: list[str],
    expected_attachment_fields: list[str],
) -> None:
    path = tmp_path / f"{table_code}-新业务-2026.xlsx"
    path.touch()
    values = {
        "ZYLBM": "04050077",
        "ZYLB": "新业务",
        "XDWMC": "示例大学",
        "FBND": "2026",
        "TGRZZY": "计算机科学",
        "QSSJ": "2026-01",
        "CSDWMC": "示例大学",
        "XRYXM": "示例获奖人",
        "XCSDW": "示例大学",
        "HJNF": "2026",
    }
    imported = ImportedFile(
        batch="新批次",
        path=str(path),
        file_name=path.name,
        claimed_table_code=table_code,
        award_name="新业务",
        year="2026",
        sheet_name="数据",
        header_codes=codes,
        header_names=codes,
        rows=[[values[code] for code in codes]],
    )
    spec = build_template_spec(table_code, "数据", codes, codes)

    context = derive_audit_case_input(
        {"resource_code": "04050077", "year": "2026"},
        imported_files=[imported],
        registry={table_code: spec},
    )

    assert context.match_fields == expected_fields
    assert context.attachment_match_fields == expected_attachment_fields


def test_bridge_is_idempotent_and_report_rows_hide_local_paths(tmp_path: Path) -> None:
    store = Store(tmp_path / "integration.db")
    batch_id = store.create_batch("M5.7")
    handoff = SearchHandoff(
        resource_code="04050099",
        award_name="待搜索奖",
        year="2026",
        trigger_code="SOURCE_URL_MISSING",
        objective="查找官方来源",
    )
    first = ensure_review_cases(
        store,
        batch_id,
        search_handoffs=[handoff],
        audit_reports=[_report()],
    )
    second = ensure_review_cases(
        store,
        batch_id,
        search_handoffs=[handoff],
        audit_reports=[_report()],
    )
    assert first.created == 2 and first.existing == 0
    assert second.created == 0 and second.existing == 2

    case_id = first.case_ids[1]
    evidence = tmp_path / "evidence.pdf"
    evidence.write_bytes(b"%PDF-1.4\n%%EOF")
    repository = CaseRepository(store)
    state = repository.load(case_id)
    state.artifacts.append(EvidenceArtifact(
        kind="pdf",
        source_url="https://official.example/evidence.pdf",
        local_path=str(evidence),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=evidence.stat().st_size,
        fetched_at="2026-07-25T00:00:00Z",
    ))
    repository.save(state, artifacts=state.artifacts)
    encoded = str(case_report_rows(store, batch_id))
    assert "https://official.example/evidence.pdf" in encoded
    assert str(tmp_path) not in encoded and "local_path" not in encoded
