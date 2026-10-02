"""Связь с другими ПК: общий буфер и файлы между компьютерами.

Связь ставится с одной стороны и работает в обе: этот ПК слушает WebSocket другого
(оттуда приходит его буфер) и отправляет ему свои клипы обычным POST /api/clip.
ПК обмениваются только клипами, появившимися у них самих (origin), и ничего не
пересылают дальше: так нет ни эха, ни повторов. Три ПК и больше связываются попарно.
ПК узнаются по id (хеш токена), имя — только для подписи: его можно сменить.
"""
import asyncio
import ssl
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
from cryptography import x509

from . import certs, config, mdns
from .hub import Clip, Hub

AUTO_LIMIT = 200 * 1024 * 1024  # просто скопированные файлы больше этого между ПК не гоняем
CHUNK = 256 * 1024
# Большой файл качается долго: ограничиваем не всё время, а паузу без данных.
RETRY_DELAY = 2  # секунд; растёт с числом неудач, но не больше 30
TRANSFER_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=60)


class LinkError(Exception):
    pass


FINGERPRINT_MISMATCH = "Сертификат не совпадает — подключайтесь из дома и проверьте отпечаток на странице ПК"
OLD_LINK = "Старая ссылка — обновите Phonenect на том ПК и скопируйте адрес заново (трей → «Связать с другим ПК…»)"


def parse_link(link: str) -> tuple[str, str, str]:
    """Адрес другого ПК (https://host:port/api/clip?t=TOKEN&fp=…) → (база, токен, отпечаток CA)."""
    parts = urlsplit(link.strip())
    query = parse_qs(parts.query)
    token, fp = query.get("t", [""])[0], query.get("fp", [""])[0].lower()
    if not parts.netloc or not token:
        raise LinkError("Это не адрес Phonenect: нужен адрес вида https://…/api/clip?t=…&fp=…")
    if parts.scheme != "https" or len(fp) != 64:
        raise LinkError(OLD_LINK)
    return f"https://{parts.netloc}", token, fp


async def fetch_ca(http: aiohttp.ClientSession, url: str, fp: str) -> str:
    """Скачивает CA того ПК и сверяет с отпечатком из ссылки. Единственный запрос без проверки сертификата."""
    try:
        async with http.get(f"{url}/ca.crt", ssl=False, timeout=aiohttp.ClientTimeout(total=5)) as r:
            pem = await r.read()
        cert = x509.load_pem_x509_certificate(pem)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise LinkError("Не удалось связаться: ПК включён, в той же сети, брандмауэр пропускает Phonenect?")
    if certs.fingerprint(cert) != fp:
        raise LinkError(FINGERPRINT_MISMATCH)
    return pem.decode()


def pinned_ssl(ca_pem: str) -> ssl.SSLContext:
    """Доверяем только CA того ПК. Имя не проверяем: CA уникален для ПК, а IP меняется."""
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.check_hostname = False
    return ctx


async def fetch_info(http: aiohttp.ClientSession, url: str, token: str, ssl_ctx: ssl.SSLContext) -> dict:
    try:
        async with http.get(f"{url}/api/info", headers={"Authorization": f"Bearer {token}"}, ssl=ssl_ctx,
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 401:
                raise LinkError("Неверный токен: скопируйте адрес заново")
            if r.status != 200:
                raise LinkError(f"Ответ {r.status}: на том ПК старая версия Phonenect?")
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise LinkError("Не удалось связаться: ПК включён, в той же сети, брандмауэр пропускает Phonenect?")


class Peer:
    def __init__(
        self, hub: Hub, http: aiohttp.ClientSession, me: str, url: str, token: str, name: str, ca: str,
        on_refused=lambda peer: None,
        locate=None,
        on_moved=lambda peer: None,
    ) -> None:
        self.locate = locate  # поиск ПК в сети по id, когда по старому адресу он молчит
        self.on_moved = on_moved
        self.on_refused = on_refused  # тот ПК отвязал нас со своей стороны
        self.hub, self.http, self.me = hub, http, me  # me — имя этого ПК для подписи
        self.url, self.token, self.name = url, token, name
        self.ca = ca  # CA того ПК, сверенный по отпечатку при связывании; другим не доверяем
        self.ssl = pinned_ssl(ca)
        self.id = config.pc_id(token)
        self.connected = False
        self.tasks: set[asyncio.Task] = set()
        self.outbox: asyncio.Queue[Clip] = asyncio.Queue()  # текст и картинки уходят строго по порядку
        self.downloads = asyncio.Semaphore(1)  # файлы оттуда — по одному, не задерживая текст

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def start(self) -> None:
        self.hub.listeners.append(self.on_clip)
        self._spawn(self.run())
        self._spawn(self.send_queued())

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def stop(self) -> None:
        if self.on_clip in self.hub.listeners:
            self.hub.listeners.remove(self.on_clip)
        for task in list(self.tasks):
            task.cancel()

    # ---------- оттуда сюда ----------

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                # Представляемся: тот ПК покажет связь у себя и сможет её разорвать.
                hello = {**self.auth, "X-Origin": self.hub.id, "X-Origin-Name": quote(self.me)}
                async with self.http.ws_connect(f"{self.url}/ws", headers=hello, heartbeat=25, ssl=self.ssl) as ws:
                    failures = 0
                    try:  # тот ПК могли переименовать
                        self.name = (await fetch_info(self.http, self.url, self.token, self.ssl)).get("name") or self.name
                    except LinkError:
                        pass
                    self.connected = True
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = msg.json()
                        if data.get("event") == "clip":
                            try:
                                await self.incoming(data["clip"])
                            except Exception as e:
                                print(f"{self.name}: не удалось принять клип: {e}")
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 403:
                    self.connected = False
                    self.on_refused(self)
                    return
            except Exception:
                pass
            self.connected = False
            failures += 1
            if failures % 3 == 0 and self.locate:
                await self.relocate()
            await asyncio.sleep(min(30, RETRY_DELAY * failures))

    async def relocate(self) -> None:
        """Тот ПК мог сменить IP (DHCP): ищем его по id и запоминаем новый адрес."""
        try:
            found = await self.locate(self.id)
        except Exception:
            return
        url = found and f"https://{found[0]}:{found[1]}"
        if url and url != self.url:
            self.url = url
            self.on_moved(self)

    async def incoming(self, meta: dict) -> None:
        if meta.get("origin") != self.id:
            return  # не его собственный клип: наш эхом или от третьего ПК, с которым связь своя
        # «ПК» там — это тот компьютер; здесь его надо звать по имени.
        source = self.name if meta["source"] == "ПК" else meta["source"]
        kind = meta["kind"]
        if kind == "text":
            await self.hub.add_remote(meta["text"].encode(), "text/plain; charset=utf-8", source, origin=self.id)
            return
        if kind == "image":
            async with self.http.get(f"{self.url}/api/clip/{meta['id']}", headers=self.auth, ssl=self.ssl) as r:
                if r.status == 200:
                    await self.hub.add_remote(await r.read(), meta["mime"], source, origin=self.id)
        elif kind == "file" and (meta["size"] <= AUTO_LIMIT or meta.get("explicit")):
            self._spawn(self.download(meta, source))

    async def download(self, meta: dict, source: str) -> None:
        async with self.downloads:
            try:
                async with self.http.get(
                    f"{self.url}/api/clip/{meta['id']}", headers=self.auth, ssl=self.ssl, timeout=TRANSFER_TIMEOUT
                ) as r:
                    if r.status != 200:
                        return
                    await self.hub.receive(
                        r.content.iter_chunked(CHUNK), meta["mime"], source, name=meta["name"], as_file=True,
                        origin=self.id,
                    )
            except Exception as e:
                print(f"{self.name}: не удалось скачать {meta.get('name')}: {e}")

    # ---------- отсюда туда ----------

    def on_clip(self, clip: Clip) -> None:
        if clip.origin != self.hub.id or not self.connected:
            return
        if clip.kind != "file":
            self.outbox.put_nowait(clip)
        elif clip.file_size <= AUTO_LIMIT or clip.explicit:
            self._spawn(self.send(clip))

    async def send_queued(self) -> None:
        while True:
            await self.send(await self.outbox.get())

    async def send(self, clip: Clip) -> None:
        # Имя телефона сохраняем, буфер этого ПК там будет подписан его именем.
        device = self.me if clip.source == "ПК" else clip.source
        headers = {**self.auth, "X-Device": quote(device), "X-Origin": self.hub.id, "Content-Type": clip.mime}
        if clip.kind == "file":
            headers["X-Filename"] = quote(clip.name)
            body = read_file(clip.path)
        else:
            body = clip.data
        try:
            async with self.http.post(
                f"{self.url}/api/clip", data=body, headers=headers, ssl=self.ssl, timeout=TRANSFER_TIMEOUT
            ) as r:
                if r.status >= 300:
                    print(f"{self.name}: ответ {r.status} на отправку")
        except Exception as e:
            print(f"{self.name}: не удалось отправить: {e}")


async def read_file(path: str):
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        while chunk := await loop.run_in_executor(None, f.read, CHUNK):
            yield chunk


async def find_on_lan(pc_id: str) -> tuple[str, int] | None:
    return await asyncio.get_running_loop().run_in_executor(None, mdns.find, pc_id)


class Peers:
    """Связанные ПК: список в config.json ("peers"), подключения живут в цикле событий хаба."""

    def __init__(self, hub: Hub, cfg: dict) -> None:
        self.hub, self.cfg = hub, cfg
        self.me = config.pc_name(cfg)
        self.my_id = config.pc_id(cfg["token"])
        self.items: list[Peer] = []
        self.incoming: dict[str, tuple[str, object]] = {}  # id → (имя, WebSocket): кто связался с нами
        self.find = find_on_lan
        self.http: aiohttp.ClientSession | None = None
        self.stale: list[dict] = []  # старые связи без шифрования: не запускаем, показываем «свяжите заново»

    def start(self) -> None:
        """Зовётся в цикле событий хаба."""
        self.http = aiohttp.ClientSession()
        for p in self.cfg.get("peers", []):
            if not p.get("ca") or p["url"].startswith("http://"):
                self.stale.append(p)
            else:
                self._spawn(p["url"], p["token"], p.get("name") or p["url"], p["ca"])

    def _spawn(self, url: str, token: str, name: str, ca: str) -> Peer:
        peer = Peer(
            self.hub, self.http, self.me, url, token, name, ca,
            on_refused=self._refused,
            locate=lambda pc_id: self.find(pc_id),  # через self — чтобы поиск можно было подменить
            on_moved=lambda _: self._save(),
        )
        self.items.append(peer)
        peer.start()
        return peer

    def _save(self) -> None:
        self.cfg["peers"] = [{"url": p.url, "token": p.token, "name": p.name, "ca": p.ca} for p in self.items]
        self.cfg["peers"] += self.stale
        config.save(self.cfg)

    def status(self) -> list[dict]:
        ours = [{"id": p.id, "name": p.name, "url": p.url, "connected": p.connected, "incoming": False,
                 "stale": False} for p in self.items]
        ours += [{"id": config.pc_id(p["token"]), "name": p.get("name") or p["url"], "url": p["url"],
                  "connected": False, "incoming": False, "stale": True} for p in self.stale]
        theirs = [{"id": i, "name": name, "url": "", "connected": True, "incoming": True}
                  for i, (name, _) in self.incoming.items() if i not in {p.id for p in self.items}]
        return ours + theirs

    # ---------- связи, поставленные с того ПК ----------

    @property
    def blocked(self) -> list[str]:
        """ПК, которых отвязали с этой стороны: их связь сюда не пускаем."""
        return self.cfg.setdefault("blocked_peers", [])

    def is_blocked(self, pc_id: str | None) -> bool:
        return bool(pc_id) and pc_id in self.blocked

    def connected_in(self, pc_id: str, name: str, ws) -> None:
        self.incoming[pc_id] = (name, ws)

    def disconnected_in(self, pc_id: str, ws) -> None:
        if self.incoming.get(pc_id, (None, None))[1] is ws:
            del self.incoming[pc_id]

    async def unlink(self, pc_id: str) -> None:
        """Отвязать ПК: свою связь удаляем, чужую запрещаем и рвём."""
        for p in [p for p in self.items if p.id == pc_id]:
            self.remove(p)
        if any(config.pc_id(s["token"]) == pc_id for s in self.stale):
            self.stale = [s for s in self.stale if config.pc_id(s["token"]) != pc_id]
            self._save()
        if pc_id in self.incoming:
            if pc_id not in self.blocked:
                self.blocked.append(pc_id)
                config.save(self.cfg)
            _, ws = self.incoming.pop(pc_id)
            await ws.close()

    def _refused(self, peer: Peer) -> None:
        if peer in self.items:
            self.remove(peer)

    async def add(self, link: str) -> Peer:
        url, token, fp = parse_link(link)
        ca = await fetch_ca(self.http, url, fp)
        info = await fetch_info(self.http, url, token, pinned_ssl(ca))
        if info.get("id") == self.my_id:
            raise LinkError("Это адрес этого же ПК — скопируйте адрес на другом компьютере")
        if self.my_id in info.get("peers", []):
            raise LinkError(f"{info.get('name')} уже связан с этим ПК — связь работает в обе стороны")
        if self.my_id in info.get("blocked", []):
            raise LinkError(f"На ПК {info.get('name')} этот ПК отвязан — свяжите их с того ПК")
        if info["id"] in self.blocked:  # связываем сами — значит, снова доверяем
            self.blocked.remove(info["id"])
        for p in list(self.items):
            if p.token == token:  # тот же ПК с новым адресом — заменяем
                self.remove(p)
        self.stale = [s for s in self.stale if s["token"] != token]  # старая связь с тем же ПК заменяется
        peer = self._spawn(url, token, info.get("name") or url, ca)
        self._save()
        return peer

    def remove(self, peer: Peer) -> None:
        peer.stop()
        self.items.remove(peer)
        self._save()
