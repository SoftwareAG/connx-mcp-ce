#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_inputs.py - What one start supplies beyond the stored environment: ask-at-start secrets and overrides.

Description:
    A secret marked ask-at-start is never stored; `start` reads it from a named environment
    variable (`--secret-env KEY=VARIABLE`), from stdin (`--secret-stdin KEY`) or, on a terminal,
    from a masked prompt, and fails naming it when prompting is off. `--cdd`, `--certs` and
    `--cdd-username` change one run only; another login needs its password for that run too. The
    values reach the container through docker's process environment (ComposeSpec.passed), never a
    file. `restart` and `update` read every value before they stop the server (`read_ahead`), so a
    missing, refused or cancelled secret leaves it running. The start steps here are registered in
    lifecycle.START_STEPS; `started-with.json` records the overrides a container runs with,
    including a stored secret replaced for one run (by key, never its value), so `status` reports
    them as drift (drift.py), and `start` warns when the environment's files have become readable
    by others.

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
import sys
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from connx_mcp import CLI_NAME, certs, compose, environment, prompts, settings
from connx_mcp.environment import SERVER_FILE, Environment
from connx_mcp.output import CliError, ExitCode, shell_argument
from connx_mcp.ports import Terminal

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun

STARTED_WITH_FILE = "started-with.json"
PASSWORD = "CDD_PASSWORD"
USERNAME = "CDD_USERNAME"


@dataclass
class StartInputs:
    """What one `start` (or `restart`) supplies beyond the stored environment.

    Attributes:
        cdd (Path | None): A CDD file mounted for this run instead of the stored one.
        certs (Path | None): A certificate directory bind-mounted for this run.
        cdd_username (str | None): A CONNX login for this run; its password is then needed for this run too.
        secret_env (Sequence[str]): `KEY=VARIABLE` items: read secret KEY from environment variable VARIABLE.
        stdin_key (str | None): The secret whose value is read from stdin, as its canonical key.
        supplied (dict[str, str] | None): The values already read, before a restart's stop; None reads them
            during the start.
    """

    cdd: Path | None = None
    certs: Path | None = None
    cdd_username: str | None = None
    secret_env: Sequence[str] = field(default_factory=list)
    stdin_key: str | None = None
    supplied: dict[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Canonicalise the stdin key once, so every comparison uses the schema's spelling."""
        self.stdin_key = settings.canonical_key(self.stdin_key) if self.stdin_key else None

    def overrides(self, asked: Collection[str] = ()) -> list[str]:
        """Return the one-run overrides, as their flags, for warnings and `status`.

        Args:
            asked (Collection[str]): The ask-at-start keys; a value given for one of them is no override.

        Returns:
            list[str]: Each override; a secret by its key, never its value.
        """
        given = [("--cdd", self.cdd), ("--certs", self.certs), ("--cdd-username", self.cdd_username)]
        flags = [f"{flag} {value}" for flag, value in given if value is not None]
        secrets = [("--secret-env", settings.canonical_key(item.partition("=")[0])) for item in self.secret_env]
        secrets += [("--secret-stdin", self.stdin_key)] if self.stdin_key else []
        return flags + [f"{flag} {key} (value not shown)" for flag, key in secrets if key not in asked]


def cdd_file(env: Environment, inputs: StartInputs) -> str | None:
    """Return the CDD file this run mounts: the `--cdd` override, else the stored one.

    Args:
        env (Environment): The environment.
        inputs (StartInputs): The run's inputs.

    Returns:
        str | None: The absolute path, or None when neither is given.
    """
    return str(inputs.cdd.expanduser().absolute()) if inputs.cdd is not None else env.host.cdd_file


# --- start steps --------------------------------------------------------------------------------------------


def check_overrides(run: StartRun) -> None:
    """Refuse a `--certs` that breaks the server's certificate rules or a `--cdd-username` it cannot take."""
    inputs = run.options.inputs
    if inputs.certs is not None:
        directory = inputs.certs.expanduser().absolute()
        primary = certs.primary_host(run.env.server.get("MCP_TLS_HOSTS"))
        # The origin names the directory, so a missing one is not named again.
        if directory.is_dir():
            reason = certs.directory_problem(directory, primary, datetime.now(UTC))
        else:
            reason = "no such directory"
        if reason:
            raise CliError(
                ExitCode.START_OVERRIDE_INVALID,
                settings.worded(f"--certs {shell_argument(str(inputs.certs))}", f"{reason}."),
                f"{CLI_NAME} start {run.env.name} --certs <directory holding one .crt/.key pair, or empty>",
            )
    if inputs.cdd_username is not None and (problem := settings.value_problem(USERNAME, inputs.cdd_username)):
        raise CliError(ExitCode.USAGE, settings.worded("--cdd-username", problem))


def warn_widened_permissions(run: StartRun) -> None:
    """Warn when the environment's directory or server settings file has become accessible to other users."""
    if os.name == "nt":
        return
    for path, owner_only in ((run.env.directory, 0o700), (run.env.directory / SERVER_FILE, 0o600)):
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            run.warnings.append(
                f"{path} is open to other users (mode {mode:04o}) and holds stored secrets; "
                f"restrict it with: chmod {owner_only:o} {path}"
            )


def sources_for(
    env: Environment, inputs: StartInputs, terminal: Terminal | None, command: str = "start"
) -> dict[str, str | None]:
    """Return the secrets a start reads and where from, refusing before anything is read when one has no source.

    Args:
        env (Environment): The environment.
        inputs (StartInputs): The run's inputs.
        terminal (Terminal | None): Where to prompt; None when prompting is off.
        command (str): The command a refusal's next step names (`start` or `restart`).

    Returns:
        dict[str, str | None]: Each secret and its environment variable; None reads stdin or the terminal.

    Raises:
        CliError: A source is malformed or names a non-secret, or a secret has no source and prompting is off.
    """
    from_env = _secret_sources(env, inputs)
    marked = sorted(env.asked_at_start)
    needed = [k for k in marked if k not in settings.SCHEMA or settings.SCHEMA[k].active(env.server)]
    if inputs.cdd_username is not None and PASSWORD not in needed:
        needed.append(PASSWORD)
    wanted = list(dict.fromkeys([*needed, *from_env, *([inputs.stdin_key] if inputs.stdin_key else [])]))
    lacking = [k for k in wanted if k not in from_env and k != inputs.stdin_key and terminal is None]
    if lacking:
        why = "another --cdd-username needs its password, and " if inputs.cdd_username is not None else ""
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Environment {env.name!r} asks for {', '.join(lacking)} at start; {why}the command cannot ask "
            "(--no-input, --json, or its input does not come from a keyboard). Give each from an environment "
            "variable or stdin.",
            f"{CLI_NAME} {command} {env.name} " + " ".join(f"--secret-env {k}=<VARIABLE>" for k in lacking),
        )
    return {key: from_env.get(key) for key in wanted}


def read_values(
    env: Environment, inputs: StartInputs, terminal: Terminal | None, command: str = "start"
) -> dict[str, str]:
    """Read and check this run's values: ask-at-start secrets, a secret given for this run, and the login override.

    Every source is checked before any is read, so a missing secret fails before a prompt or a Docker call.

    Args:
        env (Environment): The environment.
        inputs (StartInputs): The run's inputs.
        terminal (Terminal | None): Where to prompt; None when prompting is off.
        command (str): The command a refusal's next step names (`start` or `restart`).

    Returns:
        dict[str, str]: Each server setting and its value for this run.

    Raises:
        CliError: A source is missing or unusable, a value is refused, or the prompt was cancelled.
    """
    sources = sources_for(env, inputs, terminal, command)
    if inputs.stdin_key is not None and sys.stdin.isatty():
        raise CliError(
            ExitCode.USAGE,
            f"--secret-stdin {inputs.stdin_key} reads a pipe or file, but stdin is a keyboard, where the value "
            "would show as you type it. Leave --secret-stdin out so that the command asks you for it without "
            "showing it, or pipe it in.",
            f"{CLI_NAME} {command} {env.name}",
        )
    values: dict[str, str] = {} if inputs.cdd_username is None else {USERNAME: inputs.cdd_username}
    for key, variable in sources.items():
        values[key] = _read(env, inputs, terminal, key, variable, command)
    return values


def read_ahead(env: Environment, inputs: StartInputs, terminal: Terminal | None, command: str) -> None:
    """Read this run's values now, before a restart stops the server; the start then uses them unread.

    Args:
        env (Environment): The environment.
        inputs (StartInputs): The run's inputs; their `supplied` values are set.
        terminal (Terminal | None): Where to prompt; None when prompting is off.
        command (str): The command a refusal's next step names.
    """
    inputs.supplied = read_values(env, inputs, terminal, command)


def supply_values(run: StartRun) -> None:
    """Collect this run's values, unless a restart read them before its stop."""
    inputs = run.options.inputs
    supplied = inputs.supplied if inputs.supplied is not None else read_values(run.env, inputs, run.terminal)
    run.passed.update(supplied)


def _secret_sources(env: Environment, inputs: StartInputs) -> dict[str, str]:
    """Return `--secret-env` as {key: variable}, refusing a malformed item, a non-secret or a key given twice."""
    sources: dict[str, str] = {}
    asked = env.asked_at_start
    for item in inputs.secret_env:
        key, equals, variable = item.partition("=")
        key = settings.canonical_key(key)
        if not equals or not settings.KEY_PATTERN.fullmatch(variable):
            raise CliError(ExitCode.USAGE, f"--secret-env {key} needs the form KEY=VARIABLE.")
        sources[key] = variable
    stdin = [] if inputs.stdin_key is None else [inputs.stdin_key]
    for key in [*sources, *stdin]:
        if key not in asked and not settings.is_secret(key):
            raise CliError(
                ExitCode.USAGE,
                f"{key} is not a secret, so it is not read at start; store it with configure --set {key}=...",
                f"{CLI_NAME} configure {env.name} --set {key}=...",
            )
    if inputs.stdin_key in sources:
        raise CliError(ExitCode.USAGE, f"{inputs.stdin_key} is given both by --secret-env and --secret-stdin.")
    return sources


def _read(
    env: Environment,
    inputs: StartInputs,
    terminal: Terminal | None,
    key: str,
    variable: str | None,
    command: str,
) -> str:
    """Read one value from its environment variable, stdin or the terminal, and check it against the schema."""
    if variable is not None:
        value = os.environ.get(variable)
        if value is None:
            raise CliError(
                ExitCode.SECRET_SOURCE_EMPTY,
                f"--secret-env {key}={variable} names an environment variable that is not set.",
                f"{CLI_NAME} {command} {env.name} --secret-env {key}=<VARIABLE>",
            )
    elif key == inputs.stdin_key:
        value = sys.stdin.read()
        value = value[:-2] if value.endswith("\r\n") else value.removesuffix("\n")
    else:
        assert terminal is not None
        return prompts.ask_secret(terminal, key)
    if problem := settings.value_problem(key, value):
        raise CliError(ExitCode.USAGE, settings.worded(key, problem))
    return value


def apply_overrides(run: StartRun) -> None:
    """Change the render input for this run: the CDD and certificate mounts, and the values passed to it."""
    spec, inputs = run.require_spec(), run.options.inputs
    if inputs.certs is not None:
        # The rest of this run (the TLS hooks, the start's status report) sees the overriding directory too;
        # host.json was saved before this step, so the stored directory is kept.
        run.env.host.cert_dir = str(inputs.certs.expanduser().absolute())
    for at, mount in enumerate(spec.mounts):
        if mount.target == compose.CDD_TARGET:
            mount.source = cdd_file(run.env, inputs) or mount.source
        elif mount.target == compose.CERTS_TARGET and inputs.certs is not None:
            spec.mounts[at] = compose.certificate_mount(run.env.host.cert_dir)
    spec.passed.update(run.passed)
    if overrides := inputs.overrides(run.env.asked_at_start):
        run.warnings.append(
            f"This run uses one-run overrides ({', '.join(overrides)}); the container keeps them until the next "
            "start, also across reboots."
        )


def record_overrides(run: StartRun) -> None:
    """Record which overrides the container now runs with, for `status`; a run without any clears the record."""
    path = run.env.directory / STARTED_WITH_FILE
    if overrides := run.options.inputs.overrides(run.env.asked_at_start):
        record: dict[str, object] = {"overrides": overrides}
        if run.options.inputs.certs is not None:
            # apply_overrides made it absolute; status reads the certificate the container was started with.
            record["cert_dir"] = run.env.host.cert_dir
        environment.write_private(path, json.dumps(record, indent=2) + "\n")
    else:
        path.unlink(missing_ok=True)


# --- status facts -------------------------------------------------------------------------------------------


def started_with(env: Environment) -> list[str]:
    """Return the one-run overrides the environment's container was last started with.

    Args:
        env (Environment): The environment.

    Returns:
        list[str]: The overrides as flags; empty when it runs with the stored settings.
    """
    path = env.directory / STARTED_WITH_FILE
    if not path.is_file():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("overrides", []))


def started_cert_dir(env: Environment) -> str | None:
    """Return the certificate directory a `--certs` override mounted into the running container, or None.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The absolute directory of the last start's `--certs`; None when it used the stored settings.
    """
    path = env.directory / STARTED_WITH_FILE
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8")).get("cert_dir")
    return value if isinstance(value, str) else None
