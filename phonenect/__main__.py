"""Точка входа: сервер в фоне, слежение за буфером, иконка в трее."""
import argparse
import asyncio
import os
import sys
import threading
import webbrowser
import winreg

from aiohttp import web
from PIL import Image, ImageDraw

from . import config
from .hub import Hub
from .server import create_app


def run_server(hub: Hub, cfg: dict, base_url: str, ready: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hub.loop = loop
    runner = web.AppRunner(create_app(hub, cfg, base_url))
    loop.run_until_complete(runner.setup())
    try:
        loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", cfg["port"]).start())
    except OSError as e:
        # Порт занят — скорее всего, Phonenect уже запущен.
        hub.error = e
        ready.set()
        return
    ready.set()
    loop.run_forever()


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'cmd /c cd /d "{project}" && start "" "{pythonw}" -m phonenect'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "Phonenect")
            return True
    except OSError:
        return False


def toggle_autostart(icon, item) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if autostart_enabled():
            winreg.DeleteValue(key, "Phonenect")
        else:
            winreg.SetValueEx(key, "Phonenect", 0, winreg.REG_SZ, autostart_command())


def tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((10, 8, 54, 60), radius=8, fill=(40, 110, 235))
    d.rounded_rectangle((22, 2, 42, 16), radius=4, fill=(230, 236, 250))
    for y in (26, 36, 46):
        d.line((19, y, 45, y), fill=(230, 236, 250), width=4)
    return img


def main() -> None:
    parser = argparse.ArgumentParser(prog="phonenect")
    parser.add_argument("--no-tray", action="store_true", help="без иконки в трее (консольный режим)")
    args = parser.parse_args()

    cfg = config.load()
    base_url = f"http://{config.lan_ip(cfg)}:{cfg['port']}"
    local_pair = f"http://127.0.0.1:{cfg['port']}/pair"

    hub = Hub()
    ready = threading.Event()
    threading.Thread(target=run_server, args=(hub, cfg, base_url, ready), daemon=True).start()
    ready.wait()
    if getattr(hub, "error", None):
        print(f"Не удалось занять порт {cfg['port']}: {hub.error}. Phonenect уже запущен?")
        sys.exit(1)
    threading.Thread(target=hub.watch_clipboard, daemon=True).start()

    print(f"Phonenect запущен: {base_url}")
    print(f"Подключить телефон (QR): {local_pair}")

    if args.no_tray:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

    import pystray

    def toggle_pause(icon, item):
        hub.paused = not hub.paused

    def quit_app(icon, item):
        hub.stop()
        icon.stop()

    icon = pystray.Icon(
        "phonenect",
        tray_icon(),
        "Phonenect — общий буфер",
        menu=pystray.Menu(
            pystray.MenuItem("Подключить телефон…", lambda: webbrowser.open(local_pair), default=True),
            pystray.MenuItem("Пауза синхронизации", toggle_pause, checked=lambda item: hub.paused),
            pystray.MenuItem("Запускать вместе с Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
            pystray.MenuItem("Открыть папку настроек", lambda: os.startfile(config.CONFIG_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", quit_app),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
