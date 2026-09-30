"""Конфиг агента: порт и токен доступа, хранятся в %APPDATA%\\phonenect."""
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "phonenect"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PORT = 8765
MDNS_HOST = "phonenect.local"


def load() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception:
            cfg = {}
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(16)
        changed = True
    if not cfg.get("port"):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


def wifi_ssid() -> str | None:
    """Имя Wi-Fi сети ПК — подсказка для команд iPhone, которые работают только дома."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return None
    for enc in ("utf-8", "oem"):
        try:
            text = out.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def lan_ip(cfg: dict | None = None) -> str:
    """IP в домашней сети. Можно задать вручную полем "host" в config.json."""
    if cfg and cfg.get("host"):
        return cfg["host"]
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        addrs = []

    # VPN-туннели часто сидят в 172.16/12 или 10/8, а Wi-Fi роутеры — в 192.168/16.
    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    candidates = [a for a in addrs if not a.startswith(("127.", "169.254."))]
    return min(candidates, key=rank) if candidates else "127.0.0.1"
