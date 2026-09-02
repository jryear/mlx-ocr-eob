#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STRICT_MONEY_RE = re.compile(r"^-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?$")

def to_dec(x: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(x))
    except Exception:
        return None

def norm_money(s: str) -> str:
    t = (s or "").strip()
    t = t.replace("$", "").replace(",", "").replace(" ", "")
    if t.startswith("."):
        t = "0" + t
    return t

def present_money(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip()) and bool(STRICT_MONEY_RE.match(norm_money(v)))

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Diagnose services missing a 4th money field")
    ap.add_argument("--eob", type=Path, default=Path("out/eremit_rowize/EREMIT60pg.json"))
    ap.add_argument("--coh", type=Path, default=Path("out/eremit_rowize/EREMIT60pg.coherence.v1.json"))
    return ap.parse_args()

def main() -> None:
    args = parse_args()
    eob = json.loads(args.eob.read_text())
    services: List[Dict[str, Any]] = eob.get("services", [])
    missing_rows: List[Dict[str, Any]] = []
    fields = ("charged_amount","allowed_amount","insurance_paid","patient_responsibility")

    for i, s in enumerate(services):
        have = [f for f in fields if present_money(s.get(f))]
        if len(have) == 3:
            miss = [f for f in fields if f not in have][0]
            entry = {"row": i, "missing": miss}
            cands = s.get(f"_{miss}_candidates") or {}
            # Flatten for summary
            flat: List[Tuple[str, str]] = []
            for src, vals in (cands.items() if isinstance(cands, dict) else []):
                if isinstance(vals, list):
                    for v in vals:
                        if isinstance(v, dict):
                            vstr = v.get("value") or v.get("sanitized")
                            if isinstance(vstr, str):
                                flat.append((src, vstr))
                        elif isinstance(v, str):
                            flat.append((src, v))
                elif isinstance(vals, str):
                    flat.append((src, vals))
            entry["candidates"] = {src: sum(1 for ss, _ in flat if ss == src) for src in {ss for ss, _ in flat}}
            # Quick guard reasons
            reasons: Dict[str, int] = {}
            al = to_dec(s.get("allowed_amount"))
            pa = to_dec(s.get("insurance_paid"))
            pt = to_dec(s.get("patient_responsibility"))
            ch = to_dec(s.get("charged_amount"))
            def inc(k: str):
                reasons[k] = reasons.get(k, 0) + 1
            for src, v in flat:
                nv = norm_money(v)
                if not STRICT_MONEY_RE.match(nv):
                    inc("regex_fail"); continue
                # cents hint: treat integers as no_cents
                if re.match(r"^-?\d+$", nv):
                    inc("no_cents");
                if miss == "allowed_amount" and ch is not None:
                    dv = to_dec(nv)
                    if dv is not None and dv > ch and ch > Decimal("0.00"):
                        inc("clamp");
                # improvement check
                tav = al; tpv = pa; tpt = pt
                if miss == "allowed_amount": tav = to_dec(nv)
                if miss == "insurance_paid": tpv = to_dec(nv)
                if miss == "patient_responsibility": tpt = to_dec(nv)
                if tav is not None and tpv is not None and tpt is not None:
                    delta = abs(tav - (tpv + tpt))
                    base = max(Decimal("0.01"), max(tav, (tpv + tpt)))
                    if delta <= Decimal("0.01") or (delta / base) <= Decimal("0.005"):
                        inc("coherent_candidate")
                    else:
                        inc("reduces_delta?" )
            entry["reasons"] = reasons
            missing_rows.append(entry)

    # Aggregate
    by_field: Dict[str, Dict[str, int]] = {}
    with_cand = 0
    for r in missing_rows:
        miss = r["missing"]
        byf = by_field.setdefault(miss, {"rows": 0, "with_cand": 0})
        byf["rows"] += 1
        if r.get("candidates"):
            byf["with_cand"] += 1
            with_cand += 1

    report = {
        "total_missing_rows": len(missing_rows),
        "missing_by_field": by_field,
        "rows_with_any_candidates": with_cand,
        "sample": missing_rows[:10],
    }
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()

