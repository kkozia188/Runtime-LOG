#!/usr/bin/env bash
# Install or update the user service from this checkout (run on the target, as the robot user).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$here" == "$HOME/Runtime-LOG" ]] || { echo "expected checkout at ~/Runtime-LOG, got $here" >&2; exit 1; }
python3 -c 'import yaml' || { echo "python3-yaml missing" >&2; exit 1; }
command -v ethercat >/dev/null || echo "WARN: ethercat CLI not found; master polling will record errors only"
mkdir -p "$HOME/.config/systemd/user" "$HOME/runtime-log/runs"
install -m 644 "$here/systemd/runtime-log.service" "$HOME/.config/systemd/user/runtime-log.service"
[[ -f "$HOME/.config/runtime-log.env" ]] || printf 'RUNTIME_LOG_SERVE_ARGS=\n' > "$HOME/.config/runtime-log.env"
systemctl --user daemon-reload
if [[ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]]; then
  echo "NOTE: linger is off; run once: sudo loginctl enable-linger $USER (otherwise the service stops at logout)"
fi
echo "installed; start with: systemctl --user enable --now runtime-log"
