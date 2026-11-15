@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYCMD="
where python >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD (
  where py >nul 2>&1
  if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
  echo [错误] 没找到 Python 3。请先安装并勾选 Add Python to PATH。
  echo        下载: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
echo 检查当前汉化状态（不会改动任何文件）...
echo.
if not "%~1"=="" (
  %PYCMD% "%~dp0apply_zh_ui.py" --exe "%~1" --check
) else (
  %PYCMD% "%~dp0apply_zh_ui.py" --check
)
echo.
pause
