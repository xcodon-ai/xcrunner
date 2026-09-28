# xcrunner

A rootless container runtime for Docker images. It runs the images that
coala and coala-runtime use on hosts without Docker and without root.

Two engines, chosen automatically:

- **ns**: kernel user namespaces plus overlayfs, no helper binary. Native
  speed. Needs a Linux kernel 5.11 or newer with unprivileged user namespaces
  enabled. Most workstations, cloud VMs, and current HPC nodes qualify.
- **proot**: a vendored static PRoot runs the container under ptrace. Works
  on any Linux, slower, and creation copies the image rootfs.

## Install

    pip install xcrunner                # or: uv pip install xcrunner
    xcrunner info                            # shows the engine and probe results

Optional: `pip install 'xcrunner[zstd]'` for zstd-compressed layers.
On aarch64 hosts without user namespaces, provide a PRoot binary with
`XCODON_PROOT=/path/to/proot`. The command is `xcrunner`. The package on PyPI is `xcrunner`, and the Python module is `xcodon_runtime`.
## Use

    xcrunner pull python:3.12-slim
    xcrunner run --rm -v $PWD:/work -w /work python:3.12-slim python -c 'print("hi")'
    xcrunner create --name dev python:3.12-slim
    xcrunner start dev
    xcrunner exec dev pip install numpy      # persists in the container's writable layer
    xcrunner exec dev python -c 'import numpy'
    xcrunner stop dev && xcrunner rm dev
    xcrunner logs dev                        # the ns engine's keeper log
    xcrunner rmi python:3.12-slim            # drop a tag, and the image with its last tag
    xcrunner prune                           # leftovers: half-built dirs and orphan blobs
    xcrunner prune --all                     # also unused layers and day-old exited containers

    xcrunner run --rm --env-dir $PWD/.xcrunner-env python:3.12-slim pip install numpy
    xcrunner run --rm --env-dir $PWD/.xcrunner-env python:3.12-slim python -c 'import numpy'

`--env-dir` keeps the container's writable layer in a host folder, keyed by
image id, so tools installed in one container are there for the next one.
Delete `<env-dir>/<image-id>` to reset. coala-runtime uses this through the
`XCRUNNER_ENV_DIR` variable.

Two global flags come before the subcommand: `--engine ns|proot` forces an
engine, and `--home DIR` picks the state directory for this one command.

Images already in a local Docker daemon are reused through `docker save`, so
locally built images work without a registry. xcrunner records the daemon's
image id at import. When the daemon's tag later points at another image
(for example after a `docker build` there), the next `run`, `create`, or
`build` that uses the tag imports it again. `--pull never` and image ids
skip this check.

## Build and commit

    xcrunner build -t myapp:1 .                          # runs a Dockerfile subset
    xcrunner commit -m "installed numpy" dev myapp:2      # snapshot a stopped container
    xcrunner tag myapp:2 myapp:latest

`xcrunner build` runs each Dockerfile instruction in its own container and
commits one image per step, so unchanged steps are cached. The cache lives
in `<home>/build-cache.json`. The step images are untagged, and
`xcrunner prune --all` removes untagged images, which empties the cache in
effect. `xcrunner commit` also works on an `--env-dir` folder instead of a
container: `xcrunner commit --env-dir $PWD/.xcrunner-env --image myapp:1 myapp:2`.

Docker-style names work too:

    xcrunner image inspect myapp:1                       # also: image ls, image rm
    xcrunner docker build -t myapp:1 .                   # any docker verb xcrunner supports
    xcrunner docker image inspect --format '{{.Id}}' myapp:1

`xcrunner docker VERB ...` takes docker's own verbs and flags for `build`,
`image inspect|ls|rm`, `images`, `rmi`, `tag`, `pull`, `run`, `create`,
`start`, `exec`, `stop`, `rm`, `ps`, `logs`, `commit`, `inspect`, `version`, and
`info`. Other verbs exit with code 125 and a message.

For tools that shell out to a real `docker` binary directly — running
`docker build` or `docker image inspect` as a subprocess, rather than going
through cwltool's `--user-space-docker-cmd` — `xcrunner shim install` writes
a `docker` script that forwards every call to `xcrunner docker`:

    xcrunner shim install --dir ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
    docker build -t myapp:1 .
    docker image inspect myapp:1

The shim refuses to install while any real `docker` is anywhere on PATH,
even outside DIR, unless you pass `--force`. It also refuses to replace a
`DIR/docker` that is not an xcrunner shim unless you pass `--force`, and it
never replaces a directory. xcrunner also never mistakes its own shim for a
real Docker daemon: image pulls that would reuse a local `docker save` skip
a shim.

Limits on the Dockerfile subset: no multi-stage builds (a second `FROM`, or
`--from=`), no `.dockerignore`, and no remote `ADD`/`COPY` from a URL.

## Tools without conda

Agents often install command-line tools with `conda create`, `conda install` and
`conda run`. On a host with no conda, xcrunner can answer those calls itself:

    xcrunner shim install conda --dir ~/.xcodon/shim
    export PATH="$HOME/.xcodon/shim:$PATH"

This downloads a pinned micromamba (2.9.0, from conda-forge, checksum-verified) to
`<xcrunner home>/bin/micromamba-2.9.0/micromamba` and writes `conda`, `mamba` and
`micromamba` scripts that forward to `xcrunner conda`.
Offline, pass `--micromamba PATH` to use a binary you already have.

- Environments live in the project's `.xcrunner-env/conda` (found from the working
  directory, or from `XCRUNNER_ENV_DIR`), else under the xcrunner home. Downloads are
  cached once under the xcrunner home. A `.xcrunner-env` found by searching upward is
  used only when you own it and no one else can write to it.
- Launchers should export `XCRUNNER_ENV_DIR=<project>/.xcrunner-env`, or create
  `<project>/.xcrunner-env`, before the agent starts. Otherwise an early conda call
  falls back to the xcrunner home: its envs land outside the project, `conda env
  list` in the project does not show them, and the agent's `run_shell` guardrail
  refuses their paths.
- A relative `-p PATH` is always a folder under the working directory, even
  without a `/`.
- Your own `~/.conda` and `~/.condarc` are never read or written.
- `conda run -n NAME CMD` runs CMD with the env on PATH; `conda activate` is not
  supported, because it changes the calling shell.
- Each env gets `conda-explicit.txt`, listing every package URL and checksum, so it
  can be rebuilt with `conda create -p PATH --file conda-explicit.txt`.
- The shim refuses to install while a real conda, mamba or micromamba is on PATH,
  unless you pass `--force`.
- The shims use the default xcrunner home, or `XCODON_RUNTIME_HOME` when it is set;
  they do not remember an `xcrunner --home H` given at install time. After
  `xcrunner --home H shim install conda ...`, also export `XCODON_RUNTIME_HOME=H`
  wherever the shims run, or they will look for micromamba in the default home
  instead.

## Sandbox activation

`xcrunner sandbox activate /abs/project/.xcrunner-env` writes `docker`, `conda`,
`mamba` and `micromamba` shims into the project's `.xcrunner-env/bin` and prints
the two `export` lines (PATH first, then `XCRUNNER_ENV_DIR`) that send a shell's
docker and conda calls to xcrunner. The Python API is
`xcodon_runtime.sandbox.activate(env_dir)`. opencodon calls it by itself when
its container engine is `xcrunner`, so `--container-engine xcrunner` is enough.

## Environment record

xcrunner keeps `.xcrunner-env/environment.json`, a per-project record of the images a
project used (id, source, registry digests, and any build Dockerfile) and the
packages each install added, changed or removed.

For each image it also keeps the platform, package counts, and for built images
the packages the build added. `.xcrunner-env/packages/<image id>.json` holds the full
package list of each image the project used, not only the changes.

The record updates automatically after conda changes and when containers stop.
`xcrunner env show` prints it, `xcrunner env show --json` prints the file, and
`xcrunner env record` rebuilds it from what is on disk. Images keep their entries as
history; `env record` never removes one.

It is a record, not a restore mechanism: it does not recreate images or packages,
only describes what happened. Images built before this feature have no Dockerfile
on record. On the proot engine each container stop rescans the rootfs copy, which
takes about 3-4 s on large images.

## Hosts without docker

On a host with no docker daemon, a `FROM` name that only ever existed in a
local daemon, such as `coala-runtime-python:latest`, is looked up on Docker
Hub and not found there. Seed such base images once: pull the published
image and give it the local name.

    xcrunner pull hubentu/coala-runtime-python:latest
    xcrunner tag hubentu/coala-runtime-python:latest coala-runtime-python:latest
    xcrunner pull hubentu/coala-runtime-r:latest
    xcrunner tag hubentu/coala-runtime-r:latest coala-runtime-r:latest

After that, `FROM coala-runtime-python:latest` uses the stored image and
never goes to the network. xcrunner does not map image names itself.

## Clusters with shared storage

Overlayfs cannot write its upper layer to NFS, Lustre or GPFS. When the home is on
shared storage, the overlay probe fails and xcrunner falls back to the slower proot
engine. Keep the image store on shared storage and put container folders on a
node-local disk:

    export XCODON_RUNTIME_HOME=/shared/$USER/xcrunner
    export XCRUNNER_CONTAINER_DIR=${TMPDIR:-/tmp}/xcrunner-containers

Images are pulled once into the shared home and read from there. Each container's
writable layer, its keeper log and its locks live under `XCRUNNER_CONTAINER_DIR`.
Containers are then local to one node: `xcrunner ps` on another node does not list
them. `xcrunner info` shows both folders and the engine the probes chose.

The setting does not move an env folder's layers. On the ns engine they must also be
on a local disk. Set `XCRUNNER_ENV_LAYER_DIR` to node-local storage: the record stays
in the project's env folder, and each node keeps its own layers for as long as that
disk lasts. Or set `XCODON_ENGINE=proot` to keep the layers on shared storage.

## macOS

On an Apple silicon Mac with macOS 26 or newer, xcrunner runs the Linux xcrunner
inside one Lima VM. Install Lima first:

    brew install lima
    pip install xcrunner
    xcrunner machine start        # the first start creates the VM and takes a few minutes
    xcrunner run --rm alpine echo hi

- The VM is a Lima `vz` VM named `xcrunner` with Ubuntu 24.04 and Rosetta. Images,
  layers and containers live on its own disk.
- Your home folder, `/private/var/folders` and `/private/tmp` are shared into the VM
  at the same paths, so bind mounts and cwltool's temp folders work unchanged. Work
  in one of those folders, or add more with `XCRUNNER_MACHINE_MOUNTS` before the VM is
  created.
- `conda`, `shim`, `sandbox` and `machine` run on the Mac. Every other command runs
  in the VM. The conda shim installs macOS programs with a macOS micromamba.
- Env folder layers live on the VM disk; the record in `.xcrunner-env` stays with the
  project. `xcrunner info` shows where the layers are.
- x86_64-only images, such as most biocontainers, run through Rosetta.
- `xcrunner machine status|stop|shell|rm` manage the VM. `rm` deletes its disk,
  with all images and env layers.

`scripts/mac_smoke.sh` runs the end-to-end checks on a Mac and writes a report to
`~/xcrunner-mac-smoke.txt`.

GitHub's macOS runners cannot start VMs, so CI's `lima-linux` job runs the same
script on Linux against a real Lima QEMU VM. The test-only setting
`XCRUNNER_MACHINE_LINUX_TEST=1` makes a Linux host act like the Mac side. That VM is
x86_64 without Rosetta, and it shares only the home folder and
`XCRUNNER_MACHINE_MOUNTS`, so Apple's VM framework and Rosetta are still tested only
on a Mac.

## Environment variables

| Variable | Meaning |
|---|---|
| `XCODON_RUNTIME_HOME` | State directory. Default `~/.xcodon/runtime`. |
| `XCRUNNER_CONTAINER_DIR` | Container folders. Default `<home>/containers`. |
| `XCRUNNER_ENV_LAYER_DIR` | Env folder layers on another disk. Default: in the env folder. On a cluster, a node-local disk gives each node its own layers. |
| `XCRUNNER_MACHINE_NAME` | macOS: the Lima VM's name. Default `xcrunner`. |
| `XCRUNNER_MACHINE_CPUS`, `XCRUNNER_MACHINE_MEMORY`, `XCRUNNER_MACHINE_DISK` | macOS: VM size when it is created. Defaults `4`, `4GiB`, `100GiB`. |
| `XCRUNNER_MACHINE_MOUNTS` | macOS: extra Mac folders to share, separated by `:`. |
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
- macOS: Apple silicon and macOS 26 or newer only, through a Lima VM.
- Read-only binds apply to the top mount only; submounts under a bound host
  path stay writable.

## coala

In coala, add one branch to `configure_container_runner`:

```python
    if container_runner == "xcrunner":
        runtime_context.user_space_docker_cmd = shutil.which("xcrunner") or "xcrunner"
```

cwltool then calls `xcrunner inspect`, `xcrunner pull`, and `xcrunner run` with
docker-style flags.

## coala-runtime

xcodon-runtime ships `xcodon_runtime.coala_adapter.XcodonContainerManager`,
which implements coala-runtime's `ContainerManager` interface. In
coala-runtime, add `XCRUNNER = "xcrunner"` to `ContainerEngine`, return the
adapter from `make_container_manager` for that value, and try it in
autodetection after Docker and Podman and before Apptainer.

## Development

    uv venv .venv && . .venv/bin/activate
    uv pip install -e ".[dev,zstd]"
    python scripts/fetch_proot.py          # only if _bin/ is missing
    pytest -q                              # unit + ns + proot on a capable host
    XCODON_TEST_NETWORK=1 pytest -m network
