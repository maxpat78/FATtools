import sys
from FATtools.Volume import vopen
from FATtools.scripts import mkvdisk, mkfat

#~ src = vopen('KINGSTON_OLD_16GB.img', what='disk')
src = vopen('F.img', what='disk')
print(src, src.size)

# src.size va trasformato in MB per creare un disco di dimensione almeno pari al volume
par = mkvdisk.create_parser(parser_create_args=[])
args = par.parse_args(['-f', '-s 1G', 'image.vhd'])
mkvdisk.call(args)

par=mkfat.create_parser(parser_create_args=[])
args = par.parse_args(['-pMBR', '-tfat32', 'image.vhd'])
mkfat.call(args)

dst = vopen('image.vhd', mode='r+b', what='partition0')

todo = src.size
while todo > 0:
    s = src.read(4<<20)
    dst.write(s)
    todo -= (4<<20)

# nel MBR del disco, la partizione in cui e' stato copiato il volume va ridimensionata di conseguenza
""" === TODO: ===
- exFAT: il campo dwTotalSectors nel MBR *DEVE* corrispondere al campo
u64VolumeLength nel Boot, altrimenti Windows 11 NON MONTA il disco creato
copiando un volume raw in una partizione

In generale, occorre che la dimensione della partizione sia congrua (>=?)
con il volume copiato in essa?"""