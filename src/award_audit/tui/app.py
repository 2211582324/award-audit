"""award-tui 复核台：批次列表 + 暂存记录 + 复核操作 + 事件流实时刷新。

布局：上=批次表，中=选中批次的暂存记录表，下=事件日志 + 导入路径输入框。
按键：r 刷新 / Enter 看批次记录 / a 通过 / x 打回 / p 入库 / i 导入 / q 退出。
所有操作走 daemon 命令；daemon 广播的事件写进日志并触发刷新——多个 TUI 同时打开会看到彼此的操作。
"""

from __future__ import annotations

import json
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Input, RichLog, TabbedContent, TabPane

from award_audit.core.app import daemon_port
from award_audit.core.models.triage import reason_label, triage_label
from award_audit.tui.client import CoreClient, CoreClientError


class ReviewTuiApp(App[None]):
    """复核台应用。"""

    TITLE = "award-audit 复核台"
    CSS = """
    #batches { height: 30%; border: solid $primary; }
    #mid { height: 45%; border: solid $secondary; }
    #log { height: 15%; border: solid $accent; }
    #path-input { display: none; }
    #path-input.visible { display: block; }
    """
    BINDINGS = [
        Binding("r", "refresh", "刷新"),
        Binding("a", "approve", "通过"),
        Binding("x", "reject", "打回"),
        Binding("o", "open_source", "打开官网"),
        Binding("p", "promote", "入库"),
        Binding("i", "import_batch", "导入"),
        Binding("q", "quit", "退出"),
    ]

    # 初始化客户端与行号映射
    def __init__(self) -> None:
        super().__init__()
        self.client = CoreClient(port=daemon_port())
        self._batch_ids: list[int] = []      # 批次表行号 → batch_id
        self._staging_ids: list[int] = []    # 记录表行号 → staging_id
        self._audit_ids: list[int] = []      # 联网核对表行号 → audit_id
        self._audit_rows: dict[int, Any] = {}  # audit_id → 完整结论（选中时展开卡片用）
        self._current_batch: int | None = None

    # 组装界面
    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="batches", cursor_type="row")
        with TabbedContent(id="mid"):
            with TabPane("逐行记录", id="tab-records"):
                yield DataTable(id="records", cursor_type="row")
            with TabPane("联网核对", id="tab-audit"):
                yield DataTable(id="audit", cursor_type="row")
        yield RichLog(id="log", markup=False, wrap=True)
        yield Input(placeholder="输入批次文件夹路径后回车导入（Esc 取消）", id="path-input")
        yield Footer()

    # 挂载后：建表头、连 daemon、起事件订阅、首次加载
    async def on_mount(self) -> None:
        bt = self.query_one("#batches", DataTable)
        bt.add_columns("ID", "批次名", "状态", "文件", "行数", "导入时间")
        rt = self.query_one("#records", DataTable)
        rt.add_columns("ID", "文件", "行", "校验", "复核", "问题摘要")
        at = self.query_one("#audit", DataTable)
        at.add_columns("ID", "资源项", "年份", "结论", "置信", "分诊", "官网/提交", "复核")
        try:
            await self.client.connect()
        except OSError:
            self._log("[连接失败] 请先启动 daemon：award-core（或 python -m award_audit.core.app）")
            return
        self.run_worker(self.client.subscribe(self._on_event), exclusive=False)
        self._log(f"已连接 award-core（端口 {self.client.port}）")
        await self._load_batches()

    # 写一行日志
    def _log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    # daemon 事件回调：记日志并刷新对应视图
    async def _on_event(self, params: dict[str, Any]) -> None:
        etype = params.get("type", "?")
        if etype == "batch_imported":
            self._log(f"[事件] 批次导入：#{params['batch_id']} {params['name']}（{params['n_files']} 文件 {params['n_rows']} 行，严重 {params['n_blocker']}）")
            await self._load_batches()
        elif etype == "record_reviewed":
            self._log(f"[事件] 记录 #{params['staging_id']} 被{params['decision']}（{params.get('reviewer') or '匿名'}）")
            if self._current_batch == params.get("batch_id"):
                await self._load_staging(self._current_batch)
        elif etype == "audit_reviewed":
            who = params.get("reviewer") or "匿名"
            self._log(f"[事件] 联网核对 #{params['audit_id']} 被{params['decision']}（{who}）")
            if self._current_batch == params.get("batch_id"):
                await self._load_audit(self._current_batch)
        elif etype == "batch_promoted":
            self._log(f"[事件] 批次 #{params['batch_id']} 入库：新增 {params['inserted']}，跳过重复 {params['skipped_dup']}，不合格 {params['skipped_fail']}")
            await self._load_batches()
        elif etype == "core_started":
            self._log(f"[事件] daemon 启动 v{params.get('version')}")

    # 加载批次表
    async def _load_batches(self) -> None:
        try:
            resp = await self.client.call("cmd.list_batches")
        except CoreClientError as e:
            self._log(f"[错误] {e}")
            return
        bt = self.query_one("#batches", DataTable)
        bt.clear()
        self._batch_ids = []
        for b in resp.get("batches", []):
            bt.add_row(str(b["id"]), b["name"], b["status"], str(b["n_files"]), str(b["n_rows"]), b["imported_at"])
            self._batch_ids.append(int(b["id"]))

    # 加载某批次的暂存记录表
    async def _load_staging(self, batch_id: int) -> None:
        try:
            resp = await self.client.call("cmd.get_staging", {"batch_id": batch_id})
        except CoreClientError as e:
            self._log(f"[错误] {e}")
            return
        rt = self.query_one("#records", DataTable)
        rt.clear()
        self._staging_ids = []
        self._current_batch = batch_id
        for r in resp.get("records", []):
            summary = "；".join(i["message"] for i in r["issues"][:2]) or "-"
            rt.add_row(str(r["id"]), r["file"][:28], str(r["row_no"]),
                       r["check_status"], r["review_status"], summary[:48])
            self._staging_ids.append(int(r["id"]))
        self._log(f"批次 #{batch_id}：{len(self._staging_ids)} 条暂存记录")

    # 加载某批次的联网核对结论（资源项级卡）
    async def _load_audit(self, batch_id: int) -> None:
        try:
            resp = await self.client.call("cmd.get_audit_results", {"batch_id": batch_id})
        except CoreClientError as e:
            self._log(f"[错误] {e}")
            return
        at = self.query_one("#audit", DataTable)
        at.clear()
        self._audit_ids = []
        self._audit_rows = {}
        for r in resp.get("results", []):
            aid = int(r["id"])
            at.add_row(str(aid), (r["award_name"] or r["resource_code"])[:20], r["year"],
                       r["verdict"], r["confidence"], triage_label(r.get("triage", "")),
                       f'{r["extracted_count"]}/{r["submitted_count"]}', r["review_status"])
            self._audit_ids.append(aid)
            self._audit_rows[aid] = r
        if self._audit_ids:
            self._log(f"批次 #{batch_id}：{len(self._audit_ids)} 条联网核对结论（选中看证据）")

    # 在日志区展开一条联网核对结论的卡片详情（结论/证据链/人工入口）
    def _show_audit_card(self, audit_id: int) -> None:
        r = self._audit_rows.get(audit_id)
        if not r:
            return
        self._log(f"══ {r['award_name']}（{r['resource_code']}，{r['year']}）")
        self._log(f"  结论：{r['verdict']}  置信：{r['confidence']}  "
                  f"官网 {r['extracted_count']} vs 提交 {r['submitted_count']}")
        reasons = "；".join(reason_label(c) for c in r.get("reason_codes", []))
        self._log(f"  分诊：{triage_label(r.get('triage', ''))}"
                  + (f"  降级原因：{reasons}" if reasons else ""))
        if r.get("notes"):
            self._log(f"  备注：{r['notes']}")
        if r.get("missing"):
            self._log(f"  疑漏采：{r['missing'][:5]}")
        if r.get("extra"):
            self._log(f"  官网未匹配：{r['extra'][:5]}")
        for u in r.get("source_urls", []):
            self._log(f"  检索官网（o 打开）：{u}")
        for a in r.get("found_assets", [])[:5]:
            self._log(f"  名单文件/图：{a}")
        for e in r.get("evidence", []):
            self._log(f"  · {e}")

    # 批次表回车 → 加载该批次记录；其它表回车 → 展开联网核对卡片
    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        if table.id == "batches" and 0 <= event.cursor_row < len(self._batch_ids):
            bid = self._batch_ids[event.cursor_row]
            await self._load_staging(bid)
            await self._load_audit(bid)
        elif table.id == "audit" and 0 <= event.cursor_row < len(self._audit_ids):
            self._show_audit_card(self._audit_ids[event.cursor_row])

    # 取记录表当前选中的 staging_id
    def _selected_staging(self) -> int | None:
        rt = self.query_one("#records", DataTable)
        row = rt.cursor_row
        if 0 <= row < len(self._staging_ids):
            return self._staging_ids[row]
        return None

    # 取联网核对表当前选中的 audit_id
    def _selected_audit(self) -> int | None:
        at = self.query_one("#audit", DataTable)
        row = at.cursor_row
        if 0 <= row < len(self._audit_ids):
            return self._audit_ids[row]
        return None

    # 复核动作（通过/打回）：按当前活动页签分派——「联网核对」页作用于结论，否则作用于逐行记录
    async def _review(self, decision: str) -> None:
        active = self.query_one("#mid", TabbedContent).active
        if active == "tab-audit":
            aid = self._selected_audit()
            if aid is None:
                self._log("先在联网核对表选中一行")
                return
            try:
                await self.client.call("cmd.review_audit",
                                       {"audit_id": aid, "decision": decision, "reviewer": "TUI"})
            except CoreClientError as e:
                self._log(f"[错误] {e}")
            return
        sid = self._selected_staging()
        if sid is None:
            self._log("先在记录表选中一行（先在批次表回车加载记录）")
            return
        try:
            await self.client.call("cmd.review_record",
                                   {"staging_id": sid, "decision": decision, "reviewer": "TUI"})
        except CoreClientError as e:
            self._log(f"[错误] {e}")

    # r：刷新批次
    async def action_refresh(self) -> None:
        await self._load_batches()
        self._log("已刷新")

    # a：通过当前记录
    async def action_approve(self) -> None:
        await self._review("通过")

    # x：打回当前记录
    async def action_reject(self) -> None:
        await self._review("打回")

    # o：在浏览器打开选中联网核对结论的官网 URL（复核员本地核对入口）
    def action_open_source(self) -> None:
        import webbrowser
        aid = self._selected_audit()
        if aid is None:
            self._log("先在「联网核对」页选中一行")
            return
        r = self._audit_rows.get(aid) or {}
        url = r.get("source_url") or (r.get("source_urls") or [""])[0]
        if not url:
            self._log("该结论无官网 URL 可打开")
            return
        webbrowser.open(url)
        self._log(f"已在浏览器打开：{url}")

    # p：当前批次入库
    async def action_promote(self) -> None:
        if self._current_batch is None:
            self._log("先在批次表回车选择一个批次")
            return
        try:
            stats = await self.client.call("cmd.promote_batch", {"batch_id": self._current_batch, "operator": "TUI"})
            self._log(f"入库统计：{json.dumps(stats, ensure_ascii=False)}")
        except CoreClientError as e:
            self._log(f"[错误] {e}")

    # i：显示导入路径输入框
    def action_import_batch(self) -> None:
        inp = self.query_one("#path-input", Input)
        inp.add_class("visible")
        inp.focus()

    # 路径输入回车 → 调 daemon 导入
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        inp = self.query_one("#path-input", Input)
        inp.remove_class("visible")
        folder = event.value.strip().strip('"')
        inp.value = ""
        if not folder:
            return
        self._log(f"导入中：{folder} …")
        try:
            resp = await self.client.call("cmd.import_batch", {"folder": folder, "operator": "TUI"})
            self._log(f"导入完成：批次 #{resp['batch_id']}（{resp['n_files']} 文件 {resp['n_rows']} 行，问题 {resp['n_issues']} 条）")
        except CoreClientError as e:
            self._log(f"[错误] {e}")


# TUI 入口
def main() -> None:
    ReviewTuiApp().run()
