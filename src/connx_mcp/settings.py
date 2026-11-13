#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.py - The settings schema: every server setting the CLI knows, its domain and its rules.

Description:
    One table (SCHEMA) describes each server setting: its group, kind, server default, whether it
    is a secret, its value domain (including whether an explicit empty value is legitimate),
    whether it is required, and the setting it depends on. Cross-field rules (RULES) check a whole
    set of values. Prompts, flags, import, export and validation all derive from this module; a
    later group extends it by adding rows to SCHEMA and functions to RULES, never by reshaping it.
    Keys not in the schema are accepted with an "unvalidated" warning; retired keys, keys fixed in
    the image and host-side keys are refused by name. Host settings are declared here too, one
    `HostSettings` field each (HOST_SCHEMA derives from them), so rules can judge both kinds.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from connx_mcp import CLI_NAME, certs
from connx_mcp.output import shell_argument

HIDDEN = "***hidden***"


class Group(StrEnum):
    """The settings groups, in the order an administrator walks them."""

    CREDENTIALS = "credentials"
    ENGINE = "engine"
    LISTENER = "listener"
    TLS = "tls"
    OAUTH = "oauth"
    VAULT = "vault"


# What the administrator reads at the head of each group; a group without a title shows its name.
GROUP_TITLES: dict[Group, str] = {
    Group.CREDENTIALS: "Credentials",
    Group.ENGINE: "CONNX engine and licensing",
    Group.LISTENER: "Listener and sessions",
    Group.TLS: "TLS (inbound HTTPS)",
    Group.OAUTH: "OAuth sign-in",
    Group.VAULT: "Credential Vault (per-caller CONNX logins)",
}


class Kind(StrEnum):
    """The shape of a value; each kind has one domain check."""

    TEXT = "text"
    INTEGER = "integer"
    PORT = "port"
    HOST = "host"
    URL_PATH = "url_path"
    ORIGINS = "origins"
    CHOICE = "choice"
    URL = "url"


@dataclass(frozen=True)
class DependsOn:
    """A setting matters only when another setting is set (to one of `values`, when given).

    Attributes:
        key (str): The setting depended on.
        values (frozenset[str] | None): The values that make the dependent setting active; None means "any value".
        spelling (Callable[[str], str] | None): Maps a stored value to its canonical spelling before matching, for
            a setting the server parses leniently; None compares the value as stored.
    """

    key: str
    values: frozenset[str] | None = None
    spelling: Callable[[str], str] | None = None

    def active(self, values: Mapping[str, str]) -> bool:
        """Return whether the dependency holds for a set of values.

        Args:
            values (Mapping[str, str]): The environment's server settings.

        Returns:
            bool: True when the depended-on setting has a value that activates the dependent one.
        """
        value = values.get(self.key, SCHEMA[self.key].default if self.key in SCHEMA else None)
        if value is None:
            return False
        if self.spelling is not None:
            value = self.spelling(value)
        return self.values is None or value in self.values

    def describe(self) -> str:
        """Return the dependency in words."""
        if self.values is None:
            return f"{self.key} is set"
        *others, last = sorted(self.values)
        return f"{self.key} is {', '.join(others)} or {last}" if others else f"{self.key} is {last}"


@dataclass(frozen=True)
class Setting:
    """One server setting.

    Attributes:
        key (str): The variable name the server reads.
        group (Group): The settings group it belongs to.
        kind (Kind): The shape of its value.
        summary (str): What it does, in one line, for prompts and help.
        default (str | None): The server's default; None when the server has none.
        secret (bool): Never printed; shown as `***hidden***`.
        required (bool): Must be set (possibly to empty, where allowed) whenever the setting is active.
        empty_ok (bool): An explicit empty value is legitimate, as distinct from unset.
        empty_meaning (str): What an explicit empty value means, where it is allowed.
        minimum (int | None): Lowest allowed value of an integer.
        maximum (int | None): Highest allowed value of an integer.
        choices (tuple[str, ...]): The values a CHOICE takes, e.g. a group switch whose `none` turns the group off;
            each dependent setting names the choices that activate it in `depends_on`.
        depends_on (DependsOn | None): The setting this one needs to have any effect.
        required_when (DependsOn | None): Required only while this holds (and the setting is active), for a
            setting active in several modes but needed in fewer; `required` stays False for such a row.
        check (Callable[[str], str | None] | None): An extra domain check returning a reason, or None.
        asked (bool): Asked in the interactive walk; False for a value connx-mcp-ce settles itself (set by flag only).
    """

    key: str
    group: Group
    kind: Kind
    summary: str
    default: str | None = None
    secret: bool = False
    required: bool = False
    empty_ok: bool = False
    empty_meaning: str = ""
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    depends_on: DependsOn | None = None
    required_when: DependsOn | None = None
    check: Callable[[str], str | None] | None = field(default=None, compare=False)
    asked: bool = True

    def active(self, values: Mapping[str, str]) -> bool:
        """Return whether this setting has any effect given the other values.

        Args:
            values (Mapping[str, str]): The environment's server settings.

        Returns:
            bool: True when it has no dependency or its dependency holds.
        """
        return self.depends_on is None or self.depends_on.active(values)

    def is_required(self, values: Mapping[str, str]) -> bool:
        """Return whether this setting must have a value given the other values.

        Args:
            values (Mapping[str, str]): The environment's server settings.

        Returns:
            bool: True when it is active and required always, or required in the current mode.
        """
        when = self.required or (self.required_when is not None and self.required_when.active(values))
        return when and self.active(values)

    def problem(self, value: str) -> str | None:
        """Check a value against this setting's domain.

        Args:
            value (str): The value to set; "" is an explicit empty value.

        Returns:
            str | None: Why the value is refused, as a sentence that names neither the key nor the value (the
                message's origin names the key); None when it is valid.
        """
        if value == "":
            if self.empty_ok:
                return None
            fallback = f"the server default {self.default}" if self.default is not None else "no value"
            return f"it cannot be empty; give a value, or --unset it to return to {fallback}."
        reason = _KIND_CHECKS[self.kind](self, value)
        if reason is None and self.check is not None:
            reason = self.check(value)
        return f"{reason}." if reason else None


def _check_text(setting: Setting, value: str) -> str | None:
    """Accept any text."""
    return None


def _check_range(value: str, low: int | None, high: int | None) -> str | None:
    """Accept a whole number from `low` to `high`; None leaves that end open.

    Args:
        value (str): The value given.
        low (int | None): The lowest allowed number.
        high (int | None): The highest allowed number.

    Returns:
        str | None: Why the value is refused, or None.
    """
    if not re.fullmatch(r"-?[0-9]+", value):
        return "it must be a whole number"
    number = int(value)
    if low is not None and number < low:
        return f"it must be at least {low}"
    if high is not None and number > high:
        return f"it must be at most {high}"
    return None


def _check_integer(setting: Setting, value: str) -> str | None:
    """Accept a whole number within the setting's bounds."""
    return _check_range(value, setting.minimum, setting.maximum)


def _check_port(setting: Setting, value: str) -> str | None:
    """Accept a TCP port number."""
    return _check_range(value, 1, 65535)


def _check_host(setting: Setting, value: str) -> str | None:
    """Accept a host name or IP address, without scheme, port or path."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+|\[?[0-9A-Fa-f:.]+\]?", value) is None:
        return "it must be a host name or IP address, without scheme, port or path"
    return None


def _check_url_path(setting: Setting, value: str) -> str | None:
    """Accept a URL path."""
    if not value.startswith("/") or any(c.isspace() for c in value) or "?" in value or "#" in value:
        return "it must be a URL path that starts with /, without spaces, query or fragment"
    return None


def _check_origins(setting: Setting, value: str) -> str | None:
    """Accept comma-separated origins: scheme, host and optional port, nothing else."""
    for origin in (part.strip() for part in value.split(",")):
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment:
            return (
                f"{origin!r} is not an origin; give scheme://host[:port], comma-separated, e.g. https://a.example.com"
            )
    return None


def _check_choice(setting: Setting, value: str) -> str | None:
    """Accept one of the setting's choices."""
    return None if value in setting.choices else f"it must be one of {', '.join(setting.choices)}"


def _check_url(setting: Setting, value: str) -> str | None:
    """Accept an absolute http:// or https:// URL with a host."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in value):
        return "it must be an absolute URL that starts with http:// or https://, without spaces"
    return None


_KIND_CHECKS: dict[Kind, Callable[[Setting, str], str | None]] = {
    Kind.TEXT: _check_text,
    Kind.INTEGER: _check_integer,
    Kind.PORT: _check_port,
    Kind.HOST: _check_host,
    Kind.URL_PATH: _check_url_path,
    Kind.ORIGINS: _check_origins,
    Kind.CHOICE: _check_choice,
    Kind.URL: _check_url,
}

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _https_unless_loopback(value: str) -> str | None:
    """Refuse plain http except on this machine's loopback, as the server does."""
    parts = urlsplit(value)
    if parts.scheme != "https" and parts.hostname not in LOOPBACK_HOSTS:
        return "the server needs https here unless the host is localhost, 127.0.0.1 or ::1"
    return None


def _base_url(value: str) -> str | None:
    """Accept the server's public base URL: https (or loopback http) and no path."""
    if urlsplit(value).path not in ("", "/") or urlsplit(value).query or urlsplit(value).fragment:
        return "give the base URL with no path, e.g. https://mcp.example.com; the server adds MCP_PATH"
    return _https_unless_loopback(value)


def _discovery_url(value: str) -> str | None:
    """Accept an OpenID discovery document URL."""
    if not urlsplit(value).path.endswith("/.well-known/openid-configuration"):
        return "it must be the discovery URL, ending in /.well-known/openid-configuration"
    return None


def _in_container_path(value: str) -> str | None:
    """Accept an absolute path inside the container."""
    return None if value.startswith("/") else "it must be an absolute path inside the server's container"


def _odbc_credential(value: str) -> str | None:
    """Refuse what the ODBC driver cannot take literally in a credential."""
    if ";" in value:
        return "the CONNX driver cannot take a ; in a credential"
    if value.startswith("{") and value.endswith("}"):
        return "the CONNX driver takes credentials literally; do not wrap them in {}"
    return None


# Where the server keeps OAuth sign-in state in proxy mode; connx-mcp-ce mounts the environment's volume there.
OAUTH_STATE_DIR = "/opt/softwareag/connx/mcp/oauth-state"


def _oauth_state_dir(value: str) -> str | None:
    """Accept only the path connx-mcp-ce mounts the OAuth state volume at."""
    if value.rstrip("/") == OAUTH_STATE_DIR:
        return None
    return (
        f"{CLI_NAME} mounts the environment's OAuth state volume at {OAUTH_STATE_DIR}; state kept anywhere else is "
        "lost when the container is recreated, and every user is signed out. Leave it unset"
    )


OAUTH_PROVIDERS = ("keycloak", "azure", "google", "aws", "generic-oidc")
_OAUTH_ON = DependsOn("MCP_OAUTH_PROVIDER", frozenset(OAUTH_PROVIDERS))
_PROXY = DependsOn("MCP_OAUTH_PROVIDER", frozenset({"azure", "google", "aws", "generic-oidc"}))
_VAULT_ON = DependsOn("MCP_CREDENTIAL_PROVIDER", frozenset({"hashicorp"}))
# The server parses its bool settings with pydantic, which takes these spellings of "true", case-insensitively.
_TRUE = frozenset({"true", "1", "yes", "on"})


def _server_bool(value: str) -> str:
    """Return `true` for every spelling the server reads as true, else the value as stored."""
    return "true" if value.strip().lower() in _TRUE else value


_TLS_ON = DependsOn("MCP_TLS_ENABLED", frozenset({"true"}), spelling=_server_bool)

SCHEMA: dict[str, Setting] = {
    s.key: s
    for s in (
        # Credentials
        Setting(
            "CDD_USERNAME",
            Group.CREDENTIALS,
            Kind.TEXT,
            "CONNX login that the server uses",
            required=True,
            check=_odbc_credential,
        ),
        Setting(
            "CDD_PASSWORD",
            Group.CREDENTIALS,
            Kind.TEXT,
            "Password of the CONNX login",
            secret=True,
            required=True,
            empty_ok=True,
            empty_meaning="the CONNX login has no password",
            check=_odbc_credential,
        ),
        # CONNX engine and licensing
        Setting(
            "CONNX_LICENSE_HOST",
            Group.ENGINE,
            Kind.HOST,
            "Host name of the CONNX licence server; host.docker.internal when the licence server runs on this machine",
        ),
        Setting(
            "CONNX_LICENSE_PORT",
            Group.ENGINE,
            Kind.PORT,
            "Port of the CONNX licence server",
            default="7501",
            depends_on=DependsOn("CONNX_LICENSE_HOST"),
        ),
        Setting(
            "CONNX_HOST",
            Group.ENGINE,
            Kind.HOST,
            "CONNX host that replaces the host in the CDD",
        ),
        # Listener and sessions
        Setting("MCP_PATH", Group.LISTENER, Kind.URL_PATH, "URL path the MCP endpoint is served on", default="/mcp"),
        Setting(
            "MCP_SESSION_TIMEOUT_SECONDS",
            Group.LISTENER,
            Kind.INTEGER,
            "Seconds an idle MCP session is kept",
            default="300",
            minimum=60,
        ),
        Setting(
            "MCP_SESSION_CLEANUP_INTERVAL_SECONDS",
            Group.LISTENER,
            Kind.INTEGER,
            "Seconds between two checks for idle sessions",
            default="60",
            minimum=1,
        ),
        Setting(
            "MCP_TRUSTED_ORIGINS",
            Group.LISTENER,
            Kind.ORIGINS,
            "Comma-separated browser origins allowed to call the server",
            default="",
            empty_ok=True,
            empty_meaning="every browser origin except the server's own is blocked",
        ),
        Setting(
            "MCP_STARTUP_WAIT_SECONDS",
            Group.LISTENER,
            Kind.INTEGER,
            "Seconds to wait for each dependency at startup; 0 refuses at once",
            default="120",
            minimum=0,
        ),
        # TLS (inbound HTTPS)
        Setting(
            "MCP_TLS_ENABLED",
            Group.TLS,
            Kind.CHOICE,
            "Serve HTTPS with the certificate in the certificate directory (--certs) or a generated one",
            default="false",
            choices=("false", "true"),
        ),
        Setting(
            "MCP_TLS_HOSTS",
            Group.TLS,
            Kind.TEXT,
            "Comma-separated host names and IP addresses that harnesses use; a generated certificate is for the first "
            "host, and with several pairs the server uses the pair named after it",
            depends_on=_TLS_ON,
        ),
        Setting(
            "MCP_TLS_KEY_TYPE",
            Group.TLS,
            Kind.CHOICE,
            "Key type of a generated certificate",
            default="rsa2048",
            choices=("rsa2048", "rsa3072", "rsa4096", "ec-p256", "ec-p384"),
            depends_on=_TLS_ON,
        ),
        Setting(
            "MCP_TLS_CERT_VALIDITY_DAYS",
            Group.TLS,
            Kind.INTEGER,
            "Days a generated certificate is valid; renew it by purging the certificate volume",
            default="365",
            minimum=1,
            maximum=365,
            depends_on=_TLS_ON,
        ),
        Setting(
            "MCP_TLS_KEY_PASSWORD",
            Group.TLS,
            Kind.TEXT,
            "Password of the private key; leave it unset when the key has no password",
            secret=True,
            depends_on=_TLS_ON,
        ),
        # OAuth sign-in: keycloak is resource-server mode, the others proxy mode.
        Setting(
            "MCP_OAUTH_PROVIDER",
            Group.OAUTH,
            Kind.CHOICE,
            "Identity provider callers sign in with; none serves without sign-in",
            default="none",
            choices=("none", *OAUTH_PROVIDERS),
        ),
        Setting(
            "MCP_PUBLIC_BASE_URL",
            Group.OAUTH,
            Kind.URL,
            "URL of the server for harnesses, without a path; it must start with https://, unless its host is "
            "localhost, 127.0.0.1 or ::1",
            required=True,
            depends_on=_OAUTH_ON,
            check=_base_url,
        ),
        Setting(
            "MCP_OAUTH_REALM_URL",
            Group.OAUTH,
            Kind.URL,
            "Keycloak realm URL, https://<keycloak-host>/realms/<realm>; it must equal the issuer in the tokens",
            required=True,
            depends_on=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"keycloak"})),
        ),
        Setting(
            "MCP_OAUTH_TENANT_ID",
            Group.OAUTH,
            Kind.TEXT,
            "Microsoft Entra directory (tenant) ID",
            required=True,
            depends_on=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"azure"})),
        ),
        Setting(
            "MCP_OAUTH_CONFIG_URL",
            Group.OAUTH,
            Kind.URL,
            "Discovery URL of the provider, which ends in /.well-known/openid-configuration; for AWS, the user pool's, "
            "not the login domain's",
            required=True,
            depends_on=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"aws", "generic-oidc"})),
            check=_discovery_url,
        ),
        Setting(
            "MCP_OAUTH_CLIENT_ID",
            Group.OAUTH,
            Kind.TEXT,
            "Client ID that your identity provider gives the server",
            required=True,
            depends_on=_PROXY,
        ),
        Setting(
            "MCP_OAUTH_CLIENT_SECRET",
            Group.OAUTH,
            Kind.TEXT,
            "Client secret that your identity provider gives the server; a new value signs out all callers",
            secret=True,
            required=True,
            depends_on=_PROXY,
        ),
        Setting(
            "MCP_OAUTH_AUDIENCE",
            Group.OAUTH,
            Kind.TEXT,
            "Audience that every token must have; for Keycloak, MCP_PUBLIC_BASE_URL followed by MCP_PATH",
            depends_on=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"keycloak", "azure", "google", "generic-oidc"})),
            required_when=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"keycloak"})),
        ),
        Setting(
            "MCP_OAUTH_REQUIRED_SCOPES",
            Group.OAUTH,
            Kind.TEXT,
            "Scopes that every token must have, separated by spaces or commas; for Azure, the exposed scope without "
            "its prefix",
            empty_ok=True,
            empty_meaning="no scope is required",
            depends_on=_OAUTH_ON,
            required_when=DependsOn("MCP_OAUTH_PROVIDER", frozenset({"azure"})),
        ),
        Setting(
            "MCP_OAUTH_STORAGE_DIR",
            Group.OAUTH,
            Kind.TEXT,
            f"Where the server keeps sign-in state; {CLI_NAME} mounts a volume there, so leave it unset",
            default=OAUTH_STATE_DIR,
            depends_on=_PROXY,
            check=_oauth_state_dir,
            asked=False,
        ),
        Setting(
            "MCP_OAUTH_ENABLE_CIMD",
            Group.OAUTH,
            Kind.CHOICE,
            "Accept client ID metadata documents; false when the server cannot reach the URL a harness gives as its ID",
            default="true",
            choices=("false", "true"),
            depends_on=_PROXY,
        ),
        Setting(
            "MCP_OAUTH_CLIENT_IDLE_DAYS",
            Group.OAUTH,
            Kind.INTEGER,
            "Unused days after which a harness registration is removed; 0 keeps it for ever",
            default="90",
            minimum=0,
            depends_on=_PROXY,
        ),
        # Credential Vault
        Setting(
            "MCP_CREDENTIAL_PROVIDER",
            Group.VAULT,
            Kind.CHOICE,
            "Where per-caller CONNX logins come from; hashicorp needs OAuth",
            default="none",
            choices=("none", "hashicorp"),
        ),
        Setting(
            "VAULT_ADDR",
            Group.VAULT,
            Kind.URL,
            "HashiCorp Vault URL; it must start with https://, unless its host is localhost, 127.0.0.1 or ::1",
            required=True,
            depends_on=_VAULT_ON,
            check=_https_unless_loopback,
        ),
        Setting(
            "VAULT_TOKEN",
            Group.VAULT,
            Kind.TEXT,
            "Periodic Vault token that can only read the caller entries",
            secret=True,
            required=True,
            depends_on=_VAULT_ON,
        ),
        Setting(
            "VAULT_CACERT",
            Group.VAULT,
            Kind.TEXT,
            "CA bundle inside the container; give the file on this machine with --vault-ca instead",
            depends_on=_VAULT_ON,
            check=_in_container_path,
        ),
        Setting("VAULT_NAMESPACE", Group.VAULT, Kind.TEXT, "Vault Enterprise namespace", depends_on=_VAULT_ON),
        Setting(
            "MCP_CREDENTIAL_VAULT_MOUNT",
            Group.VAULT,
            Kind.TEXT,
            "KV version 2 mount holding the logins",
            default="secret",
            depends_on=_VAULT_ON,
        ),
        Setting(
            "MCP_CREDENTIAL_VAULT_PATH",
            Group.VAULT,
            Kind.TEXT,
            "Path in the mount with one entry per caller",
            default="connx-mcp/callers",
            depends_on=_VAULT_ON,
        ),
    )
}

RETIRED_KEYS = frozenset({"CONNX_BASE_URL", "CONNX_RELATIVE_CDD_PATH_AND_NAME", "REQUESTS_VERIFY_SSL"})
FIXED_KEYS = frozenset(
    {
        "MCP_PORT",
        "MCP_HOST",
        "MCP_SERVER_EDITION",
        "PATH",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "LD_LIBRARY_PATH",
        "CONNXREGISTRY64",
        "UV_PYTHON_PREFERENCE",
    }
)
# The server starts only once its EULA is accepted. The compose file sets the variable on every start; in the
# server settings file the server refuses to start, so it is never a server setting.
EULA_KEY = "ACCEPT_EULA"
EULA_ACCEPTED = "y"
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Every character `str.splitlines` breaks a line on, and NUL: a value holding one could end its line in
# the server settings file and start another setting.
LINE_BREAK = re.compile("[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\x00]")
_SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|PASSPHRASE|CREDENTIAL|_KEY$", re.IGNORECASE)


# --- Host settings ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostSetting:
    """One host setting: read only by connx-mcp-ce and compose, never handed to the container.

    Attributes:
        summary (str): What it is, in one line, for prompts and help.
        flag (str | None): The `configure` flag that sets it; None while only connx-mcp-ce sets it.
        compose_var (str | None): The variable of the shipped compose file it replaces; refused in server settings.
        required (bool): Must be set before the environment can be saved; a required setting has a flag.
        parse (Callable[[str], Any]): Turns what was given into the stored value; raises ValueError with the reason.
        set_by (str | None): How it is set when no flag sets it, as the clause a refusal of it ends with.
        name (str): Its key in `host.json` and attribute of `HostSettings`; filled in from the field.
    """

    summary: str
    flag: str | None = None
    compose_var: str | None = None
    required: bool = False
    parse: Callable[[str], Any] = field(default=str, compare=False)
    set_by: str | None = None
    name: str = ""

    @property
    def how_set(self) -> str:
        """How the administrator sets it, as the clause a refusal of it ends with: its flag, or what sets it."""
        return f"set it with {self.flag}" if self.flag else self.set_by or f"{CLI_NAME} sets it"


def _host(
    summary: str,
    *,
    flag: str | None = None,
    compose_var: str | None = None,
    required: bool = False,
    parse: Callable[[str], Any] = str,
    set_by: str | None = None,
) -> Any:
    """Declare a host setting as a `HostSettings` field: the one place a host setting is wired.

    Args:
        summary (str): What it is, in one line.
        flag (str | None): The `configure` flag that sets it; None while only connx-mcp-ce sets it.
        compose_var (str | None): The compose variable it replaces.
        required (bool): Must be set before the environment can be saved.
        parse (Callable[[str], Any]): Turns what was given into the stored value; raises ValueError.
        set_by (str | None): How it is set when no flag sets it, as the clause a refusal of it ends with.

    Returns:
        Any: A dataclass field defaulting to None that carries the HostSetting row.
    """
    row = HostSetting(summary, flag, compose_var, required, parse, set_by)
    return field(default=None, metadata={"host": row})


def _port_number(raw: str) -> int:
    """Return a TCP port number.

    Args:
        raw (str): The port given.

    Returns:
        int: The port.

    Raises:
        ValueError: It is not a whole number from 1 to 65535.
    """
    if reason := _check_range(raw, 1, 65535):
        raise ValueError(reason)
    return int(raw)


def _existing_file(raw: str) -> str:
    """Return the absolute path of an existing file.

    Args:
        raw (str): The path given, possibly relative or with `~`.

    Returns:
        str: The absolute path.

    Raises:
        ValueError: There is no such file.
    """
    path = Path(raw).expanduser().absolute()
    if not path.is_file():
        raise ValueError("no such file")
    return str(path)


def _existing_file_or_none(raw: str) -> str | None:
    """Return the absolute path of an existing file, or None for "" (which removes the setting).

    Args:
        raw (str): The path given; "" clears it.

    Returns:
        str | None: The absolute path, or None.
    """
    return _existing_file(raw) if raw else None


# The one listen address besides every network interface: the server is published on this machine only.
LISTEN_LOCALHOST = "localhost"


def _listen_address(raw: str) -> str | None:
    """Return where the server port is published: `localhost`, or None for every network interface.

    Args:
        raw (str): `localhost`, or `all` (or "") for every network interface.

    Returns:
        str | None: `localhost`, or None (the setting is removed).

    Raises:
        ValueError: It is neither.
    """
    if raw.lower() == LISTEN_LOCALHOST:
        return LISTEN_LOCALHOST
    if raw.lower() in {"all", ""}:
        return None
    raise ValueError("give localhost (this machine only) or all (every network interface)")


def _accepted(raw: str) -> bool | None:
    """Return True for an accepted risk, or None when it is no longer accepted (the setting is removed).

    Args:
        raw (str): `true` or `false`.

    Returns:
        bool | None: True, or None.

    Raises:
        ValueError: It is neither.
    """
    if raw.lower() == "true":
        return True
    if raw.lower() in {"false", ""}:
        return None
    raise ValueError("give true or false")


def _existing_directory(raw: str) -> str:
    """Return the absolute path of an existing directory.

    Args:
        raw (str): The path given, possibly relative or with `~`.

    Returns:
        str: The absolute path.

    Raises:
        ValueError: There is no such directory.
    """
    path = Path(raw).expanduser().absolute()
    if not path.is_dir():
        raise ValueError("no such directory")
    return str(path)


_IMAGE_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_REPOSITORY = rf"(?:[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?/)?{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
_SHA256 = r"sha256:[0-9a-f]{64}"
_IMAGE_TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
IMAGE_TAG = re.compile(_IMAGE_TAG)
# A registry host: a dotted name, `localhost`, or any name with a port. It starts with a letter or digit, so it
# never reaches `docker login` or `docker pull` as an option.
_REGISTRY_HOST = r"(?:[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+(?::[0-9]+)?|localhost(?::[0-9]+)?|[A-Za-z0-9][A-Za-z0-9-]*:[0-9]+)"
_IMAGE_PATH = rf"{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*(?::{_IMAGE_TAG})?(?:@{_SHA256})?"
# A full reference names its registry first: no reference silently means Docker Hub (`docker.io/...` does).
IMAGE_REFERENCE = re.compile(rf"{_REGISTRY_HOST}/{_IMAGE_PATH}")
# A reference without its registry, as stored before references had to name one; refused with a hint.
BARE_IMAGE_REFERENCE = re.compile(_IMAGE_PATH)
# A registry host, then optional namespace path parts.
IMAGE_REGISTRY = re.compile(rf"{_REGISTRY_HOST}(?:/{_IMAGE_COMPONENT})*")
# How to write a reference that names no registry; the configure refusal and the stored-reference refusal use it.
REGISTRY_FIRST = (
    "give the registry first, such as registry.example:5000/<name>:<tag>, or docker.io/<name>:<tag> for Docker Hub"
)
# The one host test: a reference's first path part that matches it is its registry.
IMAGE_HOST = re.compile(_REGISTRY_HOST)
# A repository name without a registry: lowercase path parts, the first not a host (that belongs in the registry).
IMAGE_NAME = re.compile(rf"(?!{_REGISTRY_HOST}/){_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*")
IMAGE_DIGEST = re.compile(rf"(?:{_IMAGE_REPOSITORY}@)?{_SHA256}")
IMAGE_VERSION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}")


def _image_reference(raw: str) -> str:
    """Return an image reference such as `registry:5000/name:tag`.

    Args:
        raw (str): The value given.

    Returns:
        str: The value, unchanged.

    Raises:
        ValueError: It is not an image reference.
    """
    if IMAGE_REFERENCE.fullmatch(raw):
        return raw
    if BARE_IMAGE_REFERENCE.fullmatch(raw):
        raise ValueError(f"names no registry; {REGISTRY_FIRST}")
    raise ValueError("not an image reference such as registry.example:5000/name:tag")


def _image_reference_or_none(raw: str) -> str | None:
    """Return an image reference, or None for "" (which removes the setting, so the default applies).

    Args:
        raw (str): The value given; "" clears it.

    Returns:
        str | None: The reference, or None.
    """
    return _image_reference(raw) if raw else None


def _image_digest(raw: str) -> str:
    """Return a pinned image: `repository@sha256:<64 hex>`, or `sha256:<64 hex>` for a locally built image.

    Args:
        raw (str): The value given.

    Returns:
        str: The value, unchanged.

    Raises:
        ValueError: It is neither form.
    """
    if not IMAGE_DIGEST.fullmatch(raw):
        raise ValueError("not a pinned image such as repository@sha256:<64 hex digits>")
    return raw


def _image_version(raw: str) -> str:
    """Return an image version label such as `15.0.3.26012`.

    Args:
        raw (str): The value given.

    Returns:
        str: The value, unchanged.

    Raises:
        ValueError: It is empty, too long or holds a space or other separator.
    """
    if not IMAGE_VERSION.fullmatch(raw):
        raise ValueError("not a version label such as 15.0.3.26012")
    return raw


# The demo stack's services and the ports upstream publishes them on; a remote stack may use others.
DEMO_STACK_PORTS: dict[str, int] = {"keycloak": 8080, "dex": 5556, "vault": 8200}
HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")


def _stack_host_or_none(raw: str) -> str | None:
    """Return the remote demo stack's host name, or None for "" (which ends the remote binding).

    Args:
        raw (str): The name given; "" clears it.

    Returns:
        str | None: The name, unchanged, or None.

    Raises:
        ValueError: It is not a host name or address, or it is loopback, which inside the server's container is
            the container itself.
    """
    if not raw:
        return None
    if not HOST_NAME.fullmatch(raw):
        raise ValueError("not a host name or address")
    if raw.lower() in LOOPBACK_HOSTS:
        raise ValueError(
            "inside the server's container a loopback name is the container itself; give the stack machine's name "
            "as this machine and the server's container reach it"
        )
    return raw


def _parse_stack_ports(raw: str) -> dict[str, int]:
    """Return the ports named in `service=port[,service=port...]`.

    Args:
        raw (str): The ports given.

    Returns:
        dict[str, int]: Port by named service.

    Raises:
        ValueError: An item is not `service=port`, names a service the stack does not run, or a bad port.
    """
    ports: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        service, equals, port = item.partition("=")
        if not equals or service.lower() not in DEMO_STACK_PORTS:
            raise ValueError(f"{item!r} is not service=port with service one of {', '.join(DEMO_STACK_PORTS)}")
        ports[service.lower()] = _port_number(port)
    return ports


def stack_ports(stored: str | None) -> dict[str, int]:
    """Return the remote demo stack's port per service: the stored ones over upstream's defaults.

    Args:
        stored (str | None): The `demo_stack_ports` host setting (`keycloak=8080,dex=5556,vault=8200`).

    Returns:
        dict[str, int]: Port by service, every service present.

    A stored value that does not parse ends in the ValueError of `_parse_stack_ports`.
    """
    return {**DEMO_STACK_PORTS, **(_parse_stack_ports(stored) if stored else {})}


def merge_stack_ports(stored: str | None, given: str) -> str | None:
    """Return the ports `--stack-port` gives over the stored ones: a service it does not name keeps its port.

    Args:
        stored (str | None): The `demo_stack_ports` host setting before this call.
        given (str): `service=port[,service=port...]` as given; "" for upstream's defaults.

    Returns:
        str | None: `keycloak=<port>,dex=<port>,vault=<port>`, or None for "".

    A value that does not parse ends in the ValueError of `_parse_stack_ports`.
    """
    if not given:
        return None
    merged = {**stack_ports(stored), **_parse_stack_ports(given)}
    return ",".join(f"{service}={port}" for service, port in merged.items())


def _stack_ports_or_none(raw: str) -> str | None:
    """Return the remote demo stack's ports with every service named, or None for "" (upstream's defaults).

    Args:
        raw (str): `service=port[,service=port...]`; an unnamed service keeps upstream's default port.

    Returns:
        str | None: `keycloak=<port>,dex=<port>,vault=<port>`, or None.
    """
    if not raw:
        return None
    return ",".join(f"{service}={port}" for service, port in stack_ports(raw).items())


@dataclass
class HostSettings:
    """Values only connx-mcp-ce and compose read; never handed to the container.

    Add a host setting by adding one field declared with `_host(...)`: the flag, the refusal of its compose
    variable in server settings, the missing-value check and `host.json` all follow from it.

    Attributes:
        cdd_file (str | None): Absolute path of the CDD on this machine, mounted read-only.
        published_port (int | None): The host port the server is published on; 8000 when unset.
        listen_address (str | None): `localhost` to publish the port on this machine only; every network
            interface when unset.
        open_network_accepted (bool | None): True when the administrator accepts that the network reaches the
            server without OAuth sign-in, so the warning is not repeated; None when not accepted.
        image (str | None): The image reference to pin at first start; the CLI default when unset.
        image_digest (str | None): The pinned image (`repo@sha256:...`), recorded at first start.
        image_version (str | None): The pinned image's version label, recorded with the digest.
        image_pinned_reference (str | None): The reference the pinned image was resolved from, recorded with the
            digest; None for an environment pinned before connx-mcp-ce recorded it.
        start_timeout (int | None): Seconds `start` waits for healthy; remembered from `start --timeout`.
        vault_ca_file (str | None): Absolute path of the Vault CA bundle on this machine; `start` mounts it
            read-only and points VAULT_CACERT at it.
        demo_stack_host (str | None): The demo stack machine's name, when the environment is bound to a demo
            stack that runs on another machine; None for no stack or the local one.
        demo_stack_ports (str | None): That stack's ports (`keycloak=8080,dex=5556,vault=8200`); None for
            upstream's defaults.
        cert_dir (str | None): Absolute path of the certificate directory; the `connx-mcp-ce-<name>-certs` volume
            when unset.
        ask_at_start (list[str] | None): Secrets never stored: `start` asks for them (`configure --ask-at-start`).
        unknown (dict[str, Any]): Keys this version does not know (a newer connx-mcp-ce or an import wrote them),
            written back unchanged.
    """

    cdd_file: str | None = _host(
        "The CDD file on this machine, mounted read-only",
        flag="--cdd",
        compose_var="MCP_CDD_FILE",
        required=True,
        parse=_existing_file,
    )
    published_port: int | None = _host(
        "The host port the server is published on", flag="--port", compose_var="MCP_PUBLISHED_PORT", parse=_port_number
    )
    listen_address: str | None = _host(
        "Where the server port is published: localhost for this machine only; every network interface when unset",
        flag="--listen",
        parse=_listen_address,
    )
    open_network_accepted: bool | None = _host(
        "The administrator accepts that the network reaches the server without OAuth sign-in",
        flag="--accept-open-network",
        parse=_accepted,
    )
    image: str | None = _host(
        "The image reference to pin at first start or update, starting with its registry (docker.io/... for Docker Hub)",
        flag="--image",
        compose_var="MCP_IMAGE",
        parse=_image_reference_or_none,
    )
    image_digest: str | None = _host(
        "The pinned image, recorded at first start",
        parse=_image_digest,
        set_by="start and update record it when they pin the image",
    )
    image_version: str | None = _host(
        "The pinned image's version label",
        parse=_image_version,
        set_by="start and update record it with the pinned image",
    )
    image_pinned_reference: str | None = _host(
        "The image reference the pinned image was resolved from",
        parse=_image_reference,
        set_by="start and update record it with the pinned image",
    )
    start_timeout: int | None = _host("Seconds start waits for healthy", parse=int, set_by="start --timeout records it")
    vault_ca_file: str | None = _host(
        "The Vault CA file on this machine, mounted read-only where VAULT_CACERT points",
        flag="--vault-ca",
        parse=_existing_file_or_none,
    )
    demo_stack_host: str | None = _host(
        "The demo stack machine's name as this machine and the server's container reach it (a remote stack)",
        flag="--stack-host",
        parse=_stack_host_or_none,
    )
    demo_stack_ports: str | None = _host(
        "The remote demo stack's ports, when not upstream's 8080, 5556 and 8200",
        flag="--stack-port",
        parse=_stack_ports_or_none,
    )
    cert_dir: str | None = _host(
        "The certificate directory mounted at the server's certificate path; a named volume when unset",
        flag="--certs",
        compose_var="MCP_CERT_SOURCE",
        parse=_existing_directory,
    )
    ask_at_start: list[str] | None = _host(
        "Secrets `start` asks for instead of storing them", set_by="mark a secret with --ask-at-start KEY"
    )
    unknown: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        """Return the set host settings as a JSON object, unknown keys included."""
        known = {name: getattr(self, name) for name in HOST_SCHEMA}
        return {**self.unknown, **{k: v for k, v in known.items() if v is not None}}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HostSettings:
        """Build host settings from a JSON object, keeping keys this version does not know.

        Args:
            data (dict[str, Any]): The parsed `host.json`.

        Returns:
            HostSettings: The host settings.
        """
        return cls(
            **{k: v for k, v in data.items() if k in HOST_SCHEMA},
            unknown={k: v for k, v in data.items() if k not in HOST_SCHEMA},
        )


# Every host setting, in declaration order, keyed by its name.
HOST_SCHEMA: dict[str, HostSetting] = {
    f.name: dataclasses.replace(f.metadata["host"], name=f.name)
    for f in dataclasses.fields(HostSettings)
    if "host" in f.metadata
}
# Host settings neither exported nor imported as `# host` lines: the ask-at-start marks travel as `# secret` lines.
NOT_TRANSFERRED_HOST_SETTINGS = frozenset({"ask_at_start"})
# Compose variables connx-mcp-ce fills in itself until a host setting declares one with its flag.
_MANAGED_COMPOSE_VARS = ("MCP_ENV_FILE", "MCP_OAUTH_STATE_SOURCE")
# Host-side variables of the shipped compose file and how each is set instead, as the clause a refusal ends with.
HOST_KEYS: dict[str, str] = {
    **dict.fromkeys(_MANAGED_COMPOSE_VARS, f"{CLI_NAME} fills it in from the environment"),
    **{row.compose_var: row.how_set for row in HOST_SCHEMA.values() if row.compose_var},
}


@dataclass(frozen=True)
class Missing:
    """A required value the environment lacks.

    Attributes:
        name (str): The server setting key, or the host setting name.
        label (str): How a person knows it: the key, or the host setting's flag.
        hint (str): The flag that supplies it, e.g. `--set CDD_PASSWORD=...` or `--cdd ...`.
        host (bool): True for a host setting.
    """

    name: str
    label: str
    hint: str
    host: bool = False


@dataclass(frozen=True)
class Problem:
    """A finding about a set of values.

    Attributes:
        key (str): The setting it is about.
        message (str): What is wrong and what to do.
        error (bool): True refuses the values; False only warns.
    """

    key: str
    message: str
    error: bool = True


@dataclass(frozen=True)
class Facts:
    """Everything a cross-field rule judges.

    A new fact is a field with a default (as the certificate facts are), so existing
    rules keep working.

    Attributes:
        server (Mapping[str, str]): The server settings.
        host (HostSettings): The host settings.
        serves_self_signed (bool | None): Whether the certificate the server serves over TLS is self-signed
            (issuer equals subject); None while unknown. The certificate checks fill it in.
    """

    server: Mapping[str, str]
    host: HostSettings = field(default_factory=HostSettings)
    serves_self_signed: bool | None = None


Rule = Callable[[Facts], list[Problem]]

# Scopes every OpenID provider grants; Azure needs at least one scope of the server's own API besides these.
_OIDC_SCOPES = frozenset({"openid", "profile", "email", "offline_access"})


def oauth_on(values: Mapping[str, str]) -> bool:
    """Return whether callers sign in with OAuth.

    Args:
        values (Mapping[str, str]): Server settings.

    Returns:
        bool: True when an OAuth provider is chosen.
    """
    return _OAUTH_ON.active(values)


def tls_on(values: Mapping[str, str]) -> bool:
    """Return whether the server serves HTTPS: the one reading of `MCP_TLS_ENABLED` for every command.

    Args:
        values (Mapping[str, str]): Server settings.

    Returns:
        bool: True when `MCP_TLS_ENABLED` holds a spelling the server reads as true (`true`, `1`, `yes`, `on`, any
            case).
    """
    return _TLS_ON.active(values)


def vault_on(values: Mapping[str, str]) -> bool:
    """Return whether per-caller CONNX logins come from HashiCorp Vault.

    Args:
        values (Mapping[str, str]): Server settings.

    Returns:
        bool: True when the credential provider is hashicorp.
    """
    return _VAULT_ON.active(values)


def _rule_inactive_dependents(facts: Facts) -> list[Problem]:
    """Refuse another OAuth provider's field once a provider is chosen; warn about any other value without effect.

    A chosen provider accepts only its own fields: a field of another provider is a mistake (a value meant for
    another environment, or a provider switched without its fields). With no provider chosen a field only warns,
    so the fields can be set before the switch.
    """
    values = facts.server
    provider = values.get("MCP_OAUTH_PROVIDER")
    problems: list[Problem] = []
    for key, setting in SCHEMA.items():
        depends = setting.depends_on
        if key not in values or depends is None or setting.active(values):
            continue
        if depends.key == "MCP_OAUTH_PROVIDER" and oauth_on(values) and depends.values is not None:
            owners = ", ".join(p for p in OAUTH_PROVIDERS if p in depends.values)
            message = (
                f"{key} is a field of {owners}, but MCP_OAUTH_PROVIDER is {provider}, which accepts only its own "
                f"fields. Remove it with --unset {key}, or set MCP_OAUTH_PROVIDER to {owners.replace(', ', ' or ')}."
            )
            problems.append(Problem(key, message))
        else:
            message = f"{key} has no effect unless {depends.describe()}. Remove it with --unset {key}."
            problems.append(Problem(key, message, error=False))
    return problems


def _rule_vault_needs_oauth(facts: Facts) -> list[Problem]:
    """Refuse Vault without OAuth: the server looks up each signed-in caller's login (R9)."""
    if not vault_on(facts.server) or oauth_on(facts.server):
        return []
    message = (
        "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth: Vault holds a CONNX login per signed-in caller. "
        "Choose MCP_OAUTH_PROVIDER, or set MCP_CREDENTIAL_PROVIDER to none."
    )
    return [Problem("MCP_CREDENTIAL_PROVIDER", message)]


def _rule_azure_scopes(facts: Facts) -> list[Problem]:
    """Refuse Azure without a custom scope (R4) and warn about a scope given with its api:// prefix."""
    values = facts.server
    if values.get("MCP_OAUTH_PROVIDER") != "azure" or "MCP_OAUTH_REQUIRED_SCOPES" not in values:
        return []
    scopes = [s for s in re.split(r"[\s,]+", values["MCP_OAUTH_REQUIRED_SCOPES"]) if s]
    problems: list[Problem] = []
    if all(s in _OIDC_SCOPES for s in scopes):
        message = (
            "MCP_OAUTH_PROVIDER=azure needs MCP_OAUTH_REQUIRED_SCOPES to name at least one custom scope of the "
            "app registration's exposed API, e.g. access_as_user."
        )
        problems.append(Problem("MCP_OAUTH_REQUIRED_SCOPES", message))
    for scope in (s for s in scopes if "://" in s):
        message = (
            f"MCP_OAUTH_REQUIRED_SCOPES holds {scope!r}: Azure tokens carry the scope without its api:// prefix, "
            f"so give {scope.rsplit('/', 1)[-1]!r}."
        )
        problems.append(Problem("MCP_OAUTH_REQUIRED_SCOPES", message, error=False))
    return problems


def _rule_aws_discovery(facts: Facts) -> list[Problem]:
    """Warn when the AWS discovery URL is not a Cognito user pool's (R6)."""
    values = facts.server
    url = values.get("MCP_OAUTH_CONFIG_URL")
    if values.get("MCP_OAUTH_PROVIDER") != "aws" or url is None:
        return []
    if re.fullmatch(r"cognito-idp\.[a-z0-9-]+\.amazonaws\.com", urlsplit(url).hostname or ""):
        return []
    message = (
        "MCP_OAUTH_CONFIG_URL for aws should be the user pool's discovery URL, https://cognito-idp.<region>."
        "amazonaws.com/<pool-id>/.well-known/openid-configuration, not the login domain's."
    )
    return [Problem("MCP_OAUTH_CONFIG_URL", message, error=False)]


def _rule_oauth_self_signed(facts: Facts) -> list[Problem]:
    """Refuse OAuth over TLS with a self-signed certificate (R8); warn while the certificate is unknown."""
    if not (tls_on(facts.server) and oauth_on(facts.server)) or facts.serves_self_signed is False:
        return []
    if facts.serves_self_signed:
        message = (
            "OAuth with a self-signed certificate is refused by the server: callers' sign-in cannot trust it. "
            "Provide a CA-issued certificate pair, or set MCP_OAUTH_PROVIDER to none."
        )
        return [Problem("MCP_OAUTH_PROVIDER", message)]
    message = (
        "OAuth over TLS needs a CA-issued certificate: the server refuses a self-signed one and never generates "
        "a certificate in OAuth mode."
    )
    return [Problem("MCP_OAUTH_PROVIDER", message, error=False)]


def _rule_vault_ca(facts: Facts) -> list[Problem]:
    """Warn when the Vault CA file has no effect, or VAULT_CACERT names a path only the container knows."""
    values, ca_file = facts.server, facts.host.vault_ca_file
    if ca_file and not vault_on(values):
        message = "--vault-ca has no effect unless MCP_CREDENTIAL_PROVIDER is hashicorp. Remove it with --vault-ca ''."
        return [Problem("VAULT_CACERT", message, error=False)]
    if "VAULT_CACERT" in values and vault_on(values):
        if ca_file:
            message = "VAULT_CACERT is replaced at start by the mounted --vault-ca file. Remove it with --unset."
        else:
            message = (
                "VAULT_CACERT must name a file inside the container. Give the file on this machine with "
                "--vault-ca instead, and --unset VAULT_CACERT."
            )
        return [Problem("VAULT_CACERT", message, error=False)]
    return []


def _rule_licence_host(facts: Facts) -> list[Problem]:
    """Warn that without a licence server host the server starts, but its queries cannot be licensed."""
    if "CONNX_LICENSE_HOST" in facts.server:
        return []
    message = (
        "CONNX_LICENSE_HOST is not set: the server starts, but queries need a reachable licence server. "
        "Set it, e.g. --set CONNX_LICENSE_HOST=host.docker.internal when it runs on this machine."
    )
    return [Problem("CONNX_LICENSE_HOST", message, error=False)]


def _rule_certificate_directory(facts: Facts) -> list[Problem]:
    """Refuse a certificate directory the server would refuse at startup; warn when TLS is off."""
    if facts.host.cert_dir is None:
        return []
    if not tls_on(facts.server):
        message = f"The certificate directory {facts.host.cert_dir} has no effect unless MCP_TLS_ENABLED is true."
        return [Problem("cert_dir", message, error=False)]
    primary = certs.primary_host(facts.server.get("MCP_TLS_HOSTS"))
    if reason := certs.directory_problem(Path(facts.host.cert_dir), primary, datetime.now(UTC)):
        return [Problem("cert_dir", f"--certs {shell_argument(str(facts.host.cert_dir))} is refused: {reason}.")]
    return []


def _rule_tls_hosts(facts: Facts) -> list[Problem]:
    """Refuse TLS with no certificate pair to use and no host to generate one for (the server's own refusal)."""
    if not tls_on(facts.server) or certs.primary_host(facts.server.get("MCP_TLS_HOSTS")):
        return []
    directory = Path(facts.host.cert_dir) if facts.host.cert_dir else None
    if directory is not None and certs.on_disk(directory, None) is not None:
        return []
    message = (
        "MCP_TLS_HOSTS is required with TLS on unless --certs names a directory holding a certificate pair: "
        "the server generates its certificate for the first host. Set e.g. --set MCP_TLS_HOSTS=mcp.example.com."
    )
    return [Problem("MCP_TLS_HOSTS", message)]


def serves_self_signed(values: Mapping[str, str], host: HostSettings) -> bool | None:
    """Return whether the certificate the server will serve is self-signed; None while unknown (no pair on disk)."""
    if host.cert_dir is None:
        return None
    pair = certs.on_disk(Path(host.cert_dir), certs.primary_host(values.get("MCP_TLS_HOSTS")))
    return pair.self_signed if pair is not None else None


# Cross-field rules the server enforces at startup (and soft rules that only warn). Each takes the
# server and host settings as Facts; add one function per rule.
RULES: list[Rule] = [
    _rule_inactive_dependents,
    _rule_licence_host,
    _rule_vault_needs_oauth,
    _rule_azure_scopes,
    _rule_aws_discovery,
    _rule_oauth_self_signed,
    _rule_vault_ca,
    _rule_certificate_directory,
    _rule_tls_hosts,
]


def refused_key(key: str) -> str | None:
    """Return why a key can never be a server setting, or None when it can.

    Args:
        key (str): The variable name.

    Returns:
        str | None: The reason the key is refused by name, or None. It never names the key: a message's origin
            names a valid one, and an invalid one may be a value pasted where a key belongs.
    """
    if not KEY_PATTERN.fullmatch(key):
        return (
            "the name is not a valid setting name; a setting name is letters, digits and underscores, not starting "
            "with a digit."
        )
    name = key.upper()
    if name in RETIRED_KEYS or name.endswith("_ENDPOINT"):
        return "it is retired; the server refuses to start while it is set. Leave it out."
    if name in FIXED_KEYS:
        return "it is fixed in the server image and must not be set."
    if name == EULA_KEY:
        return f"{CLI_NAME} accepts the EULA when it starts the server; leave it out."
    # A compose variable or a host setting's own name: one message, whatever names it.
    host = HOST_SCHEMA.get(key.lower())
    if (how := HOST_KEYS.get(name) or (host.how_set if host is not None else None)) is not None:
        return f"it is a host setting, not a server setting; {how}."
    return None


def worded(origin: str, reason: str) -> str:
    """Return a settings refusal or warning in the one form every source shares: `<origin>: <reason>`.

    Args:
        origin (str): What the message is about, as the administrator gave it: a flag with its operand, a file's
            line, a setting asked for.
        reason (str): Why, as a sentence that never repeats what the origin names.

    Returns:
        str: The message.
    """
    return f"{origin}: {reason}"


def canonical_key(key: str) -> str:
    """Return a schema key under its own name: the server reads setting names case-insensitively.

    Args:
        key (str): The variable name as given.

    Returns:
        str: The schema key it names in any case, else the key unchanged.
    """
    return key.upper() if key.upper() in SCHEMA else key


def is_secret(key: str) -> bool:
    """Return whether a key's value must never be printed; unknown keys are judged by their name.

    Args:
        key (str): The variable name.

    Returns:
        bool: True for a schema secret, or an unknown key whose name suggests one.
    """
    setting = SCHEMA.get(canonical_key(key))
    return setting.secret if setting is not None else _SECRET_NAME.search(key) is not None


def mask(values: Mapping[str, str]) -> dict[str, str]:
    """Return the values with every secret replaced by `***hidden***`.

    Args:
        values (Mapping[str, str]): Server settings.

    Returns:
        dict[str, str]: The same keys, safe to print.
    """
    return {k: HIDDEN if is_secret(k) else v for k, v in values.items()}


def value_problem(key: str, value: str) -> str | None:
    """Check one value against its key's domain; unknown keys are not checked.

    Args:
        key (str): The variable name.
        value (str): The value; "" is an explicit empty value.

    Returns:
        str | None: Why the value is refused, naming neither the key nor the value; or None.
    """
    if LINE_BREAK.search(value):
        return "it cannot hold a line break or NUL character; the server settings file keeps one setting per line."
    setting = SCHEMA.get(canonical_key(key))
    return setting.problem(value) if setting is not None else None


def server_setting_problem(key: str, value: str) -> str | None:
    """Return why a server setting cannot be given a value: `--set` and an import both ask this.

    Args:
        key (str): The variable name, canonical.
        value (str): The value; "" is an explicit empty value.

    Returns:
        str | None: Why the key is refused by name or the value is refused, or None.
    """
    return refused_key(key) or value_problem(key, value)


def set_host_setting(host: HostSettings, name: str, raw: str) -> str | None:
    """Parse one host setting and set it: the host flags and an import's `# host` lines both call this.

    Args:
        host (HostSettings): The host settings to change.
        name (str): A name in `HOST_SCHEMA`.
        raw (str): The value as given.

    Returns:
        str | None: Why the value is refused (nothing is set), as a sentence that does not repeat the value; or None.
    """
    try:
        setattr(host, name, HOST_SCHEMA[name].parse(raw))
    except ValueError as reason:
        return f"{reason}."
    return None


def ask_at_start_problem(key: str) -> str | None:
    """Return why a key cannot be asked for at start: `--ask-at-start` and an import's `# secret` lines both ask this.

    Args:
        key (str): The variable name, canonical.

    Returns:
        str | None: Why it is refused, or None for a secret that can be asked for.
    """
    if refusal := refused_key(key):
        return refusal
    if not is_secret(key):
        return "it is not a secret, and only a secret can be asked for at start; store its value instead."
    return None


def missing(values: Mapping[str, str], supplied_at_start: Collection[str] = frozenset()) -> list[str]:
    """Return the required, active settings that have no value, in schema order.

    Args:
        values (Mapping[str, str]): Server settings.
        supplied_at_start (Collection[str]): Keys `start` asks for (ask-at-start secrets); never missing.

    Returns:
        list[str]: The missing keys.
    """
    return [k for k, s in SCHEMA.items() if k not in values and k not in supplied_at_start and s.is_required(values)]


def describe(values: Mapping[str, str], asked_at_start: Collection[str] = frozenset()) -> dict[str, list[str]]:
    """Return the server settings grouped for a person, every secret masked and every unset setting explained.

    Args:
        values (Mapping[str, str]): Server settings.
        asked_at_start (Collection[str]): Secrets `start` asks for; shown as such instead of unset.

    Returns:
        dict[str, list[str]]: Each group (in schema order, then `unvalidated` for unknown keys, when any) and its
            lines, e.g. `MCP_PATH = '/sql'` or `MCP_PATH (unset: server default '/mcp')`.
    """
    masked = mask(values)
    sections: dict[str, list[str]] = {}
    for group in Group:
        entries = sections.setdefault(str(group), [])
        for key, setting in SCHEMA.items():
            if setting.group is not group:
                continue
            if key in masked:
                entries.append(f"{key} = {masked[key]!r}")
            elif key in asked_at_start:
                entries.append(f"{key} (asked at start)")
            else:
                default = f"server default {setting.default!r}" if setting.default is not None else "no default"
                entries.append(f"{key} (unset: {default})")
    if unknown := [k for k in masked if k not in SCHEMA]:
        sections["unvalidated"] = [f"{k} = {masked[k]!r}" for k in unknown]
    return sections


def check(values: Mapping[str, str], host: HostSettings | None = None) -> list[Problem]:
    """Run every cross-field rule; `edit.resolve` warns about an unknown key where an edit stores it.

    Args:
        values (Mapping[str, str]): Server settings.
        host (HostSettings | None): Host settings; None judges the server settings alone.

    Returns:
        list[Problem]: Errors and warnings, in rule order.
    """
    host = host if host is not None else HostSettings()
    facts = Facts(values, host, serves_self_signed=serves_self_signed(values, host))
    return [p for rule in RULES for p in rule(facts)]
