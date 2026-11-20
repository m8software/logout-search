#!/usr/bin/env python3
"""What the Logout Search container runs: install the data if it is not there, then the engine and the AI-app connector.

    /data                      the one folder to keep (a volume), laid out as a desktop install's: /data/data holds the signed
                               data release, the settings and the news file; the daily update keeps its own files beside it
    LOGOUT_MCP_HTTP            where the connector listens inside the container (default 0.0.0.0:8091; `off`: no connector)
    LOGOUT_MCP_ALLOW_ORIGIN    web pages that may call the connector, comma-separated host names (pages on this computer always may)
    LOGOUT_AUTOUPDATE          `0`: no daily data update (default: on). The program itself updates with the image.
    LOGOUT_SEARCH_SOURCE, LOGOUT_SEARCH_PUBKEY   another release source and its pinned key (testing, a mirror)

The engine listens on 127.0.0.1 inside the container, as on a desktop: only the connector reaches it. Other devices are
not paired from a container (that needs the host's network); use the desktop program for that.
Exit code 75 means "start me again" (an update was applied): `restart: unless-stopped` does it.
"""
import os
import signal
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LS = os.path.join(APP, "installer", "logout_search.py")
BASE = os.environ.get("LOGOUT_SEARCH_BASE") or "/data"
DATA = os.path.join(BASE, "data")                    # the updater takes the folder above `data` as its own (logout_search.updater_context)
PORT = 8090
LISTEN = os.environ.get("LOGOUT_MCP_HTTP") or "0.0.0.0:8091"


def say(msg: str) -> None:
    print(f"logout-search: {msg}", flush=True)


def installed() -> bool:
    return os.path.isfile(os.path.join(os.path.realpath(os.path.join(DATA, "current")), "manifest.json"))


def source() -> tuple:
    sys.path.insert(0, os.path.dirname(LS))
    import logout_search
    return (os.environ.get("LOGOUT_SEARCH_SOURCE") or logout_search.DATA_SOURCE,
            os.environ.get("LOGOUT_SEARCH_PUBKEY") or logout_search.DATA_PUBKEY)


def health() -> int:
    """The container's health check: the connector answers (the engine, when there is no connector). A first start that
    is still downloading counts as healthy: it is doing what it should, and may take an hour."""
    import urllib.request
    if not installed():
        return 0
    port = PORT if LISTEN.lower() == "off" else int(LISTEN.rpartition(":")[2])
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4).close()
        return 0
    except Exception:  # noqa: BLE001
        return 1


def main() -> int:
    if sys.argv[1:] == ["--health"]:
        return health()
    if not os.access(BASE, os.W_OK):
        say(f"{BASE} is not writable by user {os.getuid()}: use a named volume, or `chown {os.getuid()}` the folder you mounted")
        return 1
    os.makedirs(DATA, exist_ok=True)
    if not installed():
        src, key = source()
        say(f"no search data in {DATA} yet: downloading it from {src} (the sizes follow; a stopped download resumes)")
        rc = subprocess.call([sys.executable, LS, "install", "--source", src, "--pubkey", key, "--dest", DATA])
        if rc != 0:
            say("the download did not finish: start the container again to resume it")
            return rc
    env = dict(os.environ)
    env.setdefault("LOGOUT_AUTOUPDATE", "1")
    listen = LISTEN
    connector = None
    if listen.lower() != "off":
        args = [sys.executable, LS, "mcp", "--port", str(PORT), "--http", listen]
        for host in (os.environ.get("LOGOUT_MCP_ALLOW_ORIGIN") or "").split(","):
            if host.strip():
                args += ["--allow-origin", host.strip()]
        connector = subprocess.Popen(args, env=env)
    engine = subprocess.Popen([sys.executable, LS, "start", "--dest", DATA, "--port", str(PORT), "--no-peer"], env=env)

    asked_to_stop = []

    def stop(*_):                                    # the launcher stops its engine and collectors on an interrupt
        asked_to_stop.append(True)
        engine.send_signal(signal.SIGINT)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        rc = engine.wait()
        return 0 if asked_to_stop else rc            # `docker stop` is not a failure
    finally:
        if connector is not None and connector.poll() is None:
            connector.terminate()
            try:
                connector.wait(10)
            except subprocess.TimeoutExpired:
                connector.kill()


if __name__ == "__main__":
    sys.exit(main())
