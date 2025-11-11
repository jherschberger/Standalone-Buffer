@echo off
setlocal

REM Root of the project (folder with backend\ and frontend\)
set "ROOT=%~dp0"
for %%I in ("%ROOT%.") do set "ROOT=%%~fI\"

echo === Live Audio: Starting Backend and Frontend ===

REM ---------------- Backend Setup ----------------
pushd "%ROOT%backend" || (
  echo ERROR: Could not change directory to backend
  exit /b 1
)

REM Stop any stale uvicorn to avoid old code serving on port 8000
taskkill /im uvicorn.exe /f >nul 2>&1

if not exist .venv ( 
  echo Creating Python virtual environment...
  py -m venv .venv || (
    echo ERROR: Failed to create virtual environment. Ensure Python is installed and 'py' is on PATH.
    popd & exit /b 1
  )
)

echo Installing backend dependencies...
"%CD%\.venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
"%CD%\.venv\Scripts\python.exe" -m pip install -r requirements.txt || (
  echo ERROR: Failed to install backend dependencies.
  popd & exit /b 1
)

if not exist app\__init__.py (
  type nul > app\__init__.py
)

REM Warn if ffmpeg is not found
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo WARNING: 'ffmpeg' not found on PATH. Please install FFmpeg or set FFMPEG_PATH.
)

echo Starting backend server window...
start "Backend" cmd /k ""%CD%\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
popd

REM ---------------- Frontend Setup ----------------
pushd "%ROOT%frontend" || (
  echo ERROR: Could not change directory to frontend
  exit /b 1
)

if not exist node_modules (
  echo Installing frontend dependencies...
  call npm install || (
    echo ERROR: Failed to install frontend dependencies. Ensure Node.js and npm are installed.
    popd & exit /b 1
  )
)

echo Starting frontend dev server window...
start "Frontend" cmd /k "npm run dev"
popd

echo.
echo Backend:   http://localhost:8000
echo Frontend:  http://localhost:5173
echo.
echo Two new Command Prompt windows were opened: "Backend" and "Frontend".
echo If the browser doesn't open automatically, visit http://localhost:5173

endlocal
exit /b 0


