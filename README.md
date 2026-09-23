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
    xcodon info                            # shows the engine and probe results

Optional: `pip install 'xcodon-runtime[zstd]'` for zstd-compressed layers.
On aarch64 hosts without user namespaces, provide a PRoot binary with
`XCODON_PROOT=/path/to/proot`. The command is also installed as
`xcodon-runtime` for hosts where another program is already named `xcodon`.

## Use

    xcodon pull python:3.12-slim
    xcodon run --rm -v $PWD:/work -w /work python:3.12-slim python -c 'print("hi")'
    xcodon create --name dev python:3.12-slim
    xcodon start dev
    xcodon exec dev pip install numpy      # persists in the container's writable layer
    xcodon exec dev python -c 'import numpy'
    xcodon stop dev && xcodon rm dev
    xcodon logs dev                        # the ns engine's keeper log
    xcodon rmi python:3.12-slim            # drop a tag, and the image with its last tag
    xcodon prune                           # leftovers: half-built dirs and orphan blobs
    xcodon prune --all                     # also unused layers and day-old exited containers

Two global flags come before the subcommand: `--engine ns|proot` forces an
engine, and `--home DIR` picks the state directory for this one command.

Images already in a local Docker daemon are reused through `docker save`, so
locally built images work without a registry.

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
    if container_runner == "xcodon":
        runtime_context.user_space_docker_cmd = shutil.which("xcodon") or "xcodon"
```

cwltool then calls `xcodon inspect`, `xcodon pull`, and `xcodon run` with
docker-style flags.

## coala-runtime

xcodon-runtime ships `xcodon_runtime.coala_adapter.XcodonContainerManager`,
which implements coala-runtime's `ContainerManager` interface. In
coala-runtime, add `XCODON = "xcodon"` to `ContainerEngine`, return the
adapter from `make_container_manager` for that value, and try it in
autodetection after Docker and Podman and before Apptainer.

## Development

    uv venv .venv && . .venv/bin/activate
    uv pip install -e ".[dev,zstd]"
    python scripts/fetch_proot.py          # only if _bin/ is missing
    pytest -q                              # unit + ns + proot on a capable host
    XCODON_TEST_NETWORK=1 pytest -m network

Design: `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`.
