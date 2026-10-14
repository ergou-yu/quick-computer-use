@echo off
setlocal
set "QCU_INTERPRETER=%~dp0..\.venv\Scripts\python.exe"
if not exist "%QCU_INTERPRETER%" set "QCU_INTERPRETER=python"
if exist "%~dp0..\.venv\Scripts\pythonw.exe" set "QCU_INTERPRETER=%~dp0..\.venv\Scripts\pythonw.exe"
if defined QCU_PYTHON set "QCU_INTERPRETER=%QCU_PYTHON%"
pushd "%~dp0.."
"%QCU_INTERPRETER%" -X utf8 -m qcu %*
set "QCU_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %QCU_EXIT_CODE%
