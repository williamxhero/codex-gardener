@echo off
setlocal
set "PYTHONPATH=%~dp0"

if /i "%~1"=="gardener" goto gardener
if /i "%~1"=="project_boundary" goto project_boundary
echo Codex Gardener hook wrapper received an unsupported module. 1>&2
exit /b 2

:gardener
if "%~2"=="" (
  echo Codex Gardener hook wrapper requires a lifecycle event. 1>&2
  exit /b 2
)
python -m gardener hook "%~2"
exit /b %errorlevel%

:project_boundary
python -m project_boundary
exit /b %errorlevel%
