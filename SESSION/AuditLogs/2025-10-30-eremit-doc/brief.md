Mission
- Validate that Vision Document mode (no VLM) provides table structure fast enough for production, with deterministic assembly and non-empty services.

Constraints
- Do not modify existing defaults; add-only acceptance.
- Keep VLM off; optional micro-OCR is flag-gated and off by default.

KPIs
- p95 per-page < 1000 ms at 150 DPI.
- Determinism: re-assembly yields identical file SHA and canonical_sha256.
- Tables detected > 0; services_count > 0.
- Conservation report present (rate > 0 preferred, but not required for acceptance).

Procedure
1) Build: `make ensure-release`
2) Run: `scripts/acceptance_eremit_doc.py`
   - Optional: `--apply-repair`, `--micro-ocr` to probe recovery paths.
3) Inspect: `SESSION/AuditLogs/2025-10-30-eremit-doc/report.json`

Artifacts
- NDJSON: `bench/eremit_doc/eremit_doc.jsonl`
- Run manifest: `runs/eremit_doc_run.json`
- EOB JSON + sidecars: `out/eremit_doc/EREMIT60pg.json*`
- Report: `SESSION/AuditLogs/2025-10-30-eremit-doc/report.json`

