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

