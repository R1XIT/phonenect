"""Разовая настройка Android-телефона по USB: установка приложения, разрешения, подключение к ПК.

Фоновое чтение буфера на Android возможно только с разрешением READ_LOGS, а его выдаёт лишь ADB.
Здесь всё делается одной кнопкой из трея (или: python -m phonenect --setup-android).
"""
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import quote

from . import config

PACKAGE = "dev.phonenect"
APK = Path(__file__).parent / "web" / "android" / "phonenect.apk"
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
ADB_CANDIDATES = [
    config.CONFIG_DIR / "platform-tools" / "adb.exe",
    Path(r"C:\android-toolchain\sdk\platform-tools\adb.exe"),
]


class SetupError(Exception):
    pass


def _run(args: list, check: bool = True, timeout: int = 120) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if check and result.returncode != 0:
        raise SetupError((result.stderr or result.stdout).strip() or f"{args[1:3]} failed")
    return result.stdout


def find_adb(log) -> str:
    for path in ADB_CANDIDATES:
        if path.exists():
            return str(path)
    if found := shutil.which("adb"):
        return found
    # Нет adb — скачиваем platform-tools (≈10 МБ). curl, потому что VPN бывает режет TLS у Python.
    log("Скачиваю Android platform-tools…")
    zip_path = config.CONFIG_DIR / "platform-tools.zip"
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        code = subprocess.run(
            ["curl.exe", "-sSL", "-m", "300", "-C", "-", "-o", str(zip_path), PLATFORM_TOOLS_URL],
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).returncode
        if code in (0, 33):
            break
    else:
        raise SetupError("Не удалось скачать platform-tools")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(config.CONFIG_DIR)
    zip_path.unlink(missing_ok=True)
    return str(ADB_CANDIDATES[0])


def pick_device(adb: str) -> str:
    lines = _run([adb, "devices"]).splitlines()[1:]
    devices = [line.split() for line in lines if line.strip()]
    ready = [d[0] for d in devices if d[1] == "device"]
    if not devices:
        raise SetupError("Телефон не найден. Подключите его кабелем и включите «Отладку по USB» "
                         "(Настройки → Для разработчиков).")
    if not ready:
        if any(d[1] == "unauthorized" for d in devices):
            raise SetupError("Подтвердите на телефоне запрос «Разрешить отладку по USB» и повторите.")
        raise SetupError(f"Телефон не готов: {devices[0][1]}")
    return ready[0]


def setup(pair_base: str, token: str, name: str, fp: str, log=print) -> str:
    """Возвращает модель телефона. Исключение SetupError — понятное пользователю сообщение."""
    adb = find_adb(log)
    serial = pick_device(adb)
    sh = lambda *a, check=True: _run([adb, "-s", serial, "shell", *a], check=check)
    model = sh("getprop", "ro.product.model").strip() or serial

    installed = PACKAGE in sh("pm", "list", "packages", PACKAGE)
    if APK.exists():
        log(f"Устанавливаю Phonenect на {model}…")
        _run([adb, "-s", serial, "install", "-r", str(APK)], timeout=300)
    elif not installed:
        raise SetupError("Нет файла приложения: соберите его (см. README, раздел Android).")

    log("Выдаю разрешения…")
    # Главное: видеть в журнале момент копирования и открывать окно чтения из фона.
    sh("pm", "grant", PACKAGE, "android.permission.READ_LOGS")
    sh("appops", "set", PACKAGE, "SYSTEM_ALERT_WINDOW", "allow")
    # Удобства: уведомление службы и работа без экономии батареи (на старых Android их может не быть).
    sh("pm", "grant", PACKAGE, "android.permission.POST_NOTIFICATIONS", check=False)
    sh("dumpsys", "deviceidle", "whitelist", f"+{PACKAGE}", check=False)
    # READ_LOGS действует после перезапуска процесса.
    sh("am", "force-stop", PACKAGE)

    log("Подключаю приложение к ПК…")
    link = f"phonenect://pair?url={quote(pair_base, safe='')}&t={quote(token, safe='')}&name={quote(name, safe='')}&fp={quote(fp, safe='')}"
    sh("am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{link}'", PACKAGE)
    return model
