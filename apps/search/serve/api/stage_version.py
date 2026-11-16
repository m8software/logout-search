"""A stage's code version, computed from the source it can reach (phase 2, 2026-11-14).

Phase 2 keys each part of a stored answer by its stage's code version, so a deploy redoes only the parts whose code
changed. search_api.py is one 10,000-line file; before (and instead of) moving each stage into its own module, the
version of a stage is the sha256 of every top-level definition the stage's entry functions can reach in that file:
the functions and classes they call, and the module-level names (constants, settings read from the environment) they
read, followed transitively. A change anywhere else in the file leaves the stage's version alone; a change to anything
it can reach changes it.

What it cannot see, and the guards for it:
  * calls through a variable or attribute that holds a function chosen at run time (a scorer passed in): the stage
    lists such entry points itself (extra roots);
  * other modules: their whole file is hashed when a reached name is imported from them (imports=True);
  * the environment: settings are module-level names read once at import, so reaching the name reaches its os.environ
    line; a stage that reads os.environ inside a function is reported by unseen_env() and must declare it.
Pure: reads source text only, imports nothing it hashes."""
from __future__ import annotations

import ast
import hashlib
import os


# The stage modules search_api.py's private code was moved into. They are one namespace with search_api.py: the names
# they import from it are not "other modules", and a stage's version is computed over the definitions of all of them, so
# moving a definition from one file to another does not change any version.
FRAGMENTS = ("stage_parts", "stage_base", "stage_card", "stage_list", "stage_place", "stage_verdict", "stage_local", "stage_assembly")


class Module:
    def __init__(self, path: str, fragments=None):
        self.path = path
        here = os.path.dirname(os.path.abspath(path))
        if fragments is None:
            fragments = [os.path.join(here, f + ".py") for f in FRAGMENTS if os.path.basename(path) == "search_api.py"
                         and os.path.exists(os.path.join(here, f + ".py"))]
        self.files = [path] + list(fragments)
        own = ({"search_api"} | set(FRAGMENTS)) if len(self.files) > 1 else set()
        self.src = open(path, encoding="utf-8").read()
        self.defs: dict[str, list[ast.AST]] = {}          # top-level name -> the statements that bind it
        self.imports: dict[str, str] = {}                  # top-level imported name -> module name
        self.text: dict[int, str] = {}                     # id(statement) -> the source text of the file it is in
        self.tree = None
        for f in self.files:
            text = self.src if f == path else open(f, encoding="utf-8").read()
            tree = ast.parse(text)
            if f == path:
                self.tree = tree
            for node in tree.body:
                self.text[id(node)] = text
                for name in _bound(node):
                    self.defs.setdefault(name, []).append(node)
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module in own:
                        continue                       # the same namespace: the name is defined in one of the files
                    for a in node.names:
                        self.imports[a.asname or a.name] = node.module
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        self.imports[(a.asname or a.name).split(".")[0]] = a.name

    def roots_of(self, cls: str, method: str) -> list[str]:
        """The top-level names a method of a top-level class reads (its body only): the entry points of a stage that is
        a request handler's own code."""
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method:
                        return sorted(n for n in _names_read(m) if n in self.defs or n in self.imports)
        raise KeyError(f"{cls}.{method}")

    def reach(self, roots, stop=()) -> set[str]:
        """Every top-level name reachable from roots (names read anywhere inside their statements). Names in stop are
        not entered and not counted: another stage's code, covered by that stage's own key."""
        if stop == "*":                                   # shallow: only the roots' own source, nothing they call
            return {r for r in roots if r in self.defs}
        stop = set(stop)
        seen, todo = set(), [r for r in roots if (r in self.defs or r in self.imports) and r not in stop]
        while todo:
            n = todo.pop()
            if n in seen or n in stop:
                continue
            seen.add(n)
            for node in self.defs.get(n, ()):
                for m in _names_read(node):
                    if m not in seen and (m in self.defs or m in self.imports):
                        todo.append(m)
        return seen

    def version(self, roots, settings=(), imports: bool = True, stop=()) -> str:
        """sha256 (16 hex) of the source of everything roots reach, plus the declared settings' names, plus the files of
        local modules a reached name is imported from."""
        names = self.reach(roots, stop)
        h = hashlib.sha256()
        for n in sorted(names):
            for node in self.defs.get(n, ()):
                h.update(n.encode() + b"\x00" + (ast.get_source_segment(self.text.get(id(node), self.src), node) or "").encode() + b"\x00")
        for s in sorted(settings):
            h.update(b"setting\x00" + s.encode() + b"\x00")
        if imports:
            here = os.path.dirname(os.path.abspath(self.path))
            # modules imported inside the bodies of reached definitions count too (audit DB-06: 34 functions of search_api.py
            # import facts, site_pages, product_table, lookups_release ... in their bodies; editing them changed no stage's version)
            inner = {m for n in names for node in self.defs.get(n, ()) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     for m in _local_imports(node)}
            for mod in sorted({self.imports[n] for n in names if n in self.imports} | inner):
                p = os.path.join(here, mod.split(".")[0] + ".py")
                if os.path.exists(p):
                    h.update(b"module\x00" + mod.encode() + b"\x00" + open(p, "rb").read())
        return h.hexdigest()[:16]

    def env_reads(self, roots, stop=()) -> list[str]:
        """Names of the environment variables the reached statements read (os.environ.get("X"), os.environ["X"],
        os.getenv("X"), "X" in os.environ), module-level and call-time alike: the settings the stage depends on."""
        out = set()
        for n in self.reach(roots, stop):
            for node in self.defs.get(n, ()):
                for x in ast.walk(node):
                    if isinstance(x, ast.Call) and x.args and isinstance(x.args[0], ast.Constant) \
                            and isinstance(x.args[0].value, str) and isinstance(x.func, ast.Attribute) \
                            and ((x.func.attr in ("get", "setdefault", "pop") and _is_environ(x.func.value))
                                 or (x.func.attr == "getenv" and isinstance(x.func.value, ast.Name) and x.func.value.id == "os")):
                        out.add(x.args[0].value)
                    elif isinstance(x, ast.Subscript) and _is_environ(x.value) and isinstance(x.slice, ast.Constant) \
                            and isinstance(x.slice.value, str):
                        out.add(x.slice.value)
                    elif isinstance(x, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in x.ops) \
                            and isinstance(x.left, ast.Constant) and isinstance(x.left.value, str) \
                            and any(_is_environ(c) for c in x.comparators):
                        out.add(x.left.value)
        return sorted(out)

    def contextvars_read(self, roots, stop=()) -> list[str]:
        """Reached module-level names bound to a ContextVar: per-request state the stage reads. Their values at call
        time belong in the key (a setting changed per request, ?cg= and the like)."""
        out = []
        for n in sorted(self.reach(roots, stop)):
            for node in self.defs.get(n, ()):
                v = getattr(node, "value", None)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(v, ast.Call) and (
                        (isinstance(v.func, ast.Attribute) and v.func.attr == "ContextVar")
                        or (isinstance(v.func, ast.Name) and v.func.id == "ContextVar")):
                    out.append(n)
        return out

    def clock_reads(self, roots, stop=()) -> list[str]:
        """Reached definitions that read the clock or a random source (datetime.now, time.time, random.*): a stage that
        does is not a function of its inputs and must say why (allowed)."""
        out = []
        for n in sorted(self.reach(roots, stop)):
            for node in self.defs.get(n, ()):
                for x in ast.walk(node):
                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and (
                            (x.value.id in ("time", "dt", "datetime") and x.attr in ("time", "now", "utcnow", "today", "monotonic"))
                            or x.value.id == "random"):
                        out.append(n)
                        break
        return out

    def unseen_env(self, roots, stop=()) -> list[str]:
        """Functions reached from roots that read os.environ themselves (a setting read at call time, not at import)."""
        out = []
        for n in sorted(self.reach(roots, stop)):
            for node in self.defs.get(n, ()):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        isinstance(x, ast.Attribute) and x.attr == "environ" for x in ast.walk(node)):
                    out.append(n)
        return out


def _is_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name)
            and node.value.id == "os") or (isinstance(node, ast.Name) and node.id == "environ")


def _bound(node) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        out = []
        for t in targets:
            for x in ast.walk(t):
                if isinstance(x, ast.Name):
                    out.append(x.id)
        return out
    # a name bound inside a top-level if / try / with (a setting chosen by a condition, an optional import's fallback) is a
    # definition too (audit DB-06: 13 such blocks, 2 names, were invisible to a stage's version)
    if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__":
        return []
    if isinstance(node, (ast.If, ast.Try, ast.With)):
        kids = list(node.body) + list(getattr(node, "orelse", [])) + list(getattr(node, "finalbody", []))
        for h in getattr(node, "handlers", []):
            kids += h.body
        out = []
        for k in kids:
            out += _bound(k)
        return out
    return []


def _local_imports(node) -> set[str]:
    """Modules imported inside a definition's body (import x / from x import y at function level)."""
    out = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Import):
            out.update(a.name for a in x.names)
        elif isinstance(x, ast.ImportFrom) and x.module and not x.level:
            out.add(x.module)
    return out


def _names_read(node) -> set[str]:
    out = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            out.add(x.id)
        elif isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name):
            out.add(x.value.id)
    return out
