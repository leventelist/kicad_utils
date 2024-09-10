# kicad_utils
Random scripts for KiCad


# kicad_release.sh

This will do the following things:

* Call kicad_cli to create Gerbers, XY, and drill reports
* Packs up everything to zip, tar and rar archives
* It names the archive with a timestamp and git version
* Opens gerbv and evince with the generated output

By default, it uses the old gerbv program from gEDA, but this can be changed.

## Prerequisites

zip, tar, bzip2, git, evince, gerbv, bash

## Usage

kicad_release.sh your_kicad_pcb_file.kicad_pcb

# build_kicad.sh

This is a small script that configures KiCad source, and compiles it. It sets the -j parameter of make to the number of online processor count. It also sets the nice value to the lowest priority to the lowest.

It creates a build directory, and all artifacts are stored there.

## Usage

* Check out the sources of KiCad
* Get all the required prerequisites
* cd to the sources directory
* Run the build script
* cd to the build directory, and issue 'sudo make install'

# bomer2.py

This is a python script that creates nice BOMs from a schematic using a database backend.

It generates a CSV file that can be further processed.

Run the script like this:

bomer2.py -d <path/to/sqlite/database/file> -o <output_file_name.csv> <path/to/top/level/kicad_sch/file>

## Database descriptor

You can find a sample database in the db directory. There's also a kicad_dbl file provided. You might change the 'levcsi_database' name to something more sensible.

If you place a component with the choser, KiCad will automatically add id=xxx field. The bomer2.py script uses this to reference the component.
