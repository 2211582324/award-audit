"""Generate deterministic M5 P3 PDF/OCR golden samples.

Outputs three two-page Chinese roster PDFs plus page-level expected JSON:

- digital_roster.pdf: both pages contain selectable vector text.
- scanned_roster.pdf: both pages are degraded raster images with no PDF text layer.
- mixed_roster.pdf: page 1 is digital and page 2 is scanned.

The samples contain synthetic names and institutions only. No network or API key is used.
Run from the award-audit project root:

    python scripts/fixtures/gen_m5_pdf_samples.py
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import tempfile
from pathlib import Path
from typing import Any

OUT = Path("tests/data/m5_golden/pdf")
PAGE_ROWS = 8
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
)

ROWS = [
    (1, "张伟明", "清华大学", "一等奖"),
    (2, "李思远", "北京大学", "一等奖"),
    (3, "王慧敏", "浙江大学", "一等奖"),
    (4, "陈国栋", "复旦大学", "一等奖"),
    (5, "刘雅琴", "上海交通大学", "二等奖"),
    (6, "赵鹏飞", "南京大学", "二等奖"),
    (7, "孙梦洁", "武汉大学", "二等奖"),
    (8, "周建华", "中山大学", "二等奖"),
    (9, "郑晓峰", "哈尔滨工业大学", "二等奖"),
    (10, "黄丽萍", "西安交通大学", "三等奖"),
    (11, "吴俊杰", "华中科技大学", "三等奖"),
    (12, "许文博", "电子科技大学", "三等奖"),
    (13, "冯雪莲", "同济大学", "三等奖"),
    (14, "曹志强", "天津大学", "三等奖"),
    (15, "范秋实", "厦门大学", "优秀奖"),
    (16, "蒋欣怡", "东南大学", "优秀奖"),
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pil_font_path() -> Path:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到中文字体，请安装微软雅黑、宋体或黑体后重试")


def _entries() -> list[dict[str, Any]]:
    return [
        {"no": no, "name": name, "org": org, "level": level,
         "page": (index // PAGE_ROWS) + 1}
        for index, (no, name, org, level) in enumerate(ROWS)
    ]


def _draw_digital_page(canvas: Any, page_rows: list[tuple[int, str, str, str]],
                       page_no: int, page_count: int) -> None:
    from reportlab.lib.pagesizes import A4

    width, height = A4
    canvas.setFont("STSong-Light", 17)
    canvas.drawCentredString(width / 2, height - 52, "2025年度全国青年科技创新奖获奖名单")
    canvas.setFont("STSong-Light", 10)
    canvas.drawCentredString(width / 2, height - 72, "受控合成样本 - 仅用于 M5 P3 PDF/OCR 探针")

    left, right = 42, width - 42
    column_x = [left, 88, 176, 420, right]
    top, row_height = height - 108, 43
    headers = ("序号", "姓名", "单位", "等级")
    canvas.setFillColorRGB(0.90, 0.93, 0.96)
    canvas.rect(left, top - row_height, right - left, row_height, fill=1, stroke=1)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont("STSong-Light", 11)
    for index, header in enumerate(headers):
        canvas.drawCentredString((column_x[index] + column_x[index + 1]) / 2,
                                 top - 27, header)
    for row_index, row in enumerate(page_rows, start=1):
        y_top = top - row_height * row_index
        canvas.rect(left, y_top - row_height, right - left, row_height, fill=0, stroke=1)
        for index, value in enumerate(row):
            canvas.drawCentredString((column_x[index] + column_x[index + 1]) / 2,
                                     y_top - 27, str(value))
    bottom = top - row_height * (len(page_rows) + 1)
    for x in column_x:
        canvas.line(x, top, x, bottom)
    canvas.setFont("STSong-Light", 9)
    canvas.drawCentredString(width / 2, 32, f"第 {page_no} 页 / 共 {page_count} 页")
    canvas.drawRightString(right, 32, f"本页 {len(page_rows)} 条，总计 {len(ROWS)} 条")


def _draw_scan_page(path: Path, page_rows: list[tuple[int, str, str, str]],
                    page_no: int, page_count: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    width, height = 1240, 1754
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = _pil_font_path()
    title_font = ImageFont.truetype(str(font_path), 38)
    body_font = ImageFont.truetype(str(font_path), 27)
    small_font = ImageFont.truetype(str(font_path), 21)
    title = "2025年度全国青年科技创新奖获奖名单"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - title_box[2]) / 2, 82), title, fill=(15, 15, 15), font=title_font)
    subtitle = "受控合成样本 - 仅用于 M5 P3 PDF/OCR 探针"
    subtitle_box = draw.textbbox((0, 0), subtitle, font=small_font)
    draw.text(((width - subtitle_box[2]) / 2, 142), subtitle,
              fill=(55, 55, 55), font=small_font)

    column_x = [80, 190, 390, 920, 1160]
    top, row_height = 230, 84
    headers = ("序号", "姓名", "单位", "等级")
    draw.rectangle((column_x[0], top, column_x[-1], top + row_height),
                   fill=(225, 232, 239), outline=(20, 20, 20), width=2)
    for index, header in enumerate(headers):
        box = draw.textbbox((0, 0), header, font=body_font)
        x = (column_x[index] + column_x[index + 1] - box[2]) / 2
        draw.text((x, top + 24), header, fill=(20, 20, 20), font=body_font)
    for row_index, row in enumerate(page_rows, start=1):
        y = top + row_height * row_index
        draw.rectangle((column_x[0], y, column_x[-1], y + row_height),
                       outline=(30, 30, 30), width=2)
        for index, value in enumerate(row):
            text = str(value)
            box = draw.textbbox((0, 0), text, font=body_font)
            x = (column_x[index] + column_x[index + 1] - box[2]) / 2
            draw.text((x, y + 24), text, fill=(20, 20, 20), font=body_font)
    bottom = top + row_height * (len(page_rows) + 1)
    for x in column_x:
        draw.line((x, top, x, bottom), fill=(30, 30, 30), width=2)
    footer = (f"第 {page_no} 页 / 共 {page_count} 页    "
              f"本页 {len(page_rows)} 条，总计 {len(ROWS)} 条")
    footer_box = draw.textbbox((0, 0), footer, font=small_font)
    draw.text(((width - footer_box[2]) / 2, height - 95), footer,
              fill=(60, 60, 60), font=small_font)

    rng = random.Random(4200 + page_no)
    image = image.filter(ImageFilter.GaussianBlur(0.45 if page_no == 1 else 0.7))
    pixels = image.load()
    for _ in range(int(width * height * 0.0015)):
        x, y = rng.randrange(width), rng.randrange(height)
        shade = rng.choice((0, 90, 180, 220))
        pixels[x, y] = (shade, shade, shade)
    image = image.point(lambda value: int(value * 0.88 + 12))
    image.save(path, format="PNG", optimize=True)


def _write_pdf(path: Path, page_modes: list[str], scan_images: list[Path]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    document = canvas.Canvas(str(path), pagesize=A4, pageCompression=1, invariant=1)
    document.setTitle("M5 P3 受控中文获奖名单")
    document.setAuthor("award-audit synthetic probe")
    scan_readers = [ImageReader(io.BytesIO(image.read_bytes())) for image in scan_images]
    for page_index, mode in enumerate(page_modes):
        page_rows = ROWS[page_index * PAGE_ROWS:(page_index + 1) * PAGE_ROWS]
        if mode == "digital":
            _draw_digital_page(document, page_rows, page_index + 1, len(page_modes))
        else:
            document.drawImage(scan_readers[page_index], 0, 0,
                               width=A4[0], height=A4[1], preserveAspectRatio=False)
        document.showPage()
    document.save()


def _write_expected(output_dir: Path, stem: str, page_modes: list[str]) -> Path:
    expected_path = output_dir / f"{stem}.expected.json"
    expected = {
        "sample": stem,
        "title": "2025年度全国青年科技创新奖获奖名单",
        "page_count": len(page_modes),
        "page_modes": page_modes,
        "total": len(ROWS),
        "first_no": 1,
        "last_no": len(ROWS),
        "entries": _entries(),
        "synthetic": True,
        "truncated": False,
    }
    expected_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), "utf-8")
    return expected_path


def generate(output_dir: Path = OUT) -> dict[str, Any]:
    try:
        import reportlab  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "生成 P3 样本需要 reportlab 和 Pillow：pip install -e \".[probe-pdf]\""
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path("tmp/pdfs")
    temp_root.mkdir(parents=True, exist_ok=True)
    samples = {
        "digital_roster": ["digital", "digital"],
        "scanned_roster": ["scan", "scan"],
        "mixed_roster": ["digital", "scan"],
    }
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="m5-p3-", dir=temp_root) as temp_name:
        temp_dir = Path(temp_name)
        scan_images: list[Path] = []
        for page_index in range(2):
            image_path = temp_dir / f"scan-{page_index + 1}.png"
            page_rows = ROWS[page_index * PAGE_ROWS:(page_index + 1) * PAGE_ROWS]
            _draw_scan_page(image_path, page_rows, page_index + 1, 2)
            scan_images.append(image_path)
        for stem, modes in samples.items():
            pdf_path = output_dir / f"{stem}.pdf"
            expected_path = _write_expected(output_dir, stem, modes)
            _write_pdf(pdf_path, modes, scan_images)
            records.append({
                "sample": stem,
                "pdf": pdf_path.name,
                "expected": expected_path.name,
                "page_modes": modes,
                "size_bytes": pdf_path.stat().st_size,
                "sha256": _sha256(pdf_path),
            })
    manifest = {
        "schema_version": 1,
        "synthetic": True,
        "description": "M5 P3 controlled digital/scanned/mixed two-page Chinese rosters",
        "samples": records,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 M5 P3 受控 PDF/OCR 金标准样本")
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    manifest = generate(args.output_dir)
    print(f"已生成 {len(manifest['samples'])} 份 PDF + 标注 -> {args.output_dir}")
    for sample in manifest["samples"]:
        print(f"  {sample['pdf']}: {sample['page_modes']}, {sample['size_bytes']} bytes")


if __name__ == "__main__":
    main()
