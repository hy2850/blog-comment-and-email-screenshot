@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  python -m venv .venv
  if errorlevel 1 goto error
)

set "PY=.venv\Scripts\python.exe"

echo Installing runtime dependencies...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo Installing build dependency...
"%PY%" -m pip install "pyinstaller>=6.10,<7"
if errorlevel 1 goto error

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo Building executable...
"%PY%" -m PyInstaller --noconfirm naver_comment_capture.spec
if errorlevel 1 goto error

if not exist "release" mkdir "release"
if not exist "release\NaverCommentCapture" mkdir "release\NaverCommentCapture"
if exist "release\NaverCommentCapture\NaverCommentCapture.exe" del /f /q "release\NaverCommentCapture\NaverCommentCapture.exe"
if exist "release\NaverCommentCapture\_internal" rmdir /s /q "release\NaverCommentCapture\_internal"
xcopy /e /i /y "dist\NaverCommentCapture" "release\NaverCommentCapture" >nul
if errorlevel 1 goto error

echo.
echo Build complete: release\NaverCommentCapture\NaverCommentCapture.exe
goto end

:error
echo.
echo Failed to build executable. Check the message above.
pause
endlocal
exit /b 1

:end
endlocal
exit /b 0
