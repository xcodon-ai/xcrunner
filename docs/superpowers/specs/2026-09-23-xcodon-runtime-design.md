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

One Python package, `xcodon_runtime`, with a console script `xrunner`. All state
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

Data flow for `xrunner run IMAGE CMD`:

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

`--pull=always` skips step 1. `xrunner pull` runs steps 2 and 3.

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

`xrunner inspect IMAGE` prints a JSON array with one object:

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
  cmd. Empty argv is an error. A non-empty entrypoint override with no
  command discards the image CMD; an empty override (`--entrypoint=`) keeps
  it.
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
    def popen(self, container: Container, argv: list[str], env: dict[str, str],
              workdir: str, **popen_kwargs) -> subprocess.Popen: ...
    def stop(self, container: Container) -> None: ...
    def is_running(self, container: Container) -> bool: ...
```

`popen` returns the process so the caller decides how to wait on it and what
to do with its streams; `Runtime.exec` is the capturing wrapper over it.

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
into `rootfs/`, which is the persistent layer. `stop` removes the started
marker and keeps the copied rootfs, so the container stops reading as
running while a later `start` costs no second copy.

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
the failed probe. `xrunner info` prints all probe results. A container records
its engine at create time and always uses it, so a running keeper is never
orphaned by a changed decision.

## 5. Interfaces

### 5.1 CLI

Docker-compatible subset. Unknown flags fail with a message that names the
flag. `--memory`, `--cpus`, `--cpu-shares`, `--gpus`, `--net`, `--network`,
`--read-only`, and `--log-driver` are accepted and ignored with a warning,
because cwltool or docker-py callers may pass them.

```
xrunner pull [--platform P] IMAGE
xrunner inspect IMAGE
xrunner images
xrunner rmi IMAGE
xrunner run [--mount=type=bind,source=S,target=T[,readonly]] [-v|--volume S:T[:ro|:rw]]
           [--workdir=D] [-w D] [--env=K=V] [-e K=V] [--entrypoint=E] [--user=U] [-u U]
           [--name=N] [--rm] [-i] [--pull=missing|always] IMAGE [CMD...]
xrunner create  (same options as run, minus --rm and -i)  IMAGE [CMD...]
xrunner start ID
xrunner exec [--workdir=D] [--env=K=V] ID CMD...
xrunner stop ID
xrunner rm [-f] ID
xrunner ps [-a]
xrunner logs ID
xrunner info
xrunner prune
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
rt.list_images() -> list[Image]
rt.remove_image(ref: str) -> None
c = rt.create(ref, command=None, entrypoint=None, binds: list[Bind] = (),
              workdir=None, env: dict[str, str] | None = None,
              user: str | None = None, name: str | None = None) -> Container
rt.start(c) -> None
rt.popen(c, command=None, workdir=None, env=None, **popen_kwargs) -> Popen
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
`container_runner == "xrunner"`, it sets
`runtime_context.user_space_docker_cmd` to the path of the `xrunner`
executable found on PATH or next to the running interpreter. cwltool then
calls `xrunner inspect`, `xrunner pull`, and `xrunner run` with the flags listed
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

- `ContainerEngine.XRUNNER = "xrunner"`.
- `make_container_manager` returns `XcodonContainerManager` for that value.
- Autodetection tries xrunner after Docker and Podman and before Apptainer,
  when `xcodon_runtime` is importable.
- The `--engine` choices list includes `xrunner`.

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
- Read-only binds apply to the top mount only. A submount under a bound host
  path stays writable, because the remount is not recursive.
- Path length: overlayfs mount options are limited to one page. With a single
  lower directory this is never reached.
- The proot engine takes no env-layer lock (only the ns keeper does), so
  `commit --env-dir` of a proot env layer that a container is using can
  capture a tree in the middle of a write.
- Image import clears the setuid, setgid, sticky, and group/other write
  bits, so a world-writable directory such as `/tmp` becomes 0755 instead
  of 1777. The container's one uid owns every file, including under a
  non-root `USER` (checked on both engines with `USER 1000` and
  `touch /tmp/x`), so the container user can still write there. Only a
  process under some other uid could not, and the single-uid mapping
  allows no such process; a tool that checks for mode 1777 sees the
  difference.

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
  `COALA_CONTAINER_ENGINE=xrunner`. Lives in the coala-runtime repo.
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

## 10. Persistent env folder

Added 2026-09-24. Approved design for keeping tools installed inside a
container.

### 10.1 Problem

coala-runtime creates a container per tool call and removes it afterwards.
Anything the agent installs during a call, whether pip, apt, R packages, or
conda, dies with that container. With Docker the agent works around this by
baking dependency images, which needs the daemon. xrunner needs its own way
to keep installations, and it must work on hosts without Docker.

### 10.2 The env folder

An env folder is a host directory that holds a container's writable layer.
A container created with an env folder mounts its overlay upper layer from
there instead of from its own container directory. The next container
started from the same image with the same env folder sees everything the
previous one installed.

Layout:

```
<env-dir>/<image-id>/
  image.json     image ref, image id, first-use time, engine
  upper/ work/   the writable layer                      (ns engine)
  rootfs/        the persistent rootfs copy              (proot engine)
  .lock          held by the running container's keeper (ns engine)
  holder         short id of the container holding the lock (ns engine)
  .copy.lock     guards the first rootfs copy                (proot engine)
```

The layer is keyed by image id. A rebuilt base image has a new id and gets
a fresh layer, so a stale layer never sits on top of a changed image.
Deleting `<env-dir>/<image-id>` resets that env. The container directory
under the runtime home keeps `config.json`, `merged/`, `keeper.pid`, and
`keeper.log`; `xrunner rm` removes only that directory and never the env
folder.

### 10.3 Concurrency

Two overlay mounts may not share one upper directory. Starting an ns
container with an env folder therefore takes an exclusive `flock` on
`<env-dir>/<image-id>/.lock`. The starter acquires it and passes the open
descriptor to the keeper, so the lock lives exactly as long as the container
runs and survives the starting process exiting. A second start for the same
image and env folder logs a warning naming the holding container and the
`xrunner stop` command that frees it, repeats it every 60 seconds, and
blocks until the first container stops. There is no timeout. When the
keeper fails to start with an env folder, the error says the folder must be
on a local filesystem that supports overlay upper layers. Stopping a
container removes overlayfs's leftover `work/work` only while it can still
take the layer lock, so it never deletes the next container's work
directory. The proot engine has no kernel restriction; two proot containers
may share a rootfs copy. The first copy is made into `rootfs.tmp` and
renamed under `.copy.lock` inside the layer, so an interrupted copy is never
mistaken for a complete one.

### 10.4 Interfaces

- CLI: `xrunner run --env-dir DIR ...` and `xrunner create --env-dir DIR ...`.
  The flag is named to stay clear of `-e/--env`, which sets variables. The
  path is made absolute.
- Python: `Runtime.create(..., env_dir=None)` and `Runtime.run(..., env_dir=None)`.
  The container record gains `env_dir: str | None`.
- coala-runtime adapter: when the environment variable `XRUNNER_ENV_DIR` is
  set on the MCP server process, every container the adapter creates uses
  that env folder. coala-runtime itself does not change.
- opencodon: a tools field `xrunner_env_project_relative`, default
  `.xrunner-env` at the project root, mirroring
  `coala_runtime_tmpdir_project_relative`. It stays out of `workspace/`
  because the agent's file tools are rooted there and must not see the
  writable layer.
  When the effective coala-runtime engine is `xrunner` and a project root is
  known, the path is resolved inside the project root and passed to the
  coala-runtime server as `XRUNNER_ENV_DIR`. A value that resolves outside
  the project root is ignored with a warning. `coala_runtime_mcp_extra_env`
  can override the variable.

### 10.5 Out of scope

Mounting host pixi or conda folders onto PATH, sharing one env folder across
different images, exporting or importing an env, and pruning env folders.

## 11. Building images without a daemon

Added 2026-09-24. Approved design. xrunner is used where docker is absent,
so anything the agent does with `docker build` and `docker commit` must be
possible with xrunner alone.

### 11.1 Problem

The agent bakes a dependency image with `docker build` from a tiny
Dockerfile (`FROM base`, `COPY`, `RUN pip install ...`) and reuses it by tag
with `docker image inspect`. Without docker that path fails, and installs
done through xrunner's env folder cannot be turned into a named image.
Separately, when a daemon exists and rebuilds a tag, xrunner kept serving the
old image under that tag.

### 11.2 Commit

`xrunner commit` turns a writable layer into a new image layer on top of its
base image, and registers the result under a tag.

- Sources: a stopped container (`xrunner commit CONTAINER TAG`) or an env
  folder layer (`xrunner commit --env-dir DIR --image IMAGE TAG`). A layer that is in
  use, a running container or a locked env layer, is refused.
- ns engine: the overlay upper directory is translated to OCI form. A
  character device 0:0 becomes a `.wh.<name>` marker; a directory carrying
  the `user.overlay.opaque` or `trusted.overlay.opaque` attribute gets a
  `.wh..wh..opq` marker; other `overlay.*` attributes are dropped; symlinks
  are recreated, other special files skipped. The empty mountpoints the
  keeper creates for its own mounts and for bind targets (for example
  `/etc/hosts`, and a bind target's missing parents) are dropped when the
  base image lacks them.
- Regular files are copied from the writable source into a snapshot
  directory, never hardlinked: the source stays writable, and a hardlink
  would let a later write change the committed layer. The snapshot's files
  are hardlinked only when it moves into the layer store.
- proot engine: the rootfs copy is compared with the image rootfs. Entries
  that differ in type, size, mode, mtime, or link target, or are new, go in
  the layer; entries missing from the copy become `.wh.` markers.
- The layer's diff id is the SHA-256 of a deterministic tar of the layer
  directory (sorted entries, zeroed owner and mtime). The new config is the
  base config with the diff id appended, a history entry, a new `created`
  time, and any requested config changes (`ENV`, `CMD`, `ENTRYPOINT`,
  `WORKDIR`, `USER`, `LABEL`). The image id is the SHA-256 of the canonical
  config JSON, as for pulled images. The image directory is built like a
  pulled one, with `manifest.json` `source: "commit"` and the parent id.
- A commit whose snapshot is empty (no layer changes, only config changes)
  appends no diff id; its history entry has `empty_layer: true`.

### 11.3 Build

`xrunner build -t TAG [-f FILE] [--build-arg K=V]... [--no-cache] CONTEXT`
runs a Dockerfile subset entirely in xrunner containers.

- Instructions: `FROM`, `RUN` (shell and exec form), `COPY` and `ADD` for
  local paths with globs (no URLs, no tar extraction), `ENV`, `WORKDIR`,
  `USER`, `ENTRYPOINT`, `CMD`, `LABEL`, `ARG`, `SHELL`. Comments and
  backslash continuations. `EXPOSE`, `VOLUME`, `HEALTHCHECK`, `STOPSIGNAL`,
  `MAINTAINER`, and `ONBUILD` are accepted and ignored with a warning.
  `FROM ... AS` and `--target` (multi-stage) are errors.
- Each `RUN` starts a container from the current image, executes the command
  with the image's environment plus build args, streams the output, and
  commits the container. Each `COPY`/`ADD` builds a layer on the host and
  commits it. Config-only instructions commit a config-only image.
- Cache: a key of parent image id, instruction text, and for `COPY` the
  content hash of the sources, maps to the resulting image id in
  `<home>/build-cache.json`. A hit skips the step. `--no-cache` ignores it.
  Intermediate images are untagged; `prune --all` removes untagged images.
- `-t` tags the final image; several `-t` are allowed.

### 11.4 Docker-compatible surface and shim

- Aliases: `xrunner image inspect|ls|rm`, `xrunner tag SRC DST`.
- `xrunner docker VERB ...` accepts docker's verbs and flags for the subset
  the agent and cwltool use: `build`, `image inspect`, `image ls|images`,
  `image rm|rmi`, `tag`, `pull`, `run`, `create`, `start`, `exec`, `stop`,
  `rm`, `ps`, `logs`, `commit`, `version`, `info`. Unknown verbs exit 125
  with a message.
- `xrunner shim install [--dir DIR] [--force]` writes an executable named
  `docker` into DIR (default: the directory of the running interpreter) that
  forwards to `xrunner docker`. It refuses when a real docker is already on
  PATH unless `--force`, and prints how to put DIR on PATH. This is how the
  unchanged agent reaches xrunner on a host without docker.

### 11.5 Stale tags from a daemon

When resolving a tag with `pull="missing"`, if the stored image came from a
local daemon (`manifest.json` source `daemon`) and the daemon is available
and has the tag, xrunner compares the daemon's current image id
(`docker image inspect --format {{.Id}}`) with the id recorded at import
(`manifest.json` `daemon_id`) and re-imports on mismatch. The daemon's id is
never compared with xrunner's own image id: with docker's containerd image
store it is the manifest digest, not the config digest. An image imported
before `daemon_id` existed is re-imported once to record it. If a re-import
fails, xrunner logs a warning and uses the stored image. `pull="never"` and
refs that are image ids skip the check. Locally built and committed images
are never replaced by this check.

### 11.6 Out of scope

Multi-stage builds, `RUN --mount`, build secrets, `.dockerignore`, `ADD`
from URLs or tar extraction, pushing or exporting images.

## 12. Installing tools without conda

Added 2026-09-25. Approved design. Where xrunner replaces docker, the host
may have no conda either. The agent still reaches for conda whenever a
command-line tool is missing, so xrunner must answer those calls itself.

### 12.1 Problem

The agent runs command-line tools through its `run_shell` tool, on the
host, with the project root as the working directory. When a tool such as
`bwa` or `bowtie2` is missing, the model runs `conda create -p
workspace/conda_env ...`, `conda install -n NAME ...` and `conda run -n
NAME TOOL ...`. On a host without conda every one of these fails. On a host
with conda they install into the user's own environments, outside the
project.

### 12.2 Shape

- `xrunner conda ARGS...` accepts conda's command line and runs a pinned
  micromamba with project-local settings. Micromamba speaks conda's verbs
  and flags (`create`, `install`, `-n`, `-p`, `-c`), so most commands pass
  through unchanged.
- `xrunner shim install conda` writes three executables named `conda`,
  `mamba` and `micromamba`. Each forwards to `xrunner conda "$@"`. With the
  shim directory on PATH, the unchanged agent's conda calls reach xrunner.
- Tools run natively on the host, where the agent expects them. Nothing
  here runs a container.

### 12.3 Where environments live

The root prefix is the first of:

1. `<XRUNNER_ENV_DIR>/conda`, when `XRUNNER_ENV_DIR` is set.
2. `<dir>/conda` for the nearest existing directory named `.xrunner-env`,
   searching from the working directory up to the filesystem root.
3. `<home>/conda`, under the xrunner runtime home.

Rule 2 accepts a `.xrunner-env` only when its resolved directory (after
following links) is owned by the current user and is not writable by group
or others. Any other one, such as another user's `/tmp/.xrunner-env`, is
skipped and the search goes on upward; otherwise `conda run` would exec
that user's binaries and source their `activate.d` scripts.
`XRUNNER_ENV_DIR` and `-r` are trusted as given, because the caller chose
them.

An explicit `-r/--root-prefix` on the command line wins over all three.
The root prefix is created if it is missing. `-n NAME` envs live at
`<root>/envs/NAME`. `-p PATH` envs live exactly at PATH; a relative PATH is
taken from the working directory. Before calling micromamba, xrunner
rewrites every `-p/--prefix` value to an absolute path, `<cwd>/PATH`, on
every verb and in every form (`-p PATH`, `-pPATH`, `--prefix=PATH`).
Micromamba 2.9.0 reads a relative value with no `/` as an env name
(`<root>/envs/PATH`); the rewrite keeps micromamba, the rerun record and
`conda run` on the same folder. `conda install` with neither flag targets
the root prefix itself, as conda's base does.

The agent passes `XRUNNER_ENV_DIR` only to the coala-runtime process, not
to `run_shell`. Rule 2 covers that case, because `run_shell` starts in the
project root, which holds `.xrunner-env`. A launcher may also export
`XRUNNER_ENV_DIR` for the whole agent process.

`.xrunner-env` appears only once a container has started, so an early conda
call can fall through to rule 3. xrunner then prints one warning line on
stderr naming the root it used, and how to avoid it: `xrunner: no project
env folder found; using <root> (set XRUNNER_ENV_DIR or create .xrunner-env
in the project)`. Lookups of an existing `-n NAME` env, for
`run`, `list`, `env export`, `remove`, `env remove`, `uninstall`, `install`
and `update`, try the resolved root first and then `<home>/conda`, so an env
created before the project folder existed is still found. Creates always go
to the resolved root.

`ensure_root` makes a fresh root prefix a valid base env too, as real conda's
own root is: it creates `<root>/conda-meta` (along with `<root>/envs` and the
private `<root>/.home`), so a bare `conda list` or `conda run` targeting the
root itself finds an environment there.

The package cache is shared: `CONDA_PKGS_DIRS=<home>/conda-pkgs`. A second
project that needs the same package takes it from the cache instead of
downloading it again. The cache hardlinks packages into an env when it is on
the same filesystem as the env, and copies them otherwise. Micromamba locks
the cache and each prefix itself.

### 12.4 Isolation from the user's conda

Micromamba records every `-p` env in `$HOME/.conda/environments.txt` and
writes `$HOME/.cache`, with no setting to turn either off (checked with
micromamba 2.9.0). So every micromamba process xrunner starts gets:

- `HOME=<root>/.home`, so the env registry and caches stay in the project.
- `MAMBA_ROOT_PREFIX=<root>` and `CONDA_PKGS_DIRS=<home>/conda-pkgs`.
- `--no-rc`, so the user's `~/.condarc` and `~/.mambarc` are ignored.
- Channels `conda-forge` and `bioconda`, added after any channels the
  command names with `-c`, without duplicates.
- `-y` for the verbs that ask for confirmation: `create`, `install`,
  `update`, `remove`, `uninstall`, `clean`, `env create`, `env remove`.
- `-r <root>` on every command except `clean`, which micromamba 2.9.0
  rejects it for; `clean` gets the root from `MAMBA_ROOT_PREFIX` instead.

The user's real `~/.conda` is never read or written.

### 12.5 Command surface

- Passed through with the settings from 12.4: `create`, `install`,
  `update`, `remove`, `uninstall`, `list`, `search`, `info`, `clean`,
  `env list`, `env create -f FILE`, `env export`, `env remove`,
  `config list`. `env --help`/`-h` (no subcommand) also passes through to
  micromamba's own help. `config` supports only `config list`; any other
  `config` subcommand exits 2 with a message that xrunner's conda only
  supports `config list`.
- `run`: implemented by xrunner, not micromamba. Micromamba 2.9.0's `run`
  fails on this host with `exec: --: invalid option` from its own wrapper
  script. xrunner parses `-n NAME` or `-p PATH` (both together is an error:
  "use -n NAME or -p PATH, not both"); a relative PATH is made absolute
  from the working directory, the same rewrite as in 12.3. It honors `--cwd DIR`, and accepts and
  ignores `--no-capture-output` and `--live-stream`. `conda run --help`/`-h`
  prints xrunner's own usage for `run` and exits 0. It sets
  `PATH=<prefix>/bin:$PATH`, falling back to `os.defpath` when the calling
  process has no `PATH` set, plus `CONDA_PREFIX`, `CONDA_DEFAULT_ENV` and
  `CONDA_SHLVL=1`, keeps the user's real HOME, and replaces itself with the
  command, so arguments, stdin, stdout and the exit code are the tool's own.
  Before that exec, it resets `SIGPIPE` and `SIGXFSZ` to their default
  disposition: Python ignores both at startup, and exec preserves an ignored
  disposition, so an unpatched command would inherit an ignored SIGPIPE and
  survive a broken pipe instead of dying from it. When
  `<prefix>/etc/conda/activate.d` holds `*.sh` scripts, the command runs as
  `/bin/bash -c '. SCRIPT; ...; exec "$@"' bash CMD ARGS...` so those scripts
  apply. Real conda on Linux sources them with bash too. When `/bin/bash` is
  missing, `/bin/sh` takes its place (`/bin/sh -c ... sh CMD ARGS...`). A missing env exits 1 with `EnvironmentLocationNotFound: Not a
  conda environment: <path>`.
- `activate`, `deactivate`, `init` and `shell` exit 1 with a message that
  points to `conda run -n NAME CMD` or to `<prefix>/bin/CMD`. Activation
  changes the calling shell, which a separate process cannot do.
- `--version` and `-V` print `conda <micromamba version> (micromamba via
  xrunner)`.
- Any other verb exits 2 with a message naming the supported verbs.
- An option that needs a value (`-r`, `-n`, `-p`) never takes the next
  token as its value when that token is itself an option: `conda list -r
  -n c` exits 2 with `conda: -r needs a value`.
- Options before the verb, such as `--json` or `-q`, pass through as given.
  This also holds for `run` (`conda -r ROOT run -n NAME CMD` works, not only
  `conda run -r ROOT -n NAME CMD`). The `mamba` and `micromamba` shims reach
  the same code, so micromamba-only flags such as `-r` work through them
  too.

### 12.6 Rerun record

After a successful `create`, `install`, `update`, `remove`, `uninstall` or
`env create` on an env, xrunner writes `<prefix>/conda-explicit.txt` from
`micromamba env export --explicit`. It lists every package URL with its
checksum, so the env can be rebuilt exactly with `conda create -p PATH
--file conda-explicit.txt`. A failed export logs a warning and does not
change the command's exit code. `--dry-run`/`-d` writes no record and prints
no warning either, since a dry run creates nothing to record. Micromamba
2.9.0 has no `-d`, only `--dry-run`, and rejects a bare `-d` outright; on
these same verbs xrunner translates conda's `-d` to `--dry-run` before
calling micromamba.

`env create -f FILE` without `-n` or `-p` takes the target env's name from
the file's top-level `name:` line, the same as real micromamba: a `prefix:`
line (which `conda env export` also writes) is ignored. With neither a name
in the file nor `-n`/`-p` on the command line, it exits 1 with "No target
prefix specified", the same as real micromamba.

After a successful `remove --all` (or `-a`, on `remove` and `uninstall`) or
`env remove`, the env holds no packages: xrunner writes no record and
deletes any `conda-explicit.txt` left in that prefix.

When one of those commands exits 0 but leaves no `<prefix>/conda-meta` at
the target (for example an `install` into an env that was never created),
xrunner cannot export it and prints a warning naming the prefix instead of
writing `conda-explicit.txt`; the command's own exit code is unchanged.

### 12.7 The micromamba binary

- xrunner pins conda-forge's micromamba 2.9.0 package for linux-64: its
  URL, the archive's SHA-256 and the SHA-256 of `bin/micromamba` live in
  `micromamba.py`. Other platforms are an error.
- `xrunner shim install conda` downloads it once into
  `<home>/bin/micromamba-<version>/micromamba`, checks the SHA-256, writes it
  through a temporary file and a rename, and marks it executable. The file
  is named `micromamba` because micromamba names itself after its file in
  the hints and errors it prints (`micromamba run -n ...`), and that is the
  name the shim serves.
- `--micromamba PATH` copies an existing binary instead, for hosts without
  network access to the release. The environment variable
  `XRUNNER_MICROMAMBA` points xrunner at a binary at call time.
- `xrunner conda` never downloads. When no binary is present it exits 125
  with `xrunner: micromamba is not installed; run: xrunner shim install
  conda`.
- The pinned binary needs glibc 2.17 or newer and links only against glibc.
- The binary is used exactly as extracted from the archive, without the
  placeholder-prefix rewrite a conda installer normally applies
  (`info/has_prefix`); this is safe because xrunner always passes the root
  prefix explicitly on every call.

### 12.8 Shim install

`xrunner shim install [docker|conda] [--dir DIR] [--force] [--micromamba
PATH]`. The kind defaults to `docker`, so the existing command keeps its
meaning. For `conda`:

- It writes `conda`, `mamba` and `micromamba` into DIR (default: the
  directory of the running interpreter). Each is `#!/bin/sh`, carries the
  marker line `# conda shim installed by xrunner`, and runs
  `exec <absolute xrunner> conda "$@"`.
- It refuses, unless `--force`, when a real `conda`, `mamba` or
  `micromamba` is on PATH (xrunner shims are skipped), or when DIR already
  holds a non-shim file or link under one of the three names. Writes go
  through a temporary file and `os.replace`, so a link is replaced, never
  written through. This matches the docker shim in 11.4.
- It downloads or copies micromamba as in 12.7, and prints how to put DIR
  on PATH when it is not there.

### 12.9 Errors

- No micromamba: exit 125, message as in 12.7.
- Solver, network and package errors: micromamba's own message and exit
  code, unchanged.
- `run` of a missing env: exit 1. A missing command inside the env: exit
  127, as a shell reports it.
- A root prefix that cannot be created: exit 125 naming the path.

### 12.10 Testing

- Unit tests with a fake micromamba, a script that records its argv and
  environment. They need no network. They cover the root prefix order
  in 12.3, the settings in 12.4, `-y` and channel handling, the refused
  verbs, the rerun record, and shim install refusal, `--force`, and link
  replacement.
- `run` tests with real env folders built by hand: arguments with
  spaces and shell characters, stdin, exit codes, activation variables,
  `activate.d` scripts, a missing env, and a missing command.
- Network-marked tests with the real pinned micromamba: `create -n` of a
  small bioconda tool, `run -n` of it, a `-p` env at a relative path, the
  explicit export, and a sandboxed test HOME whose `.conda` stays empty.

### 12.11 Out of scope

Mounting these envs into containers, conda inside container images (the
agent's own recipe keeps `|| true`), `conda activate` in the calling
shell, platforms other than linux-64, and changes to the agent.

## 13. Environment record

Added 2026-09-25. Approved design. xrunner is xcodon's sandbox. A project may
install tools into its own folder, but what it used must be on record: which
images, and which packages each install added. This section keeps records
only. It does not restore, export or pin anything.

### 13.1 Problem

Section 10 keeps container installs in `.xrunner-env/<image-id>/upper` and
section 12 keeps conda envs under the project, but nothing lists what they
hold. Images are named by movable tags, so `coala-runtime-python:latest` can
mean a different image next month. When a tag moves, the next container gets
a new, empty layer and the earlier installs silently stay behind in the old
one.

### 13.2 The record file

`<env folder>/environment.json`, where the env folder is the `.xrunner-env`
directory (or `XRUNNER_ENV_DIR`). "Project root" below means the env
folder's parent. Paths inside the file are relative to the project root,
except image names and URLs. The file is JSON with sorted keys and two-space
indent, and carries `"version": 1`. It has three sections.

`images`: an object keyed by the image ref as the caller wrote it, for
example `coala-runtime-python:latest`. Each value holds:

- `id`: the image id (config digest, `sha256:...`).
- `source`: `registry`, `daemon`, or `build` (for `xrunner build` and
  `commit` images).
- `repo_digests`: a list of `name@sha256:...` strings that locate this image
  in a registry, when known: the registry name and manifest digest for a
  registry pull, or docker's `RepoDigests` for a daemon import. Empty when
  unknown, as for an image built locally by docker.
- `dockerfile` and `parent`: for `build` images, the Dockerfile text and the
  parent image id. `commit` images have `parent` only.
- `first_used`, `last_used`: UTC times, second precision.
- `previous_ids`: ids this ref pointed to earlier, oldest first. When a
  recorded ref resolves to a new id, the old `id` moves here.

`layers`: an object keyed by image id, one entry per `.xrunner-env/<id>`
folder. Each value holds `ref` (from the folder's `image.json`), `engine`,
and `packages`: the packages this layer added, changed or removed compared
with its image, read from files as in 13.3. Each package is
`{"manager", "name", "version", "change", "location"}` plus `"url"` when the
metadata has one. `change` is `added`, `changed` or `removed`. `location` is
the directory inside the container that holds the metadata.

`conda`: an object keyed by env, one entry per host conda env under this
project's root prefix (section 12) and per `-p` env inside the project root.
The key is `name:<NAME>` for a `-n` env and `path:<relative path>` for a
`-p` env. Each value holds `explicit` (the relative path of its
`conda-explicit.txt`), `sha256` of that file, and `packages` (the count of
package lines in it).

### 13.3 Reading packages from a layer

Packages come from the files in the layer, never from running commands in
the container. So recording works on a stopped layer, on either engine, and
cannot be steered by code in the image. For the ns engine the layer is the
overlay upper (files present, char device 0:0 whiteouts, opaque directories).
For the proot engine it is the rootfs copy compared with the image rootfs.

- pip: `*.dist-info` and `*.egg-info` directories whose parent is a
  `site-packages` or `dist-packages` directory. Name and version come from
  `METADATA` or `PKG-INFO`. Download caches such as `/root/.cache` also hold
  `*.dist-info` folders and are ignored by this parent rule.
- R: a directory that holds both `DESCRIPTION` and `Meta/package.rds`,
  which every installed R package has and source folders do not. Name and
  version come from the `Package:` and `Version:` fields of `DESCRIPTION`;
  `url` from `Repository:` when present.
- apt: `var/lib/dpkg/status` in the layer compared with the image's copy.
  Stanzas with `Status: install ok installed` are parsed for `Package`,
  `Architecture` and `Version`.
- conda inside the image: `conda-meta/*.json` files; `name`, `version` and
  `url` from the JSON.

A package whose metadata directory is new in the layer is `added`. One that
exists in both with a different version is `changed`. One whose metadata is
removed by a whiteout, an opaque parent, or its absence from the proot copy
is `removed`. A metadata file that cannot be parsed is skipped with a debug
log, never an error.

### 13.4 When the record is written

- After a successful conda command that changes an env (the RECORD verbs of
  12.6), for that env's entry.
- When a container that uses an env folder stops, through `Runtime.stop`,
  for that container's image and layer entries. `run --rm` stops before it
  removes, so it records too.
- When a container is created with an env folder, for the image entry
  (`id`, `source`, `repo_digests`, times).
- On demand: `xrunner env record [--env-dir DIR]` rebuilds every section
  from what is on disk. `DIR` defaults to the env folder found as in 12.3.

Updates hold an exclusive flock on `<env folder>/.environment.lock` while
they read, merge and write, and write through a temporary file plus
`os.replace`. A failure to record logs a warning and never changes the exit
code of the command that triggered it.

### 13.5 Image metadata the store keeps

- A registry pull stores the registry name and the manifest digest it
  resolved in the image's `manifest.json` as `repo_digest`.
- A daemon import stores docker's `RepoDigests` (`docker image inspect
  --format '{{json .RepoDigests}}'`) as `repo_digests`.
- `xrunner build` stores the Dockerfile text in the final image's
  `manifest.json` as `dockerfile`.

Images stored before this change lack these fields; their record entries
have an empty `repo_digests` list.

### 13.6 Tag moves

When a container is created with an env folder and the record already holds
the same ref at a different image id, xrunner prints one line on stderr:
`xrunner: <ref> now points to <new short id>; installs made on <old short
id> stay in .xrunner-env/<old id>`, and the record moves the old id into
`previous_ids`. Nothing else changes: the container still uses the new
image and a new layer.

### 13.7 Interfaces

- `xrunner env record [--env-dir DIR]`: rebuild the record; print its path.
- `xrunner env show [--env-dir DIR] [--json]`: print a readable summary:
  each image with its id, source and first repo digest; each layer's added,
  changed and removed packages grouped by manager; each conda env with its
  package count and explicit file. `--json` prints the file itself.
- Python: `xcodon_runtime.envrecord.record_all(env_dir) -> Path` and
  `load(env_dir) -> dict`.

### 13.8 Testing

Unit tests build layers by hand: dist-info folders under site-packages and
under `/root/.cache`, R `DESCRIPTION` files, a changed dpkg status, conda-meta
JSON, char device whiteouts (ns engine, inside a user namespace) and a proot
rootfs copy. They check the parsed packages and `change` values, the conda
section from real `conda-explicit.txt` files, the relative paths, the tag-move
warning and `previous_ids`, concurrent updates under the lock, and that a
record failure never changes an exit code. One test runs a real container
with an env folder, installs a pip package, stops it, and finds the package
in the record.

### 13.9 Out of scope

Restoring an environment, exporting images or layers, pinning a ref to an
id, recording files that no package manager owns, and packages installed
outside the metadata locations above.
