"""An in-process registry that speaks enough of the v2 API for the client tests.

Behavior modeled on Docker Hub: manifests need a bearer token obtained from
/token; blob GETs redirect to a second server on another port that must NOT
receive the Authorization header.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}  # (repo, ref) -> (body, media type)
        self.requests: list[tuple[str, str, dict]] = []
        self.token = "test-token"
        # What /token hands out, when it should differ from the accepted token
        # (Docker Hub gives an anonymous token that a missing repo still refuses).
        self.issued_token: str | None = None
        self.token_status = 200  # set to 401/404 to make /token itself fail
        self.truncate: set[str] = set()  # digests to serve half of, while still declaring the full length

    def add_image(self, repo: str, tag: str, config: dict, layers: list[bytes], multi_arch: bool = False) -> str:
        config_bytes = json.dumps(config).encode()
        self.blobs[digest_of(config_bytes)] = config_bytes
        layer_descs = []
        for data in layers:
            self.blobs[digest_of(data)] = data
            layer_descs.append(
                {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": digest_of(data), "size": len(data)}
            )
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest_of(config_bytes), "size": len(config_bytes)},
            "layers": layer_descs,
        }
        mbytes = json.dumps(manifest).encode()
        self.manifests[(repo, digest_of(mbytes))] = (mbytes, manifest["mediaType"])
        if not multi_arch:
            self.manifests[(repo, tag)] = (mbytes, manifest["mediaType"])
            return digest_of(mbytes)
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "0" * 64, "size": 1, "platform": {"os": "linux", "architecture": "s390x"}},
                {"mediaType": manifest["mediaType"], "digest": digest_of(mbytes), "size": len(mbytes), "platform": {"os": "linux", "architecture": config["architecture"]}},
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "1" * 64, "size": 1, "platform": {"os": "unknown", "architecture": "unknown"}},
            ],
        }
        ibytes = json.dumps(index).encode()
        self.manifests[(repo, tag)] = (ibytes, index["mediaType"])
        return digest_of(mbytes)

    def __enter__(self):
        reg = self
        blob_server_ref: list[HTTPServer] = []

        class BlobHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                reg.requests.append(("blob", self.path, dict(self.headers)))
                if "Authorization" in self.headers:
                    self.send_error(400, "Only one auth mechanism allowed")
                    return
                digest = self.path.rsplit("/", 1)[-1]
                data = reg.blobs.get(digest)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if digest in reg.truncate:
                    # Declare the full length but only write half, then drop the
                    # connection: simulates a connection that dies mid-transfer.
                    self.wfile.write(data[: len(data) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(data)

        class ApiHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                reg.requests.append(("api", self.path, dict(self.headers)))
                if self.path.startswith("/token"):
                    if reg.token_status != 200:
                        self.send_error(reg.token_status)
                        return
                    body = json.dumps({"token": reg.issued_token or reg.token}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.headers.get("Authorization") != f"Bearer {reg.token}":
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Bearer realm="http://{reg.host}/token",service="fake",scope="repository:x:pull"',
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                parts = self.path.split("/")  # ['', 'v2', repo..., 'manifests'|'blobs', ref]
                kind, ref = parts[-2], parts[-1]
                repo = "/".join(parts[2:-2])
                if kind == "manifests":
                    hit = reg.manifests.get((repo, ref))
                    if hit is None:
                        self.send_error(404)
                        return
                    body, mt = hit
                    self.send_response(200)
                    self.send_header("Content-Type", mt)
                    self.send_header("Docker-Content-Digest", digest_of(body))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if kind == "blobs":
                    self.send_response(307)
                    self.send_header("Location", f"http://{reg.blob_host}/store/{ref}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_error(404)

        self._api = HTTPServer(("127.0.0.1", 0), ApiHandler)
        self._blob = HTTPServer(("127.0.0.1", 0), BlobHandler)
        blob_server_ref.append(self._blob)
        self.host = f"127.0.0.1:{self._api.server_address[1]}"
        self.blob_host = f"127.0.0.1:{self._blob.server_address[1]}"
        self._threads = []
        for srv in (self._api, self._blob):
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc):
        for srv in (self._api, self._blob):
            srv.shutdown()
        for thread in self._threads:
            thread.join()
        for srv in (self._api, self._blob):
            srv.server_close()
