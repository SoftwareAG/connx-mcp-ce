#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refresh_demo_assets.py - Refresh the demo stack's embedded assets from a new test-stack zip.

Description:
    `connx-mcp-ce` embeds the server team's test-stack files verbatim (compose file,
    Keycloak and dex templates, seed script, TLS script, seed Dockerfile, renderer and users-file
    validator) and ports only the bash orchestration. This script replaces the embedded copies
    with those in a new zip, byte for byte, and removes any file no longer embedded. It records a
    SHA-256 of each upstream orchestration script beside the assets and names the ones that
    changed since the last refresh: those changes must be ported into `connx_mcp.demo` by hand.

    Usage: uv run python scripts/refresh_demo_assets/refresh_demo_assets.py <zip> [--dest DIR]

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# The zip's top-level directory.
ROOT = "connx-mcp-test-stack"

# Embedded verbatim, at the same relative path.
EMBEDDED = (
    "VERSION",
    "docker-compose.yml",
    "keycloak/realm-connx-mcp.json.template",
    "dex/config.yaml.template",
    "render.py",
    "users_file.py",
    "vault/Dockerfile.seed",
    "vault/make-tls.sh",
    "vault/seed.sh",
)

# Ported by hand into connx_mcp.demo; only their hashes are kept, to flag upstream changes.
ORCHESTRATION = ("startContainers.sh", "stopContainers.sh", "settings.sh", "test-stack.env.example")

HASHES = "UPSTREAM-ORCHESTRATION.sha256"
DEFAULT_DEST = Path(__file__).resolve().parents[2] / "src" / "connx_mcp" / "demo" / "assets"


def _read_hashes(path: Path) -> dict[str, str]:
    """Return the recorded orchestration hashes, empty when none were recorded.

    Args:
        path (Path): The hash file.

    Returns:
        dict[str, str]: File name to SHA-256.
    """
    if not path.is_file():
        return {}
    pairs = (line.split(maxsplit=1) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {name.strip(): digest for digest, name in pairs}


def refresh(zip_path: Path, dest: Path) -> list[str]:
    """Replace the embedded assets in `dest` with those in the zip.

    Args:
        zip_path (Path): The test-stack zip.
        dest (Path): The assets directory.

    Returns:
        list[str]: The orchestration scripts whose content changed since the last refresh.

    Raises:
        ValueError: The zip lacks an embedded or orchestration file.
    """
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        missing = [m for m in (*EMBEDDED, *ORCHESTRATION) if f"{ROOT}/{m}" not in names]
        if missing:
            raise ValueError(f"{zip_path} lacks {', '.join(missing)} under {ROOT}/")
        contents = {m: archive.read(f"{ROOT}/{m}") for m in EMBEDDED}
        hashes = {m: hashlib.sha256(archive.read(f"{ROOT}/{m}")).hexdigest() for m in ORCHESTRATION}

    previous = _read_hashes(dest / HASHES)
    changed = [m for m in ORCHESTRATION if previous and previous.get(m) != hashes[m]]

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".assets-", dir=dest.parent))
    staging.chmod(0o755)
    for member, data in contents.items():
        target = staging / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (staging / HASHES).write_text("".join(f"{hashes[m]}  {m}\n" for m in ORCHESTRATION), encoding="utf-8")
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    return changed


def main(argv: list[str]) -> int:
    """Refresh the assets and report what the maintainer must port by hand.

    Args:
        argv (list[str]): Command-line arguments, without the program name.

    Returns:
        int: 0 on success, 1 when the zip is refused.
    """
    parser = argparse.ArgumentParser(description="Refresh the embedded demo assets from a test-stack zip.")
    parser.add_argument("zip", type=Path, help="the connx-mcp-demo-stack-<version>.zip")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="the assets directory")
    args = parser.parse_args(argv)
    try:
        changed = refresh(args.zip, args.dest)
    except (ValueError, zipfile.BadZipFile) as error:
        print(f"refresh_demo_assets.py: {error}", file=sys.stderr)
        return 1
    version = (args.dest / "VERSION").read_text(encoding="utf-8").strip()
    print(f"refresh_demo_assets.py: embedded test stack {version} in {args.dest}")
    for name in changed:
        print(f"refresh_demo_assets.py: {name} changed upstream; port the change into src/connx_mcp/demo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
