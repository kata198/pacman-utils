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
