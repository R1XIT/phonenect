"""Доверенные сети: Phonenect работает только в сетях, которые пользователь отметил сам."""
import subprocess

from . import config


def parse_profile(text: str) -> str | None:
    """Имя сетевого профиля из вывода PowerShell: первая непустая строка."""
    for line in text.lstrip("﻿").splitlines():
        if line.strip():
            return line.strip()
    return None


def profile_name(ip: str) -> str | None:
    """Имя профиля Windows («Сеть 2») для интерфейса с этим IP."""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"(Get-NetIPAddress -IPAddress '{ip}' | Get-NetConnectionProfile).Name"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return parse_profile(out.decode("utf-8", "replace"))
    except Exception:
        return None


def current_network(cfg: dict) -> str | None:
    """Название Wi-Fi, а без Wi-Fi — имя профиля Windows у интерфейса с IP домашней сети. VPN не считается."""
    ssid = config.wifi_ssid()
    if ssid:
        return ssid
    ip = config.lan_ip(cfg)
    if ip.startswith("127."):
        return None
    return profile_name(ip)


class NetworkGuard:
    """Текущая сеть и доверие к ней. trusted читают сервер, mDNS и связи ПК; обновляет фоновый поток."""

    def __init__(self, cfg: dict, detect=current_network) -> None:
        self.cfg, self.detect = cfg, detect
        self.name: str | None = None  # текущая сеть; None — не определилась

    @property
    def trusted(self) -> bool:
        return self.name is not None and self.name in self.cfg.get("trusted_networks", [])

    def refresh(self) -> bool:
        """Заново определяет сеть (медленно — не из цикла событий). True — доверие изменилось."""
        before = self.trusted
        self.name = self.detect(self.cfg)
        return self.trusted != before

    def toggle(self) -> None:
        """Добавляет текущую сеть в доверенные или убирает из них."""
        if self.name is None:
            return
        nets = self.cfg.setdefault("trusted_networks", [])
        if self.name in nets:
            nets.remove(self.name)
        else:
            nets.append(self.name)
        config.save(self.cfg)

    def migrate(self) -> None:
        """Первый запуск этой версии: доверяем сети, в которой ПК сейчас, чтобы дома всё продолжало работать."""
        if "trusted_networks" in self.cfg:
            return
        self.refresh()
        self.cfg["trusted_networks"] = [self.name] if self.name else []
        config.save(self.cfg)
