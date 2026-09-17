#!/bin/bash
tool_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "$tool_dir/Refresh-Mac.command" --check
