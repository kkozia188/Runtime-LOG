#!/usr/bin/env bash
# PRD 10.3 shared-memory micro-benchmark on the RT CPU. The control stack MUST be stopped first
# (this script refuses to run while any FIFO thread is pinned to the RT CPU).
# Usage: tools/shm_microbench.sh <out_dir> [seconds]   (as a member of the realtime group: rtprio + memlock)
set -euo pipefail
out="$1"; secs="${2:-120}"
rt_cpu="${RT_CPU:-14}"; reader_cpus="${READER_CPUS:-0,2,4}"
here="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$out"

busy=$(ps -eLo cls,psr,comm | awk -v c="$rt_cpu" '$1=="FF" && $2==c && $3!~/^(migration|ktimers|rcuc|irq_work|ksoftirqd|cpuhp|idle_inject|posixcputmr|EtherCAT)/' | head -3)
if [[ -n "$busy" ]]; then echo "RT CPU $rt_cpu still runs FIFO user threads, stop the stack first:" >&2; echo "$busy" >&2; exit 1; fi

bin="$out/shm_bench"
g++ -O2 -std=c++17 -Wall -Wextra -Werror -I "$here/ros/runtime_log_recorder/include" "$here/ros/runtime_log_recorder/tools/shm_bench.cpp" -o "$bin"
cycles=$((secs * 1000))
uname -r > "$out/meta.txt"; echo "rt_cpu=$rt_cpu reader_cpus=$reader_cpus secs=$secs" >> "$out/meta.txt"

run() {  # label fields reader(yes|no)
  local label="$1" fields="$2" reader="$3" name="/runtime-log.bench-$1"
  rm -f "/dev/shm${name}"
  if [[ "$reader" == yes ]]; then
    taskset -c "$reader_cpus" nice -n 10 python3 "$here/tools/shm_read_bench.py" "/dev/shm${name}" --interval 0.05 --duration $((secs + 30)) > "$out/$label.reader" 2>&1 &
    local reader_pid=$!
  fi
  "$bin" --name "$name" --fields "$fields" --cycles "$cycles" --period-us 1000 --cpu "$rt_cpu" --prio 80 > "$out/$label.writer"
  [[ "$reader" == yes ]] && wait "$reader_pid"
  rm -f "/dev/shm${name}"
  echo "== $label (fields=$fields reader=$reader)"; cat "$out/$label.writer"; [[ "$reader" == yes ]] && cat "$out/$label.reader"
  return 0
}

{
  run a7_noreader 40 no
  run a7_reader 40 yes
  run a25_noreader 375 no
  run a25_reader 375 yes
} | tee "$out/summary.txt"
