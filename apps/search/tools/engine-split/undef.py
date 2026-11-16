import ast,builtins,sys,os,glob
api=sys.argv[1]
bad=0
for f in sorted(glob.glob(os.path.join(api,"search_api.py"))+glob.glob(os.path.join(api,"stage_[!v]*.py"))):
    t=ast.parse(open(f).read()); mod=set(dir(builtins))|{"__file__","__name__"}
    def bindnames(n):
        out=set()
        for x in ast.walk(n):
            if isinstance(x,ast.Name) and isinstance(x.ctx,(ast.Store,ast.Del)): out.add(x.id)
            elif isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): out.add(x.name)
            elif isinstance(x,(ast.Import,ast.ImportFrom)):
                for a in x.names: out.add((a.asname or a.name).split(".")[0])
            elif isinstance(x,ast.ExceptHandler) and x.name: out.add(x.name)
            elif isinstance(x,ast.arg): out.add(x.arg)
            elif isinstance(x,ast.Global): out|=set(x.names)
        return out
    for n in t.body: mod|=bindnames(n) if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) else {n.name}
    for n in t.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            loc=bindnames(n)
            for x in ast.walk(n):
                if isinstance(x,ast.Name) and isinstance(x.ctx,ast.Load) and x.id not in loc and x.id not in mod:
                    print(os.path.basename(f),n.name,"undefined:",x.id); bad+=1
print("undefined:",bad)
