#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""users_file.py - Load and validate the test stack's users file.

Description:
    The users file declares every OAuth identity the test stack creates: users, who sign in with
    a password, and Keycloak client_credentials clients. For each it names the identity providers
    it exists in and the CONNX login it maps to, if any. The renderer and the live tests both read
    it through this module, so the stack and the tests cannot disagree.

    Defaults: a user's OAuth password is its name, its email is <name>@example.com, and it exists
    in Keycloak and dex. A client exists in Keycloak only. An identity with no ``connx`` entry is
    created in the providers and left unmapped. A CONNX login with no password has an empty one,
    which CONNX accepts. ``restricted: true`` on a CONNX login says the CDD refuses it the test
    data's hidden table; every identity mapping the same login must agree. Any password or secret
    may be given as ``<field>_env: VARIABLE`` instead of a literal; an unset variable is refused, a
    variable set to empty is an empty value.

    Runs in the seed image as well as in the tests, so it needs only the standard library and
    PyYAML.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

IDENTITY_PROVIDERS = ("keycloak", "dex")
CLIENT_IDENTITY_PROVIDERS = ("keycloak",)

_TOP_LEVEL_KEYS = {"identities", "test_data"}
_USER_KEYS = {"name", "id", "email", "providers", "password", "password_env", "connx"}
_CLIENT_KEYS = {"client", "service_account_id", "providers", "secret", "secret_env", "connx"}
_CONNX_KEYS = {"username", "password", "password_env", "restricted"}
_TEST_DATA_KEYS = {"visible_table", "hidden_table"}
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UsersFileError(ValueError):
    """The users file is malformed, or names a secret variable that is not set."""


@dataclass(frozen=True)
class ConnxLogin:
    """The CONNX login an identity's queries run under.

    Attributes:
        username (str): The CONNX login, which the CDD in use must define.
        password (str): Its password; empty when the file gives none.
        restricted (bool): Whether the CDD refuses this login the test data's hidden table.
    """

    username: str
    password: str
    restricted: bool = False


@dataclass(frozen=True)
class Identity:
    """What every identity has: a name, a pinned id, its identity providers and its CONNX mapping.

    Attributes:
        name (str): The username, or the client id for a client.
        id (str): The pinned id of its Keycloak user (for a client, its service-account user), and
            dex's userID; its tokens' subject derives from it.
        providers (tuple[str, ...]): The identity providers it exists in.
        connx (ConnxLogin | None): The CONNX login it maps to; None when unmapped.
    """

    name: str
    id: str
    providers: tuple[str, ...]
    connx: ConnxLogin | None

    @property
    def mapped(self) -> bool:
        """Whether a Credential Vault entry is written for this identity.

        Returns:
            bool: True when it maps to a CONNX login.
        """
        return self.connx is not None


@dataclass(frozen=True)
class User(Identity):
    """An identity that signs in with a password, in Keycloak and dex by default.

    Attributes:
        email (str): The email address the identity providers carry.
        password (str): The OAuth password, the same in every identity provider.
    """

    email: str
    password: str


@dataclass(frozen=True)
class Client(Identity):
    """A Keycloak client that mints its own tokens with client_credentials; Keycloak only.

    Its ``id`` is the file's ``service_account_id``: the Keycloak user behind the client, which
    is Keycloak's term and has nothing to do with a CONNX login.

    Attributes:
        secret (str): The client secret.
    """

    secret: str


@dataclass(frozen=True)
class LiveTables:
    """The CONNX tables the live tests read.

    Attributes:
        visible_table (str): A table the restricted login may read.
        hidden_table (str): A table the restricted login is refused on.
    """

    visible_table: str
    hidden_table: str


@dataclass(frozen=True)
class UsersFile:
    """A loaded and validated users file.

    Attributes:
        identities (tuple[User | Client, ...]): Every identity, in file order.
        test_data (LiveTables | None): The live tests' tables; None when the file has none.
    """

    identities: tuple[User | Client, ...]
    test_data: LiveTables | None


def load_users_file(path: Path, environ: Mapping[str, str] | None = None) -> UsersFile:
    """Read, validate and resolve a users file.

    Args:
        path (Path): The YAML file.
        environ (Mapping[str, str] | None): Where ``*_env`` references are looked up; the process
            environment when None.

    Returns:
        UsersFile: The identities and test data, every secret resolved.

    Raises:
        UsersFileError: The file is not YAML, or fails validation; the message names the file.
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        # Not chained: the parser's own message quotes the offending line, which may be a secret (ADR-0009).
        raise UsersFileError(f"{path}: not valid YAML{_yaml_position(error)}") from None
    try:
        return parse_users_file(data, os.environ if environ is None else environ)
    except UsersFileError as error:
        raise UsersFileError(f"{path}: {error}") from error


def parse_users_file(data: Any, environ: Mapping[str, str]) -> UsersFile:
    """Validate a parsed users file and resolve its secrets.

    Args:
        data (Any): The YAML document, as ``yaml.safe_load`` returns it.
        environ (Mapping[str, str]): Where ``*_env`` references are looked up.

    Returns:
        UsersFile: The identities and test data, every secret resolved.

    Raises:
        UsersFileError: The first problem found, naming the identity and the field.
    """
    if not isinstance(data, dict):
        raise UsersFileError("the file must be a mapping with an 'identities' list")
    _refuse_unknown_keys(data, _TOP_LEVEL_KEYS, "the file")
    entries = data.get("identities")
    if not isinstance(entries, list) or not entries:
        raise UsersFileError("'identities' must be a non-empty list")

    identities = tuple(_parse_identity(entry, index, environ) for index, entry in enumerate(entries))
    _refuse_duplicates([identity.name for identity in identities], "name")
    _refuse_duplicates([identity.id for identity in identities], "id")
    _refuse_disagreeing_restrictions(identities)
    return UsersFile(identities=identities, test_data=_parse_test_data(data.get("test_data")))


def _parse_identity(entry: Any, index: int, environ: Mapping[str, str]) -> User | Client:
    """Parse one entry of ``identities`` as a user or a client.

    Args:
        entry (Any): The entry.
        index (int): Its position, for messages until its name is known.
        environ (Mapping[str, str]): Where ``*_env`` references are looked up.

    Returns:
        User | Client: The identity, defaults applied and secrets resolved.

    Raises:
        UsersFileError: The entry is not exactly one of a user or a client.
    """
    where = f"identities[{index}]"
    if not isinstance(entry, dict) or ("name" in entry) == ("client" in entry):
        raise UsersFileError(f"{where}: must have exactly one of 'name' (a user) or 'client' (a client)")
    if "name" in entry:
        return _parse_user(entry, where, environ)
    return _parse_client(entry, where, environ)


def _parse_user(entry: dict, where: str, environ: Mapping[str, str]) -> User:
    """Parse a user entry.

    Args:
        entry (dict): The entry, which has ``name``.
        where (str): Its position, for messages until its name is known.
        environ (Mapping[str, str]): Where ``*_env`` references are looked up.

    Returns:
        User: The user, defaults applied and secrets resolved.

    Raises:
        UsersFileError: The OAuth password is empty.
    """
    name = _required_string(entry, "name", where)
    label = f"identity '{name}'"
    _refuse_unknown_keys(entry, _USER_KEYS, label)
    password = _secret(entry, "password", label, environ, default=name)
    if password == "":
        raise UsersFileError(f"{label}: 'password' is empty; an OAuth password cannot be")
    return User(
        name=name,
        id=_required_string(entry, "id", label),
        email=_required_string(entry, "email", label) if "email" in entry else f"{name}@example.com",
        password=password,
        providers=_providers(entry, label, allowed=IDENTITY_PROVIDERS),
        connx=_connx(entry, label, environ),
    )


def _parse_client(entry: dict, where: str, environ: Mapping[str, str]) -> Client:
    """Parse a client entry.

    Args:
        entry (dict): The entry, which has ``client``.
        where (str): Its position, for messages until its name is known.
        environ (Mapping[str, str]): Where ``*_env`` references are looked up.

    Returns:
        Client: The client, secrets resolved.

    Raises:
        UsersFileError: The client has no secret, or an empty one.
    """
    name = _required_string(entry, "client", where)
    label = f"identity '{name}'"
    _refuse_unknown_keys(entry, _CLIENT_KEYS, label)
    secret = _secret(entry, "secret", label, environ, default="")
    if not secret:
        raise UsersFileError(f"{label}: a client needs a non-empty 'secret' or 'secret_env'")
    return Client(
        name=name,
        id=_required_string(entry, "service_account_id", label),
        secret=secret,
        providers=_providers(entry, label, allowed=CLIENT_IDENTITY_PROVIDERS),
        connx=_connx(entry, label, environ),
    )


def _connx(entry: dict, label: str, environ: Mapping[str, str]) -> ConnxLogin | None:
    """Parse an identity's ``connx`` mapping.

    Args:
        entry (dict): The identity's entry.
        label (str): The identity, for messages.
        environ (Mapping[str, str]): Where ``password_env`` is looked up.

    Returns:
        ConnxLogin | None: The login, its password empty when none is given; None when unmapped.

    Raises:
        UsersFileError: ``connx`` is not a mapping.
    """
    if "connx" not in entry:
        return None
    connx = entry["connx"]
    if not isinstance(connx, dict):
        raise UsersFileError(f"{label}: 'connx' must be a mapping with a 'username'")
    connx_label = f"{label} connx"
    _refuse_unknown_keys(connx, _CONNX_KEYS, connx_label)
    restricted = connx.get("restricted", False)
    if not isinstance(restricted, bool):
        raise UsersFileError(f"{connx_label}: 'restricted' must be true or false")
    return ConnxLogin(
        username=_required_string(connx, "username", connx_label),
        password=_secret(connx, "password", connx_label, environ, default=""),
        restricted=restricted,
    )


def _secret(entry: dict, field: str, label: str, environ: Mapping[str, str], default: str) -> str:
    """Resolve a secret given as a literal ``<field>`` or as a ``<field>_env`` reference.

    Args:
        entry (dict): The mapping holding the field.
        field (str): The literal field's name.
        label (str): The identity, for messages.
        environ (Mapping[str, str]): Where the reference is looked up.
        default (str): The value when neither form is given.

    Returns:
        str: The literal, the variable's value (empty when set to empty), or the default.

    Raises:
        UsersFileError: Both forms are given, the reference is not a variable name, the variable
            is unset, or the literal is not a string.
    """
    reference = f"{field}_env"
    if field in entry and reference in entry:
        raise UsersFileError(f"{label}: give '{field}' or '{reference}', not both")
    if reference in entry:
        variable = entry[reference]
        if not isinstance(variable, str) or not _VARIABLE_NAME.match(variable):
            raise UsersFileError(f"{label}: '{reference}' must name an environment variable")
        if variable not in environ:
            raise UsersFileError(f"{label}: '{reference}' names {variable}, which is not set")
        return environ[variable]
    if field in entry:
        value = entry[field]
        if not isinstance(value, str):
            raise UsersFileError(f"{label}: '{field}' must be a string (quote it in YAML)")
        return value
    return default


def _providers(entry: dict, label: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """Parse an identity's ``providers`` list.

    Args:
        entry (dict): The identity's entry.
        label (str): The identity, for messages.
        allowed (tuple[str, ...]): The providers this kind of identity may exist in, which is also
            the default.

    Returns:
        tuple[str, ...]: The providers, in the file's order.

    Raises:
        UsersFileError: The list is empty, repeats a provider, or names one not allowed.
    """
    if "providers" not in entry:
        return allowed
    providers = entry["providers"]
    if not isinstance(providers, list) or not providers:
        raise UsersFileError(f"{label}: 'providers' must be a non-empty list of {', '.join(allowed)}")
    for provider in providers:
        if provider not in allowed:
            raise UsersFileError(f"{label}: provider {provider!r} is not one of {', '.join(allowed)}")
    if len(set(providers)) != len(providers):
        raise UsersFileError(f"{label}: 'providers' lists a provider twice")
    return tuple(providers)


def _parse_test_data(test_data: Any) -> LiveTables | None:
    """Parse the ``test_data`` section.

    Args:
        test_data (Any): The section, or None when the file has none.

    Returns:
        LiveTables | None: The tables; None when the section is absent.

    Raises:
        UsersFileError: The section is not a mapping.
    """
    if test_data is None:
        return None
    if not isinstance(test_data, dict):
        raise UsersFileError("'test_data' must be a mapping")
    _refuse_unknown_keys(test_data, _TEST_DATA_KEYS, "test_data")
    return LiveTables(
        visible_table=_required_string(test_data, "visible_table", "test_data"),
        hidden_table=_required_string(test_data, "hidden_table", "test_data"),
    )


def _required_string(mapping: dict, field: str, label: str) -> str:
    """Return a field that must be a non-empty string.

    Args:
        mapping (dict): The mapping holding the field.
        field (str): Its name.
        label (str): What the mapping is, for messages.

    Returns:
        str: The value.

    Raises:
        UsersFileError: The field is missing, empty, or not a string.
    """
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise UsersFileError(f"{label}: '{field}' is required and must be a non-empty string")
    return value


def _refuse_unknown_keys(mapping: dict, known: set[str], label: str) -> None:
    """Refuse keys the format does not define, so a misspelt field is not silently ignored.

    Args:
        mapping (dict): The mapping to check.
        known (set[str]): The keys it may have.
        label (str): What the mapping is, for messages.

    Raises:
        UsersFileError: A key is not in ``known``.
    """
    unknown = sorted(str(key) for key in mapping if key not in known)
    if unknown:
        raise UsersFileError(f"{label}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(known))}")


def _refuse_duplicates(values: list[str], field: str) -> None:
    """Refuse a value that two identities share.

    Args:
        values (list[str]): Every identity's value of the field.
        field (str): The field, for messages.

    Raises:
        UsersFileError: A value occurs twice.
    """
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise UsersFileError(f"duplicate {field} {value!r}: each identity needs its own")
        seen.add(value)


def _refuse_disagreeing_restrictions(identities: tuple[User | Client, ...]) -> None:
    """Refuse a CONNX login one identity marks restricted and another does not.

    Restriction is the CDD's, a property of the login, so it cannot differ by identity.

    Args:
        identities (tuple[User | Client, ...]): Every identity in the file.

    Raises:
        UsersFileError: Two identities map the same login with different ``restricted``.
    """
    restricted: dict[str, bool] = {}
    for identity in identities:
        if identity.connx is None:
            continue
        login = identity.connx
        if restricted.setdefault(login.username, login.restricted) != login.restricted:
            raise UsersFileError(
                f"CONNX login {login.username!r} is marked restricted for one identity and not for another "
                f"(identity '{identity.name}'); restriction belongs to the login, so mark it the same everywhere"
            )


def _yaml_position(error: yaml.YAMLError) -> str:
    """Return where a YAML error is and what the parser found, never the source text it quotes.

    A marked error's message quotes the offending line, which may hold a password or secret, so
    only its position and problem are kept. An error with no mark (a reader error, such as a
    control character) quotes no source line, so its own message is kept whole.

    Args:
        error (yaml.YAMLError): The parser's error.

    Returns:
        str: ``" at line L, column C: <problem>"`` for a marked error, ``": <message>"`` otherwise.
    """
    if not isinstance(error, yaml.MarkedYAMLError):
        return f": {error}"
    mark = error.problem_mark or error.context_mark
    problem = error.problem or error.context
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return where + (f": {problem}" if problem else "")
