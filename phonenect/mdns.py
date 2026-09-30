"""Объявляет ПК в сети как phonenect.local (Bonjour/mDNS), чтобы смена IP не ломала команды."""
import socket
import threading

from zeroconf import IPVersion, ServiceInfo, Zeroconf

from . import config

SERVICE = "_phonenect._tcp.local."
RECHECK = 60  # секунд: IP мог смениться (переподключение Wi-Fi, DHCP)


class Advertiser:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.ip: str | None = None
        self.zc: Zeroconf | None = None
        self.info: ServiceInfo | None = None
        self._stop = threading.Event()

    def _register(self, ip: str) -> None:
        self._unregister()
        # Слушаем только интерфейс домашней сети, чтобы не светиться в VPN-туннеле.
        self.zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
        self.info = ServiceInfo(
            SERVICE,
            f"Phonenect.{SERVICE}",
            addresses=[socket.inet_aton(ip)],
            port=self.cfg["port"],
            server=config.MDNS_HOST + ".",
            properties={"path": "/"},
        )
        self.zc.register_service(self.info, allow_name_change=True)
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
            ip = config.lan_ip(self.cfg)
            if ip != self.ip and not ip.startswith("127."):
                try:
                    self._register(ip)
                except Exception as e:
                    print("mDNS недоступен:", e)
            if self._stop.wait(RECHECK):
                break
        self._unregister()

    def stop(self) -> None:
        self._stop.set()
