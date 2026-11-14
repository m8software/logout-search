#!/usr/bin/env python3
"""A tiny HTTP file server WITH Range support and a request log, for the installer tests (never used outside tests).

    search_install_server.py ROOT PORT_FILE LOG [--tamper PATH] [--drop PATH:N]

Serves ROOT on 127.0.0.1 (a free port, written to PORT_FILE). Every request is appended to LOG as `GET <path> <range start or 0> <bytes sent>`.
--tamper PATH   flip one byte of that file's response (the first byte it sends)
--drop PATH:N   close the connection after N bytes on the FIRST request for PATH (the installer must resume from N)
"""
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, port_file, log = sys.argv[1:4]
tamper = drop_path = None
drop_n = 0
args = sys.argv[4:]
while args:
    k, v = args[0], args[1]
    args = args[2:]
    if k == "--tamper":
        tamper = v
    elif k == "--drop":
        drop_path, drop_n = v.rsplit(":", 1)
        drop_n = int(drop_n)
dropped = set()
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        rel = self.path.lstrip("/")
        p = os.path.join(root, *rel.split("/"))
        if ".." in rel or not os.path.isfile(p):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = open(p, "rb").read()
        start = 0
        m = re.match(r"bytes=(\d+)-", self.headers.get("Range", ""))
        if m:
            start = int(m.group(1))
        body = data[start:]
        if tamper == rel and body:
            body = bytes([body[0] ^ 1]) + body[1:]
        cut = None
        with lock:
            if drop_path == rel and rel not in dropped:
                dropped.add(rel)
                cut = drop_n
        if m and start >= len(data):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        sent = body if cut is None else body[:cut]
        try:
            self.wfile.write(sent)
            self.wfile.flush()
        except OSError:
            pass
        with lock:
            open(log, "a").write(f"GET {rel} {start} {len(sent)}\n")
        if cut is not None:
            self.close_connection = True
            self.connection.close()


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
open(port_file, "w").write(str(srv.server_address[1]))
srv.serve_forever()
