"""Anonymous pulls from an OCI / Docker v2 registry using only the standard library."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse

from xcodon_runtime.errors import PullError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import DEFAULT_REGISTRY, Platform, Reference

log = logging.getLogger(__name__)

MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPES = {MT_OCI_INDEX, MT_DOCKER_LIST}
MANIFEST_ACCEPT = ", ".join([MT_OCI_INDEX, MT_OCI_MANIFEST, MT_DOCKER_LIST, MT_DOCKER_MANIFEST])
CHUNK = 1 << 20


@dataclass
class FetchedLayer:
    digest: str
    media_type: str
    size: int
    blob_path: Path


@dataclass
class FetchedImage:
    config_digest: str
    config: dict
    layers: list[FetchedLayer] = field(default_factory=list)
    source: str = "registry"
    # The local docker daemon's own id for the tag (``docker image inspect
    # --format {{.Id}}``) at export time. Only a daemon import sets it. With
    # docker's containerd image store it is the manifest digest, not the
    # config digest, so it is kept as is and compared as is.
    daemon_id: str | None = None
    # Where this image lives in a registry, as `name@sha256:<manifest digest>`:
    # the digest of the tag's own manifest (an index for multi-arch images, as
    # docker's RepoDigests records it). Empty when unknown.
    repo_digests: list[str] = field(default_factory=list)


def select_platform(manifests: list[dict], platform: Platform) -> str:
    """Pick the manifest digest for ``platform`` from an index. Raises PullError if none."""
    for m in manifests:
        p = m.get("platform") or {}
        if p.get("os") != platform.os or p.get("architecture") != platform.architecture:
            continue
        if platform.variant and p.get("variant") not in (None, platform.variant):
            continue
        return m["digest"]
    available = ", ".join(
        f"{(m.get('platform') or {}).get('os')}/{(m.get('platform') or {}).get('architecture')}" for m in manifests
    )
    raise PullError(f"no manifest for {platform}; available: {available}")


def _hub_not_found(ref: Reference) -> PullError:
    """The error for a Docker Hub 401/404 on a manifest or its token.

    Docker Hub answers 401 for a repository that does not exist, the same as
    for a private one. On a host without docker, a name that only a local
    daemon ever had (for example a locally built base image) lands here.
    """
    short = ref.repository.removeprefix("library/")
    tag = ref.tag or "latest"
    return PullError(
        f"image {ref.registry}/{ref.repository}:{tag} was not found on Docker Hub, or it needs a login; "
        f"if it is a local name, import it first, for example: "
        f"xrunner pull OTHER/{short.rsplit('/', 1)[-1]}:{tag} && "
        f"xrunner tag OTHER/{short.rsplit('/', 1)[-1]}:{tag} {short}:{tag}"
    )


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Registries redirect blob downloads to object storage that rejects our bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class RegistryClient:
    name = "registry"

    def __init__(self, home: RuntimeHome, scheme: str = "https", timeout: float = 60) -> None:
        self.home = home
        self.scheme = scheme
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect())
        self._tokens: dict[tuple[str, str], str] = {}

    # -- HTTP plumbing -------------------------------------------------------------

    def _open(self, url: str, headers: dict[str, str], token: str | None):
        req = urllib.request.Request(url, headers=dict(headers))
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        return self._opener.open(req, timeout=self.timeout)

    def _fetch_token(self, challenge: str, ref: Reference | None = None) -> str:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise PullError(f"unsupported auth scheme {scheme!r}; only anonymous bearer tokens are supported")
        kv = dict(re.findall(r'(\w+)="([^"]*)"', params))
        if "realm" not in kv:
            raise PullError(f"malformed auth challenge: {challenge}")
        query = {k: kv[k] for k in ("service", "scope") if k in kv}
        url = kv["realm"] + ("?" + urlencode(query) if query else "")
        try:
            with self._opener.open(urllib.request.Request(url), timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            e.close()
            if ref is not None and ref.registry == DEFAULT_REGISTRY and e.code in (401, 404):
                raise _hub_not_found(ref) from e
            raise PullError(f"token request failed: HTTP {e.code} from {url}") from e
        except urllib.error.URLError as e:
            raise PullError(f"token request failed: {e.reason}") from e
        token = data.get("token") or data.get("access_token")
        if not token:
            raise PullError(f"token response from {url} has no token")
        return token

    def _get(self, ref: Reference, path: str, accept: str | None = None):
        url = f"{self.scheme}://{ref.api_host}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept} if accept else {}
        key = (ref.api_host, ref.repository)
        # Only a manifest (or its token) miss on Docker Hub gets the "not
        # found, or needs a login" hint; a blob error is a different problem.
        hub_manifest = ref.registry == DEFAULT_REGISTRY and path.startswith("manifests/")
        try:
            return self._open(url, headers, self._tokens.get(key))
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate") if e.code == 401 else None
            e.close()
            if not challenge:
                if hub_manifest and e.code in (401, 404):
                    raise _hub_not_found(ref) from e
                raise PullError(f"{url}: HTTP {e.code} {e.reason}") from e
            self._tokens[key] = self._fetch_token(challenge, ref if hub_manifest else None)
        except urllib.error.URLError as e:
            raise PullError(f"{url}: {e.reason}") from e
        try:
            return self._open(url, headers, self._tokens[key])
        except urllib.error.HTTPError as e:
            e.close()
            if hub_manifest and e.code in (401, 404):
                raise _hub_not_found(ref) from e
            raise PullError(f"{url}: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise PullError(f"{url}: {e.reason}") from e

    # -- public API ----------------------------------------------------------------

    def _manifest_and_digest(self, ref: Reference, manifest_ref: str) -> tuple[dict, str]:
        with self._get(ref, f"manifests/{manifest_ref}", MANIFEST_ACCEPT) as r:
            body = r.read()
            media_type = r.headers.get("Content-Type", "").split(";")[0].strip()
        # The digest of a manifest is the sha256 of its bytes, by definition.
        # The Docker-Content-Digest header is not trusted: it is computed here.
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        data = json.loads(body)
        data.setdefault("mediaType", media_type)
        return data, digest

    def _manifest(self, ref: Reference, manifest_ref: str) -> dict:
        return self._manifest_and_digest(ref, manifest_ref)[0]

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        manifest, top_digest = self._manifest_and_digest(ref, ref.manifest_ref)
        if manifest.get("mediaType") in INDEX_TYPES or "manifests" in manifest:
            digest = select_platform(manifest["manifests"], platform)
            manifest = self._manifest(ref, digest)
        if "config" not in manifest or "layers" not in manifest:
            raise PullError(f"{ref.name}: unsupported manifest type {manifest.get('mediaType')!r} (schema v1 is not supported)")
        config_digest = manifest["config"]["digest"]
        config = json.loads(self.fetch_blob(ref, config_digest).read_text())
        layers = [
            FetchedLayer(l["digest"], l.get("mediaType", ""), int(l.get("size", 0)), self.fetch_blob(ref, l["digest"]))
            for l in manifest["layers"]
        ]
        return FetchedImage(config_digest, config, layers, source=self.name,
                             repo_digests=[f"{ref.registry}/{ref.repository}@{top_digest}"])

    def fetch_blob(self, ref: Reference, digest: str) -> Path:
        """Download a blob to the home blob store, verifying its digest. Idempotent."""
        algo, _, hexdigest = digest.partition(":")
        if algo != "sha256":
            raise PullError(f"unsupported digest algorithm {algo!r}")
        final = self.home.blobs / hexdigest
        if final.exists():
            return final
        part = final.with_name(hexdigest + ".part")
        h = hashlib.sha256()
        log.info("downloading %s", digest[:19])
        try:
            with self._get(ref, f"blobs/{digest}") as r, open(part, "wb") as out:
                for chunk in iter(lambda: r.read(CHUNK), b""):
                    h.update(chunk)
                    out.write(chunk)
        except (OSError, http.client.HTTPException, urllib.error.URLError) as e:
            part.unlink(missing_ok=True)
            raise PullError(f"download of {digest} failed: {e}") from e
        if h.hexdigest() != hexdigest:
            part.unlink(missing_ok=True)
            raise PullError(f"digest mismatch for {digest}: got sha256:{h.hexdigest()}")
        os.replace(part, final)
        return final
