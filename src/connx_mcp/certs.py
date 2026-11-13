#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""certs.py - TLS certificates: read one, judge a certificate directory, and judge how soon one expires.

Description:
    The server picks its certificate from its certificate directory at startup: an empty directory
    makes it generate a self-signed pair named after the primary `MCP_TLS_HOSTS` entry; one `.crt`
    and one `.key` are used whatever their names; with several, the pair named after the primary
    host; a lone `.crt` or `.key`, no matching pair and an expired certificate are refused.
    choose_pair applies the same rules so `configure` refuses what the server would. expiry rates
    a certificate against its own lifetime: a warning under one third left, critical under one
    tenth or 24 hours, so 45-day and 6-day certificates are covered without constant noise.
    Certificates are read with a small DER reader over the standard library: the CLI ships as a
    frozen binary and needs only the validity window, issuer and subject.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

WARN_FRACTION = 1 / 3
CRITICAL_FRACTION = 1 / 10
CRITICAL_FLOOR = timedelta(hours=24)
_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL)


class CertificateError(ValueError):
    """A file or a byte string is not a certificate this module can read."""


@dataclass(frozen=True)
class Certificate:
    """The facts of one X.509 certificate that the CLI judges.

    Attributes:
        der (bytes): The certificate in DER form; two certificates are the same when these are equal.
        not_before (datetime): Start of its validity, UTC.
        not_after (datetime): End of its validity, UTC.
        self_signed (bool): Issuer and subject are the same name, the server's own test for self-signed.
    """

    der: bytes
    not_before: datetime
    not_after: datetime
    self_signed: bool

    @property
    def fingerprint(self) -> str:
        """The SHA-256 fingerprint, hex."""
        return hashlib.sha256(self.der).hexdigest()

    @property
    def lifetime(self) -> timedelta:
        """How long it is valid in all."""
        return self.not_after - self.not_before


# --- Reading certificates -------------------------------------------------------------------------------------


def _element(data: bytes, at: int) -> tuple[int, int, int]:
    """Read one DER element header.

    Args:
        data (bytes): DER bytes.
        at (int): Offset of the element.

    Returns:
        tuple[int, int, int]: Its tag, the offset of its content and the offset after it.

    Raises:
        CertificateError: The bytes end early or use a length form DER forbids.
    """
    if at + 2 > len(data):
        raise CertificateError("the certificate ends early")
    tag, length = data[at], data[at + 1]
    start = at + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or start + count > len(data):
            raise CertificateError("the certificate has an invalid length")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    if start + length > len(data):
        raise CertificateError("the certificate ends early")
    return tag, start, start + length


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    """Return the elements inside a constructed element as (tag, header offset, content offset, end offset)."""
    found = []
    at = start
    while at < end:
        tag, content, after = _element(data, at)
        found.append((tag, at, content, after))
        at = after
    return found


_TIME_FORMATS = {0x17: (13, "%y%m%d%H%M%SZ"), 0x18: (15, "%Y%m%d%H%M%SZ")}


def _time(tag: int, raw: bytes) -> datetime:
    """Read an X.509 UTCTime (0x17) or GeneralizedTime (0x18), which DER writes in UTC with seconds."""
    if tag not in _TIME_FORMATS:
        raise CertificateError("the certificate's validity is not a time")
    size, layout = _TIME_FORMATS[tag]
    # strptime alone is laxer than DER: it takes single-digit fields and a UTCTime without seconds.
    if len(raw) != size or not raw[:-1].isdigit() or not raw.endswith(b"Z"):
        raise CertificateError(f"the certificate's validity time {raw!r} is not a DER time")
    try:
        moment = datetime.strptime(raw.decode("ascii"), layout)
    except ValueError as error:
        raise CertificateError(f"the certificate's validity time {raw!r} is unreadable") from error
    if tag == 0x17:
        # RFC 5280: two-digit years 50-99 are 19xx; strptime maps 00-68 to 20xx and 69-99 to 19xx.
        if moment.year < 1950:
            moment = moment.replace(year=moment.year + 100)
        elif moment.year >= 2050:
            moment = moment.replace(year=moment.year - 100)
    return moment.replace(tzinfo=UTC)


def from_der(der: bytes) -> Certificate:
    """Read a DER certificate.

    The bytes are untrusted (a file on disk, or what a server presents), so any malformed input of any
    shape comes out as CertificateError and nothing else.

    Args:
        der (bytes): The certificate.

    Returns:
        Certificate: Its validity window and whether it is self-signed.

    Raises:
        CertificateError: It is not an X.509 certificate this reader can read.
    """
    try:
        return _parse(der)
    except CertificateError:
        raise
    except (ValueError, IndexError, TypeError, OverflowError) as error:
        raise CertificateError(f"not an X.509 certificate ({type(error).__name__})") from error


def _parse(der: bytes) -> Certificate:
    """Read a DER certificate; from_der turns any slip on malformed input into CertificateError."""
    tag, content, end = _element(der, 0)
    if tag != 0x30:
        raise CertificateError("not an X.509 certificate")
    outer = _children(der, content, end)
    if not outer or outer[0][0] != 0x30:
        raise CertificateError("not an X.509 certificate")
    _, _, tbs_content, tbs_end = outer[0]
    fields = _children(der, tbs_content, tbs_end)
    if fields and fields[0][0] == 0xA0:  # the explicit [0] version is optional
        fields = fields[1:]
    if len(fields) < 5:
        raise CertificateError("the certificate is incomplete")
    _serial, _algorithm, issuer, validity, subject = fields[:5]
    if validity[0] != 0x30:
        raise CertificateError("the certificate has no validity window")
    times = _children(der, validity[2], validity[3])
    if len(times) != 2:
        raise CertificateError("the certificate has no validity window")
    (before_tag, _, before_at, before_end), (after_tag, _, after_at, after_end) = times
    return Certificate(
        der=der,
        not_before=_time(before_tag, der[before_at:before_end]),
        not_after=_time(after_tag, der[after_at:after_end]),
        # Byte comparison: a CA that writes the same name with another string type is not taken as self-signed.
        self_signed=der[issuer[1] : issuer[3]] == der[subject[1] : subject[3]],
    )


def from_pem(pem: bytes) -> Certificate:
    """Read the first certificate of a PEM text: a chain file starts with the server's own.

    Args:
        pem (bytes): PEM text.

    Returns:
        Certificate: The first certificate.

    Raises:
        CertificateError: There is no readable certificate in it.
    """
    match = _PEM.search(pem)
    if match is None:
        raise CertificateError("no PEM certificate in it")
    try:
        der = base64.b64decode(b"".join(match.group(1).split()), validate=True)
    except binascii.Error as error:
        raise CertificateError("its PEM certificate is not valid base64") from error
    return from_der(der)


def read(path: Path) -> Certificate:
    """Read the first certificate of a PEM file.

    Args:
        path (Path): A `.crt` file.

    Returns:
        Certificate: The first certificate.

    Raises:
        CertificateError: The file holds no readable certificate, naming the file.
    """
    try:
        return from_pem(path.read_bytes())
    except CertificateError as error:
        raise CertificateError(f"{path} is not a readable certificate: {error}") from error


# --- The certificate directory --------------------------------------------------------------------------------


def primary_host(hosts: str | None) -> str | None:
    """Return the primary host of `MCP_TLS_HOSTS`: its first entry.

    Args:
        hosts (str | None): The comma-separated hosts, or None when unset.

    Returns:
        str | None: The first non-empty entry, or None.
    """
    entries = [h.strip() for h in (hosts or "").split(",") if h.strip()]
    return entries[0] if entries else None


@dataclass(frozen=True)
class Pair:
    """The certificate and key the server will use from a directory.

    Attributes:
        certificate (Path): The `.crt` file.
        key (Path): The `.key` file.
    """

    certificate: Path
    key: Path


def _files(directory: Path, suffix: str) -> list[Path]:
    """Return the files with a suffix in a directory, by name."""
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == suffix)


def choose_pair(directory: Path, primary: str | None) -> Pair | None:
    """Choose the pair the server will use from its certificate directory, by the server's rules.

    Args:
        directory (Path): The certificate directory.
        primary (str | None): The primary host (first `MCP_TLS_HOSTS` entry), or None.

    Returns:
        Pair | None: The pair; None for a directory with neither `.crt` nor `.key`, where the server generates one.

    Raises:
        CertificateError: A lone `.crt` or `.key`, or several pairs and none named after the primary host.
    """
    crts, keys = _files(directory, ".crt"), _files(directory, ".key")
    if not crts and not keys:
        return None
    if len(crts) == 1 and len(keys) == 1:
        return Pair(crts[0], keys[0])
    if primary is not None:
        named = [p for p in crts if p.stem == primary], [p for p in keys if p.stem == primary]
        if named[0] and named[1]:
            return Pair(named[0][0], named[1][0])
    if not keys or not crts:
        lone = ", ".join(p.name for p in crts or keys)
        missing = "key (.key)" if crts else "certificate (.crt)"
        raise CertificateError(f"{directory} holds {lone} without its {missing}; the server refuses a lone file")
    found = ", ".join(p.name for p in [*crts, *keys])
    if primary is None:
        raise CertificateError(
            f"{directory} holds several certificates and keys ({found}); keep one pair, or set MCP_TLS_HOSTS "
            "so the server uses the pair named after its first host"
        )
    raise CertificateError(
        f"{directory} holds several certificates and keys ({found}) but no pair named {primary}.crt and "
        f"{primary}.key after the primary host {primary}; the server would refuse to start"
    )


def directory_problem(directory: Path, primary: str | None, now: datetime) -> str | None:
    """Return why the server would refuse a certificate directory at startup, or None.

    Args:
        directory (Path): The certificate directory.
        primary (str | None): The primary host, or None.
        now (datetime): The time to judge expiry at.

    Returns:
        str | None: The reason, or None when the server would take it (an empty directory included).
    """
    if not directory.is_dir():
        return f"no such directory: {directory}"
    try:
        pair = choose_pair(directory, primary)
        if pair is None:
            return None
        certificate = read(pair.certificate)
    except CertificateError as error:
        return str(error)
    except OSError as error:
        return f"cannot read {error.filename or directory} ({error.strerror or error})"
    if certificate.not_after <= now:
        return f"the certificate {pair.certificate} expired on {certificate.not_after:%Y-%m-%d %H:%M} UTC"
    return None


def on_disk(directory: Path, primary: str | None) -> Certificate | None:
    """Return the certificate the server will use from a directory, or None when there is none to read.

    Args:
        directory (Path): The certificate directory.
        primary (str | None): The primary host, or None.

    Returns:
        Certificate | None: The chosen pair's certificate; None for a missing, empty or invalid directory.
    """
    try:
        pair = choose_pair(directory, primary) if directory.is_dir() else None
        return read(pair.certificate) if pair is not None else None
    except CertificateError, OSError:
        return None


# --- Expiry ---------------------------------------------------------------------------------------------------


class Level(StrEnum):
    """How urgent a certificate's expiry is."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    EXPIRED = "expired"


def expiry(certificate: Certificate, now: datetime) -> Level:
    """Rate a certificate's expiry against its own lifetime.

    Args:
        certificate (Certificate): The certificate.
        now (datetime): The time to judge at.

    Returns:
        Level: Expired; critical under one tenth of its lifetime or 24 hours left; warning under one third; else ok.
    """
    left = certificate.not_after - now
    if left <= timedelta(0):
        return Level.EXPIRED
    if left < certificate.lifetime * CRITICAL_FRACTION or left < CRITICAL_FLOOR:
        return Level.CRITICAL
    if left < certificate.lifetime * WARN_FRACTION:
        return Level.WARNING
    return Level.OK


def describe_left(certificate: Certificate, now: datetime) -> str:
    """Say how long a certificate has left, in days or hours.

    Args:
        certificate (Certificate): The certificate.
        now (datetime): The time to judge at.

    Returns:
        str: E.g. `expires in 20 days`, `expires in 5 hours` or `expired 2 days ago`.
    """
    left = certificate.not_after - now
    span = abs(left)
    amount = f"{span.days} days" if span >= timedelta(days=2) else f"{int(span.total_seconds() // 3600)} hours"
    return f"expires in {amount}" if left > timedelta(0) else f"expired {amount} ago"


def to_json(certificate: Certificate, now: datetime) -> dict[str, Any]:
    """Return a certificate's facts as a JSON object.

    Args:
        certificate (Certificate): The certificate.
        now (datetime): The time to judge at.

    Returns:
        dict[str, Any]: Validity window (ISO 8601), days left, level, self-signed and fingerprint.
    """
    return {
        "not_before": certificate.not_before.isoformat(),
        "not_after": certificate.not_after.isoformat(),
        "days_left": round((certificate.not_after - now) / timedelta(days=1), 2),
        "level": str(expiry(certificate, now)),
        "self_signed": certificate.self_signed,
        "sha256": certificate.fingerprint,
    }
