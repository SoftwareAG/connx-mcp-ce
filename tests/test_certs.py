#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_certs.py - The certificate reader turns any input into a certificate or its own error, and expiry levels.

Description:
    The reader parses untrusted bytes: `.crt` files in the administrator's directory and the
    certificate a server presents on the network. Malformed input of any shape must come out as
    CertificateError ("not a readable certificate"), never another exception, so `configure`,
    `status` and `restart --if-cert-changed` refuse or stay quiet instead of crashing. A seeded
    fuzz run over random bytes, truncations and byte flips of a real certificate checks that
    with the standard library only. The expiry tests sit at, and one second either side of, each
    threshold: a third and a tenth of the lifetime left, and the 24-hour floor.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from connx_mcp import certs
from connx_mcp.certs import Certificate, CertificateError, Level
from tests.certificates import issue

DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
PEM_EMPTY_SEQUENCE = b"-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n"


def _with_time(der: bytes, old: bytes, new: bytes) -> bytes:
    """Return a certificate's DER with one validity time's content replaced by bytes of the same length."""
    assert len(old) == len(new) and der.count(old) == 1
    return der.replace(old, new)


def _not_before_text(der: bytes) -> bytes:
    """Return the UTCTime content of a certificate's notBefore, as its DER holds it."""
    moment = certs.from_der(der).not_before
    return moment.strftime("%y%m%d%H%M%SZ").encode("ascii")


# --- malformed input becomes CertificateError -----------------------------------------------------------------


def test_an_empty_outer_sequence_is_not_a_readable_certificate() -> None:
    with pytest.raises(CertificateError):
        certs.from_der(b"\x30\x00")


def test_a_file_holding_an_empty_sequence_leaves_the_directory_refused(tmp_path: Path) -> None:
    (tmp_path / "server.crt").write_bytes(PEM_EMPTY_SEQUENCE)
    (tmp_path / "server.key").write_bytes(b"key")

    problem = certs.directory_problem(tmp_path, None, datetime.now(UTC))

    assert problem is not None and "not a readable certificate" in problem
    assert certs.on_disk(tmp_path, None) is None


def test_a_non_ascii_validity_time_is_not_a_readable_certificate() -> None:
    der = issue(lifetime=90 * DAY, left=60 * DAY).der
    old = _not_before_text(der)

    with pytest.raises(CertificateError):
        certs.from_der(_with_time(der, old, b"\xff" * len(old)))


def _tlv(tag: int, content: bytes) -> bytes:
    """Encode one DER element (short or long length form)."""
    size = len(content)
    length = bytes([size]) if size < 0x80 else bytes([0x82]) + size.to_bytes(2, "big")
    return bytes([tag]) + length + content


def _certificate_with_times(before: tuple[int, bytes], after: tuple[int, bytes]) -> bytes:
    """A minimal certificate skeleton whose validity times are given as (tag, content)."""
    name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"x"))))
    validity = _tlv(0x30, _tlv(*before) + _tlv(*after))
    tbs = _tlv(0x02, b"\x01") + _tlv(0x30, _tlv(0x06, b"\x2a")) + name + validity + name
    return _tlv(0x30, _tlv(0x30, tbs))


def test_a_hand_built_certificate_is_read() -> None:
    certificate = certs.from_der(_certificate_with_times((0x17, b"250101120000Z"), (0x18, b"20500101120000Z")))

    assert certificate.not_before == datetime(2025, 1, 1, 12, tzinfo=UTC)
    assert certificate.not_after == datetime(2050, 1, 1, 12, tzinfo=UTC)
    assert certificate.self_signed


@pytest.mark.parametrize(
    "time",
    [(0x17, b"2501011200Z"), (0x17, b"25011120000Z"), (0x18, b"205001011200Z"), (0x17, b"250101120000")],
    ids=["utc-without-seconds", "utc-single-digit-month", "generalized-without-seconds", "no-zone"],
)
def test_a_validity_time_der_forbids_is_not_a_readable_certificate(time: tuple[int, bytes]) -> None:
    with pytest.raises(CertificateError):
        certs.from_der(_certificate_with_times(time, (0x17, b"260101120000Z")))


def test_an_unreadable_certificate_file_is_a_reason_not_a_crash(tmp_path: Path) -> None:
    issue(lifetime=90 * DAY, left=60 * DAY).write(tmp_path, "server")
    (tmp_path / "server.crt").chmod(0)
    try:
        if _readable(tmp_path / "server.crt"):
            pytest.skip("running as a user that reads mode-000 files")
        problem = certs.directory_problem(tmp_path, None, datetime.now(UTC))
    finally:
        (tmp_path / "server.crt").chmod(0o600)

    assert problem is not None and problem.startswith("cannot read ")


def _readable(path: Path) -> bool:
    """Return whether this process can read a file despite its mode (root can)."""
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def test_the_reader_raises_only_its_own_error_on_any_input() -> None:
    """Fuzz: random bytes, every truncation and seeded byte flips of a real certificate."""
    der = issue(lifetime=90 * DAY, left=60 * DAY).der
    rng = random.Random(7)
    inputs = [b"", *(rng.randbytes(rng.randrange(1, 64)) for _ in range(500))]
    inputs += [der[:cut] for cut in range(len(der))]
    for _ in range(5000):
        mutated = bytearray(der)
        for _ in range(rng.randrange(1, 4)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        inputs.append(bytes(mutated))

    for data in inputs:
        try:
            certs.from_der(data)
        except CertificateError:
            pass
        except Exception as error:  # noqa: BLE001 - the point of the test is that nothing else escapes
            pytest.fail(f"from_der({data.hex()}) raised {type(error).__name__}: {error}")


# --- expiry thresholds ----------------------------------------------------------------------------------------


def _valid_for(lifetime: timedelta) -> Certificate:
    """A certificate from a fixed start, valid for a lifetime."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return Certificate(der=b"", not_before=start, not_after=start + lifetime, self_signed=False)


@pytest.mark.parametrize(
    ("lifetime", "left", "level"),
    [
        (90 * DAY, 30 * DAY + SECOND, Level.OK),
        (90 * DAY, 30 * DAY, Level.OK),
        (90 * DAY, 30 * DAY - SECOND, Level.WARNING),
        (90 * DAY, 9 * DAY + SECOND, Level.WARNING),
        (90 * DAY, 9 * DAY, Level.WARNING),
        (90 * DAY, 9 * DAY - SECOND, Level.CRITICAL),
        (6 * DAY, DAY + SECOND, Level.WARNING),
        (6 * DAY, DAY, Level.WARNING),
        (6 * DAY, DAY - SECOND, Level.CRITICAL),
        (90 * DAY, SECOND, Level.CRITICAL),
        (90 * DAY, timedelta(0), Level.EXPIRED),
        (90 * DAY, -SECOND, Level.EXPIRED),
    ],
    ids=[
        "third+1s", "third", "third-1s", "tenth+1s", "tenth", "tenth-1s",
        "24h+1s", "24h", "24h-1s", "last-second", "at-expiry", "expired",
    ],
)  # fmt: skip
def test_expiry_levels_change_exactly_at_each_threshold(lifetime: timedelta, left: timedelta, level: Level) -> None:
    certificate = _valid_for(lifetime)

    assert certs.expiry(certificate, certificate.not_after - left) is level
