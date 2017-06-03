#!/bin/bash

# Installs all the pacman-utils
#  use ./install.sh PREFIX=$HOME to install to local home dir.

ALL_FILES="installpackage buildpkg.sh whatprovides whatprovides_upstream extractMtree.py mkgcdatar getpkgs abs2 archsrc-getpkg pacman-mirrorlist-optimize"

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

BINDIR="${DESTDIR}/${PREFIX}/bin"
BINDIR="$(echo "${BINDIR}" | sed 's|//|/|g')"

mkdir -p "${BINDIR}"

install -v -m 755 ${ALL_FILES} "${BINDIR}"
RET=$?
if [ $RET -ne 0 ];
then
    echo;
    echo;
    echo "WARNING: Install returned non-zero. Check for errors above"'!';
    exit ${RET}
fi

cd /usr/bin
ln -sf buildpkg.sh archsrc-buildpkg.sh # Right now, just symlink these two names


#install -v -m 644 "data/providesDB" "${DESTDIR}/var/lib/pacman/.providesDB"
