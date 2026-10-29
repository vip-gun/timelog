"""Certificate helpers that run inside the application image, so a server needs no openssl.

    python -m timelog.certtool selfsigned <fqdn> <outdir>
        temporary self-signed certificate (server.crt + server.key) until the PKI certificate is available
    python -m timelog.certtool pfx2pem <file.pfx> <outdir> [--password-env VAR]
        split a PFX / PKCS#12 file from the PKI into server.crt (server certificate first, then the chain)
        and server.key (unencrypted PEM) as expected by nginx. The password is read from the environment
        variable or prompted for; it is never accepted on the command line.
"""
import argparse
import datetime as dt
import getpass
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _write(outdir: Path, cert_pem: bytes, key_pem: bytes) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "server.crt").write_bytes(cert_pem)
    key = outdir / "server.key"
    key.write_bytes(key_pem)
    os.chmod(key, 0o600)


def selfsigned(fqdn: str, outdir: Path, days: int = 825) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, fqdn)])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(fqdn), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    _write(outdir, cert.public_bytes(serialization.Encoding.PEM),
           key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()))


def pfx2pem(pfx: Path, outdir: Path, password: str) -> x509.Certificate:
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(pfx.read_bytes(), password.encode() or None)
    except ValueError as exc:
        raise SystemExit(f"Cannot read the PFX file (wrong password or damaged file): {exc}")
    if key is None or cert is None:
        raise SystemExit("The PFX file does not contain both a private key and a certificate.")
    pem = cert.public_bytes(serialization.Encoding.PEM) + b"".join(
        c.public_bytes(serialization.Encoding.PEM) for c in (chain or []))
    _write(outdir, pem, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="timelog.certtool", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("selfsigned")
    s.add_argument("fqdn")
    s.add_argument("outdir", type=Path)
    p = sub.add_parser("pfx2pem")
    p.add_argument("pfx", type=Path)
    p.add_argument("outdir", type=Path)
    p.add_argument("--password-env", default="")
    args = parser.parse_args(argv)
    if args.cmd == "selfsigned":
        selfsigned(args.fqdn, args.outdir)
        print(f"Self-signed certificate for {args.fqdn} written to {args.outdir} (temporary: replace it with the PKI certificate).")
        return
    password = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("PFX password: ")
    cert = pfx2pem(args.pfx, args.outdir, password)
    names = []
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    print(f"server.crt and server.key written to {args.outdir}")
    print(f"  subject: {cert.subject.rfc4514_string()}")
    print(f"  names:   {', '.join(names) or '(no SAN)'}")
    print(f"  expires: {cert.not_valid_after_utc:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    main()
