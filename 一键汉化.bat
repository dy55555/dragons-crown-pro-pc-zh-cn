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
echo 正在汉化启动器界面与游戏内 F1 菜单...
echo.
if not "%~1"=="" (
  %PYCMD% "%~dp0apply_zh_ui.py" --exe "%~1"
) else (
  %PYCMD% "%~dp0apply_zh_ui.py" %*
)
echo.
pause
