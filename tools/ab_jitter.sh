#!/usr/bin/env bash
# A/B: RT-thread scheduling period on the isolated CPU vs Runtime-LOG polling settings.
# Usage (on the IPC, stack in OP and DISABLED, nobody operating): tools/ab_jitter.sh <out_dir> [seconds] [phases...]
# Phases: off (service stopped) | log0 | log1 | log5 (service with master polling 0/1/5 Hz).
# Only reads: ftrace sched_switch on the RT CPU in a private tracefs instance, `ethercat master`.
set -euo pipefail
out="$1"; secs="${2:-180}"; shift 2 || true
phases=("$@"); [[ ${#phases[@]} -gt 0 ]] || phases=(off log0 log1 log5)
rt_cpu="${RT_CPU:-14}"; reader_cpu="${READER_CPU:-0}"
mkdir -p "$out"
T=/sys/kernel/tracing; I="$T/instances/runtime_log_ab"
env_file="$HOME/.config/runtime-log.env"
cp "$env_file" "$out/runtime-log.env.before"
sudo() { command sudo -n "$@"; }
# Prime sudo before running (sudo -v); keep the credential alive for the whole test.
command sudo -n -v || { echo "run 'sudo -v' first" >&2; exit 1; }
( while sleep 60; do command sudo -n -v || exit; done ) & keepalive=$!

cleanup() {
  kill "$keepalive" 2>/dev/null || true
  sudo sh -c "echo 0 > $I/tracing_on" 2>/dev/null || true
  sudo rmdir "$I" 2>/dev/null || true
  cp "$out/runtime-log.env.before" "$env_file"
  systemctl --user restart runtime-log || true
}
trap cleanup EXIT

sudo mkdir -p "$I"
mask=$(printf '%x' $((1 << rt_cpu)))
sudo sh -c "echo 0 > $I/tracing_on; echo $mask > $I/tracing_cpumask; echo mono > $I/trace_clock; echo 4096 > $I/buffer_size_kb; echo sched:sched_switch > $I/set_event"

rt_tid=$(ps -eLo tid,cls,rtprio,psr,comm | awk -v c="$rt_cpu" '$2=="FF" && $3==80 && $4==c && $5=="ros2_control_no" {print $1; exit}')
[[ -n "$rt_tid" ]] || { echo "no FIFO 80 ros2_control_node thread on CPU $rt_cpu" >&2; exit 1; }
echo "rt_tid=$rt_tid rt_cpu=$rt_cpu secs=$secs phases=${phases[*]}" | tee "$out/meta.txt"
uname -r >> "$out/meta.txt"

counters() { ethercat master 2>/dev/null | awk '/Common:/{c=1} c&&/Tx frames:|Rx frames:|Lost frames:/{gsub(":","",$0); print}' | tr -s ' ' | tr '\n' ';'; ethercat master | awk '/Tx errors/{print "Tx errors " $3; exit}'; }

for phase in "${phases[@]}"; do
  case "$phase" in
    off) systemctl --user stop runtime-log ;;
    log0) printf 'RUNTIME_LOG_SERVE_ARGS=--ethercat-hz 0\n' > "$env_file"; systemctl --user restart runtime-log ;;
    log1) printf 'RUNTIME_LOG_SERVE_ARGS=--ethercat-hz 1,1\n' > "$env_file"; systemctl --user restart runtime-log ;;
    log5) printf 'RUNTIME_LOG_SERVE_ARGS=--ethercat-hz 5,5\n' > "$env_file"; systemctl --user restart runtime-log ;;
    *) echo "unknown phase $phase" >&2; exit 2 ;;
  esac
  sleep 10  # service startup (log replay, journal backfill) settles before the window
  echo "$phase start $(date +%s.%N) $(counters)" >> "$out/counters.txt"
  sudo sh -c "echo > $I/trace; echo 1 > $I/tracing_on"
  sudo timeout "$secs" taskset -c "$reader_cpu" cat "$I/trace_pipe" > "$out/$phase.trace" || true
  sudo sh -c "echo 0 > $I/tracing_on"
  echo "$phase end $(date +%s.%N) $(counters)" >> "$out/counters.txt"
  echo "$phase: $(wc -l < "$out/$phase.trace") events"
done
