"""审查→落台账 管道：不同来源形态的联网核对结论都进复核台、人工入口不丢（多样性验收）。

这份测试锁的是"每种情况都有正确归宿"，而非"某个文件跑通"——Excel名单类自动出结论、
非 Excel（图片/无名单）诚实转人工并把官网/名单入口带进复核台，是防"针对单文件写死"的验收。
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl

from award_audit.agent import loop as loop_mod
from award_audit.agent.loop import EvidenceReport, _entry_keys, _submitted_keys, verify_resource
from award_audit.agent.tools import Attachment, PageContent
from award_audit.core.models.template import MatchProfile
from award_audit.core.pipeline.store import Store


class FakeLlm:
    """返回预设抽取结果的假 LLM。"""

    def __init__(self, extraction):  # noqa: ANN001
        self.extraction = extraction

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        return self.extraction


class SequentialLlm:
    def __init__(self, extractions):  # noqa: ANN001
        self.extractions = list(extractions)

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        return self.extractions.pop(0)


def test_m4_submitted_and_web_entries_use_same_collision_disambiguation(kit) -> None:
    profile = MatchProfile(
        kind="roster",
        submit_cols=["XMMC", "XFZRXM", "XDWMC"],
        web_fields=["title", "names", "org"],
        combine="first",
    )
    codes = ["ZYLBM", "ZYLB", "XMMC", "XFZRXM", "XDWMC", "LXNF"]
    files = [kit.build(
        [
            {"XMMC": "同名研究", "XFZRXM": "负责人甲", "XDWMC": "单位甲"},
            {"XMMC": "同名研究", "XFZRXM": "负责人乙", "XDWMC": "单位乙"},
        ],
        codes=codes,
        names=codes,
        table_code="CON_GG_XK_KXYJ_KYXM",
    )]
    web_entries = [
        {"title": "同名研究", "names": "负责人甲", "org": "单位甲"},
        {"title": "同名研究", "names": "负责人乙", "org": "单位乙"},
    ]

    submitted = _submitted_keys(files, profile)
    official = _entry_keys(web_entries, profile)

    assert submitted == official
    assert len(submitted) == 2


def test_m4_skips_old_year_page_before_using_later_target_year(
    kit, xwlwhj_spec, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    old_url = "https://official.example/2024-results"
    current_url = "https://official.example/2025-results"
    pages = {
        old_url: PageContent(url=old_url, status=200, text="2024 old roster"),
        current_url: PageContent(url=current_url, status=200, text="2025 current roster"),
    }
    monkeypatch.setattr(
        loop_mod.tools,
        "fetch_page",
        lambda url, timeout=15.0: pages[url],
    )
    files = [kit.build(
        [{
            "ZYLBM": "04050014",
            "ZYLB": kit.AWARD,
            "LWTM": "目标论文",
            "ZZXM": "张三",
            "PDNY": "2025",
        }],
        year="2025",
    )]
    llm = SequentialLlm([
        {
            "page_is_target": True,
            "page_year": "2024",
            "entries": [{"title": "旧届论文", "names": "李四", "org": ""}],
        },
        {
            "page_is_target": True,
            "page_year": "2025",
            "entries": [{"title": "目标论文", "names": "张三", "org": ""}],
        },
    ])

    report = verify_resource(
        "04050014",
        files,
        [old_url, current_url],
        xwlwhj_spec,
        llm,
        tmp_path,
    )

    assert report.source_url == current_url
    assert report.page_year == "2025"
    assert report.year_match is True
    assert report.verdict == "一致"
    assert "year_mismatch" not in report.reason_codes


# 功能：不同来源形态的 verify_resource 结论都能落台账并保留人工入口——Excel名单→"一致"、
#       无Excel(图片未启视觉)→"无法核对"且 found_assets 进库（复核台能拿到官网/名单入口）
# 设计：跑两次真实 verify_resource（Excel一致 / 无Excel图片），把 EvidenceReport 落进同一批次，
#       断言 store 两条 verdict 各归其位、无法核对那条 found_assets 完整入库——"每种情况有归宿"的端到端锁
def test_audit_results_pipeline_diverse_sources(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    reports: list[EvidenceReport] = []

    # 形态①：Excel 附件、官网与提交一致 → "一致"
    page_x = PageContent(url="https://x/a", status=200, text="",
                         attachments=[Attachment(text="附件:名单", url="https://x/m.xlsx", is_excel=True)])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page_x)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda url, d, timeout=30.0, **k: d / "m.xlsx")
    monkeypatch.setattr(loop_mod.tools, "parse_award_excel",
                        lambda p, max_rows=2000: {
                            "sheet": "s", "n_rows": 2,
                            "rows": [["题目", "作者"], ["论文甲", "张三"]],
                        })
    f1 = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    reports.append(verify_resource("04050014", f1, ["https://x/a"], xwlwhj_spec,
                                   FakeLlm({"is_target": True, "year": "2024",
                                            "title_col": 0, "names_col": 1}), tmp_path))

    # 形态②：无 Excel、页面只有图片名单、未启视觉 → "无法核对"，图片进 found_assets（人工入口）
    monkeypatch.setattr(loop_mod.config, "load_env", lambda: None)
    monkeypatch.delenv("AWARD_AUDIT_VISION", raising=False)
    page_img = PageContent(url="https://y/a", status=200, text="", images=["https://y/list.png"])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page_img)
    f2 = [kit.build([{"ZYLBM": "04050099", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2024"}])]
    reports.append(verify_resource("04050099", f2, ["https://y/a"], xwlwhj_spec, FakeLlm({}), tmp_path))

    # 落台账 → 读回，断言每种归宿都在、人工入口保留
    store = Store(":memory:")
    try:
        bid = store.find_or_create_batch("提交-T")
        store.add_audit_results(bid, [r.model_dump() for r in reports])
        rows = {r["resource_code"]: r for r in store.audit_results_of(bid)}
        assert rows["04050014"]["verdict"] == "一致"
        assert rows["04050099"]["verdict"] == "无法核对"
        assert "https://y/list.png" in json.loads(rows["04050099"]["found_assets_json"])  # 非 Excel 人工入口入库
    finally:
        store.close()


def test_m4_attachment_limit_cannot_produce_high_confidence_match(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    attachment_urls = [
        f"https://x/group-{index}.xlsx" for index in range(1, 102)
    ]
    page = PageContent(
        url="https://x/a",
        status=200,
        text="",
        attachments=[
            Attachment(text=f"第{index}组", url=url, is_excel=True)
            for index, url in enumerate(attachment_urls, start=1)
        ],
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    monkeypatch.setattr(
        loop_mod.tools,
        "download_file",
        lambda url, destination, timeout=30.0, **kwargs: destination / "group.xlsx",
    )
    monkeypatch.setattr(
        loop_mod.tools,
        "parse_award_excel",
        lambda path, max_rows=100_000: {
            "sheet": "名单",
            "n_rows": 2,
            "rows": [["论文题目", "作者"], ["论文甲", "张三"]],
        },
    )
    files = [
        kit.build([
            {
                "ZYLBM": "04050014",
                "ZYLB": kit.AWARD,
                "LWTM": "论文甲",
                "ZZXM": "张三",
                "PDNY": "2024",
            }
        ])
    ]

    report = verify_resource(
        "04050014",
        files,
        [page.url],
        xwlwhj_spec,
        FakeLlm({"is_target": True, "year": "2024", "title_col": 0}),
        tmp_path,
    )

    assert report.verdict == "无法核对"
    assert report.confidence == "low"
    assert "attachment_group_incomplete" in report.reason_codes
    assert attachment_urls[-1] in report.found_assets


def test_m4_truncated_page_body_cannot_produce_complete_match(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    page = PageContent(
        url="https://x/a",
        status=200,
        title="2024年学位论文获奖名单",
        text="论文甲 张三",
        text_truncated=True,
        original_text_chars=45_000,
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    files = [
        kit.build([
            {
                "ZYLBM": "04050014",
                "ZYLB": kit.AWARD,
                "LWTM": "论文甲",
                "ZZXM": "张三",
                "PDNY": "2024",
            }
        ])
    ]
    extraction = {
        "page_is_target": True,
        "page_year": "2024",
        "entries": [{"title": "论文甲", "names": "张三", "org": ""}],
    }

    report = verify_resource(
        "04050014",
        files,
        [page.url],
        xwlwhj_spec,
        FakeLlm(extraction),
        tmp_path,
    )

    assert report.verdict == "无法核对"
    assert report.confidence == "low"
    assert "page_text_truncated" in report.reason_codes


def test_m4_matching_page_body_cannot_ignore_unparsed_pdf_attachment(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: ANN001
    pdf_url = "https://x/full-roster.pdf"
    page = PageContent(
        url="https://x/a",
        status=200,
        title="2024年学位论文获奖名单",
        text="论文甲 张三",
        attachments=[Attachment(text="完整名单.pdf", url=pdf_url, is_excel=False)],
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    monkeypatch.setattr(
        loop_mod.tools,
        "download_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not excel")),
    )
    files = [
        kit.build([
            {
                "ZYLBM": "04050014",
                "ZYLB": kit.AWARD,
                "LWTM": "论文甲",
                "ZZXM": "张三",
                "PDNY": "2024",
            }
        ])
    ]
    extraction = {
        "page_is_target": True,
        "page_year": "2024",
        "entries": [{"title": "论文甲", "names": "张三", "org": ""}],
    }

    report = verify_resource(
        "04050014", files, [page.url], xwlwhj_spec, FakeLlm(extraction), tmp_path
    )

    assert report.verdict == "无法核对"
    assert report.confidence == "low"
    assert "unresolved_page_assets" in report.reason_codes
    assert report.evidence_assets[0].url == pdf_url


# 功能：audit 命令端到端把资源项级结论写进台账（precheck 通行 → verify_resource → 落库），复核台可见
# 设计：monkeypatch 导入/预检/verify_resource/LlmClient，yes=True 免审批，跑 _cmd_audit，
#       断言返回 0 且 store 里该批次有对应 audit_result——CLI 落库线通（不只是产 markdown）
def test_cmd_audit_writes_results_to_store(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    import award_audit.agent.llm as llm_mod
    import award_audit.agent.loop as loop_mod2
    from award_audit.cli import main as cli
    from award_audit.core.pipeline.checks.l5_precheck import PrecheckResult

    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    monkeypatch.setattr(cli.importer, "import_batch", lambda folder: files)
    monkeypatch.setattr(cli, "load_template_registry", lambda: {kit.XWLWHJ_CODE: xwlwhj_spec})
    monkeypatch.setattr(cli, "load_ledger", lambda: {})
    monkeypatch.setattr(cli.l5_precheck, "run_batch",
                        lambda f, led, p: PrecheckResult(issues=[], passable=["04050014"],
                                                         passable_urls={"04050014": ["https://x/a"]}))
    monkeypatch.setattr(llm_mod, "LlmClient", lambda *a, **k: object())
    rep = EvidenceReport(resource_code="04050014", award_name=kit.AWARD, year="2024",
                         verdict="一致", confidence="high", source_kind="excel",
                         extracted_count=1, submitted_count=1)
    monkeypatch.setattr(loop_mod2, "verify_resource", lambda *a, **k: rep)
    monkeypatch.setattr(cli.config, "out_dir", lambda: tmp_path)

    db = tmp_path / "audit.db"
    rc = cli._cmd_audit(tmp_path, yes=True, limit=10, db=db)
    assert rc == 0
    store = Store(db)
    try:
        bid = store.find_or_create_batch(tmp_path.name)  # 复用 audit 建的批次
        rows = store.audit_results_of(bid)
        assert len(rows) == 1 and rows[0]["verdict"] == "一致" and rows[0]["resource_code"] == "04050014"
    finally:
        store.close()


# 功能：验证 review 一站式——解析一次→ingest 落 staging→L5 verify 落 audit_result 到同一 batch_id→出统一报告（含 L5 段）
# 设计：monkeypatch importer/load_*/l5_precheck/verify_resource/LlmClient/out_dir，yes=True 跑 _cmd_review，
#       断言同一 batch_id 既有 staging 又有 audit_result、统一 md 含「联网核对(L5)」段——CLI 统一编排线通（不再两条命令两份产出）
def test_cmd_review_end_to_end(kit, xwlwhj_spec, resource_map, tmp_path, monkeypatch) -> None:
    import award_audit.agent.llm as llm_mod
    import award_audit.agent.loop as loop_mod2
    import award_audit.agent.review_workflow as review_workflow
    from award_audit.cli import main as cli
    from award_audit.core.pipeline.checks.l5_precheck import AuditTarget, PrecheckResult

    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    monkeypatch.setattr(cli.importer, "import_batch", lambda folder: files)
    monkeypatch.setattr(cli, "load_template_registry", lambda: {kit.XWLWHJ_CODE: xwlwhj_spec})
    monkeypatch.setattr(cli, "load_resource_map", lambda: resource_map)
    monkeypatch.setattr(cli, "load_ledger", lambda: {})
    monkeypatch.setattr(cli.l5_precheck, "run_batch",
                        lambda f, led, p: PrecheckResult(issues=[], passable=["04050014"],
                                                         passable_urls={"04050014": ["https://x/a"]},
                                                         passable_targets=[AuditTarget(
                                                             resource_code="04050014",
                                                             year="2024",
                                                             award_name=kit.AWARD,
                                                             urls=["https://x/a"],
                                                             probe_status="passable",
                                                             submitted_count=1,
                                                         )]))
    monkeypatch.setattr(llm_mod, "LlmClient", lambda *a, **k: object())
    rep = EvidenceReport(resource_code="04050014", award_name=kit.AWARD, year="2024",
                         verdict="一致", confidence="high", source_kind="excel",
                         extracted_count=1, submitted_count=1)
    monkeypatch.setattr(loop_mod2, "verify_resource", lambda *a, **k: rep)
    monkeypatch.setattr(review_workflow, "verify_resource", lambda *a, **k: rep)
    monkeypatch.setattr(cli.config, "out_dir", lambda: tmp_path)

    db = tmp_path / "review.db"
    rc = cli._cmd_review(tmp_path, yes=True, limit=0, db=db)
    assert rc == 0
    store = Store(db)
    try:
        batches = store.list_batches()
        assert len(batches) == 1  # review 只建一个批次，L0–L4 与 L5 落同一 batch_id
        bid = batches[0]["id"]
        assert len(store.staging_of(bid)) == 1  # L0–L4 落 staging
        audits = store.audit_results_of(bid)
        assert len(audits) == 1 and audits[0]["verdict"] == "一致" and audits[0]["resource_code"] == "04050014"
        assert store.list_audit_cases(batch_id=bid) == []  # high-confidence auto-pass 不扩大建案
    finally:
        store.close()


def test_cmd_review_creates_m5_cases_and_unified_case_report(
    kit, xwlwhj_spec, resource_map, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    import award_audit.agent.llm as llm_mod
    import award_audit.agent.loop as loop_mod2
    import award_audit.agent.review_workflow as review_workflow
    from award_audit.cli import main as cli
    from award_audit.core.pipeline.checks.l5_precheck import (
        AuditTarget,
        PrecheckResult,
        SearchHandoff,
    )

    files = [kit.build([{
        "ZYLBM": "04050014",
        "ZYLB": kit.AWARD,
        "LWTM": "论文甲",
        "ZZXM": "张三",
        "PDNY": "2024",
    }])]
    monkeypatch.setattr(cli.importer, "import_batch", lambda folder: files)
    monkeypatch.setattr(
        cli, "load_template_registry", lambda: {kit.XWLWHJ_CODE: xwlwhj_spec}
    )
    monkeypatch.setattr(cli, "load_resource_map", lambda: resource_map)
    monkeypatch.setattr(cli, "load_ledger", lambda: {})
    monkeypatch.setattr(cli.l5_precheck, "run_batch", lambda f, led, p: PrecheckResult(
        issues=[],
        passable=["04050014"],
        passable_urls={"04050014": ["https://official.example/page"]},
        passable_targets=[AuditTarget(
            resource_code="04050014",
            year="2024",
            award_name=kit.AWARD,
            urls=["https://official.example/page"],
            probe_status="passable",
            submitted_count=1,
        )],
        search_handoffs=[SearchHandoff(
            resource_code="04050099",
            award_name="待搜索奖",
            year="2024",
            trigger_code="SOURCE_URL_MISSING",
            objective="查找官方名单",
        )],
    ))
    monkeypatch.setattr(llm_mod, "LlmClient", lambda *args, **kwargs: object())
    difficult = EvidenceReport(
        resource_code="04050014",
        award_name=kit.AWARD,
        year="2024",
        verdict="无法核对",
        confidence="low",
        source_kind="image",
        source_url="https://official.example/page",
        found_assets=["https://official.example/list.png"],
        submitted_count=1,
        reason_codes=["image_source", "coverage_unknown"],
    )
    monkeypatch.setattr(loop_mod2, "verify_resource", lambda *args, **kwargs: difficult)
    monkeypatch.setattr(
        review_workflow, "verify_resource", lambda *args, **kwargs: difficult
    )
    monkeypatch.setattr(cli.config, "out_dir", lambda: tmp_path)
    deep_review_calls: list[tuple[Path, int, list[Path]]] = []

    def fake_run_queued(db_path, batch_id, *, evidence_roots, **_kwargs):  # noqa: ANN001
        deep_review_calls.append((Path(db_path), batch_id, list(evidence_roots)))
        return []

    monkeypatch.setattr(
        review_workflow,
        "run_queued_review_cases",
        fake_run_queued,
    )

    db = tmp_path / "review-m5.db"
    assert cli._cmd_review(tmp_path, yes=True, limit=0, db=db, run_m5=True) == 0
    assert deep_review_calls == [
        (db, 1, [tmp_path, tmp_path / "m5_evidence" / "batch-1"])
    ]
    store = Store(db)
    try:
        from award_audit.agent.harness.persistence import CaseRepository

        batch_id = int(store.list_batches()[0]["id"])
        cases = store.list_audit_cases(batch_id=batch_id)
        assert len(cases) == 2
        assert {str(case["status"]) for case in cases} == {"queued"}
        assert {
            str(case["trigger_codes_json"]) for case in cases
        } == {'["SOURCE_URL_MISSING"]', '["IMAGE_ONLY"]'}
        difficult_row = next(
            case for case in cases if str(case["resource_code"]) == "04050014"
        )
        state = CaseRepository(store).load(int(difficult_row["id"]))
        assert state.submitted_summary["submission_file"] == files[0].path
        assert state.submitted_summary["submission_files"] == [files[0].path]
        assert state.submitted_summary["match_fields"] == ["LWTM", "ZZXM"]
        assert state.submitted_summary["identity_primary_alternatives"] == [
            ["LWTM", "ZZXM"]
        ]
        assert state.submitted_summary["attachment_match_fields"] == [
            "LWTM",
            "ZZXM",
        ]
        assert state.submitted_summary["submitted_rows"] == 1
    finally:
        store.close()
    summary = (tmp_path / f"反馈摘要-{tmp_path.name}.md").read_text(encoding="utf-8")
    assert "## M5 疑难案件" in summary and "queued 2" in summary
    workbook = openpyxl.load_workbook(tmp_path / f"反馈意见-{tmp_path.name}.xlsx")
    assert "疑难案件" in workbook.sheetnames
# 功能：PDF-only 来源形态——页面只有 PDF 附件(非 Excel)、无正文/图片 → "无法核对"+no_list，
#       PDF url 进 found_assets 并落库（复核台拿到人工入口）；补齐 Excel/图片/PDF 多形态归宿
# 设计：download_file 抛 RuntimeError 模拟 excel_only 拒收 PDF → acquire_excel_grid 返 None；
#       页面正文空 → 无名单可抽；断言 no_list、found_assets 含 PDF、落库读回该入口
def test_audit_pdf_only_source_to_human(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    page = PageContent(url="https://z/a", status=200, text="",
                       attachments=[Attachment(text="附件:获奖名单(PDF)",
                                               url="https://z/list.pdf", is_excel=False)])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)

    def _reject_pdf(url, d, timeout=30.0, **kw):  # noqa: ANN001, ANN202  模拟 excel_only 拒收 PDF
        raise RuntimeError("非 Excel（content-type=application/pdf），跳过")

    monkeypatch.setattr(loop_mod.tools, "download_file", _reject_pdf)
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文丙", "ZZXM": "王五", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://z/a"], xwlwhj_spec, FakeLlm({}), tmp_path)
    assert rep.verdict == "无法核对" and "pdf_only" in rep.reason_codes
    assert "https://z/list.pdf" in rep.found_assets
    assert [asset.model_dump() for asset in rep.evidence_assets] == [{
        "asset_version": 1,
        "url": "https://z/list.pdf",
        "parent_url": "https://z/a",
        "label": "附件:获奖名单(PDF)",
        "kind": "pdf",
        "status": "discovered",
        "content_type": "",
        "sha256": "",
        "size_bytes": 0,
        "fetched_at": "",
        "local_path": "",
        "truncated": False,
        "extraction_method": "",
        "error_code": "",
        "error_message": "",
        "metadata": {},
    }]

    store = Store(":memory:")
    try:
        bid = store.find_or_create_batch("提交-PDF")
        store.add_audit_results(bid, [rep.model_dump()])
        row = store.audit_results_of(bid)[0]
        assert "https://z/list.pdf" in json.loads(row["found_assets_json"])  # PDF 人工入口入库
        assert json.loads(row["evidence_assets_json"])[0]["parent_url"] == "https://z/a"
    finally:
        store.close()
