"""L5-agent 核对循环测试：mock 抓取与 LLM，覆盖 Excel 路径/缺漏/年份不符/无法核对。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from award_audit.agent import loop as loop_mod
from award_audit.agent.loop import discover_resource, verify_resource
from award_audit.agent.tools import Attachment, PageContent
from award_audit.core.models.triage import decide_triage


def test_asset_kind_recognizes_download_endpoint_filename_and_link_label() -> None:
    pdf_url = (
        "https://example.gov.cn/module/download/downfile.jsp?"
        "showname=%E8%8E%B7%E5%A5%96%E5%90%8D%E5%8D%95.pdf"
    )

    assert loop_mod._asset_kind(pdf_url) == "pdf"
    assert loop_mod._asset_kind(
        "https://example.gov.cn/sysFile/downFile.do?fileId=123",
        "official roster.xlsx",
    ) == "xlsx"


def test_m4_persists_html_roster_text_as_hashed_local_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    page = PageContent(
        url="https://example.gov.cn/roster",
        status=200,
        title="official roster",
        text="School\nTeam\nAward\nExample University\nExample Team\nFirst Prize",
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda _url: page)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *_args, **_kwargs: None)

    report = discover_resource(
        "04030061", [], [page.url], None, None, tmp_path
    )

    asset = next(item for item in report.evidence_assets if item.kind == "html")
    assert asset.status == "parsed"
    assert asset.sha256
    assert Path(asset.local_path).read_text(encoding="utf-8") == page.text


def test_html_roster_role_count_requires_multiple_visible_roster_shapes() -> None:
    assert loop_mod._html_roster_role_count(
        "\u5b66\u6821\u540d\u79f0\n\u961f\u4f0d\u540d\u79f0\n\u5956\u9879\n"
        "\u5b66\u6821\u540d\u79f0\n\u6307\u5bfc\u8001\u5e08\n"
        "\u5e8f\u53f7\n\u5355\u4f4d"
    ) == 3
    assert loop_mod._html_roster_role_count("\u5b66\u6821\u540d\u79f0\n\u961f\u4f0d\u540d\u79f0") == 1


class FakeLlm:
    """返回预设抽取结果的假 LLM。"""

    def __init__(self, extraction):  # noqa: ANN001
        self.extraction = extraction

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        return self.extraction


class BoomJsonLlm:
    """json_call 抛 LlmError 的假 LLM（模拟空响应/网络失败），用来测健壮性。"""

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        from award_audit.agent.llm import LlmError
        raise LlmError("模型输出不是合法 JSON：Expecting value")


# 把 fetch_page/download/parse 换成离线假件：页面带一个 Excel 附件
def _patch_excel_path(monkeypatch, grid_rows):  # noqa: ANN001
    page = PageContent(url="https://x.gov.cn/a", status=200, text="公示正文",
                       attachments=[Attachment(text="附件2：名单(Excel)", url="https://x.gov.cn/m.xlsx", is_excel=True)])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda url, d, timeout=30.0, **kw: d / "m.xlsx")
    monkeypatch.setattr(loop_mod.tools, "parse_award_excel",
                        lambda p, max_rows=2000: {"sheet": "s", "n_rows": len(grid_rows), "rows": grid_rows})


# 功能：验证 Excel 附件路径 + 官网与提交完全一致 → 结论"一致"、高置信、来源 excel
# 设计：官网两条=提交两行（按题目归一匹配），断言 verdict/confidence/source_kind 三元组
def test_verify_consistent(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2024"},
    ])]
    _patch_excel_path(monkeypatch, [["题目", "作者"], ["论文甲", "张三"], ["论文乙", "李四"]])
    llm = FakeLlm({"is_target": True, "year": "2024", "title_col": 0, "names_col": 1})
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec, llm, tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "一致" and rep.source_kind == "excel" and rep.confidence == "high"
    assert rep.extracted_count == 2 and rep.submitted_count == 2


def test_m4_discovery_keeps_assets_without_calling_llm(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    _patch_excel_path(monkeypatch, [["题目", "作者"], ["论文甲", "张三"]])
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
    ])]

    class NoCallLlm:
        def __getattr__(self, name: str):  # noqa: ANN201
            raise AssertionError(f"M4 discovery must not access LLM.{name}")

    report = discover_resource(
        "04050014",
        files,
        ["https://x.gov.cn/a"],
        xwlwhj_spec,
        NoCallLlm(),  # type: ignore[arg-type]
        tmp_path,
    )

    assert report.verdict == "无法核对" and report.confidence == "low"
    assert report.missing == [] and report.extra == []
    assert "m4_discovery_only" in report.reason_codes
    assert {asset.kind for asset in report.evidence_assets} >= {"html", "xlsx"}
    assert any(asset.status == "parsed" for asset in report.evidence_assets)


def test_m4_discovery_records_a_direct_pdf_as_checked_asset(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_pdf)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        loop_mod.tools,
        "fetch_page",
        lambda url, timeout=15.0: PageContent(url=url, status=404, text=""),
    )
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
    ])]

    report = discover_resource(
        "04050014",
        files,
        ["https://x.gov.cn/official-roster.pdf"],
        xwlwhj_spec,
        None,
        tmp_path,
    )

    asset = next(item for item in report.evidence_assets if item.kind == "pdf")
    assert asset.status == "parsed"
    assert asset.sha256 and asset.local_path == str(source_pdf)
    assert asset.metadata["page_count"] > 0


def test_m4_discovery_acquires_page_pdf_attachments(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    attachment_url = "https://x.gov.cn/files/2026-roster.pdf"
    page_url = "https://x.gov.cn/notice"
    page = PageContent(
        url=page_url,
        status=200,
        text="official notice",
        attachments=[
            Attachment(text="2026 roster PDF", url=attachment_url, is_excel=False)
        ],
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_pdf)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "paper", "ZZXM": "name", "PDNY": "2024"},
    ])]

    report = discover_resource(
        "04050014", files, [page_url], xwlwhj_spec, None, tmp_path
    )

    asset = next(item for item in report.evidence_assets if item.url == attachment_url)
    assert asset.kind == "pdf" and asset.status == "parsed"
    assert asset.parent_url == page_url and asset.label == "2026 roster PDF"
    assert asset.sha256 and asset.local_path == str(source_pdf)
    assert asset.extraction_method == "m4_attachment_pdf_inspection"


def test_m4_discovery_downloads_and_hashes_page_images(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_image = (
        Path(__file__).parents[1]
        / "data"
        / "m5_golden"
        / "vision"
        / "clean_roster.png"
    )
    page_url = "https://x.gov.cn/notice"
    image_urls = [
        "https://x.gov.cn/images/roster-01.png",
        "https://cdn.x.gov.cn/images/roster-01-copy.png",
    ]
    page = PageContent(
        url=page_url,
        status=200,
        text="official roster notice",
        images=image_urls,
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_image)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    files = [kit.build([{
        "ZYLBM": "04050014",
        "ZYLB": kit.AWARD,
        "LWTM": "paper",
        "ZZXM": "name",
        "PDNY": "2024",
    }])]

    report = discover_resource(
        "04050014", files, [page_url], xwlwhj_spec, None, tmp_path
    )

    images = [asset for asset in report.evidence_assets if asset.kind == "image"]
    assert [asset.url for asset in images] == image_urls
    assert all(asset.status == "downloaded" for asset in images)
    assert all(asset.content_type == "image/png" for asset in images)
    assert images[0].sha256 == images[1].sha256
    assert [asset.metadata["page"] for asset in images] == [1, 2]
    assert all(asset.metadata["total_pages"] == 2 for asset in images)
    assert all(asset.metadata["width"] > 0 for asset in images)
    assert all(asset.local_path == str(source_image) for asset in images)


def test_m4_retries_extensionless_pdf_rejected_by_excel_collector(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    attachment_url = "https://x.gov.cn/download?file=opaque"
    page_url = "https://x.gov.cn/notice"
    acquired = SimpleNamespace(
        found_assets=[attachment_url],
        documents=[],
        attachment_errors={
            attachment_url: "UnsafeFileError: file type pdf is not allowed here"
        },
        attachment_parent_urls={attachment_url: page_url},
        page_url=page_url,
        all_attachments_processed=False,
    )
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_pdf)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: acquired)
    monkeypatch.setattr(
        loop_mod.tools,
        "fetch_page",
        lambda *args, **kwargs: PageContent(url=page_url, status=404, text=""),
    )
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "paper", "ZZXM": "name", "PDNY": "2024"},
    ])]

    report = discover_resource(
        "04050014", files, [page_url], xwlwhj_spec, None, tmp_path
    )

    asset = next(item for item in report.evidence_assets if item.url == attachment_url)
    assert asset.kind == "pdf" and asset.status == "parsed"
    assert asset.parent_url == page_url and asset.sha256
    assert asset.extraction_method == "m4_extensionless_attachment_pdf_inspection"


def test_m4_binary_probes_an_unknown_page_attachment_as_pdf(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    attachment_url = "https://x.gov.cn/sysFile/downFile.do?fileId=opaque"
    page_url = "https://x.gov.cn/notice"
    page = PageContent(
        url=page_url,
        status=200,
        text="official notice",
        attachments=[Attachment(text="official award roster", url=attachment_url, is_excel=False)],
    )
    calls: list[dict[str, object]] = []

    def download(url, _workdir, **kwargs):  # noqa: ANN001, ANN202
        calls.append({"url": url, **kwargs})
        return source_pdf

    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", download)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    files = [kit.build([{
        "ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "paper", "ZZXM": "name", "PDNY": "2024",
    }])]

    report = discover_resource(
        "04050014", files, [page_url], xwlwhj_spec, None, tmp_path
    )

    asset = next(item for item in report.evidence_assets if item.url == attachment_url)
    assert asset.kind == "pdf" and asset.status == "parsed"
    assert asset.sha256 and asset.local_path == str(source_pdf)
    assert asset.extraction_method == "m4_extensionless_attachment_pdf_inspection"
    assert calls == [{"url": attachment_url, "excel_only": False, "referer": page_url}]


def test_m4_marks_unknown_attachment_http_403_as_access_denied(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    attachment_url = "https://x.gov.cn/sysFile/downFile.do?fileId=blocked"
    page_url = "https://x.gov.cn/notice"
    page = PageContent(
        url=page_url,
        status=200,
        text="official notice",
        attachments=[Attachment(text="official award roster", url=attachment_url, is_excel=False)],
    )
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(
        loop_mod.tools,
        "download_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("attachment download failed HTTP 403: blocked")
        ),
    )
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    files = [kit.build([{
        "ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "paper", "ZZXM": "name", "PDNY": "2024",
    }])]

    report = discover_resource(
        "04050014", files, [page_url], xwlwhj_spec, None, tmp_path
    )

    asset = next(item for item in report.evidence_assets if item.url == attachment_url)
    assert asset.status == "access_denied"
    assert asset.error_code == "ATTACHMENT_ACCESS_DENIED"
    assert asset.metadata["http_status"] == 403
    assert asset.parent_url == page_url
    assert "attachment_access_denied" in report.reason_codes


def test_m4_direct_pdf_failure_is_not_overwritten_as_html(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_pdf)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        loop_mod.pdf_tools,
        "inspect_pdf",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken PDF parser")),
    )
    monkeypatch.setattr(
        loop_mod.tools,
        "fetch_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct PDFs must not be fetched as HTML")
        ),
    )
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
    ])]

    report = discover_resource(
        "04050014",
        files,
        ["https://x.gov.cn/official-roster.pdf"],
        xwlwhj_spec,
        None,
        tmp_path,
    )

    assert len(report.evidence_assets) == 1
    asset = report.evidence_assets[0]
    assert asset.kind == "pdf" and asset.status == "failed"
    assert asset.error_code == "DIRECT_PDF_ACQUISITION_FAILED"
    assert asset.sha256 and asset.local_path == str(source_pdf)
    assert asset.extraction_method == "m4_direct_pdf_downloaded_unparsed"
    assert "broken PDF parser" in asset.error_message


def test_m4_excludes_direct_pdf_attachments_from_excel_collector(
    kit,
    xwlwhj_spec,
    tmp_path,
    monkeypatch,
) -> None:
    source_pdf = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    captured: dict[str, object] = {}

    def acquire(urls, workdir, **kwargs):  # noqa: ANN001, ARG001
        captured["urls"] = urls
        captured.update(kwargs)
        return None

    monkeypatch.setattr(loop_mod.tools, "download_file", lambda *args, **kwargs: source_pdf)
    monkeypatch.setattr(loop_mod.tools, "acquire_excel_grid", acquire)
    monkeypatch.setattr(
        loop_mod.tools,
        "fetch_page",
        lambda url, timeout=15.0: PageContent(url=url, status=404, text=""),
    )
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "paper", "ZZXM": "name", "PDNY": "2024"},
    ])]

    discover_resource(
        "04050014",
        files,
        ["https://x.gov.cn/notice", "https://x.gov.cn/list.pdf"],
        xwlwhj_spec,
        None,
        tmp_path,
    )

    assert captured["urls"] == ["https://x.gov.cn/notice"]
    attachment_filter = captured["attachment_filter_fn"]
    assert callable(attachment_filter)
    assert not attachment_filter(
        Attachment(text="PDF", url="https://x.gov.cn/list.pdf", is_excel=False)
    )
    assert attachment_filter(
        Attachment(text="XLSX", url="https://x.gov.cn/list.xlsx", is_excel=True)
    )


# 功能：验证官网多一条提交没有 → "疑似缺漏"并列出缺的条目（错误#5/#9 的联网证据）
# 设计：官网三条、提交两行，断言 missing 恰含缺的那条
def test_verify_missing(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2024"},
    ])]
    _patch_excel_path(monkeypatch, [
        ["题目", "作者"],
        ["论文甲", "张三"],
        ["论文乙", "李四"],
        ["论文丙", "王五"],
    ])
    llm = FakeLlm({
        "is_target": True, "year": "2024", "title_col": 0, "names_col": 1,
    })
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec, llm, tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "疑似缺漏" and rep.missing == ["论文丙;王五"]


# 功能：论文题目重复但作者各异，来源只给题目时不得折叠成“基本一致”。
# 设计：提交 8 个“题目+作者”身份，来源缺作者，只能分流 M5。
def test_verify_partial_when_rows_collapse(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    _patch_excel_path(monkeypatch, [["作品名称"], ["甲"], ["乙"]])
    rows = [{"ZYLBM": "04050014", "ZYLB": kit.AWARD,
             "LWTM": ("甲" if i % 2 == 0 else "乙"), "ZZXM": f"人{i}", "PDNY": "2024"}
            for i in range(8)]
    files = [kit.build(rows)]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          FakeLlm({}), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对" and rep.confidence == "low"
    assert "identity_fields_unverified" in rep.reason_codes


# 功能：干净一致（单源 Excel、无折叠）→ 无降级码、分诊"快速放行"，reason_codes 进 model_dump
# 设计：复用一致 setup，断言 reason_codes 为空、decide_triage→auto_pass，且 model_dump 带 reason_codes 键（落库链需要）
def test_verify_consistent_is_auto_pass(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2024"},
    ])]
    _patch_excel_path(monkeypatch, [["题目", "作者"], ["论文甲", "张三"], ["论文乙", "李四"]])
    llm = FakeLlm({"is_target": True, "year": "2024", "title_col": 0, "names_col": 1})
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec, llm, tmp_path)  # type: ignore[arg-type]
    assert rep.reason_codes == []  # 无降级：单源、无折叠、Excel 来源
    assert decide_triage(rep.verdict, rep.confidence) == "auto_pass"
    assert "reason_codes" in rep.model_dump()  # 进 model_dump → 能落库/汇报


# 功能：复合身份缺字段走 identity_fields_unverified，不再误记为同键折叠。
# 设计：复用题目重复 setup，断言不产生 collapsed_rows，分诊转人工。
def test_verify_collapse_flags_reason_code(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    _patch_excel_path(monkeypatch, [["作品名称"], ["甲"], ["乙"]])
    rows = [{"ZYLBM": "04050014", "ZYLB": kit.AWARD,
             "LWTM": ("甲" if i % 2 == 0 else "乙"), "ZZXM": f"人{i}", "PDNY": "2024"}
            for i in range(8)]
    rep = verify_resource("04050014", [kit.build(rows)], ["https://x.gov.cn/a"], xwlwhj_spec,
                          FakeLlm({}), tmp_path)  # type: ignore[arg-type]
    assert "collapsed_rows" not in rep.reason_codes
    assert "identity_fields_unverified" in rep.reason_codes
    assert decide_triage(rep.verdict, rep.confidence) == "manual"


# 功能：页面/附件都拿不到名单 → 打 no_list 码、分诊落"转人工"
# 设计：复用 404 setup，断言 reason_codes 含 no_list、decide_triage(无法核对,*)→manual
def test_verify_unverifiable_flags_no_list(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loop_mod.tools, "fetch_page",
                        lambda url, timeout=15.0: PageContent(url=url, status=404, text=""))
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/dead"], xwlwhj_spec,
                          FakeLlm({}), tmp_path)  # type: ignore[arg-type]
    assert "no_list" in rep.reason_codes
    assert decide_triage(rep.verdict, rep.confidence) == "manual"


# 功能：官网名单与提交零重叠（模拟跨年度/错来源）→ "无法核对"转人工，不硬报缺漏
# 设计：官网 3 条作品、提交 3 条完全不同 → 零重叠触发转人工（年度安全网取代旧的年份列判断）
def test_verify_year_mismatch(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "新甲", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "新乙", "ZZXM": "李四", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "新丙", "ZZXM": "王五", "PDNY": "2024"}])]
    _patch_excel_path(monkeypatch, [["作品名称"], ["旧甲"], ["旧乙"], ["旧丙"]])
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          FakeLlm({}), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对" and "零重叠" in rep.notes


# 功能：验证页面/附件都拿不到名单 → "无法核对"转人工，证据链记录访问过程
# 设计：页面 404，断言 verdict 与 evidence 含跳过记录
def test_verify_unverifiable(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loop_mod.tools, "fetch_page",
                        lambda url, timeout=15.0: PageContent(url=url, status=404, text=""))
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/dead"], xwlwhj_spec,
                          FakeLlm({}), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对" and any("404" in e for e in rep.evidence)
    assert rep.source_urls == ["https://x.gov.cn/dead"]  # 检索网址已记录，供人工


# 功能：验证图片名单页在未启用视觉时不调视觉、记录图片 URL 到 found_assets、转人工
# 设计：页面 200 带图片但无正文名单、无 Excel；vision 默认关；断言无法核对且 found_assets 含图片
def test_verify_image_list_without_vision(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loop_mod.config, "load_env", lambda: None)
    monkeypatch.delenv("AWARD_AUDIT_VISION", raising=False)
    page = PageContent(url="https://x.gov.cn/a", status=200, text="",
                       images=["https://x.gov.cn/list1.png", "https://x.gov.cn/list2.png"])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)

    class BoomLlm:
        # 若被调用即失败——用来证明未启用视觉时根本没调 LLM
        def json_call(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            return {}

        def vision_json_call(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("未启用视觉却调用了 vision_json_call")

    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          BoomLlm(), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对"
    assert "https://x.gov.cn/list1.png" in rep.found_assets  # 图片URL已留给人工


# 功能：验证无扩展附件（downFile.do?fileId=xxx）也被下载并按内容识别为 Excel（研创赛真实形态）
# 设计：附件 is_excel=False 但 openpyxl 能解析出网格 → 走 Excel 路径抽取成功
def test_verify_downloads_extensionless_attachment(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    page = PageContent(url="https://x.gov.cn/a", status=200, text="",
                       attachments=[Attachment(text="附件：获奖名单", is_excel=False,
                                               url="https://x.gov.cn/sysFile/downFile.do?fileId=abc")])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda url, d, timeout=30.0, **kw: d / "abc.bin")
    monkeypatch.setattr(loop_mod.tools, "parse_award_excel",
                        lambda p, max_rows=2000: {"sheet": "s", "n_rows": 1, "rows": [["题目"], ["论文甲"]]})
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2024", "title_col": 0}), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对" and rep.source_kind == "excel"
    assert "identity_fields_unverified" in rep.reason_codes
    assert "作者" in rep.notes


# 功能：验证 LLM 抽取抛错（空响应等）时不崩溃，优雅转"无法核对"（复现 opus 空响应崩溃 bug）
# 设计：页面有正文、LLM json_call 抛 LlmError，断言 verify_resource 正常返回而非抛异常
def test_verify_resilient_to_llm_error(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    page = PageContent(url="https://x.gov.cn/a", status=200, text="一些公示正文文字")
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          BoomJsonLlm(), tmp_path)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对"
    assert any("LLM 抽取失败" in e for e in rep.evidence)


# 功能：参考库命中时直接用缓存网格比对，全程不联网（不调 fetch_page）
# 设计：预先入库一份网格，把 fetch_page 换成"一调用即失败"的哨兵；use_corpus=True 下仍得出"一致"，
#       证明既命中库又没走网络；来源标注为参考库的 URL、证据含"参考库命中"
def test_verify_corpus_hit_skips_network(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    corpus.save("04050014", "https://gov/list",
                {"sheet": "s", "n_rows": 2,
                 "rows": [["题目", "作者"], ["论文甲", "张三"]]},
                fetched_at="2026-07-20")

    def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("命中参考库却仍联网 fetch_page")

    monkeypatch.setattr(loop_mod.tools, "fetch_page", _boom)
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://gov/list"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2024", "title_col": 0,
                                   "names_col": 1}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "一致" and rep.source_kind == "excel"
    assert rep.source_url == "https://gov/list"
    assert any("参考库命中" in e for e in rep.evidence)


# 功能：参考库未命中→联网核对成功后，把网格回写入库（下次即可命中）
# 设计：库空，走 mock 的联网 Excel 路径得出"一致"；断言调用后 corpus.has 变 True、证据含"已回写参考库"
def test_verify_corpus_miss_writes_back(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    _patch_excel_path(monkeypatch, [["题目", "作者"], ["论文甲", "张三"]])
    assert corpus.has("04050014") is False
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://x.gov.cn/a"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2024", "title_col": 0,
                                   "names_col": 1}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "一致"
    assert corpus.has("04050014") is True                       # 已回写
    assert any("已回写参考库" in e for e in rep.evidence)


# 功能：多来源网格里，年度分片按提交年度标签选中对年度来源，剔除他年度来源（① 年度分片）
# 设计：参考库存 2024/2025 两个带年度标签的来源，提交年度=2025；断言判"一致"、只算 2025 的 2 条、
#       证据含"剔除"（2024 来源被年度分片剔掉，而非靠重叠猜）
def test_verify_drops_cross_edition_source(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    grid = {"sheet": "多来源", "sheets": ["2024环境赛道", "2025完整名单"], "n_rows": 8,
            "rows": [["【名单：2024环境赛道】"], ["作品名称", "作者"],
                     ["旧作品甲", "旧甲"], ["旧作品乙", "旧乙"],
                     ["【名单：2025完整名单】"], ["作品名称", "作者"],
                     ["论文甲", "张三"], ["论文乙", "李四"]]}
    corpus.save("04050014", "https://gov/list", grid, fetched_at="2026-07-21")

    def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("命中参考库不应联网")

    monkeypatch.setattr(loop_mod.tools, "fetch_page", _boom)
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2025"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2025"}],
        year="2025")]
    rep = verify_resource("04050014", files, ["https://gov/list"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2025", "title_col": 0,
                                   "names_col": 1}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "一致"
    assert rep.extracted_count == 2                          # 只留 2025 来源的 2 条
    assert any("剔除" in e for e in rep.evidence)             # 2024 来源被年度分片剔除


# 功能：年度分片优先级高于重叠——他年度来源即便与提交有部分重叠，也按年度标签剔除，不误报缺漏
# 设计：2024 来源含提交的甲乙(有重叠)外加两条他年度作品，2025 来源恰=提交；提交年度=2025。
#       若只靠重叠安全网，2024 来源因有重叠不会被剔，其额外两条会误报"疑似缺漏"；年度分片则判"一致"
def test_verify_year_shard_beats_overlap(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    grid = {"sheet": "多来源", "sheets": ["2024届", "2025届"], "n_rows": 10,
            "rows": [["【名单：2024届获奖名单】"], ["作品名称", "作者"],
                     ["甲", "旧甲"], ["乙", "旧乙"], ["丙2024", "旧丙"], ["丁2024", "旧丁"],
                     ["【名单：2025届获奖名单】"], ["作品名称", "作者"],
                     ["甲", "张三"], ["乙", "李四"]]}
    corpus.save("04050014", "https://gov/list", grid, fetched_at="2026-07-21")
    monkeypatch.setattr(loop_mod.tools, "fetch_page",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "甲", "ZZXM": "张三", "PDNY": "2025"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "乙", "ZZXM": "李四", "PDNY": "2025"}],
        year="2025")]
    rep = verify_resource("04050014", files, ["https://gov/list"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2025", "title_col": 0,
                                   "names_col": 1}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "一致" and rep.missing == []       # 2024 的丙/丁未误报缺漏
    assert rep.extracted_count == 2                          # 只算 2025 来源


# 功能：各来源都带年度标签但无一含提交年度 → 未取到对应年度名单，转人工（不硬比）
# 设计：来源标签 2023/2024，提交年度 2025；断言"无法核对"且备注点明未取到对应年度名单
def test_verify_no_source_for_submitted_year_to_human(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    grid = {"sheet": "多来源", "sheets": ["2023届", "2024届"], "n_rows": 8,
            "rows": [["【名单：2023届获奖名单】"], ["作品名称"], ["甲"], ["乙"],
                     ["【名单：2024届获奖名单】"], ["作品名称"], ["丙"], ["丁"]]}
    corpus.save("04050014", "https://gov/list", grid, fetched_at="2026-07-21")
    monkeypatch.setattr(loop_mod.tools, "fetch_page",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "戊", "ZZXM": "王五", "PDNY": "2025"}],
        year="2025")]
    rep = verify_resource("04050014", files, ["https://gov/list"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2025", "title_col": 0}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对"
    assert "未取到对应年度名单" in rep.notes


# 功能：全部官网来源都与提交零重叠（均跨届/错采）→ 转人工，绝不硬比出假缺漏
# 设计：两来源都对不上提交，断言"无法核对"且备注点明来源存疑
def test_verify_all_sources_cross_edition_to_human(kit, xwlwhj_spec, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWARD_AUDIT_CORPUS", str(tmp_path / "corpus"))
    from award_audit.core.reference import corpus
    grid = {"sheet": "多来源", "sheets": ["赛道A", "赛道B"], "n_rows": 6,
            "rows": [["【名单：赛道A】"], ["作品名称"], ["甲作品"],
                     ["【名单：赛道B】"], ["作品名称"], ["乙作品"]]}
    corpus.save("04050014", "https://gov/list", grid, fetched_at="2026-07-21")
    monkeypatch.setattr(loop_mod.tools, "fetch_page",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    files = [kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文戊", "ZZXM": "王五", "PDNY": "2024"}])]
    rep = verify_resource("04050014", files, ["https://gov/list"], xwlwhj_spec,
                          FakeLlm({"is_target": True, "year": "2024", "title_col": 0}),
                          tmp_path, use_corpus=True)  # type: ignore[arg-type]
    assert rep.verdict == "无法核对"
    assert "零重叠" in rep.notes
