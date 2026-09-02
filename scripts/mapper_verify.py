#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Tuple


def run_case(case_dir: Path) -> Tuple[float, int, int, bool]:
    det = json.loads((case_dir / "detected.json").read_text())
    can = json.loads((case_dir / "canonical.json").read_text())
    syn = {}
    syn_path = case_dir / "synonyms.json"
    if syn_path.exists():
        syn = json.loads(syn_path.read_text())
    # Use mapper module to compute mapping
    from header_mapper import compute_mapping, norm  # type: ignore
    mapping, unmapped = compute_mapping(det, can, {norm(k): [norm(s) for s in v] for k, v in syn.items()})
    mapped = len(mapping)
    total = len(can)
    rate = 0.0 if total == 0 else (mapped / total)
    bad = any("reject_reason" in m for m in mapping)
    return rate, mapped, total, bad


def main() -> None:
    root = Path("samples/mapper")
    cases = sorted([p for p in root.iterdir() if p.is_dir()])
    if not cases:
        print("No mapper cases found under samples/mapper", file=sys.stderr)
        sys.exit(0)
    total_mapped = 0
    total_expected = 0
    any_bad = False
    for c in cases:
        rate, mapped, total, bad = run_case(c)
        total_mapped += mapped
        total_expected += total
        any_bad = any_bad or bad
        print(f"{c.name}: mapped {mapped}/{total} (rate={rate:.2f})")
    header_match_rate = 0.0 if total_expected == 0 else total_mapped / total_expected
    print(f"header_match_rate={header_match_rate:.2f}")
    if header_match_rate < 0.80 or any_bad:
        print("FAIL: header_match_rate<0.80 or reject_reason on accepted mapping")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()

