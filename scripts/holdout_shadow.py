#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "vision-ocr-cli/.build/release/vision-ocr-cli"


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def ensure_release() -> None:
    if CLI.exists():
        return
    run(["make", "ensure-release"], check=True)
    if not CLI.exists():
        raise SystemExit("vision-ocr-cli not built")


def capture_pdf(pdf: Path, ndjson: Path, run_manifest: Path, *, dpi: int = 150, mode: str = "text", lang: str = "en-US") -> float:
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    run_manifest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        os.fspath(CLI), os.fspath(pdf),
        "--dpi", str(dpi), "--mode", mode, "--lang", lang,
        "--ndjson", os.fspath(ndjson),
        "--log-run", os.fspath(run_manifest),
        "--export-images", "runs/pages",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, text=True, capture_output=True)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"capture failed rc={proc.returncode}:\n{proc.stderr}")
    return wall


def assemble_pages(pages_dir: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, os.fspath(ROOT / "scripts/assemble_eob.py"),
        "--pages", os.fspath(pages_dir),
        "--glob", "*.jsonl",
        "--mappings", os.fspath(ROOT / "config/payers"),
        "--out", os.fspath(out_dir),
        "--rowize-tokens",
        "--rowize-strict",
        "--doc-validate",
        "--doc-fallback-rowize",
        "--selective-repair-locks",
        "--bootstrap-priors",
        "--multi-pass", "3",
        "--micro-ocr",
        "--semantic-map",
    ]
    run(cmd)
    outs = sorted(out_dir.glob("*.json"))
    primaries = [p for p in outs if "." not in p.stem]
    return primaries[0] if primaries else (outs[0] if outs else out_dir)


def rows_with_3of4_money(eob: Dict[str, Any]) -> Tuple[int, int]:
    money_keys = ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility")
    rows = eob.get("services") or []
    good = 0
    for r in rows:
        cnt = sum(1 for k in money_keys if (isinstance(r.get(k), str) and r.get(k).strip()))
        if cnt >= 3:
            good += 1
    return good, len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Shadow hold-out runner (deterministic; no extra OCR)")
    ap.add_argument("--dataset", type=Path, required=True, help="datasets/<name> directory")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of PDFs (0=all)")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--mode", type=str, default="text")
    args = ap.parse_args()

    ensure_release()
    manifest = args.dataset / "manifest.jsonl"
    if not manifest.exists():
        raise SystemExit(f"manifest not found: {manifest}")
    entries: List[Dict[str, Any]] = []
    with manifest.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    if args.limit > 0:
        entries = entries[: args.limit]

    bench_dir = ROOT / "bench/holdout" / args.dataset.name
    out_dir = ROOT / "out/holdout" / args.dataset.name
    runs_dir = ROOT / "runs/holdout" / args.dataset.name
    bench_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    agg = {
        "dataset": args.dataset.name,
        "count": len(entries),
        "docs": [],
        "rows_3of4_sum": 0,
        "services_sum": 0,
        "conserved_sum": 0,
        "conservation_sum": 0.0,
        "p95_ms_pages": [],
    }

    for i, e in enumerate(entries, 1):
        pdf = Path(e.get("path"))
        sha = e.get("sha256") or ""
        ndjson = bench_dir / f"{sha}.jsonl"
        run_manifest = runs_dir / f"{sha}.run.json"
        # If NDJSON already exists, reuse it; otherwise attempt capture
        if not ndjson.exists():
            try:
                _ = capture_pdf(pdf, ndjson, run_manifest, dpi=args.dpi, mode=args.mode)
            except Exception as exc:
                agg["docs"].append({"pdf": os.fspath(pdf), "sha256": sha, "status": "capture_failed", "error": str(exc)})
                continue
        # Assemble
        assemble_pages(bench_dir, out_dir)
        slug = re_slug(pdf.stem)
        eob_path = out_dir / f"{slug}.json"
        if not eob_path.exists():
            # try any json
            outs = sorted(out_dir.glob("*.json"))
            eob_path = outs[0] if outs else eob_path
        try:
            eob = json.loads(eob_path.read_text())
        except Exception as exc:
            agg["docs"].append({"pdf": os.fspath(pdf), "sha256": sha, "status": "assemble_read_failed", "error": str(exc)})
            continue
        three, total = rows_with_3of4_money(eob)
        cons_path = eob_path.with_suffix(".conservation.json")
        cons = json.loads(cons_path.read_text()) if cons_path.exists() else {"conservation_rate": 0.0}
        agg["rows_3of4_sum"] += three
        agg["services_sum"] += total
        agg["conservation_sum"] += float(cons.get("conservation_rate") or 0.0)
        agg["docs"].append({
            "pdf": os.fspath(pdf),
            "sha256": sha,
            "rows_3of4": [three, total],
            "conservation_rate": cons.get("conservation_rate"),
            "eob": os.fspath(eob_path),
        })

    # Summary
    out_report = ROOT / "SESSION/AuditLogs/holdout" / args.dataset.name / "report.json"
    out_report.parent.mkdir(parents=True, exist_ok=True)
    agg["rows_3of4_rate"] = (agg["rows_3of4_sum"] / agg["services_sum"]) if agg["services_sum"] else 0.0
    agg["conservation_mean"] = (agg["conservation_sum"] / max(1, len(agg["docs"])) )
    out_report.write_text(json.dumps(agg, indent=2))
    print(json.dumps({
        "dataset": args.dataset.name,
        "docs": len(agg["docs"]),
        "rows_3of4_rate": agg["rows_3of4_rate"],
        "conservation_mean": agg["conservation_mean"],
        "report": os.fspath(out_report)
    }, indent=2))


def re_slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "document"


if __name__ == "__main__":
    main()
