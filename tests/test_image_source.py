#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_image_source.py - The administrator points connx-mcp-ce at another registry, image name or tag.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: CONNX_MCP_REGISTRY,
    CONNX_MCP_IMAGE and CONNX_MCP_IMAGE_TAG each replace their own part of the default image that
    `download`, `update`, `status`, `guide` and `login` use, read at each call; an environment's own
    `image` host setting (`configure --image`) wins over them, and a pinned digest never moves until
    `update`. A wrongly shaped variable is refused with a hint naming the variable the value belongs
    in. A pre-release or unlabelled image is warned about, never refused.

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
from pathlib import Path

import pytest

from tests.conftest import Cli, Machine
from tests.test_import_export import move_to_a_fresh_machine
from tests.test_lifecycle import (
    DIGEST,
    IMAGE,
    IMAGE_FORMAT,
    cdd,  # noqa: F401  (fixture)
    comes_up,
    configure,
    host_settings,
    image_found,
    image_id,
    inspect_args,
    serving,
    started,
    state,
)
from tests.test_remove import project_containers, project_volumes

BUILT_IN_REGISTRY = "softwareagcr.azurecr.io"
INTERNAL = "registry.internal.example:5000"
INTERNAL_IMAGE = f"{INTERNAL}/connx-mcp-server-ce:15.0.0-rc.2"
OTHER_IMAGE = "registry.other.example/team/connx-mcp-server-ce:15.0.4"
OTHER_DIGEST = "registry.other.example/team/connx-mcp-server-ce@sha256:" + "c" * 64
# The variables that name INTERNAL_IMAGE and OTHER_IMAGE.
INTERNAL_VARIABLES = {"CONNX_MCP_REGISTRY": INTERNAL, "CONNX_MCP_IMAGE_TAG": "15.0.0-rc.2"}
OTHER_VARIABLES = {"CONNX_MCP_REGISTRY": "registry.other.example/team", "CONNX_MCP_IMAGE_TAG": "15.0.4"}
TOKEN = "tok-for-tests-only-0123456789"  # disclosure-scan: allow


def found(cli: Cli, ref: str, digest: str, version: str | None = "15.0.4.26100") -> None:
    """`ref` (and its digest) resolve to a local image."""
    labels = {"org.opencontainers.image.version": version} if version else None
    facts = json.dumps({"id": image_id(digest), "digests": [digest], "labels": labels})
    for name in (ref, digest):
        cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, name], stdout=facts + "\n")


def pulled(cli: Cli) -> list[str]:
    return [c[1] for c in cli.docker.calls if c[0] == "pull"]


def set_variables(monkeypatch: pytest.MonkeyPatch, variables: dict[str, str]) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


# --- the default image follows the variables ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("variables", "reference"),
    [
        ({"CONNX_MCP_REGISTRY": INTERNAL}, f"{INTERNAL}/connx-mcp-server-ce:15.0"),
        ({"CONNX_MCP_IMAGE": "connx-mcp-server-ee"}, f"{BUILT_IN_REGISTRY}/connx-mcp-server-ee:15.0"),
        ({"CONNX_MCP_IMAGE_TAG": "15.0.0-rc.2"}, f"{BUILT_IN_REGISTRY}/connx-mcp-server-ce:15.0.0-rc.2"),
        (
            {"CONNX_MCP_REGISTRY": INTERNAL, "CONNX_MCP_IMAGE": "connx-mcp-server-ee", "CONNX_MCP_IMAGE_TAG": "main"},
            f"{INTERNAL}/connx-mcp-server-ee:main",
        ),
        (
            {
                "CONNX_MCP_IMAGE": "connx-mcp-server-ce",
                "CONNX_MCP_REGISTRY": "registry.example.com",
                "CONNX_MCP_IMAGE_TAG": "main",
            },
            "registry.example.com/connx-mcp-server-ce:main",
        ),
        ({"CONNX_MCP_IMAGE": "connx/connx-mcp-server-ce"}, f"{BUILT_IN_REGISTRY}/connx/connx-mcp-server-ce:15.0"),
        ({"CONNX_MCP_IMAGE": "my.image"}, f"{BUILT_IN_REGISTRY}/my.image:15.0"),
        ({"CONNX_MCP_REGISTRY": f"{INTERNAL}/connx"}, f"{INTERNAL}/connx/connx-mcp-server-ce:15.0"),
        (
            {"CONNX_MCP_REGISTRY": INTERNAL, "CONNX_MCP_IMAGE": "connx/connx-mcp-server-ce"},
            f"{INTERNAL}/connx/connx-mcp-server-ce:15.0",
        ),
    ],
)
def test_each_variable_replaces_only_its_own_part_of_the_image_download_pulls(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    reference: str,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, variables)
    cli.docker.reply(["pull", reference])
    found(cli, reference, reference.rsplit(":", 1)[0] + "@sha256:" + "d" * 64)

    code, body = cli.json("download")

    assert code == 0, body
    assert pulled(cli) == [reference]


def test_an_empty_variable_counts_as_unset(cli: Cli, cdd: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    configure(cli, cdd)
    for name in ("CONNX_MCP_IMAGE", "CONNX_MCP_REGISTRY", "CONNX_MCP_IMAGE_TAG", "CONNX_MCP_IMAGE_VERSION"):
        monkeypatch.setenv(name, "")
    cli.docker.reply(["pull", IMAGE])
    image_found(cli)

    code, body = cli.json("download")

    assert code == 0, body
    assert pulled(cli) == [IMAGE]


def test_an_environments_own_image_wins_over_the_variables(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    monkeypatch.setenv("CONNX_MCP_REGISTRY", INTERNAL)
    cli.docker.reply(["pull", OTHER_IMAGE])
    found(cli, OTHER_IMAGE, OTHER_DIGEST)

    code, body = cli.json("download")

    assert code == 0, body
    assert pulled(cli) == [OTHER_IMAGE]


@pytest.mark.parametrize("command", ["download", "status"])
def test_the_renamed_tag_variable_is_refused_even_for_an_environment_with_its_own_image(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    monkeypatch.setenv("CONNX_MCP_IMAGE_VERSION", "main")

    code, body = cli.json(command)

    assert code == 47, body
    assert "renamed to `CONNX_MCP_IMAGE_TAG`" in body["error"]["message"]
    assert body["error"]["next"] == "unset CONNX_MCP_IMAGE_VERSION"
    assert not pulled(cli)


@pytest.mark.parametrize(
    ("variable", "value", "says", "fix"),
    [
        (
            "CONNX_MCP_IMAGE",
            "registry.example.com/connx-mcp-server-ce:main",
            ["registry registry.example.com belongs in CONNX_MCP_REGISTRY", "tag main belongs in CONNX_MCP_IMAGE_TAG"],
            "export CONNX_MCP_REGISTRY=registry.example.com CONNX_MCP_IMAGE=connx-mcp-server-ce"
            " CONNX_MCP_IMAGE_TAG=main",
        ),
        (
            "CONNX_MCP_IMAGE",
            "localhost/connx-mcp-server-ce",
            ["registry localhost belongs in CONNX_MCP_REGISTRY"],
            "export CONNX_MCP_REGISTRY=localhost CONNX_MCP_IMAGE=connx-mcp-server-ce",
        ),
        (
            "CONNX_MCP_IMAGE",
            "registry.example:5000",
            ["registry registry.example:5000 belongs in CONNX_MCP_REGISTRY"],
            "export CONNX_MCP_REGISTRY=registry.example:5000 && unset CONNX_MCP_IMAGE",
        ),
        (
            "CONNX_MCP_IMAGE",
            "connx/connx-mcp-server-ce:15.0.4",
            ["tag 15.0.4 belongs in CONNX_MCP_IMAGE_TAG"],
            "export CONNX_MCP_IMAGE=connx/connx-mcp-server-ce CONNX_MCP_IMAGE_TAG=15.0.4",
        ),
        (
            "CONNX_MCP_IMAGE",
            f"connx-mcp-server-ce@sha256:{'e' * 64}",
            ["digest is never set", "connx-mcp-ce start"],
            "export CONNX_MCP_IMAGE=connx-mcp-server-ce",
        ),
        ("CONNX_MCP_IMAGE", "Not An Image", ["an image name"], "unset CONNX_MCP_IMAGE"),
        (
            "CONNX_MCP_REGISTRY",
            "https://registry.example.com/",
            ["without a scheme or a trailing /"],
            "export CONNX_MCP_REGISTRY=registry.example.com",
        ),
        (
            "CONNX_MCP_REGISTRY",
            "registry.example.com/connx/",
            ["without a scheme or a trailing /"],
            "export CONNX_MCP_REGISTRY=registry.example.com/connx",
        ),
        ("CONNX_MCP_REGISTRY", "myregistry", ["names no registry host"], "unset CONNX_MCP_REGISTRY"),
        (
            "CONNX_MCP_REGISTRY",
            "registry.example.com/Connx",
            ["its path Connx after the host registry.example.com is not a repository path"],
            "unset CONNX_MCP_REGISTRY",
        ),
        ("CONNX_MCP_IMAGE_TAG", "15 0", ["an image tag"], "unset CONNX_MCP_IMAGE_TAG"),
        ("CONNX_MCP_IMAGE_VERSION", "main", ["renamed to `CONNX_MCP_IMAGE_TAG`"], "unset CONNX_MCP_IMAGE_VERSION"),
    ],
)
def test_a_wrongly_shaped_variable_is_refused_with_the_variable_it_belongs_in(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    says: list[str],
    fix: str,
) -> None:
    configure(cli, cdd)
    monkeypatch.setenv(variable, value)

    code, body = cli.json("download")

    assert code == 47, body
    assert body["error"]["code"] == "image_variable_invalid"
    assert variable in body["error"]["message"]
    for words in says:
        assert words in body["error"]["message"]
    assert body["error"]["next"] == fix
    assert not pulled(cli)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CONNX_MCP_REGISTRY", "https://mcp-admin:s3cret-pw@registry.example.com/"),
        ("CONNX_MCP_REGISTRY", "mcp-admin:s3cret-pw@registry.example.com"),
        ("CONNX_MCP_IMAGE", "mcp-admin:s3cret-pw@registry.example.com/connx-mcp-server-ce"),
        ("CONNX_MCP_IMAGE_TAG", "mcp-admin:s3cret-pw@main"),
    ],
)
def test_a_refused_variable_that_may_hold_a_credential_is_never_repeated(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    configure(cli, cdd)
    monkeypatch.setenv(variable, value)

    code, body = cli.json("download")
    human = cli("download")

    assert code == 47, body
    assert variable in body["error"]["message"]
    assert "may hold a credential" in body["error"]["message"]
    assert body["error"]["next"] == f"unset {variable}"
    for shown in (json.dumps(body), human.stdout + human.stderr):
        assert "s3cret-pw" not in shown
        assert "mcp-admin" not in shown
    assert not pulled(cli)


@pytest.mark.parametrize("host", ["--x.example", "-registry.example:5000", "-x:5000"])
def test_a_registry_host_that_starts_with_a_dash_never_reaches_docker(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    configure(cli, cdd)
    monkeypatch.setenv("CONNX_MCP_REGISTRY", host)

    download = cli("download")
    monkeypatch.delenv("CONNX_MCP_REGISTRY")
    login = cli("login", f"--registry={host}", "--username", "mcp-admin", "--password-stdin", input=TOKEN)
    configure_code, _ = cli.json("configure", "default", "--no-input", "--image", f"{host}/connx-mcp-server-ce:15.0")

    assert (download.exit_code, login.exit_code) == (47, 2), download.output + login.output
    assert configure_code != 0
    assert not [c for c in cli.docker.calls if c[0] in ("pull", "login")]
    assert "image" not in host_settings(machine)


def test_a_variable_change_applies_at_the_next_call(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    monkeypatch.setenv("CONNX_MCP_IMAGE_TAG", "15 0")
    assert cli.json("download")[0] == 47
    monkeypatch.setenv("CONNX_MCP_IMAGE_TAG", "main")
    reference = f"{BUILT_IN_REGISTRY}/connx-mcp-server-ce:main"
    cli.docker.reply(["pull", reference])
    found(cli, reference, f"{BUILT_IN_REGISTRY}/connx-mcp-server-ce@sha256:" + "d" * 64)

    code, body = cli.json("download")

    assert code == 0, body
    assert pulled(cli) == [reference]


# --- pins and update ----------------------------------------------------------------------------------------


def test_a_changed_variable_never_moves_a_pinned_environment_on_start(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    set_variables(monkeypatch, OTHER_VARIABLES)
    comes_up(cli, machine, state())
    serving(cli)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, OTHER_IMAGE], 1, stderr=f"Error: No such image: {OTHER_IMAGE}"
    )

    code, body = cli.json("start")

    assert code == 0, body
    assert body["result"]["image"] == DIGEST
    assert host_settings(machine)["image_digest"] == DIGEST
    assert not pulled(cli)
    assert DIGEST in (machine.base / "default" / "compose.yml").read_text(encoding="utf-8")


def test_the_first_start_records_the_reference_it_pinned(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    started(cli, machine, cdd)

    assert host_settings(machine)["image_pinned_reference"] == IMAGE


def test_update_moves_the_pin_to_the_image_the_variables_now_name(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    set_variables(monkeypatch, OTHER_VARIABLES)
    cli.docker.reply(["pull", OTHER_IMAGE])
    found(cli, OTHER_IMAGE, OTHER_DIGEST)

    code, body = cli.json("update")

    assert code == 0, body
    assert pulled(cli) == [OTHER_IMAGE]
    assert host_settings(machine)["image_digest"] == OTHER_DIGEST
    assert host_settings(machine)["image_pinned_reference"] == OTHER_IMAGE


# --- every image error names where each part came from -------------------------------------------------------

INTERNAL_PARTS = (
    f"registry {INTERNAL} (CONNX_MCP_REGISTRY), name connx-mcp-server-ce (built-in default), "
    "tag 15.0.0-rc.2 (CONNX_MCP_IMAGE_TAG)"
)
INTERNAL_LINE = f"image: {INTERNAL_PARTS}"
OWN_LINE = f"image: {OTHER_IMAGE} (environment 'default', configure --image)"
REFUSED = "unauthorized: authentication required\n"


def last_line(body: dict) -> str:
    return body["error"]["message"].splitlines()[-1]


def test_a_refused_pull_names_each_parts_source_under_the_registry_hint(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(["pull", INTERNAL_IMAGE], 1, stderr=REFUSED)

    code, body = cli.json("download")

    assert code == 44, body
    assert last_line(body) == INTERNAL_LINE
    assert body["error"]["message"].count("Empower or Software AG support") == 1


def test_a_pull_that_fails_otherwise_names_each_parts_source(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(["pull", INTERNAL_IMAGE], 1, stderr="manifest unknown\n")

    code, body = cli.json("download")

    assert code == 45, body
    assert "manifest unknown" in body["error"]["message"]
    assert last_line(body) == INTERNAL_LINE


def test_an_image_not_on_this_machine_names_each_parts_source(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE], 1, stderr=f"No such image: {INTERNAL_IMAGE}"
    )

    code, body = cli.json("start")

    assert code == 30, body
    assert last_line(body) == INTERNAL_LINE


def test_an_image_older_than_supported_names_each_parts_source(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    found(cli, INTERNAL_IMAGE, f"{INTERNAL}/connx-mcp-server-ce@sha256:" + "d" * 64, version="14.9.2.25300")

    code, body = cli.json("start")

    assert code == 46, body
    assert last_line(body) == INTERNAL_LINE


def test_update_to_an_image_older_than_supported_names_each_parts_source(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(["pull", INTERNAL_IMAGE])
    found(cli, INTERNAL_IMAGE, f"{INTERNAL}/connx-mcp-server-ce@sha256:" + "d" * 64, version="14.9.2.25300")

    code, body = cli.json("update")

    assert code == 46, body
    assert last_line(body) == INTERNAL_LINE


def test_start_on_a_pin_older_than_supported_names_the_pin_and_what_update_would_download(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    path = machine.base / "default" / "host.json"
    path.write_text(json.dumps({**host_settings(machine), "image_version": "14.9.2.25300"}), encoding="utf-8")
    set_variables(monkeypatch, INTERNAL_VARIABLES)

    code, body = cli.json("start")

    assert code == 46, body
    assert body["error"]["message"].splitlines()[-2:] == [
        f"image: {DIGEST} (pinned in environment 'default' at an earlier start or update)",
        f"update downloads: {INTERNAL_PARTS}",
    ]
    assert body["error"]["next"] == "connx-mcp-ce update default"


def test_an_error_for_an_environments_own_image_names_the_environment_as_its_one_source(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    monkeypatch.setenv("CONNX_MCP_REGISTRY", INTERNAL)
    cli.docker.reply(["pull", OTHER_IMAGE], 1, stderr="manifest unknown\n")

    code, body = cli.json("download")

    assert code == 45, body
    assert last_line(body) == OWN_LINE


@pytest.mark.parametrize(
    ("variables", "flags", "registry", "line"),
    [
        ({}, (), BUILT_IN_REGISTRY, f"registry: {BUILT_IN_REGISTRY} (built-in default)"),
        (
            {"CONNX_MCP_REGISTRY": f"{INTERNAL}/connx"},
            (),
            INTERNAL,
            f"registry: {INTERNAL} (CONNX_MCP_REGISTRY)",
        ),
        (
            {"CONNX_MCP_REGISTRY": INTERNAL},
            ("--registry", "registry.example.com"),
            "registry.example.com",
            "registry: registry.example.com (--registry)",
        ),
    ],
)
def test_a_refused_login_names_the_registry_it_tried_and_its_source(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    flags: tuple[str, ...],
    registry: str,
    line: str,
) -> None:
    set_variables(monkeypatch, variables)
    cli.docker.reply(
        ["login", registry, "--username", "mcp-admin", "--password-stdin"], 1, stderr=f"unauthorized: {TOKEN}\n"
    )

    code, body = cli.json("login", *flags, "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 44, body
    assert last_line(body) == line
    assert TOKEN not in json.dumps(body)


def test_guides_no_image_step_names_each_image_part_and_its_source(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE], 1, stderr=f"No such image: {INTERNAL_IMAGE}"
    )

    code, body = cli.json("guide")

    assert code == 0, body
    assert body["result"]["stage"] == "no_image"
    assert body["result"]["reason"].splitlines()[-1] == INTERNAL_LINE


@pytest.mark.parametrize("tag", ["", "15 0"], ids=["no-variable", "ignored-invalid-variable"])
def test_guides_sign_in_step_for_an_environments_own_image_names_its_registry_whatever_the_variables_hold(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
) -> None:
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    monkeypatch.setenv("CONNX_MCP_IMAGE_TAG", tag)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, OTHER_IMAGE], 1, stderr=f"No such image: {OTHER_IMAGE}"
    )

    code, body = cli.json("guide")

    assert code == 0, body
    assert body["result"]["stage"] == "no_image"
    assert body["result"]["next"] == "connx-mcp-ce login --registry registry.other.example"


# --- status, guide, login -----------------------------------------------------------------------------------


BUILT_IN = "built-in default"
OWN = "environment (configure --image)"


@pytest.mark.parametrize(
    ("variables", "flags", "reference", "source", "shown"),
    [
        (
            {},
            (),
            IMAGE,
            {"registry": BUILT_IN, "name": BUILT_IN, "tag": BUILT_IN},
            f"registry {BUILT_IN_REGISTRY} (built-in default), name connx-mcp-server-ce (built-in default), "
            "tag 15.0 (built-in default)",
        ),
        (
            {"CONNX_MCP_IMAGE": "connx-mcp-server-ee"},
            (),
            IMAGE.replace("-ce", "-ee"),
            {"registry": BUILT_IN, "name": "CONNX_MCP_IMAGE", "tag": BUILT_IN},
            "name connx-mcp-server-ee (CONNX_MCP_IMAGE)",
        ),
        (
            INTERNAL_VARIABLES,
            (),
            INTERNAL_IMAGE,
            {"registry": "CONNX_MCP_REGISTRY", "name": BUILT_IN, "tag": "CONNX_MCP_IMAGE_TAG"},
            INTERNAL_PARTS,
        ),
        (
            INTERNAL_VARIABLES,
            ("--image", OTHER_IMAGE),
            OTHER_IMAGE,
            {"registry": OWN, "name": OWN, "tag": OWN},
            f"{OTHER_IMAGE} (environment 'default', configure --image)",
        ),
    ],
)
def test_status_shows_the_reference_and_where_each_part_came_from(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    flags: tuple[str, ...],
    reference: str,
    source: dict[str, str],
    shown: str,
) -> None:
    configure(cli, cdd, "default", *flags)
    set_variables(monkeypatch, variables)
    cli.docker.reply(
        ["container", "inspect", "--format", _state_format(), "connx-mcp-ce-default"],
        returncode=1,
        stderr="Error: No such object: connx-mcp-ce-default\n",
    )

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["reference"] == reference
    assert body["result"]["image"]["source"] == source
    assert shown in " ".join(cli("status", "default").stdout.split())


def test_human_status_of_a_pinned_environment_shows_what_update_downloads_and_where_each_part_came_from(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE], 1, stderr=f"No such image: {INTERNAL_IMAGE}"
    )

    lines = [line.strip() for line in cli("status", "default").stdout.splitlines()]

    at = lines.index(f"update downloads {INTERNAL_IMAGE}")
    assert lines[at - 1] == DIGEST
    assert lines[at + 1] == f"from {INTERNAL_PARTS}"


@pytest.mark.parametrize(
    "reference",
    [
        f"{INTERNAL}/connx/connx-mcp-server-ce:15.0.4",
        OTHER_DIGEST,
        f"localhost:5000/connx-mcp-server-ce:15.0.4@sha256:{'e' * 64}",
    ],
)
def test_status_shows_an_environments_own_full_reference_exactly_as_given(
    cli: Cli,
    cdd: Path,  # noqa: F811
    reference: str,
) -> None:
    configure(cli, cdd, "default", "--image", reference)
    cli.docker.reply(
        ["container", "inspect", "--format", _state_format(), "connx-mcp-ce-default"],
        returncode=1,
        stderr="Error: No such object: connx-mcp-ce-default\n",
    )

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["reference"] == reference


def test_login_signs_in_to_the_registry_of_the_effective_default_image(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONNX_MCP_REGISTRY", INTERNAL)
    args = ["login", INTERNAL, "--username", "mcp-admin", "--password-stdin"]
    cli.docker.reply(args, stdout="Login Succeeded\n")

    code, body = cli.json("login", "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 0, body
    assert body["result"]["registry"] == INTERNAL


def test_login_signs_in_to_the_registry_host_without_its_path(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNX_MCP_REGISTRY", f"{INTERNAL}/connx")
    args = ["login", INTERNAL, "--username", "mcp-admin", "--password-stdin"]
    cli.docker.reply(args, stdout="Login Succeeded\n")

    code, body = cli.json("login", "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 0, body
    assert body["result"]["registry"] == INTERNAL
    assert [c[1] for c in cli.docker.calls if c[0] == "login"] == [INTERNAL]


@pytest.mark.parametrize(
    "variable",
    [("CONNX_MCP_REGISTRY", "user:pw@registry.example.com"), ("CONNX_MCP_IMAGE_TAG", "not a tag"),
     ("CONNX_MCP_IMAGE_VERSION", "15.0")],
)  # fmt: skip
def test_login_with_a_registry_never_reads_the_image_variables(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, variable: tuple[str, str]
) -> None:
    monkeypatch.setenv(*variable)
    args = ["login", "registry.example.com", "--username", "mcp-admin", "--password-stdin"]
    cli.docker.reply(args, stdout="Login Succeeded\n")

    code, body = cli.json(
        "login", "--registry", "registry.example.com", "--username", "mcp-admin", "--password-stdin", input=TOKEN
    )

    assert code == 0, body
    assert body["result"]["registry"] == "registry.example.com"


@pytest.mark.parametrize("given", ["mcp-admin:s3cret-pw@registry.example.com", "registry.example.com/connx", "a b"])
def test_login_refuses_a_registry_that_is_not_a_registry_host_without_echoing_it(cli: Cli, given: str) -> None:
    code, body = cli.json("login", "--registry", given, "--username", "mcp-admin", "--password-stdin", input=TOKEN)
    human = cli("login", "--registry", given, "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 2, body
    assert body["error"]["code"] == "usage"
    assert "--registry" in body["error"]["message"]
    assert given not in json.dumps(body)
    assert given not in human.stdout + human.stderr
    assert not [c for c in cli.docker.calls if c[0] == "login"]


def test_guide_names_plain_login_for_the_effective_default_registry(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE],
        1,
        stderr=f"Error: No such image: {INTERNAL_IMAGE}",
    )

    code, body = cli.json("guide")

    assert code == 0, body
    assert body["result"]["stage"] == "no_image"
    assert body["result"]["next"] == "connx-mcp-ce login"
    assert INTERNAL in body["result"]["reason"]


def test_the_root_help_lists_the_image_variables_and_their_precedence_once(cli: Cli) -> None:
    result = cli("--help")

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    for name in ("CONNX_MCP_REGISTRY", "CONNX_MCP_IMAGE ", "CONNX_MCP_IMAGE_TAG", "MCP_ENVIRONMENT_PATH"):
        assert name in text
    assert "CONNX_MCP_IMAGE_VERSION" not in text
    for example in (
        "registry.example:5000/connx",
        "connx/connx-mcp-server-ce",
        "15.0.1-rc.2",
        "<registry>/<name>:<tag>",
    ):
        assert example in text
    assert text.count("configure --image) has priority over them") == 1
    assert text.count("first start or at `connx-mcp-ce update`") == 1
    assert "IMAGE_VARIABLE_INVALID" not in text
    codes = " ".join(cli("exit-codes").stdout.split())
    assert "CONNX_MCP_REGISTRY, CONNX_MCP_IMAGE or CONNX_MCP_IMAGE_TAG is invalid" in codes


@pytest.mark.parametrize("command", ["login", "download"])
def test_login_and_download_help_name_the_three_image_variables(cli: Cli, command: str) -> None:
    text = " ".join(cli(command, "--help").stdout.split())

    for name in ("CONNX_MCP_REGISTRY", "CONNX_MCP_IMAGE ", "CONNX_MCP_IMAGE_TAG"):
        assert name in text, command


def test_login_help_says_the_registry_is_a_host_alone_from_the_registry_variable(cli: Cli) -> None:
    text = " ".join(cli("login", "--help").stdout.split())

    assert "with no path, user or password" in text
    assert "the host of CONNX_MCP_REGISTRY, else the built-in registry" in text


# --- configure --image --------------------------------------------------------------------------------------


def test_configure_image_sets_the_environments_image(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)

    code, body = cli.json("configure", "default", "--no-input", "--image", OTHER_IMAGE)

    assert code == 0, body
    assert host_settings(machine)["image"] == OTHER_IMAGE


def test_configure_refuses_an_invalid_image(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)

    code, body = cli.json("configure", "default", "--no-input", "--image", "Not An Image")

    assert code != 0
    assert "--image" in body["error"]["message"]
    assert "image" not in host_settings(machine)


@pytest.mark.parametrize("bare", ["connx-mcp-server-ce:15.0", "team/connx-mcp-server-ce:15.0"])
def test_configure_refuses_an_image_that_names_no_registry(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    bare: str,
) -> None:
    configure(cli, cdd)

    code, body = cli.json("configure", "default", "--no-input", "--image", bare)

    assert code == 2, body
    assert body["error"]["code"] == "usage"
    assert "registry" in body["error"]["message"]
    assert "docker.io/" in body["error"]["message"]
    assert "image" not in host_settings(machine)


def test_configure_accepts_docker_hub_named_explicitly_and_an_empty_image_removes_it(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)

    code, body = cli.json("configure", "default", "--no-input", "--image", "docker.io/library/connx:15.0")
    assert code == 0, body
    assert host_settings(machine)["image"] == "docker.io/library/connx:15.0"

    code, body = cli.json("configure", "default", "--no-input", "--image", "")
    assert code == 0, body
    assert "image" not in host_settings(machine)


# --- an image stored before references had to name a registry ---------------------------------------------------

BARE_IMAGE = "connx-mcp-server-ce:15.0"
REPAIR = f"connx-mcp-ce configure default --image <registry>/{BARE_IMAGE}"


def stores_bare_image(machine: Machine, name: str = "default") -> None:
    """The environment's host settings hold a reference with no registry, as configure accepted before."""
    path = machine.base / name / "host.json"
    path.write_text(json.dumps({**host_settings(machine, name), "image": BARE_IMAGE}), encoding="utf-8")


@pytest.mark.parametrize("command", ["start", "download", "update", "status"])
@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_stored_image_that_names_no_registry_is_refused_with_its_repair(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    command: str,
    pinned: bool,
) -> None:
    if pinned:
        started(cli, machine, cdd)
    else:
        configure(cli, cdd)
    stores_bare_image(machine)
    before = host_settings(machine)

    code, body = cli.json(command, "default")

    assert code == 48, body
    assert body["error"]["code"] == "image_reference_invalid"
    assert body["error"]["next"] == REPAIR
    assert "--image ''" in body["error"]["message"]
    assert not pulled(cli)
    assert host_settings(machine) == before


@pytest.mark.parametrize("every", [[], ["--all"]], ids=["no-name", "all"])
def test_the_environment_list_shows_a_stored_image_that_names_no_registry_and_still_lists_the_rest(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    every: list[str],
) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "sales", "--port", "8001")
    stores_bare_image(machine)
    cli.docker.reply(inspect_args("sales"), returncode=1, stderr="Error: No such object: connx-mcp-ce-sales\n")

    code, body = cli.json("status", *every)

    assert code == 0, body
    rows = {row["environment"]: row for row in body["result"]["environments"]}
    assert rows["sales"]["state"] == "not created"
    assert rows["default"]["state"] == "refused"
    assert rows["default"]["error"]["code"] == "image_reference_invalid"
    assert rows["default"]["error"]["next"] == REPAIR


def test_the_human_environment_list_shows_the_repair_of_a_stored_image_that_names_no_registry(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "sales", "--port", "8001")
    stores_bare_image(machine)
    cli.docker.reply(inspect_args("sales"), returncode=1, stderr="Error: No such object: connx-mcp-ce-sales\n")

    result = cli("status")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1].split()[:2] == ["default", "refused"]
    assert lines[2].split()[:3] == ["sales", "not", "created"]
    assert f"Next: {REPAIR}" in lines


def test_download_all_pulls_for_the_rest_and_shows_a_stored_image_that_names_no_registry_as_refused(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "sales", "--port", "8001")
    stores_bare_image(machine)
    cli.docker.reply(["pull", IMAGE])
    image_found(cli)

    code, body = cli.json("download", "--all")

    assert code == 0, body
    assert pulled(cli) == [IMAGE]
    [image] = body["result"]["images"]
    assert image["environments"] == ["sales"]
    [refused] = body["result"]["refused"]
    assert refused["environment"] == "default"
    # The same error object as the environment list's refused row.
    assert set(refused["error"]) == {"code", "message", "next"}
    assert refused["error"]["code"] == "image_reference_invalid"
    assert refused["error"]["next"] == REPAIR


def test_human_download_all_names_the_repair_of_a_refused_environment(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "sales", "--port", "8001")
    stores_bare_image(machine)
    cli.docker.reply(["pull", IMAGE])
    image_found(cli)

    result = cli("download", "--all")

    assert result.exit_code == 0, result.output
    assert f"Downloaded {IMAGE}" in result.stdout
    assert f"Next: {REPAIR}" in result.stdout.splitlines()


@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_guide_names_the_repair_of_a_stored_image_that_names_no_registry_and_still_guides_the_rest(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    pinned: bool,
) -> None:
    if pinned:
        started(cli, machine, cdd)
    else:
        configure(cli, cdd)
    configure(cli, cdd, "sales", "--port", "8001")
    stores_bare_image(machine)
    image_found(cli)
    cli.docker.reply(inspect_args("sales"), returncode=1, stderr="Error: No such object: connx-mcp-ce-sales\n")

    code, body = cli.json("guide")

    assert code == 0, body
    assert body["result"]["environment"] == "default"
    assert body["result"]["next"] == REPAIR
    steps = {step["environment"]: step for step in body["result"]["environments"]}
    assert steps["default"]["stage"] == "not_configured"
    assert "names no registry" in steps["default"]["reason"]
    assert steps["sales"]["stage"] == "not_started"
    assert steps["sales"]["next"] == "connx-mcp-ce start sales"


def test_the_human_refusal_of_a_stored_image_names_the_repair(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    stores_bare_image(machine)

    result = cli("download", "default")

    assert result.exit_code == 48, result.output
    assert REPAIR in result.output


def test_configure_repairs_a_stored_image_that_names_no_registry_and_keeps_the_pin(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    started(cli, machine, cdd)
    stores_bare_image(machine)

    code, body = cli.json("configure", "default", "--no-input", "--image", f"{INTERNAL}/{BARE_IMAGE}")

    assert code == 0, body
    assert host_settings(machine)["image"] == f"{INTERNAL}/{BARE_IMAGE}"
    assert host_settings(machine)["image_digest"] == DIGEST


def test_remove_removes_an_environment_whose_stored_image_names_no_registry(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    stores_bare_image(machine)
    project_volumes(cli, names="")
    project_containers(cli)

    code, body = cli.json("remove", "default", "--yes")

    assert code == 0, body
    assert not (machine.base / "default").exists()


def test_export_writes_an_environment_whose_stored_image_names_no_registry_and_import_refuses_that_file(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure(cli, cdd)
    stores_bare_image(machine)

    code, body = cli.json("export", "default")

    assert code == 0, body
    content = body["result"]["content"]
    assert f'# host image = "{BARE_IMAGE}"' in content.splitlines()
    exported = tmp_path / "default.env"
    exported.write_text(content, encoding="utf-8")

    code, body = cli.json("configure", "sales", "--no-input", "--from-env-file", str(exported), "--cdd", str(cdd))

    assert code == 2, body
    assert "registry" in body["error"]["message"]
    assert not (machine.base / "sales").exists()


def test_configure_help_says_the_image_applies_at_the_next_start_or_update(cli: Cli) -> None:
    result = cli("configure", "--help")

    assert result.exit_code == 0, result.output
    assert "--image" in result.stdout
    assert "update" in " ".join(result.stdout.split())


# --- notices: variables an environment ignores ----------------------------------------------------------------

IGNORED = "CONNX_MCP_REGISTRY and CONNX_MCP_IMAGE_TAG are set but ignored"


def no_container(cli: Cli) -> None:
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")


def notices_naming(texts: list[str], words: str) -> list[str]:
    return [text for text in texts if words in text]


def test_status_names_the_variables_an_environment_with_its_own_image_ignores(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["ignored_variables"] == ["CONNX_MCP_REGISTRY", "CONNX_MCP_IMAGE_TAG"]
    assert len(notices_naming(body["result"]["notices"], IGNORED)) == 1
    assert f"Warning: {IGNORED}" in cli("status", "default").stdout


def own_image_with_variables(cli: Cli, cdd: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    configure(cli, cdd, "default", "--image", OTHER_IMAGE)
    set_variables(monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(["pull", OTHER_IMAGE])
    found(cli, OTHER_IMAGE, OTHER_DIGEST)


def test_download_names_the_variables_an_environment_with_its_own_image_ignores(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_image_with_variables(cli, cdd, monkeypatch)

    code, body = cli.json("download")

    assert code == 0, body
    assert pulled(cli) == [OTHER_IMAGE]
    assert len(notices_naming(body["result"]["images"][0]["warnings"], IGNORED)) == 1
    assert f"Warning: {IGNORED}" in cli("download").stdout


def test_update_names_the_variables_an_environment_with_its_own_image_ignores(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_image_with_variables(cli, cdd, monkeypatch)

    no_container(cli)

    code, body = cli.json("update", "--no-restart")

    assert code == 0, body
    assert len(notices_naming(body["result"]["warnings"], IGNORED)) == 1
    assert f"Warning: {IGNORED}" in cli("update", "--no-restart").stdout


def test_start_names_the_variables_an_environment_with_its_own_image_ignores(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_image_with_variables(cli, cdd, monkeypatch)
    comes_up(cli, machine, state())
    cli.docker.reply(
        ["container", "inspect", "--format", "{{.Image}}", "connx-mcp-ce-default"], stdout=image_id(OTHER_DIGEST)
    )
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert body["result"]["image"] == OTHER_DIGEST
    assert len(notices_naming(body["result"]["warnings"], IGNORED)) == 1


def test_start_names_the_ignored_variables_before_a_start_that_then_fails(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_image_with_variables(cli, cdd, monkeypatch)
    comes_up(cli, machine, state(status="exited", health="unhealthy", exit_code=1))

    result = cli("start")

    assert result.exit_code != 0
    assert "exited with code 1" in result.stderr
    [notice] = notices_naming(result.stderr.splitlines(), IGNORED)
    assert notice == (
        f"Warning: {IGNORED}: 'default' has its own image {OTHER_IMAGE}; "
        "`connx-mcp-ce configure default --image ''` makes the variables apply."
    )


NOW_NAME = f"the image variables now name {INTERNAL_IMAGE}"


def restarted_with(cli: Cli, machine: Machine, monkeypatch: pytest.MonkeyPatch, variables: dict[str, str]) -> None:
    set_variables(monkeypatch, variables)
    comes_up(cli, machine, state())
    serving(cli)
    image_found(cli)


def test_start_says_update_moves_a_pinned_environment_to_the_image_the_variables_now_name(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    restarted_with(cli, machine, monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE],
        1,
        stderr=f"Error: No such image: {INTERNAL_IMAGE}",
    )

    code, body = cli.json("start")

    assert code == 0, body
    assert body["result"]["image"] == DIGEST
    assert not pulled(cli)
    [notice] = notices_naming(body["result"]["warnings"], NOW_NAME)
    assert "`connx-mcp-ce update default`" in notice
    assert f"Warning: CONNX_MCP_REGISTRY and CONNX_MCP_IMAGE_TAG are set: {NOW_NAME}" in cli("start").stdout


def test_start_says_the_variables_name_another_image_even_when_that_image_is_on_this_machine(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    restarted_with(cli, machine, monkeypatch, INTERNAL_VARIABLES)
    found(cli, INTERNAL_IMAGE, f"{INTERNAL}/connx-mcp-server-ce@sha256:" + "d" * 64)

    code, body = cli.json("start")

    assert code == 0, body
    assert body["result"]["image"] == DIGEST
    [notice] = notices_naming(body["result"]["warnings"], NOW_NAME)
    assert "`connx-mcp-ce update default`" in notice


def test_start_says_nothing_when_the_variables_name_the_pinned_reference_that_is_no_longer_on_this_machine(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    restarted_with(cli, machine, monkeypatch, {"CONNX_MCP_REGISTRY": BUILT_IN_REGISTRY, "CONNX_MCP_IMAGE_TAG": "15.0"})
    cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, IMAGE], 1, stderr=f"Error: No such image: {IMAGE}")

    code, body = cli.json("start")

    assert code == 0, body
    assert not notices_naming(body["result"]["warnings"], "variables")


def test_start_says_nothing_of_the_variables_for_an_environment_pinned_before_the_reference_was_recorded(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    host_file = machine.base / "default" / "host.json"
    host = host_settings(machine)
    del host["image_pinned_reference"]
    host_file.write_text(json.dumps(host), encoding="utf-8")
    restarted_with(cli, machine, monkeypatch, INTERNAL_VARIABLES)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE],
        1,
        stderr=f"Error: No such image: {INTERNAL_IMAGE}",
    )

    code, body = cli.json("start")

    assert code == 0, body
    assert not notices_naming(body["result"]["warnings"], "variables")


def test_export_and_import_carry_the_reference_an_environment_pinned(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    moved = tmp_path / "moved.env"
    assert cli.json("export", "--include-secrets", "--output", str(moved))[0] == 0

    fresh = move_to_a_fresh_machine(machine, tmp_path, monkeypatch)
    code, body = cli.json("configure", "--no-input", "--from-env-file", str(moved), "--cdd", str(cdd))

    assert code == 0, body
    host = json.loads((fresh / "default" / "host.json").read_text(encoding="utf-8"))
    assert (host["image_digest"], host["image_pinned_reference"]) == (DIGEST, IMAGE)


def test_an_imported_pin_without_its_reference_drops_the_reference_of_the_pin_it_replaces(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started(cli, machine, cdd)
    sent = tmp_path / "sent.env"
    sent.write_text(f'# host image_digest = "{OTHER_DIGEST}"\n', encoding="utf-8")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent))

    assert code == 0, body
    host = host_settings(machine)
    assert host["image_digest"] == OTHER_DIGEST
    assert "image_pinned_reference" not in host
    restarted_with(cli, machine, monkeypatch, INTERNAL_VARIABLES)
    found(cli, OTHER_DIGEST, OTHER_DIGEST)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, INTERNAL_IMAGE],
        1,
        stderr=f"Error: No such image: {INTERNAL_IMAGE}",
    )

    code, body = cli.json("start")

    assert code == 0, body
    assert not notices_naming(body["result"]["warnings"], "variables")


@pytest.mark.parametrize(
    "variables",
    [{}, {"CONNX_MCP_REGISTRY": BUILT_IN_REGISTRY, "CONNX_MCP_IMAGE_TAG": "15.0"}],
)
def test_start_says_nothing_of_the_variables_when_none_is_set_or_they_name_the_pinned_image(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
) -> None:
    started(cli, machine, cdd)
    restarted_with(cli, machine, monkeypatch, variables)

    code, body = cli.json("start")

    assert code == 0, body
    assert not notices_naming(body["result"]["warnings"], "variables")


@pytest.mark.parametrize(
    ("variables", "flags"),
    [({}, ("--image", OTHER_IMAGE)), (INTERNAL_VARIABLES, ())],
)
def test_status_lists_no_ignored_variables_when_none_is_set_or_the_variables_apply(
    cli: Cli,
    cdd: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    flags: tuple[str, ...],
) -> None:
    configure(cli, cdd, "default", *flags)
    set_variables(monkeypatch, variables)
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["ignored_variables"] == []
    assert not notices_naming(body["result"]["notices"], "ignored")


# --- versions -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["15.0.0.dev-559", "15.0.0-rc.2", "dev-559", "latest"])
def test_a_pre_release_or_non_standard_label_is_never_refused(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    label: str,
) -> None:
    configure(cli, cdd)
    image_found(cli, version=label)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body


def test_start_and_update_help_say_an_image_without_a_release_version_gets_a_warning(cli: Cli) -> None:
    for command in ("start", "update"):
        text = " ".join(cli(command, "--help").stdout.split())
        assert "cannot find a release version in the image" in text, command


def _state_format() -> str:
    from connx_mcp.lifecycle import STATE_FORMAT

    return STATE_FORMAT
