#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_edit.py - Settings edits resolve into an environment.

Description:
    Through `edit.resolve`, against in-memory environments: precedence by source (flag over demo over
    import) whatever the order of the edits, the ties at one rank, claims by side and name, the
    lower-rank refusals that are dropped and those that are always reported, the trust given to demo
    values, ask-at-start marking after every other edit, `Unset(only_if=...)`, the `changed` diff, and the one
    wording every refusal takes: `<origin>: <reason>`.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from connx_mcp.edit import (
    AskAtStart,
    AskForMissingSecrets,
    Claim,
    Fill,
    Origin,
    OriginKind,
    Refuse,
    Set,
    SetHost,
    Unset,
    resolve,
)
from connx_mcp.environment import Environment
from connx_mcp.output import shell_argument
from connx_mcp.settings import HostSettings
from tests.conftest import as_typed

FILE = Path("/tmp/sent.env")
SET = Origin(OriginKind.SET_FLAG, "--set")
UNSET = Origin(OriginKind.SET_FLAG, "--unset")
PORT = Origin(OriginKind.HOST_FLAG, "--port")
CDD = Origin(OriginKind.HOST_FLAG, "--cdd")
ASK = Origin(OriginKind.ASK_FLAG, "--ask-at-start")
DEMO = Origin(OriginKind.DEMO, "--demo")
SWEEP = Origin(OriginKind.IMPORT_FILE, str(FILE))
PAIRS = [("flag", "demo"), ("flag", "import"), ("demo", "import")]
LICENCE_WARNING = (
    "CONNX_LICENSE_HOST is not set: the server starts, but queries need a reachable licence server. Set it, e.g. "
    "--set CONNX_LICENSE_HOST=host.docker.internal when it runs on this machine."
)


def line(number: int) -> Origin:
    return Origin(OriginKind.IMPORT_LINE, str(FILE), number)


RANKED = {"flag": SET, "demo": DEMO, "import": line(3)}


def server_value(source: str, key: str, value: str) -> Set | Fill:
    """A server value from a flag, an imported line or the demo stack (whose server values are always fills)."""
    if source == "demo":
        return Fill(key, value, DEMO)
    return Set(key, value, RANKED[source])


def environment(server: dict[str, str] | None = None, host: HostSettings | None = None) -> Environment:
    return Environment("sales", Path("/nowhere/sales"), dict(server or {}), host or HostSettings())


def test_a_flag_value_is_stored_in_a_new_environment_and_the_given_one_is_left_as_it_was() -> None:
    env = environment({"CDD_USERNAME": "reader"}, HostSettings(ask_at_start=["CDD_PASSWORD"]))

    edited = resolve(env, [Set("CDD_USERNAME", "writer", SET), Set("mcp_path", "/sql", SET)])

    assert edited.env.server == {"CDD_USERNAME": "writer", "MCP_PATH": "/sql"}
    assert env.server == {"CDD_USERNAME": "reader"}
    assert env.host == HostSettings(ask_at_start=["CDD_PASSWORD"])
    assert edited.refusals == []


@pytest.mark.parametrize("higher, lower", PAIRS)
@pytest.mark.parametrize("higher_first", [True, False], ids=["higher-first", "lower-first"])
def test_the_higher_ranked_source_wins_whatever_the_order_of_the_edits(
    higher: str, lower: str, higher_first: bool
) -> None:
    edits = [server_value(higher, "MCP_PATH", "/winner"), server_value(lower, "MCP_PATH", "/loser")]

    edited = resolve(environment(), edits if higher_first else edits[::-1])

    assert edited.env.server == {"MCP_PATH": "/winner"}
    assert edited.refusals == []


def test_a_flag_value_that_does_not_hold_is_refused_naming_the_flag_and_its_key_and_not_stored() -> None:
    edited = resolve(environment(), [Set("MCP_PATH", "sql", SET), Set("CONNX_BASE_URL", "https://x", SET)])

    assert edited.refusals == [
        "--set MCP_PATH: it must be a URL path that starts with /, without spaces, query or fragment.",
        "--set CONNX_BASE_URL: it is retired; the server refuses to start while it is set. Leave it out.",
    ]
    assert edited.env.server == {}


def test_an_empty_or_multi_line_value_is_refused_without_naming_its_key_again() -> None:
    edited = resolve(environment(), [Set("MCP_STARTUP_WAIT_SECONDS", "", SET), Set("MCP_PATH", "/a\nb", line(2))])

    assert edited.refusals == [
        "--set MCP_STARTUP_WAIT_SECONDS: it cannot be empty; give a value, or --unset it to return to the server "
        "default 120.",
        f"{FILE}, line 2 (MCP_PATH): it cannot hold a line break or NUL character; the server settings file keeps "
        "one setting per line.",
    ]


@pytest.mark.parametrize("origin", [SET, line(4)], ids=["flag", "file"])
def test_an_unknown_key_is_stored_with_a_warning_naming_its_origin(origin: Origin) -> None:
    where = "--set MCP_FUTURE_KNOB" if origin is SET else f"{FILE}, line 4 (MCP_FUTURE_KNOB)"

    edited = resolve(environment(), [Set("MCP_FUTURE_KNOB", "7", origin)])

    assert edited.env.server == {"MCP_FUTURE_KNOB": "7"}
    assert f"{where}: it is not a setting this connx-mcp-ce knows, so it is stored unvalidated." in edited.warnings
    assert not [w for w in edited.warnings if w.startswith("MCP_FUTURE_KNOB")]


def test_a_set_flag_without_a_value_is_refused_naming_the_key_only_when_it_looks_like_one() -> None:
    edited = resolve(environment(), [Set("mcp_path", None, SET), Set("not a key", None, SET)])

    assert edited.refusals == [
        "--set MCP_PATH: the item has no =; give it as KEY=VALUE (KEY= for an explicit empty value).",
        "--set: the item has no =; give it as KEY=VALUE (KEY= for an explicit empty value).",
    ]


def test_a_file_value_that_does_not_hold_is_refused_naming_its_file_and_line() -> None:
    edited = resolve(environment(), [Set("MCP_PATH", "sql", line(4))])

    assert edited.refusals == [
        f"{FILE}, line 4 (MCP_PATH): it must be a URL path that starts with /, without spaces, query or fragment."
    ]
    assert edited.env.server == {}


@pytest.mark.parametrize("higher", ["flag", "demo"])
def test_a_file_value_a_higher_source_claims_is_skipped_without_being_validated(higher: str) -> None:
    edited = resolve(environment(), [Set("MCP_PATH", "sql", line(2)), server_value(higher, "MCP_PATH", "/sql")])

    assert edited.refusals == []
    assert edited.env.server == {"MCP_PATH": "/sql"}


def test_a_demo_value_is_stored_as_given_without_a_parse() -> None:
    edited = resolve(environment(), [Fill("MCP_PATH", "not-a-path", DEMO)])

    assert edited.refusals == []
    assert edited.env.server == {"MCP_PATH": "not-a-path"}


def test_a_demo_server_value_is_only_a_fill_so_a_secret_marked_ask_at_start_is_never_stored() -> None:
    with pytest.raises(ValueError, match="CDD_PASSWORD: a demo server value is a Fill edit"):
        Set("CDD_PASSWORD", "demo-secret", DEMO)


# --- Ties at one rank -------------------------------------------------------------------------------------------


def test_the_last_of_two_flag_values_for_one_key_wins_and_an_invalid_earlier_one_is_still_reported() -> None:
    edited = resolve(environment(), [Set("MCP_PATH", "sql", SET), Set("mcp_path", "/b", SET)])

    assert edited.env.server == {"MCP_PATH": "/b"}
    assert edited.refusals == [
        "--set MCP_PATH: it must be a URL path that starts with /, without spaces, query or fragment."
    ]


def test_the_last_of_two_demo_values_for_one_key_wins() -> None:
    edited = resolve(environment(), [Fill("MCP_PATH", "/a", DEMO), Fill("MCP_PATH", "/b", DEMO)])

    assert edited.env.server == {"MCP_PATH": "/b"}
    assert edited.refusals == []


def test_a_key_given_again_in_a_file_takes_the_last_line_and_warns_at_each_later_one() -> None:
    edits = [
        Set("MCP_PATH", "/a", line(2)),
        Set("mcp_path", "/b", line(5)),
        Set("MCP_PATH", "/c", line(7)),
    ]

    edited = resolve(environment(), edits)

    assert edited.refusals == []
    assert edited.env.server == {"MCP_PATH": "/c"}
    assert edited.warnings == [
        f"{FILE}, line 5 (MCP_PATH): it is given again (line 2 is ignored).",
        f"{FILE}, line 7 (MCP_PATH): it is given again (line 5 is ignored).",
        LICENCE_WARNING,
    ]


def test_a_flag_beats_a_key_given_again_in_a_file_and_the_warning_is_still_given() -> None:
    edits = [Set("MCP_PATH", "/a", line(2)), Set("MCP_PATH", "/b", line(5)), Set("MCP_PATH", "/flag", SET)]

    edited = resolve(environment(), edits)

    assert edited.refusals == []
    assert edited.env.server == {"MCP_PATH": "/flag"}
    assert f"{FILE}, line 5 (MCP_PATH): it is given again (line 2 is ignored)." in edited.warnings


def test_the_ignored_line_of_a_key_given_again_is_not_validated() -> None:
    edits = [Set("MCP_PATH", "no-leading-slash", line(2)), Set("MCP_PATH", "/sql", line(5))]

    edited = resolve(environment(), edits)

    assert edited.refusals == []
    assert edited.env.server == {"MCP_PATH": "/sql"}


def test_a_parse_error_between_two_lines_of_a_key_is_reported_and_the_key_takes_its_last_line() -> None:
    edits = [
        Set("MCP_PATH", "/a", line(2)),
        Refuse("MCP_PATH has text after its closing quote.", line(3)),
        Set("MCP_PATH", "/sql", line(5)),
    ]

    edited = resolve(environment(), edits)

    assert edited.refusals == [f"{FILE}, line 3: MCP_PATH has text after its closing quote."]
    assert f"{FILE}, line 5 (MCP_PATH): it is given again (line 2 is ignored)." in edited.warnings
    assert edited.env.server == {"MCP_PATH": "/sql"}


@pytest.mark.parametrize("set_first", [True, False], ids=["set-first", "unset-first"])
def test_a_key_both_set_and_unset_by_flags_is_refused_and_the_unset_holds(set_first: bool) -> None:
    edits = [Set("mcp_path", "/sql", SET), Unset("MCP_PATH", UNSET)]

    edited = resolve(environment({"MCP_PATH": "/old"}), edits if set_first else edits[::-1])

    assert edited.refusals == ["--set MCP_PATH: it is both set and unset; give one of them."]
    assert edited.env.server == {}


# --- Unset ------------------------------------------------------------------------------------------------------


def test_an_unset_flag_removes_the_value_and_beats_a_file_value() -> None:
    env = environment({"MCP_PATH": "/old", "CDD_USERNAME": "reader"})

    edited = resolve(env, [Set("MCP_PATH", "/file", line(1)), Unset("mcp_path", UNSET)])

    assert edited.env.server == {"CDD_USERNAME": "reader"}
    assert edited.refusals == []


def test_an_unset_with_only_if_removes_the_value_only_while_it_is_still_the_given_one() -> None:
    env = environment({"MCP_PATH": "/rendered", "MCP_OAUTH_AUDIENCE": "changed-since"})
    edits = [Unset("MCP_PATH", DEMO, only_if="/rendered"), Unset("MCP_OAUTH_AUDIENCE", DEMO, only_if="rendered")]

    edited = resolve(env, edits)

    assert edited.env.server == {"MCP_OAUTH_AUDIENCE": "changed-since"}


@pytest.mark.parametrize("source", ["flag", "import"])
@pytest.mark.parametrize("unset_first", [True, False], ids=["unset-first", "unset-last"])
def test_an_unset_with_only_if_judges_what_the_other_edits_leave_and_never_beats_them(
    source: str, unset_first: bool
) -> None:
    env = environment({"MCP_PATH": "/rendered", "MCP_SESSION_TIMEOUT_SECONDS": "600"})
    given = [Set("MCP_PATH", "/given", RANKED[source]), Set("MCP_SESSION_TIMEOUT_SECONDS", "900", RANKED[source])]
    ended = [Unset("MCP_PATH", DEMO, only_if="/rendered"), Unset("MCP_SESSION_TIMEOUT_SECONDS", DEMO, only_if="900")]

    edited = resolve(env, [*ended, *given] if unset_first else [*given, *ended])

    # A value no longer the rendered one stays; one still the given value goes unless a flag gave it.
    expected = {"MCP_PATH": "/given", **({"MCP_SESSION_TIMEOUT_SECONDS": "900"} if source == "flag" else {})}
    assert edited.env.server == expected
    assert edited.refusals == []


# --- Host settings ----------------------------------------------------------------------------------------------


def test_a_host_flag_goes_through_its_parser_and_a_value_that_does_not_hold_is_refused_naming_the_flag(
    tmp_path: Path,
) -> None:
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")

    edited = resolve(environment(), [SetHost("published_port", "9100", PORT), SetHost("cdd_file", str(cdd), CDD)])
    refused = resolve(environment(), [SetHost("published_port", "0", PORT)])

    assert (edited.env.host.published_port, edited.env.host.cdd_file) == (9100, str(cdd))
    assert edited.refusals == []
    assert refused.refusals == ["--port 0: it must be at least 1."]
    assert refused.env.host.published_port is None


def test_a_host_flag_value_is_named_as_a_shell_would_read_it() -> None:
    edited = resolve(environment(), [SetHost("published_port", "nine thousand", PORT)])

    assert edited.refusals == [f"--port {shell_argument('nine thousand')}: it must be a whole number."]


def test_a_missing_file_is_named_once_by_a_host_flag_and_by_an_imported_line(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.cdd"

    edited = resolve(environment(), [SetHost("cdd_file", str(missing), CDD)])
    imported = resolve(environment(), [SetHost("cdd_file", str(missing), line(3))])

    assert edited.refusals == [f"--cdd {as_typed(missing)}: no such file."]
    assert imported.refusals == [
        f"{FILE}, line 3 (cdd_file): no such file; the line gives {as_typed(missing)}. "
        "Give --cdd to choose it on this machine."
    ]


@pytest.mark.parametrize(
    "name, value, problem",
    [
        (
            "published_port",
            0,
            "it must be at least 1; the line gives 0. Give --port to choose it on this machine.",
        ),
        (
            "published_port",
            True,
            "it must be a whole number; the line gives true. Give --port to choose it on this machine.",
        ),
        ("published_port", [8000], "it must be a JSON string, number or boolean."),
        (
            "ask_at_start",
            "CDD_PASSWORD",
            "it is not imported; mark each secret with a `# secret KEY` line instead.",
        ),
        (
            "colour",
            "blue",
            "it is not a host setting; the host settings are cdd_file, published_port, listen_address, "
            "open_network_accepted, image, image_digest, image_version, image_pinned_reference, start_timeout, vault_ca_file, demo_stack_host, "
            "demo_stack_ports, cert_dir.",
        ),
    ],
)
def test_a_host_setting_line_that_does_not_hold_is_refused_naming_its_line(
    name: str, value: object, problem: str
) -> None:
    edited = resolve(environment(), [SetHost(name, value, line(6))])

    assert edited.refusals == [f"{FILE}, line 6 ({name}): {problem}"]
    assert edited.env.host == HostSettings()


def test_a_host_setting_line_that_holds_is_stored_parsed() -> None:
    edited = resolve(environment(), [SetHost("published_port", 9100, line(1)), SetHost("start_timeout", "90", line(2))])

    assert (edited.env.host.published_port, edited.env.host.start_timeout) == (9100, 90)
    assert edited.refusals == []


def test_a_demo_host_value_is_stored_as_given_without_a_parse(tmp_path: Path) -> None:
    missing = tmp_path / "generated" / "ca.pem"  # start-demo creates it later

    edited = resolve(environment(), [SetHost("vault_ca_file", str(missing), DEMO)])

    assert edited.env.host.vault_ca_file == str(missing)
    assert edited.refusals == []


@pytest.mark.parametrize("higher, lower", PAIRS)
@pytest.mark.parametrize("higher_first", [True, False], ids=["higher-first", "lower-first"])
def test_the_higher_ranked_source_wins_a_host_setting_whatever_the_order_of_the_edits(
    higher: str, lower: str, higher_first: bool
) -> None:
    origins = {**RANKED, "flag": PORT}
    winner = {"flag": "9100", "demo": 9100, "import": 9100}[higher]  # a demo value is stored as given
    edits = [SetHost("published_port", winner, origins[higher]), SetHost("published_port", 0, origins[lower])]

    edited = resolve(environment(), edits if higher_first else edits[::-1])

    assert edited.env.host.published_port == 9100
    assert edited.refusals == []


def test_a_host_flag_leaves_a_server_line_of_its_name_to_be_refused_as_a_host_setting(tmp_path: Path) -> None:
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")

    edited = resolve(environment(), [Set("cdd_file", "from-the-file", line(3)), SetHost("cdd_file", str(cdd), CDD)])

    # The file's server line is judged, not skipped as the flag's: a host name is refused as a server key.
    assert edited.env.server == {}
    assert any("line 3 (cdd_file): it is a host setting, not a server setting" in r for r in edited.refusals)
    assert edited.env.host.cdd_file == str(cdd)


def test_the_last_of_two_host_setting_lines_in_a_file_wins() -> None:
    edited = resolve(
        environment(), [SetHost("published_port", 9100, line(1)), SetHost("published_port", 9200, line(4))]
    )

    assert edited.env.host.published_port == 9200
    assert edited.refusals == []


# --- Ask-at-start -----------------------------------------------------------------------------------------------


def test_an_ask_at_start_flag_drops_the_stored_value_marks_the_secret_and_beats_a_file_value() -> None:
    env = environment({"CDD_PASSWORD": "stored", "CDD_USERNAME": "reader"})

    edited = resolve(env, [Set("CDD_PASSWORD", "from-the-file\n", line(2)), AskAtStart("cdd_password", ASK)])

    assert edited.env.server == {"CDD_USERNAME": "reader"}
    assert edited.env.asked_at_start == {"CDD_PASSWORD"}
    assert edited.refusals == []


def test_an_ask_at_start_flag_for_a_key_that_is_not_a_secret_or_is_also_set_is_refused() -> None:
    edits = [
        AskAtStart("MCP_PATH", ASK),
        AskAtStart("MCP_CDD_FILE", ASK),
        AskAtStart("CDD_PASSWORD", ASK),
        Set("CDD_PASSWORD", "pw", SET),
        AskAtStart("MCP_TLS_KEY_PASSWORD", ASK),
        Unset("MCP_TLS_KEY_PASSWORD", UNSET),
    ]

    edited = resolve(environment(), edits)

    assert edited.refusals == [
        "--ask-at-start MCP_PATH: it is not a secret, and only a secret can be asked for at start; store its value "
        "instead.",
        "--ask-at-start MCP_CDD_FILE: it is a host setting, not a server setting; set it with --cdd.",
        "--ask-at-start CDD_PASSWORD: it is both asked for at start and set or unset; give one of them.",
        "--ask-at-start MCP_TLS_KEY_PASSWORD: it is both asked for at start and set or unset; give one of them.",
    ]
    assert edited.env.asked_at_start == frozenset()


@pytest.mark.parametrize(
    "edit",
    [Set("cdd_password", "pw", SET), Set("CDD_PASSWORD", "pw", line(1)), Unset("CDD_PASSWORD", UNSET)],
    ids=["flag", "file", "unset"],
)
def test_setting_or_unsetting_a_secret_marked_ask_at_start_removes_the_mark(edit: Set | Unset) -> None:
    env = environment(host=HostSettings(ask_at_start=["CDD_PASSWORD", "MCP_TLS_KEY_PASSWORD"]))

    edited = resolve(env, [edit])

    assert edited.env.asked_at_start == {"MCP_TLS_KEY_PASSWORD"}


def test_a_secret_line_marks_a_secret_the_file_leaves_out_and_says_so() -> None:
    env = environment({"MCP_TLS_KEY_PASSWORD": "stored"})
    edits = [
        AskAtStart("CDD_PASSWORD", line(3), unless_stored=True),
        AskAtStart("MCP_TLS_KEY_PASSWORD", line(4), unless_stored=True),
    ]

    edited = resolve(env, edits)

    assert edited.env.asked_at_start == {"CDD_PASSWORD"}
    assert edited.env.server == {"MCP_TLS_KEY_PASSWORD": "stored"}
    assert edited.warnings[0] == (
        f"{FILE}, line 3 (CDD_PASSWORD): its value is not in the file; start asks for it (or store it with "
        "configure --set)."
    )
    assert "MCP_TLS_KEY_PASSWORD is not in" not in " ".join(edited.warnings)


def test_a_secret_line_naming_a_key_that_cannot_be_asked_for_is_reported_even_when_a_flag_claims_it() -> None:
    edited = resolve(environment(), [AskAtStart("MCP_PATH", line(5), unless_stored=True), Set("MCP_PATH", "/s", SET)])

    assert edited.refusals == [
        f"{FILE}, line 5 (MCP_PATH): it is not a secret, and only a secret can be asked for at start; store its "
        "value instead."
    ]


def test_a_secret_line_listed_before_the_demo_fill_leaves_the_secret_the_demo_fills() -> None:
    edits = [
        AskAtStart("MCP_OAUTH_CLIENT_SECRET", line(3), unless_stored=True),
        Fill("MCP_OAUTH_CLIENT_SECRET", "s", DEMO),
    ]

    edited = resolve(environment(), edits)

    assert edited.env.asked_at_start == frozenset()
    assert edited.env.server == {"MCP_OAUTH_CLIENT_SECRET": "s"}


def test_a_secret_line_for_a_key_a_flag_unsets_is_not_marked() -> None:
    edited = resolve(
        environment(), [AskAtStart("CDD_PASSWORD", line(3), unless_stored=True), Unset("CDD_PASSWORD", UNSET)]
    )

    assert edited.env.asked_at_start == frozenset()


def test_the_sweep_marks_each_required_secret_still_missing_after_every_other_edit() -> None:
    env = environment(host=HostSettings(ask_at_start=["MCP_TLS_KEY_PASSWORD"]))
    edits = [
        AskForMissingSecrets(SWEEP),
        AskAtStart("MCP_TLS_KEY_PASSWORD", line(1), unless_stored=True),
        Fill("MCP_OAUTH_PROVIDER", "azure", DEMO),
    ]

    edited = resolve(env, edits)

    assert edited.env.asked_at_start == {"CDD_PASSWORD", "MCP_OAUTH_CLIENT_SECRET", "MCP_TLS_KEY_PASSWORD"}
    assert edited.warnings[:2] == [
        f"{FILE}: CDD_PASSWORD is not in the file; start asks for it (or store it with configure --set "
        "CDD_PASSWORD=...).",
        f"{FILE}: MCP_OAUTH_CLIENT_SECRET is not in the file; start asks for it (or store it with configure --set "
        "MCP_OAUTH_CLIENT_SECRET=...).",
    ]


def test_the_sweep_leaves_a_secret_a_flag_stores_stored_and_unmarked() -> None:
    edited = resolve(environment(), [AskForMissingSecrets(SWEEP), Set("CDD_PASSWORD", "pw", SET)])

    assert edited.env.server == {"CDD_PASSWORD": "pw"}
    assert edited.env.asked_at_start == frozenset()


def test_the_sweep_leaves_a_secret_the_demo_fills_or_a_flag_unsets() -> None:
    edits = [
        AskForMissingSecrets(SWEEP),
        Fill("MCP_OAUTH_PROVIDER", "azure", DEMO),
        Fill("MCP_OAUTH_CLIENT_SECRET", "demo-secret", DEMO),
        Unset("CDD_PASSWORD", UNSET),
    ]

    edited = resolve(environment(), edits)

    assert edited.env.asked_at_start == frozenset()
    assert edited.env.server == {"MCP_OAUTH_PROVIDER": "azure", "MCP_OAUTH_CLIENT_SECRET": "demo-secret"}


# --- Fill and Refuse --------------------------------------------------------------------------------------------


def test_a_fill_never_fills_a_secret_marked_ask_at_start_yet_still_beats_a_file_value() -> None:
    env = environment(host=HostSettings(ask_at_start=["MCP_OAUTH_CLIENT_SECRET"]))
    edits = [
        Set("MCP_OAUTH_CLIENT_SECRET", "from-the-file", line(2)),
        Fill("MCP_OAUTH_CLIENT_SECRET", "demo-secret", DEMO),
        Fill("MCP_OAUTH_CLIENT_ID", "demo-client", DEMO),
    ]

    edited = resolve(env, edits)

    assert edited.env.server == {"MCP_OAUTH_CLIENT_ID": "demo-client"}
    assert edited.env.asked_at_start == {"MCP_OAUTH_CLIENT_SECRET"}


def test_a_claim_changes_nothing_yet_beats_a_lower_source_and_keeps_its_refusal_out() -> None:
    env = environment({"VAULT_TOKEN": "stored-token"})
    edits = [Claim("VAULT_TOKEN", DEMO), Set("VAULT_TOKEN", "not:a:token", line(2)), Set("MCP_PATH", "sql", line(3))]

    edited = resolve(env, edits)

    assert edited.env.server == {"VAULT_TOKEN": "stored-token"}
    assert edited.refusals == [
        f"{FILE}, line 3 (MCP_PATH): it must be a URL path that starts with /, without spaces, query or fragment."
    ]


def test_a_claim_loses_to_a_higher_source() -> None:
    edited = resolve(environment(), [Set("MCP_PATH", "/sql", SET), Claim("mcp_path", DEMO)])

    assert edited.env.server == {"MCP_PATH": "/sql"}


def test_a_claim_of_a_host_setting_skips_a_file_line_of_it_and_leaves_the_server_key_of_that_name() -> None:
    edits = [
        Claim("vault_ca_file", DEMO, host=True),
        SetHost("vault_ca_file", "/no/such/ca.pem", line(2)),
        Set("vault_ca_file", "server-side", line(3)),
    ]

    edited = resolve(environment(), edits)

    assert edited.env.host.vault_ca_file is None
    assert edited.refusals == [
        f"{FILE}, line 3 (vault_ca_file): it is a host setting, not a server setting; set it with --vault-ca."
    ]


def test_a_refuse_edit_is_always_reported_in_its_place_naming_its_origin() -> None:
    stack_port = Origin(OriginKind.HOST_FLAG, "--stack-port")
    edits = [
        Refuse("the line is not a KEY=VALUE setting.", line(1)),
        Set("MCP_PATH", "sql", SET),
        Refuse("it needs --stack-host; the ports belong to a demo stack on another machine.", stack_port),
    ]

    edited = resolve(environment(), edits)

    assert edited.refusals == [
        f"{FILE}, line 1: the line is not a KEY=VALUE setting.",
        "--set MCP_PATH: it must be a URL path that starts with /, without spaces, query or fragment.",
        "--stack-port: it needs --stack-host; the ports belong to a demo stack on another machine.",
    ]


# --- The result -------------------------------------------------------------------------------------------------


def test_the_cross_field_rules_judge_the_result_once_after_the_edit_refusals() -> None:
    edits = [
        Set("MCP_CREDENTIAL_PROVIDER", "none", line(1)),
        Fill("MCP_CREDENTIAL_PROVIDER", "hashicorp", DEMO),
        Set("MCP_PATH", "sql", SET),
        AskForMissingSecrets(SWEEP),
    ]

    edited = resolve(environment(), edits)

    assert edited.refusals == [
        "--set MCP_PATH: it must be a URL path that starts with /, without spaces, query or fragment.",
        "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth: Vault holds a CONNX login per signed-in caller. Choose "
        "MCP_OAUTH_PROVIDER, or set MCP_CREDENTIAL_PROVIDER to none.",
    ]
    assert edited.warnings == [
        f"{FILE}: CDD_PASSWORD is not in the file; start asks for it (or store it with configure --set "
        "CDD_PASSWORD=...).",
        f"{FILE}: VAULT_TOKEN is not in the file; start asks for it (or store it with configure --set "
        "VAULT_TOKEN=...).",
        LICENCE_WARNING,
    ]


def test_the_edit_refusals_are_kept_apart_from_the_cross_field_rules() -> None:
    edits = [Set("MCP_CREDENTIAL_PROVIDER", "hashicorp", SET), Set("MCP_PATH", "sql", SET)]

    edited = resolve(environment(), edits)

    assert edited.edit_refusals == [
        "--set MCP_PATH: it must be a URL path that starts with /, without spaces, query or fragment."
    ]
    assert edited.refusals == [
        *edited.edit_refusals,
        "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth: Vault holds a CONNX login per signed-in caller. Choose "
        "MCP_OAUTH_PROVIDER, or set MCP_CREDENTIAL_PROVIDER to none.",
    ]


def test_the_values_still_missing_are_those_of_the_result_an_ask_at_start_secret_never_among_them(
    tmp_path: Path,
) -> None:
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")
    env = environment({"CDD_USERNAME": "reader"})

    edited = resolve(env, [AskAtStart("CDD_PASSWORD", ASK)])
    complete = resolve(env, [AskAtStart("CDD_PASSWORD", ASK), SetHost("cdd_file", str(cdd), CDD)])

    assert [(m.name, m.label, m.hint, m.host) for m in edited.missing] == [("cdd_file", "--cdd", "--cdd ...", True)]
    assert complete.missing == []


def test_changed_lists_each_key_whose_value_differs_server_keys_sorted_then_host_settings_then_new_marks(
    tmp_path: Path,
) -> None:
    cdd = tmp_path / "sales.cdd"
    cdd.write_bytes(b"CDD")
    env = environment({"MCP_PATH": "/a", "CDD_PASSWORD": "pw"}, HostSettings(published_port=9100))
    edits = [
        Set("MCP_PATH", "/a", SET),
        Set("CONNX_HOST", "engine", SET),
        Set("CDD_USERNAME", "reader", line(1)),
        SetHost("published_port", "9100", PORT),
        SetHost("cert_dir", str(tmp_path), Origin(OriginKind.HOST_FLAG, "--certs")),
        SetHost("cdd_file", str(cdd), CDD),
        AskAtStart("MCP_TLS_KEY_PASSWORD", ASK),
        AskAtStart("CDD_PASSWORD", ASK),
    ]

    edited = resolve(env, edits)

    assert edited.changed == [
        "CDD_PASSWORD",
        "CDD_USERNAME",
        "CONNX_HOST",
        "cdd_file",
        "cert_dir",
        "MCP_TLS_KEY_PASSWORD",
    ]


def test_changed_is_empty_when_every_edit_gives_the_value_already_stored() -> None:
    env = environment({"MCP_PATH": "/a"}, HostSettings(published_port=9100))

    edited = resolve(env, [Set("MCP_PATH", "/a", SET), SetHost("published_port", "9100", PORT)])

    assert edited.changed == []


def test_changed_lists_a_secret_no_longer_asked_for_at_start() -> None:
    env = environment(host=HostSettings(ask_at_start=["MCP_TLS_KEY_PASSWORD"]))

    edited = resolve(env, [Unset("MCP_TLS_KEY_PASSWORD", UNSET)])

    assert edited.changed == ["MCP_TLS_KEY_PASSWORD"]


def test_resolve_never_changes_the_environment_it_is_given(tmp_path: Path) -> None:
    host = HostSettings(published_port=9100, ask_at_start=["MCP_TLS_KEY_PASSWORD"], unknown={"later": 1})
    env = environment({"CDD_PASSWORD": "pw", "MCP_PATH": "/a"}, host)
    snapshot = copy.deepcopy(env)
    edits = [
        Set("MCP_PATH", "/b", SET),
        Unset("MCP_TLS_KEY_PASSWORD", UNSET),
        SetHost("published_port", "9200", PORT),
        SetHost("vault_ca_file", str(tmp_path / "ca.pem"), DEMO),
        Fill("MCP_OAUTH_CLIENT_ID", "demo-client", DEMO),
        AskAtStart("CDD_PASSWORD", ASK),
        AskForMissingSecrets(SWEEP),
    ]

    edited = resolve(env, edits)

    assert env == snapshot
    assert edited.env != snapshot


def test_the_edit_refusals_and_the_rule_refusals_never_overlap_and_together_are_the_refusals() -> None:
    edits = [Set("MCP_CREDENTIAL_PROVIDER", "hashicorp", SET), Set("MCP_PATH", "sql", SET)]

    edited = resolve(environment(), edits)

    assert edited.rule_refusals == [
        "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth: Vault holds a CONNX login per signed-in caller. Choose "
        "MCP_OAUTH_PROVIDER, or set MCP_CREDENTIAL_PROVIDER to none."
    ]
    assert not set(edited.edit_refusals) & set(edited.rule_refusals)
    assert edited.refusals == [*edited.edit_refusals, *edited.rule_refusals]
