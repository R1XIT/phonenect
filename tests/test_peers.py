"""Связь двух ПК: два настоящих сервера Phonenect в одном процессе, буфер Windows не трогаем."""
import asyncio

import pytest
from aiohttp.test_utils import TestServer

from phonenect import config
from phonenect.hub import Hub
from phonenect import peers as peers_module
from phonenect.peers import LinkError, Peers
from phonenect.server import create_app
from tls import make_certs, server_ssl


async def nowhere(pc_id):
    return None


class Pc:
    started: list["Pc"] = []  # все запущенные в сценарии — чтобы остановить их разом

    def __init__(self, name: str, tmp_path, key: str | None = None):
        key = key or name  # у двух ПК может быть одно имя — различаются токеном и папкой
        self.cfg = {"token": f"token-{key}", "port": 1, "name": name}
        self.hub = Hub(tmp_path / key, name, config.pc_id(self.cfg["token"]))
        self.hub.files_dir.mkdir()
        self.hub.paused = True  # не писать в настоящий буфер Windows
        self.cert_dir, self.fp = make_certs(tmp_path / f"certs-{key}")  # у каждого ПК свой CA
        self.peers = Peers(self.hub, self.cfg)
        self.peers.find = nowhere  # настоящий поиск по mDNS в тестах не нужен и идёт секунды
        self.server: TestServer | None = None

    async def start(self):
        self.hub.loop = asyncio.get_running_loop()
        self.peers.start()
        self.server = TestServer(create_app(self.hub, self.cfg, "https://x", self.peers, self.cert_dir, self.fp))
        await self.server.start_server(ssl=server_ssl(self.cert_dir))
        Pc.started.append(self)

    @property
    def link(self) -> str:
        return str(self.server.make_url(f"/api/clip?t={self.cfg['token']}&fp={self.fp}"))

    def copy(self, text: str):
        """Как будто на этом ПК скопировали текст."""
        self.hub._publish_local("text", text.encode(), "text/plain; charset=utf-8")

    def texts(self) -> list[tuple[str, str]]:
        return [(c.source, c.data.decode()) for c in self.hub.history if c.kind == "text"]

    @staticmethod
    async def stop_all():
        # Сначала рвём связи, потом гасим серверы: иначе сервер ждёт открытые WebSocket.
        for pc in Pc.started:
            for p in list(pc.peers.items):
                p.stop()
        await asyncio.sleep(0)
        for pc in Pc.started:
            await pc.peers.http.close()
            await pc.server.close()
        Pc.started.clear()


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "save", lambda cfg: None)  # не трогать настоящий config.json

    def runner(scenario):
        async def main():
            a, b = Pc("ALPHA", tmp_path), Pc("BETA", tmp_path)
            await a.start()
            await b.start()
            try:
                await scenario(a, b)
            finally:
                await Pc.stop_all()

        asyncio.run(main())

    return runner


async def until(check, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("не дождались")


async def linked(a: Pc, b: Pc):
    await a.peers.add(b.link)
    await until(lambda: a.peers.items[0].connected)


def test_text_copied_on_linking_pc_reaches_other_pc(run):
    async def scenario(a, b):
        await linked(a, b)
        a.copy("от альфы")
        await until(lambda: ("ALPHA", "от альфы") in b.texts())

    run(scenario)


def test_text_copied_on_linked_pc_comes_back_under_its_name(run):
    async def scenario(a, b):
        await linked(a, b)
        b.copy("от беты")
        await until(lambda: ("BETA", "от беты") in a.texts())

    run(scenario)


def test_own_clip_does_not_echo_back(run):
    async def scenario(a, b):
        await linked(a, b)
        a.copy("один раз")
        await until(lambda: b.texts())
        await asyncio.sleep(0.5)
        assert a.texts() == [("ПК", "один раз")]

    run(scenario)


def test_linking_back_is_refused_because_one_link_works_both_ways(run):
    async def scenario(a, b):
        await linked(a, b)
        with pytest.raises(LinkError, match="уже связан"):
            await b.peers.add(a.link)

    run(scenario)


def test_three_pcs_get_each_clip_exactly_once(run, tmp_path):
    async def scenario(a, b):
        c = Pc("GAMMA", tmp_path)
        await c.start()
        await linked(a, b)
        await linked(a, c)
        await b.peers.add(c.link)
        await until(lambda: b.peers.items[0].connected)

        doc = tmp_path / "смета.xlsx"
        doc.write_bytes(b"xlsx")
        a.hub._publish_files([str(doc)], explicit=True)
        c.copy("от гаммы")
        await until(lambda: (b.hub.files_dir / "смета.xlsx").exists() and (c.hub.files_dir / "смета.xlsx").exists())
        await until(lambda: ("GAMMA", "от гаммы") in a.texts() and ("GAMMA", "от гаммы") in b.texts())
        await asyncio.sleep(1)
        for pc in (b, c):
            assert [p.name for p in pc.hub.files_dir.iterdir()] == ["смета.xlsx"]
        assert list(a.hub.files_dir.iterdir()) == []
        for pc in (a, b):
            assert pc.texts().count(("GAMMA", "от гаммы")) == 1

    run(scenario)


def test_phone_clip_keeps_phone_name_on_other_pc(run):
    async def scenario(a, b):
        await linked(a, b)
        await a.hub.add_remote("с телефона".encode(), "text/plain", "Pixel 9")
        await until(lambda: ("Pixel 9", "с телефона") in b.texts())

    run(scenario)


def test_explicitly_sent_file_lands_in_other_pcs_folder(run, tmp_path):
    async def scenario(a, b):
        await linked(a, b)
        doc = tmp_path / "отчёт.pdf"
        doc.write_bytes(b"%PDF data")
        a.hub._publish_files([str(doc)], explicit=True)
        await until(lambda: (b.hub.files_dir / "отчёт.pdf").exists())
        assert (b.hub.files_dir / "отчёт.pdf").read_bytes() == b"%PDF data"
        assert b.hub.latest.source == "ALPHA"

    run(scenario)


def test_own_file_does_not_come_back_as_a_copy(run, tmp_path):
    async def scenario(a, b):
        await linked(a, b)
        doc = tmp_path / "план.docx"
        doc.write_bytes(b"docx")
        a.hub._publish_files([str(doc)], explicit=True)
        await until(lambda: (b.hub.files_dir / "план.docx").exists())
        await asyncio.sleep(0.7)
        assert list(a.hub.files_dir.iterdir()) == []
        assert [c.kind for c in a.hub.history] == ["file"]

    run(scenario)


def test_late_echo_does_not_overwrite_newer_copy(run):
    async def scenario(a, b):
        await linked(a, b)
        a.copy("старое")
        a.copy("новое")
        await until(lambda: ("ALPHA", "новое") in b.texts())
        await asyncio.sleep(0.7)
        assert a.hub.latest.data.decode() == "новое"
        assert a.texts() == [("ПК", "старое"), ("ПК", "новое")]

    run(scenario)


def test_link_survives_renaming_the_other_pc(run):
    async def scenario(a, b):
        await linked(a, b)
        b.hub.name = "BETA-НОВОЕ"  # на том ПК сменили имя компьютера
        b.copy("после переименования")
        await until(lambda: any(t == "после переименования" for _, t in a.texts()))

    run(scenario)


def test_pcs_with_same_name_do_not_bounce_files(run, tmp_path):
    async def scenario(a, _):
        twin = Pc("ALPHA", tmp_path, key="TWIN")
        await twin.start()
        await linked(a, twin)
        doc = tmp_path / "одно.txt"
        doc.write_bytes(b"1")
        a.hub._publish_files([str(doc)], explicit=True)
        await until(lambda: (twin.hub.files_dir / "одно.txt").exists())
        await asyncio.sleep(1)
        assert [p.name for p in twin.hub.files_dir.iterdir()] == ["одно.txt"]
        assert list(a.hub.files_dir.iterdir()) == []

    run(scenario)


def test_quick_copies_arrive_in_order(run):
    async def scenario(a, b):
        await linked(a, b)
        for i in range(20):
            a.copy(f"копия {i}")
        await until(lambda: len(b.texts()) == 20)
        assert [t for _, t in b.texts()] == [f"копия {i}" for i in range(20)]
        assert b.hub.latest.data.decode() == "копия 19"

    run(scenario)


def test_linked_pc_sees_link_made_from_other_side(run):
    async def scenario(a, b):
        await linked(a, b)
        await until(lambda: any(p["name"] == "ALPHA" for p in b.peers.status()))
        theirs = next(p for p in b.peers.status() if p["name"] == "ALPHA")
        assert theirs["incoming"] and theirs["connected"]

    run(scenario)


def test_unlinking_on_other_side_ends_link_for_both(run):
    async def scenario(a, b):
        await linked(a, b)
        await until(lambda: b.peers.status())
        async with b.peers.http.delete(b.server.make_url(f"/pair/peers/{a.hub.id}"), ssl=False) as r:
            assert r.status == 200
        await until(lambda: a.peers.items == [])
        assert b.peers.status() == []
        b.copy("после отвязки")
        await asyncio.sleep(0.5)
        assert "после отвязки" not in [t for _, t in a.texts()]
        with pytest.raises(LinkError, match="отвязан"):
            await a.peers.add(b.link)

    run(scenario)


def test_relinking_from_side_that_unlinked_works(run):
    async def scenario(a, b):
        await linked(a, b)
        await until(lambda: b.peers.status())
        await b.peers.unlink(a.hub.id)
        await until(lambda: a.peers.items == [])
        await linked(b, a)
        a.copy("снова вместе")
        await until(lambda: ("ALPHA", "снова вместе") in b.texts())

    run(scenario)


def test_link_follows_pc_to_new_address(run, monkeypatch):
    monkeypatch.setattr(peers_module, "RETRY_DELAY", 0.1)

    async def scenario(a, b):
        await linked(a, b)
        # У того ПК сменился IP: старый адрес молчит, ПК теперь на другом.
        for ws in list(b.hub.sockets):  # соединения рвутся, как при выключении ПК
            await ws.close()
        await b.server.close()
        b.server = TestServer(create_app(b.hub, b.cfg, "https://x", b.peers, b.cert_dir, b.fp))
        await b.server.start_server(ssl=server_ssl(b.cert_dir))
        moved_to = (b.server.host, b.server.port)
        asked = []

        async def find(pc_id):
            asked.append(pc_id)
            return moved_to

        a.peers.find = find
        await until(lambda: a.peers.items[0].connected and a.peers.items[0].url.endswith(f":{moved_to[1]}"))
        assert asked and set(asked) == {b.hub.id}
        assert a.cfg["peers"][0]["url"] == f"https://{moved_to[0]}:{moved_to[1]}"
        b.copy("на новом адресе")
        await until(lambda: ("BETA", "на новом адресе") in a.texts())

    run(scenario)


def test_html_source_copied_on_pc_arrives_unchanged(run):
    async def scenario(a, b):
        await linked(a, b)
        a.copy("<!doctype html><p>исходник</p>")
        await until(lambda: b.texts())
        assert b.texts() == [("ALPHA", "<!doctype html><p>исходник</p>")]

    run(scenario)


def test_clip_from_other_pcs_phone_still_reaches_same_named_phone_here(run):
    async def scenario(a, b):
        await linked(a, b)
        await a.hub.add_remote("с айфона на альфе".encode(), "text/plain", "iPhone")
        await until(lambda: ("iPhone", "с айфона на альфе") in b.texts())
        # «iPhone» у беты (имя по умолчанию у всех айфонов) должен получить это как новое
        assert b.hub.take_new("iPhone", max_age=60) is not None

    run(scenario)


def test_cannot_link_to_itself(run):
    async def scenario(a, b):
        with pytest.raises(LinkError, match="этого же ПК"):
            await a.peers.add(a.link)

    run(scenario)


def test_wrong_token_is_rejected(run):
    async def scenario(a, b):
        with pytest.raises(LinkError, match="токен"):
            await a.peers.add(b.link.replace("token-BETA", "wrong"))

    run(scenario)


def test_link_with_wrong_fingerprint_is_refused(run):
    async def scenario(a, b):
        bad = b.link.replace(b.fp, "0" * 64)
        with pytest.raises(LinkError, match="Сертификат не совпадает"):
            await a.peers.add(bad)
        assert a.peers.items == []

    run(scenario)


def test_link_without_fingerprint_is_refused(run):
    async def scenario(a, b):
        old = b.link.split("&fp=")[0]
        with pytest.raises(LinkError, match="Старая ссылка"):
            await a.peers.add(old)

    run(scenario)


def test_link_keeps_trusting_only_saved_ca(run, tmp_path):
    async def scenario(a, b):
        await linked(a, b)
        assert a.cfg["peers"][0]["ca"].startswith("-----BEGIN CERTIFICATE-----")
        # Тот же адрес теперь отвечает чужим сертификатом — связь не должна подняться.
        imposter_dir, _ = make_certs(tmp_path / "подмена")
        old_port = b.server.port
        for ws in list(b.hub.sockets):
            await ws.close()
        await b.server.close()
        app = create_app(b.hub, b.cfg, "https://x", b.peers, imposter_dir, b.fp)
        try:
            b.server = TestServer(app, port=old_port)
            await b.server.start_server(ssl=server_ssl(imposter_dir))
        except OSError:  # Windows не сразу освобождает порт: подмена на другом порту, связь указывает на неё
            b.server = TestServer(app)
            await b.server.start_server(ssl=server_ssl(imposter_dir))
            a.peers.items[0].url = f"https://{b.server.host}:{b.server.port}"
        await asyncio.sleep(1.5)
        assert not a.peers.items[0].connected

    run(scenario)


def test_old_http_link_from_config_is_shown_as_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "save", lambda cfg: None)

    async def main():
        pc = Pc("ALPHA", tmp_path)
        pc.cfg["peers"] = [{"url": "http://192.168.0.40:8765", "token": "t", "name": "СТАРЫЙ"}]
        await pc.start()
        try:
            assert pc.peers.status() == [{"id": config.pc_id("t"), "name": "СТАРЫЙ", "url": "http://192.168.0.40:8765",
                                          "connected": False, "incoming": False, "stale": True}]
        finally:
            await Pc.stop_all()

    asyncio.run(main())


def test_pair_page_api_manages_links(run):
    async def scenario(a, b):
        client = a.server.make_url("/pair/peers")
        http = a.peers.http
        async with http.post(client, json={"link": b.link}, ssl=False) as r:
            assert r.status == 200
            assert [p["name"] for p in await r.json()] == ["BETA"]
        async with http.delete(a.server.make_url(f"/pair/peers/{a.peers.items[0].id}"), ssl=False) as r:
            assert await r.json() == []
        assert a.hub.listeners == []

    run(scenario)
