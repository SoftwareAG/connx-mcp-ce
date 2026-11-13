#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""certificates.py - Test certificates of chosen lifetimes, self-signed or issued by a test CA.

Description:
    TLS tests need certificates whose validity window they choose: a 90-day certificate with 20
    days left, a 6-day one with 20 hours left, an expired one. `issue` makes one with the
    `cryptography` package (a dev dependency only; the CLI itself parses certificates with the
    standard library) and returns it as PEM and DER together with its key.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class Issued:
    """A certificate and its key, in the forms tests hand to the CLI and the fake probe.

    Attributes:
        pem (bytes): The certificate, PEM.
        key_pem (bytes): Its private key, unencrypted PKCS#8 PEM.
        der (bytes): The certificate, DER, as a server presents it.
    """

    pem: bytes
    key_pem: bytes
    der: bytes

    def write(self, directory: Path, stem: str) -> None:
        """Write `<stem>.crt` and `<stem>.key` into a directory.

        Args:
            directory (Path): The directory; created when missing.
            stem (str): The file name without its suffix.
        """
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.crt").write_bytes(self.pem)
        (directory / f"{stem}.key").write_bytes(self.key_pem)


@cache
def _key() -> ec.EllipticCurvePrivateKey:
    """One key for every test certificate: generating keys is the slow part."""
    return ec.generate_private_key(ec.SECP256R1())


def _name(common_name: str) -> x509.Name:
    """Return an X.509 name holding only a common name."""
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue(
    *,
    lifetime: timedelta,
    left: timedelta,
    host: str = "mcp.example.com",
    self_signed: bool = True,
) -> Issued:
    """Issue a certificate valid for `lifetime` of which `left` remains now.

    Args:
        lifetime (timedelta): Its whole validity window.
        left (timedelta): How much of it remains now; negative when it expired that long ago.
        host (str): Its subject common name and DNS subject alternative name.
        self_signed (bool): Issuer equals subject; else issued by "Test CA".

    Returns:
        Issued: The certificate as PEM and DER, with its key.
    """
    key = _key()
    not_after = datetime.now(UTC) + left
    not_before = not_after - lifetime
    issuer = _name(host) if self_signed else _name("Test CA")
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return Issued(
        pem=cert.public_bytes(serialization.Encoding.PEM),
        key_pem=key_pem,
        der=cert.public_bytes(serialization.Encoding.DER),
    )
