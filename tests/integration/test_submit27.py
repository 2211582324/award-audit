"""集成测试：对真实批次 提交-27 跑通整条核查管道（不 mock，用真实参照数据）。"""

from __future__ import annotations

import pytest

from award_audit.core import config
from award_audit.core.models.issue import Severity
from award_audit.core.pipeline.engine import check_batch


# 功能：验证整条管道能吃真实 提交-27（6 个文件）跑通、每文件有数据行、判定可计算
# 设计：走 config 回退路径定位样本；样本缺失则 skip，避免环境差异导致硬失败
def test_check_batch_submit27() -> None:
    batch = config.PROJECT_ROOT.parent / "评奖信息核查" / "提交-27"
    if not batch.is_dir():
        pytest.skip("提交-27 样本不在预期路径，跳过集成测试")
    result = check_batch(batch)
    assert len(result.files) == 6
    for fr in result.files:
        assert fr.n_rows > 0
        # 判定必为四种之一
        assert fr.verdict in {"打回", "待复核", "待修正", "可入库"}
    # 各严重度计数非负（冒烟）
    assert result.count(Severity.BLOCKER) >= 0
