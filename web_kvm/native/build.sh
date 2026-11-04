#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app="$base/Camera.app"
mkdir -p "$app/Contents/MacOS"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.web-kvm.capture</string>
<key>CFBundleName</key><string>Remote Desk Capture</string>
<key>CFBundleDisplayName</key><string>Remote Desk Capture</string>
<key>CFBundleExecutable</key><string>Capture</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>NSCameraUsageDescription</key><string>Capture the HD33 HDMI input for your local Web KVM.</string>
</dict></plist>
PLIST
/usr/bin/swiftc -O "$base/native/Capture.swift" -o "$app/Contents/MacOS/Capture" -framework AVFoundation -framework CoreImage -framework ImageIO
/usr/bin/codesign --force --sign - --identifier local.web-kvm.capture "$app"
