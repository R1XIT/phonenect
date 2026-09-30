"""HTTP API + WebSocket + веб-клиент для телефонов."""
import hmac
import io
from pathlib import Path
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

from .hub import Hub

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "pn_token"
LOCAL_ONLY = {"/pair", "/pair/qr.svg"}
PUBLIC = {"/manifest.webmanifest", "/icon.svg"}
MAX_BODY = 50 * 1024 * 1024
SHORTCUT_NAMES = {"to-pc": "На ПК", "from-pc": "С ПК"}


def create_app(hub: Hub, cfg: dict, base_url: str) -> web.Application:
    token = cfg["token"]

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
        if request.path in LOCAL_ONLY:
            if request.remote not in ("127.0.0.1", "::1"):
                raise web.HTTPForbidden(text="Страница доступна только на самом ПК")
        elif (
            request.path not in PUBLIC
            and not request.path.startswith("/shortcuts/")
            and not authorized(request)
        ):
            raise web.HTTPUnauthorized(
                text="Нет доступа. Отсканируйте QR-код из меню Phonenect в трее ПК."
            )
        return await handler(request)

    def source_name(request: web.Request) -> str:
        return (request.query.get("from") or request.headers.get("X-Device") or "Телефон")[:40]

    async def index(request: web.Request):
        if "t" in request.query:
            # Запоминаем токен в cookie и убираем его из адресной строки.
            resp = web.Response(status=302, headers={"Location": "/"})
            resp.set_cookie(COOKIE, token, max_age=10 * 365 * 24 * 3600, httponly=True, samesite="Lax")
            return resp
        # Адрес API — тот, по которому телефон нас реально видит (для «Быстрых команд»).
        html = (WEB_DIR / "index.html").read_text("utf-8")
        html = html.replace("{{API_URL}}", f"http://{request.host}/api/clip?t={token}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    def raw(clip):
        if clip is None:
            raise web.HTTPNotFound(text="Буфер пуст")
        return web.Response(
            body=clip.data,
            content_type=clip.mime.split(";")[0],
            charset="utf-8" if clip.kind == "text" else None,
            headers={"X-Clip-Id": str(clip.id), "Cache-Control": "no-store"},
        )

    async def get_latest(request):
        return raw(hub.latest)

    async def get_clip(request):
        return raw(hub.get(int(request.match_info["id"])))

    async def history(request):
        return web.json_response([c.meta() for c in reversed(hub.history)])

    async def post_clip(request: web.Request):
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            part = await reader.next()
            if part is None:
                raise web.HTTPBadRequest(text="Пустой запрос")
            body = await part.read(decode=True)
            ctype = part.headers.get("Content-Type", "")
        else:
            body = await request.read()
            ctype = request.content_type
        if not body:
            raise web.HTTPBadRequest(text="Пустой запрос")
        clip = await hub.add_remote(body, ctype, source_name(request))
        return web.json_response(clip.meta())

    async def websocket(request: web.Request):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        hub.sockets.add(ws)
        try:
            await ws.send_json({"event": "history", "clips": [c.meta() for c in reversed(hub.history)]})
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            hub.sockets.discard(ws)
        return ws

    def pair_url() -> str:
        return f"{base_url}/?t={token}"

    async def pair(request):
        html = (WEB_DIR / "pair.html").read_text("utf-8")
        html = html.replace("{{PAIR_URL}}", pair_url())
        html = html.replace("{{API_URL}}", f"{base_url}/api/clip?t={token}")
        return web.Response(text=html, content_type="text/html")

    async def pair_qr(request):
        img = qrcode.make(pair_url(), image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
        buf = io.BytesIO()
        img.save(buf)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def shortcut(request):
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
            web.get("/ws", websocket),
            web.get("/pair", pair),
            web.get("/pair/qr.svg", pair_qr),
            web.get("/shortcuts/{slug}.shortcut", shortcut),
            web.get("/manifest.webmanifest", static),
            web.get("/icon.svg", static),
        ]
    )
    return app
