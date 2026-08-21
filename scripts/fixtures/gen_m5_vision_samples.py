"""生成 M5.0 视觉探针的受控中文名单图 + 标注（幂等，可重复运行）。

产出 tests/data/m5_golden/vision/ 下：
- clean_roster.png      清晰获奖名单表格（序号/姓名/单位/等级），供视觉抽取基线
- scan_roster.png       同名单加噪+轻微旋转+压暗，模拟扫描件，测降级鲁棒性
- multi_track_roster.png 含两个赛道分段（一等奖/二等奖），测分段与覆盖识别
每张配 *.expected.json：标注应抽出条目、首尾序号、总数、赛道段——作 F1 与覆盖判定基准。

用法（award-audit 目录）：python scripts/fixtures/gen_m5_vision_samples.py
不依赖网络，不进主包，纯离线夹具生成。
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path("tests/data/m5_golden/vision")
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"       # 微软雅黑
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

# 清晰单赛道名单的条目（序号, 姓名, 单位, 等级）
CLEAN_ROWS = [
    (1, "张伟明", "清华大学", "一等奖"),
    (2, "李思远", "北京大学", "一等奖"),
    (3, "王慧敏", "浙江大学", "一等奖"),
    (4, "陈国栋", "复旦大学", "二等奖"),
    (5, "刘雅琴", "上海交通大学", "二等奖"),
    (6, "赵鹏飞", "南京大学", "二等奖"),
    (7, "孙梦洁", "武汉大学", "三等奖"),
    (8, "周建华", "中山大学", "三等奖"),
]

# 多赛道名单：两个分段，各自序号从 1 起（测跨段序号与覆盖）
TRACK_A = ("智慧城市赛道", [
    (1, "郑晓峰", "哈尔滨工业大学", "特等奖"),
    (2, "黄丽萍", "西安交通大学", "一等奖"),
    (3, "吴俊杰", "华中科技大学", "一等奖"),
])
TRACK_B = ("人工智能赛道", [
    (1, "许文博", "电子科技大学", "特等奖"),
    (2, "冯雪莲", "同济大学", "一等奖"),
    (3, "曹志强", "天津大学", "二等奖"),
    (4, "范秋实", "厦门大学", "二等奖"),
])


# 画一张表格图：标题 + 表头 + 数据行，返回 PIL Image
def _draw_table(title: str, header: tuple[str, ...], rows: list, *,
                width: int = 900, row_h: int = 52) -> Image.Image:
    title_font = ImageFont.truetype(FONT_BOLD, 30)
    head_font = ImageFont.truetype(FONT_BOLD, 22)
    cell_font = ImageFont.truetype(FONT_PATH, 22)
    top = 90
    height = top + row_h * (len(rows) + 1) + 40
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    # 标题居中
    tw = d.textbbox((0, 0), title, font=title_font)[2]
    d.text(((width - tw) / 2, 28), title, fill="black", font=title_font)
    # 列 x 边界：序号/姓名/单位/等级
    xs = [40, 140, 340, 660, width - 40]
    y = top
    # 表头
    d.rectangle([xs[0], y, xs[-1], y + row_h], fill="#e8e8e8", outline="black")
    for i, h in enumerate(header):
        d.text((xs[i] + 12, y + 14), h, fill="black", font=head_font)
    y += row_h
    # 数据行
    for r in rows:
        d.rectangle([xs[0], y, xs[-1], y + row_h], outline="black")
        for i, val in enumerate(r):
            d.text((xs[i] + 12, y + 14), str(val), fill="black", font=cell_font)
        y += row_h
    # 竖线
    for x in xs:
        d.line([x, top, x, y], fill="black")
    return img


# 把清晰图劣化成扫描件观感：轻微旋转 + 高斯模糊 + 压暗对比 + 椒盐噪点
def _degrade(img: Image.Image) -> Image.Image:
    import random

    rng = random.Random(42)                       # 固定种子，产物可复现
    img = img.rotate(-1.5, expand=True, fillcolor="white")
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):            # 0.4% 椒盐噪点
        x, y = rng.randrange(w), rng.randrange(h)
        px[x, y] = (0, 0, 0) if rng.random() < 0.5 else (180, 180, 180)
    return img.point(lambda v: int(v * 0.82 + 10))  # 整体压暗，模拟复印


# 写标注 JSON
def _write_expected(stem: str, meta: dict) -> None:
    (OUT / f"{stem}.expected.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    header = ("序号", "姓名", "单位", "等级")

    # 1) 清晰单赛道
    clean = _draw_table("2024年度全国青年科技创新奖获奖名单", header, CLEAN_ROWS)
    clean.save(OUT / "clean_roster.png")
    _write_expected("clean_roster", {
        "is_roster": True, "single_track": True,
        "total": len(CLEAN_ROWS), "first_no": 1, "last_no": len(CLEAN_ROWS),
        "entries": [{"no": n, "name": nm, "org": og, "level": lv} for n, nm, og, lv in CLEAN_ROWS],
        "truncated": False,
    })

    # 2) 扫描件（同名单劣化）
    scan = _degrade(clean.copy())
    scan.save(OUT / "scan_roster.png")
    _write_expected("scan_roster", {
        "is_roster": True, "single_track": True, "degraded": "rotate+blur+noise+darken",
        "total": len(CLEAN_ROWS), "first_no": 1, "last_no": len(CLEAN_ROWS),
        "entries": [{"no": n, "name": nm, "org": og, "level": lv} for n, nm, og, lv in CLEAN_ROWS],
        "truncated": False,
    })

    # 3) 多赛道（两段拼接为一张）
    (ta_name, ta_rows), (tb_name, tb_rows) = TRACK_A, TRACK_B
    imgs = [_draw_table(f"第九届XX大赛 · {ta_name}", header, ta_rows),
            _draw_table(f"第九届XX大赛 · {tb_name}", header, tb_rows)]
    w = max(i.width for i in imgs)
    h = sum(i.height for i in imgs)
    multi = Image.new("RGB", (w, h), "white")
    y = 0
    for im in imgs:
        multi.paste(im, (0, y))
        y += im.height
    multi.save(OUT / "multi_track_roster.png")
    _write_expected("multi_track_roster", {
        "is_roster": True, "single_track": False,
        "tracks": [
            {"track": ta_name, "total": len(ta_rows), "first_no": 1, "last_no": len(ta_rows),
             "entries": [{"no": n, "name": nm, "org": og, "level": lv} for n, nm, og, lv in ta_rows]},
            {"track": tb_name, "total": len(tb_rows), "first_no": 1, "last_no": len(tb_rows),
             "entries": [{"no": n, "name": nm, "org": og, "level": lv} for n, nm, og, lv in tb_rows]},
        ],
        "total": len(ta_rows) + len(tb_rows), "truncated": False,
    })

    print(f"已生成 3 张图 + 标注 -> {OUT}")
    for p in sorted(OUT.glob("*.png")):
        print("  ", p.name, Image.open(p).size)


if __name__ == "__main__":
    main()
