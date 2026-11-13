#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""users.py - The demo stack's users file: seeded on first use, validated strictly, owner-only.

Description:
    The users file is in the test stack's own format, and it is validated by the test stack's own
    `users_file.py` (embedded verbatim), so the CLI and the renderer in the seed container
    cannot disagree. On top of upstream's rules the CLI requires at least one identity with a CONNX
    login before the stack starts. A blank CONNX mapping is an omitted `connx`
    key: upstream refuses `connx: {username: ""}`.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType

from connx_mcp import CLI_NAME
from connx_mcp.environment import private_directory, write_private
from connx_mcp.output import CliError, ExitCode

ASSETS = Path(__file__).parent / "assets"
SEED = Path(__file__).parent / "users.seed.yaml"


@dataclass(frozen=True)
class UsersSummary:
    """What a valid users file holds, without any secret."""

    identities: int
    mapped: int


@cache
def _upstream() -> ModuleType:
    """Load the embedded `users_file.py` once, as its own module."""
    name = "connx_mcp_demo_users_file"
    spec = importlib.util.spec_from_file_location(name, ASSETS / "users_file.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {ASSETS / 'users_file.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed_users_file(path: Path) -> bool:
    """Write the default personas (alice, bob, carol, dave, no CONNX login) unless the file exists.

    Args:
        path (Path): Where the users file goes.

    Returns:
        bool: True when the file was created.
    """
    if path.exists():
        return False
    private_directory(path.parent)
    write_private(path, SEED.read_text(encoding="utf-8"))
    return True


def validate(path: Path) -> UsersSummary:
    """Validate a users file strictly, as the renderer in the seed container will.

    Every `*_env` variable it names must be set here, because `start-demo` passes it on.

    Args:
        path (Path): The users file.

    Returns:
        UsersSummary: How many identities, and how many have a CONNX login.

    Raises:
        CliError: The file is missing or invalid; the message names the entry and field, never a secret.
    """
    if not path.is_file():
        raise CliError(
            ExitCode.DEMO_USERS_INVALID,
            f"The demo users file {path} was not found.",
            f"{CLI_NAME} configure-demo --users-file <file>",
        )
    upstream = _upstream()
    try:
        users = upstream.load_users_file(path)
    except upstream.UsersFileError as error:
        raise CliError(ExitCode.DEMO_USERS_INVALID, f"The demo users file is invalid: {error}", None) from None
    return UsersSummary(
        identities=len(users.identities), mapped=sum(1 for identity in users.identities if identity.mapped)
    )


def require_mapped(path: Path, summary: UsersSummary) -> None:
    """Refuse a users file in which nobody has a CONNX login: the demo would deny every query.

    Args:
        path (Path): The users file, named in the message.
        summary (UsersSummary): Its validated contents.

    Raises:
        CliError: No identity has a CONNX login.
    """
    if summary.mapped == 0:
        raise CliError(
            ExitCode.DEMO_NO_MAPPED_USER,
            f"No user in {path} has a CONNX login, so the server would refuse every query. Edit the file: "
            "uncomment a user's `connx:` line and name a login from your CDD, then start the demo again.",
            f"{CLI_NAME} start-demo",
        )
