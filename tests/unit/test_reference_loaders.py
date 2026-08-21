"""参照加载器测试：模板注册表 + 1016 映射（跑真实参照数据）。"""

from __future__ import annotations

from award_audit.core.reference.resource_map import load_resource_map
from award_audit.core.reference.template_registry import load_template_registry


# 功能：验证 18 个模板能加载成注册表，且学位论文模板的列角色正确
# 设计：断言注册表规模、首字段 ZYLBM、主年份列 PDNY、人名列含 ZZXM，覆盖类型登记合并逻辑
def test_load_template_registry_real() -> None:
    reg = load_template_registry()
    assert len(reg) >= 15  # 17 个标准模板（院士特例不计）
    xw = reg["CON_GG_XK_RCPY_XWLWHJ"]
    assert xw.field_codes[0] == "ZYLBM"
    assert xw.primary_year_col == "PDNY"
    assert "ZZXM" in xw.name_cols


# 功能：验证模板尾部空列被裁掉（科研获奖 max_column 被撑到 55）
# 设计：断言真实字段数远小于 55 且末字段非空，覆盖 _real_ncol 去尾空列
def test_template_trailing_columns_trimmed() -> None:
    reg = load_template_registry()
    jx = reg["CON_GG_XK_KXYJ_JXKYJL"]
    assert len(jx.field_codes) < 20
    assert jx.field_codes[-1] != ""


# 功能：验证国家级教材两列同名“作者署名情况”被容错处理（不崩溃、代码各自独立）
# 设计：XSMQK 与 ZZSMQK 是不同代码但同中文名，断言都在字段集且中文名相同
def test_yxjc_duplicate_name_handled() -> None:
    reg = load_template_registry()
    yx = reg["CON_GG_XK_RCPY_YXJC"]
    assert "XSMQK" in yx.field_codes and "ZZSMQK" in yx.field_codes
    assert yx.name_of("XSMQK") == yx.name_of("ZZSMQK") == "作者署名情况"


# 功能：验证 1016 映射表加载且结构正确
# 设计：断言条数量级、任一条目的公共表表名以 CON_ 开头，覆盖列定位与前导零文本化
def test_resource_map_real() -> None:
    rmap = load_resource_map()
    assert len(rmap) > 300
    entry = next(iter(rmap.values()))
    assert entry.table_code.startswith("CON_")
