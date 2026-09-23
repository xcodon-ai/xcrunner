"""Stored images: pull from sources, extract layers once, flatten, track refs."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.errors import ImageNotFound, PullError
from xcodon_runtime.flatten import build_rootfs
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, host_platform, parse_reference
from xcodon_runtime.registry import FetchedImage, RegistryClient
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream

log = logging.getLogger(__name__)


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

    # -- pull --------------------------------------------------------------------

    def pull(self, ref: str, platform: Platform | None = None) -> Image:
        reference = parse_reference(ref)
        platform = platform or host_platform()
        lock_name = "pull-" + hashlib.sha256(reference.name.encode()).hexdigest()[:16]
        with self.home.lock(lock_name):
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

        with self.home.lock("refs"):
            refs = self.home.read_refs()
            refs[ref_name] = image_id
            self.home.write_refs(refs)
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

    # -- remove / inspect / prune ------------------------------------------------

    def remove(self, ref_or_id: str) -> None:
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

    def prune(self) -> list[Path]:
        """Remove leftovers and layers no stored image references."""
        removed = self.home.prune_leftovers()
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
