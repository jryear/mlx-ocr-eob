#!/usr/bin/env python3
"""Acceptance suite: EREMIT60pg — Vision Document mode (tables) + deterministic assembly.

Goals
- Keep VLM out of the loop to retain speed and determinism.
- Use Apple's Document API to get table structures directly from Vision.
- Assemble canonical EOB and confirm determinism across re-runs.
- Report KPIs: throughput, tables detected, services extracted, conservation.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as stats
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
CLI_BIN = ROOT / "vision-ocr-cli/.build/release/vision-ocr-cli"
DOC_DEFAULT = ROOT / "eobs/tests/EREMIT60pg.pdf"


def run(cmd: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def ensure_release() -> None:
    if CLI_BIN.exists():
        return
    run(["make", "ensure-release"], check=True, capture=False)
    if not CLI_BIN.exists():
        raise SystemExit(f"release binary not found at {CLI_BIN}")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def capture_document(doc: Path, dpi: int, lang: str, ndjson: Path, run_manifest: Path) -> Tuple[List[Dict[str, Any]], float]:
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    run_manifest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        os.fspath(CLI_BIN),
        os.fspath(doc),
        "--dpi", str(dpi),
        "--mode", "document",
        "--lang", lang,
        "--ndjson", os.fspath(ndjson),
        "--log-run", os.fspath(run_manifest),
    ]
    t0 = time.perf_counter()
    res = run(cmd, check=True)
    wall = time.perf_counter() - t0
    if res.returncode != 0:
        raise RuntimeError(f"CLI failed: rc={res.returncode}\n{res.stderr}")
    payloads = read_jsonl(ndjson)
    return payloads, wall


def _choose_canonical(out_dir: Path) -> Path:
    candidates = sorted(out_dir.glob("*.json"))
    if not candidates:
        raise RuntimeError(f"no JSON files in {out_dir}")
    primaries = [p for p in candidates if "." not in p.stem]
    return primaries[0] if primaries else candidates[0]


def assemble(pages_dir: Path, glob: str, out_dir: Path, *, apply_repair: bool = False, micro_ocr: bool = False, him_supplement: bool = True, semantic_map: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        os.fspath(ROOT / "scripts/assemble_eob.py"),
        "--pages", os.fspath(pages_dir),
        "--glob", glob,
        "--mappings", os.fspath(ROOT / "config/payers"),
        "--out", os.fspath(out_dir),
        "--spatial-merge",
    ]
    if apply_repair:
        cmd.append("--apply-repair")
    if micro_ocr:
        cmd.append("--micro-ocr")
    if him_supplement:
        cmd.append("--him-supplement")
    if semantic_map:
        cmd.append("--semantic-map")
    run(cmd, check=True)
    return _choose_canonical(out_dir)


def file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_perf(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    durations = [int(p.get("meta", {}).get("duration_ms", 0) or 0) for p in pages]
    if not durations:
        return {"pages": 0}
    ds = sorted(durations)
    p95_idx = max(0, int(round(0.95 * (len(ds) - 1))))
    return {
        "pages": len(ds),
        "min_ms": min(ds),
        "mean_ms": int(round(stats.mean(ds))),
        "p95_ms": ds[p95_idx],
        "max_ms": max(ds),
    }


def compute_tables_kpi(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    table_counts: List[int] = []
    cell_counts: List[int] = []
    coverages: List[float] = []
    for p in pages:
        tables = p.get("tables") or []
        table_counts.append(len(tables))
        for t in tables:
            cells = t.get("cells") or []
            rows = int(t.get("rows", 0) or 0)
            cols = int(t.get("cols", 0) or 0)
            expected = max(1, rows * cols)
            got = len(cells)
            cell_counts.append(got)
            coverages.append(min(1.0, got / expected))
    total_tables = sum(table_counts)
    return {
        "pages_with_tables": sum(1 for c in table_counts if c > 0),
        "total_tables": total_tables,
        "median_cells_per_table": (int(stats.median(cell_counts)) if cell_counts else 0),
        "median_coverage": (round(stats.median(coverages), 3) if coverages else None),
    }


def read_conservation(out_path: Path) -> Dict[str, Any]:
    cons_path = out_path.with_suffix(".conservation.json")
    if not cons_path.exists():
        return {"conservation_rate": None, "conserved_rows": [], "inconsistent_rows": [], "incomplete_rows": []}
    try:
        return json.loads(cons_path.read_text(encoding="utf-8"))
    except Exception:
        return {"conservation_rate": None}


def main() -> None:
    ap = argparse.ArgumentParser(description="Acceptance: EREMIT60pg Document mode (Vision tables)")
    ap.add_argument("--doc", type=Path, default=DOC_DEFAULT, help="Input PDF (default: eobs/tests/EREMIT60pg.pdf)")
    ap.add_argument("--dpi", type=int, default=150, help="Rasterization DPI (default 150)")
    ap.add_argument("--lang", default="en-US", help="Languages (default en-US)")
    ap.add_argument("--work", type=Path, default=ROOT / "bench/eremit_doc", help="Working dir for NDJSON")
    ap.add_argument("--out", type=Path, default=ROOT / "out/eremit_doc", help="Output dir for EOB JSON")
    ap.add_argument("--report", type=Path, default=ROOT / "SESSION/AuditLogs/2025-10-30-eremit-doc/report.json", help="Report path")
    ap.add_argument("--apply-repair", action="store_true", help="Enable lexical repair before conservation")
    ap.add_argument("--micro-ocr", action="store_true", help="Enable micro-OCR per-cell (slower)")
    args = ap.parse_args()

    ensure_release()

    args.work.mkdir(parents=True, exist_ok=True)
    ndjson_path = args.work / "eremit_doc.jsonl"
    run_manifest = ROOT / "runs/eremit_doc_run.json"
    for p in [ndjson_path, run_manifest]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    pages, wall = capture_document(args.doc, args.dpi, args.lang, ndjson_path, run_manifest)
    perf = compute_perf(pages)
    tables_kpi = compute_tables_kpi(pages)
    route_counts = Counter((p.get("routing", {}) or {}).get("decision") for p in pages)

    # Assemble twice for determinism
    out_path_1 = assemble(args.work, "eremit_doc.jsonl", args.out, apply_repair=args.apply_repair, micro_ocr=args.micro_ocr, him_supplement=True, semantic_map=True)
    sha1 = file_sha256(out_path_1)
    out_path_2 = assemble(args.work, "eremit_doc.jsonl", args.out, apply_repair=args.apply_repair, micro_ocr=args.micro_ocr, him_supplement=True, semantic_map=True)
    sha2 = file_sha256(out_path_2)
    payload = json.loads(out_path_2.read_text(encoding="utf-8"))
    canon_sha = (payload.get("metadata", {}) or {}).get("canonical_sha256")
    services = len(payload.get("services", []) or [])
    conservation = read_conservation(out_path_2)

    report = {
        "doc": os.fspath(args.doc.relative_to(ROOT) if args.doc.is_relative_to(ROOT) else args.doc),
        "mode": "document",
        "dpi": args.dpi,
        "lang": args.lang,
        "capture": {
            "wall_time_s": round(wall, 3),
            "per_page_ms": perf,
            "routing": dict(route_counts),
            "ndjson": os.fspath(ndjson_path.relative_to(ROOT)),
            "run_manifest": os.fspath(run_manifest.relative_to(ROOT)),
        },
        "vision_tables": tables_kpi,
        "assembly": {
            "out_json": os.fspath(out_path_2.relative_to(ROOT)),
            "file_sha256_first": sha1,
            "file_sha256_second": sha2,
            "canonical_sha256": canon_sha,
            "deterministic_equal": bool(sha1 == sha2),
            "services_count": services,
            "conservation": conservation,
        },
        "kpi": {
            "target_sub_1s_per_page": perf.get("p95_ms", 9999) < 1000,
            "deterministic_eob": bool(sha1 == sha2),
            "tables_detected": tables_kpi.get("total_tables", 0) > 0,
            "services_extracted": services > 0,
        },
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({
        "pages": perf.get("pages", 0),
        "p95_ms": perf.get("p95_ms"),
        "doc_wall_s": round(wall, 3),
        "tables": tables_kpi.get("total_tables", 0),
        "services": services,
        "deterministic": sha1 == sha2,
        "out": os.fspath(out_path_2),
        "report": os.fspath(args.report)
    }))


if __name__ == "__main__":
    main()
