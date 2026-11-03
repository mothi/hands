#!/bin/sh
# hands: the command line is hands.py, beside this file (hands.sh --help prints its usage)
exec python3 "$(dirname "$0")/hands.py" "$@"
