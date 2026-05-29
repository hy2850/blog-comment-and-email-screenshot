@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  python -m venv .venv
  if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip show playwright >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error
)

".venv\Scripts\python.exe" naver_comment_capture.py
goto end

:error
echo.
echo Failed to start the program. Check the message above.
pause

:end
endlocal
