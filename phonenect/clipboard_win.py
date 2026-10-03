"""Чтение и запись буфера обмена Windows (текст, картинки и файлы)."""
import io
import os
import struct
import time

import win32clipboard
import win32con
from PIL import Image, ImageGrab

CF_PNG = win32clipboard.RegisterClipboardFormat("PNG")
CF_DROP_EFFECT = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
DROPEFFECT_COPY = 1


def sequence_number() -> int:
    return win32clipboard.GetClipboardSequenceNumber()


def _open(retries: int = 10) -> None:
    # Буфер может быть занят другим процессом — пробуем несколько раз.
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            time.sleep(0.05)
    win32clipboard.OpenClipboard()


# Форматы, которыми менеджеры паролей помечают скопированное секретное.
SECRET_PRESENT = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")  # сам формат = секрет
SECRET_IF_ZERO = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")  # секрет, если DWORD равен 0


def is_sensitive(formats: dict[str, bytes | None]) -> bool:
    """formats: имя формата -> данные (None, если не читали). Секрет — только по явным признакам."""
    if any(name in formats for name in SECRET_PRESENT):
        return True
    for name in SECRET_IF_ZERO:
        data = formats.get(name)
        if data is not None and len(data) == 4 and struct.unpack("<I", data)[0] == 0:
            return True
    return False


def sensitive() -> bool:
    """Пометил ли менеджер паролей текущее содержимое буфера секретным."""
    _open()
    try:
        formats: dict[str, bytes | None] = {}
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            try:
                name = win32clipboard.GetClipboardFormatName(fmt)
            except Exception:
                name = None  # стандартный формат без имени
            if name:
                data = None
                if name in SECRET_IF_ZERO:
                    try:
                        data = win32clipboard.GetClipboardData(fmt)
                    except Exception:
                        data = None
                formats[name] = data if isinstance(data, bytes) else None
            fmt = win32clipboard.EnumClipboardFormats(fmt)
        return is_sensitive(formats)
    finally:
        win32clipboard.CloseClipboard()


def read():
    """Возвращает ("text", str), ("image", png_bytes), ("files", [пути]) или None."""
    try:
        img = ImageGrab.grabclipboard()
    except Exception:
        img = None
    if isinstance(img, Image.Image):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return "image", buf.getvalue()
    if isinstance(img, list):
        # Скопированы файлы в проводнике. Папки пропускаем.
        files = [p for p in img if os.path.isfile(p)]
        return ("files", files) if files else None

    _open()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            if text:
                return "text", text
    finally:
        win32clipboard.CloseClipboard()
    return None


def write_text(text: str) -> int:
    _open()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
    return sequence_number()


def write_image(png: bytes) -> int:
    img = Image.open(io.BytesIO(png))
    img.load()
    # CF_DIB — BMP без 14-байтового заголовка файла; понимают все приложения.
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")
    dib = bmp.getvalue()[14:]
    # Формат "PNG" сохраняет прозрачность для приложений, которые его знают.
    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")

    _open()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        win32clipboard.SetClipboardData(CF_PNG, png_buf.getvalue())
    finally:
        win32clipboard.CloseClipboard()
    return sequence_number()


def write_files(paths: list[str]) -> int:
    """Кладёт файлы в буфер так же, как «Копировать» в проводнике: Ctrl+V вставит сами файлы."""
    # DROPFILES: смещение списка, точка, fNC, fWide; дальше пути в UTF-16, каждый с нулём на конце, и ещё один ноль.
    names = "".join(p + chr(0) for p in paths) + chr(0)
    hdrop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16-le")
    _open()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, hdrop)
        win32clipboard.SetClipboardData(CF_DROP_EFFECT, struct.pack("<I", DROPEFFECT_COPY))
    finally:
        win32clipboard.CloseClipboard()
    return sequence_number()
