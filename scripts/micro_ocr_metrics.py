#!/usr/bin/env python3
"""Utilities for validating and summarizing micro-OCR sidecars."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import List


def _load_files(root: Path, pattern: str) -> List[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob(pattern) if path.is_file())


def _scan(root: Path) -> int:
    errors: List[str] = []
    roi_files = _load_files(root, "*.micro_roi.v1.json")
    atlas_files = _load_files(root, "*.micro_atlas.v1.json")
    stats_files = _load_files(root, "*.micro_stats.v1.json")
    if not (roi_files or atlas_files or stats_files):
        print(f"micro-ocr-scan: no micro OCR sidecars found under {root}")
        return 0

    for path in roi_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path}: json_load_failed ({exc})")
            continue
        if payload.get("schema") != "micro.roi.v1":
            errors.append(f"{path}: unexpected schema {payload.get('schema')}")
            continue
        for entry in payload.get("observations", []):
            for field in ("schema", "page_index", "table_id", "ocr_calls"):
                if field not in entry:
                    errors.append(f"{path}:observation missing {field}")
            lines = entry.get("lines", [])
            if not isinstance(lines, list):
                errors.append(f"{path}: lines not list")

    for path in atlas_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path}: json_load_failed ({exc})")
            continue
        if payload.get("schema") != "micro.atlas.v1":
            errors.append(f"{path}: unexpected schema {payload.get('schema')}")
            continue
        for atlas in payload.get("atlases", []):
            for field in ("schema", "page_index", "table_id", "atlas_sha256", "tile_pack_order"):
                if field not in atlas:
                    errors.append(f"{path}:atlas missing {field}")

    for path in stats_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path}: json_load_failed ({exc})")
            continue
        if payload.get("schema") != "micro.stats.v1":
            errors.append(f"{path}: unexpected schema {payload.get('schema')}")
            continue
        for table in payload.get("tables", []):
            for field in ("page_index", "table_id", "ocr_calls", "accepted", "pending_cells"):
                if field not in table:
                    errors.append(f"{path}:stats entry missing {field}")

    if errors:
        print("micro-ocr-scan: found issues:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(
        f"micro-ocr-scan: ✓ validated "
        f"{len(roi_files)} roi + {len(atlas_files)} atlas + {len(stats_files)} stats file(s) under {root}"
    )
    return 0


def _kpi(root: Path) -> int:
    stats_files = _load_files(root, "*.micro_stats.v1.json")
    if not stats_files:
        print(f"bench-kpi: no micro stats files under {root}")
        return 0
    total_tables = 0
    total_accepted = 0
    total_pending = 0
    ocr_calls = Counter()
    atlas_total = 0
    atlas_matched = 0
    patch_total = 0
    patch_matched = 0
    for path in stats_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"bench-kpi: warning {path} json_load_failed ({exc})")
            continue
        for table in payload.get("tables", []):
            total_tables += 1
            ocr_calls.update([table.get("ocr_calls", 0)])
            total_accepted += int(table.get("accepted_cells") or 0)
            total_pending += int(table.get("pending_cells") or 0)
            atlas_total += int(table.get("atlas_lines_total") or 0)
            atlas_matched += int(table.get("atlas_lines_matched") or 0)
            patch_total += int(table.get("patch_lines_total") or 0)
            patch_matched += int(table.get("patch_lines_matched") or 0)
    print(f"bench-kpi: source={root}")
    print(f"  tables_observed: {total_tables}")
    print(f"  accepted_cells: {total_accepted}")
    print(f"  pending_cells: {total_pending}")
    if ocr_calls:
        max_calls = max(ocr_calls)
        print(f"  ocr_calls_per_table_max: {max_calls}")
    if atlas_total:
        rate = atlas_matched / atlas_total if atlas_total else 0.0
        print(f"  atlas_match_rate: {atlas_matched}/{atlas_total} ({rate:.2%})")
    if patch_total:
        rate = patch_matched / patch_total if patch_total else 0.0
        print(f"  patch_match_rate: {patch_matched}/{patch_total} ({rate:.2%})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Micro-OCR metrics utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Validate micro-OCR atlas sidecars.")
    scan_parser.add_argument("--root", type=Path, required=True, help="Directory containing *.micro_*.v1.json files.")

    kpi_parser = subparsers.add_parser("kpi", help="Summarize atlas micro-OCR KPIs from stats files.")
    kpi_parser.add_argument("--root", type=Path, required=True, help="Directory containing *.micro_stats.v1.json files.")

    args = parser.parse_args()
    root = args.root
    if args.command == "scan":
        return _scan(root)
    if args.command == "kpi":
        return _kpi(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
