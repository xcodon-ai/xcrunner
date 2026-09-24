# Persistent Env Folder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an xrunner container keep its writable layer in a host env folder keyed by image id, so tools installed inside survive coala-runtime's per-call container teardown, and wire it through the CLI, the coala-runtime adapter, and opencodon.

**Architecture:** A new `envdir` module owns the env-folder layout and its lock. `NsEngine` puts `upper/` and `work/` in the env layer when a container has `env_dir`, and hands the keeper an inherited `flock` descriptor so the lock lives as long as the keeper. `ProotEngine` keeps its rootfs copy there. `Container`, `ContainerStore`, `Runtime`, and the CLI carry an optional `env_dir`. The adapter reads `XRUNNER_ENV_DIR`; opencodon sets it from a project-relative tools field.

**Tech Stack:** Python 3.10+, stdlib only. Existing test fixtures `home`, `busybox_image`, `engine_name`.

**Spec:** `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`, Section 10.

## Global Constraints

- Python `>=3.10`, stdlib only, plain English docstrings and messages; every error derives from `XcodonError`.
- Env layer path is `<env_dir>/<image_id>/` with `upper/`, `work/` (ns) or `rootfs/` (proot), `image.json`, `.lock`. `merged/`, `config.json`, `keeper.pid`, `keeper.log` stay in the container directory. `xrunner rm` never touches the env folder.
- Concurrency: an ns start with an env folder holds `flock(LOCK_EX)` on `.lock` for the keeper's lifetime by inheriting the descriptor; a second start logs one wait line and blocks. proot guards only the first copy.
- CLI flag is `--env-dir DIR` (never `--env`). Adapter env var is `XRUNNER_ENV_DIR`. opencodon tools field is `xrunner_env_project_relative`, default `workspace/.xrunner-env`, set only when the effective engine is `xrunner`.
- Existing behavior without `env_dir` is unchanged; the full suite (221 tests) stays green.
- Commit after every task with explicit `git add` paths. Repos: xcodon-runtime on `main`; opencodon on branch `xcodon-engine`.

## File Structure

```
src/xcodon_runtime/envdir.py           env layer paths, image.json, lock acquisition   (new)
src/xcodon_runtime/containers.py       Container.env_dir, ContainerStore.create(env_dir)
src/xcodon_runtime/engine_ns.py        upper/work from env layer; lock fd passed to keeper
src/xcodon_runtime/engine_proot.py     rootfs from env layer; copy guarded
src/xcodon_runtime/api.py              Runtime.create/run(env_dir)
src/xcodon_runtime/cli.py              --env-dir
src/xcodon_runtime/coala_adapter.py    XRUNNER_ENV_DIR
tests/test_env_dir.py                  engine-parametrized env-folder tests             (new)
tests/test_cli.py, tests/test_coala_adapter.py   additions
README.md                              env folder section
opencodon: src/xcodon/config.py, src/xcodon/agents_integration.py, tests/test_agents_integration.py
```

---

### Task 1: Env layer in the core, both engines

**Files:**
- Create: `src/xcodon_runtime/envdir.py`
- Modify: `src/xcodon_runtime/containers.py` (Container field, ContainerStore.create)
- Modify: `src/xcodon_runtime/engine_ns.py` (`_plan`, `_start_locked`, `_clear_overlay_work`)
- Modify: `src/xcodon_runtime/engine_proot.py` (`start`, `is_running`, `popen` rootfs path)
- Modify: `src/xcodon_runtime/api.py` (`create`, `run`)
- Test: `tests/test_env_dir.py`

**Interfaces:**
- Consumes: `Container`, `RuntimeHome.lock`, `container_lock`, `Runtime` fixtures.
- Produces: `envdir.env_layer_dir(env_dir: str, image_id: str) -> Path`; `envdir.prepare_env_layer(container: Container) -> Path` (creates the layer dir and `image.json`, returns the layer dir); `envdir.acquire_env_lock(layer_dir: Path, what: str) -> int` (returns an open fd holding `LOCK_EX`; logs one line if it had to wait); `envdir.ENV_LOCK_NAME = ".lock"`, `envdir.ENV_INFO_NAME = "image.json"`; `Container.env_dir: str | None = None`; `ContainerStore.create(..., env_dir: str | None = None)`; `Runtime.create(..., env_dir: str | Path | None = None)`; `Runtime.run(..., env_dir=None)`; `NsEngine.layer_paths(container) -> tuple[Path, Path]` (upper, work); `ProotEngine.rootfs_path(container) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_env_dir.py
"""Persistent env folder: the writable layer lives in a host folder keyed by image id."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.envdir import ENV_INFO_NAME, ENV_LOCK_NAME, env_layer_dir


@pytest.fixture
def rt(home, busybox_image, engine_name) -> Runtime:
    return Runtime(home.path, engine=engine_name)


def test_install_persists_across_containers(rt, busybox_image, tmp_path):
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    assert c1.env_dir == str(env)
    rt.start(c1)
    assert rt.exec(c1, "mkdir -p /usr/local/tool && echo v1 > /usr/local/tool/VERSION").code == 0
    rt.stop(c1)
    rt.remove(c1)
    layer = env_layer_dir(str(env), busybox_image.id)
    assert layer.is_dir(), "env layer keyed by image id"
    info = json.loads((layer / ENV_INFO_NAME).read_text())
    assert info["image_id"] == busybox_image.id
    assert not c1.dir.exists(), "container dir removed"
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c2)
    assert rt.exec(c2, ["/bin/cat", "/usr/local/tool/VERSION"]).stdout == b"v1\n"
    rt.stop(c2)
    rt.remove(c2)
    assert layer.is_dir(), "rm never deletes the env folder"


def test_env_is_keyed_by_image(rt, home, busybox_rootfs, tmp_path):
    from tests.conftest import pack_rootfs_as_image

    other = pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/other:latest", config={"Env": ["PATH=/bin", "OTHER=1"]})
    env = tmp_path / "env"
    a = rt.create("xcodon-test/busybox", env_dir=env)
    b = rt.create("xcodon-test/other", env_dir=env)
    rt.start(a)
    rt.exec(a, "echo a > /marker")
    rt.stop(a)
    rt.start(b)
    assert rt.exec(b, ["/bin/cat", "/marker"]).code != 0, "different image, different layer"
    rt.stop(b)
    assert (env / a.image_id).is_dir() and (env / other.id).is_dir()


def test_relative_env_dir_is_rejected(rt):
    from xcodon_runtime.errors import XcodonError

    with pytest.raises(XcodonError, match="absolute"):
        rt.create("xcodon-test/busybox", env_dir="relative/env")


def test_second_start_waits_for_first_to_stop(rt, engine_name, tmp_path):
    if engine_name != "ns":
        pytest.skip("only the ns engine locks the layer")
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c1)
    started = threading.Event()
    err: list[BaseException] = []

    def second():
        try:
            rt.start(c2)
            started.set()
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=second, daemon=True)
    t.start()
    time.sleep(0.8)
    assert not started.is_set(), "second start must block while the first holds the layer"
    lock = env_layer_dir(str(env), c1.image_id) / ENV_LOCK_NAME
    assert lock.exists()
    rt.stop(c1)
    t.join(timeout=30)
    assert not err, err
    assert started.is_set()
    assert rt.exec(c2, ["/bin/true"]).code == 0
    rt.stop(c2)


def test_proot_reuses_rootfs_copy(rt, engine_name, tmp_path):
    if engine_name != "proot":
        pytest.skip("proot only")
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c1)
    rootfs = env_layer_dir(str(env), c1.image_id) / "rootfs"
    assert (rootfs / "bin" / "busybox").exists()
    stamp = os.stat(rootfs / "bin" / "busybox").st_mtime_ns
    rt.exec(c1, "echo p > /persist")
    rt.stop(c1)
    rt.remove(c1)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c2)
    assert os.stat(rootfs / "bin" / "busybox").st_mtime_ns == stamp, "no second copy"
    assert rt.exec(c2, ["/bin/cat", "/persist"]).stdout == b"p\n"
    rt.stop(c2)


def test_run_with_env_dir(rt, tmp_path):
    env = tmp_path / "env"
    assert rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "echo r > /from-run"], rm=True, env_dir=env) == 0
    assert rt.run("xcodon-test/busybox", command=["/bin/cat", "/from-run"], rm=True, env_dir=env) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_env_dir.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.envdir'`

- [ ] **Step 3: Write envdir.py**

```python
# src/xcodon_runtime/envdir.py
"""The persistent env folder: a host directory holding a container's writable layer.

Layout is ``<env_dir>/<image_id>/`` with ``upper/`` and ``work/`` (ns engine)
or ``rootfs/`` (proot engine), an ``image.json`` for humans, and a ``.lock``
the running ns keeper holds so two overlays never share one upper directory.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xcodon_runtime.containers import Container

log = logging.getLogger(__name__)
ENV_LOCK_NAME = ".lock"
ENV_INFO_NAME = "image.json"


def env_layer_dir(env_dir: str, image_id: str) -> Path:
    return Path(env_dir) / image_id


def prepare_env_layer(container: "Container") -> Path:
    """Create the layer directory for this container's image and record what it is for."""
    assert container.env_dir is not None
    layer = env_layer_dir(container.env_dir, container.image_id)
    layer.mkdir(parents=True, exist_ok=True)
    info = layer / ENV_INFO_NAME
    if not info.exists():
        info.write_text(json.dumps({
            "image_ref": container.image_ref,
            "image_id": container.image_id,
            "engine": container.engine,
            "first_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
    return layer


def acquire_env_lock(layer_dir: Path, what: str) -> int:
    """Take the layer's exclusive lock and return the open descriptor that holds it.

    The caller passes the descriptor to the keeper, which inherits the lock and
    holds it until it exits. If another container has the layer, log once and wait.
    """
    fd = os.open(layer_dir / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("%s: waiting for env layer %s (another container of this image is running)", what, layer_dir)
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd
```

- [ ] **Step 4: Container field and store**

In `src/xcodon_runtime/containers.py`, add the field after `state`:

```python
    state: str = "created"
    env_dir: str | None = None
    dir: Path = field(default=Path("."), compare=False, repr=False)
```

`save()`/`load()` already round-trip all fields through `asdict`, and an old `config.json` without the key loads with the default. Extend `ContainerStore.create`:

```python
    def create(self, container_id: str, image: Image, image_ref: str, spec: ProcessSpec,
               binds: list[Bind], engine: str, name: str | None, env_dir: str | None = None) -> Container:
```

and pass `env_dir=env_dir` into the `Container(...)` call.

- [ ] **Step 5: ns engine uses the env layer and hands the keeper the lock**

In `src/xcodon_runtime/engine_ns.py` add imports `from xcodon_runtime.envdir import acquire_env_lock, prepare_env_layer` and a method:

```python
    def layer_paths(self, container: Container) -> tuple[Path, Path]:
        """Where this container's upper and work directories live."""
        if container.env_dir:
            layer = prepare_env_layer(container)
            return layer / "upper", layer / "work"
        return container.dir / "upper", container.dir / "work"
```

Change `_plan` to use them:

```python
        upper, work = self.layer_paths(container)
        return {
            "lower": container.image_rootfs,
            "upper": str(upper),
            "work": str(work),
            "merged": str(container.dir / "merged"),
```

In `_start_locked`, replace the directory creation and the `Popen` so the lock descriptor is inherited:

```python
        upper, work = self.layer_paths(container)
        for d in (upper, work, container.dir / "merged"):
            d.mkdir(parents=True, exist_ok=True)
        plan_path = container.dir / KEEPER_PLAN
        plan_path.write_text(json.dumps(self._plan(container), indent=2))
        log_path = container.dir / KEEPER_LOG

        lock_fd = acquire_env_lock(upper.parent, f"container {container.short_id}") if container.env_dir else None
        r, w = os.pipe()
        pass_fds = (w,) if lock_fd is None else (w, lock_fd)
        try:
            with open(log_path, "ab") as logf:
                proc = subprocess.Popen(
                    [*KEEPER_ARGV, str(plan_path), str(w)],
                    pass_fds=pass_fds, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                    start_new_session=True, close_fds=True,
                )
        finally:
            os.close(w)
            if lock_fd is not None:
                os.close(lock_fd)  # the keeper's inherited copy keeps the flock
```

Keep the rest of the handshake as it is. In `_clear_overlay_work` use the real work dir:

```python
        _, work = self.layer_paths(container)
        try:
            (work / "work").rmdir()
        except OSError:
            pass
```

Note for the implementer: the keeper must not close inherited descriptors other than its own; check `keeper.py` does not call `os.closerange`. If it does, exclude the lock fd (it is any fd above 2 that is not the info fd; simplest is to not close ranges at all).

- [ ] **Step 6: proot engine uses the env layer rootfs**

In `src/xcodon_runtime/engine_proot.py` add `from xcodon_runtime.envdir import prepare_env_layer` and `from xcodon_runtime.engine import container_lock` (already imported) plus:

```python
    def rootfs_path(self, container: Container) -> Path:
        if container.env_dir:
            return prepare_env_layer(container) / "rootfs"
        return container.dir / "rootfs"
```

Replace every `container.dir / "rootfs"` in `start`, `is_running`, and `popen` with `self.rootfs_path(container)`. In `start`, guard the copy for shared layers:

```python
            rootfs = self.rootfs_path(container)
            if not rootfs.is_dir() or not any(rootfs.iterdir()):
                home = RuntimeHome(container.dir.parent.parent)
                with home.lock(f"envlayer-{container.image_id}"):
                    if not rootfs.is_dir() or not any(rootfs.iterdir()):
                        log.info("container %s: copying rootfs (full copy unless the filesystem supports reflinks)",
                                 container.short_id)
                        _copy_rootfs(Path(container.image_rootfs), rootfs)
```

(import `RuntimeHome` from `xcodon_runtime.home`).

- [ ] **Step 7: Runtime API**

In `src/xcodon_runtime/api.py` `create` gains `env_dir: str | Path | None = None`:

```python
        env_dir_s: str | None = None
        if env_dir is not None:
            env_dir_s = str(env_dir)
            if not os.path.isabs(env_dir_s):
                raise XcodonError(f"env_dir must be an absolute path: {env_dir_s}")
            Path(env_dir_s).mkdir(parents=True, exist_ok=True)
        engine = self.engine_choice().name
        return self.store.create(container_id, image, ref, spec, list(binds), engine, name, env_dir=env_dir_s)
```

`run` gains `env_dir: str | Path | None = None` and passes `env_dir=env_dir` to `self.create(...)`.

- [ ] **Step 8: Run the tests**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_env_dir.py -v` then the full suite `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q`.
Expected: env tests pass for both engines (the lock test runs on ns, the copy test on proot); full suite green; `pgrep -af xcodon_runtime.keeper` empty afterwards.

- [ ] **Step 9: Commit**

```bash
git add src/xcodon_runtime/envdir.py src/xcodon_runtime/containers.py src/xcodon_runtime/engine_ns.py src/xcodon_runtime/engine_proot.py src/xcodon_runtime/api.py tests/test_env_dir.py
git commit -m "feat: persistent env folder holds the writable layer keyed by image id"
```

---

### Task 2: CLI `--env-dir` and README

**Files:**
- Modify: `src/xcodon_runtime/cli.py`
- Modify: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Runtime.create/run(env_dir=...)`.
- Produces: `RunOptions.env_dir: str | None`; `--env-dir DIR` accepted by `run` and `create`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`)

```python
def test_parse_run_args_env_dir_is_absolutized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = cli.parse_run_args(["--env-dir", "myenv", "img", "true"])
    assert opts.env_dir == str(tmp_path / "myenv")
    opts = cli.parse_run_args(["--env-dir=/abs/env", "img"])
    assert opts.env_dir == "/abs/env"
    assert cli.parse_run_args(["img"]).env_dir is None


def test_cli_env_dir_persists_installs(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    env = tmp_path / "env"
    assert cli.main([*e, "run", "--rm", f"--env-dir={env}", "xcodon-test/busybox", "/bin/sh", "-c", "echo tool > /usr/local/tool"]) == 0
    assert cli.main([*e, "run", "--rm", f"--env-dir={env}", "xcodon-test/busybox", "/bin/cat", "/usr/local/tool"]) == 0
    assert capfd.readouterr().out.strip().endswith("tool")
    assert cli.main([*e, "create", "--name", "envc", f"--env-dir={env}", "xcodon-test/busybox", "/bin/sh"]) == 0
    c = cli.Runtime(home.path, engine=engine_name).get_container("envc")
    assert c.env_dir == str(env)
    assert cli.main([*e, "rm", "envc"]) == 0
    assert (env / c.image_id).is_dir()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_cli.py -k env_dir`
Expected: FAIL (`UsageError: unknown option --env-dir`).

- [ ] **Step 3: Implement**

In `cli.py`: add `"--env-dir": True,` to `RUN_FLAGS`; add `env_dir: str | None = None` to `RunOptions`; in `parse_run_args` add

```python
        elif flag == "--env-dir":
            opts.env_dir = os.path.abspath(value)
```

Pass `env_dir=opts.env_dir` in both `cmd_run`'s `rt.run(...)` and `cmd_create`'s `rt.create(...)`. Add `[--env-dir DIR]` to `RUN_USAGE`. In README, add under Use:

```markdown
    xrunner run --rm --env-dir $PWD/.xrunner-env python:3.12-slim pip install numpy
    xrunner run --rm --env-dir $PWD/.xrunner-env python:3.12-slim python -c 'import numpy'

`--env-dir` keeps the container's writable layer in a host folder, keyed by
image id, so tools installed in one container are there for the next one.
Delete `<env-dir>/<image-id>` to reset. coala-runtime uses this through the
`XRUNNER_ENV_DIR` variable.
```

- [ ] **Step 4: Run tests, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_cli.py` then full suite.

```bash
git add src/xcodon_runtime/cli.py README.md tests/test_cli.py
git commit -m "feat: --env-dir on run and create"
```

---

### Task 3: Adapter reads XRUNNER_ENV_DIR

**Files:**
- Modify: `src/xcodon_runtime/coala_adapter.py`
- Test: `tests/test_coala_adapter.py`

**Interfaces:**
- Produces: `XcodonContainerManager(home=None, engine=None, env_dir=None)`; `env_dir` defaults to `os.environ.get("XRUNNER_ENV_DIR") or None`; every `create_container` passes it to `Runtime.create`. `ENV_DIR_VAR = "XRUNNER_ENV_DIR"`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_env_dir_from_environment_persists_installs(home, busybox_image, engine_name, tmp_path, monkeypatch):
    env = tmp_path / "xrunner-env"
    monkeypatch.setenv("XRUNNER_ENV_DIR", str(env))
    mgr = XcodonContainerManager(home.path, engine=engine_name)

    async def flow():
        c = await mgr.create_container("xcodon-test/busybox:latest")
        await mgr.start_container(c)
        code, _, _ = await mgr.exec_command(c, "mkdir -p /opt/tool && echo ok > /opt/tool/marker")
        assert code == 0
        await mgr.remove_container(c)
        c2 = await mgr.create_container("xcodon-test/busybox:latest")
        await mgr.start_container(c2)
        code, out, _ = await mgr.exec_command(c2, "cat /opt/tool/marker")
        assert (code, out) == (0, b"ok\n")
        assert c2.container.env_dir == str(env)
        await mgr.remove_container(c2)

    asyncio.run(flow())
    assert (env / busybox_image.id / ("upper" if engine_name == "ns" else "rootfs")).is_dir()
```

- [ ] **Step 2: Implement**

```python
ENV_DIR_VAR = "XRUNNER_ENV_DIR"

    def __init__(self, home: Path | str | None = None, engine: str | None = None,
                 env_dir: str | None = None) -> None:
        self.runtime = Runtime(home, engine=engine)
        self.env_dir = env_dir if env_dir is not None else (os.environ.get(ENV_DIR_VAR) or None)
        self.containers: Dict[str, Container] = {}
```

and in `create_container` pass `env_dir=self.env_dir` to `self.runtime.create(...)`. Import `os`. Mention in the class docstring: "Set XRUNNER_ENV_DIR to keep installs across coala-runtime's per-call containers."

- [ ] **Step 3: Run tests, commit**

```bash
PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_coala_adapter.py
git add src/xcodon_runtime/coala_adapter.py tests/test_coala_adapter.py
git commit -m "feat: adapter honors XRUNNER_ENV_DIR for persistent installs"
```

---

### Task 4: opencodon passes the project env folder

Repository: `/media/qhu/slim/Workspace/opencodon`, branch `xcodon-engine`.

**Files:**
- Modify: `src/xcodon/config.py` (ToolsConfig field)
- Modify: `src/xcodon/agents_integration.py` (env merge)
- Test: `tests/test_agents_integration.py`

**Interfaces:**
- Produces: `ToolsConfig.xrunner_env_project_relative: str | None = "workspace/.xrunner-env"`; `_xrunner_env_dir_env(tools, project_root) -> dict[str, str]`; `merge_coala_container_env_into_mcp_servers` sets `XRUNNER_ENV_DIR` on the coala-runtime server when the effective engine is `xrunner`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agents_integration.py`)

```python
def test_merge_sets_xrunner_env_dir_when_engine_is_xrunner(tmp_path: Path) -> None:
    from xcodon.agents_integration import merge_coala_container_env_into_mcp_servers
    from xcodon.config import McpServerConfig, ToolsConfig

    tools = ToolsConfig(coala_runtime_container_engine="xrunner")
    srv = McpServerConfig(name="coala-runtime", command="coala-runtime", args=[])
    out = merge_coala_container_env_into_mcp_servers([srv], tools, project_root=tmp_path)
    assert out[0].env["XRUNNER_ENV_DIR"] == str((tmp_path / "workspace" / ".xrunner-env").resolve())
    assert (tmp_path / "workspace" / ".xrunner-env").is_dir()


def test_merge_no_xrunner_env_dir_for_docker_or_outside_root(tmp_path: Path) -> None:
    from xcodon.agents_integration import merge_coala_container_env_into_mcp_servers
    from xcodon.config import McpServerConfig, ToolsConfig

    srv = McpServerConfig(name="coala-runtime", command="coala-runtime", args=[])
    docker = merge_coala_container_env_into_mcp_servers([srv], ToolsConfig(coala_runtime_container_engine="docker"), project_root=tmp_path)
    assert "XRUNNER_ENV_DIR" not in docker[0].env
    escaping = ToolsConfig(coala_runtime_container_engine="xrunner", xrunner_env_project_relative="../outside")
    out = merge_coala_container_env_into_mcp_servers([srv], escaping, project_root=tmp_path)
    assert "XRUNNER_ENV_DIR" not in out[0].env
    forced = ToolsConfig(coala_runtime_container_engine="docker", coala_runtime_mcp_extra_env={"COALA_CONTAINER_ENGINE": "xrunner"})
    out = merge_coala_container_env_into_mcp_servers([srv], forced, project_root=tmp_path)
    assert "XRUNNER_ENV_DIR" in out[0].env, "effective engine after the extra-env override"
```

- [ ] **Step 2: Implement**

`config.py`, next to `coala_runtime_tmpdir_project_relative`:

```python
    xrunner_env_project_relative: str | None = Field(default="workspace/.xrunner-env")
    """Project-relative folder that holds the xrunner writable layer, passed to the **coala-runtime** MCP process as ``XRUNNER_ENV_DIR`` when the engine is ``xrunner``. Tools installed inside a container (pip, apt, R) persist there across calls and runs, keyed by image id. Set to ``null`` to disable."""
```

`agents_integration.py`, after `_coala_runtime_tmpdir_env`:

```python
def _xrunner_env_dir_env(tools: ToolsConfig, project_root: Path | None) -> dict[str, str]:
    """If ``tools.xrunner_env_project_relative`` is set, return ``XRUNNER_ENV_DIR`` inside the project root."""
    rel = (tools.xrunner_env_project_relative or "").strip()
    if not rel or project_root is None:
        return {}
    root = project_root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning("Ignoring tools.xrunner_env_project_relative=%r: resolves outside project root %s", rel, root)
        return {}
    candidate.mkdir(parents=True, exist_ok=True)
    return {"XRUNNER_ENV_DIR": str(candidate)}
```

In `merge_coala_container_env_into_mcp_servers`, after the effective engine is settled inside the `if rt_server and s.name == rt_server:` block:

```python
            if str(eng) == "xrunner" and "XRUNNER_ENV_DIR" not in env:
                env.update(_xrunner_env_dir_env(tools, project_root))
```

Add one sentence to the function docstring.

- [ ] **Step 3: Run tests, commit**

```bash
.venv/bin/python -m pytest -q tests/test_agents_integration.py tests/test_cli_init_container_engine.py
git add src/xcodon/config.py src/xcodon/agents_integration.py tests/test_agents_integration.py
git commit -m "feat: pass the project env folder to coala-runtime as XRUNNER_ENV_DIR for the xrunner engine"
```

---

## Plan Self-Review

- Spec 10.2 layout: Task 1 (`envdir.py`, both engines). Spec 10.3 lock semantics: Task 1 (`acquire_env_lock`, inherited fd, proot copy guard). Spec 10.4 CLI: Task 2; API: Task 1; adapter: Task 3; opencodon: Task 4. Spec 10.5 out of scope: nothing added.
- Names are consistent: `env_dir` everywhere; `env_layer_dir`, `prepare_env_layer`, `acquire_env_lock`; `layer_paths` on `NsEngine`, `rootfs_path` on `ProotEngine`; `XRUNNER_ENV_DIR`; `xrunner_env_project_relative`.
- No placeholders.
