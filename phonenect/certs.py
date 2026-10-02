"""Сертификаты ПК: свой удостоверяющий центр (CA) и подписанный им сертификат сервера.

Телефоны и другие ПК доверяют только CA своего ПК (сверяют отпечаток из ссылки подключения).
Сертификат сервера перевыпускается при смене IP — CA при этом прежний, ничего переустанавливать не нужно.
"""
import datetime
import hashlib
import ipaddress
import plistlib
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
SERVER_DAYS = 397  # iOS не принимает сертификаты сервера дольше 825 дней
RENEW_BEFORE = datetime.timedelta(days=30)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def server_files(d: Path) -> tuple[Path, Path]:
    return d / "server.pem", d / "server.key"


def ca_pem(d: Path) -> bytes:
    return (d / "ca.pem").read_bytes()


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _load_ca(d: Path):
    try:
        cert = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
        key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), password=None)
        return cert, key
    except (OSError, ValueError):
        return None


def ensure_ca(d: Path, name: str) -> x509.Certificate:
    """CA этого ПК: создаётся один раз. Пересоздание меняет отпечаток — все устройства подключаются заново."""
    loaded = _load_ca(d)
    if loaded:
        return loaded[0]
    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Phonenect {name}"[:64])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now() - datetime.timedelta(minutes=5))
        .not_valid_after(now() + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    (d / "ca.key").write_bytes(_pem(key))
    (d / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


def _names(ip: str, pc_id: str) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip != "127.0.0.1":
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    names += [x509.DNSName(n) for n in ("localhost", f"phonenect-{pc_id}.local", "phonenect.local")]
    return names


def _current_is_fine(d: Path, ip: str) -> bool:
    pem, key_path = server_files(d)
    try:
        leaf = x509.load_pem_x509_certificates(pem.read_bytes())[0]
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    except (OSError, ValueError, IndexError):
        return False

    # Проверить, что сертификат выдан текущим CA
    if leaf.issuer != ca_cert.subject:
        return False
    try:
        leaf.verify_directly_issued_by(ca_cert)
    except Exception:  # x509.InvalidSignature или другие ошибки верификации
        return False

    # Проверить, что приватный ключ соответствует сертификату
    if key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        return False

    try:
        ips = {str(v) for v in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
               .get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        return False
    return ip in ips and leaf.not_valid_after_utc - now() > RENEW_BEFORE


def ensure_server_cert(d: Path, ip: str, pc_id: str) -> bool:
    """Сертификат сервера на текущий IP. True — выпущен новый (контекст TLS надо перечитать)."""
    if _current_is_fine(d, ip):
        return False
    loaded = _load_ca(d)
    if not loaded:
        raise RuntimeError("Нет сертификата CA: сначала вызовите ensure_ca")
    ca_cert, ca_key = loaded
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"phonenect-{pc_id}.local")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now() - datetime.timedelta(minutes=5))
        .not_valid_after(now() + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(_names(ip, pc_id)), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem, key_file = server_files(d)
    key_file.write_bytes(_pem(key))
    # Цепочка: лист и CA — клиенту хватит одного закреплённого CA.
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    return True


def mobileconfig(d: Path, name: str) -> bytes:
    """Профиль iOS с CA: после установки и включения доверия Safari и «Команды» принимают HTTPS ПК."""
    ca = x509.load_pem_x509_certificate(ca_pem(d))
    der = ca.public_bytes(serialization.Encoding.DER)
    ident = f"dev.phonenect.ca.{fingerprint(ca)[:12]}"
    return plistlib.dumps({
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root",
            "PayloadVersion": 1,
            "PayloadIdentifier": ident + ".cert",
            "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, ident + ".cert")).upper(),
            "PayloadDisplayName": f"Phonenect {name}",
            "PayloadContent": der,
        }],
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": ident,
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, ident)).upper(),
        "PayloadDisplayName": f"Phonenect {name}",
        "PayloadDescription": "Сертификат вашего ПК для защищённой связи с Phonenect.",
    })
