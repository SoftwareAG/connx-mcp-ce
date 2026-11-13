<!--
Set up the CONNX MCP Server Community Edition with Docker commands only, without connx-mcp-ce.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
-->

# Set up the CONNX MCP Server with Docker only

The preferred way to set up the server is [connx-mcp-ce](setup-with-cli.md), the command-line tool for
administrators. It does all the steps of this guide for you, examines your settings before the server starts, and
installs the skill for your harnesses. Use this guide only when you cannot use `connx-mcp-ce`.

This guide gives the Docker commands that start one server container. Where bash and PowerShell commands are
different, this guide shows the two. Replace each `<...>` part with your own value. Replace the example paths, such
as `/srv/connx/sales.cdd` and `C:\connx\sales.cdd`, with the paths of your files.

## Before you start

Make sure that you have each item in the [What you need](getting-started.md#what-you-need) list.
Docker must run Linux containers on an amd64 (x86-64) machine. On Windows, use Windows 11 or Windows Server 2022.
Docker runs the Linux containers there in WSL 2.

**Rancher Desktop:** in **Preferences > Container Engine > General**, select the **dockerd (moby)** container engine.
With the containerd engine, Rancher Desktop does not supply the `docker` command.

The server starts only when you accept its end-user licence agreement (EULA). The `docker run` commands of this
guide accept it with `--env ACCEPT_EULA=y`.

The commands in this guide use the variable `IMAGE` for the server image. First, set it to the image tag:

> 🐧 **Linux (bash)**
>
> ```bash
> export IMAGE=softwareagcr.azurecr.io/connx-mcp-server-ce:15.0
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $env:IMAGE = 'softwareagcr.azurecr.io/connx-mcp-server-ce:15.0'
> ```

The commands also use the container name `connx-mcp-ce`. To run more than one server on this machine, give each
container its own name, its own port and its own volume names.

## Get your registry credentials

The server image is in the container registry `softwareagcr.azurecr.io`. To download it, you need a registry user
name and token:

1. Sign in to the registry portal at <https://containers.softwareag.com> with your Empower credentials or the
   credentials that Software AG support gave you.
2. Open the **Profile** page.
3. Copy your registry user name and your token.

Keep the token private.

## Sign in and download the image

Sign in to the container registry. Docker asks you for the token. It does not show the token:

```shell
docker login softwareagcr.azurecr.io --username <user>
```

In a script, read the token from a file. The file holds the token as plain text. Make sure that only you can
read `token.txt`. These commands do this, then sign in:

> 🐧 **Linux (bash)**
>
> ```bash
> chmod 600 token.txt
> docker login softwareagcr.azurecr.io --username <user> --password-stdin < token.txt
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> icacls token.txt /inheritance:r /grant:r "${env:USERNAME}:F"
> Get-Content token.txt | docker login softwareagcr.azurecr.io --username <user> --password-stdin
> ```

If the registry refuses the sign-in, get the user name and token from the registry portal again.

Delete `token.txt` when you do not need it:

> 🐧 **Linux (bash)**
>
> ```bash
> rm token.txt
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> Remove-Item token.txt
> ```

Then download the image, and set `IMAGE` to its digest:

> 🐧 **Linux (bash)**
>
> ```bash
> docker pull "$IMAGE"
> export IMAGE=$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")
> echo "$IMAGE"
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> docker pull $env:IMAGE
> $env:IMAGE = docker image inspect --format '{{index .RepoDigests 0}}' $env:IMAGE
> $env:IMAGE
> ```

The digest identifies this one build of the image. The tag `15.0` moves to each new build, but the digest does not
change. A later `docker pull` does not change the server that you start with `IMAGE`. Write down the value that
the last command shows. You need it to roll back to this build.

## Make a settings file

The server reads its server settings when it starts. Docker gives them to the container from a settings file.
[server.env.example](server.env.example) lists all server settings with their defaults.

1. Copy `server.env.example` to a new file `server.env`.
2. Set `CDD_USERNAME` to the CONNX login that the server uses.
3. Set `CDD_PASSWORD` to the password of this login. For a login without a password, keep the value empty.
4. Remove the `# ` at the start of the `CONNX_LICENSE_HOST` line.
5. Set `CONNX_LICENSE_HOST` to the host name of your CONNX licence server. When the licence server runs on this
   machine, use `host.docker.internal`.
6. When the licence server does not use the default port, also remove the `# ` at the start of the
   `CONNX_LICENSE_PORT` line, and set it.

The minimum file has these lines:

```text
CDD_USERNAME=<login>
CDD_PASSWORD=<password>
CONNX_LICENSE_HOST=<licence-host>
```

Docker takes each value exactly as it is in the file. Do not put quotes around a value. Quotes become part of the
value. Do not set `MCP_PORT` or `MCP_HOST` in the file. The `docker run` command sets `MCP_PORT`. The image sets
`MCP_HOST`.

The file holds a password. Make sure that only you can read it:

> 🐧 **Linux (bash)**
>
> ```bash
> chmod 600 server.env
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> icacls server.env /inheritance:r /grant:r "${env:USERNAME}:F"
> ```

Docker keeps a copy of each value in the container. Each user who can use Docker on this machine can see the
passwords and tokens with `docker inspect`. Give access to Docker only to administrators.

## Start the server

On Linux, the server in the container runs as a different user. Let other users read the CDD file:

> 🐧 **Linux (bash)**
>
> ```bash
> chmod 644 /srv/connx/sales.cdd
> ```

Start the container. Give the path of your CDD file on this machine:

> 🐧 **Linux (bash)**
>
> ```bash
> docker run --detach --name connx-mcp-ce --platform linux/amd64 --restart unless-stopped \
>   --env-file server.env --env ACCEPT_EULA=y --env MCP_PORT=8000 --publish 127.0.0.1:8000:8000 \
>   --add-host host.docker.internal:host-gateway --log-opt max-size=10m --log-opt max-file=5 \
>   --volume /srv/connx/sales.cdd:/opt/softwareag/connx/ini/mcp.cdd:ro \
>   "$IMAGE"
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> docker run --detach --name connx-mcp-ce --platform linux/amd64 --restart unless-stopped `
>   --env-file server.env --env ACCEPT_EULA=y --env MCP_PORT=8000 --publish 127.0.0.1:8000:8000 `
>   --add-host host.docker.internal:host-gateway --log-opt max-size=10m --log-opt max-file=5 `
>   --volume C:\connx\sales.cdd:/opt/softwareag/connx/ini/mcp.cdd:ro `
>   $env:IMAGE
> ```

| Part of the command | What it does |
|---|---|
| `--detach` | Runs the container in the background |
| `--name connx-mcp-ce` | Gives the container the name that the other commands of this guide use |
| `--platform linux/amd64` | Runs the amd64 image. The server image is for amd64 only |
| `--restart unless-stopped` | Starts the server again after a failure, and when Docker starts |
| `--env-file server.env` | Gives the server settings to the server |
| `--env ACCEPT_EULA=y` | Accepts the end-user licence agreement. The server starts only with it |
| `--env MCP_PORT=8000` | Sets the server port in the container to 8000 |
| `--publish 127.0.0.1:8000:8000` | Makes the server reachable at port 8000 of this machine, from this machine only |
| `--add-host host.docker.internal:host-gateway` | Lets the server find a licence server on this machine |
| `--log-opt max-size=10m --log-opt max-file=5` | Keeps at most 5 log files of 10 MB. Docker deletes the oldest file |
| `--volume <CDD file>:/opt/softwareag/connx/ini/mcp.cdd:ro` | Gives the CDD file to the server, read-only |

To use a different port on this machine, change the first port number, for example
`--publish 127.0.0.1:8001:8000`. Then use that port in the `/health` URL and the MCP URL.

The server log contains the SQL queries of the callers. Set the log limits to agree with the rules of your company
for this data.

To limit the memory of the container, add `--memory <size>`, for example `--memory 4g`.

To let harnesses on other machines connect, open the port on all network interfaces with `--publish 8000:8000`.
Without OAuth sign-in, each person who can reach the port can run SQL queries with the CONNX login of the settings
file. Open the port on all network interfaces only with OAuth:
[Use HTTPS, OAuth and Vault](#use-https-oauth-and-vault).

## Monitor the server

Use these commands to watch the server:

| Task | Command |
|---|---|
| Show the container, its state and its port | `docker ps --all --filter name=^connx-mcp-ce$` |
| Show the health of the container | `docker inspect --format '{{.State.Health.Status}}' connx-mcp-ce` |
| Show how many times Docker started the server again | `docker inspect --format '{{.RestartCount}}' connx-mcp-ce` |
| Show the server log | `docker logs connx-mcp-ce` |
| Show new log lines as they arrive (press Ctrl+C to stop) | `docker logs --follow connx-mcp-ce` |
| Show the log lines of the last 10 minutes | `docker logs --since 10m connx-mcp-ce` |

The health of the container is `starting` until the server is ready, then `healthy`. When the health check fails
again and again, it is `unhealthy`.

The server first builds its data catalog from the CDD. Its port accepts connections only when the catalog is
complete.

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

Use the port on this machine. When the server uses TLS, use `https` and a host name in the certificate. Also give
`curl` the CA certificate that issued the server certificate:

> 🐧 **Linux (bash)**
>
> ```bash
> curl -s --cacert ca.pem https://mcp.example.com:8000/health
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> curl.exe -s --cacert ca.pem https://mcp.example.com:8000/health
> ```

Do not turn off the certificate check of `curl`. A healthy server sends HTTP status 200 with `"status": "ready"`.
The server sends HTTP status 503 when CONNX or Vault does not answer.

When the server cannot start, Docker starts it again and again. `docker ps` then shows the state `Restarting`, and
the restart count increases. Read the server log. It usually names the wrong setting or file. Make sure that the
licence server, the CDD file and the CONNX login are correct.

When `docker run` reports that the name or the port is in use, remove the old container or use another port.

## Stop and remove the server

Use these commands to stop, start and remove the container:

| Task | Command |
|---|---|
| Stop the server | `docker stop connx-mcp-ce` |
| Start the stopped server again | `docker start connx-mcp-ce` |
| Remove the stopped container | `docker rm connx-mcp-ce` |

### Change the settings

The container reads `server.env` only when you create it with `docker run`. The `docker run` command needs
`IMAGE`. In a new shell, set it to the digest of the container that runs:

> 🐧 **Linux (bash)**
>
> ```bash
> export IMAGE=$(docker inspect --format '{{.Config.Image}}' connx-mcp-ce)
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $env:IMAGE = docker inspect --format '{{.Config.Image}}' connx-mcp-ce
> ```

To apply a change of the settings file:

1. Stop the container.
2. Remove the container.
3. Run the `docker run` command again.

### Use a newer image

1. Write down the digest that the server uses now: `docker inspect --format '{{.Config.Image}}' connx-mcp-ce`.
2. Set `IMAGE` to the image tag again, as in [Before you start](#before-you-start).
3. Download the image, and set `IMAGE` to its digest, as in
   [Sign in and download the image](#sign-in-and-download-the-image).
4. Stop and remove the container.
5. Run the `docker run` command again.
6. Delete the old skill copy of each harness, and copy the skill from the new image, as in
   [Connect a harness](#connect-a-harness). In each instructions file, replace the old skill instructions with the
   text of the new `INSTRUCTIONS.md`.

### Roll back

To roll back to the previous build:

1. Set `IMAGE` to the digest that you wrote down before the update.
2. Stop and remove the container.
3. Run the `docker run` command again. When Docker no longer has that build, it downloads it again.
4. Replace the skill and the skill instructions, as in step 6 of [Use a newer image](#use-a-newer-image).

### Remove the volumes

If you added the named volumes of [Use HTTPS, OAuth and Vault](#use-https-oauth-and-vault), `docker rm` keeps
them. To delete them too:

```shell
docker volume rm connx-mcp-ce-certs connx-mcp-ce-oauth-state
```

Name only the volumes that you created.

## Connect a harness

A harness is an AI agent application, for example Claude Code, Claude Desktop, Codex, Pi or Hermes. It needs the
`connx-sql-expert` skill, the skill instructions and the MCP URL of the server.

### Copy the skill and the skill instructions from the image

The image contains the skill and the skill instructions for its server version. Copy them out of the image into
the current directory. `docker create` makes a container that does not start. `docker rm` removes it after the
copy. `-L` copies the file that a link points to, not the link:

> 🐧 **Linux (bash)**
>
> ```bash
> docker create --name connx-mcp-ce-skill --platform linux/amd64 "$IMAGE"
> docker cp connx-mcp-ce-skill:/opt/softwareag/connx/mcp/src/skills/connx-sql-expert ./connx-sql-expert
> docker cp -L connx-mcp-ce-skill:/opt/softwareag/connx/mcp/doc/INSTRUCTIONS.md ./INSTRUCTIONS.md
> docker rm connx-mcp-ce-skill
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> docker create --name connx-mcp-ce-skill --platform linux/amd64 $env:IMAGE
> docker cp connx-mcp-ce-skill:/opt/softwareag/connx/mcp/src/skills/connx-sql-expert .\connx-sql-expert
> docker cp -L connx-mcp-ce-skill:/opt/softwareag/connx/mcp/doc/INSTRUCTIONS.md .\INSTRUCTIONS.md
> docker rm connx-mcp-ce-skill
> ```

### Put the skill where the harness reads it

Copy the directory `connx-sql-expert` into the skills directory of each harness. On Windows, `~` is your user
profile folder.

| Harness | For yourself | For a project `<project>` |
|---|---|---|
| Claude Code | `~/.claude/skills` | `<project>/.claude/skills` |
| Codex | `~/.agents/skills` | `<project>/.agents/skills` |
| Pi | `~/.agents/skills` | `<project>/.agents/skills` |
| Hermes | `~/.hermes/skills`, on Windows `%LOCALAPPDATA%\hermes\skills` | `<project>/.agents/skills` |

When harnesses read the same skills directory, one skill copy serves all of them. Hermes loads the skills of a
project only after you run `hermes skills trust` in that project.

For example, these commands copy the skill for Claude Code for yourself:

> 🐧 **Linux (bash)**
>
> ```bash
> mkdir -p ~/.claude/skills
> cp -r connx-sql-expert ~/.claude/skills/
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> New-Item -ItemType Directory -Force $HOME\.claude\skills
> Copy-Item -Recurse connx-sql-expert $HOME\.claude\skills\
> ```

Claude Desktop takes the skill as a zip file with the directory `connx-sql-expert` in it:

> 🐧 **Linux (bash)**
>
> ```bash
> zip -r connx-sql-expert.zip connx-sql-expert
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> Compress-Archive -Path connx-sql-expert -DestinationPath connx-sql-expert.zip
> ```

To add the zip file to Claude Desktop:

1. Open **Settings > Capabilities**.
2. Turn on **Code execution and file creation**.
3. Open **Customize > Skills**.
4. Choose **+ > Create skill > Upload a skill**.
5. Upload the zip file.

### Add the skill instructions

The skill instructions tell the harness to load the skill before it uses the server. Paste the text of
`INSTRUCTIONS.md` at the end of the instructions file that the harness reads. A file that two harnesses read needs
the text one time only. Each harness reads this instructions file:

| Harness | For yourself | For a project `<project>` |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `<project>/CLAUDE.md` or `<project>/AGENTS.md` |
| Codex | `AGENTS.md` in `CODEX_HOME` (default `~/.codex`) | `<project>/AGENTS.md` |
| Pi | `~/.pi/agent/AGENTS.md` | `<project>/AGENTS.md` |
| Hermes | None | `<project>/AGENTS.md` |

Some harnesses read a different file when it exists:

- Codex reads `AGENTS.override.md` in place of `AGENTS.md` when it exists in the same directory. Then the text
  goes into `AGENTS.override.md`.
- Pi reads `~/.pi/agent/CLAUDE.md` when it exists and `~/.pi/agent/AGENTS.md` does not. Then the text goes into
  `~/.pi/agent/CLAUDE.md`.
- For a project, the text for Claude Code goes into `<project>/CLAUDE.md` when `CLAUDE.md`, `.claude/CLAUDE.md` or
  `CLAUDE.local.md` is in `<project>` or in a directory above it. Your own `~/.claude/CLAUDE.md` does not count.
  Otherwise, the text goes into `<project>/AGENTS.md`. When one of these files in `<project>` imports
  `<project>/AGENTS.md`, the text goes into `<project>/AGENTS.md`.

For example, these commands add the text for Claude Code for yourself. They add an empty line before the text, and
write UTF-8 without a byte order mark:

> 🐧 **Linux (bash)**
>
> ```bash
> printf '\n' >> ~/.claude/CLAUDE.md
> cat INSTRUCTIONS.md >> ~/.claude/CLAUDE.md
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $text = "`n" + (Get-Content -Raw -Encoding UTF8 INSTRUCTIONS.md)
> [IO.File]::AppendAllText("$HOME\.claude\CLAUDE.md", $text)
> ```

For Claude Desktop, open the project that you use with the server. Then paste the text into its project
instructions.

### Register the server

Register the server with the name `connx`, because the skill uses this name.

The MCP URL of the server is `http://localhost:<port><MCP_PATH>`, with the port on this machine. `MCP_PATH` is
`/mcp` when you do not set it in `server.env`. When the server uses TLS, the MCP URL starts with `https://`. A
harness on a different machine uses the host name of this machine in place of `localhost`. With OAuth, use exactly
`MCP_PUBLIC_BASE_URL` followed by `MCP_PATH`.

Register the server with each harness with this command:

| Harness | Command |
|---|---|
| Claude Code | `claude mcp add --transport http connx <MCP URL>` |
| Codex | `codex mcp add connx --url <MCP URL>` |
| Pi | `pi mcp add connx --url <MCP URL>` |
| Hermes | `hermes mcp add connx --url <MCP URL>`, with OAuth also `--auth oauth` |
| Claude Desktop | **Customize > Connectors**, then **+ Add > Add custom connector** with the name `connx` |

With OAuth, you also sign in from each harness:

| Harness | Sign-in |
|---|---|
| Claude Code | The first use opens your browser |
| Codex | `codex mcp login connx` |
| Pi | `pi mcp login connx` |
| Hermes | `hermes mcp login connx` |

Claude Desktop connects to a custom connector from the Anthropic cloud. The MCP URL must then be a public HTTPS URL
with a certificate from a public CA. A self-signed certificate or a certificate from an internal CA does not work.
Use OAuth for this server. Without OAuth, each person on the internet who knows the URL can run SQL queries with the
CONNX login of the settings file.

## Use HTTPS, OAuth and Vault

For production, use HTTPS, your own identity provider and your own HashiCorp Vault. The
[TLS, OAuth and Vault reference](oauth-and-vault.md) gives all server settings for them. Put these settings into
`server.env`. Do the steps in [this order](oauth-and-vault.md#order-of-the-steps): HTTPS, then OAuth, then Vault.

Each step adds options to the `docker run` command:

| Step | Add to `docker run` |
|---|---|
| HTTPS | `--volume <certificate directory>:/opt/softwareag/connx/mcp/certs:ro` |
| HTTPS with a certificate that the server generates | `--volume connx-mcp-ce-certs:/opt/softwareag/connx/mcp/certs` |
| OAuth in proxy mode | `--volume connx-mcp-ce-oauth-state:/opt/softwareag/connx/mcp/oauth-state` |
| OAuth or Vault | `--health-start-period=300s` |
| Vault with a CA certificate of your company | `--volume <CA file>:/opt/softwareag/connx/mcp/vault-ca.pem:ro` |

- HTTPS: put the certificate from your CA and its private key into one directory: one `.crt` file and one `.key`
  file. Set `MCP_TLS_ENABLED=true` and `MCP_TLS_HOSTS=<host names>`. When the directory is empty, the server
  generates a self-signed certificate. The server refuses a self-signed certificate with OAuth.
  [TLS (HTTPS)](oauth-and-vault.md#tls-https) gives the rules.
- Private key: the server in the container must read the `.key` file. On Linux, let other users read it, like the
  CDD file. Encrypt the private key with a password, and set `MCP_TLS_KEY_PASSWORD` in `server.env`.
- OAuth: `MCP_PUBLIC_BASE_URL` is the URL of the server for harnesses, without a path. It must lead to the port of
  the server. Use a reverse proxy, publish the server on port 443, or put the port into the URL, for example
  `https://mcp.example.com:8000`.
- OAuth in [proxy mode](oauth-and-vault.md#proxy-mode): the server stores the sign-ins in `MCP_OAUTH_STORAGE_DIR`.
  Leave this setting at its default, and mount a volume on that directory. Without a volume, every caller must
  sign in again whenever the container is recreated.
- Health check: with the OAuth provider `aws` or `generic-oidc`, or with Vault, the server waits for these services
  when it starts. The start period of 60 seconds in the image is then too short.
  [Startup and the container health check](oauth-and-vault.md#startup-and-the-container-health-check) explains the
  300 seconds.
- Vault CA: when an internal CA issued the certificate of Vault, also set
  `VAULT_CACERT=/opt/softwareag/connx/mcp/vault-ca.pem` in `server.env`. On Linux, let other users read the CA
  file, like the CDD file. The CA file holds no secret.
- Vault token: give the server a periodic token in `VAULT_TOKEN`. The server renews it while it runs. A token
  without a period ends at its max TTL. The server then cannot read the CONNX logins of the callers. `/health`
  sends HTTP status 503. [Vault policy and token](oauth-and-vault.md#vault-policy-and-token) tells you how to
  create a periodic token.

This example `docker run` command uses HTTPS, OAuth and Vault:

> 🐧 **Linux (bash)**
>
> ```bash
> docker run --detach --name connx-mcp-ce --platform linux/amd64 --restart unless-stopped \
>   --env-file server.env --env ACCEPT_EULA=y --env MCP_PORT=8000 --publish 127.0.0.1:8000:8000 \
>   --add-host host.docker.internal:host-gateway --log-opt max-size=10m --log-opt max-file=5 \
>   --health-start-period=300s \
>   --volume /srv/connx/sales.cdd:/opt/softwareag/connx/ini/mcp.cdd:ro \
>   --volume /srv/connx/certs:/opt/softwareag/connx/mcp/certs:ro \
>   --volume connx-mcp-ce-oauth-state:/opt/softwareag/connx/mcp/oauth-state \
>   --volume /srv/connx/vault-ca.pem:/opt/softwareag/connx/mcp/vault-ca.pem:ro \
>   "$IMAGE"
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> docker run --detach --name connx-mcp-ce --platform linux/amd64 --restart unless-stopped `
>   --env-file server.env --env ACCEPT_EULA=y --env MCP_PORT=8000 --publish 127.0.0.1:8000:8000 `
>   --add-host host.docker.internal:host-gateway --log-opt max-size=10m --log-opt max-file=5 `
>   --health-start-period=300s `
>   --volume C:\connx\sales.cdd:/opt/softwareag/connx/ini/mcp.cdd:ro `
>   --volume C:\connx\certs:/opt/softwareag/connx/mcp/certs:ro `
>   --volume connx-mcp-ce-oauth-state:/opt/softwareag/connx/mcp/oauth-state `
>   --volume C:\connx\vault-ca.pem:/opt/softwareag/connx/mcp/vault-ca.pem:ro `
>   $env:IMAGE
> ```

The MCP URL then starts with `https://`. Every host that a harness connects to must be in the certificate.
Every harness must trust the CA of the certificate.

Make sure that the server container can find and connect to your identity provider and Vault. When the server
stops, `docker logs connx-mcp-ce` shows the cause.
