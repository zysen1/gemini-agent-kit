#!/bin/zsh
set -eu
cd -- "${0:A:h}"

for kit_python in python3 python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$kit_python" >/dev/null 2>&1 && "$kit_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        exec "$kit_python" scripts/kit_app.py --open "$@"
    fi
done

print -u2 '没有找到 Python 3.11+。请让你的 Agent 安装，或访问 https://www.python.org/downloads/ 。'
print -u2 '安装完成后，再双击此文件。'
read -r '?按回车关闭…'
exit 1
