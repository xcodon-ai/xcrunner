#!/bin/bash
# Real checks of xcrunner on an Apple silicon Mac with macOS 26+ and Lima 2 (spec 16.10).
# Run it from a shell where `xcrunner` is on PATH. It writes a report to
# ~/xcrunner-mac-smoke.txt (or $XCRUNNER_SMOKE_REPORT) and prints PASS/FAIL per step.
# CI also runs it on Linux with XCRUNNER_MACHINE_LINUX_TEST=1, against a real Lima
# QEMU VM, because GitHub's macOS runners cannot start VMs.
set -u
REPORT="${XCRUNNER_SMOKE_REPORT:-$HOME/xcrunner-mac-smoke.txt}"
# Under the home folder, which the VM shares at the same path.
WORK="$(mktemp -d "$HOME/.xcrunner-smoke.XXXXXX")"
PASSED=0
FAILED=0
: >"$REPORT"

step() {
  local name="$1"
  shift
  printf '\n== %s\n' "$name" >>"$REPORT"
  if "$@" >>"$REPORT" 2>&1; then
    echo "PASS $name" | tee -a "$REPORT"
    PASSED=$((PASSED + 1))
  else
    echo "FAIL $name" | tee -a "$REPORT"
    FAILED=$((FAILED + 1))
  fi
}

machine_start() { xcrunner machine start && xcrunner machine status && xcrunner info; }

hello() { test "$(xcrunner run --rm alpine echo hi)" = hi; }

bind_mount() {
  xcrunner run --rm -v "$WORK:/w" alpine sh -c 'echo from-vm > /w/bind.txt' &&
    grep -qx from-vm "$WORK/bind.txt"
}

cwltool_run() {
  local cwltool
  cwltool="$(command -v cwltool || true)"
  if [ "${XCRUNNER_MACHINE_LINUX_TEST:-}" = 1 ]; then
    # A Linux host shares only the home folder with the VM, so keep cwltool's
    # temp files there. On a Mac they stay in /var/folders, which is shared.
    mkdir -p "$WORK/tmp"
    export TMPDIR="$WORK/tmp"
  fi
  if [ -z "$cwltool" ]; then
    python3 -m venv "$WORK/cwl-venv" && "$WORK/cwl-venv/bin/pip" install -q cwltool || return 1
    cwltool="$WORK/cwl-venv/bin/cwltool"
  fi
  cat >"$WORK/echo.cwl" <<'CWL'
cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
outputs:
  out: {type: stdout}
stdout: out.txt
requirements:
  DockerRequirement: {dockerPull: alpine}
CWL
  (cd "$WORK" && "$cwltool" --user-space-docker-cmd "$(command -v xcrunner)" --outdir "$WORK/cwl-out" \
    echo.cwl --message hello-cwl) && grep -qx hello-cwl "$WORK/cwl-out/out.txt"
}

x86_image() {
  local image=quay.io/biocontainers/bwa:0.7.19--h577a1d6_1
  xcrunner pull "$image" && xcrunner inspect "$image" | grep -qi '"architecture": "amd64"' &&
    { xcrunner run --rm "$image" bwa 2>&1 || true; } | grep -q 'Program: bwa'
}

env_folder() {
  local env="$WORK/.xcrunner-env"
  xcrunner run --rm --env-dir "$env" python:3.12-slim pip install -q tabulate &&
    xcrunner run --rm --env-dir "$env" python:3.12-slim python -c 'import tabulate' &&
    test -f "$env/environment.json"
}

conda_install() {
  # Downloads the pinned micromamba; --force because a runner may have a real conda on PATH.
  xcrunner shim install conda --dir "$WORK/shims" --force || return 1
  (cd "$WORK" && xcrunner conda create -p ./condaenv -y zlib &&
    { test -e ./condaenv/lib/libz.dylib || test -e ./condaenv/lib/libz.so; } &&
    xcrunner conda run -p ./condaenv true)
}

kill_stops_container() {
  xcrunner run --rm --name xcrunner-smoke-sleep alpine sleep 300 &
  local pid=$! i
  for i in $(seq 60); do
    xcrunner ps | grep -q xcrunner-smoke-sleep && break
    sleep 1
  done
  xcrunner ps | grep -q xcrunner-smoke-sleep || return 1
  kill -TERM "$pid"
  wait "$pid"
  for i in $(seq 30); do
    xcrunner ps -a | grep -q xcrunner-smoke-sleep || return 0
    sleep 1
  done
  return 1
}

{
  echo "date: $(date)"
  sw_vers 2>/dev/null || uname -a
  uname -m
  limactl --version
  xcrunner --version
  echo "work folder: $WORK"
} >>"$REPORT" 2>&1

step "1 machine start, status and info" machine_start
step "2 hello from alpine" hello
step "3 bind mount from the home folder" bind_mount
step "4 cwltool run through xcrunner" cwltool_run
step "5 x86_64-only bwa image (through Rosetta on a Mac)" x86_image
step "6 env folder keeps a pip install" env_folder
step "7 conda shim installs a macOS package" conda_install
step "8 killing xcrunner run stops the container" kill_stops_container

printf '\npassed %d, failed %d\n' "$PASSED" "$FAILED" | tee -a "$REPORT"
if [ "$FAILED" -eq 0 ]; then
  rm -rf "$WORK"
else
  echo "kept $WORK for inspection" | tee -a "$REPORT"
fi
echo "report: $REPORT"
[ "$FAILED" -eq 0 ]
