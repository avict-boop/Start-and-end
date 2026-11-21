#!/bin/bash
# Сборка dosrip.app и dosrip.dmg для Apple Silicon. Запускать на Mac с Apple Silicon из корня репозитория.
set -euo pipefail

VERSION=$(python3 -c "import dosrip; print(dosrip.__version__)")
BUILD=build/macos
rm -rf "$BUILD" dist/dosrip.app dist/dosrip-*.dmg
mkdir -p "$BUILD"

python3 packaging/macos/make_icon.py "$BUILD"

python3 -m PyInstaller --noconfirm --clean --windowed \
  --name dosrip \
  --target-arch arm64 \
  --icon "$BUILD/dosrip.icns" \
  --osx-bundle-identifier io.github.dosrip \
  --collect-all capstone \
  --collect-all unicorn \
  --collect-submodules webview \
  --workpath "$BUILD/pyinstaller" \
  --specpath "$BUILD" \
  packaging/macos/launcher.py

APP=dist/dosrip.app
PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace LSMinimumSystemVersion -string "11.0" "$PLIST"
plutil -replace NSHighResolutionCapable -bool true "$PLIST"
plutil -replace CFBundleDisplayName -string "dosrip" "$PLIST"

# после правки Info.plist подпись нужно обновить (ad-hoc: без аккаунта Apple Developer)
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "== Проверка архитектуры"
file "$APP/Contents/MacOS/dosrip"
echo "== Самопроверка собранного приложения"
"$APP/Contents/MacOS/dosrip" --selftest

DMG_DIR="$BUILD/dmg"
mkdir -p "$DMG_DIR"
cp -R "$APP" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"
hdiutil create -volname "dosrip" -srcfolder "$DMG_DIR" -ov -format UDZO "dist/dosrip-$VERSION-arm64.dmg"
echo "Готово: dist/dosrip-$VERSION-arm64.dmg"
