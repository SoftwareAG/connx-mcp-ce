#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_demo_stack_live.py - The demo stack starts and stops against a real Docker daemon.

Description:
    Live: start-demo with Keycloak and Vault becomes healthy and answers on its
    published ports; stop-demo --purge removes it and its seed image. Needs Docker and free ports 8080 and 8200;
    run from the host with `uv run pytest -m live tests/test_demo_stack_live.py`.

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
from pathlib import Path

import pytest

from connx_mcp.demo.stack import SEED_IMAGE
from connx_mcp.ports import default_ports
from connx_mcp.probe import UrllibProbe
from tests.conftest import Cli

USERS = """\
identities:
  - name: alice
    id: 4bb91078-2e33-4fe4-b386-bdf1d51f1e6a
    connx: { username: mcp-restricted, restricted: true }
  - name: dave
    id: 2464584c-7aac-4b8a-b021-3be750bafb2a
"""


@pytest.mark.live
def test_start_and_stop_demo_with_keycloak_and_vault(cli: Cli, tmp_path: Path) -> None:
    if os.environ.get("CONNX_MCP_LIVE_STACK_HOST"):
        pytest.skip("CONNX_MCP_LIVE_STACK_HOST names a remote demo stack: this run never starts or purges one")
    users_file = tmp_path / "users.yaml"
    users_file.write_text(USERS)
    code, envelope = cli.json("configure-demo", "--keycloak", "--vault", "--users-file", str(users_file))
    assert code == 0, envelope

    try:
        code, envelope = cli.json("start-demo")
        assert code == 0, envelope
        probe = UrllibProbe()
        assert probe.health("http://localhost:8080/realms/connx-mcp", timeout=10).status == 200
        assert probe.health("https://localhost:8200/v1/sys/health", timeout=10).status == 200
        assert Path(envelope["result"]["vault_ca"]).is_file()
    finally:
        code, envelope = cli.json("stop-demo", "--purge")

    assert code == 0, envelope
    assert envelope["result"]["purged"] is True
    assert envelope["result"]["seed_image_removed"] is True
    assert default_ports().docker.run(["image", "inspect", SEED_IMAGE]).returncode != 0
