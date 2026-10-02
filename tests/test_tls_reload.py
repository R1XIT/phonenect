"""Сертификат перевыпускается при смене IP без перезапуска сервера."""
import ssl

from cryptography import x509

from phonenect import certs
from phonenect.__main__ import TlsKeeper


def test_keeper_reloads_same_context_for_new_ip(tmp_path):
    keeper = TlsKeeper({"token": "tok", "name": "ДОМ"}, tmp_path)
    keeper.refresh("192.168.0.25")
    ctx = keeper.context()
    assert keeper.refresh("192.168.0.25") is False
    assert keeper.refresh("10.0.0.9") is True
    assert keeper.context() is ctx  # тот же объект: новые соединения получат новый сертификат
    leaf = x509.load_pem_x509_certificates(certs.server_files(tmp_path)[0].read_bytes())[0]
    ips = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
    assert "10.0.0.9" in {str(i) for i in ips}
    assert isinstance(ctx, ssl.SSLContext)
