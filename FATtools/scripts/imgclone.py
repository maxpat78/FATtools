# -*- coding: utf-8 -*-
import sys, os, argparse, logging
from FATtools.scripts.mkvdisk import call as mkvdisk
from FATtools import Volume
from FATtools.FAT import Handle
from FATtools.utils import is_vdisk

DEBUG = 0
from FATtools.debug import log

#~ logging.basicConfig(level=logging.DEBUG, filename='imgclone.log', filemode='w')

class Arguments:
    pass


def imgclone(src, dest, force=0):
    img1 = Volume.vopen(src, what='disk')
    if not img1:
        print("Couldn't open source disk image file '%s'"%src)
        sys.exit(1)
    if os.path.exists(dest) and not force:
        #~ print("imgclone error: destination disk image already exists, use -f to force overwriting")
        print("imgclone error: destination disk image already exists")
        sys.exit(1)
    o = Arguments()
    o.base_image = None
    o.large_sectors = None
    o.monolithic = None
    o.image_size = str(img1.size)
    o.image_file = dest
    mkvdisk(o)
    img2 = Volume.vopen(dest, 'r+b', what='disk')
    print('Copying %.02f MiB...' % (img1.size/(1<<20)))
    while True:
        s = img1.read(2<<20)
        if not s: break
        img2.write(s)
    img1.close()
    img2.close()
    print('Done.')

def printn(s): print(s)

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    fattools imgclone src.img dest.img
    """
    par = parser_create_fn(*parser_create_args,usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Clones a virtual disk image into another one, copying its blocks one by one.",
    epilog="Examples:\nfattools imgclone raw.img dynamic.vhd\n\nUseful for optimizing or converting between supported image formats.\n")
    par.add_argument('items', nargs='+')
    return par

def call(args):
    if len(args.items) != 2:
        print("imgclone error: you must specify a source and a destination disk image file!")
        sys.exit(1)
    imgclone(args.items[0], args.items[1])

if __name__ == '__main__':
    par=create_parser()
    args = par.parse_args()
    call(args)
