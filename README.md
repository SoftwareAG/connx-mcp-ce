<!--
connx-mcp-ce: the administrator's tool for the CONNX MCP Server Community Edition.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
-->

# connx-mcp-ce

`connx-mcp-ce` is the administrator's command-line tool for the CONNX MCP Server Community Edition. It sets up,
starts, stops and maintains server environments on one machine with Docker. It also runs a demo stack (Keycloak, dex
and HashiCorp Vault) to try OAuth sign-in and per-caller Vault logins. Do not use the demo stack in production.

[CONNX MCP Server Community Edition](docs/getting-started.md) tells you what the server is, what you need and the
first commands. It also lists all guides.

## Install

Download the binary for Linux or Windows from the
[releases page](https://github.com/softwareag/connx-mcp-ce/releases), or install from source with uv. The binary
is for x86-64 only. On Windows, use Windows 11 or Windows Server 2022.
[Install connx-mcp-ce](docs/install.md) tells you how to check the SHA-256 sum and what to do when Windows
SmartScreen shows a warning.

## First steps

```shell
connx-mcp-ce guide
```

`guide` examines this machine and tells you the next command to run. Each command has its own `--help`.

## Install the skill

`connx-mcp-ce install-skill <environment>` copies the `connx-sql-expert` skill from the server image into the
skills directory that each harness reads. It also writes the skill instructions into the instructions file of each
harness. [Connect a harness](docs/setup-with-cli.md#connect-a-harness) gives the instructions files, how to
register the server and how to remove the skill.

## Network access

By default, the server port is open on all network interfaces. Harnesses on other machines can then connect. When
only harnesses on this machine use an environment, publish its port on this machine only:
`connx-mcp-ce configure <environment> --listen localhost` (`--listen all` publishes it on all interfaces again).

This list shows what other machines reach, and what to do:

- Environment without OAuth: other machines reach the server without sign-in. Each caller runs SQL as the CONNX
  login that the environment stores. Use the environment only on a trusted network, or choose an OAuth provider
  with `connx-mcp-ce configure`. On a trusted network, `connx-mcp-ce configure <environment> --accept-open-network`
  stops the warning.
- Environment with `--listen localhost`: only this machine reaches the server.
- Environment with an OAuth provider: other machines reach the server. Each caller must sign in.
- Demo stack: other machines reach Vault and Keycloak. Vault has a fixed root token and holds the CONNX logins of
  the demo users. The Keycloak admin console uses `admin`/`admin`. Run the demo stack only for a demonstration on
  a trusted machine.
- Demo stack on another machine: each machine that reaches the stack machine reaches the same services. You cannot
  change the Keycloak admin login or the Vault tokens. Give the demo users their own passwords in the users file.
  Limit the demo stack ports (by default 8080, 5556 and 8200) to the machines that need them. Map only test CONNX
  logins to the demo users.
  [Use a demo stack on another machine](docs/setup-with-cli.md#use-a-demo-stack-on-another-machine) gives the
  commands.

`configure`, `status` and `guide` warn about an environment without OAuth that other machines reach. `start-demo`
warns about the demo stack. `configure` also warns about a demo stack on another machine.

## Licence

`connx-mcp-ce` is licensed under the Apache License, Version 2.0. [LICENSE](LICENSE) and [NOTICE](NOTICE) give the
licence text and the notice.

The CONNX MCP Server that `connx-mcp-ce` deploys is a separate product under its own end-user licence agreement
(EULA). The server starts only when you accept the EULA. `connx-mcp-ce` accepts it for you when it starts the server.
