import sys, types, importlib
api=sys.argv[1]; sys.path.insert(0,api)
for name in ("psycopg","psycopg_pool","numpy","onnxruntime","tokenizers","aiortc"):
    try: __import__(name)
    except Exception: sys.modules[name]=types.ModuleType(name)
import search_api
print("\n".join(sorted(n for n in dir(search_api))))
