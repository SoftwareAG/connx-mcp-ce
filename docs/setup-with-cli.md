<!--
Set up the CONNX MCP Server Community Edition with connx-mcp-ce: install, configure, start and connect a harness.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
-->

# Set up the CONNX MCP Server with connx-mcp-ce

`connx-mcp-ce` sets up, starts and maintains the CONNX MCP Server Community Edition on one machine with Docker. This
guide shows the usual command for each step. Each command has its own `--help` with all of its options.

An environment is one server deployment with its own settings. You usually make one environment for each CDD. An
environment name has lower-case letters, digits, `-` and `_`, starts with a letter or digit, and has a maximum of
32 characters, for example `sales`. Most commands take the environment name as their first argument. Without a
name, a command uses the environment that the variable `CONNX_MCP_ENVIRONMENT` names. Otherwise, it uses the
environment `default`.

In this guide, a variable is a variable of your shell that the programs it starts can read: `export NAME=value` in
bash, or `$env:NAME = 'value'` in PowerShell.

Where bash and PowerShell commands are different, this guide shows the two. Replace each `<...>` part with your own
value.

## Install connx-mcp-ce

[Install connx-mcp-ce](install.md) tells you how to download the binary for Linux or Windows, check its SHA-256
sum, and install it. You also need Docker Engine 24 or later with Compose v2, Docker Desktop, or Rancher Desktop with
the dockerd (moby) engine. [What you need](getting-started.md#what-you-need) lists all that you need.

Make sure that `connx-mcp-ce` starts:

```shell
connx-mcp-ce --help
```

Find the next step:

```shell
connx-mcp-ce guide
```

`guide` examines Docker, your environments, the image, the server, its certificate, the demo stack and the skill.
It tells you the first step that is not done and the command for it. It changes nothing. Run `guide` again after
each step, until it says that every step is done.

`connx-mcp-ce exit-codes` lists the exit codes of all commands and what they mean.

## Where connx-mcp-ce keeps its files

`connx-mcp-ce` keeps all environments in one base directory, one subdirectory for each environment:

| System | Base directory |
|---|---|
| Linux | `~/.connx/connx-mcp-ce` |
| Windows | `%USERPROFILE%\.connx\connx-mcp-ce` |

To use a different base directory, set the variable `MCP_ENVIRONMENT_PATH` before you run a command:

> 🐧 **Linux (bash)**
>
> ```bash
> export MCP_ENVIRONMENT_PATH=/srv/connx-mcp-ce
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $env:MCP_ENVIRONMENT_PATH = 'D:\connx-mcp-ce'
> ```

Each environment `<name>` has these files in `<base directory>/<name>/`:

| File | Contents |
|---|---|
| `server.env` | The server settings, which the server reads when it starts. Only your user account can read it |
| `host.json` | The host settings, for example the CDD file, port and image. The server does not read them |
| `compose.yml` | The Docker Compose file. Each start writes it again. Do not change it |
| `compose.override.yml` | Optional. Your changes to the compose file. `connx-mcp-ce` applies it and never changes it |
| `started-with.json` | What the last start used. `status` compares it with the settings to find drift |
| `skill-install.json` | What `install-skill` installed, so that `uninstall-skill` can remove it again |

The demo stack keeps its files in `<base directory>/.demo/`. The Docker container of an environment has the name
`connx-mcp-ce-<name>`.

To make a backup of an environment, copy its directory. `server.env` can hold passwords and tokens. Keep the copy
private.

## Get your registry credentials

The server image is in the container registry `softwareagcr.azurecr.io`. To download it, you need a registry user
name and token:

1. Sign in to the registry portal at <https://containers.softwareag.com> with your Empower credentials or the
   credentials that Software AG support gave you.
2. Open the **Profile** page.
3. Copy your registry user name and your token.

Keep the token private. You use the user name and token with `connx-mcp-ce login` in
[Sign in and download the image](#sign-in-and-download-the-image).

## Configure an environment

`configure` creates an environment or changes its settings. You need:

- The CDD file on this machine. `connx-mcp-ce` mounts it read-only into the server container.
- The CONNX login that the server uses, and its password.
- The host name of your CONNX licence server, and its port when it is not the default port. When the licence
  server runs on this machine, its host name for the server is `host.docker.internal`.

### Let the command ask you

```shell
connx-mcp-ce configure
```

Without options, the command asks you for each setting, group by group, and at the end for the CDD file. Each
question shows the current value or the server default. Press Enter to keep the value shown. Type `-` to go back to
the server default. A secret does not show on the screen while you type it. If you do not want to keep a secret in
the environment, type `?`. `start` and `restart` then ask you for it each time.

To configure an environment with a different name, give the name: `connx-mcp-ce configure sales`.

### Give the settings as options

With options, the command does not ask. It saves the environment only when each value is correct and each
necessary value is set. Use options in a script. These commands read the password from the variable
`cdd_password`, which the script fills from your secret store:

> 🐧 **Linux (bash)**
>
> ```bash
> printf '%s' "$cdd_password" | connx-mcp-ce configure sales --no-input --cdd /srv/connx/sales.cdd \
>   --set CDD_USERNAME=<login> --set CDD_PASSWORD=- --set CONNX_LICENSE_HOST=<licence-host>
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $cdd_password | connx-mcp-ce configure sales --no-input --cdd C:\connx\sales.cdd `
>   --set CDD_USERNAME=<login> --set CDD_PASSWORD=- --set CONNX_LICENSE_HOST=<licence-host>
> ```

At a keyboard, run `connx-mcp-ce configure sales` without options instead. It asks for the password without
showing it.

| Option | What it does |
|---|---|
| `--set KEY=VALUE` | Sets the value |
| `--set KEY=-` | Reads the value through the pipe, so the value does not go into your shell history |
| `--set KEY=` | Sets an empty value, for example for a CONNX login that has no password |
| `--unset KEY` | Removes the setting, so that the server default applies |

To change one setting later, give only that option:

```shell
connx-mcp-ce configure sales --set CONNX_LICENSE_PORT=7501
```

[server.env.example](server.env.example) lists all server settings with their defaults. To see the settings of an
environment, with each secret hidden:

```shell
connx-mcp-ce configure sales --show
```

### Port and network

The server uses port 8000 on this machine. Each environment that runs at the same time needs its own port:

```shell
connx-mcp-ce configure sales --port 8001
```

By default, the server port is open on all network interfaces, so that harnesses on other machines can connect.
Without OAuth sign-in, each person who can reach the port can run queries with the CONNX login of the environment.
`configure`, `status` and `guide` show a warning about this. You have two choices:

| Choice | Command |
|---|---|
| Open the port only to this machine | `connx-mcp-ce configure sales --listen localhost` |
| Accept the open network and stop the warning | `connx-mcp-ce configure sales --accept-open-network` |

### Ask-at-start secrets

An ask-at-start secret is a secret that the environment does not keep. `start` and `restart` ask you for it each
time:

```shell
connx-mcp-ce configure sales --ask-at-start CDD_PASSWORD
```

This removes the stored value. In a script, `start` can read the secret from a variable:

```shell
connx-mcp-ce start sales --no-input --secret-env CDD_PASSWORD=SALES_PASSWORD
```

## Sign in and download the image

Sign in to the container registry with the user name and token from
[Get your registry credentials](#get-your-registry-credentials):

```shell
connx-mcp-ce login
```

The command asks you for the user name and the token. It does not show the token. In a script, read the token from
a file:

> 🐧 **Linux (bash)**
>
> ```bash
> connx-mcp-ce login --username <user> --password-stdin < token.txt
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> Get-Content token.txt | connx-mcp-ce login --username <user> --password-stdin
> ```

Make sure that only you can read `token.txt`. Delete it when you do not need it.

If the registry refuses the sign-in, get the user name and token from the registry portal again.

Then download the image of the environment:

```shell
connx-mcp-ce download sales
```

`connx-mcp-ce download --all` downloads the images of all environments.

## Start, monitor and stop the server

Start the environment:

```shell
connx-mcp-ce start sales
```

The command starts the server and waits until it is healthy, by default for 180 seconds, or for 420 seconds with OAuth
or Vault ([your own identity provider and Vault](#use-https-your-own-identity-provider-and-vault)). If the server needs
more time, give a longer time with `--timeout 600`.

The first start pins the image, which means that it records the exact image. Later starts use the pinned image,
also when a newer image gets the same tag. `connx-mcp-ce update` changes the pin
([Update the image](#update-the-image)).

Monitor the server:

| Task | Command |
|---|---|
| List all environments with their state, port, version, health and drift | `connx-mcp-ce status` |
| Show one environment in detail: health report, image and certificate | `connx-mcp-ce status sales` |
| Show the server log | `connx-mcp-ce log sales` |
| Show new log lines as they arrive (press Ctrl+C to stop) | `connx-mcp-ce log sales --follow` |
| Show the log lines of the last 10 minutes | `connx-mcp-ce log sales --since 10m` |

Drift means that you changed the settings after the last start. To apply your changes, restart the environment:

```shell
connx-mcp-ce restart sales
```

You can also ask the server for its health at `/health`. This request does not need a sign-in:

> 🐧 **Linux (bash)**
>
> ```bash
> curl -s http://localhost:8000/health
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> curl.exe -s http://localhost:8000/health
> ```

Use the port of the environment. With TLS, use `https` and a host name in the certificate. Also give `curl` the
CA certificate, for example `curl -s --cacert ca.pem https://mcp.example.com:8000/health`. A healthy server sends
HTTP status 200 with `"status": "ready"`. The server sends HTTP status 503 when CONNX or Vault does not answer.

Stop the environment:

```shell
connx-mcp-ce stop sales
```

`connx-mcp-ce stop --all` stops all environments. A stopped environment keeps its settings and its image.

## Connect a harness

A harness is an AI agent application, for example Claude Code, Claude Desktop, Codex, Pi or Hermes. It needs two
things: the `connx-sql-expert` skill and the MCP URL of the server.

### Install the skill

```shell
connx-mcp-ce install-skill sales
```

The command asks you for the harnesses and selects the harnesses that it finds on this machine. It copies the skill
from the image of the environment into the skills directory that each harness reads. Each of these copies is a
skill copy. When harnesses read the same skills directory, one skill copy serves all of them.

The command also writes the skill instructions, a short section that tells the harness to load the skill before it
uses the server. The section goes into the instructions file that the harness reads. A file that two harnesses read
gets one section. Each harness reads this instructions file:

| Harness (`--harness`) | For yourself (the default) | For a project (`--project DIR`) |
|---|---|---|
| Claude Code (`claude-code`) | `~/.claude/CLAUDE.md` | `DIR/CLAUDE.md` or `DIR/AGENTS.md` |
| Codex (`codex`) | `AGENTS.md` in `CODEX_HOME` (default `~/.codex`) | `DIR/AGENTS.md` |
| Pi (`pi`) | `~/.pi/agent/AGENTS.md` | `DIR/AGENTS.md` |
| Hermes (`hermes`) | None | `DIR/AGENTS.md` |
| Shared agents directory (`agents`) | None | `DIR/AGENTS.md` |

On Windows, `~` is your user profile folder.

Some harnesses read a different file when it exists:

- Codex reads `AGENTS.override.md` in place of `AGENTS.md` when it exists in the same directory. Then the section
  goes into `AGENTS.override.md`.
- Pi reads `~/.pi/agent/CLAUDE.md` when it exists and `~/.pi/agent/AGENTS.md` does not. Then the section goes
  into `~/.pi/agent/CLAUDE.md`.
- For a project, the section for Claude Code goes into `DIR/CLAUDE.md` when `CLAUDE.md`, `.claude/CLAUDE.md` or
  `CLAUDE.local.md` is in `DIR` or in a directory above it. Your own `~/.claude/CLAUDE.md` does not count.
  Otherwise, the section goes into `DIR/AGENTS.md`. When one of these files in `DIR` imports `DIR/AGENTS.md`, the
  section goes into `DIR/AGENTS.md`.

The shared agents directory `.agents/skills` holds only the skill copy. Register the server with each harness that
reads this directory.

To name the harnesses and install for one project:

> 🐧 **Linux (bash)**
>
> ```bash
> connx-mcp-ce install-skill sales --harness claude-code --harness codex --project ~/work/sales
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> connx-mcp-ce install-skill sales --harness claude-code --harness codex --project $HOME\work\sales
> ```

Claude Desktop takes the skill as an upload. With `--harness claude-desktop`, the command writes a skill zip file
and its install notes into the current directory.

### Register the server

`install-skill` does not register the server with the harness. At the end, it shows the commands to do this for
each harness. Register the server with the name `connx`, because the skill uses this name.

The MCP URL of the server is `http://localhost:<port>/mcp`, with the port of the environment. When the server uses
TLS, it is `https://`. A harness on a different machine uses the host name of this machine in place of `localhost`.
With OAuth, use exactly `MCP_PUBLIC_BASE_URL` followed by `/mcp`.

For Claude Code, the command is for example:

```shell
claude mcp add --transport http connx http://localhost:8000/mcp
```

### Remove the skill

```shell
connx-mcp-ce uninstall-skill sales
```

Without options, the command removes all that `install-skill` installed for the environment. It shows what it
removes and asks you first. It removes only the text between the skill instructions markers, and keeps your own
text in the instructions file. It keeps a skill copy that a different harness or environment still uses, unless you
give `--shared`. The server stays registered with the harness. The command shows how to remove the registration.

## Try OAuth and Vault with the demo stack

The demo stack is a set of services on this machine: Keycloak and dex for sign-in, and HashiCorp Vault for a CONNX
login for each caller. With the demo stack, you can try OAuth sign-in and Vault without your own identity provider.

> **Warning:** The demo stack is for demonstration use only. It is not production certified. Its passwords and
> tokens are weak and fixed. Other computers can reach its services. Use it only on a trusted machine.

A machine has one demo stack. The demo stack works with one environment at a time.

### Configure the demo stack

```shell
connx-mcp-ce configure-demo --keycloak --vault --for sales
```

Choose the services with `--keycloak`, `--dex` and `--vault`. `--for` binds the demo stack to the environment. The
first run creates the users file `<base directory>/.demo/users.yaml` with four users: alice, bob, carol and dave.
To use a different users file, give `--users-file <file>`. The users file holds CONNX logins and their passwords.
Keep it private.

### Map a user to a CONNX login

The server refuses a user who has no CONNX login. Before you start the demo stack, map at least one user to a
CONNX login. In the users file, find the `connx:` line of the user. Remove the `#` at the start of the line. Then
type a CONNX login from your CDD and its password:

```yaml
  - name: alice
    id: 4bb91078-2e33-4fe4-b386-bdf1d51f1e6a
    connx: { username: <connx-login>, password: <connx-password> }
```

The password of each user is the user name. In dex, a user signs in with an email address, for example
`alice@example.com`.

### Start the demo stack

```shell
connx-mcp-ce start-demo
```

The command starts the services and waits until they are healthy. Then it shows the address of each service. When
you change the users file or the demo stack configuration, run `start-demo` again.

Vault keeps its data in memory only. After each start, Vault holds only the entries from the users file.

### Use the demo stack in an environment

```shell
connx-mcp-ce configure sales --demo keycloak --demo-vault
connx-mcp-ce restart sales
```

`--demo keycloak` or `--demo dex` sets the OAuth server settings from the demo stack, and binds the demo stack to
the environment. `--demo-vault` also sets the Vault server settings and gives the server the CA certificate of the
demo Vault.

Use plain HTTP on `localhost`, or HTTPS ([HTTPS](#https)). With OAuth, use a certificate from a CA or from your
company's internal CA. The server refuses a self-signed certificate with OAuth.

Connect a harness with the MCP URL `http://localhost:8000/mcp` ([Connect a harness](#connect-a-harness)). The harness
opens a sign-in page in your browser. Sign in as a user that you mapped. The queries of this user then use the CONNX
login of the user.

To bind the demo stack to a different environment, give `--rebind`:

```shell
connx-mcp-ce configure-demo --for test --rebind
```

Sign-in through the demo stack then fails for the old environment.

### Stop the demo stack

```shell
connx-mcp-ce stop-demo
```

The command stops the services and removes their containers. The users file, the generated files and the demo CA
stay.

`connx-mcp-ce stop-demo --purge` also deletes the generated files and the demo CA, and removes the seed image. The
users file stays. After a purge, the demo stack is bound to no environment. To use the demo stack again:

1. Bind it again, for example with `connx-mcp-ce configure-demo --keycloak --vault --for sales`.
2. Run `connx-mcp-ce start-demo`. It creates a new demo CA.
3. Restart each environment that uses the demo Vault.

### Use a demo stack on another machine

The demo stack can also run on a different machine, for example a machine that is always on. This guide calls
that machine the stack machine. The server, this machine and each browser find the demo stack by the host name of
the stack machine.

On the stack machine, configure the demo stack with its host name. Then start the demo stack:

```shell
connx-mcp-ce configure-demo --keycloak --vault --stack-host <stack-host>
connx-mcp-ce start-demo
```

On this machine, configure the environment for that demo stack:

> 🐧 **Linux (bash)**
>
> ```bash
> connx-mcp-ce configure sales --demo keycloak --demo-vault --stack-host <stack-host> --vault-ca ./stack-ca.pem
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> connx-mcp-ce configure sales --demo keycloak --demo-vault --stack-host <stack-host> --vault-ca .\stack-ca.pem
> ```

- `--stack-host`: give the same host name as on the stack machine. A short name and the full DNS name are
  different. `configure` gets the issuer from the demo stack. When the issuer is for a different name, `configure`
  refuses with exit code 190. When the demo stack does not answer, `configure` only shows a warning.
- Host name: the server container uses the DNS of Docker, which can ignore the hosts file of this machine. Use a
  DNS name.
- `--vault-ca`: copy the file `<base directory>/.demo/stack/generated/tls/ca.pem` from the stack machine to this
  machine. Give the path of the copy.
- Vault token: `--demo-vault` sets `VAULT_TOKEN` to the read token of the demo stack. The demo stack that
  `connx-mcp-ce` starts always uses its published default read token. For this reason, `configure` warns: each
  computer that can reach Vault on the stack machine can read the CONNX logins of the demo users.
- `--stack-port`: give the ports of the demo stack when they are not 8080, 5556 and 8200, for example
  `--stack-port keycloak=9080`.
- Firewall: this machine and the server container must reach Keycloak or dex and Vault on the stack machine. Each
  browser that signs in must reach Keycloak or dex. Let only these machines through the firewall of the stack
  machine to these ports. `configure` shows a warning about this.
- Users: the users file and the CONNX logins of the users are on the stack machine. Change them there. Then run
  `start-demo` on the stack machine again.
- Server URL: the demo stack signs in callers for one server URL, by default `http://localhost:8000`. For a
  different URL, do these steps on the stack machine:
  1. Configure an environment with the same URL as the environment on this machine. Give it the same
     `MCP_PUBLIC_BASE_URL`. Without `MCP_PUBLIC_BASE_URL`, give it the same port and the same TLS setting.
  2. Bind the demo stack to it with `connx-mcp-ce configure-demo --for <name>`.
  3. Run `connx-mcp-ce start-demo` again.

`status` and `guide` examine the demo stack from this machine. `start-demo` and `stop-demo` do not change a demo
stack on another machine. To stop the use of that demo stack and remove the settings that it set:

```shell
connx-mcp-ce configure sales --stack-host ''
```

## Use HTTPS, your own identity provider and Vault

For production, use HTTPS, your own identity provider and your own HashiCorp Vault. The
[TLS, OAuth and Vault reference](oauth-and-vault.md) gives all server settings for them. Do the steps in
[this order](oauth-and-vault.md#order-of-the-steps): HTTPS, then OAuth, then Vault.

This section gives the settings as options. `connx-mcp-ce configure sales` without options also asks you for them.

### HTTPS

Put the certificate from your CA and its private key into one directory: one `.crt` file and one `.key` file.
Then give the directory and the host names that harnesses use:

> 🐧 **Linux (bash)**
>
> ```bash
> connx-mcp-ce configure sales --certs /srv/connx/certs --set MCP_TLS_ENABLED=true \
>   --set MCP_TLS_HOSTS=mcp.example.com
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> connx-mcp-ce configure sales --certs C:\connx\certs --set MCP_TLS_ENABLED=true `
>   --set MCP_TLS_HOSTS=mcp.example.com
> ```

When the private key has a password, give it through the pipe with `--set MCP_TLS_KEY_PASSWORD=-`. When the directory is
empty, the server generates a self-signed certificate. The server refuses a self-signed certificate with OAuth.

The MCP URL then starts with `https://`. Every host that a harness connects to must be in the certificate.
Every harness must trust the CA of the certificate. [TLS (HTTPS)](oauth-and-vault.md#tls-https) gives the rules.

### Your identity provider

Set `MCP_OAUTH_PROVIDER`, `MCP_PUBLIC_BASE_URL` and the settings of your provider.

`MCP_PUBLIC_BASE_URL` is the URL of the server for harnesses, without a path. It must lead to the port of the
environment. Use a reverse proxy, give the environment `--port 443`, or put the port into the URL, for example
`https://mcp.example.com:8000`.

This example is for Microsoft
Entra ID. It reads the client secret from the variable `client_secret`, which you fill from your secret store:

> 🐧 **Linux (bash)**
>
> ```bash
> printf '%s' "$client_secret" | connx-mcp-ce configure sales --set MCP_OAUTH_PROVIDER=azure \
>   --set MCP_PUBLIC_BASE_URL=https://mcp.example.com --set MCP_OAUTH_TENANT_ID=<tenant-id> \
>   --set MCP_OAUTH_CLIENT_ID=<client-id> --set MCP_OAUTH_CLIENT_SECRET=- \
>   --set MCP_OAUTH_REQUIRED_SCOPES=access_as_user
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $client_secret | connx-mcp-ce configure sales --set MCP_OAUTH_PROVIDER=azure `
>   --set MCP_PUBLIC_BASE_URL=https://mcp.example.com --set MCP_OAUTH_TENANT_ID=<tenant-id> `
>   --set MCP_OAUTH_CLIENT_ID=<client-id> --set MCP_OAUTH_CLIENT_SECRET=- `
>   --set MCP_OAUTH_REQUIRED_SCOPES=access_as_user
> ```

The reference gives the settings and the preparation for each provider:

| Provider and its settings | `MCP_OAUTH_PROVIDER` |
|---|---|
| [Keycloak](oauth-and-vault.md#keycloak-keycloak) | `keycloak` |
| [Microsoft Entra ID](oauth-and-vault.md#microsoft-entra-id-azure) | `azure` |
| [Google](oauth-and-vault.md#google-google) | `google` |
| [AWS Cognito](oauth-and-vault.md#aws-cognito-aws) | `aws` |
| [Other OpenID Connect provider](oauth-and-vault.md#other-openid-connect-provider-generic-oidc) | `generic-oidc` |

With `azure`, `google`, `aws` and `generic-oidc`, register the server with your provider. The redirect URI is in
[Proxy mode](oauth-and-vault.md#proxy-mode). If you do not want the environment to keep the client secret, make it
an ask-at-start secret: `connx-mcp-ce configure sales --ask-at-start MCP_OAUTH_CLIENT_SECRET`.

### Your Vault

Set the Vault server settings. Give the read-only Vault token through the pipe. This example reads the token
from the variable `read_token`, which you fill from your secret store:

> 🐧 **Linux (bash)**
>
> ```bash
> printf '%s' "$read_token" | connx-mcp-ce configure sales --set MCP_CREDENTIAL_PROVIDER=hashicorp \
>   --set VAULT_ADDR=https://vault.example.com:8200 --set VAULT_TOKEN=- --vault-ca /srv/connx/vault-ca.pem
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $read_token | connx-mcp-ce configure sales --set MCP_CREDENTIAL_PROVIDER=hashicorp `
>   --set VAULT_ADDR=https://vault.example.com:8200 --set VAULT_TOKEN=- --vault-ca C:\connx\vault-ca.pem
> ```

When an internal CA issued the certificate of Vault, give the CA certificate on this machine with `--vault-ca`.
At each start, `connx-mcp-ce` mounts the file into the container and sets `VAULT_CACERT`. Do not set
`VAULT_CACERT` yourself. `--vault-ca ''` removes the setting and the mount. It does not delete the file.

The reference tells you how to prepare Vault:
[the Vault settings](oauth-and-vault.md#vault-per-caller-connx-logins),
[the policy and the token](oauth-and-vault.md#vault-policy-and-token) and
[an entry for each caller](oauth-and-vault.md#caller-entries).

### Start with the new settings

```shell
connx-mcp-ce restart sales
```

When the server starts, it waits for Vault, if you use it. With `aws` or `generic-oidc`, it also waits for the
discovery document of your identity provider. When one of them does not answer, the server tries again for
`MCP_STARTUP_WAIT_SECONDS` seconds (default 120). Then it stops. When Vault refuses the token, the server stops
at once. [Startup and the container health check](oauth-and-vault.md#startup-and-the-container-health-check) gives the
details.

Make sure that the server container can find and connect to your identity provider and Vault. When the server
stops, `connx-mcp-ce log sales` shows the cause.

## Run more environments

Make one environment for each CDD. Each environment has its own settings, its own server container and its own
pinned image ([Start, monitor and stop the server](#start-monitor-and-stop-the-server)).

Configure the second environment as in [Configure an environment](#configure-an-environment), with its own CDD file.
Give it a port that no other environment uses ([Port and network](#port-and-network)). Download its image. Then start
the environment:

```shell
connx-mcp-ce configure hr
connx-mcp-ce configure hr --port 8001
connx-mcp-ce download hr
connx-mcp-ce start hr
```

The MCP URL of each environment uses its own port, for example `http://localhost:8001/mcp` for `hr`. Register each
environment with its harness separately ([Register the server](#register-the-server)).

`connx-mcp-ce status` lists all environments. `connx-mcp-ce download --all` and `connx-mcp-ce stop --all` act on
all environments.

When you work with one environment for some time, put its name in the variable `CONNX_MCP_ENVIRONMENT`. Then you can
leave out the name in each command:

> 🐧 **Linux (bash)**
>
> ```bash
> export CONNX_MCP_ENVIRONMENT=hr
> connx-mcp-ce status
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $env:CONNX_MCP_ENVIRONMENT = 'hr'
> connx-mcp-ce status
> ```

A name on the command line has priority over `CONNX_MCP_ENVIRONMENT`. While `CONNX_MCP_ENVIRONMENT` names an
environment, `connx-mcp-ce status` shows only that environment. To list all environments, use
`connx-mcp-ce status --all`.

## Maintain an environment

### Update the image

```shell
connx-mcp-ce update sales
```

The command downloads the newest image for the environment and pins it. When the server runs, the command asks you
if it can restart the server on the new image. Give `--restart` or `--no-restart` so that the command does not ask.
Without a restart, the new image applies at the next start. The command refuses an image that is older than the
oldest image that this `connx-mcp-ce` supports. Then it keeps the old pin.

### Use another server image

By default, `connx-mcp-ce` uses the image `softwareagcr.azurecr.io/connx-mcp-server-ce:15.0`. You can use an image
from another registry, or another version, without a new release of `connx-mcp-ce`. These settings choose the
image:

| Setting | What it sets | Default |
|---|---|---|
| `connx-mcp-ce configure sales --image <reference>` | The image of one environment. `--image ''` removes it | None |
| `CONNX_MCP_REGISTRY` environment variable | The registry of the default image | `softwareagcr.azurecr.io` |
| `CONNX_MCP_IMAGE` environment variable | The name of the default image | `connx-mcp-server-ce` |
| `CONNX_MCP_IMAGE_TAG` environment variable | The tag of the default image | `15.0` |

The image reference of an environment starts with its registry, for example `docker.io/...` for Docker Hub. The
image of an environment has priority over the three variables.

The default image is `<registry>/<name>:<tag>`. Each variable replaces only its part of the image. For example,
`CONNX_MCP_REGISTRY=registry.example:5000/connx` takes the image from that registry, and
`CONNX_MCP_IMAGE_TAG=15.0.1` takes another version.

`connx-mcp-ce login` signs in to the registry of the default image. For an environment image from another
registry, give that registry with `--registry <host>`.

The first start of an environment pins its image. A later change applies only when you run
`connx-mcp-ce update sales`. `connx-mcp-ce status sales` shows the image that the next update downloads, and where
this value comes from.

### Copy the settings of an environment

`export` shows the settings of an environment. With `--output`, it writes them to a file that only you can read:

```shell
connx-mcp-ce export sales --output sales.env
```

The file contains the server settings and the host settings, for example the CDD file and the port. It does not
contain the stored secrets. To include them, give `--include-secrets` with `--output`. Keep that file private.
Ask-at-start secrets are never in the file.

`configure --from-env-file` reads the file into an environment. Use it to make a copy of an environment, or to move
an environment to a different machine. The options on the command line have priority over the values in the file:

```shell
connx-mcp-ce configure sales-test --no-input --from-env-file sales.env --port 8002
```

Each secret that is not in the file becomes an ask-at-start secret, unless the environment already has a value for it.
To keep it in the environment, set it with `--set KEY=-`
([Give the settings as options](#give-the-settings-as-options)).

### Remove an environment

Before you remove an environment, copy its directory as a backup.

```shell
connx-mcp-ce remove sales-test
```

The command asks you to confirm. Then it stops the server, removes its container and deletes the directory of the
environment, with its settings and your `compose.override.yml`. Give `--yes` so that the command does not ask.

When the demo stack is bound to the environment ([Configure the demo stack](#configure-the-demo-stack)), the command
refuses with exit code 141. Give `--force` to remove the environment. The demo stack is then bound to no environment.

The command keeps:

- The images, because other environments can use them.
- The Docker volumes of the environment. They keep the certificate that the server generated and the OAuth sign-in
  state. A new environment with the same name uses them again. Give `--purge` to delete them also.
- The skill copies, skill instructions and skill zip files that `install-skill` installed for the environment. The
  command lists them with the `uninstall-skill` commands that remove them. Delete a skill zip file and the
  `connx-sql-expert-install.txt` file next to it yourself. A shared copy stays, because a different environment
  still uses it. Only `uninstall-skill --shared` removes it. To remove them, run `uninstall-skill` before you run
  `remove`.

## Fix problems

Each failure shows a message that starts with `Error:`. Usually, a last line that starts with `Next:` gives the
next command to run. `connx-mcp-ce exit-codes` tells you what each exit code means.

### The registry refuses the sign-in

When the registry refuses the user name or the token, `connx-mcp-ce login` stops with exit code 44. The message
starts with this line:

```text
Error: softwareagcr.azurecr.io refused the sign-in of <user>: <the message of Docker>
```

The next line tells you to sign in again with `connx-mcp-ce login`, and that the registry credentials come from
Empower or Software AG support. The line after it starts with `registry:` and shows the registry and where it comes
from. When you give a different registry with `--registry` or the variable `CONNX_MCP_REGISTRY`, the message shows
that registry in place of `softwareagcr.azurecr.io`. The last line is `Next: connx-mcp-ce login`.

The user name or the token is not correct, or the token is expired. `download` and `update` stop with the same exit
code when the registry refuses the download. To fix the problem:

1. Get the user name and the token from the **Profile** page of the registry portal again
   ([Get your registry credentials](#get-your-registry-credentials)).
2. Run `connx-mcp-ce login` again.

### The server does not start or is not healthy

`start` and `restart` wait until the server is healthy. If the server stops, or is not healthy after the timeout,
the command stops with one of these exit codes. For exit codes 32 and 33, the message also shows the last lines of
the server log:

| Exit code | Cause | What to do |
|---|---|---|
| 49 | On Linux, other users cannot read the CDD or Vault CA file | Run the `chmod 644` command that the message gives |
| 32 | The server stops before it is healthy | Read the log lines. They usually name the wrong setting or file |
| 33 | The server is not healthy after the timeout. It continues to run | Read the `/health` answer and the log lines |
| 50 | The server cannot open the CDD file | Make sure that the CDD file is correct and that the server can read it |
| 51 | The server cannot use the Vault CA file | Make sure that the file is correct and that the server can read it |
| 71 | The certificate that the server generated has expired | Run the commands that the message gives. They renew it |

The commands for exit code 71 delete the certificate volume.

To see more of the log, and the health report of the server:

```shell
connx-mcp-ce log sales --tail 200
connx-mcp-ce status sales
```

You can also ask the server for `/health` with `curl`
([Start, monitor and stop the server](#start-monitor-and-stop-the-server)). HTTP status 503 tells you that the server
cannot connect to CONNX or to Vault. Make sure that the licence server, the CDD file and the CONNX login are correct.
Then run `connx-mcp-ce restart sales`. When a dependency is slow, give more time to the start, for example
`connx-mcp-ce start sales --timeout 600`. Later starts of the environment use the same time.

### The port is in use

`start` stops when a different program already uses the port of the environment. What it shows depends on that
program:

| Program that uses the port | Exit code | Message |
|---|---|---|
| Another environment that runs | 90 | Names the environment, and gives a `configure --port` command with a free port |
| Any other program | 31 | The error message of Docker |

Give the environment a different port, or stop the other program. Then start the environment again. For exit code
90, use the command from the last line of the message, for example:

```shell
connx-mcp-ce configure hr --port 8001
connx-mcp-ce start hr
```

### A harness shows `CREDENTIALS_NOT_RESOLVED`

The server cannot find a CONNX login for the caller in Vault. The server log names the cause. When the caller has
no entry, the log line also gives the Vault path of the entry to create:

```shell
connx-mcp-ce log sales --since 10m
```

[Caller entries](oauth-and-vault.md#caller-entries) tells you how to create an entry.
[When the server refuses a caller](oauth-and-vault.md#when-the-server-refuses-a-caller) explains each cause.
