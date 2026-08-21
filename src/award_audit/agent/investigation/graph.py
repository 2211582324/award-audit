"""A controlled LangGraph investigation loop over the M5 tool whitelist."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from award_audit.agent.toolkit.contracts import ToolBudgetLimits, utc_now
from award_audit.agent.toolkit.registry import (
    SafeToolExecutor,
    ToolExecutionContext,
    ToolRegistry,
)


class InvestigationAction(BaseModel):
    """The only decision an LLM can make at each investigation turn."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool", "compare", "manual"]
    reason: str = Field(min_length=1, max_length=1000)
    tool_name: str = Field(default="", max_length=64)
    prepared_batch_id: str = Field(default="", max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class InvestigationState(TypedDict, total=False):
    case_id: int
    objective: str
    known_urls: list[str]
    asset_index: list[dict[str, Any]]
    memory_hits: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    next_action: dict[str, Any]
    final_status: str
    final_reason: str
    step_count: int
    node_events: list[dict[str, Any]]
    stage_results: dict[str, dict[str, Any]]
    media_batches: list[dict[str, Any]]
    vision_batches_prepared: bool
    expected_record_count: int | None
    forced_followup_ready: bool
    comparison_context: dict[str, Any]


class InvestigationResult(BaseModel):
    """Secret-free graph output that can be persisted and rendered by the review desk."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["compare", "manual", "budget_exhausted", "protocol_error"]
    reason: str
    memory_hits: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    node_events: list[dict[str, Any]] = Field(default_factory=list)
    stage_results: dict[str, dict[str, Any]] = Field(default_factory=dict)


MemoryLookup = Callable[[int], Sequence[Mapping[str, Any]]]
NodeEventSink = Callable[[dict[str, Any]], None]
StageHook = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class InvestigationStageHooks:
    """Case-bound business stages executed by the compiled graph."""

    semantic_route_assets: StageHook
    build_exact_matches_and_candidates: StageHook
    deterministic_verify: StageHook
    persist: StageHook
    semantic_adjudicate_identities: StageHook | None = None

_SYSTEM_PROMPT = """You are the investigation controller for an auditable award-review case.
Choose exactly one next action as JSON. You may use only listed tools and their schemas.
Search hits are leads, never evidence: fetch or download and verify them before comparison.
Do not invent URLs, paths, scope IDs, findings, or tool names. If evidence remains unreadable,
ambiguous, unsafe, or budget is exhausted, choose manual. An asset_index entry with readable=true,
a local_path and SHA-256 is verified M4 evidence and may justify compare. Otherwise choose compare
only when tool observations contain verified, local and readable evidence.
"""


class InvestigationAgent:
    """LangGraph plan -> tool -> observe loop with hard execution boundaries."""

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        *,
        allowed_roots: Sequence[str],
        memory_lookup: MemoryLookup | None = None,
        node_event_sink: NodeEventSink | None = None,
        planner_tool_names: Sequence[str] | None = None,
        limits: ToolBudgetLimits | None = None,
        max_steps: int = 6,
    ) -> None:
        if max_steps < 1 or max_steps > 12:
            raise ValueError("max_steps must be between 1 and 12")
        self._llm = llm
        self._registry = registry
        self._allowed_roots = tuple(allowed_roots)
        self._memory_lookup = memory_lookup
        self._node_event_sink = node_event_sink
        self._planner_tool_names = (
            frozenset(planner_tool_names) if planner_tool_names is not None else None
        )
        self._limits = limits or ToolBudgetLimits()
        self._max_steps = max_steps
        self._executor = SafeToolExecutor(registry)
        self._context: ToolExecutionContext | None = None
        self._stage_hooks: InvestigationStageHooks | None = None
        self._graph = self._build_graph()

    @property
    def persists_node_events(self) -> bool:
        """Whether the caller supplied a durable checkpoint sink for graph events."""

        return self._node_event_sink is not None

    def run(
        self,
        *,
        case_id: int,
        objective: str,
        known_urls: Sequence[str],
        asset_index: Sequence[Mapping[str, Any]] = (),
        stage_hooks: InvestigationStageHooks | None = None,
        expected_record_count: int | None = None,
        comparison_context: Mapping[str, Any] | None = None,
    ) -> InvestigationResult:
        self._context = ToolExecutionContext.create(self._allowed_roots, self._limits)
        self._stage_hooks = stage_hooks
        try:
            indexed_assets = [dict(item) for item in asset_index[:100]]
            final = self._graph.invoke({
                "case_id": case_id,
                "objective": objective[:2000],
                "known_urls": list(dict.fromkeys(str(url) for url in known_urls))[:20],
                "asset_index": indexed_assets,
                "memory_hits": [],
                "observations": [],
                "actions": [],
                "step_count": 0,
                "node_events": [],
                "stage_results": {},
                "media_batches": self._initial_media_batches(indexed_assets),
                "vision_batches_prepared": False,
                "expected_record_count": expected_record_count,
                "forced_followup_ready": False,
                "comparison_context": dict(comparison_context or {}),
            }, config={"recursion_limit": 100})
        finally:
            self._stage_hooks = None
        status = str(final.get("final_status", "manual"))
        if status not in {"compare", "manual", "budget_exhausted", "protocol_error"}:
            status = "protocol_error"
        context = self._context
        trace = [item.model_dump(mode="json") for item in (context.trace if context else [])]
        return InvestigationResult(
            status=status,
            reason=str(final.get("final_reason", "investigation ended without a conclusion")),
            memory_hits=list(final.get("memory_hits", [])),
            actions=list(final.get("actions", [])),
            observations=list(final.get("observations", [])),
            tool_trace=trace,
            node_events=list(final.get("node_events", [])),
            stage_results=dict(final.get("stage_results", {})),
        )

    @staticmethod
    def _initial_media_batches(
        assets: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        references = [
            {
                "path": str(asset.get("local_path", "")),
                "page": int(asset.get("page", 0) or 0),
                "total_pages": int(asset.get("total_pages", 0) or 0),
                "source_url": str(asset.get("source_url", "")),
            }
            for asset in assets
            if str(asset.get("kind", "")).casefold() == "image"
            and bool(asset.get("readable"))
            and not bool(asset.get("parent_roster_complete"))
            and int(asset.get("page", 0) or 0) > 0
            and int(asset.get("total_pages", 0) or 0) > 0
        ]
        references.sort(key=lambda item: int(item["page"]))
        batches = [
            {
                "batch_id": f"ocr:{index // 20 + 1}",
                "tool_name": "ocr_image",
                "arguments": {"images": references[index:index + 20]},
            }
            for index in range(0, len(references), 20)
        ]
        pdf_assets = [
            asset for asset in assets
            if str(asset.get("kind", "")).casefold() == "pdf"
            and bool(asset.get("readable"))
            and str(asset.get("local_path", ""))
            and 0 < int(asset.get("page_count", 0) or 0) <= 300
        ]
        batches.extend({
            "batch_id": f"pdf-text:{index}",
            "tool_name": "extract_pdf_text",
            "arguments": {
                "path": str(asset.get("local_path", "")),
                "pages": list(range(1, int(asset.get("page_count", 0) or 0) + 1)),
                "max_chars_per_page": 20_000,
                "extract_tables": True,
            },
        } for index, asset in enumerate(pdf_assets, start=1))
        return batches

    def _build_graph(self) -> Any:
        graph = StateGraph(InvestigationState)
        graph.add_node("prepare_case", self._prepare_case)
        graph.add_node("retrieve_memory", self._retrieve_memory)
        graph.add_node("semantic_plan", self._plan)
        graph.add_node("execute_tool", self._execute_tool)
        graph.add_node("observe", self._observe)
        graph.add_node("assess_extraction_quality", self._assess_extraction_quality)
        graph.add_node("semantic_route_assets", self._semantic_route_assets)
        graph.add_node("build_exact_matches_and_candidates", self._build_exact_matches)
        graph.add_node("semantic_adjudicate_identities", self._semantic_adjudicate_identities)
        graph.add_node("deterministic_verify", self._deterministic_verify)
        graph.add_node("persist", self._persist)
        graph.add_node("waiting_human", self._waiting_human)
        graph.add_edge(START, "prepare_case")
        graph.add_edge("prepare_case", "retrieve_memory")
        graph.add_edge("retrieve_memory", "semantic_plan")
        graph.add_conditional_edges("semantic_plan", self._after_plan, {
            "execute_tool": "execute_tool",
            "semantic_route_assets": "semantic_route_assets",
            "waiting_human": "waiting_human",
            "end": END,
        })
        graph.add_edge("execute_tool", "observe")
        graph.add_edge("observe", "assess_extraction_quality")
        graph.add_conditional_edges("assess_extraction_quality", self._after_quality, {
            "execute_tool": "execute_tool",
            "semantic_plan": "semantic_plan",
            "waiting_human": "waiting_human",
        })
        graph.add_conditional_edges("semantic_route_assets", self._after_business_stage, {
            "continue": "build_exact_matches_and_candidates",
            "waiting_human": "waiting_human",
        })
        graph.add_conditional_edges(
            "build_exact_matches_and_candidates", self._after_business_stage, {
                "continue": "semantic_adjudicate_identities",
                "waiting_human": "waiting_human",
            },
        )
        graph.add_conditional_edges(
            "semantic_adjudicate_identities", self._after_business_stage, {
                "continue": "deterministic_verify",
                "waiting_human": "waiting_human",
            },
        )
        graph.add_conditional_edges("deterministic_verify", self._after_business_stage, {
            "continue": "persist",
            "waiting_human": "waiting_human",
        })
        graph.add_edge("persist", "waiting_human")
        graph.add_edge("waiting_human", END)
        return graph.compile()

    def _event(
        self,
        state: InvestigationState,
        node: str,
        *,
        transition_reason: str,
        started: float,
    ) -> dict[str, Any]:
        event = {
            "case_id": int(state["case_id"]),
            "node": node,
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "step_count": int(state.get("step_count", 0)),
            "transition_reason": transition_reason[:500],
        }
        if self._node_event_sink is not None:
            self._node_event_sink(event)
        return {"node_events": [*state.get("node_events", []), event]}

    def _prepare_case(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        update: dict[str, Any] = {}
        if not state.get("asset_index") and not state.get("known_urls"):
            update.update({
                "final_status": "manual",
                "final_reason": "case has no known source URLs or verified assets",
            })
        update.update(self._event(
            state,
            "prepare_case",
            transition_reason=(
                "bounded M4 asset index prepared"
                if not update else "no reviewable evidence was indexed"
            ),
            started=started,
        ))
        return update

    def _retrieve_memory(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        if self._memory_lookup is None:
            update = {"memory_hits": []}
            update.update(self._event(
                state, "retrieve_memory", transition_reason="memory lookup unavailable", started=started
            ))
            return update
        try:
            hits = self._memory_lookup(int(state["case_id"]))
        except Exception as exc:
            update = {"memory_hits": [], "observations": [{
                "kind": "memory", "ok": False, "summary": type(exc).__name__,
            }]}
            update.update(self._event(
                state, "retrieve_memory", transition_reason="memory lookup failed safely", started=started
            ))
            return update
        update = {"memory_hits": [dict(hit) for hit in hits[:3]]}
        update.update(self._event(
            state, "retrieve_memory", transition_reason="active memory lookup completed", started=started
        ))
        return update

    def _plan(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        step = int(state.get("step_count", 0))
        if step >= self._max_steps:
            update = {
                "final_status": "budget_exhausted",
                "final_reason": "agent step budget exhausted",
                "next_action": {},
            }
            update.update(self._event(
                state, "semantic_plan", transition_reason="agent step budget exhausted", started=started
            ))
            return update
        payload = {
            "case_id": state["case_id"],
            "objective": state["objective"],
            "known_urls": state.get("known_urls", []),
            "asset_index": state.get("asset_index", [])[:20],
            "asset_inventory": {
                "total": len(state.get("asset_index", [])),
                "by_kind": {
                    kind: sum(
                        1 for asset in state.get("asset_index", [])
                        if str(asset.get("kind", "")) == kind
                    )
                    for kind in sorted({
                        str(asset.get("kind", ""))
                        for asset in state.get("asset_index", [])
                    })
                },
            },
            "memory_hits": state.get("memory_hits", []),
            "observations": self._planner_observations(
                state.get("observations", [])[-8:]
            ),
            "tools": [
                spec.openai_schema()["function"]
                for spec in self._registry.specs()
                if self._planner_tool_names is None or spec.name in self._planner_tool_names
            ],
            "planning_rule": (
                "Prefer compare when a readable, SHA-verified, document_complete M4 asset "
                "already exists. Do not refetch it merely to repeat M4 discovery. When "
                "next_prepared_media_batch is present, execute exactly that tool using its "
                "batch_id and leave arguments empty before considering compare or manual."
            ),
            "next_prepared_media_batch": (
                {
                    "batch_id": state.get("media_batches", [])[0]["batch_id"],
                    "tool_name": state.get("media_batches", [])[0]["tool_name"],
                    "image_count": len(
                        state.get("media_batches", [])[0].get("arguments", {}).get("images", [])
                    ),
                }
                if state.get("media_batches") else None
            ),
            "media_extraction_summary": self._media_extraction_summary(state),
            "compare_handoff_rule": (
                "Choosing compare does not assert a match and does not require the planner "
                "to receive the raw submitted roster. It hands verified extraction state to "
                "the bounded semantic routing, deterministic comparison, verifier, and "
                "persistence nodes, which already hold the full ReviewCasePacket. Choose "
                "compare when the evidence extraction summary reports extraction_complete=true."
            ),
            "required_json": {
                "kind": "tool | compare | manual",
                "reason": "bounded explanation",
                "tool_name": "required when kind is tool",
                "prepared_batch_id": "required for next_prepared_media_batch",
                "arguments": "required when kind is tool",
            },
        }
        planner_user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        def validate_action(raw: Any) -> InvestigationAction:
            action = InvestigationAction.model_validate(raw)
            if action.kind == "tool" and not action.tool_name:
                raise ValueError("tool action omitted tool_name")
            if action.kind == "tool" and (
                self._registry.get(action.tool_name) is None
                or (
                    self._planner_tool_names is not None
                    and action.tool_name not in self._planner_tool_names
                )
            ):
                raise ValueError("tool action named a tool outside the planner whitelist")
            media_batches = state.get("media_batches", [])
            if media_batches:
                expected = media_batches[0]
                if (
                    action.kind != "tool"
                    or action.prepared_batch_id != expected.get("batch_id")
                    or action.tool_name != expected.get("tool_name")
                ):
                    raise ValueError("prepared media batch must be executed in order")
            elif (
                action.kind == "tool"
                and action.tool_name in {
                    "extract_pdf_text", "render_pdf_pages",
                    "ocr_image", "vision_extract_roster",
                }
            ):
                has_complete_document = any(
                    bool(asset.get("readable"))
                    and bool(asset.get("document_complete"))
                    and str(asset.get("kind", "")).casefold() != "image"
                    for asset in state.get("asset_index", [])
                )
                if not has_complete_document:
                    raise ValueError("media tools require a prepared batch")
                action = InvestigationAction(
                    kind="compare",
                    reason=(
                        "Prepared media work is empty and complete bounded M4 document "
                        "evidence is available; continue to governed semantic routing."
                    ),
                )
            return action

        try:
            raw = self._llm.json_call(_SYSTEM_PROMPT, planner_user, max_tokens=1400)
            try:
                action = validate_action(raw)
            except (ValidationError, ValueError, TypeError) as first_exc:
                # Providers occasionally return an almost-correct object. Give the
                # model one bounded correction turn, while retaining all local gates.
                details = str(first_exc)
                correction_prompt = (
                    _SYSTEM_PROMPT
                    + "\nYour previous JSON failed local validation. Return one corrected JSON object only. "
                    + "Use exactly the required_json shape; no extra keys, no markdown, and obey the "
                    + f"prepared media ordering rule. Validation error: {details[:500]}"
                )
                corrected = self._llm.json_call(
                    correction_prompt, planner_user, max_tokens=1400
                )
                action = validate_action(corrected)
        except (ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                errors = [
                    {
                        "location": [str(part) for part in error.get("loc", ())],
                        "type": str(error.get("type", "validation_error")),
                    }
                    for error in exc.errors(include_url=False)
                ][:10]
                detail = json.dumps(errors, ensure_ascii=False, separators=(",", ":"))
            else:
                detail = str(exc)
            update = {
                "final_status": "protocol_error",
                "final_reason": f"agent action protocol invalid: {type(exc).__name__}: {detail[:500]}",
                "next_action": {},
            }
            update.update(self._event(
                state, "semantic_plan", transition_reason="model action protocol invalid", started=started
            ))
            return update
        action_payload = action.model_dump(mode="json")
        update: dict[str, Any] = {
            "next_action": action_payload,
            "actions": [*state.get("actions", []), action_payload],
            "step_count": step + 1,
        }
        if action.kind != "tool":
            update["final_status"] = "compare" if action.kind == "compare" else "manual"
            update["final_reason"] = action.reason
        update.update(self._event(
            state, "semantic_plan", transition_reason=f"model selected {action.kind}", started=started
        ))
        return update

    @staticmethod
    def _media_extraction_summary(state: InvestigationState) -> dict[str, Any]:
        ocr_pages: list[Mapping[str, Any]] = []
        vision_pages: list[Mapping[str, Any]] = []
        vision_errors: list[Mapping[str, Any]] = []
        pdf_text_pages: list[Mapping[str, Any]] = []
        rendered_pdf_pages: list[Mapping[str, Any]] = []
        # Parsed HTML/XLSX/text-PDF assets are complete evidence too. They do
        # not emit OCR/vision observations, so count them directly from the
        # SHA-verified M4 asset index.
        complete_document_count = sum(
            1
            for asset in state.get("asset_index", [])
            if isinstance(asset, Mapping)
            and bool(asset.get("readable"))
            and bool(asset.get("sha256"))
            and bool(asset.get("document_complete"))
            and str(asset.get("kind", "")).casefold()
            not in {"image", "image_collection"}
        )
        complete_web_count = 0
        for observation in state.get("observations", []):
            if not isinstance(observation, Mapping):
                continue
            data = observation.get("summary", {}).get("data", {})
            if not isinstance(data, Mapping):
                continue
            if (
                observation.get("tool_name") in {"fetch_web_page", "extract_search_document"}
                and observation.get("ok")
                and data.get("coverage_complete") is True
                and data.get("award_name_match") is not False
                and data.get("year_match") is not False
            ):
                complete_web_count += 1
            pages = data.get("pages", [])
            if observation.get("tool_name") == "ocr_image" and isinstance(pages, list):
                ocr_pages.extend(page for page in pages if isinstance(page, Mapping))
            elif observation.get("tool_name") == "extract_pdf_text" and isinstance(pages, list):
                pdf_text_pages.extend(page for page in pages if isinstance(page, Mapping))
            elif observation.get("tool_name") == "render_pdf_pages" and isinstance(pages, list):
                rendered_pdf_pages.extend(page for page in pages if isinstance(page, Mapping))
            elif observation.get("tool_name") == "vision_extract_roster":
                if isinstance(pages, list):
                    vision_pages.extend(page for page in pages if isinstance(page, Mapping))
                errors = data.get("errors", [])
                if isinstance(errors, list):
                    vision_errors.extend(error for error in errors if isinstance(error, Mapping))
        candidate_pages = sum(
            1 for page in ocr_pages if len(str(page.get("text", "")).strip()) >= 80
        )
        record_count = sum(
            len(page.get("entries", []))
            for page in vision_pages
            if isinstance(page.get("entries", []), list)
        )
        image_count = sum(
            1 for asset in state.get("asset_index", [])
            if str(asset.get("kind", "")).casefold() == "image"
            and not bool(asset.get("parent_roster_complete"))
        )
        media_page_count = image_count + len(rendered_pdf_pages)
        expected_pdf_pages = sum(
            int(asset.get("page_count", 0) or 0)
            for asset in state.get("asset_index", [])
            if str(asset.get("kind", "")).casefold() == "pdf"
            and bool(asset.get("readable"))
            and str(asset.get("local_path", ""))
        )
        image_extraction_complete = bool(
            state.get("vision_batches_prepared", False)
            and not state.get("media_batches", [])
            and len(ocr_pages) == media_page_count
            and len(vision_pages) == candidate_pages
            and not vision_errors
            and record_count > 0
            and (
                state.get("expected_record_count") is None
                or record_count == int(state["expected_record_count"] or 0)
            )
            and all(
                bool(page.get("all_rows_extracted", True))
                and not bool(page.get("truncated", False))
                and not page.get("unreadable", [])
                and float(page.get("confidence", 0) or 0) >= 0.85
                and int(page.get("visible_row_count", len(page.get("entries", []))) or 0)
                == len(page.get("entries", []))
                for page in vision_pages
            )
        )
        pdf_text_complete = bool(
            expected_pdf_pages > 0
            and not state.get("media_batches", [])
            and len(pdf_text_pages) == expected_pdf_pages
            and all(
                int(page.get("text_chars", 0) or 0) >= 40
                and not bool(page.get("is_truncated", False))
                for page in pdf_text_pages
            )
        )
        document_extraction_complete = complete_document_count > 0 or complete_web_count > 0
        extraction_complete = (
            document_extraction_complete
            or image_extraction_complete
            or pdf_text_complete
        )
        return {
            "complete_document_count": complete_document_count,
            "complete_web_count": complete_web_count,
            "document_extraction_complete": document_extraction_complete,
            "image_count": image_count,
            "rendered_pdf_page_count": len(rendered_pdf_pages),
            "ocr_page_count": len(ocr_pages),
            "vision_candidate_page_count": candidate_pages,
            "vision_page_count": len(vision_pages),
            "vision_record_count": record_count,
            "expected_record_count": state.get("expected_record_count"),
            "record_count_matches_expected": (
                state.get("expected_record_count") is None
                or record_count == int(state["expected_record_count"] or 0)
            ),
            "vision_error_count": len(vision_errors),
            "pdf_expected_page_count": expected_pdf_pages,
            "pdf_text_page_count": len(pdf_text_pages),
            "pdf_text_complete": pdf_text_complete,
            "pending_batch_count": len(state.get("media_batches", [])),
            "extraction_complete": extraction_complete,
        }

    @staticmethod
    def _planner_observations(
        observations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep full extraction in state while bounding the next planner prompt."""

        summaries: list[dict[str, Any]] = []
        for observation in observations:
            raw_summary = observation.get("summary", {})
            data = raw_summary.get("data", {}) if isinstance(raw_summary, Mapping) else {}
            pages = data.get("pages", []) if isinstance(data, Mapping) else []
            errors = data.get("errors", []) if isinstance(data, Mapping) else []
            summaries.append({
                "kind": observation.get("kind", ""),
                "tool_name": observation.get("tool_name", ""),
                "prepared_batch_id": observation.get("prepared_batch_id", ""),
                "ok": bool(observation.get("ok")),
                "error_code": observation.get("error_code", ""),
                "page_count": len(pages) if isinstance(pages, list) else 0,
                "error_count": len(errors) if isinstance(errors, list) else 0,
                "complete": data.get("complete") if isinstance(data, Mapping) else None,
                "warnings": (
                    list(raw_summary.get("warnings", []))[:10]
                    if isinstance(raw_summary, Mapping) else []
                ),
            })
        return summaries

    @staticmethod
    def _after_plan(
        state: InvestigationState,
    ) -> Literal["execute_tool", "semantic_route_assets", "waiting_human", "end"]:
        status = str(state.get("final_status", ""))
        if status in {"budget_exhausted", "protocol_error"}:
            return "end"
        if status == "manual":
            return "waiting_human"
        action = state.get("next_action", {})
        if action.get("kind") == "tool":
            return "execute_tool"
        if action.get("kind") == "compare":
            return "semantic_route_assets"
        return "waiting_human"

    def _execute_tool(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        context = self._context
        if context is None:
            update = {"final_status": "protocol_error", "final_reason": "missing tool context"}
            update.update(self._event(
                state, "execute_tool", transition_reason="tool context missing", started=started
            ))
            return update
        action = state.get("next_action", {})
        arguments = dict(action.get("arguments", {}))
        tool_name = str(action.get("tool_name", ""))
        remaining_batches = list(state.get("media_batches", []))
        prepared_batch_id = str(action.get("prepared_batch_id", ""))
        if prepared_batch_id:
            if not remaining_batches or remaining_batches[0].get("batch_id") != prepared_batch_id:
                update = {
                    "final_status": "protocol_error",
                    "final_reason": "prepared media batch is unavailable or out of order",
                }
                update.update(self._event(
                    state, "execute_tool", transition_reason="prepared batch mismatch", started=started
                ))
                return update
            prepared = remaining_batches.pop(0)
            arguments = dict(prepared.get("arguments", {}))
        elif tool_name in {"fetch_web_page", "extract_search_document"}:
            url = str(arguments.get("url", "")).strip()
            if url:
                arguments = self._search_followup_arguments(
                    state,
                    tool_name=tool_name,
                    url=url,
                    source_arguments=arguments,
                )
        result = self._executor.execute(
            tool_name,
            arguments,
            context,
        )
        observation = {
            "kind": "tool",
            "tool_name": tool_name,
            "prepared_batch_id": prepared_batch_id,
            "ok": result.ok,
            "error_code": result.error_code,
            "summary": {
                "source_url": result.source_url,
                "local_path": result.local_path,
                "sha256": result.sha256,
                "data": result.data,
                "warnings": result.warnings,
                "artifact_count": len(result.artifacts),
            },
        }
        update = {
            "observations": [*state.get("observations", []), observation],
            "media_batches": remaining_batches,
            "forced_followup_ready": False,
        }
        update.update(self._event(
            state,
            "execute_tool",
            transition_reason=("tool observation recorded" if result.ok else f"tool failed: {result.error_code}"),
            started=started,
        ))
        return update

    def _observe(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        observation = state.get("observations", [])[-1] if state.get("observations") else {}
        if observation.get("error_code") in {
            "TOOL_INPUT_INVALID", "TOOL_NOT_REGISTERED", "TOOL_OUTPUT_INVALID"
        }:
            update = {
                "final_status": "protocol_error",
                "final_reason": "tool action violated the registered tool protocol",
            }
            update.update(self._event(
                state,
                "assess_extraction_quality",
                transition_reason="invalid tool protocol stopped fail-closed",
                started=started,
            ))
            return update
        return self._event(
            state,
            "observe",
            transition_reason=(
                "tool output is available to the planner"
                if observation.get("ok") else "tool failure is available to the planner"
            ),
            started=started,
        )

    def _assess_extraction_quality(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        observation = state.get("observations", [])[-1] if state.get("observations") else {}
        if not observation.get("ok") and int(state.get("step_count", 0)) >= self._max_steps:
            update = {
                "final_status": "manual",
                "final_reason": "tool failed and the investigation budget is exhausted",
            }
            update.update(self._event(
                state, "assess_extraction_quality", transition_reason="failed extraction requires human review", started=started
            ))
            return update
        update: dict[str, Any] = {}
        remaining = list(state.get("media_batches", []))
        if observation.get("tool_name") == "extract_pdf_text" and observation.get("ok"):
            summary = observation.get("summary", {})
            data = summary.get("data", {}) if isinstance(summary, Mapping) else {}
            pages = data.get("pages", []) if isinstance(data, Mapping) else []
            scan_pages = [
                int(page.get("page", 0) or 0)
                for page in pages
                if isinstance(page, Mapping)
                and int(page.get("page", 0) or 0) > 0
                and int(page.get("text_chars", 0) or 0) < 40
            ]
            if scan_pages:
                pdf_path = str(summary.get("local_path", ""))
                pdf_sha = str(summary.get("sha256", ""))
                output_dir = str(
                    Path(self._allowed_roots[-1])
                    / "langgraph-pdf-pages"
                    / pdf_sha[:16]
                )
                remaining.append({
                    "batch_id": f"pdf-render:{pdf_sha[:12]}",
                    "tool_name": "render_pdf_pages",
                    "arguments": {
                        "path": pdf_path,
                        "pages": scan_pages,
                        "output_dir": output_dir,
                        "dpi": 150,
                        "source_url": str(summary.get("source_url", "")),
                    },
                })
                update["media_batches"] = remaining
        elif observation.get("tool_name") == "render_pdf_pages" and observation.get("ok"):
            summary = observation.get("summary", {})
            data = summary.get("data", {}) if isinstance(summary, Mapping) else {}
            pages = [
                page for page in data.get("pages", [])
                if isinstance(page, Mapping) and str(page.get("path", ""))
            ] if isinstance(data, Mapping) else []
            pdf_sha = str(summary.get("sha256", ""))
            total_pages = max(
                [int(page.get("page", 0) or 0) for page in pages], default=0
            )
            matching_asset = next((
                asset for asset in state.get("asset_index", [])
                if str(asset.get("sha256", "")) == pdf_sha
            ), {})
            total_pages = int(matching_asset.get("page_count", 0) or total_pages)
            references = [{
                "path": str(page.get("path", "")),
                "page": int(page.get("page", 0) or 0),
                "total_pages": total_pages,
                "source_url": str(summary.get("source_url", "")),
            } for page in pages]
            remaining.extend({
                "batch_id": f"pdf-ocr:{pdf_sha[:12]}:{index // 20 + 1}",
                "tool_name": "ocr_image",
                "arguments": {"images": references[index:index + 20]},
            } for index in range(0, len(references), 20))
            update["media_batches"] = remaining
        if (
            observation.get("tool_name") == "ocr_image"
            and not remaining
            and not state.get("vision_batches_prepared", False)
        ):
            pages = [
                page
                for item in state.get("observations", [])
                if item.get("tool_name") == "ocr_image" and item.get("ok")
                for page in item.get("summary", {}).get("data", {}).get("pages", [])
                if isinstance(page, Mapping)
            ]
            candidate_hashes = {
                str(page.get("image_sha256", ""))
                for page in pages
                if len(str(page.get("text", "")).strip()) >= 80
                and str(page.get("image_sha256", ""))
            }
            candidate_pages = {
                int(page.get("page", 0) or 0)
                for page in pages
                if len(str(page.get("text", "")).strip()) >= 80
            }
            ocr_by_path = {
                str(page.get("path", "")): page
                for page in pages
                if str(page.get("image_sha256", "")) in candidate_hashes
                and str(page.get("path", ""))
            }
            ocr_by_page = {
                int(page.get("page", 0) or 0): page
                for page in pages
                if int(page.get("page", 0) or 0) in candidate_pages
            }
            native_references = [
                {
                    "path": str(asset.get("local_path", "")),
                    "page": int(asset.get("page", 0) or 0),
                    "total_pages": int(asset.get("total_pages", 0) or 0),
                    "source_url": str(asset.get("source_url", "")),
                }
                for asset in state.get("asset_index", [])
                if str(asset.get("sha256", "")) in candidate_hashes
                or int(asset.get("page", 0) or 0) in candidate_pages
            ]
            reference_groups: list[tuple[str, list[dict[str, Any]]]] = []
            if native_references:
                native_references.sort(key=lambda item: int(item["page"]))
                reference_groups.append(("images", native_references))
            for item in state.get("observations", []):
                if item.get("tool_name") != "render_pdf_pages" or not item.get("ok"):
                    continue
                item_summary = item.get("summary", {})
                item_data = item_summary.get("data", {}) if isinstance(item_summary, Mapping) else {}
                pdf_sha = str(item_summary.get("sha256", ""))
                matching_asset = next((
                    asset for asset in state.get("asset_index", [])
                    if str(asset.get("sha256", "")) == pdf_sha
                ), {})
                total_pages = int(matching_asset.get("page_count", 0) or 0)
                references = [{
                    "path": str(page.get("path", "")),
                    "page": int(page.get("page", 0) or 0),
                    "total_pages": total_pages,
                    "source_url": str(item_summary.get("source_url", "")),
                } for page in item_data.get("pages", [])
                    if isinstance(page, Mapping)
                    and str(page.get("sha256", "")) in candidate_hashes]
                if references:
                    references.sort(key=lambda value: int(value["page"]))
                    reference_groups.append((f"pdf-{pdf_sha[:12]}", references))
            remaining = []
            for group_key, references in reference_groups:
                for index in range(0, len(references), 20):
                    batch_references = references[index:index + 20]
                    remaining.append({
                    "batch_id": (
                        f"vision:{index // 20 + 1}"
                        if group_key == "images"
                        else f"vision:{group_key}:{index // 20 + 1}"
                    ),
                    "tool_name": "vision_extract_roster",
                    "arguments": {
                        "images": batch_references,
                        "ocr_text_by_page": {
                            int(reference["page"]): str(
                                ocr_by_path.get(
                                    reference["path"],
                                    ocr_by_page.get(int(reference["page"]), {}),
                                ).get("text", "")
                            )[:8000]
                            for reference in batch_references
                        },
                    },
                })
            update.update({
                "media_batches": remaining,
                "vision_batches_prepared": True,
            })
        effective_remaining = list(update.get("media_batches", remaining))
        current_tool = str(observation.get("tool_name", ""))
        # A search lead is a candidate, not evidence. If the direct page
        # fetch is blocked or times out, let AnySearch extract the same lead
        # before asking the planner to repeat the original dead URL.
        failed_fetch_action = state.get("next_action", {})
        failed_fetch_url = ""
        failed_fetch_arguments: Mapping[str, Any] = {}
        if (
            current_tool == "fetch_web_page"
            and not observation.get("ok")
            and isinstance(failed_fetch_action, Mapping)
        ):
            raw_arguments = failed_fetch_action.get("arguments", {})
            if isinstance(raw_arguments, Mapping):
                failed_fetch_arguments = raw_arguments
                failed_fetch_url = str(raw_arguments.get("url", "")).strip()
        already_extracted = any(
            isinstance(item, Mapping)
            and item.get("tool_name") == "extract_search_document"
            and isinstance(item.get("arguments"), Mapping)
            and str(item["arguments"].get("url", "")).rstrip("/")
            == failed_fetch_url.rstrip("/")
            for item in state.get("actions", [])
        )
        if (
            failed_fetch_url
            and not already_extracted
            and self._registry.get("extract_search_document") is not None
        ):
            fallback_arguments = self._search_followup_arguments(
                state,
                tool_name="extract_search_document",
                url=failed_fetch_url,
                source_arguments=failed_fetch_arguments,
            )
            fallback = InvestigationAction(
                kind="tool",
                reason="direct search lead fetch failed; extract the lead through AnySearch",
                tool_name="extract_search_document",
                arguments=fallback_arguments,
            ).model_dump(mode="json")
            action = fallback
            update.update({
                "next_action": action,
                "actions": [*state.get("actions", []), action],
                "forced_followup_ready": True,
            })
            transition_reason = "failed search lead queued for AnySearch extraction"
        else:
            candidate_observation = observation
            summary = observation.get("summary", {})
            data = summary.get("data", {}) if isinstance(summary, Mapping) else {}
            # A readable candidate is still only a lead when its roster does
            # not cover the submitted scope. Continue the bounded search
            # result list deterministically instead of letting the planner
            # stop early based on an incomplete secondary page.
            if (
                current_tool in {"fetch_web_page", "extract_search_document"}
                and isinstance(data, Mapping)
                and data.get("coverage_complete") is not True
            ):
                candidate_observation = next((
                    item
                    for item in reversed(state.get("observations", []))
                    if isinstance(item, Mapping)
                    and item.get("tool_name") == "search_official_award"
                    and item.get("ok")
                ), observation)
            forced_followup = self._search_candidate_followup(
                state, candidate_observation
            )
            if forced_followup is not None and not effective_remaining:
                action = forced_followup.model_dump(mode="json")
                update.update({
                    "next_action": action,
                    "actions": [*state.get("actions", []), action],
                    "forced_followup_ready": True,
                })
                transition_reason = "unvisited search candidate queued for deterministic fetch"
            elif (
                effective_remaining
                and str(effective_remaining[0].get("tool_name", "")) == current_tool
            ):
                prepared = effective_remaining[0]
                action = InvestigationAction(
                    kind="tool",
                    reason="continue the locally prepared batch for the approved tool stage",
                    tool_name=current_tool,
                    prepared_batch_id=str(prepared.get("batch_id", "")),
                    arguments={},
                ).model_dump(mode="json")
                update.update({
                    "media_batches": effective_remaining,
                    "next_action": action,
                    "actions": [*state.get("actions", []), action],
                })
                transition_reason = "next prepared batch continues without replanning"
            else:
                if "media_batches" not in update and observation.get("tool_name") in {
                    "ocr_image", "vision_extract_roster", "extract_pdf_text", "render_pdf_pages"
                }:
                    update["media_batches"] = effective_remaining
                transition_reason = "tool stage completed or changed; planner must reassess"
        update.update(self._event(
            state,
            "assess_extraction_quality",
            transition_reason=transition_reason,
            started=started,
        ))
        return update

    @staticmethod
    def _search_followup_arguments(
        state: InvestigationState,
        *,
        tool_name: str,
        url: str,
        source_arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Carry comparison context across deterministic search follow-up edges."""

        fetch_fields = {
            "expected_award_name",
            "award_aliases",
            "official_domains",
            "official_secondary_domains",
            "section_keywords",
            "section_exclude_keywords",
            "expected_year",
            "submitted_path",
            "submitted_paths",
            "match_fields",
            "match_combine",
            "expected_scope_count",
            "page_total_count",
            "relationship_terms",
        }
        extract_fields = fetch_fields - {
            "official_domains",
            "official_secondary_domains",
            "section_exclude_keywords",
        }
        allowed = extract_fields | {"search_query"} if tool_name == "extract_search_document" else fetch_fields
        context: dict[str, Any] = {}

        trusted_context = state.get("comparison_context", {})
        if isinstance(trusted_context, Mapping):
            for field in allowed:
                value = trusted_context.get(field)
                if value not in (None, "", []):
                    context[field] = value

        argument_sources: list[Mapping[str, Any]] = []
        if source_arguments is not None:
            argument_sources.append(source_arguments)
        for action in reversed(state.get("actions", [])):
            if not isinstance(action, Mapping):
                continue
            arguments = action.get("arguments", {})
            if isinstance(arguments, Mapping):
                argument_sources.append(arguments)

        for arguments in argument_sources:
            for field in allowed:
                value = arguments.get(field)
                if field not in context and value not in (None, "", []):
                    context[field] = value
            if "expected_award_name" in allowed and "expected_award_name" not in context:
                award_name = arguments.get("award_name")
                if award_name:
                    context["expected_award_name"] = award_name
            if "expected_year" in allowed and "expected_year" not in context:
                year = arguments.get("year")
                if year:
                    context["expected_year"] = year
            if "official_domains" in allowed and "official_domains" not in context:
                domains = arguments.get("official_domains") or arguments.get("site_domains")
                if domains:
                    context["official_domains"] = domains
            if tool_name == "extract_search_document" and "search_query" not in context:
                query = arguments.get("search_query") or arguments.get("query")
                if query:
                    context["search_query"] = query

        if tool_name == "extract_search_document" and "search_query" not in context:
            for observation in reversed(state.get("observations", [])):
                if not isinstance(observation, Mapping):
                    continue
                data = observation.get("summary", {}).get("data", {})
                if isinstance(data, Mapping) and str(data.get("query", "")).strip():
                    context["search_query"] = str(data["query"]).strip()[:100]
                    break
        if tool_name == "extract_search_document" and "search_query" not in context:
            parts = [
                str(context.get("expected_award_name", "")).strip(),
                str(context.get("expected_year", "")).strip(),
                *[
                    str(item).strip()
                    for item in context.get("section_keywords", [])[:2]
                    if str(item).strip()
                ],
            ]
            query = " ".join(part for part in parts if part)
            if query:
                context["search_query"] = query[:100]

        return {"url": url, **context}

    def _search_candidate_followup(
        self,
        state: InvestigationState,
        observation: Mapping[str, Any],
    ) -> InvestigationAction | None:
        """Fetch one new bounded search lead before asking the planner again."""

        if observation.get("tool_name") != "search_official_award" or not observation.get("ok"):
            return None
        data = observation.get("summary", {}).get("data", {})
        candidates = data.get("candidates", []) if isinstance(data, Mapping) else []
        if not isinstance(candidates, list):
            return None
        attempted = {
            str(action.get("arguments", {}).get("url", "")).rstrip("/")
            for action in state.get("actions", [])
            if isinstance(action, Mapping)
            and isinstance(action.get("arguments", {}), Mapping)
            and str(action.get("arguments", {}).get("url", "")).strip()
        }
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            url = str(candidate.get("url", "")).strip()
            if not url.startswith(("http://", "https://")) or url.rstrip("/") in attempted:
                continue
            suffix = Path(urlsplit(url).path).suffix.casefold()
            preferred = (
                "download_evidence"
                if suffix in {
                    ".pdf", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".webp",
                    ".tif", ".tiff",
                }
                else "fetch_web_page"
            )
            tool_name = preferred if self._registry.get(preferred) is not None else ""
            if not tool_name:
                fallback = (
                    "fetch_web_page"
                    if preferred == "download_evidence"
                    else "download_evidence"
                )
                tool_name = fallback if self._registry.get(fallback) is not None else ""
            if not tool_name:
                return None
            arguments = {"url": url}
            if tool_name == "fetch_web_page":
                arguments = self._search_followup_arguments(
                    state,
                    tool_name=tool_name,
                    url=url,
                )
            return InvestigationAction(
                kind="tool",
                reason="visit the first unattempted bounded search lead before replanning",
                tool_name=tool_name,
                arguments=arguments,
            )
        return None

    @staticmethod
    def _after_quality(
        state: InvestigationState,
    ) -> Literal["execute_tool", "semantic_plan", "waiting_human"]:
        if state.get("final_status"):
            return "waiting_human"
        action = state.get("next_action", {})
        remaining = state.get("media_batches", [])
        if (
            action.get("kind") == "tool"
            and remaining
            and action.get("prepared_batch_id") == remaining[0].get("batch_id")
            and action.get("tool_name") == remaining[0].get("tool_name")
        ):
            return "execute_tool"
        if action.get("kind") == "tool" and state.get("forced_followup_ready", False):
            return "execute_tool"
        return "semantic_plan"

    def _semantic_route_assets(self, state: InvestigationState) -> dict[str, Any]:
        return self._run_business_stage(state, "semantic_route_assets")

    def _build_exact_matches(self, state: InvestigationState) -> dict[str, Any]:
        return self._run_business_stage(state, "build_exact_matches_and_candidates")

    def _deterministic_verify(self, state: InvestigationState) -> dict[str, Any]:
        return self._run_business_stage(state, "deterministic_verify")

    def _semantic_adjudicate_identities(
        self, state: InvestigationState
    ) -> dict[str, Any]:
        return self._run_business_stage(state, "semantic_adjudicate_identities")

    def _persist(self, state: InvestigationState) -> dict[str, Any]:
        return self._run_business_stage(state, "persist")

    def _run_business_stage(
        self, state: InvestigationState, stage: str
    ) -> dict[str, Any]:
        started = time.monotonic()
        hooks = self._stage_hooks
        hook = getattr(hooks, stage, None) if hooks is not None else None
        if hook is None:
            result: dict[str, Any] = {
                "ok": True,
                "transition_reason": f"{stage} handoff completed",
            }
        else:
            try:
                result = dict(hook(state))
            except Exception as exc:  # fail closed without exposing case content
                result = {
                    "ok": False,
                    "transition_reason": f"{stage} failed safely: {type(exc).__name__}",
                }
        update: dict[str, Any] = {
            "stage_results": {**state.get("stage_results", {}), stage: result},
        }
        if result.get("ok") is not True:
            update.update({
                "final_status": "manual",
                "final_reason": str(
                    result.get("transition_reason", f"{stage} requires human review")
                )[:1000],
            })
        update.update(self._event(
            state,
            stage,
            transition_reason=str(result.get("transition_reason", stage)),
            started=started,
        ))
        return update

    @staticmethod
    def _after_business_stage(
        state: InvestigationState,
    ) -> Literal["continue", "waiting_human"]:
        return "waiting_human" if state.get("final_status") == "manual" else "continue"

    def _waiting_human(self, state: InvestigationState) -> dict[str, Any]:
        started = time.monotonic()
        return self._event(
            state, "waiting_human", transition_reason="automatic review remains gated by human review", started=started
        )
