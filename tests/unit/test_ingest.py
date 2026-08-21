"""ingest_batch 的单元测试：files= 注入免二次解析，staging 落库正确。"""

from __future__ import annotations

from pathlib import Path

from award_audit.core.pipeline import ingest as ingest_mod
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store


# 功能：验证 ingest_batch 传 files= 时不再调用 importer.import_batch（解析一次、两处复用），staging 仍正确落库
# 设计：把 importer.import_batch monkeypatch 成"一调用就抛错"，传 files= 跑 ingest_batch，
#       断言不抛错且 staging 行数/resource_code 正确——证明注入路径完全绕开二次解析
def test_ingest_batch_reuses_preparsed_files(kit, xwlwhj_spec, resource_map, monkeypatch) -> None:  # noqa: ANN001
    files = [kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文甲", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文乙", "ZZXM": "李四", "PDNY": "2024"},
    ])]

    def _boom(folder):  # noqa: ANN001, ANN202, ARG001  被调用即失败：证明 files= 路径不二次解析
        raise AssertionError("传 files= 时不应再调用 importer.import_batch")

    monkeypatch.setattr(ingest_mod.importer, "import_batch", _boom)
    store = Store(":memory:")
    try:
        bid, result = ingest_batch(Path("批次-T"), store, files=files,
                                   registry={kit.XWLWHJ_CODE: xwlwhj_spec},
                                   resource_map=resource_map, ledger={})
        rows = store.staging_of(bid)
    finally:
        store.close()
    assert len(rows) == 2
    assert [r["resource_code"] for r in rows] == ["04050014", "04050014"]
    assert result.batch == "批次-T"
