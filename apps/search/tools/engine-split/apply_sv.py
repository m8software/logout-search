import sys
p=sys.argv[1]+"/stage_version.py"
s=open(p).read()
if "FRAGMENTS" in s: sys.exit("already")
a=s.index("class Module:"); b=s.index("    def roots_of")
new='''# The stage modules search_api.py's private code was moved into. They are one namespace with search_api.py: the names
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

'''
s=s[:a]+new+s[b:]
s=s.replace("ast.get_source_segment(self.src, node)","ast.get_source_segment(self.text.get(id(node), self.src), node)")
open(p,"w").write(s)
