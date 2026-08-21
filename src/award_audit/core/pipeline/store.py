"""版本化台账存储层：批次 / 暂存 / 正式库（版本化）/ 审计。

核心不变式：**永不物理覆盖、永不物理删除**。
- 入库 = 新增 record（version=1, is_current=1）+ audit(create)。
- 勘误 = 旧版本失效，新增 version+1 当前版本 + audit(correct, 字段级 diff)。
- 回滚 = 把某历史版本重新设为 current，其余置 0 + audit(rollback)。
一条 SQL 即可查回某业务键的全部版本与变更史。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from award_audit.core.identity import (
    normalize_comparison_identity,
    normalize_identity,
    route_text_variants,
)
from award_audit.core.models.template import TemplateSpec
from award_audit.core.models.triage import decide_triage
from award_audit.core.pipeline.dedup import dedup_key_from_mapping

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")


class StateConflictError(RuntimeError):
    """Optimistic case-state update lost a version race."""


# 当前时间戳（秒级 ISO），集中一处便于将来替换/测试
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 计算字段级 diff：{字段:{old,new}}，仅含变化的字段
def _diff(old: dict[str, str], new: dict[str, str]) -> dict[str, dict[str, str]]:
    d: dict[str, dict[str, str]] = {}
    for k in set(old) | set(new):
        o, n = old.get(k, ""), new.get(k, "")
        if o != n:
            d[k] = {"old": o, "new": n}
    return d


# 资源项码归一化：数字码补足 8 位；空码返回 ""，不误变为 "00000000"。
def _norm_zylbm(code: str) -> str:
    c = (code or "").strip()
    return c.zfill(8) if c.isdigit() else c.casefold()


def _canonical_evidence_url(url: str) -> str:
    """Normalize public evidence URLs without changing their resource identity."""

    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = "&".join(
        item for item in parsed.query.split("&")
        if item and not item.lower().startswith(("utm_", "spm=", "from="))
    )
    base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    return f"{base}?{query}" if query else base


# 租约时间戳（UTC 毫秒 ISO）：租约比较必须同一格式同一时区，独立于展示用 _now()（本地秒）
def _lease_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# now + seconds 的 UTC 毫秒 ISO（claim 时算租约到期）
def _lease_expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


class PromoteBlocked(RuntimeError):
    """入库门禁未通过：整批全通过前绝不入库。reasons 为逐条阻断原因。"""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("；".join(reasons) or "promotion blocked")


class Store:
    """台账连接与操作。db_path 传 ':memory:' 可用于测试。"""

    # 打开/新建库并建表
    def __init__(self, db_path: str | Path) -> None:
        raw_db_path = str(db_path)
        self.db_path = raw_db_path
        self.conn = sqlite3.connect(raw_db_path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if raw_db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        self._ensure_audit_columns()
        self._apply_migrations()

    # M5 起使用独立、有版本记录的加性迁移；既有 M4 补列逻辑仅为旧库兼容，不再扩展。
    def _apply_migrations(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            str(row["version"])
            for row in self.conn.execute("SELECT version FROM schema_migration")
        }
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            version = path.stem
            if not _MIGRATION_NAME.fullmatch(version):
                raise RuntimeError(f"非法 migration 文件名：{path.name}")
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            applied_at = _now()
            script = (
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_migration(version, applied_at) VALUES "
                + f"('{version}', '{applied_at}');\nCOMMIT;"
            )
            try:
                self.conn.executescript(script)
            except Exception:
                self.conn.rollback()
                raise

    # audit_result 增量列迁移：既有库缺列则 ALTER 补上（IF NOT EXISTS 不改旧表；加性幂等）
    def _ensure_audit_columns(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(audit_result)")}
        for name, ddl in (("triage", "TEXT NOT NULL DEFAULT 'manual'"),
                          ("reason_codes_json", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in have:
                self.conn.execute(f"ALTER TABLE audit_result ADD COLUMN {name} {ddl}")
        self.conn.commit()

    # 关闭连接
    def close(self) -> None:
        self.conn.close()

    # ---------- 批次 ----------

    # 新建批次，返回批次 id
    def create_batch(self, name: str, *, source: str = "excel", importer: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO import_batch(name, source, imported_at, importer) VALUES (?,?,?,?)",
            (name, source, _now(), importer),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # 更新批次状态
    def set_batch_status(self, batch_id: int, status: str) -> None:
        self.conn.execute("UPDATE import_batch SET status=? WHERE id=?", (status, batch_id))
        self.conn.commit()

    # 更新批次文件数/行数
    def update_batch_counts(self, batch_id: int, n_files: int, n_rows: int) -> None:
        self.conn.execute(
            "UPDATE import_batch SET n_files=?, n_rows=? WHERE id=?",
            (n_files, n_rows, batch_id),
        )
        self.conn.commit()

    # 取批次
    def get_batch(self, batch_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM import_batch WHERE id=?", (batch_id,)
        ).fetchone()
        return row

    # 列全部批次（新→旧）
    def list_batches(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM import_batch ORDER BY id DESC").fetchall())

    def persist_import_batch(
        self,
        name: str,
        *,
        operator: str,
        rows: list[dict[str, Any]],
        n_files: int,
        n_rows: int,
        source_folder: str,
        files: list[dict[str, Any]],
        check_result: dict[str, Any],
        template_fingerprint: str,
        ledger_fingerprint: str,
        context_version: int,
    ) -> int:
        """Atomically publish one complete local-import snapshot."""

        ts = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.conn.execute(
                "INSERT INTO import_batch(name,source,imported_at,importer,n_files,n_rows,status) "
                "VALUES (?,'excel',?,?,?,?, '审核中')",
                (name, ts, operator, n_files, n_rows),
            )
            batch_id = int(cursor.lastrowid or 0)
            self._insert_staging_rows(batch_id, rows)
            self.conn.execute(
                "INSERT INTO batch_import_context(batch_id,source_folder,files_json,"
                "check_result_json,context_version,template_fingerprint,ledger_fingerprint,"
                "created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    source_folder,
                    json.dumps(files, ensure_ascii=False),
                    json.dumps(check_result, ensure_ascii=False),
                    int(context_version),
                    template_fingerprint,
                    ledger_fingerprint,
                    ts,
                ),
            )
            self.conn.execute(
                "INSERT INTO batch_stage_run(batch_id,stage,status,attempt,state_version,"
                "started_at,finished_at,updated_at) VALUES (?,'local','done',1,1,?,?,?)",
                (batch_id, ts, ts, ts),
            )
            self.conn.commit()
            return batch_id
        except Exception:
            self.conn.rollback()
            raise

    def record_failed_import(self, name: str, *, operator: str, exc: Exception) -> int:
        """Persist a fail-closed import attempt without exposing a partial batch."""

        ts = _now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO import_batch(name,source,imported_at,importer,status,needs_reimport) "
                "VALUES (?,'excel',?,?, '导入失败',1)",
                (name, ts, operator),
            )
            batch_id = int(cursor.lastrowid or 0)
            self.conn.execute(
                "INSERT INTO batch_stage_run(batch_id,stage,status,attempt,error_code,"
                "error_message,state_version,started_at,finished_at,updated_at) "
                "VALUES (?,'local','failed',1,?,?,1,?,?,?)",
                (
                    batch_id,
                    f"IMPORT_{type(exc).__name__.upper()}"[:100],
                    "本地导入未完成，批次已隔离并要求重新导入",
                    ts,
                    ts,
                    ts,
                ),
            )
        return batch_id

    # ---------- 暂存 ----------

    def _insert_staging_rows(self, batch_id: int, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO staging_record(batch_id,file,sheet,row_no,table_code,resource_code,year,"
            "dedup_key,data_json,check_status,issues_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    batch_id, r["file"], r.get("sheet", ""), r["row_no"], r["table_code"],
                    r.get("resource_code", ""), r.get("year", ""), r.get("dedup_key", ""),
                    json.dumps(r["data"], ensure_ascii=False),
                    r.get("check_status", "pass"),
                    json.dumps(r.get("issues", []), ensure_ascii=False),
                )
                for r in rows
            ],
        )

    # 批量写入待审记录；rows 每项含 file/sheet/row_no/table_code/resource_code/year/
    # dedup_key/data/check_status/issues
    def add_staging(self, batch_id: int, rows: list[dict[str, Any]]) -> None:
        self._insert_staging_rows(batch_id, rows)
        self.conn.commit()

    # 取某批次的待审记录
    def staging_of(self, batch_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM staging_record WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall())

    # 取单条暂存记录
    def get_staging_row(self, staging_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM staging_record WHERE id=?", (staging_id,)
        ).fetchone()
        return row

    # 复核一条暂存记录：写复核状态/人/时间（M3 复核台的"通过/打回"落点）
    def set_review_status(self, staging_id: int, decision: str, reviewer: str = "") -> None:
        if decision not in ("通过", "打回", "待复核"):
            raise ValueError(f"非法复核状态：{decision!r}")
        self.conn.execute(
            "UPDATE staging_record SET review_status=?, reviewer=?, reviewed_at=? WHERE id=?",
            (decision, reviewer, _now(), staging_id),
        )
        self.conn.commit()

    # ---------- 正式库（版本化）----------

    # 所有当前有效记录的去重键集合（供 L4 跨批次去重）
    def current_keys(
        self, registry: Mapping[str, TemplateSpec] | None = None
    ) -> set[str]:
        rows = self.conn.execute(
            "SELECT business_key,table_code,data_json FROM record WHERE is_current=1"
        ).fetchall()
        keys = {str(row["business_key"]) for row in rows}
        if registry is None:
            return keys
        # Historical rows retain their original business_key for audit. Recompute the
        # current profile key from data_json so identity-v2 still detects a v1 record.
        for row in rows:
            spec = registry.get(str(row["table_code"]))
            if spec is None:
                continue
            try:
                data = json.loads(str(row["data_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                key = dedup_key_from_mapping(data, spec)
                if key:
                    keys.add(key)
        return keys

    # 按去重键取当前有效记录
    def find_current(self, business_key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM record WHERE business_key=? AND is_current=1", (business_key,)
        ).fetchone()
        return row

    # 新增一条正式记录（version=1）+ 审计 create；返回 record_id
    def _insert_record(self, business_key: str, table_code: str, data: dict[str, str],
                       batch_id: int | None, operator: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO record(business_key,table_code,version,is_current,valid_from,"
            "source_batch_id,data_json,created_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                business_key, table_code, 1, 1, _now(), batch_id,
                json.dumps(data, ensure_ascii=False), operator,
            ),
        )
        rid = int(cur.lastrowid or 0)
        self._audit(rid, business_key, "create", {}, "首次入库", operator, batch_id)
        return rid

    # 写审计
    def _audit(
        self, record_id: int | None, business_key: str, action: str,
        diff: dict[str, dict[str, str]], reason: str, operator: str,
        batch_id: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(record_id,business_key,action,diff_json,reason,operator,"
            "ts,batch_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                record_id, business_key, action, json.dumps(diff, ensure_ascii=False),
                reason, operator, _now(), batch_id,
            ),
        )

    # 提升入正式库：**整批全通过才入库**（同事务门禁，认当前结果 current_result_id）；
    # 任一未过 → PromoteBlocked，零写入、批次状态不变（保守政务门禁，绝不"跳过问题行入其余"）。
    def promote_batch(
        self,
        batch_id: int,
        operator: str = "",
        *,
        allowed_roots: Sequence[str | Path] | None = None,
        template_fingerprint: str | None = None,
        ledger_fingerprint: str | None = None,
        context_version: int | None = None,
    ) -> dict[str, int]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            reasons = self.promotion_gate(
                batch_id,
                allowed_roots=allowed_roots,
                template_fingerprint=template_fingerprint,
                ledger_fingerprint=ledger_fingerprint,
                context_version=context_version,
            )
            if reasons:
                self.conn.rollback()
                raise PromoteBlocked(reasons)
            inserted = skipped_dup = 0
            for r in self.staging_of(batch_id):
                key = r["dedup_key"]
                if self.find_current(key) is not None:  # 已在正式库（跨批去重）→ 不重复入
                    skipped_dup += 1
                    continue
                self._insert_record(key, r["table_code"], json.loads(r["data_json"]),
                                    batch_id, operator)
                inserted += 1
            self.conn.execute("UPDATE import_batch SET status='已入库' WHERE id=?", (batch_id,))
            self.conn.commit()
        except PromoteBlocked:
            raise
        except Exception:
            self.conn.rollback()
            raise
        # fail/打回/未达结论已在门禁处整批拦下，故这些桶恒 0（保留键兼容既有调用打印）
        return {"inserted": inserted, "skipped_dup": skipped_dup,
                "skipped_fail": 0, "skipped_rejected": 0, "skipped_audit_rejected": 0}

    # 勘误：用新数据取代当前版本（旧版本失效，新增 version+1）+ 审计 correct(字段级 diff)
    def correct(
        self, business_key: str, new_data: dict[str, str], reason: str,
        operator: str = "",
    ) -> None:
        cur = self.find_current(business_key)
        if cur is None:
            raise ValueError(f"勘误失败：去重键 {business_key!r} 无当前记录")
        old_data: dict[str, str] = json.loads(cur["data_json"])
        now = _now()
        self.conn.execute("UPDATE record SET is_current=0, valid_to=? WHERE id=?", (now, cur["id"]))
        c2 = self.conn.execute(
            "INSERT INTO record(business_key,table_code,version,is_current,valid_from,"
            "source_batch_id,data_json,created_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                business_key, cur["table_code"], int(cur["version"]) + 1, 1, now,
                cur["source_batch_id"], json.dumps(new_data, ensure_ascii=False), operator,
            ),
        )
        self._audit(
            int(c2.lastrowid or 0), business_key, "correct", _diff(old_data, new_data),
            reason, operator, None,
        )
        self.conn.commit()

    # 回滚：把某历史版本重新设为当前（其余置 0）+ 审计 rollback
    def rollback(self, business_key: str, to_version: int, operator: str = "") -> None:
        target = self.conn.execute(
            "SELECT * FROM record WHERE business_key=? AND version=?", (business_key, to_version)
        ).fetchone()
        if target is None:
            raise ValueError(f"回滚失败：去重键 {business_key!r} 无版本 {to_version}")
        self.conn.execute(
            "UPDATE record SET is_current=0, valid_to=? "
            "WHERE business_key=? AND is_current=1",
            (_now(), business_key),
        )
        self.conn.execute(
            "UPDATE record SET is_current=1, valid_to=NULL WHERE id=?", (target["id"],)
        )
        self._audit(
            int(target["id"]), business_key, "rollback", {}, f"回滚到版本 {to_version}",
            operator, None,
        )
        self.conn.commit()

    # 某去重键的全部版本（旧→新）
    def history(self, business_key: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM record WHERE business_key=? ORDER BY version", (business_key,)
        ).fetchall())

    # 某去重键的审计流水
    def audit_of(self, business_key: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM audit_log WHERE business_key=? ORDER BY id", (business_key,)
        ).fetchall())

    # 按片段模糊搜索业务键（CLI 查历史用，去重键含分隔符不便直接输入）
    def search_keys(self, fragment: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT business_key FROM record WHERE business_key LIKE ?", (f"%{fragment}%",)
        ).fetchall()
        return [r["business_key"] for r in rows]

    # ---------- 联网核对结论（L5，资源项级）----------

    # 找最近的同名批次 id；没有则新建（audit 复用 import 建的批次，无则独立建 source='audit'）
    def find_or_create_batch(self, name: str, *, source: str = "audit") -> int:
        row = self.conn.execute(
            "SELECT id FROM import_batch WHERE name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        return int(row["id"]) if row is not None else self.create_batch(name, source=source)

    # 批量写入联网核对结论；reports 每项 = EvidenceReport.model_dump()（列表字段内部转 JSON）。
    # triage 由 verdict+confidence 确定性算出（不信任传入值，DB 恒一致，可 SQL 按分诊排队）。
    def add_audit_results(
        self, batch_id: int, reports: list[dict[str, Any]]
    ) -> list[int]:
        result_ids: list[int] = []
        with self.conn:
            for r in reports:
                cursor = self.conn.execute(
                    "INSERT INTO audit_result(batch_id,resource_code,award_name,year,verdict,"
                    "confidence,triage,reason_codes_json,identity_version,source_kind,source_url,page_year,"
                    "extracted_count,submitted_count,missing_json,extra_json,source_urls_json,"
                    "found_assets_json,evidence_assets_json,evidence_json,notes,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id, r["resource_code"], r.get("award_name", ""),
                        r.get("year", ""), r.get("verdict", "无法核对"),
                        r.get("confidence", "low"), decide_triage(
                            r.get("verdict", "无法核对"), r.get("confidence", "low")
                        ),
                        json.dumps(r.get("reason_codes", []), ensure_ascii=False),
                        r.get("identity_version", "identity-v1"),
                        r.get("source_kind", "none"), r.get("source_url", ""),
                        r.get("page_year", ""), int(r.get("extracted_count", 0)),
                        int(r.get("submitted_count", 0)),
                        json.dumps(r.get("missing", []), ensure_ascii=False),
                        json.dumps(r.get("extra", []), ensure_ascii=False),
                        json.dumps(r.get("source_urls", []), ensure_ascii=False),
                        json.dumps(r.get("found_assets", []), ensure_ascii=False),
                        json.dumps(r.get("evidence_assets", []), ensure_ascii=False),
                        json.dumps(r.get("evidence", []), ensure_ascii=False),
                        r.get("notes", ""), _now(),
                    ),
                )
                result_ids.append(int(cursor.lastrowid or 0))
        return result_ids

    # 取某批次的联网核对结论
    def audit_results_of(self, batch_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM audit_result WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall())

    # 该批次被人工打回的资源项码集合（归一、过滤空码，供 promote 资源项级闸门）。
    def rejected_audit_codes(self, batch_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT resource_code FROM audit_result WHERE batch_id=? AND review_status='打回'",
            (batch_id,),
        ).fetchall()
        return {n for r in rows if (n := _norm_zylbm(r["resource_code"]))}

    # 取单条联网核对结论
    def get_audit_row(self, audit_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM audit_result WHERE id=?", (audit_id,)).fetchone()
        return row

    # 复核一条联网核对结论：写复核状态/人/时间（复核台「联网核对」页的通过/打回落点）
    def set_audit_review(self, audit_id: int, decision: str, reviewer: str = "") -> None:
        if decision not in ("通过", "打回", "待复核"):
            raise ValueError(f"非法复核状态：{decision!r}")
        self.conn.execute(
            "UPDATE audit_result SET review_status=?, reviewer=?, reviewed_at=? WHERE id=?",
            (decision, reviewer, _now(), audit_id),
        )
        self.conn.commit()

    # ---------- 统一编排：导入溯源 / 阶段执行状态 / 并发 claim / 入库门禁（M5.7 收口）----------

    # 存导入溯源（源目录+文件哈希+序列化版本+模板/台账指纹）；L5 阶段读时重验，漂移即 fail-closed
    def save_import_context(
        self, batch_id: int, *, source_folder: str,
        files: list[dict[str, Any]], check_result: dict[str, Any],
        template_fingerprint: str, ledger_fingerprint: str, context_version: int = 1,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO batch_import_context(batch_id,source_folder,files_json,"
            "check_result_json,context_version,template_fingerprint,ledger_fingerprint,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, source_folder, json.dumps(files, ensure_ascii=False),
             json.dumps(check_result, ensure_ascii=False), int(context_version),
             template_fingerprint, ledger_fingerprint, _now()),
        )
        self.conn.commit()

    def get_import_context(self, batch_id: int) -> sqlite3.Row | None:
        """Return the raw persisted row for diagnostics; production consumers use load."""

        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM batch_import_context WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return row

    def load_import_context(
        self,
        batch_id: int,
        *,
        allowed_roots: Sequence[str | Path],
        template_fingerprint: str,
        ledger_fingerprint: str,
        context_version: int,
    ) -> dict[str, Any] | None:
        """Load and revalidate hashes, roots, serialization version, and fingerprints."""

        row = self.get_import_context(batch_id)
        if row is None:
            return None
        from award_audit.core.pipeline.provenance import validate_import_context

        return validate_import_context(
            dict(row),
            allowed_roots=allowed_roots,
            template_fingerprint=template_fingerprint,
            ledger_fingerprint=ledger_fingerprint,
            context_version=context_version,
        )

    # 标批次"需重新导入"（fail-closed：禁审/禁入库）
    def mark_needs_reimport(self, batch_id: int, flag: bool = True) -> None:
        self.conn.execute("UPDATE import_batch SET needs_reimport=? WHERE id=?",
                          (1 if flag else 0, batch_id))
        self.conn.commit()

    def needs_reimport(self, batch_id: int) -> bool:
        row = self.conn.execute(
            "SELECT needs_reimport FROM import_batch WHERE id=?", (batch_id,)).fetchone()
        return bool(row and int(row["needs_reimport"]))

    # 批次阶段互斥抢占（原子）：本 stage running 未过期→None；同批另一 m4/m5 running→None
    # （禁交叉）；否则置 running 续租，返回该 stage_run 行。
    def claim_batch_stage(self, batch_id: int, stage: str, *,
                          worker: str = "", lease_seconds: int = 300) -> sqlite3.Row | None:
        if stage not in {"local", "m4", "m5"}:
            raise ValueError(f"非法批次阶段：{stage!r}")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now, expires, ts = _lease_now(), _lease_expires(lease_seconds), _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if stage in ("m4", "m5"):
                other = "m5" if stage == "m4" else "m4"
                busy = self.conn.execute(
                    "SELECT 1 FROM batch_stage_run WHERE batch_id=? AND stage=? "
                    "AND status='running' AND lease_expires_at>? LIMIT 1",
                    (batch_id, other, now)).fetchone()
                if busy is not None:
                    self.conn.commit()
                    return None
            row = self.conn.execute(
                "SELECT * FROM batch_stage_run WHERE batch_id=? AND stage=?",
                (batch_id, stage)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO batch_stage_run(batch_id,stage,status,attempt,lease_owner,"
                    "lease_expires_at,started_at,updated_at) VALUES (?,?,'running',1,?,?,?,?)",
                    (batch_id, stage, worker, expires, ts, ts))
            elif row["status"] == "running" and row["lease_expires_at"] > now:
                self.conn.commit()
                return None
            else:
                cursor = self.conn.execute(
                    "UPDATE batch_stage_run SET status='running',attempt=attempt+1,lease_owner=?,"
                    "lease_expires_at=?,state_version=state_version+1,error_code='',error_message='',"
                    "finished_at='',started_at=?,updated_at=? "
                    "WHERE id=? AND state_version=?",
                    (worker, expires, ts, ts, int(row["id"]), int(row["state_version"])))
                if cursor.rowcount != 1:
                    raise StateConflictError("batch stage claim lost its state version")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_batch_stage_run(batch_id, stage)

    # 结束批次阶段：写终态（done/failed/partial）
    def finish_batch_stage(
        self,
        batch_id: int,
        stage: str,
        status: str,
        *,
        worker: str,
        expected_version: int,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if status not in {"done", "failed", "partial"}:
            raise ValueError(f"非法批次阶段终态：{status!r}")
        ts = _now()
        cursor = self.conn.execute(
            "UPDATE batch_stage_run SET status=?,error_code=?,error_message=?,lease_owner='',"
            "lease_expires_at='',state_version=state_version+1,finished_at=?,updated_at=? "
            "WHERE batch_id=? AND stage=? AND status='running' AND lease_owner=? "
            "AND state_version=?",
            (
                status, error_code[:100], error_message[:500], ts, ts, batch_id, stage,
                worker, expected_version,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise StateConflictError("batch stage finish lost its worker lease")
        self.conn.commit()

    def get_batch_stage_run(self, batch_id: int, stage: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM batch_stage_run WHERE batch_id=? AND stage=?",
            (batch_id, stage)).fetchone()
        return row

    def get_batch_stage_runs(self, batch_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM batch_stage_run WHERE batch_id=? ORDER BY stage",
            (batch_id,)).fetchall())

    # 回收过期 running 的批次阶段（租约过期→pending，供重跑）
    def recover_expired_stage_runs(self) -> int:
        cur = self.conn.execute(
            "UPDATE batch_stage_run SET status='pending',lease_owner='',lease_expires_at='',"
            "state_version=state_version+1,updated_at=? "
            "WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<?",
            (_now(), _lease_now()))
        self.conn.commit()
        return cur.rowcount

    # 资源项级(m4)执行 claim（原子）：done→None（幂等跳过）；running 未过期→None；
    # 其余→置 running 抢占
    def claim_stage_item(self, batch_id: int, resource_code: str, year: str, *,
                         stage: str = "m4", worker: str = "", lease_seconds: int = 300,
                          ) -> sqlite3.Row | None:
        if stage != "m4":
            raise ValueError(f"非法资源项阶段：{stage!r}")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        code, yr = _norm_zylbm(resource_code), (year or "").strip()
        now, expires, ts = _lease_now(), _lease_expires(lease_seconds), _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM audit_stage_item WHERE batch_id=? AND stage=? "
                "AND resource_code=? AND year=?", (batch_id, stage, code, yr)).fetchone()
            if row is not None and row["status"] == "done":
                self.conn.commit()
                return None
            if row is not None and row["status"] == "running" and row["lease_expires_at"] > now:
                self.conn.commit()
                return None
            if row is None:
                self.conn.execute(
                    "INSERT INTO audit_stage_item(batch_id,stage,resource_code,year,status,attempt,"
                    "lease_owner,lease_expires_at,started_at,updated_at) "
                    "VALUES (?,?,?,?,'running',1,?,?,?,?)",
                    (batch_id, stage, code, yr, worker, expires, ts, ts))
            else:
                cursor = self.conn.execute(
                    "UPDATE audit_stage_item SET status='running',attempt=attempt+1,lease_owner=?,"
                    "lease_expires_at=?,state_version=state_version+1,current_result_id=NULL,"
                    "error_code='',error_message='',finished_at='',started_at=?,updated_at=? "
                    "WHERE id=? AND state_version=?",
                    (worker, expires, ts, ts, int(row["id"]), int(row["state_version"])))
                if cursor.rowcount != 1:
                    raise StateConflictError("stage item claim lost its state version")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stage_item(batch_id, resource_code, year, stage=stage)

    # 资源项 m4 收尾：done/failed/skipped + 当前结果指针（门禁只认 current_result_id，旧结果仅审计）
    def finish_stage_item(
        self,
        batch_id: int,
        resource_code: str,
        year: str,
        *,
        status: str,
        worker: str,
        expected_version: int,
        current_result_id: int | None = None,
        stage: str = "m4",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if status not in {"done", "failed", "skipped"}:
            raise ValueError(f"非法资源项终态：{status!r}")
        code, yr, ts = _norm_zylbm(resource_code), (year or "").strip(), _now()
        cursor = self.conn.execute(
            "UPDATE audit_stage_item SET status=?,current_result_id=?,"
            "lease_owner='',lease_expires_at='',error_code=?,error_message=?,"
            "state_version=state_version+1,finished_at=?,updated_at=? "
            "WHERE batch_id=? AND stage=? AND resource_code=? AND year=? "
            "AND status='running' AND lease_owner=? AND state_version=?",
            (status, current_result_id, error_code[:100], error_message[:500], ts, ts,
             batch_id, stage, code, yr, worker, expected_version),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise StateConflictError("stage item finish lost its worker lease")
        self.conn.commit()

    def get_stage_item(self, batch_id: int, resource_code: str, year: str, *,
                       stage: str = "m4") -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM audit_stage_item WHERE batch_id=? AND stage=? "
            "AND resource_code=? AND year=?",
            (batch_id, stage, _norm_zylbm(resource_code), (year or "").strip())).fetchone()
        return row

    def get_stage_items(self, batch_id: int, *, stage: str | None = None) -> list[sqlite3.Row]:
        if stage is None:
            return list(self.conn.execute(
                "SELECT * FROM audit_stage_item WHERE batch_id=? ORDER BY id",
                (batch_id,)).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM audit_stage_item WHERE batch_id=? AND stage=? ORDER BY id",
            (batch_id, stage)).fetchall())

    # 回收过期 running 的资源项执行项（租约过期→failed，可续跑）
    def recover_expired_stage_items(self) -> int:
        cur = self.conn.execute(
            "UPDATE audit_stage_item SET status='failed',lease_owner='',lease_expires_at='',"
            "error_code='LEASE_EXPIRED',state_version=state_version+1,updated_at=? "
            "WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<?",
            (_now(), _lease_now()))
        self.conn.commit()
        return cur.rowcount

    # 该批每个有 staging 行的 (归一码,年) 的资源项最终结论（供入库门禁）：
    # pending / m4_accepted / m5_accepted / rejected / insufficient。
    # 优先级：活跃 M5 案→pending；已终审 M5 案→按人工结论（accepted 解 M4 failed/skipped）；
    # 否则 M4 当前结果的人工复核（通过→m4_accepted、打回→rejected、待复核→pending）；未核→pending。
    def resource_conclusions(self, batch_id: int) -> dict[tuple[str, str], str]:
        targets: set[tuple[str, str]] = set()
        for r in self.conn.execute(
            "SELECT DISTINCT resource_code, year FROM staging_record WHERE batch_id=?",
            (batch_id,)):
            if (code := _norm_zylbm(r["resource_code"])):
                targets.add((code, str(r["year"] or "")))
        active_case: set[tuple[str, str]] = set()
        case_decision: dict[tuple[str, str], str] = {}
        for r in self.conn.execute(
            "SELECT ac.resource_code,ac.year,ac.status,ac.human_decision,"
            "ac.origin_m4_result_id,si.current_result_id "
            "FROM audit_case ac LEFT JOIN audit_stage_item si "
            "ON si.batch_id=ac.batch_id AND si.stage='m4' "
            "AND si.resource_code=ac.resource_code AND si.year=ac.year "
            "WHERE ac.batch_id=? ORDER BY ac.id", (batch_id,)):
            code = _norm_zylbm(r["resource_code"])
            if not code:
                continue
            key = (code, str(r["year"] or ""))
            if str(r["status"]) in ("queued", "running", "waiting_human"):
                active_case.add(key)
            origin_id = int(r["origin_m4_result_id"] or 0)
            current_id = int(r["current_result_id"] or 0)
            if origin_id > 0 and origin_id == current_id and str(r["status"]) == "completed" and (
                dec := str(r["human_decision"] or "")
            ):
                case_decision[key] = dec
        m4_review: dict[tuple[str, str], str] = {}
        for r in self.conn.execute(
            "SELECT si.resource_code, si.year, ar.review_status FROM audit_stage_item si "
            "JOIN audit_result ar ON ar.id=si.current_result_id "
            "WHERE si.batch_id=? AND si.stage='m4' AND si.status='done'", (batch_id,)):
            if (code := _norm_zylbm(r["resource_code"])):
                m4_review[(code, str(r["year"] or ""))] = str(r["review_status"] or "待复核")
        out: dict[tuple[str, str], str] = {}
        for key in targets:
            if key in active_case:
                out[key] = "pending"
            elif key in case_decision:
                out[key] = {"accepted": "m5_accepted", "rejected": "rejected",
                            "insufficient": "insufficient"}.get(case_decision[key], "pending")
            elif key in m4_review:
                out[key] = {"通过": "m4_accepted", "打回": "rejected"}.get(
                    m4_review[key], "pending")
            else:
                out[key] = "pending"
        return out

    # 入库门禁（整批全通过·认当前结果）：返回逐条阻断原因（空=可入库）。
    # promote 与只读展示共用同一判据。
    def promotion_gate(
        self,
        batch_id: int,
        *,
        allowed_roots: Sequence[str | Path] | None = None,
        template_fingerprint: str | None = None,
        ledger_fingerprint: str | None = None,
        context_version: int | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        batch = self.get_batch(batch_id)
        if batch is None:
            return ["批次不存在"]
        if self.needs_reimport(batch_id):
            reasons.append("批次标记为需重新导入")
        raw_context = self.get_import_context(batch_id)
        if raw_context is None:
            reasons.append("批次缺少可信导入上下文")
        else:
            from award_audit.core.pipeline import provenance

            source_folder = str(raw_context["source_folder"])
            try:
                self.load_import_context(
                    batch_id,
                    allowed_roots=(allowed_roots if allowed_roots is not None
                                   else [source_folder]),
                    template_fingerprint=(
                        template_fingerprint if template_fingerprint is not None
                        else str(raw_context["template_fingerprint"])
                    ),
                    ledger_fingerprint=(
                        ledger_fingerprint if ledger_fingerprint is not None
                        else str(raw_context["ledger_fingerprint"])
                    ),
                    context_version=(
                        context_version if context_version is not None
                        else provenance.CONTEXT_VERSION
                    ),
                )
            except provenance.ImportContextError as exc:
                reasons.append(f"导入上下文失效：{exc}")
        if int(self.conn.execute(
            "SELECT COUNT(*) FROM audit_job WHERE batch_id=? AND status IN ('queued','running')",
            (batch_id,)).fetchone()[0]) > 0:
            reasons.append("尚有进行中的任务")
        if int(self.conn.execute(
            "SELECT COUNT(*) FROM batch_stage_run WHERE batch_id=? AND status='running'",
            (batch_id,)).fetchone()[0]) > 0:
            reasons.append("有阶段正在执行")
        bad = int(self.conn.execute(
            "SELECT COUNT(*) FROM staging_record WHERE batch_id=? "
            "AND (check_status='fail' OR review_status='打回')", (batch_id,)).fetchone()[0])
        if bad > 0:
            reasons.append(f"存在不合格或被打回的行 {bad} 条")
        staged = self.staging_of(batch_id)
        if not staged:
            reasons.append("批次没有暂存记录")
        invalid_identity = sum(
            1 for row in staged
            if not _norm_zylbm(str(row["resource_code"] or ""))
            or re.fullmatch(r"[12][0-9]{3}", str(row["year"] or "")) is None
        )
        if invalid_identity:
            reasons.append(f"存在缺失资源项码或可靠年份的行 {invalid_identity} 条")
        unresolved = [f"{c}/{y or '—'}:{st}"
                      for (c, y), st in sorted(self.resource_conclusions(batch_id).items())
                      if st not in ("m4_accepted", "m5_accepted")]
        if unresolved:
            reasons.append("以下资源项未达可入库结论：" + "；".join(unresolved[:20]))
        return reasons

    # 只读入库就绪（供 API/按钮展示，与门禁同判据）
    def promotion_readiness(self, batch_id: int, **context: Any) -> dict[str, Any]:
        reasons = self.promotion_gate(batch_id, **context)
        return {"can_promote": not reasons, "reasons": reasons}

    # ---------- M5 疑难案件（M5.4 migration）----------

    @staticmethod
    def _case_trigger_key(trigger_codes: list[str]) -> str:
        normalized = sorted({str(code).strip() for code in trigger_codes if str(code).strip()})
        if not normalized:
            raise ValueError("audit case requires at least one trigger code")
        return "|".join(normalized)

    # 取某 (批,资源项码,年) 的活跃案（queued/running/waiting_human）
    def _active_case_for(self, batch_id: int, resource_code: str,
                         year: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM audit_case WHERE batch_id=? AND resource_code=? AND year=? "
            "AND status IN ('queued','running','waiting_human') ORDER BY id LIMIT 1",
            (batch_id, _norm_zylbm(resource_code), (year or "").strip())).fetchone()
        return row

    # 合并触发原因/待答问题进既有活跃案（P0-10：一 (码,年) 一案，trigger 取并集）
    def _merge_case_triggers(self, row: sqlite3.Row, trigger_codes: list[str],
                             open_questions: list[str]) -> None:
        old = [str(c) for c in json.loads(row["trigger_codes_json"])]
        merged = list(dict.fromkeys([*old, *trigger_codes]))
        old_q = [str(q) for q in json.loads(row["open_questions_json"])]
        merged_q = list(dict.fromkeys([*old_q, *open_questions]))[:20]
        if set(merged) == set(old) and merged_q == old_q:
            return
        self.conn.execute(
            "UPDATE audit_case SET trigger_codes_json=?,trigger_key=?,open_questions_json=?,"
            "state_version=state_version+1,updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), self._case_trigger_key(merged),
             json.dumps(merged_q, ensure_ascii=False), _now(), int(row["id"])))
        self.conn.commit()

    def _case_can_rebind(self, row: sqlite3.Row) -> bool:
        if str(row["status"]) != "queued" or int(row["step_count"]) != 0:
            return False
        if str(row["human_decision"] or ""):
            return False
        case_id = int(row["id"])
        observed = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM tool_trace WHERE case_id=?) "
            "OR EXISTS(SELECT 1 FROM evidence_artifact WHERE case_id=?)",
            (case_id, case_id),
        ).fetchone()[0]
        return not bool(observed)

    def validate_audit_case_m4_binding(
        self, case_id: int, *, require_bound: bool = True
    ) -> int:
        row = self.conn.execute(
            "SELECT ac.origin_m4_result_id,si.current_result_id "
            "FROM audit_case ac LEFT JOIN audit_stage_item si "
            "ON si.batch_id=ac.batch_id AND si.stage='m4' "
            "AND si.resource_code=ac.resource_code AND si.year=ac.year "
            "WHERE ac.id=?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"audit case not found: {case_id}")
        origin_id = int(row["origin_m4_result_id"] or 0)
        current_id = int(row["current_result_id"] or 0)
        if origin_id <= 0:
            if require_bound:
                raise StateConflictError("M5 case is not bound to an M4 result")
            return 0
        if origin_id != current_id:
            raise StateConflictError("M5 case M4 result is no longer current")
        return origin_id

    def create_or_get_audit_case(self, payload: dict[str, Any]) -> tuple[int, bool]:
        """一 (批,资源项码,年) 一活跃案；已存在则合并触发原因（P0-10：不因 trigger 不同建多案）。"""

        trigger_codes = [str(code) for code in payload.get("trigger_codes", [])]
        trigger_key = self._case_trigger_key(trigger_codes)
        batch_id = int(payload["batch_id"])
        resource_code = _norm_zylbm(str(payload["resource_code"]))
        year = str(payload.get("year", "")).strip()
        origin_m4_result_id = int(payload.get("origin_m4_result_id", 0) or 0)
        if not resource_code:
            raise ValueError("audit case requires a resource code")
        open_questions = [str(q) for q in payload.get("open_questions", [])]
        existing = self._active_case_for(batch_id, resource_code, year)
        if existing is not None:
            existing_origin = int(existing["origin_m4_result_id"] or 0)
            if origin_m4_result_id > 0 and existing_origin != origin_m4_result_id:
                if self._case_can_rebind(existing):
                    self.conn.execute(
                        "UPDATE audit_case SET origin_m4_result_id=?,state_version=state_version+1,"
                        "updated_at=? WHERE id=?",
                        (origin_m4_result_id, _now(), int(existing["id"])),
                    )
                    self.conn.commit()
                    existing = self._active_case_for(batch_id, resource_code, year)
                    assert existing is not None
                else:
                    self.conn.execute(
                        "UPDATE audit_case SET status='failed',"
                        "last_error='superseded_by_new_m4_result',"
                        "state_version=state_version+1,updated_at=? WHERE id=?",
                        (_now(), int(existing["id"])),
                    )
                    self.conn.commit()
                    existing = None
        if existing is not None:
            self._merge_case_triggers(existing, trigger_codes, open_questions)
            return int(existing["id"]), False
        now = _now()
        try:
            with self.conn:
                cursor = self.conn.execute(
                    "INSERT INTO audit_case("
                    "batch_id,origin_m4_result_id,resource_code,award_name,year,trigger_key,trigger_codes_json,"
                    "objective,submitted_summary_json,known_urls_json,retrieved_memories_json,"
                    "open_questions_json,budget_json,step_count,token_used,elapsed_ms,status,"
                    "recommendation,confidence,reason_codes_json,last_action_json,last_error,"
                    "last_error_detail,llm_usage_json,verifier_llm_usage_json,pending_supplement,"
                    "evidence_progress_json,state_version,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        origin_m4_result_id or None,
                        resource_code,
                        str(payload.get("award_name", "")),
                        year,
                        trigger_key,
                        json.dumps(trigger_codes, ensure_ascii=False),
                        str(payload.get("objective", "")),
                        json.dumps(payload.get("submitted_summary", {}), ensure_ascii=False),
                        json.dumps(payload.get("known_urls", []), ensure_ascii=False),
                        json.dumps(payload.get("retrieved_memories", []), ensure_ascii=False),
                        json.dumps(payload.get("open_questions", []), ensure_ascii=False),
                        json.dumps(payload.get("budget", {}), ensure_ascii=False),
                        int(payload.get("step_count", 0)),
                        int(payload.get("token_used", 0)),
                        int(payload.get("elapsed_ms", 0)),
                        str(payload.get("status", "queued")),
                        str(payload.get("recommendation", "")),
                        str(payload.get("confidence", "low")),
                        json.dumps(payload.get("reason_codes", []), ensure_ascii=False),
                        json.dumps(payload.get("last_action", {}), ensure_ascii=False),
                        str(payload.get("last_error", "")),
                        str(payload.get("last_error_detail", "")),
                        json.dumps(payload.get("llm_usage", []), ensure_ascii=False),
                        json.dumps(
                            payload.get("verifier_llm_usage", []), ensure_ascii=False
                        ),
                        str(payload.get("pending_supplement", "")),
                        json.dumps(payload.get("evidence_progress", {}), ensure_ascii=False),
                        1,
                        now,
                        now,
                    ),
                )
            return int(cursor.lastrowid or 0), True
        except sqlite3.IntegrityError:
            row = self._active_case_for(batch_id, resource_code, year)
            if row is None:
                raise
            self._merge_case_triggers(row, trigger_codes, open_questions)
            return int(row["id"]), False

    def save_audit_case_execution(
        self,
        case_id: int,
        payload: dict[str, Any],
        *,
        expected_version: int,
        attempt_id: int | None = None,
        traces: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        verification_reports: list[dict[str, Any]] | None = None,
    ) -> int:
        """Atomically persist state plus newly observed traces/artifacts."""

        new_version = expected_version + 1
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE audit_case SET objective=?,submitted_summary_json=?,known_urls_json=?,"
                "retrieved_memories_json=?,open_questions_json=?,budget_json=?,step_count=?,"
                "token_used=?,elapsed_ms=?,reflection_count=?,status=?,recommendation=?,confidence=?,"
                "reason_codes_json=?,last_action_json=?,last_error=?,last_error_detail=?,"
                "llm_usage_json=?,verifier_llm_usage_json=?,pending_supplement=?,"
                "evidence_progress_json=?,"
                "state_version=?,updated_at=? WHERE id=? AND state_version=?",
                (
                    str(payload.get("objective", "")),
                    json.dumps(payload.get("submitted_summary", {}), ensure_ascii=False),
                    json.dumps(payload.get("known_urls", []), ensure_ascii=False),
                    json.dumps(payload.get("retrieved_memories", []), ensure_ascii=False),
                    json.dumps(payload.get("open_questions", []), ensure_ascii=False),
                    json.dumps(payload.get("budget", {}), ensure_ascii=False),
                    int(payload.get("step_count", 0)),
                    int(payload.get("token_used", 0)),
                    int(payload.get("elapsed_ms", 0)),
                    int(payload.get("reflection_count", 0)),
                    str(payload.get("status", "queued")),
                    str(payload.get("recommendation", "")),
                    str(payload.get("confidence", "low")),
                    json.dumps(payload.get("reason_codes", []), ensure_ascii=False),
                    json.dumps(payload.get("last_action", {}), ensure_ascii=False),
                    str(payload.get("last_error", "")),
                    str(payload.get("last_error_detail", "")),
                    json.dumps(payload.get("llm_usage", []), ensure_ascii=False),
                    json.dumps(
                        payload.get("verifier_llm_usage", []), ensure_ascii=False
                    ),
                    str(payload.get("pending_supplement", "")),
                    json.dumps(payload.get("evidence_progress", {}), ensure_ascii=False),
                    new_version,
                    now,
                    case_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"audit case {case_id} state version changed from {expected_version}"
                )
            for trace in traces or []:
                self.conn.execute(
                    "INSERT OR IGNORE INTO tool_trace("
                    "case_id,attempt_id,call_id,tool_name,started_at,finished_at,duration_ms,"
                    "input_summary_json,output_summary_json,ok,error_code)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        attempt_id,
                        str(trace["call_id"]),
                        str(trace["tool_name"]),
                        str(trace["started_at"]),
                        str(trace["finished_at"]),
                        int(trace.get("duration_ms", 0)),
                        json.dumps(trace.get("input_summary", {}), ensure_ascii=False),
                        json.dumps(trace.get("output_summary", {}), ensure_ascii=False),
                        1 if trace.get("ok") else 0,
                        str(trace.get("error_code", "")),
                    ),
                )
            for artifact in artifacts or []:
                self.conn.execute(
                    "INSERT OR IGNORE INTO evidence_artifact("
                    "case_id,attempt_id,kind,source_url,local_path,content_type,sha256,size_bytes,"
                    "fetched_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        attempt_id,
                        str(artifact["kind"]),
                        str(artifact["source_url"]),
                        str(artifact["local_path"]),
                        str(artifact["content_type"]),
                        str(artifact["sha256"]),
                        int(artifact.get("size_bytes", 0)),
                        str(artifact["fetched_at"]),
                        json.dumps(artifact.get("metadata", {}), ensure_ascii=False),
                    ),
                )
            next_sequence_row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM verification_report "
                "WHERE case_id=?",
                (case_id,),
            ).fetchone()
            next_sequence = int(next_sequence_row[0])
            for report in verification_reports or []:
                self.conn.execute(
                    "INSERT INTO verification_report("
                    "case_id,attempt_id,sequence,target_match,year_match,source_authority,"
                    "coverage_complete,contradictions_json,missing_evidence_json,"
                    "supplement_requests_json,recommended_action,reason_codes_json,"
                    "deterministic_action,model_used,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        attempt_id,
                        next_sequence,
                        str(report["target_match"]),
                        str(report["year_match"]),
                        str(report["source_authority"]),
                        str(report["coverage_complete"]),
                        json.dumps(report.get("contradictions", []), ensure_ascii=False),
                        json.dumps(report.get("missing_evidence", []), ensure_ascii=False),
                        json.dumps(report.get("supplement_requests", []), ensure_ascii=False),
                        str(report["recommended_action"]),
                        json.dumps(report.get("reason_codes", []), ensure_ascii=False),
                        str(report["deterministic_action"]),
                        1 if report.get("model_used") else 0,
                        now,
                    ),
                )
                next_sequence += 1
        return new_version

    def start_audit_attempt(
        self,
        case_id: int,
        *,
        kind: str,
        supplement_request: str,
        budget_limits: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create one isolated execution budget for a case."""

        if kind not in {"initial", "supplement", "resume"}:
            raise ValueError("invalid audit attempt kind")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            case = self.conn.execute(
                "SELECT id FROM audit_case WHERE id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise KeyError(f"audit case not found: {case_id}")
            running = self.conn.execute(
                "SELECT id FROM audit_attempt WHERE case_id=? AND status='running'",
                (case_id,),
            ).fetchone()
            if running is not None:
                raise StateConflictError(f"audit case {case_id} already has a running attempt")
            sequence = int(self.conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM audit_attempt WHERE case_id=?",
                (case_id,),
            ).fetchone()[0])
            cursor = self.conn.execute(
                "INSERT INTO audit_attempt(case_id,sequence,kind,supplement_request,status,"
                "phase,budget_limits_json,budget_usage_json,started_at,updated_at) "
                "VALUES (?,?,?,?, 'running','scope_confirmation',?,'{}',?,?)",
                (
                    case_id,
                    sequence,
                    kind,
                    supplement_request[:1000],
                    json.dumps(dict(budget_limits), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            attempt_id = int(cursor.lastrowid or 0)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "attempt_id": attempt_id,
            "sequence": sequence,
            "kind": kind,
            "status": "running",
            "phase": "scope_confirmation",
        }

    def finish_audit_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        phase: str,
        budget_usage: Mapping[str, Any],
        step_count: int,
        token_used: int,
        elapsed_ms: int,
        stop_reason: str,
        verifier_status: str,
        conclusion_readiness: str,
        blockers: Sequence[str],
    ) -> None:
        if status not in {"succeeded", "incomplete", "failed", "interrupted"}:
            raise ValueError("invalid terminal audit attempt status")
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE audit_attempt SET status=?,phase=?,budget_usage_json=?,step_count=?,"
                "token_used=?,elapsed_ms=?,stop_reason=?,verifier_status=?,"
                "conclusion_readiness=?,blockers_json=?,finished_at=?,updated_at=? "
                "WHERE id=? AND status='running'",
                (
                    status,
                    phase[:80],
                    json.dumps(dict(budget_usage), ensure_ascii=False),
                    max(0, step_count),
                    max(0, token_used),
                    max(0, elapsed_ms),
                    stop_reason[:100],
                    verifier_status[:40],
                    conclusion_readiness[:40],
                    json.dumps(list(dict.fromkeys(blockers))[:100], ensure_ascii=False),
                    now,
                    now,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"audit attempt {attempt_id} is not running")

    def list_audit_attempts(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_attempt WHERE case_id=? ORDER BY sequence", (case_id,)
        ).fetchall()
        return [{
            "attempt_id": int(row["id"]),
            "sequence": int(row["sequence"]),
            "kind": str(row["kind"]),
            "supplement_request": str(row["supplement_request"]),
            "status": str(row["status"]),
            "phase": str(row["phase"]),
            "budget_limits": json.loads(row["budget_limits_json"] or "{}"),
            "budget_usage": json.loads(row["budget_usage_json"] or "{}"),
            "step_count": int(row["step_count"]),
            "token_used": int(row["token_used"]),
            "elapsed_ms": int(row["elapsed_ms"]),
            "stop_reason": str(row["stop_reason"]),
            "verifier_status": str(row["verifier_status"]),
            "conclusion_readiness": str(row["conclusion_readiness"]),
            "blockers": json.loads(row["blockers_json"] or "[]"),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
        } for row in rows]

    def sync_audit_scopes(
        self,
        case_id: int,
        scopes: Sequence[Mapping[str, Any]],
        *,
        identity_version: str,
    ) -> list[dict[str, Any]]:
        """Upsert deterministic submitted-side role scopes for a case."""

        now = _now()
        with self.conn:
            for scope in scopes:
                scope_key = str(scope.get("scope_key", "")).strip()
                if not scope_key:
                    continue
                self.conn.execute(
                    "INSERT INTO audit_scope(case_id,scope_key,role_type,role_label,required,"
                    "identity_version,profile_json,business_scope_json,submitted_row_count,"
                    "submitted_identity_count,unidentified_row_count,submitted_identities_json,"
                    "status,blockers_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,scope_key) DO UPDATE SET "
                    "role_type=excluded.role_type,role_label=excluded.role_label,"
                    "required=excluded.required,identity_version=excluded.identity_version,"
                    "profile_json=excluded.profile_json,business_scope_json=excluded.business_scope_json,"
                    "submitted_row_count=excluded.submitted_row_count,"
                    "submitted_identity_count=excluded.submitted_identity_count,"
                    "unidentified_row_count=excluded.unidentified_row_count,"
                    "submitted_identities_json=excluded.submitted_identities_json,"
                    "updated_at=excluded.updated_at",
                    (
                        case_id, scope_key, str(scope.get("role_type", "work_or_project")),
                        str(scope.get("role_label", "")), int(bool(scope.get("required", True))),
                        identity_version,
                        json.dumps(scope.get("profile", {}), ensure_ascii=False),
                        json.dumps(scope.get("business_scope", {}), ensure_ascii=False),
                        int(scope.get("submitted_row_count", 0) or 0),
                        int(scope.get("submitted_identity_count", 0) or 0),
                        int(scope.get("unidentified_row_count", 0) or 0),
                        json.dumps(scope.get("submitted_identities", {}), ensure_ascii=False),
                        "pending", "[]", now, now,
                    ),
                )
        return self.list_audit_scopes(case_id)

    def list_audit_scopes(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_scope WHERE case_id=? ORDER BY required DESC,id", (case_id,)
        ).fetchall()
        return [{
            "scope_id": int(row["id"]),
            "scope_key": str(row["scope_key"]),
            "role_type": str(row["role_type"]),
            "role_label": str(row["role_label"]),
            "required": bool(row["required"]),
            "identity_version": str(row["identity_version"]),
            "profile": json.loads(row["profile_json"] or "{}"),
            "business_scope": json.loads(row["business_scope_json"] or "{}"),
            "submitted_row_count": int(row["submitted_row_count"]),
            "submitted_identity_count": int(row["submitted_identity_count"]),
            "unidentified_row_count": int(row["unidentified_row_count"]),
            "submitted_identities": json.loads(row["submitted_identities_json"] or "{}"),
            "status": str(row["status"]),
            "blockers": json.loads(row["blockers_json"] or "[]"),
        } for row in rows]

    def sync_scope_assignments(
        self, case_id: int, assignments: Sequence[Mapping[str, Any]]
    ) -> None:
        """Persist row-level conservation so no submitted row disappears silently."""

        scopes = {
            str(row["scope_key"]): int(row["id"])
            for row in self.conn.execute(
                "SELECT id,scope_key FROM audit_scope WHERE case_id=?", (case_id,)
            )
        }
        now = _now()
        with self.conn:
            for item in assignments:
                scope_keys = [
                    str(value) for value in item.get("scope_keys", [])
                    if str(value) in scopes
                ]
                status = str(item.get("status", "unassigned"))
                reasons = [str(value) for value in item.get("reasons", []) if str(value)]
                if status == "assigned" and not scope_keys:
                    status = "unassigned"
                    reasons.append("assigned_scope_not_persisted")
                self.conn.execute(
                    "INSERT INTO audit_scope_assignment(case_id,source_path,sheet_name,row_number,"
                    "category,status,scope_keys_json,scope_ids_json,reasons_json,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,source_path,sheet_name,row_number) DO UPDATE SET "
                    "category=excluded.category,status=excluded.status,"
                    "scope_keys_json=excluded.scope_keys_json,scope_ids_json=excluded.scope_ids_json,"
                    "reasons_json=excluded.reasons_json,updated_at=excluded.updated_at",
                    (
                        case_id, str(item.get("source_path", "")),
                        str(item.get("sheet_name", "")), int(item.get("row_number", 0)),
                        str(item.get("category", "")), status,
                        json.dumps(scope_keys, ensure_ascii=False),
                        json.dumps([scopes[key] for key in scope_keys], ensure_ascii=False),
                        json.dumps(list(dict.fromkeys(reasons)), ensure_ascii=False), now, now,
                    ),
                )

    def submission_conservation_summary(self, case_id: int) -> dict[str, Any]:
        counts = {
            str(row["status"]): int(row["n"])
            for row in self.conn.execute(
                "SELECT status,COUNT(*) AS n FROM audit_scope_assignment "
                "WHERE case_id=? GROUP BY status", (case_id,)
            )
        }
        unresolved = [dict(row) for row in self.conn.execute(
            "SELECT source_path,sheet_name,row_number,category,status,reasons_json "
            "FROM audit_scope_assignment WHERE case_id=? AND status<>'assigned' ORDER BY id",
            (case_id,),
        )]
        for item in unresolved:
            item["reasons"] = json.loads(str(item.pop("reasons_json") or "[]"))
        return {
            "total_rows": sum(counts.values()),
            "assigned_rows": counts.get("assigned", 0),
            "ambiguous_rows": counts.get("ambiguous", 0),
            "unassigned_rows": counts.get("unassigned", 0),
            "unresolved_rows": unresolved,
            "closed": not counts.get("ambiguous", 0) and not counts.get("unassigned", 0),
        }

    @staticmethod
    def _asset_scope_routes(
        item: Mapping[str, Any], scope_rows: Sequence[sqlite3.Row]
    ) -> list[dict[str, Any]]:
        metadata = item.get("metadata", {}) or {}
        explicit = item.get("routes") or metadata.get("routes")
        if isinstance(explicit, list) and explicit:
            return [dict(route) for route in explicit if isinstance(route, Mapping)]
        label = str(item.get("label", ""))
        semantic_text = " ".join([
            label,
            str(metadata.get("sheet_name", "")),
            str(metadata.get("section_title", "")),
        ])
        normalized_text = re.sub(r"[\W_]+", "", semantic_text).casefold()
        asset_editions = set(re.findall(r"第[一二三四五六七八九十百0-9]+届", semantic_text))
        expected_editions = {
            edition
            for row in scope_rows
            for edition in re.findall(
                r"第[一二三四五六七八九十百0-9]+届",
                " ".join(
                    str(value)
                    for value in json.loads(row["business_scope_json"] or "{}").values()
                ),
            )
        }
        if asset_editions and expected_editions and asset_editions.isdisjoint(expected_editions):
            return [{
                "scope_id": None,
                "subunit_type": "document",
                "route_source": "exact_rule",
                "confidence": 1.0,
                "route_status": "excluded",
                "reason": "asset edition conflicts with submitted audit scopes",
            }]
        matches: list[dict[str, Any]] = []
        for row in scope_rows:
            business_scope = json.loads(row["business_scope_json"] or "{}")
            values = [
                str(value).strip() for key, value in business_scope.items()
                if key not in {"ZYLBM", "year", "LXNF", "HJNF"}
                and len(str(value).strip()) >= 2
            ]
            normalized_values = [
                variant for value in values for variant in route_text_variants(value)
            ]
            if normalized_values and any(value in normalized_text for value in normalized_values):
                matches.append({
                    "scope_id": int(row["id"]), "route_source": "exact_rule",
                    "confidence": 1.0, "route_status": "routed",
                    "reason": "asset label matches submitted business scope",
                })
        if matches:
            return matches
        generic = []
        for row in scope_rows:
            if (
                not bool(row["required"])
                or int(row["submitted_identity_count"]) <= 0
            ):
                continue
            business_scope = json.loads(row["business_scope_json"] or "{}")
            if not any(
                key not in {"ZYLBM", "year", "LXNF", "HJNF"}
                and str(value).strip()
                for key, value in business_scope.items()
            ):
                generic.append(row)
        if len(generic) == 1:
            return [{
                "scope_id": int(generic[0]["id"]), "route_source": "exact_rule",
                "confidence": 1.0, "route_status": "routed",
                "reason": "asset assigned to the single generic required scope",
            }]
        applicable = [row for row in scope_rows if bool(row["required"])]
        if len(applicable) == 1:
            return [{
                "scope_id": int(applicable[0]["id"]), "route_source": "exact_rule",
                "confidence": 1.0, "route_status": "routed",
                "reason": "single required scope",
            }]
        role_types = {str(row["role_type"]) for row in applicable}
        if (
            metadata.get("m4_verified_parent_bound") is True
            and applicable
            and len(applicable) <= 12
            and len(role_types) == 1
        ):
            return [{
                "scope_id": int(row["id"]), "route_source": "exact_rule",
                "confidence": 0.85, "route_status": "routed",
                "reason": (
                    "verified-parent asset routed to all scopes of its single "
                    "business role for deterministic content filtering"
                ),
            } for row in applicable]
        return [{
            "scope_id": None, "route_source": "exact_rule", "confidence": 0.0,
            "route_status": "ambiguous", "reason": "asset scope is ambiguous",
        }]

    def sync_evidence_ledger(
        self,
        case_id: int,
        attempt_id: int,
        *,
        known_urls: Sequence[str],
        candidates: Sequence[Mapping[str, Any]],
        asset_records: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Upsert source and asset work state independently from bounded tool traces."""

        now = _now()
        scope_payload = dict(scope or {})
        candidate_by_url = {
            _canonical_evidence_url(str(item.get("url", ""))): item
            for item in candidates if str(item.get("url", "")).strip()
        }
        with self.conn:
            for raw_url in dict.fromkeys([*known_urls, *candidate_by_url.keys()]):
                canonical = _canonical_evidence_url(raw_url)
                if not canonical:
                    continue
                candidate = candidate_by_url.get(canonical, {})
                candidate_status = str(candidate.get("status", "pending"))
                source_status = {
                    "succeeded": "accepted",
                    "failed": "failed",
                    "skipped": "excluded",
                }.get(candidate_status, "discovered")
                relevance = str(candidate.get(
                    "relevance",
                    "excluded" if candidate_status == "skipped" else "unreviewed",
                ))
                self.conn.execute(
                    "INSERT INTO evidence_source(case_id,first_attempt_id,last_attempt_id,url,"
                    "canonical_url,title,provider,authority,relevance,relevance_score,scope_json,"
                    "status,exclusion_reason,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,canonical_url) DO UPDATE SET "
                    "last_attempt_id=excluded.last_attempt_id,"
                    "title=CASE WHEN excluded.title<>'' THEN excluded.title "
                    "ELSE evidence_source.title END,"
                    "provider=CASE WHEN excluded.provider<>'' THEN excluded.provider "
                    "ELSE evidence_source.provider END,"
                    "authority=CASE WHEN excluded.authority<>'unknown' "
                    "THEN excluded.authority ELSE evidence_source.authority END,"
                    "relevance=excluded.relevance,relevance_score=excluded.relevance_score,"
                    "scope_json=excluded.scope_json,"
                    "status=excluded.status,"
                    "exclusion_reason=excluded.exclusion_reason,updated_at=excluded.updated_at",
                    (
                        case_id, attempt_id, attempt_id, raw_url, canonical,
                        str(candidate.get("title", ""))[:500],
                        str(candidate.get("provider", ""))[:80],
                        str(candidate.get("source_level", "unknown"))[:40],
                        relevance, int(candidate.get("relevance_score", 0)),
                        json.dumps(scope_payload, ensure_ascii=False), source_status,
                        str(candidate.get("status_reason", ""))[:500], now, now,
                    ),
                )

            def asset_priority(item: Mapping[str, Any]) -> tuple[int, int, int, int]:
                metadata = item.get("metadata", {})
                return (
                    int(str(item.get("status", "")) in {"parsed", "processed"}),
                    int(bool(str(item.get("sha256", "")))),
                    int(bool(str(item.get("local_path", "")))),
                    int(bool(metadata)) if isinstance(metadata, Mapping) else 0,
                )

            def merge_asset_record(
                current: Mapping[str, Any], incoming: Mapping[str, Any]
            ) -> dict[str, Any]:
                """Keep parsed evidence when a duplicate URL pointer is discovered later."""

                preferred, weaker = (
                    (incoming, current)
                    if asset_priority(incoming) > asset_priority(current)
                    else (current, incoming)
                )
                preferred_metadata = preferred.get("metadata", {})
                weaker_metadata = weaker.get("metadata", {})
                weaker_metadata_dict = (
                    dict(weaker_metadata)
                    if isinstance(weaker_metadata, Mapping) else {}
                )
                preferred_metadata_dict = (
                    dict(preferred_metadata)
                    if isinstance(preferred_metadata, Mapping) else {}
                )
                return {
                    **dict(weaker),
                    **dict(preferred),
                    "metadata": {
                        **weaker_metadata_dict,
                        **preferred_metadata_dict,
                    },
                }

            combined_by_url: dict[str, dict[str, Any]] = {}
            for item in asset_records:
                canonical = _canonical_evidence_url(str(item.get("url", "")))
                if canonical:
                    existing = combined_by_url.get(canonical)
                    combined_by_url[canonical] = (
                        merge_asset_record(existing, item) if existing else dict(item)
                    )
            for artifact in artifacts:
                canonical = _canonical_evidence_url(str(artifact.get("source_url", "")))
                if not canonical:
                    continue
                metadata = artifact.get("metadata", {}) or {}
                previous = combined_by_url.get(canonical, {})
                artifact_record = {
                    "url": artifact.get("source_url", previous.get("url", "")),
                    "parent_url": previous.get("parent_url") or metadata.get(
                        "parent_url", metadata.get("source_page_url", metadata.get("page_url", ""))
                    ),
                    "label": previous.get("label", ""),
                    "kind": artifact.get("kind", previous.get("kind", "unknown")),
                    "status": artifact.get("status", "downloaded"),
                    "content_type": artifact.get("content_type", ""),
                    "sha256": artifact.get("sha256", ""),
                    "local_path": artifact.get("local_path", ""),
                    "extraction_method": metadata.get("extraction_method", ""),
                    "metadata": {**(previous.get("metadata", {}) or {}), **metadata},
                }
                combined_by_url[canonical] = (
                    merge_asset_record(previous, artifact_record)
                    if previous else artifact_record
                )
            # A case is saved repeatedly while M5 advances. Preserve the already
            # durable M4 file record when a later save only carries a URL pointer
            # or an unprocessed artifact representation for the same URL.
            if combined_by_url:
                placeholders = ",".join("?" for _ in combined_by_url)
                existing_rows = self.conn.execute(
                    "SELECT canonical_url,url,parent_url,label,kind,status,content_type,"
                    "sha256,local_path,extraction_method,extracted_count,error_code,"
                    "error_message,metadata_json FROM evidence_asset_task "
                    f"WHERE case_id=? AND canonical_url IN ({placeholders})",
                    (case_id, *combined_by_url),
                ).fetchall()
                for row in existing_rows:
                    canonical = str(row["canonical_url"])
                    current = combined_by_url.get(canonical)
                    if current is None:
                        continue
                    try:
                        stored_metadata = json.loads(row["metadata_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        stored_metadata = {}
                    combined_by_url[canonical] = merge_asset_record(
                        {
                            "url": str(row["url"]),
                            "parent_url": str(row["parent_url"]),
                            "label": str(row["label"]),
                            "kind": str(row["kind"]),
                            "status": str(row["status"]),
                            "content_type": str(row["content_type"]),
                            "sha256": str(row["sha256"]),
                            "local_path": str(row["local_path"]),
                            "extraction_method": str(row["extraction_method"]),
                            "extracted_count": int(row["extracted_count"] or 0),
                            "error_code": str(row["error_code"]),
                            "error_message": str(row["error_message"]),
                            "metadata": (
                                stored_metadata
                                if isinstance(stored_metadata, Mapping) else {}
                            ),
                        },
                        current,
                    )
            combined_assets: list[Mapping[str, Any]] = list(combined_by_url.values())
            scope_rows = self.conn.execute(
                "SELECT id,scope_key,role_type,required,submitted_identity_count,"
                "profile_json,business_scope_json "
                "FROM audit_scope WHERE case_id=? ORDER BY id",
                (case_id,),
            ).fetchall()
            for item in combined_assets:
                raw_url = str(item.get("url", "")).strip()
                if not raw_url:
                    continue
                canonical = _canonical_evidence_url(raw_url)
                parent = str(item.get("parent_url", ""))
                metadata = item.get("metadata", {}) or {}
                requested_scope_key = str(
                    item.get("scope_key", "") or metadata.get("scope_key", "")
                )
                scope_id = next((int(row["id"]) for row in scope_rows
                                 if str(row["scope_key"]) == requested_scope_key), None)
                source = self.conn.execute(
                    "SELECT id FROM evidence_source WHERE case_id=? AND canonical_url=?",
                    (case_id, _canonical_evidence_url(parent or raw_url)),
                ).fetchone()
                raw_status = str(item.get("status", "discovered"))
                error_message = str(item.get("error_message", ""))
                explicit_routes = item.get("routes") or metadata.get("routes")
                has_explicit_routes = bool(
                    isinstance(explicit_routes, list) and explicit_routes
                )
                has_manual_semantic_route = bool(
                    isinstance(explicit_routes, list)
                    and any(
                        isinstance(route, Mapping)
                        and (
                            str(route.get("route_status", "")) == "ambiguous"
                            or bool(route.get("selector", {}).get(
                                "requires_human_confirmation", False
                            ))
                            or str(route.get("selector", {}).get(
                                "roster_contribution", "")) == "manual"
                        )
                        for route in explicit_routes
                    )
                )
                status = {
                    "downloaded": "downloaded",
                    "parsed": "processed",
                    "processed": "processed",
                    "failed": "failed",
                    "access_denied": "access_denied",
                    "excluded": "excluded",
                    "skipped": "excluded",
                }.get(raw_status, "discovered")
                if (
                    status == "failed"
                    and "file type pdf is not allowed here" in error_message.casefold()
                    and not has_manual_semantic_route
                ):
                    status = "excluded"
                self.conn.execute(
                    "INSERT INTO evidence_asset_task(case_id,first_attempt_id,last_attempt_id,"
                    "source_id,scope_id,url,canonical_url,parent_url,label,kind,status,content_type,sha256,"
                    "local_path,extraction_method,extracted_count,error_code,error_message,scope_json,metadata_json,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,canonical_url) DO UPDATE SET "
                    "last_attempt_id=excluded.last_attempt_id,"
                    "source_id=COALESCE(excluded.source_id,evidence_asset_task.source_id),"
                    "scope_id=COALESCE(excluded.scope_id,evidence_asset_task.scope_id),"
                    "parent_url=CASE WHEN excluded.parent_url<>'' THEN excluded.parent_url "
                    "ELSE evidence_asset_task.parent_url END,"
                    "label=CASE WHEN excluded.label<>'' THEN excluded.label "
                    "ELSE evidence_asset_task.label END,"
                    "kind=CASE WHEN excluded.kind<>'unknown' THEN excluded.kind "
                    "ELSE evidence_asset_task.kind END,"
                    "status=CASE WHEN evidence_asset_task.status='processed' "
                    "THEN 'processed' ELSE excluded.status END,"
                    "content_type=CASE WHEN excluded.content_type<>'' THEN excluded.content_type "
                    "ELSE evidence_asset_task.content_type END,"
                    "sha256=CASE WHEN excluded.sha256<>'' THEN excluded.sha256 "
                    "ELSE evidence_asset_task.sha256 END,"
                    "local_path=CASE WHEN excluded.local_path<>'' THEN excluded.local_path "
                    "ELSE evidence_asset_task.local_path END,"
                    "extraction_method=CASE WHEN excluded.extraction_method<>'' "
                    "THEN excluded.extraction_method ELSE evidence_asset_task.extraction_method END,"
                    "extracted_count=CASE WHEN excluded.extracted_count>0 THEN excluded.extracted_count "
                    "ELSE evidence_asset_task.extracted_count END,"
                    "error_code=CASE WHEN excluded.error_code<>'' THEN excluded.error_code "
                    "ELSE evidence_asset_task.error_code END,"
                    "error_message=CASE WHEN excluded.error_message<>'' THEN excluded.error_message "
                    "ELSE evidence_asset_task.error_message END,"
                    "scope_json=excluded.scope_json,"
                    "metadata_json=CASE WHEN excluded.metadata_json<>'{}' THEN excluded.metadata_json "
                    "ELSE evidence_asset_task.metadata_json END,updated_at=excluded.updated_at",
                    (
                        case_id, attempt_id, attempt_id,
                        int(source["id"]) if source is not None else None,
                        scope_id,
                        raw_url, canonical, parent, str(item.get("label", ""))[:500],
                        str(item.get("kind", "unknown"))[:40], status,
                        str(item.get("content_type", ""))[:200], str(item.get("sha256", ""))[:64],
                        str(item.get("local_path", ""))[:2048],
                        str(item.get("extraction_method", ""))[:100],
                        int(item.get("extracted_count", metadata.get("extracted_count", 0)) or 0),
                        str(item.get("error_code", ""))[:100],
                        str(item.get("error_message", ""))[:500],
                        json.dumps(scope_payload, ensure_ascii=False),
                        json.dumps(metadata, ensure_ascii=False), now, now,
                    ),
                )

                asset_row = self.conn.execute(
                    "SELECT id,status FROM evidence_asset_task WHERE case_id=? AND canonical_url=?",
                    (case_id, canonical),
                ).fetchone()
                if asset_row is None:
                    continue
                routes = (
                    self._asset_scope_routes(item, scope_rows)
                    if isinstance(explicit_routes, list) and explicit_routes
                    else
                    [{
                        "scope_id": None,
                        "route_source": "exact_rule",
                        "confidence": 1.0,
                        "route_status": "excluded",
                        "reason": "asset was deterministically excluded before scope routing",
                    }]
                    if str(asset_row["status"]) == "excluded"
                    and not has_manual_semantic_route
                    else [{"scope_id": scope_id, "route_source": "exact_rule", "confidence": 1.0,
                           "route_status": "routed", "reason": "explicit scope key"}]
                    if scope_id is not None
                    else self._asset_scope_routes(item, scope_rows)
                )
                semantic_excluded = bool(routes) and all(
                    str(route.get("route_source", "")) == "llm"
                    and str(route.get("route_status", "")) == "excluded"
                    for route in routes
                )
                asset_status = str(asset_row["status"])
                if semantic_excluded and asset_status in {"discovered", "downloaded"}:
                    asset_status = "excluded"
                    self.conn.execute(
                        "UPDATE evidence_asset_task SET status='excluded',updated_at=? "
                        "WHERE id=?",
                        (now, int(str(asset_row["id"]))),
                    )
                processing_status = (
                    asset_status
                    if asset_status in {"processed", "failed", "access_denied", "excluded"}
                    else "pending"
                )
                has_definitive_route = any(
                    str(route.get("route_status", "")) in {
                        "routed", "out_of_scope", "excluded"
                    }
                    for route in routes
                )
                existing_routed = self.conn.execute(
                    "SELECT 1 FROM evidence_asset_scope "
                    "WHERE asset_id=? AND route_status='routed' LIMIT 1",
                    (int(str(asset_row["id"])),),
                ).fetchone() is not None
                if has_explicit_routes or has_definitive_route:
                    self.conn.execute(
                        "DELETE FROM evidence_asset_scope WHERE asset_id=?",
                        (int(str(asset_row["id"])),),
                    )
                elif existing_routed:
                    continue
                for route in routes:
                    routed_scope_id = route.get("scope_id")
                    routed_scope_number = int(str(routed_scope_id or 0))
                    routed_scope = next((row for row in scope_rows
                                         if int(row["id"]) == routed_scope_number), None)
                    profile = (
                        json.loads(routed_scope["profile_json"] or "{}")
                        if routed_scope else {}
                    )
                    raw_selector = route.get("selector", {})
                    selector = dict(raw_selector) if isinstance(raw_selector, Mapping) else {}
                    self.conn.execute(
                        "INSERT INTO evidence_asset_scope("
                        "asset_id,scope_id,subunit_type,selector_json,"
                        "identity_fields_json,route_source,confidence,route_status,processing_status,"
                        "reason,blockers_json,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT DO UPDATE SET route_source=excluded.route_source,"
                        "confidence=excluded.confidence,route_status=excluded.route_status,"
                        "processing_status=excluded.processing_status,reason=excluded.reason,"
                        "identity_fields_json=excluded.identity_fields_json,updated_at=excluded.updated_at",
                        (
                            int(str(asset_row["id"])),
                            routed_scope_number or None,
                            str(route.get("subunit_type", "document")),
                            json.dumps(selector, ensure_ascii=False),
                            json.dumps(profile.get("primary_alternatives", []), ensure_ascii=False),
                            str(route.get("route_source", "exact_rule")),
                            float(str(route.get("confidence", 0.0))),
                            str(route.get("route_status", "pending")), processing_status,
                            str(route.get("reason", "")),
                            json.dumps(route.get("blockers", []), ensure_ascii=False), now, now,
                        ),
                    )

            case_scope = self.conn.execute(
                "SELECT year,resource_code FROM audit_case WHERE id=?", (case_id,)
            ).fetchone()
            asset_rows = self.conn.execute(
                "SELECT a.id,a.parent_url,a.canonical_url,a.status,a.extracted_count,"
                "r.id AS route_id,r.scope_id "
                "FROM evidence_asset_task a JOIN evidence_asset_scope r ON r.asset_id=a.id "
                "WHERE a.case_id=? AND a.last_attempt_id=? AND r.route_status='routed'",
                (case_id, attempt_id),
            ).fetchall()
            grouped: dict[tuple[int | None, str], list[sqlite3.Row]] = {}
            for row in asset_rows:
                group_key = _canonical_evidence_url(
                    str(row["parent_url"] or row["canonical_url"])
                )
                grouped.setdefault((row["scope_id"], group_key), []).append(row)
            self.conn.execute(
                "UPDATE evidence_group SET status='excluded',expected_assets=0,"
                "terminal_assets=0,updated_at=? WHERE case_id=? AND attempt_id=?",
                (now, case_id, attempt_id),
            )
            for (scope_id, group_key), members in grouped.items():
                storage_group_key = f"{scope_id or 0}:{group_key}"
                statuses = [str(item["status"]) for item in members]
                extracted_count = sum(int(item["extracted_count"] or 0) for item in members)
                terminal = sum(
                    value in {"processed", "failed", "excluded"} for value in statuses
                )
                group_status = (
                    "failed" if "failed" in statuses
                    else ("complete" if terminal == len(members) else "collecting")
                )
                group_scope = {
                    "year": str(case_scope["year"]) if case_scope else "",
                    "resource_code": str(case_scope["resource_code"]) if case_scope else "",
                    **scope_payload,
                }
                self.conn.execute(
                    "INSERT INTO evidence_group(case_id,attempt_id,scope_id,group_key,parent_url,"
                    "scope_json,status,expected_assets,terminal_assets,extracted_count,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(attempt_id,group_key) DO UPDATE SET status=excluded.status,"
                    "expected_assets=excluded.expected_assets,terminal_assets=excluded.terminal_assets,"
                    "extracted_count=excluded.extracted_count,scope_json=excluded.scope_json,"
                    "updated_at=excluded.updated_at",
                    (
                        case_id, attempt_id, scope_id, storage_group_key, group_key,
                        json.dumps(group_scope, ensure_ascii=False), group_status,
                        len(members), terminal, extracted_count, now, now,
                    ),
                )
                group_row = self.conn.execute(
                    "SELECT id FROM evidence_group WHERE attempt_id=? AND group_key=?",
                    (attempt_id, storage_group_key),
                ).fetchone()
                if group_row is not None:
                    self.conn.executemany(
                        "UPDATE evidence_asset_scope SET group_id=? WHERE id=?",
                        [(int(group_row["id"]), int(item["route_id"])) for item in members],
                    )

    def list_evidence_asset_routes(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.*,a.url,a.parent_url,a.label,a.kind,s.scope_key,s.role_type "
            "FROM evidence_asset_scope r JOIN evidence_asset_task a ON a.id=r.asset_id "
            "LEFT JOIN audit_scope s ON s.id=r.scope_id WHERE a.case_id=? ORDER BY a.id,r.id",
            (case_id,),
        ).fetchall()
        return [{
            "route_id": int(row["id"]), "asset_id": int(row["asset_id"]),
            "scope_id": int(row["scope_id"]) if row["scope_id"] is not None else None,
            "scope_key": str(row["scope_key"] or ""), "role_type": str(row["role_type"] or ""),
            "url": str(row["url"]), "parent_url": str(row["parent_url"]),
            "label": str(row["label"]), "kind": str(row["kind"]),
            "subunit_type": str(row["subunit_type"]),
            "selector": json.loads(row["selector_json"] or "{}"),
            "identity_fields": json.loads(row["identity_fields_json"] or "[]"),
            "route_source": str(row["route_source"]), "confidence": float(row["confidence"]),
            "route_status": str(row["route_status"]),
            "processing_status": str(row["processing_status"]), "reason": str(row["reason"]),
            "blockers": json.loads(row["blockers_json"] or "[]"),
        } for row in rows]

    def evidence_workflow_summary(
        self, case_id: int, *, attempt_id: int | None = None
    ) -> dict[str, Any]:
        asset_filter = "case_id=?"
        asset_params: tuple[object, ...] = (case_id,)
        group_filter = "case_id=?"
        group_params: tuple[object, ...] = (case_id,)
        if attempt_id is not None:
            asset_filter += " AND last_attempt_id=?"
            asset_params += (attempt_id,)
            group_filter += " AND attempt_id=?"
            group_params += (attempt_id,)
        asset_sql = (
            "SELECT status,COUNT(*) AS n FROM evidence_asset_task "
            f"WHERE {asset_filter} GROUP BY status"
        )
        counts = {
            str(row["status"]): int(row["n"])
            for row in self.conn.execute(asset_sql, asset_params)
        }
        sources = {str(row["status"]): int(row["n"]) for row in self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM evidence_source WHERE case_id=? GROUP BY status",
            (case_id,),
        )}
        pending = counts.get("discovered", 0) + counts.get("downloaded", 0)
        failed = counts.get("failed", 0)
        group_sql = (
            "SELECT COUNT(*) AS n FROM evidence_group "
            f"WHERE {group_filter} AND status='collecting'"
        )
        collecting_groups = int(
            self.conn.execute(group_sql, group_params).fetchone()["n"]
        )
        blockers: list[str] = []
        if pending:
            blockers.append(f"{pending} evidence assets are not terminal")
        if failed:
            blockers.append(f"{failed} evidence assets failed")
        if collecting_groups:
            blockers.append(f"{collecting_groups} evidence groups are collecting")
        route_filter = "a.case_id=?"
        route_params: tuple[object, ...] = (case_id,)
        if attempt_id is not None:
            route_filter += " AND a.last_attempt_id=?"
            route_params += (attempt_id,)
        route_counts = {str(row["route_status"]): int(row["n"]) for row in self.conn.execute(
            "SELECT r.route_status,COUNT(*) AS n FROM evidence_asset_scope r "
            "JOIN evidence_asset_task a ON a.id=r.asset_id "
            f"WHERE {route_filter} GROUP BY r.route_status",
            route_params,
        )}
        unresolved_routes = sum(
            count for status, count in route_counts.items()
            if status not in {"routed", "out_of_scope", "excluded"}
        )
        if unresolved_routes:
            blockers.append(f"{unresolved_routes} evidence asset routes are unresolved")
        conservation = self.submission_conservation_summary(case_id)
        if conservation["ambiguous_rows"] or conservation["unassigned_rows"]:
            blockers.append(
                f"{conservation['ambiguous_rows'] + conservation['unassigned_rows']} "
                "submitted rows are unresolved"
            )
        return {
            "assets": {
                "total": sum(counts.values()),
                "processed": counts.get("processed", 0),
                "failed": failed,
                "excluded": counts.get("excluded", 0),
                "pending": pending,
            },
            "sources": {"total": sum(sources.values()), **sources},
            "blockers": blockers,
            "groups_collecting": collecting_groups,
            "routes": {"total": sum(route_counts.values()), **route_counts},
            "row_conservation": conservation,
            "ledger_closed": (
                pending == 0 and failed == 0 and collecting_groups == 0
                and unresolved_routes == 0 and conservation["closed"]
            ),
        }

    def record_evidence_comparison(
        self,
        case_id: int,
        attempt_id: int,
        *,
        facts: Sequence[Mapping[str, Any]],
        fallback_missing: Sequence[str],
        fallback_contradictions: Sequence[str],
    ) -> None:
        """Persist the complete identity diff outside bounded trace summaries."""

        status_rank = {"complete": 3, "partial": 2, "unverified": 1, "conflict": 0}
        best = max(
            facts,
            key=lambda item: (
                status_rank.get(str(item.get("status", "")), -1),
                int(item.get("expected_count") or -1),
                int(item.get("observed_count") or -1),
            ),
            default={},
        )
        matched = [str(item) for item in best.get("matched_items", []) if str(item).strip()]
        split_matched = [
            str(item) for item in best.get("split_matched_items", []) if str(item).strip()
        ]
        missing = [str(item) for item in best.get("missing_items", []) if str(item).strip()]
        extra = [str(item) for item in best.get("extra_items", []) if str(item).strip()]
        contradictions = [
            str(item) for item in best.get("contradictions", fallback_contradictions)
            if str(item).strip()
        ]
        blockers = [str(item) for item in fallback_missing if str(item).strip()]
        submitted_count = int(best.get("expected_count") or best.get("submitted_count") or 0)
        evidence_count = int(best.get("observed_count") or best.get("reference_count") or 0)
        matched_count = len(set([*matched, *split_matched]))
        if not missing and int(best.get("missing_item_count") or 0):
            blockers.append(
                f"{int(best.get('missing_item_count') or 0)} missing identities "
                "lack persisted detail"
            )
        if not extra and int(best.get("extra_item_count") or 0):
            blockers.append(
                f"{int(best.get('extra_item_count') or 0)} extra identities lack persisted detail"
            )
        status = "complete" if best and not blockers and not contradictions else "incomplete"
        now = _now()
        group = self.conn.execute(
            "SELECT id FROM evidence_group WHERE attempt_id=? ORDER BY "
            "CASE status WHEN 'complete' THEN 0 ELSE 1 END,id LIMIT 1",
            (attempt_id,),
        ).fetchone()
        group_id = int(group["id"]) if group is not None else None
        source_ref = str(best.get("source_url", ""))[:2048]
        with self.conn:
            self.conn.execute(
                "INSERT INTO evidence_comparison("
                "case_id,attempt_id,group_id,status,submitted_count,"
                "evidence_count,matched_count,missing_json,extra_json,contradictions_json,blockers_json,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(attempt_id,group_id) DO UPDATE SET status=excluded.status,"
                "submitted_count=excluded.submitted_count,evidence_count=excluded.evidence_count,"
                "matched_count=excluded.matched_count,missing_json=excluded.missing_json,"
                "extra_json=excluded.extra_json,contradictions_json=excluded.contradictions_json,"
                "blockers_json=excluded.blockers_json,updated_at=excluded.updated_at",
                (
                    case_id, attempt_id, group_id, status, submitted_count, evidence_count,
                    matched_count, json.dumps(missing, ensure_ascii=False),
                    json.dumps(extra, ensure_ascii=False),
                    json.dumps(contradictions, ensure_ascii=False),
                    json.dumps(list(dict.fromkeys(blockers)), ensure_ascii=False), now, now,
                ),
            )
            identity_rows: list[tuple[object, ...]] = []
            for origin, values in (
                ("submitted", [*matched, *split_matched, *missing]),
                ("evidence", [*matched, *split_matched, *extra]),
            ):
                for display in dict.fromkeys(values):
                    key = re.sub(r"\s+", "", display).casefold()
                    if key:
                        identity_rows.append((
                            case_id, attempt_id, group_id, origin, key, display,
                            "{}", source_ref, now,
                        ))
            self.conn.executemany(
                "INSERT OR IGNORE INTO evidence_identity(case_id,attempt_id,group_id,origin,"
                "identity_key,display_value,scope_json,source_ref,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                identity_rows,
            )

    def record_scope_comparisons(
        self,
        case_id: int,
        attempt_id: int,
        *,
        facts: Sequence[Mapping[str, Any]],
        verifier: Mapping[str, Any],
    ) -> None:
        """Persist one deterministic comparison per applicable business role."""

        scopes = self.list_audit_scopes(case_id)
        workflow_blockers = [
            str(item)
            for item in self.evidence_workflow_summary(
                case_id, attempt_id=attempt_id
            ).get("blockers", [])
            if str(item).strip()
        ]
        now = _now()
        applicable = [
            scope for scope in scopes
            if scope["required"] or scope["submitted_identity_count"] > 0
        ]
        with self.conn:
            for scope in applicable:
                scope_id = int(scope["scope_id"])
                candidates = [
                    fact for fact in facts
                    if int(fact.get("scope_id", 0) or 0) == scope_id
                ]
                submitted = {
                    str(key): str(value)
                    for key, value in scope["submitted_identities"].items()
                }
                scope_role_type = str(scope.get("role_type", ""))

                def comparison_key(value: object) -> str:
                    return normalize_comparison_identity(
                        value, role_type=scope_role_type
                    )

                submitted_by_normalized = {
                    comparison_key(display): display for display in submitted.values()
                }
                primary_alternatives = scope.get("profile", {}).get(
                    "primary_alternatives", []
                )
                primary_width = (
                    len(primary_alternatives[0])
                    if primary_alternatives and primary_alternatives[0]
                    else 1
                )

                def canonical_evidence_value(value: object) -> str:
                    display = str(value).strip()
                    exact = submitted_by_normalized.get(comparison_key(display))
                    if exact is not None:
                        return exact
                    parts = display.split(";")
                    primary = ";".join(parts[:primary_width])
                    primary_matches = [
                        submitted_display
                        for submitted_display in submitted.values()
                        if comparison_key(
                            ";".join(submitted_display.split(";")[:primary_width])
                        ) == comparison_key(primary)
                    ]
                    return primary_matches[0] if len(primary_matches) == 1 else display

                raw_matched = {
                    str(value) for fact in candidates
                    for value in fact.get("matched_items", []) if str(value).strip()
                }
                raw_split_matched = {
                    str(value) for fact in candidates
                    for value in fact.get("split_matched_items", []) if str(value).strip()
                }
                matched = {canonical_evidence_value(value) for value in raw_matched}
                split_matched = {
                    canonical_evidence_value(value) for value in raw_split_matched
                }
                evidence_values = matched | split_matched
                submitted_displays = set(submitted.values())
                related_out_of_scope: list[dict[str, str]] = []
                related_seen: set[tuple[str, str]] = set()
                for fact in candidates:
                    raw_related = fact.get("related_out_of_scope", [])
                    if not isinstance(raw_related, list):
                        continue
                    for item in raw_related:
                        if not isinstance(item, Mapping):
                            continue
                        identity = canonical_evidence_value(item.get("identity", ""))
                        if identity not in submitted_displays:
                            continue
                        source_url = str(item.get("source_url", ""))[:2048]
                        key = (identity, source_url)
                        if key in related_seen:
                            continue
                        related_seen.add(key)
                        related_out_of_scope.append({
                            "identity": identity,
                            "source_url": source_url,
                            "source_label": str(item.get("source_label", ""))[:500],
                            "reason": str(item.get("reason", ""))[:2000],
                        })
                out_of_scope_values = {
                    item["identity"] for item in related_out_of_scope
                }
                identity_conflicts: list[dict[str, str]] = []
                identity_conflict_seen: set[tuple[str, str, str, str]] = set()
                for fact in candidates:
                    raw_conflicts = fact.get("identity_conflicts", [])
                    if not isinstance(raw_conflicts, list):
                        continue
                    for item in raw_conflicts:
                        if not isinstance(item, Mapping):
                            continue
                        submitted_value = str(item.get("submitted", "")).strip()
                        source_value = str(item.get("source", "")).strip()
                        fields = str(item.get("fields", "primary")).strip() or "primary"
                        reason = str(item.get("reason", "field_conflict")).strip()
                        if not submitted_value or not source_value:
                            continue
                        canonical_submitted = canonical_evidence_value(submitted_value)
                        if canonical_submitted not in submitted_displays:
                            continue
                        key = (canonical_submitted, source_value, fields, reason)
                        if key in identity_conflict_seen:
                            continue
                        identity_conflict_seen.add(key)
                        identity_conflicts.append({
                            "submitted": canonical_submitted,
                            "source": source_value,
                            "fields": fields,
                            "reason": reason,
                            "source_url": str(
                                item.get("source_url") or fact.get("source_url", "")
                            ).strip(),
                        })
                conflict_submitted_values = {
                    item["submitted"] for item in identity_conflicts
                }
                conflict_source_values = {
                    item["source"] for item in identity_conflicts
                }
                missing = sorted(
                    submitted_displays - evidence_values - out_of_scope_values
                    - conflict_submitted_values
                )
                explicit_extra = {
                    canonical_evidence_value(value) for fact in candidates
                    for value in fact.get("extra_items", []) if str(value).strip()
                }
                matched_values = submitted_displays & evidence_values
                extra = sorted(
                    (explicit_extra | (evidence_values - submitted_displays))
                    - conflict_source_values
                )
                conflicts = list(dict.fromkeys(
                    str(value) for fact in candidates
                    for value in fact.get("contradictions", []) if str(value).strip()
                ))
                conflicts.extend(
                    "identity_field_conflict: "
                    f"{item['fields']} differs while a secondary identity matches "
                    f"| submitted={item['submitted']} | source={item['source']}"
                    for item in identity_conflicts
                )
                conflicts.extend(
                    "related_out_of_scope: "
                    f"{item['identity']} | official_category={item['source_label'] or 'unknown'} "
                    f"| source_url={item['source_url']}"
                    for item in related_out_of_scope
                )
                collapsed_variants: dict[str, set[str]] = {}
                for raw_value in raw_matched | raw_split_matched:
                    canonical = canonical_evidence_value(raw_value)
                    if canonical != raw_value and canonical in submitted_displays:
                        collapsed_variants.setdefault(canonical, set()).add(raw_value)
                conflicts.extend(
                    f"multiple evidence variants share submitted primary identity: {canonical}"
                    for canonical, variants in collapsed_variants.items()
                    if len(variants) > 1
                )
                scope_source_urls = list(dict.fromkeys(
                    str(fact.get("source_url", "")).strip()
                    for fact in candidates
                    if str(fact.get("source_url", "")).strip()
                ))
                semantic_identity_decisions = [
                    {
                        "candidate_id": str(item.get("candidate_id", ""))[:100],
                        "submitted": str(item.get("submitted", ""))[:500],
                        "source": str(item.get("source", ""))[:500],
                        "decision": str(item.get("decision", "uncertain"))[:40],
                        "confidence": float(item.get("confidence", 0) or 0),
                        "reason": str(item.get("reason", ""))[:1000],
                        "source_url": str(item.get("source_url", ""))[:2048],
                        "source_anchor": str(item.get("source_anchor", ""))[:500],
                    }
                    for fact in candidates
                    for item in fact.get("semantic_identity_decisions", [])
                    if isinstance(item, Mapping)
                ]
                comparison_differences: list[dict[str, object]] = [
                    {
                        "difference_type": "field_conflict",
                        "submitted": item["submitted"],
                        "source": item["source"],
                        "fields": item["fields"],
                        "reason": item["reason"],
                        "source_urls": [item["source_url"]]
                        if item["source_url"] else scope_source_urls,
                    }
                    for item in identity_conflicts
                ]
                comparison_differences.extend({
                    "difference_type": "missing_from_source",
                    "submitted": value,
                    "source": "",
                    "fields": "identity",
                    "reason": "submitted identity was not found in verified source records",
                    "source_urls": scope_source_urls,
                } for value in missing)
                comparison_differences.extend({
                    "difference_type": "extra_in_source",
                    "submitted": "",
                    "source": value,
                    "fields": "identity",
                    "reason": "verified source identity was not provided in the submission",
                    "source_urls": scope_source_urls,
                } for value in extra)
                blockers: list[str] = []
                if len(submitted) != len(submitted_displays):
                    blockers.append("submitted_identity_display_collision")
                # Facts are incremental observations. An early partial page or
                # batch must not poison a later complete scope aggregate; failed
                # and pending assets are enforced independently by the ledger gate.
                contributing_candidates = [
                    fact for fact in candidates
                    if fact.get("contributes_to_scope", True) is not False
                ]
                evidence_complete = bool(contributing_candidates) and any(
                    fact.get("document_complete") is True
                    or str(fact.get("status", "")) == "complete"
                    for fact in contributing_candidates
                )
                if not contributing_candidates:
                    blockers.append("scope_evidence_missing")
                if not evidence_complete:
                    blockers.append("scope_evidence_incomplete")
                if workflow_blockers:
                    blockers.extend(workflow_blockers)
                    evidence_complete = False
                comparison_result = (
                    "not_compared" if not candidates
                    else "conflict" if conflicts
                    else "differences_found" if missing or extra
                    else "matched"
                )
                status = "complete" if evidence_complete and not blockers else "incomplete"
                scope_reason_codes = [
                    "scope_evidence_complete" if evidence_complete else "scope_evidence_incomplete",
                    f"scope_comparison_{comparison_result}",
                ]
                scope_verifier = {
                    "scope_id": scope_id,
                    "scope_key": scope["scope_key"],
                    "role_type": scope["role_type"],
                    "target_match": verifier.get("target_match", "unknown"),
                    "year_match": verifier.get("year_match", "unknown"),
                    "source_authority": verifier.get("source_authority", "unknown"),
                    "coverage_complete": "yes" if evidence_complete else "no",
                    "contradictions": conflicts,
                    "identity_conflicts": identity_conflicts,
                    "semantic_identity_decisions": semantic_identity_decisions,
                    "comparison_differences": comparison_differences,
                    "source_urls": scope_source_urls,
                    "related_out_of_scope": related_out_of_scope,
                    "missing_evidence": blockers,
                    "supplement_requests": [
                        {
                            "code": f"scope_blocker_{index}",
                            "question": blocker,
                            "suggested_tools": [],
                        }
                        for index, blocker in enumerate(blockers, start=1)
                    ],
                    "recommended_action": "manual" if status == "complete" else "supplement",
                    "reason_codes": scope_reason_codes,
                    "deterministic_action": (
                        "accept_evidence" if status == "complete" else "supplement"
                    ),
                    "model_used": bool(verifier.get("model_used", False)),
                }
                self.conn.execute(
                    "INSERT INTO evidence_scope_comparison(case_id,attempt_id,scope_id,status,"
                    "evidence_complete,comparison_result,submitted_row_count,"
                    "submitted_identity_count,evidence_identity_count,matched_count,missing_json,"
                    "extra_json,conflicts_json,blockers_json,verifier_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(attempt_id,scope_id) DO UPDATE SET status=excluded.status,"
                    "evidence_complete=excluded.evidence_complete,"
                    "comparison_result=excluded.comparison_result,"
                    "evidence_identity_count=excluded.evidence_identity_count,"
                    "matched_count=excluded.matched_count,missing_json=excluded.missing_json,"
                    "extra_json=excluded.extra_json,conflicts_json=excluded.conflicts_json,"
                    "blockers_json=excluded.blockers_json,verifier_json=excluded.verifier_json,"
                    "updated_at=excluded.updated_at",
                    (
                        case_id, attempt_id, scope_id, status, int(evidence_complete),
                        comparison_result, scope["submitted_row_count"],
                        scope["submitted_identity_count"],
                        len(matched_values | set(extra) | conflict_source_values),
                        len(matched_values),
                        json.dumps(missing, ensure_ascii=False),
                        json.dumps(extra, ensure_ascii=False),
                        json.dumps(conflicts, ensure_ascii=False),
                        json.dumps(blockers, ensure_ascii=False),
                        json.dumps(scope_verifier, ensure_ascii=False), now, now,
                    ),
                )
                # Keep the scope ledger aligned with the latest persisted comparison.
                # `complete` means evidence coverage is closed; differences remain in
                # evidence_scope_comparison for the human review boundary.
                self.conn.execute(
                    "UPDATE audit_scope SET status=?,blockers_json=?,updated_at=? WHERE id=?",
                    (
                        status,
                        json.dumps(blockers, ensure_ascii=False),
                        now,
                        scope_id,
                    ),
                )
                group_row = self.conn.execute(
                    "SELECT id FROM evidence_group WHERE attempt_id=? AND scope_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (attempt_id, scope_id),
                ).fetchone()
                group_id = int(group_row["id"]) if group_row is not None else None
                source_ref = next((
                    str(fact.get("source_url", ""))
                    for fact in reversed(candidates)
                    if str(fact.get("source_url", "")).strip()
                ), "")[:2048]
                self.conn.execute(
                    "DELETE FROM evidence_identity WHERE case_id=? AND attempt_id=? "
                    "AND scope_id=?",
                    (case_id, attempt_id, scope_id),
                )
                scope_json = json.dumps(
                    scope.get("business_scope", {}), ensure_ascii=False
                )
                identity_rows: list[tuple[object, ...]] = []
                for identity_key, display in submitted.items():
                    identity_rows.append((
                        case_id, attempt_id, group_id, "submitted", identity_key,
                        display, scope_json, "", now, scope_id,
                    ))
                submitted_keys_by_display: dict[str, list[str]] = {}
                for identity_key, display in submitted.items():
                    submitted_keys_by_display.setdefault(display, []).append(identity_key)
                for display in sorted(matched_values | set(extra)):
                    keys = submitted_keys_by_display.get(display) or [
                        comparison_key(display)
                    ]
                    for identity_key in keys:
                        if identity_key:
                            identity_rows.append((
                                case_id, attempt_id, group_id, "evidence", identity_key,
                                display, scope_json, source_ref, now, scope_id,
                            ))
                for item in related_out_of_scope:
                    keys = submitted_keys_by_display.get(item["identity"]) or [
                        comparison_key(item["identity"])
                    ]
                    for identity_key in keys:
                        if identity_key:
                            identity_rows.append((
                                case_id, attempt_id, None, "related_out_of_scope",
                                identity_key, item["identity"], scope_json,
                                item["source_url"], now, scope_id,
                            ))
                self.conn.executemany(
                    "INSERT OR IGNORE INTO evidence_identity(case_id,attempt_id,group_id,"
                    "origin,identity_key,display_value,scope_json,source_ref,created_at,scope_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    identity_rows,
                )

    def list_scope_comparisons(
        self, case_id: int, *, attempt_id: int | None = None
    ) -> list[dict[str, Any]]:
        selected_attempt = attempt_id
        if selected_attempt is None:
            row = self.conn.execute(
                "SELECT MAX(attempt_id) AS attempt_id "
                "FROM evidence_scope_comparison WHERE case_id=?",
                (case_id,),
            ).fetchone()
            selected_attempt = int(row["attempt_id"] or 0) if row else 0
        rows = self.conn.execute(
            "SELECT c.*,s.scope_key,s.role_type,s.role_label,s.required "
            "FROM evidence_scope_comparison c JOIN audit_scope s ON s.id=c.scope_id "
            "WHERE c.case_id=? AND c.attempt_id=? ORDER BY s.required DESC,s.id",
            (case_id, selected_attempt),
        ).fetchall()
        return [{
            "scope_id": int(row["scope_id"]), "scope_key": str(row["scope_key"]),
            "role_type": str(row["role_type"]), "role_label": str(row["role_label"]),
            "required": bool(row["required"]), "status": str(row["status"]),
            "evidence_complete": bool(row["evidence_complete"]),
            "comparison_result": str(row["comparison_result"]),
            "submitted_row_count": int(row["submitted_row_count"]),
            "submitted_identity_count": int(row["submitted_identity_count"]),
            "evidence_identity_count": int(row["evidence_identity_count"]),
            "matched_count": int(row["matched_count"]),
            "missing": json.loads(row["missing_json"] or "[]"),
            "extra": json.loads(row["extra_json"] or "[]"),
            "conflicts": json.loads(row["conflicts_json"] or "[]"),
            "identity_conflicts": json.loads(
                row["verifier_json"] or "{}"
            ).get("identity_conflicts", []),
            "comparison_differences": json.loads(
                row["verifier_json"] or "{}"
            ).get("comparison_differences", []),
            "source_urls": json.loads(
                row["verifier_json"] or "{}"
            ).get("source_urls", []),
            "related_out_of_scope": json.loads(
                row["verifier_json"] or "{}"
            ).get("related_out_of_scope", []),
            "blockers": json.loads(row["blockers_json"] or "[]"),
            "verifier": json.loads(row["verifier_json"] or "{}"),
        } for row in rows]

    def list_evidence_groups(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT g.*,s.scope_key,s.role_type FROM evidence_group g "
            "LEFT JOIN audit_scope s ON s.id=g.scope_id "
            "WHERE g.case_id=? ORDER BY g.attempt_id,g.id", (case_id,)
        ).fetchall()
        return [{
            "group_id": int(row["id"]),
            "attempt_id": int(row["attempt_id"]),
            "group_key": str(row["group_key"]),
            "scope_id": int(row["scope_id"]) if row["scope_id"] is not None else None,
            "scope_key": str(row["scope_key"] or ""),
            "role_type": str(row["role_type"] or ""),
            "parent_url": str(row["parent_url"]),
            "title": str(row["title"]),
            "scope": json.loads(row["scope_json"] or "{}"),
            "status": str(row["status"]),
            "expected_assets": int(row["expected_assets"]),
            "terminal_assets": int(row["terminal_assets"]),
            "extracted_count": int(row["extracted_count"]),
        } for row in rows]

    def latest_evidence_comparison(self, case_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM evidence_comparison WHERE case_id=? "
            "ORDER BY attempt_id DESC,id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "comparison_id": int(row["id"]),
            "attempt_id": int(row["attempt_id"]),
            "group_id": int(row["group_id"]) if row["group_id"] is not None else None,
            "status": str(row["status"]),
            "submitted_count": int(row["submitted_count"]),
            "evidence_count": int(row["evidence_count"]),
            "matched_count": int(row["matched_count"]),
            "missing": json.loads(row["missing_json"] or "[]"),
            "extra": json.loads(row["extra_json"] or "[]"),
            "contradictions": json.loads(row["contradictions_json"] or "[]"),
            "blockers": json.loads(row["blockers_json"] or "[]"),
        }

    def get_audit_case_snapshot(self, case_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM audit_case WHERE id=?", (case_id,)).fetchone()
        if row is None:
            return None
        traces = self.conn.execute(
            "SELECT * FROM tool_trace WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        artifacts = self.conn.execute(
            "SELECT * FROM evidence_artifact WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        latest_verification = self.conn.execute(
            "SELECT * FROM verification_report WHERE case_id=? ORDER BY sequence DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        latest_attempt = self.conn.execute(
            "SELECT id,sequence,status FROM audit_attempt WHERE case_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return {
            "case_id": int(row["id"]),
            "active_attempt_id": (
                int(latest_attempt["id"])
                if latest_attempt is not None and str(latest_attempt["status"]) == "running"
                else 0
            ),
            "attempt_sequence": int(latest_attempt["sequence"]) if latest_attempt else 0,
            "batch_id": int(row["batch_id"]),
            "origin_m4_result_id": int(row["origin_m4_result_id"] or 0),
            "m4_evidence": self._current_m4_evidence_for_case(row),
            "resource_code": str(row["resource_code"]),
            "award_name": str(row["award_name"]),
            "year": str(row["year"]),
            "trigger_codes": json.loads(row["trigger_codes_json"]),
            "objective": str(row["objective"]),
            "submitted_summary": json.loads(row["submitted_summary_json"]),
            "known_urls": json.loads(row["known_urls_json"]),
            "retrieved_memories": json.loads(row["retrieved_memories_json"]),
            "artifacts": [
                {
                    "kind": str(item["kind"]),
                    "source_url": str(item["source_url"]),
                    "local_path": str(item["local_path"]),
                    "content_type": str(item["content_type"]),
                    "sha256": str(item["sha256"]),
                    "size_bytes": int(item["size_bytes"]),
                    "fetched_at": str(item["fetched_at"]),
                    "metadata": json.loads(item["metadata_json"]),
                }
                for item in artifacts
            ],
            "tool_trace": [
                {
                    "call_id": str(item["call_id"]),
                    "tool_name": str(item["tool_name"]),
                    "started_at": str(item["started_at"]),
                    "finished_at": str(item["finished_at"]),
                    "duration_ms": int(item["duration_ms"]),
                    "input_summary": json.loads(item["input_summary_json"]),
                    "output_summary": json.loads(item["output_summary_json"]),
                    "ok": bool(item["ok"]),
                    "error_code": str(item["error_code"]),
                }
                for item in traces
            ],
            "open_questions": json.loads(row["open_questions_json"]),
            "budget": json.loads(row["budget_json"]),
            "step_count": int(row["step_count"]),
            "token_used": int(row["token_used"]),
            "llm_usage": json.loads(row["llm_usage_json"]),
            "verifier_llm_usage": json.loads(row["verifier_llm_usage_json"]),
            "elapsed_ms": int(row["elapsed_ms"]),
            "reflection_count": int(row["reflection_count"]),
            "latest_verification": (
                {
                    "target_match": str(latest_verification["target_match"]),
                    "year_match": str(latest_verification["year_match"]),
                    "source_authority": str(latest_verification["source_authority"]),
                    "coverage_complete": str(latest_verification["coverage_complete"]),
                    "contradictions": json.loads(
                        latest_verification["contradictions_json"]
                    ),
                    "missing_evidence": json.loads(
                        latest_verification["missing_evidence_json"]
                    ),
                    "supplement_requests": json.loads(
                        latest_verification["supplement_requests_json"]
                    ),
                    "recommended_action": str(
                        latest_verification["recommended_action"]
                    ),
                    "reason_codes": json.loads(latest_verification["reason_codes_json"]),
                    "deterministic_action": str(
                        latest_verification["deterministic_action"]
                    ),
                    "model_used": bool(latest_verification["model_used"]),
                }
                if latest_verification is not None
                else None
            ),
            "status": str(row["status"]),
            "recommendation": str(row["recommendation"]),
            "confidence": str(row["confidence"]),
            "reason_codes": json.loads(row["reason_codes_json"]),
            "last_action": json.loads(row["last_action_json"]) or None,
            "last_error": str(row["last_error"]),
            "last_error_detail": str(row["last_error_detail"]),
            "pending_supplement": str(row["pending_supplement"]),
            "evidence_progress": json.loads(row["evidence_progress_json"]),
            "human_decision": str(row["human_decision"]),
            "human_decision_summary": str(row["human_decision_summary"]),
            "reviewed_by": str(row["reviewed_by"]),
            "reviewed_at": str(row["reviewed_at"]),
            "state_version": int(row["state_version"]),
        }

    def _current_m4_evidence_for_case(self, case: sqlite3.Row) -> dict[str, Any] | None:
        origin_id = int(case["origin_m4_result_id"] or 0)
        if origin_id <= 0:
            return None
        stage_item = self.get_stage_item(
            int(case["batch_id"]), str(case["resource_code"]), str(case["year"]), stage="m4"
        )
        if stage_item is None or int(stage_item["current_result_id"] or 0) != origin_id:
            return None
        result = self.get_audit_row(origin_id)
        if result is None:
            return None
        if int(result["batch_id"]) != int(case["batch_id"]):
            return None
        if _norm_zylbm(str(result["resource_code"])) != str(case["resource_code"]):
            return None
        if str(result["year"] or "").strip() != str(case["year"] or "").strip():
            return None

        def json_strings(column: str, limit: int) -> list[str]:
            try:
                raw = json.loads(result[column])
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
            if not isinstance(raw, list):
                return []
            values = [str(item).strip()[:2000] for item in raw if str(item).strip()]
            return list(dict.fromkeys(values))[:limit]

        primary_url = str(result["source_url"] or "").strip()
        source_urls = list(dict.fromkeys([
            *([primary_url] if primary_url else []),
            *json_strings("source_urls_json", 20),
        ]))[:20]
        found_assets = json_strings("found_assets_json", 50)

        def asset_kind(url: str) -> str:
            suffix = Path(unquote(urlsplit(url).path)).suffix.casefold()
            return {
                ".gif": "image", ".jpeg": "image", ".jpg": "image", ".png": "image",
                ".webp": "image", ".pdf": "pdf", ".xls": "xls", ".xlsx": "xlsx",
                ".doc": "document", ".docx": "document",
            }.get(suffix, "unknown")

        def bounded_asset(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            url = str(raw.get("url", "")).strip()[:2048]
            if not url.startswith(("http://", "https://")):
                return None
            parent_url = str(raw.get("parent_url", "")).strip()[:2048]
            if parent_url and not parent_url.startswith(("http://", "https://")):
                parent_url = ""
            status = str(raw.get("status", "discovered"))
            if status not in {"discovered", "downloaded", "parsed", "failed", "access_denied", "skipped"}:
                status = "discovered"
            digest = str(raw.get("sha256", "")).strip().casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                digest = ""
            metadata = raw.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            return {
                "asset_version": 1,
                "url": url,
                "parent_url": parent_url,
                "label": str(raw.get("label", ""))[:500],
                "kind": str(raw.get("kind", "unknown") or "unknown")[:40],
                "status": status,
                "content_type": str(raw.get("content_type", ""))[:200],
                "sha256": digest,
                "size_bytes": max(0, int(raw.get("size_bytes", 0) or 0)),
                "fetched_at": str(raw.get("fetched_at", ""))[:80],
                "local_path": str(raw.get("local_path", ""))[:2048],
                "truncated": bool(raw.get("truncated", False)),
                "extraction_method": str(raw.get("extraction_method", ""))[:100],
                "error_code": str(raw.get("error_code", ""))[:100],
                "error_message": str(raw.get("error_message", ""))[:500],
                "metadata": dict(list(metadata.items())[:30]),
            }

        assets: list[dict[str, Any]] = []
        try:
            raw_assets = json.loads(result["evidence_assets_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_assets = []
        if isinstance(raw_assets, list):
            for raw_asset in raw_assets[:100]:
                asset = bounded_asset(raw_asset)
                if asset is not None and asset not in assets:
                    assets.append(asset)
        if not assets:
            source_pages = [url for url in source_urls if url not in set(found_assets)]
            parent_url = source_pages[0] if len(source_pages) == 1 else ""
            assets = [
                {
                    "asset_version": 1,
                    "url": url,
                    "parent_url": parent_url,
                    "label": "",
                    "kind": asset_kind(url),
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
                    "metadata": {"legacy_fallback": True},
                }
                for url in found_assets
                if url.startswith(("http://", "https://"))
            ]
        return {
            "bundle_version": 1,
            "identity_version": str(result["identity_version"] or "identity-v1")[:40],
            "result_id": origin_id,
            "resource_code": str(result["resource_code"]),
            "award_name": str(result["award_name"] or "")[:200],
            "year": str(result["year"] or "")[:20],
            "page_year": str(result["page_year"] or "")[:20],
            "verdict": str(result["verdict"] or "无法核对")[:100],
            "confidence": str(result["confidence"] or "low")[:20],
            "triage": str(result["triage"] or "manual")[:20],
            "review_status": str(result["review_status"] or "待复核")[:20],
            "source_kind": str(result["source_kind"] or "none")[:40],
            "source_urls": source_urls,
            "found_assets": found_assets,
            "assets": assets,
            "evidence": json_strings("evidence_json", 50),
            "reason_codes": json_strings("reason_codes_json", 50),
            "submitted_count": int(result["submitted_count"] or 0),
            "extracted_count": int(result["extracted_count"] or 0),
            "missing": json_strings("missing_json", 200),
            "extra": json_strings("extra_json", 200),
            "notes": str(result["notes"] or "")[:2000],
        }

    def list_audit_cases(
        self, *, batch_id: int | None = None, status: str = ""
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(self.conn.execute(
            "SELECT * FROM audit_case" + where
            + " ORDER BY updated_at DESC, id DESC", params
        ).fetchall())

    def request_audit_case_supplement(
        self, case_id: int, request: str, *, expected_version: int
    ) -> int:
        bounded = " ".join(request.split()).strip()
        if not bounded or len(bounded) > 1000:
            raise ValueError("supplement request must contain 1-1000 characters")
        new_version = expected_version + 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            case = self.conn.execute(
                "SELECT batch_id FROM audit_case WHERE id=? AND state_version=? "
                "AND status='waiting_human'",
                (case_id, expected_version),
            ).fetchone()
            if case is None:
                raise StateConflictError(
                    "supplement requires the current waiting_human case version"
                )
            batch_id = int(case["batch_id"])
            stage = self.conn.execute(
                "SELECT status FROM batch_stage_run WHERE batch_id=? AND stage='m5'",
                (batch_id,),
            ).fetchone()
            if stage is not None and str(stage["status"]) == "running":
                raise StateConflictError(
                    "cannot request supplement while M5 stage is running"
                )
            cursor = self.conn.execute(
                "UPDATE audit_case SET pending_supplement=?,status='queued',state_version=?,"
                "updated_at=? WHERE id=? AND state_version=? AND status='waiting_human'",
                (bounded, new_version, _now(), case_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "supplement requires the current waiting_human case version"
                )
            ts = _now()
            if stage is None:
                self.conn.execute(
                    "INSERT INTO batch_stage_run(batch_id,stage,status,attempt,state_version,"
                    "updated_at) VALUES (?,'m5','pending',0,1,?)",
                    (batch_id, ts),
                )
            else:
                self.conn.execute(
                    "UPDATE batch_stage_run SET status='pending',lease_owner='',"
                    "lease_expires_at='',error_code='',error_message='',finished_at='',"
                    "state_version=state_version+1,updated_at=? "
                    "WHERE batch_id=? AND stage='m5'",
                    (ts, batch_id),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return new_version

    def finalize_audit_case(
        self,
        case_id: int,
        decision: str,
        summary: str,
        reviewer: str,
        *,
        expected_version: int,
    ) -> int:
        if decision not in {"accepted", "rejected", "insufficient"}:
            raise ValueError("invalid human decision")
        bounded_summary = " ".join(summary.split()).strip()
        bounded_reviewer = " ".join(reviewer.split()).strip()
        if not bounded_summary or len(bounded_summary) > 2000:
            raise ValueError("human decision summary must contain 1-2000 characters")
        if not bounded_reviewer or len(bounded_reviewer) > 200:
            raise ValueError("reviewer must contain 1-200 characters")
        new_version = expected_version + 1
        reviewed_at = _now()
        with self.conn:
            row = self.conn.execute(
                "SELECT origin_m4_result_id FROM audit_case WHERE id=?",
                (case_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"audit case not found: {case_id}")
            if int(row["origin_m4_result_id"] or 0) > 0:
                self.validate_audit_case_m4_binding(case_id)
            cursor = self.conn.execute(
                "UPDATE audit_case SET status='completed',human_decision=?,"
                "human_decision_summary=?,reviewed_by=?,reviewed_at=?,state_version=?,"
                "updated_at=? WHERE id=? AND state_version=? AND status='waiting_human'",
                (
                    decision,
                    bounded_summary,
                    bounded_reviewer,
                    reviewed_at,
                    new_version,
                    reviewed_at,
                    case_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "finalization requires the current waiting_human case version"
                )
        return new_version
