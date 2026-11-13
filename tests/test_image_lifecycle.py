#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_image_lifecycle.py - The administrator signs in, downloads the image and updates an environment.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: `login`
    hands the token to `docker login --password-stdin` and never shows it; `download` pulls an
    environment's image or every distinct one; a registry refusal names `connx-mcp-ce login` and
    where credentials come from; `update` pulls, re-pins and offers a restart; `status` shows the
    pinned version and digest and flags a pulled or running image that is not the pinned one;
    `start` and `update` refuse an image older than this CLI supports and warn about a newer one.
    The live test pulls without credentials from the host.

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
from pathlib import Path

import pytest

from connx_mcp import environment, exposure, image, login
from connx_mcp.output import CliError, ExitCode
from connx_mcp.ports import default_ports
from tests.conftest import Cli, Machine
from tests.test_lifecycle import (
    DIGEST,
    IMAGE,
    IMAGE_FORMAT,
    MOVED_DIGEST,
    cdd,  # noqa: F401  (fixture)
    comes_up,
    compose,
    configure,
    host_settings,
    image_found,
    image_id,
    runs_image,
    serving,
    started,
    state,
)

TOKEN = "tok-for-tests-only-0123456789"  # disclosure-scan: allow
REGISTRY = "softwareagcr.azurecr.io"
LOGIN = ["login", REGISTRY, "--username", "mcp-admin", "--password-stdin"]
UNAUTHORIZED = (
    'Error response from daemon: Head "https://softwareagcr.azurecr.io/v2/connx-mcp-server-ce/manifests/15.0": '
    "unauthorized: authentication required, visit https://aka.ms/acr/authorization for more information.\n"
)
REGISTRY_HINT = "Empower or Software AG support"


def pulls(cli: Cli, ref: str = IMAGE, returncode: int = 0, stderr: str = "") -> None:
    cli.docker.reply(
        ["pull", ref], returncode=returncode, stdout="15.0: Pulling from connx-mcp-server-ce\n", stderr=stderr
    )


# --- login ------------------------------------------------------------------------------------------------


def test_login_hands_the_token_to_docker_on_stdin_and_never_shows_it(cli: Cli) -> None:
    cli.docker.reply(LOGIN, stdout="Login Succeeded\n")

    result = cli("login", "--username", "mcp-admin", "--password-stdin", input=TOKEN + "\n")

    assert result.exit_code == 0, result.output
    assert LOGIN in cli.docker.calls
    assert cli.docker.stdins[cli.docker.calls.index(LOGIN)] == TOKEN
    assert not [c for c in cli.docker.calls if TOKEN in " ".join(c)]
    assert TOKEN not in result.output
    assert f"Signed in to {REGISTRY} as mcp-admin" in result.stdout


def test_login_as_json_reports_the_registry_and_user(cli: Cli) -> None:
    cli.docker.reply(LOGIN, stdout="Login Succeeded\n")

    code, body = cli.json("login", "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 0, body
    assert body["result"] == {"registry": REGISTRY, "username": "mcp-admin", "signed_in": True}
    assert TOKEN not in str(body)


def test_login_asks_for_user_and_token_on_a_terminal_with_the_token_masked(cli: Cli) -> None:
    cli.docker.reply(LOGIN, stdout="Login Succeeded\n")

    result, screen = cli.typed("login", keys=["mcp-admin", TOKEN])

    assert result.exit_code == 0, result.output
    assert cli.docker.stdins[cli.docker.calls.index(LOGIN)] == TOKEN
    assert TOKEN not in screen
    assert TOKEN not in result.output


def test_a_cancelled_login_prompt_exits_cancelled(cli: Cli) -> None:
    result, _ = cli.typed("login", keys=[])

    assert result.exit_code == 100, result.output
    assert "cancelled" in result.stderr
    assert not [c for c in cli.docker.calls if c[:1] == ["login"]]


def test_login_without_a_terminal_or_stdin_token_names_password_stdin(cli: Cli) -> None:
    code, body = cli.json("login", "--username", "mcp-admin")

    assert code == 3
    assert "--password-stdin" in body["error"]["next"]
    assert not [c for c in cli.docker.calls if c[0] == "login"]


def test_a_refused_login_names_where_credentials_come_from_and_hides_the_token(cli: Cli) -> None:
    cli.docker.reply(
        LOGIN,
        returncode=1,
        stderr='Error response from daemon: Get "https://softwareagcr.azurecr.io/v2/": unauthorized: '
        f"bad token {TOKEN}\n",
    )

    code, body = cli.json("login", "--username", "mcp-admin", "--password-stdin", input=TOKEN)

    assert code == 44
    assert body["error"]["code"] == "registry_unauthorized"
    assert REGISTRY_HINT in body["error"]["message"]
    assert "unauthorized" in body["error"]["message"]
    assert TOKEN not in str(body)


def test_login_takes_another_registry(cli: Cli) -> None:
    args = ["login", "registry.example.com:5000", "--username", "mcp-admin", "--password-stdin"]
    cli.docker.reply(args, stdout="Login Succeeded\n")

    code, body = cli.json(
        "login", "--registry", "registry.example.com:5000", "--username", "mcp-admin", "--password-stdin", input=TOKEN
    )

    assert code == 0, body
    assert args in cli.docker.calls


def test_login_help_names_where_credentials_come_from(cli: Cli) -> None:
    result = cli("login", "--help")

    assert result.exit_code == 0, result.output
    assert f"Registry credentials from {REGISTRY_HINT}." in result.stdout


# --- download ---------------------------------------------------------------------------------------------


def test_download_pulls_the_default_image_of_the_environment(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    pulls(cli)
    image_found(cli)

    code, body = cli.json("download")

    assert code == 0, body
    assert ["pull", "softwareagcr.azurecr.io/connx-mcp-server-ce:15.0"] in cli.docker.calls
    [pulled] = body["result"]["images"]
    assert pulled["reference"] == IMAGE
    assert pulled["environments"] == ["default"]
    assert pulled["digest"] == DIGEST
    assert pulled["version"] == "15.0.3.26012"


def test_download_pulls_the_image_named_in_host_settings(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    path = machine.base / "default" / "host.json"
    path.write_text(path.read_text().replace("{", '{"image": "registry.example.com/connx:15.0.4",', 1))
    pulls(cli, "registry.example.com/connx:15.0.4")
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, "registry.example.com/connx:15.0.4"],
        stdout='{"id": "sha256:1", "digests": ["registry.example.com/connx@sha256:1"], "labels": null}\n',
    )

    code, body = cli.json("download")

    assert code == 0, body
    assert ["pull", "registry.example.com/connx:15.0.4"] in cli.docker.calls


def test_download_all_pulls_each_distinct_image_once(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    for name in ("default", "sales", "test"):
        configure(cli, cdd, name)
    pulls(cli)
    image_found(cli)

    code, body = cli.json("download", "--all")

    assert code == 0, body
    assert [c for c in cli.docker.calls if c[0] == "pull"] == [["pull", IMAGE]]
    [pulled] = body["result"]["images"]
    assert pulled["environments"] == ["default", "sales", "test"]


def test_download_all_pulls_two_distinct_images_each_once(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    other = "registry.example.com/connx:15.0.4"
    for name in ("default", "sales", "test"):
        configure(cli, cdd, name)
    path = machine.base / "sales" / "host.json"
    path.write_text(path.read_text().replace("{", f'{{"image": "{other}",', 1))
    pulls(cli)
    pulls(cli, other)
    image_found(cli)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, other],
        stdout='{"id": "sha256:1", "digests": ["registry.example.com/connx@sha256:1"], "labels": null}\n',
    )

    code, body = cli.json("download", "--all")

    assert code == 0, body
    assert sorted(c[1] for c in cli.docker.calls if c[0] == "pull") == sorted([IMAGE, other])
    assert {i["reference"]: i["environments"] for i in body["result"]["images"]} == {
        IMAGE: ["default", "test"],
        other: ["sales"],
    }


def test_download_tells_which_environments_stay_on_their_pin(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")

    result = cli("download")

    assert result.exit_code == 0, result.output
    assert "connx-mcp-ce update default" in result.stdout
    assert host_settings(machine)["image_digest"] == DIGEST


def test_an_unauthorized_pull_names_login_and_where_credentials_come_from(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    pulls(cli, returncode=1, stderr=UNAUTHORIZED)

    code, body = cli.json("download")

    assert code == 44
    assert body["error"]["next"] == "connx-mcp-ce login"
    assert REGISTRY_HINT in body["error"]["message"]
    assert "authentication required" in body["error"]["message"]


def test_a_pull_that_fails_otherwise_shows_dockers_message(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    pulls(cli, returncode=1, stderr=f"Error response from daemon: manifest for {IMAGE} not found: manifest unknown\n")

    code, body = cli.json("download")

    assert code == 45
    assert body["error"]["code"] == "pull_failed"
    assert "manifest unknown" in body["error"]["message"]


def test_download_of_an_unknown_environment_names_configure(cli: Cli) -> None:
    code, body = cli.json("download", "sales")

    assert code == 20


# --- update -----------------------------------------------------------------------------------------------


def test_update_pulls_re_pins_and_restarts_when_asked(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])
    runs_image(cli, MOVED_DIGEST)

    code, body = cli.json("update", "--restart")

    assert code == 0, body
    result = body["result"]
    assert result["previous"] == {"digest": DIGEST, "version": "15.0.3.26012"}
    assert result["pinned"] == {"digest": MOVED_DIGEST, "version": "15.0.4.26100"}
    assert result["changed"] is True
    assert result["restarted"] is True
    assert host_settings(machine)["image_digest"] == MOVED_DIGEST
    assert host_settings(machine)["image_version"] == "15.0.4.26100"
    pull_at = cli.docker.calls.index(["pull", IMAGE])
    up_at = cli.docker.calls.index([*compose(machine), "up", "--detach", "--pull", "never", "--no-build"])
    assert pull_at < up_at
    assert f'image: "{MOVED_DIGEST}"' in (machine.base / "default" / "compose.yml").read_text(encoding="utf-8")


def test_update_without_a_terminal_re_pins_and_names_restart(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")

    code, body = cli.json("update")

    assert code == 0, body
    assert body["result"]["restarted"] is False
    assert body["result"]["next"] == "connx-mcp-ce restart default"
    assert host_settings(machine)["image_digest"] == MOVED_DIGEST
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "version" not in c]


def test_update_offers_the_restart_on_a_terminal(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])
    runs_image(cli, MOVED_DIGEST)

    result, screen = cli.typed("update", keys=["y"])

    assert result.exit_code == 0, result.output
    assert "Restart" in screen
    assert [*compose(machine), "stop", "--timeout", "10"] in cli.docker.calls


def test_update_with_nothing_new_keeps_the_pin_and_offers_no_restart(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)

    code, body = cli.json("update", "--restart")

    assert code == 0, body
    assert body["result"]["changed"] is False
    assert body["result"]["restarted"] is False
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "version" not in c]


def test_update_of_a_stopped_environment_re_pins_for_its_next_start(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    cli.docker.reply(
        ["container", "inspect", "--format", lifecycle_state_format(), "connx-mcp-ce-default"],
        stdout=state(status="exited") + "\n",
    )

    code, body = cli.json("update", "--restart")

    assert code == 0, body
    assert body["result"]["restarted"] is False
    assert body["result"]["next"] == "connx-mcp-ce start default"


def test_update_of_a_never_started_environment_pins_for_its_first_start(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    pulls(cli)
    image_found(cli)
    cli.docker.reply(
        ["container", "inspect", "--format", lifecycle_state_format(), "connx-mcp-ce-default"],
        returncode=1,
        stderr="Error: No such object: connx-mcp-ce-default\n",
    )

    code, body = cli.json("update", "--restart")

    assert code == 0, body
    assert body["result"]["previous"] == {"digest": None, "version": None}
    assert body["result"]["pinned"] == {"digest": DIGEST, "version": "15.0.3.26012"}
    assert body["result"]["restarted"] is False
    assert body["result"]["next"] == "connx-mcp-ce start default"
    assert host_settings(machine)["image_digest"] == DIGEST
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "version" not in c]


def test_an_unauthorized_update_names_login(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli, returncode=1, stderr=UNAUTHORIZED)

    code, body = cli.json("update")

    assert code == 44
    assert REGISTRY_HINT in body["error"]["message"]
    assert host_settings(machine)["image_digest"] == DIGEST


# --- versions ---------------------------------------------------------------------------------------------


def test_start_refuses_an_image_older_than_the_minimum_supported(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    image_found(cli, version="14.9.2.25300")

    code, body = cli.json("start")

    assert code == 46
    assert body["error"]["code"] == "image_too_old"
    assert "14.9.2.25300" in body["error"]["message"]
    assert "15.0" in body["error"]["message"]
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "version" not in c]
    assert "image_digest" not in host_settings(machine)


def test_update_refuses_an_image_older_than_the_minimum_and_keeps_the_pin(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="14.9.2.25300")

    code, body = cli.json("update", "--restart")

    assert code == 46
    assert host_settings(machine)["image_digest"] == DIGEST


def test_start_warns_about_an_image_newer_than_this_cli_knows(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    image_found(cli, version="15.1.0.26200")
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert any("15.1.0.26200" in w and "newer" in w for w in body["result"]["warnings"])


def test_update_warns_about_an_image_newer_than_this_cli_knows(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="16.0.0.27001")

    code, body = cli.json("update")

    assert code == 0, body
    assert any("16.0.0.27001" in w and "newer" in w for w in body["result"]["warnings"])


def test_start_warns_when_the_image_has_no_version_label(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    image_found(cli, version=None)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert any("version" in w for w in body["result"]["warnings"])


def test_start_warns_but_does_not_refuse_a_v_prefixed_version_label(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    image_found(cli, version="v14.9.2")
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert any("v14.9.2" in w and "not checked" in w for w in body["result"]["warnings"])


# --- status -----------------------------------------------------------------------------------------------


def test_status_shows_the_pinned_version_and_digest(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    image = body["result"]["image"]
    assert image["reference"] == IMAGE
    assert image["digest"] == DIGEST
    assert image["version"] == "15.0.3.26012"
    assert image["running"] == image_id(DIGEST)
    assert image["pulled"] is None
    assert body["result"]["notices"] == [exposure.warning(environment.load("default"))]
    human = cli("status", "default").stdout
    assert "15.0.3.26012" in human
    assert DIGEST in human


def test_status_flags_a_pulled_image_the_environment_does_not_run(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    started(cli, machine, cdd)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    image_found(cli, digest=DIGEST)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["pulled"] == {"digest": MOVED_DIGEST, "version": "15.0.4.26100"}
    assert any("15.0.4.26100" in n and "connx-mcp-ce update default" in n for n in body["result"]["notices"])


def test_status_flags_a_container_running_another_image_than_the_pinned(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    started(cli, machine, cdd)
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    assert cli.json("update")[0] == 0

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"]["running"] == image_id(DIGEST)
    assert any("connx-mcp-ce restart default" in n for n in body["result"]["notices"])


def test_status_of_an_unpinned_environment_asks_docker_nothing_about_images(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure(cli, cdd)
    cli.docker.reply(
        ["container", "inspect", "--format", lifecycle_state_format(), "connx-mcp-ce-default"],
        returncode=1,
        stderr="Error: No such object: connx-mcp-ce-default\n",
    )

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["image"] == {
        "reference": IMAGE,
        "source": {"registry": "built-in default", "name": "built-in default", "tag": "built-in default"},
        "digest": None,
        "version": None,
        "running": None,
        "pulled": None,
        "ignored_variables": [],
    }
    assert not [c for c in cli.docker.calls if c[:2] == ["image", "inspect"]]


def lifecycle_state_format() -> str:
    from connx_mcp.lifecycle import STATE_FORMAT

    return STATE_FORMAT


# --- reading Docker's sign-ins and an image's registry ---------------------------------------------------------


def _docker_config(directory: Path, text: str | None) -> None:
    """Write Docker's client configuration, as `docker login` leaves it."""
    directory.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (directory / "config.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("config", "registry", "expected"),
    [
        (json.dumps({"auths": {REGISTRY: {"auth": TOKEN}}}), REGISTRY, True),
        (json.dumps({"credHelpers": {REGISTRY: "acr-env"}}), REGISTRY, True),
        (json.dumps({"auths": {"https://index.docker.io/v1/": {}}}), "docker.io", True),
        (json.dumps({"auths": {"registry-1.docker.io": {}}}), "docker.io", True),
        (json.dumps({"auths": {"localhost:5000": {}}}), "localhost:5000", True),
        (json.dumps({"credsStore": "desktop"}), REGISTRY, False),
        (json.dumps({"auths": {"other.example.com": {}}}), REGISTRY, False),
        (None, REGISTRY, False),
    ],
    ids=["auths", "cred-helper", "hub-v1-url", "hub-alias", "host-port", "creds-store-only", "other", "no-file"],
)
def test_signed_in_reads_only_the_registry_names(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, config: str | None, registry: str, expected: bool
) -> None:
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    _docker_config(machine.home / ".docker", config)

    assert login.signed_in(registry) is expected


def test_signed_in_reads_the_configuration_docker_config_names(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "elsewhere"))
    _docker_config(tmp_path / "elsewhere", json.dumps({"auths": {REGISTRY: {}}}))

    assert login.signed_in(REGISTRY) is True


@pytest.mark.parametrize(
    ("text", "error"),
    [('{"auths": {"x": "' + TOKEN, "JSONDecodeError"), ('["' + TOKEN + '"]', "not a JSON object")],
    ids=["truncated", "not-an-object"],
)
def test_an_unreadable_docker_configuration_is_an_error_naming_the_file_never_its_content(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, text: str, error: str
) -> None:
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    _docker_config(machine.home / ".docker", text)

    with pytest.raises(CliError) as raised:
        login.signed_in(REGISTRY)

    assert raised.value.exit_code == ExitCode.FILE_UNREADABLE
    assert str(machine.home / ".docker" / "config.json") in raised.value.message
    assert error in raised.value.message
    assert TOKEN not in raised.value.message


def test_guide_never_shows_a_sign_in_value(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure(cli, cdd)
    cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, IMAGE], 1, stderr=f"Error: No such image: {IMAGE}")
    _docker_config(machine.home / ".docker", json.dumps({"auths": {REGISTRY: {"auth": TOKEN}}}))

    result = cli("guide", env={"DOCKER_CONFIG": str(machine.home / ".docker")})

    assert result.exit_code == 0, result.output
    assert "connx-mcp-ce download default" in result.stdout
    assert TOKEN not in result.output


@pytest.mark.parametrize(
    ("ref", "registry"),
    [
        ("softwareagcr.azurecr.io/connx-mcp-server-ce:15.0", "softwareagcr.azurecr.io"),
        ("localhost/connx-mcp-server-ce:15.0", "localhost"),
        ("registry:5000/connx-mcp-server-ce", "registry:5000"),
        ("docker.io/library/nginx:1", "docker.io"),
    ],
)
def test_the_registry_of_an_image_reference(ref: str, registry: str) -> None:
    assert image.registry(ref) == registry


# --- live -------------------------------------------------------------------------------------------------


@pytest.mark.live
def test_an_unauthenticated_pull_names_login(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    """Needs Docker on the host and no stored credentials for the default image's registry."""
    if os.environ.get("CONNX_MCP_LIVE_UNAUTHENTICATED") != "1":
        pytest.skip("set CONNX_MCP_LIVE_UNAUTHENTICATED=1 on a machine not signed in to the registry")
    real = default_ports()
    cli.docker, cli.probe = real.docker, real.probe  # type: ignore[assignment]
    path = tmp_path / "live.cdd"
    path.write_bytes(b"CDD")
    configure(cli, path, "live-04")

    code, body = cli.json("download", "live-04")

    assert code == 44, body
    assert REGISTRY_HINT in body["error"]["message"]
