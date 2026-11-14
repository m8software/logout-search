#!/usr/bin/env bash
# Run every unit test in tests/unit/, one after another; exit 1 if any fails.
# A test that prints SKIP lacked something it needs: listed at the end, not a failure.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

failed=() skipped=() passed=0
for t in tests/unit/test-search-*.sh; do
    name="$(basename "$t" .sh)"
    if bash "$t" >"$log" 2>&1; then
        if grep -qE '^\s*SKIP' "$log"; then
            skipped+=("${name}: $(grep -m1 -E '^\s*SKIP' "$log" | sed 's/^\s*//')")
        else
            passed=$((passed + 1))
        fi
    else
        failed+=("$name")
        echo "===== FAILED: ${name} (last 40 lines) ====="
        tail -40 "$log"
    fi
done

echo "passed: ${passed}   skipped: ${#skipped[@]}   failed: ${#failed[@]}"
for s in "${skipped[@]}"; do echo "  skipped  ${s}"; done
for f in "${failed[@]}"; do echo "  FAILED   ${f}"; done
[[ "${#failed[@]}" -eq 0 ]]
