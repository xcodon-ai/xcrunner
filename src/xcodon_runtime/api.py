# src/xcodon_runtime/api.py
"""The Python API. The CLI and the coala adapter are thin layers over this."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from xcodon_runtime import __version__
from xcodon_runtime.containers import Container, ContainerStore, _rmtree_tolerant
from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.engine import Bind, Engine, EngineChoice, get_engine, select_engine
from xcodon_runtime.engine_proot import find_proot
from xcodon_runtime.envdir import ENV_LOCK_NAME, env_layer_dir
from xcodon_runtime.errors import ContainerNotRunning, ImageNotFound, XcodonError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.imagestore import Image, ImageStore
from xcodon_runtime.layerdiff import snapshot_diff, snapshot_upper
from xcodon_runtime.reference import Platform, parse_reference
from xcodon_runtime.spec import build_spec

log = logging.getLogger(__name__)

# How long an exited container survives `prune --all`.
CONTAINER_MAX_AGE = timedelta(days=1)


@dataclass
class ExecResult:
    code: int
    stdout: bytes
    stderr: bytes


def _lock_env_layer(env_root: Path) -> int | None:
    """Take the exclusive lock on a shared env layer for the duration of a snapshot.

    Returns an open, locked file descriptor the caller must close once the
    snapshot is done (closing releases the lock), or ``None`` when the layer
    has no lock file yet: nothing has ever locked it, which is normal for the
    proot engine (only the ns engine's keeper takes this lock).
    """
    lock_path = env_root / ENV_LOCK_NAME
    if not lock_path.exists():
        return None
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise XcodonError(f"env layer {env_root} is in use by a running container") from None
    return fd


def _argv(command: str | Sequence[str] | None, default: list[str]) -> list[str]:
    if command is None:
        return list(default)
    if isinstance(command, str):
        return ["/bin/sh", "-c", command]
    return list(command)


class Runtime:
    def __init__(self, home: Path | str | None = None, engine: str | None = None) -> None:
        self.home = RuntimeHome(home)
        self.images = ImageStore(self.home)
        self.store = ContainerStore(self.home)
        self._engine_override = engine
        self._choice: EngineChoice | None = None
        self._engines: dict[str, Engine] = {}

    # -- engines -----------------------------------------------------------------

    def engine_choice(self) -> EngineChoice:
        if self._choice is None:
            self._choice = select_engine(self.home, self._engine_override)
        return self._choice

    def _engine(self, c: Container) -> Engine:
        if c.engine not in self._engines:
            self._engines[c.engine] = get_engine(c.engine)
        return self._engines[c.engine]

    # -- images ------------------------------------------------------------------

    def pull(self, ref: str, platform: Platform | None = None) -> Image:
        return self.images.pull(ref, platform)

    def inspect(self, ref: str) -> Image | None:
        return self.images.get(ref)

    def list_images(self) -> list[Image]:
        return self.images.images()

    def remove_image(self, ref: str) -> None:
        self.images.remove(ref)

    def resolve_image(self, ref: str, pull: str = "missing") -> Image:
        if pull == "always":
            return self.images.pull(ref)
        img = self.images.get(ref)
        if img is not None:
            # Spec 11.5: only "missing" looks for a moved daemon tag. "never"
            # means "use the store as is", and an image id names one image
            # for good, so neither asks the daemon.
            if pull == "missing" and not self._is_id_ref(ref, img) and self._daemon_tag_moved(ref, img):
                log.info("image %s changed in the local docker daemon; importing it again", ref)
                try:
                    return self.images.pull(ref)
                except XcodonError as e:
                    log.warning("could not import %s again from the local docker daemon (%s); "
                                "using the stored image %s", ref, e, img.short_id)
                    return img
            return img
        if pull == "never":
            raise ImageNotFound(f"image {ref!r} is not in the local store")
        log.info("image %s not found locally; pulling", ref)
        return self.images.pull(ref)

    def _is_id_ref(self, ref: str, img: Image) -> bool:
        """True when ``ref`` named ``img`` by its id (all 64 hex digits, or a unique prefix), not by a tag."""
        candidate = ref.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{4,64}", candidate) or not img.id.startswith(candidate):
            return False
        if len(candidate) == 64 or ref.startswith("sha256:"):
            return True
        # A short hex string is also a valid repository name. It is an id
        # only when no tag of that name exists (ImageStore.get tries tags first).
        try:
            name = parse_reference(ref).name
        except XcodonError:
            return True
        return name not in self.home.read_refs()

    def _daemon_tag_moved(self, ref: str, img: Image) -> bool:
        """True when a daemon-sourced tag now points at a different image in the daemon.

        Compares the daemon's current ``.Id`` for the tag with the ``daemon_id``
        recorded at import. That id is the config digest with docker's classic
        image store, but the manifest digest with the containerd image store,
        so it is never compared with the stored image id. An image imported
        before ``daemon_id`` was recorded counts as moved once: the import
        records the id.
        """
        try:
            manifest = json.loads((img.dir / "manifest.json").read_text())
        except (OSError, ValueError):
            return False
        if manifest.get("source") != "daemon":
            return False
        try:
            reference = parse_reference(ref)
        except XcodonError:
            return False
        daemon = next((s for s in self.images.sources if isinstance(s, DaemonSource)), None)
        if daemon is None or not daemon.available():
            return False
        # ``image_id`` returning None already covers "no such tag in the
        # daemon", so there is no need for a separate ``has_image`` call.
        current = daemon.image_id(reference)
        if not current:
            return False
        stored = manifest.get("daemon_id")
        return not stored or current != stored

    # -- containers --------------------------------------------------------------

    def create(self, ref: str, command: Sequence[str] | None = None, entrypoint: Sequence[str] | None = None,
               binds: Sequence[Bind] = (), workdir: str | None = None, env: Mapping[str, str] | None = None,
               user: str | None = None, name: str | None = None, pull: str = "missing",
               env_dir: str | Path | None = None) -> Container:
        image = self.resolve_image(ref, pull)
        container_id = os.urandom(32).hex()
        spec = build_spec(image.config, image.rootfs, container_id, command=command, entrypoint=entrypoint,
                          env=env, workdir=workdir, user=user)
        for b in binds:
            if not os.path.isabs(b.source) or not os.path.isabs(b.target):
                raise XcodonError(f"bind paths must be absolute: {b.source}:{b.target}")
        env_dir_s: str | None = None
        if env_dir is not None:
            env_dir_s = str(env_dir)
            if not os.path.isabs(env_dir_s):
                raise XcodonError(f"env_dir must be an absolute path: {env_dir_s}")
        engine = self.engine_choice().name
        c = self.store.create(container_id, image, ref, spec, list(binds), engine, name, env_dir=env_dir_s)
        if env_dir_s is not None:
            Path(env_dir_s).mkdir(parents=True, exist_ok=True)
        return c

    def start(self, c: Container) -> None:
        self._engine(c).start(c)
        c.state = "running"
        c.save()

    def popen(self, c: Container, command: str | Sequence[str] | None = None, workdir: str | None = None,
              env: Mapping[str, str] | None = None, **popen_kwargs) -> subprocess.Popen:
        engine = self._engine(c)
        # The tracked state comes first, as in remove(): a proot container
        # that has been stopped keeps its rootfs, so the engine alone cannot
        # tell "started" from "stopped".
        if c.state != "running" or not engine.is_running(c):
            if c.state == "running":
                c.state = "exited"
                c.save()
            raise ContainerNotRunning(f"container {c.short_id} is not running")
        merged_env = dict(c.env)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        return engine.popen(c, _argv(command, c.argv), merged_env, workdir or c.workdir, **popen_kwargs)

    def exec(self, c: Container, command: str | Sequence[str] | None = None, workdir: str | None = None,
             env: Mapping[str, str] | None = None, capture: bool = True, timeout: float | None = None) -> ExecResult:
        pipe = subprocess.PIPE if capture else None
        p = self.popen(c, command, workdir, env, stdout=pipe, stderr=pipe, stdin=subprocess.DEVNULL)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Terminate first, then kill: a plain kill() only stops the
            # nsexec/proot wrapper, and the guest process survives it (nsexec
            # forwards only catchable signals, and PRoot detaches its tracee
            # when killed) unless a hard SIGKILL is given a chance to reach it.
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            p.kill()
            p.communicate()
            raise
        return ExecResult(p.returncode, out or b"", err or b"")

    def stop(self, c: Container) -> None:
        self._engine(c).stop(c)
        c.state = "exited"
        c.save()

    def remove(self, c: Container, force: bool = False) -> None:
        # Consult the container's own tracked state first, not the engine
        # directly: the proot engine has no persistent process to ask about,
        # so its is_running() reports "has been started" for as long as the
        # copied rootfs exists, even long after stop(). The engine is only
        # asked to catch the other direction: a state on disk that still says
        # "running" because a keeper died without going through stop().
        if c.state == "running":
            if self._engine(c).is_running(c):
                if not force:
                    raise XcodonError(f"container {c.short_id} is running; stop it first or use force")
                self.stop(c)
            else:
                c.state = "exited"
                c.save()
        self.store.remove(c)

    def containers(self, all: bool = False) -> list[Container]:
        out = []
        for c in self.store.list():
            running = c.state == "running" and self._engine(c).is_running(c)
            if c.state == "running" and not running:
                c.state = "exited"
                c.save()
            if running or all:
                out.append(c)
        return out

    def get_container(self, key: str) -> Container:
        c = self.store.get(key)
        if c.state == "running" and not self._engine(c).is_running(c):
            c.state = "exited"
            c.save()
        return c

    def commit(self, container: Container | None = None, tag: str | None = None, *,
               env_dir: str | Path | None = None, image: str | None = None,
               changes: dict | None = None, message: str = "") -> Image:
        """Snapshot a stopped container's layer, or an env folder layer, as a new image."""
        if tag is not None:
            parse_reference(tag)
        env_lock_fd: int | None = None
        try:
            if container is not None:
                base = self.images.require(container.image_id)
                if container.state == "running" and self._engine(container).is_running(container):
                    raise XcodonError(f"container {container.short_id} is running; stop it before commit")
                if container.env_dir:
                    env_lock_fd = _lock_env_layer(env_layer_dir(container.env_dir, container.image_id))
                if container.engine == "ns":
                    from xcodon_runtime.engine_ns import NsEngine
                    upper, _ = NsEngine().layer_paths(container)
                    source = ("upper", upper)
                else:
                    from xcodon_runtime.engine_proot import ProotEngine
                    source = ("rootfs", ProotEngine().rootfs_path(container))
                created_by = message or f"xrunner commit {container.short_id}"
            elif env_dir is not None and image is not None:
                base = self.images.require(image)
                layer = env_layer_dir(str(env_dir), base.id)
                if not layer.is_dir():
                    raise XcodonError(f"no env layer for image {base.short_id} under {env_dir}")
                env_lock_fd = _lock_env_layer(layer)
                source = ("upper", layer / "upper") if (layer / "upper").is_dir() else ("rootfs", layer / "rootfs")
                created_by = message or f"xrunner commit --env-dir {env_dir}"
            else:
                raise XcodonError("commit needs a container, or --env-dir together with the base image")
            kind, path = source
            if not path.is_dir():
                raise XcodonError(f"nothing to commit: {path} does not exist")
            work = Path(tempfile.mkdtemp(prefix="commit-", dir=self.home.path))
            try:
                layer_dir = work / "layer"
                if kind == "upper":
                    snapshot_upper(path, layer_dir)
                else:
                    snapshot_diff(path, base.rootfs, layer_dir)
                return self.images.commit(base, layer_dir, changes=changes, ref=tag, created_by=created_by)
            finally:
                _rmtree_tolerant(work)
        finally:
            if env_lock_fd is not None:
                os.close(env_lock_fd)

    # -- run ---------------------------------------------------------------------

    def run(self, ref: str, command: Sequence[str] | None = None, entrypoint: Sequence[str] | None = None,
            binds: Sequence[Bind] = (), workdir: str | None = None, env: Mapping[str, str] | None = None,
            user: str | None = None, name: str | None = None, rm: bool = False, pull: str = "missing",
            cidfile: str | None = None, stdin=None, stdout=None, stderr=None,
            env_dir: str | Path | None = None) -> int:
        """Create, start, and wait for one container, forwarding SIGINT/SIGTERM to it.

        Signal forwarding only works when this is called from the main
        thread: installing a signal handler off the main thread raises
        ValueError, and ``run`` treats that as "no forwarding available"
        instead of failing.

        When ``cidfile`` is given, the container id is written there right
        after creation, before it starts.
        """
        c = self.create(ref, command, entrypoint, binds, workdir, env, user, name, pull, env_dir=env_dir)
        started = False
        try:
            if cidfile:
                try:
                    with open(cidfile, "w") as f:
                        f.write(c.id)
                except OSError as e:
                    raise XcodonError(f"cannot write cidfile {cidfile}: {e}") from e
            self.start(c)
            started = True
            p = self.popen(c, None, None, None, stdin=stdin, stdout=stdout, stderr=stderr)
            previous = {}

            def forward(signum, frame):
                try:
                    p.send_signal(signum)
                except ProcessLookupError:
                    pass

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous[sig] = signal.signal(sig, forward)
                except ValueError:
                    pass  # not the main thread; run without signal forwarding
            try:
                return p.wait()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        finally:
            try:
                if started:
                    self.stop(c)
            finally:
                if rm:
                    self.store.remove(c)

    # -- misc --------------------------------------------------------------------

    def prune(self, all: bool = False) -> list[Path]:
        """Remove leftovers. With ``all``, unreferenced layers and old containers too.

        The container sweep lives here because the Runtime owns the
        ContainerStore: it removes exited containers created more than
        ``CONTAINER_MAX_AGE`` ago. An untagged image that some existing
        container still points at (``keep``) survives the untagged sweep
        even though it has no ref.
        """
        keep = {c.image_id for c in self.store.list()}
        removed = self.images.prune(all, keep=keep)
        if all:
            removed += self._prune_old_containers()
        return removed

    def _prune_old_containers(self) -> list[Path]:
        cutoff = datetime.now(timezone.utc) - CONTAINER_MAX_AGE
        removed: list[Path] = []
        for c in self.containers(all=True):
            if c.state != "exited":
                continue
            try:
                created = datetime.fromisoformat(c.created)
            except ValueError:
                log.warning("container %s has an unreadable created time; keeping it", c.short_id)
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                self.store.remove(c)
                removed.append(c.dir)
        return removed

    def build(self, context: str | Path, dockerfile: str | Path | None = None, tags: Sequence[str] = (),
              build_args: Mapping[str, str] | None = None, no_cache: bool = False,
              out: Callable[[str], None] | None = None) -> Image:
        """Run a Dockerfile subset from a build context, one image commit per step. See build.Builder."""
        from xcodon_runtime.build import Builder

        context = Path(context)
        if not context.is_dir():
            raise XcodonError(f"build context {context} is not a directory")
        df = Path(dockerfile) if dockerfile else context / "Dockerfile"
        try:
            text = df.read_text()
        except OSError as e:
            raise XcodonError(f"cannot read Dockerfile {df}: {e}") from e
        return Builder(self, context, out).build(text, tags=tags, build_args=build_args, no_cache=no_cache)

    def info(self) -> dict:
        choice = self.engine_choice()
        return {
            "version": __version__,
            "home": str(self.home.path),
            "engine": choice.name,
            "engine_reason": choice.reason,
            "probes": choice.probes,
            "proot": find_proot(),
            "python": sys.version.split()[0],
        }
