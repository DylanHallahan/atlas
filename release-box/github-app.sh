#!/bin/bash
# The release box's GitHub access, as the your-org GitHub App your-release-app, made on 11-04 with GitHub's
# manifest flow (atlas:release-box). Its private key and id live on the disk in /root/.github-app, mode 600. No token is
# written to a file or put in a process's arguments: each call signs a short JWT with the key and trades it for a token,
# which lives an hour and reaches only the release repos in REPOS below.
#   github-app setup               once: asks for the one-time code from GitHub's manifest flow, keeps the key and the id,
#                                  and prints the link to install the app
#   github-app token               a fresh installation token on stdout, for one child process (the gh wrapper uses it)
#   github-app git-credential get  git's credential helper for https://github.com, set in /etc/gitconfig
set -euo pipefail
shopt -s inherit_errexit  # a failure inside $(...) stops the script too
set +x  # never trace: a trace would print the JWT and the token
DIR=/root/.github-app
ORG=your-org
API=https://api.github.com
# The only repos a token reaches: the six of the 11-10 release. The app is installed on every repo in the organization
# (Dylan, Wed 11-04 4:15 PM ET), so this list is the limit. It is a guard rail, not a wall: anyone holding the key could
# mint a wider token by hand.
REPOS='["Astra-Operations","Astra-Pulse","Orion-Dimensions","Astra-Accounts-Receivable","Astra-Accounts-Payable","Orion-Finance"]'
umask 077

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# One API call. The Authorization header goes to curl on stdin, so it is never in any process's arguments.
api() { # METHOD PATH AUTHORIZATION [BODY]
    local data=()
    if [ -n "${4:-}" ]; then
        data=(--data "$4")
    fi
    printf 'Authorization: %s\n' "$3" | curl -fsS --connect-timeout 10 --max-time 30 -X "$1" -H @- \
        -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' "${data[@]}" "$API$2"
}

token() {
    if [ ! -s "$DIR/key.pem" ] || [ ! -s "$DIR/id" ]; then
        echo "github-app: not set up; run github-app setup" >&2
        exit 1
    fi
    local now head body sig jwt installs id
    now=$(date +%s)
    head=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
    # iss is the app id as a number, as GitHub's JWT docs give it.
    body=$(printf '{"iat":%d,"exp":%d,"iss":%d}' $((now - 60)) $((now + 540)) "$(cat "$DIR/id")" | b64url)
    sig=$(printf '%s.%s' "$head" "$body" | openssl dgst -sha256 -sign "$DIR/key.pem" -binary | b64url)
    jwt="$head.$body.$sig"
    installs=$(api GET /app/installations "Bearer $jwt")
    id=$(printf '%s' "$installs" | jq -r --arg org "$ORG" '.[] | select(.account.login == $org) | .id')
    if [ -z "$id" ]; then
        echo "github-app: not installed on $ORG; open the link that github-app setup printed" >&2
        exit 1
    fi
    api POST "/app/installations/$id/access_tokens" "Bearer $jwt" "{\"repositories\":$REPOS}" | jq -er .token
}

setup() {
    if [ -e "$DIR/key.pem" ]; then
        echo "github-app: already set up; remove $DIR first to replace it" >&2
        exit 1
    fi
    local code r
    # Asked for, not passed, so the code stays out of shell history; it is spent once exchanged.
    read -rsp 'One-time code: ' code
    echo >&2
    case $code in
        '' | *[!A-Za-z0-9_-]*) echo "github-app: that is not a code from GitHub" >&2; exit 2 ;;
    esac
    # The URL goes to curl on stdin too, so the code is not in curl's arguments.
    r=$(printf 'url = "%s/app-manifests/%s/conversions"\n' "$API" "$code" | curl -fsS --connect-timeout 10 --max-time 30 -K - -X POST \
        -H 'Accept: application/vnd.github+json')
    # The answer holds the key, the client secret and the webhook secret. Only the key and the id are kept, and only for
    # an app that your-org owns.
    if ! printf '%s' "$r" | jq -e --arg org "$ORG" '.owner.login == $org' > /dev/null; then
        echo "github-app: that app is not owned by $ORG; nothing was kept" >&2
        exit 1
    fi
    mkdir -p "$DIR"
    chmod 700 "$DIR"
    printf '%s' "$r" | jq -er .pem > "$DIR/key.pem"
    printf '%s' "$r" | jq -er .id > "$DIR/id"
    chmod 600 "$DIR/key.pem" "$DIR/id"
    printf 'Install it: https://github.com/apps/%s/installations/new\n' "$(printf '%s' "$r" | jq -er .slug)"
}

case ${1:-} in
    setup) setup ;;
    token) token ;;
    git-credential)
        # git asks with "get"; "store" and "erase" have nothing to keep. git's request on stdin names github.com only.
        [ "${2:-}" = get ] || exit 0
        cat > /dev/null
        t=$(token)
        printf 'username=x-access-token\npassword=%s\n' "$t"
        ;;
    -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
    *) echo "usage: github-app setup | token | git-credential get" >&2; exit 2 ;;
esac
