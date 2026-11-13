#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""output.py - What every command prints: human text or one JSON object, and the exit-code table.

Description:
    A command ends in exactly one of two ways: a result, or a CliError. In JSON mode both print one
    object on stdout (`{"ok": true, "command", "result"}` or `{"ok": false, "command", "error":
    {code, exit_code, message, next}}`); in human mode a result goes to stdout and an error to
    stderr with a `Next:` line. The exit codes are a public contract for scripts and LLMs: add new
    ones, never renumber.

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
import shlex
import sys
from collections.abc import Mapping
from enum import IntEnum
from typing import Any, TextIO

import typer

from connx_mcp import CLI_NAME


class ExitCode(IntEnum):
    """Stable exit codes; the lower-case name is the JSON error code."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    INPUT_REQUIRED = 3
    NOT_IMPLEMENTED = 4
    DOCKER_MISSING = 10
    DOCKER_NOT_RUNNING = 11
    COMPOSE_MISSING = 12
    DOCKER_UNSUPPORTED = 13
    DOCKER_TOO_OLD = 14
    ENVIRONMENT_NOT_FOUND = 20
    IMAGE_NOT_FOUND = 30
    DOCKER_FAILED = 31
    CONTAINER_EXITED = 32
    START_TIMEOUT = 33
    CONTAINER_NOT_FOUND = 34
    CDD_NOT_FOUND = 35
    DEMO_NOT_CONFIGURED = 40
    DEMO_USERS_INVALID = 41
    DEMO_NO_MAPPED_USER = 42
    DEMO_FAILED = 43
    REGISTRY_UNAUTHORIZED = 44
    PULL_FAILED = 45
    IMAGE_TOO_OLD = 46
    IMAGE_VARIABLE_INVALID = 47
    IMAGE_REFERENCE_INVALID = 48
    MOUNTED_FILE_UNREADABLE = 49
    SERVER_CANNOT_OPEN_CDD = 50
    SERVER_CANNOT_USE_VAULT_CA = 51
    START_OVERRIDE_INVALID = 60
    SECRET_SOURCE_EMPTY = 61
    VAULT_CA_NOT_FOUND = 80
    PORT_IN_USE = 90
    ENVIRONMENT_UNREADABLE = 91
    FILE_UNREADABLE = 92
    CERTIFICATE_DIRECTORY_NOT_SET = 70
    CERTIFICATE_EXPIRED = 71
    CERTIFICATE_DIRECTORY_INVALID = 72
    CANCELLED = 100
    REMOVE_FAILED = 101
    IMPORT_FILE_UNREADABLE = 110
    EXPORT_WRITE_FAILED = 111
    SKILL_EXISTS = 120
    SKILL_NOT_IN_IMAGE = 121
    NO_HARNESS_CHOSEN = 122
    SKILL_CHANGED = 123
    UNINSTALL_FAILED = 124
    ZIP_FAILED = 130
    DEMO_BOUND_ELSEWHERE = 140
    DEMO_ENVIRONMENT_BOUND = 141
    DEMO_SERVICE_NOT_CHOSEN = 142
    DEMO_STACK_ISSUER_MISMATCH = 190

    @property
    def code(self) -> str:
        """The JSON error code."""
        return self.name.lower()


EXIT_CODE_MEANINGS: dict[ExitCode, str] = {
    ExitCode.OK: "success",
    ExitCode.FAILURE: "unexpected failure",
    ExitCode.USAGE: "wrong command, option or value",
    ExitCode.INPUT_REQUIRED: (
        "an answer is needed but the command cannot ask (--no-input, --json, or its input does not come from a "
        "keyboard)"
    ),
    ExitCode.NOT_IMPLEMENTED: "command not available in this version",
    ExitCode.DOCKER_MISSING: "Docker is not installed",
    ExitCode.DOCKER_NOT_RUNNING: "the Docker daemon is not running or not reachable",
    ExitCode.COMPOSE_MISSING: "Docker Compose v2 is missing",
    ExitCode.DOCKER_UNSUPPORTED: "a container runtime other than Docker (Podman)",
    ExitCode.DOCKER_TOO_OLD: "Docker Engine older than 24",
    ExitCode.ENVIRONMENT_NOT_FOUND: "no environment by that name",
    ExitCode.IMAGE_NOT_FOUND: f"the environment's image is not on this machine ({CLI_NAME} download)",
    ExitCode.DOCKER_FAILED: "a docker or docker compose command failed",
    ExitCode.CONTAINER_EXITED: "the server exited while starting",
    ExitCode.START_TIMEOUT: "the server was not healthy within the timeout; it is left running",
    ExitCode.CONTAINER_NOT_FOUND: "the environment has no container (never started)",
    ExitCode.CDD_NOT_FOUND: "the environment's CDD file is missing",
    ExitCode.DEMO_NOT_CONFIGURED: f"the demo stack is not configured ({CLI_NAME} configure-demo)",
    ExitCode.DEMO_USERS_INVALID: "the demo users file is missing or invalid",
    ExitCode.DEMO_NO_MAPPED_USER: "no demo user has a CONNX login",
    ExitCode.DEMO_FAILED: "a demo stack container failed to build, render, start or stop",
    ExitCode.REGISTRY_UNAUTHORIZED: f"the registry refused the sign-in or the download ({CLI_NAME} login)",
    ExitCode.PULL_FAILED: "the image could not be downloaded",
    ExitCode.IMAGE_TOO_OLD: f"the image is older than this {CLI_NAME} supports",
    ExitCode.IMAGE_VARIABLE_INVALID: (
        "CONNX_MCP_REGISTRY, CONNX_MCP_IMAGE or CONNX_MCP_IMAGE_TAG is invalid, or a renamed one is set"
    ),
    ExitCode.IMAGE_REFERENCE_INVALID: f"the environment's image names no registry ({CLI_NAME} configure --image)",
    ExitCode.MOUNTED_FILE_UNREADABLE: "the CDD or Vault CA file is not readable by other users (chmod 644 <path>)",
    ExitCode.SERVER_CANNOT_OPEN_CDD: "the server logged that it cannot open the CDD file",
    ExitCode.SERVER_CANNOT_USE_VAULT_CA: "the server logged that it cannot use the Vault CA file",
    ExitCode.START_OVERRIDE_INVALID: "a one-run start override names something that does not exist",
    ExitCode.SECRET_SOURCE_EMPTY: "the environment variable named for an ask-at-start secret is not set",
    ExitCode.VAULT_CA_NOT_FOUND: "the environment's Vault CA file is missing",
    ExitCode.PORT_IN_USE: f"another running environment publishes the port ({CLI_NAME} configure --port)",
    ExitCode.ENVIRONMENT_UNREADABLE: "an environment's settings file cannot be read or parsed",
    ExitCode.FILE_UNREADABLE: (
        "demo.json, a skill install record, an instructions file or Docker's config.json cannot be read or parsed, "
        "an instructions file or a skill install record cannot be written, or the image's INSTRUCTIONS.md is not "
        "UTF-8 or holds a marker line"
    ),
    ExitCode.CERTIFICATE_DIRECTORY_NOT_SET: "the environment has no certificate directory (configure --certs)",
    ExitCode.CERTIFICATE_EXPIRED: "the server exited on its expired generated certificate; purge its volume",
    ExitCode.CERTIFICATE_DIRECTORY_INVALID: "the server would refuse the certificate directory",
    ExitCode.CANCELLED: "a question was cancelled (Ctrl-C, end of input) or declined; nothing was changed",
    ExitCode.REMOVE_FAILED: "the environment has no container left, but its directory could not be deleted",
    ExitCode.IMPORT_FILE_UNREADABLE: "the settings file to import cannot be read or is not UTF-8",
    ExitCode.EXPORT_WRITE_FAILED: "the export file could not be written",
    ExitCode.SKILL_EXISTS: (
        f"a skill {CLI_NAME} did not install, skill instructions edited by hand or with broken markers, or a project "
        "instructions file that links outside the project are in the way (--force writes them anyway)"
    ),
    ExitCode.SKILL_NOT_IN_IMAGE: "the environment's image holds no connx-sql-expert skill",
    ExitCode.NO_HARNESS_CHOSEN: "no harness was chosen; nothing was installed or removed",
    ExitCode.SKILL_CHANGED: (
        "skill instructions edited by hand or with broken markers, or a project instructions file that links "
        "outside the project, are in the way of uninstall-skill; nothing was removed"
    ),
    ExitCode.UNINSTALL_FAILED: (
        "uninstall-skill could not remove a skill copy or a section, or update its record; the message lists "
        "anything removed before it"
    ),
    ExitCode.ZIP_FAILED: "the skill zip or its install notes could not be written",
    ExitCode.DEMO_BOUND_ELSEWHERE: "the demo stack is bound to another environment (configure-demo --rebind)",
    ExitCode.DEMO_ENVIRONMENT_BOUND: "the environment is bound to the demo stack (--force also unbinds it)",
    ExitCode.DEMO_SERVICE_NOT_CHOSEN: f"the demo stack does not run that service ({CLI_NAME} configure-demo)",
    ExitCode.DEMO_STACK_ISSUER_MISMATCH: "the remote demo stack names another issuer: give its exact --stack-host",
}


def exit_codes() -> list[dict[str, Any]]:
    """Return every exit code with its name and meaning, lowest first, as `exit-codes --json` lists them."""
    return [{"exit_code": int(c), "code": c.code, "meaning": EXIT_CODE_MEANINGS[c]} for c in sorted(ExitCode)]


def exit_code_table() -> str:
    """Render the exit-code table for help text."""
    return "\n".join(f"  {int(c):>3}  {c.code:<20} {EXIT_CODE_MEANINGS[c]}" for c in ExitCode)


# The JSON `error` object's own fields, which a CliError's details cannot replace.
_ERROR_FIELDS = frozenset({"code", "exit_code", "message", "next"})


class CliError(Exception):
    """A failure the administrator can act on.

    Args:
        exit_code (ExitCode): The exit code, which also gives the JSON error code.
        message (str): What went wrong and what to do about it.
        next_command (str | None): The command to run next, if one helps.
        details (Mapping[str, Any] | None): More fields for the JSON `error` object, beside code, message and next.

    Raises:
        ValueError: A detail is named like one of the fields it goes beside.
    """

    def __init__(
        self,
        exit_code: ExitCode,
        message: str,
        next_command: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if taken := sorted(_ERROR_FIELDS.intersection(details or {})):
            raise ValueError(f"These error details would replace the error's own fields: {', '.join(taken)}")
        self.exit_code = exit_code
        self.message = message
        self.next_command = next_command
        self.details = dict(details or {})

    def to_json(self) -> dict[str, str | None]:
        """Return the error as the `error` object of a list row that reports it: `{code, message, next}`."""
        return {"code": self.exit_code.code, "message": self.message, "next": self.next_command}


def can_prompt(no_input: bool, stdin: TextIO | None = None) -> bool:
    """Return whether a prompt is allowed: a terminal on stdin and no `--no-input`.

    Args:
        no_input (bool): Whether `--no-input` was given.
        stdin (TextIO | None): The input stream; `sys.stdin` when None.

    Returns:
        bool: True only when prompting cannot hang a script.
    """
    stream = sys.stdin if stdin is None else stdin
    return not no_input and stream is not None and stream.isatty()


# A Windows argument needs no quotes when every character means itself in cmd, PowerShell and Git Bash alike.
_WINDOWS_PLAIN = re.compile(r"[\w@+=:,./-]+")


def shell_argument(value: str, *, windows: bool | None = None) -> str:
    r"""Return a value as it is typed as one command-line argument, in a message or a suggested next command.

    POSIX quotes the way a POSIX shell reads it. Windows double-quotes only when a shell would read the value
    differently: a backslash (Git Bash reads an unquoted one as an escape), a space, a quote or a character cmd,
    PowerShell or Git Bash interprets; `"C:\\Users\\x"` reads the same in all three.

    Args:
        value (str): The argument.
        windows (bool | None): Quote for Windows; None quotes for the platform this runs on.

    Returns:
        str: The argument, quoted where the platform's shells need it.
    """
    if not (os.name == "nt" if windows is None else windows):
        return shlex.quote(value)
    if _WINDOWS_PLAIN.fullmatch(value):
        return value
    # Windows' argument rules: a quote is escaped, and the backslashes before a quote or the closing quote doubled.
    escaped = re.sub(r'(\\*)"', r'\1\1\\"', value)
    escaped = re.sub(r"(\\+)$", r"\1\1", escaped)
    return f'"{escaped}"'


class Output:
    """Prints a command's one result or one error.

    Args:
        json_mode (bool): Print one JSON object instead of human text.
        command (str | None): The command name, echoed in the JSON envelope.
    """

    def __init__(self, json_mode: bool, command: str | None) -> None:
        self.json_mode = json_mode
        self.command = command

    def result(self, data: dict[str, Any], human: str) -> None:
        """Print a successful result.

        Args:
            data (dict[str, Any]): The machine-readable result.
            human (str): The text a person reads.
        """
        if self.json_mode:
            self._emit({"ok": True, "command": self.command, "result": data})
        else:
            typer.echo(human)

    def progress(self, text: str) -> None:
        """Tell a person what a long step is doing; silent in JSON mode, on stderr so stdout stays the result.

        Args:
            text (str): One line.
        """
        if not self.json_mode:
            typer.echo(text, err=True)

    def error(self, error: CliError) -> int:
        """Print an error and return its exit code.

        Args:
            error (CliError): The failure.

        Returns:
            int: The exit code to end with.
        """
        if self.json_mode:
            body = {
                "code": error.exit_code.code,
                "exit_code": int(error.exit_code),
                "message": error.message,
                "next": error.next_command,
                **error.details,
            }
            self._emit({"ok": False, "command": self.command, "error": body})
        else:
            typer.echo(f"Error: {error.message}", err=True)
            if error.next_command:
                typer.echo(f"Next: {error.next_command}", err=True)
        return int(error.exit_code)

    def _emit(self, envelope: dict[str, Any]) -> None:
        """Print one JSON object on stdout."""
        typer.echo(json.dumps(envelope, indent=2))
