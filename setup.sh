#!/usr/bin/env bash
set -euo pipefail
# The module runs at the repository root, but a user-entered relative
# private backup filename should resolve from the invoking directory.
export LOCAL_SEARCH_MCP_SETUP_CALLER_CWD="$(pwd -P)"
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
exec python3 -m scripts.setup_wizard "$@"
