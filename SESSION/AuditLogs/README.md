# SESSION/AuditLogs

This directory hosts short context packages (“briefs”) for each session or change burst.

For every session:

- Create a dated subfolder `YYYY-MM-DD_<slug>`.
- Drop a single `brief.md` with:
  - **Mission** (2-3 bullets: gap / target / acceptance gates)
  - **Invariants** (remind the AI of ethos / non-negotiables relevant today)
  - **State to inspect** (hashes, sidecars, commands to run)
  - **KPIs / Validation** (expected metrics, how to verify)

All agents must read the latest brief before executing changes.

