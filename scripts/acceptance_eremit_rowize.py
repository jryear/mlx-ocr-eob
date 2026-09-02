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


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def rows_3of4(eob: Dict[str, Any]) -> Tuple[int, int, int]:
    money = ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility")
    rows = eob.get("services") or []
    three = 0
    complete = 0
    for r in rows:
        cnt = sum(1 for k in money if isinstance(r.get(k), str) and r.get(k).strip())
        if cnt >= 3:
            three += 1
        if cnt == 4:
            complete += 1
    return three, complete, len(rows)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def assemble(out_dir: Path, *, aggressive: bool = True) -> Tuple[Path, float]:
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    cmd = [
        sys.executable, os.fspath(ROOT / "scripts/assemble_eob.py"),
        "--pages", os.fspath(ROOT / "bench/eremit_rowize"),
        "--glob", "*.jsonl",
        "--mappings", os.fspath(ROOT / "config/payers"),
        "--out", os.fspath(out_dir),
        "--rowize-tokens",
        "--rowize-strict",
        "--selective-repair-locks",
        "--bootstrap-priors",
        "--multi-pass", "3",
        "--micro-ocr",
    ]
    # Use conservative thresholds by default; omit --him-aggressive
    proc = run(cmd)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise SystemExit(proc.stderr)
    outs = sorted(out_dir.glob("*.json"))
    primaries = [p for p in outs if "." not in p.stem]
    return (primaries[0] if primaries else (outs[0] if outs else out_dir)), wall


def main() -> None:
    ap = argparse.ArgumentParser(description="EREMIT Rowize Acceptance (RCF Phase 1–3)")
    ap.add_argument("--out", type=Path, default=ROOT / "out/eremit_rowize_rcf")
    ap.add_argument("--report", type=Path, default=ROOT / "SESSION/AuditLogs/2025-10-31-eremit-rowize-rcf/report.json")
    args = ap.parse_args()

    # Assemble twice to validate determinism
    eob_path_a, wall_a = assemble(args.out)
    eob_a = load_json(eob_path_a, {})
    cons_a = load_json(eob_path_a.with_suffix(".conservation.json"), {"conservation_rate": 0.0})
    coh_a = load_json(eob_path_a.with_suffix(".coherence.v1.json"), {})
    sha_a = (args.out / (eob_path_a.name + ".sha256")).read_text(encoding="utf-8").strip() if (args.out / (eob_path_a.name + ".sha256")).exists() else None

    # Re-run into a temp dir
    out_b = args.out.parent / (args.out.name + "_rerun")
    if out_b.exists():
        for f in out_b.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
    eob_path_b, wall_b = assemble(out_b)
    eob_b = load_json(eob_path_b, {})
    sha_b = (out_b / (eob_path_b.name + ".sha256")).read_text(encoding="utf-8").strip() if (out_b / (eob_path_b.name + ".sha256")).exists() else None

    three, complete, total = rows_3of4(eob_a)

    # Ring counters (if present)
    kpi = (coh_a or {}).get("kpi", {})
    ring_counts = {k: v for k, v in kpi.items() if str(k).startswith("ring_applied_count.")}
    locked_pct = float(kpi.get("row_locked_pct")) if kpi else None

    report = {
        "suite": "EREMIT Rowize RCF",
        "doc": "eobs/tests/EREMIT60pg.pdf",
        "out": os.fspath(args.out),
        "capture": {"wall_s": None, "p95_ms": None},
        "assembly": {
            "services": total,
            "rows_3of4": three,
            "complete_rows": complete,
            "conservation_rate": cons_a.get("conservation_rate"),
            "locked_pct": locked_pct,
        },
        "ring": ring_counts,
        "deterministic": bool(sha_a and sha_b and sha_a == sha_b),
        "hash": {"run_a": sha_a, "run_b": sha_b},
        "wall": {"run_a_s": round(wall_a, 3), "run_b_s": round(wall_b, 3)},
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
