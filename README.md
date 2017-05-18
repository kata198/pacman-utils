# pacman-utils
Some utils and helper scripts for archlinux packages


whatprovides
------------

Check for what package provides a given file, directory, or command.

Whatprovides generates and uses a cache of the listed package-ownership database, and automatically updates when packages are installed/removed/upgraded.

Examples:

	[tim ]$ whatprovides ld
	binutils

	[tim ]$ whatprovides /etc/ntp.conf
	ntp


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

