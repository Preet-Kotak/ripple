@echo off
REM One-command setup for Windows: double-click setup.bat, or run it in a terminal
cd /d "%~dp0"
if not exist venv (python -m venv venv || goto :fail)
call venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt || goto :fail
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Created .env. Open it in Notepad, paste your Atlas connection string, save, and run setup.bat again.
  pause
  exit /b 1
)
python scripts\smoke_test.py || goto :fail
echo.
echo All good. Start the web page with:  venv\Scripts\activate  then  streamlit run app.py
pause
exit /b 0
:fail
echo.
echo Setup failed. Read the error above.
pause
exit /b 1
