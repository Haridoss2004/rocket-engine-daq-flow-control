@echo off
cd /d "%~dp0"
python -m pytest Tests -v -p no:cacheprovider
pause
