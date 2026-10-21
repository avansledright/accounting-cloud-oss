#!/bin/sh
# Print the docker-compose.yml attached to a release: the repo's file with the
# build lines removed and the image pinned to that version.
#
#   scripts/release/release-compose.sh 0.3.0 > dist/docker-compose.yml
set -eu
version="${1:?usage: $0 VERSION}"
version="${version#v}"
src="$(dirname "$0")/../../docker-compose.yml"
grep -q 'ACCOUNTING_CLOUD_VERSION:-latest' "$src" || { echo "image line not found in $src" >&2; exit 1; }
cat <<HEADER
# Accounting Cloud $version: web app + MongoDB (+ optional MCP server).
# Written by the installer; settings live in .env next to this file.
#
#   ./accounting-cloud start|stop|status|logs|update|backup
#   docker compose --profile mcp up -d     # optional MCP server (set MCP_API_TOKEN in .env)
#
# SECURITY: the web app has NO login. The port is bound to 127.0.0.1 so it is
# only reachable from this machine. To expose it, put it behind a reverse
# proxy with authentication (docs/deployment.md in the repository).

HEADER
# Drop the repo file's leading comment block, the build lines, and pin the image.
sed -e '1,/^services:/{/^services:/!d;}' \
    -e '/^[[:space:]]*build: \.$/d' \
    -e "s/\${ACCOUNTING_CLOUD_VERSION:-latest}/\${ACCOUNTING_CLOUD_VERSION:-$version}/" \
    "$src"
