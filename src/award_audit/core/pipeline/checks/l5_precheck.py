"""L5-pre 链接预检（L5P-01 ~ L5P-05）：联网核对前的确定性分流，不用 LLM。

流程（对应实施方案 M4 设计图 ①–④）：
① 清单有此资源项吗 → ② 有采集网址吗 → ③ URL 格式合法吗
→ ④ HTTP 探测（200 通行 / 401·403 要权限 / 其余不可达）。
只有"通行"的资源项才进入 L5-agent 联网核对；其余全部 review 级分流转人工，绝不硬闯。

探测器可注入（测试用 fake，不联网）；prober=None 为离线模式，只做 ①②③。
按资源项码聚合：一个码一条结论（同码多文件不重复报），挂首个成员文件。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.reference.ledger import LedgerEntry

# 探测器签名：url -> (HTTP状态码或None, 错误/说明)。None 状态码 = 连接层失败（超时/DNS/拒连）
Prober = Callable[[str], tuple[int | None, str]]

# 清单"采集网址"是多值字段：分号（中英）/换行/空白分隔多个 URL（实测 提交-13 的研创赛条目）
_URL_SEP = re.compile(r"[;；\n\r\s]+")


# 拆分采集网址字段为多个 URL（去空项）
def split_urls(url_field: str) -> list[str]:
    return [u for u in _URL_SEP.split(url_field.strip()) if u]


# URL 格式是否合法（http/https + 有主机名）——对外复用（collect 采集命令等）
def url_is_valid(url: str) -> bool:
    return _url_ok(url)


class SearchHandoff(BaseModel):
    """Core-neutral seed consumed by the M5.4 case builder, not an AuditCase itself."""

    resource_code: str
    award_name: str
    year: str = ""
    trigger_code: Literal["SOURCE_URL_MISSING", "SOURCE_UNREACHABLE"]
    objective: str
    known_urls: list[str] = Field(default_factory=list)


class AuditTarget(BaseModel):
    """一个 (资源项码,年) 的联网核对目标：奖项/年/URL/域名/提交数。

    类型化避免四串位置参数顺序出错（P0-8）；供逐项审批展示、L5 执行、Web 预览共用。
    """

    resource_code: str
    year: str = ""
    award_name: str = ""
    urls: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    submitted_count: int = 0
    probe_status: Literal["not_checked", "passable"] = "not_checked"


class PrecheckResult(BaseModel):
    """一次批次预检的产出：问题分流 + 可进入 Agent 核对的通行清单。"""

    issues: list[Issue]
    passable: list[str] = []  # 探测通行（≥1 个 URL 返回 200）的资源项码
    passable_urls: dict[str, list[str]] = {}  # 资源项码 → 通行的 URL 们（交给 L5-agent）
    passable_targets: list[AuditTarget] = Field(default_factory=list)  # 按 (码,年) 展开的核对目标
    candidate_targets: list[AuditTarget] = Field(default_factory=list)
    retry_targets: list[AuditTarget] = Field(default_factory=list)
    search_handoffs: list[SearchHandoff] = Field(default_factory=list)
    offline: bool = False  # 是否离线模式（未做 HTTP 探测）


def _search_handoff(
    member: ImportedFile,
    *,
    resource_code: str,
    award_name: str,
    trigger_code: Literal["SOURCE_URL_MISSING", "SOURCE_UNREACHABLE"],
    known_urls: list[str] | None = None,
) -> SearchHandoff:
    objective = (
        "查找该奖项对应年份/届次的主管方或主办方正式公示与完整名单"
        if trigger_code == "SOURCE_URL_MISSING"
        else "查找登记来源的可核验官方替代页面、附件或历史公示"
    )
    return SearchHandoff(
        resource_code=resource_code,
        award_name=award_name or member.award_name or resource_code,
        year=member.year,
        trigger_code=trigger_code,
        objective=objective,
        known_urls=known_urls or [],
    )


# URL 是否格式合法（http/https + 有主机名）
def _url_ok(url: str) -> bool:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


# URL 主机名（去 www. 前缀，脱敏/展示用；取不到返回 ""）
def _host(url: str) -> str:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# 标准浏览器请求头：部分站点（如 cpipc）对程序化 UA 返回 403，但同一公开页面浏览器可正常访问——
# 用标准头访问公开内容属正常采集（不涉登录/验证码）；实测浏览器可开而程序 403 的根因即 UA 检测
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


# 默认探测器：httpx HEAD 优先、失败退 GET（stream 不下正文）；未安装 httpx 时抛 RuntimeError
def default_prober(url: str, timeout: float = 10.0) -> tuple[int | None, str]:
    try:
        import httpx
    except ImportError as exc:  # 预检在线模式需要 httpx（pip install httpx）
        raise RuntimeError("在线预检需要 httpx：pip install httpx（或改用 --offline）") from exc
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            try:
                resp = client.head(url)
                if resp.status_code in (
                    405,
                    501,
                    403,
                ):  # HEAD 被拒时退 GET 再确认（部分 WAF 只拦 HEAD）
                    raise httpx.HTTPStatusError("HEAD 被拒", request=resp.request, response=resp)
            except httpx.HTTPStatusError:
                with client.stream("GET", url) as resp2:
                    return resp2.status_code, ""
            return resp.status_code, ""
    except httpx.TimeoutException:
        return None, "超时"
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"


# 跑批次预检：按资源项码聚合分流；prober=None 为离线模式（跳过 HTTP 探测）
def run_batch(
    files: list[ImportedFile],
    ledger: dict[str, LedgerEntry],
    prober: Prober | None = None,
) -> PrecheckResult:
    issues: list[Issue] = []
    passable: list[str] = []
    passable_urls: dict[str, list[str]] = {}
    search_handoffs: list[SearchHandoff] = []
    candidate_targets: list[AuditTarget] = []
    passable_targets: list[AuditTarget] = []
    retry_targets: list[AuditTarget] = []

    # 审核身份按 (归一码,年)；同码 URL 只探测一次，再展开到各年份目标。
    groups: dict[tuple[str, str], list[ImportedFile]] = {}
    for imp in files:
        raw_code = imp.first_zylbm.strip()
        code = raw_code.zfill(8) if raw_code.isdigit() else raw_code.casefold()
        if code:
            groups.setdefault((code, imp.year.strip()), []).append(imp)

    code_groups: dict[str, list[tuple[str, list[ImportedFile]]]] = {}
    for (code, year), members in groups.items():
        code_groups.setdefault(code, []).append((year, members))

    for code, year_groups in code_groups.items():
        # ① 清单收录了吗（码位数不足时尝试补零匹配——前导零丢失是 Excel 的经典错误，
        #    补零命中则容错继续用清单条目找 URL，前导零本身的错误由 L1-03 负责报）
        entry = ledger.get(code)
        if entry is None and code.zfill(8) != code:
            entry = ledger.get(code.zfill(8))
        if entry is None:
            for _year, members in year_groups:
                member = members[0]
                scope = f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""
                issues.append(
                    make_issue(
                        "L5P-01", batch=member.batch, file=member.file_name,
                        sheet=member.sheet_name, field_code="ZYLBM",
                        message=f"资源项码 {code} 不在采集清单中，无来源网址可核{scope}",
                        current_value=code,
                    )
                )
                search_handoffs.append(
                    _search_handoff(
                        member, resource_code=code, award_name=member.award_name,
                        trigger_code="SOURCE_URL_MISSING",
                    )
                )
            continue

        # ② 有采集网址吗
        url = entry.collect_url.strip()
        if not url:
            for _year, members in year_groups:
                member = members[0]
                scope = f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""
                issues.append(
                    make_issue(
                        "L5P-02", batch=member.batch, file=member.file_name,
                        sheet=member.sheet_name, field_code="ZYLBM",
                        message=(f"资源项 {entry.resource_name or code} 在清单中未登记"
                                 f"采集网址，无法联网核对{scope}"),
                        current_value=code,
                    )
                )
                search_handoffs.append(
                    _search_handoff(
                        member, resource_code=code, award_name=entry.resource_name,
                        trigger_code="SOURCE_URL_MISSING",
                    )
                )
            continue

        # ③ 拆分多值网址并逐个判格式（一个条目常塞多个 URL，整串探测会误判 403）
        urls = split_urls(url)
        valid_urls = [u for u in urls if _url_ok(u)]
        if not valid_urls:
            for _year, members in year_groups:
                member = members[0]
                scope = f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""
                issues.append(
                    make_issue(
                        "L5P-03", batch=member.batch, file=member.file_name,
                        sheet=member.sheet_name,
                        message=(f"资源项 {entry.resource_name or code} 的 {len(urls)} 个"
                                 f"采集网址格式均异常{scope}"),
                        current_value=url[:120],
                    )
                )
                search_handoffs.append(_search_handoff(
                    member, resource_code=code, award_name=entry.resource_name,
                    trigger_code="SOURCE_UNREACHABLE",
                ))
            continue

        targets = [AuditTarget(
            resource_code=code,
            year=year,
            award_name=members[0].award_name or entry.resource_name,
            urls=valid_urls,
            domains=list(dict.fromkeys(h for item in valid_urls if (h := _host(item)))),
            submitted_count=sum(member.n_rows for member in members),
            probe_status="not_checked",
        ) for year, members in year_groups]
        candidate_targets.extend(targets)

        # ④ HTTP 逐个探测（离线模式跳过，视为待探测不入 passable）：
        #    ≥1 个 200 即通行（有可核对的来源就能干活）；全部失败按主导原因分流
        if prober is None:
            continue
        ok_urls: list[str] = []
        n_auth = n_dead = n_inconclusive = 0
        detail_parts: list[str] = []
        for u in valid_urls:
            status, detail = prober(u)
            if status == 200:
                ok_urls.append(u)
            elif status in (401, 403):
                n_auth += 1
                detail_parts.append(f"HTTP {status}: {u}")
            elif status is None:
                n_inconclusive += 1
                detail_parts.append(f"{detail or '连接失败'}: {u}")
            else:
                n_dead += 1
                detail_parts.append(f"HTTP {status}: {u}")
        if ok_urls:
            passable.append(code)
            passable_urls[code] = ok_urls
            passable_targets.extend(target.model_copy(update={
                # One reachable URL admits this resource to M4, but M4 must keep
                # every registered candidate available for bounded discovery. A
                # redirecting current page must not hide an older official notice.
                "urls": valid_urls,
                "domains": list(dict.fromkeys(
                    host for item in valid_urls if (host := _host(item))
                )),
                "probe_status": "passable",
            }) for target in targets)
        elif n_inconclusive:
            # A probe timeout is not evidence that a registered public source is
            # unavailable. Let M4 make its bounded fetch attempt and keep the
            # precheck outcome visible separately from a confirmed HTTP success.
            retry_targets.extend(targets)
            for _year, members in year_groups:
                member = members[0]
                issues.append(make_issue(
                    "L5P-05",
                    batch=member.batch, file=member.file_name, sheet=member.sheet_name,
                    message=f"{n_inconclusive} 个采集网址预探测未完成，M4 将使用已登记 URL 重试："
                    + "；".join(detail_parts[:3])
                    + (f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""),
                    current_value=valid_urls[0],
                ))
        elif n_auth >= n_dead:
            for _year, members in year_groups:
                member = members[0]
                issues.append(make_issue(
                    "L5P-04",
                    batch=member.batch, file=member.file_name, sheet=member.sheet_name,
                    message=(
                        f"{len(valid_urls)} 个采集网址均不可核对"
                        f"（{n_auth} 个需登录/权限），转人工："
                    )
                    + "；".join(detail_parts[:3])
                    + (f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""),
                    current_value=valid_urls[0],
                ))
        else:
            for _year, members in year_groups:
                member = members[0]
                issues.append(make_issue(
                    "L5P-05",
                    batch=member.batch, file=member.file_name, sheet=member.sheet_name,
                    message=f"{len(valid_urls)} 个采集网址均不可达，待人工更新清单："
                    + "；".join(detail_parts[:3])
                    + (f"（涉及 {len(members)} 个文件）" if len(members) > 1 else ""),
                    current_value=valid_urls[0],
                ))
                search_handoffs.append(
                    _search_handoff(
                        member, resource_code=code, award_name=entry.resource_name,
                        trigger_code="SOURCE_UNREACHABLE", known_urls=valid_urls,
                    )
                )

    return PrecheckResult(
        issues=issues,
        passable=passable,
        passable_urls=passable_urls,
        passable_targets=passable_targets,
        candidate_targets=candidate_targets,
        retry_targets=retry_targets,
        search_handoffs=search_handoffs,
        offline=(prober is None),
    )
