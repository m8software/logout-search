#!/usr/bin/env bash
# Stops OUR scoring processes inside WSL, by process id, each only after its command line and folder are checked:
# a python of our venv running curated-score.py with its working directory under /mnt/c/logout-stored/.
for p in /proc/[0-9]*; do
    c=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$c" in
        */data/release/wse/venv/bin/python*curated-score.py*)
            d=$(readlink "$p/cwd" 2>/dev/null)
            case "$d" in /mnt/c/logout-stored/*) kill "${p#/proc/}" 2>/dev/null && echo "stopped ${p#/proc/}" ;; esac ;;
    esac
done
# the answer-builder helper (build-helper.sh), when this machine is lending its processor: its container, by name
command -v docker >/dev/null 2>&1 && docker rm -f search-build-helper >/dev/null 2>&1 && echo "stopped search-build-helper"
exit 0
