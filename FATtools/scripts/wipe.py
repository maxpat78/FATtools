# -*- coding: utf-8 -*-
import sys, os, argparse, logging
from FATtools import Volume

DEBUG = 0
from FATtools.debug import log

#~ logging.basicConfig(level=logging.DEBUG, filename='wipe.log', filemode='w')

class Arguments:
    pass


def wipe(img):
    v = Volume.vopen(img,'r+b')
    if not hasattr(v, 'wipefreespace'):
        print("Couldn't open a FAT/exFAT filesystem inside '%s'"%img)
        sys.exit(1)
    print('Wiping %d free clusters (%d bytes) . . .' % v.getdiskspace())
    v.wipefreespace()
    print('Done.')

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    fattools wipe IMAGE_FILE
    """
    par = parser_create_fn(*parser_create_args,usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Wipes the free space in an (ex)FAT formatted disk, zeroing all free clusters.",
    epilog="Combined with imgclone tool, it permits to optimize a virtual disk image size.\n")
    par.add_argument('image_file', nargs=1)
    return par

def call(args):
    wipe(args.image_file[0])

if __name__ == '__main__':
    par=create_parser()
    args = par.parse_args()
    call(args)
