"""Сертификаты и TLS-контексты для тестов."""
import ssl

from phonenect import certs

TEST_ID = "0123456789ab"


def make_certs(d):
    ca = certs.ensure_ca(d, "тест")
    certs.ensure_server_cert(d, "127.0.0.1", TEST_ID)
    return d, certs.fingerprint(ca)


def server_ssl(d):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(*certs.server_files(d))
    return ctx


def client_ssl(d):
    ctx = ssl.create_default_context(cadata=certs.ca_pem(d).decode())
    ctx.check_hostname = False  # как у Android и ПК: доверяем CA, имя не важно
    return ctx
