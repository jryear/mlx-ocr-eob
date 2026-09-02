#!/usr/bin/env python3
"""
Frozen selection policy per Alignment Locks.

Rules:
  - latch = 0.15 (mapper.header_match)
  - If mapper.header_match >= latch and mapper.sample_cons > 0 → mapper
  - Else if him.sample_cons >= mapper.sample_cons + 0.10 → him
  - Else → him with semantic_uncertain = True (no priors/locks)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectionInput:
    mapper_header_match: float
    mapper_sample_cons: float
    him_sample_cons: float
    latch: float = 0.15
    delta: float = 0.10


def choose_variant(si: SelectionInput) -> dict:
    m_h = float(si.mapper_header_match)
    m_c = float(si.mapper_sample_cons)
    h_c = float(si.him_sample_cons)
    latch = float(si.latch)
    delta = float(si.delta)
    if m_h >= latch and m_c > 0.0:
        return {"extraction_variant": "mapper", "semantic_uncertain": False}
    if h_c >= (m_c + delta):
        return {"extraction_variant": "him", "semantic_uncertain": False}
    return {"extraction_variant": "him", "semantic_uncertain": True}


if __name__ == "__main__":
    # tiny smoke
    ex = SelectionInput(mapper_header_match=0.2, mapper_sample_cons=0.01, him_sample_cons=0.0)
    print(choose_variant(ex))

