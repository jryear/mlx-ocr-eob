#!/usr/bin/env python3
"""
Deterministic row law solver stubs with integer-cents arithmetic.

Exposes:
  - in_network(row_values)
  - noncovered(row_values)
  - cob_secondary(row_values)
  - min_energy_row(row_values)

Row values are strings or numbers representing money values. This module converts
to integer cents and computes violation energies and derived terms. It does not
perform any I/O.

This is a scaffold: small, pure, and easy to unit test. It is safe to import
into scripts/assemble_eob.py to attach row/table certificates and locks.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional, Tuple


@dataclass(frozen=True)
class SolveResult:
    law_id: str
    certified: bool
    violation_energy: int
    rounding_locus: Optional[str]
    derived_terms: Dict[str, int]


def _to_cents(val: Optional[Any]) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int,)):
        return int(val)
    try:
        # Normalize to Decimal in dollars, then to cents (integer)
        d = Decimal(str(val).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return int(round(d * 100))


def _abs(x: int) -> int:
    return -x if x < 0 else x


def in_network(row: Dict[str, Any]) -> SolveResult:
    """In-network law: charged ≥ allowed; allowed = paid + patient + plan_adjust; contractual_writeoff = charged − allowed ≥ 0.

    Rounds only at plan_adjust (single locus). All values are integer cents.
    """
    charged = _to_cents(row.get("charged"))
    allowed = _to_cents(row.get("allowed"))
    paid = _to_cents(row.get("paid"))
    patient = _to_cents(row.get("patient"))
    if charged is None or paid is None or patient is None:
        return SolveResult("in_network", False, 10_000_000, None, {})
    derived: Dict[str, int] = {}
    rounding_locus: Optional[str] = None
    violations = 0

    # Inequalities
    if allowed is not None and allowed > charged:
        violations += (allowed - charged)

    if allowed is None and charged is not None:
        # Derive allowed as paid + patient + plan_adjust; prefer minimal non-negative plan_adjust
        plan_adjust = max(0, charged - (paid + patient))
        allowed = paid + patient + plan_adjust
        derived["plan_adjust"] = plan_adjust
        rounding_locus = rounding_locus or "plan_adjust"
    else:
        # With allowed provided, check closure and derive plan_adjust if needed
        plan_adjust = max(0, allowed - (paid + patient)) if allowed is not None else 0
        if plan_adjust < 0:
            violations += _abs(plan_adjust)
            plan_adjust = 0
        derived["plan_adjust"] = plan_adjust

    # contractual_writeoff = charged − allowed ≥ 0
    if charged is not None and allowed is not None:
        cwo = charged - allowed
        if cwo < 0:
            violations += _abs(cwo)
        derived["contractual_writeoff"] = max(0, cwo)

    # Closure residual for allowed = paid + patient + plan_adjust
    residual = 0
    if allowed is not None:
        residual = allowed - (paid + patient + derived.get("plan_adjust", 0))

    energy = _abs(residual) + violations
    certified = energy == 0
    return SolveResult("in_network", certified, energy, rounding_locus, derived)


def noncovered(row: Dict[str, Any]) -> SolveResult:
    """Non-covered/Out-of-network: allowed may be absent; enforce paid ≤ charged; patient ≥ 0.
    Track noncovered = charged − (paid + patient + contractual_writeoff) with non-negativity.
    """
    charged = _to_cents(row.get("charged"))
    paid = _to_cents(row.get("paid"))
    patient = _to_cents(row.get("patient"))
    if charged is None or paid is None or patient is None:
        return SolveResult("noncovered", False, 10_000_000, None, {})
    violations = 0
    derived: Dict[str, int] = {}

    if paid > charged:
        violations += (paid - charged)
    if patient < 0:
        violations += _abs(patient)

    # Allow contractual_writeoff ≥ 0; prefer zero if unspecified
    cwo = max(0, charged - paid - patient)
    derived["noncovered"] = cwo
    residual = charged - (paid + patient + derived["noncovered"])  # should be 0

    energy = _abs(residual) + violations
    certified = energy == 0
    return SolveResult("noncovered", certified, energy, None, derived)


def cob_secondary(row: Dict[str, Any]) -> SolveResult:
    """Coordination of benefits: paid_total = paid_primary + paid_secondary; patient non-negative.
    If paid_total present, validate; else derive from parts. No negative terms.
    """
    paid_primary = _to_cents(row.get("paid_primary"))
    paid_secondary = _to_cents(row.get("paid_secondary"))
    paid_total = _to_cents(row.get("paid"))  # accept 'paid' as total
    patient = _to_cents(row.get("patient"))
    violations = 0
    derived: Dict[str, int] = {}
    if paid_primary is None or paid_secondary is None:
        return SolveResult("cob_secondary", False, 10_000_000, None, {})

    if paid_total is None:
        paid_total = paid_primary + paid_secondary
        derived["paid"] = paid_total
    residual = paid_total - (paid_primary + paid_secondary)
    if patient is not None and patient < 0:
        violations += _abs(patient)

    energy = _abs(residual) + violations
    certified = energy == 0
    return SolveResult("cob_secondary", certified, energy, None, derived)


def min_energy_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate all admissible laws on a row and pick the minimal energy.

    Returns a JSON-serializable dict shaped for row.ccert.v1.json rows[] entries.
    """
    candidates = [in_network(row), noncovered(row), cob_secondary(row)]
    best = min(candidates, key=lambda r: (r.violation_energy, 0 if r.law_id != "in_network" else 1))
    return {
        "law": best.law_id,
        "certified": bool(best.certified),
        "violation_energy": int(best.violation_energy),
        "rounding_locus": best.rounding_locus,
        "derived_terms": best.derived_terms,
    }


if __name__ == "__main__":  # simple smoke
    sample = {"charged": "120.00", "allowed": "100.00", "paid": "80.00", "patient": "20.00"}
    print(min_energy_row(sample))

