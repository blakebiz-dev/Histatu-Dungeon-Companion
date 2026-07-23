@echo off
rem ---------------------------------------------------------------------------
rem Build the Histatu Runner Windows app with Nuitka as a STANDALONE folder
rem (the exe + its DLLs; no Python needed on the user's machine), then zip it.
rem
rem Usage:  build.bat            builds build\HistatuRunner-windows.zip
rem
rem --standalone (not --onefile): the onefile bootstrap unpacks a payload to
rem temp at runtime, which is a top antivirus false-positive trigger. Builds are
rem UNSIGNED, so first-run SmartScreen shows "Windows protected your PC" ->
rem "More info" -> "Run anyway". Signing needs a certificate.
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
echo == Building HistatuRunner ^(standalone, v%VER%^) - this takes several minutes ==
py -3 -m nuitka ^
  --standalone ^
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

if not exist "build\histatu_runner.dist\HistatuRunner.exe" (echo Build produced no exe & goto :err)
if exist "build\HistatuRunner" rmdir /s /q "build\HistatuRunner"
ren "build\histatu_runner.dist" "HistatuRunner"
powershell -NoProfile -Command "Compress-Archive -Path 'build/HistatuRunner' -DestinationPath 'build/HistatuRunner-windows.zip' -Force"
if errorlevel 1 goto :err

echo.
echo == Done. build\HistatuRunner-windows.zip (unzip -^> run HistatuRunner\HistatuRunner.exe) ==
exit /b 0

:err
echo.
echo Build FAILED. See the messages above.
exit /b 1
