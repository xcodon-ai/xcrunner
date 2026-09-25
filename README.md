# xcodon-runtime

A rootless container runtime for Docker images. It runs the images that
coala and coala-runtime use on hosts without Docker and without root.

Two engines, chosen automatically:

- **ns**: kernel user namespaces plus overlayfs, no helper binary. Native
  speed. Needs a Linux kernel 5.11 or newer with unprivileged user namespaces
  enabled. Most workstations, cloud VMs, and current HPC nodes qualify.
- **proot**: a vendored static PRoot runs the container under ptrace. Works
  on any Linux, slower, and creation copies the image rootfs.

## Install

    pip install xcodon-runtime            # or: uv pip install xcodon-runtime
    xrunner info                            # shows the engine and probe results

Optional: `pip install 'xcodon-runtime[zstd]'` for zstd-compressed layers.
On aarch64 hosts without user namespaces, provide a PRoot binary with
`XCODON_PROOT=/path/to/proot`. The command is `xrunner`; the package and its Python module keep the name xcodon-runtime.
## Use

    xrunner pull python:3.12-slim
    xrunner run --rm -v $PWD:/work -w /work python:3.12-slim python -c 'print("hi")'
    xrunner create --name dev python:3.12-slim
    xrunner start dev
    xrunner exec dev pip install numpy      # persists in the container's writable layer
    xrunner exec dev python -c 'import numpy'
    xrunner stop dev && xrunner rm dev
    xrunner logs dev                        # the ns engine's keeper log
    xrunner rmi python:3.12-slim            # drop a tag, and the image with its last tag
    xrunner prune                           # leftovers: half-built dirs and orphan blobs
    xrunner prune --all                     # also unused layers and day-old exited containers

    xrunner run --rm --env-dir $PWD/.xrunner-env python:3.12-slim pip install numpy
    xrunner run --rm --env-dir $PWD/.xrunner-env python:3.12-slim python -c 'import numpy'

`--env-dir` keeps the container's writable layer in a host folder, keyed by
image id, so tools installed in one container are there for the next one.
Delete `<env-dir>/<image-id>` to reset. coala-runtime uses this through the
`XRUNNER_ENV_DIR` variable.

Two global flags come before the subcommand: `--engine ns|proot` forces an
engine, and `--home DIR` picks the state directory for this one command.

Images already in a local Docker daemon are reused through `docker save`, so
locally built images work without a registry. xrunner records the daemon's
image id at import. When the daemon's tag later points at another image
(for example after a `docker build` there), the next `run`, `create`, or
`build` that uses the tag imports it again. `--pull never` and image ids
skip this check.

## Build and commit

    xrunner build -t myapp:1 .                          # runs a Dockerfile subset
    xrunner commit -m "installed numpy" dev myapp:2      # snapshot a stopped container
    xrunner tag myapp:2 myapp:latest

`xrunner build` runs each Dockerfile instruction in its own container and
commits one image per step, so unchanged steps are cached. The cache lives
in `<home>/build-cache.json`. The step images are untagged, and
`xrunner prune --all` removes untagged images, which empties the cache in
effect. `xrunner commit` also works on an `--env-dir` folder instead of a
container: `xrunner commit --env-dir $PWD/.xrunner-env --image myapp:1 myapp:2`.

Docker-style names work too:

    xrunner image inspect myapp:1                       # also: image ls, image rm
    xrunner docker build -t myapp:1 .                   # any docker verb xrunner supports
    xrunner docker image inspect --format '{{.Id}}' myapp:1

`xrunner docker VERB ...` takes docker's own verbs and flags for `build`,
`image inspect|ls|rm`, `images`, `rmi`, `tag`, `pull`, `run`, `create`,
`start`, `exec`, `stop`, `rm`, `ps`, `logs`, `commit`, `inspect`, `version`, and
`info`. Other verbs exit with code 125 and a message.

For tools that shell out to a real `docker` binary directly — running
`docker build` or `docker image inspect` as a subprocess, rather than going
through cwltool's `--user-space-docker-cmd` — `xrunner shim install` writes
a `docker` script that forwards every call to `xrunner docker`:

    xrunner shim install --dir ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
    docker build -t myapp:1 .
    docker image inspect myapp:1

The shim refuses to install while any real `docker` is anywhere on PATH,
even outside DIR, unless you pass `--force`. It also refuses to replace a
`DIR/docker` that is not an xrunner shim unless you pass `--force`, and it
never replaces a directory. xrunner also never mistakes its own shim for a
real Docker daemon: image pulls that would reuse a local `docker save` skip
a shim.

Limits on the Dockerfile subset: no multi-stage builds (a second `FROM`, or
`--from=`), no `.dockerignore`, and no remote `ADD`/`COPY` from a URL.

## Hosts without docker

On a host with no docker daemon, a `FROM` name that only ever existed in a
local daemon, such as `coala-runtime-python:latest`, is looked up on Docker
Hub and not found there. Seed such base images once: pull the published
image and give it the local name.

    xrunner pull hubentu/coala-runtime-python:latest
    xrunner tag hubentu/coala-runtime-python:latest coala-runtime-python:latest
    xrunner pull hubentu/coala-runtime-r:latest
    xrunner tag hubentu/coala-runtime-r:latest coala-runtime-r:latest

After that, `FROM coala-runtime-python:latest` uses the stored image and
never goes to the network. xrunner does not map image names itself.

## Environment variables

| Variable | Meaning |
|---|---|
| `XCODON_RUNTIME_HOME` | State directory. Default `~/.xcodon/runtime`. |
| `XCODON_ENGINE` | `ns` or `proot`. Skips probing. |
| `XCODON_PROOT` | Path to a PRoot binary. |
| `XCODON_PROOT_ARGS` | Extra PRoot flags, for example `-k 5.15.0`. |
| `XCODON_LOG` | `info` or `debug`. |

## Limits

- One uid inside the container. Every image file is owned by that uid.
  `chown` to another user fails. Setuid binaries do not elevate.
- Host network only. No `--net=none`, no port mapping.
- No cgroups. `--memory` and `--cpus` are accepted and ignored with a warning.
- No GPU passthrough.
- Read-only binds apply to the top mount only; submounts under a bound host
  path stay writable.

## coala

In coala, add one branch to `configure_container_runner`:

```python
    if container_runner == "xrunner":
        runtime_context.user_space_docker_cmd = shutil.which("xrunner") or "xrunner"
```

cwltool then calls `xrunner inspect`, `xrunner pull`, and `xrunner run` with
docker-style flags.

## coala-runtime

xcodon-runtime ships `xcodon_runtime.coala_adapter.XcodonContainerManager`,
which implements coala-runtime's `ContainerManager` interface. In
coala-runtime, add `XRUNNER = "xrunner"` to `ContainerEngine`, return the
adapter from `make_container_manager` for that value, and try it in
autodetection after Docker and Podman and before Apptainer.

## Development

    uv venv .venv && . .venv/bin/activate
    uv pip install -e ".[dev,zstd]"
    python scripts/fetch_proot.py          # only if _bin/ is missing
    pytest -q                              # unit + ns + proot on a capable host
    XCODON_TEST_NETWORK=1 pytest -m network

Design: `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`.
