#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docker.py - The Docker port, its subprocess adapter, and the prerequisite check.

Description:
    Every daemon interaction goes through the DockerPort: `which` finds an executable and `run`
    runs `docker <args>` and returns exit code, stdout and stderr; `stream` lets docker write to
    the terminal itself (a followed log). Tests replace it with a fake
    that replays recorded outputs; SubprocessDocker is the real adapter. check_docker turns the
    port's raw answers into either the facts later commands need or a CliError that tells the
    administrator what to install or start.

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
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol

from connx_mcp import CLI_NAME
from connx_mcp.output import CliError, ExitCode

MIN_ENGINE_MAJOR = 24
INSTALL_GUIDANCE = (
    "Install Docker Engine 24 or later with the Compose v2 plugin (https://docs.docker.com/engine/install/), "
    "Docker Desktop (https://docs.docker.com/desktop/), or Rancher Desktop with the dockerd (moby) container engine "
    "(https://docs.rancherdesktop.io/)."
)
VERSION_ARGS = ("version", "--format", "{{json .}}")
COMPOSE_VERSION_ARGS = ("compose", "version", "--short")
# Container states in which the container exists with its process: a paused one is up but does not answer.
CONTAINER_UP = frozenset({"running", "restarting", "paused"})
# How long a query a working daemon answers at once (version, inspect, ps, a log tail) may take. A daemon still
# starting or hung would otherwise block status, guide and start for ever. Pulls, compose up and streams are
# legitimately long and get no timeout.
QUERY_SECONDS = 30.0


@dataclass(frozen=True)
class DockerResult:
    """What one `docker` invocation returned."""

    returncode: int
    stdout: str
    stderr: str


class DockerPort(Protocol):
    """Every interaction with the Docker CLI and daemon."""

    def which(self, name: str) -> str | None:
        """Return the path of an executable on PATH, or None."""
        ...

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> DockerResult:
        """Run `docker <args>` and return its result; `env` adds variables to its environment only.

        A `timeout` in seconds ends a command that has not returned with subprocess.TimeoutExpired.
        """
        ...

    def stream(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run `docker <args>` writing straight to this process's stdout and stderr; return its exit code."""
        ...


class SubprocessDocker:
    """The real Docker port: runs the `docker` executable found on PATH."""

    def which(self, name: str) -> str | None:
        """Return the path of an executable on PATH, or None."""
        return shutil.which(name)

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> DockerResult:
        """Run `docker <args>`, capturing stdout and stderr for parsing.

        Args:
            args (Sequence[str]): Arguments after `docker`.
            stdin (str | None): Text fed to the command's stdin.
            env (Mapping[str, str] | None): Variables added to docker's environment only, never to this
                process's (compose interpolates ask-at-start secrets from them).
            timeout (float | None): Seconds to wait before killing the command; None waits for ever. When it
                elapses, subprocess.run raises subprocess.TimeoutExpired.

        Returns:
            DockerResult: Exit code and captured output.

        Raises:
            FileNotFoundError: When `docker` is not on PATH.
        """
        executable = shutil.which("docker")
        if executable is None:
            raise FileNotFoundError("docker is not on PATH")
        done = subprocess.run(
            [executable, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            env=_environment(env),
            timeout=timeout,
        )
        return DockerResult(done.returncode, done.stdout, done.stderr)

    def stream(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run `docker <args>` on this terminal, so a followed log or compose's progress appears as it is written.

        Args:
            args (Sequence[str]): Arguments after `docker`.
            env (Mapping[str, str] | None): Variables added to docker's environment only.

        Returns:
            int: Docker's exit code; 130 when interrupted with Ctrl+C.

        Raises:
            FileNotFoundError: When `docker` is not on PATH.
        """
        executable = shutil.which("docker")
        if executable is None:
            raise FileNotFoundError("docker is not on PATH")
        try:
            return subprocess.run([executable, *args], check=False, env=_environment(env)).returncode
        except KeyboardInterrupt:
            return 130


def query(port: DockerPort, args: Sequence[str]) -> DockerResult:
    """Run a docker command a working daemon answers at once, giving up after QUERY_SECONDS.

    Args:
        port (DockerPort): The Docker port.
        args (Sequence[str]): Arguments after `docker`: version, inspect, ps or a bounded log tail.

    Returns:
        DockerResult: Exit code and captured output.

    Raises:
        CliError: Docker did not answer in time (exit 11), with the step to check the daemon.
    """
    try:
        return port.run(args, timeout=QUERY_SECONDS)
    except subprocess.TimeoutExpired:
        raise CliError(
            ExitCode.DOCKER_NOT_RUNNING,
            f"Docker did not answer `docker {' '.join(args[:2])}` within {QUERY_SECONDS:.0f} seconds: the daemon is "
            "still starting or has hung. Wait until Docker is ready (or restart Docker Desktop or Rancher Desktop, or "
            "`sudo systemctl restart docker` on Linux) and try again.",
            "docker info",
        ) from None


def _environment(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return this process's environment plus `extra` for a child, or None to inherit it unchanged."""
    return None if not extra else {**os.environ, **extra}


@dataclass(frozen=True)
class DockerInfo:
    """The facts check_docker established."""

    engine_version: str
    engine_major: int
    compose_version: str


def check_docker(port: DockerPort) -> DockerInfo:
    """Check that Docker Engine 24+ with Compose v2 is installed and running.

    Args:
        port (DockerPort): The Docker port.

    Returns:
        DockerInfo: Engine and Compose versions.

    Raises:
        CliError: With install or start guidance when a prerequisite is missing.
    """
    if port.which("docker") is None:
        if port.which("podman") is not None:
            _refuse_podman()
        raise CliError(ExitCode.DOCKER_MISSING, f"Docker is not installed. {INSTALL_GUIDANCE}", "docker version")

    version = query(port, VERSION_ARGS)
    if "podman" in (version.stdout + version.stderr).lower():
        _refuse_podman()
    if version.returncode != 0:
        _refuse_not_running(version.stderr.strip())
    engine = _engine_version(version.stdout)
    if engine is None:
        _refuse_not_running(version.stderr.strip() or "docker version reported no server.")
    major = int(engine.split(".")[0])
    if major < MIN_ENGINE_MAJOR:
        raise CliError(
            ExitCode.DOCKER_TOO_OLD,
            f"Docker Engine {engine} is too old; {CLI_NAME} needs Docker Engine {MIN_ENGINE_MAJOR} or later. "
            + INSTALL_GUIDANCE,
            "docker version",
        )

    compose = query(port, COMPOSE_VERSION_ARGS)
    match = re.match(r"v?(\d+)\.\d+", compose.stdout.strip())
    if compose.returncode != 0 or match is None or int(match.group(1)) < 2:
        found = compose.stderr.strip() or f"docker compose reported version {compose.stdout.strip() or 'none'}."
        raise CliError(
            ExitCode.COMPOSE_MISSING,
            f"Docker Compose v2 is missing ({found}) Install the Compose plugin "
            "(https://docs.docker.com/compose/install/), or use Docker Desktop or Rancher Desktop "
            "(dockerd (moby) engine), which include it.",
            "docker compose version",
        )
    return DockerInfo(engine_version=engine, engine_major=major, compose_version=compose.stdout.strip().lstrip("v"))


def _engine_version(stdout: str) -> str | None:
    """Return the server version from `docker version` JSON, or None when there is no server."""
    try:
        server = json.loads(stdout).get("Server")
    except json.JSONDecodeError, AttributeError:
        return None
    version = server.get("Version") if isinstance(server, dict) else None
    return version if isinstance(version, str) and re.match(r"\d+\.", version) else None


def _refuse_not_running(docker_says: str) -> NoReturn:
    """Fail as daemon-unreachable, keeping Docker's own message visible."""
    if "permission denied" in docker_says.lower():
        hint = (
            "Your user may not access the Docker daemon: add it to the docker group "
            "(sudo usermod -aG docker $USER, then sign in again)."
        )
    else:
        hint = (
            "Start Docker (Docker Desktop or Rancher Desktop, or `sudo systemctl start docker` on Linux) and try again."
        )
    raise CliError(ExitCode.DOCKER_NOT_RUNNING, f"Docker is not reachable: {docker_says} {hint}", "docker info")


def _refuse_podman() -> NoReturn:
    """Fail because Podman is not supported."""
    raise CliError(
        ExitCode.DOCKER_UNSUPPORTED,
        f"Podman was found, but this version of {CLI_NAME} supports Docker only. {INSTALL_GUIDANCE}",
        "docker version",
    )
