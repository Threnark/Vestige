@echo off
REM ---------------------------------------------------------------------------
REM Builds Vestige into a folder you can zip and share, using PyInstaller.
REM
REM RUN THIS FROM WINDOWS, in the project folder, with your virtual environment
REM ACTIVE (you should see "(.venv)" at the start of the prompt).
REM
REM Notes:
REM  * It uses "python -m ..." (the Python of the ACTIVE environment). The "py"
REM    launcher can pick a DIFFERENT Python than your environment, which makes the
REM    build look for packages in the wrong place.
REM  * It builds a FOLDER (--onedir), not a single .exe. A single .exe unpacks
REM    itself into a temporary folder on every launch; a folder doesn't, which fits
REM    Vestige's "leave no files behind" goal. Write mode's Vestige_DATA folder
REM    then sits right next to Vestige.exe, in plain sight.
REM  * --collect-all customtkinter is required: CustomTkinter ships theme and
REM    asset files that PyInstaller would otherwise leave out.
REM  * --add-data "assets;assets" bundles the icon files; --icon sets the .exe icon.
REM  * Build on the OS you want to run on (no cross-building).
REM ---------------------------------------------------------------------------

echo Using this Python:
python -c "import sys; print(sys.executable); print(sys.version)"
echo.

python -m pip install --upgrade pyinstaller customtkinter pillow anthropic certifi
if errorlevel 1 goto :fail

REM Optional: lets the program import Outlook / Hotmail .pst export files. If it can't be
REM installed (no pre-built package for this Python and no C compiler), the build still
REM works; that build simply can't import .pst files and says so when asked to.
set PST_ARGS=
python -m pip install --upgrade libpff-python
if errorlevel 1 (
    echo.
    echo WARNING: libpff-python could not be installed. This build will NOT be able to import .pst files.
    echo.
) else (
    set PST_ARGS=--hidden-import pypff
)

python -m PyInstaller --noconfirm --clean --windowed --onedir ^
    --name "Vestige" ^
    --icon "assets\vestige.ico" ^
    --add-data "assets;assets" ^
    --collect-all customtkinter ^
    --collect-all anthropic ^
    --collect-all certifi ^
    %PST_ARGS% ^
    gui_app.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo Done. The app is the folder:  dist\Vestige\
echo Run:   dist\Vestige\Vestige.exe
echo Share: zip the whole Vestige folder.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo The build failed - see the messages above.
pause
exit /b 1
