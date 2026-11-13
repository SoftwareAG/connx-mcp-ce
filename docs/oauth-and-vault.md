<!--
Set up TLS, OAuth sign-in and per-caller CONNX logins from HashiCorp Vault for the CONNX MCP Server.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
-->

# TLS, OAuth and Vault reference

Use this reference to secure the CONNX MCP Server in three ways:

| Feature | What it gives you |
|---|---|
| TLS | Harnesses connect over HTTPS. A harness is the AI application that calls the server, for example Claude Code |
| OAuth | Each caller signs in with your identity provider before the server answers |
| Vault | Each caller queries CONNX with their own CONNX login, which the server reads from HashiCorp Vault |

Every value in this reference is a server setting. With connx-mcp-ce, set one with
`connx-mcp-ce configure <environment> --set KEY=VALUE`. Without connx-mcp-ce, give it to the server container as an
environment variable. [server.env.example](server.env.example) lists all other server settings.

The server checks its settings when it starts. When a setting is wrong, the server stops. Its log names the setting.

## Order of the steps

Set up TLS first, then OAuth, then Vault. Each step relies on the one before it:

| Set up | Before | Because |
|---|---|---|
| TLS | OAuth | Your identity provider stores the server's `https://` address. With TLS first, you register it once |
| OAuth | Vault | Vault holds a CONNX login for each signed-in caller. Without OAuth, callers have no identity |

To try OAuth on one machine, you can skip TLS. The server accepts OAuth on plain HTTP for `localhost`, `127.0.0.1`
and `::1`.

## TLS (HTTPS)

The server's port accepts HTTPS only, not plain HTTP.

| Server setting | Value |
|---|---|
| `MCP_TLS_ENABLED` | `true` |
| `MCP_TLS_HOSTS` | A comma-separated list of the host names and IP addresses that harnesses use |
| `MCP_TLS_KEY_PASSWORD` | The password of the private key. Leave it unset when the key has no password |
| `MCP_TLS_KEY_TYPE` | For a generated certificate: `rsa2048` (default), `rsa3072`, `rsa4096`, `ec-p256` or `ec-p384` |
| `MCP_TLS_CERT_VALIDITY_DAYS` | For a generated certificate: the days it is valid, 1 to 365. Default: `365` |

The server reads its certificate from `/opt/softwareag/connx/mcp/certs` in the container. With connx-mcp-ce,
`configure --certs <directory>` mounts your directory there. What the server does depends on the files it finds:

| Files in the certificate directory | What the server does |
|---|---|
| None | Generates a self-signed certificate for the first host in `MCP_TLS_HOSTS`. Stops when it is empty |
| One `.crt` file and one `.key` file | Uses them as they are |
| More than one pair | Uses the pair named after the first host, for example `mcp.example.com.crt` and `.key` |
| More than one pair, none named after the first host | Stops |
| A `.crt` file or a `.key` file alone | Stops |

The certificate must be valid. If it has expired, the server does not start.

With OAuth, use either a certificate from a certificate authority (CA) or a certificate from your company's
internal CA. The server refuses a self-signed certificate with OAuth, because harnesses cannot trust it during
sign-in. A certificate counts as self-signed when its issuer and its subject are the same.

Every host that a harness connects to must be in the certificate. Every harness must trust the CA of the
certificate.

## OAuth sign-in

Each caller signs in with your identity provider. The server accepts only requests that carry a valid access token.
It keeps no user accounts or passwords. `MCP_OAUTH_PROVIDER` selects your provider. Your provider decides how
sign-in works:

| Mode | `MCP_OAUTH_PROVIDER` | How sign-in works |
|---|---|---|
| Resource server | `keycloak` | The harness gets a token from Keycloak. The server checks the token |
| Proxy | `azure`, `google`, `aws`, `generic-oidc` | The server signs callers in with your provider and issues tokens |

These server settings apply to every provider:

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | The provider. Default: `none`, which serves without sign-in |
| `MCP_PUBLIC_BASE_URL` | The URL of the server for harnesses, without a path. Example: `https://mcp.example.com` |
| `MCP_OAUTH_REQUIRED_SCOPES` | The scopes that every token must have, separated by spaces or commas. Empty: none |

`MCP_PUBLIC_BASE_URL` must start with `https://`, unless its host is `localhost`, `127.0.0.1` or `::1`. For these
local hosts, `http://` also works.

Harnesses connect to the server at its MCP URL: `MCP_PUBLIC_BASE_URL` followed by `/mcp`, for example
`https://mcp.example.com/mcp`. Use exactly this URL when you add the server to a harness. Each access token is
valid for this URL only. The server refuses the token of a harness that connects at any other URL, for example
at an IP address.

### Proxy mode

Register the server with your provider as a web application, with this redirect URI:

```text
<MCP_PUBLIC_BASE_URL>/auth/callback
```

For example, `https://mcp.example.com/auth/callback`. The URI that you register must match exactly. If one
character is different, for example an extra `/` at the end, your provider refuses the sign-in. To get it right,
copy the URI from the server log. The server writes it there when it starts.

The server stores the sign-ins of its callers in the directory that `MCP_OAUTH_STORAGE_DIR` names. Leave this
setting at its default, and mount a volume on that directory. Without a volume, every caller must sign in again
whenever the container is recreated. connx-mcp-ce mounts this volume for you. Each server needs its own volume,
because two servers cannot share one sign-in directory.

Keep these points in mind when you run the server in proxy mode:

| Point | What to know |
|---|---|
| Client secret | A new `MCP_OAUTH_CLIENT_SECRET` signs out all callers |
| Firewall | Allow outgoing HTTPS connections from the server to your identity provider |
| Harness URLs | If the server cannot reach the URL a harness gives as its ID, set `MCP_OAUTH_ENABLE_CIMD` to `false` |
| Unused clients | A harness registration is removed after `MCP_OAUTH_CLIENT_IDLE_DAYS` unused days. Default: 90 |
| Consent | The server shows a consent page before it sends a caller to your provider |

### Provider settings

Each identity provider needs its own server settings and some set-up on the provider's side. The section for your
provider lists both.

#### Keycloak (`keycloak`)

Each harness registers itself as a client in your realm.

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | `keycloak` |
| `MCP_PUBLIC_BASE_URL` | The URL of the server, for example `https://mcp.example.com` |
| `MCP_OAUTH_REALM_URL` | `https://<keycloak-host>/realms/<realm>`. It must equal the issuer in the tokens |
| `MCP_OAUTH_AUDIENCE` | Required: `<MCP_PUBLIC_BASE_URL>/mcp`, for example `https://mcp.example.com/mcp` |

Prepare the realm:

1. Create a client scope with an audience mapper. Set **Included Custom Audience** of the mapper to the value of
   `MCP_OAUTH_AUDIENCE`.
2. In the **Client scopes** list of the realm, set the **Assigned type** of that client scope to **Default**.
   Keycloak then adds the audience to the tokens of every new client.
3. Let harnesses register their clients. Under **Clients** > **Client registration** > **Anonymous access
   policies**, open the **Trusted Hosts** policy. Add the host names or IP addresses of the machines where
   harnesses run, and add `localhost`. Harnesses register from these machines and receive the sign-in on a
   `localhost` redirect URI.
4. When Keycloak has more than one host name, set one fixed host name, so that the issuer in the tokens does not
   change.

Keycloak documentation: [Client registration policies][keycloak-registration].

[keycloak-registration]: https://www.keycloak.org/securing-apps/client-registration#_client_registration_policies

#### Microsoft Entra ID (`azure`)

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | `azure` |
| `MCP_PUBLIC_BASE_URL` | The URL of the server |
| `MCP_OAUTH_TENANT_ID` | The directory (tenant) ID |
| `MCP_OAUTH_CLIENT_ID` | The application (client) ID |
| `MCP_OAUTH_CLIENT_SECRET` | The value of the client secret, not its secret ID |
| `MCP_OAUTH_REQUIRED_SCOPES` | Required: the exposed scope without its prefix, for example `access_as_user` |

Prepare the app registration:

1. Add the redirect URI on the **Web** platform.
2. Under **Expose an API**, add a scope, for example `access_as_user`. Set `MCP_OAUTH_REQUIRED_SCOPES` to its
   name. Entra issues access tokens for the server only for a scope that the app exposes.
3. Under **Manifest**, set `requestedAccessTokenVersion` in the `api` section to `2`, so that Entra issues
   version 2 access tokens. The server refuses version 1 tokens.

Microsoft documentation:
[Register an application](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app).

#### Google (`google`)

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | `google` |
| `MCP_PUBLIC_BASE_URL` | The URL of the server |
| `MCP_OAUTH_CLIENT_ID` | The client ID, which ends in `.apps.googleusercontent.com` |
| `MCP_OAUTH_CLIENT_SECRET` | The client secret |
| `MCP_OAUTH_REQUIRED_SCOPES` | Empty. To log the email address and name of each caller, set `openid email profile` |

Prepare the OAuth client:

1. Create a client of the type **Web application**. Add the redirect URI to it.
2. Give the server a public host name in `MCP_PUBLIC_BASE_URL`, for example `mcp.example.com`. Google refuses a
   redirect URI with an IP address or an internal host name such as `mcp.corp.local`. Only `localhost` is exempt.
3. Let the server connect to `oauth2.googleapis.com` and `www.googleapis.com`. The server checks each token with
   Google on each request.
4. Under **Audience**, choose the user type **Internal** if you use Google Workspace. Otherwise, choose
   **External**, and select **Publish app**. While an External app is in **Testing**, Google ends each sign-in
   after seven days. Callers must then sign in again.

Google documentation:
[OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server).

#### AWS Cognito (`aws`)

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | `aws` |
| `MCP_PUBLIC_BASE_URL` | The URL of the server |
| `MCP_OAUTH_CONFIG_URL` | `https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/openid-configuration` |
| `MCP_OAUTH_CLIENT_ID` | The app client ID |
| `MCP_OAUTH_CLIENT_SECRET` | The app client secret |

Prepare the user pool:

1. Create an app client of the type **Traditional web application**. This type has a client secret. Add the
   redirect URI as a callback URL.
2. Give the user pool a domain. Without a domain, the user pool has no OAuth endpoints. Do not use this domain in
   `MCP_OAUTH_CONFIG_URL`.
3. Keep the issuer type **Original**.
4. Allow only the authorization code grant and only the `openid` scope. Other scopes can cause an `invalid_grant`
   error.

AWS documentation:
[App clients](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-apps.html).

#### Other OpenID Connect provider (`generic-oidc`)

Use `generic-oidc` for any other OpenID Connect provider, for example Okta, Auth0, PingFederate or dex. The provider
must support:

- OpenID Connect discovery. The server reads the discovery document when it starts.
- Access tokens that are signed JWTs.
- PKCE with `S256`.
- The scopes `openid` and `offline_access`. Without `offline_access`, the provider gives no refresh token. Each
  session then ends when its first access token expires.

| Server setting | Value |
|---|---|
| `MCP_OAUTH_PROVIDER` | `generic-oidc` |
| `MCP_PUBLIC_BASE_URL` | The URL of the server |
| `MCP_OAUTH_CONFIG_URL` | The discovery URL of the provider, which ends in `/.well-known/openid-configuration` |
| `MCP_OAUTH_CLIENT_ID` | The client ID |
| `MCP_OAUTH_CLIENT_SECRET` | The client secret |
| `MCP_OAUTH_AUDIENCE` | `<MCP_PUBLIC_BASE_URL>/mcp` or the client ID, depending on your provider |
| `MCP_OAUTH_REQUIRED_SCOPES` | Empty. When you set scopes, include `openid` and `offline_access` |

Your provider binds each access token either to a resource or to the client. To see which, decode one access token
and read its `aud` claim:

| The provider binds each token to | Set `MCP_OAUTH_AUDIENCE` to |
|---|---|
| A resource, for example Auth0 or Okta | `<MCP_PUBLIC_BASE_URL>/mcp`. Register it as the resource with your provider |
| The client, for example dex | The value of `MCP_OAUTH_CLIENT_ID` |

Prepare the client:

1. Create a confidential client with a client secret.
2. Add the redirect URI, and allow the authorization code grant.

OpenID documentation: [OpenID Connect Discovery](https://openid.net/specs/openid-connect-discovery-1_0.html).

## Vault (per-caller CONNX logins)

Vault lets each caller query CONNX with their own CONNX login and rights, instead of the shared login in
`CDD_USERNAME`. The server reads each caller's login from HashiCorp Vault. A caller with no login in Vault is
refused.

You need:

- OAuth with a provider other than `none`. Without it, the server does not start.
- HashiCorp Vault with a KV version 2 secrets engine.
- A Vault token that can only read the caller entries.
- A CONNX login for each caller.

| Server setting | Value |
|---|---|
| `MCP_CREDENTIAL_PROVIDER` | `hashicorp` |
| `VAULT_ADDR` | The Vault URL, for example `https://vault.example.com:8200` |
| `VAULT_TOKEN` | The read-only Vault token |
| `VAULT_CACERT` | For a Vault certificate from an internal CA: the path of that CA's certificate in the container |
| `VAULT_NAMESPACE` | The namespace. Vault Enterprise only |
| `MCP_CREDENTIAL_VAULT_MOUNT` | The KV version 2 mount. Default: `secret` |
| `MCP_CREDENTIAL_VAULT_PATH` | The path in the mount with one entry per caller. Default: `connx-mcp/callers` |

`VAULT_ADDR` must start with `https://`, unless its host is `localhost`, `127.0.0.1` or `::1`. For these local
hosts, `http://` also works.

If you use connx-mcp-ce, give it the CA certificate file on your machine with `configure --vault-ca <file>`.
connx-mcp-ce mounts the file into the container and sets `VAULT_CACERT` for you. Do not set `VAULT_CACERT`
yourself.

### Vault policy and token

Save this policy as `connx-mcp-read.hcl`. It lets the server read the entries at the default mount and path. When you
change `MCP_CREDENTIAL_VAULT_MOUNT` or `MCP_CREDENTIAL_VAULT_PATH`, change the paths in the policy to match.

```hcl
path "secret/data/connx-mcp/callers/*" {
  capabilities = ["read"]
}
path "secret/metadata/connx-mcp/callers/*" {
  capabilities = ["list"]
}
```

Give the server a periodic token. The server renews a periodic token when half of its period has passed and keeps
it valid while it runs. A token without a period has a maximum lifetime (its max TTL), which renewals cannot
extend. When that lifetime ends, the server refuses all callers.

To create a periodic token, you need a root token or a Vault policy with the `sudo` capability on
`auth/token/create`. Load the policy. Then create the token:

```shell
vault policy write connx-mcp-read connx-mcp-read.hcl
vault token create -policy=connx-mcp-read -period=1h
```

### Caller entries

With the default mount and path, each caller has one entry at:

```text
secret/connx-mcp/callers/<sub>
```

`<sub>` identifies the caller: the `sub` claim of their access token, or the `oid` claim for Microsoft Entra ID.
The easiest way to get it is to let the caller call the server once. The server then logs the issuer, the subject
and the path to create. Copy the path from that log line.

| Field | Value |
|---|---|
| `issuer` | The issuer of your identity provider, as in the log line. It must match the issuer in the token |
| `username` | The CONNX login for the queries of this caller. It must not be empty |
| `password` | The password of that CONNX login. Leave it empty when the login has no password |

Each field must be text. You can add other fields for your own notes. The server ignores them.

Create the entry. The first command asks you for the CONNX password without showing it, so the password does not
stay in your shell history:

> 🐧 **Linux (bash)**
>
> ```bash
> read -rsp 'CONNX password: ' connx_password; echo
> vault kv put secret/connx-mcp/callers/<sub> issuer="<issuer>" username="<connx-login>" \
>   password="$connx_password"
> ```

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $ConnxPassword = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'CONNX password')).Password
> vault kv put secret/connx-mcp/callers/<sub> issuer="<issuer>" username="<connx-login>" `
>   password="$ConnxPassword"
> ```

The server reads the entry on each request. A new, changed or deleted entry applies from the next request.

### When the server refuses a caller

When the server cannot find a CONNX login for a caller, the caller's harness shows the error
`CREDENTIALS_NOT_RESOLVED`. Its `details` field gives the cause:

| `details` | Cause | What to do |
|---|---|---|
| `NOT_PROVISIONED` | The caller has no entry | Create the entry at the path in the server log |
| `STORE_UNAVAILABLE` | The server cannot read Vault | Check that Vault runs, and check `VAULT_ADDR` and `VAULT_TOKEN` |
| `ENTRY_INVALID` | The entry breaks a rule in [Caller entries](#caller-entries) | Correct the entry |
| `SUBJECT_MISSING` | The token has no subject | Correct the token settings of your identity provider |

The server log gives more detail for each cause.

If the server cannot reach Vault, it refuses every caller and reports itself as unhealthy: `GET /health` returns
status 503.

If the entry exists but CONNX rejects its login or password, the caller sees a normal CONNX connection error, not
`CREDENTIALS_NOT_RESOLVED`.

## Startup and the container health check

With some identity providers or Vault, the server takes longer to start. Give the container health check enough
time, or the container can restart before the server is healthy.

When the server starts, it waits for each service it depends on, one after the other:

| Service | When the server waits for it |
|---|---|
| The discovery document of your identity provider | `MCP_OAUTH_PROVIDER` is `aws` or `generic-oidc` |
| Vault | `MCP_CREDENTIAL_PROVIDER` is `hashicorp` |

When a service does not answer, the server tries again for `MCP_STARTUP_WAIT_SECONDS` seconds (default: 120), then
stops. When Vault refuses the token, the server stops at once. After the waits, the server builds its data catalog
from the CDD. Its port accepts connections only when the catalog is complete.

If the container health check fails during this time, a platform such as Kubernetes can restart the container.
The server then starts all its waits again. The health check in the server image allows 60 seconds, which is
enough only when the server waits for no service. With `aws`, `generic-oidc` or Vault, give the health check
enough time for the worst case: each wait lasting the full `MCP_STARTUP_WAIT_SECONDS`, and then the catalog
build. With the default of 120 seconds, 300 seconds is safe.

connx-mcp-ce sets 300 seconds for you when OAuth or Vault is on. If you run the container yourself, set it:

| Container tool | Health check setting |
|---|---|
| `docker run` | `--health-start-period=300s` |
| Docker Compose | `start_period: 300s` in the `healthcheck` section of the service |
| Kubernetes | A `startupProbe` whose `failureThreshold` × `periodSeconds` is more than 300 seconds |
