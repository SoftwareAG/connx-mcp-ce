#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe.py - The endpoint probe: the server's `/health` answer and its served certificate.

Description:
    The network side of `status`, `start` and `restart --if-cert-changed` goes through the
    EndpointProbe port, so tests choose health bodies and certificates instead of waiting on a
    server. UrllibProbe is the real adapter. TLS verification is off for the server's own probes,
    because the point is to read whatever certificate it serves; a probe given a CA file (a remote
    demo stack's Vault) verifies against it.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HealthReply:
    """The `/health` answer: HTTP status and body, or why there was none.

    Attributes:
        status (int | None): The HTTP status; None without an answer.
        body (str): The body.
        error (str | None): Why there was no answer.
        certificate_refused (bool): The served certificate did not verify against the CA file given.
    """

    status: int | None
    body: str
    error: str | None = None
    certificate_refused: bool = False


class EndpointProbe(Protocol):
    """Reads the server's health and certificate over the network."""

    def health(self, url: str, timeout: float = 5.0, ca_file: str | None = None) -> HealthReply:
        """Return the reply to GET `url`; with `ca_file`, the served certificate must verify against it."""
        ...

    def served_certificate(self, host: str, port: int, timeout: float = 5.0) -> bytes | None:
        """Return the DER certificate served on host:port, or None without TLS."""
        ...

    def resolves(self, host: str) -> bool:
        """Return whether a host name resolves on this machine."""
        ...


def _unverified_context() -> ssl.SSLContext:
    """Return a TLS context that accepts any certificate, for probing only."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class UrllibProbe:
    """The real endpoint probe, using the standard library."""

    def health(self, url: str, timeout: float = 5.0, ca_file: str | None = None) -> HealthReply:
        """GET `url` and return status and body; an unreachable server gives status None.

        Args:
            url (str): The `/health` URL.
            timeout (float): Seconds to wait.
            ca_file (str | None): The CA bundle the served certificate must verify against, its name included;
                None accepts any certificate.

        Returns:
            HealthReply: The answer, or the connection error.
        """
        try:
            context = ssl.create_default_context(cafile=ca_file) if ca_file else _unverified_context()
        except (ssl.SSLError, OSError) as error:
            return HealthReply(
                status=None, body="", error=f"the CA file cannot be read: {error}", certificate_refused=True
            )
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=context) as reply:
                return HealthReply(status=reply.status, body=reply.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return HealthReply(status=error.code, body=error.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            refused = isinstance(reason, ssl.SSLCertVerificationError)
            return HealthReply(status=None, body="", error=str(error), certificate_refused=refused)

    def served_certificate(self, host: str, port: int, timeout: float = 5.0) -> bytes | None:
        """Return the DER certificate the server presents, or None when it does not speak TLS.

        Args:
            host (str): Host name or address.
            port (int): TCP port.
            timeout (float): Seconds to wait.

        Returns:
            bytes | None: The leaf certificate in DER form.
        """
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with _unverified_context().wrap_socket(raw, server_hostname=host) as tls:
                    return tls.getpeercert(binary_form=True)
        except ssl.SSLError, OSError:
            return None

    def resolves(self, host: str) -> bool:
        """Return whether a host name resolves on this machine, as a browser or harness here would look it up.

        Args:
            host (str): Host name or address.

        Returns:
            bool: True when the name resolves to an address.
        """
        try:
            return bool(socket.getaddrinfo(host, None))
        except OSError:
            return False
