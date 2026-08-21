from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from award_audit.agent.investigation import InvestigationAgent
from award_audit.agent.investigation.graph import InvestigationState
from award_audit.agent.toolkit.contracts import ToolResult, ToolSpec
from award_audit.agent.toolkit.registry import ToolRegistry


class FetchInput(BaseModel):
    url: str


class ImageBatchInput(BaseModel):
    images: list[dict[str, object]]


class PdfTextInput(BaseModel):
    path: str
    pages: list[int]
    max_chars_per_page: int
    extract_tables: bool


class PdfRenderInput(BaseModel):
    path: str
    pages: list[int]
    output_dir: str
    dpi: int
    source_url: str = ""


class EmptyInput(BaseModel):
    pass


class ContextFetchInput(BaseModel):
    url: str
    expected_award_name: str = ""
    expected_year: str = ""
    official_domains: list[str] = []
    submitted_path: Path | None = None
    submitted_paths: list[Path] = []
    match_fields: list[str] = []
    expected_scope_count: int | None = None


class ScriptedLlm:
    def __init__(self) -> None:
        self.calls = 0

    def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
        self.calls += 1
        if self.calls == 1:
            return {
                "kind": "tool",
                "reason": "read the registered official page",
                "tool_name": "fetch_page",
                "arguments": {"url": "https://official.example/list"},
            }
        return {"kind": "compare", "reason": "verified local evidence is available"}


def test_langgraph_agent_plans_executes_observes_and_stops(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="fetch_page",
            description="fetch an allowlisted page",
            input_model=FetchInput,
        ),
        lambda args, _context: ToolResult(
            ok=True,
            source_url=args.url,
            data={"observed_year": "2025", "coverage_complete": True},
        ),
    )
    persisted_events: list[dict[str, object]] = []
    agent = InvestigationAgent(
        ScriptedLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        memory_lookup=lambda _case_id: [{"memory_id": 7, "resolution": "verify source"}],
        node_event_sink=persisted_events.append,
    )

    result = agent.run(
        case_id=12,
        objective="verify the official roster",
        known_urls=["https://official.example/list"],
    )

    assert result.status == "compare"
    assert result.memory_hits == [{"memory_id": 7, "resolution": "verify source"}]
    assert [item["kind"] for item in result.actions] == ["tool", "compare"]
    assert result.observations[0]["tool_name"] == "fetch_page"
    assert result.tool_trace[0]["tool_name"] == "fetch_page"
    assert agent.persists_node_events is True
    assert [event["node"] for event in persisted_events] == [
        "prepare_case",
        "retrieve_memory",
        "semantic_plan",
        "execute_tool",
        "observe",
        "assess_extraction_quality",
        "semantic_plan",
            "semantic_route_assets",
            "build_exact_matches_and_candidates",
            "semantic_adjudicate_identities",
            "deterministic_verify",
        "persist",
        "waiting_human",
    ]


def test_search_candidate_is_fetched_before_the_planner_runs_again(
    tmp_path: Path,
) -> None:
    class SearchThenManualLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            self.calls += 1
            if self.calls == 1:
                return {
                    "kind": "tool",
                    "reason": "search for the corrected official URL",
                    "tool_name": "search_official_award",
                    "arguments": {},
                }
            return {"kind": "manual", "reason": "bounded candidate was checked"}

    candidate_url = "https://official.example/corrected-list"
    fetched: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_official_award",
            description="search",
            input_model=EmptyInput,
            kind="search",
        ),
        lambda _args, _context: ToolResult(ok=True, data={
            "candidates": [{"url": candidate_url}],
        }),
    )
    registry.register(
        ToolSpec(
            name="fetch_web_page",
            description="fetch",
            input_model=FetchInput,
        ),
        lambda args, _context: (
            fetched.append(args.url) or ToolResult(ok=True, source_url=args.url)
        ),
    )

    result = InvestigationAgent(
        SearchThenManualLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        planner_tool_names=("search_official_award", "fetch_web_page"),
    ).run(
        case_id=13,
        objective="recover an official page",
        known_urls=["https://official.example/obsolete-list"],
    )

    assert fetched == [candidate_url]
    assert [action["tool_name"] for action in result.actions] == [
        "search_official_award", "fetch_web_page", "",
    ]


def test_incomplete_fetched_candidate_forces_next_search_candidate(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="fetch_web_page", description="fetch", input_model=FetchInput),
        lambda args, _context: ToolResult(ok=True, source_url=args.url),
    )
    agent = InvestigationAgent(
        ScriptedLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
    )
    first_url = "https://secondary.example/incomplete-list"
    second_url = "https://official.example/complete-list"
    search_observation = {
        "tool_name": "search_official_award",
        "ok": True,
        "summary": {"data": {"candidates": [
            {"url": first_url},
            {"url": second_url},
        ]}},
    }
    state: InvestigationState = {
        "case_id": 14,
        "step_count": 2,
        "actions": [
            {
                "kind": "tool",
                "tool_name": "search_official_award",
                "arguments": {},
            },
            {
                "kind": "tool",
                "tool_name": "fetch_web_page",
                "arguments": {"url": first_url},
            },
        ],
        "observations": [
            search_observation,
            {
                "tool_name": "fetch_web_page",
                "ok": True,
                "summary": {"data": {"coverage_complete": False}},
            },
        ],
        "media_batches": [],
        "next_action": {
            "kind": "tool",
            "tool_name": "fetch_web_page",
            "arguments": {"url": first_url},
        },
    }

    update = agent._assess_extraction_quality(state)

    assert update["forced_followup_ready"] is True
    assert update["next_action"]["kind"] == "tool"
    assert update["next_action"]["tool_name"] == "fetch_web_page"
    assert update["next_action"]["arguments"]["url"] == second_url


def test_search_candidate_followup_preserves_comparison_context(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="fetch_web_page", description="fetch", input_model=ContextFetchInput),
        lambda _args, _context: ToolResult(ok=True),
    )
    agent = InvestigationAgent(
        ScriptedLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
    )
    submitted = tmp_path / "submitted.xlsx"
    state: InvestigationState = {
        "actions": [
            {
                "kind": "tool",
                "tool_name": "fetch_web_page",
                "arguments": {
                    "url": "https://obsolete.example/list",
                    "submitted_path": str(submitted),
                    "submitted_paths": [str(submitted)],
                    "match_fields": ["TDRYMC"],
                    "expected_scope_count": 190,
                },
            },
            {
                "kind": "tool",
                "tool_name": "search_official_award",
                "arguments": {
                    "award_name": "全国高校黄大年式教师团队",
                    "year": "2025",
                    "official_domains": ["moe.gov.cn"],
                },
            },
        ],
    }

    action = agent._search_candidate_followup(state, {
        "tool_name": "search_official_award",
        "ok": True,
        "summary": {"data": {"candidates": [{
            "url": "https://www.cernet.edu.cn/result.html",
        }]}},
    })

    assert action is not None
    assert action.arguments == {
        "url": "https://www.cernet.edu.cn/result.html",
        "expected_award_name": "全国高校黄大年式教师团队",
        "expected_year": "2025",
        "official_domains": ["moe.gov.cn"],
        "submitted_path": str(submitted),
        "submitted_paths": [str(submitted)],
        "match_fields": ["TDRYMC"],
        "expected_scope_count": 190,
    }


def test_search_extract_fallback_preserves_context_and_query(tmp_path: Path) -> None:
    submitted = tmp_path / "submitted.xlsx"
    state: InvestigationState = {
        "actions": [{
            "kind": "tool",
            "tool_name": "search_official_award",
            "arguments": {
                "award_name": "全国高校黄大年式教师团队",
                "year": "2025",
                "query": "黄大年 教师团队 2025 认定名单",
                "submitted_paths": [str(submitted)],
                "match_fields": ["TDRYMC"],
                "expected_scope_count": 190,
            },
        }],
    }

    arguments = InvestigationAgent._search_followup_arguments(
        state,
        tool_name="extract_search_document",
        url="https://www.edu.cn/result.html",
    )

    assert arguments["expected_award_name"] == "全国高校黄大年式教师团队"
    assert arguments["expected_year"] == "2025"
    assert arguments["search_query"] == "黄大年 教师团队 2025 认定名单"
    assert arguments["submitted_paths"] == [str(submitted)]
    assert arguments["match_fields"] == ["TDRYMC"]
    assert arguments["expected_scope_count"] == 190


def test_search_followup_uses_trusted_comparison_context(tmp_path: Path) -> None:
    submitted = tmp_path / "submitted.xlsx"
    state: InvestigationState = {
        "comparison_context": {
            "expected_award_name": "全国高校黄大年式教师团队",
            "expected_year": "2025",
            "submitted_paths": [str(submitted)],
            "match_fields": ["TDMC", "XDWMC"],
            "match_combine": "all",
            "expected_scope_count": 190,
        },
        "observations": [{
            "tool_name": "search_official_award",
            "ok": True,
            "summary": {"data": {"query": "全国高校黄大年式教师团队 2025 名单"}},
        }],
    }

    arguments = InvestigationAgent._search_followup_arguments(
        state,
        tool_name="extract_search_document",
        url="https://www.cernet.edu.cn/result.html",
        source_arguments={"match_fields": ["name", "org"]},
    )

    assert arguments["submitted_paths"] == [str(submitted)]
    assert arguments["match_fields"] == ["TDMC", "XDWMC"]
    assert arguments["match_combine"] == "all"
    assert arguments["search_query"] == "全国高校黄大年式教师团队 2025 名单"


def test_complete_html_and_xlsx_assets_count_as_extraction_complete() -> None:
    state: InvestigationState = {
        "asset_index": [
            {
                "kind": "html",
                "readable": True,
                "sha256": "a" * 64,
                "document_complete": True,
            },
            {
                "kind": "xlsx",
                "readable": True,
                "sha256": "b" * 64,
                "document_complete": True,
            },
        ],
        "media_batches": [],
    }

    summary = InvestigationAgent._media_extraction_summary(state)

    assert summary["complete_document_count"] == 2
    assert summary["document_extraction_complete"] is True
    assert summary["extraction_complete"] is True


def test_incomplete_document_does_not_count_as_extraction_complete() -> None:
    state: InvestigationState = {
        "asset_index": [
            {
                "kind": "html",
                "readable": True,
                "sha256": "a" * 64,
                "document_complete": False,
            },
        ],
        "media_batches": [],
    }

    summary = InvestigationAgent._media_extraction_summary(state)

    assert summary["complete_document_count"] == 0
    assert summary["extraction_complete"] is False


def test_complete_verified_web_extraction_counts_as_extraction_complete() -> None:
    state: InvestigationState = {
        "asset_index": [],
        "media_batches": [],
        "observations": [{
            "tool_name": "extract_search_document",
            "ok": True,
            "summary": {"data": {
                "coverage_complete": True,
                "award_name_match": True,
                "year_match": True,
            }},
        }],
    }

    summary = InvestigationAgent._media_extraction_summary(state)

    assert summary["complete_web_count"] == 1
    assert summary["document_extraction_complete"] is True
    assert summary["extraction_complete"] is True


def test_langgraph_agent_stops_on_invalid_model_action(tmp_path: Path) -> None:
    class InvalidLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            self.calls += 1
            return {"kind": "tool", "reason": "missing name"}

    llm = InvalidLlm()
    result = InvestigationAgent(
        llm, ToolRegistry(), allowed_roots=[str(tmp_path)]
    ).run(case_id=1, objective="test", known_urls=[])

    assert result.status == "protocol_error"
    assert llm.calls == 2
    assert "tool action omitted tool_name" in result.reason


def test_langgraph_agent_corrects_one_invalid_model_action(tmp_path: Path) -> None:
    class CorrectingLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            self.calls += 1
            if self.calls == 1:
                return {"kind": "compare", "reason": "ready", "unexpected": True}
            return {"kind": "compare", "reason": "verified evidence is ready"}

    llm = CorrectingLlm()
    result = InvestigationAgent(
        llm, ToolRegistry(), allowed_roots=[str(tmp_path)]
    ).run(case_id=1, objective="test", known_urls=[])

    assert result.status == "compare"
    assert llm.calls == 2
    assert result.actions == [{
        "kind": "compare",
        "reason": "verified evidence is ready",
        "tool_name": "",
        "prepared_batch_id": "",
        "arguments": {},
    }]


def test_langgraph_rejects_tool_outside_planner_whitelist(tmp_path: Path) -> None:
    class LegacyToolLlm:
        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            return {
                "kind": "tool",
                "reason": "use a legacy composite tool",
                "tool_name": "legacy_composite",
                "arguments": {},
            }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="legacy_composite",
            description="legacy",
            input_model=FetchInput,
        ),
        lambda _args, _context: ToolResult(ok=True),
    )
    result = InvestigationAgent(
        LegacyToolLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        planner_tool_names=("fetch_web_page",),
    ).run(case_id=1, objective="test", known_urls=[])

    assert result.status == "protocol_error"
    assert result.tool_trace == []


def test_langgraph_does_not_run_unprepared_media_over_complete_html(
    tmp_path: Path,
) -> None:
    class DirectVisionLlm:
        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            return {
                "kind": "tool",
                "reason": "inspect two discovered page images",
                "tool_name": "vision_extract_roster",
                "arguments": {"images": [
                    {"path": str(tmp_path / "old.jpg"), "page": 1, "total_pages": 1},
                    {"path": str(tmp_path / "current.jpg"), "page": 1, "total_pages": 1},
                ]},
            }

    called = False

    def vision(_args: ImageBatchInput, _context: object) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult(ok=True, data={"pages": []})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="vision_extract_roster",
            description="test vision",
            input_model=ImageBatchInput,
        ),
        vision,
    )
    result = InvestigationAgent(
        DirectVisionLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        planner_tool_names=("vision_extract_roster",),
    ).run(
        case_id=2,
        objective="use complete HTML evidence",
        known_urls=["https://official.example/list"],
        asset_index=[
            {
                "asset_id": "sha256:" + "a" * 64,
                "kind": "html",
                "local_path": str(tmp_path / "roster.txt"),
                "sha256": "a" * 64,
                "readable": True,
                "document_complete": True,
            },
            {
                "asset_id": "sha256:" + "b" * 64,
                "kind": "image",
                "local_path": str(tmp_path / "old.jpg"),
                "sha256": "b" * 64,
                "readable": True,
                "document_complete": True,
                "parent_roster_complete": True,
                "page": 1,
                "total_pages": 1,
            },
        ],
    )

    assert result.status == "compare"
    assert result.actions[0]["kind"] == "compare"
    assert called is False


def test_langgraph_schedules_ocr_then_only_roster_sized_vision_batches(
    tmp_path: Path,
) -> None:
    class MediaLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            self.calls += 1
            if self.calls == 1:
                return {
                    "kind": "tool",
                    "reason": "run the prepared local OCR batch",
                    "tool_name": "ocr_image",
                    "prepared_batch_id": "ocr:1",
                    "arguments": {},
                }
            if self.calls == 2:
                return {
                    "kind": "tool",
                    "reason": "structure the roster-like OCR pages",
                    "tool_name": "vision_extract_roster",
                    "prepared_batch_id": "vision:1",
                    "arguments": {},
                }
            return {"kind": "compare", "reason": "media extraction is complete"}

    seen_batches: list[tuple[str, list[int]]] = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="ocr_image",
            description="test OCR batch",
            input_model=ImageBatchInput,
        ),
        lambda args, _context: (
            seen_batches.append((
                "ocr",
                [int(item["page"]) for item in args.images],
            ))
            or ToolResult(ok=True, data={"pages": [
                {
                    "page": int(item["page"]),
                    "text": "roster row " * 10 if int(item["page"]) < 4 else "logo",
                }
                for item in args.images
            ]})
        ),
    )
    registry.register(
        ToolSpec(
            name="vision_extract_roster",
            description="test vision batch",
            input_model=ImageBatchInput,
        ),
        lambda args, _context: (
            seen_batches.append((
                "vision",
                [int(item["page"]) for item in args.images],
            ))
            or ToolResult(ok=True, data={"pages": []})
        ),
    )
    assets = [
        {
            "asset_id": f"sha256:{page:064x}",
            "kind": "image",
            "source_url": f"https://official.example/{page}.png",
            "local_path": str(tmp_path / f"{page}.png"),
            "sha256": f"{page:064x}",
            "readable": True,
            "page": page,
            "total_pages": 4,
        }
        for page in range(1, 5)
    ]
    result = InvestigationAgent(
        MediaLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        planner_tool_names=("ocr_image", "vision_extract_roster"),
    ).run(
        case_id=2,
        objective="extract image roster",
        known_urls=["https://official.example/list"],
        asset_index=assets,
    )

    assert result.status == "compare"
    assert seen_batches == [("ocr", [1, 2, 3, 4]), ("vision", [1, 2, 3])]
    assert [item["prepared_batch_id"] for item in result.actions[:2]] == [
        "ocr:1",
        "vision:1",
    ]


def test_langgraph_schedules_verified_pdf_text_before_compare(tmp_path: Path) -> None:
    observed_max_chars: list[int] = []

    class PdfLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            self.calls += 1
            if self.calls == 1:
                return {
                    "kind": "tool",
                    "reason": "extract the prepared PDF pages",
                    "tool_name": "extract_pdf_text",
                    "prepared_batch_id": "pdf-text:1",
                    "arguments": {},
                }
            return {"kind": "compare", "reason": "all PDF pages are readable"}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="extract_pdf_text",
            description="extract bounded PDF pages",
            input_model=PdfTextInput,
        ),
        lambda args, _context: (
            observed_max_chars.append(args.max_chars_per_page),
            ToolResult(ok=True, data={"pages": [
                {"page": page, "text": "roster row " * 10, "text_chars": 100,
                 "is_truncated": False}
                for page in args.pages
            ]}),
        )[1],
    )
    result = InvestigationAgent(
        PdfLlm(),
        registry,
        allowed_roots=[str(tmp_path)],
        planner_tool_names=("extract_pdf_text",),
    ).run(
        case_id=3,
        objective="extract PDF roster",
        known_urls=["https://official.example/roster.pdf"],
        asset_index=[{
            "asset_id": "sha256:" + "a" * 64,
            "kind": "pdf",
            "source_url": "https://official.example/roster.pdf",
            "local_path": str(tmp_path / "roster.pdf"),
            "sha256": "a" * 64,
            "readable": True,
            "page_count": 2,
        }],
    )

    assert result.status == "compare"
    assert result.actions[0]["prepared_batch_id"] == "pdf-text:1"
    assert observed_max_chars == [20_000]
    assert result.observations[0]["tool_name"] == "extract_pdf_text"


def test_langgraph_schedules_scanned_pdf_render_ocr_and_vision(tmp_path: Path) -> None:
    pdf_sha = "a" * 64
    image_hashes = ["b" * 64, "c" * 64]
    batch_ids = [
        "pdf-text:1",
        f"pdf-render:{pdf_sha[:12]}",
        f"pdf-ocr:{pdf_sha[:12]}:1",
        f"vision:pdf-{pdf_sha[:12]}:1",
    ]

    class ScanLlm:
        def __init__(self) -> None:
            self.calls = 0

        def json_call(self, _system: str, _user: str, *, max_tokens: int) -> object:
            if self.calls < len(batch_ids):
                batch_id = batch_ids[self.calls]
                self.calls += 1
                return {
                    "kind": "tool",
                    "reason": "execute the prepared scan fallback batch",
                    "tool_name": {
                        "pdf-text": "extract_pdf_text",
                        "pdf-render": "render_pdf_pages",
                        "pdf-ocr": "ocr_image",
                        "vision": "vision_extract_roster",
                    }[batch_id.split(":", 1)[0]],
                    "prepared_batch_id": batch_id,
                    "arguments": {},
                }
            return {"kind": "compare", "reason": "scan fallback is complete"}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="extract_pdf_text", description="pdf text", input_model=PdfTextInput),
        lambda args, _context: ToolResult(
            ok=True,
            local_path=args.path,
            sha256=pdf_sha,
            source_url="https://official.example/scan.pdf",
            data={"pages": [
                {"page": page, "text": "", "text_chars": 0, "is_truncated": False}
                for page in args.pages
            ]},
        ),
    )
    registry.register(
        ToolSpec(name="render_pdf_pages", description="render", input_model=PdfRenderInput),
        lambda args, _context: ToolResult(
            ok=True,
            sha256=pdf_sha,
            source_url=args.source_url,
            data={"pages": [
                {"page": page, "path": str(tmp_path / f"scan-{page}.png"),
                 "sha256": image_hashes[page - 1]}
                for page in args.pages
            ]},
        ),
    )
    registry.register(
        ToolSpec(name="ocr_image", description="ocr", input_model=ImageBatchInput),
        lambda args, _context: ToolResult(ok=True, data={"pages": [
            {"page": int(image["page"]), "path": str(image["path"]),
             "text": "roster row " * 10, "image_sha256": image_hashes[int(image["page"]) - 1]}
            for image in args.images
        ]}),
    )
    registry.register(
        ToolSpec(name="vision_extract_roster", description="vision", input_model=ImageBatchInput),
        lambda args, _context: ToolResult(ok=True, data={"pages": [
            {"page": int(image["page"]), "image_sha256": image_hashes[int(image["page"]) - 1],
             "entries": [{"no": number, "name": f"item-{number}"} for number in range(
                 (int(image["page"]) - 1) * 8 + 1, int(image["page"]) * 8 + 1
             )], "confidence": 0.99, "visible_row_count": 8,
             "all_rows_extracted": True, "truncated": False, "unreadable": []}
            for image in args.images
        ], "errors": []}),
    )
    result = InvestigationAgent(
        ScanLlm(), registry, allowed_roots=[str(tmp_path)],
        planner_tool_names=(
            "extract_pdf_text", "render_pdf_pages", "ocr_image", "vision_extract_roster"
        ),
    ).run(
        case_id=4,
        objective="extract scanned PDF roster",
        known_urls=["https://official.example/scan.pdf"],
        expected_record_count=16,
        asset_index=[{
            "asset_id": "sha256:" + pdf_sha,
            "kind": "pdf",
            "source_url": "https://official.example/scan.pdf",
            "local_path": str(tmp_path / "scan.pdf"),
            "sha256": pdf_sha,
            "readable": True,
            "page_count": 2,
        }],
    )

    assert result.status == "compare"
    assert [action["prepared_batch_id"] for action in result.actions[:4]] == batch_ids
    assert [observation["tool_name"] for observation in result.observations] == [
        "extract_pdf_text", "render_pdf_pages", "ocr_image", "vision_extract_roster"
    ]
