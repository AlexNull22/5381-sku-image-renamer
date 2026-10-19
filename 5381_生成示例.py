"""生成完全自制、可重现的 SKU 微图与改名清单，不调用改名引擎。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 112, 64
EXTENSIONS = (".PNG", ".png", ".JPG", ".jpg")
INPUT_FOLDER = "5381_输入图片"
MAPPING_NAME = "5381_改名清单.csv"


def _image(path, number, extension):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#F3F6F8")
    draw = ImageDraw.Draw(image)
    small = ImageFont.load_default(size=10)
    label = ImageFont.load_default(size=15)
    micro = ImageFont.load_default(size=8)
    accent = (35 + number * 37 % 150, 40 + number * 67 % 140, 55 + number * 97 % 130)
    draw.rectangle((0, 0, WIDTH, 17), fill="#173348")
    draw.text((7, 3), "SYNTHETIC DEMO", fill="white", font=small)
    draw.rectangle((8, 25, 11, 40), fill=accent)
    draw.text((17, 23), f"AA{number:04d}", fill="#173348", font=label)
    draw.text((8, 43), f"SKU TEST / {extension}", fill="#536D7D", font=micro)
    for bit in range(16):
        x = 8 + bit * 6
        shade = accent if number & (1 << bit) else "#DAE3E9"
        draw.rectangle((x, 55, x + 4, 60), fill=shade)
    if extension.lower() == ".png":
        image.save(path, format="PNG", optimize=False, compress_level=9)
    else:
        image.save(path, format="JPEG", quality=85, subsampling=0, optimize=False, progressive=False)
    image.close()


def generate_samples(output_dir, count=1500):
    """创建新目录，内含微图文件夹及UTF-8 BOM清单。拒绝覆盖已有内容。"""
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("count must be an integer from 1 to 10000")
    output = Path(output_dir).absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output directory must be new or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix="5381_生成暂存_", dir=output.parent) as stage_name:
        stage = Path(stage_name)
        images = stage / INPUT_FOLDER
        images.mkdir()
        for number in range(1, count + 1):
            extension = EXTENSIONS[(number - 1) % len(EXTENSIONS)]
            original = f"5381_AA{number:04d}{extension}"
            new_name = f"5381_AA{number:04d}_1{extension}"
            path = images / original
            _image(path, number, extension)
            records.append({"original": original, "new_name": new_name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "bytes": path.stat().st_size, "width": WIDTH, "height": HEIGHT,
                            "format": "PNG" if extension.lower() == ".png" else "JPEG"})
        with (stage / MAPPING_NAME).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["original", "new_name"])
            writer.writeheader()
            writer.writerows({key: item[key] for key in ("original", "new_name")} for item in records)
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
    return {"count": count, "input_dir": INPUT_FOLDER, "mapping_csv": MAPPING_NAME, "records": records}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成自制SKU微图和改名清单（默认1500张）")
    parser.add_argument("--output", required=True, help="新建或空的输出目录")
    parser.add_argument("--count", type=int, default=1500, help="图片数量，默认1500")
    args = parser.parse_args(argv)
    try:
        result = generate_samples(args.output, args.count)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(json.dumps({key: result[key] for key in ("count", "input_dir", "mapping_csv")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
