#!/usr/bin/env python3
"""Deterministic header→schema mapper (CP6) with stable ties.

Implements the cost model and emits telemetry:
- Normalization: NFC, uppercase, collapse multi-space, strip punctuation except `/` and `-`
- Cost terms (frozen weights; calibrate later):
  cost = 0.60·lev_norm + 0.20·(1−jaccard) + 0.15·pos_pen − 0.05·syn_bonus
  where:
    lev_norm = Levenshtein(h, c) / max(|h|,|c|)
    jaccard  = Jaccard over whitespace tokens (bigrams if single-token)
    pos_pen  = |i − j| / max(n−1, 1)
    syn_bonus = 1.0 if h ∈ synonyms[c] (case-insensitive), else 0.0

Tie-break: lower cost → longer canonical length → lex canonical → lex detected_norm.
Reject matches with cost ≥ 0.50 (canonical recorded under unmapped with reject_reason).

CLI
  header_mapper.py --detected headers.json --canonical columns.json [--synonyms synonyms.json]
  Prints JSON: {mapping: [...], unmapped: [...]}.
"""
from __future__ import annotations

import argparse
import json
import unicodedata as ud
from typing import Any, Dict, List, Tuple


def nfc(s: str) -> str:
    return ud.normalize("NFC", s or "")


def norm(s: str) -> str:
    import re
    s = nfc(s).upper()
    s = re.sub(r"[^A-Z0-9/\- ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def lev(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    cur = [0] * (lb + 1)
    for i in range(1, la + 1):
        cur[0] = i
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cb = b[j - 1]
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[lb]


def tokens_or_bigrams(s: str) -> List[str]:
    parts = [p for p in norm(s).split(" ") if p]
    if len(parts) <= 1:
        w = parts[0] if parts else ""
        return [w[i : i + 2] for i in range(max(0, len(w) - 1))] or ([w] if w else [])
    return parts


def jaccard(a: str, b: str) -> float:
    A = set(tokens_or_bigrams(a))
    B = set(tokens_or_bigrams(b))
    if not A and not B:
        return 1.0
    inter = len(A & B)
    union = len(A | B)
    return inter / union if union else 0.0


def load_synonyms(path: str | None) -> Dict[str, List[str]]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    syn: Dict[str, List[str]] = {}
    for k, v in raw.items():
        syn[norm(k)] = [norm(x) for x in (v or [])]
    return syn


def compute_mapping(detected: List[str], canonical: List[str], synonyms: Dict[str, List[str]] | None = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    synonyms = synonyms or {}
    n = max(len(detected), len(canonical)) or 1
    mapping: List[Dict[str, Any]] = []
    unmapped: List[Dict[str, Any]] = []
    used: set[int] = set()

    # Greedy per-canonical with deterministic tie-breaks
    for j, can in enumerate(canonical):
        best = None
        best_cost = 1e9
        best_terms = (1.0, 0.0, 1.0, 0.0)
        nd_can = norm(can)
        for i, det in enumerate(detected):
            if i in used:
                continue
            nd_det = norm(det)
            levn = lev(nd_det, nd_can) / float(max(1, max(len(nd_det), len(nd_can))))
            jacc = jaccard(nd_det, nd_can)
            pos = abs(i - j) / float(max(1, n - 1))
            syn_hit = nd_det in (synonyms.get(nd_can) or [])
            syn_bonus = 1.0 if syn_hit else 0.0
            total = 0.60 * levn + 0.20 * (1.0 - jacc) + 0.15 * pos - 0.25 * syn_bonus
            tr = (round(levn, 6), round(jacc, 6), round(pos, 6), round(syn_bonus, 6))
            # tie-break: lower cost → longer canonical → lex canonical → lex detected
            def tb_key(cost: float, nd_can: str, nd_det: str):
                return (round(cost, 12), -len(nd_can), nd_can, nd_det)
            if best is None or tb_key(total, nd_can, nd_det) < tb_key(best_cost, norm(best[1]), norm(best[0])):
                best = (det, can, i, j, tr)
                best_cost = total
                best_terms = tr
        if best is not None:
            det, can, i, j, tr = best
            used.add(i)
            if best_cost < 0.50:
                nd_can_b = norm(can)
                nd_det_b = norm(det)
                source = "exact" if nd_det_b == nd_can_b else ("synonym" if nd_det_b in (synonyms.get(nd_can_b) or []) else "distance")
                mapping.append(
                    {
                        "detected": det,
                        "canonical": can,
                        "detected_idx": i,
                        "expected_idx": j,
                        "cost": round(best_cost, 6),
                        "terms": {"lev_norm": tr[0], "jaccard": tr[1], "pos_pen": tr[2], "syn_bonus": tr[3]},
                        "mapping_source": source,
                        "rank": 0
                    }
                )
            else:
                unmapped.append({"canonical": can, "expected_idx": j, "reject_reason": "cost>=0.50"})
        else:
            unmapped.append({"canonical": can, "expected_idx": j, "reject_reason": "no_candidate"})
    mapping.sort(key=lambda m: (m["cost"], -len(norm(m["canonical"])), norm(m["canonical"])) )
    # assign rank
    for r, m in enumerate(mapping, 1):
        m["rank"] = r
    return mapping, unmapped


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic header→schema mapper (CP6)")
    ap.add_argument("--detected", required=True, help="JSON file with array of detected header strings")
    ap.add_argument("--canonical", required=True, help="JSON file with array of canonical column names (ordered)")
    ap.add_argument("--synonyms", help="JSON file mapping canonical→[synonyms]")
    args = ap.parse_args()

    with open(args.detected, "r", encoding="utf-8") as fh:
        detected = json.load(fh)
    with open(args.canonical, "r", encoding="utf-8") as fh:
        canonical = json.load(fh)
    synonyms = load_synonyms(args.synonyms)
    mapping, unmapped = compute_mapping(detected, canonical, synonyms)
    out = {"mapping": mapping, "unmapped": unmapped}
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
