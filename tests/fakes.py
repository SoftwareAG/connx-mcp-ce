#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fakes.py - Fakes for the Docker port and the endpoint probe.

Description:
    FakeDocker replays recorded `docker` outputs from tests/fixtures/docker/<recording>.json and
    records every call, so a test asserts what the CLI asked the daemon, never how. A call with no
    recorded reply fails the test, so a recording never silently goes stale. A reply can be a
    sequence (a container that is `starting` twice, then `healthy`): each call takes the next one
    and the last repeats. FakeProbe returns chosen `/health` replies and served certificates;
    FakeClock lets a test wait minutes without waiting.

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
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from connx_mcp.docker import DockerResult
from connx_mcp.probe import HealthReply

RECORDINGS = Path(__file__).parent / "fixtures" / "docker"
EPOCH = 1_792_000_000.0  # 2026-10-15, where FakeClock's wall time starts


class FakeDocker:
    """A Docker port that replays a recording and keeps the calls made to it."""

    def __init__(self, executables: dict[str, str | None], replies: dict[tuple[str, ...], DockerResult]) -> None:
        self.executables = dict(executables)
        self.replies: dict[tuple[str, ...], list[DockerResult]] = {k: [v] for k, v in replies.items()}
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.environments: list[dict[str, str]] = []
        self.timeouts: list[float | None] = []
        self.hangs: list[Callable[[list[str]], bool]] = []
        self.rules: list[tuple[Callable[[list[str]], bool], DockerResult]] = []
        self.actions: list[tuple[Callable[[list[str]], bool], Callable[[list[str]], DockerResult]]] = []

    @classmethod
    def from_recording(cls, name: str) -> FakeDocker:
        """Load tests/fixtures/docker/<name>.json."""
        data = json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))
        replies = {
            tuple(c["args"]): DockerResult(c["returncode"], c.get("stdout", ""), c.get("stderr", ""))
            for c in data["commands"]
        }
        return cls(data["executables"], replies)

    def reply(self, args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> FakeDocker:
        """Add or replace the reply to one command; returns self for chaining."""
        self.replies[tuple(args)] = [DockerResult(returncode, stdout, stderr)]
        return self

    def replies_in_turn(self, args: Sequence[str], *results: DockerResult) -> FakeDocker:
        """Answer successive calls of one command in turn; the last reply repeats."""
        self.replies[tuple(args)] = list(results)
        return self

    def reply_when(
        self, match: Callable[[list[str]], bool], returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> FakeDocker:
        """Reply to every command `match` accepts (arguments holding temp paths); the latest rule wins."""
        self.rules.insert(0, (match, DockerResult(returncode, stdout, stderr)))
        return self

    def act_when(self, match: Callable[[list[str]], bool], act: Callable[[list[str]], DockerResult]) -> FakeDocker:
        """Answer every command `match` accepts by calling `act`, which may write files as docker would."""
        self.actions.insert(0, (match, act))
        return self

    def hang_when(self, match: Callable[[list[str]], bool]) -> FakeDocker:
        """Let every command `match` accepts hang, as on a daemon that does not answer; returns self.

        A hanging call given a timeout ends as the real port does, in TimeoutExpired; one given none fails the
        test, because the real command would never return.
        """
        self.hangs.append(match)
        return self

    def which(self, name: str) -> str | None:
        """Return the recorded path of an executable, or None."""
        return self.executables.get(name)

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> DockerResult:
        """Replay the recorded reply to `docker <args>`; `environments` keeps the variables each call was given."""
        self.calls.append(list(args))
        self.stdins.append(stdin)
        self.environments.append(dict(env or {}))
        self.timeouts.append(timeout)
        if any(hang(list(args)) for hang in self.hangs):
            if timeout is None:
                raise AssertionError(f"docker {' '.join(args)} hangs and was given no timeout")
            raise subprocess.TimeoutExpired(["docker", *args], timeout)
        queue = self.replies.get(tuple(args))
        if queue is not None:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        for match, act in self.actions:
            if match(list(args)):
                return act(list(args))
        for match, result in self.rules:
            if match(list(args)):
                return result
        raise AssertionError(f"no recorded reply for: docker {' '.join(args)}")

    def stream(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Replay a reply straight to stdout and stderr, as the real port lets docker write to the terminal."""
        result = self.run(args, env=env)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode


class FakeProbe:
    """An endpoint probe returning chosen health replies and certificates."""

    def __init__(self) -> None:
        self.health_replies: dict[str, HealthReply] = {}
        self.certificates: dict[tuple[str, int], bytes] = {}
        self.unresolvable: set[str] = set()
        self.calls: list[str] = []
        self.ca_files: dict[str, str | None] = {}

    def health(self, url: str, timeout: float = 5.0, ca_file: str | None = None) -> HealthReply:
        """Return the chosen reply for `url`, or an unreachable one; record the CA file it was asked with."""
        self.calls.append(url)
        self.ca_files[url] = ca_file
        return self.health_replies.get(url, HealthReply(status=None, body="", error="connection refused"))

    def served_certificate(self, host: str, port: int, timeout: float = 5.0) -> bytes | None:
        """Return the chosen DER certificate for host:port, or None."""
        self.calls.append(f"cert {host}:{port}")
        return self.certificates.get((host, port))

    def resolves(self, host: str) -> bool:
        """Return whether `host` resolves: every name does, except those in `unresolvable`; record the lookup."""
        self.calls.append(f"resolves {host}")
        return host not in self.unresolvable


class FakeClock:
    """A clock whose sleep only moves time on."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the fake time in seconds."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Move the fake time on."""
        self.now += seconds

    def time(self) -> float:
        """Return the fake time as seconds since the epoch."""
        return EPOCH + self.now
