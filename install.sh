#!/bin/bash

# Installs all the pacman-utils

ALL_FILES="installpackage buildpkg.sh whatprovides whatprovides_upstream extractMtree.py mkgcdatar"

if [ -z "${PREFIX}" ];
then
    PREFIX="usr"
fi
if [ -z "${DESTDIR}" ];
then
    DESTDIR=""
fi

mkdir -p "${DESTDIR}/${PREFIX}/bin"

install -v -m 755 ${ALL_FILES} "${DESTDIR}/${PREFIX}/bin"
install -v -m 644 "data/providesDB" "${DESTDIR}/var/lib/pacman/.providesDB"
