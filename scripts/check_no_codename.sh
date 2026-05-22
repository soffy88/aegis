#!/usr/bin/env bash
set -euo pipefail
if grep -rn --include="*.py" --include="*.md" -i "helixa" aegis/; then
    echo "ERROR: internal codename found in source — remove before committing"
    exit 1
fi
