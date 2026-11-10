# The shared release box: a Render background worker that the insights team reaches with `render ssh` (atlas:release-box).
# It holds tools only. No credential is in the image, the repo or the service's env vars: each person signs in on the box.
# The persistent disk is mounted at /root, root's home, so sign-ins, repos and agent sessions survive a deploy. Every tool is
# installed outside /root, so the mount hides none of them. Render builds this folder only (build filter release-box/**), and
# auto-deploy is off, so a push to Atlas never restarts the box.
FROM node:24.19.0-bookworm-slim

ARG GH_VERSION=2.98.0
ARG GH_SHA256=f65a3fa2fa0eb2e97c445ee3f5e087a40aae03b64847f45a8f13805e504535d6
ARG DATABRICKS_VERSION=1.17.0
ARG DATABRICKS_SHA256=b5702b5ae4f03c598d734a3957625d550b594a5641a54250ce51e97367db619d
ARG CLAUDE_CODE_VERSION=2.1.318
ARG RAINCLI_TAG=v0.6.1
ARG RAINCLI_COMMIT=6b3b27968852b631f94652b5a9889ab0a6eb077a
ARG HERDR_VERSION=0.9.5
ARG HERDR_SHA256=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d

# git, jq, tmux and Python 3 (bookworm's 3.11, which RainCLI's client needs at least), with `python` too, since the release skill calls
# `python`; the rest serves the installs below and
# the people working in a shell.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq less openssh-client openssl procps python-is-python3 python3 python3-venv tmux unzip \
    && rm -rf /var/lib/apt/lists/*

# gh and the Databricks CLI from their release files, each checked against the checksum its publisher lists.
RUN curl -fsSLo /tmp/gh.deb "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.deb" \
    && echo "${GH_SHA256}  /tmp/gh.deb" | sha256sum -c - \
    && dpkg -i /tmp/gh.deb && rm /tmp/gh.deb \
    && curl -fsSLo /tmp/databricks.zip "https://github.com/databricks/cli/releases/download/v${DATABRICKS_VERSION}/databricks_cli_${DATABRICKS_VERSION}_linux_amd64.zip" \
    && echo "${DATABRICKS_SHA256}  /tmp/databricks.zip" | sha256sum -c - \
    && unzip -q /tmp/databricks.zip databricks -d /usr/local/bin && rm /tmp/databricks.zip

# Claude Code. The sign-in on the insights plan is done on the box, and lands on the disk.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

# The RainCLI client from its public repo at a pinned tag and commit, into a venv under /opt (its SETUP.md, step 1; stdlib only).
RUN git clone --quiet --depth 1 --branch "${RAINCLI_TAG}" https://github.com/DylanHallahan/raincli /tmp/raincli \
    && test "$(git -C /tmp/raincli rev-parse HEAD)" = "${RAINCLI_COMMIT}" \
    && python3 -m venv /opt/raincli \
    && /opt/raincli/bin/pip install --quiet --no-deps /tmp/raincli/raincli \
    && ln -s /opt/raincli/bin/raincli /usr/local/bin/raincli \
    && rm -rf /tmp/raincli

# herdr, which runs the release team's agents in panes and which RainCLI's instant inbox needs (0.9.3 or later), from its
# release file, checked against the checksum of the file verified at that release.
RUN curl -fsSLo /usr/local/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64" \
    && echo "${HERDR_SHA256}  /usr/local/bin/herdr" | sha256sum -c - \
    && chmod 755 /usr/local/bin/herdr

# GitHub, as the your-org GitHub App: github-app makes each hour-long token from the key on the disk, git asks it
# through a credential helper, and gh runs through a wrapper that puts the token in its own environment (see the scripts).
COPY github-app.sh /usr/local/bin/github-app
COPY gh.sh /usr/local/bin/gh
RUN git config --system credential.https://github.com.helper '!/usr/local/bin/github-app git-credential'

# PID 1, which keeps the worker up: the agents' memory ceiling (see the script). People work in their own `render ssh` shells.
COPY watchdog.sh /usr/local/bin/release-box-watchdog
WORKDIR /root
CMD ["/usr/local/bin/release-box-watchdog"]
