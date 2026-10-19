"""Validate a complete CSV rename plan, then copy images without changing bytes.

Dependency: Pillow. Original images are never renamed or modified. Output is
published only after every staged image passes SHA256 verification.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

FIELDS = ["original", "new_name"]
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
OUTPUTS = {"images": "5381_已改名图片", "json": "5381_执行报告.json",
           "csv": "5381_核验清单.csv", "html": "5381_运行报告.html"}
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10)
} | {prefix + digit for prefix in ("COM", "LPT") for digit in "¹²³"}


class WorkflowError(Exception):
    """Actionable validation failure; no formal output files were published."""
    def __init__(self, message, issues=None):
        self.issues = list(issues or [])
        super().__init__(message + ("\n" + "\n".join("- " + item for item in self.issues) if self.issues else ""))


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def name_problem(name):
    if not isinstance(name, str) or not name or name != name.strip() or name in {".", ".."}:
        return "use a non-empty basename without leading or trailing whitespace"
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")) or Path(name).name != name:
        return "paths, Windows illegal characters, and trailing dots/spaces are not allowed"
    if name.split(".", 1)[0].upper() in RESERVED:
        return "Windows reserved device names are not allowed"
    if len(name.encode("utf-16-le")) // 2 > 255:
        return "filename exceeds the Windows 255-character component limit"
    if Path(name).suffix.lower() not in FORMATS:
        return "only PNG, JPG, JPEG, and WEBP extensions are supported"
    return None


def image_inventory(input_dir):
    raw = Path(input_dir).absolute()
    if raw.is_symlink():
        raise WorkflowError("Input folder must not be a symbolic link.")
    folder = raw.resolve()
    if not folder.is_dir():
        raise WorkflowError("Input folder does not exist or is not a directory.")
    try:
        entries = list(folder.iterdir())
        links = sorted(entry.name for entry in entries if entry.is_symlink())
        if links:
            raise WorkflowError("Symbolic links are not accepted in the input folder.", links)
        files = sorted((p for p in entries if p.is_file() and p.suffix.lower() in FORMATS), key=lambda p: (p.name.casefold(), p.name))
        if not files:
            raise WorkflowError("No supported images found. Only immediate files are scanned.")
        return folder, files
    except OSError as exc:
        raise WorkflowError(f"Cannot list input folder: {exc}") from exc


def read_mapping(mapping_csv):
    mapping = Path(mapping_csv)
    if mapping.is_symlink():
        raise WorkflowError("Mapping CSV must not be a symbolic link.")
    try:
        with mapping.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != FIELDS:
                raise WorkflowError("Mapping columns must be exactly: original,new_name")
            rows = list(reader)
        if not rows:
            raise WorkflowError("Mapping CSV has no image rows.")
        return rows
    except WorkflowError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WorkflowError(f"Cannot read UTF-8 mapping CSV: {exc}") from exc


def validate_image(payload, extension):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != FORMATS[extension.lower()]:
                raise ValueError(f"actual format {image.format} does not match extension {extension}")
            image.verify()
        # Decode as well as checking container structure; reject truncated data.
        with Image.open(io.BytesIO(payload)) as image:
            image.load()


def load_plan(input_dir, mapping_csv):
    """Read-only preflight. Every immediate source image needs exactly one row."""
    folder, files = image_inventory(input_dir)
    rows = read_mapping(mapping_csv)
    issues, sources, targets, seen_input = [], set(), set(), set()
    inventory = {path.name: path for path in files}
    for path in files:
        problem = name_problem(path.name)
        if problem:
            issues.append(f"Source {path.name}: {problem}")
        if path.name.casefold() in seen_input:
            issues.append(f"Input filename collision when compared without case: {path.name}")
        seen_input.add(path.name.casefold())
    mappings = []
    for number, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            issues.append(f"CSV row {number}: expected exactly two fields")
            continue
        original, target = row["original"], row["new_name"]
        label = f"CSV row {number} ({original or 'empty original'})"
        for role, name in (("original", original), ("new_name", target)):
            problem = name_problem(name)
            if problem:
                issues.append(f"{label}: {role}: {problem}")
        if original.casefold() in sources:
            issues.append(label + ": duplicate original filename or duplicate row")
        sources.add(original.casefold())
        if target.casefold() in targets:
            issues.append(label + ": destination filename collision when compared without case")
        targets.add(target.casefold())
        if original not in inventory:
            issues.append(label + ": source is missing or filename case does not match")
        if Path(original).suffix != Path(target).suffix:
            issues.append(label + ": extension must be preserved exactly, including letter case")
        mappings.append((original, target))
    unlisted = sorted(set(inventory) - {row.get("original", "") for row in rows})
    if unlisted:
        issues.append("Source images not listed in mapping: " + ", ".join(unlisted))
    if issues:
        raise WorkflowError("Rename plan is invalid. Correct the mapping before execution.", issues)
    plan = []
    for original, target in sorted(mappings, key=lambda pair: (pair[0].casefold(), pair[0])):
        try:
            path = inventory[original]
            if path.is_symlink() or path.resolve().parent != folder:
                raise ValueError("source is a symbolic link or is outside the input folder")
            payload = path.read_bytes()
            validate_image(payload, path.suffix)
            plan.append({"original": original, "new_name": target, "sha256": digest(payload), "bytes": len(payload)})
        except Exception as exc:
            issues.append(f"{original}: cannot validate image: {exc}")
    if issues:
        raise WorkflowError("Image validation failed. No output files were created.", issues)
    return {"status": "ready", "counts": {"files": len(plan), "bytes": sum(item["bytes"] for item in plan), "conflicts": 0}, "plan": plan}


def safe_csv(value):
    text = str(value)
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) else text


def size_text(number):
    return f"{number / 1048576:.2f} MB" if number >= 1048576 else f"{number / 1024:.1f} KB"


def write_html(path, report):
    esc = lambda value: html.escape(str(value), quote=True)
    counts = report["counts"]
    cards = "".join(f'<div class="card"><span>{label}</span><strong>{value}</strong></div>' for label, value in [("FILES COPIED", f'{counts["files"]:,}'), ("SHA256 MATCHES", f'{counts["verified"]:,}'), ("NAME CONFLICTS", str(counts["conflicts"])), ("BYTES PRESERVED", size_text(counts["bytes"]))])
    rows = "".join(f'<tr><td>{index:,}</td><td><code>{esc(item["original"])}</code></td><td><code>{esc(item["new_name"])}</code></td><td>{item["bytes"]:,}</td><td><span class="ok">SHA256 matched</span><details><summary>View digest</summary><code class="hash">{item["sha256"]}</code></details></td></tr>' for index, item in enumerate(report["plan"], 1))
    document = '''<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Batch image rename audit</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f8;color:#173448;font:15px/1.5 system-ui,Segoe UI,sans-serif}main{max-width:1280px;margin:auto;padding:36px 28px}header{padding-bottom:24px;border-bottom:1px solid #d7e2ea}.eyebrow{font-size:12px;letter-spacing:.15em;color:#137f84;font-weight:750}h1{font-size:36px;line-height:1.2;margin:10px 0}.muted{color:#667d8f;font-size:13px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:25px 0}.card{background:white;border:1px solid #d8e3eb;border-radius:12px;padding:19px}.card span{font-size:11px;letter-spacing:.09em;color:#667d8f}.card strong{display:block;margin-top:5px;font-size:33px;color:#117f79;font-weight:650}.note{background:#e1f2ee;border-left:4px solid #178d7d;border-radius:5px;padding:15px 19px}.controls{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin:24px 0 12px}h2{font-size:21px;margin:0}input,button{border:1px solid #bccfdd;border-radius:7px;background:white;padding:9px 12px;color:#173448;font:inherit}input{width:280px}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}.table{background:white;border:1px solid #d8e3eb;border-radius:10px;overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}th{background:#173448;color:white;font-size:12px;padding:14px;white-space:nowrap}td{padding:13px 14px;border-bottom:1px solid #e6edf2;vertical-align:top}code{font:12px/1.5 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}.ok{color:#08765d;background:#e1f2e9;border-radius:12px;padding:3px 9px;font-size:12px;white-space:nowrap}summary{cursor:pointer;color:#667d8f;font-size:11px;margin-top:7px}.hash{display:block;max-width:210px;margin-top:6px}.pager{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-top:14px}.pager div{display:flex;gap:8px}footer{font-size:12px;color:#667d8f;margin-top:24px;overflow-wrap:anywhere}@media(max-width:700px){main{padding:22px 14px}h1{font-size:29px}.cards{grid-template-columns:repeat(2,1fr)}.card strong{font-size:28px}input{width:220px}}
</style><main><header><div class="eyebrow">LOCAL IMAGE WORKFLOW / VERIFIED OUTPUT</div><h1>Batch image renaming, with every file verified</h1><div class="muted">Mapping: MAPPING<br>Generated UTC: GENERATED</div></header><section class="cards">CARDS</section><div class="note"><b>All output bytes match their sources.</b> Each source image was validated before copying. Filenames follow the reviewed mapping; extension spelling and letter case are preserved. Originals are unchanged.</div><div class="controls"><h2>Before and after</h2><input id="search" type="search" aria-label="Search filenames" placeholder="Search original or new filename"></div><div class="table"><table><thead><tr><th>#</th><th>ORIGINAL NAME</th><th>NEW NAME</th><th>BYTES</th><th>VERIFICATION</th></tr></thead><tbody>ROWS</tbody></table></div><div class="pager"><span class="muted" id="range"></span><div><button id="previous">Previous</button><button id="next">Next</button></div></div><footer>Original files were read only. Images were copied without recompression, conversion, or metadata rewriting. Invalid plans are rejected before formal output is published. This report works offline.</footer></main><script>
const allRows=[...document.querySelectorAll('tbody tr')], pageSize=10;let page=0,filtered=allRows;function render(){allRows.forEach(row=>row.hidden=true);let start=page*pageSize;filtered.slice(start,start+pageSize).forEach(row=>row.hidden=false);document.getElementById('range').textContent=filtered.length?`${start+1}–${Math.min(start+pageSize,filtered.length)} of ${filtered.length.toLocaleString()} files`:'No matching files';document.getElementById('previous').disabled=page===0;document.getElementById('next').disabled=start+pageSize>=filtered.length}document.getElementById('search').addEventListener('input',function(){const query=this.value.toLocaleLowerCase();filtered=allRows.filter(row=>row.cells[1].textContent.toLocaleLowerCase().includes(query)||row.cells[2].textContent.toLocaleLowerCase().includes(query));page=0;render()});document.getElementById('previous').onclick=()=>{page--;render()};document.getElementById('next').onclick=()=>{page++;render()};render();
</script></html>'''
    replacements = {"MAPPING": esc(report["mapping"]), "GENERATED": esc(report["generated_at"]), "CARDS": cards, "ROWS": rows}
    document = re.sub(r"\b(MAPPING|GENERATED|CARDS|ROWS)\b", lambda match: replacements[match[0]], document)
    Path(path).write_text(document, encoding="utf-8")


def execute_plan(input_dir, mapping_csv, output_dir):
    """Copy and verify every planned image, then atomically publish one folder."""
    source = Path(input_dir).resolve()
    raw_output = Path(output_dir).absolute()
    if raw_output.is_symlink():
        raise WorkflowError("Output directory must not be a symbolic link.")
    destination = raw_output.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise WorkflowError("Input and output directories must not overlap or contain each other.")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise WorkflowError("Output directory must be new or empty. Existing files are never overwritten.")
    result = load_plan(input_dir, mapping_csv)
    stage = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="5381_改名暂存_", dir=destination.parent))
        images = stage / OUTPUTS["images"]
        images.mkdir()
        verified_plan = []
        for item in result["plan"]:
            path = source / item["original"]
            if path.is_symlink() or path.resolve().parent != source:
                raise WorkflowError("Source path changed after preflight: " + item["original"])
            payload = path.read_bytes()
            if len(payload) != item["bytes"] or digest(payload) != item["sha256"]:
                raise WorkflowError("Source changed after preflight: " + item["original"])
            target = images / item["new_name"]
            with target.open("xb") as stream:
                stream.write(payload)
            if digest(target.read_bytes()) != item["sha256"]:
                raise WorkflowError("Copied image failed SHA256 verification: " + item["new_name"])
            verified_plan.append({**item, "verified": True})
        if len(list(images.iterdir())) != len(result["plan"]):
            raise WorkflowError("Output image count verification failed.")
        _, current_files = image_inventory(input_dir)
        if {p.name for p in current_files} != {item["original"] for item in result["plan"]}:
            raise WorkflowError("Input image inventory changed after preflight; run the plan again.")
        report = {"schema_version": 1, "status": "complete", "input_folder": source.name,
                  "mapping": Path(mapping_csv).name, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "counts": {**result["counts"], "verified": len(verified_plan)}, "plan": verified_plan, "outputs": dict(OUTPUTS)}
        (stage / OUTPUTS["json"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with (stage / OUTPUTS["csv"]).open("w", encoding="utf-8-sig", newline="") as stream:
            fields = ["original", "new_name", "bytes", "sha256", "verified"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for item in verified_plan:
                writer.writerow({key: safe_csv(item[key]) if key in {"original", "new_name"} else str(item[key]).lower() if key == "verified" else item[key] for key in fields})
        write_html(stage / OUTPUTS["html"], report)
        if destination.exists():
            destination.rmdir()  # Only succeeds if still empty; never removes user files.
        os.replace(stage, destination)
        stage = None
        return report
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"Execution failed; no formal output files were published: {exc}") from exc
    finally:
        if stage is not None and stage.parent.resolve() == destination.parent and stage.name.startswith("5381_改名暂存_"):
            shutil.rmtree(stage)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate a CSV image rename plan and copy with byte-level verification.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "execute"):
        command = commands.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--mapping", required=True)
        if name == "execute":
            command.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = load_plan(args.input, args.mapping) if args.command == "plan" else execute_plan(args.input, args.mapping, args.output)
        summary = {key: result[key] for key in ("status", "counts")}
        if "outputs" in result:
            summary["outputs"] = result["outputs"]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: Unexpected failure: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
