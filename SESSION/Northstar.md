# Northstar — Geometry → Roles → Proofs (Deterministic, Payer‑Agnostic)

Author: Core Team
Date: 2025-11-06
Status: Authoritative northstar and chat/build log template

## Mission
- Hit ≥90% conservation on gold payers with a single payer‑agnostic path.
- p95 ≤ 3 s/page; OCR/page ≤ 4; byte‑stable outputs; PHI‑safe (no network/VLM by default).
- Cents‑only math; inequalities enforced; proofs for every acceptance.

## Alignment Locks (Authoritative)
```
SELECTION_POLICY:
  latch = 0.15
  if mapper.header_match >= 0.15 and mapper.sample_cons > 0:
    extraction_variant = "mapper"
  elif him.sample_cons >= mapper.sample_cons + 0.10:
    extraction_variant = "him"
  else:
    extraction_variant = "him"
    semantic_uncertain = true  # no priors/locks

DOC_MODE_SERVICE_GATING (span-aware):
  min_rows >= 6
  money_rows_ratio >= 0.50
  code_or_date_rows_ratio >= 0.10
  rows_fit_rate >= 0.00
  header_spans ∋ {Code|ADA|CPT|DOS}
  # relaxation:
  if money_rows_ratio >= 0.75: code_or_date_rows_ratio >= 0.05

PRIORS:
  require_locked_rows >= 5
  IQR(allowed/charged) <= 0.25
  mode_allowed_ratio clamped [0.20,1.00], quantized 0.01
  exclude semantic_uncertain rows from priors/locks

STANDING INVARIANTS:
  OCR/page <= 4; p95 <= 3s/page; cents-only math; inequalities enforced
  Ring repairs: charged, allowed, paid, patient
  Multi-pass <= 3; locks monotone; byte-stable outputs
```

## First‑Principles Architecture

**Evidence Plane (E)**
- Build a token lattice from OCR NDJSON without trusting widget tables.
- Rows: isotonic y‑baseline grid; tie‑break by line density and token‑center votes.
- Columns: x‑partitioning at 0.5% page‑width steps minimizing within‑partition x‑angle variance; include a column independence test (swap adjacent partitions should increase violation energy when roles are correct).
- Typedness witnesses (auditable):
  - Money: regex id, thousands/decimal signature, left/right alignment std, currency symbol proximity, neighborhood digit entropy.
  - Date: locale patterns, month lexeme proximity, service→adjudication monotonicity cues.
  - CDT/ADA: `^D\d{4}$` with year validity; tooth/surface shapes tracked separately.
  - IDs: length/checksum shapes when present.
- Output: `evidence.v1.json` (rows, cols, per‑cell tokens, typedness per cell/col, quantization hashes and reasons).

**Role + Law Solver (S)**
- Column‑role assignment via Hungarian/ILP‑lite over {charged, allowed, paid, patient}.
- Energy functional (deterministic):
  - Column map: `E_cols = wH·header_cost + wT·typedness_gap + wS·shape_mismatch + wC·(1−conservation_gain) + wI·ineq_violations`.
  - Row closure (given a map): `E_row = wM·missing_observed + wR·round_ops + wF·fills + wV·violation_penalty(law)`.
- Integer‑cents arithmetic only; prefer observed tokens, penalize fills/derivations; exactly one allowed rounding locus per law.
- Row Law Library (family, not single identity): choose the law that minimizes violations without inventing negatives.
  - In‑network: charged ≥ allowed; allowed = paid + patient + plan_adjust (plan_adjust ≥ 0); contractual_writeoff = charged − allowed ≥ 0.
  - Out‑of‑network / non‑covered: paid ≤ charged; patient ≥ 0; track `noncovered = charged − (paid + patient + contractual_writeoff)`.
  - COB/secondary: paid_total = Σ paid_k; patient typically after primaries; guard against negatives.
- Output: `assign.proof.v1.json` (selected column‑role map, alternatives within δ, costs), `row.ccert.v1.json` (per‑row law chosen, fills/derivations, rounding loci, predecessor token hashes, inequality checks).

**Dual Candidates + Frozen Policy (H)**
- Candidate A (header‑nudged): headers only shape cost; never hard‑bind roles.
- Candidate B (headerless): typedness + closure only.
- Compute 5‑row sample conservation and apply Alignment Locks SELECTION_POLICY. Also log certified_rows and energies.
- Output: `sem_map.choice.v1.json` (decision, evidence, deltas, certified_rows counts).

**Reconciliation (R)**
- Summary boxes: parse claim/page totals and add a Lagrange penalty aligning row sums with summaries (in cents, with rounding locus).
- Counterfactual swap test: swap any two money roles on a 5–10 row probe; if energy drops by >τ, flip mapping deterministically.

**Locks + Priors (P)**
- Lock rows only when certified (identities satisfied, inequalities enforced); locks are monotone across ≤3 passes.
- Priors: compute `mode_allowed_ratio` only from locked rows; n ≥ 5, IQR ≤ 0.25; clamp [0.20,1.00], quantize 0.01; exclude `semantic_uncertain=true`.
- Output: `priors.v1.json` (n, iqr, mode, applied rows, excluded reasons).

**Budgets & Stability (B)**
- Hard counters: OCR/page, calls/table; micro‑OCR/VLM off by default; cap retries deterministically.
- Stepwise normalization; rerun CRCs for every sidecar; byte‑stable reruns required.
- Output: `budget.v1.json`, `stability.v1.json` (rerun CRCs, decision diffs).

## Sidecars (AP:false)
- `evidence.v1.json` — token lattice, grids, typedness, quantization hashes.
- `assign.proof.v1.json` — column‑role mapping, alternatives, costs.
- `row.ccert.v1.json` — per‑row law, fills/derivations, rounding loci, inequality checks.
- `table.ccert.v1.json` — certified_rows, conservation, inequality = 0 proof, summary residuals.
- `sem_map.choice.v1.json` — candidate decision under Alignment Locks.
- `priors.v1.json` — doc‑level priors, gating metrics, applied rows.
- `budget.v1.json`, `stability.v1.json` — budget counters, byte‑stability.

## Deterministic Failure Codes (operators)
- `MAP_MISSING[hard]`: no extraction_role_map present when required for extraction.
- `MAP_INCOMPLETE[post-ilp]`: map_completeness < 3 after ILP promotion (when enforcement is ON).
- `ZERO_CERT_AFTER_PROMOTION`: certified_rows == 0 after applying a mapping.
- `SUMMARY_EXPLODES`: average residual > 50¢/row; skip summary penalty this pass.
- `BYTE_DRIFT`: byte-stability mismatch on reruns.
- `SUPPLEMENT_MIX`: service-gate rejected table (non-service signals).
- `NUMERIC_WEAK`: relaxed ILP admission rejected (no currency/thousands anchors ≥ 0.30).

## Service-Table Gate (canary)
Tables enter ILP only if they look like service grids: ≥ 3 admitted money columns (strict typedness), low alpha mean (≤ 0.35), and at least one CDT/date column. Otherwise skip ILP and rely on rowize fallback.

## Units
Residuals/penalties: cents. Service values: dollars. Summary residuals in `table.ccert.summary_alignment` are signed; large opposite swings in Paid/Patient often indicate a 3‑money role swap.

## Policy Clarity
`--summary-penalty` is telemetry-only; only the canary apply preset (`--summary-penalty-apply`) affects energy/selection. Selection remains always‑on telemetry; priors/locks remain gated per invariants.

## Validation Gates
- Coverage: rows_3of4 ≥ 0.55 (smoke).
- Accuracy: conservation ≥ 0.35 to ship; target ≥ 0.90 on gold payers (≥ 10 docs / ≥ 300 lines).
- Inequalities: no violations; rounding recorded at a single locus.
- Locks: row_locked_pct ≥ 0.20; monotone across passes; `semantic_uncertain=true` excluded from priors/locks but always extracted and output.
- Budgets: p95 ≤ 3 s/page; OCR/page ≤ 4; ≤ 3 calls/table.

## Execution Scaffolding (concrete plan and paths)

- Law solver scaffolding
  - File: `scripts/solver_laws.py`
    - Exposes: `in_network()`, `noncovered()`, `cob_secondary()` with integer‑cents per‑row solves
    - API: `min_energy_row(row_values) -> {law, certified, violation_energy, rounding_locus, derived_terms}`
  - Sidecars: schemas added
    - `schemas/row.ccert.v1.json`, `schemas/table.ccert.v1.json`
  - Wiring points in `scripts/assemble_eob.py`
    - `compute_coherence()` → call `compute_row_law_violation()` using `solver_laws`
    - `apply_coherence_lock()` → attach row certificates; lock only certified rows; exclude `_semantic_uncertain` from priors/locks

- Payer quality map tool (ROI shortlist)
  - File: `scripts/tools/payer_quality_map.py`
    - Input: `SESSION/AuditLogs/holdout/<dataset>/aggregate.json`
    - Output: `out/holdout/payer_quality_map.json` with: docs, services, doc_mode_accept_rate, mapper_win_rate, money_rows_ratio_mean, code/date coverage, uncertain_rate, rows_3of4_rate, conservation_mean, p95_s_per_page, roi

- Optional (stubs now, tune later)
  - Evidence schema: `schemas/evidence.v1.json` for typedness and grid hashes
  - Stability/Budget schemas: `schemas/stability.v1.json`, `schemas/budget.v1.json`
  - Summary reconciler hook: add `summary_box.v1.json` and wire a stub parser; apply Lagrange penalty when enabled

## Ops — How To Use In Sessions
- Always attach: `SESSION/Northstar.md`, `SESSION/Alignment.md`, `SESSION/AI_Primer.md`, latest brief under `SESSION/AuditLogs/<date_slug>/brief.md`.
- New behavior ships behind flags (default OFF) and emits its own sidecars. Keep current defaults unchanged until validated.
- Every rejection/fallback logs a deterministic `reason` plus relevant slice hashes.

## Repro Commands (reference; keep byte‑stable)
- Holdout (document mode, gated, reuse NDJSON when present): see `SESSION/BuildLog.md` (Run Index and commands).
- Doc‑Only acceptance and hybrid runs: see `SESSION/BuildLog.md`.

## Chat/Build Log Template (append‑only)
```
### <YYYY-MM-DD> — <short title>
- Context: <what changed / what was investigated>
- Inputs: <pages/corpus>, git_sha=<sha>, macOS/Swift/Xcode versions
- Commands: <exact commands run>
- Artifacts: <paths to sidecars / out dirs>
- KPIs: conservation=<x>, rows_3of4=<x>, inequality_ok=<x>, p95_s_per_page=<x>
- Decisions: <selection policy outcome, locks/priors applied, rounding loci>
- Reasons: <structured reasons / hashes for rejections>
- Byte stability: <hashes same? yes/no; diffs if any>
- Next: <next rung / risks>
```

## Build Log (append‑only)
### 2025-11-06 — Northstar doc + Alignment Locks
- Context: Established Geometry→Roles→Proofs northstar; codified Alignment Locks in BuildLog.
- Inputs: N/A (documentation update)
- Commands: Updated `SESSION/BuildLog.md`; added `SESSION/Northstar.md`.
- Artifacts: `SESSION/BuildLog.md`, `SESSION/Northstar.md`
- KPIs: N/A
- Decisions: Selection policy/gating/priors frozen per Alignment Locks.
- Byte stability: N/A
- Next: Scaffold solver schemas and flags; keep defaults OFF until validated.
