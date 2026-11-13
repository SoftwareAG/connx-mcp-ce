#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""conftest.py - Shared fixtures: an isolated machine and the in-process CLI seam.

Description:
    Every test runs with its own home directory, base directory and no CONNX_MCP_ENVIRONMENT, so
    nothing reads or writes the developer's real ~/.connx. The `cli` fixture invokes `connx-mcp-ce`
    in process with the fake Docker port and endpoint probe injected; outside `live` tests the
    real adapters refuse to be built, so a test can never reach a real daemon by accident. In a
    `live` test it has the real Docker port and probe; when CONNX_MCP_BINARY names a built binary,
    it runs that binary as a process instead, so that a Release build's live run tests the bytes
    it ships.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.plain_text import PlainTextOutput
from typer.testing import CliRunner, Result

from connx_mcp import cli as cli_module
from connx_mcp import image
from connx_mcp.cli import app
from connx_mcp.ports import Ports, Terminal, default_ports
from tests.fakes import FakeClock, FakeDocker, FakeProbe


class WideScreen(PlainTextOutput):
    """A plain-text terminal screen wide enough that no prompt line wraps."""

    def get_size(self) -> Size:
        return Size(rows=40, columns=1000)


@dataclass
class Machine:
    """The isolated machine a test runs on."""

    home: Path
    base: Path


@pytest.fixture(autouse=True)
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Machine:
    home = tmp_path / "home"
    base = tmp_path / "base"
    home.mkdir()
    # A Windows profile always holds these; the release binary unpacks itself under AppData\Local and aborts
    # before connx-mcp-ce runs when the folder is missing.
    (home / "AppData" / "Local").mkdir(parents=True)
    (home / "AppData" / "Roaming").mkdir()
    live = request.node.get_closest_marker("live") is not None
    if live:
        # The Docker CLI finds its registry sign-ins and (Docker Desktop) context under the real home.
        monkeypatch.setenv("DOCKER_CONFIG", os.environ.get("DOCKER_CONFIG") or str(Path.home() / ".docker"))
    else:
        monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Hermes keeps its Windows user skills under LOCALAPPDATA: never the runner's own.
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("MCP_ENVIRONMENT_PATH", str(base))
    monkeypatch.delenv("CONNX_MCP_ENVIRONMENT", raising=False)
    if not live:
        # The image variables a live run passes through (they choose its image) never reach a test of fakes.
        for name in (*image.IMAGE_VARIABLES, image.RENAMED_TAG_VARIABLE):
            monkeypatch.delenv(name, raising=False)

        def refuse() -> Ports:
            raise AssertionError("a non-live test reached the real Docker port or endpoint probe")

        monkeypatch.setattr(cli_module, "default_ports", refuse)
    return Machine(home=home, base=base)


@dataclass(frozen=True)
class BinaryResult:
    """What a run of the built binary left: its exit code and its output, as a CliRunner result names them."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """stdout, then stderr."""
        return self.stdout + self.stderr


@dataclass
class Cli:
    """Runs `connx-mcp-ce` in process with the fakes injected, or the built ``binary`` as a process."""

    docker: FakeDocker = field(default_factory=lambda: FakeDocker.from_recording("docker-ready"))
    probe: FakeProbe = field(default_factory=FakeProbe)
    clock: FakeClock = field(default_factory=FakeClock)
    binary: Path | None = None

    def __call__(
        self, *args: str, input: str | None = None, env: dict[str, str] | None = None
    ) -> Result | BinaryResult:
        if self.binary is not None:
            return self._run_binary(list(args), input, env)
        return CliRunner().invoke(
            app, list(args), input=input, env=env, obj=Ports(self.docker, self.probe, clock=self.clock)
        )

    def _run_binary(self, args: list[str], input: str | None, env: dict[str, str] | None) -> BinaryResult:
        """Run the binary with this test's environment (its home and base directory) and ``env`` laid over it."""
        environ = dict(os.environ) | (env or {})
        done = subprocess.run(
            [str(self.binary), *args], input=input if input is not None else "", capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environ, timeout=900, check=False,
        )  # fmt: skip
        return BinaryResult(done.returncode, done.stdout, done.stderr)

    def json(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> tuple[int, Any]:
        """Run with --json and return the exit code and the one parsed JSON object."""
        result = self(*args, "--json", input=input, env=env)
        return result.exit_code, json.loads(result.stdout)

    def typed(self, *args: str, keys: Sequence[str]) -> tuple[Result, str]:
        """Run on a terminal whose keyboard types `keys` (one string per answer, Enter added) and then ends.

        Returns the result and everything the prompts drew on the terminal. Input running out before the
        command finishes ends it the way Ctrl-D would, so a test never hangs on a prompt it did not expect.
        In process only: a binary has no terminal to type on.
        """
        assert self.binary is None, "a typed run needs the in-process CLI"
        screen = io.StringIO()
        with create_pipe_input() as keyboard:
            keyboard.send_text("".join(f"{k}\r" for k in keys))
            keyboard.close()
            terminal = Terminal(input=keyboard, output=WideScreen(screen))
            result = CliRunner().invoke(app, list(args), obj=Ports(self.docker, self.probe, terminal, self.clock))
        return result, screen.getvalue()


@pytest.fixture
def cli(request: pytest.FixtureRequest) -> Cli:
    """The CLI: with fakes, or in a `live` test with the real ports, or the built binary CONNX_MCP_BINARY names."""
    if request.node.get_closest_marker("live") is None:
        return Cli()
    binary = os.environ.get("CONNX_MCP_BINARY")
    live = Cli(binary=Path(binary).resolve() if binary else None)
    real = default_ports()
    live.docker, live.probe, live.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    return live


def refused_with(body: Any, prefix: str) -> bool:
    """Return whether one refusal of a refused command's JSON error (one per line) starts with `prefix`."""
    return any(line.startswith(prefix) for line in body["error"]["message"].splitlines())


def mounts(rendered: str) -> list[dict[str, Any]]:
    """Return the server service's volume entries of a rendered compose file, as compose reads them."""
    (service,) = yaml.safe_load(rendered)["services"].values()
    return service["volumes"]


def bind(source: Path, target: str, *, read_only: bool = False) -> dict[str, Any]:
    """Return a bind mount's volume entry as compose reads it: `read_only` appears only when it is set."""
    return {"type": "bind", "source": str(source), "target": target} | ({"read_only": True} if read_only else {})


def as_typed(path: Path) -> str:
    """Return a temporary path as a message shows it as a command-line argument on this platform.

    A Windows path holds backslashes, which Windows quotes with double quotes; a POSIX temporary path needs none.
    """
    return f'"{path}"' if os.name == "nt" else str(path)
