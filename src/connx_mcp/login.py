#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""login.py - `login`: sign the administrator in to the container registry.

Description:
    Wraps `docker login --password-stdin`: the token reaches Docker only on its stdin, read from
    this command's stdin (`--password-stdin`) or typed at a masked prompt, and never appears in an
    argument, the terminal or `--json` output; Docker's own message is shown with the token
    replaced. A refusal names where registry credentials come from (image.REGISTRY_HINT).

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import questionary

from connx_mcp import CLI_NAME, image
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.ports import Ports, Terminal
from connx_mcp.settings import HIDDEN, IMAGE_HOST

EXAMPLE = f"{CLI_NAME} login --username <user> --password-stdin < token.txt"
DOCKER_HUB_KEYS = ("docker.io", "index.docker.io", "registry-1.docker.io")


@dataclass
class LoginOptions:
    """What one `login` call asks for.

    Attributes:
        registry (image.ImagePart): The registry host and where it came from (registry_to_sign_in), named when the
            registry refuses the sign-in.
        username (str | None): The user; asked for on a terminal when None.
        password_stdin (bool): Read the token from stdin instead of prompting.
    """

    registry: image.ImagePart
    username: str | None = None
    password_stdin: bool = False


def registry_to_sign_in(given: str | None) -> image.ImagePart:
    """Return the registry `login` signs in to: `--registry` when given, else the default image's registry host.

    The image variables are read only when `--registry` is absent, so an invalid one never blocks an explicit sign-in.

    Args:
        given (str | None): The `--registry` value; None or "" when the flag is absent.

    Returns:
        image.ImagePart: The registry host and where it came from.

    Raises:
        CliError: The value is not a registry host; the message never repeats it, since it may hold a credential
            (`user:password@host`).
    """
    if not given:
        return image.default_registry()
    if not IMAGE_HOST.fullmatch(given):
        raise CliError(
            ExitCode.USAGE,
            "--registry takes a registry host alone, such as registry.example.com or registry.example.com:5000, "
            "with no path, user or password; the value given is not repeated because it may hold a credential.",
            f"{CLI_NAME} login --registry <registry host>",
        )
    return image.ImagePart(given, image.SOURCE_REGISTRY_FLAG)


def _ask(terminal: Terminal | None, message: str, secret: bool, what: str) -> str:
    """Ask for one value on the terminal, masked when secret; refuse when there is no terminal."""
    if terminal is None:
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"The registry {what} is needed and the command cannot ask you.",
            EXAMPLE,
        )
    with terminal.session():
        try:
            ask = questionary.password if secret else questionary.text
            return ask(message).unsafe_ask().strip()
        except KeyboardInterrupt, EOFError:
            raise CliError(ExitCode.CANCELLED, "Sign-in was cancelled.", f"{CLI_NAME} login") from None


def login(ports: Ports, out: Output, options: LoginOptions, terminal: Terminal | None, stdin: TextIO) -> None:
    """Run `login`.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        options (LoginOptions): Registry, user and where the token comes from.
        terminal (Terminal | None): Where to prompt; None when prompting is not allowed.
        stdin (TextIO): This command's stdin, read for `--password-stdin`.

    Raises:
        CliError: A value is missing, the registry refused the credentials, or docker login failed.
    """
    host = options.registry.value
    username = options.username or _ask(terminal, f"Username for {host}:", False, "username")
    if options.password_stdin:
        token = stdin.read().strip()
    else:
        token = _ask(terminal, f"Token for {username}:", True, "token")
    if not username or not token:
        raise CliError(ExitCode.INPUT_REQUIRED, "Both a username and a token are needed to sign in.", EXAMPLE)
    out.progress(f"Signing in to {host} as {username} ...")
    result = ports.docker.run(["login", host, "--username", username, "--password-stdin"], stdin=token)
    if result.returncode != 0:
        says = result.stderr.replace(token, HIDDEN).strip()
        what = f"{host} refused the sign-in of {username}"
        image.refuse_if_unauthorized(what, says, options.registry.line("registry"))
        raise CliError(ExitCode.DOCKER_FAILED, f"docker login {host} failed: {says}")
    out.result(
        {"registry": host, "username": username, "signed_in": True},
        f"Signed in to {host} as {username}.",
    )


def signed_in(registry: str) -> bool:
    """Return whether Docker's client configuration holds a sign-in for a registry.

    `docker login` records each registry under `auths` (the token itself may sit in a credential store) or
    names a credential helper for it; this reads only those names, never a credential.

    Args:
        registry (str): The registry host, `docker.io` for Docker Hub.

    Returns:
        bool: True when the configuration names the registry; False without a configuration file.

    Raises:
        CliError: The configuration file cannot be read or is not a JSON object; the message names the file
            and the error class, never the file's content.
    """
    directory = Path(os.environ["DOCKER_CONFIG"]) if os.environ.get("DOCKER_CONFIG") else Path.home() / ".docker"
    path = directory / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        raise CliError(
            ExitCode.FILE_UNREADABLE,
            f"Docker's configuration {path} cannot be read ({type(error).__name__}).",
            "docker login",
        ) from None
    if not isinstance(config, dict):
        raise CliError(ExitCode.FILE_UNREADABLE, f"Docker's configuration {path} is not a JSON object.", "docker login")
    names: set[str] = set()
    for section in ("auths", "credHelpers"):
        entries = config.get(section)
        if isinstance(entries, dict):
            names |= {_host(key) for key in entries}
    wanted = set(DOCKER_HUB_KEYS) if registry in DOCKER_HUB_KEYS else {registry}
    return bool(names & wanted)


def _host(key: str) -> str:
    """Return the registry host of a Docker configuration key such as `https://index.docker.io/v1/`."""
    return key.split("://", 1)[-1].split("/", 1)[0]
