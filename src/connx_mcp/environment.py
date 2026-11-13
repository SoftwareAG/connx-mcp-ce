#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""environment.py - The environment store: base directory, names, server and host settings files.

Description:
    An environment is a directory `<base>/<name>/` holding `server.env` (server
    settings, handed to the container verbatim, only the keys the administrator set, owner-only)
    and `host.json` (host settings, read only by connx-mcp-ce and compose). The base directory is
    `~/.connx/connx-mcp-ce` unless MCP_ENVIRONMENT_PATH names another; the environment name comes
    from the command line, then CONNX_MCP_ENVIRONMENT, then `default`.

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
import re
import subprocess
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from connx_mcp import CLI_NAME, settings
from connx_mcp.output import CliError, ExitCode
from connx_mcp.settings import HOST_SCHEMA, LINE_BREAK, HostSettings, Missing

DEFAULT_NAME = "default"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SERVER_FILE = "server.env"
HOST_FILE = "host.json"
# The variables connx-mcp-ce reads from its own environment: the environment a command uses when none is named,
# and the base directory that holds every environment.
ENVIRONMENT_VARIABLE = "CONNX_MCP_ENVIRONMENT"
BASE_VARIABLE = "MCP_ENVIRONMENT_PATH"
# The base directory when MCP_ENVIRONMENT_PATH is unset, as `--help` names it.
DEFAULT_BASE = f"~/.connx/{CLI_NAME}"
_UNQUOTED = re.compile(r"[A-Za-z0-9_./:@,+-]*")


def base_directory() -> Path:
    """Return the directory that holds every environment.

    Returns:
        Path: MCP_ENVIRONMENT_PATH when set, else DEFAULT_BASE, with `~` expanded.
    """
    return Path(os.environ.get(BASE_VARIABLE) or DEFAULT_BASE).expanduser()


def suggest_name(name: str) -> str:
    """Turn an invalid name into the nearest valid one.

    Args:
        name (str): The name given.

    Returns:
        str: A name matching the environment name pattern.
    """
    candidate = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).lstrip("_-")[:32].rstrip("-")
    return candidate if candidate and NAME_PATTERN.fullmatch(candidate) else DEFAULT_NAME


def resolve_name(given: str | None, command: str) -> str:
    """Pick and validate the environment name.

    Args:
        given (str | None): The name on the command line, if any.
        command (str): The command, for the suggested next command.

    Returns:
        str: The name: the one given, else CONNX_MCP_ENVIRONMENT, else `default`.

    Raises:
        CliError: The name cannot be a container or volume name.
    """
    name = given or os.environ.get(ENVIRONMENT_VARIABLE) or DEFAULT_NAME
    if not NAME_PATTERN.fullmatch(name):
        suggestion = suggest_name(name)
        raise CliError(
            ExitCode.USAGE,
            f"Environment name {name!r} is not valid: use 1-32 lower-case letters, digits, '-' or '_', "
            f"starting with a letter or digit. Try {suggestion!r}.",
            f"{CLI_NAME} {command} {suggestion}",
        )
    return name


# Host settings that must be set before the environment can start, with the flag that sets each.
REQUIRED_HOST = {name: row.flag or name for name, row in HOST_SCHEMA.items() if row.required}


@dataclass
class Environment:
    """One environment's settings.

    Attributes:
        name (str): The environment name.
        directory (Path): Its directory under the base directory.
        server (dict[str, str]): Server settings; only the keys the administrator set.
        host (HostSettings): Host settings.
    """

    name: str
    directory: Path
    server: dict[str, str] = field(default_factory=dict)
    host: HostSettings = field(default_factory=HostSettings)

    @property
    def exists(self) -> bool:
        """Whether the environment has been saved."""
        return (self.directory / SERVER_FILE).is_file()

    @property
    def asked_at_start(self) -> frozenset[str]:
        """The server keys marked ask-at-start: `start` asks for them, and they are never stored."""
        return frozenset(self.host.ask_at_start or ())

    def missing_host(self) -> list[str]:
        """Return the flags of required host settings with no value."""
        return [m.label for m in self.missing() if m.host]

    def missing(self, supplied_at_start: Collection[str] = frozenset()) -> list[Missing]:
        """Return every required value the environment lacks: server settings first, then host settings.

        Args:
            supplied_at_start (Collection[str]): Server keys `start` asks for (ask-at-start secrets); never missing.

        Returns:
            list[Missing]: Each missing value with the flag that supplies it.
        """
        server = [Missing(k, k, f"--set {k}=...") for k in settings.missing(self.server, supplied_at_start)]
        host = [
            Missing(name, row.flag or name, f"{row.flag or name} ...", host=True)
            for name, row in HOST_SCHEMA.items()
            if row.required and getattr(self.host, name) is None
        ]
        return server + host


class EnvironmentNotFound(CliError):
    """A command needs an environment that has not been saved; the next command creates it.

    Args:
        env (Environment): The environment that does not exist.
    """

    def __init__(self, env: Environment) -> None:
        super().__init__(
            ExitCode.ENVIRONMENT_NOT_FOUND,
            f"There is no environment {env.name!r} in {env.directory.parent}.",
            f"{CLI_NAME} configure {env.name}",
        )


def load(name: str) -> Environment:
    """Read an environment; a missing one comes back empty with `exists` False.

    Args:
        name (str): A valid environment name.

    Returns:
        Environment: The stored settings.

    Raises:
        CliError: A settings file cannot be read or parsed (exit 91); the error names the file and the next step.
    """
    directory = base_directory() / name
    env = Environment(name=name, directory=directory)
    server_file = directory / SERVER_FILE
    host_file = directory / HOST_FILE
    current = server_file
    problem: str | None = None
    try:
        if server_file.is_file():
            env.server = parse_env(server_file.read_text(encoding="utf-8"))
        current = host_file
        if host_file.is_file():
            data = json.loads(host_file.read_text(encoding="utf-8"))
            problem = None if isinstance(data, dict) else "it does not hold a JSON object"
            if problem is None:
                env.host = HostSettings.from_json(data)
    except (OSError, ValueError, TypeError) as error:
        problem = str(error)
    if problem is not None:
        raise CliError(
            ExitCode.ENVIRONMENT_UNREADABLE,
            f"Environment {name!r} cannot be read: {current} is unreadable ({problem}). Repair the file, or delete "
            "it and set its settings again with configure.",
            f"{CLI_NAME} configure {name}",
        )
    return env


def names() -> list[str]:
    """Return the name of every saved environment under the base directory.

    Returns:
        list[str]: Sorted names; directories that are not environments (the demo stack's) are skipped.
    """
    base = base_directory()
    if not base.is_dir():
        return []
    return sorted(
        path.name for path in base.iterdir() if NAME_PATTERN.fullmatch(path.name) and (path / SERVER_FILE).is_file()
    )


def save(env: Environment) -> None:
    """Write an environment's files: the directory and server settings owner-only; ask-at-start secrets never.

    Args:
        env (Environment): The environment to write.
    """
    private_directory(env.directory)
    asked = env.asked_at_start
    write_private(env.directory / SERVER_FILE, render_env({k: v for k, v in env.server.items() if k not in asked}))
    save_host(env)


def save_host(env: Environment) -> None:
    """Write only an environment's host settings, leaving its server settings file as it is.

    Args:
        env (Environment): The environment to write.
    """
    (env.directory / HOST_FILE).write_text(json.dumps(env.host.to_json(), indent=2) + "\n", encoding="utf-8")


def write_private(path: Path, text: str) -> None:
    """Replace a file atomically with one only its owner can read, never readable by others even for a moment.

    The text goes to a new owner-only file beside it, which then replaces the old one: an existing file with a
    wider mode is never written to, and a failed write leaves the old file whole.

    Args:
        path (Path): The file to replace.
        text (str): Its new content.

    Raises:
        BaseException: The write failed or was interrupted; the temporary file is removed and the old file kept.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            restrict_to_owner(temporary)
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def private_directory(path: Path) -> Path:
    """Create a directory owner-only, and restrict the base directory it lies in to the owner too.

    Restricting on every call also tightens a directory an earlier version left with the umask's mode.

    Args:
        path (Path): The directory.

    Returns:
        Path: The same directory.
    """
    path.mkdir(parents=True, exist_ok=True)
    base = base_directory()
    if path.is_relative_to(base) and path != base:
        restrict_to_owner(base)
    restrict_to_owner(path)
    return path


def restrict_to_owner(
    path: Path, *, windows: bool = os.name == "nt", run: Callable[..., object] = subprocess.run
) -> None:
    """Make a file or directory accessible to its owner only: POSIX mode, or an ACL on Windows.

    Args:
        path (Path): The file or directory.
        windows (bool): Whether to set a Windows ACL instead of the POSIX mode; tests force it.
        run (Callable[..., object]): Runs the `icacls` command; tests pass a fake.
    """
    if windows:
        user = os.environ.get("USERNAME", "")
        grant = f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F"
        run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], check=True, stdout=subprocess.DEVNULL)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def render_env(values: dict[str, str]) -> str:
    """Render server settings as the server settings file (`server.env`) compose reads: plain where safe, else quoted.

    Single quotes keep a value literal; a value holding a single quote is double-quoted with
    backslash escapes, and `$` doubled so compose does not interpolate it.

    Args:
        values (dict[str, str]): Server settings.

    Returns:
        str: The file content.

    Raises:
        ValueError: A value holds a line break, which would let it start another setting.
    """
    lines = []
    for key, value in values.items():
        if LINE_BREAK.search(value):
            raise ValueError(f"{key} holds a line break or NUL character; the server settings file cannot keep it.")
        if _UNQUOTED.fullmatch(value):
            lines.append(f"{key}={value}")
        elif "'" not in value:
            lines.append(f"{key}='{value}'")
        else:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
            lines.append(f'{key}="{escaped}"')
    return "".join(f"{line}\n" for line in lines)


def parse_env(text: str) -> dict[str, str]:
    """Parse a server settings file (`server.env`) in the forms render_env writes.

    Args:
        text (str): The file content.

    Returns:
        dict[str, str]: The settings, in file order.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        if len(raw) >= 2 and raw[0] == raw[-1] == "'":
            value = raw[1:-1]
        elif len(raw) >= 2 and raw[0] == raw[-1] == '"':
            value = re.sub(r"\\(.)|\$\$", lambda m: m.group(1) or "$", raw[1:-1])
        else:
            value = raw
        values[key.strip()] = value
    return values
