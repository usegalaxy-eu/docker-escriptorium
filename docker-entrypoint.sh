#!/bin/bash

# Started without arguments (plain `docker run`, CMD): launch the full stack.
# Started with a command (Galaxy Interactive Tools run their <command> block
# as the container command): execute that instead — the tool command does its
# setup (output directories, input symlinks, env vars) and then starts
# supervisord itself in the foreground.
if [ "$#" -eq 0 ]; then
    exec /usr/bin/supervisord -n
else
    exec "$@"
fi
