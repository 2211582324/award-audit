"""SQLite-backed single-worker queue with leases and persistent events."""

from __future__ import annotations

import asyncio
import builtins
import inspect
import json
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from award_audit.core.pipeline.store import StateConflictError, Store
from award_audit.web.models import AuditEvent, HumanAction, JobOutcome, JobRecord

_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|password|secret|signature|token)"
    r"([\s=:]+)[^\s,;&]+"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1\2[REDACTED]", text)[:500]


class JobRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _job_from_row(row: Any) -> JobRecord:
        return JobRecord.model_validate({
            "job_id": int(row["id"]),
            "kind": str(row["kind"]),
            "batch_id": int(row["batch_id"]) if row["batch_id"] is not None else None,
            "case_id": int(row["case_id"]) if row["case_id"] is not None else None,
            "status": str(row["status"]),
            "input": json.loads(row["input_json"]),
            "progress": int(row["progress"]),
            "progress_message": str(row["progress_message"]),
            "result": json.loads(row["result_json"]),
            "error_code": str(row["error_code"]),
            "error_message": str(row["error_message"]),
            "attempt": int(row["attempt"]),
            "max_attempts": int(row["max_attempts"]),
            "lease_owner": str(row["lease_owner"]),
            "lease_expires_at": str(row["lease_expires_at"]),
            "state_version": int(row["state_version"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
        })

    def _append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        topic: str = "global",
        job_id: int | None = None,
        case_id: int | None = None,
        batch_id: int | None = None,
    ) -> int:
        cursor = self.store.conn.execute(
            "INSERT INTO audit_event(event_type,topic,job_id,case_id,batch_id,"
            "payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                event_type,
                topic[:100],
                job_id,
                case_id,
                batch_id,
                json.dumps(payload, ensure_ascii=False),
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        **links: Any,
    ) -> int:
        with self.store.conn:
            return self._append_event(event_type, payload, **links)

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        created_by: str,
        batch_id: int | None = None,
        case_id: int | None = None,
        max_attempts: int = 1,
    ) -> JobRecord:
        kind = " ".join(kind.split()).strip()
        created_by = " ".join(created_by.split()).strip()
        if not kind or len(kind) > 80:
            raise ValueError("job kind must contain 1-80 characters")
        if not created_by or len(created_by) > 200:
            raise ValueError("created_by must contain 1-200 characters")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be in 1-10")
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > 20_000:
            raise ValueError("job input exceeds 20000 characters")
        now = _now()
        with self.store.conn:
            cursor = self.store.conn.execute(
                "INSERT INTO audit_job(kind,batch_id,case_id,status,input_json,"
                "max_attempts,created_by,created_at,updated_at)"
                " VALUES (?,?,?,'queued',?,?,?,?,?)",
                (kind, batch_id, case_id, encoded, max_attempts, created_by, now, now),
            )
            job_id = int(cursor.lastrowid or 0)
            self._append_event(
                "job.queued",
                {"job_id": job_id, "kind": kind, "status": "queued"},
                topic="jobs",
                job_id=job_id,
                case_id=case_id,
                batch_id=batch_id,
            )
        return self.get(job_id)

    def enqueue_once(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        created_by: str,
        batch_id: int,
        case_id: int | None = None,
        max_attempts: int = 1,
    ) -> JobRecord:
        """原子入队：撞活跃唯一索引（uq_active_job / uq_active_batch_stage）则返回既有活跃 job。

        防重不靠"先查再插"（有 TOCTOU 竞态），靠部分唯一索引在 INSERT 时原子拦截；
        两个并发请求只会入一个活跃 job。
        """
        try:
            return self.enqueue(
                kind, payload, created_by=created_by, batch_id=batch_id,
                case_id=case_id, max_attempts=max_attempts,
            )
        except sqlite3.IntegrityError:
            if kind in {"audit_batch", "review_batch"}:
                row = self.store.conn.execute(
                    "SELECT * FROM audit_job WHERE batch_id=? "
                    "AND kind IN ('audit_batch','review_batch') "
                    "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                    (batch_id,),
                ).fetchone()
            else:
                row = self.store.conn.execute(
                    "SELECT * FROM audit_job WHERE batch_id=? AND kind=? "
                    "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                    (batch_id, kind),
                ).fetchone()
            if row is None:
                raise
            return self._job_from_row(row)

    def get(self, job_id: int) -> JobRecord:
        row = self.store.conn.execute(
            "SELECT * FROM audit_job WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"audit job not found: {job_id}")
        return self._job_from_row(row)

    def list(self, *, limit: int = 100) -> list[JobRecord]:
        rows = self.store.conn.execute(
            "SELECT * FROM audit_job ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def recover_expired(self) -> int:
        now = _now()
        with self.store.conn:
            rows = self.store.conn.execute(
                "SELECT id,attempt,max_attempts,case_id,batch_id FROM audit_job "
                "WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<?",
                (now,),
            ).fetchall()
            for row in rows:
                exhausted = int(row["attempt"]) >= int(row["max_attempts"])
                status = "failed" if exhausted else "queued"
                self.store.conn.execute(
                    "UPDATE audit_job SET status=?,lease_owner='',lease_expires_at='',"
                    "error_code=?,error_message=?,state_version=state_version+1,updated_at=?,"
                    "finished_at=CASE WHEN ?='failed' THEN ? ELSE '' END WHERE id=?",
                    (
                        status,
                        "JOB_LEASE_EXPIRED" if exhausted else "",
                        "worker lease expired" if exhausted else "",
                        now,
                        status,
                        now,
                        int(row["id"]),
                    ),
                )
                self._append_event(
                    f"job.{status}",
                    {"job_id": int(row["id"]), "status": status, "recovered": True},
                    topic="jobs",
                    job_id=int(row["id"]),
                    case_id=(int(row["case_id"]) if row["case_id"] is not None else None),
                    batch_id=(int(row["batch_id"]) if row["batch_id"] is not None else None),
                )
        return len(rows)

    def claim_next(self, worker_id: str, *, lease_seconds: int = 30) -> JobRecord | None:
        if not worker_id or not 5 <= lease_seconds <= 600:
            raise ValueError("invalid worker lease")
        now = _now()
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds")
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.conn.execute(
                "SELECT * FROM audit_job WHERE status='queued' AND attempt<max_attempts "
                "ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                self.store.conn.commit()
                return None
            job_id = int(row["id"])
            updated = self.store.conn.execute(
                "UPDATE audit_job SET status='running',attempt=attempt+1,lease_owner=?,"
                "lease_expires_at=?,progress=CASE WHEN progress=0 THEN 1 ELSE progress END,"
                "state_version=state_version+1,updated_at=?,"
                "started_at=CASE WHEN started_at='' THEN ? ELSE started_at END "
                "WHERE id=? AND status='queued'",
                (worker_id, expires, now, now, job_id),
            )
            if updated.rowcount != 1:
                self.store.conn.rollback()
                return None
            claimed = self.get(job_id)
            self._append_event(
                "job.running",
                {"job_id": job_id, "status": "running", "attempt": claimed.attempt},
                topic="jobs",
                job_id=job_id,
                case_id=claimed.case_id,
                batch_id=claimed.batch_id,
            )
            self.store.conn.commit()
            return claimed
        except Exception:
            self.store.conn.rollback()
            raise

    def heartbeat(self, job_id: int, worker_id: str, *, lease_seconds: int = 30) -> bool:
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE audit_job SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (expires, _now(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def progress(self, job_id: int, worker_id: str, percent: int, message: str) -> JobRecord:
        percent = max(1, min(percent, 99))
        message = " ".join(message.split()).strip()[:500]
        now = _now()
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE audit_job SET progress=?,progress_message=?,state_version=state_version+1,"
                "updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
                (percent, message, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("job progress lost its worker lease")
            job = self.get(job_id)
            self._append_event(
                "job.progress",
                {"job_id": job_id, "progress": percent, "message": message},
                topic="jobs",
                job_id=job_id,
                case_id=job.case_id,
                batch_id=job.batch_id,
            )
        return self.get(job_id)

    def finish(self, job_id: int, worker_id: str, outcome: JobOutcome) -> JobRecord:
        encoded = json.dumps(outcome.result, ensure_ascii=False)
        if len(encoded) > 50_000:
            encoded = json.dumps({"is_truncated": True}, ensure_ascii=False)
        now = _now()
        progress = 100 if outcome.status == "completed" else 99
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE audit_job SET status=?,progress=?,result_json=?,error_code='',"
                "error_message='',lease_owner='',lease_expires_at='',"
                "state_version=state_version+1,updated_at=?,finished_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (outcome.status, progress, encoded, now, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("job completion lost its worker lease")
            job = self.get(job_id)
            self._append_event(
                f"job.{outcome.status}",
                {"job_id": job_id, "status": outcome.status, "progress": progress},
                topic="jobs",
                job_id=job_id,
                case_id=job.case_id,
                batch_id=job.batch_id,
            )
        return self.get(job_id)

    def cancel(self, job_id: int, *, expected_version: int) -> JobRecord:
        """Cancel work that is not executing; running handlers are never hard-killed."""

        now = _now()
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE audit_job SET status='cancelled',lease_owner='',lease_expires_at='',"
                "state_version=state_version+1,updated_at=?,finished_at=? "
                "WHERE id=? AND state_version=? AND status IN ('queued','waiting_human')",
                (now, now, job_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "job cancellation requires the current queued or waiting_human version"
                )
            job = self.get(job_id)
            self._append_event(
                "job.cancelled",
                {"job_id": job_id, "status": "cancelled"},
                topic="jobs",
                job_id=job_id,
                case_id=job.case_id,
                batch_id=job.batch_id,
            )
        return self.get(job_id)

    def fail(self, job_id: int, worker_id: str, exc: BaseException) -> JobRecord:
        now = _now()
        code = f"JOB_{type(exc).__name__.upper()}"[:100]
        message = _redact(str(exc) or type(exc).__name__)
        with self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE audit_job SET status='failed',error_code=?,error_message=?,"
                "lease_owner='',lease_expires_at='',state_version=state_version+1,"
                "updated_at=?,finished_at=? WHERE id=? AND status='running' AND lease_owner=?",
                (code, message, now, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("job failure lost its worker lease")
            job = self.get(job_id)
            self._append_event(
                "job.failed",
                {"job_id": job_id, "status": "failed", "error_code": code},
                topic="jobs",
                job_id=job_id,
                case_id=job.case_id,
                batch_id=job.batch_id,
            )
        return self.get(job_id)

    def events_after(
        self, cursor: int, *, limit: int = 100
    ) -> builtins.list[AuditEvent]:
        rows = self.store.conn.execute(
            "SELECT * FROM audit_event WHERE id>? ORDER BY id LIMIT ?",
            (max(0, cursor), max(1, min(limit, 500))),
        ).fetchall()
        return [AuditEvent(
            event_id=int(row["id"]),
            event_type=str(row["event_type"]),
            topic=str(row["topic"]),
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            case_id=int(row["case_id"]) if row["case_id"] is not None else None,
            batch_id=int(row["batch_id"]) if row["batch_id"] is not None else None,
            payload=json.loads(row["payload_json"]),
            created_at=str(row["created_at"]),
        ) for row in rows]

    def log_human_action(self, action: HumanAction) -> int:
        now = _now()
        with self.store.conn:
            cursor = self.store.conn.execute(
                "INSERT INTO human_action_log(reviewer,action,target_type,target_id,"
                "before_version,after_version,summary,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    action.reviewer,
                    action.action,
                    action.target_type,
                    action.target_id,
                    action.before_version,
                    action.after_version,
                    action.summary,
                    now,
                ),
            )
            action_id = int(cursor.lastrowid or 0)
            self._append_event(
                "human.action",
                {
                    "action_id": action_id,
                    "reviewer": action.reviewer,
                    "action": action.action,
                    "target_type": action.target_type,
                    "target_id": action.target_id,
                },
                topic="human-actions",
            )
        return action_id


class JobContext:
    def __init__(self, repository: JobRepository, job: JobRecord, worker_id: str) -> None:
        self.repository = repository
        self.job = job
        self.worker_id = worker_id

    async def progress(self, percent: int, message: str) -> None:
        self.job = self.repository.progress(
            self.job.job_id, self.worker_id, percent, message
        )
        await asyncio.sleep(0)


JobResult = JobOutcome | dict[str, Any]
JobHandler = Callable[[JobContext], Awaitable[JobResult] | JobResult]


class JobWorker:
    def __init__(
        self,
        repository: JobRepository,
        handlers: dict[str, JobHandler] | None = None,
        *,
        poll_seconds: float = 0.25,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.handlers = handlers or {}
        self.poll_seconds = max(0.05, poll_seconds)
        self.lease_seconds = lease_seconds
        self.worker_id = f"web-{uuid.uuid4().hex[:12]}"
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self.repository.recover_expired()
            self._task = asyncio.create_task(self._run(), name="award-audit-job-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _heartbeat(self, job_id: int) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            if not self.repository.heartbeat(
                job_id, self.worker_id, lease_seconds=self.lease_seconds
            ):
                return

    async def _execute(self, job: JobRecord) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.repository.fail(
                job.job_id,
                self.worker_id,
                RuntimeError(f"no handler registered for job kind {job.kind}"),
            )
            return
        heartbeat = asyncio.create_task(self._heartbeat(job.job_id))
        try:
            raw = handler(JobContext(self.repository, job, self.worker_id))
            result = await raw if inspect.isawaitable(raw) else raw
            outcome = result if isinstance(result, JobOutcome) else JobOutcome(result=result)
            self.repository.finish(job.job_id, self.worker_id, outcome)
        except Exception as exc:
            self.repository.fail(job.job_id, self.worker_id, exc)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            job = self.repository.claim_next(
                self.worker_id, lease_seconds=self.lease_seconds
            )
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.poll_seconds
                    )
                except asyncio.TimeoutError:
                    continue
            else:
                await self._execute(job)
