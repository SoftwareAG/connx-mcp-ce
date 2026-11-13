#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_remove.py - The administrator removes an environment.

Description:
    Through the CLI seam with the fake Docker port: `remove <env>` takes the server
    down with compose and deletes the environment's directory after confirmation (`--yes` skips
    it; `--no-input` or `--json` without `--yes` fails), keeps the certificate and OAuth state
    volumes unless `--purge`, and never removes an image. The live test removes a started
    environment against the real image from the host.

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
import shlex
import shutil
from pathlib import Path

import pytest

from connx_mcp import remove, skill
from connx_mcp.output import CliError, ExitCode
from connx_mcp.ports import default_ports
from tests.conftest import Cli, Machine, as_typed
from tests.test_install_skill import configure as configure_pinned
from tests.test_install_skill import sales_project, skill_in_image

SKILL = "connx-sql-expert"

PASSWORD = "pw-for-tests-only"
VOLUMES = "connx-mcp-ce-default-certs\nconnx-mcp-ce-default-oauth-state\n"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def configure(cli: Cli, cdd: Path, name: str = "default") -> None:
    """Create a complete environment."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}",
        "--cdd", str(cdd),
    )  # fmt: skip
    assert code == 0, body


def started(machine: Machine, name: str = "default") -> Path:
    """Leave the compose file a start writes."""
    compose_file = machine.base / name / "compose.yml"
    compose_file.write_text("name: connx-mcp-ce-default\n", encoding="utf-8")
    return compose_file


def down_args(machine: Machine, name: str = "default") -> list[str]:
    compose_file = machine.base / name / "compose.yml"
    return [
        "compose",
        "-p",
        f"connx-mcp-ce-{name}",
        "-f",
        str(compose_file),
        "down",
        "--timeout",
        "10",
        "--remove-orphans",
    ]


def volume_ls_args(name: str = "default") -> list[str]:
    return ["volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project=connx-mcp-ce-{name}"]


def project_volumes(cli: Cli, names: str = VOLUMES, name: str = "default") -> None:
    cli.docker.reply(volume_ls_args(name), stdout=names)


def ps_args(name: str = "default") -> list[str]:
    """The `docker ps` call that finds an environment's containers by compose label."""
    return ["ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project=connx-mcp-ce-{name}"]


def project_containers(cli: Cli, ids: str = "", name: str = "default") -> None:
    """Answer the label lookup made when the environment has no compose.yml."""
    cli.docker.reply(ps_args(name), stdout=ids)


def removed_images(cli: Cli) -> list[list[str]]:
    return [c for c in cli.docker.calls if "rmi" in c or c[:2] in (["image", "rm"], ["image", "prune"])]


# --- remove --yes ----------------------------------------------------------------------------------------


def test_remove_yes_takes_the_server_down_deletes_the_directory_and_keeps_volumes(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd)
    started(machine)
    cli.docker.reply(down_args(machine))
    project_volumes(cli)

    code, body = cli.json("remove", "--yes")

    assert code == 0, body
    assert not (machine.base / "default").exists()
    assert down_args(machine) in cli.docker.calls
    assert not [c for c in cli.docker.calls if "--volumes" in c or c[:2] == ["volume", "rm"]]
    assert removed_images(cli) == []
    result = body["result"]
    assert result["environment"] == "default"
    assert result["removed"] is True
    assert result["volumes_kept"] == ["connx-mcp-ce-default-certs", "connx-mcp-ce-default-oauth-state"]
    assert result["volumes_removed"] == []


def test_remove_says_how_to_delete_the_kept_volumes(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    cli.docker.reply(down_args(machine))
    project_volumes(cli)

    result = cli("remove", "--yes")

    assert result.exit_code == 0, result.output
    assert "Removed environment 'default'" in result.stdout
    assert "docker volume rm connx-mcp-ce-default-certs connx-mcp-ce-default-oauth-state" in result.stdout


def test_remove_purge_also_removes_the_volumes_but_never_an_image(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    cli.docker.reply(down_args(machine))
    project_volumes(cli)
    rm = ["volume", "rm", "connx-mcp-ce-default-certs", "connx-mcp-ce-default-oauth-state"]
    cli.docker.reply(rm)

    code, body = cli.json("remove", "--yes", "--purge")

    assert code == 0, body
    assert rm in cli.docker.calls
    assert cli.docker.calls.index(down_args(machine)) < cli.docker.calls.index(rm)
    assert removed_images(cli) == []
    assert body["result"]["volumes_removed"] == ["connx-mcp-ce-default-certs", "connx-mcp-ce-default-oauth-state"]
    assert body["result"]["volumes_kept"] == []
    assert not (machine.base / "default").exists()


def test_remove_of_a_never_started_environment_needs_no_compose_down(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "--yes")

    assert code == 0, body
    assert not [c for c in cli.docker.calls if c[:1] == ["compose"] and "down" in c]
    assert not (machine.base / "default").exists()


def test_remove_touches_only_the_named_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "sales")
    project_volumes(cli, names="", name="sales")
    project_containers(cli, name="sales")

    code, body = cli.json("remove", "sales", "--yes")

    assert code == 0, body
    assert not (machine.base / "sales").exists()
    assert (machine.base / "default" / "server.env").is_file()


# --- confirmation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [["--no-input"], ["--json"]], ids=["no-input", "json"])
def test_remove_without_yes_cannot_confirm_and_changes_nothing(
    cli: Cli, machine: Machine, cdd: Path, flags: list[str]
) -> None:
    configure(cli, cdd)
    started(machine)

    result = cli("remove", *flags)

    assert result.exit_code == 3, result.output
    assert "--yes" in result.output
    assert (machine.base / "default" / "server.env").is_file()
    assert not [c for c in cli.docker.calls if "down" in c or c[:1] == ["volume"]]


def test_remove_asks_on_a_terminal_and_removes_on_yes(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    cli.docker.reply(down_args(machine))
    project_volumes(cli)

    result, screen = cli.typed("remove", keys=["y"])

    assert result.exit_code == 0, result.output
    assert "Remove environment 'default'" in screen
    assert "connx-mcp-ce-default-certs" in screen
    assert not (machine.base / "default").exists()


def test_remove_declined_on_the_terminal_changes_nothing(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    project_volumes(cli)

    result, _ = cli.typed("remove", keys=["n"])

    assert result.exit_code == 100, result.output
    assert "not removed" in result.stderr
    assert cli.json("remove", "--no-input")[0] != 100  # without a terminal it is not a cancellation
    assert (machine.base / "default" / "server.env").is_file()
    assert not [c for c in cli.docker.calls if "down" in c or c[:2] == ["volume", "rm"]]


def test_the_purge_question_names_the_volumes_it_deletes(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    project_volumes(cli)

    result, screen = cli.typed("remove", "--purge", keys=["n"])

    assert result.exit_code == 100, result.output
    assert "deleted" in screen
    assert "connx-mcp-ce-default-oauth-state" in screen


# --- failures --------------------------------------------------------------------------------------------


def test_remove_of_an_unknown_environment_fails(cli: Cli) -> None:
    code, body = cli.json("remove", "nope", "--yes")

    assert code == 20
    assert body["error"]["code"] == "environment_not_found"


def test_a_failed_compose_down_keeps_the_directory(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    project_volumes(cli)
    cli.docker.reply(down_args(machine), returncode=1, stderr="Error response from daemon: conflict")

    code, body = cli.json("remove", "--yes")

    assert code == 31
    assert "conflict" in body["error"]["message"]
    assert (machine.base / "default" / "server.env").is_file()


def test_a_failed_volume_purge_keeps_the_directory_for_a_retry(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    started(machine)
    cli.docker.reply(down_args(machine))
    project_volumes(cli)
    rm = ["volume", "rm", "connx-mcp-ce-default-certs", "connx-mcp-ce-default-oauth-state"]
    cli.docker.reply(rm, returncode=1, stderr="volume is in use")

    code, body = cli.json("remove", "--yes", "--purge")

    assert code == 31
    assert "volume is in use" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce remove default --yes --purge"
    assert (machine.base / "default" / "server.env").is_file()


def test_a_pre_remove_check_refuses_before_anything_changes(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook the refusal to remove a demo-bound environment plugs into."""

    def refuse(run: remove.RemoveRun) -> None:
        raise CliError(ExitCode.USAGE, f"{run.env.name} is held by a test check")

    monkeypatch.setattr(remove, "PRE_REMOVE_CHECKS", [refuse])
    configure(cli, cdd)
    started(machine)

    code, body = cli.json("remove", "--yes")

    assert code == 2
    assert "held by a test check" in body["error"]["message"]
    assert (machine.base / "default" / "server.env").is_file()
    assert not [c for c in cli.docker.calls if "down" in c or c[:1] == ["volume"]]


def test_remove_help_names_yes_and_purge(cli: Cli) -> None:
    result = cli("remove", "--help")

    assert result.exit_code == 0
    assert "--yes" in result.stdout
    assert "--purge" in result.stdout


# --- the skill copies remove leaves behind ---------------------------------------------------------------


def skill_installed(cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the skill for Claude Code for the user and for Codex in a project; return the project."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    configure_pinned(cli, cdd)
    project = sales_project(tmp_path)
    skill_in_image(cli)
    assert cli.json("install-skill", "--harness", "claude-code", "--user")[0] == ExitCode.OK
    skill_in_image(cli)
    assert cli.json("install-skill", "--harness", "codex", "--project", str(project))[0] == ExitCode.OK
    project_volumes(cli, names="")
    project_containers(cli)
    return project


def test_remove_lists_the_skill_copies_and_sections_it_keeps_with_the_commands_that_remove_them(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    assert not (machine.base / "default").exists()
    user_copy = machine.home / ".claude" / "skills" / SKILL
    project_copy = project / ".agents" / "skills" / SKILL
    assert user_copy.is_dir() and project_copy.is_dir()
    assert body["result"]["skill_kept"] == {
        "copies": [
            {"path": str(user_copy), "harnesses": ["claude-code"], "project": None},
            {"path": str(project_copy), "harnesses": ["codex"], "project": str(project.resolve())},
        ],
        "instructions": [
            {"path": str(machine.home / ".claude" / "CLAUDE.md"), "harnesses": ["claude-code"], "project": None},
            {"path": str(project / "AGENTS.md"), "harnesses": ["codex"], "project": str(project.resolve())},
        ],
        "zips": [],
        "shared": [],
        "commands": [
            "connx-mcp-ce uninstall-skill default --harness claude-code --user",
            f"connx-mcp-ce uninstall-skill default --harness codex --project {as_typed(project.resolve())}",
        ],
    }


def test_the_listed_commands_remove_the_kept_copies_after_the_environment_is_gone(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)
    code, body = cli.json("remove", "--yes")
    assert code == ExitCode.OK, body

    for command in body["result"]["skill_kept"]["commands"]:
        args = shlex.split(command, posix=os.name != "nt")[1:]
        code, ran = cli.json(*[a.strip('"') for a in args], "--yes")
        assert code == ExitCode.OK, ran

    assert not (machine.home / ".claude" / "skills" / SKILL).exists()
    assert not (machine.home / ".claude" / "CLAUDE.md").exists()
    assert not (project / ".agents" / "skills" / SKILL).exists()
    assert not (project / "AGENTS.md").exists()


def test_human_output_lists_the_kept_copies_and_the_commands(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)

    result = cli("remove", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"Skill copy {project / '.agents' / 'skills' / SKILL} (codex)" in result.stdout
    assert f"Skill instructions section in {machine.home / '.claude' / 'CLAUDE.md'} (claude-code)" in result.stdout
    assert "connx-mcp-ce uninstall-skill default --harness claude-code --user" in result.stdout


def shared_with_hr(cli: Cli, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install as skill_installed does, and Claude Code for the user from environment hr too; return the project."""
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)
    configure_pinned(cli, cdd, name="hr")
    skill_in_image(cli)
    assert cli.json("install-skill", "hr", "--harness", "claude-code", "--user")[0] == ExitCode.OK
    return project


def test_a_copy_another_environment_uses_is_listed_as_kept_for_it_and_not_under_a_command(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = shared_with_hr(cli, cdd, tmp_path, monkeypatch)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    kept = body["result"]["skill_kept"]
    hr = [{"environment": "hr", "harnesses": ["claude-code"], "project": None}]
    assert kept["shared"] == [
        {"path": str(machine.home / ".claude" / "skills" / SKILL), "harnesses": ["claude-code"], "project": None}
        | {"claimants": hr},
        {"path": str(machine.home / ".claude" / "CLAUDE.md"), "harnesses": ["claude-code"], "project": None}
        | {"claimants": hr},
    ]
    assert [c["path"] for c in kept["copies"]] == [str(project / ".agents" / "skills" / SKILL)]
    assert [e["path"] for e in kept["instructions"]] == [str(project / "AGENTS.md")]
    assert kept["commands"] == [
        f"connx-mcp-ce uninstall-skill default --harness codex --project {as_typed(project.resolve())}"
    ]


def test_human_output_says_which_environment_also_uses_a_kept_copy(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_with_hr(cli, cdd, tmp_path, monkeypatch)

    result = cli("remove", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    copy = machine.home / ".claude" / "skills" / SKILL
    assert f"Skill copy {copy} (claude-code) was kept: environment hr also uses it\n" in result.stdout
    assert "--harness claude-code --user" not in result.stdout


def test_an_older_shared_entry_says_who_keeps_it_without_a_command(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_with_hr(cli, cdd, tmp_path, monkeypatch)
    moved = machine.home / "old-skills" / SKILL
    moved.parent.mkdir()
    (machine.home / ".claude" / "skills" / SKILL).rename(moved)
    for name in ("default", "hr"):
        record_file = machine.base / name / skill.INSTALL_RECORD
        record = json.loads(record_file.read_text(encoding="utf-8"))
        record["copies"] = [{"path": str(moved), "project": None}]
        record_file.write_text(json.dumps(record), encoding="utf-8")

    result = cli("remove", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"  Skill copy {moved} was kept: environment hr also uses it\n" in result.stdout


def test_the_confirmation_says_the_skill_copies_are_kept(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)

    result, screen = cli.typed("remove", keys=["n"])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert "The skill copies and skill instructions install-skill wrote for environment default are kept:" in screen
    assert str(project / ".agents" / "skills" / SKILL) in screen


def test_copies_no_longer_there_are_not_listed_and_with_none_left_remove_says_nothing_of_the_skill(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = skill_installed(cli, cdd, tmp_path, monkeypatch)
    code, body = cli.json("uninstall-skill", "--harness", "codex", "--project", str(project), "--yes")
    assert code == ExitCode.OK, body

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    assert [c["harnesses"] for c in body["result"]["skill_kept"]["copies"]] == [["claude-code"]]
    assert body["result"]["skill_kept"]["commands"] == [
        "connx-mcp-ce uninstall-skill default --harness claude-code --user"
    ]

    configure_pinned(cli, cdd, name="sales")
    skill_in_image(cli)
    assert cli.json("install-skill", "sales", "--harness", "codex", "--project", str(project))[0] == ExitCode.OK
    assert cli.json("uninstall-skill", "sales", "--harness", "codex", "--project", str(project), "--yes")[0] == 0
    project_volumes(cli, names="", name="sales")
    project_containers(cli, name="sales")

    result = cli("remove", "sales", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert "skill" not in result.stdout.lower()


def test_a_kept_zip_is_listed_with_the_claude_desktop_command(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure_pinned(cli, cdd)
    handover = tmp_path / "handover"
    handover.mkdir()
    skill_in_image(cli)
    code, body = cli.json("install-skill", "--harness", "claude-desktop", "--zip", str(handover))
    assert code == ExitCode.OK, body
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    kept = body["result"]["skill_kept"]
    assert [z["path"] for z in kept["zips"]] == [str(handover / f"{SKILL}.zip")]
    assert kept["commands"] == ["connx-mcp-ce uninstall-skill default --harness claude-desktop --user"]


def test_an_unreadable_install_record_is_a_note_and_remove_still_succeeds(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure_pinned(cli, cdd)
    (machine.base / "default" / "skill-install.json").write_bytes(b"{not json")
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    assert not (machine.base / "default").exists()
    [note] = body["result"]["notes"]
    assert "skill-install.json cannot be read" in note
    assert "connx-mcp-ce uninstall-skill default --harness <harness> --user" in note
    assert body["result"]["skill_kept"]["commands"] == []


def test_the_confirmation_gives_the_note_for_an_unreadable_install_record(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure_pinned(cli, cdd)
    (machine.base / "default" / "skill-install.json").write_bytes(b"{not json")
    project_volumes(cli, names="")
    project_containers(cli)

    result, screen = cli.typed("remove", keys=["n"])

    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert "skill-install.json cannot be read" in screen
    assert "connx-mcp-ce uninstall-skill default --harness <harness> --user" in screen


def recorded_without_harnesses(machine: Machine, copy: Path | None = None) -> None:
    """Rewrite the install record as if it named no harnesses; `copy` moves the user copy there first."""
    record_file = machine.base / "default" / "skill-install.json"
    record = json.loads(record_file.read_text(encoding="utf-8"))
    if copy is not None:
        copy.parent.mkdir(parents=True)
        shutil.move(record["copies"][0]["path"], copy)
        record = {"copies": [{"path": str(copy)}], "instructions": []}
    for entry in [*record["copies"], *record.get("instructions", [])]:
        entry.pop("harnesses", None)
    record_file.write_text(json.dumps(record), encoding="utf-8")


def test_an_entry_recorded_without_harnesses_is_removed_by_the_harnesses_that_use_its_path(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure_pinned(cli, cdd)
    skill_in_image(cli)
    assert cli.json("install-skill", "--harness", "claude-code", "--user")[0] == ExitCode.OK
    recorded_without_harnesses(machine)
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    assert body["result"]["skill_kept"]["commands"] == [
        "connx-mcp-ce uninstall-skill default --harness claude-code --user"
    ]


def test_a_copy_no_harness_reads_is_listed_with_the_generic_command_and_no_listed_command(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure_pinned(cli, cdd)
    skill_in_image(cli)
    assert cli.json("install-skill", "--harness", "claude-code", "--user")[0] == ExitCode.OK
    elsewhere = tmp_path / "elsewhere" / SKILL
    recorded_without_harnesses(machine, elsewhere)
    project_volumes(cli, names="")
    project_containers(cli)

    result = cli("remove", "--yes")

    assert result.exit_code == ExitCode.OK, result.output
    assert f"Skill copy {elsewhere}: no harness reads it; remove it with: connx-mcp-ce uninstall-skill" in result.stdout
    assert "Remove them with:" not in result.stdout


@pytest.mark.parametrize(
    "text", ["[]", '"x"', '{"copies": "x"}', '{"zips": {}}', '{"instructions": 1}', '{"copies": [5]}']
)
def test_an_install_record_of_the_wrong_shape_is_a_note_and_remove_still_succeeds(
    cli: Cli, machine: Machine, cdd: Path, text: str
) -> None:
    configure_pinned(cli, cdd)
    (machine.base / "default" / "skill-install.json").write_text(text, encoding="utf-8")
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "--yes")

    assert code == ExitCode.OK, body
    [note] = body["result"]["notes"]
    assert "skill-install.json cannot be read" in note


# --- live: the real image ---------------------------------------------------------------------------------


@pytest.mark.live
def test_remove_a_started_environment_against_the_real_image(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-10", "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}",
    )  # fmt: skip
    assert code == 0, body
    try:
        code, body = cli.json("start", "live-10", "--timeout", "300")
        assert code == 0, body

        code, body = cli.json("remove", "live-10", "--yes")
        assert code == 0, body
        assert sorted(body["result"]["volumes_kept"]) == [
            "connx-mcp-ce-live-10-certs",
            "connx-mcp-ce-live-10-oauth-state",
        ]
        assert real.docker.run(["container", "inspect", "connx-mcp-ce-live-10"]).returncode != 0
        assert not (machine.base / "live-10").exists()
    finally:
        real.docker.run(["rm", "--force", "connx-mcp-ce-live-10"])
        real.docker.run(["volume", "rm", "connx-mcp-ce-live-10-certs", "connx-mcp-ce-live-10-oauth-state"])
