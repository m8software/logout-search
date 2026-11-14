# shellcheck shell=bash
# Sourced by the pipeline's shell helpers. A helper may create and remove files only inside the stage output folder the
# runner gave it: `require_outdir PATH` exits 2 unless PATH is absolute, exists, and is (or is under) a
# WORK/stages/NAME/<key>.partial folder. Nothing recursive runs on a path that did not pass this.
require_outdir() {
  local p="${1:-}"
  [ -n "$p" ] && [ "${p:0:1}" = "/" ] && [ -d "$p" ] || { echo "refusing: output folder '$p' is not an existing absolute path" >&2; exit 2; }
  case "$(realpath "$p")" in
    */stages/*/*.partial|*/stages/*/*.partial/*) ;;
    *) echo "refusing: '$p' is not a pipeline stage output folder (WORK/stages/NAME/KEY.partial)" >&2; exit 2 ;;
  esac
}
