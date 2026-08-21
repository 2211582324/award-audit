"""集成测试：真实批次 提交-27 走 ingest → 溯源/年份落库 → 全通过门禁拦截 → 重导去重显现。"""

from __future__ import annotations

import pytest

from award_audit.core import config
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import PromoteBlocked, Store


# 功能：验证真实批次 ingest 写台账/溯源/业务年份，且新版"整批全通过"门禁在未审核的真实数据上正确拦截
# 设计：提交-27 的资源项均未经 L5/人工审核 → promote 抛 PromoteBlocked、零写入。台账"入库+跨批去重"
#       闭环由 test_store.test_promote_all_pass_dedup / test_promote_cross_batch_dedup（合成+全接受）覆盖，
#       跨批去重需先成功入库，故不在"门禁拦截"这条真实用例里重复验证。
def test_ingest_writes_provenance_and_gate_blocks(tmp_path) -> None:
    batch = config.PROJECT_ROOT.parent / "评奖信息核查" / "提交-27"
    if not batch.is_dir():
        pytest.skip("提交-27 样本不在预期路径，跳过集成测试")

    store = Store(tmp_path / "ledger.db")
    try:
        # 导入：写台账暂存（ledger 传空 dict 关掉 L3 数量核查——聚焦台账/溯源/门禁）
        b1, r1 = ingest_batch(batch, store, ledger={})
        staged = store.staging_of(b1)
        assert len(staged) == sum(fr.n_rows for fr in r1.files)  # 每行一条暂存
        assert store.get_batch(b1)["status"] == "审核中"

        # 新版：导入溯源与业务年份已落库（供 L5 阶段重验、门禁按 (码,年) 匹配）
        ctx = store.get_import_context(b1)
        assert ctx is not None and str(ctx["source_folder"]) and str(ctx["template_fingerprint"])
        assert any(str(row["year"]) for row in staged)  # staging 已带业务年份，不再入库时临时猜
        assert store.get_batch_stage_run(b1, "local")["status"] == "done"  # local 阶段执行事实落表

        # 整批门禁：资源项均未经审核（+ 可能 fail 行）→ promote 被拦、零写入
        with pytest.raises(PromoteBlocked):
            store.promote_batch(b1)
        assert store.current_keys() == set()
    finally:
        store.close()


def _imported_file(path, spec) -> ImportedFile:  # noqa: ANN001
    return ImportedFile(
        batch="提交-原子",
        path=str(path),
        file_name=path.name,
        claimed_table_code=spec.table_code,
        award_name="测试奖",
        year="2024",
        sheet_name=spec.sheet_name,
        header_codes=list(spec.field_codes),
        header_names=[spec.name_of(code) for code in spec.field_codes],
        rows=[["04050014" if code == "ZYLBM" else "测试奖" if code == "ZYLB" else
               "2024" if code == "PDNY" else "题目" if code == "LWTM" else
               "张三" if code == "ZZXM" else "" for code in spec.field_codes]],
    )


def test_ingest_atomically_persists_batch_context_and_local_done(
    tmp_path, xwlwhj_spec, resource_map,
) -> None:  # noqa: ANN001
    source = tmp_path / "submission"
    source.mkdir()
    material = source / "submission.xlsx"
    material.write_bytes(b"offline-fixture")
    imported = _imported_file(material, xwlwhj_spec)
    store = Store(tmp_path / "atomic.db")
    try:
        bid, _ = ingest_batch(
            source,
            store,
            registry={xwlwhj_spec.table_code: xwlwhj_spec},
            resource_map=resource_map,
            ledger={},
            files=[imported],
        )
        assert store.get_batch(bid)["status"] == "审核中"
        assert store.staging_of(bid)[0]["year"] == "2024"
        assert store.get_import_context(bid) is not None
        assert store.get_batch_stage_run(bid, "local")["status"] == "done"
    finally:
        store.close()


def test_ingest_persistence_failure_leaves_only_failed_batch(
    tmp_path, xwlwhj_spec, resource_map,
) -> None:  # noqa: ANN001
    source = tmp_path / "submission-fail"
    source.mkdir()
    material = source / "submission.xlsx"
    material.write_bytes(b"offline-fixture")
    imported = _imported_file(material, xwlwhj_spec)
    store = Store(tmp_path / "atomic-fail.db")
    store.conn.execute(
        "CREATE TRIGGER fail_staging BEFORE INSERT ON staging_record "
        "BEGIN SELECT RAISE(ABORT, 'forced staging failure'); END"
    )
    store.conn.commit()
    try:
        with pytest.raises(Exception, match="forced staging failure"):
            ingest_batch(
                source,
                store,
                registry={xwlwhj_spec.table_code: xwlwhj_spec},
                resource_map=resource_map,
                ledger={},
                files=[imported],
            )
        batches = store.list_batches()
        assert len(batches) == 1
        failed = batches[0]
        assert failed["status"] == "导入失败" and bool(failed["needs_reimport"])
        assert store.staging_of(int(failed["id"])) == []
        assert store.get_import_context(int(failed["id"])) is None
        assert store.get_batch_stage_run(int(failed["id"]), "local")["status"] == "failed"
    finally:
        store.close()
