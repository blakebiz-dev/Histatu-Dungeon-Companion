@echo off
rem ---------------------------------------------------------------------------
rem Build the Histatu Runner Windows .exe with Nuitka - a single, self-contained
rem binary (no Python needed on the user's machine).
rem
rem Usage:  build.bat            builds HistatuRunner.exe into build\
rem
rem Nuitka (not PyInstaller) triggers fewer AV false positives. Builds are
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

echo.
echo == Building HistatuRunner.exe ^(v%VER%^) - this takes several minutes ==
py -3 -m nuitka ^
  --onefile ^
  --assume-yes-for-downloads ^
  --enable-plugin=tk-inter ^
  --windows-console-mode=disable ^
  --include-package=winsdk ^
  --include-package-data=winsdk ^
  --windows-icon-from-ico=icon.ico ^
  --company-name=Histatu ^
  --product-name="Histatu Runner" ^
  --file-description="Histatu dungeon runner overlay for Hytale" ^
  --file-version=%VER%.0 ^
  --product-version=%VER% ^
  --output-filename=HistatuRunner.exe ^
  --output-dir=build ^
  histatu_runner.py
if errorlevel 1 goto :err

echo.
echo == Done. HistatuRunner.exe is in build\ ==
exit /b 0

:err
echo.
echo Build FAILED. See the messages above.
exit /b 1
