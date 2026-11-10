#!/bin/sh
# PID 1 of the release box: a memory ceiling below the container's 2 GiB limit. Once a second it reads the container's
# unreclaimable memory (anon in its cgroup's memory.stat, so page cache never counts). At or over the ceiling it kills the
# process using the most memory, whatever it is (an agent, a build, git, python or databricks), except PID 1 and anything
# ssh. That way a runaway dies before the container hits its limit and Render restarts the box, with every shell and agent
# on it. The cgroup is read-only here, so no child group can hold the agents, and Claude Code is a native binary, so a Node
# heap flag would not reach it. If this script dies, the container exits and Render restarts it.
CEILING_MB=${CEILING_MB:-1536}
# Render's SSH needs root's home to hold a ~/.ssh it can use, and OpenSSH refuses a home that others can write. The disk is
# mounted at /root, and Render sets its mode at mount, so both modes are set at every boot, whether ~/.ssh is new or not
# (Render's docs, "Docker-specific configuration").
mkdir -p /root/.ssh; chmod 700 /root /root/.ssh
trap 'exit 0' TERM INT
while :; do
    anon=$(awk '$1 == "anon" { print int($2 / 1048576) }' /sys/fs/cgroup/memory.stat)
    if [ "${anon:-0}" -ge "$CEILING_MB" ]; then
        set -- $(ps -e -o pid=,rss=,comm= --sort=-rss | awk '$1 != 1 && $3 !~ /ssh/ { print $1, int($2 / 1024), $3; exit }')
        if [ -n "$1" ] && kill -9 "$1"; then
            echo "release-box: memory ${anon} MB, at or over ${CEILING_MB} MB; killed pid $1 ($3, $2 MB)" >&2
        fi
    fi
    sleep 1
done
