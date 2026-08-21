"""Governed candidate creation, lifecycle and Top-3 Case Memory retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from award_audit.agent.memory.models import CaseMemory, MemoryHit, TaxonomyEntry
from award_audit.core.pipeline.store import StateConflictError, Store

if TYPE_CHECKING:
    from award_audit.agent.harness.models import AuditCaseState

_TOKEN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_DATE = re.compile(r"^$|^\d{4}-\d{2}-\d{2}$")
_SOURCE_VERSION_REASONS = {"year_mismatch", "cross_year_dropped", "non_final_source"}
_TRIGGER_CATEGORY = {
    "SOURCE_URL_MISSING": "SOURCE_DISCOVERY",
    "SOURCE_UNREACHABLE": "SOURCE_DISCOVERY",
    "PAGE_TARGET_UNCERTAIN": "SOURCE_DISCOVERY",
    "PDF_ONLY": "DOCUMENT_EXTRACTION",
    "IMAGE_ONLY": "DOCUMENT_EXTRACTION",
    "COLUMN_AMBIGUOUS": "FIELD_SEMANTICS",
    "SOFT_RULE_SUSPECT": "FIELD_SEMANTICS",
    "ZERO_OVERLAP": "COVERAGE_PATTERN",
    "COVERAGE_UNKNOWN": "COVERAGE_PATTERN",
    "EVIDENCE_CONFLICT": "EVIDENCE_CONFLICT",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_pattern(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"\d+", "<n>", normalized)
    return " ".join(normalized.split()).strip()[:1000]


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for token in _TOKEN.findall(normalize_pattern(text)):
        if "\u4e00" <= token[0] <= "\u9fff" and len(token) > 1:
            result.update(token[index:index + 2] for index in range(len(token) - 1))
        else:
            result.add(token)
    return result


class MemoryRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def taxonomy(self, code: str, version: int = 1) -> TaxonomyEntry | None:
        row = self.store.conn.execute(
            "SELECT * FROM error_taxonomy WHERE code=? AND version=?",
            (code, version),
        ).fetchone()
        if row is None:
            return None
        return TaxonomyEntry.model_validate({
            "code": str(row["code"]),
            "version": int(row["version"]),
            "name": str(row["name"]),
            "definition": str(row["definition"]),
            "examples": json.loads(row["examples_json"]),
            "candidate_eligible": bool(row["candidate_eligible"]),
            "status": str(row["status"]),
        })

    def list_taxonomy(self) -> list[TaxonomyEntry]:
        rows = self.store.conn.execute(
            "SELECT * FROM error_taxonomy ORDER BY version, code"
        ).fetchall()
        entries = [
            self.taxonomy(str(row["code"]), int(row["version"])) for row in rows
        ]
        return [entry for entry in entries if entry is not None]

    def _memory(self, memory_id: int) -> CaseMemory:
        row = self.store.conn.execute(
            "SELECT * FROM case_memory WHERE id=?", (memory_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"case memory not found: {memory_id}")
        sources = [
            int(item["case_id"])
            for item in self.store.conn.execute(
                "SELECT case_id FROM case_memory_source WHERE memory_id=? ORDER BY case_id",
                (memory_id,),
            ).fetchall()
        ]
        return CaseMemory.model_validate({
            "memory_id": int(row["id"]),
            "status": str(row["status"]),
            "category_code": str(row["category_code"]),
            "taxonomy_version": int(row["taxonomy_version"]),
            "resource_type": str(row["resource_type"]),
            "field_code": str(row["field_code"]),
            "symptom_text": str(row["symptom_text"]),
            "normalized_pattern": str(row["normalized_pattern"]),
            "resolution": str(row["resolution"]),
            "evidence_summary": str(row["evidence_summary"]),
            "final_human_decision": str(row["final_human_decision"]),
            "source_case_id": int(row["source_case_id"]),
            "source_case_ids": sources,
            "applicable_from": str(row["applicable_from"]),
            "applicable_to": str(row["applicable_to"]),
            "occurrence_count": int(row["occurrence_count"]),
            "fingerprint": str(row["fingerprint"]),
            "created_by": str(row["created_by"]),
            "approved_by": str(row["approved_by"]),
            "merged_into_id": (
                int(row["merged_into_id"]) if row["merged_into_id"] is not None else None
            ),
            "state_version": int(row["state_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        })

    def get(self, memory_id: int) -> CaseMemory:
        return self._memory(memory_id)

    def upsert_candidate(
        self,
        payload: dict[str, Any],
        *,
        source_case_id: int,
    ) -> tuple[CaseMemory, bool]:
        case = self.store.conn.execute(
            "SELECT status,human_decision,human_decision_summary,reviewed_by,reviewed_at "
            "FROM audit_case WHERE id=?",
            (source_case_id,),
        ).fetchone()
        if case is None or str(case["status"]) != "completed":
            raise ValueError("candidate memory requires a completed source case")
        if (
            str(case["human_decision"]) not in {"accepted", "rejected"}
            or not str(case["reviewed_by"])
            or not str(case["reviewed_at"])
        ):
            raise ValueError("candidate memory requires a conclusive human review")
        taxonomy = self.taxonomy(
            str(payload["category_code"]), int(payload.get("taxonomy_version", 1))
        )
        if taxonomy is None or taxonomy.status != "active" or not taxonomy.candidate_eligible:
            raise ValueError("taxonomy category is not eligible for automatic candidate memory")
        fingerprint = str(payload["fingerprint"])
        existing = self.store.conn.execute(
            "SELECT id FROM case_memory WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        now = _now()
        with self.store.conn:
            if existing is not None:
                memory_id = int(existing["id"])
                linked = self.store.conn.execute(
                    "INSERT OR IGNORE INTO case_memory_source(memory_id,case_id,linked_at) "
                    "VALUES (?,?,?)",
                    (memory_id, source_case_id, now),
                )
                if linked.rowcount == 1:
                    self.store.conn.execute(
                        "UPDATE case_memory SET occurrence_count=occurrence_count+1,"
                        "updated_at=? WHERE id=?",
                        (now, memory_id),
                    )
                return self._memory(memory_id), False
            cursor = self.store.conn.execute(
                "INSERT INTO case_memory("
                "status,category_code,taxonomy_version,resource_type,field_code,"
                "symptom_text,normalized_pattern,resolution,evidence_summary,"
                "final_human_decision,source_case_id,applicable_from,applicable_to,"
                "occurrence_count,fingerprint,created_by,approved_by,merged_into_id,"
                "state_version,created_at,updated_at)"
                " VALUES ('candidate',?,?,?,?,?,?,?,?,?,?,?,?,1,?,?, '',NULL,1,?,?)",
                (
                    str(payload["category_code"]),
                    int(payload.get("taxonomy_version", 1)),
                    str(payload.get("resource_type", "")),
                    str(payload.get("field_code", "")),
                    str(payload["symptom_text"]),
                    str(payload["normalized_pattern"]),
                    str(payload["resolution"]),
                    str(payload.get("evidence_summary", "")),
                    str(case["human_decision"]),
                    source_case_id,
                    str(payload.get("applicable_from", "")),
                    str(payload.get("applicable_to", "")),
                    fingerprint,
                    str(case["reviewed_by"]),
                    now,
                    now,
                ),
            )
            memory_id = int(cursor.lastrowid or 0)
            self.store.conn.execute(
                "INSERT INTO case_memory_source(memory_id,case_id,linked_at) VALUES (?,?,?)",
                (memory_id, source_case_id, now),
            )
        return self._memory(memory_id), True

    def transition(
        self,
        memory_id: int,
        status: str,
        reviewer: str,
        *,
        expected_version: int,
        merged_into_id: int | None = None,
    ) -> CaseMemory:
        reviewer = " ".join(reviewer.split()).strip()
        if not reviewer or len(reviewer) > 200:
            raise ValueError("reviewer must contain 1-200 characters")
        current = self._memory(memory_id)
        if current.state_version != expected_version:
            raise StateConflictError(
                f"case memory {memory_id} state version changed from {expected_version}"
            )
        allowed = {
            "candidate": {"active", "deprecated", "merged"},
            "active": {"deprecated", "merged"},
            "deprecated": set(),
            "merged": set(),
        }
        if status not in allowed[current.status]:
            raise ValueError(f"invalid memory transition: {current.status} -> {status}")
        if status == "merged":
            if merged_into_id is None or merged_into_id == memory_id:
                raise ValueError("merged memory requires a different active target")
            target = self._memory(merged_into_id)
            if target.status != "active":
                raise ValueError("merge target must be active")
        elif merged_into_id is not None:
            raise ValueError("merged_into_id is only valid for merged status")
        new_version = expected_version + 1
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE case_memory SET status=?,approved_by=?,merged_into_id=?,"
                "state_version=?,updated_at=? WHERE id=? AND state_version=?",
                (
                    status,
                    reviewer if status == "active" else current.approved_by,
                    merged_into_id,
                    new_version,
                    _now(),
                    memory_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"case memory {memory_id} state version changed from {expected_version}"
                )
        return self._memory(memory_id)

    def active(self) -> list[CaseMemory]:
        rows = self.store.conn.execute(
            "SELECT id FROM case_memory WHERE status='active' ORDER BY id"
        ).fetchall()
        return [self._memory(int(row["id"])) for row in rows]

    def list(self, *, status: str = "", limit: int = 200) -> list[CaseMemory]:
        if status and status not in {"candidate", "active", "deprecated", "merged"}:
            raise ValueError("invalid memory status")
        if status:
            rows = self.store.conn.execute(
                "SELECT id FROM case_memory WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT id FROM case_memory ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._memory(int(row["id"])) for row in rows]


class CaseMemoryService:
    def __init__(self, store: Store) -> None:
        self.repository = MemoryRepository(store)

    @staticmethod
    def category_for_case(state: AuditCaseState) -> str:
        if set(state.reason_codes).intersection(_SOURCE_VERSION_REASONS):
            return "SOURCE_VERSION"
        for trigger in state.trigger_codes:
            if trigger in _TRIGGER_CATEGORY:
                return _TRIGGER_CATEGORY[trigger]
        return "OTHER"

    def propose_from_case(
        self,
        state: AuditCaseState,
        *,
        resolution: str = "",
        symptom_text: str = "",
        resource_type: str = "",
        field_code: str = "",
        category_code: str = "",
        applicable_from: str = "",
        applicable_to: str = "",
    ) -> CaseMemory | None:
        if state.status != "completed" or state.human_decision == "insufficient":
            return None
        category = category_code or self.category_for_case(state)
        taxonomy = self.repository.taxonomy(category)
        if taxonomy is None or not taxonomy.candidate_eligible:
            return None
        resolution = " ".join(
            (resolution or state.human_decision_summary).split()
        ).strip()[:2000]
        if not resolution:
            return None
        for value in (applicable_from, applicable_to):
            if not _DATE.fullmatch(value):
                raise ValueError("applicable dates must use YYYY-MM-DD")
        if applicable_from and applicable_to and applicable_from > applicable_to:
            raise ValueError("applicable_from cannot be after applicable_to")
        summary = state.submitted_summary
        resource_type = (resource_type or str(summary.get("resource_type", ""))).strip()[:80]
        field_code = (field_code or str(summary.get("field_code", ""))).strip()[:80]
        symptom = " ".join(
            (
                symptom_text
                or " ".join(state.open_questions)
                or f"{' '.join(state.trigger_codes)} {state.objective}"
            ).split()
        ).strip()[:2000]
        normalized = normalize_pattern(symptom)
        if not normalized:
            return None
        fingerprint_input = "|".join(
            [category, resource_type.casefold(), field_code.casefold(), normalized]
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        memory, _created = self.repository.upsert_candidate(
            {
                "category_code": category,
                "taxonomy_version": taxonomy.version,
                "resource_type": resource_type,
                "field_code": field_code,
                "symptom_text": symptom,
                "normalized_pattern": normalized,
                "resolution": resolution,
                "evidence_summary": state.recommendation[:2000],
                "applicable_from": applicable_from,
                "applicable_to": applicable_to,
                "fingerprint": fingerprint,
            },
            source_case_id=state.case_id,
        )
        return memory

    def retrieve_for_case(
        self,
        state: AuditCaseState,
        *,
        limit: int = 3,
        on_date: date | None = None,
    ) -> list[MemoryHit]:
        limit = max(1, min(limit, 3))
        current_date = (on_date or date.today()).isoformat()
        resource_type = str(state.submitted_summary.get("resource_type", "")).strip()
        field_code = str(state.submitted_summary.get("field_code", "")).strip()
        primary_category = self.category_for_case(state)
        relevant_categories = {primary_category, "STANDARD_CORRECTION"}
        query_text = " ".join(
            [state.objective, *state.open_questions, *state.reason_codes]
        )
        query_tokens = _tokens(query_text)
        ranked: list[tuple[float, CaseMemory]] = []
        for memory in self.repository.active():
            if memory.applicable_from and memory.applicable_from > current_date:
                continue
            if memory.applicable_to and memory.applicable_to < current_date:
                continue
            if memory.resource_type and memory.resource_type != resource_type:
                continue
            if memory.field_code and memory.field_code != field_code:
                continue
            if primary_category != "OTHER" and memory.category_code not in relevant_categories:
                continue
            memory_tokens = _tokens(f"{memory.symptom_text} {memory.resolution}")
            overlap = len(query_tokens.intersection(memory_tokens))
            score = 0.0
            if memory.category_code == primary_category:
                score += 3.0
            if memory.resource_type and memory.resource_type == resource_type:
                score += 2.0
            if memory.field_code and memory.field_code == field_code:
                score += 2.0
            if query_tokens:
                score += 4.0 * overlap / len(query_tokens)
            score += min(memory.occurrence_count, 10) / 20
            if score > 0:
                ranked.append((score, memory))
        ranked.sort(key=lambda item: (-item[0], item[1].memory_id))
        return [
            MemoryHit(
                memory_id=memory.memory_id,
                category_code=memory.category_code,
                symptom_text=memory.symptom_text,
                resolution=memory.resolution,
                final_human_decision=memory.final_human_decision,
                applicable_from=memory.applicable_from,
                applicable_to=memory.applicable_to,
                source_case_ids=memory.source_case_ids,
                score=round(score, 4),
            )
            for score, memory in ranked[:limit]
        ]
