"""参照数据路径解析。

设计：三件套标准答案（模板库 / 资源项码映射 / 采集清单）是系统地基，路径解析带回退——
项目内 ``reference/`` 优先（自包含、可交付），回退到同级 ``评奖信息核查/``（开发期直接用原始资料），
也可用环境变量 ``AWARD_AUDIT_REFERENCE`` 覆盖。让项目"复制了能跑、没复制也能跑"。
"""

from __future__ import annotations

import os
from threading import Lock
from pathlib import Path

# 项目根：本文件在 award-audit/src/award_audit/core/config.py，向上 3 层到 award-audit/
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_LOAD_LOCK = Lock()
_LOADED_ENV_FILES: set[Path] = set()

# 同级原始资料目录（开发期回退用）
_RAW_DIR = PROJECT_ROOT.parent / "评奖信息核查"
_RAW_TEMPLATES = _RAW_DIR / "附件5：数据采集模板"
_RAW_RESOURCE_MAP = _RAW_DIR / "数据资源对应模板-1016.xlsx"


# 解析参照数据根目录：环境变量 > 项目内 reference/
def _reference_root() -> Path:
    env = os.environ.get("AWARD_AUDIT_REFERENCE")
    if env:
        return Path(env)
    return PROJECT_ROOT / "reference"


# 解析模板库目录：项目内 reference/templates 有 .xlsx 则用它，否则回退原始资料
def templates_dir() -> Path:
    inside = _reference_root() / "templates"
    if inside.is_dir() and any(inside.glob("*.xlsx")):
        return inside
    return _RAW_TEMPLATES


# 解析资源项码映射表（1016）：项目内优先，否则回退原始资料
def resource_map_path() -> Path:
    inside = _reference_root() / "resource_map.xlsx"
    if inside.is_file():
        return inside
    return _RAW_RESOURCE_MAP


# 解析采集清单（2025，L3 查全用，M2 起）：项目内优先，否则回退原始资料
def ledger_path() -> Path:
    inside = _reference_root() / "collection_ledger.xlsx"
    if inside.is_file():
        return inside
    return _RAW_DIR / "网络数据采集-2025（1218）.xlsx"


# 解析报告输出目录（反馈意见落地处），不存在则创建
def out_dir() -> Path:
    d = PROJECT_ROOT / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 解析台账 SQLite 库路径（M2）；可用环境变量 AWARD_AUDIT_DB 覆盖
def db_path() -> Path:
    env = os.environ.get("AWARD_AUDIT_DB")
    return Path(env) if env else PROJECT_ROOT / "award_audit.db"


# 解析参考库根目录（M4 参考库/RAG）：缓存官网名单原件+网格；可用环境变量 AWARD_AUDIT_CORPUS 覆盖
def corpus_dir() -> Path:
    env = os.environ.get("AWARD_AUDIT_CORPUS")
    return Path(env) if env else PROJECT_ROOT / "data" / "reference_corpus"


# 加载项目根的 .env 到 os.environ（已存在的环境变量不覆盖；无 .env 静默跳过）。
# 安全约定：key 只进本进程内存，绝不写日志/报告/台账；.env 已被 .gitignore 与权限锁双重排除。
def load_env(path: Path | None = None) -> None:
    p = (path or PROJECT_ROOT / ".env").resolve(strict=False)
    with _ENV_LOAD_LOCK:
        if p in _LOADED_ENV_FILES:
            return
        if p.is_file():
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
        _LOADED_ENV_FILES.add(p)
