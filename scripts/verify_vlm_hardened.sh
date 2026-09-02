#\!/bin/bash
# Verification script for hardened VLM runner outputs

VLM_DIR="${1:-runs/vlm_hardened}"

echo "=== VLM Hardened Runner Verification ==="
echo "Directory: $VLM_DIR"
echo ""

# 1. Check error logging
echo "1. Error logging check:"
if [ -d "$VLM_DIR" ]; then
    ERROR_COUNT=$(find "$VLM_DIR" -name "*.raw.txt" -o -name "*.reason.json" 2>/dev/null | wc -l)
    echo "   Error files found: $ERROR_COUNT"
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo "   Sample reasons:"
        find "$VLM_DIR" -name "*.reason.json" 2>/dev/null | head -3 | xargs -I {} sh -c 'echo "   - $(basename {}): $(jq -r .reason {})"'
    fi
else
    echo "   ❌ Directory not found"
    exit 1
fi
echo ""

# 2. Check for populated cells
echo "2. Populated cells check:"
TOTAL_CELLS=0
NON_EMPTY_CELLS=0
for f in "$VLM_DIR"/*_data_response.json; do
    [ -f "$f" ] || continue
    STATUS=$(jq -r '.status' "$f")
    if [ "$STATUS" = "ok" ]; then
        CELLS=$(jq -r '.cells | length' "$f")
        NON_EMPTY=$(jq -r '.cells | map(select(. \!= "")) | length' "$f")
        TOTAL_CELLS=$((TOTAL_CELLS + CELLS))
        NON_EMPTY_CELLS=$((NON_EMPTY_CELLS + NON_EMPTY))
    fi
done
echo "   Total cells: $TOTAL_CELLS"
echo "   Non-empty cells: $NON_EMPTY_CELLS"
if [ "$NON_EMPTY_CELLS" -gt 0 ]; then
    RATE=$(awk "BEGIN {printf \"%.2f\", $NON_EMPTY_CELLS/$TOTAL_CELLS}")
    echo "   Fill rate: $RATE"
    if (( $(echo "$RATE > 0.5" | bc -l) )); then
        echo "   ✅ PASS: Cells populated"
    else
        echo "   ⚠️  WARN: Low fill rate"
    fi
else
    echo "   ❌ FAIL: All cells empty"
fi
echo ""

# 3. Schema validity
echo "3. Schema validity check:"
VALID_COUNT=0
INVALID_COUNT=0
for f in "$VLM_DIR"/*_data_response.json; do
    [ -f "$f" ] || continue
    STATUS=$(jq -r '.status' "$f")
    if [ "$STATUS" = "ok" ]; then
        VALID_COUNT=$((VALID_COUNT + 1))
    else
        INVALID_COUNT=$((INVALID_COUNT + 1))
    fi
done
echo "   Valid (status=ok): $VALID_COUNT"
echo "   Invalid: $INVALID_COUNT"
echo ""

# 4. Sample output
echo "4. Sample populated response:"
for f in "$VLM_DIR"/*_data_response.json; do
    [ -f "$f" ] || continue
    NON_EMPTY=$(jq -r '.cells | map(select(. \!= "")) | length' "$f")
    if [ "$NON_EMPTY" -gt 0 ]; then
        echo "   File: $(basename $f)"
        jq '{status, rows, cols, first_3_non_empty: [.cells[] | select(. \!= "")] | .[0:3]}' "$f"
        break
    fi
done
echo ""

echo "=== Summary ==="
if [ "$NON_EMPTY_CELLS" -gt 0 ] && [ "$VALID_COUNT" -gt 0 ]; then
    echo "✅ Hardened runner producing populated cells - ready for Test 8 re-run"
else
    echo "❌ Still seeing empty cells - consider:"
    echo "   - VLM_MODEL=mlx-community/Qwen2-VL-7B-bf16"
    echo "   - VLM_MAX_NEW_TOKENS=1536"
    echo "   - Check $VLM_DIR/*.reason.json for model failure patterns"
fi
