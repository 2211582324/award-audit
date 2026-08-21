"""命令行入口。

M1：check（核查批次，出反馈意见，不入库）、templates（列模板）。
M2：import（核查并写台账·暂存）、batches（列台账）、promote（入正式库·版本化）、history（查某记录变更史）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from award_audit.core import config
from award_audit.core.models.issue import Severity
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.triage import decide_triage, reason_label, triage_label
from award_audit.core.pipeline import importer, report
from award_audit.core.pipeline.checks import l5_precheck
from award_audit.core.pipeline.engine import BatchResult, check_batch
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import load_ledger
from award_audit.core.reference.resource_map import load_resource_map
from award_audit.core.reference.template_registry import load_template_registry


# 打印批次核查概览（check 与 import 共用）
def _print_overview(result: BatchResult) -> None:
    print(f"\n批次 {result.batch}：{len(result.files)} 个文件，问题 {result.total_issues} 条 "
          f"（严重 {result.count(Severity.BLOCKER)} / 格式 {result.count(Severity.FORMAT)} / 待复核 {result.count(Severity.REVIEW)}）\n")
    for fr in result.files:
        print(f"  [{fr.verdict}] {fr.file}  行{fr.n_rows}  "
              f"严重{fr.count(Severity.BLOCKER)} 格式{fr.count(Severity.FORMAT)} 待复核{fr.count(Severity.REVIEW)}")


# check：核查批次并产出反馈意见（不入库）
def _cmd_check(folder: Path) -> int:
    if not folder.is_dir():
        print(f"[错误] 批次文件夹不存在：{folder}", file=sys.stderr)
        return 2
    result = check_batch(folder)
    xlsx, md = report.write_reports(result)
    _print_overview(result)
    print(f"\n反馈意见 → {xlsx}\n反馈摘要 → {md}")
    return 1 if result.count(Severity.BLOCKER) else 0


# import：核查并写入台账（暂存态）
def _cmd_import(folder: Path, db: Path) -> int:
    if not folder.is_dir():
        print(f"[错误] 批次文件夹不存在：{folder}", file=sys.stderr)
        return 2
    store = Store(db)
    try:
        batch_id, result = ingest_batch(folder, store)
        xlsx, md = report.write_reports(result)
        _print_overview(result)
        print(f"\n已写入台账（批次 #{batch_id}，暂存·审核中）→ {db}")
        print(f"反馈意见 → {xlsx}")
        print(f"下一步：award-audit promote {batch_id}   # 通过校验的记录入正式库")
    finally:
        store.close()
    return 1 if result.count(Severity.BLOCKER) else 0


# batches：列出台账所有批次
def _cmd_batches(db: Path) -> int:
    store = Store(db)
    try:
        rows = store.list_batches()
        if not rows:
            print("台账为空。先 award-audit import <批次文件夹>")
            return 0
        print(f"{'ID':>3}  {'批次名':<24} {'状态':<8} {'文件':>4} {'行数':>6}  导入时间")
        for r in rows:
            print(f"{r['id']:>3}  {r['name']:<24} {r['status']:<8} {r['n_files']:>4} {r['n_rows']:>6}  {r['imported_at']}")
    finally:
        store.close()
    return 0


# promote：把批次通过校验的暂存记录入正式库（版本化）
def _cmd_promote(batch_id: int, db: Path) -> int:
    store = Store(db)
    try:
        if store.get_batch(batch_id) is None:
            print(f"[错误] 批次 #{batch_id} 不存在", file=sys.stderr)
            return 2
        stats = store.promote_batch(batch_id)
        print(f"批次 #{batch_id} 入库完成：新增 {stats['inserted']} 条，"
              f"跳过重复 {stats['skipped_dup']} 条，跳过不合格 {stats['skipped_fail']} 条，"
              f"跳过逐行打回 {stats['skipped_rejected']} 条，跳过资源项打回 {stats['skipped_audit_rejected']} 条。")
    finally:
        store.close()
    return 0


# history：查某记录（按片段匹配业务键）的版本史与审计流水
def _cmd_history(fragment: str, db: Path) -> int:
    store = Store(db)
    try:
        keys = store.search_keys(fragment)
        if not keys:
            print(f"未找到匹配 {fragment!r} 的正式记录")
            return 0
        for key in keys:
            print(f"\n=== 业务键：{key.replace(chr(0x1f), ' | ')} ===")
            print("  版本史：")
            for v in store.history(key):
                cur = "★当前" if v["is_current"] else "  "
                print(f"    v{v['version']} {cur}  生效 {v['valid_from']}  失效 {v['valid_to'] or '-'}  批次#{v['source_batch_id']}")
            print("  审计流水：")
            for a in store.audit_of(key):
                print(f"    [{a['ts']}] {a['action']}  {a['reason']}  {a['diff_json'] if a['diff_json'] != '{}' else ''}")
    finally:
        store.close()
    return 0


# precheck：联网核对前的链接预检（L5P-01~05 分流；--offline 只做清单/网址静态检查）
def _cmd_precheck(folder: Path, offline: bool) -> int:
    if not folder.is_dir():
        print(f"[错误] 批次文件夹不存在：{folder}", file=sys.stderr)
        return 2
    files = importer.import_batch(folder)
    ledger = load_ledger()
    prober = None if offline else l5_precheck.default_prober
    mode = "离线（仅清单/网址静态检查）" if offline else "在线（含 HTTP 探测）"
    print(f"\n链接预检 {folder.name}：{len(files)} 个文件，模式：{mode}\n")
    result = l5_precheck.run_batch(files, ledger, prober)
    for i in result.issues:
        print(f"  [{i.rule_id}] {i.message}")
    if not result.issues:
        print("  （无分流问题）")
    if not result.offline:
        print(f"\n探测通行（可进入联网核对）：{len(result.passable)} 个资源项 {result.passable}")
    print(f"分流待人工：{len(result.issues)} 条")
    return 0


# audit：联网核对（预检分流 → 外呼审批 → Agent 逐资源项核对 → 证据链报告）
def _cmd_audit(folder: Path, yes: bool, limit: int, db: Path) -> int:
    """Compatibility-only M4 audit command; not the complete M5 workflow."""
    from award_audit.agent.llm import LlmClient
    from award_audit.agent.loop import verify_resource

    if not folder.is_dir():
        print(f"[错误] 批次文件夹不存在：{folder}", file=sys.stderr)
        return 2
    files = importer.import_batch(folder)
    reg = load_template_registry()
    ledger = load_ledger()

    print(f"\n联网核对 {folder.name}：先做链接预检…")
    pre = l5_precheck.run_batch(files, ledger, l5_precheck.default_prober)
    for i in pre.issues:
        print(f"  [{i.rule_id}] {i.message}")
    if not pre.passable:
        print("无探测通行的资源项，全部转人工。")
        return 0
    print(f"探测通行 {len(pre.passable)} 个资源项：{pre.passable}")

    by_code: dict[str, list[ImportedFile]] = {}
    for imp in files:
        by_code.setdefault(imp.first_zylbm, []).append(imp)

    llm = LlmClient()  # 默认档（抽取任务）
    workdir = config.out_dir() / "agent_downloads"
    reports = []
    for code in pre.passable[:limit]:
        members = by_code.get(code) or by_code.get(code.zfill(8)) or []
        if not members:
            continue
        urls = pre.passable_urls.get(code, [])
        award = members[0].award_name
        if not yes:
            ans = input(f"\n将访问官网核对「{award}」（{len(urls)} 个来源）。允许？[y/N/a(全部)] ").strip().lower()
            if ans == "a":
                yes = True
            elif ans != "y":
                print("  跳过")
                continue
        spec = reg.get(members[0].claimed_table_code)
        print(f"  核对中：{award} …")
        try:
            rep = verify_resource(code, members, urls, spec, llm, workdir, use_corpus=True)
        except Exception as exc:  # noqa: BLE001  单个资源出意外不杀整批，标转人工
            from award_audit.agent.loop import EvidenceReport
            rep = EvidenceReport(resource_code=code, award_name=award,
                                 year=members[0].year, source_urls=list(urls),
                                 submitted_count=sum(m.n_rows for m in members),
                                 notes=f"核对过程异常，转人工：{type(exc).__name__}: {str(exc)[:80]}")
            print(f"    [异常] {type(exc).__name__}: {str(exc)[:80]} —— 已转人工")
        reports.append(rep)
        tri = triage_label(decide_triage(rep.verdict, rep.confidence))
        print(f"  → [{rep.verdict}] 官网 {rep.extracted_count} 条 vs 提交 {rep.submitted_count} 行"
              f"（来源:{rep.source_kind} 置信:{rep.confidence} 分诊:{tri}）")
        if rep.missing:
            print(f"    疑漏采 {len(rep.missing)} 条，如：{rep.missing[:3]}")
        if rep.extra:
            print(f"    官网未匹配 {len(rep.extra)} 条，如：{rep.extra[:3]}")
        if rep.verdict == "无法核对":  # 转人工时把该查的网址直接给出来
            for u in rep.source_urls:
                print(f"    人工核对官网：{u}")
            for a in rep.found_assets[:5]:
                print(f"    名单文件/图片：{a}")

    # 证据链报告落盘
    out_md = config.out_dir() / f"联网核对-{folder.name}.md"
    lines = [f"# 联网核对报告 —— {folder.name}\n"]
    for r in reports:
        tri = triage_label(decide_triage(r.verdict, r.confidence))
        reasons = "；".join(reason_label(c) for c in r.reason_codes)
        lines += [f"## {r.award_name}（{r.resource_code}，{r.year}）",
                  f"- 结论：**{r.verdict}**（置信 {r.confidence} ｜ 分诊 {tri}）",
                  f"- 降级原因：{reasons or '无'}",
                  f"- 来源：{r.source_kind} {r.source_url}",
                  f"- 官网年份：{r.page_year or '未识别'} ｜ 官网 {r.extracted_count} 条 vs 提交 {r.submitted_count} 行",
                  f"- 疑漏采：{r.missing if r.missing else '无'}",
                  f"- 官网未匹配：{r.extra if r.extra else '无'}",
                  f"- 备注：{r.notes or '—'}",
                  "- 检索网址（供人工核对）：\n" + "\n".join(f"  - {u}" for u in r.source_urls),
                  "- 名单文件/图片：\n" + ("\n".join(f"  - {a}" for a in r.found_assets) or "  - （未发现）"),
                  "- 过程证据：\n" + "\n".join(f"  - {e}" for e in r.evidence), ""]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n证据链报告 → {out_md}")

    # 落台账：资源项级结论进复核台「联网核对」页终审（markdown 仅人读存档）
    if reports:
        store = Store(db)
        try:
            batch_id = store.find_or_create_batch(folder.name)
            store.add_audit_results(batch_id, [r.model_dump() for r in reports])
            print(f"已写入台账（批次 #{batch_id}，{len(reports)} 条）→ 复核台「联网核对」页可终审")
        finally:
            store.close()
    print("全部结论为 review 级，请在复核台/人工终审后处置。")
    return 0


def _cli_approve(target: l5_precheck.AuditTarget) -> bool:
    urls = "、".join(target.urls) or "无"
    domains = "、".join(target.domains) or "无"
    answer = input(
        f"\n将联网核对「{target.award_name}」；年份 {target.year}；"
        f"资源项 {target.resource_code}；提交 {target.submitted_count} 条；"
        f"域名 {domains}；URL {urls}。允许？[y/N] "
    ).strip().lower()
    return answer == "y"


# audit --m5：M5 受控 Harness 复现提交-14 真实审查（默认干跑；加 --confirm-real-api 才真跑）
def _cmd_audit_m5(args: argparse.Namespace) -> int:
    from award_audit.agent.harness import acceptance

    cfg = acceptance.AcceptanceConfig(
        mode=args.mode,
        cases=args.cases,
        manifest=args.manifest or acceptance.DEFAULT_MANIFEST,
        submission_dir=args.submission_dir,
        evidence_dir=args.evidence_dir or acceptance.DEFAULT_EVIDENCE_DIR,
        output=args.output,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_tool_calls=args.max_tool_calls,
        confirm_real_api=args.confirm_real_api,
        recover_db=args.recover_db,
        recover_label=args.recover_label,
    )
    try:
        _result, code = acceptance.execute(cfg, printer=print)
    except Exception as exc:  # noqa: BLE001  启动期失败（缺 manifest/Key/文件）脱敏退出，不吐栈
        print(f"[错误] M5 验收无法启动：{type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        return 2
    return code


# review：一站式统一入口——L0–L4 落台账 + L5 联网核对进复核队列，汇成一份统一反馈（check/import/audit 保留兼容）
def _cmd_review(
    folder: Path,
    yes: bool,
    limit: int,
    db: Path,
    *,
    run_m5: bool = False,
) -> int:
    from award_audit.agent.integration import case_report_rows
    from award_audit.agent.review_workflow import (
        prepare_review_batch,
        run_audit_stage,
        run_queued_review_cases,
    )

    if not folder.is_dir():
        print(f"[错误] 批次文件夹不存在：{folder}", file=sys.stderr)
        return 2

    store = Store(db)
    try:
        files = importer.import_batch(folder)  # 只解析一次，L0–L4 与 L5 共用
        reg = load_template_registry()
        rmap = load_resource_map()
        led = load_ledger()

        # L0–L4：共享应用服务保留解析结果，CLI/Web 后续使用同一建案上下文。
        prepared = prepare_review_batch(
            folder,
            store,
            imported_files=files,
            registry=reg,
            resource_map=rmap,
            ledger=led,
        )
        batch_id, result = prepared.batch_id, prepared.result
        _print_overview(result)  # 快结果先出（L5 联网慢）

        # M4：共享阶段统一执行 claim、按 (码,年) 核验、失败续跑与建案。
        print("\n联网核对（L5）：先做链接预检…")
        approved_count = 0

        def approve_target(target: l5_precheck.AuditTarget) -> bool:
            nonlocal approved_count
            if limit and approved_count >= limit:
                return False
            allowed = True if yes else _cli_approve(target)
            approved_count += int(allowed)
            return allowed

        stage = run_audit_stage(
            store,
            prepared,
            ledger=led,
            approve=None if yes and not limit else approve_target,
            workdir=config.out_dir() / "agent_downloads",
            use_corpus=False,
        )
        pre = stage.precheck
        for i in pre.issues:
            print(f"  [{i.rule_id}] {i.message}")
        dumps = list(stage.reports)
        bridge = stage.bridge
        if run_m5 and bridge.case_ids:
            evidence_dir = config.out_dir() / "m5_evidence" / f"batch-{batch_id}"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            print(f"\nM5 深度审核：执行 {len(bridge.case_ids)} 个疑难案件…")
            deep_results = run_queued_review_cases(
                db,
                batch_id,
                evidence_roots=[folder, evidence_dir],
                progress=lambda index, total, _case_id: print(
                    f"  案件 {index}/{total}"
                ),
            )
            waiting_count = sum(
                item["status"] == "waiting_human" for item in deep_results
            )
            print(
                f"M5 深度审核完成：处理 {len(deep_results)} 个，"
                f"待人工 {waiting_count} 个。"
            )
        m5_cases = case_report_rows(store, batch_id)
        xlsx, md = report.write_reports(
            result,
            audit_reports=dumps,
            audit_cases=m5_cases,
        )

        print(f"\n统一核查报告 → {xlsx}\n           → {md}")
        covered, passable = len(dumps), len(pre.passable_targets)
        note = "" if covered >= passable else "（其余转人工/被 --limit 截断，见预检分流）"
        print(f"L5 覆盖：已核对 {covered} / 探测通行 {passable} 个资源项{note}")
        print(
            f"M5 疑难案件：新增 {bridge.created}，复用 {bridge.existing}，"
            f"自动放行未建案 {bridge.skipped}，当前共 {len(m5_cases)} 个"
        )
        print(
            f"\n已落台账（批次 #{batch_id}，暂存·审核中）。复核台终审后："
            f"award-audit promote {batch_id}"
        )
        print(
            "  提示：联网核对页 x 打回某资源项 → promote 时该资源项全部行被拦"
            "（skipped_audit_rejected）"
        )
    finally:
        store.close()
    return 1 if result.count(Severity.BLOCKER) else 0


# collect：按采集清单批量下载官网 Excel 名单入参考库（离线核对的"预热"，未命中回源）
def _cmd_collect(limit: int, force: bool, yes: bool, only_code: str | None = None) -> int:
    from award_audit.agent import tools
    from award_audit.core.reference import corpus

    ledger = load_ledger()
    todo = [(code, e) for code, e in ledger.items()
            if e.collect_url.strip()
            and (only_code is None or code == only_code or code == only_code.zfill(8))]
    if not todo:
        who = f"资源项码 {only_code}" if only_code else "带采集网址的资源项"
        print(f"采集清单中没有{who}（或该项无采集网址）。")
        return 0
    if limit:
        todo = todo[:limit]
    print(f"\n批量采集入库：清单中 {len(todo)} 个带网址的资源项 → 参考库 {config.corpus_dir()}")
    if not yes:
        ans = input("将联网访问上述官网并下载名单入库。允许？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return 0

    workdir = config.out_dir() / "collect_downloads"
    n_ok = n_cached = n_skip = 0
    for code, e in todo:
        if not force and corpus.has(code):  # 已收录且未强制则跳过（公示名单冻结，无需重采）
            n_cached += 1
            continue
        urls = [u for u in l5_precheck.split_urls(e.collect_url) if l5_precheck.url_is_valid(u)]
        if not urls:
            n_skip += 1
            continue
        acquired = tools.acquire_excel_grid(urls, workdir)
        if acquired is None:  # 无可解析 Excel（正文/图片/死链）——留待审查时联网或人工
            print(f"  [跳过] {code} {e.resource_name}：未取到可解析的 Excel 名单")
            n_skip += 1
            continue
        meta = corpus.save(code, acquired.source_url, acquired.grid, raw_path=acquired.raw_path)
        print(f"  [入库] {code} {e.resource_name}：{meta.n_rows} 行，等级 {meta.sheets or '单表'}")
        n_ok += 1

    print(f"\n完成：入库 {n_ok}，已存跳过 {n_cached}，无Excel/网址跳过 {n_skip}。")
    print("未入库的资源项审查时会自动联网回源，取不到则转人工。")
    return 0


# 列出已加载模板（调试）
def _cmd_templates() -> int:
    reg = load_template_registry()
    print(f"已加载 {len(reg)} 个标准模板：\n")
    for code, spec in sorted(reg.items()):
        print(f"  {code}  [{spec.sheet_name}]  {len(spec.field_codes)}字段  "
              f"名称列={spec.title_col} 去重键={spec.dedup_key_cols}")
    return 0


# CLI 主入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="award-audit", description="评奖审核入库智能体（M1 核查 + M2 台账）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="核查一个批次（出反馈意见，不入库）")
    p_check.add_argument("folder", type=Path)

    p_import = sub.add_parser("import", help="核查并写入台账（暂存）")
    p_import.add_argument("folder", type=Path)
    p_import.add_argument("--db", type=Path, default=None)

    p_batches = sub.add_parser("batches", help="列出台账批次")
    p_batches.add_argument("--db", type=Path, default=None)

    p_promote = sub.add_parser("promote", help="把批次入正式库（版本化）")
    p_promote.add_argument("batch_id", type=int)
    p_promote.add_argument("--db", type=Path, default=None)

    p_hist = sub.add_parser("history", help="查某记录变更史（按片段匹配业务键）")
    p_hist.add_argument("fragment", type=str)
    p_hist.add_argument("--db", type=Path, default=None)

    p_pre = sub.add_parser("precheck", help="联网核对前的链接预检（L5P 分流）")
    p_pre.add_argument("folder", type=Path)
    p_pre.add_argument("--offline", action="store_true", help="不做 HTTP 探测，只查清单收录/网址登记/格式")

    p_audit = sub.add_parser("audit", help="联网核对（M4 逐资源项核对；--m5 走 M5 受控 Harness）")
    p_audit.add_argument("folder", type=Path, nargs="?", default=None,
                         help="批次文件夹（M4 路径必填；--m5 忽略，改由 manifest 驱动）")
    p_audit.add_argument("--yes", action="store_true", help="跳过逐项外呼审批")
    p_audit.add_argument("--limit", type=int, default=10, help="最多核对的资源项数（默认 10）")
    p_audit.add_argument("--db", type=Path, default=None)
    # M5：受控 Harness 复现提交-14 真实审查；不带 --confirm-real-api 只做干跑校验（不烧 token）
    p_audit.add_argument("--m5", action="store_true",
                         help="用 M5 受控 Harness 跑验收 manifest（复现提交-14 真实审查）")
    p_audit.add_argument(
        "--mode",
        choices=("e2e", "m5_regression"),
        default="e2e",
        help="M5：e2e 跑完整决策链（默认）；m5_regression 强制案例进入 M5 回归",
    )
    p_audit.add_argument("--cases", default="all", help="M5：逗号分隔 case id 或 all（默认 all）")
    p_audit.add_argument("--manifest", type=Path, default=None,
                         help="M5：验收清单（默认 tests/data/m5_real/submission14_manifest.json）")
    p_audit.add_argument("--submission-dir", default="", help="M5：覆盖 manifest 里的提交目录")
    p_audit.add_argument("--evidence-dir", type=Path, default=None, help="M5：证据落盘目录")
    p_audit.add_argument("--output", type=Path, default=None, help="M5：脱敏结果 JSON 输出路径")
    p_audit.add_argument("--max-steps", type=int, default=12, help="M5：单案步数上限")
    p_audit.add_argument("--max-tokens", type=int, default=50000, help="M5：单案 Token 上限")
    p_audit.add_argument("--max-tool-calls", type=int, default=10, help="M5：单案 Tool 上限")
    p_audit.add_argument("--confirm-real-api", action="store_true",
                         help="M5：真的调用真实 API（约 10 万 token）；不加只做干跑校验")
    p_audit.add_argument("--recover-db", type=Path, default=None,
                         help="M5：从已完成但报告失败的库恢复脱敏结果，不重调 API")
    p_audit.add_argument(
        "--recover-label",
        default="RECOVERED_CASE",
        help="M5：恢复模式的显示标签",
    )

    p_review = sub.add_parser("review", help="一站式核查（L0–L5 汇一份统一结论；check/import/audit 保留兼容）")
    p_review.add_argument("folder", type=Path)
    p_review.add_argument("--yes", action="store_true", help="跳过逐项外呼审批")
    p_review.add_argument("--limit", type=int, default=0, help="最多联网核对的资源项数（0=全部）")
    p_review.add_argument("--db", type=Path, default=None)
    p_review.add_argument(
        "--run-m5",
        action="store_true",
        help="建立疑难案件后立即运行 M5 深度取证；会调用配置的真实模型和搜索服务",
    )

    p_collect = sub.add_parser("collect", help="按采集清单批量下载官网名单入参考库（离线核对预热）")
    p_collect.add_argument("--limit", type=int, default=0, help="最多采集的资源项数（0=全部）")
    p_collect.add_argument("--code", type=str, default=None, help="只采集指定资源项码（精准测试用）")
    p_collect.add_argument("--force", action="store_true", help="已入库的也重新采集覆盖")
    p_collect.add_argument("--yes", action="store_true", help="跳过联网确认")

    sub.add_parser("templates", help="列出已加载模板（调试）")

    args = parser.parse_args(argv)
    db: Path = getattr(args, "db", None) or config.db_path()
    if args.cmd == "check":
        return _cmd_check(args.folder)
    if args.cmd == "import":
        return _cmd_import(args.folder, db)
    if args.cmd == "batches":
        return _cmd_batches(db)
    if args.cmd == "promote":
        return _cmd_promote(args.batch_id, db)
    if args.cmd == "history":
        return _cmd_history(args.fragment, db)
    if args.cmd == "precheck":
        return _cmd_precheck(args.folder, args.offline)
    if args.cmd == "audit":
        if getattr(args, "m5", False):
            return _cmd_audit_m5(args)
        if args.folder is None:
            print("[错误] audit 需要批次文件夹（或用 --m5 跑 M5 验收）", file=sys.stderr)
            return 2
        return _cmd_audit(args.folder, args.yes, args.limit, db)
    if args.cmd == "review":
        return _cmd_review(
            args.folder,
            args.yes,
            args.limit,
            db,
            run_m5=args.run_m5,
        )
    if args.cmd == "collect":
        return _cmd_collect(args.limit, args.force, args.yes, args.code)
    if args.cmd == "templates":
        return _cmd_templates()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
