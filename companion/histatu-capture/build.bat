@echo off
rem ---------------------------------------------------------------------------
rem Build the Histatu Runner Windows .exe editions with Nuitka - single, self-
rem contained binaries (no Python needed on the user's machine).
rem
rem Editions (one source, chosen at build time via a generated _edition.py):
rem   full   -> HistatuRunner.exe         (default public build)
rem   lite   -> HistatuRunner-Lite.exe    (no detection-report uploader at all)
rem
rem Usage:  build.bat            builds both editions
rem         build.bat full       builds just one edition (full|lite)
rem
rem Nuitka (not PyInstaller) triggers far fewer AV false positives. Builds are
rem UNSIGNED, so first-run SmartScreen shows "Windows protected your PC" ->
rem "More info" -> "Run anyway". Signing needs a paid certificate.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo == Installing/updating build dependencies ==
py -3 -m pip install --upgrade nuitka pillow winsdk ordered-set zstandard
if errorlevel 1 goto :err

echo == Refreshing icon ==
py -3 make_icon.py

rem single source of truth: the __version__ = "x.y.z" line in histatu_runner.py
set VER=
for /f tokens^=2^ delims^=^" %%v in ('findstr /b /c:"__version__ = " histatu_runner.py') do set VER=%%v
if "%VER%"=="" (echo Could not read __version__ from histatu_runner.py & goto :err)

set WHICH=%~1
if "%WHICH%"=="" set WHICH=all
set DIDONE=0

if /i "%WHICH%"=="all"    ( call :build full HistatuRunner.exe & call :build lite HistatuRunner-Lite.exe & set DIDONE=1 )
if /i "%WHICH%"=="full"   ( call :build full   HistatuRunner.exe        & set DIDONE=1 )
if /i "%WHICH%"=="lite"   ( call :build lite   HistatuRunner-Lite.exe    & set DIDONE=1 )
if errorlevel 1 goto :err
if "%DIDONE%"=="0" ( echo Unknown edition "%WHICH%" ^(use full^|lite^|all^) & goto :err )

rem leave the working tree as the default so a plain `py histatu_runner.py` is "full"
del /q _edition.py 2>nul
echo.
echo == Done. Binaries are in build\ ==
exit /b 0

rem --- :build EDITION OUTPUT_EXE -------------------------------------------------
:build
set ED=%~1
set OUT=%~2
echo.
echo == Building %OUT% ^(edition=%ED%, v%VER%^) - this takes several minutes ==
> _edition.py echo EDITION = "%ED%"
py -3 -m nuitka ^
  --onefile ^
  --assume-yes-for-downloads ^
  --enable-plugin=tk-inter ^
  --windows-console-mode=disable ^
  --include-package=winsdk ^
  --include-package-data=winsdk ^
  --include-module=_edition ^
  --windows-icon-from-ico=icon.ico ^
  --company-name=Histatu ^
  --product-name="Histatu Runner" ^
  --file-description="Histatu dungeon runner overlay for Hytale" ^
  --file-version=%VER%.0 ^
  --product-version=%VER% ^
  --output-filename=%OUT% ^
  --output-dir=build ^
  histatu_runner.py
if errorlevel 1 exit /b 1
exit /b 0

:err
echo.
echo Build FAILED. See the messages above.
del /q _edition.py 2>nul
exit /b 1
