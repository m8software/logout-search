# engine-split tooling

Moves the private code of each stage out of `serve/api/search_api.py` into `stage_*.py`, byte for byte, and proves nothing
else changed. Re-run it on a new base instead of rebasing the moves: the result is the same function of the source.

- `split_tool.py API_DIR MODULE` moves one module's private code (code only that stage reaches, plus its entry points).
  Modules in order: stage_parts, stage_base, stage_card, stage_list, stage_place, stage_verdict, stage_local, stage_assembly.
- `step.sh MODULE` runs the tool, adds the module to the copy lists (Dockerfile, Dockerfile.rtc, dev-engine.sh, installer
  bundle) and checks: compiles; no undefined name; **every stage's part version, settings and ContextVars equal the baseline**
  (`part-versions.py`, compare with `base-versions.json` made from the tree before the move); the module's namespace is identical
  (`dircmp.py`); the public-repo scrub rules still match (`scrubcheck.py`); every removed line is in a stage module.
- `apply_sv.py API_DIR` patches stage_version.py to read search_api.py with its stage modules as one namespace (already in master of
  the branch as a commit; kept for a tree that lacks it).
