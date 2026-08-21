#!/bin/sh
# Publish sregym-env to the Environments Hub.
# Vendors the sregym core into this directory (the Hub installs from the pulled source
# tree, so the package must be self-contained), replicates the Hub's install test, then
# pushes. Bump the version in pyproject.toml before running.
set -e
cd "$(dirname "$0")"

rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' ../../sregym/ sregym/

# what the Hub's test suite does: install this directory into a fresh venv and import
tmp="$(mktemp -d)"
uv venv -q "$tmp/.venv" --python 3.12
uv pip install -q --python "$tmp/.venv/bin/python" .
"$tmp/.venv/bin/python" -c "import sregym_env, sregym; print('source-tree install OK')"
rm -rf "$tmp"

prime env push -p . -v PUBLIC "$@"
