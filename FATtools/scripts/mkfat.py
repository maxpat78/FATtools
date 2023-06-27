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
    par.add_argument("-c", "--cluster", dest="cluster_size", help="force a specified cluster size between 512, 1024, 2048, 4096, 8192, 16384, 32768 (since DOS) or 65536 bytes (Windows NT+; 128K and 256K clusters are allowed with 4K sectors) for FAT. exFAT permits clusters up to 32M. Default: based on medium size. Accepts 'k' and 'm' postfix for Kibibytes and Mebibytes.", metavar="CLUSTER")
    par.add_argument("-p", "--partition", dest="part_type", help="create a single partition from all disk space before formatting. Accepts MBR (up to 2 TB; 16 TB with 4K sectors), GPT or MBR_OLD (2 GB max, MS-DOS <7.1 compatible)", metavar="PARTTYPE")
    par.add_argument("--fat-copies", dest="fat_copies", help="set the number of FAT tables (default: 2)", metavar="COPIES")
    par.add_argument("--fat32compat", action="store_true",  dest="fat32_compat", help="FAT32 is applied in Windows XP compatibility mode, i.e. only if 65525 < clusters < 4177918 (otherwise: 2^28-11 clusters allowed)")
    par.add_argument("--no-fat12", action="store_true",  dest="fat12_disable", help="FAT12 is never applied to small hard disks (~127/254M on DOS/NT systems)")
    par.add_argument("--no-64k-cluster", action="store_true",  dest="disable_64k", help="cluster size is limited to 32K (DOS compatibility)")
    return par

def call(args):
    # Try to open first partition...
    dsk = vopen(args.fs, 'r+b', 'partition0')
    # ...if it fails, or it's explicitly required to partition, try to open disk
    if dsk in ('EINV', 'EINVMBR') or args.part_type:
        if type(dsk) != str: dsk.close()
        dsk = vopen(args.fs, 'r+b', 'disk')
        if dsk == 'EINV':
            print('Invalid disk or image file specified!')
            sys.exit(1)

    SECTOR = 512
    if dsk.type() == 'VHDX' and dsk.metadata.physical_sector_size == 4096: SECTOR = 4096
    opts={'phys_sector':SECTOR}

    # Windows 10 Shell (or START command) happily auto-mounts a VHD ONLY IF partitioned and formatted
    # However, a valid VHD is always mounted and can be handled with Diskpart (GUI/CUI)
    if args.part_type:
        t = args.part_type.lower()
        if t not in ('mbr', 'mbr_old', 'gpt'):
            print('You must specify MBR, MBR_OLD or GPT to auto partition disk space!')
            sys.exit(1)
        print("Creating a %s partition with all disk space..."%t.upper())
        if t in ('mbr', 'mbr_old'):
            if dsk.size > (2<<40): 
                if SECTOR==512: 
                    print('You MUST use GPT partition scheme with disks >2TB!')
                    sys.exit(1)
            opts['lba_mode'] = 1
            if dsk.size > (16<<40) and SECTOR==4096: 
                print('You MUST use GPT partition scheme with 4K sectored disks >16TB!')
                sys.exit(1)
            if t == 'mbr_old':
                if dsk.size > (2<<30): print('Warning: old DOS does not like primary partitions >2GB, size reduced automatically!')
                partutils.partition(dsk, 'mbr', {'compatibility':0})
                if args.fs_type and args.fs_type.lower() == 'fat32':
                    args.fs_type = 'fat16'
                    print('Warning: old DOS does not know FAT32, switching to FAT16.')
            else:
                partutils.partition(dsk, 'mbr', options=opts)
        else:
            partutils.partition(dsk, 'gpt', options=opts)
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
        if t in ('fat12','fat16','fat32'):
            format = fat_mkfs
            params['fat_bits'] = {'fat12':12,'fat16':16,'fat32':32}[t]
        elif t == 'exfat':
            format = exfat_mkfs
        else:
            print("mkfat error: bad file system specified!")
            sys.exit(1)
    else:
        if dsk.size < 126<<30: # 126G
            format = fat_mkfs
            t = 'FAT'
        else:
            format = exfat_mkfs
            t = 'exFAT'
        print("%s format selected." % t)

    if args.cluster_size:
        max_clust = 17
        if SECTOR == 4096: max_clust = 19
        t = args.cluster_size.lower()
        if t[-1] == 'k':
            params['wanted_cluster'] = int(args.cluster_size[:-1])<<10
        elif t[-1] == 'm':
            params['wanted_cluster'] = int(args.cluster_size[:-1])<<20
        else:
            params['wanted_cluster'] = int(args.cluster_size)
        if params['wanted_cluster'] not in [512<<i for i in range(0,max_clust)]:
            print("mkfat error: bad cluster size specified!")
            sys.exit(1)

    if args.fat32_compat:
        params['fat32_forbids_low_clusters'] = 1
        params['fat32_forbids_high_clusters'] = 1
    if args.fat12_disable:
        params['fat12_disabled'] = 1
    if args.disable_64k:
        params['fat_no_64K_cluster'] = 1
    if args.fat_copies:
        params['fat_copies'] = int(args.fat_copies)

    params['show_info'] = 1
    ret = format(dsk, dsk.size, SECTOR, params)
    if ret != 0:
        print('mkfat failed!')
        sys.exit(1)
    if ret == 0 and dsk.size > (2880<<10) and dsk.type() == 'partition':
        # don't change if GPT partition
        if dsk.mbr.partitions[0].bType == 0xEE: return
         # set the right MBR partition type
        if format == exfat_mkfs:
            dsk.mbr.partitions[0].bType = 7
        else:
            if params['fat_bits'] == 32:
                dsk.mbr.partitions[0].bType = 0xB
                if dsk.size > 1024*255*63*SECTOR: dsk.mbr.partitions[0].bType = 0xC
            elif params['fat_bits'] == 16:
                dsk.mbr.partitions[0].bType = 6
                if dsk.size < (32<<20): dsk.mbr.partitions[0].bType = 4
            elif params['fat_bits'] == 12: dsk.mbr.partitions[0].bType = 4
        # update the MBR
        dsk.disk.seek(0)
        dsk.disk.write(dsk.mbr.pack(SECTOR))
        


if __name__ == '__main__':
    par=create_parser()
    args = par.parse_args()
    call(args)
