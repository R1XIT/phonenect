"""Доверенные сети: определение сети, список доверенных, закрытие сервера для чужих запросов."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from unittest import mock

from phonenect import config, network
from phonenect.network import NetworkGuard
from phonenect.server import network_gate


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "save", lambda cfg: calls.append([*cfg.get("trusted_networks", [])]))
    return calls


def guard_in(cfg: dict, net):
    """Охранник, у которого сеть берётся из списка net (меняем его на ходу)."""
    current = {"name": net}
    guard = NetworkGuard(cfg, detect=lambda c: current["name"])
    guard.move = lambda name: current.update(name=name)
    return guard


def test_migrate_without_key_trusts_current_network(saved):
    cfg = {}
    guard = guard_in(cfg, "Home-5G")
    guard.migrate()
    assert cfg["trusted_networks"] == ["Home-5G"]
    assert saved == [["Home-5G"]]
    assert guard.trusted


def test_migrate_without_detected_network_makes_empty_list(saved):
    cfg = {}
    guard_in(cfg, None).migrate()
    assert cfg["trusted_networks"] == []
    assert saved == [[]]


def test_migrate_with_key_does_not_touch_config(saved):
    cfg = {"trusted_networks": ["Офис"]}
    guard_in(cfg, "Home-5G").migrate()
    assert cfg["trusted_networks"] == ["Офис"]
    assert saved == []


def test_trusted_only_for_listed_network():
    cfg = {"trusted_networks": ["Home-5G"]}
    guard = guard_in(cfg, "Cafe")
    guard.refresh()
    assert not guard.trusted
    guard.move("Home-5G")
    guard.refresh()
    assert guard.trusted


def test_unknown_network_is_never_trusted():
    guard = guard_in({"trusted_networks": ["Home-5G"]}, None)
    guard.refresh()
    assert guard.name is None and not guard.trusted


def test_no_list_in_config_means_untrusted():
    guard = guard_in({}, "Home-5G")
    guard.refresh()
    assert not guard.trusted


def test_refresh_tells_whether_trust_changed():
    guard = guard_in({"trusted_networks": ["Home-5G"]}, "Home-5G")
    assert guard.refresh() is True  # до первой проверки сеть неизвестна
    assert guard.refresh() is False
    guard.move("Cafe")
    assert guard.refresh() is True
    guard.move("Other")  # из недоверенной в другую недоверенную — доверие то же
    assert guard.refresh() is False


def test_toggle_adds_and_removes_current_network(saved):
    cfg = {"trusted_networks": ["Офис"]}
    guard = guard_in(cfg, "Cafe")
    guard.refresh()
    guard.toggle()
    assert cfg["trusted_networks"] == ["Офис", "Cafe"] and guard.trusted
    guard.toggle()
    assert cfg["trusted_networks"] == ["Офис"] and not guard.trusted
    assert saved == [["Офис", "Cafe"], ["Офис"]]


def test_toggle_without_network_does_nothing(saved):
    cfg = {"trusted_networks": []}
    guard = guard_in(cfg, None)
    guard.refresh()
    guard.toggle()
    assert cfg["trusted_networks"] == [] and saved == []


# ---------- определение сети ----------

def test_profile_name_is_first_non_empty_line():
    assert network.parse_profile("\r\nСеть 2\r\n") == "Сеть 2"
    assert network.parse_profile("﻿Home Office  \r\nlost\r\n") == "Home Office"


def test_profile_name_missing():
    assert network.parse_profile("") is None
    assert network.parse_profile("  \r\n \r\n") is None


def test_current_network_prefers_wifi(monkeypatch):
    monkeypatch.setattr(config, "wifi_ssid", lambda: "Home-5G")
    monkeypatch.setattr(network, "profile_name", lambda ip: pytest.fail("профиль не нужен"))
    assert network.current_network({}) == "Home-5G"


def test_current_network_falls_back_to_profile_of_lan_interface(monkeypatch):
    asked = []
    monkeypatch.setattr(config, "wifi_ssid", lambda: None)
    monkeypatch.setattr(config, "lan_ip", lambda cfg=None: "192.168.0.10")
    monkeypatch.setattr(network, "profile_name", lambda ip: asked.append(ip) or "Сеть 2")
    assert network.current_network({}) == "Сеть 2"
    assert asked == ["192.168.0.10"]


def test_current_network_unknown_without_wifi_and_ip(monkeypatch):
    monkeypatch.setattr(config, "wifi_ssid", lambda: None)
    monkeypatch.setattr(config, "lan_ip", lambda cfg=None: "127.0.0.1")
    monkeypatch.setattr(network, "profile_name", lambda ip: pytest.fail("без IP профиль не ищем"))
    assert network.current_network({}) is None


def test_profile_name_runs_powershell_without_window(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return mock.Mock(stdout="Сеть 2\r\n".encode("utf-8"))

    monkeypatch.setattr(network.subprocess, "run", fake_run)
    assert network.profile_name("192.168.0.10") == "Сеть 2"
    assert "192.168.0.10" in " ".join(seen["cmd"])
    assert seen["kw"]["creationflags"] == network.subprocess.CREATE_NO_WINDOW


def test_profile_name_without_powershell_is_none(monkeypatch):
    def broken(*a, **kw):
        raise OSError("нет powershell")

    monkeypatch.setattr(network.subprocess, "run", broken)
    assert network.profile_name("192.168.0.10") is None


# ---------- закрытый сервер ----------

class FakeTransport:
    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default


def ask(guard, remote: str):
    """Прогоняет запрос «с адреса remote» через network_gate; возвращает (статус, текст)."""
    request = make_mocked_request("GET", "/api/info", transport=FakeTransport((remote, 5000)))

    async def handler(request):
        return web.Response(text="пропущен")

    async def go():
        try:
            resp = await network_gate(guard)(request, handler)
        except web.HTTPException as e:
            return e.status, e.text
        return resp.status, resp.text

    return asyncio.run(go())


def test_gate_answers_503_not_403_to_remote_request_in_untrusted_network():
    guard = guard_in({"trusted_networks": []}, "Cafe")
    guard.refresh()
    assert ask(guard, "192.168.0.40") == (503, "Phonenect выключен: эта сеть не отмечена как доверенная")


def test_gate_lets_this_pc_through_in_untrusted_network():
    guard = guard_in({"trusted_networks": []}, "Cafe")
    guard.refresh()
    assert ask(guard, "127.0.0.1") == (200, "пропущен")
    assert ask(guard, "::1") == (200, "пропущен")


def test_gate_lets_remote_request_through_in_trusted_network():
    guard = guard_in({"trusted_networks": ["Home"]}, "Home")
    guard.refresh()
    assert ask(guard, "192.168.0.40") == (200, "пропущен")


def through_app(app, remote: str, path: str) -> int:
    """Весь путь запроса «с адреса remote» через middleware приложения."""
    app.freeze()  # без этого middleware не запускаются
    request = make_mocked_request("GET", path, transport=FakeTransport((remote, 5000)), app=app)

    async def go():
        try:
            return (await app._handle(request)).status
        except web.HTTPException as e:
            return e.status

    return asyncio.run(go())


@pytest.mark.parametrize("path", ["/api/info", "/ca.crt", "/shortcuts/to-pc.shortcut"])
def test_both_apps_close_for_remote_when_untrusted(tmp_path, path):
    from phonenect.hub import Hub
    from phonenect.peers import Peers
    from phonenect.server import create_app, create_setup_app
    from tls import make_certs

    cert_dir, fp = make_certs(tmp_path / "certs")
    cfg = {"token": "tok", "port": 1, "name": "ДОМ"}
    hub = Hub(tmp_path, "ДОМ", config.pc_id("tok"))
    guard = guard_in({"trusted_networks": []}, "Cafe")
    guard.refresh()
    main = create_app(hub, cfg, "https://x", Peers(hub, cfg), cert_dir, fp, guard=guard)
    setup = create_setup_app(cfg, cert_dir, fp, "https://x", guard=guard)
    assert through_app(main, "192.168.0.40", path) == 503
    assert through_app(setup, "192.168.0.40", path) == 503
    assert through_app(setup, "127.0.0.1", "/ca.crt") == 200
    guard.move("Home")
    guard.cfg["trusted_networks"].append("Home")
    guard.refresh()
    assert through_app(setup, "192.168.0.40", "/ca.crt") == 200


def test_advertiser_announces_only_in_trusted_network(monkeypatch):
    import threading
    import time

    from phonenect import mdns

    events = []
    monkeypatch.setattr(config, "lan_ip", lambda cfg=None: "192.168.0.10")
    adv = mdns.Advertiser({"token": "t", "port": 1}, guard=Guard(False))
    adv.recheck = 30
    monkeypatch.setattr(adv, "_register", lambda ip: (events.append("on"), setattr(adv, "zc", object()),
                                                      setattr(adv, "ip", ip)))
    monkeypatch.setattr(adv, "_unregister", lambda: (events.append("off") if adv.zc else None,
                                                     setattr(adv, "zc", None)))

    def settle(expected):
        for _ in range(100):
            if events == expected:
                return
            time.sleep(0.02)
        assert events == expected

    thread = threading.Thread(target=adv.run, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert events == []  # недоверенная сеть — не объявляемся
    adv.guard.trusted = True
    adv.poke()
    settle(["on"])
    adv.guard.trusted = False
    adv.poke()
    settle(["on", "off"])
    adv.guard.trusted = True
    adv.poke()
    settle(["on", "off", "on"])
    adv.stop()
    thread.join(2)
    assert not thread.is_alive()


class Guard:
    def __init__(self, trusted):
        self.trusted = trusted
