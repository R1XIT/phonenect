"""HTTP API + WebSocket + веб-клиент для телефонов."""
import hmac
import io
from collections.abc import Callable
from html import escape
import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

from . import certs, config
from .hub import Hub
from .peers import LinkError, Peers

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "pn_token"
LOCAL_PREFIX = "/pair"  # страница подключения и управление связями с ПК — только с самого ПК
PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/ca.crt"}
MAX_BODY = 50 * 1024 * 1024
SHORTCUT_NAMES = {
    "to-pc": "На ПК",
    "from-pc": "С ПК",
    "auto-to-pc": "Авто — На ПК",
    "auto-from-pc": "Авто — С ПК",
}
DEFAULT_MAX_AGE = 120  # секунд: более старый буфер ПК автоматизация не подхватит


LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def is_local_host(host: str | None) -> bool:
    """Host вида 127.0.0.1:8765 или localhost — не чужое имя, подменённое на наш адрес."""
    if not host:
        return False
    name = host if host.endswith("]") else host.rsplit(":", 1)[0]
    return name.lower() in LOCAL_NAMES


def is_local_origin(origin: str | None) -> bool:
    """Без Origin — не из браузера (или обычный переход); иначе только страница этого же ПК."""
    if not origin:
        return True
    return is_local_host(urlsplit(origin).netloc)


def attachment(name: str) -> str:
    """Content-Disposition с русским именем: ASCII-запасное плюс filename* в UTF-8."""
    fallback = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


async def shortcut_handler(request):
    # Подписанные команды без секретов внутри — отдаём без токена.
    slug = request.match_info["slug"]
    path = WEB_DIR / "shortcuts" / f"{slug}.shortcut"
    if slug not in SHORTCUT_NAMES or not path.exists():
        raise web.HTTPNotFound()
    filename = quote(f"{SHORTCUT_NAMES[slug]}.shortcut")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename=\"{slug}.shortcut\"; filename*=UTF-8''{filename}",
        },
    )

async def apk_handler(request):
    # Приложение без секретов внутри — адрес и токен оно получает по ссылке phonenect://pair.
    path = WEB_DIR / "android" / "phonenect.apk"
    if not path.exists():
        raise web.HTTPNotFound(text="Приложение ещё не собрано")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": 'attachment; filename="phonenect.apk"',
        },
    )


def fingerprint_text(fp: str) -> str:
    """Отпечаток для глаз: группы по 4 символа."""
    return " ".join(fp[i:i + 4] for i in range(0, len(fp), 4)).upper()


def is_local_path(path: str) -> bool:
    return path == LOCAL_PREFIX or path.startswith(LOCAL_PREFIX + "/")


def check_local(request: web.Request) -> None:
    """Страницы /pair — только с самого ПК и только со своей страницы."""
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="Страница доступна только на самом ПК")
    # Браузер на этом ПК могут натравить и чужие сайты: DNS rebinding (чужое имя на 127.0.0.1)
    # и запросы с других страниц. Пускаем только свои адрес и страницу.
    if not is_local_host(request.host) or not is_local_origin(request.headers.get("Origin")):
        raise web.HTTPForbidden(text="Запрос не со страницы Phonenect")
    if request.method == "POST" and request.content_type != "application/json":
        # С application/json браузер сначала спрашивает разрешения (preflight), а его мы не даём.
        raise web.HTTPUnsupportedMediaType(text="Нужен application/json")


@web.middleware
async def local_pages(request: web.Request, handler):
    if is_local_path(request.path):
        check_local(request)
    return await handler(request)


def qr_svg(text: str) -> web.Response:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    buf = io.BytesIO()
    img.save(buf)
    return web.Response(body=buf.getvalue(), content_type="image/svg+xml")


def pair_routes(cfg: dict, base: Callable[[], str], peers: Peers, fp: str) -> list[web.RouteDef]:
    """Страница подключения и связи с другими ПК: открывается только на самом ПК (см. check_local)."""
    token = cfg["token"]
    name = config.pc_name(cfg)

    def pair_url() -> str:
        return f"{base()}/?t={token}&name={quote(name)}&fp={fp}"

    def setup_url() -> str:
        host = urlsplit(base()).hostname
        return f"http://{host}:{cfg.get('setup_port', config.DEFAULT_SETUP_PORT)}/"

    async def pair(request):
        html = (WEB_DIR / "pair.html").read_text("utf-8")
        html = html.replace("{{PAIR_URL}}", pair_url())
        html = html.replace("{{API_URL}}", f"{base()}/api/clip?t={token}&fp={fp}")
        html = html.replace("{{SETUP_URL}}", setup_url())
        html = html.replace("{{FP}}", fingerprint_text(fp))
        html = html.replace("{{NAME}}", escape(name))
        return web.Response(text=html, content_type="text/html")

    async def peers_list(request):
        return web.json_response(peers.status())

    async def peers_add(request):
        try:
            body = await request.json()
            link = body.get("link") if isinstance(body, dict) else None
        except ValueError:
            link = None
        if not isinstance(link, str):
            raise web.HTTPBadRequest(text="Нужен адрес другого ПК")
        try:
            await peers.add(link)
        except LinkError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response(peers.status())

    async def peers_remove(request):
        # По id, а не по номеру в списке: список на странице мог устареть.
        await peers.unlink(request.match_info["id"])
        return web.json_response(peers.status())

    async def pair_qr(request):
        return qr_svg(pair_url())

    async def setup_qr(request):
        return qr_svg(setup_url())

    return [
        web.get("/pair/peers", peers_list),
        web.post("/pair/peers", peers_add),
        web.delete("/pair/peers/{id}", peers_remove),
        web.get("/pair", pair),
        web.get("/pair/qr.svg", pair_qr),
        web.get("/pair/setup-qr.svg", setup_qr),
    ]


def create_app(
    hub: Hub, cfg: dict, base_url: Callable[[], str] | str, peers: Peers, cert_dir: Path, fp: str
) -> web.Application:
    token = cfg["token"]
    name = config.pc_name(cfg)
    base = base_url if callable(base_url) else (lambda: base_url)  # IP может смениться на ходу

    def authorized(request: web.Request) -> bool:
        given = (
            request.query.get("t")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.cookies.get(COOKIE)
            or ""
        )
        return hmac.compare_digest(given.encode(), token.encode())

    @web.middleware
    async def auth(request: web.Request, handler):
        if is_local_path(request.path):
            check_local(request)
        elif (
            request.path not in PUBLIC
            and not request.path.startswith("/shortcuts/")
            and request.path != "/android/phonenect.apk"
            and not authorized(request)
        ):
            raise web.HTTPUnauthorized(
                text="Нет доступа. Отсканируйте QR-код из меню Phonenect в трее ПК."
            )
        return await handler(request)

    def source_name(request: web.Request) -> str:
        # В заголовке имя в %-кодировке: кириллицу заголовки не пропускают.
        return (request.query.get("from") or unquote(request.headers.get("X-Device", "")) or "Телефон")[:40]

    def set_token_cookie(resp: web.StreamResponse) -> None:
        # Secure: cookie привязана к хосту, а не к порту, и без этого флага телефон отправил бы токен
        # открытым текстом на http-порт того же адреса.
        resp.set_cookie(COOKIE, token, max_age=10 * 365 * 24 * 3600, httponly=True, samesite="Lax", secure=True)

    async def index(request: web.Request):
        if "t" in request.query:
            # Запоминаем токен в cookie и убираем его из адресной строки.
            resp = web.Response(status=302, headers={"Location": "/"})
            set_token_cookie(resp)
            return resp
        # Адрес API — тот, по которому телефон нас реально видит (для «Быстрых команд»).
        html = (WEB_DIR / "index.html").read_text("utf-8")
        html = html.replace("{{API_URL}}", f"https://{request.host}/api/clip?t={token}&fp={fp}")
        html = html.replace("{{MDNS_URL}}", f"https://{config.mdns_host(token)}:{cfg['port']}/api/clip?t={token}&fp={fp}")
        # Имя сети попадает в JS-строку — экранируем как JSON (и «<», чтобы не закрыть </script>).
        html = html.replace('"{{NAME}}"', json.dumps(name).replace("<", "\\u003c"))
        html = html.replace('"{{WIFI}}"', json.dumps(config.wifi_ssid() or "").replace("<", "\\u003c"))
        resp = web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})
        if request.cookies.get(COOKIE):
            set_token_cookie(resp)  # заменяет старую cookie без Secure (от прошлой версии по http)
        return resp

    def raw(clip):
        if clip is None:
            raise web.HTTPNotFound(text="Буфер пуст")
        if clip.kind == "file":
            if not Path(clip.path).is_file():
                raise web.HTTPNotFound(text="Файла больше нет на ПК")
            return web.FileResponse(
                clip.path,
                headers={
                    "Content-Type": clip.mime,
                    "Content-Disposition": attachment(clip.name),
                    "X-Clip-Id": str(clip.id),
                    "Cache-Control": "no-store",
                },
            )
        return web.Response(
            body=clip.data,
            content_type=clip.mime.split(";")[0],
            charset="utf-8" if clip.kind == "text" else None,
            headers={"X-Clip-Id": str(clip.id), "Cache-Control": "no-store"},
        )

    def flag(request: web.Request, header: str, query: str) -> bool:
        return (request.headers.get(header) or request.query.get(query) or "") in ("1", "true", "yes")

    async def get_latest(request):
        if flag(request, "X-Only-New", "new"):
            # Для автоматизаций: пустой ответ, если нового для этого устройства нет.
            max_age = float(request.headers.get("X-Max-Age") or request.query.get("max_age") or DEFAULT_MAX_AGE)
            clip = hub.take_new(source_name(request), max_age)
            if clip is None:
                return web.Response(status=204)
            return raw(clip)
        return raw(hub.latest)

    async def get_clip(request):
        return raw(hub.get(int(request.match_info["id"])))

    async def info(request):
        # peers — с кем этот ПК уже связан: связь в обе стороны ставится один раз.
        return web.json_response(
            {
                "name": name,
                "id": config.pc_id(token),
                "peers": [p.id for p in peers.items],
                "blocked": peers.blocked,
            }
        )

    async def history(request):
        return web.json_response([c.meta() for c in reversed(hub.history)])

    async def post_clip(request: web.Request):
        if peers.is_blocked(request.headers.get("X-Origin")):
            raise web.HTTPForbidden(text="Этот ПК отвязан")
        # Имя файла (в заголовке — в %-кодировке) означает: это файл, а не содержимое буфера.
        name = unquote(request.headers.get("X-Filename", "")) or request.query.get("name") or None
        as_file = bool(name)
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            part = await reader.next()
            if part is None:
                raise web.HTTPBadRequest(text="Пустой запрос")
            name = name or unquote(part.filename or "")
            ctype = part.headers.get("Content-Type", "")

            async def chunks():
                while chunk := await part.read_chunk():
                    yield chunk
        else:
            ctype = request.headers.get("Content-Type", "")

            async def chunks():
                async for chunk in request.content.iter_any():
                    yield chunk
        try:
            clip = await hub.receive(
                chunks(),
                ctype,
                source_name(request),
                name=name,
                as_file=as_file,
                auto=flag(request, "X-Auto", "auto"),
                origin=unquote(request.headers.get("X-Origin", "")) or None,  # клип прислал связанный ПК
            )
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        if clip is None:
            return web.json_response({"skipped": True})
        return web.json_response(clip.meta())

    async def websocket(request: web.Request):
        origin = request.headers.get("X-Origin")  # подключился связанный ПК
        if peers.is_blocked(origin):
            raise web.HTTPForbidden(text="Этот ПК отвязан")
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        hub.sockets.add(ws)
        if origin:
            peers.connected_in(origin, unquote(request.headers.get("X-Origin-Name", "")) or origin, ws)
        try:
            await ws.send_json({"event": "history", "clips": [c.meta() for c in reversed(hub.history)]})
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            hub.sockets.discard(ws)
            if origin:
                peers.disconnected_in(origin, ws)
        return ws

    async def ca_crt(request):
        return web.Response(body=certs.ca_pem(cert_dir), content_type="application/x-x509-ca-cert")

    async def static(request):
        return web.FileResponse(WEB_DIR / request.path.lstrip("/"))

    app = web.Application(middlewares=[auth], client_max_size=MAX_BODY)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/clip", get_latest),
            web.post("/api/clip", post_clip),
            web.get("/api/clip/{id:\\d+}", get_clip),
            web.get("/api/history", history),
            web.get("/api/info", info),
            web.get("/ws", websocket),
            *pair_routes(cfg, base, peers, fp),
            web.get("/ca.crt", ca_crt),
            web.get("/shortcuts/{slug}.shortcut", shortcut_handler),
            web.get("/android/phonenect.apk", apk_handler),
            web.get("/manifest.webmanifest", static),
            web.get("/icon.svg", static),
        ]
    )
    return app


def create_setup_app(
    cfg: dict, cert_dir: Path, fp: str, base_url: Callable[[], str] | str, peers: Peers | None = None
) -> web.Application:
    """HTTP-порт без секретов: сертификат, профиль iOS, приложение, команды. Токен здесь не нужен и не принимается."""
    name = config.pc_name(cfg)
    base = base_url if callable(base_url) else (lambda: base_url)

    async def page(request):
        html = (WEB_DIR / "setup.html").read_text("utf-8")
        html = html.replace("{{NAME}}", escape(name)).replace("{{FP}}", fingerprint_text(fp))
        html = html.replace("{{HTTPS_URL}}", escape(base()))
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def ca_crt(request):
        return web.Response(body=certs.ca_pem(cert_dir), content_type="application/x-x509-ca-cert",
                            headers={"Content-Disposition": 'attachment; filename="phonenect-ca.crt"'})

    async def profile(request):
        return web.Response(body=certs.mobileconfig(cert_dir, name), content_type="application/x-apple-aspen-config",
                            headers={"Content-Disposition": 'attachment; filename="phonenect.mobileconfig"'})

    async def icon(request):
        return web.FileResponse(WEB_DIR / "icon.svg")

    # С peers отсюда открывается и страница подключения — только с самого ПК: браузер ПК не знает нашего CA.
    app = web.Application(middlewares=[local_pages] if peers is not None else [])
    if peers is not None:
        app.add_routes(pair_routes(cfg, base, peers, fp))
    app.add_routes([
        web.get("/", page),
        web.get("/ca.crt", ca_crt),
        web.get("/ca.mobileconfig", profile),
        web.get("/android/phonenect.apk", apk_handler),
        web.get("/shortcuts/{slug}.shortcut", shortcut_handler),
        web.get("/icon.svg", icon),
    ])
    return app
