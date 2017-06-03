#!/bin/bash

# install_extras - Install "extra" stuff useful for building packages.
#
#  makepkg.conf -  My makepkg.conf which support additions in PKGBUILDS:
#     Instructions therein and simple support for various CFLAGS (native, lto, etc),
#      and profiling support, alongside other pacman-utils tools
#    

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

ETCDIR="${DESTDIR}/etc"
ETCDIR="$(echo "${ETCDIR}" | sed 's|//|/|g')"

mkdir -p "${ETCDIR}"
if [ -f "${ETCDIR}/makepkg.conf" ];
then
    echo "Backing up '${ETCDIR}/makepkg.conf' to '${ETCDIR}/makepkg.conf.bak'" >&2
    cp -f "${ETCDIR}/makepkg.conf" "${ETCDIR}/makepkg.conf.bak"
    if [ $? -ne 0 ];
    then
        echo "Failed to backup '${ETCDIR}/makepkg.conf' to '${ETCDIR}/makepkg.conf.bak'" >&2
        exit 1
    fi
fi

install -v -m 644 "data/makepkg.conf" "${ETCDIR}/makepkg.conf"
RET=$?
if [ $RET -ne 0 ];
then
    echo;
    echo;
    echo 'Error installing extras! Please check error messages above for more info!'
    exit $RET
fi

