@echo off
rem ============================================================
rem  Windows 一键启动（源码方式，无需打包）
rem  首次运行会自动安装依赖；之后直接双击即可打开界面。
rem  也可以把 PDF 文件拖到本文件上，或用命令行传参：
rem      run_windows.bat --cli 发票.pdf -o 输出目录
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.9+ 并勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

python -c "import pymupdf, PIL, tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖...
    python -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

if "%~1"=="" (
    rem 无参数：打开图形界面（pythonw 不弹控制台）
    start "" pythonw pdf2image_app.py
) else (
    rem 有参数：走命令行，保留控制台以便看到输出
    python pdf2image_app.py %*
)
exit /b 0
