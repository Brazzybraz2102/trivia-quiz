#!/bin/sh
# Install (or update) the Rhythmbox plugins in this folder for the current user.
# Usage: ./install.sh            install both plugins
#        ./install.sh --remove   uninstall them
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="${XDG_DATA_HOME:-$HOME/.local/share}/rhythmbox/plugins"

for plugin in name-that-tune now-playing; do
    rm -rf "$dest/$plugin"
    if [ "$1" != "--remove" ]; then
        mkdir -p "$dest/$plugin"
        cp "$here/$plugin"/*.plugin "$here/$plugin"/*.py "$dest/$plugin/"
        echo "Installed $plugin -> $dest/$plugin"
    else
        echo "Removed $plugin"
    fi
done

if [ "$1" != "--remove" ]; then
    echo
    echo "Restart Rhythmbox, then turn the plugins on in Preferences > Plugins."
fi
