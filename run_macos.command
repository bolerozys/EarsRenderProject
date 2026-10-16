#!/usr/bin/env bash
# ============================================================================
#  macOS 一键启动（源码方式，无需打包）
#  首次双击前先执行一次： chmod +x run_macos.command
#  之后双击即可打开界面；也可以传参数走命令行：
#      ./run_macos.command --cli 发票.pdf -o 输出目录
#
#  依赖装在项目内的 .venv-mac 虚拟环境里，不会动系统 Python，
#  也避免 Homebrew Python 的 “externally-managed-environment” 报错。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV="${PDF2IMG_VENV:-$PWD/.venv-mac}"

alert() {
  /usr/bin/osascript -e "display alert \"$1\" message \"$2\" as critical" >/dev/null 2>&1 || true
  echo "[错误] $1 - $2" >&2
}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  alert "未找到 python3" "请先安装 Python 3（推荐 python.org 官方安装包，自带 Tk）。"
  exit 1
fi

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
  alert "当前 Python 缺少 tkinter" "Homebrew 用户执行：brew install python-tk；或改用 python.org 的安装包。"
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "首次运行，正在创建虚拟环境并安装依赖..."
  "$PYTHON" -m venv "$VENV"
fi

VPY="$VENV/bin/python"
if ! "$VPY" -c "import pymupdf, PIL, tkinterdnd2" >/dev/null 2>&1; then
  echo "正在安装依赖..."
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
fi

exec "$VPY" pdf2image_app.py "$@"
