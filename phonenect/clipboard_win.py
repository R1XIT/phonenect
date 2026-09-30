"""Чтение и запись буфера обмена Windows (текст и картинки)."""
import io
import time

import win32clipboard
import win32con
from PIL import Image, ImageGrab

CF_PNG = win32clipboard.RegisterClipboardFormat("PNG")


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


def read():
    """Возвращает ("text", str), ("image", png_bytes) или None."""
    try:
        img = ImageGrab.grabclipboard()
    except Exception:
        img = None
    if isinstance(img, Image.Image):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return "image", buf.getvalue()
    if isinstance(img, list):
        # Скопированы файлы в проводнике — берём первую картинку, если есть.
        for path in img:
            try:
                with Image.open(path) as f:
                    buf = io.BytesIO()
                    f.save(buf, "PNG")
                    return "image", buf.getvalue()
            except Exception:
                continue

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
