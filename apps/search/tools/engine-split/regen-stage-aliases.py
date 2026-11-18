#!/usr/bin/env python3
"""regen-stage-aliases.py: rewrite apps/search/serve/api/stage_aliases.py so every stage keeps the version of the engine it is declared identical to.
Run from the repo root after ANY edit to a file a stage reaches (search_api.py, stage_*.py, lookups_release.py, sem_ivf.py, warmplan.py, parts.py ...),
with /tmp/pv/r2.json = part-versions.py of that earlier engine (git archive <commit> apps/search/serve/api). Only for changes proven answer-identical
(same 2,036 names, golden, evals: docs/internal/warmup-plan.md "Proof"); a real change to answers must move the version, so do not run it for those."""
import json,re,subprocess,sys
api="apps/search/serve/api"
out=subprocess.run(["/home/owner/data/pyt/bin/python","apps/search/tools/engine-split/part-versions.py",api],capture_output=True,text=True).stdout
b=json.loads(out); a=json.load(open("/tmp/pv/r2.json"))
# real versions: parts.info exposes "real"; part-versions prints version only, so recompute without aliases
import os
os.rename(api+"/stage_aliases.py",api+"/stage_aliases.py.off")
open(api+"/stage_aliases.py","w").write("ALIASES: dict = {}\n")
raw=json.loads(subprocess.run(["/home/owner/data/pyt/bin/python","apps/search/tools/engine-split/part-versions.py",api],capture_output=True,text=True).stdout)
os.remove(api+"/stage_aliases.py"); os.rename(api+"/stage_aliases.py.off",api+"/stage_aliases.py")
s=open(api+"/stage_aliases.py").read()
body="ALIASES: dict = {\n"+"".join(f'    "{k}": {{"{raw[k]["version"]}": "{a[k]["version"]}"}},\n' for k in a if raw[k]["version"]!=a[k]["version"])+"}\n"
s=re.sub(r"ALIASES: dict = \{.*?\n\}\n",body,s,flags=re.S)
open(api+"/stage_aliases.py","w").write(s)
fin=json.loads(subprocess.run(["/home/owner/data/pyt/bin/python","apps/search/tools/engine-split/part-versions.py",api],capture_output=True,text=True).stdout)
print({k:fin[k]["version"]==a[k]["version"] for k in a})
