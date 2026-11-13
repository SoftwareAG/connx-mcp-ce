#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_uninstall_skill.py - The administrator uninstalls the skill and its skill instructions that install-skill wrote.

Description:
    Through the CLI seam with the fake Docker port, which only the install that sets up each case uses:
    uninstall-skill recomputes install-skill's targets for the named harnesses at one scope, adds the
    environment's recorded instructions files, shows a plan and asks, then deletes each skill copy that
    carries connx-mcp-ce's marker and removes each skill instructions section, keeping the rest of the file.

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
import shutil
import stat
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from connx_mcp import skill
from connx_mcp.output import ExitCode
from tests.conftest import Cli, Machine, as_typed
from tests.test_install_skill import (
    BUILT_IN_SECTION,
    LOOKALIKE_MARKER_FILE,
    LOOKALIKE_SECTION,
    LOOKALIKE_ZIP_COMMENT,
    configure,
    hand_edit,
    needs_permissions,
    sales_project,
    skill_in_image,
)
from tests.test_remove import project_containers, project_volumes

SKILL = "connx-sql-expert"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file the environments point at."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def install(cli: Cli, *args: str) -> dict:
    """Run install-skill with the given options; return the JSON result."""
    skill_in_image(cli)
    code, body = cli.json("install-skill", *args)
    assert code == ExitCode.OK, body
    return body["result"]


# --- The command and its options -----------------------------------------------------------------------


def test_uninstall_skill_is_listed_beside_install_skill_with_its_options(cli: Cli) -> None:
    listed = cli("--help").stdout
    shown = cli("uninstall-skill", "--help").stdout

    assert listed.index("install-skill") < listed.index("uninstall-skill")
    for option in ("--harness", "--user", "--project", "--yes", "--json", "--no-input"):
        assert option in shown, option


def test_user_and_project_together_are_a_usage_error(cli: Cli, tmp_path: Path) -> None:
    code, body = cli.json("uninstall-skill", "--harness", "codex", "--user", "--project", str(tmp_path))

    assert code == ExitCode.USAGE, body


def test_an_unknown_harness_is_a_usage_error(cli: Cli) -> None:
    code, body = cli.json("uninstall-skill", "--harness", "cursor", "--yes")

    assert code == ExitCode.USAGE, body
    assert "claude-code, codex, pi, hermes, agents" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce uninstall-skill --help"


# --- Removing what install wrote -----------------------------------------------------------------------


def test_a_project_install_for_codex_is_undone(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    copy = project / ".agents" / "skills" / SKILL
    assert not copy.exists()
    assert not (project / "AGENTS.md").exists()
    assert body["result"]["removed"] is True
    assert body["result"]["scope"] == "project"
    assert body["result"]["copies"] == [{"path": str(copy), "harnesses": ["codex"], "outcome": "deleted"}]
    assert body["result"]["instructions"] == [
        {"path": str(project / "AGENTS.md"), "harnesses": ["codex"], "outcome": "file_deleted"}
    ]


def test_uninstall_never_calls_docker(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))
    calls = len(cli.docker.calls)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert len(cli.docker.calls) == calls


@pytest.mark.parametrize(
    ("harness", "copy", "file"),
    [
        ("claude-code", ".claude/skills", ".claude/CLAUDE.md"),
        ("codex", ".agents/skills", ".codex/AGENTS.md"),
        ("pi", ".agents/skills", ".pi/agent/AGENTS.md"),
    ],
)
def test_a_user_install_is_undone_for_each_harness_with_a_user_file(
    cli: Cli, cdd: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch, harness: str, copy: str, file: str
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)
    install(cli, "--harness", harness, "--user")

    code, body = cli.json("uninstall-skill", "--harness", harness, "--user", "--yes")

    assert code == ExitCode.OK, body
    assert not (machine.home / copy / SKILL).exists()
    assert not (machine.home / file).exists()
    assert body["result"]["scope"] == "user"
    assert [(r["harnesses"], r["outcome"]) for r in body["result"]["copies"]] == [([harness], "deleted")]
    assert body["result"]["instructions"] == [
        {"path": str(machine.home / file), "harnesses": [harness], "outcome": "file_deleted"}
    ]


@pytest.mark.parametrize(("harness", "copy"), [("hermes", ".hermes/skills"), ("agents", ".agents/skills")])
def test_hermes_and_agents_at_user_scope_remove_only_their_skill_copy(
    cli: Cli, cdd: Path, machine: Machine, harness: str, copy: str
) -> None:
    if os.name == "nt" and harness == "hermes":
        copy = "AppData/Local/hermes/skills"
    configure(cli, cdd)
    install(cli, "--harness", harness, "--user")
    assert (machine.home / copy / SKILL / "SKILL.md").is_file()

    code, body = cli.json("uninstall-skill", "--harness", harness, "--yes")

    assert code == ExitCode.OK, body
    assert not (machine.home / copy / SKILL).exists()
    (skipped,) = body["result"]["instructions"]
    assert (skipped["path"], skipped["harnesses"], skipped["outcome"]) == (None, [harness], "skipped")


@pytest.mark.parametrize(
    ("harness", "copy"),
    [
        ("claude-code", ".claude/skills"),
        ("codex", ".agents/skills"),
        ("pi", ".agents/skills"),
        ("hermes", ".agents/skills"),
        ("agents", ".agents/skills"),
    ],
)
def test_a_project_install_is_undone_for_each_harness(
    cli: Cli, cdd: Path, tmp_path: Path, harness: str, copy: str
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", harness, "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", harness, "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert not (project / copy / SKILL).exists()
    assert not (project / "AGENTS.md").exists()


def test_claude_code_in_a_project_with_a_claude_md_has_its_section_removed_from_claude_md(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "CLAUDE.md").write_bytes(b"# Claude rules\n")
    install(cli, "--harness", "claude-code", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert (project / "CLAUDE.md").read_bytes() == b"# Claude rules\n"
    assert not (project / "AGENTS.md").exists()


def test_a_section_in_agents_md_is_found_after_agents_override_md_appeared(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    install(cli, "--harness", "codex", "--project", str(project))
    (project / "AGENTS.override.md").write_bytes(b"# Override\n")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"
    assert (project / "AGENTS.override.md").read_bytes() == b"# Override\n"
    assert [(r["path"], r["outcome"]) for r in body["result"]["instructions"]] == [
        (str(project / "AGENTS.override.md"), "not_found"),
        (str(project / "AGENTS.md"), "removed"),
    ]


def test_a_recorded_skill_copy_the_rules_no_longer_name_is_removed(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user", "--no-instructions")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    (entry,) = record["copies"]
    moved = machine.home / "old-skills" / SKILL
    moved.parent.mkdir()
    Path(entry["path"]).rename(moved)
    entry["path"] = str(moved)
    record_file.write_text(json.dumps(record), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--user", "--yes")

    assert code == ExitCode.OK, body
    assert not moved.exists()
    assert {"path": str(moved), "harnesses": ["codex"], "outcome": "deleted"} in body["result"]["copies"]


# --- Removing a section --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "own",
    [b"# Ours\n\nKeep this.\n", b"\xef\xbb\xbf# Ours\r\n\r\nKeep this.\r\n", b"# Ours\r\n", b"\xef\xbb\xbf# Ours\n"],
    ids=["lf", "bom-crlf", "crlf", "bom"],
)
def test_the_file_is_left_as_it_was_before_the_install(cli: Cli, cdd: Path, tmp_path: Path, own: bytes) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(own)
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").read_bytes() == own
    assert body["result"]["instructions"][0]["outcome"] == "removed"


def test_text_after_the_section_is_kept_with_one_blank_line_between(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    install(cli, "--harness", "codex", "--project", str(project))
    agents = project / "AGENTS.md"
    agents.write_bytes(agents.read_bytes() + b"\nAdded later.\n")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert agents.read_bytes() == b"# Ours\n\nAdded later.\n"


@pytest.mark.parametrize("own", [b"", b"\n\n", b"# AGENTS.md\n"], ids=["empty", "blank", "heading"])
def test_a_file_left_with_nothing_of_its_own_is_deleted(cli: Cli, cdd: Path, tmp_path: Path, own: bytes) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(own)
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert not (project / "AGENTS.md").exists()
    assert body["result"]["instructions"][0]["outcome"] == "file_deleted"


@needs_permissions
def test_a_rewritten_file_keeps_its_permissions(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"Mine.\n")
    (project / "AGENTS.md").chmod(0o640)
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in project.iterdir()) == [".agents", "AGENTS.md"]


def test_a_symlinked_user_file_is_edited_at_its_target(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)
    target = machine.home / "dotfiles" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"# Mine\n")
    (machine.home / ".claude").mkdir()
    (machine.home / ".claude" / "CLAUDE.md").symlink_to(target)
    install(cli, "--harness", "claude-code", "--user")

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--yes")

    assert code == ExitCode.OK, body
    assert (machine.home / ".claude" / "CLAUDE.md").is_symlink()
    assert target.read_bytes() == b"# Mine\n"


# --- Skill copies connx-mcp-ce did not install ------------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--force"]], ids=["plain", "force"])
def test_a_skill_directory_without_the_marker_is_left_in_place_and_reported(
    cli: Cli, cdd: Path, machine: Machine, flags: list[str]
) -> None:
    configure(cli, cdd)
    own = machine.home / ".claude" / "skills" / SKILL
    own.mkdir(parents=True)
    (own / "SKILL.md").write_bytes(b"my own skill\n")

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--yes", *flags)

    assert code == ExitCode.OK, body
    assert (own / "SKILL.md").read_bytes() == b"my own skill\n"
    assert body["result"]["copies"] == [{"path": str(own), "harnesses": ["claude-code"], "outcome": "left_foreign"}]
    assert body["result"]["removed"] is False
    result = cli("uninstall-skill", "--harness", "claude-code", "--yes")
    assert "not installed by connx-mcp-ce" in result.stdout


# --- Look-alike names without `-ce`: read as anyone else's ---------------------------------------------


def test_only_the_new_section_is_removed_beside_one_with_lookalike_markers(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A `<!-- connx-mcp begin -->` section is the administrator's text: only connx-mcp-ce's section goes."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    own = f"# Ours\n\n{LOOKALIKE_SECTION}\n".encode()
    (project / "AGENTS.md").write_bytes(own)
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").read_bytes() == own
    assert body["result"]["instructions"][0]["outcome"] == "removed"


def test_a_copy_with_only_a_lookalike_marker_file_is_left_in_place_and_reported(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """A copy marked `.connx-mcp-install.json` is not connx-mcp-ce's: it stays and is reported as left_foreign."""
    configure(cli, cdd)
    old = machine.home / ".claude" / "skills" / SKILL
    old.mkdir(parents=True)
    (old / "SKILL.md").write_bytes(b"old skill\n")
    (old / LOOKALIKE_MARKER_FILE).write_text(json.dumps({"installed_by": "connx-mcp"}), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--yes")

    assert code == ExitCode.OK, body
    assert (old / "SKILL.md").read_bytes() == b"old skill\n"
    assert body["result"]["copies"] == [{"path": str(old), "harnesses": ["claude-code"], "outcome": "left_foreign"}]
    assert body["result"]["removed"] is False


def test_a_copy_whose_marker_file_names_a_lookalike_tool_is_left_in_place_and_reported(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    """A `.connx-mcp-ce-install.json` whose `installed_by` is `connx-mcp` is not connx-mcp-ce's: it stays."""
    configure(cli, cdd)
    old = machine.home / ".claude" / "skills" / SKILL
    old.mkdir(parents=True)
    (old / "SKILL.md").write_bytes(b"old skill\n")
    (old / ".connx-mcp-ce-install.json").write_text(json.dumps({"installed_by": "connx-mcp"}), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--yes")

    assert code == ExitCode.OK, body
    assert (old / "SKILL.md").read_bytes() == b"old skill\n"
    assert body["result"]["copies"] == [{"path": str(old), "harnesses": ["claude-code"], "outcome": "left_foreign"}]
    assert body["result"]["removed"] is False


def test_a_zip_with_a_lookalike_archive_comment_is_kept_with_its_notes(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    """A connx-sql-expert.zip commented `Written by connx-mcp` is not connx-mcp-ce's: it and its notes stay."""
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME
    with zipfile.ZipFile(archive, "w") as old:
        old.comment = LOOKALIKE_ZIP_COMMENT
        old.writestr("connx-sql-expert/SKILL.md", "old skill")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["zips"] == [{"path": str(archive), "notes": str(notes), "outcome": "left_foreign"}]
    assert archive.is_file()
    assert notes.is_file()


# --- Confirmation --------------------------------------------------------------------------------------


def installed_for_codex(cli: Cli, cdd: Path, tmp_path: Path) -> Path:
    """Install the skill and its instructions for Codex in a project with its own AGENTS.md; return the project."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    install(cli, "--harness", "codex", "--project", str(project))
    return project


def unchanged(project: Path) -> bool:
    """Return whether the project still holds the skill copy and the section."""
    begin = BUILT_IN_SECTION.splitlines()[0].encode()
    copy = project / ".agents" / "skills" / SKILL / "SKILL.md"
    return copy.is_file() and begin in (project / "AGENTS.md").read_bytes()


def test_without_yes_and_without_a_terminal_input_is_required(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--no-input")

    assert code == ExitCode.INPUT_REQUIRED, body
    assert body["error"]["message"] == (
        f"connx-mcp-ce asks before it removes {SKILL}; pass --yes to remove it without a question."
    )
    assert body["error"]["next"] == (
        f"connx-mcp-ce uninstall-skill default --harness codex --project {as_typed(project)} --yes"
    )
    assert unchanged(project)


def test_a_terminal_shows_the_plan_and_removes_on_yes(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    result, screen = cli.typed("uninstall-skill", "--harness", "codex", "--project", str(project), keys=["y"])

    assert result.exit_code == ExitCode.OK, result.output
    assert f"The skill instructions section in {project / 'AGENTS.md'} is removed." in screen
    assert f"Skill copy {project / '.agents' / 'skills' / SKILL} is deleted." in screen
    assert f"Uninstall {SKILL}?" in screen
    assert not unchanged(project)


def test_the_plan_says_a_file_with_nothing_else_in_it_is_deleted(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))

    result, screen = cli.typed("uninstall-skill", "--harness", "codex", "--project", str(project), keys=["n"])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert f"File {project / 'AGENTS.md'} is deleted (nothing else was in it)." in screen


def test_human_output_says_what_was_removed(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    result = cli("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"The skill instructions section in {project / 'AGENTS.md'} was removed." in result.stdout
    assert f"Skill copy {project / '.agents' / 'skills' / SKILL} was deleted." in result.stdout


@pytest.mark.parametrize("keys", [["n"], [""], []], ids=["declined", "default-no", "end-of-input"])
def test_declining_or_cancelling_changes_nothing(cli: Cli, cdd: Path, tmp_path: Path, keys: list[str]) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    result, _ = cli.typed("uninstall-skill", "--harness", "codex", "--project", str(project), keys=keys)

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert "Nothing was removed." in result.stderr
    assert unchanged(project)


# --- Nothing to remove ---------------------------------------------------------------------------------


def test_nothing_to_remove_succeeds_and_lists_the_places_checked(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)

    code, body = cli.json("uninstall-skill", "--harness", "claude-code", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["removed"] is False
    copy, file = machine.home / ".claude" / "skills" / SKILL, machine.home / ".claude" / "CLAUDE.md"
    assert body["result"]["checked"] == [str(copy), str(file)]
    result = cli("uninstall-skill", "--harness", "claude-code", "--no-input")
    assert result.exit_code == ExitCode.OK
    assert "Nothing to remove" in result.stdout
    assert str(file) in result.stdout


def test_an_environment_never_installed_to_has_nothing_to_remove_without_harness(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("uninstall-skill", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["removed"] is False


# --- Refusals ------------------------------------------------------------------------------------------


def test_an_edited_section_is_refused_with_skill_changed(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    edited = hand_edit(project / "AGENTS.md")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.SKILL_CHANGED, body
    assert body["error"]["code"] == "skill_changed"
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert "edited" in body["error"]["message"]
    assert body["error"]["next"] == (
        f"connx-mcp-ce uninstall-skill default --harness codex --project {as_typed(project)} --force --yes"
    )
    assert (project / "AGENTS.md").read_bytes() == edited
    assert (project / ".agents" / "skills" / SKILL / "SKILL.md").is_file()


def test_a_section_edited_after_its_environment_went_is_refused_by_another_environments_record(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    edited = hand_edit(project / "AGENTS.md")

    # The environment default still claims the section: --shared reaches it, and its record vouches for no edit.
    code, body = cli.json(
        "uninstall-skill", "gone", "--harness", "codex", "--project", str(project), "--shared", "--yes"
    )

    assert code == ExitCode.SKILL_CHANGED, body
    assert "edited" in body["error"]["message"]
    assert (project / "AGENTS.md").read_bytes() == edited


def test_broken_markers_are_refused_with_skill_changed(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    broken = agents.read_bytes() + b"\n<!-- connx-mcp-ce end -->\n"
    agents.write_bytes(broken)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.SKILL_CHANGED, body
    assert "markers" in body["error"]["message"]
    assert agents.read_bytes() == broken
    assert (project / ".agents" / "skills" / SKILL / "SKILL.md").is_file()


def test_a_project_file_linked_outside_the_project_is_refused(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    outside = tmp_path / "dotfiles" / "notes.md"
    outside.parent.mkdir()
    outside.write_bytes(b"# Notes\n")
    (project / "AGENTS.md").symlink_to(outside)
    install(cli, "--harness", "codex", "--project", str(project), "--force")
    written = outside.read_bytes()

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.SKILL_CHANGED, body
    assert str(outside) in body["error"]["message"]
    assert outside.read_bytes() == written


def test_a_file_that_is_not_utf8_is_refused_as_unreadable(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    latin = agents.read_bytes() + b"f\xfcr Codex\n"
    agents.write_bytes(latin)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(agents) in body["error"]["message"]
    assert agents.read_bytes() == latin
    assert (project / ".agents" / "skills" / SKILL / "SKILL.md").is_file()


@needs_permissions
def test_a_read_only_file_is_refused_as_unreadable(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    (project / "AGENTS.md").chmod(0o444)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "read-only" in body["error"]["message"]
    assert unchanged(project)


@needs_permissions
def test_a_file_that_cannot_be_read_is_refused_as_unreadable(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    agents.chmod(0o000)
    try:
        code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")
    finally:
        agents.chmod(0o644)

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(agents) in body["error"]["message"]
    assert "cannot be read" in body["error"]["message"]
    assert unchanged(project)


# --- --force -------------------------------------------------------------------------------------------


def test_force_removes_an_edited_section_and_the_plan_names_it_edited(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    hand_edit(project / "AGENTS.md")

    result, screen = cli.typed(
        "uninstall-skill", "--harness", "codex", "--project", str(project), "--force", keys=["y"]
    )

    assert result.exit_code == ExitCode.OK, result.output
    assert f"The skill instructions section in {project / 'AGENTS.md'} is removed, though it was edited." in screen
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"
    assert not (project / ".agents" / "skills" / SKILL).exists()


@pytest.mark.parametrize(
    ("after", "left"),
    [
        (b"\nMine\n\n<!-- connx-mcp-ce end -->\n\nTail\n", b"# Ours\n\nMine\n\nTail\n"),
        (b"<!-- connx-mcp-ce end -->\n", b"# Ours\n"),
    ],
    ids=["stray-between-text", "stray-at-the-end"],
)
def test_force_strips_broken_markers_and_keeps_the_rest_of_the_file(
    cli: Cli, cdd: Path, tmp_path: Path, after: bytes, left: bytes
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    agents.write_bytes(agents.read_bytes() + after)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--force", "--yes")

    assert code == ExitCode.OK, body
    assert agents.read_bytes() == left
    assert body["result"]["instructions"] == [
        {"path": str(agents), "harnesses": ["codex"], "outcome": "removed", "forced": "broken_markers"}
    ]


def test_force_removes_the_section_from_a_project_file_linked_outside(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    outside = tmp_path / "dotfiles" / "notes.md"
    outside.parent.mkdir()
    outside.write_bytes(b"# Notes\n")
    (project / "AGENTS.md").symlink_to(outside)
    install(cli, "--harness", "codex", "--project", str(project), "--force")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--force", "--yes")

    assert code == ExitCode.OK, body
    assert outside.read_bytes() == b"# Notes\n"
    assert (project / "AGENTS.md").is_symlink()
    assert body["result"]["instructions"][0]["forced"] == "outside_project"


def test_force_leaves_a_file_that_is_not_utf8_refused_as_unreadable(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    latin = agents.read_bytes() + b"f\xfcr Codex\n"
    agents.write_bytes(latin)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--force", "--yes")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert agents.read_bytes() == latin
    assert (project / ".agents" / "skills" / SKILL / "SKILL.md").is_file()


@needs_permissions
def test_force_leaves_an_edited_read_only_file_refused_as_unreadable(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    edited = hand_edit(project / "AGENTS.md")
    (project / "AGENTS.md").chmod(0o444)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--force", "--yes")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "read-only" in body["error"]["message"]
    assert (project / "AGENTS.md").read_bytes() == edited


def test_forceable_and_unforceable_refusals_together_exit_unreadable_and_name_force(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    (project / "CLAUDE.md").write_bytes(b"# Claude\n")
    install(cli, "--harness", "codex", "--harness", "claude-code", "--project", str(project))
    hand_edit(project / "AGENTS.md")
    claude = project / "CLAUDE.md"
    claude.write_bytes(claude.read_bytes() + b"f\xfcr Claude\n")

    code, body = cli.json(
        "uninstall-skill", "--harness", "codex", "--harness", "claude-code", "--project", str(project), "--yes"
    )

    assert code == ExitCode.FILE_UNREADABLE, body
    assert str(project / "AGENTS.md") in body["error"]["message"]
    assert str(claude) in body["error"]["message"]
    assert body["error"]["next"] == (
        "connx-mcp-ce uninstall-skill default --harness codex --harness claude-code "
        f"--project {as_typed(project)} --force --yes"
    )


# --- A failure part-way --------------------------------------------------------------------------------


def recorded(machine: Machine) -> dict:
    """Return the default environment's install record."""
    return json.loads((machine.base / "default" / skill.INSTALL_RECORD).read_text(encoding="utf-8"))


@needs_permissions
def test_a_failure_part_way_names_what_was_removed_and_a_rerun_finishes(
    cli: Cli, cdd: Path, tmp_path: Path, machine: Machine
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))
    agents, copy = project / "AGENTS.md", project / ".agents" / "skills" / SKILL
    env_dir = machine.base / "default"
    env_dir.chmod(0o555)
    try:
        code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")
    finally:
        env_dir.chmod(0o755)

    assert code == ExitCode.UNINSTALL_FAILED, body
    assert body["error"]["code"] == "uninstall_failed"
    assert f"Removed before the failure:\n  {agents}" in body["error"]["message"]
    assert body["error"]["removed_paths"] == [str(agents)]
    assert not agents.exists()
    assert (copy / "SKILL.md").is_file()

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert recorded(machine)["copies"] == []
    assert recorded(machine)["instructions"] == []


@pytest.mark.parametrize(
    "written", ["{not json", "[]", '{"places": 5}'], ids=["not-json", "not-an-object", "older-shape-spoiled"]
)
def test_a_record_spoiled_during_the_run_stops_it_and_names_what_was_removed(
    cli: Cli, cdd: Path, tmp_path: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents, copy = project / "AGENTS.md", project / ".agents" / "skills" / SKILL
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    read_record = skill.read_record

    def read_then_spoil(env: object) -> object:
        """Read the record as the plan does, then let something else spoil it before the first removal."""
        record = read_record(env)  # type: ignore[arg-type]
        record_file.write_text(written, encoding="utf-8")
        return record

    monkeypatch.setattr(skill, "read_record", read_then_spoil)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.UNINSTALL_FAILED, body
    assert f"Could not update the skill install record {record_file}" in body["error"]["message"]
    assert body["error"]["removed_paths"] == [str(agents)]
    assert (copy / "SKILL.md").is_file()


def test_a_copy_left_part_deleted_by_a_failed_run_is_deleted_and_reported_by_a_rerun(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    skills = project / ".agents" / "skills"

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        """Fail as a file held open by another program makes the deletion fail."""
        raise PermissionError(13, "Permission denied", str(path))

    with monkeypatch.context() as patched:
        patched.setattr(shutil, "rmtree", refuse)
        code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")
    assert code == ExitCode.UNINSTALL_FAILED, body
    assert f"Could not remove {skills / SKILL}" in body["error"]["message"]
    [leftover] = list(skills.iterdir())

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert {"path": str(leftover), "harnesses": ["codex"], "outcome": "deleted"} in body["result"]["copies"]
    assert list(skills.iterdir()) == []


@needs_permissions
def test_a_read_only_skills_directory_is_refused_before_anything_is_removed(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    skills = project / ".agents" / "skills"
    skills.chmod(0o555)
    try:
        code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--force", "--yes")
    finally:
        skills.chmod(0o755)

    assert code == ExitCode.FILE_UNREADABLE, body
    assert f"{skills / SKILL} cannot be deleted, because {skills} is read-only" in body["error"]["message"]
    assert unchanged(project)
    assert sorted(p.name for p in skills.iterdir()) == [SKILL]


def test_a_copy_holding_a_read_only_file_is_deleted(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    skills = project / ".agents" / "skills"
    locked = skills / SKILL / "kept.txt"
    locked.write_text("kept", encoding="utf-8")
    locked.chmod(stat.S_IREAD)

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert list(skills.iterdir()) == []


@needs_permissions
def test_a_copy_holding_a_read_only_directory_is_deleted(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    skills = project / ".agents" / "skills"
    locked = skills / SKILL / "locked"
    locked.mkdir()
    (locked / "kept.txt").write_text("kept", encoding="utf-8")
    locked.chmod(0o555)
    try:
        code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")
    finally:
        for left in skills.rglob("locked"):
            left.chmod(0o755)

    assert code == ExitCode.OK, body
    assert list(skills.iterdir()) == []


def while_asked(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Make a change while the confirmation waits for its answer, as the administrator or a harness might."""
    confirm = skill._confirm_uninstall

    def confirm_then_change(*args: Any) -> None:
        confirm(*args)
        change()

    monkeypatch.setattr(skill, "_confirm_uninstall", confirm_then_change)


def test_an_instructions_file_edited_while_the_question_waits_is_left_as_edited(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    agents = project / "AGENTS.md"
    while_asked(monkeypatch, lambda: agents.write_bytes(agents.read_bytes() + b"\nMy late edit.\n"))

    result, _ = cli.typed("uninstall-skill", "--harness", "codex", "--project", str(project), keys=["y"])

    assert result.exit_code == ExitCode.UNINSTALL_FAILED, result.output
    assert agents.read_bytes().endswith(b"\nMy late edit.\n")
    assert unchanged(project)
    assert f"Could not remove {agents}: it changed after connx-mcp-ce read it" in result.output

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert agents.read_bytes() == b"# Ours\n\nMy late edit.\n"


def test_a_link_pointed_elsewhere_while_the_question_waits_is_left(
    cli: Cli, cdd: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(cli, cdd)
    target, other = machine.home / "dotfiles" / "CLAUDE.md", machine.home / "dotfiles" / "OTHER.md"
    target.parent.mkdir()
    target.write_bytes(b"# Mine\n")
    other.write_bytes(b"# Other\n")
    link = machine.home / ".claude" / "CLAUDE.md"
    link.parent.mkdir()
    link.symlink_to(target)
    install(cli, "--harness", "claude-code", "--user")
    written = target.read_bytes()

    def point_elsewhere() -> None:
        link.unlink()
        link.symlink_to(other)

    while_asked(monkeypatch, point_elsewhere)

    result, _ = cli.typed("uninstall-skill", "--harness", "claude-code", keys=["y"])

    assert result.exit_code == ExitCode.UNINSTALL_FAILED, result.output
    assert other.read_bytes() == b"# Other\n"
    assert target.read_bytes() == written


def test_a_copy_swapped_for_another_skill_while_the_question_waits_is_left_in_place(
    cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    copy = project / ".agents" / "skills" / SKILL

    def swap() -> None:
        shutil.rmtree(copy)
        copy.mkdir()
        (copy / "SKILL.md").write_text("my own skill", encoding="utf-8")

    while_asked(monkeypatch, swap)

    result, _ = cli.typed("uninstall-skill", "--harness", "codex", "--project", str(project), keys=["y"])

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").read_text(encoding="utf-8") == "my own skill"
    assert sorted(p.name for p in copy.parent.iterdir()) == [SKILL]
    assert f"Skill copy {copy} was left in place: not installed by connx-mcp-ce." in result.stdout


def test_a_renamed_aside_directory_of_another_name_is_never_deleted(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user", "--no-instructions")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    stuff = machine.home / "stuff"
    aside = stuff / ".mine.0123456789abcdef.deleted"
    aside.mkdir(parents=True)
    (aside / "precious.txt").write_text("mine", encoding="utf-8")
    record["copies"].append({"path": str(stuff / "mine"), "harnesses": ["codex"], "project": None})
    record_file.write_text(json.dumps(record), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--user", "--yes")

    assert code == ExitCode.OK, body
    assert (aside / "precious.txt").is_file()
    assert str(aside) not in [row["path"] for row in body["result"]["copies"]]


@pytest.mark.parametrize("key", ["copies", "instructions", "zips"])
def test_a_record_naming_a_relative_path_cannot_be_read(cli: Cli, cdd: Path, machine: Machine, key: str) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record[key] = [{"path": f"relative/{SKILL}", "harnesses": ["codex"], "project": None}]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.FILE_UNREADABLE, body
    assert "not an install record" in body["error"]["message"]


def test_the_record_drops_each_entry_it_removed(cli: Cli, cdd: Path, tmp_path: Path, machine: Machine) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))
    install(cli, "--harness", "claude-code", "--user")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    record = recorded(machine)
    assert [c["path"] for c in record["copies"]] == [str(machine.home / ".claude" / "skills" / SKILL)]
    assert [i["path"] for i in record["instructions"]] == [str(machine.home / ".claude" / "CLAUDE.md")]
    assert list((project / ".agents" / "skills").iterdir()) == []


def test_the_record_drops_only_the_named_harness_from_an_entry(
    cli: Cli, cdd: Path, tmp_path: Path, machine: Machine
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--harness", "pi", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    record = recorded(machine)
    assert [(c["path"], c["harnesses"]) for c in record["copies"]] == [
        (str(project / ".agents" / "skills" / SKILL), ["pi"])
    ]
    assert [(i["path"], i["harnesses"]) for i in record["instructions"]] == [(str(project / "AGENTS.md"), ["pi"])]
    assert "uninstalled" not in record


def test_a_plain_run_after_the_zip_was_deleted_by_hand_ends_uninstalled(
    cli: Cli, cdd: Path, tmp_path: Path, machine: Machine
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--project", str(project))
    install(cli, "--harness", "codex", "--zip", str(out))
    (out / skill.ZIP_NAME).unlink()

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    record = recorded(machine)
    assert (record["copies"], record["instructions"], record["zips"]) == ([], [], [])
    assert "uninstalled" in record


# --- Without the environment ---------------------------------------------------------------------------


def test_a_missing_environment_with_harness_uses_the_recomputed_targets(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    # The environment default still claims the copy and the section, so only --shared removes them.
    code, body = cli.json(
        "uninstall-skill", "gone", "--harness", "codex", "--project", str(project), "--shared", "--yes"
    )

    assert code == ExitCode.OK, body
    assert body["result"]["environment"] == "gone"
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"
    assert not (project / ".agents" / "skills" / SKILL).exists()


def test_a_missing_environment_without_harness_and_without_a_terminal_needs_input(cli: Cli) -> None:
    cli.docker.executables["codex"] = "/usr/local/bin/codex"

    code, body = cli.json("uninstall-skill", "gone", "--no-input")

    assert code == ExitCode.INPUT_REQUIRED, body
    assert "codex" in body["error"]["message"]
    assert body["error"]["next"].startswith("connx-mcp-ce uninstall-skill gone --harness codex")


def test_a_missing_environment_without_harness_offers_the_detected_harnesses(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)
    for name in ("claude", "pi", "hermes"):
        cli.docker.executables[name] = None
    cli.docker.executables["codex"] = "/usr/local/bin/codex"

    result, screen = cli.typed("uninstall-skill", "gone", "--project", str(project), "--shared", keys=["", "y"])

    assert result.exit_code == ExitCode.OK, result.output
    assert "codex (Codex)" in screen
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"


# --- The plain run and the flag rule -------------------------------------------------------------------


@pytest.fixture
def two_scopes(cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install Claude Code and Codex for the user and for a project; return the project."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "claude-code", "--user")
    install(cli, "--harness", "codex", "--user")
    install(cli, "--harness", "claude-code", "--harness", "codex", "--project", str(project))
    return project


def left(machine: Machine, project: Path) -> dict[str, bool]:
    """Return which skill copies and instructions files the two-scope install wrote are still there."""
    return {
        "user claude-code copy": (machine.home / ".claude" / "skills" / SKILL).exists(),
        "user CLAUDE.md": (machine.home / ".claude" / "CLAUDE.md").exists(),
        "user codex copy": (machine.home / ".agents" / "skills" / SKILL).exists(),
        "user codex AGENTS.md": (machine.home / ".codex" / "AGENTS.md").exists(),
        "project claude-code copy": (project / ".claude" / "skills" / SKILL).exists(),
        "project codex copy": (project / ".agents" / "skills" / SKILL).exists(),
        "project AGENTS.md": (project / "AGENTS.md").exists(),
    }


def test_a_plain_run_removes_everything_the_environment_installed(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "claude-code", "--user")
    install(cli, "--harness", "codex", "--project", str(project))

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    assert not (machine.home / ".claude" / "skills" / SKILL).exists()
    assert not (machine.home / ".claude" / "CLAUDE.md").exists()
    assert not (project / ".agents" / "skills" / SKILL).exists()
    assert not (project / "AGENTS.md").exists()
    assert body["result"]["removed"] is True
    assert body["result"]["scope"] == "all"


def test_harness_alone_removes_that_harness_at_every_recorded_scope(
    cli: Cli, machine: Machine, two_scopes: Path
) -> None:
    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    found = left(machine, two_scopes)
    # Both harnesses read the project's AGENTS.md: whether its section stays is the shared-copy rule's.
    found.pop("project AGENTS.md")
    assert found == {
        "user claude-code copy": True,
        "user CLAUDE.md": True,
        "user codex copy": False,
        "user codex AGENTS.md": False,
        "project claude-code copy": True,
        "project codex copy": False,
    }


def test_project_alone_removes_every_harness_in_that_project(cli: Cli, machine: Machine, two_scopes: Path) -> None:
    code, body = cli.json("uninstall-skill", "--project", str(two_scopes), "--yes")

    assert code == ExitCode.OK, body
    assert body["result"]["scope"] == "project"
    assert left(machine, two_scopes) == {
        "user claude-code copy": True,
        "user CLAUDE.md": True,
        "user codex copy": True,
        "user codex AGENTS.md": True,
        "project claude-code copy": False,
        "project codex copy": False,
        "project AGENTS.md": False,
    }


def test_user_alone_removes_every_harness_for_the_user(cli: Cli, machine: Machine, two_scopes: Path) -> None:
    code, body = cli.json("uninstall-skill", "--user", "--yes")

    assert code == ExitCode.OK, body
    assert body["result"]["scope"] == "user"
    assert left(machine, two_scopes) == {
        "user claude-code copy": False,
        "user CLAUDE.md": False,
        "user codex copy": False,
        "user codex AGENTS.md": False,
        "project claude-code copy": True,
        "project codex copy": True,
        "project AGENTS.md": True,
    }


def test_a_plain_run_finds_the_section_in_agents_md_after_agents_override_md_appeared(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    install(cli, "--harness", "codex", "--project", str(project))
    (project / "AGENTS.override.md").write_bytes(b"# Override\n")

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"
    assert (project / "AGENTS.override.md").read_bytes() == b"# Override\n"
    assert not (project / ".agents" / "skills" / SKILL).exists()


def test_a_plain_run_without_a_terminal_names_the_plain_retry(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    project = installed_for_codex(cli, cdd, tmp_path)

    code, body = cli.json("uninstall-skill", "--no-input")

    assert code == ExitCode.INPUT_REQUIRED, body
    assert body["error"]["next"] == "connx-mcp-ce uninstall-skill default --yes"
    assert unchanged(project)


def test_harness_without_a_record_removes_the_recomputed_user_targets(
    cli: Cli, cdd: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user")
    (machine.base / "default" / skill.INSTALL_RECORD).unlink()

    plain_code, plain = cli.json("uninstall-skill", "--yes")
    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert plain_code == ExitCode.OK, plain
    assert plain["result"]["removed"] is False
    assert code == ExitCode.OK, body
    assert body["result"]["scope"] == "user"
    assert not (machine.home / ".agents" / "skills" / SKILL).exists()
    assert not (machine.home / ".codex" / "AGENTS.md").exists()


# --- The record after an uninstall ---------------------------------------------------------------------


def record_of(machine: Machine) -> dict:
    """Return the default environment's install record as written."""
    return json.loads((machine.base / "default" / skill.INSTALL_RECORD).read_text(encoding="utf-8"))


def test_a_full_uninstall_records_the_date_it_was_uninstalled(cli: Cli, machine: Machine, two_scopes: Path) -> None:
    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    record = record_of(machine)
    assert date.fromisoformat(record["uninstalled"]) == date.today()
    assert (record["copies"], record["instructions"]) == ([], [])


def test_a_plain_run_deletes_the_recorded_zip_and_dates_the_record(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    assert not (out / skill.ZIP_NAME).exists()
    assert [s["harness"] for s in body["result"]["steps"]] == ["codex"]
    assert date.fromisoformat(record_of(machine)["uninstalled"]) == date.today()


def test_a_scope_flag_leaves_the_recorded_zip(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))

    code, body = cli.json("uninstall-skill", "--user", "--yes")

    assert code == ExitCode.OK, body
    assert (out / skill.ZIP_NAME).is_file()
    assert body["result"]["zips"] == []


def test_a_partial_uninstall_records_no_date(cli: Cli, machine: Machine, two_scopes: Path) -> None:
    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    record = record_of(machine)
    assert "uninstalled" not in record
    assert {h for c in record["copies"] for h in c["harnesses"]} == {"claude-code"}


def test_a_later_install_clears_the_uninstalled_date(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "claude-code", "--user")
    code, body = cli.json("uninstall-skill", "--yes")
    assert code == ExitCode.OK, body

    install(cli, "--harness", "claude-code", "--user")

    assert "uninstalled" not in record_of(machine)


# --- Exit codes ----------------------------------------------------------------------------------------


def test_exit_codes_lists_skill_changed_and_uninstall_failed(cli: Cli) -> None:
    code, body = cli.json("exit-codes")

    assert code == ExitCode.OK, body
    listed = {c["exit_code"]: c["code"] for c in body["result"]["exit_codes"]}
    assert (listed[123], listed[124]) == ("skill_changed", "uninstall_failed")


# --- Claude Desktop, the zip handover and registration -------------------------------------------------


def test_claude_desktop_gets_the_manual_steps_and_no_question(cli: Cli) -> None:
    code, body = cli.json("uninstall-skill", "gone", "--harness", "claude-desktop", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["removed"] is False
    [row] = body["result"]["steps"]
    assert row["harness"] == "claude-desktop"
    assert "Customize > Skills" in row["steps"][0]
    assert SKILL in row["steps"][0]
    assert "project instructions" in row["steps"][1]
    assert "<!-- connx-mcp-ce begin" in row["steps"][1]
    shown = cli("uninstall-skill", "gone", "--harness", "claude-desktop", "--no-input")
    assert shown.exit_code == ExitCode.OK, shown.output
    assert "Customize > Skills" in shown.stdout


def recorded_zips(name: str = "default") -> list[dict]:
    """Return the zips in an environment's install record."""
    record = Path(os.environ["MCP_ENVIRONMENT_PATH"]) / name / skill.INSTALL_RECORD
    return json.loads(record.read_text(encoding="utf-8"))["zips"]


def test_a_recorded_zip_and_its_notes_are_deleted_and_dropped_from_the_record(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    assert not archive.exists()
    assert not notes.exists()
    assert body["result"]["removed"] is True
    assert body["result"]["zips"] == [{"path": str(archive), "notes": str(notes), "outcome": "deleted"}]
    assert recorded_zips() == []


def test_the_plan_lists_the_zip_and_its_notes(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME

    result, screen = cli.typed("uninstall-skill", "--harness", "codex", keys=["n"])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert f"Zip {archive} and its install notes {notes} are deleted." in screen
    assert archive.is_file()
    assert notes.is_file()


def test_a_zip_without_connx_mcps_archive_comment_is_kept_with_its_notes(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME
    with zipfile.ZipFile(archive, "w") as theirs:
        theirs.writestr("connx-sql-expert/SKILL.md", "their fork")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["removed"] is False
    assert body["result"]["zips"] == [{"path": str(archive), "notes": str(notes), "outcome": "left_foreign"}]
    assert archive.is_file()
    assert notes.is_file()
    assert [Path(z["path"]) for z in recorded_zips()] == [archive]
    shown = cli("uninstall-skill", "--harness", "codex", "--no-input").stdout
    assert f"Zip {archive} was left in place: not written by connx-mcp-ce." in shown
    assert f"Install notes {notes} were left in place" in shown


def test_notes_without_connx_mcps_zip_beside_them_are_kept(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME
    archive.unlink()

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["removed"] is False
    assert body["result"]["zips"] == [{"path": str(archive), "notes": str(notes), "outcome": "not_found"}]
    assert notes.is_file()


@needs_permissions
def test_notes_that_cannot_be_deleted_stop_the_run_and_a_rerun_deletes_zip_and_notes(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    project = sales_project(tmp_path)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--project", str(project))
    install(cli, "--harness", "codex", "--zip", str(out))
    archive, notes = out / skill.ZIP_NAME, out / skill.NOTES_NAME
    agents, copy = project / "AGENTS.md", project / ".agents" / "skills" / SKILL
    out.chmod(0o555)
    try:
        code, body = cli.json("uninstall-skill", "--yes")
    finally:
        out.chmod(0o755)

    assert code == ExitCode.UNINSTALL_FAILED, body
    assert body["error"]["removed_paths"] == [str(agents), str(copy)]
    assert f"Could not remove {notes}" in body["error"]["message"]
    assert archive.is_file()
    assert notes.is_file()
    assert [Path(z["path"]) for z in recorded_zips()] == [archive]

    code, body = cli.json("uninstall-skill", "--yes")

    assert code == ExitCode.OK, body
    assert not archive.exists()
    assert not notes.exists()
    assert recorded_zips() == []


def test_the_record_keeps_fields_it_does_not_know(cli: Cli, cdd: Path, tmp_path: Path, machine: Machine) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "codex", "--zip", str(out))
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record["written_by"] = "a later connx-mcp-ce"
    record_file.write_text(json.dumps(record), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    assert not (out / skill.ZIP_NAME).exists()
    left = recorded(machine)
    assert left["written_by"] == "a later connx-mcp-ce"


def test_a_harness_handed_the_zip_gets_the_manual_steps(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--zip", str(tmp_path / "out"))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    [row] = body["result"]["steps"]
    assert row["harness"] == "codex"
    assert "Codex" in row["steps"][0]
    assert SKILL in row["steps"][0]
    assert "<!-- connx-mcp-ce begin" in row["steps"][1]


def test_a_zip_from_an_older_record_gives_its_harness_the_manual_steps(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "pi", "--zip", str(out))
    record = Path(os.environ["MCP_ENVIRONMENT_PATH"]) / "default" / skill.INSTALL_RECORD
    body = json.loads(record.read_text(encoding="utf-8"))
    body["zips"] = [str(out / skill.ZIP_NAME)]
    record.write_text(json.dumps(body), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--yes")

    assert code == ExitCode.OK, body
    assert body["result"]["zips"][0]["outcome"] == "deleted"
    assert [row["harness"] for row in body["result"]["steps"]] == ["codex"]


def test_a_zip_written_for_another_harness_is_left_alone(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    out = tmp_path / "out"
    install(cli, "--harness", "pi", "--zip", str(out))

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--no-input")

    assert code == ExitCode.OK, body
    assert body["result"]["zips"] == []
    assert body["result"]["steps"] == []
    assert (out / skill.ZIP_NAME).is_file()


def test_every_run_returns_optional_unregister_steps_per_harness(cli: Cli) -> None:
    code, body = cli.json(
        "uninstall-skill", "gone", "--harness", "claude-code", "--harness", "codex", "--harness", "claude-desktop",
        "--no-input",
    )  # fmt: skip

    assert code == ExitCode.OK, body
    rows = {r["harness"]: r for r in body["result"]["registration"]}
    assert list(rows) == ["claude-code", "codex", "claude-desktop"]
    assert rows["claude-code"]["when"] == "If you no longer use this server from Claude Code"
    assert rows["claude-code"]["steps"][0] == "claude mcp remove connx"
    assert rows["codex"]["steps"][0] == "codex mcp remove connx"
    assert "Customize > Connectors" in rows["claude-desktop"]["steps"][0]
    shown = cli("uninstall-skill", "gone", "--harness", "codex", "--no-input").stdout
    assert "If you no longer use this server from Codex:" in shown
    assert "codex mcp remove connx" in shown


@pytest.mark.parametrize("harness", list(skill.HARNESSES))
def test_each_harness_has_unregister_steps_that_name_the_server(cli: Cli, harness: str) -> None:
    code, body = cli.json("uninstall-skill", "gone", "--harness", harness, "--no-input")

    assert code == ExitCode.OK, body
    [row] = body["result"]["registration"]
    assert row["harness"] == harness
    assert "connx" in " ".join(row["steps"])


def test_a_run_with_only_manual_steps_lists_no_empty_places(cli: Cli) -> None:
    shown = cli("uninstall-skill", "gone", "--harness", "claude-desktop", "--no-input")

    assert shown.exit_code == ExitCode.OK, shown.output
    assert shown.stdout.startswith("Nothing for connx-mcp-ce to remove here.\n")
    assert "looked at these places" not in shown.stdout


def test_pis_unregister_steps_are_marked_not_confirmed(cli: Cli) -> None:
    code, body = cli.json("uninstall-skill", "gone", "--harness", "pi", "--harness", "codex", "--no-input")

    assert code == ExitCode.OK, body
    rows = {r["harness"]: r["confirmed"] for r in body["result"]["registration"]}
    assert rows == {"pi": False, "codex": True}


# --- Shared copies -------------------------------------------------------------------------------------


@pytest.fixture
def sales_and_hr(cli: Cli, cdd: Path, machine: Machine) -> tuple[Path, Path]:
    """Install Claude Code for the user from environments sales and hr; return the copy and CLAUDE.md."""
    for name in ("sales", "hr"):
        configure(cli, cdd, name=name)
        install(cli, name, "--harness", "claude-code", "--user")
    return machine.home / ".claude" / "skills" / SKILL, machine.home / ".claude" / "CLAUDE.md"


def holds_section(path: Path) -> bool:
    """Return whether an instructions file holds the skill instructions section."""
    return path.is_file() and BUILT_IN_SECTION.splitlines()[0].encode() in path.read_bytes()


def record_named(machine: Machine, name: str) -> dict:
    """Return an environment's install record as written."""
    return json.loads((machine.base / name / skill.INSTALL_RECORD).read_text(encoding="utf-8"))


def test_a_copy_another_environment_uses_is_kept_and_named(
    cli: Cli, machine: Machine, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, claude_md = sales_and_hr

    code, body = cli.json("uninstall-skill", "sales", "--no-input")

    assert code == ExitCode.OK, body
    assert (copy / "SKILL.md").is_file()
    assert holds_section(claude_md)
    result = body["result"]
    assert result["removed"] is False
    assert result["copies"] == [{"path": str(copy), "harnesses": ["claude-code"], "outcome": "kept_shared"}]
    assert result["instructions"] == [{"path": str(claude_md), "harnesses": ["claude-code"], "outcome": "kept_shared"}]
    claimants = [{"environment": "hr", "harnesses": ["claude-code"], "project": None}]
    retry = "connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared"
    assert result["kept"] == [
        {"path": str(copy), "claimants": claimants, "retry": retry},
        {"path": str(claude_md), "claimants": claimants, "retry": retry},
    ]
    record = record_named(machine, "sales")
    assert (record["copies"], record["instructions"]) == ([], [])


def test_human_output_says_which_environment_also_uses_a_kept_copy(cli: Cli, sales_and_hr: tuple[Path, Path]) -> None:
    copy, claude_md = sales_and_hr

    result = cli("uninstall-skill", "sales", "--no-input")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"Skill copy {copy} was kept: environment hr also uses it." in result.stdout
    assert f"The skill instructions section in {claude_md} was kept: environment hr also uses it." in result.stdout
    assert "  connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared\n" in result.stdout


def test_the_last_environment_to_uninstall_removes_the_shared_copy(cli: Cli, sales_and_hr: tuple[Path, Path]) -> None:
    copy, claude_md = sales_and_hr
    assert cli.json("uninstall-skill", "sales", "--no-input")[0] == ExitCode.OK

    code, body = cli.json("uninstall-skill", "hr", "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert not claude_md.exists()
    assert body["result"]["kept"] == []


@pytest.fixture
def codex_and_pi(cli: Cli, cdd: Path, tmp_path: Path) -> Path:
    """Install Codex, then Pi, into one project; return the project."""
    configure(cli, cdd)
    project = sales_project(tmp_path)
    install(cli, "--harness", "codex", "--project", str(project))
    install(cli, "--harness", "pi", "--project", str(project))
    return project


def test_a_copy_another_harness_uses_is_kept_until_that_harness_uninstalls(
    cli: Cli, machine: Machine, codex_and_pi: Path
) -> None:
    project = codex_and_pi
    copy, agents = project / ".agents" / "skills" / SKILL, project / "AGENTS.md"

    result = cli("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").is_file()
    assert holds_section(agents)
    assert f"Skill copy {copy} was kept: pi also uses it." in result.stdout
    assert f"{agents} was kept: pi also uses it." in result.stdout
    record = record_of(machine)
    assert [c["harnesses"] for c in record["copies"]] == [["pi"]]
    assert [e["harnesses"] for e in record["instructions"]] == [["pi"]]

    code, body = cli.json("uninstall-skill", "--harness", "pi", "--project", str(project), "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert not agents.exists()


def test_shared_removes_a_copy_still_used_elsewhere_and_names_who_loses_it(
    cli: Cli, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, claude_md = sales_and_hr

    result, screen = cli.typed("uninstall-skill", "sales", "--shared", keys=["y"])

    assert result.exit_code == ExitCode.OK, result.output
    assert f"--shared: environment hr loses skill copy {copy} too." in screen
    assert f"--shared: environment hr loses the skill instructions section in {claude_md} too." in screen
    assert not copy.exists()
    assert not claude_md.exists()


def test_shared_names_the_harnesses_that_lose_it_in_json(cli: Cli, codex_and_pi: Path) -> None:
    project = codex_and_pi
    copy = project / ".agents" / "skills" / SKILL

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--shared", "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert not (project / "AGENTS.md").exists()
    assert {"path": str(copy), "harnesses": ["codex"], "outcome": "deleted"} in body["result"]["copies"]
    pi = [{"environment": "default", "harnesses": ["pi"], "project": str(project)}]
    assert body["result"]["removed_shared"] == [
        {"path": str(copy), "claimants": pi},
        {"path": str(project / "AGENTS.md"), "claimants": pi},
    ]
    assert body["result"]["kept"] == []


def test_force_does_not_remove_a_shared_copy_even_while_it_forces_an_edited_section(
    cli: Cli, tmp_path: Path, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, claude_md = sales_and_hr
    project = sales_project(tmp_path)
    install(cli, "sales", "--harness", "codex", "--project", str(project))
    hand_edit(project / "AGENTS.md")

    code, body = cli.json("uninstall-skill", "sales", "--force", "--yes")

    assert code == ExitCode.OK, body
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".agents" / "skills" / SKILL).exists()
    assert (copy / "SKILL.md").is_file()
    assert holds_section(claude_md)
    assert [row["path"] for row in body["result"]["kept"]] == [str(copy), str(claude_md)]


def test_the_plan_names_the_shared_retry_before_it_asks(
    cli: Cli, tmp_path: Path, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, _ = sales_and_hr
    install(cli, "sales", "--harness", "codex", "--project", str(sales_project(tmp_path)))

    result, screen = cli.typed("uninstall-skill", "sales", keys=["n"])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert f"Skill copy {copy} is kept: environment hr also uses it." in screen
    assert "To remove it anyway, run:" in screen
    assert "connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared" in screen


def other_spelling(project: Path, how: str) -> Path:
    """Return a second spelling of an existing project directory: its parent in other letter case, or a symlink."""
    if how == "letter case":
        spelling = project.parent.with_name(project.parent.name.swapcase()) / project.name
        if not spelling.is_dir():
            pytest.skip("this file system tells letter case apart")
        if not spelling.samefile(project):
            pytest.skip("this file system gives each spelling its own inode (sshfs)")
        return spelling
    spelling = project.parent.with_name("linked")
    try:
        spelling.symlink_to(project.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"this machine cannot make a symlink: {error}")
    return spelling / project.name


@pytest.mark.parametrize("how", ["letter case", "symlink"])
def test_a_copy_two_environments_install_through_two_spellings_of_one_project_is_kept(
    cli: Cli, cdd: Path, tmp_path: Path, how: str
) -> None:
    project = tmp_path / "work" / "p"
    project.mkdir(parents=True)
    for name, spelling in (("sales", project), ("hr", other_spelling(project, how))):
        configure(cli, cdd, name=name)
        install(cli, name, "--harness", "codex", "--project", str(spelling))
    copy = project / ".agents" / "skills" / SKILL

    result = cli("uninstall-skill", "sales", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").is_file()
    assert holds_section(project / "AGENTS.md")
    assert f"Skill copy {copy} was kept: environment hr also uses it." in result.stdout


@pytest.fixture
def spelling_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path.resolve keep a path's spelling, as macOS keeps the letter case of a case-insensitive name."""
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self)))


@pytest.mark.usefixtures("spelling_kept")
def test_two_spellings_resolve_keeps_apart_are_one_copy_for_claims(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    project = tmp_path / "work" / "p"
    project.mkdir(parents=True)
    for name, spelling in (("sales", project), ("hr", other_spelling(project, "symlink"))):
        configure(cli, cdd, name=name)
        install(cli, name, "--harness", "codex", "--project", str(spelling))
    copy = project / ".agents" / "skills" / SKILL

    result = cli("uninstall-skill", "sales", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").is_file()
    assert f"Skill copy {copy} was kept: environment hr also uses it." in result.stdout
    assert record_named(machine, "sales")["copies"] == []
    assert [c["harnesses"] for c in record_named(machine, "hr")["copies"]] == [["codex"]]


@pytest.mark.usefixtures("spelling_kept")
def test_a_project_named_in_another_spelling_drops_the_records_claims(
    cli: Cli, cdd: Path, machine: Machine, tmp_path: Path
) -> None:
    project = tmp_path / "work" / "p"
    project.mkdir(parents=True)
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--project", str(project))
    spelling = other_spelling(project, "symlink")

    result = cli("uninstall-skill", "--harness", "codex", "--project", str(spelling), "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert not (project / ".agents" / "skills" / SKILL).exists()
    record = record_of(machine)
    assert (record["copies"], record["instructions"]) == ([], [])


def test_a_copy_another_harness_and_another_environment_use_is_kept_naming_both(
    cli: Cli, cdd: Path, codex_and_pi: Path
) -> None:
    project = codex_and_pi
    configure(cli, cdd, name="hr")
    install(cli, "hr", "--harness", "codex", "--project", str(project))
    copy, agents = project / ".agents" / "skills" / SKILL, project / "AGENTS.md"

    result = cli("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"Skill copy {copy} was kept: pi and environment hr also use it." in result.stdout
    assert f"{agents} was kept: pi and environment hr also use it." in result.stdout


def test_a_kept_copy_names_the_scope_where_the_same_harness_still_uses_it(
    cli: Cli, cdd: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user", "--no-instructions")
    install(cli, "--harness", "codex", "--project", str(machine.home), "--no-instructions")
    copy = machine.home / ".agents" / "skills" / SKILL

    result = cli("uninstall-skill", "--harness", "codex", "--user", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").is_file()
    assert f"Skill copy {copy} was kept: codex in project {machine.home} also uses it." in result.stdout


@pytest.mark.parametrize("force", [[], ["--force"]])
def test_the_shared_retry_adds_force_when_a_kept_section_was_edited(
    cli: Cli, sales_and_hr: tuple[Path, Path], force: list[str]
) -> None:
    _, claude_md = sales_and_hr
    hand_edit(claude_md)

    code, body = cli.json("uninstall-skill", "sales", *force, "--yes")

    assert code == ExitCode.OK, body
    retry = "connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared --force --yes"
    assert [row["retry"] for row in body["result"]["kept"]] == [retry, retry]


def test_the_shared_retry_removes_the_copy_the_run_kept(cli: Cli, sales_and_hr: tuple[Path, Path]) -> None:
    copy, claude_md = sales_and_hr
    code, body = cli.json("uninstall-skill", "sales", "--no-input")
    assert code == ExitCode.OK, body
    [retry] = {row["retry"] for row in body["result"]["kept"]}
    assert retry == "connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared"

    code, body = cli.json(*retry.split()[1:], "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert not claude_md.exists()


def test_each_kept_scope_gets_its_own_shared_retry_that_removes_it(
    cli: Cli, tmp_path: Path, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, claude_md = sales_and_hr
    project = sales_project(tmp_path)
    for name in ("sales", "hr"):
        install(cli, name, "--harness", "codex", "--project", str(project))
    project_copy, agents = project / ".agents" / "skills" / SKILL, project / "AGENTS.md"

    result = cli("uninstall-skill", "sales", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    user_retry = "connx-mcp-ce uninstall-skill sales --harness claude-code --user --shared --yes"
    project_args = ["sales", "--harness", "codex", "--project", str(project), "--shared", "--yes"]
    project_retry = skill.UninstallRequest(["codex"], project, False, True, None).command(
        "sales", ["codex"], yes=True, shared=True
    )
    assert f"  {user_retry}" in result.stdout
    assert f"  {project_retry}" in result.stdout
    code, body = cli.json("uninstall-skill", "sales", "--no-input")
    assert code == ExitCode.OK, body
    assert body["result"]["kept"] == []

    code, body = cli.json("uninstall-skill", *project_args)

    assert code == ExitCode.OK, body
    assert not project_copy.exists()
    assert not agents.exists()
    assert (copy / "SKILL.md").is_file()
    assert holds_section(claude_md)


def test_a_removed_environments_claims_no_longer_protect_a_copy(cli: Cli, sales_and_hr: tuple[Path, Path]) -> None:
    copy, claude_md = sales_and_hr
    project_volumes(cli, names="", name="hr")
    project_containers(cli, name="hr")
    assert cli.json("remove", "hr", "--yes")[0] == ExitCode.OK

    code, body = cli.json("uninstall-skill", "sales", "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    assert not claude_md.exists()


def test_another_environments_unreadable_record_claims_nothing_and_the_result_says_so(
    cli: Cli, machine: Machine, sales_and_hr: tuple[Path, Path]
) -> None:
    copy, _ = sales_and_hr
    (machine.base / "hr" / skill.INSTALL_RECORD).write_bytes(b"{not json")

    code, body = cli.json("uninstall-skill", "sales", "--yes")

    assert code == ExitCode.OK, body
    assert not copy.exists()
    [warning] = body["result"]["warnings"]
    assert str(machine.base / "hr" / skill.INSTALL_RECORD) in warning
    assert "Environment hr may still use" in warning
    assert "Environment hr may still use" in cli("uninstall-skill", "sales", "--no-input").stdout


def test_an_older_record_entry_without_harnesses_counts_for_any_harness(cli: Cli, cdd: Path, machine: Machine) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user", "--no-instructions")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    (entry,) = record["copies"]
    moved = machine.home / "old-skills" / SKILL
    moved.parent.mkdir()
    Path(entry["path"]).rename(moved)
    record["copies"] = [{"path": str(moved), "project": None}]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    code, body = cli.json("uninstall-skill", "--harness", "codex", "--user", "--yes")

    assert code == ExitCode.OK, body
    assert not moved.exists()


def test_a_kept_copy_this_environment_claims_at_another_scope_does_not_name_it_as_another_environment(
    cli: Cli, cdd: Path, machine: Machine
) -> None:
    configure(cli, cdd)
    install(cli, "--harness", "codex", "--user", "--no-instructions")
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    (entry,) = record["copies"]
    moved = machine.home / "old-skills" / SKILL
    moved.parent.mkdir()
    Path(entry["path"]).rename(moved)
    record["copies"] = [{"path": str(moved), "project": None}, {"path": str(moved), "project": str(machine.home)}]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    result = cli("uninstall-skill", "--harness", "codex", "--user", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (moved / "SKILL.md").is_file()
    assert f"Skill copy {moved} was kept: this environment in project {machine.home} also uses it." in result.stdout


def test_an_older_record_copy_without_harnesses_stays_for_the_other_harness_using_it(
    cli: Cli, machine: Machine, codex_and_pi: Path
) -> None:
    project = codex_and_pi
    copy = project / ".agents" / "skills" / SKILL
    record_file = machine.base / "default" / skill.INSTALL_RECORD
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record["copies"] = [{k: v for k, v in c.items() if k != "harnesses"} for c in record["copies"]]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    result = cli("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert (copy / "SKILL.md").is_file()
    assert f"Skill copy {copy} was kept: pi, hermes and agents also use it.\n" in result.stdout
    [left] = [c["harnesses"] for c in record_of(machine)["copies"]]
    assert "pi" in left
    assert "codex" not in left
