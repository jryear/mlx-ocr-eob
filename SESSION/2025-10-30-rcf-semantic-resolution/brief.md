# RCF — Semantic Resolution with Conservation Fitness (Owner Synthesis)

Author: Core Team (Owner synthesis)
Date: 2025-10-30
Status: Adopted (flag-gated rollout)

## Core Insight

Geometry carries tokens; semantics resolves truth; conservation is the fitness function. Every ambiguous decision (which columns mean what, which candidate to accept, whether to derive) is decided by Δ‑conservation, not by heuristics. Each document becomes its own learning context: rows that satisfy identities are locked and then teach the rest via doc‑level priors.

## Key Findings and Reframing

- OCR is fast and clean (p95 ≈ 0.3–0.5s/page). Failures are role assignment + identity closure (semantics), not recognition.
- Hold‑out smoke hits ~80% rows_3of4 and ~40% conservation with budgets frozen. EREMIT‑like payers fail due to ambiguous “Amount×N” headers → right numbers, wrong roles.
- New frame: multiple semantic strategies compete (Profile/Mapper/HIM) before extraction; the winner is chosen by sample conservation. Conservation arbitrates Ring selections, derivations, and mapper vs HIM. Documents become truth‑pools: lock early; derive from locked rows; multi‑pass grows the pool.

## Implementation Overview — Token‑Efficient Phases with Checkpoints

### Phase 0 — Fitness Spine & Evidence (enable first)

What: Make conservation fitness first‑class; standardize sidecars; when `--semantic-map` is requested but NDJSON has no tables (text mode), emit deterministic WARN and switch to HIM‑only semantics.

Exit:
- Sidecars present: `sem_map.choice.v1.json`, `coherence.v1.json`, `bootstrap.v1.json`, `micro_stats.v1.json`.
- Deterministic tie‑breaks; byte‑stable reruns.
- Budget invariants logged (OCR/page ≤ 4; calls/table ≤ 3).

### Phase 1 — Semantic Configuration Resolution (soft‑accept)

What: Per table, run **Profile**, **Mapper (span‑merged headers)**, **HIM (typedness τₘ=0.15)**. Rank by 5‑row sample conservation; accept winner. If Δ < 0.10, mark `semantic_uncertain=true` and choose safer hypothesis.

Gates:
- Accept if ≥ 3/4 roles mapped OR header‑match ≥ 0.30 OR sample_conservation ≥ 0.40. Else fallback to HIM‑conservative (τₘ=0.10).

Expected: +20–40 pp rows_3of4; +15–25 pp conservation on ambiguous‑header payers.

### Phase 2 — Role‑Targeted Extraction + Ring(C) (include charged)

What: Extract by **role** (Vision→Atlas→Patch→HIM); extend Ring to the full set {charged, allowed, paid, patient}; enforce inequalities; cents‑only arithmetic.

Gates:
- Coverage: rows_3of4 ≥ 0.55 (smoke).
- Integrity: inequality violations trend ↓; clamp events recorded (allowed ≤ charged).

Expected: +10–15 pp conservation from identity closure; +10–20 pp coverage from Ring fills.

### Phase 3 — Multi‑Pass + Doc‑Level Priors (truth‑pool growth)

What: Up to 3 passes: (1) Ring repair → (2) Bootstrap from locked rows (`mode_allowed_ratio`: n ≥ 5; IQR ≤ threshold; clamp [0.20,1.00]; quantize 0.01) → (3) Derive identities (`allowed = paid + patient`) → Lock.

Gates:
- Accuracy: conservation ≥ 0.35 (basic ship); Locked pool: row_locked_pct ≥ 0.20; pass_count ≤ 3 with strictly monotone locks.

Expected: +10–20 pp conservation; +10–15 pp more rows locked.

### Phase 4 — Payer Learning & Promotion (mapper‑first by proof)

What: Persist alias banks, layout fingerprints, and winning `semantic_config`s per payer; compute payer confidence from volume and stability. Try profile first only when **confidence ≥ 0.80**, else continue soft‑accept.

Gold Gates (per payer, to flip defaults): conservation ≥ 0.90; inequality ≥ 0.98; rows_3of4 ≥ 0.95 over ≥ 10 docs / ≥ 300 lines; p95 ≤ 3s/page; byte‑stable reruns.

## Three Refinements to Lock‑In

1) **Soft‑Accept sample size** — sample_size = min(5, max(3, len(rows)//2)); if sample_size < 3, deterministic fallback: mapper if header_match ≥ 0.30; else HIM.

2) **Adaptive IQR gate** — IQR threshold for bootstrap priors: n ≥ 20 → 0.30; n ≥ 10 → 0.25; n ≥ 5 → 0.20; reject priors if any ratio < 0.15 or > 1.05.

3) **Payer confidence ramp** — confidence = min(0.95, base_confidence * conservation_stability), with base_confidence = 0.50 + min(doc_count/30, 0.45) and conservation_stability = 1 − (iqr_conservation/0.30). Prevents premature promotion on volatile payers.

## Artifacts (AP:false sidecars)

- `sem_map.v1.json` (mapper scores, col_map, accepted)
- `sem_map.choice.v1.json` (mapper vs HIM decision, Δ, evidence)
- `coherence.v1.json` (row repairs, locks, deltas)
- `bootstrap.v1.json` (doc priors: mode, sample_size, iqr, applied)
- `micro_{atlas,patch,stats}.v1.json` (OCR attempts, match rates, page OCR calls)

## Budgets & Performance (frozen)

- p95 ≤ 3s/page; OCR/page ≤ 4; ≤ 3 calls/table; deterministic_equal=true. Current capture already meets sub‑1s/page on many suites; do not regress.

## Acceptance Gates

- Coverage: rows_3of4 ≥ 0.55 (smoke), increasing with phases.
- Accuracy: conservation ≥ 0.35 to ship; stretch ≥ 0.90 on per‑payer gold sets before promotion to mapper‑first.
- Locks: row_locked_pct ≥ 0.20; monotone across passes.

## Expected Lift by Milestone (ranges by payer variance)

| Milestone                | rows_3of4 | conservation |
| ------------------------ | --------: | -----------: |
| Phase 1 (soft‑accept)    | 45–65%    | 25–45%       |
| Phase 2 (Ring+C)         | —         | 40–60%       |
| Phase 3 (priors + multi) | —         | 60–70%       |
| Phase 4 (payer gold)     | 95%+      | ≥ 90%        |

*EREMIT‑like outliers:* ~9–15% → **60–70%** after Phases 1–3; **85–90%** once payer profile is matured.

## Risks & Mitigations

- Priors bias on atypical docs → clamp, adaptive IQR, provenance; monotone rule prevents regressions.
- Header homonyms across payers → soft‑accept and per‑payer promotion gates; always fallback to HIM; expand alias banks over time.

## Owner Notes (Northstar)

Semantics resolves truth, conservation arbitrates ambiguity, and documents teach themselves. Budgets remain frozen; determinism is absolute. This is how we hit ≥ 90% conservation without adding new models or network calls.

