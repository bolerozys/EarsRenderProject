#!/usr/bin/env bash
# ============================================================================
#  macOS 一键构建：生成可以直接双击运行的客户端
#
#      dist/PDF发票转图片.app     图形界面客户端（可拖进“应用程序”）
#      dist/pdf2img               命令行版
#
#  用法：
#      bash packaging/build_macos.sh              # 本机架构（推荐）
#      bash packaging/build_macos.sh --universal  # 通用二进制（Intel + Apple Silicon）
#      bash packaging/build_macos.sh --no-open    # 构建完不自动打开 Finder
#
#  特点：
#    * 用独立虚拟环境安装依赖，不污染系统 Python，也绕开 Homebrew 的
#      “externally-managed-environment” 限制
#    * 自动用系统自带 sips/iconutil 生成 .icns 图标
#    * 构建后自动做 ad-hoc 签名（Apple Silicon 上未签名会直接被系统杀掉）
#    * 自动去掉 quarantine 标记，减少“已损坏/无法验证”的提示
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="PDF发票转图片"
VENV="${PDF2IMG_VENV:-$ROOT/.venv-mac}"
PYTHON="${PYTHON:-python3}"
UNIVERSAL=0
OPEN_FINDER=1

for arg in "$@"; do
  case "$arg" in
    --universal|-u) UNIVERSAL=1 ;;
    --no-open)      OPEN_FINDER=0 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "未知参数：$arg（可用：--universal / --no-open）" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[提示]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then
  die "这个脚本要在 macOS 上运行（当前是 $(uname -s)）。"
fi

# ---------------------------------------------------------------- 1. 检查环境
say "[1/7] 检查 Python 与 Tk"
command -v "$PYTHON" >/dev/null 2>&1 || die "没有找到 $PYTHON。请先安装 Python 3.9+：https://www.python.org/downloads/macos/"

PY_VER="$("$PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PYTHON" - <<'PY' || die "Python 版本过低，需要 3.9 以上。"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
say "      使用 Python $PY_VER（$(command -v "$PYTHON")）"

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
  cat >&2 <<'MSG'
[错误] 当前 Python 缺少 tkinter（图形界面必需）。
       解决办法二选一：
         1) 用 Homebrew：  brew install python@3.12 python-tk@3.12
         2) 用官方安装包： https://www.python.org/downloads/macos/（自带 Tk，最省事）
MSG
  exit 1
fi

ARCH="$(uname -m)"
if [ "$UNIVERSAL" = "1" ]; then
  PY_ARCH="$("$PYTHON" -c 'import platform;print(platform.machine())')"
  if [ "$PY_ARCH" = "universal2" ]; then
    export PDF2IMG_TARGET_ARCH="universal2"
    say "      将构建通用二进制（Intel + Apple Silicon）"
  else
    warn "当前 Python 是 $PY_ARCH（不是 universal2），无法构建通用二进制，改为构建本机版本。"
    warn "如需通用二进制，请用 python.org 的官方安装包。"
    UNIVERSAL=0
  fi
fi

# ---------------------------------------------------------------- 2. 虚拟环境
say "[2/7] 准备虚拟环境（不影响系统 Python）"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV" || die "创建虚拟环境失败：$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install --quiet --upgrade pip

say "[3/7] 安装依赖（首次约 1-2 分钟）"
"$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
"$VPY" -m pip install --quiet --disable-pip-version-check -r requirements-build.txt

# ---------------------------------------------------------------- 4. 图标资源
say "[4/7] 生成图标与 .icns"
"$VPY" tools/make_icon.py

ICONSET="build/icon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
while read -r size name; do
  sips -z "$size" "$size" assets/icon.png --out "$ICONSET/icon_${name}.png" >/dev/null
done <<'SPECS'
16 16x16
32 16x16@2x
32 32x32
64 32x32@2x
128 128x128
256 128x128@2x
256 256x256
512 256x256@2x
512 512x512
1024 512x512@2x
SPECS
iconutil -c icns "$ICONSET" -o assets/icon.icns
say "      已生成 assets/icon.icns"

# ---------------------------------------------------------------- 5. 构建
say "[5/7] 开始打包（首次约 1-3 分钟，请稍候）"
"$VPY" -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath build \
  packaging/pdf2image.spec

APP_PATH="dist/$APP_NAME.app"
[ -d "$APP_PATH" ] || die "打包结束但没有找到 $APP_PATH，请把上面的完整输出发出来看看。"

# ---------------------------------------------------------------- 6. 签名
say "[6/7] 做 ad-hoc 签名（Apple Silicon 必需）"
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP_PATH" >/dev/null 2>&1 \
    && say "      已签名" \
    || warn "ad-hoc 签名失败（不影响本机使用，首次打开请右键 -> 打开）"
else
  warn "没有找到 codesign，跳过签名"
fi

# 去掉下载/拷贝带来的隔离标记
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

# 命令行版也检查一下
if [ -x dist/pdf2img ]; then
  xattr -dr com.apple.quarantine dist/pdf2img 2>/dev/null || true
fi

# ---------------------------------------------------------------- 7. 完成
say "[7/7] 完成"
cat <<MSG

客户端已生成：$(pwd)/$APP_PATH

  * 双击即可运行；建议把「$APP_NAME」拖进「应用程序」文件夹
  * 首次打开若提示“无法验证开发者”：在图标上右键 -> 打开 -> 再点“打开”
    （或执行：xattr -dr com.apple.quarantine "/Applications/$APP_NAME.app"）
  * 命令行版：$(pwd)/dist/pdf2img
  * 自检命令：$(pwd)/dist/pdf2img --selftest

MSG

if [ "$OPEN_FINDER" = "1" ]; then
  open dist 2>/dev/null || true
fi
