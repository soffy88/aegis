#!/usr/bin/env bash
set -euo pipefail
if grep -rn --include="*.py" -E 'Path\("/var/|Path\("/opt/|Path\("/etc/' aegis/; then
    echo "ERROR: hardcoded system Path() found — use sentinel pattern (see config.py)"
    exit 1
fi
