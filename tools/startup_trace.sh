#!/usr/bin/env bash
# Trace the RT CPU (sched_switch) across a stack start: tracing starts before the start command
# and runs for <seconds>. Read-only kernel tracing in a private tracefs instance.
# Usage: SUDO_ASKPASS=<helper> tools/startup_trace.sh <out_file> <seconds> -- <start command...>
set -euo pipefail
out="$1"; secs="$2"; shift 2; [[ "${1:-}" == "--" ]] && shift
rt_cpu="${RT_CPU:-14}"; reader_cpu="${READER_CPU:-0}"
if [[ -n "${SUDO_ASKPASS:-}" ]]; then sudo() { command sudo -A "$@"; }; else sudo() { command sudo -n "$@"; }; fi
T=/sys/kernel/tracing; I="$T/instances/runtime_log_startup"
cleanup() { sudo sh -c "echo 0 > $I/tracing_on" 2>/dev/null || true; sudo rmdir "$I" 2>/dev/null || true; }
trap cleanup EXIT
sudo mkdir -p "$I"
mask=$(printf '%x' $((1 << rt_cpu)))
sudo sh -c "echo 0 > $I/tracing_on; echo $mask > $I/tracing_cpumask; echo mono > $I/trace_clock; echo 8192 > $I/buffer_size_kb; echo sched:sched_switch > $I/set_event; echo > $I/trace; echo 1 > $I/tracing_on"
sudo timeout "$secs" taskset -c "$reader_cpu" cat "$I/trace_pipe" > "$out" &
reader=$!
sleep 0.5
echo "TRACE_START mono=$(awk '{print $1}' /proc/uptime) wall=$(date +%s.%N)" > "$out.meta"
"$@"
wait "$reader" || true
echo "events=$(wc -l < "$out")" >> "$out.meta"
