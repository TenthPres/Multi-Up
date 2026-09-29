@echo off
REM Builds a self-contained multiup.exe (no Python needed on the target PC).
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --onefile --console --name multiup --collect-all pikepdf multiup.py || exit /b 1
echo.
echo Built dist\multiup.exe
