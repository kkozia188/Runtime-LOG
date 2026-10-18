#!/usr/bin/env bash
# Build the optional runtime_log_recorder plugin into an existing RT-Control workspace install
# (merged, symlink install) without touching any other package. Records the source commit.
# Usage: deploy/build_recorder.sh <workspace_root>   e.g. ~/rt-control-electri127
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
ws="$(cd "$1" && pwd)"
[[ -f "$ws/install/.colcon_install_layout" && "$(cat "$ws/install/.colcon_install_layout")" == merged ]] \
  || { echo "$ws/install is not a merged colcon install" >&2; exit 1; }
commit="$(git -C "$here" rev-parse HEAD)"
dirty="$(git -C "$here" status --porcelain -- ros | wc -l)"
set +u; source /opt/ros/humble/setup.bash; source "$ws/install/setup.bash"; set -u
nice -n 10 colcon --log-base "$ws/log" build \
  --base-paths "$here/ros" \
  --build-base "$ws/build" \
  --install-base "$ws/install" \
  --merge-install --symlink-install \
  --packages-select runtime_log_recorder \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
share="$ws/install/share/runtime_log_recorder"
printf 'commit=%s\ndirty_files=%s\nbuilt=%s\n' "$commit" "$dirty" "$(date -Is)" > "$share/RUNTIME_LOG_BUILD"
echo "built runtime_log_recorder at ${commit:0:12} (dirty files under ros/: $dirty) -> $share"
