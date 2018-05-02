# This file is designed to be sourced by bash scripts that should be killable
# by ctrl-C, it does not do anythnig on itself

killtree() {
    pid=$1
    echo "Killing process tree $pid"
    kill -- -$(ps -o pgid= $pid | grep -o [0-9]*)
    exit 1
}

killwrap_tree() {
    "$@" &
    pid=$!
    trap "killtree $pid" SIGINT
    wait $pid
}
