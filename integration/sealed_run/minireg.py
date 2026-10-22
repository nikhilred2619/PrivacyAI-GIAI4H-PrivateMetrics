"""A minimal, in-memory OCI distribution registry for local integration runs.

Test infrastructure only. The upstream worker pulls participant images **by
digest** and checks the pulled image's RepoDigests, so a sealed run needs a
registry. Where the public `registry:3` image cannot be pulled (offline or
egress-restricted environments), this stdlib server provides the handful of
endpoints `docker push` / `docker pull` use: /v2/, blob upload (POST, PATCH,
PUT), blob HEAD/GET, manifest PUT/HEAD/GET. No authentication, a throwaway
self-signed certificate, binds to 127.0.0.1 only, and forgets everything on
exit. Never deploy it.
"""
from __future__ import annotations

import hashlib
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BLOBS: dict[str, bytes] = {}
MANIFESTS: dict[tuple[str, str], tuple[bytes, str]] = {}
UPLOADS: dict[str, bytearray] = {}
LOCK = threading.Lock()

NAME = r"(?P<name>[a-z0-9]+(?:[._/-][a-z0-9]+)*)"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes = b"", headers: dict | None = None, head: bool = False):
        self.send_response(code)
        self.send_header("Docker-Distribution-API-Version", "registry/2.0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and not head:
            self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _route(self, method: str):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/v2", "/v2/"):
            return self._send(200, b"{}", {"Content-Type": "application/json"}, head=method == "HEAD")
        m = re.fullmatch(rf"/v2/{NAME}/blobs/uploads/?", path)
        if m and method == "POST":
            uid = uuid.uuid4().hex
            data = self._body()
            with LOCK:
                UPLOADS[uid] = bytearray(data)
            if "digest" in params:
                return self._finish_upload(m["name"], uid, params["digest"].replace("%3A", ":"))
            return self._send(202, headers={"Location": f"/v2/{m['name']}/blobs/uploads/{uid}",
                                            "Range": "0-0", "Docker-Upload-UUID": uid})
        m = re.fullmatch(rf"/v2/{NAME}/blobs/uploads/(?P<uid>[0-9a-f]+)", path)
        if m and method in ("PATCH", "PUT"):
            data = self._body()
            with LOCK:
                UPLOADS[m["uid"]].extend(data)
                size = len(UPLOADS[m["uid"]])
            if method == "PATCH":
                return self._send(202, headers={"Location": f"/v2/{m['name']}/blobs/uploads/{m['uid']}",
                                                "Range": f"0-{max(size - 1, 0)}", "Docker-Upload-UUID": m["uid"]})
            return self._finish_upload(m["name"], m["uid"], params.get("digest", "").replace("%3A", ":"))
        m = re.fullmatch(rf"/v2/{NAME}/blobs/(?P<d>sha256:[0-9a-f]{{64}})", path)
        if m and method in ("GET", "HEAD"):
            data = BLOBS.get(m["d"])
            if data is None:
                return self._send(404, head=method == "HEAD")
            return self._send(200, data, {"Content-Type": "application/octet-stream", "Docker-Content-Digest": m["d"]},
                              head=method == "HEAD")
        m = re.fullmatch(rf"/v2/{NAME}/manifests/(?P<ref>[A-Za-z0-9_.:-]+)", path)
        if m and method == "PUT":
            data = self._body()
            ctype = self.headers.get("Content-Type", "application/vnd.oci.image.manifest.v1+json")
            d = _digest(data)
            with LOCK:
                MANIFESTS[(m["name"], m["ref"])] = (data, ctype)
                MANIFESTS[(m["name"], d)] = (data, ctype)
            return self._send(201, headers={"Docker-Content-Digest": d,
                                            "Location": f"/v2/{m['name']}/manifests/{d}"})
        if m and method in ("GET", "HEAD"):
            hit = MANIFESTS.get((m["name"], m["ref"]))
            if hit is None:
                return self._send(404, head=method == "HEAD")
            data, ctype = hit
            return self._send(200, data, {"Content-Type": ctype, "Docker-Content-Digest": _digest(data)},
                              head=method == "HEAD")
        return self._send(404, head=method == "HEAD")

    def _finish_upload(self, name: str, uid: str, digest: str):
        with LOCK:
            data = bytes(UPLOADS.pop(uid))
        if _digest(data) != digest:
            return self._send(400, b"digest mismatch")
        BLOBS[digest] = data
        return self._send(201, headers={"Docker-Content-Digest": digest, "Location": f"/v2/{name}/blobs/{digest}"})

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_PUT(self):
        self._route("PUT")


def _self_signed(directory: str) -> tuple[str, str]:
    """A throwaway self-signed certificate for 127.0.0.1/localhost (openssl CLI)."""
    import os
    import subprocess
    cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    return cert, key


def serve(port: int = 0, *, tls: bool = True) -> tuple[ThreadingHTTPServer, int]:
    """Start the registry on 127.0.0.1. With `tls` (default) it speaks HTTPS with
    a throwaway self-signed certificate: dockerd treats localhost registries as
    insecure (HTTPS without verification, then HTTP), and its HTTP fallback is
    unreliable for concurrent layer requests, while unverified HTTPS works."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if tls:
        import ssl
        import tempfile
        d = tempfile.mkdtemp(prefix="minireg-")
        cert, key = _self_signed(d)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    import sys
    import time
    s, p = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
    print(f"listening on 127.0.0.1:{p}", flush=True)
    while True:
        time.sleep(3600)
