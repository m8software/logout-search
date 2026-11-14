#!/usr/bin/env bash
# shellcheck disable=SC2154  # tmp, PY, REPO_ROOT, SCRIPT_DIR_T come from the sourcing test
# Sourced by test-search-assemble.sh and test-search-install.sh: the synthetic release fx-1 (and fx-2), assembled and signed with a TEST key.
# Needs: PY (tantivy + numpy), REPO_ROOT, SCRIPT_DIR_T, tmp.  Sets: FX R1 R2 P1 P2 (parts dirs) REL1 REL2 PUB1 PUB2 KEYA KEYB PUBA PUBB ASM
LIBD="$SCRIPT_DIR_T/lib"
REL="$REPO_ROOT/apps/search/release"
INST="$REPO_ROOT/apps/search/installer/install.py"
ASM=("$PY" "$REL/assemble.py")
FX="$tmp/fx"
asm_setup() {
    "$PY" "$LIBD/search_update_fixture.py" build "$FX" --wiki 1500 --months 1 --seed 3 --frac 0.04 >/dev/null 2>&1 || return 1
    R1="$FX/r1"; R2="$FX/r2"; P1="$tmp/p1"; P2="$tmp/p2"
    mapfile -t ARGS1 < <("$PY" "$LIBD/search_assemble_parts.py" "$P1" "$R1" fx-1)
    mapfile -t ARGS2 < <("$PY" "$LIBD/search_assemble_parts.py" "$P2" "$R2" fx-2)
    KEYA="$tmp/test-a"; KEYB="$tmp/test-b"
    "${ASM[@]}" test-keygen "$KEYA" >/dev/null && "${ASM[@]}" test-keygen "$KEYB" >/dev/null || return 1
    REL1="$tmp/rel1"; REL2="$tmp/rel2"; PUB1="$tmp/pub1"; PUB2="$tmp/pub2"
    LIC="$REPO_ROOT/apps/search/serve/api/licenses/Apache-2.0.txt"
    "${ASM[@]}" build --release-id fx-1 --out "$REL1" --publish "$PUB1" --licence-text "$LIC" "${ARGS1[@]}" >/dev/null || return 1
    "${ASM[@]}" build --release-id fx-2 --out "$REL2" --publish "$PUB2" --licence-text "$LIC" "${ARGS2[@]}" >/dev/null || return 1
    "${ASM[@]}" sign "$REL1" "$PUB1" --key "$KEYA.key" >/dev/null && "${ASM[@]}" sign "$REL2" "$PUB2" --key "$KEYA.key" >/dev/null || return 1
}
