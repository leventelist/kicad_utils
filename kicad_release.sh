#!/bin/sh

#edit these
PCB_FILE=$1

#Layers to export
LAYERS="F.Cu,B.Cu,B.Paste,F.Paste,B.Mask,F.Mask,F.Silkscreen,B.Silkscreen,Edge.Cuts"

if [ -z $PCB_FILE ]; then
	echo "Usge $0 pcb_file_name"
	exit 1
fi

#Test if pcb file exists
if [ ! -e $PCB_FILE ]; then
	echo "File $PCB_FILE does not exist."
	exit 1
fi

#Output directory
PCBOUTDIR=release_plot

#Utils
GERBV=gerbv
EVINCE=evince

# Replace this whatever you want. It will be visible with the filename
VERSION=`git describe --tags`

#Don't edit further
DATE_S=`date +%Y%m%d`

OUTFILEBASE=${PCB_FILE%.*}_${VERSION}_${DATE_S}

#create destination directory, if not existed.
mkdir -p $PCBOUTDIR

#clean up
rm -rf $PCBOUTDIR/*
rm -f *.tar.bz2 *.rar *.zip

#do the output
echo "Creating output...\n\n"

kicad-cli pcb export gerbers -o ${PCBOUTDIR}/ -l ${LAYERS} --ev --subtract-soldermask --use-drill-file-origin ${PCB_FILE}
kicad-cli pcb export drill --excellon-separate-th --drill-origin plot --generate-map -o ${PCBOUTDIR}/ ${PCB_FILE}
kicad-cli pcb export pos --units mm --use-drill-file-origin -o ${PCBOUTDIR}/xy.csv ${PCB_FILE}


#pack
echo "Creating archive...\n\n"

tar -jcvf ${OUTFILEBASE}.tar.bz2 ${PCBOUTDIR}
rar a ${OUTFILEBASE}.rar ${PCBOUTDIR}
zip -r ${OUTFILEBASE}.zip ${PCBOUTDIR}

#profit
$GERBV ${PCBOUTDIR}/* &
$EVINCE ${PCBOUTDIR}/*.pdf &
