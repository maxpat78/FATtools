import struct, os, sys, pprint, math, importlib, locale
import argparse
from FATtools import utils, partutils
from FATtools.FAT import *
from FATtools.exFAT import *
from FATtools.Volume import vopen
from FATtools.mkfat import *


def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    par = parser_create_fn(*parser_create_args,description="Applies a FAT12/16/32 or exFAT File System to a disk device or file image.")
    par.add_argument('fs',help="The image file or disk device to write to",metavar="FS")
    par.add_argument("-t", "--fstype", dest="fs_type", help="try to apply the specified File System between FAT12, FAT16, FAT32 or EXFAT. Default: based on medium size.", metavar="FSTYPE")
    par.add_argument("-c", "--cluster", dest="cluster_size", help="force a specified cluster size between 512, 1024, 2048, 4096, 8192, 16384, 32768 (since MS-DOS) or 65536 bytes (Windows NT+) for FAT. exFAT permits up to 32M. Default: based on medium size. Accepts 'k' and 'm' postfix for Kibibytes and Mebibytes.", metavar="CLUSTER")
    par.add_argument("-p", "--partition", dest="part_type", help="create a single partition from all disk space before formatting. Accepts MBR, GPT or MBR_OLD (2 GB max, MS-DOS <7.1 compatible)", metavar="PARTTYPE")
    return par

def call(args):
    dsk = vopen(args.fs, 'r+b', 'disk')
    if dsk == 'EINV':
        print('Invalid disk or image file specified!')
        sys.exit(1)

    # Windows 10 Shell happily auto-mounts a VHD ONLY IF partitioned and formatted
    if args.part_type:
        t = args.part_type.lower()
        if t not in ('mbr', 'mbr_old', 'gpt'):
            print('You must specify MBR, MBR_OLD or GPT to auto partition disk space!')
            sys.exit(1)
        print("Creating a %s partition with all disk space..."%t.upper())
        if t in ('mbr', 'mbr_old'):
            if dsk.size > (2<<40): 
                print('You must specify GPT partition scheme with disks >2TB!')
                sys.exit(1)
            if t == 'mbr_old':
                mbrtyp = 6
                if dsk.size < 32<<20:
                    mbrtyp = 4
                if args.fs_type and args.fs_type.lower() == 'fat32':
                    mbrtyp = 0xB
                partutils.partition(dsk, 'mbr', mbr_type=mbrtyp)
            else: # MS-DOS >7.0
                partutils.partition(dsk, 'mbr', mbr_type=0xC)
        else:
            partutils.partition(dsk)
        dsk.close()
        dsk = vopen(args.fs, 'r+b', 'partition0')
        if type(dsk) == type(''):
            print("mkfat error opening new partition:", dsk)
            sys.exit(1)
        else:
            print("Disk was correctly partitioned with %s scheme."%t.upper())
            

    params = {}

    if args.fs_type:
        t = args.fs_type.lower()
        if t == 'fat12':
            format = fat12_mkfs
        elif t == 'fat16':
            format = fat16_mkfs
        elif t == 'fat32':
            format = fat32_mkfs
            params['fat32_allows_few_clusters'] = 1
        elif t == 'exfat':
            format = exfat_mkfs
        else:
            print("mkfat error: bad file system specified!")
            par.print_help()
            sys.exit(1)
        params['wanted_fs'] = t
    else:
        if dsk.size < 127<<20: # 127M
            format = fat12_mkfs
            t = 'FAT12'
        elif 127<<20 <= dsk.size < 2047<<20: # 2G
            format = fat16_mkfs
            t = 'FAT16'
        elif 2047<<20 <= dsk.size < 126<<30: # 126G, but could be up to 8T w/ 32K cluster
            format = fat32_mkfs
            t = 'FAT32'
        else:
            format = exfat_mkfs # can be successfully applied to an 1.44M floppy, too!
            t = 'exFAT'
        print("%s file system auto selected..." % t)

    if args.cluster_size:
        t = args.cluster_size.lower()
        if t[-1] == 'k':
            params['wanted_cluster'] = int(args.cluster_size[:-1])<<10
        elif t[-1] == 'm':
            params['wanted_cluster'] = int(args.cluster_size[:-1])<<20
        else:
            params['wanted_cluster'] = int(args.cluster_size)
        if params['wanted_cluster'] not in [512<<i for i in range(0,17)]:
            print("mkfat error: bad cluster size specified!")
            par.print_help()
            sys.exit(1)

    format(dsk, dsk.size, params=params)

if __name__ == '__main__':
    par=create_parser()
    args = par.parse_args()
    call(args)
