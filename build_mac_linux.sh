#!/bin/bash
# Builds Vestige with PyInstaller. Run it on the OS you want the result for
# (macOS builds a .app, Linux builds a folder), with your virtual environment ACTIVE.
#
# It builds a FOLDER/.app (--onedir) rather than one self-extracting file, so nothing is
# unpacked into temporary folders at launch and write mode's Vestige_DATA folder sits
# next to the program in plain sight.
set -e

echo "Using this Python:"
python3 -c "import sys; print(sys.executable); print(sys.version)"
echo

python3 -m pip install --upgrade pyinstaller customtkinter pillow anthropic certifi

# Optional: lets the program import Outlook / Hotmail .pst export files. If it can't be
# installed (no pre-built package for this platform and no C compiler) the build still
# works; that build simply can't import .pst files and says so when asked to.
PST_ARGS=()
if python3 -m pip install --upgrade libpff-python; then
    PST_ARGS=(--hidden-import pypff)
else
    echo "WARNING: libpff-python could not be installed. This build will NOT be able to import .pst files."
fi

ICON_ARGS=()
if [[ "$OSTYPE" == "darwin"* ]]; then
    ICON_ARGS=(--icon "assets/vestige.icns")
fi

python3 -m PyInstaller --noconfirm --clean --windowed --onedir \
    --name "Vestige" \
    "${ICON_ARGS[@]}" \
    --add-data "assets:assets" \
    --collect-all customtkinter \
    --collect-all anthropic \
    --collect-all certifi \
    "${PST_ARGS[@]}" \
    gui_app.py

echo
echo "============================================================"
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "Done: dist/Vestige.app"
else
    echo "Done: dist/Vestige/Vestige"
fi
echo "Share by zipping the whole output (the app is a folder, not one file)."
echo "============================================================"
