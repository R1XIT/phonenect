"""Объявляет ПК в сети (Bonjour/mDNS) как phonenect-<id>.local и phonenect.local, чтобы смена IP не ломала команды."""
import socket
import threading

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from . import config

SERVICE = "_phonenect._tcp.local."
RECHECK = 60  # секунд: IP мог смениться (переподключение Wi-Fi, DHCP)


def find(pc_id: str, timeout: float = 5) -> tuple[str, int] | None:
    """Ищет в сети Phonenect с данным id (связанный ПК сменил IP). Блокирует — звать в потоке."""
    found = threading.Event()
    result: list[tuple[str, int]] = []

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=2000)
            if info and info.properties.get(b"id") == pc_id.encode() and info.parsed_addresses():
                result.append((info.parsed_addresses()[0], info.port))
                found.set()

        update_service = add_service

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        browser = ServiceBrowser(zc, SERVICE, Listener())
        found.wait(timeout)
        browser.cancel()
    finally:
        zc.close()
    return result[0] if result else None


class Advertiser:
    def __init__(self, cfg: dict, guard=None) -> None:
        self.cfg, self.guard = cfg, guard
        self.ip: str | None = None
        self.zc: Zeroconf | None = None
        self.infos: list[ServiceInfo] = []
        self._stop = threading.Event()
        self._wake = threading.Event()  # проверить сеть и IP прямо сейчас
        self.recheck = RECHECK

    def _register(self, ip: str) -> None:
        self._unregister()
        # Слушаем только интерфейс домашней сети, чтобы не светиться в VPN-туннеле.
        self.zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
        name = config.pc_name(self.cfg)
        # Своё имя — для новых команд iPhone; общее phonenect.local — для настроенных раньше.
        hosts = [(config.mdns_host(self.cfg["token"]), ""), (config.MDNS_HOST, " (общее имя)")]
        self.infos = [
            ServiceInfo(
                SERVICE,
                f"Phonenect {name}{suffix}.{SERVICE}",
                addresses=[socket.inet_aton(ip)],
                port=self.cfg["port"],
                server=host + ".",
                # id отличает ПК друг от друга: телефон после смены IP ищет именно свой.
                properties={"path": "/", "id": config.pc_id(self.cfg["token"]), "name": name},
            )
            for host, suffix in hosts
        ]
        for info in self.infos:
            self.zc.register_service(info, allow_name_change=True)
        self.ip = ip

    def _unregister(self) -> None:
        if self.zc:
            try:
                self.zc.unregister_all_services()
                self.zc.close()
            except Exception:
                pass
        self.zc = None

    def run(self) -> None:
        """Поток: регистрирует имя и перерегистрирует его при смене IP."""
        while True:
            self._wake.clear()  # до работы, а не после ожидания: poke() посреди цикла не теряется
            ip = config.lan_ip(self.cfg)
            if self.guard and not self.guard.trusted:
                # Чужая сеть: ПК не объявляем; вернёмся в доверенную — зарегистрируемся заново.
                if self.zc:
                    self._unregister()
                    self.ip = None
            elif ip != self.ip and not ip.startswith("127."):
                try:
                    self._register(ip)
                except Exception as e:
                    print("mDNS недоступен:", e)
            self._wake.wait(self.recheck)
            if self._stop.is_set():
                break
        self._unregister()

    def poke(self) -> None:
        """Доверие к сети изменилось — не ждать следующей проверки."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
