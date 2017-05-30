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

install -v -m 644 "data/providesDB" "${VARDIR}/.providesDB"
RET=$?
if [ $RET -ne 0 ];
then
    echo;
    echo;
    echo 'Error installing data! Please check error messages above for more info!'
    exit $RET
fi

