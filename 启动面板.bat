@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto run_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto run_python
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto run_python3

echo 没有找到 Python 3.11+。请让你的 Agent 安装，或访问 https://www.python.org/downloads/ 。
echo 安装完成后，再双击此文件。
pause
exit /b 1

:run_py
py -3 scripts\kit_app.py --open %*
set "KIT_EXIT=%errorlevel%"
goto finished

:run_python
python scripts\kit_app.py --open %*
set "KIT_EXIT=%errorlevel%"
goto finished

:run_python3
python3 scripts\kit_app.py --open %*
set "KIT_EXIT=%errorlevel%"

:finished
if not "%KIT_EXIT%"=="0" pause
exit /b %KIT_EXIT%
