"""Общий буфер: история клипов, синхронизация с буфером Windows, рассылка клиентам."""
import asyncio
import hashlib
import io
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser

from PIL import Image
from striprtf.striprtf import rtf_to_text

from . import clipboard_win as cb

try:  # фото с iPhone часто приходят в HEIC
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

HISTORY_SIZE = 30
POLL_INTERVAL = 0.4
WEB_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@dataclass
class Clip:
    id: int
    kind: str  # "text" | "image"
    data: bytes  # utf-8 для текста, байты файла для картинки
    mime: str
    source: str
    ts: float = field(default_factory=time.time)

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.data).hexdigest()

    def meta(self) -> dict:
        m = {
            "id": self.id,
            "kind": self.kind,
            "mime": self.mime,
            "source": self.source,
            "ts": self.ts,
            "size": len(self.data),
        }
        if self.kind == "text":
            m["text"] = self.data.decode("utf-8", "replace")
        return m


class Hub:
    def __init__(self) -> None:
        self.history: deque[Clip] = deque(maxlen=HISTORY_SIZE)
        self.sockets: set = set()
        self.paused = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._last_seq = cb.sequence_number()
        self._stop = threading.Event()

    # ---------- история ----------

    @property
    def latest(self) -> Clip | None:
        return self.history[-1] if self.history else None

    def get(self, clip_id: int) -> Clip | None:
        return next((c for c in self.history if c.id == clip_id), None)

    def _add(self, kind: str, data: bytes, mime: str, source: str) -> Clip | None:
        latest = self.latest
        if latest and latest.kind == kind and latest.data == data:
            return None
        clip = Clip(next(self._ids), kind, data, mime, source)
        self.history.append(clip)
        return clip

    # ---------- ПК -> телефоны ----------

    def watch_clipboard(self) -> None:
        """Поток: следит за буфером Windows через номер последовательности."""
        while not self._stop.wait(POLL_INTERVAL):
            with self._lock:
                seq = cb.sequence_number()
                if seq == self._last_seq:
                    continue
                self._last_seq = seq
            if self.paused:
                continue
            try:
                content = cb.read()
            except Exception as e:
                print("clipboard read failed:", e)
                continue
            if not content:
                continue
            kind, value = content
            if kind == "text":
                clip_args = ("text", value.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                clip_args = ("image", value, "image/png")
            self.loop.call_soon_threadsafe(self._publish_local, *clip_args)

    def _publish_local(self, kind: str, data: bytes, mime: str) -> None:
        clip = self._add(kind, data, mime, "ПК")
        if clip:
            asyncio.ensure_future(self.broadcast(clip))

    def stop(self) -> None:
        self._stop.set()

    # ---------- телефон -> ПК ----------

    async def add_remote(self, body: bytes, content_type: str, source: str) -> Clip | None:
        kind, mime = classify(body, content_type)
        if kind == "text":
            body = plain_text(body, content_type)
        elif mime not in WEB_IMAGE_MIMES:
            # HEIC/TIFF/BMP браузеры не покажут — перекодируем в PNG.
            with Image.open(io.BytesIO(body)) as img:
                buf = io.BytesIO()
                img.save(buf, "PNG")
            body, mime = buf.getvalue(), "image/png"
        clip =self._add(kind, body, mime, source)
        if not clip:
            return self.latest
        if not self.paused:
            await asyncio.get_running_loop().run_in_executor(None, self._write_pc, clip)
        await self.broadcast(clip)
        return clip

    def _write_pc(self, clip: Clip) -> None:
        with self._lock:
            if clip.kind == "text":
                self._last_seq = cb.write_text(clip.data.decode("utf-8"))
            else:
                self._last_seq = cb.write_image(clip.data)

    # ---------- рассылка ----------

    async def broadcast(self, clip: Clip) -> None:
        msg = {"event": "clip", "clip": clip.meta()}
        for ws in list(self.sockets):
            try:
                await ws.send_json(msg)
            except Exception:
                self.sockets.discard(ws)


class _HtmlText(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(body: bytes, content_type: str) -> bytes:
    """iOS может прислать скопированное из Safari как RTF или HTML — достаём чистый текст."""
    ct = (content_type or "").lower()
    head = body[:200].lstrip().lower()
    text = body.decode("utf-8", "replace")
    if "rtf" in ct or head.startswith(b"{\\rtf"):
        text = rtf_to_text(text)
    elif "html" in ct or head.startswith((b"<!doctype html", b"<html", b"<meta")):
        parser = _HtmlText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text.strip("\r\n").encode("utf-8")


def classify(body: bytes, content_type: str) -> tuple[str, str]:
    ct = (content_type or "").lower()
    if ct.startswith("text/"):
        return "text", "text/plain; charset=utf-8"
    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt = (img.format or "png").lower()
        return "image", Image.MIME.get(fmt.upper(), f"image/{fmt}")
    except Exception:
        return "text", "text/plain; charset=utf-8"
