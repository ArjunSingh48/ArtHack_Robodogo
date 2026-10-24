#!/usr/bin/env bash
# Run from any directory; additional arguments are passed to `itu show`.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/itu ]]; then
    cat >&2 <<'EOF'
Install the prototype first, from this repository:
  uv venv --python 3.12
  uv pip install -e '.[dev]'
Then rerun ./run_simulation.sh
EOF
    exit 1
fi

export APP_MODE=simulation ROBOT_MODEL=mock
export ROBOT_MOTION_ENABLED=false SHOW_MOTION_ENABLED=false
export OPENAI_VOICE_PROVIDER=none OPENAI_ENABLE_LIVE_CONVERSATION=false
export LOCAL_AUDIO_PLAYBACK=false

exec .venv/bin/itu show --dry-run --no-voice --source synthetic:sitting --display web "$@"
