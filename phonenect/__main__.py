"""Точка входа: сервер в фоне, слежение за буфером, иконка в трее."""
import argparse
import asyncio
import concurrent.futures
import errno
import os
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
import webbrowser
import winreg

from aiohttp import web
from PIL import Image, ImageDraw

from . import android_setup, certs, config
from .hub import Hub
from .mdns import Advertiser
from .network import NetworkGuard
from .peers import Peers
from .server import create_app, create_setup_app


class TlsKeeper:
    """Держит TLS-контекст сервера и перевыпускает сертификат, когда меняется IP."""

    def __init__(self, cfg: dict, cert_dir: Path) -> None:
        self.cfg, self.dir = cfg, cert_dir
        self.ca = certs.ensure_ca(cert_dir, config.pc_name(cfg))
        self.fp = certs.fingerprint(self.ca)
        self._ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._lock = threading.Lock()
        self._dirty = True  # в контексте ещё нет (актуального) сертификата
        self.loop: asyncio.AbstractEventLoop | None = None  # цикл сервера: перечитываем сертификат на нём

    def context(self) -> ssl.SSLContext:
        return self._ctx

    def _load(self) -> None:
        self._ctx.load_cert_chain(*certs.server_files(self.dir))  # действует для новых соединений

    def _reload(self) -> None:
        loop = self.loop
        if loop is None or not loop.is_running():
            self._load()
            return
        # Не во время рукопожатия: контекст читает поток сервера, поэтому меняем его на нём же.
        done: concurrent.futures.Future = concurrent.futures.Future()

        def work() -> None:
            try:
                self._load()
                done.set_result(None)
            except BaseException as e:
                done.set_exception(e)

        loop.call_soon_threadsafe(work)
        done.result(timeout=10)

    def refresh(self, ip: str) -> bool:
        with self._lock:  # из трея и из фонового потока
            new = certs.ensure_server_cert(self.dir, ip, config.pc_id(self.cfg["token"]))
            if new:
                self._dirty = True  # файлы уже перезаписаны; если загрузка упадёт, повторим в следующий раз
            if self._dirty:
                self._reload()
                self._dirty = False
            return new


_PORT_BUSY_WINERRORS = (10048, 10013)  # WSAEADDRINUSE, WSAEACCES (порт занят другим процессом)


def _port_busy(e: OSError) -> bool:
    return e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) in _PORT_BUSY_WINERRORS


def retry_os_error(start, attempts: int = 10, pause: float = 1.0):
    """Занять порт с повторами: после перезапуска старый процесс освобождает порты не мгновенно.

    Повторяем только при «адрес занят»; другие ошибки и последняя неудача пробрасываются."""
    for n in range(attempts):
        try:
            return start()
        except OSError as e:
            if not _port_busy(e) or n == attempts - 1:
                raise
            time.sleep(pause)


def restart_command() -> tuple[list[str], str]:
    """Команда и рабочая папка для запуска нового экземпляра тем же интерпретатором и с теми же аргументами."""
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-m", "phonenect", *sys.argv[1:]], project


REVOKE_LOCK = threading.Lock()


def revoke_all_devices(confirm, rotate, spawn, show, quit_, lock=REVOKE_LOCK) -> None:
    """«Отключить все устройства»: после подтверждения процесс обязан остановиться в любом случае,
    иначе отозванный ключ продолжал бы работать. Всё внешнее передаётся извне (для тестов)."""
    if not lock.acquire(blocking=False):
        return  # окно уже открыто или отзыв идёт
    if not confirm():
        lock.release()
        return
    problem = None
    try:
        rotate()
    except Exception as e:
        problem = f"Не удалось сохранить новый ключ доступа: {e}. Phonenect остановлен."
    else:
        try:
            spawn()
        except Exception:
            problem = "Ключ доступа заменён, но Phonenect не смог перезапуститься — запустите его вручную."
    try:
        if problem:
            show(problem)
    finally:
        quit_()  # замок не отпускаем: процесс завершается


def run_server(hub: Hub, cfg: dict, tls: TlsKeeper, ready: threading.Event, guard: NetworkGuard) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hub.loop = loop
    tls.loop = loop
    base =lambda: f"https://{config.lan_ip(cfg)}:{cfg['port']}"  # IP может смениться на ходу
    peers = Peers(hub, cfg, guard)
    runner = web.AppRunner(create_app(hub, cfg, base, peers, tls.dir, tls.fp, guard))
    setup_runner = web.AppRunner(create_setup_app(cfg, tls.dir, tls.fp, base, peers, guard))
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(setup_runner.setup())
    try:
        # Порты могут ещё держаться за прежним процессом (перезапуск из трея): ждём до ~10 с.
        retry_os_error(lambda: loop.run_until_complete(
            web.TCPSite(runner, "0.0.0.0", cfg["port"], ssl_context=tls.context()).start()))
        retry_os_error(lambda: loop.run_until_complete(
            web.TCPSite(setup_runner, "0.0.0.0", cfg["setup_port"]).start()))
    except OSError as e:
        # Порт занят — скорее всего, Phonenect уже запущен.
        hub.error = e
        ready.set()
        return
    ready.set()
    loop.call_soon(peers.start)
    loop.run_forever()


TITLE = "Phonenect — общий буфер"
TITLE_UNTRUSTED = "Phonenect — сеть не доверенная, синхронизация выключена"
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
    guard = NetworkGuard(cfg)
    guard.migrate()  # первый запуск этой версии: текущая сеть становится доверенной
    guard.refresh()
    try:
        tls = TlsKeeper(cfg, config.CONFIG_DIR)
        tls.refresh(config.lan_ip(cfg))
    except Exception as e:
        # Под pythonw консоли нет: без окна запуск провалился бы молча.
        text = f"Не удалось подготовить сертификат для защищённой связи: {e}"
        if args.no_tray or args.setup_android:
            print(text)
        else:
            import win32api
            import win32con

            win32api.MessageBox(0, text, "Phonenect", win32con.MB_ICONERROR)
        sys.exit(1)

    def current_base() -> str:
        # IP мог смениться с запуска: адрес считаем в момент использования, сертификат обновляем под него.
        ip = config.lan_ip(cfg)
        tls.refresh(ip)
        return f"https://{ip}:{cfg['port']}"

    base_url = f"https://{config.lan_ip(cfg)}:{cfg['port']}"
    # Браузер ПК не знает нашего CA, поэтому страница подключения открывается по HTTP стартового порта
    # (она и так пускает только запросы с самого ПК).
    local_pair = f"http://127.0.0.1:{cfg['setup_port']}/pair"

    if args.setup_android:
        try:
            print("Готово:", android_setup.setup(current_base(), cfg["token"], config.pc_name(cfg), tls.fp))
        except android_setup.SetupError as e:
            print("Ошибка:", e)
            sys.exit(1)
        return

    files_dir = config.files_dir(cfg)
    hub = Hub(files_dir, config.pc_name(cfg), config.pc_id(cfg["token"]))
    hub.guard = guard
    ready = threading.Event()
    threading.Thread(target=run_server, args=(hub, cfg, tls, ready, guard), daemon=True).start()
    ready.wait()
    if getattr(hub, "error", None):
        print(f"Не удалось занять порт {cfg['port']}: {hub.error}. Phonenect уже запущен?")
        sys.exit(1)
    threading.Thread(target=hub.watch_clipboard, daemon=True).start()
    mdns = Advertiser(cfg, guard)
    threading.Thread(target=mdns.run, daemon=True).start()

    stop_tls = threading.Event()

    def keep_tls_fresh() -> None:
        # Раз в минуту: сменился IP — выпускаем сертификат под новый адрес.
        while not stop_tls.wait(60):
            try:
                tls.refresh(config.lan_ip(cfg))
            except Exception as e:
                print("Не удалось обновить сертификат:", e)

    threading.Thread(target=keep_tls_fresh, daemon=True).start()

    icon = None  # создаётся ниже; без трея остаётся None

    def network_changed() -> None:
        mdns.poke()  # объявить ПК или снять объявление сразу
        if not guard.trusted and hub.loop:
            asyncio.run_coroutine_threadsafe(hub.close_remote_sockets(), hub.loop)  # открытые связи рвём
        if icon:
            try:
                icon.title = TITLE if guard.trusted else TITLE_UNTRUSTED
                icon.update_menu()
            except Exception as e:  # значок ещё не показан
                print("Не удалось обновить значок:", e)

    def watch_network() -> None:
        # Раз в минуту: сменилась сеть — доверие могло измениться.
        while not stop_tls.wait(60):
            try:
                if guard.refresh():
                    network_changed()
            except Exception as e:
                print("Не удалось определить сеть:", e)

    threading.Thread(target=watch_network, daemon=True).start()

    print(f"Phonenect запущен: {base_url}")
    print(f"Отпечаток сертификата: {tls.fp}")
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

    def toggle_trust(icon, item):
        def work():
            guard.refresh()  # имя сети могло устареть
            guard.toggle()
            guard.refresh()
            network_changed()

        threading.Thread(target=work, daemon=True).start()  # определение сети идёт секунды

    def trust_label(item) -> str:
        return f"Доверять сети «{guard.name}»" if guard.name else "Доверять этой сети"

    def toggle_pause(icon, item):
        hub.paused = not hub.paused

    def setup_android(icon, item):
        def work():
            notify = lambda text: icon.notify(text, "Phonenect")
            try:
                model = android_setup.setup(current_base(), cfg["token"], config.pc_name(cfg), tls.fp, log=notify)
                notify(f"{model} настроен: буфер теперь общий автоматически.")
            except android_setup.SetupError as e:
                notify(str(e))
            except Exception as e:
                notify(f"Не получилось: {e}")

        threading.Thread(target=work, daemon=True).start()

    def quit_app(icon, item):
        hub.stop()
        mdns.stop()
        stop_tls.set()
        icon.stop()

    def revoke_all(icon, item):
        import win32api
        import win32con

        flags = win32con.MB_TOPMOST | win32con.MB_SETFOREGROUND

        def confirm() -> bool:
            return win32api.MessageBox(
                0,
                "Все телефоны и связанные ПК потеряют доступ к этому ПК, подключать их придётся заново. Продолжить?",
                "Phonenect",
                win32con.MB_YESNO | win32con.MB_ICONWARNING | flags,
            ) == win32con.IDYES

        def spawn() -> None:
            cmd, cwd = restart_command()
            subprocess.Popen(
                cmd, cwd=cwd, close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )

        def show(text: str) -> None:
            win32api.MessageBox(0, text, "Phonenect", win32con.MB_ICONERROR | flags)

        threading.Thread(  # окно блокирует, а меню трея должно жить
            target=revoke_all_devices, daemon=True,
            args=(confirm, lambda: config.rotate_token(cfg), spawn, show, lambda: quit_app(icon, item)),
        ).start()

    icon = pystray.Icon(
        "phonenect",
        tray_icon(),
        TITLE if guard.trusted else TITLE_UNTRUSTED,
        menu=pystray.Menu(
            pystray.MenuItem("Подключить телефон…", lambda: webbrowser.open(local_pair), default=True),
            pystray.MenuItem("Связать с другим ПК…", lambda: webbrowser.open(local_pair + "#pc")),
            pystray.MenuItem("Отправить файлы на телефон…", send_files),
            pystray.MenuItem("Открыть полученные файлы", lambda: os.startfile(files_dir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Настроить Android по USB", setup_android),
            pystray.MenuItem(trust_label, toggle_trust, checked=lambda item: guard.trusted, enabled=lambda item: bool(guard.name)),
            pystray.MenuItem("Пауза синхронизации", toggle_pause, checked=lambda item: hub.paused),
            pystray.MenuItem("Запускать вместе с Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
            pystray.MenuItem("Открыть папку настроек", lambda: os.startfile(config.CONFIG_DIR)),
            pystray.MenuItem("Отключить все устройства…", revoke_all),
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
