#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_output.py - The shared JSON envelope and the prompt rule.

Description:
    Unit tests for the output layer's success envelope
    and for the rule that decides whether a prompt is allowed.

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

import pytest

from connx_mcp.output import (
    EXIT_CODE_MEANINGS,
    CliError,
    ExitCode,
    Output,
    can_prompt,
    exit_code_table,
    shell_argument,
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_success_envelope_carries_the_result(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=True, command="status").result({"environments": []}, human="No environments.")

    assert json.loads(capsys.readouterr().out) == {"ok": True, "command": "status", "result": {"environments": []}}


def test_success_in_human_mode_prints_the_human_text(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=False, command="status").result({"environments": []}, human="No environments.")

    assert capsys.readouterr().out == "No environments.\n"


def test_error_envelope_names_code_message_and_next(capsys: pytest.CaptureFixture[str]) -> None:
    error = CliError(ExitCode.INPUT_REQUIRED, "CONNX_USERNAME is required.", next_command="connx-mcp-ce configure")

    exit_code = Output(json_mode=True, command="configure").error(error)

    assert exit_code == 3
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "command": "configure",
        "error": {
            "code": "input_required",
            "exit_code": 3,
            "message": "CONNX_USERNAME is required.",
            "next": "connx-mcp-ce configure",
        },
    }


def test_error_details_are_added_beside_code_message_and_next(capsys: pytest.CaptureFixture[str]) -> None:
    error = CliError(ExitCode.UNINSTALL_FAILED, "Could not remove.", details={"removed_paths": ["AGENTS.md"]})

    Output(json_mode=True, command="uninstall-skill").error(error)

    body = json.loads(capsys.readouterr().out)["error"]
    assert (body["code"], body["message"], body["removed_paths"]) == (
        "uninstall_failed",
        "Could not remove.",
        ["AGENTS.md"],
    )


@pytest.mark.parametrize("key", ["code", "exit_code", "message", "next"])
def test_error_details_cannot_replace_code_message_or_next(key: str) -> None:
    with pytest.raises(ValueError, match=key):
        CliError(ExitCode.UNINSTALL_FAILED, "Could not remove.", details={key: "replaced"})


def test_every_exit_code_is_unique_and_documented() -> None:
    members = ExitCode.__members__
    assert len({int(code) for code in members.values()}) == len(members)
    assert set(EXIT_CODE_MEANINGS) == set(ExitCode)


def test_a_cancelled_prompt_and_an_unreadable_file_have_their_own_codes() -> None:
    assert (int(ExitCode.CANCELLED), ExitCode.CANCELLED.code) == (100, "cancelled")
    assert (int(ExitCode.FILE_UNREADABLE), ExitCode.FILE_UNREADABLE.code) == (92, "file_unreadable")
    table = exit_code_table()
    assert "100  cancelled" in table
    assert "92  file_unreadable" in table


@pytest.mark.parametrize(
    ("no_input", "stdin", "expected"),
    [(False, _Tty(), True), (True, _Tty(), False), (False, io.StringIO(), False)],
    ids=["terminal", "no-input", "piped-stdin"],
)
def test_prompts_only_on_a_terminal_without_no_input(no_input: bool, stdin: io.StringIO, expected: bool) -> None:
    assert can_prompt(no_input=no_input, stdin=stdin) is expected


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("/home/x/sales.cdd", "/home/x/sales.cdd"),
        ("/home/x/my sales.cdd", "'/home/x/my sales.cdd'"),
        ("it's", "'it'\"'\"'s'"),
        ("", "''"),
    ],
)
def test_an_argument_on_posix_is_quoted_the_way_a_posix_shell_reads_it(value: str, shown: str) -> None:
    assert shell_argument(value, windows=False) == shown


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("sales", "sales"),
        ("C:/Users/x/sales.cdd", "C:/Users/x/sales.cdd"),
        (r"C:\Users\x\sales.cdd", r'"C:\Users\x\sales.cdd"'),
        (r"C:\Users\x\my sales", r'"C:\Users\x\my sales"'),
        ("a&b", '"a&b"'),
        ("100%", '"100%"'),
        ('say "hi"', r'"say \"hi\""'),
        ("C:\\dir\\", '"C:\\dir\\\\"'),
        ("", '""'),
    ],
)
def test_an_argument_on_windows_is_double_quoted_only_when_a_shell_would_read_it_differently(
    value: str, shown: str
) -> None:
    assert shell_argument(value, windows=True) == shown
