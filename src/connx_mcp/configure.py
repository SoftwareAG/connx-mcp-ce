#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""configure.py - The `configure` command: create or change an environment, or show it masked.

Description:
    On a terminal with no change given, walks every settings group by prompts (prompts.walk).
    Otherwise applies `--unset KEY`, `--set KEY=VALUE` (`--set KEY=` for an explicit empty value) and the
    host-setting flags and `--ask-at-start KEY` (a secret never stored, asked for at every start)
    to an environment, with the lines of a `--from-env-file` import (a flag beats a line, as settings edits),
    validates every value against the settings schema, refuses retired, image-fixed and host-side keys
    by name, warns about unknown keys, and refuses to save while required values are missing, listing
    exactly those. Nothing is written unless the whole change is valid. `--show` prints the environment
    with every secret masked.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from connx_mcp import CLI_NAME, edit, environment, exposure, import_export, prompts, settings
from connx_mcp.demo import bind, remote
from connx_mcp.demo.stack import DemoSettings
from connx_mcp.edit import OriginKind
from connx_mcp.environment import EnvironmentNotFound
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.ports import Terminal

# `--set KEY=-` reads KEY's value from stdin, so a secret never reaches argv or shell history.
FROM_STDIN = "-"
# The remote stack's host settings: a refusal names the flags given for them.
_STACK = ("demo_stack_host", "demo_stack_ports")
# Stack ports stored for no remote stack: binding housekeeping, stored as given like the demo's other values.
_ORPHANED_PORTS = edit.Origin(OriginKind.DEMO, "stack ports without a stack host")


@dataclass
class Changes:
    """What one `configure` call asks for.

    Attributes:
        sets (Sequence[str]): `KEY=VALUE` items from `--set`.
        unsets (Sequence[str]): Keys from `--unset`.
        cdd (Path | None): The CDD file from `--cdd`.
        host (Mapping[str, str]): Host settings by name (`settings.HOST_SCHEMA`), as given to their flags.
        ask_at_start (Sequence[str]): Secrets from `--ask-at-start`: never stored, asked for at every start.
        demo (bind.DemoRequest | None): `--demo` and `--demo-vault`: fill the settings from the demo stack and bind it.
        from_env_file (Path | None): A settings file to import first (`--from-env-file`); the other flags win.
    """

    sets: Sequence[str] = field(default_factory=list)
    unsets: Sequence[str] = field(default_factory=list)
    cdd: Path | None = None
    host: Mapping[str, str] = field(default_factory=dict)
    ask_at_start: Sequence[str] = field(default_factory=list)
    demo: bind.DemoRequest | None = None
    from_env_file: Path | None = None

    @property
    def empty(self) -> bool:
        """Whether no change was asked for."""
        given = (self.sets, self.unsets, self.host_values(), self.ask_at_start, self.demo, self.from_env_file)
        return not any(given)

    def host_values(self) -> dict[str, str]:
        """Return every host setting given, by name; `--cdd` included."""
        return {**self.host, **({"cdd_file": str(self.cdd)} if self.cdd is not None else {})}

    @property
    def demo_refusals(self) -> tuple[edit.Refuse, ...]:
        """Why the demo request alone is refused (`bind.request`); none without one."""
        return self.demo.refusals if self.demo is not None else ()

    def pairs(self) -> list[tuple[str, str | None]]:
        """Return each `--set` item as (key, value); the value is None when the item has no `=`."""
        return [(key, value if equals else None) for key, equals, value in (s.partition("=") for s in self.sets)]


def configure(
    out: Output, given_name: str | None, changes: Changes, show: bool, terminal: Terminal | None = None
) -> None:
    """Run `configure`: walk the settings on a terminal when no change is given, else apply the flags.

    Args:
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        changes (Changes): The requested changes.
        show (bool): Print the environment instead of changing it.
        terminal (Terminal | None): The terminal to prompt on; None when prompting is off.

    Raises:
        CliError: Bad usage, an invalid value, missing required values or a missing environment.
    """
    name = environment.resolve_name(given_name, "configure")
    if show:
        if not changes.empty:
            raise CliError(
                ExitCode.USAGE, "--show only shows the environment; give it without --set, --unset or host flags."
            )
        _show(out, environment.load(name))
        return

    env = environment.load(name)
    created = not env.exists
    if changes.empty and terminal is not None:
        before = copy.deepcopy(env)
        prompts.walk(env, terminal)
        binding = bind.follow(env)
        # Not a no-op: the walk checked each answer alone, and resolving no edits judges the whole result (the
        # cross-field rules, the missing values). Its `changed` compares env with itself, so it is taken from before.
        judged = edit.resolve(env, [])
        _save(out, replace(judged, changed=edit.changed_keys(before, env)), created, binding)
        return
    _apply_flags(out, env, created, changes)


def _apply_flags(out: Output, stored: environment.Environment, created: bool, changes: Changes) -> None:
    """Resolve the import, the flags and the demo stack's edits as settings edits, and save the result.

    Two passes: the first resolves the file's lines and the flags, what the demo fills claimed, and
    the demo binding is planned from its result, the end of a remote binding applied; the second resolves them
    with the demo's edits, again from the environment as stored, so a flag beats the demo and the demo beats the
    file whatever their order. Any refusal of the first pass's edits leaves the demo out.

    Args:
        out (Output): Where the result goes.
        stored (environment.Environment): The environment as stored; never changed.
        created (bool): The environment is new.
        changes (Changes): The requested changes.
    """
    argv_warnings = _secrets_in_argv(changes)
    changes.sets = _read_stdin_item(changes, stored.name)
    imported = import_export.read(changes.from_env_file) if changes.from_env_file is not None else None
    file = import_export.edits(imported) if imported is not None else import_export.FileEdits()
    call = _Call(stored, changes, remote.stack_settings(stored), file)
    requested, first = call.first_pass()
    edits = requested.ordered()
    planned, demo_edits = call.demo_edits(edits, first) if not first.edit_refusals else (None, [])
    # The claims stay when the demo is left out, so a file value it would replace is still not judged.
    final = edit.resolve(stored, [*edits, *demo_edits])
    if changes.demo is None:
        binding = bind.follow(final.env)  # may move the demo-filled URL with the port, so `changed` is taken after
    else:
        binding = planned.bind(final.env) if planned is not None else None
    notes = import_export.notes(imported, final) if imported is not None else []
    checked = replace(final, changed=edit.changed_keys(stored, final.env))
    _save(out, checked, created, binding, [*notes, *argv_warnings])


@dataclass(frozen=True)
class _Requested:
    """This call's edits before the demo's own, by source.

    Attributes:
        file (import_export.FileEdits): The imported file's edits; none without `--from-env-file`.
        flags (list[edit.Edit]): The flags' edits.
        claims (list[edit.Claim]): The demo's claims on what it fills.
    """

    file: import_export.FileEdits
    flags: list[edit.Edit]
    claims: list[edit.Claim]

    def ordered(self) -> list[edit.Edit]:
        """Return every edit in the order their refusals are reported: file, flags, ask-at-start, then claims."""
        return [*self.file.lines, *self.flags, *self.file.asks, *self.claims]


@dataclass(frozen=True)
class _Call:
    """What every pass of one `configure` call reads.

    Attributes:
        stored (environment.Environment): The environment as stored; never changed.
        changes (Changes): The requested changes.
        bound_to (DemoSettings | None): The remote stack the environment was bound to before this call.
        file (import_export.FileEdits): The imported file's edits; none without `--from-env-file`.
    """

    stored: environment.Environment
    changes: Changes
    bound_to: DemoSettings | None
    file: import_export.FileEdits

    def first_pass(self) -> tuple[_Requested, edit.Edited]:
        """Resolve the file's lines and the flags with what the demo fills claimed; return the edits and the result.

        What the stack-port flags mean depends on the stack host the file and the flags leave, so it is read
        from the result: `--stack-port` for the stack the environment stays bound to is merged with the bound
        ports (a service it does not name keeps its port), `--stack-port` without a stack host is refused, and
        stored or imported ports left without one are cleared. What the demo claims depends on it too (a local
        stack's Vault CA file). When either differs from the first guess, the pass runs again.

        Returns:
            tuple[_Requested, edit.Edited]: The edits, and the first pass's result.
        """
        demo = self.changes.demo
        requested = _Requested(self.file, _flag_edits(self.changes), bind.claims(self.stored, demo))
        first = edit.resolve(self.stored, requested.ordered())
        stack_ports = self._stack_port_edits(first.env.host)
        settled = bind.claims(first.env, demo)
        if stack_ports or settled != requested.claims:
            requested = _Requested(self.file, [*requested.flags, *stack_ports], settled)
            first = edit.resolve(self.stored, requested.ordered())
        return requested, first

    def demo_edits(self, edits: list[edit.Edit], first: edit.Edited) -> tuple[bind.Plan | None, list[edit.Edit]]:
        """Return the demo binding's plan and the demo stack's edits, once the first pass has no edit refusal.

        The end of a remote binding is resolved before the read token is judged and the binding planned, so
        both see the settings the ended binding leaves, not the URL or Vault it rendered.

        Args:
            edits (list[edit.Edit]): The first pass's edits.
            first (edit.Edited): The first pass's result.

        Returns:
            tuple[bind.Plan | None, list[edit.Edit]]: The plan (None without `--demo`), and the edits.
        """
        found: list[edit.Edit] = []
        ended = first.env
        if self.bound_to is not None and first.env.host.demo_stack_host is None:
            found += bind.end_remote(first.env, self.bound_to)
            ended = edit.resolve(self.stored, [*edits, *found]).env
            found += bind.drop_read_token(self.stored, ended, self.bound_to)
        if self.changes.demo is None:
            return None, found
        planned, fill = bind.plan(ended, self.stored, self.changes.demo)
        return planned, [*found, *fill]

    def _stack_port_edits(self, host: settings.HostSettings) -> list[edit.Edit]:
        """Return the edits the stack-port flags lead to, given the host settings the flags leave.

        Args:
            host (settings.HostSettings): The host settings after the flags.

        Returns:
            list[edit.Edit]: The merged ports, the `--stack-port` refusal or the clearing of orphaned ports; or
                none.
        """
        changes, bound_to = self.changes, self.bound_to
        ports = changes.host_values().get("demo_stack_ports")
        if _stack_flags_without_demo(changes):
            return []  # refused as a whole by _flag_edits
        if ports and bound_to is not None and host.demo_stack_host == bound_to.stack_host:
            before = f"keycloak={bound_to.keycloak_port},dex={bound_to.dex_port},vault={bound_to.vault_port}"
            try:
                merged = settings.merge_stack_ports(before, ports) or ""
            except ValueError:
                return []  # refused as given by its parser
            flag = edit.Origin(OriginKind.HOST_FLAG, _flag("demo_stack_ports"))
            return [edit.SetHost("demo_stack_ports", merged, flag)]
        if host.demo_stack_host is None and host.demo_stack_ports is not None:
            if ports:
                return [
                    edit.Refuse(
                        f"it needs {_flag('demo_stack_host')}; the ports belong to a demo stack on another machine.",
                        edit.Origin(OriginKind.DEMO_FLAG, _flag("demo_stack_ports")),
                    )
                ]
            return [edit.SetHost("demo_stack_ports", None, _ORPHANED_PORTS)]  # cleared without a word
        return []


def _flag_edits(changes: Changes) -> list[edit.Edit]:
    """Return the flags as settings edits, in the order their refusals are reported.

    Args:
        changes (Changes): The requested changes.

    Returns:
        list[edit.Edit]: `--unset`, `--set`, the host flags, the stack-flag refusal, the demo request's own
            refusals, then `--ask-at-start`.
    """
    given = changes.host_values()
    edits: list[edit.Edit] = [edit.Unset(key, edit.Origin(OriginKind.SET_FLAG, "--unset")) for key in changes.unsets]
    edits += [edit.Set(key, value, edit.Origin(OriginKind.SET_FLAG, "--set")) for key, value in changes.pairs()]
    edits += [edit.SetHost(name, raw, edit.Origin(OriginKind.HOST_FLAG, _flag(name))) for name, raw in given.items()]
    if _stack_flags_without_demo(changes):
        given_flags = [_flag(name) for name in _STACK if given.get(name)]
        edits.append(
            edit.Refuse(
                "a remote demo stack is bound only with --demo keycloak or --demo dex, which renders the settings "
                f"from it. {_flag('demo_stack_host')} '' ends a remote binding.",
                edit.Origin(OriginKind.DEMO_FLAG, " and ".join(given_flags)),
            )
        )
    edits += changes.demo_refusals
    ask = edit.Origin(OriginKind.ASK_FLAG, "--ask-at-start")
    return [*edits, *(edit.AskAtStart(key, ask) for key in changes.ask_at_start)]


def _flag(name: str) -> str:
    """Return the flag that sets a host setting, as `--help` shows it: a refusal names it."""
    return settings.HOST_SCHEMA[name].flag or name


def _stack_flags_without_demo(changes: Changes) -> bool:
    """Return whether `--stack-host` (other than '') or `--stack-port` is given without `--demo`: refused."""
    given = changes.host_values()
    return changes.demo is None and bool(given.get("demo_stack_host") or given.get("demo_stack_ports"))


def _save(
    out: Output,
    checked: edit.Edited,
    created: bool,
    binding: bind.Binding | None = None,
    notes: Sequence[str] = (),
) -> None:
    """Save the resolved environment, or refuse naming every problem; nothing is written on refusal.

    The refusals and missing values are those `edit.resolve` judged over the result, then the demo binding's.
    A demo binding is saved after the environment, so a refused change binds nothing.

    Args:
        out (Output): Where the result goes.
        checked (edit.Edited): The resolved environment, its refusals, warnings, missing values and changed keys.
        created (bool): The environment is new.
        binding (bind.Binding | None): The demo binding to commit once the environment is saved.
        notes (Sequence[str]): Warnings that come before the resolved edits' own.

    Raises:
        CliError: A refusal (USAGE) or a missing required value (INPUT_REQUIRED).
    """
    env, name = checked.env, checked.env.name
    if refusals := [*checked.refusals, *(r.message for r in bind.refusals(env, binding))]:
        raise CliError(ExitCode.USAGE, "\n".join(refusals), f"{CLI_NAME} configure {name} --help")

    if missing := checked.missing:
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Environment {name!r} is missing required values: {', '.join(m.label for m in missing)}. "
            "Nothing was saved.",
            f"{CLI_NAME} configure {name} --no-input {' '.join(m.hint for m in missing)}",
        )

    environment.save(env)
    exposed = exposure.warning(env)
    warnings = [*notes, *checked.warnings, *([exposed] if exposed else [])]
    warnings += binding.warnings if binding is not None else []
    verb = "Created" if created else "Updated"
    lines = [f"{verb} environment {name!r} in {env.directory}.", *(f"Warning: {w}" for w in warnings)]
    data = {
        "environment": name,
        "directory": str(env.directory),
        "created": created,
        "changed": checked.changed,
        "warnings": warnings,
    }
    if binding is not None:
        binding.commit()
        lines += binding.notes
        data["demo"] = binding.to_json()
    out.result(data, "\n".join(lines))


def _secrets_in_argv(changes: Changes) -> list[str]:
    """Return a warning for each secret whose value was given on the command line."""
    return [
        f"{key}'s value was given on the command line, where shell history and the process list keep it. Next "
        f"time pipe it in with --set {key}=- (for example: printf '%s' \"$VALUE\" | {CLI_NAME} configure "
        f"--set {key}=-), or run configure from a keyboard so that it asks you without showing the value."
        for key, value in ((settings.canonical_key(k), v) for k, v in changes.pairs())
        if value not in (None, "", FROM_STDIN) and settings.is_secret(key)
    ]


def _read_stdin_item(changes: Changes, name: str) -> list[str]:
    """Return the `--set` items with the one `KEY=-` item's value read from stdin, one trailing newline dropped.

    Args:
        changes (Changes): The requested changes.
        name (str): The environment name, for the next command.

    Returns:
        list[str]: The `--set` items, `KEY=-` replaced by `KEY=<value read>`.

    Raises:
        CliError: More than one item reads stdin, or stdin is a terminal, where the value would echo as typed.
    """
    keys = [key for key, value in changes.pairs() if value == FROM_STDIN]
    if not keys:
        return list(changes.sets)
    if len(keys) > 1:
        raise CliError(
            ExitCode.USAGE,
            f"Only one --set item can read stdin; {', '.join(f'{k}=-' for k in keys)} all ask for it.",
            f"{CLI_NAME} configure {name} --set {keys[0]}=-",
        )
    if sys.stdin.isatty():
        raise CliError(
            ExitCode.USAGE,
            f"--set {keys[0]}=- reads a pipe or file, but stdin is a keyboard, where the value would show as you "
            "type it. Pipe the value in, or run configure without flags so that it asks you without showing it.",
            f"{CLI_NAME} configure {name}",
        )
    value = sys.stdin.read()
    value = value[:-2] if value.endswith("\r\n") else value.removesuffix("\n")
    return [f"{keys[0]}={value}" if item == f"{keys[0]}={FROM_STDIN}" else item for item in changes.sets]


def _show(out: Output, env: environment.Environment) -> None:
    """Print an environment with every secret masked."""
    if not env.exists:
        raise EnvironmentNotFound(env)
    masked = settings.mask(env.server)
    host = env.host.to_json()
    lines = [f"Environment {env.name!r} ({env.directory})", ""]
    for section, entries in settings.describe(env.server, env.asked_at_start).items():
        lines += [f"[{section}]", *(f"  {entry}" for entry in entries), ""]
    lines.append("[host]")
    lines += [f"  {k} = {v}" for k, v in host.items()] or ["  (none set)"]
    out.result(
        {"environment": env.name, "directory": str(env.directory), "server_settings": masked, "host_settings": host},
        "\n".join(lines),
    )
