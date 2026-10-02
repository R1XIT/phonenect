"""Конфиг агента: порт и токен доступа, хранятся в %APPDATA%\\phonenect."""
import hashlib
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
DEFAULT_SETUP_PORT = 8766  # открытый HTTP-порт со стартовой страницей: сертификат без секретов
MDNS_HOST = "phonenect.local"  # общее имя из первых версий: на нём настроены старые команды iPhone


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
    if not cfg.get("setup_port"):
        cfg["setup_port"] = DEFAULT_SETUP_PORT
        changed = True
    if changed:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


def pc_name(cfg: dict) -> str:
    """Как этот ПК видят телефоны и другие ПК. Можно задать полем "name" в config.json."""
    return (cfg.get("name") or socket.gethostname())[:40]


def pc_id(token: str) -> str:
    """Постоянный идентификатор ПК для поиска в сети. Из него токен не восстановить."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def mdns_host(token: str) -> str:
    """Своё имя ПК в сети: если ПК с Phonenect несколько, phonenect.local у них общий."""
    return f"phonenect-{pc_id(token)}.local"


def files_dir(cfg: dict) -> Path:
    """Куда складываются файлы с телефонов. Можно задать полем "files_dir" в config.json."""
    if cfg.get("files_dir"):
        path = Path(cfg["files_dir"])
    else:
        try:
            from win32com.shell import shell

            downloads = Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Downloads))
        except Exception:
            downloads = Path.home() / "Downloads"
        path = downloads / "Phonenect"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
