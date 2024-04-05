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

