# MangaDex plugin for Comic Tagger

A plugin for [Comic Tagger](https://github.com/comictagger/comictagger/releases) to allow the use of the metadata from [MangaDex](https://mangadex.org).

## Installation

The easiest installation method as of ComicTagger 1.6.0-beta.1 for the plugin is to place the [release](https://github.com/mizaki/mangadex_talker/releases) zip file
`mangadex_talker-plugin-<version>.zip` (or wheel `.whl`) into the [plugins](https://github.com/comictagger/comictagger/wiki/Installing-plugins) directory.

## Development Installation

You can build the wheel with `tox run -m build` or clone ComicTagger and clone the talker and install the talker into the ComicTagger environment `pip install -e .`
