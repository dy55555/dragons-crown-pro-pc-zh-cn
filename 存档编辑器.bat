@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PYCMD="

rem Pick first Python that has tkinter (skip stripped-down builds)
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
  echo [ERROR] No Python 3 with tkinter found.
  echo Install official Python from https://www.python.org/downloads/
  echo Check "tcl/tk and IDLE" during install.
  echo.
  pause
  exit /b 1
)
echo Starting save editor...
echo NOTE: Quit the game completely before editing!
echo.
%PYCMD% "%~dp0save_editor_gui.py"
if errorlevel 1 (
  echo.
  echo [ERROR] Editor failed to start. Report the message above.
  pause
)
