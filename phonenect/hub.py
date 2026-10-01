"""Общий буфер: история клипов, синхронизация с буфером Windows, рассылка клиентам."""
import asyncio
import hashlib
import io
import itertools
import mimetypes
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

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
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
MAX_CLIP = 50 * 1024 * 1024  # больше этого в буфер не кладём — только файлом
MAX_FILES = 20  # сколько файлов за раз уходит на телефоны при копировании в проводнике
# Тип, по которому не понять, текст это, картинка или файл, — тогда смотрим на содержимое.
VAGUE_TYPES = {"", "application/octet-stream", "application/x-www-form-urlencoded"}


@dataclass
class Clip:
    id: int
    kind: str  # "text" | "image" | "file"
    data: bytes  # utf-8 для текста, байты картинки; у файла пусто — он лежит на диске
    mime: str
    source: str
    ts: float = field(default_factory=time.time)
    path: str | None = None  # для файла
    file_size: int = 0
    sha: str | None = None  # для файла, если считали при приёме
    explicit: bool = False  # файл отправили нарочно (не просто скопировали) — качать при любом размере
    origin: str = ""  # id ПК, где клип появился (его буфер или его телефоны) — связанные ПК обмениваются только своими

    @property
    def name(self) -> str | None:
        return os.path.basename(self.path) if self.path else None

    @property
    def digest(self) -> str:
        return self.sha or hashlib.sha1(self.data).hexdigest()

    def meta(self) -> dict:
        m = {
            "id": self.id,
            "kind": self.kind,
            "mime": self.mime,
            "source": self.source,
            "ts": self.ts,
            "size": self.file_size if self.kind == "file" else len(self.data),
        }
        if self.kind == "text":
            m["text"] = self.data.decode("utf-8", "replace")
        elif self.kind == "file":
            m["name"] = self.name
            m["explicit"] = self.explicit
        m["origin"] = self.origin
        return m


class Hub:
    def __init__(self, files_dir: Path, name: str, pc_id: str) -> None:
        self.name = name  # имя этого ПК — для подписи клипов
        self.id = pc_id  # постоянный id этого ПК — для origin
        self.history: deque[Clip] = deque(maxlen=HISTORY_SIZE)
        self.sockets: set = set()
        self.paused = False
        self.files_dir = files_dir
        self.on_file: Callable[[Clip], None] | None = None  # пришёл файл с телефона — для уведомления
        self.listeners: list[Callable[[Clip], None]] = []  # связанные ПК: каждый новый клип
        self.delivered: dict[str, int] = {}  # устройство -> id последнего полученного клипа
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

    def _add(self, kind: str, data: bytes, mime: str, source: str, origin: str | None = None, **file) -> Clip | None:
        latest = self.latest
        if latest and latest.kind == kind and latest.data == data and latest.path == file.get("path"):
            return None
        clip = Clip(next(self._ids), kind, data, mime, source, origin=origin or self.id, **file)
        self.history.append(clip)
        return clip

    # ---------- ПК -> телефоны ----------

    def watch_clipboard(self) -> None:
        """Поток: следит за буфером Windows через номер последовательности."""
        mimetypes.init()  # в Windows читает реестр секунду-другую — делаем это здесь, а не в цикле событий
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
            if kind == "files":
                # Одна скопированная картинка, как раньше, уходит в буфер телефона, остальное — файлами.
                image = single_image(value)
                if image is None:
                    self.send_files(value)
                    continue
                kind, value = "image", image
            if kind == "text":
                clip_args = ("text", value.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                clip_args = ("image", value, "image/png")
            self.loop.call_soon_threadsafe(self._publish_local, *clip_args)

    def _publish_local(self, kind: str, data: bytes, mime: str) -> None:
        clip = self._add(kind, data, mime, "ПК")
        if clip:
            asyncio.ensure_future(self.broadcast(clip))

    def send_files(self, paths: list[str], explicit: bool = False) -> None:
        """Отправить файлы ПК на телефоны. Можно звать из любого потока."""
        self.loop.call_soon_threadsafe(self._publish_files, paths, explicit)

    def _publish_files(self, paths: list[str], explicit: bool = False) -> None:
        for path in paths[:MAX_FILES]:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            clip = self._add("file", b"", mime, "ПК", path=path, file_size=size, explicit=explicit)
            if clip:
                asyncio.ensure_future(self.broadcast(clip))

    def stop(self) -> None:
        self._stop.set()

    # ---------- телефон -> ПК ----------

    def take_new(self, device: str, max_age: float) -> Clip | None:
        """Последний клип, если это устройство его ещё не получало и он не старше max_age секунд."""
        clip = self.latest
        if (
            clip is None
            # Файлы автоматизациям не отдаём: незачем качать гигабайт при каждом открытии приложения.
            or clip.kind == "file"
            or (clip.source == device and clip.origin == self.id)  # своё, а не тёзки с другого ПК
            or self.delivered.get(device, 0) >= clip.id
            or time.time() - clip.ts > max_age
        ):
            return None
        self.delivered[device] = clip.id
        return clip

    async def receive(
        self,
        chunks: AsyncIterator[bytes],
        content_type: str,
        source: str,
        name: str | None = None,
        as_file: bool = False,
        auto: bool = False,
        origin: str | None = None,
    ) -> Clip | None:
        """Принимает присланное потоком: текст и картинки — в буфер, остальное — файлом в папку.
        None — автоотправка пропущена как уже известная."""
        ct = (content_type or "").split(";")[0].strip().lower()
        tmp = self.files_dir / f".{uuid.uuid4().hex}.part"
        sha = hashlib.sha1()
        size = 0
        try:
            # Тело может быть большим — пишем на диск по кускам, а не держим в памяти.
            with open(tmp, "wb") as f:
                async for chunk in chunks:
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
            if not size:
                raise ValueError("Пустой запрос")
            if not as_file and size <= MAX_CLIP and (ct.startswith(("text/", "image/")) or ct in VAGUE_TYPES):
                body = tmp.read_bytes()
                if ct.startswith(("text/", "image/")) or looks_like_clip(body):
                    return await self.add_remote(body, content_type, source, auto, origin)
            digest = sha.hexdigest()
            if auto and any(c.digest == digest for c in self.history):
                return None
            path = unique_path(self.files_dir / (safe_name(name) or default_name(source, ct)))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        mime = ct if ct not in VAGUE_TYPES else mimetypes.guess_type(path)[0] or "application/octet-stream"
        clip = self._add("file", b"", mime, source, path=str(path), file_size=size, sha=digest, explicit=as_file, origin=origin)
        self._mark_delivered(clip)
        if not self.paused:
            await asyncio.get_running_loop().run_in_executor(None, self._write_pc, clip)
        if self.on_file:
            self.on_file(clip)
        await self.broadcast(clip)
        return clip

    def _mark_delivered(self, clip: Clip) -> None:
        """Своё устройство не должно забирать этот клип обратно через «только новое».
        Телефон с другого ПК здесь чужой, даже если зовётся так же («iPhone»)."""
        if clip.origin == self.id:
            self.delivered[clip.source] = max(self.delivered.get(clip.source, 0), clip.id)

    async def add_remote(
        self, body: bytes, content_type: str, source: str, auto: bool = False, origin: str | None = None
    ) -> Clip | None:
        """Кладёт присланное в буфер ПК. None — автоотправка пропущена как уже известная."""
        kind, mime = classify(body, content_type)
        if kind == "text":
            # С другого ПК приходит уже чистый текст — HTML-исходник там мог быть скопирован нарочно.
            if not origin:
                body = plain_text(body, content_type)
        elif mime not in WEB_IMAGE_MIMES:
            # HEIC/TIFF/BMP браузеры не покажут — перекодируем в PNG.
            with Image.open(io.BytesIO(body)) as img:
                buf = io.BytesIO()
                img.save(buf, "PNG")
            body, mime = buf.getvalue(), "image/png"
        if auto and any(c.kind == kind and c.data == body for c in self.history):
            # Автоматизация шлёт буфер iPhone при каждом закрытии приложения. Если там то,
            # что уже было (в том числе забранное с ПК), свежий буфер ПК не трогаем.
            return None
        clip = self._add(kind, body, mime, source, origin)
        if not clip:
            return self.latest
        self._mark_delivered(clip)
        if not self.paused:
            await asyncio.get_running_loop().run_in_executor(None, self._write_pc, clip)
        await self.broadcast(clip)
        return clip

    def _write_pc(self, clip: Clip) -> None:
        with self._lock:
            if clip.kind == "text":
                self._last_seq = cb.write_text(clip.data.decode("utf-8"))
            elif clip.kind == "file":
                self._last_seq = cb.write_files([clip.path])
            else:
                self._last_seq = cb.write_image(clip.data)

    # ---------- рассылка ----------

    async def broadcast(self, clip: Clip) -> None:
        for listener in self.listeners:
            listener(clip)
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


def looks_like_clip(body: bytes) -> bool:
    """Без внятного Content-Type: картинка или текст идут в буфер, остальное — файлом."""
    try:
        with Image.open(io.BytesIO(body)):
            return True
    except Exception:
        pass
    try:
        return "\x00" not in body.decode("utf-8")
    except UnicodeDecodeError:
        return False


def single_image(paths: list[str]) -> bytes | None:
    """PNG, если в проводнике скопирована ровно одна картинка."""
    if len(paths) != 1 or Path(paths[0]).suffix.lower() not in IMAGE_EXTS:
        return None
    try:
        if os.path.getsize(paths[0]) > MAX_CLIP:
            return None
        with Image.open(paths[0]) as img:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        return None


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str | None) -> str | None:
    """Имя файла от телефона без путей и запрещённых в Windows символов."""
    if not name:
        return None
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")
    stem, ext = os.path.splitext(name)
    if not stem:
        return None
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    return stem[:120] + ext[:20]


def default_name(source: str, content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type) if content_type not in VAGUE_TYPES else None
    return safe_name(f"{source} {time.strftime('%Y-%m-%d %H-%M-%S')}{ext or ''}") or "Файл"


def unique_path(path: Path) -> Path:
    """report.pdf → report (2).pdf, если такой уже есть."""
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate
