"""Приём и отдача клипов и файлов через HTTP API. Буфер Windows не трогаем (пауза)."""
import asyncio
import io
import os
import socket
from urllib.parse import quote

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from phonenect import config
from phonenect.hub import Hub
from phonenect.peers import Peers
from phonenect.server import create_app

H = {"Authorization": "Bearer tok", "X-Device": "iPhone"}


@pytest.fixture
def api(tmp_path):
    """Запускает сценарий с клиентом к свежему серверу; отдаёт (client, hub)."""

    def runner(scenario):
        async def main():
            hub = Hub(tmp_path, "ПК-тест", config.pc_id("tok"))
            hub.paused = True
            hub.loop = asyncio.get_running_loop()
            cfg = {"token": "tok", "port": 1}
            async with TestClient(TestServer(create_app(hub, cfg, "http://x", Peers(hub, cfg)))) as client:
                await scenario(client, hub)

        asyncio.run(main())

    return runner


async def post(client, body, **headers):
    r = await client.post("/api/clip", data=body, headers={**H, **headers})
    return r.status, (await r.json() if r.status == 200 else await r.text())


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


PDF = b"%PDF-1.4\n\x00\xff\xfe binary" * 100


def test_text_and_image_go_to_clipboard(api):
    async def scenario(client, hub):
        assert (await post(client, "привет".encode(), **{"Content-Type": "text/plain"}))[1]["kind"] == "text"
        assert (await post(client, png(), **{"Content-Type": "image/png"}))[1]["kind"] == "image"

    api(scenario)


def test_document_type_is_saved_as_file(api, tmp_path):
    async def scenario(client, hub):
        _, m = await post(client, PDF, **{"Content-Type": "application/pdf"})
        assert m["kind"] == "file" and m["name"].endswith(".pdf") and m["size"] == len(PDF)
        assert (tmp_path / m["name"]).read_bytes() == PDF

    api(scenario)


def test_vague_type_is_sniffed(api):
    async def scenario(client, hub):
        assert (await post(client, PDF, **{"Content-Type": "application/octet-stream"}))[1]["kind"] == "file"
        assert (await post(client, b"plain words", **{"Content-Type": "application/octet-stream"}))[1]["kind"] == "text"

    api(scenario)


def test_filename_forces_file_and_is_made_safe_and_unique(api):
    async def scenario(client, hub):
        name = {"X-Filename": quote("Отчёт: итог?.txt")}
        assert (await post(client, b"hello", **name, **{"Content-Type": "text/plain"}))[1]["name"] == "Отчёт_ итог_.txt"
        assert (await post(client, b"hello", **name))[1]["name"] == "Отчёт_ итог_ (2).txt"
        assert (await post(client, b"x", **{"X-Filename": quote("../../evil/CON.txt")}))[1]["name"] == "_CON.txt"

    api(scenario)


def test_multipart_filename_is_decoded(api):
    async def scenario(client, hub):
        fd = FormData()
        fd.add_field("f", b"\x00\x01zip", filename="архив.zip", content_type="application/zip")
        assert (await post(client, fd))[1]["name"] == "архив.zip"

    api(scenario)


def test_auto_send_skips_known_file(api):
    async def scenario(client, hub):
        await post(client, PDF, **{"Content-Type": "application/pdf"})
        assert (await post(client, PDF, **{"Content-Type": "application/pdf", "X-Auto": "1"}))[1] == {"skipped": True}

    api(scenario)


def test_empty_body_is_rejected(api):
    async def scenario(client, hub):
        assert (await post(client, b"", **{"Content-Type": "application/pdf"}))[0] == 400

    api(scenario)


def test_large_file_streams_to_disk_and_back(api):
    async def scenario(client, hub):
        big = os.urandom(60 * 1024 * 1024)
        _, m = await post(client, big, **{"Content-Type": "application/octet-stream"})
        assert m["kind"] == "file" and m["size"] == len(big)
        r = await client.get(f"/api/clip/{m['id']}", headers=H)
        assert await r.read() == big
        assert r.headers["Content-Disposition"].startswith("attachment;")

    api(scenario)


def test_pc_file_is_served_but_not_to_automations(api, tmp_path):
    async def scenario(client, hub):
        doc = tmp_path / "from-pc.docx"
        doc.write_bytes(b"docx")
        hub._publish_files([str(doc)])
        assert hub.latest.meta()["name"] == "from-pc.docx"
        assert (await client.get("/api/clip", headers={**H, "X-Only-New": "1"})).status == 204
        r = await client.get("/api/clip", headers=H)
        assert await r.read() == b"docx"
        assert "filename*=UTF-8''from-pc.docx" in r.headers["Content-Disposition"]

    api(scenario)


def test_phone_name_in_header_is_percent_decoded(api):
    async def scenario(client, hub):
        _, m = await post(client, b"hi", **{"Content-Type": "text/plain", "X-Device": quote("Телефон Ромы")})
        assert m["source"] == "Телефон Ромы"

    api(scenario)


def test_info_reports_name_and_id(api):
    async def scenario(client, hub):
        r = await client.get("/api/info", headers=H)
        assert await r.json() == {"name": socket.gethostname()[:40], "id": config.pc_id("tok"), "peers": [], "blocked": []}
        assert (await client.get("/api/info")).status == 401

    api(scenario)


def test_pc_name_cannot_break_out_of_pages(tmp_path):
    async def main():
        cfg = {"token": "tok", "port": 1, "name": "</script><b>x"}
        hub = Hub(tmp_path, cfg["name"], "id")
        async with TestClient(TestServer(create_app(hub, cfg, "http://x", Peers(hub, cfg)))) as client:
            page = await (await client.get("/", headers=H)).text()
            assert "</script><b>" not in page and "u003c/script>" in page
            pair = await (await client.get("/pair")).text()
            assert "&lt;/script&gt;&lt;b&gt;x" in pair

    asyncio.run(main())


def run_local(tmp_path, scenario):
    async def main():
        cfg = {"token": "tok", "port": 1}
        hub = Hub(tmp_path, "ПК-тест", config.pc_id("tok"))
        async with TestClient(TestServer(create_app(hub, cfg, "http://x", Peers(hub, cfg)))) as client:
            await scenario(client)

    asyncio.run(main())


def test_other_site_cannot_link_pc_from_browser(tmp_path):
    """Страница чужого сайта шлёт no-cors POST на 127.0.0.1 — связь создаваться не должна."""

    async def scenario(client):
        body = '{"link": "http://evil.example/api/clip?t=x"}'
        r = await client.post("/pair/peers", data=body, headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
        assert r.status == 403
        r = await client.post("/pair/peers", data=body, headers={"Content-Type": "text/plain"})
        assert r.status == 415  # простой запрос без preflight — только JSON
        r = await client.delete("/pair/peers/abc", headers={"Origin": "https://evil.example"})
        assert r.status == 403

    run_local(tmp_path, scenario)


def test_pair_pages_refuse_foreign_host_name(tmp_path):
    """DNS rebinding: evil.example указывает на 127.0.0.1, но Host у запроса чужой."""

    async def scenario(client):
        assert (await client.get("/pair", headers={"Host": "evil.example:8765"})).status == 403
        assert (await client.get("/pair/peers", headers={"Host": "evil.example"})).status == 403
        assert (await client.get("/pair", headers={"Host": "localhost:8765"})).status == 200

    run_local(tmp_path, scenario)


def test_bad_link_body_is_400_not_500(tmp_path):
    async def scenario(client):
        r = await client.post("/pair/peers", data="not json", headers={"Content-Type": "application/json"})
        assert r.status == 400
        r = await client.post("/pair/peers", data="[1]", headers={"Content-Type": "application/json"})
        assert r.status == 400

    run_local(tmp_path, scenario)


def test_web_client_offers_this_pcs_own_mdns_name(tmp_path):
    """У двух ПК в сети не может быть одного имени phonenect.local — даём командам iPhone своё."""

    async def scenario(client):
        page = await (await client.get("/", headers=H)).text()
        assert f'"http://phonenect-{config.pc_id("tok")}.local:1/api/clip?t=tok"' in page

    run_local(tmp_path, scenario)
