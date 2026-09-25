"""Stored images: pull from sources, extract layers once, flatten, track refs."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.errors import ImageNotFound, PullError, XcodonError
from xcodon_runtime.flatten import build_rootfs
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.layerdiff import hash_layer_dir, link_tree
from xcodon_runtime.reference import Platform, host_platform, parse_reference
from xcodon_runtime.registry import FetchedImage, RegistryClient
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream

log = logging.getLogger(__name__)

LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"


def apply_config_changes(config: dict, changes: dict) -> None:
    """Apply docker-style config changes in place: Env merges by key, Labels merge, others replace."""
    cfg = config.setdefault("config", {})
    for key, value in (changes or {}).items():
        if key == "Env":
            merged: dict[str, str] = {}
            for item in list(cfg.get("Env") or []) + list(value):
                k, _, v = item.partition("=")
                merged[k] = v
            cfg["Env"] = [f"{k}={v}" for k, v in merged.items()]
        elif key == "Labels":
            cfg["Labels"] = {**(cfg.get("Labels") or {}), **value}
        elif key in ("Cmd", "Entrypoint", "WorkingDir", "User", "Shell"):
            cfg[key] = value
        else:
            raise XcodonError(f"unsupported config change {key!r}")


@dataclass
class Image:
    id: str
    dir: Path
    config: dict
    refs: list[str]

    @property
    def rootfs(self) -> Path:
        return self.dir / "rootfs"

    @property
    def short_id(self) -> str:
        return self.id[:12]


class ImageStore:
    def __init__(self, home: RuntimeHome, sources: list | None = None) -> None:
        self.home = home
        self.sources = sources if sources is not None else [DaemonSource(home), RegistryClient(home)]

    # -- lookup ------------------------------------------------------------------

    def _load(self, image_id: str) -> Image:
        d = self.home.images / image_id
        config = json.loads((d / "config.json").read_text())
        refs = sorted(name for name, iid in self.home.read_refs().items() if iid == image_id)
        return Image(image_id, d, config, refs)

    def get(self, ref_or_id: str) -> Image | None:
        refs = self.home.read_refs()
        try:
            name = parse_reference(ref_or_id).name
        except ValueError:
            name = None
        if name in refs and (self.home.images / refs[name]).exists():
            return self._load(refs[name])
        candidate = ref_or_id.removeprefix("sha256:")
        matches = [p.name for p in self.home.images.iterdir() if p.name.startswith(candidate) and not p.name.endswith(".tmp")]
        if len(matches) == 1 and len(candidate) >= 4:
            return self._load(matches[0])
        return None

    def require(self, ref_or_id: str) -> Image:
        img = self.get(ref_or_id)
        if img is None:
            raise ImageNotFound(f"image {ref_or_id!r} is not in the local store; run: xcodon pull {ref_or_id}")
        return img

    def images(self) -> list[Image]:
        return [self._load(p.name) for p in sorted(self.home.images.iterdir()) if not p.name.endswith(".tmp")]

    def layer_dirs(self, image: Image) -> list[Path]:
        """The extracted layer directories of an image, in order."""
        manifest = json.loads((image.dir / "manifest.json").read_text())
        dirs = []
        for diff_id in manifest.get("diff_ids", []):
            d = self.home.layers / diff_id.split(":", 1)[1]
            if not d.is_dir():
                raise XcodonError(f"layer {diff_id[:19]} of image {image.short_id} is missing; pull the image again")
            dirs.append(d)
        return dirs

    # -- pull --------------------------------------------------------------------

    def pull(self, ref: str, platform: Platform | None = None) -> Image:
        reference = parse_reference(ref)
        platform = platform or host_platform()
        lock_name = "pull-" + hashlib.sha256(reference.name.encode()).hexdigest()[:16]
        # ``store`` shared is held for the whole pull, fetch included, so a
        # concurrent prune (which takes it exclusive) cannot delete a blob
        # that is still downloading. Lock order: pull -> store -> layer/image/refs.
        with self.home.lock(lock_name), self.home.lock("store", shared=True):
            errors: list[str] = []
            for source in self.sources:
                if isinstance(source, DaemonSource):
                    if not source.available() or not source.has_image(reference):
                        continue
                try:
                    fetched = source.fetch(reference, platform)
                    break
                except PullError as e:
                    errors.append(f"{source.name}: {e}")
            else:
                raise PullError(f"could not fetch {reference.name}: " + ("; ".join(errors) or "no source available"))
            return self.import_fetched(fetched, reference.name)

    def import_fetched(self, fetched: FetchedImage, ref_name: str) -> Image:
        """Import a fetched image.

        Holds ``store`` shared (a reader), so a concurrent ``prune`` (which
        takes ``store`` exclusive) cannot delete a layer while it is being
        extracted or read here, or an image directory while it is being
        built. Lock order: pull -> store -> layer/image/refs.
        """
        with self.home.lock("store", shared=True):
            image_id = fetched.config_digest.split(":", 1)[1]
            diff_ids = fetched.config.get("rootfs", {}).get("diff_ids", [])
            if len(diff_ids) != len(fetched.layers):
                raise PullError(f"config lists {len(diff_ids)} diff_ids but manifest has {len(fetched.layers)} layers")
            layer_dirs = [self._ensure_layer(diff_id, layer.blob_path) for diff_id, layer in zip(diff_ids, fetched.layers)]

            image_dir = self.home.images / image_id
            with self.home.lock(f"image-{image_id}"):
                if not image_dir.exists():
                    with self.home.atomic_dir(image_dir) as tmp:
                        (tmp / "config.json").write_text(json.dumps(fetched.config, indent=2))
                        (tmp / "manifest.json").write_text(
                            json.dumps(
                                {
                                    "config": fetched.config_digest,
                                    "diff_ids": diff_ids,
                                    "layers": [{"digest": l.digest, "mediaType": l.media_type, "size": l.size} for l in fetched.layers],
                                    "source": fetched.source,
                                },
                                indent=2,
                            )
                        )
                        log.info("flattening %d layers for %s", len(layer_dirs), image_id[:12])
                        build_rootfs(layer_dirs, tmp / "rootfs")
            for layer in fetched.layers:
                layer.blob_path.unlink(missing_ok=True)
            (self.home.blobs / image_id).unlink(missing_ok=True)

            self._set_ref(ref_name, image_id)
            return self._load(image_id)

    def _ensure_layer(self, diff_id: str, blob_path: Path) -> Path:
        hexdigest = diff_id.split(":", 1)[1]
        dest = self.home.layers / hexdigest
        if dest.exists():
            return dest
        with self.home.lock(f"layer-{hexdigest}"):
            if dest.exists():
                return dest
            log.info("extracting layer %s", hexdigest[:12])
            with self.home.atomic_dir(dest) as tmp, open_layer_stream(blob_path) as stream:
                skipped = extract_layer(stream, tmp)
                if skipped:
                    log.info("layer %s: skipped %d special entries", hexdigest[:12], skipped)
        return dest

    # -- commit / tag --------------------------------------------------------------

    def commit(self, base: Image, layer_dir: Path | None, *, changes: dict | None = None,
               ref: str | None = None, created_by: str = "") -> Image:
        """Compose a new image from ``base`` plus one layer directory and config changes.

        ``layer_dir`` is an OCI layer directory (whiteouts as ``.wh.`` files) or None for a
        config-only image. Holds ``store`` shared like a pull. Lock order: store -> layer/image/refs.
        """
        with self.home.lock("store", shared=True):
            base_dirs = self.layer_dirs(base)
            config = copy.deepcopy(base.config)
            apply_config_changes(config, changes or {})
            diff_ids = list(config.setdefault("rootfs", {}).setdefault("diff_ids", []))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if layer_dir is not None:
                diff_id = hash_layer_dir(layer_dir)
                hexd = diff_id.split(":", 1)[1]
                dest = self.home.layers / hexd
                if not dest.exists():
                    with self.home.lock(f"layer-{hexd}"):
                        if not dest.exists():
                            with self.home.atomic_dir(dest) as tmp:
                                link_tree(layer_dir, tmp)
                diff_ids.append(diff_id)
                base_dirs = base_dirs + [dest]
            config["rootfs"]["diff_ids"] = diff_ids
            config["created"] = now
            config.setdefault("history", []).append(
                {"created": now, "created_by": created_by or "xrunner commit", "empty_layer": layer_dir is None})
            canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            image_id = hashlib.sha256(canonical).hexdigest()
            image_dir = self.home.images / image_id
            with self.home.lock(f"image-{image_id}"):
                if not image_dir.exists():
                    with self.home.atomic_dir(image_dir) as tmp:
                        (tmp / "config.json").write_text(json.dumps(config, indent=2))
                        (tmp / "manifest.json").write_text(json.dumps({
                            "config": f"sha256:{image_id}", "diff_ids": diff_ids,
                            "layers": [{"digest": d, "mediaType": LAYER_MEDIA_TYPE, "size": 0} for d in diff_ids],
                            "source": "commit", "parent": base.id,
                        }, indent=2))
                        build_rootfs(base_dirs, tmp / "rootfs")
            if ref:
                self._set_ref(ref, image_id)
            return self._load(image_id)

    def _set_ref(self, ref: str, image_id: str) -> None:
        name = parse_reference(ref).name
        with self.home.lock("refs"):
            refs = self.home.read_refs()
            refs[name] = image_id
            self.home.write_refs(refs)

    def tag(self, ref_or_id: str, new_ref: str) -> Image:
        """Point ``new_ref`` at the image ``ref_or_id`` resolves to.

        Holds ``store`` shared around the lookup and the ref write, so a
        concurrent ``prune(all=True)`` (which takes ``store`` exclusive)
        cannot delete the image as untagged in between.
        """
        with self.home.lock("store", shared=True):
            img = self.require(ref_or_id)
            self._set_ref(new_ref, img.id)
            return self._load(img.id)

    def untagged(self) -> list[Image]:
        return [i for i in self.images() if not i.refs]

    # -- remove / inspect / prune ------------------------------------------------

    def remove(self, ref_or_id: str) -> None:
        """Drop a ref and, if nothing else references the image, delete it.

        Holds ``store`` exclusive (a writer), so no concurrent ``pull`` can
        add a ref to this image between the refs update and the directory
        delete. Lock order: pull -> store -> layer/image/refs.
        """
        with self.home.lock("store"):
            img = self.require(ref_or_id)
            with self.home.lock("refs"):
                refs = self.home.read_refs()
                try:
                    name = parse_reference(ref_or_id).name
                except ValueError:
                    name = None
                if name in refs:
                    del refs[name]
                else:
                    for n in list(refs):
                        if refs[n] == img.id:
                            del refs[n]
                self.home.write_refs(refs)
                still_referenced = img.id in refs.values()
            if not still_referenced:
                with self.home.lock(f"image-{img.id}"):
                    shutil.rmtree(img.dir, ignore_errors=True)

    def inspect(self, ref_or_id: str) -> list[dict]:
        img = self.get(ref_or_id)
        if img is None:
            return []
        manifest = json.loads((img.dir / "manifest.json").read_text())
        return [
            {
                "Id": f"sha256:{img.id}",
                "RepoTags": img.refs,
                "Architecture": img.config.get("architecture"),
                "Os": img.config.get("os"),
                "Created": img.config.get("created"),
                "Config": img.config.get("config", {}),
                "RootFS": {"Type": "layers", "Layers": manifest.get("diff_ids", [])},
                "XcodonSource": manifest.get("source"),
            }
        ]

    def prune(self, all: bool = False, keep: set[str] = frozenset()) -> list[Path]:
        """Remove leftovers and orphan blobs. With ``all``, unreferenced layers too.

        Holds ``store`` exclusive (a writer), so it waits out any pull that
        is still fetching, extracting a layer, or building an image (a pull
        holds ``store`` shared from start to finish), and no such pull can
        start once pruning has begun. Lock order: pull -> store -> layer/image/refs.

        Because no pull can be in flight here, every blob that is not a
        half-written ``*.part`` is left over from a pull that died after its
        download and before its import, so all of them are removed.

        ``keep`` is a set of image ids that must survive the untagged sweep
        even without a ref, e.g. images that a container still uses.
        """
        with self.home.lock("store"):
            removed = self.home.prune_leftovers()
            for blob in self.home.blobs.iterdir():
                if blob.is_file():
                    blob.unlink(missing_ok=True)
                    removed.append(blob)
            if all:
                for img in self.untagged():
                    if img.id in keep:
                        continue
                    with self.home.lock(f"image-{img.id}"):
                        shutil.rmtree(img.dir, ignore_errors=True)
                    if not img.dir.exists():
                        removed.append(img.dir)
                used: set[str] = set()
                for img in self.images():
                    manifest = json.loads((img.dir / "manifest.json").read_text())
                    used.update(d.split(":", 1)[1] for d in manifest.get("diff_ids", []))
                for layer_dir in self.home.layers.iterdir():
                    if layer_dir.name not in used:
                        with self.home.lock(f"layer-{layer_dir.name}"):
                            shutil.rmtree(layer_dir, ignore_errors=True)
                        removed.append(layer_dir)
            return removed
