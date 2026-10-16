@echo off
rem ============================================================
rem  Windows 打包脚本：生成免安装的 dist\PDF发票转图片.exe 和 dist\pdf2img.exe
rem  用法：双击本文件，或在命令行执行 packaging\build_windows.bat
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.9+ 并勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

echo [1/4] 安装运行依赖...
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

echo [2/4] 安装打包依赖...
python -m pip install --disable-pip-version-check -r requirements-build.txt
if errorlevel 1 goto :failed

echo [3/4] 生成图标...
python tools\make_icon.py

echo [4/4] 开始打包（首次约 1-3 分钟）...
python -m PyInstaller --noconfirm --clean packaging\pdf2image.spec
if errorlevel 1 goto :failed

echo.
echo 打包完成，产物在 dist 目录：
echo   dist\PDF发票转图片.exe   图形界面版（发给别人直接双击即可，无需装 Python）
echo   dist\pdf2img.exe         命令行版
echo.
pause
exit /b 0

:failed
echo.
echo [失败] 打包过程中出现错误，请查看上面的输出。
pause
exit /b 1
