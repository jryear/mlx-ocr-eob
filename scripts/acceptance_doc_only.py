#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def assemble_doc_only(pages_path: Path, out_dir: Path, latch: float) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, os.fspath(ROOT / "scripts/assemble_eob.py"),
        "--pages", os.fspath(pages_path),
        "--glob", "*.jsonl",
        "--mappings", os.fspath(ROOT / "config/payers"),
        "--out", os.fspath(out_dir),
        "--doc-validate",
        "--semantic-map",
        "--selective-repair-locks",
        "--bootstrap-priors",
        "--multi-pass", "3",
        "--mapper-latch", str(latch),
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(proc.returncode)
    outs = sorted(out_dir.glob("*.json"))
    primaries = [p for p in outs if "." not in p.stem]
    return primaries[0] if primaries else (outs[0] if outs else out_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="Doc-only acceptance (Vision tables gated; no rowize fallback)")
    ap.add_argument("--pages", type=Path, default=ROOT / "bench/eremit_doc", help="Directory with doc-mode NDJSON")
    ap.add_argument("--out", type=Path, default=ROOT / "out/eremit_doc_only_accept")
    ap.add_argument("--latch", type=float, default=0.15, help="Mapper latch threshold (default 0.15)")
    ap.add_argument("--report", type=Path, default=ROOT / "SESSION/AuditLogs/eremit-doc-only-accept/report.json")
    args = ap.parse_args()

    eob_path = assemble_doc_only(args.pages, args.out, args.latch)
    eob = load_json(eob_path, {})
    cons = load_json(eob_path.with_suffix(".conservation.json"), {"conservation_rate": 0.0})
    docval = load_json(eob_path.with_suffix(".doc_tables.validate.v1.json"), {"summary": {}})
    semchoice = load_json(eob_path.with_suffix(".sem_map.choice.v1.json"), {"choices": []})

    rows = eob.get("services") or []
    money = ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility")
    three = sum(1 for r in rows if sum(1 for k in money if isinstance(r.get(k), str) and r.get(k).strip()) >= 3)
    complete = sum(1 for r in rows if all(isinstance(r.get(k), str) and r.get(k).strip() for k in money))
    mapper_wins = sum(1 for c in (semchoice.get("choices") or []) if c.get("chosen") == "mapper")
    him_wins = sum(1 for c in (semchoice.get("choices") or []) if c.get("chosen") == "him")

    report = {
        "suite": "Doc-only Acceptance",
        "pages": os.fspath(args.pages),
        "out": os.fspath(args.out),
        "doc_tables": docval.get("summary", {}),
        "semantics": {"mapper": mapper_wins, "him": him_wins},
        "assembly": {
            "services": len(rows),
            "rows_3of4": three,
            "complete_rows": complete,
            "conservation_rate": cons.get("conservation_rate"),
        }
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

