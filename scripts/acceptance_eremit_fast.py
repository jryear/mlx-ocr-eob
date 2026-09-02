#!/usr/bin/env python3
"""Acceptance suite: EREMIT60pg — Vision-only fast path + deterministic assembly.

What it does:
- Ensures the Swift CLI is built (release).
- Runs capture on eobs/tests/EREMIT60pg.pdf in Text mode (Vision-only) at configurable DPI.
- Collects per-page duration stats and document wall time.
- Assembles a canonical EOB with spatial-merge fallback (no VLM, no micro-OCR).
- Reassembles to confirm identical canonical_sha256 (deterministic output).
- Writes a JSON report with key metrics and artifact paths.

This script does not mutate existing Makefile or defaults; it is a new, opt-in suite.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics as stats
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
CLI_BIN = ROOT / "vision-ocr-cli/.build/release/vision-ocr-cli"
DOC_DEFAULT = ROOT / "eobs/tests/EREMIT60pg.pdf"


def run(cmd: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def ensure_release() -> None:
    # Build Swift CLI in release if missing
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
        try:
            out.append(json.loads(line))
        except Exception as exc:
            raise RuntimeError(f"invalid JSONL at {path}: {exc}")
    return out


def capture(doc: Path, dpi: int, lang: str, ndjson: Path, run_manifest: Path) -> Tuple[List[Dict[str, Any]], float]:
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    run_manifest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        os.fspath(CLI_BIN),
        os.fspath(doc),
        "--dpi", str(dpi),
        "--mode", "text",
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
    # Prefer the main canonical file (slug.json), not sidecars like .conservation.json
    candidates = sorted(out_dir.glob("*.json"))
    if not candidates:
        raise RuntimeError(f"no JSON files in {out_dir}")
    primaries = [p for p in candidates if "." not in p.stem]
    return primaries[0] if primaries else candidates[0]


def assemble(pages_dir: Path, glob: str, out_dir: Path) -> Path:
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
    run(cmd, check=True)
    return _choose_canonical(out_dir)


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_stats(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    durations = [int(p.get("meta", {}).get("duration_ms", 0) or 0) for p in pages]
    if not durations:
        raise RuntimeError("no durations found in page payloads")
    durations_sorted = sorted(durations)
    p95_idx = max(0, int(round(0.95 * (len(durations_sorted) - 1))))
    return {
        "pages": len(durations),
        "min_ms": min(durations),
        "mean_ms": int(round(stats.mean(durations))),
        "p95_ms": durations_sorted[p95_idx],
        "max_ms": max(durations),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Acceptance: EREMIT60pg fast path + deterministic assembly")
    ap.add_argument("--doc", type=Path, default=DOC_DEFAULT, help="Input PDF (defaults to eobs/tests/EREMIT60pg.pdf)")
    ap.add_argument("--dpi", type=int, default=150, help="Rasterization DPI (default 150 for speed)")
    ap.add_argument("--lang", default="en-US", help="Comma-separated language codes (default en-US)")
    ap.add_argument("--work", type=Path, default=ROOT / "bench/eremit_fast", help="Working dir for JSONL (default bench/eremit_fast)")
    ap.add_argument("--out", type=Path, default=ROOT / "out/eremit_fast", help="Output dir for EOB JSON (default out/eremit_fast)")
    ap.add_argument("--report", type=Path, default=ROOT / "SESSION/AuditLogs/2025-10-30-eremit-fast/report.json", help="Report path")
    args = ap.parse_args()

    ensure_release()

    args.work.mkdir(parents=True, exist_ok=True)
    ndjson_path = args.work / "eremit60_text.jsonl"
    run_manifest = ROOT / "runs/eremit60_text_run.json"

    # Clean prior outputs to keep this acceptance atomic
    for p in [ndjson_path, run_manifest]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if args.out.exists():
        # Remove only ER files to avoid disturbing other work
        for fp in args.out.glob("EREMIT60pg*.json*"):
            try:
                fp.unlink()
            except Exception:
                pass

    pages, wall = capture(args.doc, args.dpi, args.lang, ndjson_path, run_manifest)
    perf = compute_stats(pages)

    # Assemble twice to verify determinism of canonical output
    eob_path_1 = assemble(args.work, "eremit60_text.jsonl", args.out)
    sha1 = file_sha256(eob_path_1)
    # Re-assemble against the same inputs; should produce identical file content
    eob_path_2 = assemble(args.work, "eremit60_text.jsonl", args.out)
    sha2 = file_sha256(eob_path_2)

    # Read canonical digest written by the assembler (metadata.canonical_sha256)
    payload = json.loads(eob_path_2.read_text(encoding="utf-8"))
    canon_sha = payload.get("metadata", {}).get("canonical_sha256")

    report = {
        "doc": os.fspath(args.doc.relative_to(ROOT) if args.doc.is_relative_to(ROOT) else args.doc),
        "mode": "text",
        "dpi": args.dpi,
        "lang": args.lang,
        "capture": {
            "wall_time_s": round(wall, 3),
            "per_page_ms": perf,
            "route_decisions": [p.get("routing", {}).get("decision") for p in pages],
            "ndjson": os.fspath(ndjson_path.relative_to(ROOT)),
            "run_manifest": os.fspath(run_manifest.relative_to(ROOT)),
        },
        "assembly": {
            "out_json": os.fspath(eob_path_2.relative_to(ROOT)),
            "file_sha256_first": sha1,
            "file_sha256_second": sha2,
            "canonical_sha256": canon_sha,
            "deterministic_equal": bool(sha1 == sha2),
        },
        "kpi": {
            "target_sub_1s_per_page": perf["p95_ms"] < 1000,
            "target_50_pages_under_60s": wall < 60.0,
            "deterministic_eob": bool(sha1 == sha2),
        },
    }

    # Write report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True))
    # Also echo a concise summary to stdout
    print(json.dumps({
        "pages": perf["pages"],
        "p95_ms": perf["p95_ms"],
        "doc_wall_s": round(wall, 3),
        "deterministic": sha1 == sha2,
        "out": os.fspath(eob_path_2),
        "report": os.fspath(args.report)
    }))


if __name__ == "__main__":
    main()
