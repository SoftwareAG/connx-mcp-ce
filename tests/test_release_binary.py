#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_release_binary.py - Daemon-free checks of a built release binary, run on Linux and Windows CI.

Description:
    The compiled binary is what administrators run, so these start it as a process: help, `--version`, configure,
    the demo users file seeded from package data, the missing-Docker failure with docker kept off
    PATH, install-skill --zip (its help, its missing-Docker failure before anything is written,
    and, against a docker stand-in, install notes that quote the built-in skill instructions the
    binary ships as package data), and uninstall-skill undoing a project install without docker.
    They run only when CONNX_MCP_BINARY names a built
    binary (`uv run pytest -m binary`); otherwise they are skipped.

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
import subprocess
import sys
from pathlib import Path

import pytest

from connx_mcp import version
from connx_mcp.instructions import BUILT_IN_FILE
from connx_mcp.skill import IMAGE_SKILL_PATH
from tests.conftest import Machine

BINARY = os.environ.get("CONNX_MCP_BINARY")

pytestmark = [
    pytest.mark.binary,
    pytest.mark.skipif(not BINARY, reason="CONNX_MCP_BINARY does not name a built binary"),
]


def run_binary(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [str(Path(BINARY or "").resolve()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )


def test_help_lists_the_commands_under_the_product_name() -> None:
    result = run_binary("--help")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Usage: connx-mcp-ce ")
    for command in ("configure", "start", "status", "install-skill", "guide"):
        assert command in result.stdout


def test_version_shows_the_product_version_the_binary_was_built_with() -> None:
    result = run_binary("--version")

    assert result.returncode == 0, result.stderr
    # The build sets CONNX_MCP_PRODUCT_VERSION to the Product version it stamped; a local run checks the shape only.
    expected = os.environ.get("CONNX_MCP_PRODUCT_VERSION")
    if expected:
        assert result.stdout == f"connx-mcp-ce {version.display(expected)}\n"
    else:
        assert re.fullmatch(r"connx-mcp-ce \d+\.\d+\.\d+( \(build (\d{5}|dev\d+)\))?\n", result.stdout)


def test_a_missing_docker_fails_with_its_own_exit_code(tmp_path: Path) -> None:
    result = run_binary("status", "--json", path=str(tmp_path))

    envelope = json.loads(result.stdout)
    assert result.returncode == 10
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "docker_missing"


def test_configure_writes_an_environment(tmp_path: Path, machine: Machine) -> None:
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")

    result = run_binary(
        "configure", "--json", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=pw-for-tests-only",
        "--cdd", str(cdd),
    )  # fmt: skip

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"]["environment"] == "default"
    assert (machine.base / "default" / "server.env").is_file()


def test_the_demo_users_file_is_seeded_from_the_bundled_package_data(machine: Machine) -> None:
    result = run_binary("configure-demo", "--keycloak", "--json", "--no-input")

    assert result.returncode == 0, result.stdout + result.stderr
    seeded = Path(json.loads(result.stdout)["result"]["users_file"]).read_text(encoding="utf-8")
    assert "alice" in seeded


def test_install_skill_zip_is_offered_and_fails_cleanly_without_docker(tmp_path: Path, machine: Machine) -> None:
    """install-skill offers --zip and claude-desktop, and without Docker fails with docker_missing, writing nothing."""
    helped = run_binary("install-skill", "--help")
    assert helped.returncode == 0, helped.stderr
    assert "--zip" in helped.stdout
    assert "claude-desktop" in helped.stdout

    out = tmp_path / "handover"
    result = run_binary(
        "install-skill", "--zip", str(out), "--harness", "claude-desktop", "--json", "--no-input",
        path=str(tmp_path),
    )  # fmt: skip

    assert result.returncode == 10, result.stdout + result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "docker_missing"
    assert not out.exists()


# A docker stand-in for the binary: a running engine whose image holds only the skill (no INSTRUCTIONS.md).
FAKE_DOCKER = """\
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["version"]:
    print(json.dumps({"Server": {"Version": "27.0.3"}}))
elif args[:2] == ["compose", "version"]:
    print("2.29.1")
elif args[:1] == ["create"]:
    print("fakecontainer")
elif args[:1] == ["cp"] and args[1] == "fakecontainer:IMAGE_SKILL_PATH":
    skill = Path(args[2]) / "connx-sql-expert"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"---\\nname: connx-sql-expert\\n---\\n")
elif args[:2] == ["rm", "-f"]:
    pass
else:
    sys.exit("Error response from daemon: Could not find the file " + " ".join(args))
"""


def fake_docker(directory: Path) -> Path:
    """Write a `docker` that runs FAKE_DOCKER with this test's Python; return its directory, to put on PATH."""
    directory.mkdir()
    script = directory / "fake_docker.py"
    script.write_text(FAKE_DOCKER.replace("IMAGE_SKILL_PATH", IMAGE_SKILL_PATH), encoding="utf-8")
    if os.name == "nt":
        (directory / "docker.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = directory / "docker"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return directory


def test_install_skill_zip_notes_quote_the_built_in_skill_instructions(tmp_path: Path, machine: Machine) -> None:
    """The binary ships the built-in skill instructions: the zip's notes quote them and say where each goes."""
    configure_pinned(tmp_path, machine)
    out = tmp_path / "handover"

    result = run_binary(
        "install-skill", "--zip", str(out), "--harness", "claude-desktop", "--harness", "codex", "--json",
        "--no-input", path=str(fake_docker(tmp_path / "bin")),
    )  # fmt: skip

    assert result.returncode == 0, result.stdout + result.stderr
    # connx-mcp-ce writes the OS line ending; compare with LF.
    notes = (out / "connx-sql-expert-install.txt").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert "Add the skill instructions:" in notes
    assert "project instructions" in notes
    assert "AGENTS.override.md" in notes
    # The whole built-in text, as this checkout's package data holds it, between the markers.
    built_in = BUILT_IN_FILE.read_bytes().decode("utf-8").replace("\r\n", "\n").rstrip("\n")
    assert "\n<!-- connx-mcp-ce begin version=" in notes
    quoted = notes.rsplit("\n<!-- connx-mcp-ce begin version=", 1)[1].split(" -->\n", 1)[1]
    assert quoted == f"{built_in}\n<!-- connx-mcp-ce end -->\n"


def configure_pinned(tmp_path: Path, machine: Machine) -> None:
    """Configure the default environment with the binary, and pin the digest a first start would have pinned."""
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")
    configured = run_binary(
        "configure", "--json", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=pw-for-tests-only",
        "--cdd", str(cdd),
    )  # fmt: skip
    assert configured.returncode == 0, configured.stdout + configured.stderr
    host_file = machine.base / "default" / "host.json"
    host = json.loads(host_file.read_text(encoding="utf-8"))
    host.update(image_digest="docker.io/softwareag/connx-mcp-server@sha256:" + "a" * 64, image_version="15.0.3")
    host_file.write_text(json.dumps(host), encoding="utf-8")


def test_uninstall_skill_removes_what_install_skill_wrote_without_docker(tmp_path: Path, machine: Machine) -> None:
    """After an install into a project, uninstall-skill deletes the skill copy and the section, with no docker on PATH."""
    configure_pinned(tmp_path, machine)
    project = tmp_path / "sales"
    project.mkdir()
    (project / "AGENTS.md").write_bytes(b"# Ours\n")
    installed = run_binary(
        "install-skill", "--harness", "codex", "--project", str(project), "--json", "--no-input",
        path=str(fake_docker(tmp_path / "bin")),
    )  # fmt: skip
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (project / ".agents" / "skills" / "connx-sql-expert" / "SKILL.md").is_file()

    result = run_binary(
        "uninstall-skill", "--harness", "codex", "--project", str(project), "--yes", "--json", path=str(tmp_path)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"]["removed"] is True
    assert not (project / ".agents" / "skills" / "connx-sql-expert").exists()
    assert (project / "AGENTS.md").read_bytes() == b"# Ours\n"
