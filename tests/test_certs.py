"""Сертификаты ПК: свой CA и подписанный им сертификат сервера."""
import datetime
import plistlib
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from phonenect import certs

ID = "863a39a5d131"


def san(cert):
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in ext.get_values_for_type(x509.IPAddress)} | set(ext.get_values_for_type(x509.DNSName))


def load_server(d):
    pem, _ = certs.server_files(d)
    return x509.load_pem_x509_certificates(pem.read_bytes())


def test_ca_is_created_once_and_reused(tmp_path):
    first = certs.ensure_ca(tmp_path, "ДОМ")
    again = certs.ensure_ca(tmp_path, "ДОМ")
    assert certs.fingerprint(first) == certs.fingerprint(again)
    assert first.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert len(certs.fingerprint(first)) == 64


def test_server_cert_names_every_address(tmp_path):
    certs.ensure_ca(tmp_path, "ДОМ")
    assert certs.ensure_server_cert(tmp_path, "192.168.0.25", ID) is True
    leaf, ca = load_server(tmp_path)
    assert san(leaf) == {"192.168.0.25", "127.0.0.1", "localhost", f"phonenect-{ID}.local", "phonenect.local"}
    assert leaf.issuer == ca.subject
    days = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
    assert days <= 397


def test_server_cert_is_kept_while_valid_and_reissued_for_new_ip(tmp_path):
    certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    assert certs.ensure_server_cert(tmp_path, "192.168.0.25", ID) is False
    assert certs.ensure_server_cert(tmp_path, "10.0.0.7", ID) is True
    assert "10.0.0.7" in san(load_server(tmp_path)[0])


def test_server_cert_is_renewed_before_expiry(tmp_path, monkeypatch):
    certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=380)
    monkeypatch.setattr(certs, "now", lambda: later)
    assert certs.ensure_server_cert(tmp_path, "192.168.0.25", ID) is True


def test_broken_server_files_are_recreated(tmp_path):
    certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    certs.server_files(tmp_path)[0].write_text("мусор")
    assert certs.ensure_server_cert(tmp_path, "192.168.0.25", ID) is True


def test_python_client_trusting_ca_accepts_server_chain(tmp_path):
    certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(*certs.server_files(tmp_path))  # не падает: ключ и цепочка согласованы
    client = ssl.create_default_context(cadata=certs.ca_pem(tmp_path).decode())
    assert client.get_ca_certs()[0]["subject"]


def test_mobileconfig_contains_the_ca(tmp_path):
    ca = certs.ensure_ca(tmp_path, "ДОМ")
    profile = plistlib.loads(certs.mobileconfig(tmp_path, "ДОМ"))
    payload = profile["PayloadContent"][0]
    assert payload["PayloadType"] == "com.apple.security.root"
    assert payload["PayloadContent"] == ca.public_bytes(serialization.Encoding.DER)
    assert profile["PayloadType"] == "Configuration"
