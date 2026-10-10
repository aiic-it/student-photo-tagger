@echo off
rem Builds StudentPhotoTagger.exe on this PC (needs Python from python.org, with "Add to PATH" ticked).
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :error
python -m unittest discover -s tests -t . || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name StudentPhotoTagger run.py || goto :error
echo.
echo Done. The app is in the "dist" folder: dist\StudentPhotoTagger.exe
pause
exit /b 0
:error
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
