#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompts.py - The interactive walk: every settings group once, each prompt derived from the settings schema.

Description:
    `walk` asks for each active server setting, group by group in schema order, then for the CDD
    file. Each prompt shows the current value or the server default and whether it is set. Enter
    keeps the shown value, a value sets it, `-` returns to the server default, `""` sets an
    explicit empty value where the setting allows it, and for a secret `?` asks for it at every
    start instead of storing it; anything else the schema refuses is explained and asked again.
    Secrets are typed hidden and shown as `***hidden***`; ask_secret asks for one at `start`. A setting is
    asked only while its dependency holds on the answers so far, so a group switch left at `none`
    skips its dependent fields and a chosen option asks only the fields that name it. Prompts run
    on a Terminal: the real one, or a given prompt_toolkit input and output (tests pass a pipe).

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from collections.abc import Callable

import questionary
from prompt_toolkit import print_formatted_text

from connx_mcp import CLI_NAME, settings
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode, shell_argument
from connx_mcp.ports import Terminal

KEEP = ""
TO_DEFAULT = "-"
EXPLICIT_EMPTY = '""'
ASK_AT_START = "?"
LEGEND = (
    'Enter keeps the shown value, a value sets it, - returns to the server default, "" sets an explicit empty '
    "value where allowed, and ? asks for a secret at every start instead of storing it. Ctrl-C cancels without "
    "saving."
)
# Server settings that name a path inside the container: the walk asks for the file on this machine instead
# (a host setting), and `start` mounts it where the server setting points.
ASKED_ON_HOST = {"VAULT_CACERT": "vault_ca_file"}


def walk(env: Environment, terminal: Terminal) -> None:
    """Walk every settings group and the CDD file, changing the environment in memory only.

    Args:
        env (Environment): The environment to change; nothing is saved here.
        terminal (Terminal): Where to prompt.

    Raises:
        CliError: The administrator cancelled (Ctrl-C) or the input ended.
    """
    with terminal.session():
        try:
            _say(LEGEND)
            for group in settings.Group:
                rows = [s for s in settings.SCHEMA.values() if s.group is group and s.asked]
                if rows:
                    _say(f"\n[{settings.GROUP_TITLES.get(group, str(group))}]")
                    _walk_group(env, rows)
            _say("\n[Host settings]")
            _ask_cdd(env)
        except KeyboardInterrupt, EOFError:
            raise CliError(
                ExitCode.CANCELLED,
                "Configuration was cancelled; nothing was saved.",
                f"{CLI_NAME} configure {env.name}",
            ) from None


def _walk_group(env: Environment, rows: list[settings.Setting]) -> None:
    """Ask a group's active settings, offer to drop what a switch made inactive, and re-ask while a rule breaks.

    A cross-field rule is checked as the walk leaves the group, so one mistake never discards the whole walk.
    """
    keys = {s.key for s in rows}
    while True:
        for setting in rows:
            if setting.active(env.server):
                if setting.key in ASKED_ON_HOST:
                    _ask_host_file(env, ASKED_ON_HOST[setting.key])
                else:
                    _ask(env, setting)
        _offer_to_drop(env, rows)
        refusals = [p.message for p in settings.check(env.server, env.host) if p.error and p.key in keys]
        if not refusals:
            return
        for message in refusals:
            _say(f"  {message}")
        _say("  Answer this group again.")


def _offer_to_drop(env: Environment, rows: list[settings.Setting]) -> None:
    """Offer to drop the stored values of a group that have no effect now, e.g. a former provider's secret."""
    inactive = [s for s in rows if not s.active(env.server)]
    stale = [s.key for s in inactive if s.key in env.server]
    stale_host = [ASKED_ON_HOST[s.key] for s in inactive if s.key in ASKED_ON_HOST]
    stale_host = [name for name in stale_host if getattr(env.host, name) is not None]
    if not stale and not stale_host:
        return
    names = [*stale, *(settings.HOST_SCHEMA[name].flag or name for name in stale_host)]
    drop = questionary.confirm(
        f"Drop {', '.join(names)}? They have no effect with the choice above.", default=True, auto_enter=False
    ).unsafe_ask()
    if drop:
        for key in stale:
            env.server.pop(key)
        for name in stale_host:
            setattr(env.host, name, None)


def _say(text: str) -> None:
    """Print a line on the prompting terminal."""
    print_formatted_text(text)


def _quoted(value: str) -> str:
    """Return a value in single quotes as typed: a repr would double a Windows path's backslashes."""
    return f"'{value}'"


def state(setting: settings.Setting, values: dict[str, str], asked: bool = False) -> str:
    """Describe a setting's shown value and whether it is set, never revealing a secret.

    Args:
        setting (settings.Setting): The setting.
        values (dict[str, str]): The server settings so far.
        asked (bool): The secret is asked for at every start instead of stored.

    Returns:
        str: E.g. `'/api' (set)`, `'300' (not set: server default)`, `(asked at start)` or `(not set, required)`.
    """
    if asked:
        return "(asked at start)"
    if setting.key in values:
        value = values[setting.key]
        if setting.secret:
            return f"{settings.HIDDEN} (set)"
        return "'' (set: explicitly empty)" if value == "" else f"{_quoted(value)} (set)"
    if setting.default is not None:
        return f"{_quoted(setting.default)} (not set: server default)"
    return "(not set, required)" if setting.is_required(values) else "(not set, no default)"


def _ask(env: Environment, setting: settings.Setting) -> None:
    """Ask for one setting and apply the answer to the environment; a secret may be marked ask-at-start."""
    values = env.server
    marked = set(env.asked_at_start)
    asked = setting.key in marked
    _say(f"  {setting.summary}")
    message = f"{setting.key} {state(setting, values, asked)}"
    if setting.kind is settings.Kind.CHOICE:
        _ask_choice(values, setting, message)
        return
    ask = questionary.password if setting.secret else questionary.text
    answer = _until_accepted(lambda: ask(message).unsafe_ask(), lambda raw: refusal(setting, values, raw, asked))
    if answer == KEEP:
        return
    if answer == ASK_AT_START and setting.secret:
        values.pop(setting.key, None)
        marked.add(setting.key)
    else:
        marked.discard(setting.key)
        if answer == TO_DEFAULT:
            values.pop(setting.key, None)
        else:
            values[setting.key] = "" if answer == EXPLICIT_EMPTY else answer
    env.host.ask_at_start = sorted(marked) or None


def ask_secret(terminal: Terminal, key: str) -> str:
    """Ask for one secret on a terminal, typed hidden, until the schema accepts it; for `start`.

    Args:
        terminal (Terminal): Where to prompt.
        key (str): The secret's server setting.

    Returns:
        str: The value; Enter gives an explicit empty value where the setting allows one.

    Raises:
        CliError: The administrator cancelled (Ctrl-C) or the input ended.
    """
    setting = settings.SCHEMA.get(key)
    with terminal.session():
        try:
            if setting is not None:
                _say(f"  {setting.summary}")
            return _until_accepted(
                lambda: questionary.password(f"{key} (asked at start)").unsafe_ask(),
                lambda answer: _named(key, settings.value_problem(key, answer)),
            )
        except KeyboardInterrupt, EOFError:
            raise CliError(ExitCode.CANCELLED, f"No value for {key} was given; nothing was started.") from None


def _until_accepted(ask: Callable[[], str], refuse: Callable[[str], str | None]) -> str:
    """Ask until an answer is accepted, explaining each refusal before asking again on a fresh line."""
    while True:
        answer = ask()
        reason = refuse(answer)
        if reason is None:
            return answer
        _say(f"  {reason}")


def refusal(setting: settings.Setting, values: dict[str, str], answer: str, asked: bool = False) -> str | None:
    """Return why a typed answer cannot be taken, or None.

    Args:
        setting (settings.Setting): The setting asked for.
        values (dict[str, str]): The server settings so far.
        answer (str): What was typed: "" keeps, "-" returns to the default, '""' is an explicit empty value,
            "?" asks for a secret at every start.
        asked (bool): The secret is already asked for at every start, so Enter keeps that.

    Returns:
        str | None: The explanation shown before asking again; None accepts the answer.
    """
    lacks_default = setting.is_required(values) and setting.default is None
    if answer == ASK_AT_START and setting.secret:
        return None
    if answer == KEEP:
        return _required(setting) if lacks_default and setting.key not in values and not asked else None
    if answer == TO_DEFAULT:
        return _required(setting) if lacks_default else None
    if answer == EXPLICIT_EMPTY:
        if setting.empty_ok:
            return None
        back = f"the server default {_quoted(setting.default)}" if setting.default is not None else "no value"
        return settings.worded(setting.key, f"it cannot be empty; type - to return to {back}, or give a value.")
    return _named(setting.key, settings.value_problem(setting.key, answer))


def _named(key: str, reason: str | None) -> str | None:
    """Return a value's refusal as `<key>: <reason>`, the form every settings refusal takes; None stays None."""
    return settings.worded(key, reason) if reason else None


def _required(setting: settings.Setting) -> str:
    """Explain that a setting needs a value."""
    empty = f', or "" when {setting.empty_meaning}' if setting.empty_ok else ""
    asked = ", or ? to be asked for it at every start" if setting.secret else ""
    return settings.worded(setting.key, f"it is required; type a value{empty}{asked}.")


def _path_refusal(row: settings.HostSetting, answer: str) -> str | None:
    """Return why a typed path cannot be taken, as the host setting's flag with the path would be refused; or None.

    Args:
        row (settings.HostSetting): The host setting asked for.
        answer (str): The path typed.

    Returns:
        str | None: The refusal; None accepts the path.
    """
    try:
        row.parse(answer)
    except ValueError as reason:
        return settings.worded(f"{row.flag} {shell_argument(answer)}", f"{reason}.")
    return None


def _ask_choice(values: dict[str, str], setting: settings.Setting, message: str) -> None:
    """Ask for a choice from a menu; the menu also offers the way back to the default once one is set."""
    choices = [questionary.Choice(c, value=c) for c in setting.choices]
    if setting.key in values and not (setting.is_required(values) and setting.default is None):
        back = "the server default" if setting.default is not None else "unset"
        choices.append(questionary.Choice(f"- (back to {back})", value=TO_DEFAULT))
    shown = values.get(setting.key, setting.default)
    default = shown if shown in setting.choices else None
    answer = questionary.select(message, choices=choices, default=default).unsafe_ask()
    if answer == TO_DEFAULT:
        values.pop(setting.key, None)
    elif answer != shown or setting.key in values:
        values[setting.key] = answer


def _ask_cdd(env: Environment) -> None:
    """Ask for the CDD file the environment mounts read-only."""
    current = env.host.cdd_file
    row = settings.HOST_SCHEMA["cdd_file"]
    _say(f"  {row.summary} ({row.flag})")
    message = f"CDD file {_quoted(current)} (set)" if current else "CDD file (not set, required)"

    def cdd_refusal(answer: str) -> str | None:
        """Accept Enter on a set file, or the path of an existing file."""
        if answer == KEEP:
            return None if current else settings.worded(f"{row.flag}", "it is required; type its path.")
        return _path_refusal(row, answer)

    answer = _until_accepted(lambda: questionary.text(message).unsafe_ask(), cdd_refusal)
    if answer != KEEP:
        env.host.cdd_file = row.parse(answer)


def _ask_host_file(env: Environment, name: str) -> None:
    """Ask for an optional file on this machine that a host setting holds; - removes it."""
    row = settings.HOST_SCHEMA[name]
    current = getattr(env.host, name)
    label = row.summary.split(",")[0]
    _say(f"  {row.summary} ({row.flag})")
    message = f"{label} {_quoted(current)} (set)" if current else f"{label} (not set)"

    def file_refusal(answer: str) -> str | None:
        """Accept Enter, -, or the path of an existing file."""
        if answer in (KEEP, TO_DEFAULT):
            return None
        return _path_refusal(row, answer)

    answer = _until_accepted(lambda: questionary.text(message).unsafe_ask(), file_refusal)
    if answer == TO_DEFAULT:
        setattr(env.host, name, None)
    elif answer != KEEP:
        setattr(env.host, name, row.parse(answer))
