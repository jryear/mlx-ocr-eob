# Release Manifest Template

## Release Information
- **Date**: YYYY-MM-DD HH:MM UTC
- **Version**: vX.Y.Z or YYYY-MM-DD
- **Release Type**: [production|canary|staged]
- **Git SHA**: [commit hash]
- **Tag**: [git tag if applicable]

## Environment
- **macOS Version**: [Darwin version]
- **Swift Version**: [swift --version]
- **Xcode Version**: [xcodebuild -version]
- **Python Version**: [python3 --version]

## Configuration
### Flags (Production Preset)
```bash
--solve-roles
--ilp-assign-extract
--summary-reconcile
--emit-evidence
# Optional strict mode:
# --solver-locks-strict
# Optional enforcement:
# --ilp-enforce-map
```

### Policy Parameters
- **Selection latch**: 0.15
- **ILP gate**: certified_rows_sample ≥ 1 OR min_money_typedness ≥ 0.20
- **Map completeness threshold**: ≥ 3 (when enforced)
- **Priors gate**: require_locked_rows ≥ 5, IQR ≤ 0.25

### Schema Versions
- **EOB Schema**: DentalEOBSchema.v1.2.json
- **Assign Proof**: assign.proof.v1
- **Evidence**: evidence.v1
- **Map Metrics**: extraction.map_metrics.v1
- **Selection Evidence**: selection.evidence.v1
- **Row/Table Certs**: row.ccert.v1 / table.ccert.v1

## Verification Results
### Verifier Run
```bash
# Command:
make verify-northstar BASE=out/synth STRICT=out/synth_strict ENFORCE=1

# Exit code: [0=pass, 1=fail, 4=MAP_INCOMPLETE]
# Output:
```

### KPIs (Control Set)
- **Certified rows**: [count]
- **Conservation**: [0.0-1.0]
- **Row locked %**: [0.0-1.0]
- **Applied for extraction rate**: [0.0-1.0]
- **Map completeness**: median=[0-4], p95=[0-4]
- **Anchors per row mode**: [0-3]
- **Role coverage**: [0.0-1.0]

### Byte Stability
```bash
# Command:
find out/synth -name '*.json' | sort | xargs shasum -a 256 > hashes_1.txt
# Rerun...
find out/synth -name '*.json' | sort | xargs shasum -a 256 > hashes_2.txt
diff hashes_1.txt hashes_2.txt

# Result: [identical|differences detected]
```

## Artifacts
### Sidecars Emitted
- [ ] assign.proof.v1.json (ILP decisions + evidence)
- [ ] extraction.map_metrics.v1.json (completeness, anchors, coverage)
- [ ] row.ccert.v1.json + table.ccert.v1.json (certificates)
- [ ] selection.evidence.v1.json (always-on telemetry)
- [ ] evidence.v1.json (typedness, quantization, swap energy)
- [ ] summary_box.v1.json (reconciliation)
- [ ] Optional: budget.v1.json, stability.v1.json

### Artifact Hashes (SHA-256)
```
[list key artifact hashes for audit trail]
```

## Test Coverage
### Control Tests
- [ ] Synthetic complete (clean headers): PASS
- [ ] Map completeness ≥ 3: PASS
- [ ] Strict locks == certified: PASS

### Adversarial Tests  
- [ ] EREMIT (mixed quality): PASS
- [ ] Intelligent ILP gating: PASS
- [ ] HIM fallback working: PASS

### Enforcement Tests
- [ ] Hard enforcement on control: PASS
- [ ] Enforcement bypass on low-quality: PASS

## Known Issues / Limitations
- [List any known limitations or edge cases]
- [Document any expected failures or warnings]

## Rollout Plan
### Stage 1: Canary (24-48h)
- Corpus: [specify canary set]
- Enforcement: [ON|OFF]
- Monitoring: [KPIs to track]

### Stage 2: Gradual Widen (2-3 days)
- Corpus: [expanded set]
- Enforcement: [ON for control, OFF for general]

### Stage 3: Full Release
- Corpus: [production corpus]
- Enforcement: [final setting]

## Approval
- **Reviewer**: [name] - [date]
- **Master Agent**: [approval status]
- **Deployment**: [scheduled date/time]

## Notes
[Any additional context, observations, or next steps]
