"""Generate a self-signed TLS certificate (PEM) for the connector. The Linux server pins it via
RR_BRIDGE_CA_BUNDLE. Usage: python gen_cert.py <out_dir> <hostname_or_ip> [<extra SAN> ...]"""
import datetime
import ipaddress
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

out, names = sys.argv[1], sys.argv[2:]
key = ec.generate_private_key(ec.SECP256R1())
sans = []
for n in names:
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(n)))
    except ValueError:
        sans.append(x509.DNSName(n))
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "connector.key"), "wb") as f:
    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
with open(os.path.join(out, "connector.crt"), "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))
print("written", out)
