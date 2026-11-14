#!/usr/bin/env python3
"""Build the one-folder bundle of logout search for the system this runs on (gate T7): `logout-search`
(installer/logout_search.py) with Python, the engine (serve/api), install.py, the update tools and the libraries inside.

    python build.py [--out DIR] [--no-smoke]

Needs the packages in requirements.txt (PyInstaller and what the engine imports), the same versions the engine image pins.
Writes DIR/logout-search-<system>-<arch>/ (the folder people unpack), DIR/logout-search-<system>-<arch>.{tar.gz|zip} and
DIR/SHA256SUMS, then runs the built program: `version`, `engine --check-imports` (every library loads), and the dry runs of
`autostart` and `host register` against a fake install. A bundle that fails any of them is not kept.

Systems: Linux x86_64, Windows x86_64, macOS arm64 (onnxruntime 1.30 publishes no macOS x86_64 wheel). The CI workflow
(tools/public-repo/root/.github/workflows/bundle.yml) runs this on all three and attaches the results to a release.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.dirname(HERE)
SEARCH = os.path.dirname(INSTALLER)
ENGINE = os.path.join(SEARCH, "serve", "api")
RELEASE = os.path.join(SEARCH, "release")

# what the engine image copies (serve/api/Dockerfile), less the servers' own tools; shipped on disk so search_api.py runs as a file
ENGINE_FILES = ["search_api.py", "lookups_sqlite.py", "lookups_release.py", "kinds_release.py", "tv_open.py", "release_root.py",
                "sem_ivf.py", "site_pages.py", "product_table.py", "gov_state.py", "site_slots.py", "state_anchor.py", "result_store.py", "facts.py", "news_sqlite.py", "instant_answers.py", "ranker.json", "warm.py",
                "native_host.py", "rtc_peer.py", "av_absent.py", "NOTICE"]
ENGINE_DIRS = ["licenses"]
# analysed with the launcher so every library and standard-library module they import is in the bundle
ANALYSED = [os.path.join(INSTALLER, "logout_search.py"), os.path.join(ENGINE, "search_api.py"), os.path.join(ENGINE, "native_host.py"),
            os.path.join(ENGINE, "rtc_peer.py"), os.path.join(ENGINE, "av_absent.py"), os.path.join(INSTALLER, "install.py"), os.path.join(RELEASE, "update_fetch.py"), os.path.join(RELEASE, "update_apply.py")]
ICON = os.path.join(HERE, "logout-search.ico")
COLLECT_ALL = ["onnxruntime", "tokenizers", "tantivy", "psycopg", "psycopg_binary",
               "aiortc", "aioice", "pylibsrtp", "google_crc32c", "qrcode"]      # the peer (other devices pair through it)


def system_name() -> str:
    s = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    m = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m)
    return f"{s}-{arch}"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_version() -> str:
    """The version this build carries (installer/autoupdate.py compares it with the latest release): the tag of a release
    build (GITHUB_REF_NAME=v0.4.3 -> 0.4.3, or LOGOUT_VERSION), else "dev", which never updates itself."""
    v = os.environ.get("LOGOUT_VERSION") or ""
    ref = os.environ.get("GITHUB_REF_NAME") or ""
    if not v and os.environ.get("GITHUB_REF_TYPE") == "tag" and re.fullmatch(r"v\d+\.\d+(\.\d+)?", ref):
        v = ref[1:]
    return v or "dev"


def stage_engine(stage: str) -> str:
    with open(os.path.join(stage, "version.txt"), "w", encoding="utf-8") as f:
        f.write(release_version() + "\n")
    eng = os.path.join(stage, "engine")
    os.makedirs(eng)
    for f in ENGINE_FILES:
        shutil.copy2(os.path.join(ENGINE, f), eng)
    for d in ENGINE_DIRS:
        shutil.copytree(os.path.join(ENGINE, d), os.path.join(eng, d))
    return eng


def pyinstaller(stage: str, work: str, dist: str) -> None:
    import importlib.util
    import PyInstaller.__main__ as pim
    if importlib.util.find_spec("av") is not None:
        raise SystemExit("build: PyAV (`av`) is installed here and would be bundled: run `pip uninstall -y av` first "
                         "(it is FFmpeg with GPL codecs; the peer uses serve/api/av_absent.py in its place)")
    args = ["--noconfirm", "--clean", "--onedir", "--name", "logout-search", "--console", "--icon", ICON,
            "--workpath", work, "--distpath", dist, "--specpath", work,
            "--paths", INSTALLER, "--paths", RELEASE, "--paths", ENGINE,
            "--add-data", f"{os.path.join(stage, 'engine')}{os.pathsep}engine",
            "--add-data", f"{os.path.join(stage, 'version.txt')}{os.pathsep}.",
            "--hidden-import", "numpy", "--hidden-import", "psycopg", "--hidden-import", "websockets",
            "--exclude-module", "av"]               # PyAV is FFmpeg (GPL codecs): never in the bundle, av_absent.py stands in
    for m in COLLECT_ALL:
        args += ["--collect-all", m]
    args += ANALYSED
    print("pyinstaller " + " ".join(args), flush=True)
    pim.run(args)


def setup_exe(archive: str, work: str, out: str) -> str:
    """Windows: logout-search-setup.exe, one file that holds the bundle's zip, unpacks it once and runs the program's
    setup (setup_stub.py). What a person downloads: nothing to unzip, nothing to type."""
    w = os.path.join(work, "setup")
    shutil.rmtree(w, ignore_errors=True)
    os.makedirs(w)
    payload = os.path.join(w, "logout-search.zip")
    shutil.copyfile(archive, payload)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                        "--name", "logout-search-setup", "--icon", ICON, "--workpath", os.path.join(w, "work"), "--distpath", out,
                        "--specpath", w, "--add-data", f"{payload}{os.pathsep}.", os.path.join(HERE, "setup_stub.py")])
    exe = os.path.join(out, "logout-search-setup.exe" if sys.platform == "win32" else "logout-search-setup")
    if r.returncode or not os.path.isfile(exe):
        raise SystemExit("build: the one-file setup program was not produced")
    # its own test: unpack to a throwaway folder (no setup run) and start the program that came out
    with tempfile.TemporaryDirectory() as t:
        u = subprocess.run([exe, "--dest", os.path.join(t, "ls"), "--no-run"], capture_output=True, text=True, timeout=600)
        got = exe_in(os.path.join(t, "ls", "app"))
        print(f"  $ logout-search-setup --no-run -> {u.returncode}")
        if u.returncode or not os.path.isfile(got):
            print(u.stdout[-1500:], u.stderr[-1500:])
            raise SystemExit("smoke: the setup program did not unpack the bundle")
        v = subprocess.run([got, "version"], capture_output=True, text=True, timeout=300)
        assert v.returncode == 0 and json.loads(v.stdout)["frozen"] is True, (v.stdout, v.stderr)
    return exe


def tray_exe(folder: str, work: str) -> None:
    """Windows: logout-search-tray.exe beside the program, windowed (no console): the tray icon that runs the engine, the
    target of the Start menu entries and of start-at-sign-in (installer/tray_win.py, standard library only)."""
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                        "--name", "logout-search-tray", "--icon", ICON, "--workpath", os.path.join(work, "tray"),
                        "--distpath", folder, "--specpath", os.path.join(work, "tray"), os.path.join(INSTALLER, "tray_win.py")])
    if r.returncode or not os.path.isfile(os.path.join(folder, "logout-search-tray.exe")):
        raise SystemExit("build: logout-search-tray.exe was not produced")
    shutil.copyfile(ICON, os.path.join(folder, "logout-search.ico"))


def exe_in(folder: str) -> str:
    return os.path.join(folder, "logout-search.exe" if sys.platform == "win32" else "logout-search")


def smoke(folder: str) -> None:
    exe = exe_in(folder)
    env = dict(os.environ, ORT_DISABLE_TELEMETRY="1")

    def run(*args, **kw):
        r = subprocess.run([exe, *args], capture_output=True, text=True, env={**env, **kw.get("env", {})}, timeout=600)
        print(f"  $ logout-search {' '.join(args)} -> {r.returncode}")
        if r.returncode:
            print(r.stdout[-2000:], r.stderr[-2000:])
            raise SystemExit(f"smoke: `{' '.join(args)}` exited {r.returncode}")
        return r.stdout

    v = json.loads(run("version"))
    assert v["frozen"] is True, v
    assert v["launcher"] == release_version(), (v["launcher"], release_version())   # the tag reached the program
    print(f"  version: {v['launcher']}")
    imp = json.loads(run("engine", "--check-imports"))
    sv = tuple(int(x) for x in imp["sqlite"].split("."))
    if sv < (3, 43) or not imp.get("fts5_contentless_delete"):
        raise SystemExit(f"smoke: the bundle's SQLite {imp['sqlite']} cannot read the release's place index (needs 3.43+)")
    assert "search_api.py" in imp["engine_files"] and "ranker.json" in imp["engine_files"], imp
    print("  libraries: " + ", ".join(f"{k} {imp[k]}" for k in ("numpy", "tantivy", "onnxruntime", "tokenizers", "psycopg")))
    with tempfile.TemporaryDirectory() as t:              # the updater is in the bundle and answers
        au = json.loads(run("autoupdate", "status", "--dest", t))
        assert au["enabled"] is True, au
    peer = json.loads(run("peer", "--check"))            # the peer's libraries load, without PyAV
    assert peer.get("aiortc") and peer.get("qr") is True, peer
    plan = run("setup", "--print", "--dest", os.path.join(tempfile.gettempdir(), "logout-search-smoke"))
    if sys.platform == "win32":                           # the tray icon appears, and goes, on this machine
        t = subprocess.run([os.path.join(folder, "logout-search-tray.exe"), "--selftest"], capture_output=True, text=True, timeout=120)
        print(f"  $ logout-search-tray --selftest -> {t.returncode}")
        if t.returncode:
            raise SystemExit(f"smoke: the tray icon failed: {t.stdout[-800:]} {t.stderr[-800:]}")
    assert "copy" in plan and "hf:logoutsearch/logout-search" in plan and "pair" in plan, plan
    with tempfile.TemporaryDirectory() as t:
        base = os.path.join(t, "base")
        rel = os.path.join(base, "releases", "fx-1")
        os.makedirs(os.path.join(rel, "models", "rerank-turbo"))
        with open(os.path.join(rel, "manifest.json"), "w") as f:
            json.dump({"release": "fx-1", "engine": {"reranker": {"cpu": "models/rerank-turbo"}}}, f)
        for n in ("tokenizer.json", "model_int8.onnx"):
            open(os.path.join(rel, "models", "rerank-turbo", n), "w").close()
        sys.path.insert(0, RELEASE)
        import release_ptr
        release_ptr.set_pointer(base, "current", "fx-1")
        out = run("start", "--dest", base, "--print-env", "--format", "json")
        e = json.loads(out)
        assert e["SEARCH_API_BIND"] == "127.0.0.1" and e["SEARCH_RELEASE_ID"] == "fx-1", e
        run("autostart", "on", "--dest", base, "--print")
        home = os.path.join(t, "home")
        for d in (("home", ".config", "google-chrome"), ("home", "Library", "Application Support", "Google", "Chrome"),
                  ("home", "AppData", "Local", "Google", "Chrome")):
            os.makedirs(os.path.join(t, *d))
        run("host", "register", "--dest", base, "--print",
            env={"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "USERPROFILE": home, "LOCALAPPDATA": os.path.join(home, "AppData", "Local"), "APPDATA": os.path.join(home, "AppData", "Roaming")})
        st = json.loads(run("status", "--dest", base))
        assert st["current"] == "fx-1", st


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(HERE, "build", "out"))
    ap.add_argument("--no-smoke", action="store_true")
    a = ap.parse_args()
    name = f"logout-search-{system_name()}"
    build = os.path.join(HERE, "build")
    stage, work, dist = (os.path.join(build, d) for d in ("stage", "work", "dist"))
    for d in (stage, work, dist):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    stage_engine(stage)
    pyinstaller(stage, work, dist)
    folder = os.path.join(dist, "logout-search")
    if not os.path.isfile(exe_in(folder)):
        raise SystemExit(f"build: {exe_in(folder)} was not produced")
    so = os.environ.get("LOGOUT_SQLITE_SO", "")
    if so:                                                # Linux: the current SQLite built in bundle.yml, over the system's old one
        dest = os.path.join(folder, "_internal", "libsqlite3.so.0")
        if not os.path.isfile(dest):
            raise SystemExit(f"build: {dest} is not where the bundle keeps SQLite: cannot replace it")
        shutil.copyfile(so, dest)
        print(f"  SQLite: {so} -> {dest}")
    if sys.platform == "win32":
        tray_exe(folder, work)
    with open(os.path.join(folder, "README.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("logout search: a web search engine that runs on your own computer.\n\n"
                "  logout-search install --source hf:logoutsearch/logout-search --pubkey ed25519:<key from the dataset page> --dest <folder>\n"
                "  logout-search start --dest <folder>              the engine answers on http://127.0.0.1:8090\n"
                "  logout-search autostart on --dest <folder>       start it at logon\n"
                "  logout-search host register --dest <folder>      connect the logout browser extension\n"
                "  logout-search update --dest <folder> --source hf:logoutsearch/logout-search --pubkey <key.pem>\n\n"
                "Licences: engine/NOTICE and engine/licenses/ (the engine), LICENSE (AGPL-3.0); the data carries its own.\n")
    if not a.no_smoke:
        print("smoke:", flush=True)
        smoke(folder)
    os.makedirs(a.out, exist_ok=True)
    final = os.path.join(a.out, name)
    shutil.rmtree(final, ignore_errors=True)
    shutil.move(folder, final)
    if sys.platform == "win32":
        archive = final + ".zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for d, _dirs, fs in os.walk(final):
                for fn in fs:
                    p = os.path.join(d, fn)
                    z.write(p, os.path.relpath(p, a.out))
    else:
        archive = final + ".tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            t.add(final, arcname=name)
    made = [archive]
    if sys.platform == "win32" and not a.no_smoke:
        made.append(setup_exe(archive, work, a.out))
    sums = os.path.join(a.out, "SHA256SUMS")
    names = {os.path.basename(p) for p in made}
    lines = [ln for ln in (open(sums).read().splitlines() if os.path.isfile(sums) else []) if ln.split("  ")[-1] not in names]
    lines += [f"{sha256(p)}  {os.path.basename(p)}" for p in made]
    with open(sums, "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    size = sum(os.path.getsize(os.path.join(d, fn)) for d, _x, fs in os.walk(final) for fn in fs)
    print(f"built {final} ({size / 1e6:.0f} MB unpacked, {os.path.getsize(archive) / 1e6:.0f} MB archive)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
