#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ports.py - The ports a command reaches the outside world through.

Description:
    Commands take their Docker port, endpoint probe and clock from the Typer context object. Tests
    pass fakes there (`CliRunner.invoke(app, args, obj=Ports(...))`); a real run gets
    default_ports(). The terminal rides along: tests give it a prompt_toolkit pipe input to answer
    prompts. The clock lets a test wait out a start timeout without waiting.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import Input
from prompt_toolkit.output import Output as TerminalOutput

from connx_mcp.docker import DockerPort, SubprocessDocker
from connx_mcp.output import can_prompt
from connx_mcp.probe import EndpointProbe, UrllibProbe


class Clock(Protocol):
    """Time as the lifecycle commands see it."""

    def monotonic(self) -> float:
        """Return seconds on a clock that never goes back."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait."""
        ...

    def time(self) -> float:
        """Return seconds since the epoch, as `docker logs --since` takes them."""
        ...


class SystemClock:
    """The real clock."""

    def time(self) -> float:
        """Return seconds since the epoch, as `docker logs --since` takes them."""
        return time.time()

    def monotonic(self) -> float:
        """Return seconds on a clock that never goes back."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Wait.

        Args:
            seconds (float): How long.
        """
        time.sleep(seconds)


@dataclass(frozen=True)
class Terminal:
    """Where prompts read keys and draw: the real terminal, or a given prompt_toolkit input and output.

    Attributes:
        input (Input | None): The keyboard; None means the real terminal.
        output (TerminalOutput | None): The screen; None means the real terminal.
    """

    input: Input | None = None
    output: TerminalOutput | None = None

    def attached(self) -> bool:
        """Return whether there is a keyboard to prompt on: a given input, or a terminal on stdin."""
        return self.input is not None or can_prompt(no_input=False)

    @contextmanager
    def session(self) -> Generator[None]:
        """Route every prompt and message inside the block to this terminal.

        Yields:
            None: Prompts built inside the block use this terminal.
        """
        with create_app_session(input=self.input, output=self.output):
            yield


@dataclass(frozen=True)
class Ports:
    """The Docker port, the endpoint probe, the terminal prompts run on (tests pass a pipe input) and the clock."""

    docker: DockerPort
    probe: EndpointProbe
    terminal: Terminal = field(default_factory=Terminal)
    clock: Clock = field(default_factory=SystemClock)


def default_ports() -> Ports:
    """Return the real adapters."""
    return Ports(docker=SubprocessDocker(), probe=UrllibProbe(), clock=SystemClock())
