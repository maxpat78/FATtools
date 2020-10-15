# -*- coding: utf-8 -*-

import sys, argparse
from FATtools import vhdutils, vdiutils, vmdkutils
from FATtools import Volume


def is_vdisk(s):
    "Returns the base virtual disk image path if it contains a known extension or an empty string"
    image_path=''
    for ext in ('vhd', 'vdi', 'vmdk', 'img', 'dsk', 'raw', 'bin'):
        if '.'+ext in s.lower():
            i = s.lower().find(ext)
            image_path = s[:i+len(ext)]
            break
    return image_path

def printn(s):
 print(s)



help_s = """
cp.py <file1 or dir1> [file2 or dir2...] <dst>
"""
par = argparse.ArgumentParser(usage=help_s,
formatter_class=argparse.RawDescriptionHelpFormatter,
description="Copies files and directories between real and virtual disks.",
epilog="Examples:\ncp.py File1.txt File2.txt Dir1 image.vhd/\ncp.py File*.txt Dir? image.vhd/Subdir\ncp.py image.vhd/Subdir1 C:\\MyDir")
par.add_argument('items', nargs='*')
args = par.parse_args()

if len(args.items) < 2:
    print("copy error: you must specify at least one source and the destination!")
    par.print_help()
    sys.exit(1)

dest = args.items.pop()
if is_vdisk(dest):
    dst_image = is_vdisk(dest)
    sub_path = dest[len(dst_image):]
    dest = Volume.vopen(dst_image, 'r+b')
    if sub_path:
        dest = dest.mkdir(sub_path[1:])
    Volume.copy_in(args.items, dest, printn, 2)
else:
    print ('NOT SUPPORTED (YET)')
