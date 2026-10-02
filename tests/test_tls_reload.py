"""Сертификат перевыпускается при смене IP без перезапуска сервера."""
import ssl

import pytest
from cryptography import x509

from phonenect import certs
from phonenect.__main__ import TlsKeeper


def handshake(server_ctx, ca_pem, hostname):
    """Настоящее TLS-рукопожатие в памяти; возвращает сертификат сервера, каким его увидел клиент."""
    client_ctx = ssl.create_default_context(cadata=ca_pem.decode())
    s_in, s_out, c_in, c_out = (ssl.MemoryBIO() for _ in range(4))
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    client = client_ctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=hostname)
    done = [False, False]
    for _ in range(100):
        for i, conn in enumerate((client, server)):
            if not done[i]:
                try:
                    conn.do_handshake()
                    done[i] = True
                except ssl.SSLWantReadError:
                    pass
        if c_out.pending:
            s_in.write(c_out.read())
        if s_out.pending:
            c_in.write(s_out.read())
        if all(done):
            break
    assert all(done), "рукопожатие не завершилось"
    return x509.load_der_x509_certificate(client.getpeercert(binary_form=True))


def ips_of(cert):
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def test_keeper_reloads_same_context_for_new_ip(tmp_path):
    keeper = TlsKeeper({"token": "tok", "name": "ДОМ"}, tmp_path)
    keeper.refresh("192.168.0.25")
    ctx = keeper.context()
    ca = certs.ca_pem(tmp_path)
    assert "192.168.0.25" in ips_of(handshake(ctx, ca, "192.168.0.25"))
    assert keeper.refresh("192.168.0.25") is False
    assert keeper.refresh("10.0.0.9") is True
    assert keeper.context() is ctx  # тот же объект: новые соединения получат новый сертификат
    assert "10.0.0.9" in ips_of(handshake(ctx, ca, "10.0.0.9"))
    assert isinstance(ctx, ssl.SSLContext)


def test_keeper_retries_load_after_failure(tmp_path):
    keeper = TlsKeeper({"token": "tok", "name": "ДОМ"}, tmp_path)
    keeper.refresh("192.168.0.25")
    real = keeper.context()

    class Flaky:
        fail = True

        def load_cert_chain(self, *args):
            if self.fail:
                self.fail = False
                raise OSError("файл занят")
            real.load_cert_chain(*args)

    keeper._ctx = Flaky()
    with pytest.raises(OSError):
        keeper.refresh("10.0.0.9")  # файлы уже перезаписаны, а загрузка упала
    assert keeper.refresh("10.0.0.9") is False  # сертификат уже выпущен...
    ca = certs.ca_pem(tmp_path)
    assert "10.0.0.9" in ips_of(handshake(real, ca, "10.0.0.9"))  # ...но загрузка повторена
