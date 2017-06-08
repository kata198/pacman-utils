#!/bin/bash

# Installs all the pacman-utils
#  use ./install.sh PREFIX=$HOME to install to local home dir.

BIN_FILES="installpackage archsrc-buildpkg whatprovides whatprovides_upstream mkgcdatar getpkgs abs2 archsrc-getpkg pacman-mirrorlist-optimize extractMtree.py aur-getpkg"

process_installdir_args() {

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

}

########################
## failed_install - 
##          Output that install was a failure, and exit with provided code
##
##      Args:
##
##          Arg1 (optional)  <int>  - Exit Code
##
##                                    If not provided, default: 1
##
##          Arg2 (optional)  <str>  - Optional step name  (e.x. "mkdir" )
##                                      Used for the error message output to user.
##
##                                    If not provided - "Install" will be used
##
##     Notes:
##
##         * WILL TERMINATE PROGRAM
##         * This function's documentation is 3x the length of its body!
##
##
failed_install() {
    _FI_EXIT_CODE="$1"
    _FI_OPT_NAME="$2"

    [ -z "${_FI_EXIT_CODE}" ] && _FI_EXIT_CODE=1

    [ -z "${_FI_OPT_NAME}" ] && _FI_OPT_NAME="Install"

    printf "\n\n" >&2
    printf "ERR: Install returned non-zero [ %d ]. Check above for errors.\n\n" "${RET}"

    exit ${_FI_EXIT_CODE}
}


#####################
## MAIN
#########

process_installdir_args "$@";

BINDIR="${DESTDIR}/${PREFIX}/bin"
BINDIR="$(echo "${BINDIR}" | sed 's|//|/|g')"

mkdir -p "${BINDIR}"

install -v -m 755 ${BIN_FILES} "${BINDIR}" || failed_install 1 "Install programs to '${BINDIR}'"

cd "${BINDIR}"
rm -f archsrc-buildpkg.sh buildpkg.sh

# buildpkg.sh
# echo "Creating link from ${BINDIR}/archsrc-buildpkg -> ${BINDIR}/buildpkg.sh  (new name -> old name)"
# ln -sf archsrc-buildpkg buildpkg.sh # Right now, just symlink these two names


#install -v -m 644 "data/providesDB" "${DESTDIR}/var/lib/pacman/.providesDB"
