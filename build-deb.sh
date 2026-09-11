#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
command -v dpkg-deb >/dev/null || { echo '需要系统 dpkg-deb 工具'; exit 1; }
build_dir=$(mktemp -d)
trap 'rm -rf -- "$build_dir"' EXIT
mkdir -p "$build_dir/DEBIAN" "$build_dir/usr/share/deepseek-client/static" "$build_dir/usr/share/applications"
cat > "$build_dir/DEBIAN/control" <<'CONTROL'
Package: deepseek-client
Version: 1.0.0
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.7)
Recommends: x-www-browser, poppler-utils
Maintainer: Local Desktop Client <noreply@localhost>
Description: Local DeepSeek API desktop client for UOS
 Pure Python standard-library backend with a browser interface.
 Chat history, file text extraction and streamed API responses.
CONTROL
cp app.py extract.py index.html start.sh diagnose.sh README.md VALIDATION.md "$build_dir/usr/share/deepseek-client/"
cp static/app.js static/style.css "$build_dir/usr/share/deepseek-client/static/"
cat > "$build_dir/usr/share/applications/deepseek-client.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=DeepSeek 工作助手
Comment=本地 DeepSeek API 客户端
Exec=/bin/bash /usr/share/deepseek-client/start.sh
Icon=applications-office
Terminal=true
Categories=Office;
DESKTOP
find "$build_dir" -type d -exec chmod 755 {} +
find "$build_dir" -type f -exec chmod 644 {} +
chmod 755 "$build_dir/usr/share/deepseek-client/start.sh" "$build_dir/usr/share/deepseek-client/diagnose.sh"
dpkg-deb --root-owner-group --build "$build_dir" ../deepseek-client_1.0.0_all.deb
