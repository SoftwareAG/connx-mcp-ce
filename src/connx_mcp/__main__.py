#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""__main__.py - Runs `connx-mcp-ce` as `python -m connx_mcp`; the entry point the release binaries are compiled from.

Description:
    Nuitka compiles this module into the single-file `connx-mcp-ce` binaries. The program name is fixed
    so the help and usage lines read `connx-mcp-ce` whatever the file is called on disk.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from connx_mcp import CLI_NAME
from connx_mcp.cli import app

if __name__ == "__main__":
    app(prog_name=CLI_NAME)
