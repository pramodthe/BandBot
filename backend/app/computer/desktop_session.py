"""Guest-only systemd sessions. Inputs are server-allocated integers and hex IDs."""
import hashlib
import shlex

from app.computer.user_desktop import ports


def profile_root(key):
    return "/home/tenki/workspace/.band-desktops/" + hashlib.sha256(key.encode()).hexdigest()[:32]


def setup_script(display, key):
    if type(display) is not int or not 1 <= display <= 64:
        raise ValueError("Invalid desktop display")
    root = profile_root(key)
    control, watch = ports(display)
    units = {
        f"band-agent-display-{display}": f"""[Unit]
Description=Band agent display {display}
[Service]
User=tenki
ExecStart=/usr/bin/Xvfb :{display} -ac -screen 0 1280x800x24 -nolisten tcp
Restart=on-failure
""",
        f"band-agent-desktop-{display}": f"""[Unit]
Requires=band-agent-display-{display}.service
After=band-agent-display-{display}.service
[Service]
User=tenki
Environment=HOME=/home/tenki
Environment=DISPLAY=:{display}
Environment=XDG_CONFIG_HOME={root}/config
Environment=XDG_CACHE_HOME=/tmp/band-agent-cache-{display}
ExecStartPre=/bin/sh -c 'for i in $(seq 1 40); do xdpyinfo >/dev/null 2>&1 && exit 0; sleep .25; done; exit 1'
ExecStart=/usr/bin/dbus-run-session /usr/bin/xfce4-session
Restart=on-failure
""",
    }
    for name, web, vnc, extra in (("control", control, 10000 + display * 2, ""),
                                   ("watch", watch, 10001 + display * 2, "-viewonly")):
        units[f"band-agent-vnc-{display}-{name}"] = f"""[Unit]
Requires=band-agent-desktop-{display}.service
After=band-agent-desktop-{display}.service
[Service]
User=tenki
ExecStart=/usr/bin/x11vnc -display :{display} -forever -shared -localhost -rfbport {vnc} -nopw {extra}
Restart=on-failure
"""
        units[f"band-agent-web-{display}-{name}"] = f"""[Unit]
Requires=band-agent-vnc-{display}-{name}.service
After=band-agent-vnc-{display}-{name}.service
[Service]
User=tenki
ExecStart=/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:{web} localhost:{vnc}
Restart=on-failure
"""
    commands = [f"mkdir -p {root}/config /tmp/band-agent-cache-{display}"]
    for name, contents in units.items():
        commands.append("printf %s " + shlex.quote(contents) + " | sudo tee /etc/systemd/system/" + name + ".service >/dev/null")
    commands.extend(["sudo systemctl daemon-reload",
        f"sudo systemctl start band-agent-web-{display}-control band-agent-web-{display}-watch"])
    return "\n".join(commands)
