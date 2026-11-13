#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_export.py - Import a settings file into an environment, and export an environment to one.

Description:
    The file is the server's env-file format (what compose reads through `env_file:`): `KEY=VALUE` lines,
    optional `export ` prefix, blank lines and `#` comments, values plain, 'single-quoted' (literal) or
    "double-quoted" (backslash escapes, `$$` for `$`); a quoted value may span lines. Two comment forms carry
    what server settings cannot: `# host <name> = <JSON>` (a host setting) and `# secret <KEY>: ...` (a secret
    left out, which import marks ask-at-start). Import validates every value with the rules `--set` uses and
    reports each problem with its line; export writes server settings in the form the environment stores them,
    secrets left out unless asked for, host settings as comments.

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from connx_mcp import CLI_NAME, edit, environment, settings
from connx_mcp.environment import EnvironmentNotFound
from connx_mcp.output import CliError, ExitCode, Output

_ASSIGNMENT = re.compile(r"(?:export\s+)?([^=\s]+)\s*=\s*(.*)")
_HOST_COMMENT = re.compile(r"#\s*host\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_SECRET_COMMENT = re.compile(r"#\s*secret\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::.*)?")
_INLINE_COMMENT = re.compile(r"\s+#.*")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


@dataclass
class Imported:
    """What a settings file holds, each item with the line it came from.

    Attributes:
        path (Path): The file.
        server (list[tuple[str, str, int]]): (canonical key, value, line) per setting, in file order.
        host (list[tuple[str, Any, int]]): (name, JSON value, line) per `# host` comment.
        secrets (list[tuple[str, int]]): (canonical key, line) per `# secret` comment.
        errors (list[tuple[str, int]]): (reason, line) per line that is not a setting.
        notes (dict[int, str]): By line, why a value is kept as written where compose would read it differently;
            reported only when the line's value is stored.
    """

    path: Path
    server: list[tuple[str, str, int]] = field(default_factory=list)
    host: list[tuple[str, Any, int]] = field(default_factory=list)
    secrets: list[tuple[str, int]] = field(default_factory=list)
    errors: list[tuple[str, int]] = field(default_factory=list)
    notes: dict[int, str] = field(default_factory=dict)


def read(path: Path) -> Imported:
    """Read and parse a settings file; values are not judged yet.

    Args:
        path (Path): The file to import.

    Returns:
        Imported: Its settings, host comments, secret markers and syntax errors.

    Raises:
        CliError: The file cannot be read or is not UTF-8 (import_file_unreadable).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as reason:
        raise CliError(
            ExitCode.IMPORT_FILE_UNREADABLE,
            f"Cannot read the settings file {path}: {reason}. Nothing was changed.",
            f"{CLI_NAME} configure --help",
        ) from reason
    return parse(text, path)


def parse(text: str, path: Path) -> Imported:
    """Parse the text of a settings file.

    Lines end only at a line feed (a trailing carriage return is dropped): any other line-break character stays
    inside the value it is in, so the value check refuses it instead of it starting another setting.

    Args:
        text (str): The file content.
        path (Path): The file, for messages.

    Returns:
        Imported: Its settings, host comments, secret markers and syntax errors.
    """
    found = Imported(path)
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line.startswith("#"):
            _comment(found, line, number)
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            found.errors.append(("the line is not a KEY=VALUE setting.", number))
            continue
        key, rest = settings.canonical_key(match.group(1)), match.group(2)
        if rest[:1] in ("'", '"'):
            value, index, problem, lone_dollar = _quoted(rest, lines, index)
            if problem is not None:
                found.errors.append((f"{key} {problem}", number))
                continue
        else:
            raw = _INLINE_COMMENT.sub("", rest).strip()
            value, lone_dollar = raw.replace("$$", "$"), "$" in raw.replace("$$", "")
        if lone_dollar:
            found.notes[number] = (
                "it holds a lone $, which compose would expand; it is imported as written (write $$ to say so)."
            )
        found.server.append((key, value, number))
    return found


def _comment(found: Imported, line: str, number: int) -> None:
    """Record a `# host` or `# secret` comment; any other comment is ignored."""
    if host := _HOST_COMMENT.fullmatch(line):
        name = host.group(1).lower()
        try:
            found.host.append((name, json.loads(host.group(2)), number))
        except json.JSONDecodeError as reason:
            where = f"{reason.msg[:1].lower()}{reason.msg[1:]} at character {reason.pos + 1}"
            # A Windows path pasted as written holds single backslashes, which JSON reads as escapes.
            fix = (
                "a backslash must be written twice (\\\\), or use / in the path"
                if reason.msg.startswith("Invalid \\escape")
                else "give a number, or a string in double quotes"
            )
            problem = f"host setting {name} is not a JSON value ({where}); {fix}."
            found.errors.append((problem, number))
    elif secret := _SECRET_COMMENT.fullmatch(line):
        found.secrets.append((settings.canonical_key(secret.group(1)), number))


def _quoted(rest: str, lines: list[str], index: int) -> tuple[str, int, str | None, bool]:
    """Read a quoted value that starts `rest`, continuing over the following lines until its closing quote.

    Args:
        rest (str): The text after `=`, starting with the quote.
        lines (list[str]): Every line of the file.
        index (int): The index of the line after the one `rest` is on.

    Returns:
        tuple[str, int, str | None, bool]: The value, the index of the next unread line, a problem (which never
            holds the value) or None, and whether a double-quoted value holds a lone `$`.
    """
    quote, text, position, value, lone_dollar = rest[0], rest, 1, [], False
    while True:
        if position >= len(text):
            if index >= len(lines):
                return "", index, "has a quote that is never closed.", False
            text += "\n" + lines[index]
            index += 1
            continue
        char = text[position]
        if char == quote:
            break
        if quote == '"' and char == "\\" and position + 1 < len(text):
            following = text[position + 1]
            value.append(_ESCAPES.get(following, "\\" + following))
            position += 2
        elif quote == '"' and text.startswith("$$", position):
            value.append("$")
            position += 2
        else:
            lone_dollar = lone_dollar or (quote == '"' and char == "$")
            value.append(char)
            position += 1
    if _INLINE_COMMENT.sub("", text[position + 1 :]).strip():
        return "", index, "has text after its closing quote.", False
    return "".join(value), index, None, lone_dollar


@dataclass(frozen=True)
class FileEdits:
    """An imported file as settings edits, in the two places they go around the flags' edits.

    Attributes:
        lines (list[edit.Edit]): The edits of the file's lines, which come before the flags' edits.
        asks (list[edit.Edit]): The ask-at-start edits, which come after them, so refusals are reported in that order.
    """

    lines: list[edit.Edit] = field(default_factory=list)
    asks: list[edit.Edit] = field(default_factory=list)


def edits(found: Imported) -> FileEdits:
    """Return a parsed file as settings edits, each naming its line; `edit.resolve` judges them with the flags.

    A setting line is a `Set`, a `# host` line a `SetHost`, a `# secret` line marks its secret ask-at-start
    unless a value is stored, and the sweep marks every required secret still missing. A line the file could
    not parse is a `Refuse`.

    Args:
        found (Imported): The parsed file.

    Returns:
        FileEdits: The edits of the file's lines and its ask-at-start edits.
    """
    label = str(found.path)
    file = edit.Origin(edit.OriginKind.IMPORT_FILE, label)
    lines: list[edit.Edit] = [edit.Refuse(reason, _line(label, n)) for reason, n in found.errors]
    lines += [edit.Set(key, value, _line(label, n), note=found.notes.get(n)) for key, value, n in found.server]
    lines += [edit.SetHost(name, value, _line(label, n)) for name, value, n in found.host]
    asks: list[edit.Edit] = [edit.AskAtStart(key, _line(label, n), unless_stored=True) for key, n in found.secrets]
    return FileEdits(lines, [*asks, edit.AskForMissingSecrets(file)])


def _line(label: str, number: int) -> edit.Origin:
    """Return the origin of one line of an imported file."""
    return edit.Origin(edit.OriginKind.IMPORT_LINE, label, number)


def notes(found: Imported, edited: edit.Edited) -> list[str]:
    """Return what the administrator should know about an import beyond the resolved edits' own warnings.

    Args:
        found (Imported): The parsed file.
        edited (edit.Edited): The resolved edits, the file's among them.

    Returns:
        list[str]: A note on an image pin the import changed, or none.
    """
    if "image_digest" not in edited.changed:
        return []
    return [
        f"image_digest from {found.path} pins the image start runs; if it is not on this machine, "
        f"{CLI_NAME} update {edited.env.name} pulls the image and pins it again."
    ]


# --- Export ----------------------------------------------------------------------------------------------------


def render(env: environment.Environment, include_secrets: bool) -> tuple[str, list[str]]:
    """Render an environment as a settings file.

    Args:
        env (environment.Environment): The environment.
        include_secrets (bool): Write stored secret values; ask-at-start secrets stay out either way.

    Returns:
        tuple[str, list[str]]: The file content, and the secrets left out (sorted).
    """
    asked = env.asked_at_start
    stored_secrets = [k for k in env.server if settings.is_secret(k)]
    left_out = sorted({*asked, *([] if include_secrets else stored_secrets)})
    server = {k: v for k, v in env.server.items() if k not in left_out}
    host = {
        k: v
        for k, v in env.host.to_json().items()
        if k in settings.HOST_SCHEMA and k not in settings.NOT_TRANSFERRED_HOST_SETTINGS
    }
    lines = [
        f"# Settings of CONNX MCP Server environment {env.name!r}, exported by {CLI_NAME}.",
        f"# Import: {CLI_NAME} configure <name> --from-env-file <this file>",
        *(f"# host {name} = {json.dumps(value)}" for name, value in host.items()),
        *(f"# host.json key {key!r} left out: this {CLI_NAME} does not know it." for key in env.host.unknown),
        *(f"# secret {key}: left out; start asks for it unless configure --set stores it" for key in left_out),
        "",
    ]
    return "\n".join(lines) + "\n" + environment.render_env(server), left_out


def export(out: Output, given_name: str | None, include_secrets: bool, output: Path | None) -> None:
    """Run `export`: print an environment's settings file, or write it owner-only to a file.

    Args:
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        include_secrets (bool): Write stored secret values; only allowed with `output`.
        output (Path | None): The file to write; None or `-` prints to stdout.

    Raises:
        CliError: Secrets asked for without a file, or a file that cannot be written.
        EnvironmentNotFound: There is no such environment.
    """
    name = environment.resolve_name(given_name, "export")
    if output is not None and str(output) == "-":
        output = None
    if include_secrets and output is None:
        raise CliError(
            ExitCode.USAGE,
            "--include-secrets writes secrets only to a file, never to the screen: give --output FILE.",
            f"{CLI_NAME} export {name} --include-secrets --output {name}.env",
        )
    env = environment.load(name)
    if not env.exists:
        raise EnvironmentNotFound(env)
    content, left_out = render(env, include_secrets)
    data: dict[str, Any] = {"environment": name, "secrets_included": include_secrets, "secrets_left_out": left_out}
    if output is None:
        out.result({**data, "content": content}, content.rstrip("\n"))
        return
    try:
        environment.write_private(output, content)
    except OSError as reason:
        raise CliError(
            ExitCode.EXPORT_WRITE_FAILED,
            f"Cannot write {output}: {reason}.",
            f"{CLI_NAME} export {name} --output <file in an existing directory>",
        ) from reason
    left = f" Secrets left out: {', '.join(left_out)}." if left_out else ""
    out.result({**data, "file": str(output)}, f"Wrote environment {name!r} to {output}.{left}")
