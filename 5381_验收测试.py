"""图片批量改名工具的业务验收。所有自制样本及输出均位于临时目录。"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = _load_module("image_renamer_5381", "5381_图片改名.py")
samples = _load_module("image_samples_5381", "5381_生成示例.py")
FIELDS = ["original", "new_name"]


class ImageRenamingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="5381_验收_")
        self.root = Path(self.temporary.name)
        self.input = self.root / "5381_输入图片"
        self.input.mkdir()
        self.mapping = self.root / "5381_改名清单.csv"
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def image(self, filename="5381_AA0001.PNG", number=1, actual_format=None, folder=None):
        path = (self.input if folder is None else Path(folder)) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (16, 12))
        image.putdata([((x * 17 + number * 31) % 256, (x * 29 + number * 13) % 256,
                        (x * 11 + number * 47) % 256) for x in range(16 * 12)])
        actual_format = actual_format or {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[path.suffix.lower()]
        image.save(path, format=actual_format)
        image.close()
        return path

    def write_mapping(self, rows, fields=None):
        with self.mapping.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS if fields is None else fields)
            writer.writeheader()
            writer.writerows(rows)

    def one_image(self):
        source = self.image()
        self.write_mapping([{"original": source.name, "new_name": "5381_AA0001_1.PNG"}])
        return source

    def snapshot_sources(self):
        return {path.name: self.digest(path) for path in self.input.iterdir() if path.is_file()}

    def assert_blocked(self, output_name="5381_拒绝输出"):
        before = self.snapshot_sources()
        with self.assertRaises(engine.WorkflowError) as preflight:
            engine.load_plan(self.input, self.mapping)
        self.assertTrue(str(preflight.exception))
        self.assertIsInstance(preflight.exception.issues, list)
        output = self.root / output_name
        with self.assertRaises(engine.WorkflowError) as execute:
            engine.execute_plan(self.input, self.mapping, output)
        self.assertTrue(str(execute.exception))
        self.assertFalse(output.exists() and any(output.iterdir()), "拒绝运行后留下了部分输出")
        self.assertEqual(before, self.snapshot_sources(), "源文件发生改变")

    def execute(self, name="5381_输出"):
        output = self.root / name
        report = engine.execute_plan(self.input, self.mapping, output)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(set(report["outputs"]), {"images", "json", "csv", "html"})
        paths = {key: output / value for key, value in report["outputs"].items()}
        for key, path in paths.items():
            self.assertEqual(path.parent, output)
            self.assertTrue(path.name.startswith("5381_"))
            self.assertTrue(path.is_dir() if key == "images" else path.is_file())
        return report, paths

    def test_preflight_is_read_only_and_reports_exact_names_sizes_and_hashes(self):
        rows = []
        for index, suffix in enumerate((".PNG", ".png", ".JPG", ".jpg"), 1):
            original = f"5381_AA{index:04d}{suffix}"
            self.image(original, number=index)
            rows.append({"original": original, "new_name": f"5381_AA{index:04d}_1{suffix}"})
        self.write_mapping(rows)
        before = {str(path.relative_to(self.root)): self.digest(path) for path in self.root.rglob("*") if path.is_file()}
        plan = engine.load_plan(self.input, self.mapping)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["counts"], {"files": 4, "bytes": sum((self.input / row["original"]).stat().st_size for row in rows), "conflicts": 0})
        by_original = {row["original"]: row for row in plan["plan"]}
        self.assertEqual(set(by_original), {row["original"] for row in rows})
        for row in rows:
            actual = by_original[row["original"]]
            self.assertEqual(actual["new_name"], row["new_name"])
            self.assertEqual(actual["sha256"], self.digest(self.input / row["original"]))
            self.assertEqual(actual["bytes"], (self.input / row["original"]).stat().st_size)
        after = {str(path.relative_to(self.root)): self.digest(path) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_execute_preserves_every_byte_and_extension_case_and_writes_audit(self):
        rows = []
        for index, suffix in enumerate((".PNG", ".png", ".JPG", ".jpg", ".JPEG", ".jpeg", ".WEBP", ".webp"), 1):
            original = f"5381_AA{index:04d}{suffix}"
            self.image(original, number=index)
            rows.append({"original": original, "new_name": f"5381_AA{index:04d}_1{suffix}"})
        self.write_mapping(rows)
        before = self.snapshot_sources()
        report, paths = self.execute()
        self.assertEqual(report["counts"], {"files": 8, "bytes": sum((self.input / row["original"]).stat().st_size for row in rows), "verified": 8, "conflicts": 0})
        self.assertEqual({path.name for path in paths["images"].iterdir()}, {row["new_name"] for row in rows})
        for row in rows:
            self.assertEqual((self.input / row["original"]).read_bytes(), (paths["images"] / row["new_name"]).read_bytes())
            self.assertEqual(Path(row["original"]).suffix, Path(row["new_name"]).suffix)
        self.assertEqual(before, self.snapshot_sources())
        saved = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
        self.assertEqual(saved["counts"], report["counts"])
        self.assertEqual(saved["plan"], report["plan"])
        self.assertNotIn(str(self.root), paths["json"].read_text(encoding="utf-8-sig"))
        with paths["csv"].open(encoding="utf-8-sig", newline="") as stream:
            audit = list(csv.DictReader(stream))
        self.assertEqual(len(audit), 8)
        self.assertEqual({row["new_name"] for row in audit}, {row["new_name"] for row in rows})
        for row in audit:
            self.assertEqual(row["verified"].lower(), "true")
            self.assertEqual(row["sha256"], before[row["original"]])
            self.assertEqual(int(row["bytes"]), (self.input / row["original"]).stat().st_size)

    def test_target_extension_must_match_source_including_case_and_alias(self):
        self.one_image()
        for index, new_name in enumerate(("5381_AA0001_1.png", "5381_AA0001_1.JPG", "5381_AA0001_1.PNG ")):
            with self.subTest(new_name=new_name):
                self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": new_name}])
                self.assert_blocked(f"5381_扩展名拒绝_{index}")

    def test_mapping_cannot_change_the_actual_source_extension_case(self):
        self.image("5381_AA0001.PNG")
        self.write_mapping([{"original": "5381_AA0001.png", "new_name": "5381_AA0001_1.png"}])
        self.assert_blocked()

    def test_jpeg_alias_cannot_be_substituted(self):
        self.image("5381_AA0001.JPG")
        self.write_mapping([{"original": "5381_AA0001.JPG", "new_name": "5381_AA0001_1.JPEG"}])
        self.assert_blocked()

    def test_windows_casefold_target_collision_is_rejected(self):
        self.image("5381_甲.PNG", 1)
        self.image("5381_乙.PNG", 2)
        self.write_mapping([{"original": "5381_甲.PNG", "new_name": "5381_Result.PNG"},
                            {"original": "5381_乙.PNG", "new_name": "5381_RESULT.PNG"}])
        self.assert_blocked()

    def test_duplicate_original_mapping_is_rejected(self):
        self.image()
        self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": "5381_结果甲.PNG"},
                            {"original": "5381_AA0001.PNG", "new_name": "5381_结果乙.PNG"}])
        self.assert_blocked()

    def test_casefold_duplicate_original_mapping_is_rejected(self):
        self.image()
        self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": "5381_结果甲.PNG"},
                            {"original": "5381_aa0001.PNG", "new_name": "5381_结果乙.PNG"}])
        self.assert_blocked()

    def test_missing_mapped_image_is_rejected(self):
        self.one_image()
        self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": "5381_AA0001_1.PNG"},
                            {"original": "5381_不存在.PNG", "new_name": "5381_不存在_1.PNG"}])
        self.assert_blocked()

    def test_unlisted_image_is_rejected(self):
        self.one_image()
        self.image("5381_未列入.PNG", 2)
        self.assert_blocked()

    def test_unsafe_source_paths_are_rejected(self):
        self.one_image()
        outside = self.image("5381_外部.PNG", 2, folder=self.root)
        nested = self.image("5381_嵌套.PNG", 3, folder=self.input / "5381_子目录")
        unsafe = ["../" + outside.name, "..\\" + outside.name, str(outside), "5381_子目录/" + nested.name]
        for index, filename in enumerate(unsafe):
            with self.subTest(original=filename):
                self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": "5381_AA0001_1.PNG"},
                                    {"original": filename, "new_name": "5381_非法来源.PNG"}])
                self.assert_blocked(f"5381_来源路径拒绝_{index}")

    def test_unsafe_targets_and_windows_reserved_names_are_rejected(self):
        self.image()
        unsafe = ["../5381_逃逸.PNG", "..\\5381_逃逸.PNG", str(self.root / "5381_绝对.PNG"),
                  "5381_子目录/5381_图片.PNG", "5381_问号?.PNG", "5381_冒号:.PNG", "CON.PNG", "aux.PNG", "LPT1.PNG"]
        for index, filename in enumerate(unsafe):
            with self.subTest(new_name=filename):
                self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": filename}])
                self.assert_blocked(f"5381_目标路径拒绝_{index}")

    def test_corrupt_image_is_rejected(self):
        self.one_image().write_bytes(b"synthetic fixture: this is not a PNG")
        self.assert_blocked()

    def test_decoded_format_must_match_extension(self):
        self.image("5381_AA0001.PNG", actual_format="JPEG")
        self.write_mapping([{"original": "5381_AA0001.PNG", "new_name": "5381_AA0001_1.PNG"}])
        self.assert_blocked()

    def test_truncated_image_is_rejected(self):
        path = self.one_image()
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])
        self.assert_blocked()

    def test_mapping_requires_exact_columns(self):
        self.image()
        cases = [(["Original", "new_name"], {"Original": "5381_AA0001.PNG", "new_name": "5381_AA0001_1.PNG"}),
                 (["original"], {"original": "5381_AA0001.PNG"}),
                 (["original", "new_name", "extra"], {"original": "5381_AA0001.PNG", "new_name": "5381_AA0001_1.PNG", "extra": "unexpected"})]
        for index, (fields, row) in enumerate(cases):
            with self.subTest(fields=fields):
                self.write_mapping([row], fields)
                self.assert_blocked(f"5381_表头拒绝_{index}")

    def test_empty_mapping_and_blank_names_are_rejected(self):
        self.image()
        cases = [[], [{"original": "", "new_name": "5381_结果.PNG"}],
                 [{"original": "5381_AA0001.PNG", "new_name": ""}]]
        for index, rows in enumerate(cases):
            with self.subTest(rows=rows):
                self.write_mapping(rows)
                self.assert_blocked(f"5381_空值拒绝_{index}")

    def test_existing_output_is_preserved_without_partial_files(self):
        self.one_image()
        before = self.snapshot_sources()
        output = self.root / "5381_已有输出"
        output.mkdir()
        sentinel = output / "5381_原有文件.txt"
        sentinel.write_text("keep unchanged", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.execute_plan(self.input, self.mapping, output)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep unchanged")
        self.assertEqual(self.snapshot_sources(), before)

    def test_unwritable_output_parent_returns_error_without_modifying_sources(self):
        self.one_image()
        before = self.snapshot_sources()
        parent = self.root / "5381_非目录.txt"
        parent.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.execute_plan(self.input, self.mapping, parent / "5381_输出")
        self.assertEqual(parent.read_text(encoding="utf-8"), "not a directory")
        self.assertEqual(self.snapshot_sources(), before)

    def test_unicode_spaces_commas_and_ampersands_roundtrip_safely(self):
        original, new_name = "5381_图 片,甲&B.PNG", "5381_图 片,甲&B_1.PNG"
        self.image(original)
        self.write_mapping([{"original": original, "new_name": new_name}])
        _, paths = self.execute()
        self.assertEqual((paths["images"] / new_name).read_bytes(), (self.input / original).read_bytes())
        with paths["csv"].open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual((row["original"], row["new_name"]), (original, new_name))
        document = paths["html"].read_text(encoding="utf-8-sig")
        self.assertIn("&amp;B", document)

    def test_generator_is_byte_reproducible_and_refuses_overwrite(self):
        left_dir, right_dir = self.root / "5381_第一批", self.root / "5381_第二批"
        left = samples.generate_samples(left_dir, count=8)
        right = samples.generate_samples(right_dir, count=8)
        self.assertEqual(left, right)
        self.assertEqual((left_dir / left["mapping_csv"]).read_bytes(), (right_dir / right["mapping_csv"]).read_bytes())
        for item in left["records"]:
            self.assertEqual((left_dir / left["input_dir"] / item["original"]).read_bytes(),
                             (right_dir / right["input_dir"] / item["original"]).read_bytes())
        with self.assertRaises(ValueError):
            samples.generate_samples(left_dir, count=8)

    def test_1500_image_batch_preserves_exact_names_counts_extensions_and_hashes(self):
        fixture = self.root / "5381_1500张样例"
        generated = samples.generate_samples(fixture, count=1500)
        input_dir, mapping = fixture / generated["input_dir"], fixture / generated["mapping_csv"]
        output = self.root / "5381_1500张输出"
        self.assertEqual(generated["count"], 1500)
        self.assertEqual(len(generated["records"]), 1500)
        self.assertEqual(len({item["sha256"] for item in generated["records"]}), 1500)
        self.assertTrue(mapping.read_bytes().startswith(b"\xef\xbb\xbf"))
        plan = engine.load_plan(input_dir, mapping)
        self.assertEqual(plan["counts"]["files"], 1500)
        self.assertEqual(plan["counts"]["conflicts"], 0)
        report = engine.execute_plan(input_dir, mapping, output)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["counts"]["files"], 1500)
        self.assertEqual(report["counts"]["verified"], 1500)
        images = output / report["outputs"]["images"]
        self.assertEqual(len(list(input_dir.iterdir())), 1500)
        self.assertEqual({path.name for path in images.iterdir()}, {item["new_name"] for item in generated["records"]})
        by_original = {row["original"]: row for row in report["plan"]}
        for item in generated["records"]:
            source, renamed = input_dir / item["original"], images / item["new_name"]
            self.assertEqual(source.suffix, renamed.suffix)
            self.assertEqual(self.digest(source), item["sha256"])
            self.assertEqual(self.digest(renamed), item["sha256"])
            self.assertEqual(by_original[item["original"]]["new_name"], item["new_name"])
            self.assertEqual(by_original[item["original"]]["sha256"], item["sha256"])
            with Image.open(renamed) as image:
                self.assertEqual(image.size, (112, 64))
                self.assertEqual(image.format, item["format"])
                image.load()
        with (output / report["outputs"]["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
