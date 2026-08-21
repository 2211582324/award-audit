"""L5-pre 链接预检的单元测试（fake prober，不联网）。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l5_precheck
from award_audit.core.reference.ledger import LedgerEntry


# 造清单条目
def _entry(code: str, url: str = "") -> LedgerEntry:
    return LedgerEntry(resource_code=code, resource_name="某奖", collect_url=url)


# 功能：验证五条分流各自命中：未收录/无网址/格式异常/需权限/不可达，且 200 进通行清单
# 设计：五个资源项各造一种情形 + fake prober 按 URL 返回不同状态码，一次覆盖全部分支
def test_precheck_full_branches(kit) -> None:
    files = [
        kit.build([{"ZYLBM": c, "ZYLB": kit.AWARD, "PDNY": "2024"}], award=kit.AWARD)
        for c in ("00000001", "00000002", "00000003", "00000004", "00000005")
    ]
    ledger = {
        # 00000001 不在清单 → L5P-01
        "00000002": _entry("00000002", url=""),                       # L5P-02 无网址
        "00000003": _entry("00000003", url="不是网址"),                # L5P-03 格式异常
        "00000004": _entry("00000004", url="https://a.gov.cn/x"),     # 403 → L5P-04
        "00000005": _entry("00000005", url="https://b.gov.cn/y"),     # 200 → 通行
    }

    def prober(url: str) -> tuple[int | None, str]:
        return (403, "") if "a.gov.cn" in url else (200, "")

    result = l5_precheck.run_batch(files, ledger, prober)
    ids = sorted(i.rule_id for i in result.issues)
    assert ids == ["L5P-01", "L5P-02", "L5P-03", "L5P-04"]
    assert result.passable == ["00000005"]
    assert [item.trigger_code for item in result.search_handoffs] == [
        "SOURCE_URL_MISSING",
        "SOURCE_URL_MISSING",
        "SOURCE_UNREACHABLE",
    ]


# 功能：验证连接层失败（超时/DNS）走 L5P-05 不可达
# 设计：prober 返回 (None, "超时")，断言 L5P-05 且消息含原因
def test_precheck_unreachable(kit) -> None:
    files = [kit.build([{"ZYLBM": "00000009", "ZYLB": kit.AWARD, "PDNY": "2024"}])]
    ledger = {"00000009": _entry("00000009", url="https://dead.gov.cn/")}
    result = l5_precheck.run_batch(files, ledger, lambda u: (None, "超时"))
    assert [i.rule_id for i in result.issues] == ["L5P-05"]
    assert "超时" in result.issues[0].message
    assert result.search_handoffs == []
    assert result.retry_targets[0].urls == ["https://dead.gov.cn/"]


# 功能：验证离线模式只做静态检查、不产生探测类分流、passable 为空
# 设计：prober=None，含合法网址的资源项既不报也不通行（待在线探测），静态问题照报
def test_precheck_offline(kit) -> None:
    files = [
        kit.build([{"ZYLBM": "00000002", "ZYLB": kit.AWARD, "PDNY": "2024"}]),
        kit.build([{"ZYLBM": "00000005", "ZYLB": kit.AWARD, "PDNY": "2024"}]),
    ]
    ledger = {
        "00000002": _entry("00000002", url=""),
        "00000005": _entry("00000005", url="https://b.gov.cn/y"),
    }
    result = l5_precheck.run_batch(files, ledger, prober=None)
    assert result.offline and result.passable == []
    assert [i.rule_id for i in result.issues] == ["L5P-02"]
    assert len(result.candidate_targets) == 1
    assert result.candidate_targets[0].resource_code == "00000005"
    assert result.candidate_targets[0].probe_status == "not_checked"
    assert result.passable_targets == []


# 功能：验证同码异年按 (码,年) 分开，不把两个审核目标塌缩
# 设计：两个文件同码异年且未收录，断言分别产生问题和 handoff
def test_precheck_groups_by_code_and_year(kit) -> None:
    files = [
        kit.build([{"ZYLBM": "00000001", "ZYLB": kit.AWARD, "PDNY": "2018"}], year="2018"),
        kit.build([{"ZYLBM": "00000001", "ZYLB": kit.AWARD, "PDNY": "2019"}], year="2019"),
    ]
    result = l5_precheck.run_batch(files, {}, prober=None)
    assert len(result.issues) == 2
    assert {item.year for item in result.search_handoffs} == {"2018", "2019"}


# 功能：验证 URL 合法性判定边界
# 设计：http/https 合法；无 scheme、非 http scheme、纯文本不合法
def test_url_ok() -> None:
    assert l5_precheck._url_ok("https://www.moe.gov.cn/a")
    assert l5_precheck._url_ok("http://x.cn")
    assert not l5_precheck._url_ok("www.moe.gov.cn")
    assert not l5_precheck._url_ok("ftp://x.cn")
    assert not l5_precheck._url_ok("不是网址")


# 功能：验证清单一格多网址（中英分号/换行分隔）被正确拆分（实测：研创赛条目）
# 设计：混合中英分号与换行的字段拆出 3 个 URL，锁定多值形态支持
def test_split_urls() -> None:
    field = "https://a.cn/x；https://b.cn/y;\nhttps://c.cn/z"
    assert l5_precheck.split_urls(field) == ["https://a.cn/x", "https://b.cn/y", "https://c.cn/z"]


# 功能：验证多网址逐个探测：部分 403 但有一个 200 → 通行且记录可用 URL，不误报权限问题
# 设计：两 URL 一 403 一 200，断言 passable 命中、passable_urls 只含 200 的、无 Issue——
#       修复"整串当一个 URL 探测导致误判 403"的回归
def test_multi_url_partial_ok(kit) -> None:
    files = [kit.build([{"ZYLBM": "00000008", "ZYLB": kit.AWARD, "PDNY": "2024"}])]
    ledger = {"00000008": _entry("00000008", url="https://auth.gov.cn/a；https://open.gov.cn/b")}

    def prober(url: str) -> tuple[int | None, str]:
        return (403, "") if "auth" in url else (200, "")

    result = l5_precheck.run_batch(files, ledger, prober)
    assert result.issues == []
    assert result.passable == ["00000008"]
    assert result.passable_urls["00000008"] == ["https://open.gov.cn/b"]
    assert result.passable_targets[0].urls == [
        "https://auth.gov.cn/a", "https://open.gov.cn/b"
    ]


# 功能：验证多网址全部需权限时按数量汇总一条 L5P-04
# 设计：两 URL 都 403，断言一条 Issue 且消息含"2 个"
def test_multi_url_all_auth(kit) -> None:
    files = [kit.build([{"ZYLBM": "00000007", "ZYLB": kit.AWARD, "PDNY": "2024"}])]
    ledger = {"00000007": _entry("00000007", url="https://a.gov.cn/1;https://a.gov.cn/2")}
    result = l5_precheck.run_batch(files, ledger, lambda u: (403, ""))
    assert [i.rule_id for i in result.issues] == ["L5P-04"]
    assert "2 个" in result.issues[0].message
