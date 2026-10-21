#!/bin/sh
# Accounting Cloud installer for macOS and Linux.
#
#   curl -fsSL https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.sh | sh
#
# Runs the app with Docker: it downloads the release's compose file, writes a
# .env with a random SECRET_KEY and your time zone, pulls the images, starts
# them and opens the browser. Running it again updates to the latest release
# and keeps your .env and data.
#
# Options (environment variables):
#   ACCOUNTING_CLOUD_DIR      install folder         (default ~/accounting-cloud)
#   ACCOUNTING_CLOUD_VERSION  release to install, e.g. 0.3.0 (default: latest)
#   WEB_PORT                  port on this machine   (default 5052, first install only)
#   NO_BROWSER=1              don't open the browser when done
#
# Nothing outside the install folder is touched, except a desktop menu entry
# on Linux (~/.local/share/applications/accounting-cloud.desktop).

set -eu

REPO="avansledright/accounting-cloud-oss"
PROJECT="accountingcloud"   # Docker Compose project name: prefixes the containers and data volumes

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nError: %s\n' "$*" >&2; exit 1; }

download() { # url dest
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        die "curl or wget is needed to download files."
    fi
}

random_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

detect_tz() {
    tz=""
    if [ -n "${TZ:-}" ]; then
        tz="$TZ"
    elif [ -L /etc/localtime ]; then
        # macOS and most Linux: /etc/localtime -> .../zoneinfo/Area/City
        tz=$(readlink /etc/localtime | sed -n 's|.*zoneinfo/||p')
    fi
    if [ -z "$tz" ] && [ -r /etc/timezone ]; then
        tz=$(head -n1 /etc/timezone)
    fi
    if [ -z "$tz" ] && command -v timedatectl >/dev/null 2>&1; then
        tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    fi
    case "$tz" in
        */*|UTC) printf '%s' "$tz" ;;
        *) printf 'UTC' ;;
    esac
}

docker_help() {
    case "$(uname -s)" in
        Darwin)
            say "Install Docker Desktop for Mac, start it once, then run this installer again:"
            say "  https://docs.docker.com/desktop/setup/install/mac-install/" ;;
        *)
            say "Install Docker Engine with the Compose plugin, then run this installer again:"
            say "  https://docs.docker.com/engine/install/"
            say "Quickest on most distributions:  curl -fsSL https://get.docker.com | sudo sh" ;;
    esac
}

open_url() {
    [ "${NO_BROWSER:-}" = "1" ] && return 0
    if command -v open >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
        open "$1" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$1" >/dev/null 2>&1 || true
    fi
}

write_helper() { # dir
    cat > "$1/accounting-cloud" <<'HELPER'
#!/bin/sh
# Manage this Accounting Cloud install. Run with no arguments for help.
set -eu
cd "$(dirname "$0")"
port=$(sed -n 's/^WEB_PORT=//p' .env | head -n1); port=${port:-5052}
url="http://localhost:$port"
case "${1:-}" in
    start)   docker compose up -d --wait && echo "Running at $url" ;;
    stop)    docker compose stop ;;
    status)  docker compose ps ;;
    logs)    docker compose logs --tail 200 -f ;;
    open)    if [ "$(uname -s)" = "Darwin" ]; then open "$url"; else xdg-open "$url"; fi ;;
    launch)  docker compose up -d --wait >/dev/null 2>&1 || true
             if [ "$(uname -s)" = "Darwin" ]; then open "$url"; else xdg-open "$url"; fi ;;
    update)  curl -fsSL "https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.sh" \
               | ACCOUNTING_CLOUD_DIR="$(pwd)" NO_BROWSER=1 sh ;;
    backup)  f="backup-$(date +%Y-%m-%d_%H%M%S).archive.gz"
             docker compose exec -T db mongodump --archive --gzip > "$f" && echo "Saved $(pwd)/$f" ;;
    uninstall)
             echo "This stops Accounting Cloud and removes its containers."
             if [ "${2:-}" = "--delete-data" ]; then
                 docker compose down --volumes
                 echo "Containers and data deleted."
             else
                 docker compose down
                 echo "Containers removed. Your data is kept in Docker volumes;"
                 echo "run '$0 uninstall --delete-data' to delete it too."
             fi
             rm -f "$HOME/.local/share/applications/accounting-cloud.desktop"
             echo "You can now delete this folder: $(pwd)" ;;
    *)       echo "Usage: $0 start|stop|status|logs|open|launch|update|backup|uninstall [--delete-data]" ;;
esac
HELPER
    chmod +x "$1/accounting-cloud"
}

write_desktop_entry() { # dir
    [ "$(uname -s)" = "Linux" ] || return 0
    apps="$HOME/.local/share/applications"
    mkdir -p "$apps"
    cat > "$apps/accounting-cloud.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Accounting Cloud
Comment=Household finances
Exec="$1/accounting-cloud" launch
Icon=office-calendar
Terminal=false
Categories=Office;Finance;
EOF
}

main() {
    dir="${ACCOUNTING_CLOUD_DIR:-$HOME/accounting-cloud}"
    version="${ACCOUNTING_CLOUD_VERSION:-}"
    version="${version#v}"
    if [ -n "${ACCOUNTING_CLOUD_BASE_URL:-}" ]; then
        base="$ACCOUNTING_CLOUD_BASE_URL"      # testing: serve the release files yourself
    elif [ -n "$version" ]; then
        base="https://github.com/$REPO/releases/download/v$version"
    else
        base="https://github.com/$REPO/releases/latest/download"
    fi

    say "Accounting Cloud installer"

    step "Checking Docker"
    command -v docker >/dev/null 2>&1 || { say "Docker is not installed."; docker_help; exit 1; }
    if ! docker info >/dev/null 2>&1; then
        say "Docker is installed but not running (or this user can't use it)."
        case "$(uname -s)" in
            Darwin) say "Open Docker Desktop, wait for it to say it's running, then run this again." ;;
            *)      say "Start it with 'sudo systemctl start docker'. If it is running, add yourself"
                    say "to the docker group: 'sudo usermod -aG docker \$USER', then log out and in." ;;
        esac
        exit 1
    fi
    docker compose version >/dev/null 2>&1 || { say "The Docker Compose plugin is missing."; docker_help; exit 1; }
    say "OK"

    # A first install (no .env yet) must not silently attach to data volumes
    # that already exist under this project name: another copy, an old test.
    if [ ! -f "$dir/.env" ]; then
        existing=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")
        if [ -n "$existing" ] && [ "${ACCOUNTING_CLOUD_REUSE_DATA:-}" != "1" ]; then
            say ""
            say "Docker already has Accounting Cloud data from another install:"
            say "$existing" | sed 's/^/  /'
            say "Nothing was changed. To use that data, run the installer again with"
            say "ACCOUNTING_CLOUD_REUSE_DATA=1 in front of 'sh'. To start fresh instead, remove"
            say "those volumes with 'docker volume rm' (this permanently deletes the data)."
            exit 1
        fi
    fi

    step "Downloading release files to $dir"
    mkdir -p "$dir"
    tmp="$dir/.docker-compose.yml.download"
    download "$base/docker-compose.yml" "$tmp" || die "could not download $base/docker-compose.yml"
    mv "$tmp" "$dir/docker-compose.yml"
    write_helper "$dir"
    say "OK"

    step "Settings"
    if [ -f "$dir/.env" ]; then
        say "Keeping existing $dir/.env"
    else
        port="${WEB_PORT:-5052}"
        tz=$(detect_tz)
        (umask 077; cat > "$dir/.env" <<EOF
# Accounting Cloud settings. Full list: https://github.com/$REPO/blob/main/.env.example
# Restart after editing:  ./accounting-cloud start
COMPOSE_PROJECT_NAME=$PROJECT
SECRET_KEY=$(random_hex)
TZ=$tz
WEB_PORT=$port
EOF
        )
        say "Created $dir/.env (time zone $tz, port $port)"
    fi
    port=$(sed -n 's/^WEB_PORT=//p' "$dir/.env" | head -n1); port=${port:-5052}

    step "Downloading and starting (the first time takes a minute or two)"
    cd "$dir"
    # </dev/null: when piped from curl, stdin is this script.
    if [ "${ACCOUNTING_CLOUD_SKIP_PULL:-}" != "1" ]; then   # testing: use locally built images
        docker compose pull --quiet </dev/null || die "could not download the images (see above)."
    fi
    docker compose up -d --wait --remove-orphans </dev/null \
        || die "the app did not start. See the logs with: $dir/accounting-cloud logs"

    write_desktop_entry "$dir"
    url="http://localhost:$port"

    say ""
    say "Accounting Cloud is running at $url"
    say ""
    say "Manage it with $dir/accounting-cloud"
    say "  start | stop | status | logs | open | update | backup | uninstall"
    say ""
    say "It starts again by itself whenever Docker starts. It has no login, so"
    say "it only listens on this computer; see docs/deployment.md before exposing it."
    open_url "$url"
}

main "$@"
