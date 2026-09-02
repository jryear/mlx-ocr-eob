#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import List, Dict, Any

ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description="Create hold-out dataset manifest (deterministic)")
    ap.add_argument("--src", type=Path, required=True, help="Path to PDF directory (e.g., /Users/.../claimsTest)")
    ap.add_argument("--name", type=str, required=True, help="Dataset name (e.g., EOB_ZIP_2025Q3)")
    ap.add_argument("--out", type=Path, default=ROOT / "datasets", help="Datasets root directory")
    args = ap.parse_args()

    src = args.src
    if not src.exists():
        raise SystemExit(f"Source path does not exist: {src}")
    pdfs: List[Path] = []
    for root, _, files in os.walk(src):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdfs.append(Path(root) / f)
    if not pdfs:
        raise SystemExit(f"No PDFs found under {src}")

    pdfs.sort(key=lambda p: str(p).lower())
    entries: List[Dict[str, Any]] = []
    sha_list: List[str] = []
    for p in pdfs:
        sha = file_sha256(p)
        entries.append({"path": os.fspath(p), "sha256": sha})
        sha_list.append(sha)

    dataset_id = hashlib.sha256("".join(sorted(sha_list)).encode("utf-8")).hexdigest()
    out_dir = args.out / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "DATASET_ID").write_text(dataset_id + "\n", encoding="utf-8")
    with (out_dir / "manifest.jsonl").open("w", encoding="utf-8") as mf:
        for e in entries:
            mf.write(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps({
        "dataset": args.name,
        "dataset_dir": os.fspath(out_dir),
        "dataset_id": dataset_id,
        "count": len(entries)
    }, indent=2))


if __name__ == "__main__":
    main()

