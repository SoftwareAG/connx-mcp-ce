#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill.py - The skill installer: copy connx-sql-expert out of an environment's image into harness directories.

Description:
    The skill is taken from the environment's pinned image (or, before a first start, from the
    image its reference resolves to), so it matches the server version the environment runs. The
    harness map is data: each harness names its user and project skill directories, the
    executables that reveal it is installed, and its registration steps. Harnesses sharing a
    directory get one copy. A marker file in each copy records the installing CLI and image; a
    copy without it is someone else's and is replaced only with `--force`. Registration steps are
    printed with the environment's URL, never executed; each harness's steps follow its own public
    documentation (cited beside its entry), and a step that documentation does not confirm says so.
    `--zip <dir>` hands the skill over instead: a zip whose folder is named after the skill, plus
    install notes per harness written beside it. A zip-only harness (Claude Desktop, which takes the
    skill as an upload) has no skills directory and always gets the zip; a file connx-mcp-ce did not
    write where the zip goes is replaced only with `--force`. Each harness's entry also names the
    instructions file it reads at each scope (a rule over the scope, home, environment, OS and an
    injectable file lookup, so it is tested on any OS's paths); Hermes and `agents` have none at user
    scope and are reported as skipped. install-skill writes the skill instructions there
    (connx_mcp.instructions), one write per distinct file, and records each file with the hash of
    its section for `guide`, which hints when a recorded section has gone, and for the next run, which
    refuses a section edited since. The record keeps every run's skill copies, zips and files: a run
    replaces the entries for the paths it wrote again, adding its harnesses to their claims. Every
    refusal (skill copies, the zip, instructions files) is gathered and reported together before
    anything is written. The text is the image's INSTRUCTIONS.md, copied out with the skill, under the
    image's version; an image without it gets the built-in text under connx-mcp-ce's version. No
    instructions file is written for a zip: the install notes beside it quote the same section and say
    where each harness takes it (Claude Desktop: its project instructions).
    `uninstall-skill` is the inverse, on the same harness rules: it recomputes install's targets
    for the named harnesses at one scope, or for each harness and scope the record names when a flag is
    left out, adds the skill copies and instructions files the record names for them there, gathers every
    refusal, shows the plan and asks, then removes each section and deletes each skill copy that carries
    connx-mcp-ce's marker; a copy without it is left in place. It never calls Docker.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Protocol, TypeGuard

import questionary
from prompt_toolkit import print_formatted_text

from connx_mcp import CLI_NAME, environment, image, instructions, lifecycle, settings
from connx_mcp.docker import DockerPort
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode, Output, shell_argument
from connx_mcp.ports import Ports, Terminal
from connx_mcp.version import installed_version

SKILL_NAME = "connx-sql-expert"
IMAGE_SKILL_PATH = f"/opt/softwareag/connx/mcp/src/skills/{SKILL_NAME}"
# The skill instructions an image may ship, body only; images without it get the built-in text.
IMAGE_INSTRUCTIONS_PATH = "/opt/softwareag/connx/mcp/doc/INSTRUCTIONS.md"
# The name the image's skill instructions are copied out under.
IMAGE_FILE = "INSTRUCTIONS.md"
# How `docker cp` says a path is not in the container: current Docker, then older releases.
_MISSING_IN_IMAGE = re.compile(r"Could not find the file|No such container:path")
MARKER_FILE = f".{CLI_NAME}-install.json"
# In the environment directory: every skill copy, zip and instructions file install-skill wrote, with the
# harnesses that claim each, for `guide`, the next install and uninstall-skill.
INSTALL_RECORD = "skill-install.json"
CopyOutcome = Literal["deleted", "kept_shared", "left_foreign", "not_found"]
# What uninstall-skill does to a recorded zip.
ZipOutcome = Literal["deleted", "left_foreign", "not_found"]
KEPT_SHARED = "kept_shared"
ZIP_NAME = f"{SKILL_NAME}.zip"
# Beside the zip: how to add the skill, the skill instructions and the server to each harness.
NOTES_NAME = f"{SKILL_NAME}-install.txt"
INSTALLED_BY = CLI_NAME
# The skill names the server's tools `mcp__connx__...`, so every harness registers the server under this name.
SERVER_NAME = "connx"


@dataclass(frozen=True)
class Registration:
    """How a harness registers the server; `{url}`, `{name}` and `{title}` are filled in when printed.

    `{oauth_flags}` becomes `oauth_flags` with OAuth on and nothing without; `{bridge_url}` is the URL a
    local bridge dials (the public URL with OAuth on, since it is the token audience, else this machine's).

    Attributes:
        confirmed (bool): Whether the harness's public documentation gives these steps; a step it does not
            confirm starts with "Not confirmed".
        steps (tuple[str, ...]): Always printed.
        oauth_steps (tuple[str, ...]): Printed when the environment turns OAuth on.
        tls_steps (tuple[str, ...]): Printed when the environment turns TLS on.
        project_steps (tuple[str, ...]): Printed when the skill is installed for a project.
        oauth_flags (str): What `{oauth_flags}` becomes with OAuth on.
        unregister_steps (tuple[str, ...]): How to remove the server from the harness again: uninstall-skill's
            optional steps.
        unregister_confirmed (bool): Whether the harness's public documentation gives `unregister_steps`.
    """

    confirmed: bool
    steps: tuple[str, ...]
    oauth_steps: tuple[str, ...] = ()
    tls_steps: tuple[str, ...] = ()
    project_steps: tuple[str, ...] = ()
    oauth_flags: str = ""
    unregister_steps: tuple[str, ...] = ()
    unregister_confirmed: bool = True


@dataclass(frozen=True)
class Harness:
    """One harness the skill can be installed for.

    Attributes:
        name (str): The `--harness` value.
        title (str): The name people know it by.
        user_dir (tuple[str, ...] | None): Its skills directory under the home directory (`--user`); None: zip only.
        project_dir (tuple[str, ...] | None): Its skills directory under a project (`--project <dir>`).
        executables (tuple[str, ...]): Executables on PATH that show it is installed.
        registration (Registration): How to register the server with it.
        config_dirs (tuple[tuple[str, ...], ...]): Directories under the home directory that show it is installed.
        upload_steps (tuple[str, ...]): How a zip-only harness takes the zip; `{zip}` is the zip's file name.
        windows_user_dir (tuple[str, ...] | None): On Windows, its user skills directory under LOCALAPPDATA
            instead of `user_dir` under the user profile.
        project_note (str): What a project install needs beyond the copy, e.g. trusting the project.
        instructions_file (Callable[[InstructionsScope], PurePath | None] | None): Given the scope, the
            instructions file the harness reads there (it may look at what exists), or None when it has no file
            at that scope; None: the harness takes no instructions file at any scope.
        instructions_places (tuple[str, ...]): Where the person given the zip pastes the skill instructions,
            worded to match `instructions_file`; `{title}` is the harness's title.
    """

    name: str
    title: str
    user_dir: tuple[str, ...] | None
    project_dir: tuple[str, ...] | None
    executables: tuple[str, ...]
    registration: Registration
    config_dirs: tuple[tuple[str, ...], ...] = ()
    upload_steps: tuple[str, ...] = ()
    windows_user_dir: tuple[str, ...] | None = None
    project_note: str = ""
    instructions_file: Callable[[InstructionsScope], PurePath | None] | None = None
    instructions_places: tuple[str, ...] = ()

    @property
    def zip_only(self) -> bool:
        """Whether the harness takes the skill only as a zip (it has no skills directory)."""
        return self.user_dir is None or self.project_dir is None


# --- Where the skill instructions go --------------------------------------------------------------------


class FileLookup(Protocol):
    """What the harness map asks of the filesystem to choose an instructions file; tests give a fake one."""

    def is_file(self, path: PurePath) -> bool:
        """Return whether a file exists at the path."""
        ...

    def read(self, path: PurePath) -> str | None:
        """Return the file's text, or None when there is no file; raise OSError when it cannot be read."""
        ...

    def resolve(self, path: PurePath) -> PurePath:
        """Return the path with symlinks followed."""
        ...


class DiskFiles:
    """The filesystem of this machine, as the harness map sees it."""

    def is_file(self, path: PurePath) -> bool:
        """Return whether a file exists at the path.

        Args:
            path (PurePath): The path.

        Returns:
            bool: True for an existing file.
        """
        return Path(path).is_file()

    def read(self, path: PurePath) -> str | None:
        """Return the file's text (invalid UTF-8 replaced), or None when there is no file.

        An OSError for a file that exists but cannot be read passes to the caller.

        Args:
            path (PurePath): The file.

        Returns:
            str | None: The text; None when the file does not exist.
        """
        try:
            return Path(path).read_bytes().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None

    def resolve(self, path: PurePath) -> PurePath:
        """Return the path with symlinks followed.

        Args:
            path (PurePath): The path.

        Returns:
            PurePath: The resolved path.
        """
        return Path(path).resolve()


@dataclass(frozen=True)
class InstructionsScope:
    """Where install-skill writes the skill instructions, and what the harness map needs to find each file there.

    Attributes:
        project (PurePath | None): The project directory for `--project`; None writes the user-scope files.
        home (PurePath): The home directory (home_directory).
        environ (Mapping[str, str]): The environment variables.
        os_name (str): `os.name`: `nt` on Windows.
        files (FileLookup): The filesystem the rules look at.
    """

    project: PurePath | None
    home: PurePath
    environ: Mapping[str, str]
    os_name: str
    files: FileLookup

    def path(self, value: str) -> PurePath:
        """Return a path given as text (an environment variable), as a path of this scope's OS."""
        return os_path(self.os_name, value)


def os_path(os_name: str, value: str) -> PurePath:
    """Return a path given as text as a path of an OS: Windows' for `nt`, else POSIX.

    Args:
        os_name (str): `os.name`: `nt` on Windows.
        value (str): The path as text.

    Returns:
        PurePath: The path.
    """
    return PureWindowsPath(value) if os_name == "nt" else PurePosixPath(value)


# Claude Code's file, the Claude files whose presence in a project or above it make Claude Code skip AGENTS.md,
# the managed files an administrator deploys for the whole machine, and its import syntax:
# https://code.claude.com/docs/en/memory
CLAUDE_FILE = "CLAUDE.md"
CLAUDE_FILES = ((CLAUDE_FILE,), (".claude", CLAUDE_FILE), ("CLAUDE.local.md",))
CLAUDE_MANAGED_FILES = (
    PurePosixPath("/Library/Application Support/ClaudeCode/CLAUDE.md"),
    PurePosixPath("/etc/claude-code/CLAUDE.md"),
    PureWindowsPath(r"C:\Program Files\ClaudeCode\CLAUDE.md"),
)
# The imports, in each project Claude file, that reach the project's AGENTS.md: an import resolves from the file
# that holds it.
_AGENTS_IMPORTS = {
    file: re.compile(rf"(?:^|\s)@(?:{'|'.join(re.escape(i) for i in spellings)})(?=\s|$)")
    for file, spellings in (
        ((CLAUDE_FILE,), ("AGENTS.md", "./AGENTS.md")),
        ((".claude", CLAUDE_FILE), ("../AGENTS.md",)),
        (("CLAUDE.local.md",), ("AGENTS.md", "./AGENTS.md")),
    )
}
# Codex reads AGENTS.override.md instead of AGENTS.md when it exists, in the project and in CODEX_HOME:
# https://learn.chatgpt.com/docs/agent-configuration/agents-md
CODEX_OVERRIDE_FILE = "AGENTS.override.md"


def agents_md(scope: InstructionsScope) -> PurePath | None:
    """Return the file Hermes and the shared agents directory read: the project's AGENTS.md.

    Args:
        scope (InstructionsScope): Where the instructions go.

    Returns:
        PurePath | None: `<project>/AGENTS.md`; None at user scope, where they read no instructions file.
    """
    return scope.project / instructions.AGENTS_FILE if scope.project is not None else None


def codex_file(scope: InstructionsScope) -> PurePath:
    """Return the file Codex reads: in the project, or at user scope in CODEX_HOME (default `<home>/.codex`).

    Args:
        scope (InstructionsScope): Where the instructions go.

    Returns:
        PurePath: AGENTS.override.md in that directory when it exists, else AGENTS.md there.
    """
    given = scope.environ.get("CODEX_HOME")
    if scope.project is not None:
        directory = scope.project
    else:
        directory = scope.path(given) if given else scope.home / ".codex"
    override = directory / CODEX_OVERRIDE_FILE
    return override if scope.files.is_file(override) else directory / instructions.AGENTS_FILE


def pi_file(scope: InstructionsScope) -> PurePath:
    """Return the file Pi reads: the project's AGENTS.md, or at user scope the file in `<home>/.pi/agent`.

    Pi reads AGENTS.md, or CLAUDE.md when it is the only one of the two: https://pi.dev/docs/latest/configuration

    Args:
        scope (InstructionsScope): Where the instructions go.

    Returns:
        PurePath: `<project>/AGENTS.md`; at user scope `<home>/.pi/agent/CLAUDE.md` when only it exists, else
            `<home>/.pi/agent/AGENTS.md`.
    """
    if scope.project is not None:
        return scope.project / instructions.AGENTS_FILE
    agent = scope.home / ".pi" / "agent"
    agents, claude = agent / instructions.AGENTS_FILE, agent / CLAUDE_FILE
    return claude if scope.files.is_file(claude) and not scope.files.is_file(agents) else agents


def claude_code_file(scope: InstructionsScope) -> PurePath:
    """Return the file Claude Code reads.

    At user scope this is `<home>/.claude/CLAUDE.md`. In a project, Claude Code reads AGENTS.md only when no
    Claude file exists in the project or any directory above it, so a Claude file on that path sends the section
    to the project's CLAUDE.md. Claude Code's own user file and the managed files are not project files and do
    not count. A Claude file in the project that imports the project's AGENTS.md (`@AGENTS.md` from `CLAUDE.md`
    or `CLAUDE.local.md`, `@../AGENTS.md` from `.claude/CLAUDE.md`) loads it, so AGENTS.md then gets the section
    and Claude Code does not load it twice. An OSError for a project Claude file that exists but
    cannot be read passes to the caller.

    Args:
        scope (InstructionsScope): Where the instructions go.

    Returns:
        PurePath: The user file; in a project `<project>/AGENTS.md` when no Claude file is on the path or one of
            the project's Claude files imports it, else `<project>/CLAUDE.md`.
    """
    user_file = scope.home / ".claude" / CLAUDE_FILE
    if scope.project is None:
        return user_file
    files = scope.files
    resolved = files.resolve(scope.project)
    skipped = {files.resolve(user_file), *CLAUDE_MANAGED_FILES}
    found = {d.joinpath(*f) for d in (resolved, *resolved.parents) for f in CLAUDE_FILES}
    claude_file_above = any(files.is_file(p) for p in found - skipped)
    agents = scope.project / instructions.AGENTS_FILE
    if not claude_file_above or any(
        _imports_agents_md(files.read(scope.project.joinpath(*f)), i) for f, i in _AGENTS_IMPORTS.items()
    ):
        return agents
    return scope.project / CLAUDE_FILE


# Text Claude Code does not expand imports in: HTML comments, fenced and indented code blocks, code spans.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_LIST_ITEM = re.compile(r"^( {0,3}(?:[-+*]|\d{1,9}[.)]))(?:( {1,4})(?=\S)|[ \t]*$)")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)
_BLANK_LINES = re.compile(r"\n[ \t]*\n")


def _prose(text: str) -> str:
    """Return a Markdown text without its comments, code blocks and code spans.

    An indented line is code where a paragraph cannot continue: at the start, after a blank line, a heading or
    a fenced block. In a list item the code starts four columns past the item's text. A code span never
    crosses a blank line.

    Args:
        text (str): The Markdown text.

    Returns:
        str: The prose that is left.
    """
    kept: list[str] = []
    fence = ""
    code_can_start = True
    list_indent = 0
    for line in _COMMENT.sub(" ", text).splitlines():
        opened = _FENCE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                if not line.strip().lstrip(fence[0]):
                    fence = ""
                    code_can_start = True
            continue
        if opened:
            fence = opened.group(1)
            continue
        if not line.strip():
            code_can_start = True
            kept.append("")
            continue
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip())
        if code_can_start and indent < list_indent:
            list_indent = 0
        if code_can_start and indent >= list_indent + 4:
            continue
        item = _LIST_ITEM.match(expanded[list_indent:])
        if item:
            # The item's text starts after the marker and its spaces; an empty item's, one column after the marker.
            list_indent += item.end(1) + len(item.group(2) or " ")
        code_can_start = bool(_HEADING.match(line))
        kept.append(line)
    paragraphs = _BLANK_LINES.split("\n".join(kept))
    return "\n\n".join(_CODE_SPAN.sub(" ", p) for p in paragraphs)


def _imports_agents_md(text: str | None, imports: re.Pattern[str]) -> bool:
    """Return whether a Claude file's prose imports the project's AGENTS.md.

    Args:
        text (str | None): The Claude file's text; None when there is no file.
        imports (re.Pattern[str]): The imports that reach the project's AGENTS.md from that file.

    Returns:
        bool: True when the prose holds one of them.
    """
    return text is not None and imports.search(_prose(text)) is not None


# The install notes' words for each harness's instructions file (its `instructions_file` rule), at each scope,
# for the person given the zip: the files on their machine cannot be looked at, so each condition is spelled out.
_PROJECT_AGENTS_MD = "For one project, paste the skill instructions at the end of <project>/AGENTS.md."
_NO_USER_FILE = "{title} has no instructions file for yourself, so paste them into each project."


# Hermes reads project skills (.hermes/skills and .agents/skills at the git root) only once the project is
# trusted: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md
_HERMES_PROJECT_TRUST = "Hermes loads a project's skills only after you run hermes skills trust in that project."

HARNESSES: dict[str, Harness] = {
    h.name: h
    for h in (
        Harness(
            name="claude-code",
            title="Claude Code",
            user_dir=(".claude", "skills"),
            project_dir=(".claude", "skills"),
            executables=("claude",),
            instructions_file=claude_code_file,
            instructions_places=(
                _PROJECT_AGENTS_MD,
                "If the project or a directory above it has a CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md, "
                "paste them at the end of <project>/CLAUDE.md instead. Do not count ~/.claude/CLAUDE.md or a "
                "CLAUDE.md that an administrator deployed for the whole machine.",
                "The project's CLAUDE.md or CLAUDE.local.md can import @AGENTS.md, and its .claude/CLAUDE.md can "
                "import @../AGENTS.md. If one of them does so in its text, not in a code block, code span or "
                "comment, paste them at the end of <project>/AGENTS.md, not of CLAUDE.md.",
                "For yourself, paste them at the end of ~/.claude/CLAUDE.md.",
            ),
            registration=Registration(
                confirmed=True,
                unregister_steps=(
                    "claude mcp remove {name}",
                    "Run it in the directory you added {name} from; Claude Code keeps a server for that project "
                    "unless you added it with -s user.",
                ),
                steps=(
                    "claude mcp add --transport http {name} {url}",
                    "In Claude Code, run /mcp and check that {name} is connected.",
                ),
                oauth_steps=("The first use opens your browser to sign in.",),
                tls_steps=(
                    "If the server's certificate is self-signed, point Claude Code at it before launching, "
                    "e.g. export NODE_EXTRA_CA_CERTS=/path/to/server.crt",
                ),
            ),
        ),
        # Codex: `codex mcp add/login` (https://github.com/openai/codex/blob/main/codex-rs/cli/src/mcp_cmd.rs),
        # config.toml and project scope (https://learn.chatgpt.com/docs/extend/mcp?surface=cli), skills
        # directories (https://learn.chatgpt.com/docs/build-skills).
        Harness(
            name="codex",
            title="Codex",
            user_dir=(".agents", "skills"),
            project_dir=(".agents", "skills"),
            executables=("codex",),
            instructions_file=codex_file,
            instructions_places=(
                "For one project, paste the skill instructions at the end of <project>/AGENTS.override.md when "
                "it exists, otherwise at the end of <project>/AGENTS.md.",
                "For yourself, paste them at the end of AGENTS.override.md in the CODEX_HOME directory (default "
                "~/.codex) when it exists, otherwise at the end of AGENTS.md there.",
            ),
            registration=Registration(
                confirmed=True,
                unregister_steps=(
                    "codex mcp remove {name}",
                    "For one trusted project, delete [mcp_servers.{name}] and its lines from .codex/config.toml in "
                    "that project.",
                ),
                steps=(
                    "codex mcp add {name} --url {url}",
                    "This command writes ~/.codex/config.toml. For one trusted project only, add "
                    '[mcp_servers.{name}] with url = "{url}" to .codex/config.toml in that project.',
                ),
                oauth_steps=(
                    "codex mcp login {name}",
                    "On a machine without a browser, add --no-browser to the login command.",
                    "If the identity provider does not allow self-registration, add --oauth-client-id <client id> "
                    "to the add command.",
                ),
                tls_steps=(
                    "Not confirmed: Codex documents no setting to trust a self-signed or private-CA certificate. "
                    "Use a certificate from a public CA.",
                ),
            ),
        ),
        # Pi has MCP built in: `pi mcp add/login/list`, `-l` for the project's .pi/mcp.json, read only in a
        # trusted project, and tool exposure (https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/
        # docs/cli.md, .../docs/mcp.md). Pi reads ~/.agents/skills and .agents/skills (.../docs/skills.md).
        Harness(
            name="pi",
            title="Pi",
            user_dir=(".agents", "skills"),
            project_dir=(".agents", "skills"),
            executables=("pi",),
            instructions_file=pi_file,
            instructions_places=(
                _PROJECT_AGENTS_MD,
                "For yourself, paste them at the end of ~/.pi/agent/AGENTS.md, or of ~/.pi/agent/CLAUDE.md when "
                "that is the only file there.",
            ),
            registration=Registration(
                confirmed=True,
                unregister_confirmed=False,
                unregister_steps=(
                    "Not confirmed: pi mcp remove {name}. Pi documents adding and listing servers, not removing "
                    "them; otherwise delete the {name} entry from Pi's mcp.json.",
                    "For one project, delete the {name} entry from .pi/mcp.json in that project.",
                ),
                steps=(
                    "pi mcp add {name} --url {url}",
                    "For one project only, run pi mcp add -l {name} --url {url} in that project. It writes "
                    ".pi/mcp.json, which Pi reads only after you trust the project.",
                    "Run pi mcp list to connect to {name} and check it.",
                    "Pi shows MCP tools through code mode by default. To let the model call the CONNX tools "
                    'directly, set "exposure": "direct" on the {name} entry in mcp.json.',
                ),
                oauth_steps=(
                    "pi mcp login {name}",
                    "If the identity provider does not allow self-registration, add --oauth-client-id <client id> "
                    "to the add command.",
                ),
                tls_steps=(
                    "Not confirmed: Pi documents no setting to trust a self-signed or private-CA certificate. "
                    "Pi runs on Node.js, so NODE_EXTRA_CA_CERTS=/path/to/ca.pem can work.",
                ),
            ),
        ),
        # Hermes: `hermes mcp add --url --auth oauth` and `hermes mcp login`
        # (https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md),
        # `mcp_servers.<name>` in config.yaml with `ssl_verify` and `oauth.client_id`
        # (.../website/docs/reference/mcp-config-reference.md). User skills in ~/.hermes/skills, which is
        # %LOCALAPPDATA%\hermes\skills on Windows (Hermes' Windows data directory, .../docs/getting-started/
        # installation.md); project skills in .agents/skills once trusted (.../docs/user-guide/features/skills.md).
        Harness(
            name="hermes",
            title="Hermes Agent",
            user_dir=(".hermes", "skills"),
            project_dir=(".agents", "skills"),
            executables=("hermes",),
            instructions_file=agents_md,
            instructions_places=(_PROJECT_AGENTS_MD, _NO_USER_FILE),
            windows_user_dir=("hermes", "skills"),
            project_note=_HERMES_PROJECT_TRUST,
            registration=Registration(
                confirmed=True,
                unregister_steps=(
                    "hermes mcp remove {name}",
                    "In a running Hermes session, run /reload-mcp to drop {name}.",
                ),
                steps=(
                    "hermes mcp add {name} --url {url}{oauth_flags}",
                    "In a running Hermes session, run /reload-mcp to load {name}.",
                ),
                oauth_flags=" --auth oauth",
                oauth_steps=(
                    "hermes mcp login {name}",
                    "Run the login in a second command window: a running session waits only 30 seconds for it.",
                    "If the identity provider does not allow self-registration, set oauth: client_id: <client id> "
                    "under mcp_servers.{name} in Hermes' config.yaml.",
                ),
                tls_steps=(
                    "If the server's certificate is self-signed or from a private CA, set ssl_verify: "
                    "/path/to/ca.pem under mcp_servers.{name} in Hermes' config.yaml.",
                ),
                project_steps=(_HERMES_PROJECT_TRUST,),
            ),
        ),
        Harness(
            name="agents",
            title="Shared agents directory",
            user_dir=(".agents", "skills"),
            project_dir=(".agents", "skills"),
            executables=(),
            instructions_file=agents_md,
            instructions_places=(_PROJECT_AGENTS_MD, _NO_USER_FILE),
            registration=Registration(
                confirmed=False,
                unregister_steps=(
                    "The shared .agents/skills directory holds only the skill: remove the server named {name} from "
                    "each harness that reads it.",
                ),
                steps=(
                    "The shared .agents/skills directory holds only the skill: register the server named {name} "
                    "with the URL {url} in each harness that reads it.",
                ),
            ),
        ),
        Harness(
            name="claude-desktop",
            instructions_places=(
                "In Claude Desktop, open the project you use with the server and paste the skill instructions into "
                "its project instructions.",
            ),
            title="Claude Desktop",
            user_dir=None,
            project_dir=None,
            executables=(),
            config_dirs=(
                ("Library", "Application Support", "Claude"),
                ("AppData", "Roaming", "Claude"),
                (".config", "Claude"),
            ),
            # Skill upload: https://support.claude.com/en/articles/12512180-use-skills-in-claude
            upload_steps=(
                "In Claude Desktop, open Settings > Capabilities and turn on Code execution and file creation.",
                "Open Customize > Skills and choose + > Create skill > Upload a skill.",
                "Upload {zip} as it is. Do not unzip it.",
            ),
            # Custom connectors, the OAuth client choice and the Team/Enterprise owner rule:
            # https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
            registration=Registration(
                confirmed=True,
                unregister_steps=(
                    "In Claude Desktop, open Customize > Connectors, choose {name} and remove it.",
                    "On a Team or Enterprise plan, only an owner removes the connector, under Organization settings > "
                    "Connectors.",
                    "If you added the mcp-remote bridge instead, delete the {name} entry under mcpServers in "
                    "claude_desktop_config.json and restart Claude Desktop.",
                ),
                steps=(
                    "In Claude Desktop, open Customize > Connectors and choose + Add > Add custom connector.",
                    "Name the connector {name}, enter the URL {url} and choose Continue.",
                    "On a Team or Enterprise plan, only an owner adds the connector, under Organization settings > "
                    "Connectors. Members then choose Connect.",
                    "Custom connectors are dialled from Anthropic's cloud, not from this machine, so {url} must be "
                    "publicly reachable over HTTPS; a localhost or private-network URL does not connect.",
                    "Local alternative, a community practice that neither Anthropic nor Software AG supports and "
                    "that needs Node.js (npx) on the Claude Desktop machine: the mcp-remote bridge. Add to "
                    'claude_desktop_config.json: {{"mcpServers": {{"{name}": {{"command": "npx", '
                    '"args": ["mcp-remote", "{bridge_url}"]}}}}}} and restart Claude Desktop.',
                ),
                oauth_steps=(
                    "Under OAuth client, choose Register automatically.",
                    "If the identity provider does not allow self-registration, choose Use your own OAuth client. "
                    "Then enter its client id and secret.",
                    "Claude Desktop then opens your browser to sign in.",
                ),
                tls_steps=(
                    "Anthropic's cloud must trust the server's certificate: a self-signed certificate does not "
                    "connect as a custom connector.",
                    "For the mcp-remote bridge with a self-signed certificate, add "
                    '"env": {{"NODE_EXTRA_CA_CERTS": "/path/to/server.crt"}} to its entry.',
                ),
            ),
        ),
    )
}


# --- Where the skill goes -------------------------------------------------------------------------------


def home_directory(os_name: str, environ: Mapping[str, str]) -> PurePath:
    """Return what `~` means for a harness directory: the user profile on Windows, HOME elsewhere.

    Args:
        os_name (str): `os.name`: `nt` on Windows.
        environ (Mapping[str, str]): The environment variables.

    Returns:
        PurePath: The home directory, as a path of that OS.
    """
    return os_path(os_name, environ.get("USERPROFILE" if os_name == "nt" else "HOME") or str(Path.home()))


def local_app_data(os_name: str, environ: Mapping[str, str]) -> PurePath | None:
    """Return Windows' per-user application data directory, where some harnesses keep their skills.

    Args:
        os_name (str): `os.name`: `nt` on Windows.
        environ (Mapping[str, str]): The environment variables.

    Returns:
        PurePath | None: LOCALAPPDATA (default `AppData/Local` under the user profile); None off Windows.
    """
    if os_name != "nt":
        return None
    given = environ.get("LOCALAPPDATA")
    return os_path(os_name, given) if given else home_directory(os_name, environ) / "AppData" / "Local"


def skill_directory(
    harness: Harness, home: PurePath, project: PurePath | None = None, *, local: PurePath | None = None
) -> PurePath | None:
    """Return the directory the skill is written to for one harness.

    Args:
        harness (Harness): The harness.
        home (PurePath): The home directory (home_directory).
        project (PurePath | None): The project directory for `--project`; None installs for the user.
        local (PurePath | None): LOCALAPPDATA on Windows (local_app_data); None elsewhere.

    Returns:
        PurePath | None: `<skills directory>/connx-sql-expert`; None for a zip-only harness.
    """
    if harness.user_dir is None or harness.project_dir is None:
        return None
    if project is not None:
        root = project.joinpath(*harness.project_dir)
    elif local is not None and harness.windows_user_dir is not None:
        root = local.joinpath(*harness.windows_user_dir)
    else:
        root = home.joinpath(*harness.user_dir)
    return root / SKILL_NAME


def targets(
    names: Sequence[str], home: PurePath, project: PurePath | None, *, local: PurePath | None = None
) -> dict[PurePath, list[str]]:
    """Map each distinct skill directory to the harnesses that read it, so a shared directory is written once.

    A zip-only harness has no directory and is left out.

    Args:
        names (Sequence[str]): Harness names, in the order chosen.
        home (PurePath): The home directory.
        project (PurePath | None): The project directory, or None for the user.
        local (PurePath | None): LOCALAPPDATA on Windows; None elsewhere.

    Returns:
        dict[PurePath, list[str]]: Directory to harness names, in first-chosen order.
    """
    found: dict[PurePath, list[str]] = {}
    for name in names:
        directory = skill_directory(HARNESSES[name], home, project, local=local)
        if directory is None:
            continue
        harnesses = found.setdefault(directory, [])
        if name not in harnesses:
            harnesses.append(name)
    return found


def instructions_file(harness: Harness, scope: InstructionsScope) -> PurePath | None:
    """Return the instructions file one harness reads at a scope.

    An OSError for a file the harness's rule must read and cannot passes to the caller.

    Args:
        harness (Harness): The harness.
        scope (InstructionsScope): Where the instructions go.

    Returns:
        PurePath | None: The file; None for a harness with no instructions file at that scope (Hermes and the
            shared agents directory at user scope, Claude Desktop at any scope).
    """
    return harness.instructions_file(scope) if harness.instructions_file is not None else None


@dataclass(frozen=True)
class InstructionsTargets:
    """The instructions files install-skill writes for the chosen harnesses at one scope.

    Attributes:
        files (dict[Path, list[str]]): Each distinct file to the harnesses that read it, in first-chosen order.
        skipped (list[str]): The harnesses with no instructions file at this scope, though they have one in a
            project.
        refusals (list[instructions.Refusal]): Files the harness map had to read and could not.
    """

    files: dict[Path, list[str]]
    skipped: list[str]
    refusals: list[instructions.Refusal]


def instructions_targets(names: Sequence[str], scope: InstructionsScope) -> InstructionsTargets:
    """Map each distinct instructions file to the harnesses that read it, so a shared file is written once.

    Files are told apart by their resolved path (symlinks followed); each keeps the path its first harness
    named.

    Args:
        names (Sequence[str]): Harness names, in the order chosen.
        scope (InstructionsScope): Where the instructions go.

    Returns:
        InstructionsTargets: The files, the skipped harnesses and the files that could not be read.
    """
    found: dict[PurePath, tuple[Path, list[str]]] = {}
    skipped: list[str] = []
    refusals: list[instructions.Refusal] = []
    for name in names:
        harness = HARNESSES[name]
        if harness.instructions_file is None:
            continue
        try:
            path = instructions_file(harness, scope)
        except OSError as error:
            unread = Path(error.filename) if error.filename else Path(scope.project or scope.home)
            refusal = instructions.Refusal(unread, f"cannot be read: {error.strerror or error}", forceable=False)
            if refusal not in refusals:
                refusals.append(refusal)
            continue
        if path is None:
            skipped.append(name)
            continue
        _, harnesses = found.setdefault(scope.files.resolve(path), (Path(path), []))
        if name not in harnesses:
            harnesses.append(name)
    return InstructionsTargets(dict(found.values()), list(dict.fromkeys(skipped)), refusals)


def detect(which: Callable[[str], str | None], home: Path) -> list[str]:
    """Return the harnesses whose executables are on PATH or whose configuration directory exists.

    Args:
        which (Callable[[str], str | None]): Finds an executable on PATH.
        home (Path): The home directory.

    Returns:
        list[str]: Harness names, in map order.
    """
    return [
        h.name
        for h in HARNESSES.values()
        if any(which(e) for e in h.executables) or any(home.joinpath(*d).is_dir() for d in h.config_dirs)
    ]


# --- The skill out of the image -------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillSource:
    """The image the skill comes from.

    Attributes:
        image (str): The digest (or image ID) the skill is copied from.
        version (str | None): The image's version label.
    """

    image: str
    version: str | None


def source_for(docker: DockerPort, env: Environment) -> SkillSource:
    """Return the environment's pinned image, or the image its reference resolves to before a first start.

    Args:
        docker (DockerPort): The Docker port.
        env (Environment): The environment.

    Returns:
        SkillSource: The image and its version.
    """
    if env.host.image_digest:
        return SkillSource(env.host.image_digest, env.host.image_version)
    resolved = image.resolve(docker, image.choice(env), env.name)
    return SkillSource(resolved.digest, resolved.version)


def extract(docker: DockerPort, source: SkillSource, into: Path, update_command: str) -> Path:
    """Copy the skill out of an image without running it: create a container, copy, remove the container.

    The skill instructions the image may ship are copied too, following a symlink, to `<into>/INSTRUCTIONS.md`,
    read by `instructions_text`; an image without them is normal, but any other failure to copy them fails.

    Args:
        docker (DockerPort): The Docker port.
        source (SkillSource): The image.
        into (Path): An existing, empty directory.
        update_command (str): The command that updates the environment to another image, for the error.

    Returns:
        Path: `<into>/connx-sql-expert`.

    Raises:
        CliError: Docker failed, or the image holds no skill.
    """
    created = docker.run(["create", source.image])
    if created.returncode != 0:
        raise CliError(ExitCode.DOCKER_FAILED, f"docker create {source.image} failed: {created.stderr.strip()}")
    container = created.stdout.strip()
    try:
        copied = docker.run(["cp", f"{container}:{IMAGE_SKILL_PATH}", str(into)])
        if copied.returncode == 0:
            shipped = docker.run(["cp", "-L", f"{container}:{IMAGE_INSTRUCTIONS_PATH}", str(into / IMAGE_FILE)])
            if shipped.returncode != 0 and not _MISSING_IN_IMAGE.search(shipped.stderr):
                raise CliError(
                    ExitCode.DOCKER_FAILED,
                    f"Copying {IMAGE_INSTRUCTIONS_PATH} out of the image {source.image} failed: "
                    f"{shipped.stderr.strip()}",
                )
    finally:
        docker.run(["rm", "-f", container])
    skill = into / SKILL_NAME
    if copied.returncode != 0 or not (skill / "SKILL.md").is_file():
        said = copied.stderr.strip() or f"{IMAGE_SKILL_PATH} has no SKILL.md"
        raise CliError(
            ExitCode.SKILL_NOT_IN_IMAGE,
            f"The image {source.image} holds no {SKILL_NAME} skill at {IMAGE_SKILL_PATH} ({said}). "
            "Update the environment to an image that ships the skill.",
            update_command,
        )
    return skill


def instructions_text(extracted: Path, source: SkillSource, update_command: str) -> instructions.InstructionsText:
    """Return the skill instructions to write: the image's when `extract` copied them, else the built-in text.

    Args:
        extracted (Path): The directory `extract` copied into.
        source (SkillSource): The image.
        update_command (str): The command that updates the environment to another image, for the errors.

    Returns:
        instructions.InstructionsText: The image's text under its version, or the built-in text under connx-mcp-ce's.
    """
    shipped = extracted / IMAGE_FILE
    if not shipped.is_file():
        return instructions.built_in(installed_version())
    return instructions.from_image(shipped.read_bytes(), source.version or "unknown", source.image, update_command)


# --- Registration ---------------------------------------------------------------------------------------


def server_url(env: Environment) -> str:
    """Return the URL harnesses dial: the public base URL when set (the OAuth token audience), else this machine's.

    Args:
        env (Environment): The environment.

    Returns:
        str: `<base URL><MCP_PATH>`.
    """
    path = env.server.get("MCP_PATH") or "/mcp"
    public = env.server.get("MCP_PUBLIC_BASE_URL", "").strip()
    return (public.rstrip("/") if public else lifecycle.base_url(env)) + path


def local_url(env: Environment) -> str:
    """Return the URL this machine dials whatever the public base URL: what a local bridge connects to.

    Args:
        env (Environment): The environment.

    Returns:
        str: `<this machine's base URL><MCP_PATH>`.
    """
    return lifecycle.base_url(env) + (env.server.get("MCP_PATH") or "/mcp")


def registration_steps(harness: Harness, env: Environment, *, project: bool = False) -> dict[str, Any]:
    """Return one harness's copy-paste registration steps for an environment.

    Args:
        harness (Harness): The harness.
        env (Environment): The environment, for its URL, OAuth and TLS.
        project (bool): The skill was installed for a project.

    Returns:
        dict[str, Any]: `harness`, `confirmed` and `steps`.
    """
    rows = harness.registration
    steps = list(rows.steps)
    oauth = env.server.get("MCP_OAUTH_PROVIDER", "none").strip().lower() not in {"", "none"}
    if oauth:
        steps += rows.oauth_steps
        steps.append("With OAuth on, dial exactly {url}: it is the token audience.")
    if settings.tls_on(env.server):
        steps += rows.tls_steps
    if project:
        steps += rows.project_steps
    url = server_url(env)
    fill = {
        "url": url,
        "bridge_url": url if oauth else local_url(env),
        "name": SERVER_NAME,
        "title": harness.title,
        "oauth_flags": rows.oauth_flags if oauth else "",
    }
    return {"harness": harness.name, "confirmed": rows.confirmed, "steps": [s.format(**fill) for s in steps]}


# --- The zip --------------------------------------------------------------------------------------------


def zip_steps(harness: Harness) -> dict[str, Any]:
    """Return how one harness takes the skill from the zip: an upload, or unzipping into its skills directory.

    Args:
        harness (Harness): The harness.

    Returns:
        dict[str, Any]: `harness` and `steps`.
    """
    if harness.user_dir is None or harness.project_dir is None:
        steps = [s.format(zip=ZIP_NAME) for s in harness.upload_steps]
    else:
        user = "~/" + "/".join(harness.user_dir)
        project = "<project>/" + "/".join(harness.project_dir)
        steps = [
            f"Unzip {ZIP_NAME} into {user} for yourself, or into {project} for one project.",
            f"Check that {SKILL_NAME}/SKILL.md sits directly in that directory.",
            "On Windows, ~ is your user profile folder.",
        ]
        if harness.windows_user_dir is not None:
            windows = "\\".join(("%LOCALAPPDATA%", *harness.windows_user_dir))
            steps.append(f"For yourself on Windows, unzip into {windows} instead of {user}.")
        if harness.project_note:
            steps.append(harness.project_note)
        steps.append(f"Restart {harness.title} so that it loads the skill.")
    return {"harness": harness.name, "steps": steps}


def instructions_steps(harness: Harness) -> list[str]:
    """Return where one harness takes the skill instructions quoted in the install notes.

    Args:
        harness (Harness): The harness.

    Returns:
        list[str]: The steps.
    """
    return [s.format(title=harness.title) for s in harness.instructions_places]


# The zip's archive comment marks it as written by connx-mcp-ce, so a later run replaces it without --force.
ZIP_COMMENT = f"Written by {INSTALLED_BY}".encode()


def zip_is_ours(archive: Path) -> bool:
    """Return whether connx-mcp-ce wrote a zip (its archive comment says so).

    Args:
        archive (Path): The zip.

    Returns:
        bool: True for a readable zip carrying connx-mcp-ce's comment.
    """
    try:
        with zipfile.ZipFile(archive) as handle:
            return handle.comment == ZIP_COMMENT
    except OSError, zipfile.BadZipFile:
        return False


def write_zip(skill: Path, archive: Path) -> bool:
    """Zip the skill under a folder named after it, replacing an earlier zip; return whether one was replaced.

    The marker file stays out: the zip is a handover, and its recipient's copy is theirs.

    Args:
        skill (Path): The extracted skill directory.
        archive (Path): The zip to write.

    Returns:
        bool: Whether an earlier file was replaced.

    Raises:
        CliError: The zip could not be written.
    """
    replaced = archive.exists()
    partial = archive.with_name(archive.name + ".partial")
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.comment = ZIP_COMMENT
            for path in sorted(skill.rglob("*")):
                if path.is_file() and path.name != MARKER_FILE:
                    handle.write(path, PurePosixPath(SKILL_NAME, path.relative_to(skill).as_posix()).as_posix())
        partial.replace(archive)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise CliError(ExitCode.ZIP_FAILED, f"Could not write the skill zip {archive}: {error}") from error
    return replaced


def _write_notes(path: Path, text: str) -> None:
    """Write the install notes beside the zip."""
    try:
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as error:
        raise CliError(ExitCode.ZIP_FAILED, f"Could not write the install notes {path}: {error}") from error


# --- install-skill --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallRequest:
    """What `install-skill` was asked to do.

    Attributes:
        harnesses (list[str]): `--harness` values; empty asks on a terminal.
        project (Path | None): `--project <dir>`; None installs for the user.
        force (bool): Replace a skill connx-mcp-ce did not install, and edited or broken skill instructions.
        terminal (Terminal | None): Where to ask for harnesses; None when prompting is off.
        zip_dir (Path | None): `--zip <dir>`: hand the skill over as a zip there instead of installing it.
        user (bool): `--user` was given.
        no_instructions (bool): `--no-instructions`: write no skill instructions file.
    """

    harnesses: list[str]
    project: Path | None
    force: bool
    terminal: Terminal | None
    zip_dir: Path | None = None
    user: bool = False
    no_instructions: bool = False

    def command(self, env: Environment, harnesses: Sequence[str], *, force: bool = False) -> str:
        """Return this request as a command line for some harnesses, keeping its scope, `--zip` and `--no-instructions`.

        Args:
            env (Environment): The environment.
            harnesses (Sequence[str]): The `--harness` values; empty writes a placeholder.
            force (bool): Add `--force`.

        Returns:
            str: The command line.
        """
        parts = [CLI_NAME, "install-skill", env.name]
        parts += [p for n in harnesses for p in ("--harness", n)] or ["--harness", "<harness>"]
        if self.project is not None:
            parts += ["--project", str(self.project)]
        elif self.user:
            parts.append("--user")
        if self.zip_dir is not None:
            parts += ["--zip", str(self.zip_dir)]
        if self.no_instructions:
            parts.append("--no-instructions")
        if force:
            parts.append("--force")
        return " ".join(p if p == "<harness>" else shell_argument(p) for p in parts)


def _choose(request: InstallRequest, detected: list[str], env: Environment) -> list[str]:
    """Return the harnesses to install for: the flags, else the administrator's pick on a terminal."""
    unknown = [n for n in request.harnesses if n not in HARNESSES]
    if unknown:
        raise CliError(
            ExitCode.USAGE,
            f"Unknown harness {', '.join(unknown)}; choose from {', '.join(HARNESSES)}.",
            f"{CLI_NAME} install-skill --help",
        )
    if request.harnesses:
        return list(dict.fromkeys(request.harnesses))
    command = request.command(env, detected)
    if request.terminal is None:
        found = f"Detected on this machine: {', '.join(detected)}." if detected else "No harness was detected."
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Name the harnesses to install for with --harness ({', '.join(HARNESSES)}). {found}",
            command,
        )
    choices = [
        questionary.Choice(f"{h.name} ({h.title})", value=h.name, checked=h.name in detected)
        for h in HARNESSES.values()
    ]
    with request.terminal.session():
        try:
            chosen = questionary.checkbox(f"Install {SKILL_NAME} for (detected ones are ticked):", choices).unsafe_ask()
        except KeyboardInterrupt, EOFError:
            raise CliError(ExitCode.CANCELLED, "Cancelled; nothing was installed.", command) from None
    if not chosen:
        raise CliError(ExitCode.NO_HARNESS_CHOSEN, "No harness was chosen; nothing was installed.", command)
    return list(chosen)


def _ours(directory: Path) -> bool:
    """Return whether connx-mcp-ce installed the skill in a directory (its marker file says so)."""
    try:
        marker = json.loads((directory / MARKER_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return isinstance(marker, dict) and marker.get("installed_by") == INSTALLED_BY


def _foreign_copies(directories: Sequence[Path]) -> list[instructions.Refusal]:
    """Return a refusal for each skill directory that holds a skill connx-mcp-ce did not install."""
    return [
        instructions.Refusal(
            d, f"holds a skill {CLI_NAME} did not install, so it may hold your own edits", forceable=True
        )
        for d in directories
        if (d.exists() or d.is_symlink()) and not _ours(d)
    ]


def _foreign_zip(archive: Path) -> list[instructions.Refusal]:
    """Return a refusal when a file connx-mcp-ce did not write sits where the zip goes.

    A directory there is left to fail as a write error.
    """
    if (archive.is_file() or archive.is_symlink()) and not zip_is_ours(archive):
        return [instructions.Refusal(archive, f"already exists and {CLI_NAME} did not write it", forceable=True)]
    return []


def _outside(project: Path, files: Iterable[Path]) -> list[instructions.Refusal]:
    """Return a refusal for each project instructions file that a link sends outside the project.

    Args:
        project (Path): The project directory.
        files (Iterable[Path]): The instructions files in it.

    Returns:
        list[instructions.Refusal]: One forceable refusal per file whose resolved path is outside the project;
            the project is matched by `file_identity`, so another spelling of it counts as inside.
    """
    inside = file_identity(project)
    return [
        instructions.Refusal(path, f"is a link to {path.resolve()}, which is outside {project}", forceable=True)
        for path in files
        if not any(file_identity(d) == inside for d in path.resolve().parents)
    ]


def _refuse(refusals: Sequence[instructions.Refusal], force_command: str, retry_command: str) -> None:
    """Report every refusal together, before anything is written.

    `--force` overrides a forceable refusal; an unreadable or read-only file must be fixed first.

    Args:
        refusals (Sequence[instructions.Refusal]): The refusals `--force` does not override in this run.
        force_command (str): The command with `--force`, offered when `--force` would help.
        retry_command (str): The command to run again once the files are fixed.

    Raises:
        CliError: There is at least one refusal.
    """
    if not refusals:
        return
    unforceable = [r for r in refusals if not r.forceable]
    any_forceable = len(unforceable) < len(refusals)
    listed = "\n".join(f"  {r.path} {r.reason}." for r in refusals)
    if not unforceable:
        advice = "Move or correct what is named, or replace it with --force."
    elif any_forceable:
        advice = "Fix the unreadable or read-only files, then replace the rest with --force."
    else:
        advice = "Fix what is named, then run the command again."
    raise CliError(
        ExitCode.FILE_UNREADABLE if unforceable else ExitCode.SKILL_EXISTS,
        f"Nothing was installed:\n{listed}\n{advice}",
        force_command if any_forceable else retry_command,
    )


def _write(skill: Path, directory: Path, marker: dict[str, Any]) -> bool:
    """Write one copy of the skill with its marker; return whether it replaced an existing one."""
    replaced = directory.exists() or directory.is_symlink()
    if directory.is_dir() and not directory.is_symlink():
        shutil.rmtree(directory)
    elif replaced:
        directory.unlink()
    directory.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill, directory)
    (directory / MARKER_FILE).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return replaced


@dataclass(frozen=True)
class RecordedInstructions:
    """One instructions file install-skill wrote, as the install record keeps it.

    Attributes:
        path (Path): The file.
        sha256 (str): The SHA-256 of the section as written, markers included.
        harnesses (tuple[str, ...]): The harnesses that read it.
        project (Path | None): The project it was written for; None for the user.
    """

    path: Path
    sha256: str
    harnesses: tuple[str, ...]
    project: Path | None

    def to_json(self) -> dict[str, Any]:
        """Return the record's entry for this file."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "harnesses": list(self.harnesses),
            "project": str(self.project) if self.project is not None else None,
        }

    @classmethod
    def from_json(cls, entry: object) -> RecordedInstructions | None:
        """Return the entry read from the record, or None for one that is not well formed.

        Args:
            entry (object): One item of the record's `instructions` list.

        Returns:
            RecordedInstructions | None: The entry.
        """
        if not isinstance(entry, dict) or not _absolute(entry.get("path")):
            return None
        return cls(Path(entry["path"]), str(entry.get("sha256") or ""), _harnesses_in(entry), _project_in(entry))


def _absolute(path: object) -> TypeGuard[str]:
    """Return whether a record's path is an absolute path: a relative one would depend on where connx-mcp-ce runs."""
    return isinstance(path, str) and Path(path).is_absolute()


def _harnesses_in(entry: dict[str, Any]) -> tuple[str, ...]:
    """Return a record entry's `harnesses`; none when the entry has no list of them."""
    harnesses = entry.get("harnesses")
    return tuple(h for h in harnesses if isinstance(h, str)) if isinstance(harnesses, list) else ()


def _project_in(entry: dict[str, Any]) -> Path | None:
    """Return a record entry's `project`; None for the user scope."""
    project = entry.get("project")
    return Path(project) if isinstance(project, str) else None


@dataclass(frozen=True)
class RecordedCopy:
    """One skill copy install-skill wrote, as the install record keeps it.

    The record keeps one entry per directory and scope: `--user` then `--project ~` write one directory and
    leave two entries. The claims on a directory are the harnesses of all its entries.

    Attributes:
        path (Path): The copy's directory, `<skills directory>/connx-sql-expert`.
        harnesses (tuple[str, ...]): The harnesses that installed it at this scope (its claims).
        project (Path | None): The project it was written for, resolved; None for the user.
    """

    path: Path
    harnesses: tuple[str, ...]
    project: Path | None

    def to_json(self) -> dict[str, Any]:
        """Return the record's entry for this copy."""
        return {
            "path": str(self.path),
            "harnesses": list(self.harnesses),
            "project": str(self.project) if self.project is not None else None,
        }

    @classmethod
    def from_json(cls, entry: object) -> RecordedCopy | None:
        """Return the entry read from the record's `copies` list, or None for one that is not well formed.

        Args:
            entry (object): One item of the record's `copies` list.

        Returns:
            RecordedCopy | None: The entry.
        """
        if not isinstance(entry, dict) or not _absolute(entry.get("path")):
            return None
        return cls(Path(entry["path"]), _harnesses_in(entry), _project_in(entry))


@dataclass(frozen=True)
class RecordedZip:
    """One skill zip install-skill wrote, as the install record keeps it.

    Attributes:
        path (Path): The zip, `<handover directory>/connx-sql-expert.zip`.
        harnesses (tuple[str, ...]): The harnesses it was written for (its claims); none for the single zip
            of an older record, which counts as written for any harness.
    """

    path: Path
    harnesses: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Return the record's entry for this zip."""
        return {"path": str(self.path), "harnesses": list(self.harnesses)}

    @classmethod
    def from_json(cls, entry: object) -> RecordedZip | None:
        """Return the entry read from the record's `zips` list, or None for one that is not well formed.

        Args:
            entry (object): One item of the record's `zips` list; a bare path names no harnesses.

        Returns:
            RecordedZip | None: The entry.
        """
        if _absolute(entry):
            return cls(Path(entry), ())
        if not isinstance(entry, dict) or not _absolute(entry.get("path")):
            return None
        return cls(Path(entry["path"]), _harnesses_in(entry))


@dataclass(frozen=True)
class InstallRecord:
    """What install-skill wrote for an environment, over all its runs: `skill-install.json` in its directory.

    Attributes:
        copies (tuple[RecordedCopy, ...]): The skill copies, one entry per directory and scope.
        zips (tuple[RecordedZip, ...]): The skill zips, one per handover directory.
        instructions (tuple[RecordedInstructions, ...]): The instructions files; a record written before
            the skill instructions has none.
        uninstalled (str | None): The date (ISO 8601) uninstall-skill removed the last thing the record named;
            None until then, and again after the next install-skill.
    """

    copies: tuple[RecordedCopy, ...] = ()
    zips: tuple[RecordedZip, ...] = ()
    instructions: tuple[RecordedInstructions, ...] = ()
    uninstalled: str | None = None

    @property
    def empty(self) -> bool:
        """Whether the record names no skill copy, zip or instructions file."""
        return not (self.copies or self.zips or self.instructions)

    def to_json(self) -> dict[str, Any]:
        """Return the record as written to `skill-install.json`; `uninstalled` only once it is set."""
        written = {
            "copies": [c.to_json() for c in self.copies],
            "zips": [z.to_json() for z in self.zips],
            "instructions": [i.to_json() for i in self.instructions],
        }
        return written | ({"uninstalled": self.uninstalled} if self.uninstalled is not None else {})


def _write_record(env: Environment, record: InstallRecord) -> None:
    """Write the environment's install record whole, so a crash never leaves a truncated record behind."""
    path = env.directory / INSTALL_RECORD
    try:
        instructions.replace_file(path, (json.dumps(record.to_json(), indent=2) + "\n").encode("utf-8"))
    except OSError as error:
        raise CliError(ExitCode.FILE_UNREADABLE, f"Could not write the skill install record {path}: {error}") from error


def read_record(env: Environment) -> InstallRecord | None:
    """Return the environment's install record, or None before the first install.

    A record written before claims (`places`, a list of directories, and a single `zip`) reads in the same
    shape: each copy claimed by every harness reading its directory, the zip as a one-item list naming no
    harnesses.

    Args:
        env (Environment): The environment.

    Returns:
        InstallRecord | None: The record.

    Raises:
        CliError: The record cannot be read.
    """
    path = env.directory / INSTALL_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise CliError(
            ExitCode.FILE_UNREADABLE,
            f"{path} cannot be read ({type(error).__name__}).",
            f"{CLI_NAME} install-skill {env.name}",
        ) from None
    if not _well_formed(record):
        raise CliError(
            ExitCode.FILE_UNREADABLE,
            f"{path} cannot be read (not an install record).",
            f"{CLI_NAME} install-skill {env.name}",
        )
    entries = record.get("instructions")
    found = (RecordedInstructions.from_json(e) for e in (entries if isinstance(entries, list) else ()))
    home = home_directory(os.name, os.environ)
    local = local_app_data(os.name, os.environ)
    uninstalled = record.get("uninstalled")
    return InstallRecord(
        _recorded_copies(record, home, local),
        _recorded_zips(record),
        tuple(e for e in found if e is not None),
        uninstalled if isinstance(uninstalled, str) else None,
    )


def _well_formed(record: object) -> bool:
    """Return whether parsed JSON has an install record's shape: an object whose lists hold well-formed entries.

    Each key may be missing (an older or a newer record); one that is there must have its kind of value. An
    older record's `zip` may be null.
    """
    if not isinstance(record, dict):
        return False
    lists = {
        "copies": RecordedCopy.from_json,
        "zips": RecordedZip.from_json,
        "instructions": RecordedInstructions.from_json,
    }
    for key, read in lists.items():
        entries = record.get(key, [])
        if not isinstance(entries, list) or any(read(e) is None for e in entries):
            return False
    places = record.get("places", [])
    return (
        isinstance(places, list)
        and all(isinstance(p, str) for p in places)
        and isinstance(record.get("zip"), str | None)
    )


def _recorded_copies(record: dict[str, Any], home: PurePath, local: PurePath | None) -> tuple[RecordedCopy, ...]:
    """Return the record's skill copies: `copies`, or a record's older `places`, a list of directories.

    An older record named no harnesses: each of its copies counts as claimed by every harness that reads that
    directory at its scope (_legacy_copy).
    """
    copies = record.get("copies")
    if isinstance(copies, list):
        return tuple(c for c in (RecordedCopy.from_json(e) for e in copies) if c is not None)
    places = record.get("places")
    if not isinstance(places, list):
        return ()
    return tuple(_legacy_copy(Path(p), home, local) for p in places if isinstance(p, str))


def _recorded_zips(record: dict[str, Any]) -> tuple[RecordedZip, ...]:
    """Return the record's skill zips: `zips`, or a record's older single `zip` as a one-item list."""
    zips = record.get("zips")
    if isinstance(zips, list):
        return tuple(z for z in (RecordedZip.from_json(e) for e in zips) if z is not None)
    archive = record.get("zip")
    return (RecordedZip(Path(archive), ()),) if isinstance(archive, str) else ()


def _legacy_copy(directory: Path, home: PurePath, local: PurePath | None) -> RecordedCopy:
    """Return an older record's skill copy with the harnesses that read its directory, and its scope.

    The user scope wins when the directory is a harness's user directory (a project at the home directory
    cannot be told apart); otherwise the project is the directory a harness's project directory sits under.
    A directory no harness reads keeps no harnesses.

    Args:
        directory (Path): The recorded copy.
        home (PurePath): The user's home directory.
        local (PurePath | None): Windows' per-user application data directory.

    Returns:
        RecordedCopy: The copy with its claims.
    """
    users = [n for n, h in HARNESSES.items() if _same(skill_directory(h, home, local=local), directory)]
    if users:
        return RecordedCopy(directory, tuple(users), None)
    for harness in HARNESSES.values():
        if harness.project_dir is None or len(directory.parents) <= len(harness.project_dir):
            continue
        project = directory.parents[len(harness.project_dir)]
        if _same(skill_directory(harness, home, project), directory):
            claimed = [n for n, h in HARNESSES.items() if _same(skill_directory(h, home, project), directory)]
            return RecordedCopy(directory, tuple(claimed), project.resolve())
    return RecordedCopy(directory, (), None)


def _same(found: PurePath | None, directory: Path) -> bool:
    """Return whether a harness's skill directory is the recorded one, in any spelling (`file_identity`)."""
    return found is not None and file_identity(found) == file_identity(directory)


def file_identity(path: PurePath) -> Hashable:
    """Return what makes two spellings of a path one file or directory: the identity the file system gives it.

    On a case-insensitive file system (macOS) `~/work/p` and `~/Work/p` resolve to two spellings of one
    directory, so spellings cannot tell. The key is the device and inode of the path, or of its nearest
    existing ancestor followed by the names below it, so a gone path stays one with its other spellings.
    Where the file system has no inode numbers (st_ino 0, such as FAT), the key is the resolved spelling.

    Args:
        path (PurePath): The path; relative to the working directory when not absolute.

    Returns:
        Hashable: Equal for two paths that name one file or directory.
    """
    resolved = Path(path).resolve()
    below: list[str] = []
    for existing in (resolved, *resolved.parents):
        try:
            found = existing.stat()
        except OSError:
            below.insert(0, existing.name)
            continue
        if found.st_ino == 0:
            break
        return (found.st_dev, found.st_ino, *below)
    return str(resolved)


def _written_hashes(
    env: Environment, record: InstallRecord, paths: Iterable[Path] = ()
) -> dict[Hashable, frozenset[str]]:
    """Map each instructions file the environment's record names to the section hashes any environment wrote there.

    Environments share user-scope files, so a section another environment wrote is connx-mcp-ce's too. A record
    of another environment that cannot be read is skipped: it vouches for nothing.

    Args:
        env (Environment): The environment installing or uninstalling.
        record (InstallRecord): Its install record.
        paths (Iterable[Path]): Further files to look up, whether or not this record names them.

    Returns:
        dict[Hashable, frozenset[str]]: Each file's `file_identity` to the hashes every environment's record
            keeps for it; an empty set when no record names the file.
    """
    found: dict[Hashable, set[str]] = {
        file_identity(p): set() for p in [*(e.path for e in record.instructions), *paths]
    }
    for entry in (e for r in [record, *other_records(env.name).records.values()] for e in r.instructions):
        found.get(file_identity(entry.path), set()).add(entry.sha256)
    return {path: frozenset(hashes) for path, hashes in found.items()}


@dataclass(frozen=True)
class OtherRecords:
    """The install records of the other environments under the base directory.

    Attributes:
        records (dict[str, InstallRecord]): Environment name to its record, in the base directory's order; an
            environment without a record has none here.
        unreadable (list[str]): A sentence for each record that cannot be read: it vouches for no section and
            claims no copy.
    """

    records: dict[str, InstallRecord]
    unreadable: list[str]


def other_records(name: str) -> OtherRecords:
    """Return the install record of every other environment under the base directory.

    Args:
        name (str): The environment to leave out.

    Returns:
        OtherRecords: The records, and the ones that cannot be read.
    """
    found = OtherRecords({}, [])
    for other in environment.names():
        if other == name:
            continue
        try:
            record = read_record(Environment(other, environment.base_directory() / other))
        except CliError as error:
            found.unreadable.append(
                f"{error.message} Environment {other} may still use a skill copy or section, but {CLI_NAME} cannot "
                f"tell, so {CLI_NAME} does not count its claims."
            )
            continue
        if record is not None:
            found.records[other] = record
    return found


@dataclass(frozen=True)
class Claimant:
    """An environment whose install record still claims a skill copy or a section at one scope, with the harnesses.

    Attributes:
        environment (str): The environment.
        harnesses (tuple[str, ...]): Its harnesses claiming it; none for an older record's entry, which names none.
        project (Path | None): The project the claim was installed for; None for the user.
    """

    environment: str
    harnesses: tuple[str, ...]
    project: Path | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the claimant as a result row."""
        project = str(self.project) if self.project is not None else None
        return {"environment": self.environment, "harnesses": list(self.harnesses), "project": project}


def claimants(path: Path, records: Mapping[str, InstallRecord], *, copy: bool) -> list[Claimant]:
    """Return who claims a skill copy or an instructions file: each record with an entry naming that file.

    Two spellings of one file or directory (a symlink, or letter case on a case-insensitive file system) name
    one file: see `file_identity`.

    Args:
        path (Path): The skill copy or the instructions file.
        records (Mapping[str, InstallRecord]): Environment name to its install record.
        copy (bool): Look among the records' skill copies; otherwise among their instructions files.

    Returns:
        list[Claimant]: One per environment and scope that claims it, in the order of `records`.
    """
    identity = file_identity(path)
    found: list[Claimant] = []
    for name, record in records.items():
        entries: Sequence[RecordedCopy | RecordedInstructions] = record.copies if copy else record.instructions
        claims = [e for e in entries if file_identity(e.path) == identity]
        for project in dict.fromkeys(e.project for e in claims):
            harnesses = [h for e in claims if e.project == project for h in e.harnesses]
            found.append(Claimant(name, _union((), harnesses), project))
    return found


def installed(env: Environment) -> bool:
    """Return whether a skill copy or zip connx-mcp-ce installed for an environment is still in place.

    A project install or a zip may sit anywhere, so the harness directories alone cannot answer: install-skill
    records where it put the skill, and a copy counts while its marker file is there (a zip, while the file is).
    The skill instructions do not count: the skill alone is installed.

    Args:
        env (Environment): The environment.

    Returns:
        bool: True when a recorded copy or zip is still there; False without a record.
    """
    record = read_record(env)
    if record is None:
        return False
    return any(_ours(c.path) for c in record.copies) or any(zip_is_ours(z.path) for z in record.zips)


def still_in_place(record: InstallRecord) -> InstallRecord:
    """Return the part of an install record that is still in place: what uninstall-skill would find.

    A copy counts while its marker file says connx-mcp-ce installed it, a zip while its archive comment says
    connx-mcp-ce wrote it, and an instructions file while it holds connx-mcp-ce's markers; a file that cannot be read
    counts, since nothing shows its section is gone.

    Args:
        record (InstallRecord): The record.

    Returns:
        InstallRecord: The entries still in place, in the record's order.
    """

    def has_section(path: Path) -> bool:
        try:
            found = _section_in(path)
        except OSError, UnicodeDecodeError:
            return path.exists()
        return found.span is not None or found.broken

    return InstallRecord(
        tuple(c for c in record.copies if _ours(c.path)),
        tuple(z for z in record.zips if zip_is_ours(z.path)),
        tuple(e for e in record.instructions if has_section(e.path)),
    )


def instructions_hints(env: Environment) -> list[str]:
    """Return a hint for each recorded instructions file that no longer holds one well-formed section.

    Each hint names the file and the install-skill command that writes the section again.

    Args:
        env (Environment): The environment.

    Returns:
        list[str]: The hints; empty without a record.
    """
    record = read_record(env)
    hints: list[str] = []
    for entry in record.instructions if record is not None else ():
        request = InstallRequest(list(entry.harnesses), entry.project, False, None, user=entry.project is None)
        command = request.command(env, entry.harnesses)
        try:
            found = _section_in(entry.path)
        except (OSError, UnicodeDecodeError) as error:
            hints.append(f"{_unchecked(entry.path, error)} Fix it, then run {command}")
            continue
        if found.broken:
            hints.append(
                f"The skill instructions markers in {entry.path} do not make one section. Correct or remove "
                f"them, then run {command}"
            )
        elif found.span is None:
            hints.append(f"{entry.path} no longer holds the skill instructions. To write them again, run {command}")
    return hints


def _section_in(path: Path) -> instructions.Found:
    """Return where a file holds the skill instructions section; a missing file holds none.

    A file that exists but cannot be read raises its OSError, and one that is not UTF-8 text its
    UnicodeDecodeError, for the caller to name.

    Args:
        path (Path): The instructions file.

    Returns:
        instructions.Found: Where the section sits.
    """
    try:
        return instructions.find(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return instructions.Found(None, broken=False)


def _unchecked(path: Path, error: OSError | UnicodeDecodeError) -> str:
    """Return the sentence saying a recorded instructions file could not be checked, naming the read failure."""
    return (
        f"{path} cannot be read as UTF-8 text ({error}), so {CLI_NAME} cannot tell whether it still holds the skill "
        "instructions."
    )


def _leftovers(record: InstallRecord, targeted: Iterable[Path]) -> list[str]:
    """Return a warning for each recorded file this run targets that still holds a well-formed section.

    A file that cannot be read gets a warning naming the read failure.

    Args:
        record (InstallRecord): The install record before this run.
        targeted (Iterable[Path]): The instructions files this run would write without `--no-instructions`.

    Returns:
        list[str]: The warnings.
    """
    identities = {file_identity(p) for p in targeted}
    warnings: list[str] = []
    for entry in record.instructions:
        if file_identity(entry.path) not in identities:
            continue
        try:
            held = _section_in(entry.path).span is not None
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(_unchecked(entry.path, error))
            continue
        if held:
            warnings.append(
                f"{entry.path} still holds the skill instructions that an earlier install-skill wrote. "
                "--no-instructions left them as they are."
            )
    return warnings


def install(ports: Ports, out: Output, given_name: str | None, request: InstallRequest) -> None:
    """Run `install-skill`.

    With `--zip`, nothing is installed on this machine: the zip and the install notes for every chosen
    harness go to that directory. Without it, a zip-only harness (Claude Desktop) still gets the zip, in
    the working directory, while the others are installed. With `--no-instructions`, no instructions file
    is written and the notes quote no section; the record keeps the files earlier runs wrote, and the
    result warns about a section still in a file this run targets.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        request (InstallRequest): The command line's options.

    Raises:
        CliError: A write failed after others succeeded; the message names those written.
    """
    env = lifecycle.load_configured(given_name, "install-skill")
    home = Path(home_directory(os.name, os.environ))
    local = local_app_data(os.name, os.environ)
    chosen = _choose(request, detect(ports.docker.which, home), env)
    project = request.project.absolute() if request.project is not None else None
    if request.zip_dir is not None:
        places: dict[Path, list[str]] = {}
        zipped = chosen
    else:
        places = {
            Path(d): names
            for d, names in targets(chosen, home, project, local=Path(local) if local is not None else None).items()
        }
        zipped = [n for n in chosen if HARNESSES[n].zip_only]
    archive = (request.zip_dir if request.zip_dir is not None else Path.cwd()).absolute() / ZIP_NAME
    earlier = read_record(env) or InstallRecord()
    hashes = _written_hashes(env, earlier)
    # Zip mode writes no instructions file: it neither plans one nor warns about one left behind.
    scope = InstructionsScope(project, home, os.environ, os.name, DiskFiles())
    found = InstructionsTargets({}, [], []) if request.zip_dir is not None else instructions_targets(chosen, scope)
    targeted = found.files
    # In zip mode `targeted` is empty, so nothing is planned.
    writes_instructions = not request.no_instructions
    # Planned before the image is touched so a refusal comes first; planned again below with the image's text.
    text = instructions.built_in(installed_version())
    planned = [
        instructions.plan(path, tuple(names), text, recorded=hashes.get(file_identity(path)), force=request.force)
        for path, names in (targeted.items() if writes_instructions else ())
    ]
    plans = [p for p in planned if isinstance(p, instructions.Plan)]
    refusals = _foreign_copies(list(places)) + (_foreign_zip(archive) if zipped else [])
    refusals += found.refusals if writes_instructions else []
    refusals += _outside(project, targeted) if writes_instructions and project is not None else []
    refusals += [p for p in planned if isinstance(p, instructions.Refusal)]

    def refuse(outcomes: Iterable[instructions.Refusal | instructions.Plan]) -> None:
        """Report the refusals among the outcomes that `--force` does not override in this run."""
        _refuse(
            [r for r in outcomes if isinstance(r, instructions.Refusal) and not (request.force and r.forceable)],
            request.command(env, chosen, force=True),
            request.command(env, chosen, force=request.force),
        )

    refuse(refusals)

    source = source_for(ports.docker, env)
    update_command = f"{CLI_NAME} update {shell_argument(env.name)}"
    marker = {
        "installed_by": INSTALLED_BY,
        "cli_version": installed_version(),
        "image": source.image,
        "image_version": source.version,
        "environment": env.name,
    }
    out.progress(f"Copying {SKILL_NAME} out of {source.image}...")
    handover: dict[str, Any] | None = None
    # The notes beside a zip quote the same text a run on this machine writes: the image's when it ships one.
    quotes = bool(zipped) and not request.no_instructions
    quoted: instructions.InstructionsText | None = None
    # What was written so far, named when a later write fails.
    done: list[Path] = []
    recorded_project = project.resolve() if project is not None else None

    def remember() -> None:
        """Add what this run has written so far to the record, read again so a concurrent run's entries stay."""
        wrote = set(done)
        run = InstallRecord(
            tuple(RecordedCopy(d, tuple(names), recorded_project) for d, names in places.items() if d in wrote),
            (RecordedZip(archive, tuple(zipped)),) if archive in wrote else (),
            tuple(
                RecordedInstructions(c.path, c.text.sha256, c.harnesses, recorded_project)
                for c in plans
                if c.path in wrote
            ),
        )
        _write_record(env, _merged(read_record(env) or InstallRecord(), run))

    with tempfile.TemporaryDirectory(prefix=f"{CLI_NAME}-skill-") as scratch:
        skill = extract(ports.docker, source, Path(scratch), update_command)
        # Looked up once, before anything is written, so a bad image INSTRUCTIONS.md leaves nothing behind.
        if plans or quotes:
            text = instructions_text(Path(scratch), source, update_command)
            quoted = text if quotes else None
        if plans:
            replanned = [
                instructions.plan(
                    c.path, c.harnesses, text, recorded=hashes.get(file_identity(c.path)), force=request.force
                )
                for c in plans
            ]
            # The files are unchanged since the first plan, so a refusal here is only a file changed meanwhile.
            refuse(replanned)
            plans = [p for p in replanned if isinstance(p, instructions.Plan)]
        installed: list[dict[str, Any]] = []
        try:
            for directory, names in places.items():
                installed.append(
                    {"path": str(directory), "harnesses": names, "replaced": _write(skill, directory, marker)}
                )
                done.append(directory)
            for change in plans:
                change.write()
                done.append(change.path)
            if zipped:
                handover = {
                    "path": str(archive),
                    "replaced": write_zip(skill, archive),
                    "notes": str(archive.with_name(NOTES_NAME)),
                    "steps": [zip_steps(HARNESSES[n]) for n in zipped],
                }
                done.append(archive)
        except CliError as error:
            # What was written before the failure is recorded; a record that cannot be written then leaves the
            # message, which names those writes, as the only account of them.
            with contextlib.suppress(CliError):
                remember()
            raise CliError(error.exit_code, _written_before(error, done), error.next_command) from error

    try:
        remember()
    except CliError as error:
        raise CliError(error.exit_code, _written_before(error, done), error.next_command) from error
    registration = [registration_steps(HARNESSES[n], env, project=project is not None) for n in chosen]
    data = {
        "environment": env.name,
        "skill": SKILL_NAME,
        "image": source.image,
        "image_version": source.version,
        "cli_version": marker["cli_version"],
        "scope": "zip" if request.zip_dir is not None else "project" if project is not None else "user",
        "url": server_url(env),
        "installed": installed,
        "instructions": [
            {
                "path": str(c.path),
                "harnesses": list(c.harnesses),
                "outcome": c.outcome,
                "source": c.text.source,
                "version": c.text.version,
            }
            for c in plans
        ]
        + ([_skipped(found.skipped)] if found.skipped and writes_instructions else []),
        "zip": handover,
        "registration": registration,
        "warnings": _leftovers(earlier, targeted) if request.no_instructions else [],
    }
    if handover is not None:
        try:
            _write_notes(Path(handover["notes"]), _notes(data, handover, quoted))
        except CliError as error:
            raise CliError(error.exit_code, _written_before(error, done), error.next_command) from error
    out.result(data, _human(data))


def _merged(earlier: InstallRecord, run: InstallRecord) -> InstallRecord:
    """Return the install record after a run: what it wrote first, then each earlier entry it did not rewrite.

    Entries are matched by `file_identity`, so a symlinked spelling counts, and a skill copy also by scope: a
    copy written for the user and for a project at the same directory keeps an entry for each. The record
    no longer says the skill was uninstalled.

    Args:
        earlier (InstallRecord): The record before the run.
        run (InstallRecord): What the run wrote.

    Returns:
        InstallRecord: The record to write.
    """
    return InstallRecord(
        _merge_entries(earlier.copies, run.copies, lambda c: (file_identity(c.path), _identity(c.project))),
        _merge_entries(earlier.zips, run.zips, lambda z: file_identity(z.path)),
        _merge_entries(earlier.instructions, run.instructions, lambda e: file_identity(e.path)),
    )


def _merge_entries[E: (RecordedCopy, RecordedZip, RecordedInstructions)](
    earlier: Sequence[E], written: Sequence[E], key: Callable[[E], Hashable]
) -> tuple[E, ...]:
    """Return the run's entries, one per key, then each earlier entry whose key the run did not write.

    A run's entry replaces the earlier one with its key, and its harnesses are the earlier claims followed by
    the run's: a harness installed before keeps its claim. Two of the run's entries with one key (two spellings
    of one directory) become one, with the harnesses of both.

    Args:
        earlier (Sequence[E]): The record's entries before the run.
        written (Sequence[E]): The entries for what the run wrote.
        key (Callable[[E], Hashable]): What makes two entries one.

    Returns:
        tuple[E, ...]: The merged entries.
    """
    before = [(key(e), e) for e in earlier]
    claims: dict[Hashable, tuple[str, ...]] = {}
    for k, entry in before:
        claims[k] = _union(claims.get(k, ()), entry.harnesses)
    rewritten: dict[Hashable, E] = {}
    for entry in written:
        k = key(entry)
        found = rewritten[k].harnesses if k in rewritten else claims.get(k, ())
        rewritten[k] = replace(entry, harnesses=_union(found, entry.harnesses))
    return tuple(rewritten.values()) + tuple(e for k, e in before if k not in rewritten)


def _identity(project: Path | None) -> Hashable:
    """Return a recorded project's `file_identity`; None for the user scope."""
    return file_identity(project) if project is not None else None


def _union(earlier: Sequence[str], added: Sequence[str]) -> tuple[str, ...]:
    """Return the earlier claims followed by each added harness not among them."""
    return tuple(dict.fromkeys([*earlier, *added]))


def _written_before(error: CliError, done: Sequence[Path]) -> str:
    """Return a write failure's message, followed by what install-skill had written before it.

    Args:
        error (CliError): The write failure.
        done (Sequence[Path]): The skill copies, instructions files and zip written before it.

    Returns:
        str: The message; it lists `done` when there is any.
    """
    if not done:
        return error.message
    listed = "\n".join(f"  {path}" for path in done)
    return f"{error.message}\nWritten before the failure:\n{listed}"


SKIPPED = "skipped"


def _skipped(harnesses: Sequence[str]) -> dict[str, Any]:
    """Return the result row for the harnesses that have no instructions file for the user."""
    return {
        "path": None,
        "harnesses": list(harnesses),
        "outcome": SKIPPED,
        "source": None,
        "version": None,
        "reason": "These harnesses have no instructions file for yourself. With --project DIR, install-skill "
        "writes the skill instructions into DIR/AGENTS.md for them.",
    }


def _notes(data: dict[str, Any], handover: dict[str, Any], text: instructions.InstructionsText | None) -> str:
    """Render the install notes written beside the zip, for the person it is handed to.

    With the skill instructions, each harness gets a step saying where to paste them, and the notes end with
    the section to paste, markers included, so that a later install-skill run there replaces it in place.

    Args:
        data (dict[str, Any]): The install's result.
        handover (dict[str, Any]): The result's zip entry.
        text (instructions.InstructionsText | None): The skill instructions to quote; None quotes none.

    Returns:
        str: The notes.
    """
    lines = [
        f"Install the {data['skill']} skill (image version {data['image_version'] or 'unknown'}) from {ZIP_NAME}",
        f"The CONNX MCP Server is at {data['url']}.",
    ]
    registration = {row["harness"]: row["steps"] for row in data["registration"]}
    for row in handover["steps"]:
        lines += ["", f"{HARNESSES[row['harness']].title}:", "  Add the skill:"]
        lines += [f"    {step}" for step in row["steps"]]
        if text is not None:
            lines.append("  Add the skill instructions:")
            lines += [f"    {step}" for step in instructions_steps(HARNESSES[row["harness"]])]
        lines.append("  Register the server:")
        lines += [f"    {step}" for step in registration[row["harness"]]]
    if text is not None:
        lines += [
            "",
            "The skill instructions are below. Paste all of their lines, markers included. If the file does not "
            "exist, create it.",
            "",
            text.section,
        ]
    return "\n".join(lines)


def _human(data: dict[str, Any]) -> str:
    """Render an install's result for a person."""
    lines: list[str] = []
    if data["installed"]:
        lines.append(f"Installed {data['skill']} from {data['image']} (version {data['image_version'] or 'unknown'}):")
        for row in data["installed"]:
            verb = "replaced" if row["replaced"] else "new"
            lines.append(f"  {row['path']}  ({', '.join(row['harnesses'])}; {verb})")
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in data["instructions"]:
        (skipped if row["outcome"] == SKIPPED else written).append(row)
    if written:
        first = written[0]
        lines += ["", f"Wrote the skill instructions ({first['source']} text, version {first['version']}):"]
        lines += [f"  {row['path']}  ({', '.join(row['harnesses'])}; {row['outcome']})" for row in written]
    for row in skipped:
        lines += ["", f"Skipped the skill instructions for {', '.join(row['harnesses'])}. {row['reason']}"]
    handover = data["zip"]
    if handover is not None:
        if lines:
            lines.append("")
        verb = "replaced" if handover["replaced"] else "new"
        lines += [
            f"Wrote {data['skill']} from {data['image']} (version {data['image_version'] or 'unknown'}) as a zip:",
            f"  {handover['path']}  ({verb})",
            f"  {handover['notes']}  (install notes to hand over with it)",
        ]
        for row in handover["steps"]:
            lines.append(f"\n{row['harness']}, add the skill:")
            lines += [f"  {step}" for step in row["steps"]]
    lines += ["", "Register the server with each harness; nothing was registered for you:"]
    for row in data["registration"]:
        lines.append(f"\n{row['harness']}:")
        lines += [f"  {step}" for step in row["steps"]]
    if data["warnings"]:
        lines.append("")
        lines += [f"Warning: {w}" for w in data["warnings"]]
    return "\n".join(lines)


# --- uninstall-skill ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UninstallRequest:
    """What `uninstall-skill` was asked to do.

    Attributes:
        harnesses (list[str]): `--harness` values; empty asks on a terminal.
        project (Path | None): `--project <dir>`; None uninstalls for the user.
        user (bool): `--user` was given.
        yes (bool): `--yes`: remove without a question to confirm.
        terminal (Terminal | None): Where to ask; None when prompting is off.
        force (bool): `--force`: remove edited sections, broken markers and sections through a link outside
            the project.
        shared (bool): `--shared`: remove shared copies too, whoever else still uses them.
    """

    harnesses: list[str]
    project: Path | None
    user: bool
    yes: bool
    terminal: Terminal | None
    force: bool = False
    shared: bool = False

    def command(
        self, name: str, harnesses: Sequence[str], *, yes: bool = False, force: bool = False, shared: bool = False
    ) -> str:
        """Return this request as a command line for some harnesses, keeping its scope.

        Args:
            name (str): The environment name.
            harnesses (Sequence[str]): The `--harness` values; none for a run that reads the record.
            yes (bool): Add `--yes`.
            force (bool): Add `--force`.
            shared (bool): Add `--shared`.

        Returns:
            str: The command line.
        """
        parts = [CLI_NAME, "uninstall-skill", name]
        parts += [p for n in harnesses for p in ("--harness", n)]
        if self.project is not None:
            parts += ["--project", str(self.project)]
        elif self.user:
            parts.append("--user")
        if shared:
            parts.append("--shared")
        if force:
            parts.append("--force")
        if yes:
            parts.append("--yes")
        return " ".join(p if p == HARNESS_PLACEHOLDER else shell_argument(p) for p in parts)


HARNESS_PLACEHOLDER = "<harness>"


@dataclass(frozen=True)
class TargetFilter:
    """What one uninstall-skill run selects from the record: each flag narrows only its own dimension.

    Attributes:
        harnesses (tuple[str, ...]): The harnesses; empty selects every harness the record names.
        project (Path | None): `--project DIR`, absolute; None selects no single project.
        user (bool): `--user`: the user scope alone.
    """

    harnesses: tuple[str, ...]
    project: Path | None
    user: bool

    def scoped(self, project: Path | None) -> bool:
        """Return whether a scope is selected: the named project, the user, or with neither flag any scope.

        Args:
            project (Path | None): The scope's project; None for the user.

        Returns:
            bool: True when the scope is selected.
        """
        if self.project is not None:
            return project is not None and file_identity(project) == file_identity(self.project)
        return project is None or not self.user

    def claims(self, harnesses: Sequence[str]) -> list[str] | None:
        """Return the selected harnesses among an entry's, or None when the entry is not selected.

        Args:
            harnesses (Sequence[str]): The harnesses the entry was written for; none (an older record's entry)
                counts as any harness.

        Returns:
            list[str] | None: Without named harnesses, all of the entry's (possibly none); otherwise those both
                name, or every named one for an entry that names none.
        """
        if not self.harnesses or not harnesses:
            return list(self.harnesses or harnesses)
        return [h for h in self.harnesses if h in harnesses] or None


@dataclass(frozen=True)
class RecordedTarget:
    """A skill copy or an instructions file that an environment's install record names.

    Attributes:
        path (Path): The skill copy or the instructions file.
        harnesses (tuple[str, ...]): The harnesses it was written for; empty when the record does not say.
        project (Path | None): The project it was written for; None for the user.
    """

    path: Path
    harnesses: tuple[str, ...]
    project: Path | None

    def selected(self, chosen: TargetFilter) -> list[str] | None:
        """Return the selected harnesses this entry was written for, or None when the run does not select it.

        Args:
            chosen (TargetFilter): What the run selects.

        Returns:
            list[str] | None: The harnesses; None when the scope or every harness differs.
        """
        return chosen.claims(self.harnesses) if chosen.scoped(self.project) else None


def recorded_targets(record: InstallRecord | None) -> tuple[list[RecordedTarget], list[RecordedTarget]]:
    """Return the skill copies and the instructions files an install record names: uninstall's one view of it.

    Args:
        record (InstallRecord | None): The record; None when there is none.

    Returns:
        tuple[list[RecordedTarget], list[RecordedTarget]]: The skill copies, then the instructions files.
    """
    if record is None:
        return [], []
    copies = [RecordedTarget(c.path, c.harnesses, c.project) for c in record.copies]
    files = [RecordedTarget(e.path, e.harnesses, e.project) for e in record.instructions]
    return copies, files


def target_scopes(record: InstallRecord | None, chosen: TargetFilter) -> dict[Path | None, list[str]]:
    """Return each scope whose targets install's rules recompute, with the harnesses to recompute there.

    Named harnesses are recomputed at the named scope, the user by default; each harness of a selected record
    entry is recomputed at that entry's scope as well.

    Args:
        record (InstallRecord | None): The install record; None when there is none.
        chosen (TargetFilter): What the run selects.

    Returns:
        dict[Path | None, list[str]]: Project directory, or None for the user, to its harnesses.
    """
    scopes: dict[Path | None, list[str]] = {}
    if chosen.harnesses:
        scopes[chosen.project] = list(chosen.harnesses)
    copies, files = recorded_targets(record)
    for entry in copies + files:
        names = entry.selected(chosen)
        if not names:
            continue
        # A named project keeps its spelling from the command line; the record keeps projects resolved.
        found = scopes.setdefault(chosen.project if chosen.project is not None else entry.project, [])
        found += [n for n in names if n not in found]
    return scopes


def recorded_zips(record: InstallRecord | None, chosen: TargetFilter) -> list[RecordedZip]:
    """Return the record's skill zips a run selects; a zip has no user or project scope, so a scope flag selects none.

    Args:
        record (InstallRecord | None): The install record; None when there is none.
        chosen (TargetFilter): What the run selects.

    Returns:
        list[RecordedZip]: The zips written for a selected harness; a zip naming no harnesses counts as any.
    """
    if record is None or chosen.project is not None or chosen.user:
        return []
    return [z for z in record.zips if not z.harnesses or chosen.claims(z.harnesses)]


def _choose_to_uninstall(request: UninstallRequest, detected: list[str], name: str) -> list[str]:
    """Return the harnesses to uninstall for: the flags, else the administrator's pick on a terminal."""
    unknown = [n for n in request.harnesses if n not in HARNESSES]
    if unknown:
        raise CliError(
            ExitCode.USAGE,
            f"Unknown harness {', '.join(unknown)}; choose from {', '.join(HARNESSES)}.",
            f"{CLI_NAME} uninstall-skill --help",
        )
    if request.harnesses:
        return list(dict.fromkeys(request.harnesses))
    command = request.command(name, detected or [HARNESS_PLACEHOLDER])
    if request.terminal is None:
        found = f"Detected on this machine: {', '.join(detected)}." if detected else "No harness was detected."
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Name the harnesses to uninstall the skill for with --harness ({', '.join(HARNESSES)}). {found}",
            command,
        )
    choices = [
        questionary.Choice(f"{h.name} ({h.title})", value=h.name, checked=h.name in detected)
        for h in HARNESSES.values()
    ]
    with request.terminal.session():
        try:
            question = questionary.checkbox(f"Uninstall {SKILL_NAME} for (detected ones are ticked):", choices)
            chosen = question.unsafe_ask()
        except KeyboardInterrupt, EOFError:
            raise CliError(ExitCode.CANCELLED, "Cancelled; nothing was removed.", command) from None
    if not chosen:
        raise CliError(ExitCode.NO_HARNESS_CHOSEN, "No harness was chosen; nothing was removed.", command)
    return list(chosen)


@dataclass(frozen=True)
class CopyTarget:
    """One skill copy uninstall-skill looked at.

    Attributes:
        path (Path): The skill directory.
        harnesses (list[str]): The requested harnesses that read it.
        outcome (CopyOutcome): `deleted`, `kept_shared` (another harness or environment still claims it),
            `left_foreign` (it has no connx-mcp-ce marker) or `not_found`.
    """

    path: Path
    harnesses: list[str]
    outcome: CopyOutcome


def _copy_targets(copies: dict[Path, list[str]], kept: Collection[Path]) -> list[CopyTarget]:
    """Return what uninstall does to each skill directory: only a copy with connx-mcp-ce's marker is deleted.

    A copy an earlier run renamed aside but could not delete (`_delete_copy`) is deleted too, as its own row.

    Args:
        copies (dict[Path, list[str]]): Each skill directory, to the requested harnesses that read it.
        kept (Collection[Path]): The directories still claimed by others, which a copy connx-mcp-ce installed keeps.

    Returns:
        list[CopyTarget]: What is done to each directory.
    """
    found: list[CopyTarget] = []
    seen: set[Hashable] = set()
    for path, names in copies.items():
        outcome: CopyOutcome
        if not (path.exists() or path.is_symlink()):
            outcome = "not_found"
        elif not _ours(path):
            outcome = "left_foreign"
        else:
            outcome = "kept_shared" if path in kept else "deleted"
        found.append(CopyTarget(path, names, outcome))
        for leftover in _renamed_aside(path):
            if file_identity(leftover) not in seen:
                seen.add(file_identity(leftover))
                found.append(CopyTarget(leftover, names, "deleted"))
    return found


def _renamed_aside(directory: Path) -> list[Path]:
    """Return the copies of the skill directory that `_delete_copy` renamed aside and could not delete."""
    if not directory.parent.is_dir():
        return []
    return sorted(
        p
        for p in directory.parent.iterdir()
        if _ASIDE.fullmatch(p.name) and p.name.startswith(f".{directory.name}.") and p.is_dir() and not p.is_symlink()
    )


def _undeletable_copies(copies: Iterable[CopyTarget]) -> list[instructions.Refusal]:
    """Return a refusal for each skill copy to delete whose skills directory is read-only: `--force` cannot help."""
    return [
        instructions.Refusal(c.path, f"cannot be deleted, because {c.path.parent} is read-only", forceable=False)
        for c in copies
        if c.outcome == "deleted" and not os.access(c.path.parent, os.W_OK | os.X_OK)
    ]


def harnesses_at(path: Path, project: Path | None, *, copy: bool) -> tuple[str, ...]:
    """Return the harnesses that use a skill copy's or instructions file's path at a scope.

    An older record's entry names no harnesses; these are the harnesses it counts as claimed by.

    Args:
        path (Path): The skill copy or the instructions file.
        project (Path | None): Its project; None for the user.
        copy (bool): The path is a skill copy; otherwise an instructions file.

    Returns:
        tuple[str, ...]: The harnesses whose rules name the path there; none when no rule names it.
    """
    home = home_directory(os.name, os.environ)
    names = list(HARNESSES)
    if copy:
        found = targets(names, home, project, local=local_app_data(os.name, os.environ))
    else:
        found = instructions_targets(names, InstructionsScope(project, home, os.environ, os.name, DiskFiles())).files
    identity = file_identity(path)
    return next((tuple(h) for p, h in found.items() if file_identity(p) == identity), ())


def _dropped[E: (RecordedCopy, RecordedInstructions)](entry: E, chosen: TargetFilter) -> E | None:
    """Return a record entry without the claims a run drops; None when no claim is left on it.

    An entry naming no harnesses (an older record's) counts as claimed by every harness that uses its path at
    its scope, or by any harness when none does.
    """
    harnesses = entry.harnesses or harnesses_at(entry.path, entry.project, copy=isinstance(entry, RecordedCopy))
    names = RecordedTarget(entry.path, harnesses, entry.project).selected(chosen)
    if names is None:
        return entry
    left = tuple(h for h in harnesses if h not in names)
    return replace(entry, harnesses=left) if left else None


def _without_claims(record: InstallRecord, chosen: TargetFilter) -> InstallRecord:
    """Return an install record without the claims a run drops: the harnesses it selects, at the scopes it selects."""
    return replace(
        record,
        copies=tuple(c for c in (_dropped(e, chosen) for e in record.copies) if c is not None),
        instructions=tuple(i for i in (_dropped(e, chosen) for e in record.instructions) if i is not None),
    )


def _merge(found: dict[Path, list[str]], path: Path, names: Iterable[str]) -> Path:
    """Add a target's harnesses under the path first named for its file (`file_identity`); return that path."""
    identity = file_identity(path)
    key = next((p for p in found if file_identity(p) == identity), path)
    harnesses = found.setdefault(key, [])
    harnesses += [n for n in names if n not in harnesses]
    return key


def _refuse_removal(refusals: Sequence[instructions.Refusal], force_command: str, retry_command: str) -> None:
    """Report every refusal together, before anything is removed.

    Args:
        refusals (Sequence[instructions.Refusal]): The refusals.
        force_command (str): The command with `--force`, offered when `--force` would help.
        retry_command (str): The command to run again once the files are fixed.

    Raises:
        CliError: There is at least one refusal.
    """
    if not refusals:
        return
    unforceable = [r for r in refusals if not r.forceable]
    any_forceable = len(unforceable) < len(refusals)
    listed = "\n".join(f"  {r.path} {r.reason}." for r in refusals)
    if not unforceable:
        advice = "Correct what is named, or remove it anyway with --force."
    elif any_forceable:
        advice = "Fix the unreadable or read-only files, then remove the rest with --force."
    else:
        advice = "Fix what is named, then run the command again."
    raise CliError(
        ExitCode.FILE_UNREADABLE if unforceable else ExitCode.SKILL_CHANGED,
        f"Nothing was removed:\n{listed}\n{advice}",
        force_command if any_forceable else retry_command,
    )


@dataclass(frozen=True)
class ZipTarget:
    """One recorded skill zip uninstall-skill looked at, with the install notes beside it.

    Attributes:
        path (Path): The zip.
        notes (Path | None): The install notes beside it; None when there are none.
        harnesses (tuple[str, ...]): The selected harnesses it was written for; every harness the run
            removes the skill for when an older record's zip names none.
        outcome (ZipOutcome): `deleted` (its archive comment is connx-mcp-ce's), `left_foreign` or `not_found`.
    """

    path: Path
    notes: Path | None
    harnesses: tuple[str, ...]
    outcome: ZipOutcome


def _zip_targets(record: InstallRecord | None, chosen: TargetFilter) -> list[ZipTarget]:
    """Return the recorded zips the run selects (recorded_zips), and what is done to each.

    A zip is deleted only when its archive comment is install's; the notes go only with such a zip.
    """
    found: list[ZipTarget] = []
    for entry in recorded_zips(record, chosen):
        notes = entry.path.with_name(NOTES_NAME)
        outcome: ZipOutcome
        if not (entry.path.exists() or entry.path.is_symlink()):
            outcome = "not_found"
        else:
            outcome = "deleted" if zip_is_ours(entry.path) and not entry.path.is_symlink() else "left_foreign"
        harnesses = tuple(chosen.claims(entry.harnesses) or ())
        found.append(ZipTarget(entry.path, notes if notes.is_file() else None, harnesses, outcome))
    return found


@dataclass(frozen=True)
class UninstallPlan:
    """What one uninstall-skill run removes and leaves, computed before anything is removed.

    Attributes:
        copies (list[CopyTarget]): Each skill directory looked at.
        files (dict[Path, list[str]]): Each instructions file looked at, to the requested harnesses that read it.
        changes (dict[Path, instructions.Removal | instructions.Refusal | None]): Each file's removal, refusal,
            or None when it holds no section.
        skipped (list[str]): The requested harnesses with no instructions file at this scope.
        refusals (list[instructions.Refusal]): Every refusal, the files' and the harness rules'.
        zips (list[ZipTarget]): Each recorded zip the run selects.
        harnesses (list[str]): Every harness the run removes the skill for, named or from the record.
        claims (dict[Path, list[Claimant]]): Each skill copy and instructions file someone else still claims
            once the run drops its own claims: this environment's other harnesses, or another environment.
        kept (frozenset[Path]): The shared copies and sections kept for them; empty with `--shared`.
        warnings (list[str]): The other environments' records that cannot be read, so claim nothing.
        scopes (dict[Path, Path | None]): Each skill copy and instructions file to the scope it was found at:
            its project, or None for the user.
        needs_force (frozenset[Path]): The kept sections a `--shared` retry would remove only with `--force`.
    """

    copies: list[CopyTarget]
    files: dict[Path, list[str]]
    changes: dict[Path, instructions.Removal | instructions.Refusal | None]
    skipped: list[str]
    refusals: list[instructions.Refusal]
    zips: list[ZipTarget] = field(default_factory=list)
    harnesses: list[str] = field(default_factory=list)
    claims: dict[Path, list[Claimant]] = field(default_factory=dict)
    kept: frozenset[Path] = frozenset()
    warnings: list[str] = field(default_factory=list)
    scopes: dict[Path, Path | None] = field(default_factory=dict)
    needs_force: frozenset[Path] = frozenset()

    @property
    def removals(self) -> list[instructions.Removal]:
        """The section removals, in the order the files were found."""
        return [c for c in self.changes.values() if isinstance(c, instructions.Removal)]


def _plan_uninstall(
    env: Environment,
    record: InstallRecord | None,
    chosen: TargetFilter,
    *,
    force: bool = False,
    shared: bool = False,
) -> UninstallPlan:
    """Find the targets (install's rules recomputed at each selected scope, plus the selected record entries).

    A skill copy or section that another harness of the environment, or another environment, still claims
    once the run drops its own claims is a shared copy: it is kept, and nothing about it is refused, unless
    `--shared` removes it anyway.

    Args:
        env (Environment): The environment; it need not exist.
        record (InstallRecord | None): Its install record; None when there is none.
        chosen (TargetFilter): What the run selects.
        force (bool): `--force`: forceable refusals become removals that name why they needed it.
        shared (bool): `--shared`: remove shared copies too.

    Returns:
        UninstallPlan: The plan.
    """
    home = Path(home_directory(os.name, os.environ))
    local = local_app_data(os.name, os.environ)
    copies: dict[Path, list[str]] = {}
    files: dict[Path, list[str]] = {}
    # Each project instructions file with its project, to refuse one that a link sends outside it.
    in_projects: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    refusals: list[instructions.Refusal] = []
    found_at: dict[Path, Path | None] = {}
    scopes = target_scopes(record, chosen)
    for project, names in scopes.items():
        for directory, users in targets(names, home, project, local=Path(local) if local is not None else None).items():
            found_at.setdefault(_merge(copies, Path(directory), users), project)
        found = instructions_targets(names, InstructionsScope(project, home, os.environ, os.name, DiskFiles()))
        for path, readers in found.files.items():
            found_at.setdefault(_merge(files, path, readers), project)
            in_projects += [(project, path)] if project is not None else []
        skipped += [n for n in found.skipped if n not in skipped]
        refusals += [r for r in found.refusals if r not in refusals]
    recorded_copies, recorded_files = recorded_targets(record)
    for entry in recorded_copies:
        if (names := entry.selected(chosen)) is not None:
            found_at.setdefault(_merge(copies, entry.path, names), entry.project)
    for entry in recorded_files:
        if (names := entry.selected(chosen)) is not None:
            found_at.setdefault(_merge(files, entry.path, names), entry.project)
            in_projects += [(entry.project, entry.path)] if entry.project is not None else []
    hashes = _written_hashes(env, record or InstallRecord(), files)
    changes: dict[Path, instructions.Removal | instructions.Refusal | None] = {
        p: instructions.unplan(p, tuple(n), recorded=hashes.get(file_identity(p)), force=force)
        for p, n in files.items()
    }
    others = other_records(env.name)
    records = {env.name: _without_claims(record or InstallRecord(), chosen), **others.records}
    claims = {p: found for p in copies if (found := claimants(p, records, copy=True))}
    claims |= {p: found for p, c in changes.items() if c is not None and (found := claimants(p, records, copy=False))}
    candidates = frozenset() if shared else frozenset(claims)
    planned = _copy_targets(copies, candidates)
    # A kept section is left as it is, so nothing about it is refused.
    kept = frozenset(c.path for c in planned if c.outcome == KEPT_SHARED) | (candidates & changes.keys())
    # Only the --shared retry would remove a kept section, so only that retry needs --force for it.
    outside = {file_identity(path) for project, path in in_projects if _outside(project, [path])}
    needs_force = frozenset(
        p
        for p, c in changes.items()
        if p in kept and ((isinstance(c, instructions.Refusal) and c.forceable) or file_identity(p) in outside)
    )
    changes = {p: None if p in kept else c for p, c in changes.items()}
    refusals += [c for c in changes.values() if isinstance(c, instructions.Refusal)]
    removed = {file_identity(c.path): c.path for c in changes.values() if isinstance(c, instructions.Removal)}
    for project, path in in_projects:
        if (named := removed.get(file_identity(path))) is None:
            continue
        for outside in _outside(project, [named]):
            change = changes[named]
            if force and isinstance(change, instructions.Removal):
                changes[named] = replace(change, forced=change.forced or "outside_project")
            elif outside not in refusals:
                refusals.append(outside)
    refusals += _undeletable_copies(planned)
    zips = _zip_targets(record, chosen)
    harnesses = [
        *chosen.harnesses,
        *(n for names in scopes.values() for n in names),
        *(n for z in zips for n in z.harnesses),
    ]
    ordered = [n for n in HARNESSES if n in harnesses]
    # An older record's zip names no harnesses: it may have gone to any of the run's.
    zips = [z if z.harnesses else replace(z, harnesses=tuple(ordered)) for z in zips]
    return UninstallPlan(
        planned, files, changes, skipped, refusals, zips, ordered, claims, kept, others.unreadable, found_at,
        needs_force,
    )  # fmt: skip


def _shared_retries(name: str, request: UninstallRequest, plan: UninstallPlan) -> dict[Path, str]:
    """Return the `--shared` retry for each kept shared copy: one command per scope, naming its harnesses.

    The run has already dropped this environment's claims on what it keeps, so a retry that read the record
    would find nothing; each names its harnesses and scope, and recomputes the targets from them.

    Args:
        name (str): The environment name.
        request (UninstallRequest): The command line's options.
        plan (UninstallPlan): The plan.

    Returns:
        dict[Path, str]: Each kept skill copy and instructions file to its retry.
    """
    readers = {c.path: c.harnesses for c in plan.copies} | plan.files
    groups: dict[Path | None, list[str]] = {}
    for path in plan.kept:
        project = plan.scopes.get(path)
        names = readers.get(path) or list(harnesses_at(path, project, copy=path not in plan.files))
        group = groups.setdefault(project, [])
        group += [n for n in names if n not in group]
    commands: dict[Path | None, str] = {}
    for project, names in groups.items():
        scoped = replace(request, project=project, user=project is None)
        force = request.force or any(plan.scopes.get(p) == project for p in plan.needs_force)
        harnesses = [n for n in HARNESSES if n in names] or [HARNESS_PLACEHOLDER]
        commands[project] = scoped.command(name, harnesses, yes=request.yes, force=force, shared=True)
    return {path: commands[plan.scopes.get(path)] for path in plan.kept}


def uninstall(ports: Ports, out: Output, given_name: str | None, request: UninstallRequest) -> None:
    """Run `uninstall-skill`: remove the skill copies and the skill instructions install-skill wrote.

    The targets are install-skill's rules recomputed for the requested harnesses at the requested scope,
    plus the skill copies and instructions files the environment's record names for them there. Without
    `--harness`, the record names the harnesses, and without `--user` or `--project` it names the scopes:
    each flag narrows only its own dimension. Afterwards, the record drops each selected entry that is gone,
    and a record left with nothing holds the date in `uninstalled`. Only a skill copy with
    connx-mcp-ce's marker is deleted, and only the text between connx-mcp-ce's markers is removed. Every refusal
    comes before the confirmation, and the confirmation before anything is removed. The environment need not
    exist when `--harness` names the harnesses; Docker is never called.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        request (UninstallRequest): The command line's options.

    Raises:
        CliError: A run with nothing to remove could not write the install record.
        UninstallFailed: A removal, or the record update after it, failed; the message names those removed.
    """
    env = environment.load(environment.resolve_name(given_name, "uninstall-skill"))
    record = read_record(env) if env.exists else None
    project = request.project.absolute() if request.project is not None else None
    # Without --harness, an environment's record names the harnesses; only a missing environment asks.
    if request.harnesses or not env.exists:
        chosen = _choose_to_uninstall(
            request, detect(ports.docker.which, Path(home_directory(os.name, os.environ))), env.name
        )
    else:
        chosen = []
    selected = TargetFilter(tuple(chosen), project, request.user)
    plan = _plan_uninstall(env, record, selected, force=request.force, shared=request.shared)
    _refuse_removal(
        plan.refusals,
        request.command(env.name, chosen, yes=request.yes, force=True, shared=request.shared),
        request.command(env.name, chosen, yes=request.yes, shared=request.shared),
    )
    scope = "project" if project is not None else "user" if request.user or record is None else "all"
    data = _uninstall_result(env.name, scope, plan, _shared_retries(env.name, request, plan))
    handed = {n for z in plan.zips for n in z.harnesses}
    data["steps"] = [_manual_steps(HARNESSES[n]) for n in plan.harnesses if HARNESSES[n].zip_only or n in handed]
    data["registration"] = [_unregistration_steps(HARNESSES[n]) for n in plan.harnesses]
    # A run that only keeps shared copies changes no file, so it asks nothing; it drops the run's claims.
    if not data["removed"]:
        if record is not None:
            try:
                _forget_in_record(env, selected, [*_found_gone(plan), *plan.kept], removed=False)
            except (OSError, ValueError) as error:
                raise CliError(
                    ExitCode.FILE_UNREADABLE,
                    f"Could not write the skill install record {env.directory / INSTALL_RECORD}: {error}",
                ) from error
        out.result(data, _uninstall_human(data))
        return
    retry = request.command(env.name, chosen, yes=True, force=request.force, shared=request.shared)
    if not request.yes:
        _confirm_uninstall(request.terminal, data, retry)
    steps: list[tuple[Path, Callable[[], None]]] = [(c.path, c.apply) for c in plan.removals]
    steps += [(c.path, lambda path=c.path: _delete_copy(path)) for c in plan.copies if c.outcome == "deleted"]
    for archive in (z for z in plan.zips if z.outcome == "deleted"):
        # The notes go first: a re-run after a failure finds the zip still recorded and removes it then.
        if archive.notes is not None and archive.notes.exists():
            steps.append((archive.notes, archive.notes.unlink))
        steps.append((archive.path, archive.path.unlink))
    done: list[Path] = []
    foreign: list[Path] = []
    # A shared copy removed with --shared takes every claim of this environment's on it along.
    everyone = TargetFilter((), None, user=False)
    for path, remove in steps:
        try:
            remove()
        except _CopyNotOurs:
            foreign.append(path)
            continue
        except OSError as error:
            raise UninstallFailed(f"Could not remove {path}", error, done, retry) from error
        done.append(path)
        dropping = everyone if path in plan.claims else selected
        _forget_or_fail(
            env, record, done, retry, lambda path=path, f=dropping: _forget_in_record(env, f, [path], removed=True)
        )
    left = [*_found_gone(plan), *plan.kept, *foreign]
    _forget_or_fail(env, record, done, retry, lambda: _forget_in_record(env, selected, left, removed=True))
    for row in data["copies"]:
        row["outcome"] = "left_foreign" if Path(row["path"]) in foreign else row["outcome"]
    data["removed"] = bool(done)
    out.result(data, _uninstall_human(data))


def _forget_or_fail(
    env: Environment, record: InstallRecord | None, done: Sequence[Path], retry: str, forget: Callable[[], None]
) -> None:
    """Update the environment's install record, when it has one; a failure stops the run with what was removed."""
    if record is None:
        return
    try:
        forget()
    except (OSError, ValueError) as error:
        record_file = env.directory / INSTALL_RECORD
        raise UninstallFailed(f"Could not update the skill install record {record_file}", error, done, retry) from error


class UninstallFailed(CliError):
    """An uninstall stopped part-way: what failed, and anything removed before it (message and `removed_paths`).

    Args:
        what (str): What could not be done.
        error (OSError | ValueError): Why.
        done (Sequence[Path]): What was removed before the failure.
        retry (str): The same command, which removes the rest once the cause is fixed.
    """

    def __init__(self, what: str, error: OSError | ValueError, done: Sequence[Path], retry: str) -> None:
        listed = "".join(f"\n  {p}" for p in done)
        before = f"\nRemoved before the failure:{listed}" if done else ""
        super().__init__(
            ExitCode.UNINSTALL_FAILED,
            f"{what}: {getattr(error, 'strerror', None) or error}{before}\nFix the cause, then run the same command again.",
            retry,
            details={"removed_paths": [str(p) for p in done]},
        )


def _found_gone(plan: UninstallPlan) -> list[Path]:
    """Return the targets the plan found gone already: no skill copy of connx-mcp-ce's, no section, no zip."""
    copies = [c.path for c in plan.copies if c.outcome in ("not_found", "left_foreign")]
    files = [p for p, change in plan.changes.items() if change is None and p not in plan.kept]
    return copies + files + [z.path for z in plan.zips if z.outcome == "not_found"]


def _forget_in_record(env: Environment, chosen: TargetFilter, paths: Iterable[Path], *, removed: bool) -> None:
    """Drop the run's claims on the skill copies, zips and instructions files at the paths from the install record.

    Every uninstall change to the record goes through here. Uninstall calls it after each removal, so a run
    that fails part-way leaves a record a re-run continues from, and once more for the targets it found gone.
    Each entry at a path that the run selects loses the selected harnesses; an entry left with none is
    dropped. The record is read again and edited as written: an entry connx-mcp-ce cannot read and another
    field stay as they are; an older record's shape is written in the current one. A record left naming
    nothing gets the date in `uninstalled`, once something was dropped or `removed`. Paths compare by
    `file_identity`; a missing record is left alone, and an OSError reading or writing it passes to the caller.

    Args:
        env (Environment): The environment.
        chosen (TargetFilter): What the run selects.
        paths (Iterable[Path]): The skill copies, zips or instructions files that are gone.
        removed (bool): The run removed something, so a record that names nothing now is dated.

    Raises:
        ValueError: The record is not a JSON object of connx-mcp-ce's, so the run cannot say what it removed.
    """
    path = env.directory / INSTALL_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except ValueError as error:
        raise ValueError(f"it is not valid JSON ({error})") from error
    if not isinstance(record, dict):
        raise ValueError("it is not a JSON object")
    current = dict(record)
    if "copies" not in current and ("places" in current or "zip" in current):
        try:
            legacy = read_record(env) or InstallRecord()
        except CliError as error:
            raise ValueError(error.message) from error
        current = {k: v for k, v in current.items() if k not in ("places", "zip")} | legacy.to_json()
    gone = {file_identity(p) for p in paths}
    kept = dict(current)
    for key in ("copies", "instructions", "zips"):
        if isinstance(entries := current.get(key), list):
            left = (_unclaimed(e, chosen, gone, kind=key) for e in entries)
            kept[key] = [e for e in left if e is not None]
    named = any(kept.get(k) for k in ("copies", "zips", "instructions"))
    if not named and not kept.get("uninstalled") and (removed or kept != record):
        kept["uninstalled"] = date.today().isoformat()
    if kept != record:
        instructions.replace_file(path, (json.dumps(kept, indent=2) + "\n").encode("utf-8"))


def _unclaimed(entry: object, chosen: TargetFilter, gone: set[Hashable], *, kind: str) -> object | None:
    """Return a record entry without the run's claims on it; None when it has none left.

    Args:
        entry (object): One entry of the record's `copies`, `instructions` or `zips`, as written.
        chosen (TargetFilter): What the run selects.
        gone (set[Hashable]): The `file_identity` of each path that is gone.
        kind (str): `copies`, `instructions` or `zips`; a zip has no scope, so a scope flag selects no zip.

    Returns:
        object | None: The entry as it was when the run does not select it, with fewer harnesses, or None.
    """
    fields = entry if isinstance(entry, dict) else {"path": entry}
    if not isinstance(fields.get("path"), str) or file_identity(Path(fields["path"])) not in gone:
        return entry
    project = _project_in(fields)
    if not (chosen.scoped(project) if kind != "zips" else chosen.project is None and not chosen.user):
        return entry
    harnesses = _harnesses_in(fields)
    if not harnesses and kind != "zips":
        harnesses = harnesses_at(Path(fields["path"]), project, copy=kind == "copies")
    dropped = chosen.claims(harnesses) if harnesses else []
    if dropped is None:
        return entry
    left = [h for h in harnesses if h not in dropped]
    return {**fields, "harnesses": left} if left else None


# Where connx-mcp-ce cannot reach a harness (Claude Desktop, or a machine a zip went to), the administrator removes the
# skill and its section by hand.
_SECTION_LINES = f"every line from {instructions.BEGIN_PREFIX} to {instructions.END_MARKER}, both included."
_HANDOVER_REMOVAL = (
    f"Where {ZIP_NAME} was handed over, delete the {SKILL_NAME} folder unzipped from it into {{title}}'s skills "
    "directory.",
    f"Remove the skill instructions from the instructions file they were pasted into: {_SECTION_LINES}",
)
_DESKTOP_REMOVAL = (
    f"In Claude Desktop, open Customize > Skills and delete {SKILL_NAME}.",
    "In the Claude Desktop project you use with the server, remove the skill instructions from its project "
    f"instructions: {_SECTION_LINES}",
)


def _manual_steps(harness: Harness) -> dict[str, Any]:
    """Return how to remove the skill and its skill instructions by hand from a harness connx-mcp-ce cannot reach.

    Args:
        harness (Harness): The harness: Claude Desktop, or one the skill was handed over to as a zip.

    Returns:
        dict[str, Any]: `harness` and `steps`.
    """
    if harness.zip_only:
        return {"harness": harness.name, "steps": list(_DESKTOP_REMOVAL)}
    return {"harness": harness.name, "steps": [s.format(title=harness.title) for s in _HANDOVER_REMOVAL]}


def _unregistration_steps(harness: Harness) -> dict[str, Any]:
    """Return one harness's optional steps to remove the server from it, the inverse of its registration steps.

    Args:
        harness (Harness): The harness.

    Returns:
        dict[str, Any]: `harness`, `confirmed`, `when` (the condition to follow them on) and `steps`.
    """
    rows = harness.registration
    steps = [s.format(name=SERVER_NAME) for s in rows.unregister_steps]
    return {
        "harness": harness.name,
        "confirmed": rows.confirmed and rows.unregister_confirmed,
        "when": f"If you no longer use this server from {harness.title}",
        "steps": steps,
    }


def uninstalled_on(env: Environment) -> str | None:
    """Return the date uninstall-skill removed the last thing the environment's record named, or None.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The ISO 8601 date; None without a record, or after a later install-skill.
    """
    record = read_record(env)
    return record.uninstalled if record is not None else None


def _uninstall_result(name: str, scope: str, plan: UninstallPlan, retries: dict[Path, str]) -> dict[str, Any]:
    """Return an uninstall's result: what is removed, what is left, and every place looked at.

    Args:
        name (str): The environment name.
        scope (str): `user`, `project` or `all`.
        plan (UninstallPlan): The plan.
        retries (dict[Path, str]): Each kept shared copy's command with `--shared`.

    Returns:
        dict[str, Any]: The result.
    """
    instructions_rows = [
        {
            "path": str(path),
            "harnesses": plan.files[path],
            "outcome": change.outcome
            if isinstance(change, instructions.Removal)
            else KEPT_SHARED
            if path in plan.kept
            else "not_found",
        }
        | ({"forced": change.forced} if isinstance(change, instructions.Removal) and change.forced else {})
        for path, change in plan.changes.items()
    ]
    removed = [c.path for c in plan.copies if c.outcome == "deleted"] + [c.path for c in plan.removals]
    kept = [c.path for c in plan.copies if c.outcome == KEPT_SHARED] + [p for p in plan.changes if p in plan.kept]
    return {
        "environment": name,
        "scope": scope,
        "removed": bool(plan.removals)
        or any(c.outcome == "deleted" for c in plan.copies)
        or any(z.outcome == "deleted" for z in plan.zips),
        "copies": [{"path": str(c.path), "harnesses": c.harnesses, "outcome": c.outcome} for c in plan.copies],
        "instructions": instructions_rows + ([_uninstall_skipped(plan.skipped)] if plan.skipped else []),
        "zips": [
            {"path": str(z.path), "notes": str(z.notes) if z.notes is not None else None, "outcome": z.outcome}
            for z in plan.zips
        ],
        "kept": [
            {"path": str(p), "claimants": [c.to_json() for c in plan.claims[p]], "retry": retries[p]} for p in kept
        ],
        "warnings": plan.warnings,
        "removed_shared": [
            {"path": str(p), "claimants": [c.to_json() for c in plan.claims[p]]} for p in removed if p in plan.claims
        ],
        "checked": [str(c.path) for c in plan.copies] + [str(p) for p in plan.files] + [str(z.path) for z in plan.zips],
    }


# The name `_delete_copy` gives a skill copy before it deletes it: `.connx-sql-expert.<16 hex digits>.deleted`.
_ASIDE = re.compile(rf"\.{re.escape(SKILL_NAME)}\.[0-9a-f]{{16}}\.deleted")


class _CopyNotOurs(Exception):
    """The skill copy lost connx-mcp-ce's marker after the plan was made, so it was left in place."""


def _delete_copy(directory: Path) -> None:
    """Delete one skill copy: a symlink or junction is unlinked; a directory is renamed beside itself, then deleted.

    The rename takes the copy away whole or not at all, so a failure never leaves a copy without its marker,
    which a re-run would then leave in place as not installed by connx-mcp-ce. A failure after the rename leaves
    the renamed copy, which a re-run finds (`_renamed_aside`) and deletes here without renaming it again. The
    marker is checked again after the rename, so a directory put in the copy's place while the confirmation
    waited is renamed back and kept. Read-only entries inside a copy proven to be one connx-mcp-ce installed are
    made writable.

    Args:
        directory (Path): The skill copy, or a copy an earlier run renamed aside.

    Raises:
        _CopyNotOurs: The directory no longer holds connx-mcp-ce's marker; it is back in place.
    """
    if directory.is_symlink() or directory.is_junction():
        directory.unlink()
        return
    if _ASIDE.fullmatch(directory.name):
        shutil.rmtree(directory, onexc=_made_writable)
        return
    # A fresh name: Windows renames a directory only onto a name that does not exist.
    doomed = directory.with_name(f".{directory.name}.{secrets.token_hex(8)}.deleted")
    directory.rename(doomed)
    if not _ours(doomed):
        doomed.rename(directory)
        raise _CopyNotOurs(directory)
    shutil.rmtree(doomed, onexc=_made_writable)


def _made_writable(function: Callable[[str], object], path: str, error: BaseException) -> None:
    """Retry a failed deletion in a skill copy once the entry and its directory are writable (`rmtree`'s onexc).

    Windows refuses to delete a read-only file, and POSIX a file in a read-only directory. A link is never
    changed, so its target keeps its mode.
    """
    try:
        for entry in (os.path.dirname(path), path):
            if not os.path.islink(entry):
                os.chmod(entry, os.stat(entry).st_mode | stat.S_IRWXU)
    except OSError:
        raise error from None
    function(path)


def _uninstall_skipped(harnesses: Sequence[str]) -> dict[str, Any]:
    """Return the result row for the harnesses that have no instructions file for the user."""
    return {
        "path": None,
        "harnesses": list(harnesses),
        "outcome": SKIPPED,
        "reason": "These harnesses have no instructions file for yourself, so only their skill copy is removed.",
    }


# What the plan says about a removal --force allowed, after the removal itself.
_FORCED = {
    "edited": ", though it was edited",
    "broken_markers": f", though its markers were broken (every {CLI_NAME} marker goes)",
    "outside_project": ", though the file links outside the project",
}


def _uninstall_plan_lines(data: dict[str, Any], *, done: bool = False) -> list[str]:
    """Describe what the uninstall does to each file and skill copy: the plan, or with `done` the result.

    Args:
        data (dict[str, Any]): The uninstall's result.
        done (bool): Say what was done, not what is done.

    Returns:
        list[str]: One line per file or skill copy that is changed or left in place.
    """
    verb = "was" if done else "is"
    lines: list[str] = []
    name = data["environment"]
    dropping = {row["path"]: row["harnesses"] for row in [*data["copies"], *data["instructions"]] if "path" in row}
    kept = {row["path"]: _kept_because(row["claimants"], name, dropping.get(row["path"], [])) for row in data["kept"]}
    for row in data["instructions"]:
        why = _FORCED.get(row.get("forced") or "", "")
        if row["outcome"] == "removed":
            lines.append(f"The skill instructions section in {row['path']} {verb} removed{why}.")
        elif row["outcome"] == "file_deleted":
            lines.append(f"File {row['path']} {verb} deleted (nothing else was in it){why}.")
        elif row["outcome"] == KEPT_SHARED:
            lines.append(f"The skill instructions section in {row['path']} {verb} {kept[row['path']]}.")
    for row in data["copies"]:
        if row["outcome"] == "deleted":
            lines.append(f"Skill copy {row['path']} {verb} deleted.")
        elif row["outcome"] == KEPT_SHARED:
            lines.append(f"Skill copy {row['path']} {verb} {kept[row['path']]}.")
        elif row["outcome"] == "left_foreign":
            lines.append(f"Skill copy {row['path']} {verb} left in place: not installed by {CLI_NAME}.")
    plural_verb = "were" if done else "are"
    for row in data["zips"]:
        if row["outcome"] == "deleted":
            notes = f" and its install notes {row['notes']} {plural_verb}" if row["notes"] else f" {verb}"
            lines.append(f"Zip {row['path']}{notes} deleted.")
            continue
        if row["outcome"] == "left_foreign":
            lines.append(f"Zip {row['path']} {verb} left in place: not written by {CLI_NAME}.")
        if row["notes"]:
            lines.append(
                f"Install notes {row['notes']} {plural_verb} left in place: no zip by {CLI_NAME} is beside them."
            )
    copies = {row["path"] for row in data["copies"]}
    for row in data["removed_shared"]:
        users = list(dict.fromkeys(_claimant_name(c, name) for c in row["claimants"]))
        what = "skill copy" if row["path"] in copies else "the skill instructions section in"
        lose = "loses" if len(users) == 1 else "lose"
        lines.append(f"--shared: {' and '.join(users)} {lose} {what} {row['path']} too.")
    return lines


def _claimant_name(claimant: dict[str, Any], name: str) -> str:
    """Name who claims a shared copy: a harness of this environment by its name, another environment as such."""
    if claimant["environment"] == name and claimant["harnesses"]:
        return " and ".join(claimant["harnesses"])
    return f"environment {claimant['environment']}"


def _kept_because(claimants: Sequence[dict[str, Any]], name: str, dropping: Sequence[str]) -> str:
    """Say who still uses a kept shared copy: this environment's other harnesses, then other environments.

    A harness the run drops a claim for that still claims the copy at another scope is named with that scope.

    Args:
        claimants (Sequence[dict[str, Any]]): The kept row's claimants.
        name (str): This environment.
        dropping (Sequence[str]): The harnesses the run drops its claims for on this copy.

    Returns:
        str: For example `kept: pi and environment hr also use it`, or `kept: codex in project /home/me
            also uses it`.
    """
    harnesses = [
        h + (_scope_named(c["project"]) if h in dropping else "")
        for c in claimants
        if c["environment"] == name
        for h in c["harnesses"]
    ]
    # An older record's entry names no harnesses: this environment is named with the entry's scope.
    harnesses += [
        f"this environment{_scope_named(c['project'])}"
        for c in claimants
        if c["environment"] == name and not c["harnesses"]
    ]
    others = list(dict.fromkeys(c["environment"] for c in claimants if c["environment"] != name))
    return kept_by(harnesses, others)


def kept_by(harnesses: Sequence[str], environments: Sequence[str]) -> str:
    """Say who still uses a kept shared copy, in the words uninstall-skill and remove both use.

    Args:
        harnesses (Sequence[str]): This environment's harnesses that still use it, each with its scope when
            the scope needs saying, or `this environment` with a scope for a claim naming no harness.
        environments (Sequence[str]): The other environments that still use it.

    Returns:
        str: For example `kept: pi also uses it`, `kept: environment hr also uses it` or `kept: pi and
            environment hr also use it`.
    """
    users = list(dict.fromkeys([*harnesses, *(f"environment {e}" for e in environments)]))
    listed = ", ".join(users[:-1]) + f" and {users[-1]}" if len(users) > 1 else "".join(users)
    return f"kept: {listed} also {'uses' if len(users) == 1 else 'use'} it"


def _scope_named(project: str | None) -> str:
    """Name a claim's scope after its harness: ` in project DIR`, or ` for the user`."""
    return f" in project {project}" if project is not None else " for the user"


def _confirm_uninstall(terminal: Terminal | None, data: dict[str, Any], retry: str) -> None:
    """Ask before removing; without a terminal, name the command that removes without a question.

    Args:
        terminal (Terminal | None): Where to ask; None when prompting is off.
        data (dict[str, Any]): The planned result.
        retry (str): The command with `--yes`.

    Raises:
        CliError: There is no terminal to ask on, or the administrator declined or cancelled.
    """
    if terminal is None:
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"{CLI_NAME} asks before it removes {SKILL_NAME}; pass --yes to remove it without a question.",
            retry,
        )
    with terminal.session():
        print_formatted_text("\n".join([*_uninstall_plan_lines(data), *_shared_retry_lines(data)]))
        try:
            agreed = questionary.confirm(f"Uninstall {SKILL_NAME}?", default=False).unsafe_ask()
        except KeyboardInterrupt, EOFError:
            agreed = False
    if not agreed:
        raise CliError(ExitCode.CANCELLED, "Nothing was removed.")


def _shared_retry_lines(data: dict[str, Any]) -> list[str]:
    """Name the commands that remove the kept shared copies too, one per scope; none when nothing is kept."""
    retries = list(dict.fromkeys(row["retry"] for row in data["kept"]))
    if not retries:
        return []
    return [
        "",
        "A shared copy stays until the last harness and environment that use it uninstall. To remove it anyway, run:",
        *(f"  {retry}" for retry in retries),
    ]


def _uninstall_human(data: dict[str, Any]) -> str:
    """Render an uninstall's result for a person."""
    if data["removed"]:
        lines = [f"Uninstalled {SKILL_NAME}:"]
    elif data["kept"]:
        lines = ["Nothing was removed:"]
    elif not data["checked"]:
        lines = [f"Nothing for {CLI_NAME} to remove here."]
    else:
        lines = [f"Nothing to remove. {CLI_NAME} looked at these places:", *(f"  {p}" for p in data["checked"])]
    lines += [f"  {line}" for line in _uninstall_plan_lines(data, done=True)]
    lines += _shared_retry_lines(data)
    lines += [line for warning in data["warnings"] for line in ("", warning)]
    for row in data["instructions"]:
        if row["outcome"] == SKIPPED:
            lines += ["", f"{', '.join(row['harnesses'])}: {row['reason']}"]
    for row in data["steps"]:
        lines += ["", f"{CLI_NAME} cannot reach {HARNESSES[row['harness']].title}; remove the skill there yourself:"]
        lines += [f"  {step}" for step in row["steps"]]
    if data["registration"]:
        lines += ["", "The server stays registered with each harness. To unregister it too:"]
    for row in data["registration"]:
        lines.append(f"\n{row['when']}:")
        lines += [f"  {step}" for step in row["steps"]]
    return "\n".join(lines)
