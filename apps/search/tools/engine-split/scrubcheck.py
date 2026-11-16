import ast,sys,os
R=os.environ.get("REPO") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../.."))
src=open(R+"/apps/search/tools/public-repo/scrub.py").read()
t=ast.parse(src)
rules=None
for n in t.body:
    if isinstance(n,ast.Assign) and any(getattr(x,"id",None)=="LINES" for x in n.targets):
        rules=ast.literal_eval(n.value)
bad=0
for path,old,new in rules:
    p=R+"/"+path
    base=os.path.dirname(p)
    files=[p]
    if path.endswith("serve/api/search_api.py"):
        files+= [base+"/"+f for f in os.listdir(base) if f.startswith("stage_") and f!="stage_version.py"]
    if not any(os.path.exists(f) and old in open(f,encoding="utf-8").read() for f in files):
        print("scrub rule text no longer found:",path,old[:60].replace("\n"," ")); bad+=1
sys.exit(1 if bad else 0)
