#!/bin/bash
# Clean build input: no workspace keys, browser profile, volume or gateway grants.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends xfce4 xfce4-terminal xvfb x11vnc novnc websockify xdotool scrot x11-utils dbus-x11 nginx curl ca-certificates fonts-liberation
curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /home/tenki/chrome.deb
sudo apt-get install -y -qq /home/tenki/chrome.deb
rm /home/tenki/chrome.deb
sudo rm -f /etc/nginx/sites-enabled/default
printf 'server { listen 6099; server_tokens off; return 403; }\n' | sudo tee /etc/nginx/conf.d/band.conf >/dev/null
sudo tee /etc/systemd/system/band-display.service >/dev/null <<'UNIT'
[Unit]
Description=Band virtual display
[Service]
User=tenki
ExecStart=/usr/bin/Xvfb :0 -ac -screen 0 1280x800x24 -nolisten tcp
Restart=on-failure
UNIT
sudo tee /etc/systemd/system/band-desktop.service >/dev/null <<'UNIT'
[Unit]
Description=Band desktop
Requires=band-display.service
After=band-display.service
ConditionPathIsMountPoint=/home/tenki/workspace
[Service]
User=tenki
Environment=DISPLAY=:0
Environment=HOME=/home/tenki
ExecStartPre=/bin/sh -c 'for i in $(seq 1 40); do xdpyinfo >/dev/null 2>&1 && exit 0; sleep .25; done; exit 1'
ExecStart=/usr/bin/dbus-run-session /usr/bin/xfce4-session
Restart=on-failure
UNIT
for mode in control watch; do
  port=5900; web=6080; extra=''
  if [ "$mode" = watch ]; then port=5901; web=6081; extra=-viewonly; fi
  sudo tee /etc/systemd/system/band-vnc-$mode.service >/dev/null <<UNIT
[Unit]
Requires=band-desktop.service
After=band-desktop.service
[Service]
User=tenki
ExecStart=/usr/bin/x11vnc -display :0 -forever -shared -localhost -rfbport $port -nopw $extra
Restart=on-failure
UNIT
  sudo tee /etc/systemd/system/band-web-$mode.service >/dev/null <<UNIT
[Unit]
Requires=band-vnc-$mode.service
After=band-vnc-$mode.service
[Service]
User=tenki
ExecStart=/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:$web localhost:$port
Restart=on-failure
UNIT
done
sudo systemctl daemon-reload
sudo systemctl enable --now nginx
sudo nginx -t
echo 'Clean desktop image ready'
