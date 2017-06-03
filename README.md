# pacman-utils
Some utils and helper scripts for archlinux packages


How to install
==============

As root, run ./install.sh followed by ./install\_data.sh . Please use the install\_data.sh instead of running your own!



I will update the *data*/providesDB often (for whatprovides\_upstream). Only generate your own if you absolutely need to, like if you are freezing a version of archlinux for offline forking for an internal OS.

whatprovides
------------

Check for what package provides a given file or directory, from installed packages on the system.

Whatprovides generates and uses a cache of the listed package-ownership database, and automatically updates when packages are installed/removed/upgraded.

Supports glob expressions, i.e. '\*/libc.so\*' . If in glob mode, will print the providing package followed by a tab and the provided file.


Examples:

	[tim ]$ whatprovides ld
	binutils

	[tim ]$ whatprovides /etc/ntp.conf
	ntp

	[tim ]$ whatprovides '*/libc.so*'
	glibc   /usr/lib/libc.so
	glibc   /usr/lib/libc.so.6
	lib32-glibc     /usr/lib32/libc.so
	lib32-glibc     /usr/lib32/libc.so.6



whatprovides\_upstream
----------------------

Check for what package provides a given file or directory, from all available packages (installed or available).

This requires *extractMtree.py* to be ran to create the providesDB, or (recommended) use the data/providesDB (copy to /var/lib/pacman/.providesDB) as it takes a long time to run, and will strain mirros if everyone starts running them. I'd like to get the mirrors to execute a modified version locally and supply providesDB, but they currently don't.

I update this version often.

Supports glob expressions, i.e. '\*/libc.so\*' . If in glob mode, will print the providing package followed by a tab and the provided file.

installpackage
--------------

Installs packages, for use with **makepkg** and lazy people \[ aren't we all? :) \] 

With no argument, installs all packages in current directory.

With a single argment, installs all packages whose name matches given glob pattern ( e.x. *installpackage 'utils'*  would install all packages in current dir that contain the word 'utils'. *installpackage '-2'* would install all packages, release 2 (useful for when multiple conflicting releases are in same dir).

Passing -d followed by a directory name, like  *installpackage -d mypkgs* would install all packages in the "mypkgs" directory.

If you are not already root, calling "installpackage" will prompt for root password and su into root to perform the operation.


buildpkg.sh
----------

Builds (from abs) and installs a provided list of packages.

Will automatically refresh abs database if it exceeds 1 day of age (by default).

It's a set-it-and-forget-it build system! If any errors occur, they will be reported at the bottom. No prompts, no mess, no fuss!

Example Usage:

	buildit.sh tar xz zip  # This will compile and install "tar" "xz" and "zip" packages based on settings in /etc/makepkg.conf



abs2
----


Sorta supports some old "abs-style" development, whilst not
overwhelimg the archlinux servers as developers have requested

Fetches a package into a new "abs2" dir style, which is shared amongst
all users in the "users" group.

Arguments are individual package names, and will fetch that package.


Example Usage:

	abs2 zip  # Get the "zip" package

	NEW_DIR=$(abs2 -q zi[)  # Get the directory of the zip package
	cd ${NEW_DIR}/trunk     # Go to trunk dir therein


You should NOT modify the contents of these manually, but copy them off to /usr/src/arch


archsrc-getpkg
--------------

Gets the latest version of an archlinux package build files, and checks them out to current dirctory.

Optionally backs up the old directory. Will not remove data without user input.


Profile Guided Optimization
===========================

Profile guided optimization is easily supported and incorporated with archlinux and these tools.

