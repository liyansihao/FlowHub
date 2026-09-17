#!/bin/bash
set -u
tool_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
python_runner=""
for candidate in "${FLOWHUB_PYTHON:-}" "$HOME"/Library/Application\ Support/FlowHub*/runtime/.venv/bin/python python3; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c 'import httpx, cryptography; import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    python_runner="$candidate"
    break
  fi
done
if [ -z "$python_runner" ]; then
  echo '未找到 FlowHub Python 运行环境。请先安装本机 FlowHub 后台，或设置 FLOWHUB_PYTHON 为其 Python 完整路径。'
  status=1
else
  args=()
  if [ "${1:-}" != '--check' ]; then args+=(--apply); fi
  "$python_runner" "$tool_dir/refresh.py" --config "$tool_dir/config.json" "${args[@]}"
  status=$?
fi
if [ -t 0 ]; then read -r -p '按回车关闭……' _; fi
exit "$status"
