#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_docker_prerequisites.py - Daemon-bound commands check Docker first and explain what is missing.

Description:
    A missing Docker, a stopped daemon, Compose older than v2 and a Podman-only host each
    fail before any other daemon call, with install guidance and their own exit code.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from connx_mcp.docker import SubprocessDocker, check_docker
from tests.conftest import Cli
from tests.fakes import FakeDocker
from tests.test_discover import DAEMON_COMMANDS

INSTALL_DOCS = "https://docs.docker.com/engine/install/"


@pytest.mark.parametrize(
    ("recording", "exit_code", "code", "guidance"),
    [
        ("docker-missing", 10, "docker_missing", INSTALL_DOCS),
        ("daemon-stopped", 11, "docker_not_running", "Start Docker"),
        ("daemon-permission-denied", 11, "docker_not_running", "docker group"),
        ("compose-missing", 12, "compose_missing", "https://docs.docker.com/compose/install/"),
        ("compose-v1", 12, "compose_missing", "Compose v2"),
        ("engine-too-old", 14, "docker_too_old", "Docker Engine 24"),
        ("podman-only", 13, "docker_unsupported", "supports Docker only"),
        ("podman-docker-shim", 13, "docker_unsupported", "supports Docker only"),
    ],
)
def test_docker_problem_fails_with_guidance_and_its_own_exit_code(
    cli: Cli, recording: str, exit_code: int, code: str, guidance: str
) -> None:
    cli.docker = FakeDocker.from_recording(recording)

    status, envelope = cli.json("status")

    assert status == exit_code
    assert envelope["error"]["code"] == code
    assert guidance in envelope["error"]["message"]


@pytest.mark.parametrize(
    ("recording", "guidance"),
    [
        ("docker-missing", "Rancher Desktop with the dockerd (moby) container engine"),
        ("engine-too-old", "Rancher Desktop with the dockerd (moby) container engine"),
        ("daemon-stopped", "Start Docker (Docker Desktop or Rancher Desktop"),
        ("compose-missing", "Rancher Desktop (dockerd (moby) engine)"),
    ],
)
def test_docker_guidance_offers_rancher_desktop_with_its_dockerd_engine(
    cli: Cli, recording: str, guidance: str
) -> None:
    """Rancher Desktop is offered beside Docker Desktop, with the dockerd engine the docker CLI needs."""
    cli.docker = FakeDocker.from_recording(recording)

    _, envelope = cli.json("status")

    assert guidance in envelope["error"]["message"]


def test_command_help_names_rancher_desktop_as_a_docker_option(cli: Cli) -> None:
    """A daemon-bound command's prerequisites name Rancher Desktop and its dockerd engine."""
    result = cli("start", "--help")

    assert "Rancher Desktop with the dockerd (moby) engine" in " ".join(result.stdout.split())


def test_stopped_daemon_shows_dockers_own_message(cli: Cli) -> None:
    cli.docker = FakeDocker.from_recording("daemon-stopped")

    result = cli("start")

    assert result.exit_code == 11
    assert "Is the docker daemon running?" in result.stderr
    assert "Next: docker info" in result.stderr


def test_a_daemon_that_does_not_answer_ends_as_not_running(cli: Cli) -> None:
    cli.docker.hang_when(lambda args: args[:1] == ["version"])

    status, envelope = cli.json("status")

    assert status == 11
    assert envelope["error"]["code"] == "docker_not_running"
    assert "did not answer" in envelope["error"]["message"]
    assert envelope["error"]["next"] == "docker info"


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script stands in for docker")
def test_the_real_port_gives_up_on_a_docker_that_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexec sleep 30\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessDocker().run(["version"], timeout=0.2)


@pytest.mark.parametrize("command", DAEMON_COMMANDS)
def test_every_daemon_command_checks_docker_first(cli: Cli, command: str) -> None:
    cli.docker = FakeDocker.from_recording("docker-missing")

    status, envelope = cli.json(command)

    assert status == 10
    assert envelope["error"]["code"] == "docker_missing"
    assert cli.docker.calls == []


def test_ready_docker_lets_the_command_proceed(cli: Cli) -> None:
    status, envelope = cli.json("status", "default")

    assert envelope["error"]["code"] == "environment_not_found"
    assert cli.docker.calls == [["version", "--format", "{{json .}}"], ["compose", "version", "--short"]]


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("configure", "input_required"),
        ("export", "environment_not_found"),
        ("configure-demo", "input_required"),
    ],
)
def test_daemon_free_commands_never_touch_docker(cli: Cli, command: str, code: str) -> None:
    cli.docker = FakeDocker.from_recording("docker-missing")

    status, envelope = cli.json(command, "--no-input")

    assert envelope["error"]["code"] == code, envelope
    assert cli.docker.calls == []


@pytest.mark.live
def test_real_docker_meets_the_prerequisites() -> None:
    info = check_docker(SubprocessDocker())

    assert info.engine_major >= 24
    assert int(info.compose_version.split(".")[0]) >= 2
