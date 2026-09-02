Answer → Assumptions → Confidence [E2] → Residual risks (2)

# Answer

Contract pack that closes the **six blocking** and **three important** gaps. Determinism is preserved: state is explicit, hashes bind all derived artifacts, and every fallback is recorded with reason codes.

---

## Update — 2025-10-24

- Assembly now supports a flag-gated VLM table provider (`--use-vlm-tables`). When enabled, doc SHA resolution follows run manifest → source hash → explicit override, VLM `result.data.v1` artifacts are filtered for `status:"ok"` + rectangularized cells, and deterministic `.vlm_tables.json` sidecars capture accepted paths, hashes, and skip reasons.
- Smoke plan expanded: step 8 validates provenance source coverage, `.vlm_tables.json` determinism, and error reasons via jq. Current blocker is runner outputs with blank cells; prompt tightening + JSON validation + larger model under way to unblock conservation improvements.

---

## 1) Dual-pass VLM contract (headers → data) [BLOCKING — CLOSED]

### Determinism principle

* **No hidden runner state.** Pass-2 must carry the **header lock** inline. The runner *may* cache, but cache is advisory; inputs are canonical.
* Every pass emits a **content hash** of its primary output, bound to the image hash.

### Message flow (STDIO JSON-lines; all fields required unless noted)

**Pass-1 request (headers-only)**

```json
{
  "type": "infer.headers.v1",
  "session_id": "sess-20251023-abc",
  "roi_id": "roi-000123",
  "image_sha256": "…",
  "image_b64": "<bytes>",
  "schema_hint": {
    "canonical_columns": ["proc_cd","desc","dos","billed","allowed","paid","adjust","patient"],
    "types": {"billed":"money","dos":"date"}
  },
  "payer_hint": "PPO_ACME",
  "decode": {"max_new_tokens": 512, "timeout_s": 60, "greedy": true},
  "budget": {"class": "typical", "est_rows": 8, "est_cols": 7, "est_cells": 56, "est_source": "vision_lines"},
  "compact": true
}
```

**Pass-1 response**

```json
{
  "type": "result.headers.v1",
  "session_id": "sess-20251023-abc",
  "roi_id": "roi-000123",
  "detected_headers": [
    {"text":"DATE OF SERVICE","text_norm":"DATE OF SERVICE","bbox":[x,y,w,h],"order_idx":0},
    {"text":"ALLOWED","text_norm":"ALLOWED","bbox":[x,y,w,h],"order_idx":1},
    …
  ],
  "mapping": [
    {"det_idx":0,"canonical":"dos","cost":0.18,"cost_terms":{"lev":0.10,"jacc":0.12,"pos":0.0,"syn":-0.04}},
    {"det_idx":1,"canonical":"allowed","cost":0.07,"cost_terms":{"lev":0.05,"jacc":0.06,"pos":0.0,"syn":-0.04}}
  ],
  "canonical_order": ["proc_cd","desc","dos","billed","allowed","paid","adjust","patient"],
  "header_lock": {
    "canonical_order": ["proc_cd","desc","dos","billed","allowed","paid","adjust","patient"],
    "mapping": [{"canonical":"dos","det_idx":0}, {"canonical":"allowed","det_idx":1}, …],
    "unmapped": ["proc_cd","desc","adjust","patient"],        // empty if perfect
    "lock_sha256": "sha256(canonical_order||mapping||image_sha256)"
  },
  "model_meta": {
    "response_sha256":"…",
    "parse_repair":"balanced_braces|none",
    "decode_kwargs_accepted":{"max_new_tokens":512},
    "budget_class": "typical",
    "est_cells": 56,
    "est_source": "vision_lines",
    "runner_mode":"persistent",
    "model_fingerprint": { "name":"…","revision_sha":"…","quant":"…" }
  },
  "status": "ok|empty|invalid"
}
```

**Pass-2 request (data under locked headers)**

```json
{
  "type": "infer.data.v1",
  "session_id": "sess-20251023-abc",
  "roi_id": "roi-000123",
  "image_sha256": "…",
  "image_b64": "<bytes>",
  "header_lock": {
    "canonical_order": ["proc_cd","desc","dos","billed","allowed","paid","adjust","patient"],
    "mapping": [{"canonical":"dos","det_idx":0}, {"canonical":"allowed","det_idx":1}, …],
    "lock_sha256": "…"
  },
  "decode": {"max_new_tokens": 1024, "timeout_s": 120, "greedy": true},
  "budget": {"class":"typical","est_rows":8,"est_cols":7,"est_cells":56,"est_source":"vision_lines"},
  "compact": true
}
```

**Pass-2 response**

```json
{
  "type": "result.data.v1",
  "session_id": "sess-20251023-abc",
  "roi_id": "roi-000123",
  "rows": 12,
  "cols": 8,
  "cells": ["D2740","Crown","2025-06-01","110000","80000","20000","-10000","10000", …],
  "rectangularized": true,
  "model_meta": {
    "attempts": 1,
    "response_sha256":"…",
    "parse_repair":"balanced_braces|row_patch|none",
    "budget_class":"typical",
    "est_cells":56,
    "est_source":"vision_lines"
  },
  "header_lock_sha256":"…",
  "status":"ok|truncated|invalid"
}
```

### Failure policy

* Pass-1 `status!="ok"` → **single preproc-retry** then **abort** to Vision/other ROI; record `route_reason`.
* Pass-2 `status="truncated|invalid"` → **row-targeted re-decode +25%**; if still invalid on any row after one retry → **mark_incomplete** for those rows; do **not** mutate header_lock.

### Schemas

* `schemas/vlm/header_pass.v1.json` and `schemas/vlm/data_pass.v1.json` mirror the above fields with `required` lists and type guards (money, date, code). (Implement exactly as shown; use `enum` for `status` and `parse_repair`.)

---

## 2) `est_cells` provenance and logging [BLOCKING — CLOSED]

**Primary signal (`vision_lines`)**

```
est_rows = count of Vision text lines within ROI whose width ≥ 0.4*ROI_w OR that contain a money/date/code token
est_cols = max( number of header tokens separated by ≥ δx , peaks_in_vertical_projection )
δx = 0.06 * ROI_w  (fixed)
est_cells = clamp(est_rows * est_cols, 1, 4096)
```

**Fallback 1 (`proj_peaks`)**
If Vision returns < 4 lines or confidence median < 0.55:

* Binarize (Sauvola), compute vertical projection; peaks via local-max with prominence ≥ 0.08 * max; `est_cols = peaks`.
* `est_rows` = number of horizontal run clusters whose height ≥ 0.75 * median text height heuristic (= 0.012 * ROI_long_side).

**Fallback 2 (`default`)**
If both unavailable: `est_rows=8, est_cols=7, est_cells=56`.

**Logging**

* Put `budget.est_rows|est_cols|est_cells|est_source` in every **infer** request and mirror to `model_meta` in every **result**.
* Persist in page preproc manifest: `page.meta.vision_est_cells`.

---

## 3) Hungarian matching specification [BLOCKING — CLOSED]

**Normalization**

* Uppercase, fold diacritics, remove punctuation, collapse whitespace.

**Cost terms**

* `lev_norm(h,c)` = Levenshtein(h,c) / max(|h|,|c|).
* `jac_sim(h,c)` = Jaccard(tokens(h), tokens(c)); use bigrams if single-token.
* `pos_pen(i,j,n)` = |i−j| / max(n−1,1) where `i` is detected header order by x-center, `j` is canonical index, `n` is column count.
* `syn_bonus(h,c)` = 0 if none; else −0.20 if `h` in synonym set of `c` (e.g., ALLOWED∈{ALLOWED,ALLOWANCE}).

**Total cost**

```
cost(i,j) = 0.60*lev_norm + 0.40*(1 - jac_sim) + λ*pos_pen - syn_bonus
λ = 0.25
Clamp to [0,1].
```

**Assignment**

* Build cost matrix `C[i,j]` over detected headers i and canonical columns j.
* Solve with Hungarian algorithm (minimize sum).
* Reject matches with `cost ≥ 0.50` → those canonicals go to `unmapped`.

**Tie-breaks (deterministic)**

1. Lower `cost`.
2. Lower `pos_pen`.
3. Lexicographic `canonical`.
4. Lexicographic `detected_headers[i].text_norm`.

**Test cases**

* Provide `docs/hungarian_matching.md` with 6 fixtures:

  * Perfect match; synonym (“DOS”→`dos`).
  * Column order swapped; verify pos penalty resolves.
  * Spelling error (“ALLOWD”).
  * Extra detected header; ensure unmapped canonicals.
  * Mixed case and punctuation.
  * Dense table with 10 columns.

**Telemetry**

* Emit `mapping[].cost_terms` in `result.headers.v1`.

---

## 4) ROI classification rules [BLOCKING — CLOSED]

**Deterministic heuristic**
Given ROI with normalized centroid `cy = (y_center / page_h)` and token features:

* Compute features:

  * `H_pos = 1` if `cy ≤ 0.25` else `0`.
  * `H_lex = count(header_keywords ∩ tokens)`, header_keywords = {EOB, EXPLANATION, PROVIDER, SUBSCRIBER, MEMBER, CLAIM, CHECK, ERA}.
  * `M_money = number of money tokens ($ or 1–3 comma-separated groups with 2 decimals)`.
  * `M_codes = count ADA/CPT-like codes`.
  * `N_foot = 1` if `cy ≥ 0.75` else `0`.
* Rules (first match wins):

  1. **header** if `H_pos=1` **and** `H_lex ≥ 1`, or `H_lex ≥ 2`.
  2. **money** if `M_money ≥ 6` **or** (`M_money ≥ 3` **and** `M_codes ≥ 2`).
  3. **notes** if `N_foot=1`.
  4. Default **money** (bias to tables).

**Logging**

* `roi.classification: "header"|"money"|"notes"`, plus feature counts. Persist at page level.

---

## 5) Budget class thresholds [BLOCKING — CLOSED]

```
est_cells < 36                        → "small"
36 ≤ est_cells ≤ 120                  → "typical"
est_cells > 120                       → "large"
```

* Log as `budget.class` in request and `model_meta.budget_class` in response.
* Payer overrides: optional table `{payer_class → class_shift}`, e.g., `"dense-EOB": +1 bucket` (typical→large).

---

## 6) Gold set definition [BLOCKING — CLOSED]

**Directory**

```
bench/gold_set_v1/
  manifest.json
  eob_0001/ doc.pdf  truth.json
  eob_0002/ doc.pdf  truth.json
  …
```

**`manifest.json`**

```json
{
  "version": 1,
  "count": 10,
  "strata": {"simple":3,"typical":4,"complex":2,"fax":1},
  "items": [
    {"id":"eob_0001","sha256_pdf":"…","stratum":"simple"},
    …
  ]
}
```

**`truth.json` schema (per doc)**

```json
{
  "patient": "…",
  "provider": "…",
  "claims": [
    {
      "claim_id": "…",
      "rows": [
        {"proc_cd":"D2740","desc":"…","dos":"2025-06-01","billed":110000,"allowed":80000,"paid":20000,"adjust":-10000,"patient":10000}
      ],
      "money_conservation": true
    }
  ]
}
```

* All money in **cents**. Dates ISO. Codes uppercase.
* **Immutable.** New samples → `gold_set_v2`.

---

## 7) Confusion map scope [IMPORTANT — CLOSED]

**`confusion_maps.v1.json`**

```json
{
  "CODE":  {"O":"0","I":"1","L":"1","S":"5","B":"8","Z":"2","G":"6","Q":"0","D":"0"},
  "MONEY": {"O":"0","o":"0","S":"5","l":"1","I":"1"},
  "DATE":  {},
  "ID":    {"O":"0","I":"1","l":"1","S":"5","B":"8"},
  "TEXT":  {}
}
```

**Application rule**

* Determine column **type** from `schema_hint.types`. Apply confusion map **only** to that type.
* Patient/provider TEXT columns: **no mapping**; preserve case; ASCII-fold only.

---

## 8) Escalation policy precision [IMPORTANT — CLOSED]

**Per-row state**

```
row.retry_count ∈ {0,1}
row.status ∈ {"ok","truncated","invalid","incomplete"}
```

**Per-page counters**

```
page.retry_count ∈ {0,1}   // page-level re-decodes (row-targeted batches count as 1 when triggered)
page.failed_conservation_rows = count(rows with |billed - (allowed+paid+adjust+patient)| > 1 cent)
```

**Algorithm**

```
if page.failed_conservation_rows == 0 → accept
else:
   if page.retry_count == 0:
       re-decode only failed rows with budget * 1.25; page.retry_count = 1
   else:
       escalate model to 7B (same headers lock); re-decode only failed rows
       if any still fail → mark those rows "incomplete"
```

* Never mutate `header_lock`. Record `escalation: "row_retry"|"model_escalate"|"incomplete"` in manifest.

---

## 9) Triage grid methodology [IMPORTANT — CLOSED]

**Sample**

* 6 pages: 2 simple, 2 typical, 1 complex, 1 fax (from gold set strata).

**Ablations (independent toggles)**

* DPI: 300 vs **360** vs 420
* CLAHE: off vs **on**
* Deskew: off vs **on**
* Padding: 0 vs **12**
* Long-side: 1536 vs **1792** vs 2048

**Metric & decision**

* Primary: Vision **median confidence** per page.
* Statistic: **Wilcoxon signed-rank** comparing baseline vs candidate across the 6 pages.
* Lock a knob if `p < 0.05` **and** `Δmedian ≥ 0.05`. Persist selection to `profiles/production.json`.

---

## 10) New schemas (filenames and required fields)

* `schemas/preproc.page.v1.json`
  Required: `pdf_sha256, page_index, raster{dpi, colorspace, interpolation, cropbox}, deskew_deg, clahe{clip,tiles}, medians{vision_line_conf_median,page_conf_median}, route_decision, vision_estimate{est_rows,est_cols,est_cells,est_source}`.

* `schemas/vlm/header_pass.v1.json` and `schemas/vlm/data_pass.v1.json`
  Required exactly as in §1 messages.

* `schemas/budget_policy.v1.json`

  ```
  {
    "budget_class": {"enum":["small","typical","large"]},
    "thresholds": {"small_max":36,"typical_max":120},
    "est": {"rows": "integer ≥1", "cols": "integer ≥1", "cells": "integer ≥1", "source":{"enum":["vision_lines","proj_peaks","default"]}}
  }
  ```

All schemas use explicit `additionalProperties:false`.

---

## 11) Runner state and manifest fields (additions)

* Runner lifecycle messages: `hello/goodbye` (as previously defined).
* Each **result** carries `header_lock_sha256` (pass-2), `model_meta.budget_class`, `model_meta.est_cells`, `model_meta.est_source`.
* Run manifest append:

  * `runner_mode`, `model_fingerprint{…}`, `cache_paths{HF_HOME,TRANSFORMERS_CACHE}`
  * `session_stats{roi_count, avg_sec_per_roi, p95_sec_per_roi}`
  * `routing{roi_id, classification, route_reason}`

---

## 12) Test fixtures (minimal to start)

* `docs/hungarian_matching.md::tests/` with the 6 header matching JSON fixtures.
* `bench/gold_set_v1/` as specified.
* `scripts/tests/test_budget_policy.py` (thresholds and sources).
* `scripts/tests/test_roi_classification.py` (feature→class).

---

## 13) Execution switch

Proceed to implement phases exactly as you outlined, with these contracts as the ground truth. Any deviation requires a versioned schema bump and a recorded RFC delta.

---

## Assumptions

* PDFs are image-based; Vision and VLM run offline; Apple silicon with MLX-VLM 0.3.4; greedy decode; money in cents; ISO dates; uppercase codes.

## Confidence

[E2] High — all blocking items now have explicit schemas, formulas, thresholds, and tie-breaks; no implicit runner state; hashes bind cross-pass dependencies.

## Residual risks

* Extreme fax artifacts may still undercut `vision_lines`; fallback `proj_peaks` chosen to be conservative but could over/under-estimate columns.
* Gold set representativeness: v1 (10 docs) may not capture rare payer patterns; expect v2 with expanded strata after first CI cycle.
