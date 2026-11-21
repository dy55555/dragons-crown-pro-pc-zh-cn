@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PYCMD="

rem 逐个候选 Python，挑第一个能 import tkinter 的（避免选到精简版）
for %%P in ("C:\Python313\python.exe" "C:\Python312\python.exe" "C:\Python311\python.exe") do (
  if exist %%~P (
    %%~P -c "import tkinter" >nul 2>&1
    if not errorlevel 1 (
      set "PYCMD=%%~P"
      goto :found
    )
  )
)
for %%C in (python py) do (
  where %%C >nul 2>&1
  if not errorlevel 1 (
    if "%%C"=="py" (
      py -3 -c "import tkinter" >nul 2>&1
      if not errorlevel 1 (
        set "PYCMD=py -3"
        goto :found
      )
    ) else (
      python -c "import tkinter" >nul 2>&1
      if not errorlevel 1 (
        set "PYCMD=python"
        goto :found
      )
    )
  )
)
:found
if not defined PYCMD (
  echo [错误] 没找到带图形界面(tkinter)的 Python 3。
  echo        请安装官方 Python: https://www.python.org/downloads/
  echo        安装时勾选 "Add Python to PATH" 和 "tcl/tk and IDLE"。
  echo.
  pause
  exit /b 1
)
echo 正在启动存档编辑器...
echo 提醒：编辑前请先完全退出游戏！
echo.
%PYCMD% "%~dp0save_editor_gui.py"
if errorlevel 1 (
  echo.
  echo [错误] 编辑器启动失败，请把上面的报错反馈。
  pause
)
