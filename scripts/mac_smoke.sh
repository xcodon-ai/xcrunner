#!/bin/bash
# Real checks of xrunner on an Apple silicon Mac with macOS 26+ and Lima 2 (spec 16.10).
# Run it from a shell where `xrunner` is on PATH. It writes a report to
# ~/xrunner-mac-smoke.txt (or $XRUNNER_SMOKE_REPORT) and prints PASS/FAIL per step.
set -u
REPORT="${XRUNNER_SMOKE_REPORT:-$HOME/xrunner-mac-smoke.txt}"
# Under the home folder, which the VM shares at the same path.
WORK="$(mktemp -d "$HOME/.xrunner-smoke.XXXXXX")"
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

machine_start() { xrunner machine start && xrunner machine status && xrunner info; }

hello() { test "$(xrunner run --rm alpine echo hi)" = hi; }

bind_mount() {
  xrunner run --rm -v "$WORK:/w" alpine sh -c 'echo from-vm > /w/bind.txt' &&
    grep -qx from-vm "$WORK/bind.txt"
}

cwltool_run() {
  local cwltool
  cwltool="$(command -v cwltool || true)"
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
  (cd "$WORK" && "$cwltool" --user-space-docker-cmd "$(command -v xrunner)" --outdir "$WORK/cwl-out" \
    echo.cwl --message hello-cwl) && grep -qx hello-cwl "$WORK/cwl-out/out.txt"
}

x86_image() {
  local image=quay.io/biocontainers/bwa:0.7.19--h577a1d6_1
  xrunner pull "$image" && xrunner inspect "$image" | grep -q '"architecture": "amd64"' &&
    { xrunner run --rm "$image" bwa 2>&1 || true; } | grep -q 'Program: bwa'
}

env_folder() {
  local env="$WORK/.xrunner-env"
  xrunner run --rm --env-dir "$env" python:3.12-slim pip install -q tabulate &&
    xrunner run --rm --env-dir "$env" python:3.12-slim python -c 'import tabulate' &&
    test -f "$env/environment.json"
}

conda_install() {
  (cd "$WORK" && xrunner conda create -p ./condaenv -y zlib && test -e ./condaenv/lib/libz.dylib &&
    xrunner conda run -p ./condaenv true)
}

kill_stops_container() {
  xrunner run --rm --name xrunner-smoke-sleep alpine sleep 300 &
  local pid=$! i
  for i in $(seq 60); do
    xrunner ps | grep -q xrunner-smoke-sleep && break
    sleep 1
  done
  xrunner ps | grep -q xrunner-smoke-sleep || return 1
  kill -TERM "$pid"
  wait "$pid"
  for i in $(seq 30); do
    xrunner ps -a | grep -q xrunner-smoke-sleep || return 0
    sleep 1
  done
  return 1
}

{
  echo "date: $(date)"
  sw_vers
  uname -m
  limactl --version
  xrunner --version
  echo "work folder: $WORK"
} >>"$REPORT" 2>&1

step "1 machine start, status and info" machine_start
step "2 hello from alpine" hello
step "3 bind mount from the home folder" bind_mount
step "4 cwltool run through xrunner" cwltool_run
step "5 x86_64-only bwa image through Rosetta" x86_image
step "6 env folder keeps a pip install" env_folder
step "7 conda shim installs a macOS package" conda_install
step "8 killing xrunner run stops the container" kill_stops_container

printf '\npassed %d, failed %d\n' "$PASSED" "$FAILED" | tee -a "$REPORT"
if [ "$FAILED" -eq 0 ]; then
  rm -rf "$WORK"
else
  echo "kept $WORK for inspection" | tee -a "$REPORT"
fi
echo "report: $REPORT"
[ "$FAILED" -eq 0 ]
