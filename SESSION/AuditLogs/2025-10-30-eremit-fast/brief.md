Mission
- Confirm we’re achieving sub-1s/page capture and deterministic canonical outputs on EREMIT60pg without involving VLM.

Constraints
- No edits to existing pipelines; add-only acceptance assets.
- Vision-only Text mode; no VLM runner; no micro-OCR.
- Offline, deterministic behavior; capture any reasons/sidecars as AP:false.

KPIs
- p95 per-page duration < 1000 ms.
- 50–60 page doc completes < 60 s wall time.
- Repeated assembly yields identical canonical_sha256 and file SHA.

Procedure
1) Build Swift CLI: `make ensure-release`
2) Run acceptance suite: `scripts/acceptance_eremit_fast.py`
3) Inspect report: `SESSION/AuditLogs/2025-10-30-eremit-fast/report.json`
   - Fields: pages, p95_ms, doc_wall_s, deterministic, artifact paths.

Artifacts
- NDJSON: `bench/eremit_fast/eremit60_text.jsonl`
- Run manifest: `runs/eremit60_text_run.json`
- Canonical output: `out/eremit_fast/EREMIT60pg.json[.sha256]`
- Report: `SESSION/AuditLogs/2025-10-30-eremit-fast/report.json`

Notes
- This suite deliberately skips Document mode and VLM routing to isolate Vision throughput.
- Use `--dpi 300` if you want higher fidelity; it should still hit the target on current MBP hardware.

