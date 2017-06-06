#!/bin/bash

# Installs all the pacman-utils

PROVIDESDB_DIR="/var/lib/pacman/.providesDB"

for arg in "$@";
do
    if ( echo "${arg}" | grep -q '^PREFIX=' );
    then
        export "${arg}"
    elif ( echo "${arg}" | grep -q '^DESTDIR=' );
    then
        export "${arg}"
    fi
done

if [ -z "${PREFIX}" ];
then
    PREFIX="usr"
else
    if [ "${PREFIX}" != "/" ];
    then
        PREFIX="$(echo "${PREFIX}" | sed 's|[/][/]*$||g')"
        PREFIX="$(echo "${PREFIX}" | sed 's|//|/|g')"
    fi
fi
if [ -z "${DESTDIR}" ];
then
    DESTDIR=""
else
    if [ "${DESTDIR}" != "/" ];
    then
        DESTDIR="$(echo "${DESTDIR}" | sed 's|[/][/]*$||g')"
        DESTDIR="$(echo "${DESTDIR}" | sed 's|//|/|g')"
    else
        DESTDIR=''
    fi
fi

VARDIR="${DESTDIR}/var/lib/pacman"
VARDIR="$(echo "${VARDIR}" | sed 's|//|/|g')"

mkdir -p "${VARDIR}"

if [ ! -d "pacman-utils-data" ];
then
    git clone https://github.com/kata198/pacman-utils-data
    if [ $? -ne 0 ];
    then
        echo "Failed to clone: https://github.com/kata198/pacman-utils-data" >&2
        exit 1
    fi
else
    cd pacman-utils-data
    git pull;
    if [ $? -ne 0 ];
    then
        echo "Warning: failed to update pacman-utils-data dir (from https://github.com/kata198/pacman-utils-data )" >&2
    fi
    cd ..
fi

install -v -m 644 "pacman-utils-data/providesDB" "${VARDIR}/.providesDB"
RET=$?
if [ $RET -ne 0 ];
then
    echo;
    echo;
    echo 'Error installing data! Please check error messages above for more info!'
    exit $RET
fi

