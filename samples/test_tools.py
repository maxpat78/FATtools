# -*- coding: utf-8 -*-

VHD_MODE = 0

import os, sys, glob, ctypes, uuid, shutil, logging
import hexdump

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

logging.basicConfig(level=logging.DEBUG, filename='test_tools.log', filemode='w')

from FATtools.debug import log
from FATtools import Volume, mkfat, vhdutils, partutils
import stress
Volume.exFAT.hexdump = hexdump


def printn(s): print(s)

def test(img_file, fssize=32<<20, fat_type='exfat'):
    VHD_MODE=0
    
    if img_file.lower().endswith('.vhd'):
        VHD_MODE=1
        log("Creating a blank %.02f MiB Dynamic VHD disk image", (fssize/(1<<20)))
        print("Creating a blank %.02f MiB Dynamic VHD disk image" % (fssize/(1<<20)))
        vhdutils.mk_dynamic(img_file, fssize, upto=40<<30, overwrite='yes')
    else:
        if img_file[-1] != ':' and '\\\\.\\' not in img_file:
            log("Creating a blank %.02f MiB disk image", (fssize/(1<<20)))
            print("Creating a blank %.02f MiB disk image" % (fssize/(1<<20)))
            f = open(img_file,'wb'); f.seek(fssize); f.truncate(); f.close()

    f = Volume.vopen(img_file, 'r+b', 'disk')
    if f == 'EINV':
        print('Invalid disk or image file specified to test!')
        sys.exit(1)

    if len(sys.argv)>2 and sys.argv[2]=='mbr':
        print("Creating a MBR partition on disk")
        gpt = partutils.partition(f, 'mbr', mbr_type=6)
    else:
        print("Creating a GPT partition on disk")
        gpt = partutils.partition(f)
    f.close() # always close, to avoid tstamp problems!
    
    print("Applying FAT File System on partition:", fat_type)
    log("Applying FAT File System on partition: %s", fat_type)
    f = Volume.vopen(img_file, 'r+b', 'partition0')
    print('Opened', f)
    log('Opened %s', f)
    if fat_type == 'exfat':
        fmt = mkfat.exfat_mkfs
    elif fat_type == 'fat32':
        fmt = mkfat.fat32_mkfs
    elif fat_type == 'fat16':
        fmt = mkfat.fat16_mkfs
    elif fat_type == 'fat12':
        fmt = mkfat.fat12_mkfs
    if len(sys.argv)>2 and sys.argv[2]=='mbr':
        fmt(f, f.size)
    else:
        fmt(f, (gpt.partitions[0].u64EndingLBA-gpt.partitions[0].u64StartingLBA+1)*512)
    f.close()

    #~ root = openpart(DISK, 'r+b').open()
    #~ root.create('a.txt').write('CIAO')

    print("Injecting a tree")
    log("Injecting a tree")

    def mktree():
        try:
            os.mkdir('t')
            os.mkdir('t/a')
            os.mkdir('t/a/a1')
            os.mkdir('t/a/a1/a2')
        except WindowsError:
            pass
        for base in ('t/a/a1/a2', 't/a/a1', 't/a'):
            for i in range(20):
                pn = base+'/File%02d.txt'%i
                open(pn,'w').write(pn)
    mktree()
    root = Volume.vopen(img_file, 'r+b')
    subdir = root.mkdir('T')
    Volume.copy_tree_in('.\T', subdir, printn, 2)
    root.flush()
    #~ root.close() # always close, to avoid tstamp problems!

    if VHD_MODE:
        print("Creating a blank %.02f MiB Differencing VHD disk image, linked to previous one" % (fssize/(1<<20)))
        vhdutils.mk_diff(img_file[:-4]+'_delta.vhd', img_file, overwrite='yes')

        root = Volume.vopen(img_file[:-4]+'_delta.vhd', 'r+b')
        root.create('a.txt').write(b'CIAO')
        root.rmtree('T')
        root.flush()

        subdir = root.mkdir('T')
        Volume.copy_tree_in('.\T', subdir, printn, 2)
        root.flush()

    shutil.rmtree('t')
    
    print("Running stress test...")
    class Opts():
     pass
     
    opts = Opts()
    opts.threshold=60
    opts.file_size=1<<20
    opts.programs=63
    #~ opts.programs=31 # exclude buggy dir cleaning
    opts.debug=7
    opts.sha1=1
    opts.sha1chk=0
    opts.fix=0
    #~ stress.seed(4)
    if VHD_MODE:
        stress.stress(opts, [img_file[:-4]+'_delta.vhd'])
    else:
        stress.stress(opts, [img_file])



if __name__ == '__main__':
    fmts = ['fat12', 'fat16', 'fat32', 'exfat']
    for fmt in fmts:
        test(sys.argv[1], fat_type=fmt)
