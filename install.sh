#!/bin/bash

# Installs all the pacman-utils

ALL_FILES="installpackage buildit.sh whatprovides"

install -m 755 ${ALL_FILES} "/usr/bin"
