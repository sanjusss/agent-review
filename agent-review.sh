#!/bin/bash
# 保留 Bash 入口；复杂状态和结果校验由 Python 标准库实现。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/scripts/agent_review.py" "$@"
