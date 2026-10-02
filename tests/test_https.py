"""HTTPS-сервер и открытая стартовая страница."""
import asyncio
import ssl

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from phonenect import certs, config
from phonenect.hub import Hub
from phonenect.peers import Peers
from phonenect.server import create_app, create_setup_app
from tls import client_ssl, make_certs, server_ssl

TOKEN = "tok"


def run(tmp_path, scenario):
    async def main():
        cert_dir, fp = make_certs(tmp_path / "certs")
        cfg = {"token": TOKEN, "port": 1, "name": "ДОМ"}
        hub = Hub(tmp_path, "ДОМ", config.pc_id(TOKEN))
        hub.loop = asyncio.get_running_loop()
        base = lambda: "https://127.0.0.1:8765"
        peers = Peers(hub, cfg)
        app = create_app(hub, cfg, base, peers, cert_dir, fp)
        https = TestServer(app)
        await https.start_server(ssl=server_ssl(cert_dir))
        setup = TestServer(create_setup_app(cfg, cert_dir, fp, base, peers))
        await setup.start_server()
        async with aiohttp.ClientSession() as http:
            try:
                await scenario(http, https, setup, cert_dir, fp)
            finally:
                await https.close()
                await setup.close()

    asyncio.run(main())


def test_api_works_over_https_with_pinned_ca(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        async with http.get(https.make_url(f"/api/info?t={TOKEN}"), ssl=client_ssl(cert_dir)) as r:
            assert r.status == 200 and (await r.json())["name"] == "ДОМ"

    run(tmp_path, scenario)


def test_client_with_other_ca_is_refused(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        other, _ = make_certs(tmp_path / "чужой")
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await http.get(https.make_url(f"/api/info?t={TOKEN}"), ssl=client_ssl(other))

    run(tmp_path, scenario)


def test_ca_is_public_on_https_and_matches_fingerprint(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        async with http.get(https.make_url("/ca.crt"), ssl=False) as r:
            pem = await r.read()
        from cryptography import x509
        assert certs.fingerprint(x509.load_pem_x509_certificate(pem)) == fp

    run(tmp_path, scenario)


def test_setup_port_serves_only_public_files(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        for path in ("/", "/ca.crt", "/ca.mobileconfig", "/icon.svg"):
            async with http.get(setup.make_url(path)) as r:
                assert r.status == 200, path
        async with http.get(setup.make_url("/ca.mobileconfig")) as r:
            assert r.headers["Content-Type"] == "application/x-apple-aspen-config"
        for path in (f"/api/clip?t={TOKEN}", f"/api/info?t={TOKEN}", "/ws", f"/?t={TOKEN}x"):
            async with http.get(setup.make_url(path), allow_redirects=False) as r:
                body = await r.text()
                assert TOKEN not in body, path
                assert r.status in (200, 404), path
        async with http.post(setup.make_url(f"/api/clip?t={TOKEN}"), data=b"x") as r:
            assert r.status in (404, 405)

    run(tmp_path, scenario)


def test_pair_page_opens_locally_over_setup_port_but_not_remotely(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        async with http.get(setup.make_url("/pair")) as r:
            assert r.status == 200  # с 127.0.0.1 — можно
        async with http.get(setup.make_url("/pair"), headers={"Host": "evil.example"}) as r:
            assert r.status == 403

    run(tmp_path, scenario)


def test_setup_page_shows_fingerprint_and_https_next_step(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        page = await (await http.get(setup.make_url("/"))).text()
        assert fp in page.replace(" ", "").lower() and "https://127.0.0.1:8765" in page

    run(tmp_path, scenario)


def test_pair_page_links_are_https_with_fingerprint(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        async with http.get(https.make_url("/pair"), ssl=client_ssl(cert_dir)) as r:
            page = await r.text()
        assert f"https://127.0.0.1:8765/api/clip?t={TOKEN}&amp;fp={fp}" in page or \
            f"https://127.0.0.1:8765/api/clip?t={TOKEN}&fp={fp}" in page
        assert "http://127.0.0.1:" in page  # шаг 1 — стартовая страница

    run(tmp_path, scenario)


def test_web_client_gets_https_api_address(tmp_path):
    async def scenario(http, https, setup, cert_dir, fp):
        async with http.get(https.make_url("/"), headers={"Authorization": f"Bearer {TOKEN}"},
                            ssl=client_ssl(cert_dir)) as r:
            page = await r.text()
        assert f'"https://{https.host}:{https.port}/api/clip?t={TOKEN}&fp={fp}"' in page

    run(tmp_path, scenario)
