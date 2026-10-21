#!/usr/bin/env bash
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
scratch="$(mktemp -d)"
scratch="$(CDPATH= cd -- "$scratch" && pwd -P)"
# This example cleans up only its own mktemp directory, not a user's release store.
cleanup() {
  chmod -R u+w "$scratch"
  rm -rf -- "$scratch"
}
trap cleanup EXIT
cli="$project_dir/bin/releasecraft"
store="$scratch/store"
"$cli" --root "$store" init
"$cli" --root "$store" stage --source "$project_dir/examples/site-v1" --version v1
"$cli" --root "$store" activate --version v1
cat "$store/current/index.html"
"$cli" --root "$store" stage --source "$project_dir/examples/site-v2" --version v2
"$cli" --root "$store" verify --version v2
"$cli" --root "$store" activate --version v2
cat "$store/current/index.html"
"$cli" --root "$store" rollback
cat "$store/current/index.html"
"$cli" --root "$store" history
