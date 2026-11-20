#!/bin/sh
# POSIX entry point for macOS and Linux. Preserve argv, signals and exit codes.
set -eu
script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
exec python3 "$script_dir/run_agent.py" "$@"
