#!/bin/sh
set -eu
repo_dir=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd -P)
cd "$repo_dir"
exec python3 -m harness.cli "$@"
