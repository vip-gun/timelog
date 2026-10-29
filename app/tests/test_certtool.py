"""Certificate helpers used by the offline package (self-signed certificate, PFX -> PEM)."""
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from timelog import certtool


def _pair(cn, issuer=None, issuer_key=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer.subject if issuer else name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(issuer_key or key, hashes.SHA256()))
    return key, cert


def test_selfsigned_certificate(tmp_path):
    certtool.selfsigned("timelog.test.local", tmp_path)
    cert = x509.load_pem_x509_certificate((tmp_path / "server.crt").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "timelog.test.local" in names
    key = serialization.load_pem_private_key((tmp_path / "server.key").read_bytes(), None)
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()
    assert (tmp_path / "server.key").stat().st_mode & 0o077 == 0


def test_pfx_to_pem_puts_chain_after_leaf(tmp_path):
    ca_key, ca = _pair("Test Root CA")
    key, leaf = _pair("timelog.corp.example.lu", ca, ca_key)
    pfx = tmp_path / "server.pfx"
    pfx.write_bytes(pkcs12.serialize_key_and_certificates(
        b"x", key, leaf, [ca], serialization.BestAvailableEncryption(b"secret-pw")))
    out = tmp_path / "out"
    certtool.pfx2pem(pfx, out, "secret-pw")
    certs = x509.load_pem_x509_certificates((out / "server.crt").read_bytes())
    assert [c.subject.rfc4514_string() for c in certs] == ["CN=timelog.corp.example.lu", "CN=Test Root CA"]
    assert b"BEGIN PRIVATE KEY" in (out / "server.key").read_bytes()
    with pytest.raises(SystemExit):
        certtool.pfx2pem(pfx, out, "wrong")
