#!/bin/zsh
cd /Users/dev/Desktop/myFirstForge-week4cli || exit 2
exec cli/.venv/bin/python cli/tests/demo_approval.py launch "$@"
