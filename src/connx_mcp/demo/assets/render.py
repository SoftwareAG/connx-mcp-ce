#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render.py - Render the test stack's configuration from its users file.

Description:
    Turns the users file (users_file.py) into what the stack's containers read, in generated/:
    the Keycloak realm (keycloak/realm-connx-mcp.json), the dex configuration (dex/config.yaml)
    and the HashiCorp Vault entries the seed writes (vault/entries.json).

    The templates keep the hand-verified structure: the realm's scopes, audience mapper and the
    connx-mcp-generic proxy client; dex's issuer, client and grants. Only the identities are
    generated into them: the realm's users (each with its pinned id) and identity clients (each
    with its service-account user, id pinned), and dex's static passwords (bcrypt hash, userID the
    same pinned id). Every subject is therefore known before the stack starts: Keycloak's is the
    pinned id, dex's is computed from it. The live subject check proves both against real tokens.

    Vault gets one entry per mapped identity per identity provider it exists in, keyed by that
    provider's subject and naming that provider's issuer, plus two fixed bad entries for the deny
    paths. An unmapped identity gets none.

    Runs in the seed image (Python 3, PyYAML, bcrypt), started by startContainers.sh, so the host
    needs nothing but Docker and bash; the unit tests and the live tests import it. A command-line
    tool, not part of the server, so it reports with print(): the server's logger is not there to
    import.
    Usage: render.py --users FILE --stack DIR --out DIR, with STACK_HOST, KEYCLOAK_PORT, DEX_PORT
    and MCP_PUBLIC_BASE_URL in the environment, and every variable a ``*_env`` field names.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

import argparse
import base64
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import bcrypt
import yaml
from users_file import Client, Identity, User, UsersFile, UsersFileError, load_users_file

REALM = "connx-mcp"
DEFAULT_ROLES = f"default-roles-{REALM}"
DEX_CONNECTOR = "local"
BCRYPT_COST = 10

BAD_ENTRY_WRONG_ISSUER = "seed-wrong-issuer"
BAD_ENTRY_NO_PASSWORD = "seed-no-password"
_BAD_ENTRY_USERNAME = "seed-invalid"

# The scopes every generated client takes: the realm's defaults, the audience scope among them.
_CLIENT_SCOPES = ["basic", "acr", "profile", "email", "roles", "web-origins", "connx-mcp-audience"]


class RenderError(ValueError):
    """The users file cannot be rendered into the stack as it stands."""


@dataclass(frozen=True)
class Issuers:
    """The public issuers the stack's tokens carry, which each Vault entry must name.

    Attributes:
        keycloak (str): The Keycloak realm's issuer.
        dex (str): dex's issuer.
    """

    keycloak: str
    dex: str

    def of(self, provider: str) -> str:
        """Return the issuer of one identity provider.

        Args:
            provider (str): ``keycloak`` or ``dex``.

        Returns:
            str: Its issuer.

        Raises:
            RenderError: The stack runs no such provider.
        """
        if provider not in ("keycloak", "dex"):
            raise RenderError(f"unknown identity provider {provider!r}")
        return self.keycloak if provider == "keycloak" else self.dex


def dex_subject(user_id: str, connector: str = DEX_CONNECTOR) -> str:
    """Compute the ``sub`` dex issues for a user of a connector.

    dex marshals the protobuf ``IDTokenSubject{user_id = 1, conn_id = 2}`` and encodes it as
    unpadded base64url. Both fields are strings, written with a one-byte length.

    Args:
        user_id (str): The user's id: a static password's ``userID``.
        connector (str): The connector id; ``local`` for the password database.

    Returns:
        str: The subject.

    Raises:
        RenderError: A field is too long for a one-byte length.
    """
    encoded = b""
    for tag, label, value in ((0x0A, "userID", user_id), (0x12, "connector id", connector)):
        raw = value.encode("utf-8")
        if len(raw) > 127:
            raise RenderError(f"dex {label} {value[:20]!r}... is longer than 127 bytes")
        encoded += bytes([tag, len(raw)]) + raw
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def subject_for(identity: Identity, provider: str) -> str:
    """Return the subject an identity's tokens carry from one identity provider.

    Args:
        identity (Identity): A user or a client.
        provider (str): ``keycloak`` or ``dex``.

    Returns:
        str: Keycloak's pinned id, or dex's subject computed from it.
    """
    return identity.id if provider == "keycloak" else dex_subject(identity.id)


def keycloak_username(identity: Identity) -> str:
    """Return the Keycloak username of an identity: its name, or a client's service-account user.

    Args:
        identity (Identity): A user or a client.

    Returns:
        str: The username Keycloak knows it by.
    """
    return f"service-account-{identity.name}" if isinstance(identity, Client) else identity.name


def _first_and_last_name(user: User) -> tuple[str, str]:
    """Derive a first and last name from a username, which Keycloak's default user profile requires.

    Args:
        user (User): The user.

    Returns:
        tuple[str, str]: The name split at its first ``-``, ``.``, ``_`` or space and capitalised:
        ``connx-user`` gives ("Connx", "User"); a name with no separator gives (it, "User").
    """
    parts = re.split(r"[-._ ]", user.name, maxsplit=1)
    first = parts[0].capitalize() or user.name
    last = parts[1].capitalize() if len(parts) > 1 and parts[1] else "User"
    return first, last


def realm_users(users: UsersFile) -> list[dict]:
    """Render the realm's ``users``: each Keycloak user, and each client's service-account user.

    Args:
        users (UsersFile): The loaded users file.

    Returns:
        list[dict]: Keycloak user representations, ids pinned, in file order.
    """
    rendered = []
    for identity in users.identities:
        if "keycloak" not in identity.providers:
            continue
        user: dict[str, object] = {"id": identity.id, "username": keycloak_username(identity), "enabled": True}
        if isinstance(identity, User):
            first, last = _first_and_last_name(identity)
            user |= {
                "email": identity.email,
                "emailVerified": True,
                "firstName": first,
                "lastName": last,
                "credentials": [{"type": "password", "value": identity.password, "temporary": False}],
            }
        else:
            user["serviceAccountClientId"] = identity.name
        user["realmRoles"] = [DEFAULT_ROLES]
        rendered.append(user)
    return rendered


def realm_clients(users: UsersFile, authored: list[dict]) -> list[dict]:
    """Render the realm's ``clients``: the authored ones, then one per users-file client not among them.

    An authored client the users file also lists keeps its structure; it gains a service account
    (whose user ``realm_users`` renders with the pinned id). Its secret must be the authored one,
    which generated/mcp-server.env names.

    Args:
        users (UsersFile): The loaded users file.
        authored (list[dict]): The template's clients, which it owns.

    Returns:
        list[dict]: The authored clients, then the generated ones in file order.

    Raises:
        RenderError: The users file gives an authored client a different secret.
    """
    clients = [dict(client) for client in authored]
    by_id = {client["clientId"]: client for client in clients}
    for identity in users.identities:
        if not isinstance(identity, Client):
            continue
        if identity.name in by_id:
            client = by_id[identity.name]
            if client.get("secret") != identity.secret:
                raise RenderError(
                    f"client {identity.name!r} is authored in the realm template with its own secret, which "
                    "generated/mcp-server.env names; give that secret in the users file"
                )
            client["serviceAccountsEnabled"] = True
            continue
        clients.append(
            {
                "clientId": identity.name,
                "name": identity.name,
                "description": "A test-stack identity: mints its own token with client_credentials (users file).",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "secret": identity.secret,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "redirectUris": [],
                "webOrigins": [],
                "defaultClientScopes": list(_CLIENT_SCOPES),
                "optionalClientScopes": [],
            }
        )
    return clients


def dex_static_passwords(users: UsersFile) -> list[dict]:
    """Render dex's ``staticPasswords``: each user that exists in dex, with a bcrypt hash.

    Args:
        users (UsersFile): The loaded users file.

    Returns:
        list[dict]: dex static passwords, ``userID`` the pinned id, in file order.
    """
    return [
        {
            "email": identity.email,
            "hash": bcrypt.hashpw(identity.password.encode("utf-8"), bcrypt.gensalt(BCRYPT_COST)).decode("ascii"),
            "username": identity.name,
            "userID": identity.id,
        }
        for identity in users.identities
        if isinstance(identity, User) and "dex" in identity.providers
    ]


def vault_entries(users: UsersFile, issuers: Issuers) -> list[dict]:
    """Render the HashiCorp Vault entries: one per mapped identity per provider, plus the bad ones.

    Args:
        users (UsersFile): The loaded users file.
        issuers (Issuers): The stack's public issuers.

    Returns:
        list[dict]: ``{"subject", "identity", "provider", "data"}`` per entry; ``data`` is the KV
        entry as the Credential Vault reads it.
    """
    entries = []
    for identity in users.identities:
        if identity.connx is None:
            continue
        if isinstance(identity, User):
            first, last = _first_and_last_name(identity)
            hints = {"email": identity.email, "display_name": f"{first} {last}"}
        else:
            username = keycloak_username(identity)
            hints = {"email": f"{username}@example.com", "display_name": username}
        for provider in identity.providers:
            data = {
                "issuer": issuers.of(provider),
                "username": identity.connx.username,
                "password": identity.connx.password,
            } | hints
            entries.append(
                {
                    "subject": subject_for(identity, provider),
                    "identity": identity.name,
                    "provider": provider,
                    "data": data,
                }
            )
    other_realm = issuers.keycloak.rsplit("/realms/", 1)[0] + "/realms/some-other-realm"
    entries.append(
        {
            "subject": BAD_ENTRY_WRONG_ISSUER,
            "identity": None,
            "provider": None,
            "data": {"issuer": other_realm, "username": _BAD_ENTRY_USERNAME, "password": ""},
        }
    )
    entries.append(
        {
            "subject": BAD_ENTRY_NO_PASSWORD,
            "identity": None,
            "provider": None,
            "data": {"issuer": issuers.keycloak, "username": _BAD_ENTRY_USERNAME},
        }
    )
    return entries


def render_realm(template: str, users: UsersFile, mcp_url: str) -> dict:
    """Fill the realm template and generate its users and identity clients.

    Args:
        template (str): The realm template's text.
        users (UsersFile): The loaded users file.
        mcp_url (str): The MCP server's public base URL, no trailing slash.

    Returns:
        dict: The realm, ready to import.
    """
    realm = json.loads(template.replace("@@MCP_PUBLIC_BASE_URL@@", mcp_url))
    realm["clients"] = realm_clients(users, realm.get("clients", []))
    realm["users"] = realm_users(users)
    return realm


def render_dex(template: str, users: UsersFile, mcp_url: str, dex_issuer: str) -> dict:
    """Fill the dex template and generate its static passwords.

    Args:
        template (str): The dex configuration template's text.
        users (UsersFile): The loaded users file.
        mcp_url (str): The MCP server's public base URL, no trailing slash.
        dex_issuer (str): dex's public issuer.

    Returns:
        dict: dex's configuration.
    """
    config = yaml.safe_load(template.replace("@@MCP_PUBLIC_BASE_URL@@", mcp_url).replace("@@DEX_ISSUER@@", dex_issuer))
    config["staticPasswords"] = dex_static_passwords(users)
    return config


def stack_issuers(environ: Mapping[str, str]) -> Issuers:
    """Return the public issuers for the stack's settings.

    Args:
        environ (Mapping[str, str]): STACK_HOST, KEYCLOAK_PORT and DEX_PORT, each defaulted.

    Returns:
        Issuers: Keycloak's realm issuer and dex's issuer.
    """
    host = environ.get("STACK_HOST") or "localhost"
    return Issuers(
        keycloak=f"http://{host}:{environ.get('KEYCLOAK_PORT') or '8080'}/realms/{REALM}",
        dex=f"http://{host}:{environ.get('DEX_PORT') or '5556'}/dex",
    )


def main(argv: list[str]) -> int:
    """Render generated/ from the users file and the templates.

    Args:
        argv (list[str]): The command-line arguments, without the program name.

    Returns:
        int: 0 on success, 1 when the users file is refused.
    """
    parser = argparse.ArgumentParser(description="Render the test stack from its users file.")
    parser.add_argument("--users", type=Path, required=True, help="the users file")
    parser.add_argument("--stack", type=Path, required=True, help="the test-stack directory (templates)")
    parser.add_argument("--out", type=Path, required=True, help="the generated/ directory")
    args = parser.parse_args(argv)

    mcp_url = (os.environ.get("MCP_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
    issuers = stack_issuers(os.environ)
    try:
        users = load_users_file(args.users)
        realm = render_realm((args.stack / "keycloak" / "realm-connx-mcp.json.template").read_text(), users, mcp_url)
        dex = render_dex((args.stack / "dex" / "config.yaml.template").read_text(), users, mcp_url, issuers.dex)
        entries = vault_entries(users, issuers)
    except (UsersFileError, RenderError) as error:
        print(f"render.py: {error}", file=sys.stderr)
        return 1

    for directory in ("keycloak", "dex", "vault"):
        (args.out / directory).mkdir(parents=True, exist_ok=True)
    (args.out / "keycloak" / "realm-connx-mcp.json").write_text(json.dumps(realm, indent=2) + "\n")
    (args.out / "dex" / "config.yaml").write_text(
        "# Generated by startContainers.sh from dex/config.yaml.template and the users file.\n"
        + yaml.safe_dump(dex, sort_keys=False)
    )
    # The CONNX passwords are in here, so generated/ is gitignored. World-readable like Vault's server key
    # beside it (make-tls.sh): the seed reads it as a container user that need not be its host owner, so the
    # mode is set rather than left to the umask.
    entries_file = args.out / "vault" / "entries.json"
    entries_file.write_text(json.dumps(entries, indent=2) + "\n")
    entries_file.chmod(0o644)
    mapped = sum(1 for entry in entries if entry["identity"])
    print(
        f"render.py: {len(users.identities)} identities from {args.users.name}: "
        f"{len(realm['users'])} Keycloak users, {len(dex['staticPasswords'])} dex users, "
        f"{mapped} HashiCorp Vault entries and the 2 bad ones"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
