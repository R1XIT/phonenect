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

from . import android_setup, config
from .hub import Hub
from .mdns import Advertiser
from .peers import Peers
from .server import create_app


def run_server(hub: Hub, cfg: dict, base_url: str, ready: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hub.loop = loop
    peers = Peers(hub, cfg)
    runner = web.AppRunner(create_app(hub, cfg, base_url, peers))
    loop.run_until_complete(runner.setup())
    try:
        loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", cfg["port"]).start())
    except OSError as e:
        # Порт занят — скорее всего, Phonenect уже запущен.
        hub.error = e
        ready.set()
        return
    ready.set()
    loop.call_soon(peers.start)
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


def pick_files() -> list[str]:
    """Стандартное окно выбора нескольких файлов."""
    import win32con
    import win32gui

    try:
        result = win32gui.GetOpenFileNameW(
            Title="Отправить на телефон",
            Flags=win32con.OFN_ALLOWMULTISELECT | win32con.OFN_EXPLORER | win32con.OFN_FILEMUSTEXIST,
            MaxFile=65536,
        )[0]
    except win32gui.error:  # нажали «Отмена»
        return []
    # Один файл — полный путь; несколько — папка и имена через нулевой символ.
    parts = result.split(chr(0))
    if len(parts) == 1:
        return parts
    return [os.path.join(parts[0], name) for name in parts[1:] if name]


def main() -> None:
    parser = argparse.ArgumentParser(prog="phonenect")
    parser.add_argument("--no-tray", action="store_true", help="без иконки в трее (консольный режим)")
    parser.add_argument("--setup-android", action="store_true", help="настроить Android-телефон по USB и выйти")
    args = parser.parse_args()

    cfg = config.load()
    base_url = f"http://{config.lan_ip(cfg)}:{cfg['port']}"
    local_pair = f"http://127.0.0.1:{cfg['port']}/pair"

    if args.setup_android:
        try:
            print("Готово:", android_setup.setup(base_url, cfg["token"], config.pc_name(cfg)))
        except android_setup.SetupError as e:
            print("Ошибка:", e)
            sys.exit(1)
        return

    files_dir = config.files_dir(cfg)
    hub = Hub(files_dir, config.pc_name(cfg), config.pc_id(cfg["token"]))
    ready = threading.Event()
    threading.Thread(target=run_server, args=(hub, cfg, base_url, ready), daemon=True).start()
    ready.wait()
    if getattr(hub, "error", None):
        print(f"Не удалось занять порт {cfg['port']}: {hub.error}. Phonenect уже запущен?")
        sys.exit(1)
    threading.Thread(target=hub.watch_clipboard, daemon=True).start()
    mdns = Advertiser(cfg)
    threading.Thread(target=mdns.run, daemon=True).start()

    print(f"Phonenect запущен: {base_url}")
    print(f"Подключить телефон (QR): {local_pair}")

    if args.no_tray:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

    import pystray

    def send_files(icon, item):
        def work():
            paths = pick_files()
            if paths:
                hub.send_files(paths, explicit=True)
                icon.notify(f"Отправлено на телефоны: {len(paths)} шт.", "Phonenect")

        threading.Thread(target=work, daemon=True).start()

    def toggle_pause(icon, item):
        hub.paused = not hub.paused

    def setup_android(icon, item):
        def work():
            notify = lambda text: icon.notify(text, "Phonenect")
            try:
                model = android_setup.setup(base_url, cfg["token"], config.pc_name(cfg), log=notify)
                notify(f"{model} настроен: буфер теперь общий автоматически.")
            except android_setup.SetupError as e:
                notify(str(e))
            except Exception as e:
                notify(f"Не получилось: {e}")

        threading.Thread(target=work, daemon=True).start()

    def quit_app(icon, item):
        hub.stop()
        mdns.stop()
        icon.stop()

    icon = pystray.Icon(
        "phonenect",
        tray_icon(),
        "Phonenect — общий буфер",
        menu=pystray.Menu(
            pystray.MenuItem("Подключить телефон…", lambda: webbrowser.open(local_pair), default=True),
            pystray.MenuItem("Связать с другим ПК…", lambda: webbrowser.open(local_pair + "#pc")),
            pystray.MenuItem("Отправить файлы на телефон…", send_files),
            pystray.MenuItem("Открыть полученные файлы", lambda: os.startfile(files_dir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Настроить Android по USB", setup_android),
            pystray.MenuItem("Пауза синхронизации", toggle_pause, checked=lambda item: hub.paused),
            pystray.MenuItem("Запускать вместе с Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
            pystray.MenuItem("Открыть папку настроек", lambda: os.startfile(config.CONFIG_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", quit_app),
        ),
    )
    hub.on_file = lambda clip: icon.notify(
        f"{clip.name} — в папке «{files_dir.name}» и в буфере (Ctrl+V)", f"Файл от {clip.source}"
    )
    icon.run()


if __name__ == "__main__":
    main()
