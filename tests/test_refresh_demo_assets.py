#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_refresh_demo_assets.py - The maintainer refreshes the embedded demo assets from a test-stack zip.

Description:
    The script copies the embedded files verbatim from a new zip, drops
    files that are no longer embedded, refuses a zip that lacks one, and names the upstream
    orchestration scripts that changed, because those are ported by hand.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.refresh_demo_assets.refresh_demo_assets import EMBEDDED, ORCHESTRATION, main

ROOT = "connx-mcp-test-stack"


def _zip(tmp_path: Path, files: dict[str, bytes], name: str = "stack.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in files.items():
            archive.writestr(f"{ROOT}/{member}", data)
    return path


def _upstream() -> dict[str, bytes]:
    files = {member: f"upstream {member}\r\nkeeps CRLF\n".encode() for member in (*EMBEDDED, *ORCHESTRATION)}
    files["README.md"] = b"not embedded"
    return files


def test_refresh_copies_every_embedded_file_verbatim(tmp_path: Path) -> None:
    dest = tmp_path / "assets"
    files = _upstream()

    code = main([str(_zip(tmp_path, files)), "--dest", str(dest)])

    assert code == 0
    for member in EMBEDDED:
        assert (dest / member).read_bytes() == files[member], member
    assert not (dest / "README.md").exists()
    assert not (dest / "startContainers.sh").exists()


def test_refresh_removes_a_file_no_longer_embedded(tmp_path: Path) -> None:
    dest = tmp_path / "assets"
    (dest / "old").mkdir(parents=True)
    (dest / "old" / "stale.txt").write_text("stale")

    assert main([str(_zip(tmp_path, _upstream())), "--dest", str(dest)]) == 0

    assert not (dest / "old").exists()


def test_refresh_refuses_a_zip_missing_an_embedded_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = tmp_path / "assets"
    files = _upstream()
    del files["render.py"]

    code = main([str(_zip(tmp_path, files)), "--dest", str(dest)])

    assert code == 1
    assert "render.py" in capsys.readouterr().err
    assert not dest.exists()


def test_refresh_names_the_orchestration_scripts_that_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "assets"
    assert main([str(_zip(tmp_path, _upstream())), "--dest", str(dest)]) == 0
    capsys.readouterr()

    changed = _upstream()
    changed["startContainers.sh"] = b"#!/usr/bin/env bash\necho changed\n"
    code = main([str(_zip(tmp_path, changed, "new.zip")), "--dest", str(dest)])

    out = capsys.readouterr().out
    assert code == 0
    assert "startContainers.sh changed" in out
    assert "stopContainers.sh changed" not in out
