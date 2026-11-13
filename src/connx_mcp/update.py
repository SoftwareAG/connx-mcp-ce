#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update.py - `download` and `update`: pull an environment's image, and move its pin deliberately.

Description:
    `download` pulls one environment's image reference, or with `--all` every distinct reference
    the environments use, once each; a pinned environment keeps its digest and is told to update,
    and one whose stored image names no registry is listed as refused with its repair.
    `update` pulls, refuses an image older than this CLI supports (keeping the old pin), re-pins
    the digest and version and offers a restart when the server is running: `--restart`
    restarts, `--no-restart` does not, otherwise a terminal is asked and a script is told the
    command. A restart reads every ask-at-start secret (terminal, `--secret-env`, `--secret-stdin`)
    before it stops the server, so a missing one leaves the server running on the old image. The
    commands live apart from image.py because they use the server lifecycle, which itself uses
    image.py.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import questionary

from connx_mcp import CLI_NAME, environment, image, lifecycle, start_inputs
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.ports import Ports, Terminal


def configured_environments() -> list[Environment]:
    """Return every saved environment under the base directory, by name.

    Returns:
        list[Environment]: The environments.
    """
    base = environment.base_directory()
    if not base.is_dir():
        return []
    names = sorted(p.name for p in base.iterdir() if p.is_dir() and environment.NAME_PATTERN.fullmatch(p.name))
    return [env for env in map(environment.load, names) if env.exists]


# --- download -------------------------------------------------------------------------------------------


@dataclass
class _SharedPull:
    """One distinct reference `download --all` pulls once for every environment that uses it.

    Attributes:
        chosen (image.ImageChoice): The first environment's choice; a failed pull names its parts' sources.
        users (list[Environment]): The environments that use the reference.
        notices (list[str]): The ignored-variable notices of those environments.
    """

    chosen: image.ImageChoice
    users: list[Environment] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def download(ports: Ports, out: Output, given_name: str | None, every: bool) -> None:
    """Run `download`: pull one environment's image, or every distinct image with `--all`.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        every (bool): Pull for every environment.

    Raises:
        CliError: A name with `--all`, an unknown environment, a named environment's stored image naming no
            registry (with `--all` it is listed as refused instead), or a failed pull.
    """
    if every and given_name:
        raise CliError(ExitCode.USAGE, "Name an environment or give --all, not both.", f"{CLI_NAME} download --all")
    envs = configured_environments() if every else [lifecycle.load_configured(given_name, "download")]
    shared: dict[str, _SharedPull] = {}
    # The environment list's refused row shape: with --all only the environment whose stored image names no
    # registry is refused; the rest are pulled.
    refused: list[dict[str, Any]] = []
    for env in envs:
        if every and (refusal := image.refused_reference(env)) is not None:
            refused.append({"environment": env.name, "error": refusal.to_json()})
            continue
        chosen = image.choice(env)
        entry = shared.setdefault(chosen.reference, _SharedPull(chosen))
        entry.users.append(env)
        if notice := image.ignored_notice(chosen):
            entry.notices.append(notice)
    images: list[dict[str, Any]] = []
    lines: list[str] = []
    for ref, entry in shared.items():
        users = entry.users
        image.pull(ports.docker, out, entry.chosen)
        local = image.inspect(ports.docker, ref)
        digest, version = (local.digest, local.version) if local else (None, None)
        stay = [e.name for e in users if e.host.image_digest and e.host.image_digest != digest]
        warnings = image.warnings(version, ref, *entry.notices)
        images.append(
            {
                "reference": ref,
                "environments": [e.name for e in users],
                "digest": digest,
                "version": version,
                "update_available": stay,
                "warnings": warnings,
            }
        )
        lines.append(f"Downloaded {ref} (version {version or 'unknown'}) for {', '.join(e.name for e in users)}.")
        lines += [f"  {name!r} stays on its pinned image; `{CLI_NAME} update {name}` moves it." for name in stay]
        lines += [f"Warning: {w}" for w in warnings]
    for row in refused:
        lines += ["", row["error"]["message"], f"Next: {row['error']['next']}"]
    out.result(
        {"images": images, "refused": refused},
        "\n".join(lines).strip() or "No environments are configured; nothing to download.",
    )


# --- update ---------------------------------------------------------------------------------------------


def _wants_restart(env: Environment, restart: bool | None, terminal: Terminal | None, version: str | None) -> bool:
    """Decide the restart: the flag, else the administrator's answer on a terminal, else no."""
    if restart is not None:
        return restart
    if terminal is None:
        return False
    with terminal.session():
        try:
            question = f"Restart {lifecycle.container_name(env)} now to run version {version or 'unknown'}?"
            return bool(questionary.confirm(question, default=True).unsafe_ask())
        except KeyboardInterrupt, EOFError:
            return False


def update(
    ports: Ports,
    out: Output,
    given_name: str | None,
    restart: bool | None,
    terminal: Terminal | None,
    options: lifecycle.StartOptions | None = None,
) -> None:
    """Run `update`: pull, re-pin and offer a restart.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        restart (bool | None): `--restart` / `--no-restart`; None asks on a terminal.
        terminal (Terminal | None): Where to ask; None when prompting is not allowed.
        options (lifecycle.StartOptions | None): The restart's ask-at-start secret sources; defaults when None.
    """
    options = options or lifecycle.StartOptions()
    env = lifecycle.load_configured(given_name, "update")
    chosen = image.choice(env)
    ref = chosen.reference
    image.pull(ports.docker, out, chosen)
    new = image.resolve(ports.docker, chosen, env.name)
    image.refuse_unsupported(new.version, new.digest, None, chosen)
    warnings = image.warnings(new.version, new.digest, image.ignored_notice(chosen))
    previous = {"digest": env.host.image_digest, "version": env.host.image_version}
    changed = new.digest != env.host.image_digest
    if changed or new.reference != env.host.image_pinned_reference:
        image.record_pin(env, new)
        environment.save_host(env)
    state = lifecycle.container_state(ports, lifecycle.container_name(env)) if changed else None
    running = state is not None and state.status == "running"
    restarted = False
    next_command: str | None = None
    if changed and running and _wants_restart(env, restart, terminal, new.version):
        _read_secrets(env, options, terminal, new.version)
        run = lifecycle.restart_environment(env, ports, out, options, terminal)
        warnings += [w for w in run.warnings if w not in warnings]
        restarted = True
    elif changed:
        next_command = f"{CLI_NAME} {'restart' if running else 'start'} {env.name}"
    data = {
        "environment": env.name,
        "reference": ref,
        "previous": previous,
        "pinned": {"digest": new.digest, "version": new.version},
        "changed": changed,
        "restarted": restarted,
        "next": next_command,
        "warnings": warnings,
    }
    out.result(data, _update_text(env, data))


def _read_secrets(
    env: Environment, options: lifecycle.StartOptions, terminal: Terminal | None, version: str | None
) -> None:
    """Read the restart's ask-at-start secrets before the stop; a refusal says the server still runs the old image."""
    try:
        start_inputs.read_ahead(env, options.inputs, terminal, "restart")
    except CliError as error:
        raise CliError(
            error.exit_code,
            f"{error.message} {env.name!r} is pinned to version {version or 'unknown'} but was not restarted; "
            "it keeps running the previous image.",
            error.next_command,
        ) from None


def _update_text(env: Environment, data: dict[str, Any]) -> str:
    """Return an update's result as text."""
    pinned = data["pinned"]
    if not data["changed"]:
        lines = [
            f"{env.name!r} already runs the newest {data['reference']} (version {pinned['version'] or 'unknown'})."
        ]
    else:
        was = data["previous"]["version"] or "unpinned"
        lines = [f"Pinned {env.name!r} to version {pinned['version'] or 'unknown'} (was {was}): {pinned['digest']}"]
        if data["restarted"]:
            lines.append(f"Restarted {lifecycle.container_name(env)}; it is healthy.")
        elif data["next"]:
            lines.append(f"It takes effect at the next start: {data['next']}")
    return "\n".join([*lines, *(f"Warning: {w}" for w in data["warnings"])])
