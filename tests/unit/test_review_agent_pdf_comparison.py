from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from award_audit.agent.review_agent import runner as runner_module
from award_audit.agent.toolkit import pdf as pdf_tools
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord
from award_audit.core.identity import normalize_identity


def test_complete_single_scope_graph_web_extraction_becomes_comparison_fact() -> None:
    packet = SimpleNamespace(
        award_name="全国高校黄大年式教师团队",
        year="2025",
        scopes=[SimpleNamespace(
            required=True,
            scope_id=7,
            source_role_type="team_or_unit",
        )],
        submission_summary=SimpleNamespace(submitted_rows=190),
    )
    graph_state = {"observations": [{
        "tool_name": "extract_search_document",
        "ok": True,
        "summary": {
            "source_url": "https://www.cernet.edu.cn/result.html",
            "data": {
                "coverage_complete": True,
                "award_name_match": True,
                "year_match": True,
                "observed_count": 190,
                "expected_count": 190,
                "matched_items": ["示例团队"],
                "missing_items": [],
            },
        },
    }]}

    facts = runner_module._graph_web_comparison_facts(graph_state, packet)

    assert len(facts) == 1
    assert facts[0]["scope_id"] == 7
    assert facts[0]["status"] == "complete"
    assert facts[0]["observed_count"] == 190


def test_incomplete_graph_web_extraction_stays_out_of_comparison() -> None:
    packet = SimpleNamespace(
        award_name="示例奖",
        year="2025",
        scopes=[SimpleNamespace(required=True, scope_id=7, source_role_type="work_or_project")],
        submission_summary=SimpleNamespace(submitted_rows=10),
    )
    graph_state = {"observations": [{
        "tool_name": "extract_search_document",
        "ok": True,
        "summary": {"source_url": "https://example.edu/result", "data": {
            "coverage_complete": False,
            "award_name_match": True,
            "year_match": True,
        }},
    }]}

    assert runner_module._graph_web_comparison_facts(graph_state, packet) == []


def test_complete_graph_web_fact_replaces_partial_fact_for_same_scope() -> None:
    partial = {"scope_id": 7, "status": "partial", "coverage_complete": False}
    other_scope = {"scope_id": 8, "status": "partial", "coverage_complete": False}
    complete = {"scope_id": 7, "status": "complete", "coverage_complete": True}

    merged = runner_module._merge_complete_graph_web_facts(
        [partial, other_scope], [complete]
    )

    assert merged == [other_scope, complete]


def test_unique_pdf_route_uses_document_scope_when_rows_omit_scope_label() -> None:
    records = [{
        "role_type": "work_or_project",
        "title": "attachment special-task roster",
        "category_values": [],
        "level_values": [],
        "row_values": {"project_title": "project alpha"},
    }]
    scope = {"business_scope": {"BZ": "full special-task scope label"}}

    routed = runner_module._records_for_assessment_scope(
        records,
        scope=scope,
        expected_role="work_or_project",
        discriminator_keys={"BZ"},
        document_routed_scope_count=1,
    )
    not_routed = runner_module._records_for_assessment_scope(
        records,
        scope=scope,
        expected_role="work_or_project",
        discriminator_keys={"BZ"},
        document_routed_scope_count=2,
    )

    assert routed == records
    assert not_routed == []


def test_graph_media_observations_build_one_logical_roster_and_keep_sources() -> None:
    assets = [
        EvidenceAssetRecord(
            url=f"https://official.example/{page}.png",
            parent_url="https://official.example/notice",
            kind="image",
            status="downloaded",
            sha256=str(page) * 64,
            local_path=f"C:/evidence/{page}.png",
            metadata={"page": page, "total_pages": 3},
        )
        for page in range(1, 4)
    ]
    graph_state = {"observations": [
        {
            "tool_name": "ocr_image",
            "summary": {"data": {"pages": [
                {
                    "page": 1,
                    "text": "roster text " * 10,
                    "lines": [{"text": "艺术人才培训项目（1项）"}],
                },
                {
                    "page": 2,
                    "text": "continued roster " * 10,
                    "lines": [{"text": "青年艺术创作人才项目（1项）"}],
                },
                {"page": 3, "text": "logo"},
            ]}},
        },
        {
            "tool_name": "vision_extract_roster",
            "summary": {"data": {"pages": [
                {
                    "page": 1,
                    "total_pages": 3,
                    "is_roster_page": True,
                    "section_title": "艺术人才培训项目",
                    "entries": [{"no": 1, "name": "项目甲", "org": "单位甲"}],
                    "first_no": 1,
                    "last_no": 1,
                    "truncated": False,
                    "unreadable": [],
                    "confidence": 0.98,
                    "image_sha256": "1" * 64,
                },
                {
                    "page": 2,
                    "total_pages": 3,
                    "is_roster_page": True,
                    "section_title": "",
                    "entries": [{
                        "no": 2,
                        "name": "项目乙",
                        "org": "单位乙",
                        "section_title": "音乐作曲",
                    }],
                    "first_no": 2,
                    "last_no": 2,
                    "truncated": False,
                    "unreadable": [],
                    "confidence": 0.97,
                    "image_sha256": "2" * 64,
                },
            ], "errors": [], "complete": True}},
        },
    ]}

    updated, aggregate, roster_keys = runner_module._hydrate_graph_image_assets(
        assets, graph_state, ["艺术人才培训项目", "青年艺术创作人才项目"]
    )

    assert aggregate is not None
    assert len(aggregate.metadata["image_sources"]) == 3
    assert len(aggregate.metadata["semantic_records"]) == 2
    assert aggregate.metadata["vision_pages"][1]["inherited_section_title"] == (
        "艺术人才培训项目"
    )
    assert aggregate.metadata["semantic_records"][0]["row_values"]["XMMC"] == "项目甲"
    assert (
        aggregate.metadata["semantic_records"][0]["row_values"]["XRYXM"]
        == aggregate.metadata["semantic_records"][0]["identity"]
    )
    assert (
        aggregate.metadata["semantic_records"][0]["row_values"]["XDWMC"]
        == aggregate.metadata["vision_pages"][0]["entries"][0]["org"]
    )
    assert aggregate.metadata["semantic_records"][1]["row_values"]["XMLB"] == (
        "青年艺术创作人才项目"
    )
    assert aggregate.metadata["semantic_records"][1]["source_section_title"] == (
        "音乐作曲"
    )
    assert aggregate.metadata["scope_segmentation_complete"] is True
    assert aggregate.metadata["declared_scope_total"] == 2
    assert [asset.status for asset in updated] == ["parsed", "parsed", "parsed"]
    assert updated[2].metadata["image_is_roster_page"] is False
    assert len(roster_keys) == 2


def test_graph_scan_observations_hydrate_the_source_pdf() -> None:
    pdf_sha = "a" * 64
    image_sha = "b" * 64
    pdf = EvidenceAssetRecord(
        url="https://official.example/scan.pdf",
        kind="pdf",
        status="parsed",
        sha256=pdf_sha,
        local_path="C:/evidence/scan.pdf",
        metadata={"page_count": 1},
    )
    graph_state = {"observations": [
        {"tool_name": "render_pdf_pages", "summary": {
            "sha256": pdf_sha,
            "data": {"pages": [{
                "page": 1, "path": "C:/evidence/pages/scan-1.png", "sha256": image_sha,
            }]},
        }},
        {"tool_name": "ocr_image", "summary": {"data": {"pages": [{
            "page": 1, "path": "C:/evidence/pages/scan-1.png",
            "text": "名单文字 " * 20, "lines": [], "image_sha256": image_sha,
        }]}}},
        {"tool_name": "vision_extract_roster", "summary": {"data": {
            "pages": [{
                "page": 1, "total_pages": 1, "image_sha256": image_sha,
                "is_roster_page": True, "section_title": "一等奖",
                "entries": [{"no": 1, "name": "项目甲", "org": "单位甲"}],
                "first_no": 1, "last_no": 1, "truncated": False,
                "unreadable": [], "confidence": 0.99,
            }], "errors": [],
        }}},
    ]}

    hydrated, keys = runner_module._hydrate_graph_pdf_assets([pdf], graph_state)

    assert keys == {runner_module.asset_packet_key(hydrated[0])}
    assert hydrated[0].extraction_method == "langgraph_pdf_ocr_vision"
    assert hydrated[0].metadata["semantic_records"][0]["identity"] == "项目甲"
    assert hydrated[0].metadata["semantic_records"][0]["source_anchor"] == "image:1:row:1"


def test_pdf_uses_row_category_when_parent_scope_is_only_in_document_title() -> None:
    records = [
        {
            "role_type": "work_or_project",
            "title": "project alpha",
            "category_values": ["Planning Fund"],
            "level_values": [],
            "row_values": {"project_title": "project alpha"},
        },
        {
            "role_type": "work_or_project",
            "title": "project beta",
            "category_values": ["Youth Fund"],
            "level_values": [],
            "row_values": {"project_title": "project beta"},
        },
    ]
    scope = {
        "business_scope": {
            "BZ": "Western Project",
            "XMLB": "Planning Fund",
        },
    }

    routed = runner_module._records_for_assessment_scope(
        records,
        scope=scope,
        expected_role="work_or_project",
        discriminator_keys={"BZ", "XMLB"},
        document_routed_scope_count=2,
    )

    assert [record["title"] for record in routed] == ["project alpha"]


def test_pdf_table_records_preserve_project_category(monkeypatch) -> None:  # noqa: ANN001
    page = pdf_tools.PdfTextPage(
        page=1,
        text="",
        text_chars=0,
        table_rows=1,
        tables=[pdf_tools.PdfTableCandidate(
            rows=[
                ["序号", "项目名称", "项目类别", "申请人"],
                ["1", "项目甲", "青年基金项目", "张三"],
            ],
            row_count=2,
            column_count=4,
        )],
    )
    monkeypatch.setattr(
        runner_module.pdf_tools, "extract_pdf_text", lambda *_args, **_kwargs: [page]
    )

    records, complete = runner_module._pdf_semantic_roster_records(
        Path("sample.pdf"), {"page_count": 1}
    )

    assert complete is True
    assert records[0]["identity"] == "项目甲"
    assert records[0]["category_values"] == ["青年基金项目"]


def test_pdf_numbered_list_records_preserve_section_context(monkeypatch) -> None:  # noqa: ANN001
    page = pdf_tools.PdfTextPage(
        page=1,
        text="一、最佳组织奖\n1. 甲知识产权局\n二、最佳推荐奖\n1. 乙院士",
        text_chars=30,
        table_rows=0,
    )
    monkeypatch.setattr(
        runner_module.pdf_tools, "extract_pdf_text", lambda *_args, **_kwargs: [page]
    )

    records, complete = runner_module._pdf_semantic_roster_records(
        Path("numbered-list.pdf"), {"page_count": 1}
    )

    assert complete is True
    assert [(record["title"], record["identity"]) for record in records] == [
        ("最佳组织奖", "甲知识产权局"),
        ("最佳推荐奖", "乙院士"),
    ]
    assert all(record["role_type"] == "unclassified" for record in records)
    assert records[0]["source_anchor"] == "page:1:line:2"


def test_mixed_numbered_roster_routes_by_section_before_role_coercion() -> None:
    records = runner_module._pdf_numbered_list_records([
        pdf_tools.PdfTextPage(
            page=1,
            text="一、最佳组织奖\n1. 甲知识产权局\n二、最佳推荐奖\n1. 于吉红院士",
            text_chars=36,
            table_rows=0,
        ),
    ])
    scope = {"business_scope": {"XMLB": "最佳推荐奖"}}

    routed = runner_module._mixed_records_for_scope(records, scope)

    assert [record["identity"] for record in routed] == ["于吉红院士"]
    assert runner_module._source_identity_for_scope(routed[0], ["XRYXM"]) == (
        "于吉红院士"
    )


def test_pdf_table_merges_wrapped_rows_and_repairs_superscript_sequences(
    monkeypatch,
) -> None:  # noqa: ANN001
    pages = [pdf_tools.PdfTextPage(
        page=1,
        text="",
        text_chars=20,
        table_rows=101,
        tables=[pdf_tools.PdfTableCandidate(
            rows=[
                ["序号", "专利号", "专利名称"],
                ["1", "ZL-1", "一种很长的"],
                ["", "", "专利名称"],
                *[[str(number), f"ZL-{number}", f"专利{number}"] for number in range(2, 100)],
                ["1001", "ZL-100", "专利100"],
            ],
            row_count=102,
            column_count=3,
        )],
    ), pdf_tools.PdfTextPage(
        page=2,
        text="",
        text_chars=10,
        table_rows=3,
        tables=[pdf_tools.PdfTableCandidate(
            rows=[
                ["序号", "专利号", "专利名称"],
                ["", "", "续页"],
                ["1012", "ZL-101", "专利101"],
            ],
            row_count=3,
            column_count=3,
        )],
    )]
    monkeypatch.setattr(
        runner_module.pdf_tools, "extract_pdf_text", lambda *_args, **_kwargs: pages
    )

    records, complete = runner_module._pdf_semantic_roster_records(
        Path("wrapped.pdf"), {"page_count": 2}
    )

    assert complete is True
    assert len(records) == 101
    assert records[0]["row_values"]["专利名称"] == "一种很长的专利名称"
    assert records[-2]["row_values"]["专利名称"] == "专利100续页"
    assert records[-2]["row_values"]["序号"] == "100"
    assert records[-1]["row_values"]["序号"] == "101"


def test_pdf_whitespace_table_records_parse_team_roster_and_sequence() -> None:
    pages = [
        pdf_tools.PdfTextPage(
            page=1,
            text=(
                "序号        参赛单位        参赛队伍        奖项\n"
                " 1          甲大学          甲队            一等奖\n"
                " 2          乙大学          乙队            二等奖"
            ),
            text_chars=50,
            table_rows=0,
        ),
        pdf_tools.PdfTextPage(
            page=2,
            text="3          丙大学          丙队            三等奖",
            text_chars=20,
            table_rows=0,
        ),
    ]

    records, sequence_complete = runner_module._pdf_whitespace_table_records(pages)

    assert sequence_complete is True
    assert [record["identity"] for record in records] == ["甲队", "乙队", "丙队"]
    assert records[0]["row_values"]["参赛单位"] == "甲大学"
    assert records[2]["source_anchor"] == "page:2:row:3"


def test_pdf_extracted_tables_parse_competition_team_and_check_sequence(
    monkeypatch,
) -> None:  # noqa: ANN001
    pages = [
        pdf_tools.PdfTextPage(
            page=1,
            text="",
            text_chars=20,
            table_rows=3,
            tables=[pdf_tools.PdfTableCandidate(
                rows=[
                    ["序号", "参赛单位", "参赛队伍", "奖项"],
                    ["1", "甲大学", "甲队", "一等奖"],
                    ["2", "乙大学", "乙队", "二等奖"],
                ],
                row_count=3,
                column_count=4,
            )],
        ),
        pdf_tools.PdfTextPage(
            page=2,
            text="",
            text_chars=20,
            table_rows=1,
            tables=[pdf_tools.PdfTableCandidate(
                rows=[
                    ["3", "丙大学", "丙队", "三等奖"],
                ],
                row_count=1,
                column_count=4,
            )],
        ),
    ]
    monkeypatch.setattr(
        runner_module.pdf_tools, "extract_pdf_text", lambda *_args, **_kwargs: pages
    )

    records, complete = runner_module._pdf_semantic_roster_records(
        Path("competition.pdf"), {"page_count": 2}
    )

    assert complete is True
    assert [record["identity"] for record in records] == ["甲队", "乙队", "丙队"]
    assert records[0]["level_values"] == ["一等奖"]


def test_source_patent_number_is_an_xmbh_identity() -> None:
    identity = runner_module._source_identity_for_scope(
        {"row_values": {"专利号": "ZL202111257655.3"}}, ["XMBH"]
    )

    assert identity == "ZL202111257655.3"


def test_html_roster_headers_are_source_identity_candidates() -> None:
    assert runner_module._source_identity_for_scope(
        {"row_values": {"\u6307\u5bfc\u8001\u5e08": "\u5f20\u4e09"}}, ["XRYXM"]
    ) == "\u5f20\u4e09"
    assert runner_module._source_identity_for_scope(
        {"row_values": {"\u5355\u4f4d": "\u7532\u5927\u5b66"}}, ["XCSDW"]
    ) == "\u7532\u5927\u5b66"


def test_numbered_pdf_section_survives_page_boundary() -> None:
    records = runner_module._pdf_numbered_list_records([
        pdf_tools.PdfTextPage(
            page=1,
            text="\u4e00\u3001\u6700\u4f73\u7ec4\u7ec7\u5956\n1. A",
            text_chars=10,
            table_rows=0,
        ),
        pdf_tools.PdfTextPage(
            page=2,
            text="2. B",
            text_chars=4,
            table_rows=0,
        ),
    ])

    assert [(record["title"], record["identity"]) for record in records] == [
        ("\u6700\u4f73\u7ec4\u7ec7\u5956", "A"),
        ("\u6700\u4f73\u7ec4\u7ec7\u5956", "B"),
    ]


def test_person_honorific_is_preserved_for_primary_comparison() -> None:
    assert runner_module._primary_identity("\u4e8e\u5409\u7ea2\u9662\u58eb", 1) == "\u4e8e\u5409\u7ea2\u9662\u58eb"


def test_html_roster_records_reconstruct_team_person_and_organization_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "official-roster.txt"
    path.write_text(
        "\n".join([
            "\u7b2c\u4e8c\u5c4a\u5927\u8d5b\u83b7\u5956\u540d\u5355",
            "\u5b66\u6821\u540d\u79f0", "\u961f\u4f0d\u540d\u79f0", "\u5956\u9879",
            "\u7532\u5927\u5b66", "\u7532\u961f", "\u4e00\u7b49\u5956",
            "\u6307\u5bfc\u6559\u5e08", "\u5b66\u6821\u540d\u79f0", "\u6307\u5bfc\u8001\u5e08",
            "\u7532\u5927\u5b66", "\u5f20\u4e09",
            "\u7b2c\u4e8c\u5c4a\u5927\u8d5b", "\u7ec4\u7ec7\u5355\u4f4d", "\u5e8f\u53f7", "\u5355\u4f4d",
            "1", "\u7532\u5927\u5b66",
            "\u5927\u8d5b\u6982\u51b5",
        ]),
        encoding="utf-8",
    )

    records, complete = runner_module._html_semantic_roster_records(
        path,
        expected_sha256="",
        document_title="\u7b2c\u4e8c\u5c4a\u5927\u8d5b\u83b7\u5956\u540d\u5355",
    )

    assert complete is True
    assert [(record["role_type"], record["identity"]) for record in records] == [
        ("team", "\u7532\u961f"),
        ("instructor_or_person", "\u5f20\u4e09"),
        ("organization", "\u7532\u5927\u5b66"),
    ]


def test_duplicate_project_title_uses_pdf_applicant_and_school_to_disambiguate() -> None:
    title = "“AI辅导员”的意识形态风险及防范化解策略研究"
    identity, ambiguity = runner_module._source_comparison_identity(
        {
            "row_values": {
                "项目名称": title,
                "申请人": "廖江海",
                "学校名称": "深圳大学",
            }
        },
        primary_fields=["XMMC"],
        discriminator_fields=["XFZRXM", "XDWMC"],
        ambiguous_primaries={normalize_identity(title)},
    )

    assert identity == f"{title};廖江海;深圳大学"
    assert ambiguity == ""


def test_duplicate_project_title_without_source_discriminators_stays_ambiguous() -> None:
    title = "生成式人工智能对大学生就业的影响及对策研究"
    identity, ambiguity = runner_module._source_comparison_identity(
        {"row_values": {"项目名称": title}},
        primary_fields=["XMMC"],
        discriminator_fields=["XFZRXM", "XDWMC"],
        ambiguous_primaries={normalize_identity(title)},
    )

    assert identity == title
    assert "cannot disambiguate" in ambiguity


def test_duplicate_code_uses_project_title_and_reports_code_conflicts() -> None:
    submitted = {
        "ZL201910243534.X;一种基于动态白盒的数据处理方法、装置及设备",
        "ZL201910243534.X;一种空气交换量的测量方法及系统",
    }
    source_identity, ambiguity = runner_module._source_comparison_identity(
        {
            "row_values": {
                "专利号": "ZL201910245733.4",
                "专利名称": "一种基于动态白盒的数据处理方法、装置及设备",
            }
        },
        primary_fields=["XMBH"],
        discriminator_fields=["XDWMC"],
        supplemental_fields=["XMMC"],
        ambiguous_primaries={runner_module.normalize_identity("ZL201910243534.X")},
    )

    matched, extra, conflicts = runner_module._compare_source_identities(
        submitted,
        [
            source_identity,
            "ZL201910243534.X;一种空气交换量的测量方法及系统",
        ],
        primary_width=1,
        primary_fields=["XMBH"],
    )

    assert ambiguity == ""
    assert source_identity == "ZL201910245733.4;一种基于动态白盒的数据处理方法、装置及设备"
    assert matched == ["ZL201910243534.X;一种空气交换量的测量方法及系统"]
    assert extra == []
    assert conflicts == [{
        "submitted": "ZL201910243534.X;一种基于动态白盒的数据处理方法、装置及设备",
        "source": "ZL201910245733.4;一种基于动态白盒的数据处理方法、装置及设备",
        "fields": "XMBH",
        "reason": "same_secondary_different_primary",
    }]


def test_person_honorific_requires_semantic_disambiguation() -> None:
    matched, extra, conflicts = runner_module._compare_source_identities(
        {"于吉红"},
        ["于吉红院士"],
        primary_width=1,
        primary_fields=["XRYXM"],
        role_type="instructor_or_person",
    )

    assert matched == []
    assert extra == ["于吉红院士"]
    assert conflicts == []


def test_person_honorific_candidate_is_applied_only_after_llm_adjudication() -> None:
    facts = [{
        "scope_id": 7,
        "role_type": "instructor_or_person",
        "submitted_items": ["于吉红"],
        "matched_items": [],
        "extra_items": ["于吉红院士"],
        "identity_conflicts": [],
        "source_url": "https://official.example/people.pdf",
        "source_identity_anchors": {"于吉红院士": "page:2:row:1"},
    }]

    candidates = runner_module._identity_candidates_from_facts(facts)
    assert len(candidates) == 1
    assert candidates[0]["submitted"] == "于吉红"
    assert candidates[0]["source"] == "于吉红院士"

    updated, decisions = runner_module._apply_identity_adjudications(
        facts,
        candidates,
        {"decisions": [{
            "candidate_id": candidates[0]["candidate_id"],
            "decision": "same_identity",
            "confidence": 0.99,
            "reason": "The source adds an honorific to the same full name.",
        }]},
    )

    assert updated[0]["matched_items"] == ["于吉红"]
    assert updated[0]["extra_items"] == []
    assert decisions[0]["source_anchor"] == "page:2:row:1"


def test_semantic_identity_adjudication_rejects_many_to_one_matches() -> None:
    facts = [{
        "scope_id": 7,
        "role_type": "instructor_or_person",
        "submitted_items": ["甲", "乙"],
        "matched_items": [],
        "extra_items": ["来源人"],
        "source_identity_anchors": {"来源人": "page:1:row:1"},
    }]
    candidates = [
        {"candidate_id": "c1", "scope_id": 7, "role_type": "instructor_or_person",
         "submitted": "甲", "source": "来源人", "source_url": "https://official.example/a",
         "source_anchor": "page:1:row:1", "local_similarity": 0.8},
        {"candidate_id": "c2", "scope_id": 7, "role_type": "instructor_or_person",
         "submitted": "乙", "source": "来源人", "source_url": "https://official.example/a",
         "source_anchor": "page:1:row:1", "local_similarity": 0.8},
    ]

    with pytest.raises(ValueError, match="one-to-one"):
        runner_module._apply_identity_adjudications(
            facts,
            candidates,
            {"decisions": [
                {"candidate_id": "c1", "decision": "same_identity", "confidence": 0.99,
                 "reason": "same"},
                {"candidate_id": "c2", "decision": "same_identity", "confidence": 0.99,
                 "reason": "same"},
            ]},
        )


def test_source_identity_does_not_repeat_same_primary_as_supplement() -> None:
    identity, ambiguity = runner_module._source_comparison_identity(
        {"identity": "于吉红院士", "role_type": "unclassified"},
        primary_fields=["XRYXM"],
        supplemental_fields=["XMMC"],
        discriminator_fields=[],
        ambiguous_primaries=set(),
        role_type="instructor_or_person",
    )

    assert identity == "于吉红院士"
    assert ambiguity == ""
