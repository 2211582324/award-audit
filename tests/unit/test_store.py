"""版本化台账 Store 的单元测试（内存 SQLite，覆盖批次→暂存→入库→勘误→回滚→审计全生命周期）。

入库门禁升级为"整批全通过"（M5.7 收口）：每个 (归一码,年) 资源项须达 m4_accepted/m5_accepted，
且无 fail/打回行、无活跃任务/阶段、非 needs_reimport，否则 promote 抛 PromoteBlocked（零写入）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from award_audit.core.pipeline.store import PromoteBlocked, Store


# 造一个内存库
@pytest.fixture
def store() -> Store:
    return Store(":memory:")


# 造暂存行
def _srow(key: str, status: str = "pass", title: str = "论文A", code: str = "04050014",
          year: str = "2025") -> dict:
    return {
        "file": "f.xlsx", "sheet": "s", "row_no": 1, "table_code": "CON_X",
        "resource_code": code, "year": year, "dedup_key": key,
        "data": {"LWTM": title, "ZZXM": "张三"}, "check_status": status, "issues": [],
    }


# 造一条联网核对结论（EvidenceReport.model_dump() 的形态）
def _arow(code: str = "04030058", verdict: str = "一致", **kw: object) -> dict:
    base: dict = {
        "resource_code": code, "award_name": "测试奖", "year": "2025", "verdict": verdict,
        "confidence": "high", "source_kind": "excel", "extracted_count": 2, "submitted_count": 2,
        "missing": [], "extra": [], "source_urls": ["https://gov/list"], "found_assets": [],
        "evidence": ["证据1"], "notes": "",
    }
    base.update(kw)
    return base


# 把某批次所有 (归一码,年) 资源项标记为 m4_accepted：造 audit_result + stage_item 链 current_result_id + 人工"通过"。
# 真实流程由 run_audit_stage + 人工复核完成；这里是让"整批全通过"门禁放行的单测捷径。
def _seed_context(store: Store, bid: int) -> None:
    if store.get_import_context(bid) is None:
        store.save_import_context(
            bid,
            source_folder=str(Path.cwd()),
            files=[],
            check_result={"batch": "test", "files": []},
            template_fingerprint="test-template",
            ledger_fingerprint="test-ledger",
        )


def _accept_all(store: Store, bid: int, verdict: str = "一致", confidence: str = "high") -> None:
    _seed_context(store, bid)
    seen: set[tuple[str, str]] = set()
    for r in store.staging_of(bid):
        code = str(r["resource_code"] or "")
        norm = code.zfill(8) if code.isdigit() else code
        year = str(r["year"] or "")
        if not norm or (norm, year) in seen:
            continue
        seen.add((norm, year))
        store.add_audit_results(bid, [_arow(norm, verdict, year=year, confidence=confidence)])
        rid = int(store.audit_results_of(bid)[-1]["id"])
        claim = store.claim_stage_item(bid, norm, year, worker="test")
        assert claim is not None
        store.finish_stage_item(
            bid, norm, year, status="done", current_result_id=rid,
            worker="test", expected_version=int(claim["state_version"]),
        )
        store.set_audit_review(rid, "通过")


# 功能：验证批次创建→计数→状态流转的基本生命周期
# 设计：断言新批次默认"暂存"、更新后状态与计数落库，覆盖台账最小闭环
def test_batch_lifecycle(store: Store) -> None:
    bid = store.create_batch("提交-T")
    b = store.get_batch(bid)
    assert b is not None and b["status"] == "暂存"
    store.update_batch_counts(bid, 2, 10)
    store.set_batch_status(bid, "审核中")
    b2 = store.get_batch(bid)
    assert b2 is not None and (b2["n_files"], b2["n_rows"], b2["status"]) == (2, 10, "审核中")


# 功能：验证新版整批门禁——存在 fail 行则整批 PromoteBlocked、零写入（即便资源项已接受）
# 设计：pass+fail 两行、全部资源项标接受，仍因 fail 行整批拦下（保守政务门禁）
def test_promote_blocks_on_fail_row(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1"), _srow("K3", status="fail")])
    _accept_all(store, bid)
    with pytest.raises(PromoteBlocked):
        store.promote_batch(bid)
    assert store.current_keys() == set()  # 零写入
    b = store.get_batch(bid)
    assert b is not None and b["status"] != "已入库"


# 功能：验证全通过后 promote 入库，且批内重复键只入一次
# 设计：pass+warn+同键重复三行，全部资源项接受后 promote，断言 inserted=2/skipped_dup=1、状态已入库
def test_promote_all_pass_dedup(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1"), _srow("K2", status="warn"), _srow("K1")])
    _accept_all(store, bid)
    stats = store.promote_batch(bid)
    assert stats["inserted"] == 2 and stats["skipped_dup"] == 1
    assert store.current_keys() == {"K1", "K2"}
    b = store.get_batch(bid)
    assert b is not None and b["status"] == "已入库"


# 功能：验证逐行人工"打回"整批拦下 promote（零写入），且复核字段落库
# 设计：两条 pass 全接受，再打回第 1 条，断言 promote 抛 PromoteBlocked、正式库空、打回字段已写
def test_promote_blocks_on_rejected_row(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1"), _srow("K2")])
    _accept_all(store, bid)
    first = store.staging_of(bid)[0]
    store.set_review_status(first["id"], "打回", reviewer="质检员")
    with pytest.raises(PromoteBlocked):
        store.promote_batch(bid)
    assert store.current_keys() == set()
    row = store.get_staging_row(first["id"])
    assert row is not None and row["review_status"] == "打回" and row["reviewer"] == "质检员"


# 功能：验证跨批次去重——第二批同键记录 promote 时被跳过，不入库两次
# 设计：两批各接受后 promote 同键 K1，断言第二次 inserted=0/skipped_dup=1，正式库仍只一条当前记录
def test_promote_cross_batch_dedup(store: Store) -> None:
    b1 = store.create_batch("提交-1")
    store.add_staging(b1, [_srow("K1")])
    _accept_all(store, b1)
    store.promote_batch(b1)
    b2 = store.create_batch("提交-2")
    store.add_staging(b2, [_srow("K1")])
    _accept_all(store, b2)
    stats = store.promote_batch(b2)
    assert stats["inserted"] == 0 and stats["skipped_dup"] == 1
    assert len(store.history("K1")) == 1


# 功能：验证勘误=新版本+旧版本失效+审计记字段级 diff（永不物理覆盖）
# 设计：接受并入库后 correct 改题目，断言 v2 为当前、v1 失效有 valid_to，audit 的 diff 含 LWTM 的 old/new
def test_correct_versioning_and_audit(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1", title="旧题目")])
    _accept_all(store, bid)
    store.promote_batch(bid)
    store.correct("K1", {"LWTM": "新题目", "ZZXM": "张三"}, reason="官网核对更正", operator="质检员")
    hist = store.history("K1")
    assert [h["version"] for h in hist] == [1, 2]
    assert hist[0]["is_current"] == 0 and hist[0]["valid_to"] is not None
    assert hist[1]["is_current"] == 1
    audits = store.audit_of("K1")
    assert [a["action"] for a in audits] == ["create", "correct"]
    diff = json.loads(audits[-1]["diff_json"])
    assert diff["LWTM"] == {"old": "旧题目", "new": "新题目"}


# 功能：验证回滚——历史版本可重新设为当前，且留审计
# 设计：接受入库后勘误到 v2，再 rollback 到 v1，断言 v1 重新 is_current、审计含 rollback 动作
def test_rollback(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1", title="v1数据")])
    _accept_all(store, bid)
    store.promote_batch(bid)
    store.correct("K1", {"LWTM": "v2数据", "ZZXM": "张三"}, reason="误改")
    store.rollback("K1", to_version=1)
    hist = {h["version"]: h for h in store.history("K1")}
    assert hist[1]["is_current"] == 1 and hist[2]["is_current"] == 0
    assert store.audit_of("K1")[-1]["action"] == "rollback"


# 功能：验证对不存在的键勘误/回滚会明确报错而非静默
# 设计：空库上 correct/rollback 断言抛 ValueError，守住误操作边界
def test_correct_rollback_errors(store: Store) -> None:
    with pytest.raises(ValueError):
        store.correct("NOPE", {}, reason="x")
    with pytest.raises(ValueError):
        store.rollback("NOPE", 1)


# 功能：验证联网核对结论存取——find_or_create_batch 复用同名、结论落库读回、复核状态与人/时间写入
# 设计：同名批次二次调用返回同 id；写两条结论读回；复核一条为"通过"，断言状态/reviewer/时间落库，
#       且"无法核对"那条的 found_assets（人工入口）完整入库——L5 结论进复核台的最小闭环
def test_audit_result_store_and_review(store: Store) -> None:
    bid = store.find_or_create_batch("提交-13")
    assert store.find_or_create_batch("提交-13") == bid  # 复用同名，不新建
    store.add_audit_results(bid, [
        _arow("04030058", "基本一致（需人工抽核）", confidence="medium"),
        _arow("04050014", "无法核对", source_kind="none",
              found_assets=["https://gov/a.pdf"], source_urls=["https://gov/p"]),
    ])
    rows = store.audit_results_of(bid)
    assert len(rows) == 2
    by_code = {r["resource_code"]: r for r in rows}
    assert by_code["04030058"]["verdict"] == "基本一致（需人工抽核）" and by_code["04030058"]["confidence"] == "medium"
    assert json.loads(by_code["04050014"]["found_assets_json"]) == ["https://gov/a.pdf"]  # 人工入口入库
    assert by_code["04050014"]["review_status"] == "待复核"
    aid = by_code["04050014"]["id"]
    store.set_audit_review(aid, "通过", reviewer="复核员A")
    row = store.get_audit_row(aid)
    assert row is not None and row["review_status"] == "通过" and row["reviewer"] == "复核员A" and row["reviewed_at"]


# 功能：验证 reason_codes 随结论落库、triage 由 verdict×confidence 派生入库（可 SQL 按分诊排队）
# 设计：写"一致×high"与"无法核对×low"两条，断言 triage=auto_pass/manual、reason_codes_json 原样读回
def test_audit_result_triage_and_reasons_persist(store: Store) -> None:
    bid = store.find_or_create_batch("提交-T")
    store.add_audit_results(bid, [
        _arow("04030058", "一致", confidence="high", reason_codes=["multi_source"]),
        _arow("04050014", "无法核对", confidence="low", reason_codes=["no_list"]),
    ])
    by = {r["resource_code"]: r for r in store.audit_results_of(bid)}
    assert by["04030058"]["triage"] == "auto_pass"
    assert json.loads(by["04030058"]["reason_codes_json"]) == ["multi_source"]
    assert by["04050014"]["triage"] == "manual"
    assert json.loads(by["04050014"]["reason_codes_json"]) == ["no_list"]


# 功能：验证既有库（audit_result 缺 triage/reason_codes_json 两列）经 Store.__init__ 幂等补列后可正常写读
# 设计：手造"旧版"audit_result（无新列），Store 打开触发迁移，断言两列补齐 + add_audit_results 落库、triage 兜底算对
def test_store_migrates_missing_audit_columns(tmp_path) -> None:  # noqa: ANN001
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(  # ② 之前的 audit_result：无 triage/reason_codes_json
        "CREATE TABLE audit_result(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,"
        " resource_code TEXT NOT NULL, award_name TEXT, year TEXT, verdict TEXT, confidence TEXT,"
        " source_kind TEXT, source_url TEXT, page_year TEXT, extracted_count INTEGER, submitted_count INTEGER,"
        " missing_json TEXT, extra_json TEXT, source_urls_json TEXT, found_assets_json TEXT,"
        " evidence_json TEXT, notes TEXT, review_status TEXT DEFAULT '待复核', reviewer TEXT,"
        " reviewed_at TEXT, created_at TEXT)")
    con.commit()
    con.close()
    store = Store(db)  # __init__ 跑 _ensure_audit_columns 补列
    try:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(audit_result)")}
        assert {"triage", "reason_codes_json"} <= cols  # 迁移补齐
        bid = store.find_or_create_batch("提交-T")
        store.add_audit_results(bid, [_arow("04030058", "一致", confidence="high",
                                             reason_codes=["corpus_hit"])])
        row = store.audit_results_of(bid)[0]
        assert row["triage"] == "auto_pass"  # 迁移后 INSERT 成功、triage 兜底算入库
        assert json.loads(row["reason_codes_json"]) == ["corpus_hit"]
    finally:
        store.close()


# 功能：验证联网核对复核状态非法值被拒（守边界）
# 设计：set_audit_review 传非枚举值断言抛 ValueError
def test_audit_review_rejects_bad_status(store: Store) -> None:
    bid = store.find_or_create_batch("提交-T")
    store.add_audit_results(bid, [_arow()])
    aid = store.audit_results_of(bid)[0]["id"]
    with pytest.raises(ValueError):
        store.set_audit_review(aid, "乱填")


# 功能：验证某 (码,年) 联网核对当前结果被人工"打回"→ 该资源项结论 rejected → 整批 PromoteBlocked（资源项级）
# 设计：两条同 resource_code 的 pass 暂存 + 该码 audit_result 经 stage_item 链 current_result_id 后人工打回，
#       断言 promote 抛 PromoteBlocked、正式库空——一条资源项结论挡住整批
def test_promote_blocks_on_audit_rejection(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1"), _srow("K2")])
    store.add_audit_results(bid, [_arow("04050014", "疑似缺漏", year="2025")])
    rid = int(store.audit_results_of(bid)[0]["id"])
    claim = store.claim_stage_item(bid, "04050014", "2025", worker="test")
    assert claim is not None
    store.finish_stage_item(
        bid, "04050014", "2025", status="done", current_result_id=rid,
        worker="test", expected_version=int(claim["state_version"]),
    )
    store.set_audit_review(rid, "打回", reviewer="复核员A")
    with pytest.raises(PromoteBlocked):
        store.promote_batch(bid)
    assert store.current_keys() == set()


# 功能：验证机器 verdict 仍"待复核"（未人工终审）→ pending → 阻断；人工"通过"后放行（守"仅人工接受才入库"）
# 设计：一条 audit 经 stage_item 链但 review 仍待复核 → PromoteBlocked；改"通过"后 promote 成功入库两行
def test_promote_blocks_until_human_accepts(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K1"), _srow("K2")])
    _seed_context(store, bid)
    store.add_audit_results(bid, [_arow("04050014", "疑似缺漏", year="2025")])
    rid = int(store.audit_results_of(bid)[0]["id"])
    claim = store.claim_stage_item(bid, "04050014", "2025", worker="test")
    assert claim is not None
    store.finish_stage_item(
        bid, "04050014", "2025", status="done", current_result_id=rid,
        worker="test", expected_version=int(claim["state_version"]),
    )
    with pytest.raises(PromoteBlocked):  # 待复核 → pending → 阻断
        store.promote_batch(bid)
    store.set_audit_review(rid, "通过")  # 人工接受
    stats = store.promote_batch(bid)
    assert stats["inserted"] == 2 and store.current_keys() == {"K1", "K2"}


# 功能：验证资源项级结论 zfill(8) 归一——audit/stage_item 存 7 位、staging 存 8 位仍对得上并阻断
# 设计：audit_result.resource_code="4050014"(7位) 经 claim_stage_item("4050014") 归一为 8 位链到 staging 的 04050014，
#       人工打回后断言整批 PromoteBlocked
def test_promote_audit_reject_zfill_normalization(store: Store) -> None:
    bid = store.create_batch("提交-T")
    store.add_staging(bid, [_srow("K8", code="04050014")])
    store.add_audit_results(bid, [_arow("4050014", "疑似缺漏", year="2025")])  # 官网侧码丢了前导零
    rid = int(store.audit_results_of(bid)[0]["id"])
    claim = store.claim_stage_item(bid, "4050014", "2025", worker="test")
    assert claim is not None  # _norm_zylbm → 04050014，与 staging 对上
    store.finish_stage_item(
        bid, "4050014", "2025", status="done", current_result_id=rid,
        worker="test", expected_version=int(claim["state_version"]),
    )
    store.set_audit_review(rid, "打回")
    with pytest.raises(PromoteBlocked):
        store.promote_batch(bid)
    assert store.current_keys() == set()
