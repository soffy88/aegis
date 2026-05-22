#!/usr/bin/env bash
set -euo pipefail
if grep -rn --include="*.py" --include="*.ts" --include="*.tsx" \
    -E '(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36,}|AIza[a-zA-Z0-9]{35})' .; then
    echo "ERROR: possible inline API key found — use env var instead"
    exit 1
fi
