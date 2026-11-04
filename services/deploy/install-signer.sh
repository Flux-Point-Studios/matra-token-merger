#!/usr/bin/env bash
# Lay out one cMATRA signer under its own service user, from this checkout's
# commit, and install its systemd unit. It never enables or starts the unit.
#
#   sudo services/deploy/install-signer.sh a1 [--ledger-from FILE]   surrender-api, Server A
#   sudo services/deploy/install-signer.sh a2 [--ledger-from FILE]   cmatra-cosigner, Server B
#   sudo services/deploy/install-signer.sh a1|a2 --check             verify the layout, change nothing
#
# For ROLE a1 or a2 and its user cmatra-ROLE:
#   /opt/cmatra-ROLE/app -> app.<commit>   `git archive` of the commit and a .venv
#                                          from the hash-pinned lock; root-owned
#   /etc/cmatra-ROLE/                      0700 root: the env file (0600 root), which
#                                          systemd reads; the service user cannot
#   /var/lib/cmatra-ROLE/                  0700 cmatra-ROLE: keys/ (the admin key, 0400),
#                                          ledger/ (the one path the unit lets it write)
#                                          and the files the env names
#
# --ledger-from moves the signer's existing ledger into ledger/ with
# `python -m services.redemption_ledger copy`: byte for byte, proven by sha256,
# SQLite's integrity check and every row, after which the old file is 0400.
# --check exits 1 unless every path, mode and owner above holds, the env file
# points only into the layout, and the installed unit and release are this
# checkout's.
set -euo pipefail

die() { echo "install-signer: $*" >&2; exit 1; }

role=${1:-}
case "$role" in
    a1) unit=surrender-api.service; env_name=env.surrender-api; ledger_name=surrender-ledger.sqlite3
        key_var=ADMIN_SKEY_PATH; ledger_var=SURRENDER_LEDGER_PATH ;;
    a2) unit=cmatra-cosigner.service; env_name=env.cosigner; ledger_name=cosigned.sqlite3
        key_var=COSIGNER_SKEY_PATH; ledger_var=COSIGNER_LEDGER_PATH ;;
    *) die "usage: install-signer.sh a1|a2 [--ledger-from FILE | --check]" ;;
esac
shift
ledger_from=
check=0
while (($#)); do
    case "$1" in
        --ledger-from) ledger_from=${2:?--ledger-from needs the signer\'s current ledger file}; shift 2 ;;
        --check) check=1; shift ;;
        *) die "unknown option $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run it as root"

user=cmatra-$role
opt=/opt/$user
etc=/etc/$user
state=/var/lib/$user
env_file=$etc/$env_name
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
git=(git -c "safe.directory=$repo" -C "$repo")
commit=$("${git[@]}" rev-parse HEAD)

# --- check ---------------------------------------------------------------

failed=0
report() { echo "$1 $2"; [[ $1 == ok ]] || failed=1; }

expect() {   # PATH "TYPE MODE OWNER GROUP"
    local got
    got=$(stat -c '%F %a %U %G' "$1" 2>/dev/null) || got=missing
    if [[ $got == "$2" ]]; then report ok "$1 ($2)"; else report FAIL "$1: $got, want $2"; fi
}

env_value() {   # the last value the env file gives NAME, unquoted
    sed -n "s/^$1=//p" "$env_file" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

check_layout() {
    local shell='' home='' value='' name=''
    IFS=: read -r _ _ _ _ _ home shell < <(getent passwd "$user") || true
    if [[ $home == "$state" && $shell == */nologin ]]; then report ok "user $user ($home, $shell)"
    else report FAIL "user $user: home '$home', shell '$shell', want $state and nologin"; fi
    expect "$etc" "directory 700 root root"
    expect "$env_file" "regular file 600 root root"
    expect "$state" "directory 700 $user $user"
    expect "$state/keys" "directory 700 $user $user"
    expect "$state/ledger" "directory 700 $user $user"
    expect "$state/ledger/$ledger_name" "regular file 600 $user $user"
    if [[ -f $env_file ]]; then
        if [[ $(env_value "$ledger_var") == "$state/ledger/$ledger_name" ]]; then report ok "$ledger_var"
        else report FAIL "$ledger_var is not $state/ledger/$ledger_name"; fi
        value=$(env_value "$key_var")
        if [[ $value == "$state/keys/"* ]]; then expect "$value" "regular file 400 $user $user"
        else report FAIL "$key_var is not a file in $state/keys"; fi
        value=$(env_value CHAIN_API_KEY_FILE)
        if [[ -n $value ]]; then
            if [[ $value == "$state/"* ]]; then expect "$value" "regular file 400 $user $user"
            else report FAIL "CHAIN_API_KEY_FILE is not a file in $state"; fi
        fi
        name=$(grep -oE '^[A-Z_]+=["'\'']?(/home/|/root/|/run/user/)' "$env_file" | cut -d= -f1 | head -n 1 || true)
        if [[ -z $name ]]; then report ok "no setting points under a home directory"
        else report FAIL "$name points under a home directory, which ProtectHome=yes hides"; fi
        if runuser -u "$user" -- test -r "$env_file" 2>/dev/null; then report FAIL "$user can read $env_file"
        else report ok "$user cannot read $env_file"; fi
    fi
    if cmp -s "$repo/services/deploy/systemd/$unit" "/etc/systemd/system/$unit"; then report ok "/etc/systemd/system/$unit"
    else report FAIL "/etc/systemd/system/$unit is not this checkout's services/deploy/systemd/$unit"; fi
    if [[ $(readlink "$opt/app") == "app.$commit" && $(cat "$opt/app/RELEASE" 2>/dev/null) == "$commit" ]]; then
        report ok "$opt/app is release $commit"
    else report FAIL "$opt/app is not release $commit"; fi
    if [[ -d "$opt/app.$commit" && -z $(find "$opt/app.$commit" ! -type l \( ! -user root -o -perm /022 \) -print -quit) ]]; then
        report ok "$opt/app.$commit is root's and nobody else's to change"
    else report FAIL "$opt/app.$commit is missing or writable by someone other than root"; fi
}

if ((check)); then
    check_layout
    exit "$failed"
fi

# --- install -------------------------------------------------------------

if command -v systemctl >/dev/null && systemctl is-active --quiet "$unit"; then
    die "$unit is running; stop it before moving it"
fi
python=$(command -v python3.12 || command -v python3) || die "no python3"
"$python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' || die "the lock is compiled for Python 3.12"

getent passwd "$user" >/dev/null \
    || useradd --system --user-group --home-dir "$state" --no-create-home --shell /usr/sbin/nologin "$user"
install -d -m 0755 -o root -g root "$opt"
install -d -m 0700 -o root -g root "$etc"
install -d -m 0700 -o "$user" -g "$user" "$state" "$state/keys" "$state/ledger"

release=$opt/app.$commit
if [[ ! -f $release/RELEASE ]]; then
    rm -rf "$release"
    install -d -m 0755 -o root -g root "$release"
    "${git[@]}" archive --format=tar "$commit" | tar -x -C "$release" --no-same-owner
    "$python" -m venv "$release/.venv"
    "$release/.venv/bin/pip" install --quiet --no-cache-dir --require-hashes \
        -r "$release/services/deploy/requirements-cosigner.txt"
    "$release/.venv/bin/python" -m compileall -q "$release/services" "$release/tools"
    chmod -R u=rwX,go=rX "$release"
    echo "$commit" > "$release/RELEASE"
fi
ln -sfn "app.$commit" "$opt/app.next"
mv -T "$opt/app.next" "$opt/app"

install -m 0644 -o root -g root "$repo/services/deploy/systemd/$unit" "/etc/systemd/system/$unit"
if [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
fi

if [[ -n $ledger_from ]]; then
    (cd "$opt/app" && .venv/bin/python -m services.redemption_ledger copy --owner "$user" \
        "$ledger_from" "$state/ledger/$ledger_name")
fi

echo "installed $unit for $user at release $commit (not enabled, not started)"
echo "next: write $env_file (0600 root), put the key in $state/keys (0400 $user), then: $0 $role --check"
