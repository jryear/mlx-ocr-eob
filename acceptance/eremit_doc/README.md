EREMIT60pg — Document Mode Acceptance Suite (Vision tables)

Purpose
- Measure Vision Document-mode throughput with no VLM.
- Verify deterministic, canonical assembly while extracting services.

Artifacts
- NDJSON: bench/eremit_doc/eremit_doc.jsonl
- Run manifest: runs/eremit_doc_run.json
- Canonical EOB: out/eremit_doc/EREMIT60pg.json
- Conservation: out/eremit_doc/EREMIT60pg.conservation.json
- Report: SESSION/AuditLogs/2025-10-30-eremit-doc/report.json

Run
1) Build CLI:
   make ensure-release

2) Execute acceptance:
   scripts/acceptance_eremit_doc.py

   Options:
   - --dpi 150 (default) or 300
   - --apply-repair to enable lexical repair
   - --micro-ocr to enable per-cell micro-OCR (slower)

KPIs
- Speed: p95_ms < 1000
- Determinism: file SHA stable and canonical_sha256 present
- Tables detected: total_tables > 0
- Services extracted: services_count > 0

Notes
- VLM is not invoked; the suite relies on Vision’s document/table extraction.
- Spatial-merge is enabled in assembly as a deterministic fallback.

