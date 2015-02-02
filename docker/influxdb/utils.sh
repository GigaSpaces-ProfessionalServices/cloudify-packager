#!/usr/bin/env bash

function wait_for_port
{
    c=0
    while ! echo exit | curl http://localhost:$1;
    do
            if [[ $c -gt 24 ]]; then
                echo "ERROR: failed to connect to service... (status $?)" 1>&2
                exit 1
            fi
            echo "host not up yet... retrying... ($c/24)"
            sleep 5;
            c=$((c+1))
    done
}