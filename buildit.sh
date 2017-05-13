#!/bin/bash

# Copyright (c) 2017 Timothy Savannah - All Rights Reserved
#   This code is licensed under the terms of the APACHE license version 2.0
#
# buildit.sh - Builds and installs one or more packages.
#   Run as regular user, and provide as arguments a list of packages (available through abs)

# ABS_STALE_AFTER - Max age in seconds of abs dir. Will refresh after this time.
#  86400 = one day

ABS_STALE_AFTER=86400


echoerr() {
    echo "$@" >&2
}

if [ "$1" = "--help" -o "$1" = "-h" ];
then
    echoerr "Usage: buildit.sh [package_name] (Optional: [package_name2] [package_name..N])"
    echoerr "   Builds listed packages as current user, and then installs resulting packages as root."
    echoerr;
    exit 1
fi


if [ "$1" != "_secret_arg_" ];
then
    BUILD_AS="$(whoami)"
    if [ "$BUILD_AS" = "root" ];
    then
        echoerr "You must run buildit.sh as a non-root user (to build the package)."
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

need_update_abs() {
    if [ ! -f "/var/abs/.buildit_last_fetch" ];
    then
        return 0;
    fi
    NOW="$(date +%s)"
    THEN="$(cat /var/abs/.buildit_last_fetch)"

    [ $(( ${NOW} - ${THEN} )) -ge ${ABS_STALE_AFTER} ] && return 0;
    return 1;
}


maybe_update_abs() {
    if ! ( need_update_abs );
    then
        return;
    fi

    /usr/bin/abs >/dev/null
    if [ $? -ne 0 ];
    then
        echoerr "Failed to refresh ABS database."
    else
        echoerr "Refreshed ABS database."
    fi
    
    NOW="$(date +%s)"
    printf "%s" "${NOW}" > /var/abs/.buildit_last_fetch;
}

[ ! -d "/usr/src/arch" ] && mkdir -p '/usr/src/arch'

maybe_update_abs;

PKGNAME="$1"

if [ -d "/usr/src/arch/$PKGNAME" ];
then
    rm -Rf "/usr/src/arch/${PKGNAME}.bak" || exiterr "Cannot remove old directory"
    mv "/usr/src/arch/${PKGNAME}" "/usr/src/arch/${PKGNAME}.bak" || exiterr "Cannot move old directory to .bak"
fi

SUCCESS=0
for repo in "core" "extra" "community" "testing";
do
    if [ -d "/var/abs/$repo/$PKGNAME" ];
    then
        cp -R "/var/abs/$repo/$PKGNAME" "/usr/src/arch/$PKGNAME" || exiterr "Cannot copy from abs to /usr/src/arch"
        SUCCESS=1
        break
    fi
done

if [ $SUCCESS -ne 1 ];
then
    exiterr "Failed to find $PKGNAME in /var/abs"
fi

chown -R "${BUILD_AS}" "/usr/src/arch/$PKGNAME"
pushd "/usr/src/arch/$PKGNAME"
su "${BUILD_AS}" /usr/bin/makepkg || exiterr "makepkg failed"
pacman -U --noconfirm *.pkg.tar.* || exiterr "Failed to install"
popd

