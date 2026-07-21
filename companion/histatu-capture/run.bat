@echo off
rem Histatu Runner - installs dependencies on first run, then starts the overlay.
cd /d "%~dp0"
py -3 -c "import PIL, winsdk" 2>nul || py -3 -m pip install --user pillow winsdk
py -3 histatu_runner.py %*
pause
