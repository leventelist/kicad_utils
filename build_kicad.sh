#!/bin/sh

NPROC=`nproc`
NICE=20

set -e

git clean -xfd

mkdir -p build
cd build

cmake -DCMAKE_BUILD_TYPE=Release -DKICAD_USE_OCE=OFF -DKICAD_USE_OCC=ON -DKICAD_SCRIPTING_PYTHON3=ON -DKICAD_SCRIPTING_WXPYTHON_PHOENIX=ON ../
nice -n $NICE make -j$NPROC
