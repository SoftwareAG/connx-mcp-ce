<!--
Install connx-mcp-ce: download a release, check its SHA-256 sum, get past SmartScreen on Windows, or install with uv.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
-->

# Install connx-mcp-ce

`connx-mcp-ce` is one file for each operating system. You can also install it from source with uv.

## System requirements

| Item | Requirement |
|---|---|
| Linux | x86-64, glibc 2.34 or later: RHEL 9, Rocky Linux 9, AlmaLinux 9, Ubuntu 22.04, Debian 12 or later |
| Windows | Windows 11 or Windows Server 2022, x86-64 |
| Docker | Docker Engine 24 or later with Compose v2, Docker Desktop, or Rancher Desktop (dockerd (moby) engine) |

`connx-mcp-ce` runs the `docker` command and Docker Compose v2.

**Rancher Desktop:** in **Preferences > Container Engine > General**, select the **dockerd (moby)** container engine.
With the containerd engine, Rancher Desktop does not supply the `docker` command.

RHEL 8 and other systems with glibc older than 2.34 cannot run the Linux binary. On these systems, install with uv
(see [Install with uv](#install-with-uv)).

## Download a release

Open the [releases page](https://github.com/softwareag/connx-mcp-ce/releases) and select a release. Each release
has these files:

| File | Contents |
|---|---|
| `connx-mcp-ce-linux-x64` | The Linux binary |
| `connx-mcp-ce-windows-x64.exe` | The Windows binary |
| `SHA256SUMS` | The SHA-256 sum of each binary |
| `THIRD-PARTY-NOTICES` | The third-party software in the binaries, with the licence texts |
| `LICENSE`, `NOTICE` | The licence of `connx-mcp-ce` (Apache-2.0) and its notice |

Download the binary for your system and `SHA256SUMS` into the same folder.

## Check the SHA-256 sum

The binaries have no code signature. Compare the SHA-256 sum of the binary with `SHA256SUMS` before you run it.

In the download folder, run the commands for your system:

> 🐧 **Linux (bash)**
>
> ```bash
> sha256sum -c --ignore-missing SHA256SUMS
> ```
>
> The command shows `connx-mcp-ce-linux-x64: OK` when the file is correct.

> 🪟 **Windows (PowerShell)**
>
> ```powershell
> $expected = (Select-String -Path .\SHA256SUMS -Pattern 'connx-mcp-ce-windows-x64.exe').Line.Split(' ')[0]
> $actual = (Get-FileHash .\connx-mcp-ce-windows-x64.exe -Algorithm SHA256).Hash.ToLower()
> if ($actual -eq $expected) { 'OK' } else { 'FAILED' }
> ```
>
> The last command shows `OK` when the file is correct.

If you see `FAILED`, delete the binary. Do not run it. Download it again and do the check again.

## Install on Linux

1. Copy the binary to a folder on your `PATH` as `connx-mcp-ce`. The `install` command also makes it executable.
   - For your user only, without `sudo`: `install -D connx-mcp-ce-linux-x64 ~/.local/bin/connx-mcp-ce`.
   - For all users: `sudo install connx-mcp-ce-linux-x64 /usr/local/bin/connx-mcp-ce`.
2. Make sure that it starts: `connx-mcp-ce --help`.

If the command is not found after you install it in `~/.local/bin`, that folder is not on your `PATH`. Add the line
`export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc`, then open a new terminal.

## Install on Windows

1. Make a folder for the binary, for example `%LOCALAPPDATA%\Programs\connx-mcp-ce`.
2. Move `connx-mcp-ce-windows-x64.exe` into this folder and rename it to `connx-mcp-ce.exe`.
3. Add the folder to your user `PATH` (Settings, System, About, Advanced system settings, Environment Variables).
4. Open a new terminal and run `connx-mcp-ce --help`.

### Windows SmartScreen

When you start `connx-mcp-ce.exe` for the first time, Windows can show the message "Windows protected your PC".
SmartScreen shows this message because the binary has no code signature.

1. Make sure that the SHA-256 sum is correct (see [Check the SHA-256 sum](#check-the-sha-256-sum)).
2. In the SmartScreen window, click **More info**.
3. Make sure that the app name is `connx-mcp-ce.exe` or `connx-mcp-ce-windows-x64.exe`.
4. Click **Run anyway**.

Windows keeps this decision for this file. Each new release shows the message one time again.

If your antivirus policy blocks binaries that have no code signature, install with uv instead.

## Install with uv

uv installs `connx-mcp-ce` from the source code of a release tag. Use this method when your system cannot run the
binary, or when your antivirus policy blocks it.

You need [uv](https://docs.astral.sh/uv/) and Git. uv gets the correct Python version itself.

1. Find the release tag on the releases page, for example `15.0.0.26004`. The tag has no `v`.
2. Install that release:

   ```shell
   uv tool install git+https://github.com/softwareag/connx-mcp-ce@15.0.0.26004
   ```

3. Make sure that it starts: `connx-mcp-ce --help`.

If the command is not found, run `uv tool update-shell` and open a new terminal.

To change to a different release, run the install command again with the new tag and `--force`. To remove
`connx-mcp-ce`, run `uv tool uninstall connx-mcp-ce`.
