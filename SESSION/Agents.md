# Agents Alignment Deck

This document is the single entrypoint any AI or human collaborator must read **after** syncing the repo. It links to all context packages and lays out exactly how to operate inside NativeOCR.

## 1. Attach these files to every session immediately
- `SESSION/Northstar.md` — northstar + alignment locks + chat/build log
- `SESSION/Alignment.md` — ethos + non-negotiables
- `SESSION/AI_Primer.md` — operational playbook
- Latest brief under `SESSION/AuditLogs/<date_slug>/brief.md`

## 2. Quickstart checklist for new agents
1. **Read the Alignment + Primer**; they are the contract.
2. Load the latest mission brief in `SESSION/AuditLogs`. Extract:
   - Current gap / mission
   - Invariants to maintain
   - State to inspect before coding
   - KPIs / validations after changes
3. Run the command(s) listed in the brief to reproduce state locally. Confirm byte stability (same hashes on reruns).
4. Plan work: state target acceptance criteria and failure classes before editing.
5. Make changes deterministically (flags default OFF, log reasons, update sidecars).
6. Update the mission brief (or create a new dated brief) once the work is done. Append to `SESSION/10-23/Acceptance.md` if the contract shifts.

## 3. Non-negotiable invariants (repeat every session)
- Elegance > expediency; remove failure categories.
- Deterministic: offline, greedy decode, `*_sha256` hashes for every new artifact.
- No schema churn (`page v1.2`, `run v1.2` immutable). Detailed data lives in sidecars/AP:false.
- Flags before policy: new behavior is behind a CLI flag, OFF by default, with logged evidence.
- Every rejection or fallback must emit a structured reason + slice hash in an error sidecar.
- Re-run after changes to ensure byte-identical outputs; if hashes differ unexpectedly, stop and inspect.

## 4. Critical directories & files
- `SESSION/Alignment.md` — ethos / core constraints
- `SESSION/AI_Primer.md` — full operational playbook (architecture, ladders, validation commands)
- `SESSION/AuditLogs/` — session briefs (mission, invariants, KPIs)
- `SESSION/10-23/Acceptance.md` — historical log; append dated updates when behavior changes
- `scripts/dots-ocrd.py` — runner logic (dual-pass VLM, idempotency, header echo detection)
- `scripts/assemble_eob.py` — assembler (Vision tables, VLM tables, spatial merge, HIM)
- `runs/` — sample outputs (keep at least one success + one failure fixture)

## 5. Reporting cadence
- After completing a change or investigation, append a dated update to `SESSION/10-23/Acceptance.md` and drop/refresh the brief in `SESSION/AuditLogs`.
- Summaries should include: change description, state/provenance files touched, remaining gaps, next recommended rung.

## 6. How to ask questions / escalate
- If you encounter a failure path not covered by the primer, produce a new brief with:
  - Minimal reproduction
  - Observed reason codes / hashes
  - Hypothesized remedy (deterministic only)
- Share the brief before coding; align on the rung (CP#) before implementation.

By following this deck, any agent—human, ChatGPT, Claude, etc.—arrives synchronized with the current mission, invariants, and validation plan. DO NOT begin edits until you’ve consumed these files and confirmed the latest brief.
