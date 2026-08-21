"""daemon 集成测试：进程内起 CoreApp（随机端口 + 临时库），客户端走完命令与事件全链路。

不 spawn 子进程——同一事件循环里 server+client 直连，快且无端口就绪轮询；
TCP、NDJSON、JSON-RPC、事件广播全部是真实路径。
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import openpyxl

from award_audit.core.app import CoreApp
from award_audit.tui.client import CoreClient, CoreClientError

XW = "CON_GG_XK_RCPY_XWLWHJ"
AWARD = "中国人工智能学会优秀博士学位论文"


# 取一个空闲 TCP 端口
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# 造一个 2 行干净数据的小批次文件夹
def _make_batch(tmp: Path) -> Path:
    folder = tmp / "提交-T"
    folder.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = f"{XW}-学位论文获奖信息_"
    codes = ["ZYLBM", "BZ", "ZYLB", "SSDM", "SSMC", "XXDM", "MLM", "YJXKM", "YJXKMC",
             "XKDM", "XKMC", "LWTM", "LWGJC", "ZZXM", "DSXM", "XWJB", "PDNY", "PDJG", "XDWMC"]
    names = ["资源项码", "备注", "资源项", "省市代码", "省市名称", "学校代码", "门类码", "一级学科码", "一级学科名称",
             "学科码", "学科名称", "论文题目", "论文关键词", "作者姓名", "导师姓名", "学位级别", "评定年月", "评定机构", "学校名称"]
    ws.append(codes)
    ws.append(names)
    for i, (title, author) in enumerate([("论文甲", "张三"), ("论文乙", "李四")]):
        row = {"ZYLBM": "04050014", "ZYLB": AWARD, "LWTM": title, "ZZXM": author,
               "XWJB": "博士", "PDNY": "2024", "PDJG": "中国人工智能学会", "XDWMC": "某大学"}
        ws.append([row.get(c, "") for c in codes])
    wb.save(folder / f"{XW}-{AWARD}-2024.xlsx")
    return folder


# 把某 (码,年) 资源项标为 m4_accepted：造 audit_result + stage_item 链 current_result_id + 人工"通过"。
# 真实由 L5 阶段 + 复核台完成；测试里让新版"整批全通过"入库门禁放行。
def _resolve_m4(store, bid: int, code: str = "04050014", year: str = "2024") -> None:  # noqa: ANN001
    store.add_audit_results(bid, [{
        "resource_code": code, "award_name": AWARD, "year": year, "verdict": "一致",
        "confidence": "high", "source_kind": "excel", "extracted_count": 2, "submitted_count": 2,
        "missing": [], "extra": [], "source_urls": ["https://gov/p"], "found_assets": [],
        "evidence": [], "notes": "",
    }])
    rid = int(store.audit_results_of(bid)[-1]["id"])
    claim = store.claim_stage_item(bid, code, year, worker="test")
    assert claim is not None
    store.finish_stage_item(
        bid, code, year, status="done", current_result_id=rid,
        worker="test", expected_version=int(claim["state_version"]),
    )
    store.set_audit_review(rid, "通过")


# 完整场景：ping → import → get_staging → review 打回 → promote → 事件断言
async def _scenario(tmp: Path) -> None:
    port = _free_port()
    app = CoreApp(port=port, db_path=tmp / "t.db")
    app._ledger = {}  # 注入空采集清单关掉 L3——本测试聚焦协议与广播，不受真实清单数量口径影响
    await app.server.start()
    client = CoreClient(port=port)
    events: list[dict[str, Any]] = []

    # 事件回调：收集
    async def on_event(params: dict[str, Any]) -> None:
        events.append(params)

    try:
        await client.connect()
        sub_task = asyncio.create_task(client.subscribe(on_event))

        # ping
        pong = await client.call("cmd.ping")
        assert pong["pong"] is True

        # 未知方法 → 协议错误
        try:
            await client.call("cmd.nope")
            raise AssertionError("未知方法应报错")
        except CoreClientError as e:
            assert "-32601" in str(e)

        # 导入小批次
        folder = _make_batch(tmp)
        resp = await client.call("cmd.import_batch", {"folder": str(folder)})
        bid = resp["batch_id"]
        assert resp["n_rows"] == 2

        # 取暂存记录（瘦身响应：展示字段 + 问题摘要，无完整 data）
        staging = (await client.call("cmd.get_staging", {"batch_id": bid}))["records"]
        assert len(staging) == 2 and staging[0]["resource_code"] == "04050014"
        assert staging[0]["review_status"] == "待复核"

        # 复核第 1 条为"通过"（record_reviewed 事件）；资源项经审核接受后整批 promote 入库
        await client.call("cmd.review_record", {"staging_id": staging[0]["id"], "decision": "通过", "reviewer": "测试员"})
        _resolve_m4(app.store, bid)  # 造 audit_result+stage_item 链+人工通过（真实由 L5+复核完成）
        stats = await client.call("cmd.promote_batch", {"batch_id": bid})
        assert stats["inserted"] == 2

        # 等广播送达，断言三类事件都收到了
        await asyncio.sleep(0.3)
        types = [e.get("type") for e in events]
        assert "batch_imported" in types and "record_reviewed" in types and "batch_promoted" in types

        sub_task.cancel()
    finally:
        await client.close()
        await app.server.stop()
        app.store.close()


# 功能：验证 daemon 全链路——TCP/NDJSON/JSON-RPC 命令分发、错误码、复核闸门、事件广播
# 设计：进程内起真实 server + 双连接客户端（命令+订阅），一个场景覆盖六个命令与三类事件；
#       随机端口避免冲突，tmp 库隔离状态，asyncio.run 包装免去 pytest-asyncio 依赖
def test_daemon_full_roundtrip(tmp_path) -> None:
    asyncio.run(_scenario(tmp_path))


# L5 联网核对结论经 daemon 读取+复核的场景（模拟 audit CLI 已落库→复核台读+终审+广播）
async def _scenario_audit(tmp: Path) -> None:
    port = _free_port()
    app = CoreApp(port=port, db_path=tmp / "a.db")
    app._ledger = {}
    await app.server.start()
    client = CoreClient(port=port)
    events: list[dict[str, Any]] = []

    async def on_event(params: dict[str, Any]) -> None:
        events.append(params)

    try:
        await client.connect()
        sub_task = asyncio.create_task(client.subscribe(on_event))

        folder = _make_batch(tmp)
        bid = (await client.call("cmd.import_batch", {"folder": str(folder)}))["batch_id"]

        # 模拟 audit CLI 已把资源项级结论落库（一条"无法核对·转人工"，带 found_assets 人工入口）
        app.store.add_audit_results(bid, [{
            "resource_code": "04050014", "award_name": AWARD, "year": "2024", "verdict": "无法核对",
            "confidence": "low", "source_kind": "none", "extracted_count": 0, "submitted_count": 2,
            "missing": [], "extra": [], "source_urls": ["https://gov/p"],
            "found_assets": ["https://gov/a.pdf"], "evidence": ["访问页面", "转人工"], "notes": "未取到名单",
        }])

        # 读回：结论 + 证据链 + 人工入口都随响应回传
        results = (await client.call("cmd.get_audit_results", {"batch_id": bid}))["results"]
        assert len(results) == 1 and results[0]["verdict"] == "无法核对"
        assert results[0]["found_assets"] == ["https://gov/a.pdf"]
        aid = results[0]["id"]

        # 资源项级终审：通过 → 状态落库 + 广播 audit_reviewed
        await client.call("cmd.review_audit", {"audit_id": aid, "decision": "通过", "reviewer": "测试员"})
        after = (await client.call("cmd.get_audit_results", {"batch_id": bid}))["results"]
        assert after[0]["review_status"] == "通过" and after[0]["reviewer"] == "测试员"

        await asyncio.sleep(0.3)
        assert "audit_reviewed" in [e.get("type") for e in events]
        sub_task.cancel()
    finally:
        await client.close()
        await app.server.stop()
        app.store.close()


# 功能：验证 L5 联网核对结论经 daemon 的读取(cmd.get_audit_results)+终审(cmd.review_audit)+事件广播全链路
# 设计：进程内起 daemon，import 一批后直接落一条 audit_result，走 get→review→再 get 断言状态变更、
#       并收到 audit_reviewed 事件——证明资源项级复核卡已接入 daemon/事件流（与逐行复核并行）
def test_daemon_audit_roundtrip(tmp_path) -> None:
    asyncio.run(_scenario_audit(tmp_path))


# L5 联网核对被人工打回 → promote 拦下该资源项全部暂存行的场景（阶段2 资源项级闸门端到端）
async def _scenario_audit_reject(tmp: Path) -> None:
    port = _free_port()
    app = CoreApp(port=port, db_path=tmp / "r.db")
    app._ledger = {}
    await app.server.start()
    client = CoreClient(port=port)
    try:
        await client.connect()
        folder = _make_batch(tmp)  # 2 行同 resource_code=04050014
        bid = (await client.call("cmd.import_batch", {"folder": str(folder)}))["batch_id"]

        # 落一条该资源项的联网核对结论（机器判"疑似缺漏"，待人工终审）
        app.store.add_audit_results(bid, [{
            "resource_code": "04050014", "award_name": AWARD, "year": "2024", "verdict": "疑似缺漏",
            "confidence": "medium", "source_kind": "excel", "extracted_count": 3, "submitted_count": 2,
            "missing": ["漏采的X"], "extra": [], "source_urls": ["https://gov/p"],
            "found_assets": [], "evidence": [], "notes": "",
        }])
        aid = (await client.call("cmd.get_audit_results", {"batch_id": bid}))["results"][0]["id"]
        # 链 stage_item.current_result_id → 该 (码,年) 的当前 M4 结果就是这条
        claim = app.store.claim_stage_item(bid, "04050014", "2024", worker="test")
        assert claim is not None
        app.store.finish_stage_item(
            bid, "04050014", "2024", status="done", current_result_id=aid,
            worker="test", expected_version=int(claim["state_version"]),
        )

        # 复核台把该资源项打回 → 整批门禁拦下 promote（资源项 rejected），零写入
        await client.call("cmd.review_audit", {"audit_id": aid, "decision": "打回", "reviewer": "测试员"})
        try:
            await client.call("cmd.promote_batch", {"batch_id": bid})
            raise AssertionError("打回资源项应拦下整批 promote")
        except CoreClientError as e:
            assert "PromoteBlocked" in str(e) or "未达可入库结论" in str(e)
        assert app.store.current_keys() == set()  # 正式库为空——一条资源项打回挡住整批
    finally:
        await client.close()
        await app.server.stop()
        app.store.close()


# 功能：验证复核台把资源项级联网结论打回后，daemon promote 被整批门禁拦下（人在环闸门端到端）
# 设计：进程内起 daemon，import 2 行同码批次→落一条 audit_result 并链 stage_item→cmd.review_audit 打回→
#       cmd.promote_batch 抛 PromoteBlocked、正式库空——证明 TUI 打回真能挡整批入库（资源项级 rejected）
def test_daemon_audit_reject_blocks_promote(tmp_path) -> None:
    asyncio.run(_scenario_audit_reject(tmp_path))
