#!/usr/bin/env python3
"""
Automated checkpoint verifier for Northstar solver sidecars.

Usage examples:
  # Baseline (no behavior change expected)
  python3 scripts/verify_checkpoint.py --baseline out/northstar_baseline

  # Strict locks (certified-only locking expected)
  python3 scripts/verify_checkpoint.py --strict out/northstar_strict

Exit code is non-zero if any requested verification fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to read JSON {path}: {exc}")


def _find_eob_files(outdir: Path) -> List[Path]:
    files = []
    for p in outdir.glob("*.json"):
        # Exclude known sidecars (they append extra suffixes)
        if any(
            p.name.endswith(suf)
            for suf in (
                ".conservation.json",
                ".sem_map.v1.json",
                ".sem_map.choice.v1.json",
                ".coherence.v1.json",
                ".bootstrap.v1.json",
                ".rowize.json",
                ".doc_tables.validate.v1.json",
                ".repair_summary.json",
                ".him.json",
                ".vlm_tables.json",
                ".vlm_tables_rejected.json",
                ".spatial_merge.json",
                ".micro_roi.v1.json",
                ".micro_atlas.v1.json",
                ".micro_patch.v1.json",
                ".micro_stats.v1.json",
                ".row.ccert.v1.json",
                ".table.ccert.v1.json",
                ".summary_box.v1.json",
                ".assign.proof.v1.json",
                ".evidence.v1.json",
                ".extraction.map_metrics.v1.json",
                ".selection.evidence.v1.json",
            )
        ):
            continue
        # Must look like an EOB (contain services array)
        try:
            data = _load_json(p)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("services"), list):
            files.append(p)
    return sorted(files)


def _derive_sidecars(eob_path: Path) -> Tuple[Path, Path]:
    return (
        eob_path.with_suffix(".row.ccert.v1.json"),
        eob_path.with_suffix(".table.ccert.v1.json"),
    )


def _validate_assign(path: Path, require: bool) -> Tuple[bool, List[str]]:
    notes: List[str] = []
    if not path.exists():
        return (not require), ([] if not require else [f"missing {path.name}"])
    try:
        js = _load_json(path)
    except Exception as exc:
        return False, [f"{path.name}: unreadable ({exc})"]
    ok = True
    if js.get("schema") not in ("assign.proof.v1", None):
        notes.append(f"{path.name}: unexpected/missing schema")
    tables = js.get("tables")
    if not isinstance(tables, list) or not tables:
        notes.append(f"{path.name}: tables[] missing/empty")
        ok = False
    else:
        t0 = tables[0]
        for key in ("table_id", "page_index", "extraction_variant", "column_map", "alternatives", "costs"):
            if key not in t0:
                notes.append(f"{path.name}: missing {key}")
                ok = False
        # Certified rows sample should be present in costs for the chosen mapping
        if isinstance(t0.get("costs"), dict) and "certified_rows_sample" not in t0.get("costs"):
            notes.append(f"{path.name}: costs.certified_rows_sample missing")
            ok = False
        # If ILP is present, ensure structure includes best/runner_up or alternatives include ilp_runner_up
        ilp = t0.get("ilp")
        alts = t0.get("alternatives") or []
        if ilp:
            for k in ("best", "runner_up"):
                if k in ilp and ilp[k] is not None:
                    d = ilp[k]
                    if "column_map" not in d or "certified_rows_sample" not in d:
                        notes.append(f"{path.name}: ilp.{k} missing required fields")
                        ok = False
        else:
            for a in alts:
                if a.get("variant") == "ilp_runner_up" and "certified_rows_sample" not in a:
                    notes.append(f"{path.name}: ilp_runner_up missing certified_rows_sample")
                    ok = False
    return ok, notes


def _validate_evidence(path: Path, require: bool) -> Tuple[bool, List[str]]:
    notes: List[str] = []
    if not path.exists():
        return (not require), ([] if not require else [f"missing {path.name}"])
    try:
        js = _load_json(path)
    except Exception as exc:
        return False, [f"{path.name}: unreadable ({exc})"]
    ok = True
    if js.get("schema") not in ("evidence.v1", None):
        notes.append(f"{path.name}: unexpected/missing schema")
    tables = js.get("tables")
    if not isinstance(tables, list) or not tables:
        notes.append(f"{path.name}: tables[] missing/empty")
        ok = False
    else:
        t0 = tables[0]
        for key in ("table_id", "page_index", "rows", "cols", "typedness", "quantization"):
            if key not in t0:
                notes.append(f"{path.name}: missing {key}")
                ok = False
        q = t0.get("quantization", {}) or {}
        h = q.get("hash", "")
        if not (isinstance(h, str) and len(h) == 64):
            notes.append(f"{path.name}: quantization.hash malformed")
            ok = False
        # If there appear to be >1 money columns, expect swap_energy to be present (if provided)
        typed = t0.get("typedness", {}) or {}
        money = typed.get("money") or []
        if isinstance(money, list) and sum(1 for v in money if (isinstance(v, (int,float)) and v > 0.05)) >= 2:
            se = t0.get("swap_energy")
            if se is None:
                notes.append(f"{path.name}: expected swap_energy when multiple money columns detected")
                ok = False
    return ok, notes


def _validate_map_metrics(path: Path, require: bool, enforce_completeness: bool = False) -> Tuple[bool, List[str]]:
    """Validate extraction.map_metrics.v1.json sidecar."""
    notes: List[str] = []
    if not path.exists():
        return (not require), ([] if not require else [f"missing {path.name}"])
    try:
        js = _load_json(path)
    except Exception as exc:
        return False, [f"{path.name}: unreadable ({exc})"]
    ok = True
    if js.get("schema") not in ("extraction.map_metrics.v1", None):
        notes.append(f"{path.name}: unexpected/missing schema")
    tables = js.get("tables")
    if not isinstance(tables, list) or not tables:
        notes.append(f"{path.name}: tables[] missing/empty")
        ok = False
    else:
        for tbl in tables:
            tid = tbl.get("table_id", "unknown")
            pidx = tbl.get("page_index", "unknown")
            for key in ("table_id", "page_index", "map_completeness", "anchors_per_row", "role_coverage"):
                if key not in tbl:
                    notes.append(f"{path.name}: table {tid} p{pidx} missing {key}")
                    ok = False
            # Check map_completeness ≥ 3 when ILP was applied
            applied = tbl.get("applied_for_extraction", False)
            mc = tbl.get("map_completeness")
            if enforce_completeness and applied and isinstance(mc, (int, float)) and mc < 3:
                notes.append(f"MAP_INCOMPLETE[post-ilp]: {path.name} table {tid} p{pidx} completeness={mc} < 3")
                ok = False
    return ok, notes


def verify_baseline(outdir: Path, require_assign: bool = False, require_evidence: bool = False, require_map_metrics: bool = False, enforce_completeness: bool = False) -> Tuple[bool, List[str]]:
    ok = True
    notes: List[str] = []
    eobs = _find_eob_files(outdir)
    if not eobs:
        return False, [f"no EOB JSON files found in {outdir}"]
    for eob in eobs:
        row_ccert, tbl_ccert = _derive_sidecars(eob)
        if not row_ccert.exists():
            ok = False
            notes.append(f"missing {row_ccert.name}")
        if not tbl_ccert.exists():
            ok = False
            notes.append(f"missing {tbl_ccert.name}")
        # Minimal schema checks (keys present)
        if row_ccert.exists():
            rc = _load_json(row_ccert)
            if "rows" not in rc or not isinstance(rc["rows"], list):
                ok = False
                notes.append(f"{row_ccert.name}: rows[] missing or wrong type")
        if tbl_ccert.exists():
            tc = _load_json(tbl_ccert)
            for k in ("certified_rows", "conservation"):
                if k not in tc:
                    ok = False
                    notes.append(f"{tbl_ccert.name}: missing key {k}")
        # Optional: assign/evidence/map_metrics sidecars
        ap = eob.with_suffix(".assign.proof.v1.json")
        ev = eob.with_suffix(".evidence.v1.json")
        mm = eob.with_suffix(".extraction.map_metrics.v1.json")
        aok, an = _validate_assign(ap, require_assign)
        eok, en = _validate_evidence(ev, require_evidence)
        mok, mn = _validate_map_metrics(mm, require_map_metrics, enforce_completeness)
        ok = ok and aok and eok and mok
        notes.extend(an)
        notes.extend(en)
        notes.extend(mn)
    return ok, notes


def verify_strict(outdir: Path, require_assign: bool = False, require_evidence: bool = False, require_map_metrics: bool = False, enforce_completeness: bool = False) -> Tuple[bool, List[str]]:
    ok = True
    notes: List[str] = []
    eobs = _find_eob_files(outdir)
    if not eobs:
        return False, [f"no EOB JSON files found in {outdir}"]
    for eob in eobs:
        data = _load_json(eob)
        row_ccert, tbl_ccert = _derive_sidecars(eob)
        if not row_ccert.exists() or not tbl_ccert.exists():
            ok = False
            notes.append(f"{eob.name}: missing required sidecars for strict check")
            continue
        tc = _load_json(tbl_ccert)
        certified_rows = int(tc.get("certified_rows") or 0)
        locked = sum(1 for s in (data.get("services") or []) if s.get("_row_locked"))
        if locked != certified_rows:
            ok = False
            notes.append(f"{eob.name}: locked({locked}) != certified_rows({certified_rows})")
        # No uncertain locked
        bad = [1 for s in (data.get("services") or []) if s.get("_semantic_uncertain") and s.get("_row_locked")]
        if bad:
            ok = False
            notes.append(f"{eob.name}: found locked rows marked semantic_uncertain")
        # Assign/evidence/map_metrics sidecars (if present or required)
        ap = eob.with_suffix(".assign.proof.v1.json")
        ev = eob.with_suffix(".evidence.v1.json")
        mm = eob.with_suffix(".extraction.map_metrics.v1.json")
        aok, an = _validate_assign(ap, require_assign)
        eok, en = _validate_evidence(ev, require_evidence)
        mok, mn = _validate_map_metrics(mm, require_map_metrics, enforce_completeness)
        ok = ok and aok and eok and mok
        notes.extend(an)
        notes.extend(en)
        notes.extend(mn)
        # Budget sidecar checks (if present)
        budget = eob.with_suffix(".budget.v1.json")
        if budget.exists():
            b = _load_json(budget)
            p95 = float(b.get("p95_s_per_page") or 0.0)
            if p95 > 3.0 + 1e-6:
                ok = False
                notes.append(f"{eob.name}: p95_s_per_page={p95} exceeds 3s cap")
        # Stability sidecar shape (if present)
        stab = eob.with_suffix(".stability.v1.json")
        if stab.exists():
            s = _load_json(stab)
            if "rerun_crc_equal" not in s:
                ok = False
                notes.append(f"{eob.name}: stability sidecar missing rerun_crc_equal")
    return ok, notes


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify Northstar solver checkpoints")
    ap.add_argument("--baseline", help="Output directory for baseline verification (sidecars exist)")
    ap.add_argument("--strict", help="Output directory for strict verification (locks == certified and no uncertain locked)")
    ap.add_argument("--require-assign", action="store_true", help="Fail if assign.proof.v1.json missing")
    ap.add_argument("--require-evidence", action="store_true", help="Fail if evidence.v1.json missing")
    ap.add_argument("--require-map-metrics", action="store_true", help="Fail if extraction.map_metrics.v1.json missing")
    ap.add_argument("--enforce-map-completeness", action="store_true", help="Fail if map_completeness < 3 when ILP applied (exit 4 = MAP_INCOMPLETE)")
    args = ap.parse_args()

    any_fail = False
    map_incomplete = False
    if args.baseline:
        ok, notes = verify_baseline(
            Path(args.baseline),
            require_assign=args.require_assign,
            require_evidence=args.require_evidence,
            require_map_metrics=args.require_map_metrics,
            enforce_completeness=args.enforce_map_completeness,
        )
        print(f"BASELINE: {'PASS' if ok else 'FAIL'}")
        for n in notes:
            print(f" - {n}")
            if "MAP_INCOMPLETE[post-ilp]" in n:
                map_incomplete = True
        any_fail = any_fail or (not ok)
    if args.strict:
        ok, notes = verify_strict(
            Path(args.strict),
            require_assign=args.require_assign,
            require_evidence=args.require_evidence,
            require_map_metrics=args.require_map_metrics,
            enforce_completeness=args.enforce_map_completeness,
        )
        print(f"STRICT: {'PASS' if ok else 'FAIL'}")
        for n in notes:
            print(f" - {n}")
            if "MAP_INCOMPLETE[post-ilp]" in n:
                map_incomplete = True
        any_fail = any_fail or (not ok)
    if not args.baseline and not args.strict:
        print("Nothing to verify: pass --baseline and/or --strict paths", file=sys.stderr)
        sys.exit(2)
    # Exit code 4 for MAP_INCOMPLETE enforcement failure
    if map_incomplete:
        sys.exit(4)
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
