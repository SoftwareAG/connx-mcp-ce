#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_install_skill.py - The administrator installs the connx-sql-expert skill into harnesses on this machine.

Description:
    Through the CLI seam with the fake Docker port: the skill is copied out of the
    environment's pinned image into each chosen harness's skill directory, for the user or for one
    project, written once per shared directory, guarded by a marker file, and followed by
    copy-paste registration steps that use the environment's URL. The harness map is also checked
    directly on POSIX and Windows paths, since Windows CI cannot be faked through the CLI.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from connx_mcp import environment, skill
from connx_mcp.docker import DockerResult
from connx_mcp.instructions import Refusal
from connx_mcp.output import CliError, ExitCode
from tests.conftest import Cli, Machine, as_typed
from tests.test_lifecycle import env_dir

IMAGE = "softwareagcr.azurecr.io/connx-mcp-server-ce:15.0"
DIGEST = "softwareagcr.azurecr.io/connx-mcp-server-ce@sha256:" + "a" * 64
IMAGE_FORMAT = '{"id":{{json .Id}},"digests":{{json .RepoDigests}},"labels":{{json .Config.Labels}}}'
IN_IMAGE = "/opt/softwareag/connx/mcp/src/skills/connx-sql-expert"
IN_IMAGE_INSTRUCTIONS = "/opt/softwareag/connx/mcp/doc/INSTRUCTIONS.md"
CONTAINER = "0123456789ab"
SKILL_MD = "---\nname: connx-sql-expert\ndescription: CONNX SQL-2 query expert\n---\n# CONNX SQL\n"
REFERENCE_MD = "# SQL reference\n"
# The installing connx-mcp-ce's version, which the built-in skill instructions show.
VERSION = metadata.version("connx-mcp-ce")


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file the environments point at."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def configure(cli: Cli, cdd: Path, *extra: str, name: str = "default", pinned: bool = True) -> None:
    """Create a complete environment; `pinned` records the digest a first start would have pinned."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=pw-for-tests-only",
        "--cdd", str(cdd), *extra,
    )  # fmt: skip
    assert code == ExitCode.OK, body
    if pinned:
        host_file = Path(os.environ["MCP_ENVIRONMENT_PATH"]) / name / "host.json"
        host = json.loads(host_file.read_text(encoding="utf-8"))
        host.update(image_digest=DIGEST, image_version="15.0.3.26012")
        host_file.write_text(json.dumps(host), encoding="utf-8")


def copies_instructions(args: list[str]) -> bool:
    """Match the `docker cp` of the image's INSTRUCTIONS.md, whatever its options."""
    return args[0] == "cp" and f"{CONTAINER}:{IN_IMAGE_INSTRUCTIONS}" in args


def skill_in_image(
    cli: Cli, image: str = DIGEST, files: dict[str, str] | None = None, instructions: str | bytes | None = None
) -> None:
    """The image holds the skill: `docker cp` out of a created container writes its files.

    `instructions` is the image's INSTRUCTIONS.md, text or raw bytes; without it, `docker cp` fails as for a
    missing path.
    """
    files = files if files is not None else {"SKILL.md": SKILL_MD, "references/sql-reference.md": REFERENCE_MD}
    cli.docker.reply(["create", image], stdout=f"{CONTAINER}\n")
    cli.docker.reply(["rm", "-f", CONTAINER])

    def copy_instructions(args: list[str]) -> DockerResult:
        if instructions is None:
            return DockerResult(
                1, "", f"Error: Could not find the file {IN_IMAGE_INSTRUCTIONS} in container {CONTAINER}"
            )
        raw = instructions if isinstance(instructions, bytes) else instructions.encode("utf-8")
        Path(args[-1]).write_bytes(raw)
        return DockerResult(0, "", "")

    cli.docker.act_when(copies_instructions, copy_instructions)

    def copy(args: list[str]) -> DockerResult:
        target = Path(args[2]) / "connx-sql-expert"
        for relative, text in files.items():
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            # Bytes, as `docker cp` writes them: a text-mode write would turn \n into \r\n on Windows.
            (target / relative).write_bytes(text.encode("utf-8"))
        return DockerResult(0, "", "")

    cli.docker.act_when(lambda a: a[:2] == ["cp", f"{CONTAINER}:{IN_IMAGE}"], copy)


def installed(directory: Path) -> dict[str, str]:
    """Return the files of an installed skill, by path relative to it."""
    return {
        p.relative_to(directory).as_posix(): p.read_bytes().decode("utf-8") for p in directory.rglob("*") if p.is_file()
    }


# --- Copy from the pinned image ------------------------------------------------------------------------


def test_installs_the_skill_from_the_pinned_image_for_claude_code(cli: Cli, cdd: Path, machine: Machine) -> None:
    """The skill is copied from the pinned image into Claude Code's user directory."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.OK, body
    target = machine.home / ".claude" / "skills" / "connx-sql-expert"
    files = installed(target)
    assert files["SKILL.md"] == SKILL_MD
    assert files["references/sql-reference.md"] == REFERENCE_MD
    assert ["create", DIGEST] in cli.docker.calls
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]
    assert body["result"]["image"] == DIGEST
    assert body["result"]["image_version"] == "15.0.3.26012"
    assert body["result"]["installed"] == [{"path": str(target), "harnesses": ["claude-code"], "replaced": False}]


def test_the_installed_and_zipped_skill_keeps_the_images_bytes(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Line endings are the image's, never the platform's: the skill is copied and zipped as bytes."""
    configure(cli, cdd)
    mixed = "---\nname: connx-sql-expert\r\n---\n"
    skill_in_image(cli, files={"SKILL.md": mixed})

    code, body = cli.json("install-skill", "--harness", "claude-code")
    assert code == ExitCode.OK, body
    code, body = cli.json("install-skill", "--harness", "claude-code", "--zip", str(tmp_path / "out"))

    assert code == ExitCode.OK, body
    copied = machine.home / ".claude" / "skills" / "connx-sql-expert" / "SKILL.md"
    assert copied.read_bytes() == mixed.encode("utf-8")
    with zipfile.ZipFile(tmp_path / "out" / "connx-sql-expert.zip") as archive:
        assert archive.read("connx-sql-expert/SKILL.md") == mixed.encode("utf-8")


def test_before_a_first_start_the_skill_comes_from_the_image_the_reference_resolves_to(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """Without a pinned digest the skill comes from the resolved image, and nothing is pinned."""
    configure(cli, cdd, pinned=False)
    labels = {"org.opencontainers.image.version": "15.0.4.26100"}
    found = {"id": "sha256:" + "c" * 64, "digests": [DIGEST], "labels": labels}
    cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, IMAGE], stdout=json.dumps(found))
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.OK, body
    assert body["result"]["image_version"] == "15.0.4.26100"
    host = json.loads((machine.base / "default" / "host.json").read_text(encoding="utf-8"))
    assert "image_digest" not in host  # pinning stays the first start's job


def test_an_image_without_the_skill_fails_and_removes_its_container(cli: Cli, cdd: Path, machine: Machine) -> None:
    """An image with no skill fails with skill_not_in_image and still removes its container."""
    configure(cli, cdd)
    cli.docker.reply(["create", DIGEST], stdout=f"{CONTAINER}\n")
    cli.docker.reply(["rm", "-f", CONTAINER])
    cli.docker.reply_when(
        lambda a: a[:2] == ["cp", f"{CONTAINER}:{IN_IMAGE}"], returncode=1, stderr="Could not find the file"
    )

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.SKILL_NOT_IN_IMAGE
    assert body["error"]["code"] == "skill_not_in_image"
    assert body["error"]["next"] == "connx-mcp-ce update default"
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]
    assert not (machine.home / ".claude").exists()


def test_install_skill_needs_a_configured_environment(cli: Cli) -> None:
    """install-skill on an unknown environment points at configure."""
    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.ENVIRONMENT_NOT_FOUND
    assert body["error"]["next"] == "connx-mcp-ce configure default"


# --- Harness map ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "user", "project"),
    [
        ("claude-code", ".claude/skills", ".claude/skills"),
        ("codex", ".agents/skills", ".agents/skills"),
        ("pi", ".agents/skills", ".agents/skills"),
        ("hermes", ".hermes/skills", ".agents/skills"),
        ("agents", ".agents/skills", ".agents/skills"),
    ],
)
def test_each_harness_maps_to_its_directories_on_linux(harness: str, user: str, project: str) -> None:
    """Each harness's user and project directories on POSIX paths."""
    home = skill.home_directory("posix", {"HOME": "/home/ada"})
    entry = skill.HARNESSES[harness]

    assert skill.skill_directory(entry, home) == PurePosixPath(f"/home/ada/{user}/connx-sql-expert")
    assert skill.skill_directory(entry, home, PurePosixPath("/src/sales")) == PurePosixPath(
        f"/src/sales/{project}/connx-sql-expert"
    )


@pytest.mark.parametrize(
    ("harness", "user"),
    [
        ("claude-code", r"C:\Users\ada\.claude\skills\connx-sql-expert"),
        ("codex", r"C:\Users\ada\.agents\skills\connx-sql-expert"),
        ("hermes", r"C:\Users\ada\AppData\Local\hermes\skills\connx-sql-expert"),
    ],
)
def test_on_windows_the_home_directory_is_the_user_profile(harness: str, user: str) -> None:
    """On Windows `~` is USERPROFILE, not HOME; Hermes keeps its user skills under LOCALAPPDATA."""
    environ = {
        "USERPROFILE": r"C:\Users\ada",
        "HOME": "/c/msys/home/ada",
        "LOCALAPPDATA": r"C:\Users\ada\AppData\Local",
    }
    home = skill.home_directory("nt", environ)
    local = skill.local_app_data("nt", environ)

    assert skill.skill_directory(skill.HARNESSES[harness], home, local=local) == PureWindowsPath(user)
    project = PureWindowsPath(r"D:\work\sales")
    assert skill.skill_directory(skill.HARNESSES["hermes"], home, project, local=local) == (
        PureWindowsPath(r"D:\work\sales\.agents\skills\connx-sql-expert")
    )


def test_off_windows_there_is_no_local_app_data() -> None:
    """LOCALAPPDATA means nothing off Windows, so Hermes keeps ~/.hermes/skills there."""
    assert skill.local_app_data("posix", {"LOCALAPPDATA": "/tmp/x"}) is None


def test_on_windows_local_app_data_defaults_under_the_user_profile() -> None:
    """Without LOCALAPPDATA, Windows' default under the user profile is used."""
    assert skill.local_app_data("nt", {"USERPROFILE": r"C:\Users\ada"}) == PureWindowsPath(
        r"C:\Users\ada\AppData\Local"
    )


@dataclass
class FakeFiles:
    """The files the harness map sees: path to text, or the error reading it raises; nothing else exists."""

    contents: dict[PurePath, str | OSError] = field(default_factory=dict)

    def is_file(self, path: PurePath) -> bool:
        return path in self.contents

    def read(self, path: PurePath) -> str | None:
        found = self.contents.get(path)
        if isinstance(found, OSError):
            raise found
        return found

    def resolve(self, path: PurePath) -> PurePath:
        return path


def scope(
    os_name: str, environ: dict[str, str], project: PurePath | None = None, files: FakeFiles | None = None
) -> skill.InstructionsScope:
    """The scope install-skill would build on that OS, over fake files."""
    home = skill.home_directory(os_name, environ)
    return skill.InstructionsScope(project, home, environ, os_name, files or FakeFiles())


LINUX = {"HOME": "/home/ada"}
WINDOWS = {"USERPROFILE": r"C:\Users\ada", "HOME": "/c/msys/home/ada"}


@pytest.mark.parametrize(
    ("harness", "file"),
    [
        ("claude-code", "/home/ada/.claude/CLAUDE.md"),
        ("codex", "/home/ada/.codex/AGENTS.md"),
        ("pi", "/home/ada/.pi/agent/AGENTS.md"),
    ],
)
def test_each_harness_maps_to_its_user_instructions_file_on_linux(harness: str, file: str) -> None:
    """At user scope each harness reads one file under the home directory."""
    found = skill.instructions_file(skill.HARNESSES[harness], scope("posix", LINUX))

    assert found == PurePosixPath(file)


@pytest.mark.parametrize(
    ("harness", "file"),
    [
        ("claude-code", r"C:\Users\ada\.claude\CLAUDE.md"),
        ("codex", r"C:\Users\ada\.codex\AGENTS.md"),
        ("pi", r"C:\Users\ada\.pi\agent\AGENTS.md"),
    ],
)
def test_on_windows_the_user_instructions_files_are_under_the_user_profile(harness: str, file: str) -> None:
    """On Windows the user-scope files are under USERPROFILE, not HOME."""
    found = skill.instructions_file(skill.HARNESSES[harness], scope("nt", WINDOWS))

    assert found == PureWindowsPath(file)


@pytest.mark.parametrize(
    ("harness", "file"),
    [
        ("claude-code", r"D:\work\sales\AGENTS.md"),
        ("codex", r"D:\work\sales\AGENTS.md"),
        ("pi", r"D:\work\sales\AGENTS.md"),
        ("hermes", r"D:\work\sales\AGENTS.md"),
        ("agents", r"D:\work\sales\AGENTS.md"),
    ],
)
def test_on_windows_the_project_instructions_files_are_under_the_project(harness: str, file: str) -> None:
    """A Windows project without Claude or override files gets AGENTS.md for every harness."""
    found = skill.instructions_file(skill.HARNESSES[harness], scope("nt", WINDOWS, PureWindowsPath(r"D:\work\sales")))

    assert found == PureWindowsPath(file)


@pytest.mark.parametrize(
    ("os_name", "environ", "file"),
    [
        ("posix", {**LINUX, "CODEX_HOME": "/srv/codex"}, PurePosixPath("/srv/codex/AGENTS.md")),
        ("posix", {**LINUX, "CODEX_HOME": ""}, PurePosixPath("/home/ada/.codex/AGENTS.md")),
        ("nt", {**WINDOWS, "CODEX_HOME": r"D:\codex"}, PureWindowsPath(r"D:\codex\AGENTS.md")),
    ],
)
def test_codex_user_instructions_follow_codex_home(os_name: str, environ: dict[str, str], file: PurePath) -> None:
    """CODEX_HOME, when set, replaces ~/.codex as Codex's home directory."""
    assert skill.instructions_file(skill.HARNESSES["codex"], scope(os_name, environ)) == file


@pytest.mark.parametrize("harness", ["hermes", "agents"])
@pytest.mark.parametrize(("os_name", "environ"), [("posix", LINUX), ("nt", WINDOWS)])
def test_hermes_and_agents_have_no_user_instructions_file(harness: str, os_name: str, environ: dict[str, str]) -> None:
    """Hermes and the shared agents directory read no instructions file at user scope."""
    assert skill.instructions_file(skill.HARNESSES[harness], scope(os_name, environ)) is None


def test_claude_desktop_takes_no_instructions_file() -> None:
    """Claude Desktop takes the instructions through its install notes only, at either scope."""
    entry = skill.HARNESSES["claude-desktop"]

    assert skill.instructions_file(entry, scope("posix", LINUX)) is None
    assert skill.instructions_file(entry, scope("posix", LINUX, PurePosixPath("/src/sales"))) is None


def test_codex_user_instructions_prefer_agents_override_md() -> None:
    """Codex reads AGENTS.override.md in its home directory instead of AGENTS.md when it exists."""
    files = FakeFiles({PurePosixPath("/srv/codex/AGENTS.md"): "", PurePosixPath("/srv/codex/AGENTS.override.md"): ""})

    found = skill.instructions_file(
        skill.HARNESSES["codex"], scope("posix", {**LINUX, "CODEX_HOME": "/srv/codex"}, files=files)
    )

    assert found == PurePosixPath("/srv/codex/AGENTS.override.md")


@pytest.mark.parametrize(
    ("existing", "file"),
    [((), "AGENTS.md"), (("CLAUDE.md",), "CLAUDE.md"), (("CLAUDE.md", "AGENTS.md"), "AGENTS.md")],
)
def test_pi_user_instructions_use_claude_md_only_when_it_is_the_only_file(existing: tuple[str, ...], file: str) -> None:
    """Pi reads ~/.pi/agent/AGENTS.md, or the CLAUDE.md there when it is the only file."""
    agent = PurePosixPath("/home/ada/.pi/agent")
    files = FakeFiles({agent / name: "# Rules\n" for name in existing})

    assert skill.instructions_file(skill.HARNESSES["pi"], scope("posix", LINUX, files=files)) == agent / file


SALES = PurePosixPath("/home/ada/work/sales")


@pytest.mark.parametrize(
    ("existing", "file"),
    [
        ((), "AGENTS.md"),
        (("/home/ada/.claude/CLAUDE.md",), "AGENTS.md"),
        (("/etc/claude-code/CLAUDE.md",), "AGENTS.md"),
        (("/home/ada/CLAUDE.md",), "CLAUDE.md"),
        (("/home/ada/work/sales/CLAUDE.local.md",), "CLAUDE.md"),
    ],
)
def test_claude_codes_user_and_managed_files_do_not_count_as_project_files(
    existing: tuple[str, ...], file: str
) -> None:
    """Only project Claude files send the section to CLAUDE.md; ~/CLAUDE.md above the project is one of them."""
    files = FakeFiles({PurePosixPath(p): "# Rules\n" for p in existing})

    found = skill.instructions_file(skill.HARNESSES["claude-code"], scope("posix", LINUX, SALES, files))

    assert found == SALES / file


IMPORTS = [
    ("{agents}\n", True),
    ("See {agents} for the shared rules.\n", True),
    ("```\n{agents}\n```\n", False),
    ("~~~~markdown\n{agents}\n~~~~\n", False),
    ("Rules:\n\n    {agents}\n", False),
    ("Write ` {agents} ` to import it.\n", False),
    ("<!-- {agents} -->\n", False),
    ("<!--\n{agents}\n-->\n", False),
    pytest.param("- Shared rules:\n\n    {agents}\n", True, id="list-continuation"),
    pytest.param("Use ` here.\n\n{agents}\n\nAnd ` there.\n", True, id="backtick-across-paragraphs"),
    pytest.param("# Rules\n    {agents}\n", False, id="indented-after-heading"),
    pytest.param("Rules.\n```\nx\n```\n    {agents}\n", False, id="indented-after-fence"),
]
# How each project Claude file imports the project's AGENTS.md: imports resolve from the importing file.
IMPORT_SPELLINGS = {"CLAUDE.md": "@AGENTS.md", ".claude/CLAUDE.md": "@../AGENTS.md", "CLAUDE.local.md": "@./AGENTS.md"}


@pytest.mark.parametrize(("text", "imports"), IMPORTS)
@pytest.mark.parametrize("claude_file", list(IMPORT_SPELLINGS))
def test_only_a_prose_import_in_a_project_claude_file_sends_claude_code_to_agents_md(
    text: str, imports: bool, claude_file: str
) -> None:
    """Claude Code expands an import in prose, not in a comment, code block or code span, in any project Claude file."""
    files = FakeFiles({SALES / claude_file: text.format(agents=IMPORT_SPELLINGS[claude_file])})

    found = skill.instructions_file(skill.HARNESSES["claude-code"], scope("posix", LINUX, SALES, files))

    assert found == SALES / ("AGENTS.md" if imports else "CLAUDE.md")


@pytest.mark.parametrize(
    ("claude_file", "line", "imports"),
    [
        ("CLAUDE.md", "@./AGENTS.md", True),
        ("CLAUDE.local.md", "@AGENTS.md", True),
        ("CLAUDE.md", "@../AGENTS.md", False),
        (".claude/CLAUDE.md", "@AGENTS.md", False),
        (".claude/CLAUDE.md", "@./AGENTS.md", False),
        ("CLAUDE.md", "@docs/AGENTS.md", False),
    ],
)
def test_only_an_import_that_resolves_to_the_projects_agents_md_counts(
    claude_file: str, line: str, imports: bool
) -> None:
    """An import resolves from the file that holds it: .claude/CLAUDE.md reaches the project's AGENTS.md as ../."""
    files = FakeFiles({SALES / claude_file: f"{line}\n"})

    found = skill.instructions_file(skill.HARNESSES["claude-code"], scope("posix", LINUX, SALES, files))

    assert found == SALES / ("AGENTS.md" if imports else "CLAUDE.md")


def test_an_unreadable_project_claude_file_is_refused_by_name() -> None:
    """A Claude file that exists but cannot be read is named in a refusal that --force does not override."""
    claude = SALES / "CLAUDE.md"
    files = FakeFiles({claude: PermissionError(13, "Permission denied", str(claude))})

    found = skill.instructions_targets(["claude-code", "codex"], scope("posix", LINUX, SALES, files))

    assert found.refusals == [Refusal(Path(claude), "cannot be read: Permission denied", forceable=False)]
    assert found.files == {Path(SALES / "AGENTS.md"): ["codex"]}


def test_hermes_and_agents_at_user_scope_are_skipped_targets() -> None:
    """The targets name the harnesses with no user-scope file, apart from the files written."""
    found = skill.instructions_targets(["hermes", "claude-code", "agents", "claude-desktop"], scope("posix", LINUX))

    assert found.files == {Path("/home/ada/.claude/CLAUDE.md"): ["claude-code"]}
    assert found.skipped == ["hermes", "agents"]


def test_harnesses_sharing_a_directory_get_one_copy_and_claude_code_its_own(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """Codex, Pi and agents share one copy; Claude Code gets its own."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json(
        "install-skill", "--harness", "codex", "--harness", "pi", "--harness", "agents", "--harness", "claude-code"
    )

    assert code == ExitCode.OK, body
    shared = machine.home / ".agents" / "skills" / "connx-sql-expert"
    claude = machine.home / ".claude" / "skills" / "connx-sql-expert"
    assert body["result"]["installed"] == [
        {"path": str(shared), "harnesses": ["codex", "pi", "agents"], "replaced": False},
        {"path": str(claude), "harnesses": ["claude-code"], "replaced": False},
    ]
    assert installed(claude)["SKILL.md"] == SKILL_MD
    assert [c[:2] for c in cli.docker.calls].count(["cp", f"{CONTAINER}:{IN_IMAGE}"]) == 1


def test_project_installs_go_under_the_project(cli: Cli, cdd: Path, machine: Machine, tmp_path: Path) -> None:
    """--project writes under the project, not the home directory."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = sales_project(tmp_path)

    code, body = cli.json("install-skill", "--harness", "hermes", "--harness", "claude-code", "--project", str(project))

    assert code == ExitCode.OK, body
    assert body["result"]["scope"] == "project"
    assert installed(project / ".agents" / "skills" / "connx-sql-expert")["SKILL.md"] == SKILL_MD
    assert installed(project / ".claude" / "skills" / "connx-sql-expert")["SKILL.md"] == SKILL_MD
    assert not (machine.home / ".hermes").exists()


def test_user_and_project_together_are_refused(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """--user and --project together are a usage error."""
    configure(cli, cdd)

    code, body = cli.json("install-skill", "--harness", "codex", "--user", "--project", str(tmp_path))

    assert code == ExitCode.USAGE
    assert body["error"]["code"] == "usage"


def test_an_unknown_harness_is_refused_naming_the_known_ones(cli: Cli, cdd: Path) -> None:
    """An unknown harness is refused with the known ones listed."""
    configure(cli, cdd)

    code, body = cli.json("install-skill", "--harness", "cursor")

    assert code == ExitCode.USAGE
    assert "claude-code, codex, pi, hermes, agents" in body["error"]["message"]


# --- Choosing harnesses ---------------------------------------------------------------------------------


def test_without_harness_no_input_fails_naming_the_detected_set(cli: Cli, cdd: Path, machine: Machine) -> None:
    """--no-input without --harness fails, naming the detected harnesses as the next command."""
    configure(cli, cdd)
    cli.docker.executables.update(claude="/usr/local/bin/claude", hermes="/usr/local/bin/hermes")

    code, body = cli.json("install-skill", "--no-input")

    assert code == ExitCode.INPUT_REQUIRED
    assert body["error"]["code"] == "input_required"
    assert "claude-code, hermes" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce install-skill default --harness claude-code --harness hermes"
    assert not (machine.home / ".claude").exists()


def test_without_harness_a_terminal_offers_the_detected_ones_ticked(cli: Cli, cdd: Path, machine: Machine) -> None:
    """On a terminal the detected harnesses are offered ticked."""
    configure(cli, cdd)
    skill_in_image(cli)
    cli.docker.executables.update(codex="/usr/local/bin/codex")

    result, screen = cli.typed("install-skill", keys=[""])

    assert result.exit_code == ExitCode.OK, result.output
    assert "codex (Codex)" in screen
    assert "claude-code (Claude Code)" in screen
    assert installed(machine.home / ".agents" / "skills" / "connx-sql-expert")["SKILL.md"] == SKILL_MD
    assert not (machine.home / ".claude").exists()


def test_a_cancelled_harness_choice_exits_cancelled(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Input ending at the harness choice installs nothing and exits 100."""
    configure(cli, cdd)

    result, _ = cli.typed("install-skill", keys=[])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert not (machine.home / ".agents").exists()


def test_an_unreadable_install_record_is_an_unreadable_file(cli: Cli, cdd: Path, machine: Machine) -> None:
    """A damaged install record exits 92, naming the file."""
    configure(cli, cdd)
    record = machine.base / "default" / skill.INSTALL_RECORD
    record.write_text("{not json", encoding="utf-8")

    with pytest.raises(CliError) as raised:
        skill.installed(environment.load("default"))

    assert raised.value.exit_code == ExitCode.FILE_UNREADABLE
    assert str(record) in raised.value.message


def test_an_install_record_of_the_wrong_shape_is_an_unreadable_file(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Valid JSON that is not an install record exits 92 as damaged JSON does, never reads as empty."""
    configure(cli, cdd)
    record = machine.base / "default" / skill.INSTALL_RECORD
    record.write_text('{"copies": "x"}', encoding="utf-8")

    with pytest.raises(CliError) as raised:
        skill.installed(environment.load("default"))

    assert raised.value.exit_code == ExitCode.FILE_UNREADABLE
    assert str(record) in raised.value.message


def test_choosing_no_harness_on_a_terminal_installs_nothing(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Choosing nothing on a terminal installs nothing and exits 122."""
    configure(cli, cdd)

    result, _ = cli.typed("install-skill", keys=[""])

    assert result.exit_code == ExitCode.NO_HARNESS_CHOSEN
    assert not (machine.home / ".agents").exists()


# --- Marker file and overwrites -------------------------------------------------------------------------


def test_the_marker_records_the_cli_and_image_versions(cli: Cli, cdd: Path, machine: Machine) -> None:
    """The marker file records the installing CLI and image versions."""
    configure(cli, cdd)
    skill_in_image(cli)

    cli.json("install-skill", "--harness", "claude-code")

    marker_file = machine.home / ".claude" / "skills" / "connx-sql-expert" / ".connx-mcp-ce-install.json"
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    assert marker["installed_by"] == "connx-mcp-ce"
    assert marker["cli_version"] == VERSION
    assert marker["image"] == DIGEST
    assert marker["image_version"] == "15.0.3.26012"


def test_a_skill_connx_mcp_did_not_install_is_refused_unless_forced(cli: Cli, cdd: Path, machine: Machine) -> None:
    """A foreign skill blocks the whole install until --force replaces it."""
    configure(cli, cdd)
    skill_in_image(cli)
    own = machine.home / ".claude" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("my own edits\n", encoding="utf-8")

    code, body = cli.json("install-skill", "--harness", "codex", "--harness", "claude-code")

    assert code == ExitCode.SKILL_EXISTS
    assert body["error"]["code"] == "skill_exists"
    assert str(own) in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce install-skill default --harness codex --harness claude-code --force"
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "my own edits\n"
    assert not (machine.home / ".agents").exists()  # nothing is written when one target is refused
    assert not [c for c in cli.docker.calls if c[0] == "create"]

    code, body = cli.json("install-skill", "--harness", "claude-code", "--force")

    assert code == ExitCode.OK, body
    assert installed(own)["SKILL.md"] == SKILL_MD
    assert body["result"]["installed"][0]["replaced"] is True


def test_the_force_command_after_a_refusal_keeps_the_project_scope(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Following the refusal's next command forces the same project install, never a user-scope one."""
    configure(cli, cdd, name="sales")
    project = tmp_path / "sales"
    own = project / ".claude" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("my own edits\n", encoding="utf-8")

    code, body = cli.json("install-skill", "sales", "--harness", "claude-code", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS
    assert body["error"]["next"] == (
        f"connx-mcp-ce install-skill sales --harness claude-code --project {as_typed(project)} --force"
    )


def test_without_harness_no_input_keeps_the_project_in_the_next_command(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The --no-input next command keeps --project, so following it installs in the same scope."""
    configure(cli, cdd)
    cli.docker.executables.update(codex="/usr/local/bin/codex")

    code, body = cli.json("install-skill", "--no-input", "--project", str(tmp_path))

    assert code == ExitCode.INPUT_REQUIRED
    assert body["error"]["next"] == f"connx-mcp-ce install-skill default --harness codex --project {as_typed(tmp_path)}"


def test_a_skill_connx_mcp_installed_is_replaced_without_force(cli: Cli, cdd: Path, machine: Machine) -> None:
    """A copy connx-mcp-ce installed is replaced whole without --force."""
    configure(cli, cdd)
    skill_in_image(cli, files={"SKILL.md": "old\n", "references/old.md": "gone\n"})
    cli.json("install-skill", "--harness", "claude-code")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.OK, body
    files = installed(machine.home / ".claude" / "skills" / "connx-sql-expert")
    assert files["SKILL.md"] == SKILL_MD
    assert "references/old.md" not in files
    assert body["result"]["installed"][0]["replaced"] is True


# --- Registration steps ---------------------------------------------------------------------------------


def server_env(*lines: str) -> None:
    """Append server settings to the default environment, as a hand edit would."""
    env_file = Path(os.environ["MCP_ENVIRONMENT_PATH"]) / "default" / "server.env"
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def registration(body: dict) -> dict[str, dict]:
    """Return the registration rows of an install-skill result, by harness."""
    return {r["harness"]: r for r in body["result"]["registration"]}


def test_registration_steps_use_the_environment_url_and_register_nothing(cli: Cli, cdd: Path) -> None:
    """Every harness gets its own add command with the environment URL; nothing is run."""
    configure(cli, cdd, "--set", "MCP_PATH=/sql")
    skill_in_image(cli)
    host_file = Path(os.environ["MCP_ENVIRONMENT_PATH"]) / "default" / "host.json"
    host = json.loads(host_file.read_text(encoding="utf-8"))
    host_file.write_text(json.dumps({**host, "published_port": 8123}), encoding="utf-8")

    code, body = cli.json(
        "install-skill", "--harness", "claude-code", "--harness", "codex", "--harness", "pi", "--harness", "hermes"
    )

    assert code == ExitCode.OK, body
    assert body["result"]["url"] == "http://localhost:8123/sql"
    rows = registration(body)
    assert all(rows[h]["confirmed"] is True for h in ("claude-code", "codex", "pi", "hermes"))
    assert "claude mcp add --transport http connx http://localhost:8123/sql" in rows["claude-code"]["steps"]
    assert "codex mcp add connx --url http://localhost:8123/sql" in rows["codex"]["steps"]
    assert "pi mcp add connx --url http://localhost:8123/sql" in rows["pi"]["steps"]
    assert "hermes mcp add connx --url http://localhost:8123/sql" in rows["hermes"]["steps"]
    for harness in ("codex", "pi", "hermes"):
        text = " ".join(rows[harness]["steps"])
        assert "Not confirmed" not in text
        assert "login" not in text  # OAuth is off
        assert "ssl_verify" not in text  # TLS is off
    assert {c[0] for c in cli.docker.calls} <= {"version", "compose", "create", "cp", "rm"}


def test_pi_offers_a_project_registration_that_needs_a_trusted_project(cli: Cli, cdd: Path) -> None:
    """Pi registers for one project with -l, and reads that file only in a trusted project."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "pi")

    assert code == ExitCode.OK, body
    text = " ".join(registration(body)["pi"]["steps"])
    assert "pi mcp add -l connx --url http://localhost:8000/mcp" in text
    assert ".pi/mcp.json" in text
    assert "trust" in text


def test_with_oauth_the_steps_dial_the_public_url(cli: Cli, cdd: Path) -> None:
    """With OAuth on every harness dials the public base URL and signs in through its own login command."""
    configure(cli, cdd)
    server_env("MCP_OAUTH_PROVIDER=keycloak", "MCP_PUBLIC_BASE_URL=https://mcp.example.com/")
    skill_in_image(cli)

    code, body = cli.json(
        "install-skill", "--harness", "claude-code", "--harness", "codex", "--harness", "pi", "--harness", "hermes"
    )

    assert code == ExitCode.OK, body
    assert body["result"]["url"] == "https://mcp.example.com/mcp"
    steps = {h: r["steps"] for h, r in registration(body).items()}
    assert "claude mcp add --transport http connx https://mcp.example.com/mcp" in steps["claude-code"]
    assert any("browser" in s for s in steps["claude-code"])
    assert "codex mcp add connx --url https://mcp.example.com/mcp" in steps["codex"]
    assert "codex mcp login connx" in steps["codex"]
    assert "pi mcp login connx" in steps["pi"]
    assert "hermes mcp add connx --url https://mcp.example.com/mcp --auth oauth" in steps["hermes"]
    assert any(s.startswith("hermes mcp login connx") for s in steps["hermes"])
    assert any("token audience" in s for s in steps["hermes"])


def test_with_tls_hermes_is_told_how_to_trust_a_private_certificate(cli: Cli, cdd: Path) -> None:
    """With TLS on Hermes gets the ssl_verify hint; Codex and Pi say their CA setting is not confirmed."""
    configure(cli, cdd)
    server_env("MCP_TLS_ENABLED=true", "MCP_TLS_HOSTS=mcp.example.com")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "codex", "--harness", "pi", "--harness", "hermes")

    assert code == ExitCode.OK, body
    text = {h: " ".join(r["steps"]) for h, r in registration(body).items()}
    assert "ssl_verify" in text["hermes"]
    assert "self-signed" in text["hermes"]
    assert "Not confirmed" not in text["hermes"]
    for harness in ("codex", "pi"):
        assert "Not confirmed" in text[harness]
        assert "self-signed" in text[harness]


def test_hermes_project_skills_need_hermes_skills_trust(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A project install for Hermes says the project's skills load only after hermes skills trust."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "hermes", "--project", str(tmp_path))

    assert code == ExitCode.OK, body
    assert any("hermes skills trust" in s for s in registration(body)["hermes"]["steps"])


def test_hermes_user_install_does_not_mention_project_trust(cli: Cli, cdd: Path) -> None:
    """A user install for Hermes needs no project trust."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "hermes")

    assert code == ExitCode.OK, body
    assert not any("hermes skills trust" in s for s in registration(body)["hermes"]["steps"])


def test_human_output_lists_the_copies_and_the_steps(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Human output lists each copy and the registration steps."""
    configure(cli, cdd)
    skill_in_image(cli)

    result = cli("install-skill", "--harness", "claude-code")

    assert result.exit_code == ExitCode.OK, result.output
    assert str(machine.home / ".claude" / "skills" / "connx-sql-expert") in result.stdout
    assert "nothing was registered for you" in result.stdout
    assert "claude mcp add --transport http connx http://localhost:8000/mcp" in result.stdout


# --- Zip and Claude Desktop -----------------------------------------------------------------------------


def zipped(path: Path) -> dict[str, str]:
    """Return the files in a zip, by their name in it."""
    with zipfile.ZipFile(path) as archive:
        return {n: archive.read(n).decode("utf-8") for n in archive.namelist() if not n.endswith("/")}


def test_zip_writes_the_skill_zip_and_install_steps_for_each_harness(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """--zip hands the skill over: a zip whose folder is named after the skill, plus steps per harness."""
    configure(cli, cdd)
    skill_in_image(cli)
    out = tmp_path / "handover"

    code, body = cli.json("install-skill", "--zip", str(out), "--harness", "claude-code", "--harness", "codex")

    assert code == ExitCode.OK, body
    archive = out / "connx-sql-expert.zip"
    assert zipped(archive) == {
        "connx-sql-expert/SKILL.md": SKILL_MD,
        "connx-sql-expert/references/sql-reference.md": REFERENCE_MD,
    }
    with zipfile.ZipFile(archive) as handle:
        assert handle.comment == b"Written by connx-mcp-ce"
    assert body["result"]["installed"] == []
    assert not (machine.home / ".claude").exists()
    assert not (machine.home / ".agents").exists()
    handover = body["result"]["zip"]
    assert handover["path"] == str(archive)
    steps = {r["harness"]: " ".join(r["steps"]) for r in handover["steps"]}
    assert "~/.claude/skills" in steps["claude-code"]
    assert "~/.agents/skills" in steps["codex"]
    instructions = Path(handover["notes"]).read_text(encoding="utf-8")
    assert "~/.claude/skills" in instructions
    assert "claude mcp add --transport http connx http://localhost:8000/mcp" in instructions


def test_zip_replaces_an_earlier_zip(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A second --zip into the same directory replaces the earlier zip and says so."""
    configure(cli, cdd)
    skill_in_image(cli, files={"SKILL.md": "old\n", "references/old.md": "gone\n"})
    cli.json("install-skill", "--zip", str(tmp_path), "--harness", "codex")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "codex")

    assert code == ExitCode.OK, body
    assert body["result"]["zip"]["replaced"] is True
    assert "connx-sql-expert/references/old.md" not in zipped(tmp_path / "connx-sql-expert.zip")


def test_claude_desktop_always_gets_a_zip_in_the_working_directory(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Desktop has no skills directory: it gets a zip, while Claude Code is still installed."""
    configure(cli, cdd)
    skill_in_image(cli)
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)

    code, body = cli.json("install-skill", "--harness", "claude-desktop", "--harness", "claude-code")

    assert code == ExitCode.OK, body
    assert zipped(here / "connx-sql-expert.zip")["connx-sql-expert/SKILL.md"] == SKILL_MD
    target = machine.home / ".claude" / "skills" / "connx-sql-expert"
    assert body["result"]["installed"] == [{"path": str(target), "harnesses": ["claude-code"], "replaced": False}]
    assert [r["harness"] for r in body["result"]["zip"]["steps"]] == ["claude-desktop"]
    assert [c[:2] for c in cli.docker.calls].count(["cp", f"{CONTAINER}:{IN_IMAGE}"]) == 1


def test_claude_desktop_steps_upload_the_zip_and_explain_the_public_url(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The Desktop steps upload the zip, add a connector, and say a connector needs a public URL or mcp-remote."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "claude-desktop")

    assert code == ExitCode.OK, body
    upload = " ".join(body["result"]["zip"]["steps"][0]["steps"])
    assert "connx-sql-expert.zip" in upload
    assert "Settings > Capabilities" in upload
    assert "Code execution and file creation" in upload
    assert "Customize > Skills" in upload
    assert "+ > Create skill > Upload a skill" in upload
    rows = body["result"]["registration"][0]
    assert rows["harness"] == "claude-desktop"
    text = " ".join(rows["steps"])
    assert "Customize > Connectors" in text
    assert "+ Add > Add custom connector" in text
    assert "Team or Enterprise" in text
    assert "owner" in text
    assert "publicly reachable" in text
    assert "mcp-remote" in text
    assert "community practice" in text
    assert "Node" in text
    assert '"args": ["mcp-remote", "http://localhost:8000/mcp"]' in text


def test_with_oauth_claude_desktop_and_its_bridge_dial_the_public_url(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """With OAuth on the mcp-remote bridge dials the public URL, the token audience, and the OAuth client is named."""
    configure(cli, cdd)
    server_env("MCP_OAUTH_PROVIDER=keycloak", "MCP_PUBLIC_BASE_URL=https://mcp.example.com/")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "claude-desktop")

    assert code == ExitCode.OK, body
    text = " ".join(registration(body)["claude-desktop"]["steps"])
    assert '"args": ["mcp-remote", "https://mcp.example.com/mcp"]' in text
    assert "http://localhost:8000/mcp" not in text
    assert "OAuth client" in text
    assert "Register automatically" in text
    assert "Use your own OAuth client" in text
    assert "advanced settings" not in text


def test_with_tls_the_claude_desktop_bridge_is_told_to_trust_the_certificate(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """With TLS on the mcp-remote bridge runs under Node, which needs NODE_EXTRA_CA_CERTS for a self-signed cert."""
    configure(cli, cdd)
    server_env("MCP_TLS_ENABLED=true", "MCP_TLS_HOSTS=mcp.example.com")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "claude-desktop")

    assert code == ExitCode.OK, body
    assert "NODE_EXTRA_CA_CERTS" in " ".join(registration(body)["claude-desktop"]["steps"])


def test_zip_steps_for_hermes_name_the_windows_directory_and_project_trust(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The handed-over Hermes steps name its Windows skills directory and the project trust command."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "hermes")

    assert code == ExitCode.OK, body
    steps = " ".join(body["result"]["zip"]["steps"][0]["steps"])
    assert r"%LOCALAPPDATA%\hermes\skills" in steps
    assert "hermes skills trust" in steps
    assert "connx-sql-expert/SKILL.md sits directly in that directory" in steps
    instructions = Path(body["result"]["zip"]["notes"]).read_text(encoding="utf-8")
    assert instructions.startswith("Install the connx-sql-expert skill")


def test_a_zip_connx_mcp_did_not_write_in_the_working_directory_is_refused_unless_forced(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connx-sql-expert.zip someone else left in the working directory is kept unless --force."""
    configure(cli, cdd)
    monkeypatch.chdir(tmp_path)
    theirs = tmp_path / "connx-sql-expert.zip"
    theirs.write_bytes(b"their own file")

    code, body = cli.json("install-skill", "--harness", "claude-desktop")

    assert code == ExitCode.SKILL_EXISTS, body
    assert body["error"]["code"] == "skill_exists"
    assert str(theirs) in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce install-skill default --harness claude-desktop --force"
    assert theirs.read_bytes() == b"their own file"
    assert not any(c[0] == "create" for c in cli.docker.calls)

    skill_in_image(cli)
    code, body = cli.json("install-skill", "--harness", "claude-desktop", "--force")

    assert code == ExitCode.OK, body
    assert body["result"]["zip"]["replaced"] is True
    assert zipped(theirs)["connx-sql-expert/SKILL.md"] == SKILL_MD


def test_human_output_names_the_zip_its_steps_file_and_the_public_url_rule(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A person sees where the zip and its steps went, the upload step and the public-URL rule."""
    configure(cli, cdd)
    skill_in_image(cli)

    result = cli("install-skill", "--zip", str(tmp_path), "--harness", "claude-desktop")

    assert result.exit_code == ExitCode.OK, result.output
    assert str(tmp_path / "connx-sql-expert.zip") in result.stdout
    assert str(tmp_path / "connx-sql-expert-install.txt") in result.stdout
    assert "Upload connx-sql-expert.zip as it is" in result.stdout
    assert "publicly reachable" in result.stdout
    assert "Installed" not in result.stdout


def test_claude_desktop_is_zip_only_in_the_harness_map() -> None:
    """Claude Desktop has no skills directory, so a directory install skips it."""
    desktop = skill.HARNESSES["claude-desktop"]
    home = PurePosixPath("/home/ada")

    assert skill.skill_directory(desktop, home) is None
    assert skill.targets(["claude-desktop", "claude-code"], home, None) == {
        PurePosixPath("/home/ada/.claude/skills/connx-sql-expert"): ["claude-code"]
    }


def test_claude_desktop_is_detected_by_its_configuration_directory(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Claude Desktop has no executable on PATH; its configuration directory shows it is installed."""
    configure(cli, cdd)
    (machine.home / ".config" / "Claude").mkdir(parents=True)

    code, body = cli.json("install-skill", "--no-input")

    assert code == ExitCode.INPUT_REQUIRED
    assert body["error"]["next"] == "connx-mcp-ce install-skill default --harness claude-desktop"


def test_zip_with_project_is_refused(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """--zip installs nowhere on this machine, so --project with it is a usage error."""
    configure(cli, cdd)

    code, body = cli.json("install-skill", "--harness", "codex", "--zip", str(tmp_path), "--project", str(tmp_path))

    assert code == ExitCode.USAGE
    assert body["error"]["code"] == "usage"


def test_a_zip_that_cannot_be_written_fails_with_its_own_code(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A directory where the zip should go fails with zip_failed and the container is still removed."""
    configure(cli, cdd)
    skill_in_image(cli)
    (tmp_path / "connx-sql-expert.zip").mkdir()

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "codex")

    assert code == ExitCode.ZIP_FAILED
    assert body["error"]["code"] == "zip_failed"
    assert str(tmp_path / "connx-sql-expert.zip") in body["error"]["message"]
    assert not (tmp_path / "connx-sql-expert.zip.partial").exists()
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]


def test_a_failed_zip_after_the_copies_names_what_was_already_written(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's copy and instructions file are written before Claude Desktop's zip fails; the error says so."""
    configure(cli, cdd)
    skill_in_image(cli)
    here = tmp_path / "here"
    (here / "connx-sql-expert.zip").mkdir(parents=True)
    monkeypatch.chdir(here)

    code, body = cli.json("install-skill", "--harness", "claude-code", "--harness", "claude-desktop")

    assert code == ExitCode.ZIP_FAILED, body
    message = body["error"]["message"]
    assert str(here / "connx-sql-expert.zip") in message
    assert "Written before the failure:" in message
    assert str(machine.home / ".claude" / "skills" / "connx-sql-expert") in message
    assert str(machine.home / ".claude" / "CLAUDE.md") in message


def test_the_force_command_keeps_every_harness_and_user(cli: Cli, cdd: Path, machine: Machine) -> None:
    """The refusal's next command keeps every --harness and --user, then adds --force."""
    configure(cli, cdd)
    own = machine.home / ".claude" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)

    code, body = cli.json("install-skill", "--harness", "claude-code", "--harness", "claude-desktop", "--user")

    assert code == ExitCode.SKILL_EXISTS
    assert body["error"]["next"] == (
        "connx-mcp-ce install-skill default --harness claude-code --harness claude-desktop --user --force"
    )


# --- Skill instructions ---------------------------------------------------------------------------------

BUILT_IN_SECTION = f"""<!-- connx-mcp-ce begin version={VERSION} -->
## CONNX MCP Server

Always load and use the `connx-sql-expert` skill before using the CONNX MCP Server.

## CONNX queries

When using the `connx-sql-expert` skill:

- Read the catalog first (the `file:///catalog.md` resource, or the `read_catalog` tool if the resource isn't \
available). Don't take column names from `describe_table` or from memory.
- Write the PRE-FLIGHT block in full before each `read_data` call, then repeat it word for word, as a fenced code \
block, at the top of the message that presents the results. Text before a tool call may show up only as a summary, \
and a summary doesn't count.
<!-- connx-mcp-ce end -->"""
# The AGENTS.md install-skill creates: a new file ends its lines the way the OS does.
CREATED_AGENTS_MD = f"# AGENTS.md\n\n{BUILT_IN_SECTION}\n".replace("\n", os.linesep)


def sales_project(tmp_path: Path) -> Path:
    """Return a new, empty project directory."""
    project = tmp_path / "sales"
    project.mkdir()
    return project


def install_for_project(cli: Cli, project: Path, *harnesses: str) -> dict:
    """Install the skill and its instructions for a project; return the JSON result."""
    skill_in_image(cli)
    flags = [p for h in harnesses for p in ("--harness", h)]
    code, body = cli.json("install-skill", *flags, "--project", str(project))
    assert code == ExitCode.OK, body
    return body["result"]


def test_a_project_without_an_instructions_file_gets_agents_md(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """codex at project scope creates AGENTS.md: the heading, then one section with the built-in text."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    result = install_for_project(cli, project, "codex")

    agents = project / "AGENTS.md"
    assert agents.read_bytes().decode("utf-8") == CREATED_AGENTS_MD
    assert result["instructions"] == [
        {
            "path": str(agents),
            "harnesses": ["codex"],
            "outcome": "created",
            "source": "built-in",
            "version": VERSION,
        }
    ]


@pytest.mark.parametrize("own", ["# Ours\n\nKeep this.\n", "Ends in a blank line.\n\n", "No final line ending", ""])
def test_an_existing_agents_md_keeps_its_text_and_gets_the_section_at_the_end(
    cli: Cli, cdd: Path, tmp_path: Path, own: str
) -> None:
    """The administrator's text stays byte for byte; the section follows it after a blank line."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(own.encode("utf-8"))

    result = install_for_project(cli, project, "hermes")

    content = (project / "AGENTS.md").read_bytes().decode("utf-8")
    assert content.startswith(own)
    expected_gap = "" if not own or own.endswith("\n\n") else "\n" if own.endswith("\n") else "\n\n"
    assert content == f"{own}{expected_gap}{BUILT_IN_SECTION}\n"
    assert result["instructions"][0]["outcome"] == "appended"


def test_a_rerun_replaces_the_section_where_it_was_moved(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A section moved higher is replaced where it sits; the text around it is untouched."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    old_section = BUILT_IN_SECTION.replace(f"version={VERSION}", "version=0.0.9").replace("Always load", "Load")
    before, after = "# AGENTS.md\n\nIntro.\n\n", "\n\n## Later rules\n\nStay here.\n"
    (project / "AGENTS.md").write_bytes(f"{before}{old_section}{after}".encode())

    result = install_for_project(cli, project, "pi")

    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == f"{before}{BUILT_IN_SECTION}{after}"
    assert result["instructions"][0]["outcome"] == "replaced"


def test_harnesses_that_read_one_file_write_it_once(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """codex, hermes, agents and pi all read the project's AGENTS.md: one section, one row naming them all."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    result = install_for_project(cli, project, "codex", "hermes", "agents", "pi")

    content = (project / "AGENTS.md").read_bytes().decode("utf-8")
    assert content.count("<!-- connx-mcp-ce begin") == 1
    assert [(r["path"], r["harnesses"]) for r in result["instructions"]] == [
        (str(project / "AGENTS.md"), ["codex", "hermes", "agents", "pi"])
    ]


def test_the_install_record_keeps_each_instructions_file_with_the_sections_hash(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """skill-install.json lists the file and the SHA-256 of the section as written, markers included."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_project(cli, project, "codex")

    record = json.loads((machine.base / "default" / skill.INSTALL_RECORD).read_text(encoding="utf-8"))
    [entry] = record["instructions"]
    assert entry["path"] == str(project / "AGENTS.md")
    assert entry["sha256"] == hashlib.sha256(BUILT_IN_SECTION.encode("utf-8")).hexdigest()


def test_an_install_record_without_instructions_still_reads(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """A record written before the skill instructions counts the skill and is rewritten with the list."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    copy = project / ".agents" / "skills" / "connx-sql-expert"
    record_file.write_text(json.dumps({"places": [str(copy)], "zip": None}), encoding="utf-8")

    assert skill.installed(environment.load("default")) is True
    assert skill.instructions_hints(environment.load("default")) == []

    result = install_for_project(cli, project, "codex")

    assert result["instructions"][0]["outcome"] == "replaced"
    assert json.loads(record_file.read_text(encoding="utf-8"))["instructions"][0]["path"] == str(project / "AGENTS.md")


def test_human_output_lists_each_instructions_file_and_its_outcome(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The human output names the file, its harnesses, and created then replaced."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli)

    first = cli("install-skill", "--harness", "codex", "--harness", "pi", "--project", str(project))
    second = cli("install-skill", "--harness", "codex", "--project", str(project))

    assert first.exit_code == ExitCode.OK, first.output
    assert f"Wrote the skill instructions (built-in text, version {VERSION}):" in first.stdout
    assert f"{project / 'AGENTS.md'}  (codex, pi; created)" in first.stdout
    assert f"{project / 'AGENTS.md'}  (codex; replaced)" in second.stdout


def test_a_zip_writes_no_instructions_file(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """--zip installs nothing on this machine, so no instructions file is written."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "codex", "--zip", str(tmp_path / "out"))

    assert code == ExitCode.OK, body
    assert body["result"]["instructions"] == []
    assert not list(tmp_path.rglob("AGENTS.md"))


# --- Skill instructions: ownership, --force and file handling -------------------------------------------

# A section an earlier version of connx-mcp-ce wrote.
EARLIER_BEGIN, END = "<!-- connx-mcp-ce begin version=0.0.9 -->", "<!-- connx-mcp-ce end -->"
EARLIER_SECTION = f"{EARLIER_BEGIN}\nold text\n{END}"


def project_with_agents_md(tmp_path: Path, content: bytes) -> Path:
    """Return a project directory whose AGENTS.md holds the given bytes."""
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(content)
    return project


def hand_edit(path: Path) -> bytes:
    """Edit the text inside a section connx-mcp-ce wrote, as an administrator would; return the new bytes."""
    edited = path.read_bytes().replace(b"Always load", b"Please load")
    path.write_bytes(edited)
    return edited


def test_a_hand_edited_section_is_refused_unless_forced(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Text edited inside a recorded section is kept until --force replaces it."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    agents = project / "AGENTS.md"
    edited = hand_edit(agents)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert body["error"]["code"] == "skill_exists"
    assert str(agents) in body["error"]["message"]
    assert "edited" in body["error"]["message"]
    assert body["error"]["next"] == (
        f"connx-mcp-ce install-skill default --harness codex --project {as_typed(project)} --force"
    )
    assert agents.read_bytes() == edited

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--force")

    assert code == ExitCode.OK, body
    assert agents.read_bytes().decode("utf-8") == CREATED_AGENTS_MD
    assert body["result"]["instructions"][0]["outcome"] == "replaced"


# --- Look-alike names without `-ce`: read as anyone else's ---------------------------------------------

# A section, marker file and zip comment under a look-alike name without `-ce`: the tool must treat them as foreign.
LOOKALIKE_SECTION = "<!-- connx-mcp begin version=0.0.9 -->\nold text\n<!-- connx-mcp end -->"
LOOKALIKE_MARKER_FILE = ".connx-mcp-install.json"
LOOKALIKE_ZIP_COMMENT = b"Written by connx-mcp"


def test_a_section_with_lookalike_markers_is_kept_and_a_new_one_appended(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A `<!-- connx-mcp begin -->` section is the administrator's text: it stays, the new section follows."""
    configure(cli, cdd)
    own = f"# AGENTS.md\n\n{LOOKALIKE_SECTION}\n"
    project = project_with_agents_md(tmp_path, own.encode())

    result = install_for_project(cli, project, "codex")

    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == f"{own}\n{BUILT_IN_SECTION}\n"
    assert result["instructions"][0]["outcome"] == "appended"


def test_a_copy_with_only_a_lookalike_marker_file_is_refused_as_foreign(cli: Cli, cdd: Path, machine: Machine) -> None:
    """A copy marked `.connx-mcp-install.json` is not connx-mcp-ce's: it blocks the install until --force."""
    configure(cli, cdd)
    skill_in_image(cli)
    old = machine.home / ".claude" / "skills" / "connx-sql-expert"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("old skill\n", encoding="utf-8")
    (old / LOOKALIKE_MARKER_FILE).write_text(json.dumps({"installed_by": "connx-mcp"}), encoding="utf-8")

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(old) in body["error"]["message"]
    assert (old / "SKILL.md").read_text(encoding="utf-8") == "old skill\n"


def test_a_copy_whose_marker_file_names_a_lookalike_tool_is_refused_as_foreign(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """A `.connx-mcp-ce-install.json` whose `installed_by` is `connx-mcp` is not connx-mcp-ce's: --force decides."""
    configure(cli, cdd)
    skill_in_image(cli)
    old = machine.home / ".claude" / "skills" / "connx-sql-expert"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("old skill\n", encoding="utf-8")
    (old / ".connx-mcp-ce-install.json").write_text(json.dumps({"installed_by": "connx-mcp"}), encoding="utf-8")

    code, body = cli.json("install-skill", "--harness", "claude-code")

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(old) in body["error"]["message"]
    assert (old / "SKILL.md").read_text(encoding="utf-8") == "old skill\n"


def test_a_zip_with_a_lookalike_archive_comment_is_refused_as_foreign(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connx-sql-expert.zip commented `Written by connx-mcp` is not connx-mcp-ce's: it is kept unless --force."""
    configure(cli, cdd)
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "connx-sql-expert.zip"
    with zipfile.ZipFile(old, "w") as archive:
        archive.comment = LOOKALIKE_ZIP_COMMENT
        archive.writestr("connx-sql-expert/SKILL.md", "old skill")
    before = old.read_bytes()

    code, body = cli.json("install-skill", "--harness", "claude-desktop")

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(old) in body["error"]["message"]
    assert old.read_bytes() == before


@pytest.mark.parametrize(
    ("broken", "kept"),
    [
        pytest.param(
            "# AGENTS.md\n\nMine.\n\n<!-- connx-mcp-ce begin version=0.0.9 -->\nhalf a section\n",
            "# AGENTS.md\n\nMine.\n\nhalf a section",
            id="begin-without-end",
        ),
        pytest.param(
            "# AGENTS.md\n\nhalf a section\n<!-- connx-mcp-ce end -->\n\nMine.\n",
            "# AGENTS.md\n\nhalf a section\n\nMine.",
            id="end-without-begin",
        ),
        pytest.param(
            "Mine.\n<!-- connx-mcp-ce end -->\nbetween\n<!-- connx-mcp-ce begin version=0.0.9 -->\nLater.\n",
            "Mine.\nbetween\nLater.",
            id="out-of-order",
        ),
        pytest.param(
            f"Intro.\n\n{EARLIER_SECTION}\n\nMiddle.\n\n{EARLIER_SECTION}\n\nEnd.\n",
            "Intro.\n\nMiddle.\n\nEnd.",
            id="two-sections",
        ),
        pytest.param(
            f"Mine.\n{EARLIER_BEGIN}\nouter\n{EARLIER_BEGIN}\ninner\n{END}\nafter\n{END}\nEnd.\n",
            "Mine.\nouter\nafter\nEnd.",
            id="nested",
        ),
        pytest.param(
            f"Mine.\n{EARLIER_BEGIN}\nfirst\n{EARLIER_BEGIN}\nsecond\n{END}\nEnd.\n",
            "Mine.\nfirst\nEnd.",
            id="two-begins-one-end",
        ),
        pytest.param(
            f"Mine.\n{EARLIER_BEGIN}\ninside\n{END}\nafter\n{END}\nEnd.\n",
            "Mine.\nafter\nEnd.",
            id="two-ends-one-begin",
        ),
    ],
)
def test_broken_markers_are_refused_and_force_leaves_one_clean_section(
    cli: Cli, cdd: Path, tmp_path: Path, broken: str, kept: str
) -> None:
    """Broken markers leave the file and the skill copies unwritten; --force strips them and appends one section."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = project_with_agents_md(tmp_path, broken.encode())
    agents = project / "AGENTS.md"

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(agents) in body["error"]["message"]
    assert body["error"]["next"] == (
        f"connx-mcp-ce install-skill default --harness codex --project {as_typed(project)} --force"
    )
    assert agents.read_bytes() == broken.encode()
    assert not (project / ".agents").exists()

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--force")

    assert code == ExitCode.OK, body
    assert agents.read_bytes().decode("utf-8") == f"{kept}\n\n{BUILT_IN_SECTION}\n"


def test_a_section_in_a_file_the_record_does_not_name_is_replaced_without_force(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """With a record for another project only, a section that differs from the text is treated as ours."""
    configure(cli, cdd)
    other = tmp_path / "other"
    other.mkdir()
    install_for_project(cli, other, "codex")
    project = project_with_agents_md(tmp_path, f"Mine.\n\n{EARLIER_SECTION}\n".encode())

    result = install_for_project(cli, project, "codex")

    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == f"Mine.\n\n{BUILT_IN_SECTION}\n"
    assert result["instructions"][0]["outcome"] == "replaced"


def test_the_record_keeps_an_earlier_projects_file_so_its_edits_stay_protected(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Installing for project B keeps project A's entry: a later edit in A is still refused."""
    configure(cli, cdd)
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    install_for_project(cli, first, "codex")
    install_for_project(cli, second, "codex")
    record = json.loads((machine.base / "default" / skill.INSTALL_RECORD).read_text(encoding="utf-8"))
    hand_edit(first / "AGENTS.md")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(first))

    assert sorted(e["path"] for e in record["instructions"]) == [str(first / "AGENTS.md"), str(second / "AGENTS.md")]
    assert code == ExitCode.SKILL_EXISTS, body
    assert str(first / "AGENTS.md") in body["error"]["message"]


def two_environments_on_two_versions(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Environments `default` (15.0.3) and `other` (15.0.4) whose image ships the skill instructions."""
    configure(cli, cdd)
    configure(cli, cdd, name="other")
    host_file = machine.base / "other" / "host.json"
    host = json.loads(host_file.read_text(encoding="utf-8"))
    host_file.write_text(json.dumps({**host, "image_version": "15.0.4.26100"}), encoding="utf-8")
    skill_in_image(cli, instructions=IMAGE_TEXT)


def test_a_section_another_environment_wrote_is_replaced_without_force(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Two environments share ~/.claude/CLAUDE.md: each replaces the other's section, as its record shows."""
    two_environments_on_two_versions(cli, cdd, machine)
    claude = machine.home / ".claude" / "CLAUDE.md"

    for name in ("default", "other", "default"):
        code, body = cli.json("install-skill", name, "--harness", "claude-code", "--user")
        assert code == ExitCode.OK, body

    assert claude.read_text(encoding="utf-8").count("connx-mcp-ce begin version=15.0.3.26012") == 1
    assert "15.0.4" not in claude.read_text(encoding="utf-8")


def test_a_section_no_environment_wrote_is_still_refused(cli: Cli, cdd: Path, machine: Machine) -> None:
    """A hand edit after another environment's run matches no record, so it is refused without --force."""
    two_environments_on_two_versions(cli, cdd, machine)
    claude = machine.home / ".claude" / "CLAUDE.md"
    cli.json("install-skill", "default", "--harness", "claude-code", "--user")
    cli.json("install-skill", "other", "--harness", "claude-code", "--user")
    claude.write_bytes(claude.read_bytes().replace(b"Use the", b"Please use the"))

    code, body = cli.json("install-skill", "default", "--harness", "claude-code", "--user")

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(claude) in body["error"]["message"]


def test_an_unreadable_record_of_another_environment_is_skipped(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Another environment's broken record vouches for nothing and fails nothing: the section is just refused."""
    two_environments_on_two_versions(cli, cdd, machine)
    cli.json("install-skill", "default", "--harness", "claude-code", "--user")
    cli.json("install-skill", "other", "--harness", "claude-code", "--user")
    (machine.base / "other" / skill.INSTALL_RECORD).write_text("{not json", encoding="utf-8")

    code, body = cli.json("install-skill", "default", "--harness", "claude-code", "--user")

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(machine.home / ".claude" / "CLAUDE.md") in body["error"]["message"]


def test_a_refused_instructions_file_leaves_the_skill_unwritten(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """An edited section stops the skill copies too."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    hand_edit(project / "AGENTS.md")

    code, body = cli.json("install-skill", "--harness", "codex", "--harness", "claude-code", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert not (project / ".claude").exists()


def test_a_refused_skill_copy_leaves_the_instructions_unwritten(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A skill connx-mcp-ce did not install stops the instructions file being created."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = tmp_path / "sales"
    own = project / ".agents" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_bytes(b"my own edits\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert not (project / "AGENTS.md").exists()


def test_every_refusal_is_reported_together(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A foreign skill copy and an edited section are named in one refusal with one --force command."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    agents = project / "AGENTS.md"
    edited = hand_edit(agents)
    own = project / ".claude" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_bytes(b"my own edits\n")

    code, body = cli.json("install-skill", "--harness", "claude-code", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(own) in body["error"]["message"]
    assert str(agents) in body["error"]["message"]
    assert body["error"]["next"] == (
        "connx-mcp-ce install-skill default --harness claude-code --harness codex "
        f"--project {as_typed(project)} --force"
    )
    assert agents.read_bytes() == edited
    assert (own / "SKILL.md").read_bytes() == b"my own edits\n"


def test_a_refusal_to_fix_by_hand_and_one_to_force_are_reported_together(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A file that is not UTF-8 must be fixed first; the foreign copy beside it is left to --force."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = project_with_agents_md(tmp_path, b"caf\xe9\n")
    own = project / ".claude" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_bytes(b"my own edits\n")
    flags = ["--harness", "claude-code", "--harness", "codex", "--project", str(project)]
    forced = (
        "connx-mcp-ce install-skill default --harness claude-code --harness codex "
        f"--project {as_typed(project)} --force"
    )

    code, body = cli.json("install-skill", *flags)

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(own) in body["error"]["message"]
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert "Fix the unreadable or read-only files, then replace the rest with --force." in body["error"]["message"]
    assert body["error"]["next"] == forced

    code, body = cli.json("install-skill", *flags, "--force")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(own) not in body["error"]["message"]
    assert "Fix what is named, then run the command again." in body["error"]["message"]
    assert body["error"]["next"] == forced
    assert (own / "SKILL.md").read_bytes() == b"my own edits\n"


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["no-bom", "bom"])
def test_crlf_files_stay_crlf_and_a_bom_is_kept(cli: Cli, cdd: Path, tmp_path: Path, bom: bytes) -> None:
    """Appending and then replacing keep the file's CRLF line endings and its leading BOM."""
    configure(cli, cdd)
    project = project_with_agents_md(tmp_path, bom + b"# Ours\r\n\r\nKeep this.\r\n")
    crlf_section = BUILT_IN_SECTION.replace("\n", "\r\n").encode()

    first = install_for_project(cli, project, "codex")
    after_append = (project / "AGENTS.md").read_bytes()
    second = install_for_project(cli, project, "codex")

    assert after_append == bom + b"# Ours\r\n\r\nKeep this.\r\n\r\n" + crlf_section + b"\r\n"
    assert (project / "AGENTS.md").read_bytes() == after_append
    assert (first["instructions"][0]["outcome"], second["instructions"][0]["outcome"]) == ("appended", "replaced")


def test_a_section_right_after_the_bom_is_replaced_and_the_bom_kept(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A section on the first line, after the BOM, is found and replaced; the BOM stays."""
    configure(cli, cdd)
    project = project_with_agents_md(tmp_path, b"\xef\xbb\xbf" + EARLIER_SECTION.encode() + b"\nMine.\n")

    install_for_project(cli, project, "codex")

    assert (project / "AGENTS.md").read_bytes() == b"\xef\xbb\xbf" + BUILT_IN_SECTION.encode() + b"\nMine.\n"


def test_a_new_file_uses_the_os_line_ending(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A created AGENTS.md ends its lines the way this OS does."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_project(cli, project, "codex")

    content = (project / "AGENTS.md").read_bytes()
    assert content == CREATED_AGENTS_MD.encode()
    assert content.count(b"\r\n") == (content.count(b"\n") if os.name == "nt" else 0)


def test_a_file_that_is_not_utf8_is_refused_by_name_and_left_as_it_is(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Latin-1 bytes are never rewritten, even with --force; the skill is not installed either."""
    configure(cli, cdd)
    skill_in_image(cli)
    latin = b"# Notizen f\xfcr Codex\n"
    project = project_with_agents_md(tmp_path, latin)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--force")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert (project / "AGENTS.md").read_bytes() == latin
    assert not (project / ".agents").exists()


needs_permissions = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits stop neither Windows directories nor root",
)


@needs_permissions
def test_a_rewritten_instructions_file_keeps_its_permissions_and_leaves_no_temporary_file(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """The file is replaced whole, yet keeps its mode, and nothing else is left in the project."""
    configure(cli, cdd)
    project = project_with_agents_md(tmp_path, b"Mine.\n")
    (project / "AGENTS.md").chmod(0o640)

    install_for_project(cli, project, "codex")

    assert (project / "AGENTS.md").stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in project.iterdir()) == [".agents", "AGENTS.md"]


@needs_permissions
def test_a_read_only_instructions_file_is_refused_before_anything_is_written(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """A read-only AGENTS.md is named before the skill is copied out of the image."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = project_with_agents_md(tmp_path, b"Mine.\n")
    (project / "AGENTS.md").chmod(0o444)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--force")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert (project / "AGENTS.md").read_bytes() == b"Mine.\n"
    assert not (project / ".agents").exists()
    assert not any(c[0] == "create" for c in cli.docker.calls)


@needs_permissions
def test_a_read_only_project_directory_is_refused_before_anything_is_written(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """AGENTS.md cannot be created in a read-only directory: the file is named and no skill is copied."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = tmp_path / "sales"
    (project / ".agents").mkdir(parents=True)
    project.chmod(0o555)
    try:
        code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))
    finally:
        project.chmod(0o755)

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".agents" / "skills").exists()


def test_no_instructions_installs_only_the_skill(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """--no-instructions copies the skill and creates no instructions file."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = sales_project(tmp_path)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--no-instructions")

    assert code == ExitCode.OK, body
    assert installed(project / ".agents" / "skills" / "connx-sql-expert")["SKILL.md"] == SKILL_MD
    assert body["result"]["instructions"] == []
    assert body["result"]["warnings"] == []
    assert not (project / "AGENTS.md").exists()


def test_no_instructions_leaves_a_recorded_section_and_warns_it_remains(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """An earlier run's section stays byte for byte, is named in a warning, and stays in the record."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    agents = project / "AGENTS.md"
    before = agents.read_bytes()
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    recorded = json.loads(record_file.read_text(encoding="utf-8"))["instructions"]

    code, body = cli.json("install-skill", "--harness", "pi", "--project", str(project), "--no-instructions")
    human = cli("install-skill", "--harness", "pi", "--project", str(project), "--no-instructions")

    assert code == ExitCode.OK, body
    assert agents.read_bytes() == before
    assert body["result"]["instructions"] == []
    [warning] = body["result"]["warnings"]
    assert str(agents) in warning
    assert json.loads(record_file.read_text(encoding="utf-8"))["instructions"] == recorded
    assert human.exit_code == ExitCode.OK, human.output
    assert f"Warning: {warning}" in human.stdout


def test_no_instructions_does_not_warn_about_a_section_that_has_gone(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A recorded file that no longer holds the section draws no warning."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    (project / "AGENTS.md").write_bytes(b"# AGENTS.md\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--no-instructions")

    assert code == ExitCode.OK, body
    assert body["result"]["warnings"] == []
    assert (project / "AGENTS.md").read_bytes() == b"# AGENTS.md\n"


def test_no_instructions_warns_naming_the_read_failure_of_an_unreadable_file(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """A recorded file that is no longer UTF-8 is not silently taken as having lost its section."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")
    latin = (project / "AGENTS.md").read_bytes() + b"f\xfcr\n"
    (project / "AGENTS.md").write_bytes(latin)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--no-instructions")

    assert code == ExitCode.OK, body
    [warning] = body["result"]["warnings"]
    assert str(project / "AGENTS.md") in warning
    assert "cannot be read as UTF-8 text" in warning
    assert (project / "AGENTS.md").read_bytes() == latin


def test_the_force_command_after_a_refusal_keeps_no_instructions(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The refusal's next command keeps --no-instructions, so following it replaces the copy and writes no file."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = tmp_path / "sales"
    own = project / ".agents" / "skills" / "connx-sql-expert"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("my own edits\n", encoding="utf-8")
    flags = ["--harness", "codex", "--project", str(project), "--no-instructions"]

    code, body = cli.json("install-skill", *flags)

    assert code == ExitCode.SKILL_EXISTS, body
    assert body["error"]["next"] == (
        f"connx-mcp-ce install-skill default --harness codex --project {as_typed(project)} --no-instructions --force"
    )

    code, body = cli.json("install-skill", *flags, "--force")

    assert code == ExitCode.OK, body
    assert installed(own)["SKILL.md"] == SKILL_MD
    assert not (project / "AGENTS.md").exists()


@pytest.mark.parametrize(
    "claude_file", ["sales/CLAUDE.md", "sales/.claude/CLAUDE.md", "sales/CLAUDE.local.md", "CLAUDE.md"]
)
def test_claude_code_writes_claude_md_when_a_claude_file_is_on_the_path(
    cli: Cli, cdd: Path, tmp_path: Path, claude_file: str
) -> None:
    """A Claude file in the project or a parent makes Claude Code ignore AGENTS.md, so the section goes to CLAUDE.md."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# AGENTS.md\n")
    existing = tmp_path / claude_file
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"# Rules\n")

    result = install_for_project(cli, project, "claude-code")

    assert [(r["path"], r["harnesses"]) for r in result["instructions"]] == [
        (str(project / "CLAUDE.md"), ["claude-code"])
    ]
    assert "connx-mcp-ce begin" in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert (project / "AGENTS.md").read_bytes() == b"# AGENTS.md\n"


@pytest.mark.parametrize("line", ["@AGENTS.md", "See @./AGENTS.md for the shared rules."])
def test_a_claude_md_importing_agents_md_sends_claude_code_to_agents_md(
    cli: Cli, cdd: Path, tmp_path: Path, line: str
) -> None:
    """Claude Code loads the imported AGENTS.md, so only it gets the section and CLAUDE.md stays as it was."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    claude = f"# Claude\n\n{line}\n".encode()
    (project / "CLAUDE.md").write_bytes(claude)

    result = install_for_project(cli, project, "claude-code")

    assert [r["path"] for r in result["instructions"]] == [str(project / "AGENTS.md")]
    assert "connx-mcp-ce begin" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert (project / "CLAUDE.md").read_bytes() == claude


def test_a_claude_md_linked_to_agents_md_gets_one_section_through_the_link(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """CLAUDE.md and AGENTS.md are one file: Claude Code and Codex write it once, through the link."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# AGENTS.md\n")
    (project / "CLAUDE.md").symlink_to("AGENTS.md")

    result = install_for_project(cli, project, "claude-code", "codex")

    assert [(r["path"], r["harnesses"]) for r in result["instructions"]] == [
        (str(project / "CLAUDE.md"), ["claude-code", "codex"])
    ]
    assert (project / "CLAUDE.md").is_symlink()
    assert (project / "AGENTS.md").read_text(encoding="utf-8").count("connx-mcp-ce begin") == 1


def test_a_project_file_linked_outside_the_project_is_refused_unless_forced(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """AGENTS.md linked to a file outside DIR is named with its target; nothing is written until --force."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = sales_project(tmp_path)
    outside = tmp_path / "dotfiles" / "bashrc"
    outside.parent.mkdir()
    outside.write_bytes(b"export PATH\n")
    (project / "AGENTS.md").symlink_to(outside)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.SKILL_EXISTS, body
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert str(outside) in body["error"]["message"]
    assert body["error"]["next"] == (
        f"connx-mcp-ce install-skill default --harness codex --project {as_typed(project)} --force"
    )
    assert outside.read_bytes() == b"export PATH\n"
    assert not (project / ".agents").exists()

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--force")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").is_symlink()
    assert outside.read_bytes().decode("utf-8") == f"export PATH\n\n{BUILT_IN_SECTION}\n"


def test_a_user_file_linked_elsewhere_is_written_through_the_link(cli: Cli, cdd: Path, machine: Machine) -> None:
    """~/.claude/CLAUDE.md linked into a dotfiles directory gets the section in its target and stays a link."""
    configure(cli, cdd)
    target = machine.home / "dotfiles" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"# Mine\n")
    (machine.home / ".claude").mkdir()
    (machine.home / ".claude" / "CLAUDE.md").symlink_to(target)

    install_for_user(cli, "claude-code")

    assert (machine.home / ".claude" / "CLAUDE.md").is_symlink()
    assert target.read_bytes().decode("utf-8") == f"# Mine\n\n{BUILT_IN_SECTION}\n"


@pytest.mark.parametrize(("override", "written"), [(True, "AGENTS.override.md"), (False, "AGENTS.md")])
def test_codex_writes_agents_override_md_when_it_exists_and_agents_md_otherwise(
    cli: Cli, cdd: Path, tmp_path: Path, override: bool, written: str
) -> None:
    """Codex reads AGENTS.override.md instead of AGENTS.md, so the section goes where Codex looks."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# AGENTS.md\n")
    if override:
        (project / "AGENTS.override.md").write_bytes(b"# Override\n")

    result = install_for_project(cli, project, "codex")

    assert [r["path"] for r in result["instructions"]] == [str(project / written)]
    assert "connx-mcp-ce begin" in (project / written).read_text(encoding="utf-8")


def test_claude_code_and_codex_on_a_project_with_both_files_write_both(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Each harness reads its own file, so each gets the section and the output names the harnesses per file."""
    configure(cli, cdd)
    skill_in_image(cli)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# AGENTS.md\n")
    (project / "CLAUDE.md").write_bytes(b"# Claude\n")

    result = cli("install-skill", "--harness", "claude-code", "--harness", "codex", "--project", str(project))

    assert result.exit_code == ExitCode.OK, result.output
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert (project / name).read_text(encoding="utf-8").count("connx-mcp-ce begin") == 1
    assert f"{project / 'CLAUDE.md'}  (claude-code; appended)" in result.stdout
    assert f"{project / 'AGENTS.md'}  (codex; appended)" in result.stdout


IMAGE_TEXT = "## CONNX\r\n\r\nUse the connx-sql-expert skill.\r\n"


def test_an_image_that_ships_instructions_gives_its_text_under_the_image_version(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """The image's INSTRUCTIONS.md, line endings made LF, is the section under the image's version."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=IMAGE_TEXT)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    section = (
        "<!-- connx-mcp-ce begin version=15.0.3.26012 -->\n## CONNX\n\nUse the connx-sql-expert skill.\n"
        "<!-- connx-mcp-ce end -->"
    )
    expected = f"# AGENTS.md\n\n{section}\n".replace("\n", os.linesep)
    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == expected
    [entry] = body["result"]["instructions"]
    assert (entry["source"], entry["version"]) == ("image", "15.0.3.26012")
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]


@pytest.mark.parametrize("label", ["15.0\n<!-- connx-mcp-ce end -->", "15.0 -->", "", "15.0/beta"])
def test_an_image_version_that_would_break_the_begin_marker_is_written_as_unknown(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, label: str
) -> None:
    """Only letters, digits and . _ + - go into the begin marker; any other version label shows as unknown."""
    configure(cli, cdd)
    host_file = machine.base / "default" / "host.json"
    host = json.loads(host_file.read_text(encoding="utf-8"))
    host_file.write_text(json.dumps({**host, "image_version": label}), encoding="utf-8")
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=IMAGE_TEXT)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    content = (project / "AGENTS.md").read_bytes().decode("utf-8")
    assert content.splitlines()[2] == "<!-- connx-mcp-ce begin version=unknown -->"
    assert content.count("<!-- connx-mcp-ce end -->") == 1
    assert body["result"]["instructions"][0]["version"] == "unknown"


def test_a_built_in_section_is_replaced_by_the_images_text_without_force(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Moving to an image that ships the text: the next run replaces the recorded built-in section."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    first = install_for_project(cli, project, "codex")
    assert first["instructions"][0]["source"] == "built-in"
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]
    skill_in_image(cli, instructions="Use the skill.\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    section = "<!-- connx-mcp-ce begin version=15.0.3.26012 -->\nUse the skill.\n<!-- connx-mcp-ce end -->"
    expected = f"# AGENTS.md\n\n{section}\n".replace("\n", os.linesep)
    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == expected
    [entry] = body["result"]["instructions"]
    assert (entry["outcome"], entry["source"]) == ("replaced", "image")
    record = json.loads((Path(os.environ["MCP_ENVIRONMENT_PATH"]) / "default" / "skill-install.json").read_text())
    assert record["instructions"][0]["sha256"] == hashlib.sha256(section.encode()).hexdigest()


def test_image_instructions_that_are_not_utf8_are_refused_and_nothing_is_written(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """An INSTRUCTIONS.md that is not UTF-8 fails as unreadable; no file is written and the container is removed."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=b"caf\xe9")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "INSTRUCTIONS.md" in body["error"]["message"]
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".agents").exists()
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]


def test_no_instructions_ignores_the_images_instructions(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """With --no-instructions the image's INSTRUCTIONS.md is never read, so even an unreadable one installs."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=b"caf\xe9")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project), "--no-instructions")

    assert code == ExitCode.OK, body
    assert body["result"]["instructions"] == []
    assert not (project / "AGENTS.md").exists()


# --- Skill instructions in the install notes ------------------------------------------------------------


def notes_by_harness(notes: str) -> dict[str, str]:
    """Return each harness's block of the install notes, by the harness's title."""
    return {b.split(":\n", 1)[0]: b for b in notes.split("\n\n") if ":\n  " in b}


def test_zip_notes_quote_the_skill_instructions_and_name_each_harness_place(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--zip notes end with the section to paste, and each harness's steps name the file it reads."""
    configure(cli, cdd)
    skill_in_image(cli)
    out = tmp_path / "handover"
    monkeypatch.chdir(tmp_path)
    harnesses = ["claude-code", "codex", "pi", "hermes"]

    code, body = cli.json("install-skill", "--zip", str(out), *[p for h in harnesses for p in ("--harness", h)])

    assert code == ExitCode.OK, body
    notes = Path(body["result"]["zip"]["notes"]).read_text(encoding="utf-8")
    assert notes.endswith(f"\n\n{BUILT_IN_SECTION}\n")
    places = notes_by_harness(notes)
    assert "Add the skill instructions:" in places["Claude Code"]
    assert "~/.claude/CLAUDE.md" in places["Claude Code"]
    assert "CLAUDE.local.md" in places["Claude Code"]
    assert "@AGENTS.md" in places["Claude Code"]
    assert "AGENTS.override.md" in places["Codex"]
    assert "CODEX_HOME" in places["Codex"]
    assert "~/.pi/agent/AGENTS.md" in places["Pi"]
    assert "<project>/AGENTS.md" in places["Hermes Agent"]
    assert body["result"]["instructions"] == []
    assert sorted(p.name for p in out.iterdir()) == ["connx-sql-expert-install.txt", "connx-sql-expert.zip"]
    assert not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("harness", list(skill.HARNESSES))
def test_every_harness_says_where_its_skill_instructions_are_pasted(harness: str) -> None:
    """The install notes have a place for the skill instructions for every harness in the map."""
    entry = skill.HARNESSES[harness]

    assert entry.instructions_places
    assert all(step.strip() for step in skill.instructions_steps(entry))


def test_claude_desktop_notes_say_to_paste_the_text_into_the_project_instructions(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Desktop's zip in the working directory comes with notes that quote the text for its project."""
    configure(cli, cdd)
    skill_in_image(cli)
    monkeypatch.chdir(tmp_path)

    code, body = cli.json("install-skill", "--harness", "claude-desktop")

    assert code == ExitCode.OK, body
    notes = (tmp_path / "connx-sql-expert-install.txt").read_text(encoding="utf-8")
    assert "project instructions" in notes_by_harness(notes)["Claude Desktop"]
    assert notes.endswith(f"\n\n{BUILT_IN_SECTION}\n")
    assert body["result"]["instructions"] == []


def test_zip_notes_quote_the_images_text_when_the_image_ships_it(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """The notes quote the image's skill instructions under the image's version, not the built-in text."""
    configure(cli, cdd)
    skill_in_image(cli, instructions=IMAGE_TEXT)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "codex")

    assert code == ExitCode.OK, body
    notes = (tmp_path / "connx-sql-expert-install.txt").read_text(encoding="utf-8")
    image_section = (
        "<!-- connx-mcp-ce begin version=15.0.3.26012 -->\n## CONNX\n\nUse the connx-sql-expert skill.\n"
        "<!-- connx-mcp-ce end -->"
    )
    assert notes.endswith(f"\n\n{image_section}\n")
    assert "Always load and use" not in notes


def test_zip_notes_without_instructions_carry_no_instructions_step(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """--zip --no-instructions notes keep the skill and server steps and leave the skill instructions out."""
    configure(cli, cdd)
    skill_in_image(cli)

    code, body = cli.json(
        "install-skill", "--zip", str(tmp_path), "--harness", "claude-desktop", "--harness", "codex",
        "--no-instructions",
    )  # fmt: skip

    assert code == ExitCode.OK, body
    notes = (tmp_path / "connx-sql-expert-install.txt").read_bytes().decode("utf-8")
    assert "Add the skill:" in notes
    assert "Register the server:" in notes
    assert "skill instructions" not in notes
    assert "connx-mcp-ce begin" not in notes


def test_zip_mode_gives_no_warning_about_a_section_an_earlier_run_wrote(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Zip mode writes no instructions file, so it does not warn about one, with or without --no-instructions."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_project(cli, project, "codex")

    for extra in ([], ["--no-instructions"]):
        code, body = cli.json("install-skill", "--zip", str(tmp_path / "out"), "--harness", "codex", *extra)

        assert code == ExitCode.OK, body
        assert body["result"]["warnings"] == []


def test_no_instructions_warns_only_about_the_files_this_run_targets(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A section recorded for another project is not this run's: --no-instructions does not warn about it."""
    configure(cli, cdd)
    first, second = tmp_path / "sales", tmp_path / "billing"
    first.mkdir()
    second.mkdir()
    install_for_project(cli, first, "codex")
    install_for_project(cli, second, "codex")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(second), "--no-instructions")

    assert code == ExitCode.OK, body
    [warning] = body["result"]["warnings"]
    assert str(second / "AGENTS.md") in warning
    assert str(first) not in warning


def test_an_image_without_instructions_gives_the_built_in_text_under_the_cli_version(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """An image whose INSTRUCTIONS.md is missing installs the built-in text under connx-mcp-ce's version, no error."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    [entry] = body["result"]["instructions"]
    assert (entry["source"], entry["version"]) == ("built-in", VERSION)
    text = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert f"<!-- connx-mcp-ce begin version={VERSION} -->\n## CONNX MCP Server\n" in text


def test_an_image_instructions_copy_that_fails_otherwise_fails_the_install_with_dockers_words(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """Only a missing INSTRUCTIONS.md falls back to the built-in text; any other copy failure fails, container removed."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli)
    cli.docker.act_when(
        copies_instructions, lambda a: DockerResult(1, "", "Error: write /tmp/x: no space left on device\n")
    )

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.DOCKER_FAILED, body
    assert "no space left on device" in body["error"]["message"]
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".agents").exists()
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]


def test_a_no_such_container_path_answer_counts_as_missing_instructions(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Older Docker says `No such container:path` for a missing file: that too gives the built-in text."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli)
    cli.docker.act_when(
        copies_instructions,
        lambda a: DockerResult(1, "", f"Error: No such container:path: {CONTAINER}:{IN_IMAGE_INSTRUCTIONS}\n"),
    )

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    assert body["result"]["instructions"][0]["source"] == "built-in"


def test_image_instructions_are_copied_following_a_symlink(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """An INSTRUCTIONS.md that is a symlink in the image is copied as the file it points to (`docker cp -L`)."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions="Use the skill.\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    [copy] = [call for call in cli.docker.calls if copies_instructions(call)]
    assert copy[:2] == ["cp", "-L"]


@pytest.mark.parametrize("marker", ["<!-- connx-mcp-ce begin version=1 -->", "<!-- connx-mcp-ce end -->"])
def test_image_instructions_holding_a_marker_line_are_refused(cli: Cli, cdd: Path, tmp_path: Path, marker: str) -> None:
    """The image's INSTRUCTIONS.md must hold the body only: a marker line in it fails and nothing is written."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=f"Use the skill.\n{marker}\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.FILE_UNREADABLE, body
    message = body["error"]["message"]
    assert DIGEST in message
    assert "body only" in message
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".agents").exists()


def test_image_instructions_holding_lookalike_marker_lines_are_embedded_as_text(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    """`<!-- connx-mcp begin/end -->` lines are not connx-mcp-ce's markers: the image's text keeps them as its body."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli, instructions=f"Use the skill.\n{LOOKALIKE_SECTION}\n")

    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))

    assert code == ExitCode.OK, body
    section = (
        "<!-- connx-mcp-ce begin version=15.0.3.26012 -->\nUse the skill.\n<!-- connx-mcp begin version=0.0.9 -->\n"
        "old text\n<!-- connx-mcp end -->\n<!-- connx-mcp-ce end -->"
    )
    expected = f"# AGENTS.md\n\n{section}\n".replace("\n", os.linesep)
    assert (project / "AGENTS.md").read_bytes().decode("utf-8") == expected
    assert body["result"]["instructions"][0]["source"] == "image"


# --- Skill instructions at user scope -------------------------------------------------------------------


def install_for_user(cli: Cli, *harnesses: str) -> dict:
    """Install the skill and its instructions for the user; return the JSON result."""
    skill_in_image(cli)
    flags = [p for h in harnesses for p in ("--harness", h)]
    code, body = cli.json("install-skill", *flags, "--user")
    assert code == ExitCode.OK, body
    return body["result"]


def test_a_user_install_writes_each_harness_user_scope_file(
    cli: Cli, cdd: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--user writes ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md and ~/.pi/agent/AGENTS.md, and records no project."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)

    result = install_for_user(cli, "claude-code", "codex", "pi")

    claude = machine.home / ".claude" / "CLAUDE.md"
    codex = machine.home / ".codex" / "AGENTS.md"
    pi = machine.home / ".pi" / "agent" / "AGENTS.md"
    assert [(r["path"], r["harnesses"], r["outcome"]) for r in result["instructions"]] == [
        (str(claude), ["claude-code"], "created"),
        (str(codex), ["codex"], "created"),
        (str(pi), ["pi"], "created"),
    ]
    assert claude.read_bytes().decode("utf-8") == f"{BUILT_IN_SECTION}\n".replace("\n", os.linesep)
    assert codex.read_bytes().decode("utf-8") == CREATED_AGENTS_MD
    assert pi.read_bytes().decode("utf-8") == CREATED_AGENTS_MD
    record = json.loads((machine.base / "default" / skill.INSTALL_RECORD).read_text(encoding="utf-8"))
    assert [(e["path"], e["project"]) for e in record["instructions"]] == [
        (str(claude), None),
        (str(codex), None),
        (str(pi), None),
    ]


def test_a_user_install_for_codex_writes_into_codex_home(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CODEX_HOME set, Codex's user file is there, not in ~/.codex."""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    configure(cli, cdd)

    result = install_for_user(cli, "codex")

    assert [r["path"] for r in result["instructions"]] == [str(codex_home / "AGENTS.md")]
    assert "connx-mcp-ce begin" in (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert not (machine.home / ".codex").exists()


def test_a_user_install_for_pi_keeps_to_its_only_claude_md(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Pi reads the CLAUDE.md in ~/.pi/agent when it is the only file there, so the section goes into it."""
    configure(cli, cdd)
    agent = machine.home / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "CLAUDE.md").write_bytes(b"# Mine\n")

    result = install_for_user(cli, "pi")

    assert [(r["path"], r["outcome"]) for r in result["instructions"]] == [(str(agent / "CLAUDE.md"), "appended")]
    assert not (agent / "AGENTS.md").exists()


SKIPPED_REASON = (
    "These harnesses have no instructions file for yourself. With --project DIR, install-skill writes the skill "
    "instructions into DIR/AGENTS.md for them."
)


def test_hermes_and_agents_at_user_scope_are_skipped_with_the_project_pointer(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """The JSON result has a skipped row for them that points to --project, and nothing is written for them."""
    configure(cli, cdd)

    result = install_for_user(cli, "hermes", "claude-code", "agents")

    assert result["instructions"] == [
        {
            "path": str(machine.home / ".claude" / "CLAUDE.md"),
            "harnesses": ["claude-code"],
            "outcome": "created",
            "source": "built-in",
            "version": VERSION,
        },
        {
            "path": None,
            "harnesses": ["hermes", "agents"],
            "outcome": "skipped",
            "source": None,
            "version": None,
            "reason": SKIPPED_REASON,
        },
    ]
    assert not list(machine.home.rglob("AGENTS.md"))


def test_human_output_says_hermes_and_agents_were_skipped_and_points_to_project(cli: Cli, cdd: Path) -> None:
    """A person sees which harnesses got no skill instructions, and how to give them some."""
    configure(cli, cdd)
    skill_in_image(cli)

    result = cli("install-skill", "--harness", "hermes", "--harness", "agents", "--user")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"Skipped the skill instructions for hermes, agents. {SKIPPED_REASON}" in result.stdout
    assert "Wrote the skill instructions" not in result.stdout


def test_claude_codes_user_file_does_not_send_projects_under_home_to_claude_md(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """After a user install writes ~/.claude/CLAUDE.md, a project under home still gets AGENTS.md for Claude Code."""
    configure(cli, cdd)
    install_for_user(cli, "claude-code")
    project = machine.home / "work" / "sales"
    project.mkdir(parents=True)

    result = install_for_project(cli, project, "claude-code")

    assert [r["path"] for r in result["instructions"]] == [str(project / "AGENTS.md")]


BAD_IMAGE_INSTRUCTIONS = pytest.mark.parametrize(
    "bad", [b"caf\xe9\n", b"Use the skill.\n<!-- connx-mcp-ce end -->\n"], ids=["not-utf8", "marker-line"]
)


def bad_instructions_in_image(cli: Cli, bad: bytes) -> None:
    """The image holds the skill and an INSTRUCTIONS.md that `from_image` refuses."""
    skill_in_image(cli, instructions=bad)


@BAD_IMAGE_INSTRUCTIONS
def test_bad_image_instructions_refuse_a_claude_desktop_run_before_anything_is_written(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    """Hermes writes no user-scope instructions file, but Claude Desktop's notes quote the text: a bad one fails first."""
    configure(cli, cdd)
    bad_instructions_in_image(cli, bad)
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)

    code, body = cli.json("install-skill", "--user", "--harness", "hermes", "--harness", "claude-desktop")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "INSTRUCTIONS.md" in body["error"]["message"]
    assert not list(machine.home.rglob("SKILL.md"))
    assert list(here.iterdir()) == []
    assert not (machine.base / "default" / skill.INSTALL_RECORD).exists()
    assert cli.docker.calls[-1] == ["rm", "-f", CONTAINER]


@BAD_IMAGE_INSTRUCTIONS
def test_bad_image_instructions_refuse_a_zip_before_anything_is_written(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, bad: bytes
) -> None:
    """--zip notes quote the image's text, so a bad image file fails the run with no zip and no notes."""
    configure(cli, cdd)
    bad_instructions_in_image(cli, bad)
    out = tmp_path / "handover"
    out.mkdir()

    code, body = cli.json("install-skill", "--zip", str(out), "--harness", "codex")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "INSTRUCTIONS.md" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce update default"
    assert list(out.iterdir()) == []
    assert not (machine.base / "default" / skill.INSTALL_RECORD).exists()


@BAD_IMAGE_INSTRUCTIONS
def test_no_instructions_zips_despite_bad_image_instructions(cli: Cli, cdd: Path, tmp_path: Path, bad: bytes) -> None:
    """With --no-instructions the notes quote nothing, so the image's INSTRUCTIONS.md is never read."""
    configure(cli, cdd)
    bad_instructions_in_image(cli, bad)

    code, body = cli.json("install-skill", "--zip", str(tmp_path), "--harness", "codex", "--no-instructions")

    assert code == ExitCode.OK, body
    assert (tmp_path / "connx-sql-expert.zip").is_file()


# --- Install record: every skill copy and zip -----------------------------------------------------------


def recorded(machine: Machine) -> dict[str, Any]:
    """Return the environment's install record as written."""
    return json.loads((env_dir(machine) / skill.INSTALL_RECORD).read_text(encoding="utf-8"))


def copies(machine: Machine) -> set[tuple[str, tuple[str, ...], str | None]]:
    """Return the record's skill copies as (path, harnesses, project)."""
    return {(c["path"], tuple(c["harnesses"]), c["project"]) for c in recorded(machine)["copies"]}


def test_a_user_install_then_a_project_install_keeps_both_skill_copies(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """A project install for Codex after a user install for Claude Code leaves both copies in the record."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_user(cli, "claude-code")
    install_for_project(cli, project, "codex")

    assert copies(machine) == {
        (str(machine.home / ".claude" / "skills" / "connx-sql-expert"), ("claude-code",), None),
        (str(project / ".agents" / "skills" / "connx-sql-expert"), ("codex",), str(project.resolve())),
    }


def test_two_harnesses_installed_into_one_directory_both_claim_its_copy(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Codex then Pi into the same project: one copy, claimed by both."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_project(cli, project, "codex")
    install_for_project(cli, project, "pi")

    assert copies(machine) == {
        (str(project / ".agents" / "skills" / "connx-sql-expert"), ("codex", "pi"), str(project.resolve())),
    }


def test_reinstalling_a_harness_into_the_same_directory_keeps_one_entry(cli: Cli, cdd: Path, machine: Machine) -> None:
    """The same harness and path twice: one entry, one claim."""
    configure(cli, cdd)

    install_for_user(cli, "claude-code")
    install_for_user(cli, "claude-code")

    assert recorded(machine)["copies"] == [
        {"path": str(machine.home / ".claude" / "skills" / "connx-sql-expert"), "harnesses": ["claude-code"],
         "project": None},
    ]  # fmt: skip


def test_two_harnesses_installed_into_one_project_both_claim_its_instructions_file(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Codex then Pi into the same project: AGENTS.md's entry keeps both claims, as the copy's does."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_project(cli, project, "codex")
    install_for_project(cli, project, "pi")

    assert [(e["path"], e["harnesses"]) for e in recorded(machine)["instructions"]] == [
        (str(project / "AGENTS.md"), ["codex", "pi"])
    ]


def zips(machine: Machine) -> set[tuple[str, tuple[str, ...]]]:
    """Return the record's zips as (path, harnesses)."""
    return {(z["path"], tuple(z["harnesses"])) for z in recorded(machine)["zips"]}


def test_zips_into_two_directories_are_both_recorded(cli: Cli, cdd: Path, machine: Machine, tmp_path: Path) -> None:
    """Each --zip run adds its zip with its harnesses; one into the same directory again adds its claims."""
    configure(cli, cdd)
    for out, harness in (("first", "codex"), ("second", "codex"), ("first", "pi")):
        skill_in_image(cli)
        code, body = cli.json("install-skill", "--zip", str(tmp_path / out), "--harness", harness)
        assert code == ExitCode.OK, body

    assert zips(machine) == {
        (str(tmp_path / "first" / "connx-sql-expert.zip"), ("codex", "pi")),
        (str(tmp_path / "second" / "connx-sql-expert.zip"), ("codex",)),
    }


def test_a_zip_for_claude_desktop_beside_an_install_records_only_its_harness(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code installs on this machine; the zip in the working directory is Claude Desktop's alone."""
    configure(cli, cdd)
    monkeypatch.chdir(tmp_path)

    install_for_user(cli, "claude-code", "claude-desktop")

    assert zips(machine) == {(str(tmp_path / "connx-sql-expert.zip"), ("claude-desktop",))}


def test_a_zip_run_keeps_the_skill_copies_recorded_before(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """A --zip run installs nothing on this machine and forgets nothing an earlier run installed."""
    configure(cli, cdd)
    install_for_user(cli, "claude-code")
    skill_in_image(cli)

    code, body = cli.json("install-skill", "--zip", str(tmp_path / "out"), "--harness", "codex")

    assert code == ExitCode.OK, body
    assert copies(machine) == {(str(machine.home / ".claude" / "skills" / "connx-sql-expert"), ("claude-code",), None)}


def test_an_older_record_counts_every_harness_reading_a_copy_and_is_rewritten(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Copies without harnesses and a single zip still read; the next install writes the new shape."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    user_copy = machine.home / ".claude" / "skills" / "connx-sql-expert"
    project_copy = project / ".agents" / "skills" / "connx-sql-expert"
    archive = tmp_path / "out" / "connx-sql-expert.zip"
    old = {"places": [str(user_copy), str(project_copy)], "zip": str(archive), "instructions": []}
    (machine.base / "default" / skill.INSTALL_RECORD).write_text(json.dumps(old), encoding="utf-8")

    install_for_project(cli, project, "pi")

    record = recorded(machine)
    assert "places" not in record
    assert "zip" not in record
    assert record["zips"] == [{"path": str(archive), "harnesses": []}]
    assert copies(machine) == {
        (str(user_copy), ("claude-code",), None),
        (str(project_copy), ("codex", "pi", "hermes", "agents"), str(project.resolve())),
    }


def test_a_failed_zip_still_records_the_copy_and_file_written_before_it(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Desktop's zip fails after Claude Code's copy and CLAUDE.md are written: the record keeps those two."""
    configure(cli, cdd)
    skill_in_image(cli)
    (tmp_path / "connx-sql-expert.zip").mkdir()
    monkeypatch.chdir(tmp_path)

    code, body = cli.json("install-skill", "--harness", "claude-code", "--harness", "claude-desktop", "--user")

    assert code == ExitCode.ZIP_FAILED, body
    assert copies(machine) == {(str(machine.home / ".claude" / "skills" / "connx-sql-expert"), ("claude-code",), None)}
    assert [e["path"] for e in recorded(machine)["instructions"]] == [str(machine.home / ".claude" / "CLAUDE.md")]
    assert recorded(machine)["zips"] == []


def test_a_project_named_through_dot_dot_is_recorded_resolved(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """--project sales/../sales records the project as the directory itself."""
    configure(cli, cdd)
    project = sales_project(tmp_path)

    install_for_project(cli, project / ".." / "sales", "codex")

    assert {c[2] for c in copies(machine)} == {str(project.resolve())}
    assert {e["project"] for e in recorded(machine)["instructions"]} == {str(project.resolve())}


def test_two_harness_directories_linked_together_record_one_copy(cli: Cli, cdd: Path, machine: Machine) -> None:
    """With ~/.claude/skills a link to ~/.agents/skills, Claude Code and Codex share one copy and one entry."""
    configure(cli, cdd)
    (machine.home / ".agents" / "skills").mkdir(parents=True)
    (machine.home / ".claude").mkdir(parents=True, exist_ok=True)
    (machine.home / ".claude" / "skills").symlink_to(machine.home / ".agents" / "skills", target_is_directory=True)

    install_for_user(cli, "claude-code", "codex")

    [entry] = recorded(machine)["copies"]
    assert sorted(entry["harnesses"]) == ["claude-code", "codex"]


def test_a_user_install_then_a_project_install_at_home_keeps_both_scopes(cli: Cli, cdd: Path, machine: Machine) -> None:
    """--user then --project ~ write one copy; the record keeps an entry for each scope."""
    configure(cli, cdd)
    copy = str(machine.home / ".agents" / "skills" / "connx-sql-expert")

    install_for_user(cli, "codex")
    install_for_project(cli, machine.home, "codex")

    assert copies(machine) == {(copy, ("codex",), None), (copy, ("codex",), str(machine.home.resolve()))}


def test_an_install_keeps_entries_another_run_recorded_while_it_ran(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """A run that recorded a copy while this one copied the skill out keeps its entry."""
    configure(cli, cdd)
    other = str(tmp_path / "elsewhere" / ".agents" / "skills" / "connx-sql-expert")
    skill_in_image(cli)

    def another_run_finishes(args: list[str]) -> DockerResult:
        entry = {"path": other, "harnesses": ["agents"], "project": str(tmp_path / "elsewhere")}
        record = {"copies": [entry], "zips": [], "instructions": []}
        (env_dir(machine) / skill.INSTALL_RECORD).write_text(json.dumps(record), encoding="utf-8")
        return DockerResult(1, "", f"Error: Could not find the file {IN_IMAGE_INSTRUCTIONS} in container {CONTAINER}")

    cli.docker.act_when(copies_instructions, another_run_finishes)
    code, body = cli.json("install-skill", "--harness", "claude-code", "--user")

    assert code == ExitCode.OK, body
    assert copies(machine) == {
        (str(machine.home / ".claude" / "skills" / "connx-sql-expert"), ("claude-code",), None),
        (other, ("agents",), str(tmp_path / "elsewhere")),
    }


def test_a_zip_in_an_older_record_counts_as_installed(cli: Cli, cdd: Path, machine: Machine, tmp_path: Path) -> None:
    """A single recorded zip that is still there makes the skill installed."""
    configure(cli, cdd)
    skill_in_image(cli)
    code, body = cli.json("install-skill", "--zip", str(tmp_path / "out"), "--harness", "codex")
    assert code == ExitCode.OK, body
    archive = tmp_path / "out" / "connx-sql-expert.zip"
    old = {"places": [], "zip": str(archive)}
    (machine.base / "default" / skill.INSTALL_RECORD).write_text(json.dumps(old), encoding="utf-8")

    assert skill.installed(environment.load("default")) is True
    archive.unlink()
    assert skill.installed(environment.load("default")) is False


def test_a_copy_left_from_an_earlier_install_still_counts_as_installed(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    """Deleting the latest run's copy leaves the skill installed while an earlier run's copy is there."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install_for_user(cli, "claude-code")
    install_for_project(cli, project, "codex")

    shutil.rmtree(project / ".agents")

    assert skill.installed(environment.load("default")) is True


# --- Live -----------------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_install_skill_zips_the_skill_out_of_the_real_image(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Live: --zip for Claude Desktop zips the real image's skill under a folder named after it."""
    configured = cli(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd)
    )
    assert configured.exit_code == ExitCode.OK, configured.output
    out = tmp_path / "handover"

    result = cli("install-skill", "--zip", str(out), "--harness", "claude-desktop", "--json")

    assert result.exit_code == ExitCode.OK, result.output
    with zipfile.ZipFile(out / "connx-sql-expert.zip") as archive:
        names = archive.namelist()
    assert "connx-sql-expert/SKILL.md" in names
    assert all(n.startswith("connx-sql-expert/") for n in names)


@pytest.mark.live
def test_live_install_skill_copies_the_skill_out_of_the_real_image(cli: Cli, cdd: Path, machine: Machine) -> None:
    """Live: the skill comes out of the real image."""
    configured = cli(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd)
    )
    assert configured.exit_code == ExitCode.OK, configured.output

    result = cli("install-skill", "--harness", "claude-code", "--json")

    assert result.exit_code == ExitCode.OK, result.output
    target = machine.home / ".claude" / "skills" / "connx-sql-expert"
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target / ".connx-mcp-ce-install.json").is_file()


@pytest.mark.live
def test_live_install_skill_writes_the_image_instructions_into_a_project(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """Live: the real image ships INSTRUCTIONS.md, so the project gets the image's section at the image's version."""
    configured = cli(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd)
    )
    assert configured.exit_code == ExitCode.OK, configured.output
    project = sales_project(tmp_path)

    result = cli("install-skill", "--harness", "codex", "--project", str(project), "--json")

    assert result.exit_code == ExitCode.OK, result.output
    [entry] = json.loads(result.stdout)["result"]["instructions"]
    assert entry["source"] == "image"
    assert entry["version"]
    text = (project / "AGENTS.md").read_bytes().decode("utf-8")
    # A new AGENTS.md takes the OS's line ending: CRLF on Windows.
    assert f"<!-- connx-mcp-ce begin version={entry['version']} -->{os.linesep}" in text
    assert text.endswith(f"<!-- connx-mcp-ce end -->{os.linesep}")
