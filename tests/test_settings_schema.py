#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_settings_schema.py - The settings schema table and the server settings file format.

Description:
    Unit tests for invariants every row added to the schema must keep (dependencies name known
    keys, explicit empty is explained where allowed, the known secrets are secrets) and for the
    server settings file round trip.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import random

import pytest

from connx_mcp.environment import parse_env, render_env
from connx_mcp.settings import (
    HIDDEN,
    HOST_SCHEMA,
    SCHEMA,
    DependsOn,
    Group,
    HostSettings,
    Kind,
    Setting,
    mask,
    missing,
    refused_key,
)


@pytest.mark.parametrize("key", list(SCHEMA))
def test_every_row_is_consistent(key: str) -> None:
    setting = SCHEMA[key]

    assert setting.key == key
    if setting.depends_on is not None:
        assert setting.depends_on.key in SCHEMA
    if setting.empty_ok:
        assert setting.empty_meaning
    if setting.default is not None:
        assert setting.problem(setting.default) is None


def test_the_password_is_a_secret_and_the_username_is_not() -> None:
    assert mask({"CDD_PASSWORD": "x", "CDD_USERNAME": "mcp"}) == {"CDD_PASSWORD": HIDDEN, "CDD_USERNAME": "mcp"}


@pytest.mark.parametrize(
    "value",
    ["plain", "", "with space", "it's", 'say "hi"', "$HOME", "back\\slash", "a=b#c", 'it\'s $x "y" \\z'],
)
def test_a_value_survives_the_server_settings_file(value: str) -> None:
    assert parse_env(render_env({"K": value})) == {"K": value}


# Every character that has a meaning in an env file, quoting or shell syntax, plus whitespace and non-ASCII.
TRICKY = "aZ09 \t'\"\\$#=;{}`!*?~&|<>()[],.:/@+-_%^é€\u00a0\u200b"


def _values() -> list[str]:
    """Every value of up to two tricky characters, plus seeded random longer ones."""
    rng = random.Random(2026)
    short = ["", *TRICKY, *(a + b for a in TRICKY for b in TRICKY)]
    return short + ["".join(rng.choices(TRICKY, k=rng.randint(3, 24))) for _ in range(2000)]


def test_every_value_without_a_line_break_survives_the_server_settings_file() -> None:
    failures = [v for v in _values() if parse_env(render_env({"K": v, "NEXT": "x"})) != {"K": v, "NEXT": "x"}]

    assert failures == []


@pytest.mark.parametrize("brk", ["\n", "\r", "\u2028", "\x00"])
def test_the_server_settings_file_never_holds_a_line_break_in_a_value(brk: str) -> None:
    with pytest.raises(ValueError, match="K"):
        render_env({"K": f"a{brk}B=evil"})


@pytest.fixture
def mode_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch and a row that is active in two modes but required only in one, as the OAuth audience is."""
    modes = ("none", "keycloak", "proxy")
    monkeypatch.setitem(
        SCHEMA, "T_MODE", Setting("T_MODE", Group.LISTENER, Kind.CHOICE, "mode", default="none", choices=modes)
    )
    monkeypatch.setitem(
        SCHEMA,
        "T_AUDIENCE",
        Setting(
            "T_AUDIENCE",
            Group.LISTENER,
            Kind.TEXT,
            "audience",
            depends_on=DependsOn("T_MODE", frozenset({"keycloak", "proxy"})),
            required_when=DependsOn("T_MODE", frozenset({"keycloak"})),
        ),
    )


@pytest.mark.usefixtures("mode_rows")
@pytest.mark.parametrize(("mode", "required"), [("none", False), ("proxy", False), ("keycloak", True)])
def test_a_setting_can_be_active_in_one_mode_and_required_only_in_another(mode: str, required: bool) -> None:
    values = {"CDD_USERNAME": "mcp", "CDD_PASSWORD": "", "T_MODE": mode}

    assert ("T_AUDIENCE" in missing(values)) is required
    assert SCHEMA["T_AUDIENCE"].is_required(values) is required


@pytest.mark.parametrize("name", list(HOST_SCHEMA))
def test_every_host_setting_row_is_consistent(name: str) -> None:
    row = HOST_SCHEMA[name]

    assert row.name == name
    assert row.summary
    assert (row.flag is None) != (row.set_by is None)  # a refusal names one way to set it
    if row.required:
        assert row.flag is not None and row.flag.startswith("--")
    if row.compose_var is not None:
        assert refused_key(row.compose_var) is not None
        assert row.compose_var not in SCHEMA


def test_host_settings_round_trip_through_json_with_unknown_keys() -> None:
    data = {"cdd_file": "/srv/a.cdd", "published_port": 8443, "image_digest": "r@sha256:1", "later": [1]}

    assert HostSettings.from_json(data).to_json() == data


def test_a_secret_supplied_at_start_is_not_missing() -> None:
    assert missing({"CDD_USERNAME": "mcp"}) == ["CDD_PASSWORD"]
    assert missing({"CDD_USERNAME": "mcp"}, supplied_at_start={"CDD_PASSWORD"}) == []
