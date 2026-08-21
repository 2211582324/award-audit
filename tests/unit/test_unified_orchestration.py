"""统一审核编排·Phase A 地基单测：迁移数据感知 / 资源项执行状态 / 资源项最终结论 / 入库门禁 / 原子并发 / 导入溯源。

全离线、内存或临时 SQLite；对应 codex 两轮 P0：C1 最终结论解 M4/M5 交叉、C2 只认 current_result_id、
C3 claim+租约、C5 迁移解冲突、C6 阶段执行事实、P0-4 enqueue_once、P0-5 全通过门禁、P0-9 溯源。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from award_audit.core.pipeline import store as store_mod
from award_audit.core.pipeline.store import PromoteBlocked, StateConflictError, Store
from award_audit.web.jobs import JobRepository


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def _srow(key: str, code: str = "04050014", year: str = "2024", status: str = "pass") -> dict:
    return {"file": "f.xlsx", "sheet": "s", "row_no": 1, "table_code": "CON_X",
            "resource_code": code, "year": year, "dedup_key": key,
            "data": {"LWTM": "题", "ZZXM": "张三"}, "check_status": status, "issues": []}


def _arow(code: str = "04050014", year: str = "2024", verdict: str = "一致",
          confidence: str = "high") -> dict:
    return {"resource_code": code, "award_name": "奖", "year": year, "verdict": verdict,
            "confidence": confidence, "source_kind": "excel", "extracted_count": 2,
            "submitted_count": 2, "missing": [], "extra": [], "source_urls": [],
            "found_assets": [], "evidence": [], "notes": ""}


def _seed_context(store: Store, bid: int) -> None:
    store.save_import_context(
        bid,
        source_folder=str(Path.cwd()),
        files=[],
        check_result={"batch": "test", "files": []},
        template_fingerprint="test-template",
        ledger_fingerprint="test-ledger",
    )


def _finish_m4_with_result(
    store: Store,
    bid: int,
    *,
    status: str,
    worker: str = "test",
) -> int:
    result_id = store.add_audit_results(
        bid, [_arow(verdict="无法核对", confidence="low")]
    )[0]
    claim = store.claim_stage_item(bid, "04050014", "2024", worker=worker)
    assert claim is not None
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status=status,
        current_result_id=result_id,
        error_code="NET" if status == "failed" else "NO_SOURCE",
        worker=worker,
        expected_version=int(claim["state_version"]),
    )
    return result_id


# 造一个"应用了 0001-0006、但尚未应用 0007"的旧库，用于测 0007 的数据感知迁移
def _pre_0007_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(store_mod._SCHEMA.read_text("utf-8"))
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for p in sorted(store_mod._MIGRATIONS.glob("*.sql")):
        if p.stem >= "0007_unified_review_orchestration":
            continue
        con.executescript(p.read_text("utf-8"))
        con.execute("INSERT INTO schema_migration(version,applied_at) VALUES (?, '2026-01-01')",
                    (p.stem,))
    con.commit()
    return con


def _insert_case(con: sqlite3.Connection, batch_id: int, code: str, year: str, trigger_key: str) -> None:
    con.execute(
        "INSERT INTO audit_case(batch_id,resource_code,year,trigger_key,objective,status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,'queued','2026-01-01','2026-01-01')",
        (batch_id, code, year, trigger_key, "核验"))


# ---------- C5：0007 数据感知迁移 ----------

# 功能：新库应用 0007 后新表/列/索引齐备、旧案件唯一索引被替换
# 设计：内存库直接由 Store.__init__ 应用全部迁移，断言 3 新表、staging.year、needs_reimport、案件新索引
def test_migration_creates_tables_and_replaces_case_index(store: Store) -> None:
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"batch_stage_run", "audit_stage_item", "batch_import_context"} <= tables
    assert "year" in {r[1] for r in store.conn.execute("PRAGMA table_info(staging_record)")}
    assert "needs_reimport" in {r[1] for r in store.conn.execute("PRAGMA table_info(import_batch)")}
    assert "origin_m4_result_id" in {
        r[1] for r in store.conn.execute("PRAGMA table_info(audit_case)")
    }
    case_idx = {r[1] for r in store.conn.execute("PRAGMA index_list(audit_case)")}
    assert "uq_audit_case_active" in case_idx and "uq_audit_case_active_trigger" not in case_idx


# 功能：旧库已有 staging 行（year 全空、无法可靠推导）→ 0007 标该批 needs_reimport（禁审/禁入库）
# 设计：pre-0007 库插一个批次+一条 staging，Store 打开应用 0007 后断言 needs_reimport=1
def test_migration_marks_old_staged_batch_needs_reimport(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    con.execute("INSERT INTO staging_record(batch_id,file,row_no,table_code,data_json) "
                "VALUES (?,?,1,'X','{}')", (bid, "f.xlsx"))
    con.commit()
    con.close()
    store = Store(db)
    try:
        assert store.needs_reimport(bid) is True
    finally:
        store.close()


def test_migration_backfills_reliable_year_without_reimport(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old-with-year.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    con.execute(
        "INSERT INTO staging_record(batch_id,file,row_no,table_code,data_json) "
        "VALUES (?,?,1,'X',?)",
        (bid, "f.xlsx", '{"ZYLBM":"04050014","PDNY":"2024"}'),
    )
    con.commit()
    con.close()

    store = Store(db)
    try:
        assert store.staging_of(bid)[0]["year"] == "2024"
        assert store.needs_reimport(bid) is False
    finally:
        store.close()


def test_migration_is_version_idempotent_on_reopen(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "reopen.db"
    first = Store(db)
    first.close()
    second = Store(db)
    try:
        applied = second.conn.execute(
            "SELECT COUNT(*) FROM schema_migration "
            "WHERE version='0007_unified_review_orchestration'"
        ).fetchone()[0]
        assert applied == 1
    finally:
        second.close()


# 功能：旧库同 (批,码,年) 有多个活跃案（不同 trigger）→ 0007 合并为一活跃、其余降终态、标 needs_reimport
# 设计：pre-0007 库插两条同 (批,04050014,2024) 不同 trigger 的活跃案，Store 打开后断言活跃案剩一条、批 needs_reimport
def test_migration_resolves_conflicting_active_cases(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    _insert_case(con, bid, "04050014", "2024", "COVERAGE_UNKNOWN")
    _insert_case(con, bid, "04050014", "2024", "PDF_ONLY")
    con.commit()
    con.close()
    store = Store(db)
    try:
        active = store.conn.execute(
            "SELECT COUNT(*) FROM audit_case WHERE batch_id=? AND resource_code='04050014' "
            "AND year='2024' AND status IN ('queued','running','waiting_human')", (bid,)
        ).fetchone()[0]
        assert active == 1  # 冲突已解，只剩一活跃案
        keeper = store.conn.execute(
            "SELECT trigger_codes_json FROM audit_case WHERE batch_id=? "
            "AND status IN ('queued','running','waiting_human')",
            (bid,),
        ).fetchone()
        assert keeper is not None
        assert set(__import__("json").loads(keeper["trigger_codes_json"])) == {
            "COVERAGE_UNKNOWN", "PDF_ONLY",
        }
        assert store.needs_reimport(bid) is False
    finally:
        store.close()


def test_migration_merges_zero_padded_case_identity(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old-codes.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    _insert_case(con, bid, "4050014", "2024", "COVERAGE_UNKNOWN")
    _insert_case(con, bid, "04050014", "2024", "PDF_ONLY")
    con.commit()
    con.close()

    store = Store(db)
    try:
        active = store.list_audit_cases(batch_id=bid, status="queued")
        assert len(active) == 1
        assert active[0]["resource_code"] == "04050014"
    finally:
        store.close()


# 功能：旧库同 (kind,batch) 有重复活跃任务 → 0007 保留最新、其余 cancel，再建唯一索引不失败
# 设计：pre-0007 库插两条同 (audit_batch,bid) 的 queued job，Store 打开后断言活跃仅一条
def test_migration_dedups_active_jobs(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    for _ in range(2):
        con.execute("INSERT INTO audit_job(kind,batch_id,status,created_by,created_at,updated_at) "
                    "VALUES ('audit_batch',?,'queued','r','2026-01-01','2026-01-01')", (bid,))
    con.commit()
    con.close()
    store = Store(db)
    try:
        active = store.conn.execute(
            "SELECT COUNT(*) FROM audit_job WHERE batch_id=? AND kind='audit_batch' "
            "AND status IN ('queued','running')", (bid,)).fetchone()[0]
        assert active == 1
    finally:
        store.close()


def test_migration_cancels_cross_stage_job_conflict(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old-jobs.db"
    con = _pre_0007_db(db)
    con.execute("INSERT INTO import_batch(name,imported_at) VALUES ('旧批','2026-01-01')")
    bid = int(con.execute("SELECT id FROM import_batch").fetchone()["id"])
    for kind in ("audit_batch", "review_batch"):
        con.execute(
            "INSERT INTO audit_job(kind,batch_id,status,created_by,created_at,updated_at) "
            "VALUES (?,?,'queued','r','2026-01-01','2026-01-01')",
            (kind, bid),
        )
    con.commit()
    con.close()

    store = Store(db)
    try:
        rows = store.conn.execute(
            "SELECT status FROM audit_job WHERE batch_id=? ORDER BY id", (bid,)
        ).fetchall()
        assert [row["status"] for row in rows] == ["cancelled", "queued"]
    finally:
        store.close()


# ---------- C3/C6：阶段执行状态 + 原子 claim + 租约 ----------

# 功能：资源项 claim 幂等且按 (码,年) 隔离——done 跳过、running 被占、同码异年各自独立
# 设计：claim→running；同项再 claim 返 None；finish done 后再 claim 返 None；异年 claim 成功
def test_stage_item_claim_idempotent_and_year_scoped(store: Store) -> None:
    bid = store.create_batch("t")
    claim = store.claim_stage_item(bid, "04030060", "2023", worker="test")
    assert claim is not None
    assert store.claim_stage_item(bid, "04030060", "2023") is None  # running 被占
    store.finish_stage_item(
        bid, "04030060", "2023", status="done", worker="test",
        expected_version=int(claim["state_version"]),
    )
    assert store.claim_stage_item(bid, "04030060", "2023") is None  # done 幂等跳过
    assert store.claim_stage_item(bid, "04030060", "2025") is not None  # 同码异年独立，不塌缩


# 功能：failed（如瞬时网络失败）可续跑；只认 current_result_id，旧"无法核对"不永久挡后来成功
# 设计：先 failed 链旧结果，再续跑 done 链新结果+人工通过，断言最终结论 m4_accepted 且旧结果仍在（审计）
def test_stage_item_failed_reclaimable_and_current_result_only(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    store.add_audit_results(bid, [_arow(verdict="无法核对", confidence="low")])
    rid_old = int(store.audit_results_of(bid)[-1]["id"])
    first = store.claim_stage_item(bid, "04050014", "2024", worker="test-1")
    assert first is not None
    store.finish_stage_item(
        bid, "04050014", "2024", status="failed", current_result_id=rid_old,
        error_code="NET", worker="test-1", expected_version=int(first["state_version"]),
    )
    second = store.claim_stage_item(bid, "04050014", "2024", worker="test-2")
    assert second is not None  # failed 可续跑
    store.add_audit_results(bid, [_arow(verdict="一致", confidence="high")])
    rid_new = int(store.audit_results_of(bid)[-1]["id"])
    store.finish_stage_item(
        bid, "04050014", "2024", status="done", current_result_id=rid_new,
        worker="test-2", expected_version=int(second["state_version"]),
    )
    store.set_audit_review(rid_new, "通过")
    assert store.resource_conclusions(bid) == {("04050014", "2024"): "m4_accepted"}
    assert len(store.audit_results_of(bid)) == 2  # 旧结果保留为审计，不参与门禁


# 功能：过期 running 的资源项被回收为 failed（可续跑），避免死锁
# 设计：claim 后把租约置为明确的过去时间，recover_expired_stage_items 后状态 failed
def test_stage_item_expired_lease_recovered(store: Store) -> None:
    bid = store.create_batch("t")
    store.claim_stage_item(bid, "04050014", "2024")
    store.conn.execute(
        "UPDATE audit_stage_item SET lease_expires_at='2000-01-01T00:00:00.000+00:00' "
        "WHERE batch_id=?", (bid,))
    store.conn.commit()
    assert store.recover_expired_stage_items() == 1
    assert store.get_stage_item(bid, "04050014", "2024")["status"] == "failed"


def test_stage_item_claim_is_atomic_across_connections(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "claim.db"
    seed = Store(db)
    bid = seed.create_batch("t")
    seed.close()

    def claim(worker: str) -> bool:
        candidate = Store(db)
        try:
            return candidate.claim_stage_item(
                bid, "04050014", "2024", worker=worker
            ) is not None
        finally:
            candidate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        won = list(pool.map(claim, ("w1", "w2")))
    assert sorted(won) == [False, True]


def test_stale_stage_item_worker_cannot_finish(store: Store) -> None:
    bid = store.create_batch("t")
    claim = store.claim_stage_item(bid, "04050014", "2024", worker="w1")
    assert claim is not None
    with pytest.raises(RuntimeError):
        store.finish_stage_item(
            bid,
            "04050014",
            "2024",
            status="done",
            worker="stale",
            expected_version=int(claim["state_version"]),
        )


def test_failed_retry_clears_previous_current_result(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    store.add_audit_results(bid, [_arow()])
    rid = int(store.audit_results_of(bid)[-1]["id"])
    first = store.claim_stage_item(bid, "04050014", "2024", worker="w1")
    assert first is not None
    store.finish_stage_item(
        bid, "04050014", "2024", status="done", current_result_id=rid,
        worker="w1", expected_version=int(first["state_version"]),
    )
    store.set_audit_review(rid, "通过")
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "m4_accepted"

    store.conn.execute("UPDATE audit_stage_item SET status='failed' WHERE batch_id=?", (bid,))
    store.conn.commit()
    retry = store.claim_stage_item(bid, "04050014", "2024", worker="w2")
    assert retry is not None
    store.finish_stage_item(
        bid, "04050014", "2024", status="failed", error_code="NET",
        worker="w2", expected_version=int(retry["state_version"]),
    )
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "pending"


# 功能：批次阶段互斥——同批 M4 running 时 M5 不能 claim（禁交叉），CLI 直跑与 Web 都过此闸
# 设计：claim m4 成功；claim m5 返 None；finish m4 后 claim m5 成功
def test_batch_stage_m4_m5_mutex(store: Store) -> None:
    bid = store.create_batch("t")
    claim = store.claim_batch_stage(bid, "m4", worker="w1")
    assert claim is not None
    assert store.claim_batch_stage(bid, "m5", worker="w2") is None  # 交叉被禁
    store.finish_batch_stage(
        bid, "m4", "done", worker="w1", expected_version=int(claim["state_version"])
    )
    assert store.claim_batch_stage(bid, "m5", worker="w2") is not None


def test_batch_stage_claim_is_atomic_across_connections(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "batch-claim.db"
    seed = Store(db)
    bid = seed.create_batch("t")
    seed.close()

    def claim(stage: str) -> bool:
        candidate = Store(db)
        try:
            return candidate.claim_batch_stage(bid, stage, worker=stage) is not None
        finally:
            candidate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        won = list(pool.map(claim, ("m4", "m5")))
    assert sorted(won) == [False, True]


def test_stale_batch_stage_worker_cannot_finish(store: Store) -> None:
    bid = store.create_batch("t")
    claim = store.claim_batch_stage(bid, "m4", worker="w1")
    assert claim is not None
    with pytest.raises(RuntimeError):
        store.finish_batch_stage(
            bid, "m4", "done", worker="stale",
            expected_version=int(claim["state_version"]),
        )


# ---------- C1：资源项最终结论解 M4/M5 交叉 ----------

# 功能：M5 人工 accepted 解对应 (码,年) 的 M4 failed（否则批次永远入不了库）
# 设计：M4 failed 无接受结果 + M5 案人工 accepted，断言该资源项结论 m5_accepted
def test_m5_accept_resolves_m4_failure(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    result_id = _finish_m4_with_result(store, bid, status="failed")
    store.create_or_get_audit_case({"batch_id": bid, "resource_code": "04050014",
                                    "year": "2024", "trigger_codes": ["COVERAGE_UNKNOWN"],
                                    "objective": "核验",
                                    "origin_m4_result_id": result_id})
    cid = int(store.list_audit_cases(batch_id=bid)[0]["id"])
    store.conn.execute("UPDATE audit_case SET status='waiting_human' WHERE id=?", (cid,))
    store.conn.commit()
    sv = int(store.list_audit_cases(batch_id=bid)[0]["state_version"])
    store.finalize_audit_case(cid, "accepted", "人工确认可入库", "复核员", expected_version=sv)
    assert store.resource_conclusions(bid) == {("04050014", "2024"): "m5_accepted"}


def test_m5_accept_resolves_m4_skipped_and_rejected_still_blocks(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    result_id = _finish_m4_with_result(store, bid, status="skipped")
    cid, _ = store.create_or_get_audit_case({
        "batch_id": bid,
        "resource_code": "04050014",
        "year": "2024",
        "trigger_codes": ["NO_SOURCE"],
        "objective": "核验",
        "origin_m4_result_id": result_id,
    })
    store.conn.execute("UPDATE audit_case SET status='waiting_human' WHERE id=?", (cid,))
    store.conn.commit()
    version = int(store.get_audit_case_snapshot(cid)["state_version"])
    store.finalize_audit_case(
        cid, "rejected", "人工确认不通过", "复核员", expected_version=version,
    )
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "rejected"


def test_create_case_merges_zero_padded_resource_code(store: Store) -> None:
    bid = store.create_batch("t")
    first, created = store.create_or_get_audit_case({
        "batch_id": bid, "resource_code": "4050014", "year": "2024",
        "trigger_codes": ["A"], "objective": "核验",
    })
    second, created_again = store.create_or_get_audit_case({
        "batch_id": bid, "resource_code": "04050014", "year": "2024",
        "trigger_codes": ["B"], "objective": "核验",
    })
    assert first == second and created is True and created_again is False
    row = store.list_audit_cases(batch_id=bid)[0]
    assert row["resource_code"] == "04050014"
    assert set(__import__("json").loads(row["trigger_codes_json"])) == {"A", "B"}


# 功能：M5 案 rejected/insufficient 或活跃未终审 → 该资源项不可入库（pending/rejected）
# 设计：活跃 M5 案 → pending；rejected → rejected；均不在 {m4_accepted,m5_accepted}
def test_active_or_rejected_case_blocks(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    result_id = _finish_m4_with_result(store, bid, status="failed")
    store.create_or_get_audit_case({"batch_id": bid, "resource_code": "04050014",
                                    "year": "2024", "trigger_codes": ["PDF_ONLY"],
                                    "objective": "核验",
                                    "origin_m4_result_id": result_id})
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "pending"  # 活跃未终审
    cid = int(store.list_audit_cases(batch_id=bid)[0]["id"])
    store.conn.execute("UPDATE audit_case SET status='waiting_human' WHERE id=?", (cid,))
    store.conn.commit()
    sv = int(store.list_audit_cases(batch_id=bid)[0]["state_version"])
    store.finalize_audit_case(cid, "insufficient", "证据不足", "复核员", expected_version=sv)
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "insufficient"


def test_stale_m5_result_is_ignored_after_m4_retry(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    old_result_id = _finish_m4_with_result(store, bid, status="failed", worker="m4-old")
    case_id, _ = store.create_or_get_audit_case({
        "batch_id": bid,
        "resource_code": "04050014",
        "year": "2024",
        "trigger_codes": ["COVERAGE_UNKNOWN"],
        "objective": "核验",
        "origin_m4_result_id": old_result_id,
    })
    store.conn.execute(
        "UPDATE audit_case SET status='waiting_human' WHERE id=?", (case_id,)
    )
    store.conn.commit()
    version = int(store.get_audit_case_snapshot(case_id)["state_version"])
    store.finalize_audit_case(
        case_id, "accepted", "基于旧 M4 证据通过", "复核员", expected_version=version
    )
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "m5_accepted"

    retry = store.claim_stage_item(bid, "04050014", "2024", worker="m4-new")
    assert retry is not None
    new_result_id = store.add_audit_results(
        bid, [_arow(verdict="无法核对", confidence="low")]
    )[0]
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status="failed",
        current_result_id=new_result_id,
        error_code="NET",
        worker="m4-new",
        expected_version=int(retry["state_version"]),
    )
    assert store.resource_conclusions(bid)[("04050014", "2024")] == "pending"


def test_stale_bound_case_cannot_be_finalized(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    old_result_id = _finish_m4_with_result(store, bid, status="failed", worker="m4-old")
    case_id, _ = store.create_or_get_audit_case({
        "batch_id": bid,
        "resource_code": "04050014",
        "year": "2024",
        "trigger_codes": ["COVERAGE_UNKNOWN"],
        "objective": "核验",
        "origin_m4_result_id": old_result_id,
    })
    store.conn.execute(
        "UPDATE audit_case SET status='waiting_human' WHERE id=?", (case_id,)
    )
    store.conn.commit()
    version = int(store.get_audit_case_snapshot(case_id)["state_version"])

    retry = store.claim_stage_item(bid, "04050014", "2024", worker="m4-new")
    assert retry is not None
    new_result_id = store.add_audit_results(
        bid, [_arow(verdict="无法核对", confidence="low")]
    )[0]
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status="failed",
        current_result_id=new_result_id,
        error_code="NET",
        worker="m4-new",
        expected_version=int(retry["state_version"]),
    )

    with pytest.raises(StateConflictError, match="M4"):
        store.finalize_audit_case(
            case_id, "accepted", "不应使用旧证据", "复核员", expected_version=version
        )


def test_bound_case_snapshot_restores_current_m4_evidence_bundle(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    report = _arow(verdict="无法核对", confidence="low")
    report.update({
        "source_kind": "pdf",
        "source_url": "https://official.example/list.pdf",
        "source_urls": ["https://official.example/page"],
        "found_assets": ["https://official.example/list.pdf"],
        "evidence_assets": [{
            "asset_version": 1,
            "url": "https://official.example/list.pdf",
            "parent_url": "https://official.example/page",
            "label": "获奖名单",
            "kind": "pdf",
            "status": "parsed",
            "content_type": "application/pdf",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "fetched_at": "2026-08-01T00:00:00+00:00",
            "local_path": "C:/tmp/list.pdf",
            "truncated": False,
            "extraction_method": "pdf_text",
            "error_code": "",
            "error_message": "",
            "metadata": {"page_count": 2},
        }],
        "evidence": ["已读取官方页面和名单附件"],
        "reason_codes": ["coverage_unknown"],
        "missing": ["名单甲"],
        "extra": ["提交乙"],
        "page_year": "2024",
    })
    result_id = store.add_audit_results(bid, [report])[0]
    claim = store.claim_stage_item(bid, "04050014", "2024", worker="m4")
    assert claim is not None
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status="failed",
        current_result_id=result_id,
        error_code="HANDOFF",
        worker="m4",
        expected_version=int(claim["state_version"]),
    )
    case_id, _ = store.create_or_get_audit_case({
        "batch_id": bid,
        "resource_code": "04050014",
        "year": "2024",
        "trigger_codes": ["PDF_ONLY"],
        "objective": "核验完整名单",
        "origin_m4_result_id": result_id,
    })

    bundle = store.get_audit_case_snapshot(case_id)["m4_evidence"]
    assert bundle["bundle_version"] == 1
    assert bundle["identity_version"] == "identity-v1"
    assert bundle["result_id"] == result_id
    assert bundle["source_urls"] == [
        "https://official.example/list.pdf",
        "https://official.example/page",
    ]
    assert bundle["found_assets"] == ["https://official.example/list.pdf"]
    assert bundle["assets"][0]["parent_url"] == "https://official.example/page"
    assert bundle["assets"][0]["sha256"] == "a" * 64
    assert bundle["missing"] == ["名单甲"]
    assert bundle["extra"] == ["提交乙"]

    retry = store.claim_stage_item(bid, "04050014", "2024", worker="m4-retry")
    assert retry is not None
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status="failed",
        error_code="RETRY",
        worker="m4-retry",
        expected_version=int(retry["state_version"]),
    )
    assert store.get_audit_case_snapshot(case_id)["m4_evidence"] is None


def test_bound_case_snapshot_synthesizes_fail_closed_assets_for_legacy_m4_result(
    store: Store,
) -> None:
    bid = store.create_batch("legacy")
    store.add_staging(bid, [_srow("K1")])
    report = _arow(verdict="无法核对", confidence="low")
    report.update({
        "source_url": "https://official.example/page",
        "source_urls": ["https://official.example/page"],
        "found_assets": ["https://official.example/list.xlsx"],
    })
    result_id = store.add_audit_results(bid, [report])[0]
    claim = store.claim_stage_item(bid, "04050014", "2024", worker="m4")
    assert claim is not None
    store.finish_stage_item(
        bid,
        "04050014",
        "2024",
        status="failed",
        current_result_id=result_id,
        error_code="HANDOFF",
        worker="m4",
        expected_version=int(claim["state_version"]),
    )
    case_id, _ = store.create_or_get_audit_case({
        "batch_id": bid,
        "resource_code": "04050014",
        "year": "2024",
        "trigger_codes": ["NO_LIST"],
        "objective": "核验完整名单",
        "origin_m4_result_id": result_id,
    })

    bundle = store.get_audit_case_snapshot(case_id)["m4_evidence"]
    assert bundle["assets"] == [{
        "asset_version": 1,
        "url": "https://official.example/list.xlsx",
        "parent_url": "https://official.example/page",
        "label": "",
        "kind": "xlsx",
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
    }]


# ---------- P0-5：入库门禁（整批全通过·同事务·认当前结果） ----------

# 功能：门禁在 fail 行 / 未达结论 / 活跃任务 / running 阶段 / needs_reimport 各条件下阻断，全通过才放行
# 设计：逐个制造条件断言 gate 非空、promote 抛 PromoteBlocked；最后全接受断言 gate 空、promote 成功
def test_promotion_gate_conditions(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1"), _srow("K2", status="fail")])
    assert store.promotion_gate(bid)  # fail 行 + 未审核 → 阻断
    with pytest.raises(PromoteBlocked):
        store.promote_batch(bid)
    # 清 fail 行、全接受资源项
    store2 = Store(":memory:")
    bid2 = store2.create_batch("t2")
    store2.add_staging(bid2, [_srow("K1")])
    _seed_context(store2, bid2)
    store2.add_audit_results(bid2, [_arow()])
    rid = int(store2.audit_results_of(bid2)[-1]["id"])
    claim = store2.claim_stage_item(bid2, "04050014", "2024", worker="test")
    assert claim is not None
    store2.finish_stage_item(
        bid2, "04050014", "2024", status="done", current_result_id=rid,
        worker="test", expected_version=int(claim["state_version"]),
    )
    store2.set_audit_review(rid, "通过")
    assert store2.promotion_gate(bid2) == []  # 全通过
    assert store2.promote_batch(bid2)["inserted"] == 1
    store2.close()


# 功能：needs_reimport 批次直接被门禁拦下（fail-closed）
# 设计：标 needs_reimport 后即便无 staging，gate 也含"需重新导入"
def test_needs_reimport_blocks_promotion(store: Store) -> None:
    bid = store.create_batch("t")
    store.mark_needs_reimport(bid)
    assert any("需重新导入" in r for r in store.promotion_gate(bid))


# ---------- P0-4：enqueue_once 原子防重 ----------

# 功能：同 (kind,batch) 并发入队只入一个活跃 job（撞唯一索引返既有）
# 设计：enqueue_once 两次，断言返回同一 job_id、库中活跃仅一条
def test_enqueue_once_dedups_same_kind(store: Store) -> None:
    bid = store.create_batch("t")
    jobs = JobRepository(store)
    j1 = jobs.enqueue_once("audit_batch", {"batch_id": bid}, created_by="r", batch_id=bid)
    j2 = jobs.enqueue_once("audit_batch", {"batch_id": bid}, created_by="r", batch_id=bid)
    assert j1.job_id == j2.job_id
    n = store.conn.execute("SELECT COUNT(*) FROM audit_job WHERE batch_id=? AND kind='audit_batch' "
                           "AND status IN ('queued','running')", (bid,)).fetchone()[0]
    assert n == 1


# 功能：同批 M4/M5 任务互斥——已有活跃 audit_batch 时入队 review_batch 返回既有 audit_batch（禁交叉）
# 设计：enqueue_once audit_batch 后 enqueue_once review_batch，断言返回的 kind 仍是 audit_batch
def test_enqueue_once_mutex_across_stage_jobs(store: Store) -> None:
    bid = store.create_batch("t")
    jobs = JobRepository(store)
    jobs.enqueue_once("audit_batch", {"batch_id": bid}, created_by="r", batch_id=bid)
    got = jobs.enqueue_once("review_batch", {"batch_id": bid}, created_by="r", batch_id=bid)
    assert got.kind == "audit_batch"  # 被 uq_active_batch_stage 拦下，返回既有活跃阶段任务


# ---------- P0-9：导入溯源存取 ----------

# 功能：导入溯源存取往返——源目录/文件哈希/指纹落库读回
# 设计：save_import_context 后 load 断言字段一致
def test_import_context_roundtrip(store: Store) -> None:
    bid = store.create_batch("t")
    store.save_import_context(
        bid, source_folder="/data/提交-X",
        files=[{"file_name": "a.xlsx", "path": "/data/提交-X/a.xlsx", "sha256": "abc"}],
        check_result={"batch": "提交-X", "files": []},
        template_fingerprint="tf", ledger_fingerprint="lf")
    ctx = store.get_import_context(bid)
    assert ctx is not None
    assert ctx["source_folder"] == "/data/提交-X"
    assert ctx["template_fingerprint"] == "tf" and ctx["ledger_fingerprint"] == "lf"


def test_import_context_validates_hash_roots_version_and_fingerprints(
    store: Store, tmp_path: Path,
) -> None:
    source = tmp_path / "submission"
    source.mkdir()
    material = source / "a.xlsx"
    material.write_bytes(b"original")
    bid = store.create_batch("t")
    from award_audit.core.pipeline import provenance

    store.save_import_context(
        bid,
        source_folder=str(source),
        files=[{
            "file_name": material.name,
            "path": str(material),
            "sha256": provenance.file_sha256(material),
        }],
        check_result={"batch": "t", "files": []},
        template_fingerprint="tf",
        ledger_fingerprint="lf",
    )
    loaded = store.load_import_context(
        bid,
        allowed_roots=[tmp_path],
        template_fingerprint="tf",
        ledger_fingerprint="lf",
        context_version=provenance.CONTEXT_VERSION,
    )
    assert loaded is not None

    material.write_bytes(b"changed")
    with pytest.raises(RuntimeError):
        store.load_import_context(
            bid,
            allowed_roots=[tmp_path],
            template_fingerprint="tf",
            ledger_fingerprint="lf",
            context_version=provenance.CONTEXT_VERSION,
        )
    material.write_bytes(b"original")
    for kwargs in (
        {"allowed_roots": [tmp_path / "elsewhere"], "template_fingerprint": "tf",
         "ledger_fingerprint": "lf", "context_version": provenance.CONTEXT_VERSION},
        {"allowed_roots": [tmp_path], "template_fingerprint": "other",
         "ledger_fingerprint": "lf", "context_version": provenance.CONTEXT_VERSION},
        {"allowed_roots": [tmp_path], "template_fingerprint": "tf",
         "ledger_fingerprint": "other", "context_version": provenance.CONTEXT_VERSION},
        {"allowed_roots": [tmp_path], "template_fingerprint": "tf",
         "ledger_fingerprint": "lf", "context_version": 999},
    ):
        with pytest.raises(RuntimeError):
            store.load_import_context(bid, **kwargs)


def test_promotion_gate_blocks_missing_import_context(store: Store) -> None:
    bid = store.create_batch("t")
    store.add_staging(bid, [_srow("K1")])
    assert any("导入上下文" in reason for reason in store.promotion_gate(bid))
