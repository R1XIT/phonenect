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


def test_server_cert_reissued_when_ca_changes(tmp_path):
    """При смене CA старый сертификат сервера перевыпускается."""
    old_ca = certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    old_leaf = load_server(tmp_path)[0]

    # Удалить старый CA
    (tmp_path / "ca.pem").unlink()
    (tmp_path / "ca.key").unlink()

    # Создать новый CA
    new_ca = certs.ensure_ca(tmp_path, "ДОМ")
    assert certs.fingerprint(old_ca) != certs.fingerprint(new_ca)

    # Сертификат сервера должен быть переиздан для нового CA
    assert certs.ensure_server_cert(tmp_path, "192.168.0.25", ID) is True
    new_leaf = load_server(tmp_path)[0]

    # Новый лист должен быть выдан новым CA
    new_ca_from_chain = load_server(tmp_path)[1]
    assert certs.fingerprint(new_ca_from_chain) == certs.fingerprint(new_ca), "Цепочка содержит новый CA"
    old_leaf.verify_directly_issued_by(old_ca)
    new_leaf.verify_directly_issued_by(new_ca)


def test_ensure_server_cert_requires_ca(tmp_path):
    """ensure_server_cert падает с RuntimeError если CA отсутствует."""
    import pytest
    with pytest.raises(RuntimeError, match="Нет сертификата CA"):
        certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)


def test_real_tls_handshake_with_server_chain(tmp_path):
    """TLS handshake успешен когда клиент доверяет CA."""
    certs.ensure_ca(tmp_path, "ДОМ")
    certs.ensure_server_cert(tmp_path, "192.168.0.25", ID)
    pem_path, key_path = certs.server_files(tmp_path)
    ca_pem = certs.ca_pem(tmp_path)

    # Сервер с сертификатом
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(pem_path, key_path)

    # Клиент доверяет CA этого сервера
    client_ctx = ssl.create_default_context(cadata=ca_pem.decode())
    client_ctx.check_hostname = True
    client_ctx.server_hostname = "localhost"

    # Handshake в памяти через MemoryBIO
    server_bio_in = ssl.MemoryBIO()
    server_bio_out = ssl.MemoryBIO()
    client_bio_in = ssl.MemoryBIO()
    client_bio_out = ssl.MemoryBIO()

    server_conn = server_ctx.wrap_bio(server_bio_in, server_bio_out, server_side=True)
    client_conn = client_ctx.wrap_bio(client_bio_in, client_bio_out, server_side=False, server_hostname="localhost")

    # Шаттл данных между BIO до успешного handshake
    client_done = False
    server_done = False

    for _ in range(100):  # Максимум итераций
        if not client_done:
            try:
                client_conn.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass

        if not server_done:
            try:
                server_conn.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass

        # Шаттл данных
        client_to_server = client_bio_out.pending
        if client_to_server:
            server_bio_in.write(client_bio_out.read())

        server_to_client = server_bio_out.pending
        if server_to_client:
            client_bio_in.write(server_bio_out.read())

        if client_done and server_done:
            break

    assert client_done and server_done, "Handshake не успел завершиться"


def test_tls_handshake_fails_with_different_ca(tmp_path):
    """TLS handshake не удаётся когда клиент доверяет другому CA."""
    import pytest

    # Создать два разных CA
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()

    certs.ensure_ca(dir1, "ДОМ1")
    certs.ensure_ca(dir2, "ДОМ2")
    certs.ensure_server_cert(dir1, "192.168.0.25", ID)

    pem_path, key_path = certs.server_files(dir1)
    ca_pem_2 = certs.ca_pem(dir2)  # Клиент доверяет другому CA

    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(pem_path, key_path)

    client_ctx = ssl.create_default_context(cadata=ca_pem_2.decode())
    client_ctx.check_hostname = False  # Пропустить проверку имени хоста

    server_bio_in = ssl.MemoryBIO()
    server_bio_out = ssl.MemoryBIO()
    client_bio_in = ssl.MemoryBIO()
    client_bio_out = ssl.MemoryBIO()

    server_conn = server_ctx.wrap_bio(server_bio_in, server_bio_out, server_side=True)
    client_conn = client_ctx.wrap_bio(client_bio_in, client_bio_out, server_side=False, server_hostname="localhost")

    # Попытаться выполнить handshake — должно упасть с ошибкой верификации
    client_error = None
    server_error = None

    for _ in range(100):
        if not client_error:
            try:
                client_conn.do_handshake()
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLCertVerificationError as e:
                client_error = e

        if not server_error:
            try:
                server_conn.do_handshake()
            except ssl.SSLWantReadError:
                pass
            except (ssl.SSLError, ssl.SSLCertVerificationError) as e:
                server_error = e

        # Шаттл данных
        client_to_server = client_bio_out.pending
        if client_to_server:
            server_bio_in.write(client_bio_out.read())

        server_to_client = server_bio_out.pending
        if server_to_client:
            client_bio_in.write(server_bio_out.read())

        if client_error or server_error:
            break

    # Должна быть ошибка верификации
    assert client_error is not None or server_error is not None, "Handshake должен был упасть с разными CA"


def test_mobileconfig_contains_the_ca(tmp_path):
    ca = certs.ensure_ca(tmp_path, "ДОМ")
    profile = plistlib.loads(certs.mobileconfig(tmp_path, "ДОМ"))
    payload = profile["PayloadContent"][0]
    assert payload["PayloadType"] == "com.apple.security.root"
    assert payload["PayloadContent"] == ca.public_bytes(serialization.Encoding.DER)
    assert profile["PayloadType"] == "Configuration"
