# RFC-0013: Stage D — Deterministic Chunk & Index for Dental EOB Retrieval

**Status:** Standards Track / Architecture (Proposed)
**Audience:** OCR/VLM owners, domain assembler owners, retrieval/infra owners
**Immutability:** Normative requirements are binding once merged; future changes MUST be versioned and ledgered.
**Context:** Extends current pipeline after **Assemble Dental EOBs** (Stage C), without modifying upstream contracts. Current stack already logs deterministic run manifests with NDJSON CRCs and VLM seams.   

---

## 1) North Star

*Max-precision, low-latency retrieval over EOBs with perfect replay:* chunk semantically, normalize strictly, enrich with provenance, and index with **metadata-first filters → embedding rerank**, all under append-only, reproducible logs. We **do not** change OCR/VLM contracts or run-manifest structure; we **mirror** them to preserve determinism and auditability.  

---

## 2) Scope & Non-Goals

**In-scope (Stage D):**

* Transform `out/eob/*.json` into small, filterable chunks (`header`, `claim`, `service_line`) with canonical `text` for embeddings.
* Add deterministic IDs/hashes, strict enums, and JSON Pointer provenance.
* Build a local DuckDB index + optional vector table.
* Extend run manifests with `chunk_runs[]`.

**Non-Goals:**

* Changing Stage A/B/C schemas or VLM contracts. 
* Networked services or cloud dependencies (remain local/offline by default). 

---

## 3) Invariants & Guardrails

* **Determinism:** fixed canonicalization; IDs/hashes derived from canonical JSON (scope documented below).
* **Provenance:** carry `source_doc_sha256`, `page_idx`, `line_idx`, and `eob_json_pointer` back to EOB JSON.
* **Append-only:** write NDJSON, record `line_count` and `crc32` for each chunk file, mirror existing run-manifest guarantees for NDJSON CRCs. 
* **No network calls;** fail fast on malformed input. 

---

## 4) Contracts

### 4.1 Chunk Schema (`schemas/rag/chunk.v1.json`) — **Normative**

Key requirements:

* Exactly three types: `header`, `claim`, `service_line`.
* **One canonical `text` field** (≤8000 chars) per record for embeddings; overflow to `notes`.
* Deterministic identity: `chunk_uid` and `chunk_sha256` computed from the **canonical JSON with `{chunk_uid, chunk_sha256, *_scope}` omitted**.
* Normalization: uppercase codes; unify to `proc_cd` + `proc_system ∈ {"ADA","CPT"}`; strict `decision` enum; duplicate dates as ISO strings; add `claim_quarter` (e.g., `2025Q3`).
* Provenance: `source_doc_sha256`, `page_idx`, `line_idx`, `eob_json_pointer`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "rag/chunk.v1.json",
  "type": "object",
  "required": ["type","text","source_doc_sha256","chunk_uid","chunk_sha256"],
  "properties": {
    "type": { "enum": ["header","claim","service_line"] },
    "text": { "type": "string", "minLength": 1, "maxLength": 8000 },
    "notes": { "type": "string" },

    "payer_name": { "type": "string" },
    "payer_id":   { "type": ["string","null"] },
    "member_id":  { "type": "string" },
    "subscriber_name": { "type": ["string","null"] },
    "provider_npi": { "type": ["string","null"], "pattern": "^[0-9]{10}$" },
    "office_id": { "type": ["string","null"] },

    "claim_id":   { "type": ["string","null"] },
    "claim_date": { "type": ["string","null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "claim_status": { "type": ["string","null"] },
    "claim_quarter": { "type": ["string","null"], "pattern": "^\\d{4}Q[1-4]$" },

    "proc_system": { "type": ["string","null"], "enum": ["ADA","CPT",null] },
    "proc_cd": { "type": ["string","null"] },
    "dos": { "type": ["string","null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "decision": { "type": ["string","null"], "enum": ["Approved","Denied","Partial","Pending","Adjusted",null] },
    "denial_reasons": { "type": "array", "items": { "type": "string" } },

    "total_billed": { "type": ["number","null"] },
    "total_allowed": { "type": ["number","null"] },
    "total_paid": { "type": ["number","null"] },
    "total_patient": { "type": ["number","null"] },

    "billed": { "type": ["number","null"] },
    "allowed": { "type": ["number","null"] },
    "paid": { "type": ["number","null"] },
    "adjustment": { "type": ["number","null"] },
    "patient_responsibility": { "type": ["number","null"] },

    "tooth": { "type": ["string","null"] },
    "surface": { "type": ["string","null"] },

    "source_doc_sha256": { "type": "string", "minLength": 32 },
    "page_idx": { "type": "array", "items": { "type": "integer" } },
    "line_idx": { "type": ["integer","null"] },
    "eob_json_pointer": { "type": ["string","null"] },

    "chunk_uid": { "type": "string", "minLength": 32 },
    "chunk_sha256": { "type": "string", "minLength": 32 },
    "chunk_sha256_scope": { "type": "string" }
  },
  "additionalProperties": false
}
```

### 4.2 Chunk-Run Manifest Extension — **Normative**

Extend existing run manifest (which already logs input SHA-256, per-page CRC32, and NDJSON CRC/line-count) with:

```
"chunk_runs": [{
  "input_eob_path": "out/eob/ABC.json",
  "output_jsonl_path": "out/chunks/ABC.jsonl",
  "line_count": 1234,
  "crc32": "2fdc1b83",
  "response_sha256": "<sha256(canonical file contents)>",
  "schema": "rag/chunk.v1",
  "ts": "2025-10-23T01:23:45.678Z"
}]
```

This mirrors the existing manifest ethos and ensures audit-ready replay.  

---

## 5) Algorithms (Deterministic)

1. **Canonicalization:** NFC-normalize strings; canonical JSON dump (`sort_keys=True`, commas/colons compact).
2. **IDs & Hashes:** Compute `chunk_uid` and `chunk_sha256` over the canonical record **omitting** `{chunk_uid, chunk_sha256, *_scope}`; set `chunk_sha256_scope = "canonical JSON with {chunk_uid,chunk_sha256,*_scope} omitted"`.
3. **Type-specific `text`:**

   * `header`: payer/member/provider/remit/office in 4–6 labeled lines.
   * `claim`: claim id/date/status, patient, totals, denial reasons.
   * `service_line`: `proc_system proc_cd`, description, DOS, tooth/surface, decision + money line, reasons.
     Cap to 8k chars; overflow → `notes`.
4. **Normalization:** uppercase code → `proc_cd`; infer `proc_system` (`D*`→ADA, numeric→CPT); ISO dates only; compute `claim_quarter` from `claim_date`.
5. **Provenance:** copy `source_doc_sha256`; thread `page_idx`, `line_idx`; build `eob_json_pointer` (e.g., `/claims/2/service_lines/3`).
6. **Write:** newline-delimited JSON to `out/chunks/<eob_stem>.jsonl`; record `line_count`, `crc32`, and `response_sha256` in `chunk_runs[]`.

---

## 6) Directory Layout & Make Targets

```
out/
  eob/*.json            # Stage C output (unchanged)
  chunks/*.jsonl        # NEW Stage D artifacts
index/
  duckdb/eobs.duckdb    # local index (optional vectors table)
```

**Makefile**

```make
emit-chunks:        ## Stage D: EOB → Chunks
\tpython3 scripts/eob_to_chunks.py

schema-check-chunks:
\tnpx ajv-cli -s schemas/rag/chunk.v1.json -d 'out/chunks/*.jsonl' --spec=draft2020

index-chunks:
\tpython3 scripts/index_chunks.py  # optional: DuckDB + vectors
```

---

## 7) Indexing (DuckDB + Vectors)

**Relational core** (metadata-first filters):

```sql
CREATE TABLE IF NOT EXISTS chunks AS SELECT * FROM read_json_auto('out/chunks/*.jsonl');

CREATE INDEX IF NOT EXISTS idx_type      ON chunks(type);
CREATE INDEX IF NOT EXISTS idx_proc_cd   ON chunks(proc_cd);
CREATE INDEX IF NOT EXISTS idx_decision  ON chunks(decision);
CREATE INDEX IF NOT EXISTS idx_dos       ON chunks(dos);
CREATE INDEX IF NOT EXISTS idx_claim_qtr ON chunks(claim_quarter);
CREATE INDEX IF NOT EXISTS idx_office    ON chunks(office_id);
CREATE INDEX IF NOT EXISTS idx_payer     ON chunks(payer_name);
```

**Vectors** (optional, local): table `chunk_vecs(chunk_uid TEXT PRIMARY KEY, vec BLOB)`.
Query pattern: **filter → top-K → embed `text` → cosine rerank**.

Example candidate set (D2740 denied, last quarter, site = Lehi):

```sql
SELECT * FROM chunks
WHERE type='service_line'
  AND proc_cd='D2740'
  AND decision='Denied'
  AND office_id='PLATINUM-LEHI'
  AND dos BETWEEN '2025-07-01' AND '2025-09-30'
LIMIT 500;
```

---

## 8) Reference Implementation (Skeleton)

**Chunker — `scripts/eob_to_chunks.py`** (deterministic IDs, canonical `text`, enums, JSON Pointer)

```python
#!/usr/bin/env python3
import json, sys, pathlib, unicodedata, hashlib

def nfc(s): return unicodedata.normalize("NFC", s or "")
canon = lambda o: json.dumps(o, sort_keys=True, separators=(",",":"), ensure_ascii=False)
sha   = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()

def quarter(date_iso):
    if not date_iso: return None
    y,m,_ = date_iso.split("-"); return f"{y}Q{(int(m)-1)//3 + 1}"

def add_ids(rec):
    tmp = {k:v for k,v in rec.items() if k not in ("chunk_uid","chunk_sha256","chunk_sha256_scope")}
    payload = canon(tmp); h = sha(payload)
    rec["chunk_uid"] = h; rec["chunk_sha256"] = h
    rec["chunk_sha256_scope"] = "canonical JSON with {chunk_uid,chunk_sha256,*_scope} omitted"
    return rec

def text_header(h):
    parts = [f"Payer: {h.get('payer_name','')} ({h.get('payer_id','')})",
             f"Member: {h.get('member_id','')} / {h.get('subscriber_name','')}",
             f"Provider NPI: {h.get('provider_npi','')}",
             f"Remit: {h.get('check_number','') or h.get('era_trace','')} on {h.get('check_date','')}",
             f"Office: {h.get('office_id','')}"]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def text_claim(c):
    parts = [f"Claim {c.get('claim_id','')} on {c.get('claim_date','')} — {c.get('claim_status','')}",
             f"Patient: {c.get('patient_name','')} (DOB {c.get('patient_dob','')})",
             f"Totals: billed ${c.get('total_billed',0):.2f} | allowed ${c.get('total_allowed',0):.2f} | paid ${c.get('total_paid',0):.2f} | patient ${c.get('total_patient',0):.2f}",
             ("Reasons: " + "; ".join(c.get("denial_reasons", []))) if c.get("denial_reasons") else ""]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def text_line(sl):
    parts = [f"{sl.get('proc_system','ADA')} {sl.get('proc_cd','')}: {sl.get('description','')}",
             f"DOS {sl.get('dos','')} | Tooth {sl.get('tooth','')}" + (f" Surface {sl.get('surface','')}" if sl.get('surface') else ""),
             f"Decision: {sl.get('decision','')} — billed ${sl.get('billed',0):.2f}, allowed ${sl.get('allowed',0):.2f}, paid ${sl.get('paid',0):.2f}, patient ${sl.get('patient_responsibility',0):.2f}",
             ("Reasons: " + "; ".join(sl.get("denial_reasons", []))) if sl.get("denial_reasons") else ""]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def emit(eob, w, src_sha):
    payer, member, provider = eob.get("payer",{}), eob.get("member",{}), eob.get("provider",{})
    hdr = {"type":"header","payer_name":payer.get("name"),"payer_id":payer.get("id"),
           "member_id":member.get("id"),"subscriber_name":member.get("name"),
           "provider_npi":provider.get("npi"),"office_id":eob.get("office_id"),
           "check_number":eob.get("remit",{}).get("check_number"),
           "check_date":eob.get("remit",{}).get("check_date"),
           "era_trace":eob.get("remit",{}).get("era_trace"),
           "source_doc_sha256":src_sha,
           "page_idx": sorted({pi for c in eob.get("claims",[]) for pi in c.get("page_idx",[])})}
    hdr["text"] = text_header(hdr); add_ids(hdr); w.write(canon(hdr)+"\n")

    for ci,c in enumerate(eob.get("claims",[])):
        claim = {"type":"claim","payer_name":payer.get("name"),"member_id":member.get("id"),
                 "provider_npi":provider.get("npi"),"office_id":eob.get("office_id"),
                 "claim_id":c.get("id"),"claim_date":c.get("date"),"claim_status":c.get("status"),
                 "claim_quarter":quarter(c.get("date")),
                 "patient_name":c.get("patient",{}).get("name"),"patient_dob":c.get("patient",{}).get("dob"),
                 "total_billed":c.get("totals",{}).get("billed"),"total_allowed":c.get("totals",{}).get("allowed"),
                 "total_paid":c.get("totals",{}).get("paid"),"total_patient":c.get("totals",{}).get("patient"),
                 "denial_reasons":c.get("denials",[]),
                 "source_doc_sha256":src_sha,"page_idx":c.get("page_idx",[]),
                 "eob_json_pointer":f"/claims/{ci}"}
        claim["text"] = text_claim(claim); add_ids(claim); w.write(canon(claim)+"\n")

        for li,sl in enumerate(c.get("service_lines",[])):
            proc = (sl.get("ada_cd") or sl.get("code") or sl.get("cpt_cd") or "").upper()
            system = "ADA" if proc.startswith("D") else ("CPT" if proc[:1].isdigit() else None)
            line = {"type":"service_line","claim_id":c.get("id"),
                    "payer_name":payer.get("name"),"member_id":member.get("id"),
                    "provider_npi":provider.get("npi"),"office_id":eob.get("office_id"),
                    "proc_system":system,"proc_cd":proc,"description":sl.get("description"),
                    "dos":sl.get("dos"),"decision":sl.get("decision"),"denial_reasons":sl.get("denials",[]),
                    "billed":sl.get("billed"),"allowed":sl.get("allowed"),"paid":sl.get("paid"),
                    "adjustment":sl.get("adjustment"),"patient_responsibility":sl.get("patient"),
                    "tooth":sl.get("tooth"),"surface":sl.get("surface"),
                    "source_doc_sha256":src_sha,"page_idx":sl.get("page_idx",[]),
                    "line_idx":li,"eob_json_pointer":f"/claims/{ci}/service_lines/{li}"}
            line["text"] = text_line(line); add_ids(line); w.write(canon(line)+"\n")

if __name__ == "__main__":
    in_dir  = pathlib.Path("out/eob")
    out_dir = pathlib.Path("out/chunks"); out_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(in_dir.glob("*.json")):
        eob = json.loads(p.read_text())
        src_sha = eob.get("source_doc_sha256") or hashlib.sha256(p.read_bytes()).hexdigest()
        with (out_dir / (p.stem + ".jsonl")).open("w", encoding="utf-8") as w:
            emit(eob, w, src_sha)
```

---

## 9) Acceptance Gates

* `emit-chunks` writes `out/chunks/*.jsonl`; **every line** validates against `schemas/rag/chunk.v1.json`.
* Each record contains **`text`**, **`chunk_uid`**, **`chunk_sha256`**, and provenance (`source_doc_sha256`, `page_idx`, `line_idx` or `eob_json_pointer`).
* The query macro **“D2740 crowns denied last quarter @ site X”** resolves by **filters only**; embeddings only rerank ties.
* Run manifest contains a `chunk_runs[]` entry with `line_count`, `crc32`, and `response_sha256` (canonical file hash), mirroring existing manifest practices (NDJSON CRCs, env, routing, VLM logs). 

---

## 10) Migration & Compatibility

* **Zero impact** to Stages A–C; all inputs/outputs preserved. VLM seam, CLI flags, and run-manifest behavior remain intact. 
* Stage D is **additive**; can be toggled via Make targets.

---

## 11) Risks & Mitigations

* **Duplicate lines across EOB variants** → deterministic `chunk_uid` de-dupes at index layer.
* **Enum drift (decision/denials)** → enforce schema; surface unknown values to SRE log; add controlled vocab mapping in a follow-up RFC.

---

## 12) Ops Playbook (Day-1)

1. `make assemble && make schema-check-eob` (Stage C). 
2. `make emit-chunks && make schema-check-chunks`.
3. `make index-chunks` (optional vectors).
4. Run canned queries; verify candidate counts vs. fixture expectations.
5. Archive run manifests + `out/chunks` together for replay.

---

### Appendices

* **Upstream Guarantees referenced:** NDJSON CRC/line-count, routing + VLM logs, deterministic env logging, exit codes.   

---

## Assumptions

* `out/eob/*.json` adheres to `DentalEOBSchema.v1.2.json` and already contains `source_doc_sha256` or can compute it. 
* Local embeddings available (e.g., Gemma slice) or deferred; not normative to this RFC.

## Confidence

**[E2]** — Architecture aligns with your existing contracts and manifests; minor details (decision enum scope, denial taxonomy) may evolve.

## Residual Risks

* Denial reason strings remain heterogeneous; a controlled vocabulary may be required to maximize filter precision later.
* Quarter bucketing (`claim_quarter`) could mislead if data consumers expect fiscal rather than calendar quarters; document consumer expectations in downstream specs.
