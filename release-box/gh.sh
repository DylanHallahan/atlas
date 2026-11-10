#!/bin/sh
# gh on the release box. Once the GitHub App is set up, every gh call runs as the app, with a fresh installation token in
# gh's own environment only, so it reaches the release repos named in github-app and nothing else. Before that, it is
# plain gh.
if [ -z "${GH_TOKEN:-}" ] && [ -s /root/.github-app/key.pem ]; then
    GH_TOKEN=$(github-app token) || exit 1
    export GH_TOKEN
fi
exec /usr/bin/gh "$@"
