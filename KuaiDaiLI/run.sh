#!/bin/sh
# Linux/VPS 上用 .venv/bin/python，Windows（Git Bash）上用 .venv/Scripts/python
if [ -x .venv/bin/python ]; then
    .venv/bin/python proxy-test.py "$@"
else
    .venv/Scripts/python proxy-test.py "$@"
fi
