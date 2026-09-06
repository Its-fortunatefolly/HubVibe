#!/usr/bin/env bash
# SUPERSEDED. This script no longer does anything except point at the one
# that does.
#
# There used to be two go-live scripts, one per rail, each ending in its own
# full source deploy -- two deploys, two waits, and a window between them
# where one rail was live and the other was in whatever state the first had
# left it. scripts/go-live.sh resolves both recipients first and deploys once.
#
# This file stays so that an old handoff entry, a shell history, or a
# half-remembered command lands HERE and reads this, rather than getting
# "No such file or directory" and going looking. That exact failure --
# a script deleted along with the README that explained it -- has already
# cost this project a re-derivation once.
printf '\n  \033[31mSTOP\033[0m  scripts/go-live-x402.sh is superseded. Run instead:\n\n'
printf '      bash scripts/go-live.sh\n\n'
printf '  That turns on every rail that can settle, in one deploy. To do only x402:\n\n'
printf '      RAILS=x402 bash scripts/go-live.sh\n\n'
exit 1
