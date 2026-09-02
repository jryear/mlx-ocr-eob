# 🚀 Processing Your Own EOB Data

Complete workflow from PDF → Extraction → Visualization

---

## 📁 Step 1: Prepare Your Data

### Option A: You Have PDFs

Place your EOB PDFs in a directory:
```bash
mkdir -p eobs/my_data
cp /path/to/your/eobs/*.pdf eobs/my_data/
```

### Option B: You Already Have NDJSON

If you've already run OCR and have NDJSON page files:
```bash
mkdir -p bench/my_data
cp /path/to/your/pages/*.jsonl bench/my_data/
```

---

## 🔬 Step 2: OCR Processing (PDF → NDJSON)

If starting from PDFs, use the vision-ocr-cli tool:

```bash
# Basic OCR (Vision.framework only)
vision-ocr-cli/.build/arm64-apple-macosx/release/vision-ocr-cli \
  eobs/my_data/document.pdf \
  --mode document \
  --lang en-US \
  --dpi 300 \
  --ndjson bench/my_data/document.jsonl

# Advanced: With VLM table detection (if configured)
VLM_PYTHON=~/.venvs/ml-mps/bin/python \
VLM_MODEL=mlx-community/Qwen2-VL-2B-Instruct-4bit \
vision-ocr-cli/.build/arm64-apple-macosx/release/vision-ocr-cli \
  eobs/my_data/document.pdf \
  --mode document \
  --lang en-US \
  --dpi 300 \
  --ndjson bench/my_data/document.jsonl \
  --vlm-runner scripts/dots-ocrd.py \
  --vlm-preprocess \
  --vlm-preprocess-script scripts/preprocess_roi.py
```

**Expected output**: `bench/my_data/document.jsonl` containing page data with tables and cells

---

## ⚙️ Step 3: Run Extraction Pipeline

### Basic Extraction (No Solver)
```bash
python3 scripts/assemble_eob.py \
  --pages bench/my_data \
  --mappings config/payers \
  --out out/my_extraction
```

### Full Feature Extraction (Recommended)
```bash
python3 scripts/assemble_eob.py \
  --pages bench/my_data \
  --mappings config/payers \
  --out out/my_extraction \
  --solve-roles \
  --emit-evidence \
  --budget-stability
```

### Production Mode (Strict Locks)
```bash
python3 scripts/assemble_eob.py \
  --pages bench/my_data \
  --mappings config/payers \
  --out out/my_extraction \
  --solve-roles \
  --solver-locks-strict \
  --emit-evidence \
  --summary-reconcile \
  --budget-stability
```

**Flag Guide**:
- `--solve-roles`: Enable law-based solver certification
- `--solver-locks-strict`: Lock certified rows (production mode)
- `--emit-evidence`: Generate evidence plane with swap energy
- `--summary-reconcile`: Check totals match sum of rows
- `--budget-stability`: Track performance metrics

---

## 📊 Step 4: Visualize Results

### Comprehensive Report
```bash
python3 scripts/visualize_extraction.py out/my_extraction
```

Shows:
- Document info
- Extracted services table
- Column assignments + ILP alternatives
- Evidence plane with swap energy
- Solver certification results
- Performance metrics

### Table Structure Diagram
```bash
python3 scripts/visualize_table_diagram.py out/my_extraction
```

Shows:
- ASCII art table layout
- Column-to-role mappings
- ILP alternative mappings
- Inequality checks with ✓/✗

---

## 🔍 Step 5: Interpret Your Results

### ✅ Good Quality Indicators

**High Certification Rate**:
```
Total Services: 15
Certified Rows: 12
```
→ 80% certified = good quality extraction

**High Conservation**:
```
Conservation: 95.0%
```
→ Most rows have all 4 money fields populated

**Zero Violations**:
```
Inequality Violations: 0
```
→ All certified rows pass math checks

**Rows Locked** (with --solver-locks-strict):
```
Row 1: 🔒 LOCKED
```
→ Production-ready, high confidence

### ⚠️ Quality Issues to Fix

**Low Conservation**:
```
Conservation: 30.0%
```
→ Missing money fields, check column mapping

**High Violations**:
```
Inequality Violations: 8
Violation Energy: 50000
```
→ Column assignments may be wrong

**ILP Shows Better Mapping**:
```
⚡ ILP ADVANTAGE: +5 certified rows vs chosen mapping
```
→ Consider using ILP's suggested columns

**Positive Swap Energy**:
```
Swap columns 3 ↔ 4:  + 3 certified rows
```
→ Adjacent columns may be misaligned

---

## 🛠️ Troubleshooting Common Issues

### Issue: "No tables found"
**Cause**: OCR didn't detect any tables
**Fix**:
1. Check PDF quality (scan vs digital)
2. Try --vlm-runner for better table detection
3. Inspect NDJSON: `jq '.tables' bench/my_data/*.jsonl`

### Issue: "0 certified rows"
**Cause**: Data doesn't satisfy any law
**Fix**:
1. Check conservation % (need all 4 money fields)
2. Inspect column mapping (wrong columns assigned?)
3. Look at violation energy (how close to passing?)

### Issue: "Wrong procedure codes extracted"
**Cause**: Column mapping incorrect
**Fix**:
1. Check ILP alternatives (better mapping available?)
2. Inspect typedness scores (columns confused?)
3. Verify payer mapping YAML has correct headers

### Issue: "Totals don't match"
**Cause**: Missing rows or incorrect values
**Fix**:
1. Use --summary-reconcile to see residuals
2. Check row count vs expected
3. Inspect summary_box.v1.json for deltas

---

## 📈 Batch Processing Multiple Documents

### Process Entire Directory
```bash
# 1. OCR all PDFs
for pdf in eobs/my_data/*.pdf; do
  base=$(basename "$pdf" .pdf)
  vision-ocr-cli/.build/arm64-apple-macosx/release/vision-ocr-cli \
    "$pdf" \
    --mode document \
    --ndjson "bench/batch/$base.jsonl"
done

# 2. Extract all documents
python3 scripts/assemble_eob.py \
  --pages bench/batch \
  --mappings config/payers \
  --out out/batch \
  --solve-roles \
  --solver-locks-strict \
  --emit-evidence \
  --budget-stability

# 3. Verify quality
python3 scripts/verify_checkpoint.py \
  --strict out/batch \
  --require-assign \
  --require-evidence

# 4. Visualize each document
for eob in out/batch/*.json; do
  [[ "$eob" == *".ccert."* ]] && continue
  [[ "$eob" == *".proof."* ]] && continue
  echo "=== $(basename $eob) ==="
  python3 scripts/visualize_extraction.py "$(dirname $eob)" | head -50
done
```

---

## 📊 Quality Metrics to Track

Create a simple summary:
```bash
# Count certified rows across all documents
for cert in out/batch/*.table.ccert.v1.json; do
  doc=$(basename "$cert" .table.ccert.v1.json)
  certified=$(jq -r '.certified_rows' "$cert")
  conservation=$(jq -r '.conservation' "$cert")
  echo "$doc: $certified certified, ${conservation}% conservation"
done | sort -t: -k2 -rn
```

Expected output:
```
document1: 24 certified, 100.0% conservation  ← Excellent
document2: 18 certified, 95.0% conservation   ← Very good
document3: 5 certified, 60.0% conservation    ← Needs review
document4: 0 certified, 30.0% conservation    ← Failed extraction
```

---

## 🎯 Next Steps After Visualization

### If Results Are Good (80%+ certified)
1. ✅ Enable `--solver-locks-strict` for production
2. ✅ Use locked rows for training data
3. ✅ Track metrics over time
4. ✅ Monitor performance budgets

### If Results Need Work (< 50% certified)
1. 🔧 Review column mappings (check ILP alternatives)
2. 🔧 Improve payer YAML headers
3. 🔧 Try --him-aggressive for headerless tables
4. 🔧 Check PDF quality and re-scan if needed

### If You Want to Improve Further
1. 📈 Analyze swap energy patterns
2. 📈 Add priors from locked rows (--bootstrap-priors)
3. 📈 Use semantic mapping (--semantic-map)
4. 📈 Enable multi-pass extraction (--multi-pass 2)

---

## 🎓 Example: Full Workflow with Real EOB

```bash
# 1. Start with PDF
cp eobs/tests/DD_3.pdf eobs/my_test/

# 2. OCR to NDJSON
vision-ocr-cli/.build/arm64-apple-macosx/release/vision-ocr-cli \
  eobs/my_test/DD_3.pdf \
  --mode document \
  --ndjson bench/my_test/DD_3.jsonl

# 3. Extract with all features
python3 scripts/assemble_eob.py \
  --pages bench/my_test \
  --mappings config/payers \
  --out out/my_test \
  --solve-roles \
  --solver-locks-strict \
  --emit-evidence \
  --budget-stability

# 4. Visualize results
python3 scripts/visualize_extraction.py out/my_test
python3 scripts/visualize_table_diagram.py out/my_test

# 5. Verify quality
python3 scripts/verify_checkpoint.py --strict out/my_test

# 6. Check specific metrics
jq '.certified_rows, .conservation, .row_locked_pct' \
  out/my_test/DD_3.table.ccert.v1.json
```

---

## 📚 Additional Resources

- **README.md**: Overall project documentation
- **VISUALIZATION_GUIDE.md**: Detailed guide on reading output
- **SESSION/Northstar.md**: Checkpoint architecture details
- **schemas/**: JSON schema definitions for all outputs
- **config/payers/**: Header mapping YAML templates

---

## 💡 Pro Tips

1. **Start small**: Test with 1-2 documents first
2. **Iterate**: Use visualization to identify issues, fix, re-run
3. **Track metrics**: Conservation % and certified rows are key indicators
4. **Compare alternatives**: ILP may find better mappings than extraction
5. **Use strict locks**: Only in production after validating quality
6. **Monitor performance**: P95 times should stay under 1s per page

Happy processing! 🚀
