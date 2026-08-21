"""核查规则共用小工具：中文/英文判定、年份提取、多值切分、字符集检测。

这些是 L1/L2 规则频繁用到的确定性判定，抽出来集中测试、避免各处重复。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[一-鿿]")
_YEAR = re.compile(r"(19|20)\d{2}")
_ASCII_LETTERS = re.compile(r"[A-Za-z]")
# 分号分隔符（半角/全角都接受为正确分隔）
_SEMI = re.compile(r"[;；]")
# 非法多值分隔符：中文顿号/中文逗号/英文逗号/斜杠/竖线（分号不算错）
_BAD_SEP = re.compile(r"[、，,/｜|]")


# 是否含中文字符
def has_cjk(s: str) -> bool:
    return _CJK.search(s) is not None


# 是否含英文字母
def has_latin(s: str) -> bool:
    return _ASCII_LETTERS.search(s) is not None


# 从文本中提取第一个 4 位年份（19xx/20xx），无则 None
def extract_year(s: str) -> str | None:
    m = _YEAR.search(s)
    return m.group(0) if m else None


# 按分号（半角/全角）切分多值，去空白与空项
def split_multi(s: str) -> list[str]:
    return [p.strip() for p in _SEMI.split(s) if p.strip()]


# 是否含非法多值分隔符（说明多值没用中文 ; 或夹了别的符号）
def has_bad_separator(s: str) -> bool:
    return _BAD_SEP.search(s) is not None


# 中文姓名内部是否有空格（半角/全角），如 "张 楠"
def cjk_has_inner_space(name: str) -> bool:
    return has_cjk(name) and (" " in name or "　" in name)


# 英文姓名内部是否有空格（应改用 - 连接），如 "John Smith"
def latin_has_inner_space(name: str) -> bool:
    return has_latin(name) and not has_cjk(name) and " " in name.strip()
