#!/usr/bin/env python3
"""part-versions.py API_DIR -> JSON {stage: {version, env, ctx}} for the engine tree at API_DIR (uses that tree's own parts.py / stage_version.py)."""
import json, os, re, sys
api = os.path.abspath(sys.argv[1]); sys.path.insert(0, api)
import parts as P
src = open(os.path.join(api, "search_api.py"), encoding="utf-8").read()
m = re.search(r"PARTS_SPEED_ONLY = .*?\nPART_STAGES = \{.*?\n\}\n", src, re.S)
ns = {"base_runtime": lambda: {}}
exec(m.group(0), ns)
stgs = [P.Stage(n, **c) for n, c in ns["PART_STAGES"].items()]
real = P.Parts(None, os.path.join(api, "search_api.py"), stgs, {})
print(json.dumps({n: {k: real.info(n)[k] for k in ("version", "env", "ctx")} for n in ns["PART_STAGES"]}, indent=1, sort_keys=True))
