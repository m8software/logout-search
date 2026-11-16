#!/usr/bin/env python3
"""split_tool.py API_DIR — move each stage's private code out of search_api.py into stage_*.py modules.
A def is private to a module when only that module's roots reach it (not another module's, not the HTTP glue). The moved
source is byte-identical (comments above it travel with it); search_api.py imports the moved names back at its end, and each
stage module imports what it needs from search_api. Run on a copy; prints a report."""
import ast, builtins, collections, os, sys, re
api = sys.argv[1]
only = sys.argv[2].split(",") if len(sys.argv) > 2 else None
sys.path.insert(0, api)
import stage_version as sv

path = os.path.join(api, "search_api.py")
src = open(path, encoding="utf-8").read()
lines = src.split("\n")
tree = ast.parse(src)

MODULES = {          # module -> explicit root names (entry points that belong to it)
    "stage_parts": ["part_run", "note_part", "part_stamp", "base_runtime", "PartStale", "_pcheck", "_part_keys", "_parts_off", "_parts_state",
                    "_parts_lock", "PARTS_ON", "PART_STAGES", "PARTS_SPEED_ONLY", "PARTS_CLOCK_OK", "_warm_q"],
    "stage_base": ["base_part", "_search_retry"],
    "stage_card": ["card_pre", "card_build", "card_stage"],
    "stage_list": ["list_build", "list_stage"],
    "stage_place": ["place_build", "place_stage"],
    "stage_verdict": ["verdict_build", "verdict_stage", "conf_of", "intent_kinds"],
    "stage_local": ["local_build", "local_part"],
    "stage_assembly": ["finished_answer", "glue_build"],
}
STAY = {"parts_for"}          # reads __file__ / globals(): must live in search_api.py

# ---- top-level nodes
class N:
    pass
nodes = []
for i, node in enumerate(tree.body):
    n = N(); n.i = i; n.node = node
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    n.start = start; n.end = node.end_lineno
    # leading comment block directly above
    s = start
    while s - 2 >= 0 and lines[s - 2].lstrip().startswith("#"):
        s -= 1
    n.cstart = s
    n.names = sv._bound(node)
    nodes.append(n)
# bindings inside compound top-level statements (If/Try/With/For) count as module-level names, never moved
compound_names = set()
for n in nodes:
    if isinstance(n.node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
        for x in ast.walk(n.node):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                compound_names.add(x.id)
            if isinstance(x, (ast.Import, ast.ImportFrom)):
                for a in x.names:
                    compound_names.add((a.asname or a.name).split(".")[0])
            if isinstance(x, (ast.FunctionDef, ast.ClassDef)):
                compound_names.add(x.name)
bycount = collections.Counter(nm for n in nodes for nm in n.names)
multi = {nm for nm, c in bycount.items() if c > 1}
owner = {}
for n in nodes:
    for nm in n.names:
        owner[nm] = n
imports = {}       # name -> import statement text (top-level direct imports)
for n in nodes:
    if isinstance(n.node, ast.Import):
        for a in n.node.names:
            imports[(a.asname or a.name).split(".")[0]] = ("import " + a.name + (" as " + a.asname if a.asname else ""))
    elif isinstance(n.node, ast.ImportFrom):
        mod = ("." * n.node.level) + (n.node.module or "")
        for a in n.node.names:
            imports[a.asname or a.name] = f"from {mod} import {a.name}" + (f" as {a.asname}" if a.asname else "")
modnames = set(owner) | set(imports) | compound_names
rebound = set()
for x in ast.walk(tree):
    if isinstance(x, ast.Global):
        rebound |= set(x.names)

# names other engine modules reach into search_api for (attributes or getattr strings): a REBOUND global read that way must
# stay where it is (a re-exported copy would stay None forever)
external = set()
for f in os.listdir(api):
    if f.endswith(".py") and f not in ("search_api.py", "stage_version.py", "parts.py") and not f.startswith("stage_"):
        try:
            t = ast.parse(open(os.path.join(api, f), encoding="utf-8").read())
        except SyntaxError:
            continue
        for x in ast.walk(t):
            if isinstance(x, ast.Attribute): external.add(x.attr)
            elif isinstance(x, ast.Constant) and isinstance(x.value, str) and len(x.value) < 60: external.add(x.value)

def reads(node):
    return sv._names_read(node)

def eager(node):
    out = set()
    def visit(x):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in x.decorator_list: visit(d)
            for d in x.args.defaults + [k for k in x.args.kw_defaults if k]: visit(d)
            return
        if isinstance(x, ast.Lambda):
            for d in x.args.defaults + [k for k in x.args.kw_defaults if k]: visit(d)
            return
        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
            out.add(x.id)
        for c in ast.iter_child_nodes(x):
            visit(c)
    visit(node)
    return out

# ---- reach with module stops
m = sv.Module(path)
rootsets = {k: set(v) for k, v in MODULES.items()}
allroots = set().union(*rootsets.values())
glue_roots = ["Handler"]
for n in nodes:                          # non-binding statements run at import: they are glue
    if not n.names and not isinstance(n.node, (ast.Import, ast.ImportFrom)):
        glue_roots += [x for x in reads(n.node) if x in m.defs]
glue = m.reach(glue_roots, stop=allroots)
reach = {k: m.reach(sorted(v), stop=allroots - v) for k, v in rootsets.items()}
sig = {}
for nm in m.defs:
    s = tuple(sorted(k for k, r in reach.items() if nm in r))
    if nm in glue: s += ("GLUE",)
    sig[nm] = s

move = collections.defaultdict(set)      # module -> node set
for k in MODULES:
    if only and k not in only: continue
    for nm in m.defs:
        if sig[nm] == (k,) and nm not in STAY and nm not in multi and nm in owner:
            move[k].add(owner[nm].i)
    for nm in MODULES[k]:
        if nm in owner and nm not in multi and nm not in STAY:
            move[k].add(owner[nm].i)
moved_to = {}
for k, s in move.items():
    for i in s: moved_to[i] = k

def blocked(n):
    """why a node cannot move, or None"""
    r = reads(n.node)
    if r & {"__file__", "__name__", "globals", "locals", "vars"}:
        return "reads __file__/__name__/globals()"
    for nm in n.names:
        if nm in rebound and nm in external:
            return "a rebound global other modules read through search_api"
    return None

changed = True
while changed:
    changed = False
    # 1. nodes that cannot move
    for i in list(moved_to):
        n = nodes[i]
        why = blocked(n)
        if why:
            del moved_to[i]; changed = True; continue
    moved_names = {nm for i in moved_to for nm in nodes[i].names}
    # 2. remaining nodes that read moved names at import time
    for n in nodes:
        if n.i in moved_to: continue
        e = eager(n.node) & moved_names
        for nm in e:
            j = owner[nm].i
            if j in moved_to:
                del moved_to[j]; changed = True
    # 2b. a node that declares `global X` moves only together with X's binding
    for i in list(moved_to):
        n = nodes[i]
        for x in ast.walk(n.node):
            if isinstance(x, ast.Global):
                for g in x.names:
                    if g in owner and owner[g].i not in moved_to:
                        del moved_to[i]; changed = True; break
                else:
                    continue
                break
    # 2c. a rebound global moved away while something that stays reads it
    moved_names = {nm for i in moved_to for nm in nodes[i].names}
    for n in nodes:
        if n.i in moved_to: continue
        for nm in (reads(n.node) & rebound & moved_names):
            j = owner[nm].i
            if j in moved_to:
                del moved_to[j]; changed = True
    # 3. moved nodes reading a rebound global or another module's moved name eagerly / rebound not moved with it
    for i in list(moved_to):
        n = nodes[i]; k = moved_to[i]
        for nm in reads(n.node) & modnames:
            if nm in rebound and nm in owner and owner[nm].i not in moved_to:
                del moved_to[i]; changed = True; break
            if nm in owner and owner[nm].i in moved_to and moved_to[owner[nm].i] != k:
                # reads a name moved to ANOTHER module: lazy reads are fine (resolved through search_api's re-export); eager are not
                if nm in eager(n.node):
                    del moved_to[i]; changed = True; break
        else:
            continue

mods = collections.defaultdict(list)
for i, k in moved_to.items(): mods[k].append(i)
report = []
for k in MODULES:
    idx = sorted(mods.get(k, []))
    nl = sum(nodes[i].end - nodes[i].cstart + 1 for i in idx)
    report.append((k, len(idx), nl))
for r in report: print("%-16s %4d nodes %5d lines" % r)
print("total moved lines", sum(r[2] for r in report), "of", len(lines))

# ---- write modules
moved_name_module = {nm: moved_to[i] for i in moved_to for nm in nodes[i].names}
os.makedirs(os.path.join(api), exist_ok=True)
for k, idx in mods.items():
    idx.sort()
    body_names = {nm for i in idx for nm in nodes[i].names}
    free = set()
    for i in idx:
        free |= reads(nodes[i].node)
    free -= body_names
    free &= modnames
    imp_lines, from_names, cross = [], [], collections.defaultdict(list)
    for nm in sorted(free):
        if nm in moved_name_module and moved_name_module[nm] != k:
            cross[moved_name_module[nm]].append(nm)          # another stage module's name: through search_api's re-export
            from_names.append(nm)
        elif nm in imports and nm not in compound_names:
            imp_lines.append(imports[nm])
        elif nm in compound_names or nm in owner:
            from_names.append(nm)
    # the original from __future__ import annotations travels with the code
    out = ["\"\"\"%s: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from\nsearch_api (imported at the end of that file's definitions) and the stdlib.\"\"\"" % k, "from __future__ import annotations", ""]
    seen = set()
    for l in imp_lines:
        if l not in seen and "__future__" not in l:
            seen.add(l); out.append(l)
    if from_names:
        out.append("from search_api import (")
        for nm in sorted(set(from_names)):
            out.append(f"    {nm},")
        out.append(")")
    out.append("")
    for i in idx:
        n = nodes[i]
        out.append("")
        out.extend(lines[n.cstart - 1:n.end])
    out.append("")
    open(os.path.join(api, k + ".py"), "w", encoding="utf-8").write("\n".join(out))

# ---- rewrite search_api.py without the moved nodes, importing them back at the end
cut = set()
for i in moved_to:
    n = nodes[i]
    j = n.end
    while j < len(lines) and lines[j].strip() == "":          # the blank lines after it go too: what is above it keeps its spacing
        j += 1
    cut.update(range(n.cstart - 1, j))
text = "\n".join(l for j, l in enumerate(lines) if j not in cut)
have_alias = 'sys.modules.setdefault("search_api"' in text
imp = ["", "", "# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so",
       "# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.",
       ]
if not have_alias:
    imp += ["# Run as `python search_api.py` this module is __main__: register it under its own name, so `from search_api import ...` in the stage",
            "# modules finds this module and not a second copy of it.",
            "if __name__ == \"__main__\":",
            "    sys.modules.setdefault(\"search_api\", sys.modules[\"__main__\"])", ""]
for k in MODULES:
    names = sorted(nm for nm, mk in moved_name_module.items() if mk == k)
    if not names: continue
    imp.append(f"from {k} import (")
    for nm in names:
        imp.append(f"    {nm},")
    imp.append(")")
imp.append("")
# insert before `if __name__ == "__main__":`
marker = "\nif __name__ == \"__main__\":"
assert marker in text
text = text.replace(marker, "\n".join(imp) + marker, 1)
# the module must be importable under its own name when run as a script
alias = "\n# Run as `python search_api.py`, this module is __main__: register it under its own name so the stage modules' `from search_api import ...` finds it.\nif __name__ == \"__main__\":\n    sys.modules.setdefault(\"search_api\", sys.modules[\"__main__\"])\n"
text = text.replace("\nfrom __future__ import annotations\n", "\nfrom __future__ import annotations\n", 1)
open(path, "w", encoding="utf-8").write(text)
print("search_api.py now", len(text.split("\n")), "lines")
