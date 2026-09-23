# xcodon-runtime design

Date: 2026-09-23
Status: approved design, ready for implementation planning

## 1. Goal

xcodon-runtime is a rootless container runtime for Docker images. It runs the
images that coala and coala-runtime already use, on hosts where Docker is not
available and the user has no root. It works on two kinds of hosts:

- Linux with unprivileged user namespaces (workstations, cloud VMs, most
  current HPC nodes). Uses kernel namespaces and overlayfs directly, with no
  helper binary.
- Linux with user namespaces disabled (locked-down HPC). Uses a vendored PRoot
  binary and ptrace.

It plugs into coala through cwltool's user-space docker command and into
coala-runtime through its `ContainerManager` interface.

### In scope

- Pull images anonymously from public registries (Docker Hub, quay.io,
  ghcr.io).
- Reuse images from a local Docker daemon through `docker save`.
- Honor image config: ENTRYPOINT, CMD, ENV, WORKDIR, USER.
- A writable container layer that persists across repeated exec calls.
- Bind mounts, working directory, and environment overrides.
- A docker-compatible CLI subset and a Python API.

### Out of scope

- Network isolation. Containers use the host network.
- GPU passthrough.
- cgroups and resource limits. `--memory` and `--cpus` are accepted and
  ignored with a warning.
- Private registry credentials.
- Loading `docker save` tarballs from files. Only the daemon path is needed
  now. The loader is the same, so adding a file path later is small.
- macOS and Windows.
- Multiple uids inside one container. See section 6.

## 2. Architecture

One Python package, `xcodon_runtime`, with a console script `xcodon`. All state
lives under one directory, default `~/.xcodon/runtime`, overridable with
`XCODON_RUNTIME_HOME`.

Five components. Each is a module with one job.

| Component | Job |
|---|---|
| Image store | Turn an image reference into a stored image: config JSON plus an ordered list of layer directories plus a flattened rootfs. |
| Container store | Create, track, and remove containers. A container is a directory. |
| Engines | Run processes inside a container. Two implementations behind one interface. |
| Spec builder | Merge image config with run options into argv, env, workdir, uid, gid. |
| Interfaces | CLI, Python API, and a `ContainerManager` adapter for coala-runtime. |

Data flow for `xcodon run IMAGE CMD`:

1. Image store resolves IMAGE, pulling if needed.
2. Container store creates a container directory.
3. Spec builder computes the process spec.
4. The engine starts the keeper (ns engine only) and execs the spec with
   inherited stdio.
5. The exit code is returned. With `--rm` the container is removed.

Dependencies: Python 3.10 or newer, Linux. No required third-party Python
packages. Optional extra `zstd` installs `zstandard` for zstd-compressed
layers. The ns engine needs only the kernel. PRoot static binaries for the
proot engine are vendored in the wheel.

## 3. Image store

### 3.1 Reference resolution

References are normalized like docker does:

- `python:3.12` becomes `docker.io/library/python:3.12`.
- `xcodon/foo` becomes `docker.io/xcodon/foo:latest`.
- `quay.io/biocontainers/samtools:1.20--h50ea8bc_0` passes through.
- `name@sha256:<hex>` is a digest reference.

Lookup order for a name:

1. The local store (`refs.json`).
2. A local Docker daemon, when `docker` is on PATH and `docker version`
   succeeds.
3. The registry.

`--pull=always` skips step 1. `xcodon pull` runs steps 2 and 3.

### 3.2 Sources

Both loaders produce the same result: a config blob and an ordered list of
layer blobs with their media types and diff ids.

**Registry loader.** Standard library `urllib` only.

- GET the manifest with Accept headers for OCI index, OCI manifest, Docker
  manifest list, and Docker manifest v2.
- On 401, parse `WWW-Authenticate`, fetch an anonymous bearer token from the
  named realm with the given service and scope, and retry.
- If the result is an index, pick the entry for `linux/<host arch>`.
  Fail with a clear message when none matches.
- Download config and layers to `blobs/sha256/<hex>.part`, verify the SHA-256
  digest, rename to the final name.
- Supported layer media types: tar+gzip and plain tar. tar+zstd needs the
  `zstandard` module, else the pull fails with a message naming the extra.

**Daemon loader.** Runs `docker save REF` and streams the tar. The archive is
an OCI layout: `index.json`, `oci-layout`, `blobs/sha256/*`. The top-level
index may point to a nested index, so the loader follows one level of
indirection and then picks the platform manifest as above. Layers here are
uncompressed tar. The legacy `manifest.json` in the same archive is ignored.

### 3.3 Storage layout

```
<runtime home>/
  blobs/sha256/<hex>         downloaded blobs, deleted after extraction
  layers/<diffid-hex>/       one extracted layer, shared across images
  images/<imageid-hex>/      config.json, manifest.json, rootfs/
  refs.json                  "docker.io/library/python:3.12" -> image id
  containers/<id>/           see section 4
  locks/                     flock files for layers and images
```

The image id is the SHA-256 digest of the config blob, as in docker. Layers are
keyed by diff id, the digest of the uncompressed tar, so a gzip layer from a
registry and the same plain layer from `docker save` share one directory.

### 3.4 Layer extraction

Each layer is extracted once, into `layers/<diffid>.tmp/`, then renamed.
Extraction uses Python's `tarfile` with the `tar` filter. Effects:

- Files are owned by the invoking user. Ownership from the tar is ignored,
  because changing it needs privilege.
- Setuid, setgid, and sticky bits are dropped. Group and other write bits are
  dropped. Inside the sandbox there is one uid, so this loses nothing.
- Absolute and relative symlinks are recreated as is.
- Writing through a symlink that points outside the layer directory is
  refused. Entries with `..` components are refused.
- Device nodes, sockets, and fifos raise an error that is caught and logged.
  The entry is skipped.
- Hardlinks inside a layer are preserved.
- Whiteout markers (`.wh.<name>` and `.wh..wh..opq`) are extracted as plain
  files and left in place.

### 3.5 Flattened rootfs

After all layers exist, the image gets `images/<id>/rootfs/`, built into a
`.tmp` sibling and renamed. The builder walks layers in order:

- A directory entry creates the directory if missing and applies its mode.
- A regular file is hardlinked from the layer directory. If the layer is on a
  different filesystem from the image directory, the file is copied.
- A symlink is recreated. An existing entry at that path is removed first.
- `.wh.<name>` removes `<name>` from the rootfs tree, recursively for
  directories.
- `.wh..wh..opq` in a directory removes everything under that directory that
  came from earlier layers before this layer's entries are applied.

The result is a complete rootfs. The ns engine uses it as the single overlay
lower directory. The proot engine copies it. Because files are hardlinks into
shared layers, twenty derived images pay for a common base layer once.

Overlayfs never writes to a lower directory. Copy-up creates a new inode in
the upper layer, so the shared hardlinked files are never modified through the
ns engine. The proot engine works on a copy, so it never modifies them either.

### 3.6 Concurrency

cwltool runs jobs in parallel. Every pull holds `flock` on
`locks/image-<id>` while it works, and each layer extraction holds
`locks/layer-<diffid>`. A process that waits on a lock re-checks whether the
result exists after acquiring it and skips the work when it does.

### 3.7 Inspect

`xcodon inspect IMAGE` prints a JSON array with one object:

```json
[{"Id": "sha256:...", "RepoTags": ["..."], "Architecture": "amd64", "Os": "linux",
  "Config": { ...the OCI image config "config" object... },
  "RootFS": {"Type": "layers", "Layers": ["sha256:...", ...]}}]
```

Exit code 1 and an empty array when the image is not in the store. cwltool
only parses the output as JSON and checks the exit code.

## 4. Containers and engines

### 4.1 Container directory

```
containers/<id>/
  config.json    image id, image ref, engine, argv, env, workdir, uid, gid,
                 binds, name, created time, state
  upper/  work/  writable layer and overlay work dir          (ns engine)
  merged/        overlay mount point, empty when not running    (ns engine)
  rootfs/        full copy of the image rootfs                (proot engine)
  keeper.pid     pid and start time of the sandbox init       (ns engine)
  keeper.log     keeper stderr                                (ns engine)
```

The container id is 64 hex characters from `os.urandom`. Any unique prefix is
accepted on the command line. `state` is one of `created`, `running`,
`exited`.

### 4.2 Spec builder

Input: image config, plus options command, entrypoint, env, workdir, user.
Output: argv, env, workdir, uid, gid.

- argv: if `--entrypoint` is given it replaces the image ENTRYPOINT. If a
  command is given it replaces the image CMD. argv is entrypoint followed by
  cmd. Empty argv is an error.
- env: start from image ENV. Set `HOSTNAME` to the first 12 characters of the
  container id. If `HOME` is missing, look up the uid in the rootfs
  `/etc/passwd`; fall back to `/root` for uid 0 and `/` otherwise. Apply
  `--env` overrides last. If `PATH` is still missing, set the docker default
  `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
- workdir: `--workdir`, else image WORKDIR, else `/`. The directory is created
  in the writable layer if missing, as docker does.
- uid and gid: `--user`, else image USER, else `0:0`. Names are resolved
  through the rootfs `/etc/passwd` and `/etc/group`. A `uid` without a group
  uses the passwd entry's gid, else the same number.

### 4.3 Engine interface

```python
class Engine(Protocol):
    name: str
    def start(self, container: Container) -> None: ...
    def exec(self, container: Container, argv: list[str], env: dict[str, str],
             workdir: str, stdin, stdout, stderr) -> int: ...
    def stop(self, container: Container) -> None: ...
```

`start` applies the bind mounts recorded in the container config. Both engines
bind the host `/etc/resolv.conf` and `/etc/hosts` read-only, because there is
no network namespace and DNS must keep working.

### 4.4 ns engine

The keeper is a Python process that builds the sandbox with system calls and
then blocks inside it forever. It uses ctypes for `unshare`, `mount`,
`umount2`, `pivot_root`, `sethostname`, and `setns`. No external binary is
involved.

`start` forks the keeper. The keeper, in order:

1. Calls `unshare(CLONE_NEWUSER | CLONE_NEWNS)`.
2. Writes `deny` to `/proc/self/setgroups`, then `<uid> <host uid> 1` to
   `/proc/self/uid_map` and `<gid> <host gid> 1` to `/proc/self/gid_map`.
   This single-line self mapping needs no `newuidmap` and no capabilities.
3. Makes mount propagation private with `mount(NULL, "/", NULL,
   MS_REC | MS_PRIVATE)`, so nothing it mounts leaks to the host.
4. Mounts overlayfs on `containers/<id>/merged` with the image rootfs as
   `lowerdir`, `upper/` as `upperdir`, and `work/` as `workdir`. The kernel
   allows this inside a user namespace since 5.11.
5. Calls `unshare(CLONE_NEWPID | CLONE_NEWUTS)` and forks. The parent writes
   `pid <child pid>` to the info pipe and exits. The child is pid 1 of the new
   pid namespace and continues. It writes `ready` to the same pipe only after
   step 11, so `start` never returns while mounts are half built.
6. Creates a directory with a random name under `merged`, calls
   `pivot_root(merged, <that directory>)`, and changes directory to `/`.
   From here on every target path is inside the new root, so a symlink
   planted in the image can only resolve inside the sandbox, never onto the
   host. The old root stays mounted at that random-named directory and is the
   source prefix for the host binds below. A user bind whose target lies under
   that directory is refused.
7. Mounts a tmpfs on `/dev`, then bind-mounts the old root's `/dev/null`,
   `/dev/zero`, `/dev/full`, `/dev/random`, `/dev/urandom`, and `/dev/tty`
   onto empty files inside it. Mounts `devpts` on `/dev/pts` with
   `newinstance,ptmxmode=0666,mode=0620` and symlinks `ptmx` to `pts/ptmx`.
   Mounts a tmpfs on `/dev/shm`. Symlinks `fd`, `stdin`, `stdout`, and
   `stderr` to `/proc/self/fd` entries.
8. Mounts `proc` on `/proc`. This is allowed because the keeper owns a fresh
   pid namespace.
9. Bind-mounts the old root's `/sys` read-only on `/sys`. A read-only
   remount inside a user namespace must repeat the source mount's locked
   flags (nosuid, nodev, noexec, atime flags), read from
   `/proc/self/mountinfo`, or the kernel refuses it with EPERM.
10. Bind-mounts the old root's `/etc/resolv.conf` and `/etc/hosts` read-only,
    then each bind from the container config with its source under the old
    root, read-only when asked. Missing mount points are created in the
    writable layer first, files for files and directories for directories,
    replacing an entry of the wrong type. Creates the working directory.
11. Unmounts the old root with `MNT_DETACH`, removes its directory, and sets
    the hostname to the first 12 characters of the container id.
12. Installs a SIGCHLD handler that reaps every zombie with
    `waitpid(-1, WNOHANG)`, installs a SIGTERM handler that exits, and blocks
    in `signal.pause()` in a loop. Its stdout and stderr were pointed at
    `keeper.log` by `start` when the process was launched.

The keeper keeps running after `pivot_root` even though the new root has no
Python. Its code and libraries are already mapped in memory, but nothing
may be imported lazily after the pivot, so the keeper warms the one codec the
mount helpers need before step 6. The launcher opens no descriptors for the
keeper other than the log and the info pipe.

`start` reads the pid line and waits for the `ready` line, then records the pid in
`keeper.pid` with its start time from `/proc/<pid>/stat`. `start` fails with
the keeper's log if the pipe closes before a pid arrives. Because the keeper
is pid 1 of its pid namespace, killing it kills every process in the
sandbox, and the overlay unmounts when the last process leaves the mount
namespace.

`exec` forks a child. The child opens all four of
`/proc/<keeper pid>/ns/{user,mnt,pid,uts}` first, because after entering the
mount namespace `/proc` is the sandbox's own, then calls `setns` on each in
that order, forks again so the grandchild is inside
the pid namespace, changes to `workdir`, and calls `execve(argv, env)`. The
parent waits and returns the exit code. Stdio is inherited, so stdin, stdout,
and stderr stream through. A missing `/proc/<pid>` or `ESRCH` from `setns`
raises `ContainerNotRunning` and marks the container `exited`. The exec child
sets `PR_SET_NO_NEW_PRIVS` before `execve` so a setuid binary in the image
cannot change credentials.

`stop` sends SIGTERM to the keeper, waits up to two seconds, then SIGKILL.

Liveness: a container is running when `keeper.pid` names a live process whose
start time in `/proc/<pid>/stat` matches the recorded one. This survives pid
reuse after a reboot.

### 4.5 proot engine

`start` copies the image rootfs into `containers/<id>/rootfs/` with
`cp -a --reflink=auto`. On XFS and Btrfs this is a metadata operation. On
ext4 and network filesystems it is a full copy, and the log says so once.
Nothing stays running after `start`.

`exec` runs the vendored PRoot directly:

```
proot -r <rootfs> -w <workdir> -i <uid>:<gid>
      -b /dev -b /proc -b /sys -b /etc/resolv.conf -b /etc/hosts
      [-b <host>:<guest> ...]
      argv...
```

Environment is passed through the process environment. Writes go straight
into `rootfs/`, which is the persistent layer. `stop` is a no-op.

PRoot lookup order: `XCODON_PROOT` if set, else `proot` on PATH, else the
vendored `xcodon_runtime/_bin/proot-x86_64`. The PRoot project publishes a
static build only for x86_64, so aarch64 hosts must supply their own binary
through `XCODON_PROOT` or PATH. `XCODON_PROOT_ARGS` appends extra flags,
which lets a user turn seccomp acceleration off on kernels where it
misbehaves.

### 4.6 Engine selection

Computed on each invocation unless `XCODON_ENGINE=ns|proot` is set. The ns
engine is selected when all three probes pass, each in a short-lived child
process that unshares user and mount namespaces:

1. `unshare(CLONE_NEWUSER | CLONE_NEWNS)` succeeds and the uid map can be
   written.
2. An overlayfs mount succeeds inside that namespace on a temporary
   directory.
3. `unshare(CLONE_NEWPID)` followed by a `proc` mount succeeds. Some
   hardened kernels allow user namespaces but not this.

Otherwise the proot engine is selected, and one log line at info level names
the failed probe. `xcodon info` prints all probe results. A container records
its engine at create time and always uses it, so a running keeper is never
orphaned by a changed decision.

## 5. Interfaces

### 5.1 CLI

Docker-compatible subset. Unknown flags fail with a message that names the
flag. `--memory`, `--cpus`, `--cpu-shares`, `--gpus`, `--net`, `--network`,
`--read-only`, and `--log-driver` are accepted and ignored with a warning,
because cwltool or docker-py callers may pass them.

```
xcodon pull [--platform P] IMAGE
xcodon inspect IMAGE
xcodon images
xcodon rmi IMAGE
xcodon run [--mount=type=bind,source=S,target=T[,readonly]] [-v|--volume S:T[:ro|:rw]]
           [--workdir=D] [-w D] [--env=K=V] [-e K=V] [--entrypoint=E] [--user=U] [-u U]
           [--name=N] [--rm] [-i] [--pull=missing|always] IMAGE [CMD...]
xcodon create  (same options as run, minus --rm and -i)  IMAGE [CMD...]
xcodon start ID
xcodon exec [--workdir=D] [--env=K=V] ID CMD...
xcodon stop ID
xcodon rm [-f] ID
xcodon ps [-a]
xcodon logs ID
xcodon info
xcodon prune
```

`--mount` values are parsed as CSV, matching what cwltool emits.
`--user` maps the host user to the given uid and gid, the same mechanism as
USER. `-i` is accepted and stdin always passes through. `--cidfile` is
honored by writing the container id, because it costs nothing.

Exit codes follow docker: 125 for a runtime error, 126 when the command
cannot be executed, 127 when the command is not found, otherwise the exit
code of the process.

### 5.2 Python API

`xcodon_runtime.api`, synchronous:

```python
rt = Runtime(home: Path | None = None, engine: str | None = None)
rt.pull(ref: str, platform: str | None = None) -> Image
rt.inspect(ref: str) -> Image | None
rt.images() -> list[Image]
rt.remove_image(ref: str) -> None
c = rt.create(ref, command=None, entrypoint=None, binds: list[Bind] = (),
              workdir=None, env: dict[str, str] | None = None,
              user: str | None = None, name: str | None = None) -> Container
rt.start(c) -> None
rt.exec(c, command: str | Sequence[str], workdir=None, env=None,
        capture: bool = True) -> ExecResult   # code, stdout, stderr
rt.stop(c) -> None
rt.remove(c, force: bool = False) -> None
rt.containers(all: bool = False) -> list[Container]
rt.info() -> dict
```

A string command runs through `/bin/sh -c`. A sequence runs as argv. The CLI
is an argparse layer over this API and holds no logic of its own.

### 5.3 coala integration

coala's `configure_container_runner` gains one branch. When
`container_runner == "xcodon"`, it sets
`runtime_context.user_space_docker_cmd` to the path of the `xcodon`
executable found on PATH or next to the running interpreter. cwltool then
calls `xcodon inspect`, `xcodon pull`, and `xcodon run` with the flags listed
in 5.1. No other coala change is needed.

### 5.4 coala-runtime integration

xcodon-runtime ships `xcodon_runtime.coala_adapter.XcodonContainerManager`
with the seven methods coala-runtime calls: `ensure_image`,
`create_container`, `start_container`, `exec_command`, `get_logs`,
`remove_container`, `cleanup_all`. It wraps the synchronous API with
`run_in_executor`, translates the `{host: {"bind": ..., "mode": ...}}`
volumes dict into `Bind` objects, and sets
`system_site_packages_writable = True`. It imports nothing from
coala-runtime, so it has no dependency on it.

coala-runtime's change is small and lives in its own repo:

- `ContainerEngine.XCODON = "xcodon"`.
- `make_container_manager` returns `XcodonContainerManager` for that value.
- Autodetection tries xcodon after Docker and Podman and before Apptainer,
  when `xcodon_runtime` is importable.
- The `--engine` choices list includes `xcodon`.

Locally built `coala-runtime-python:latest` images work because
`ensure_image` pulls from the local daemon when they are not in the store.

### 5.5 Distribution

`pyproject.toml` with hatchling. Console script `xcodon`. Vendored binaries
under `xcodon_runtime/_bin/`: `proot-x86_64`, `MANIFEST` with version and
SHA-256, and the PRoot license (GPL-2.0, distributed alongside this MIT
project as a separate program). PRoot comes from the proot-me GitHub release
v5.4.1 static build. Total under 2 MB.

## 6. Known limits

- One uid inside the sandbox. All rootfs files appear owned by that uid.
  `chown` to another uid fails. Setuid binaries do not elevate. Package
  managers that create users (`useradd`) still work because they only edit
  `/etc/passwd`, but anything that then `chown`s to the new user fails.
- Host network only. `--net=none` is ignored.
- No cgroups. Resource flags are ignored with a warning.
- Device nodes, sockets, and fifos inside images are skipped at extraction.
  `/dev` inside the sandbox is built by the keeper from host device binds.
- proot engine: slower on syscall-heavy programs, no hostname or pid
  isolation, and container creation costs a full copy on filesystems without
  reflinks.
- Path length: overlayfs mount options are limited to one page. With a single
  lower directory this is never reached.

## 7. Error handling

Exception base `XcodonError`. Subclasses: `ImageNotFound`, `PullError`
(HTTP status, auth failure, digest mismatch, unsupported platform),
`UnsupportedLayer`, `EngineUnavailable`, `ContainerNotFound`,
`ContainerNotRunning`, `ExecError`. Messages say what failed and, where there
is one, the fix.

Atomic state: blobs, layers, image directories, and flattened rootfs are
built under `.part` or `.tmp` names and renamed when complete. `prune`
removes leftovers, unreferenced layers, and exited containers older than a
day when `--all` is given, or only leftovers by default.

`run` forwards SIGINT and SIGTERM to the child and, with `--rm`, stops and
removes the container even when the command failed.

Logging uses the standard `logging` module to stderr. Default level is
warning. `-v` sets info, `-vv` sets debug, and `XCODON_LOG=info|debug` does
the same without flags.

## 8. Testing

pytest with markers `network`, `ns`, `proot`, `docker`, `cwltool`,
`coala_runtime`. Each marker skips when its prerequisite is missing, so the
unit tier runs anywhere.

Unit tests, no network and no namespaces:

- Reference normalization and digest references.
- Manifest, index, and nested index parsing from fixture JSON, including
  platform selection and the no-match error.
- Whiteout and opaque handling on synthetic layer directories, including a
  file re-added after deletion in a later layer.
- Layer extraction filter behavior: absolute symlink kept, escape refused,
  device skipped.
- Spec builder rules for argv, env, HOME, PATH, workdir, and user resolution.
- `--mount` CSV and `-v` parsing, ignored-flag warnings, unknown-flag error.
- `docker save` loader against an OCI-layout tarball built in the test.
- Engine selection with probe results stubbed.
- Keeper mount plan: the ordered list of mount and bind operations the
  keeper would perform, computed as data and checked without running it.
- Lock behavior: a waiter skips work already done.

Integration tests:

- One engine suite, parametrized over `ns` and `proot`: create, start, exec,
  a write that persists across exec, stop, start again, remove. Uses a tiny
  rootfs built in the test from host binaries, so no pull is needed.
- `ns` keeper checks: inside the sandbox `/proc/self/status` shows pid 1 for
  the keeper, `/dev/null` and `/dev/pts` work, `hostname` is the container
  id, a zombie left by a backgrounded exec is reaped, the host mount table is
  unchanged after `start`, and killing the keeper ends every sandbox process.
- `network`: pull `busybox:latest` from Docker Hub, run `echo`.
- `docker`: load an image from the local daemon and run it.
- `cwltool`: run a CWL CommandLineTool with a DockerRequirement through
  `cwltool --user-space-docker-cmd xcodon`.
- `coala_runtime`: run coala-runtime's Python executor with
  `COALA_CONTAINER_ENGINE=xcodon`. Lives in the coala-runtime repo.
- Vendored binary manifest: sizes and SHA-256 match `MANIFEST`.

CI: GitHub Actions on `ubuntu-latest`, which allows user namespaces, runs the
unit tier plus `ns`, `proot`, and `network`.

## 9. Decisions and alternatives considered

- **In-process system calls over an OCI runtime (crun, youki, runc).**
  An OCI runtime gives multi-uid ownership and capabilities but needs a
  vendored runtime, a generated `config.json`, `newuidmap`, and an unverified
  rootless overlay path. The chosen approach was verified on this host and
  shares one image store and container layout with the proot engine.
- **Python-native keeper over bwrap.** bwrap was verified to work as the
  sandbox process, but it must be installed on the host, and its command must
  exist inside the image, which distroless images break. Doing the mount setup
  in the keeper itself removes both problems at the cost of about 200 lines
  that follow bwrap's sequence. The exec path is unchanged either way.
- **Own image store over uDocker.** uDocker is a large legacy dependency and
  stays on ptrace even where namespaces exist. cwltool already speaks to it,
  so wrapping it would add nothing for coala.
- **ctypes system calls over util-linux `unshare` and `nsenter`.**
  `--map-user` and `--wdns` need util-linux 2.38 and 2.39, and Ubuntu 22.04
  ships 2.37. Three ctypes calls remove the version dependency.
- **Flattened rootfs by hardlinks over per-layer overlay lower dirs.**
  Whiteouts in kernel overlayfs need character devices or trusted xattrs,
  which unprivileged extraction cannot create. Flattening applies whiteouts
  in Python and costs almost no disk.
- **PRoot fallback over delegating to Apptainer.** Keeps the runtime
  self-contained and behaving the same on every host. Apptainer remains
  available in coala-runtime as its own engine.
