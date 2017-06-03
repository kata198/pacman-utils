#!/bin/bash

# vim: set ts=4 sw=4 expandtab :

# Copyright (c) 2017 Timothy Savannah - All Rights Reserved
#   This code is licensed under the terms of the APACHE license version 2.0
#
# buildpkg.sh - Builds and installs one or more packages.
#   Run as regular user, and provide as arguments a list of packages (available through abs)

# ABS_STALE_AFTER - Max age in seconds of abs dir. Will refresh after this time.
#  86400 = one day

ABS_STALE_AFTER=86400


echoerr() {
    echo "$@" >&2
}


if [ "$1" != "_secret_arg_" ];
then
    BUILD_AS="$(whoami)"
    if [ "$BUILD_AS" = "root" ];
    then
        echoerr "You must run buildpkg.sh as a non-root user (to build the package)."
        exit 1;
    fi
    if [ $# -eq 0 ];
    then
        echoerr "Missing package name(s)."
        exit 1;
    fi
    
    if ( which sudo >/dev/null 2>&1 );
    then
        sudo $0 "_secret_arg_" "${BUILD_AS}" "$@";
        exit $?
    else
        echo "Login as root:"
        su root -c "$0 _secret_arg_ ${BUILD_AS} $@";
        exit $?
    fi
fi

shift; # _secret_arg_

BUILD_AS="${1}"

shift # BUILD_AS


if [ $# -gt 1 ];
then
    FAILED=
    for arg in "$@";
    do
        $0 "_secret_arg_" "${BUILD_AS}" "$arg"
        if [ $? -ne 0 ];
        then
            FAILED="$FAILED $arg"
        fi
    done
    if [ ! -z "$FAILED" ];
    then
        echo "Following packages failed: $FAILED"
        exit 2
    fi
    exit 0
fi


exiterr() {
    echo "[$PKGNAME] (`date`) $1" >&2
    exit 2
}

update_abs() {

    ABS_DIR="$(abs2 "${1}")"

    echo "${ABS_DIR}"
}

[ ! -d "/usr/src/arch" ] && mkdir -p '/usr/src/arch'


PKGNAME="$1"

ABS_DIR="$(update_abs "${PKGNAME}")"
if [ -z "${ABS_DIR}" ];
then
    exiterr "Failed to find ABS2 dir.\n"
fi

if [ -d "/usr/src/arch/$PKGNAME" ];
then
    rm -Rf "/usr/src/arch/${PKGNAME}.bak" || exiterr "Cannot remove old directory"
    mv "/usr/src/arch/${PKGNAME}" "/usr/src/arch/${PKGNAME}.bak" || exiterr "Cannot move old directory to .bak"
fi

cp -R "${ABS_DIR}/trunk" "/usr/src/arch/${PKGNAME}" || exiterror "Cannot copy ${ABS_DIR}/trunk to /usr/src/arch/${PKGNAME}"

chown -R "${BUILD_AS}" "/usr/src/arch/$PKGNAME"
pushd "/usr/src/arch/$PKGNAME"
su "${BUILD_AS}" /usr/bin/makepkg || exiterr "makepkg failed"
pacman -U --noconfirm *.pkg.tar.* || exiterr "Failed to install"
popd

# vim: set ts=4 sw=4 expandtab :
