# -*- coding: cp1252 -*-

#
# High-level functions to open disks, partitions, volumes and play with them easily.
#
#

import os, time, sys, re, glob, fnmatch
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from io import BytesIO
from FATtools import disk, utils, FAT, exFAT
from FATtools.partutils import MBR, GPT
from FATtools import utils, vhdutils, vhdxutils, vdiutils, vmdkutils
from FATtools.debug import log
from .NTFS import ntfs_emu_dirtable

def vopen(path, mode='rb', what='auto'):
    """Opens a disk, partition or volume according to 'what' parameter: 'auto' 
    selects the volume in the first partition or disk; 'disk' selects the raw disk;
    'partitionN' tries to open partition number N; 'volume' tries to open a file
    system. 'path' can be: 1) a file or device path; 2) a FATtools disk or virtual
    disk object; 3) a BytesIO object if mode is 'ramdisk'."""
    if DEBUG&2: log("vopen in '%s' mode", what)
    PHYS_SECTOR = 512 # defualt physical sector size
    if type(path) in (disk.disk, vhdutils.Image, vhdxutils.Image, vdiutils.Image, vmdkutils.Image, BytesIO):
        if isinstance(path, BytesIO):
            # Opens a Ram Disk with a BytesIO object
            d = disk.disk(path, 'ramdisk')
        else:
            if path.mode == mode:
                d = path
            else:
                d = type(path)(path.name, mode) # reopens with right mode
    else:
        # Tries to open a raw disk or disk image
        if os.name =='nt' and len(path)==2 and path[1] == ':':
            path = '\\\\.\\'+path
        if path.lower().endswith('.vhd'): # VHD image
            d = vhdutils.Image(path, mode)
        elif path.lower().endswith('.vhdx'): # VHDX image
            d = vhdxutils.Image(path, mode)
            PHYS_SECTOR = d.metadata.physical_sector_size
        elif path.lower().endswith('.vdi'): # VDI image
            d = vdiutils.Image(path, mode)
        elif path.lower().endswith('.vmdk'): # VMDK image
            d = vmdkutils.Image(path, mode)
        else:
            d = disk.disk(path, mode) # disk or disk image
        if DEBUG&2: log("Opened disk type '%s', size %Xh (%Xh sectors) ", d, d.size, d.size//PHYS_SECTOR)
    d.seek(0)
    if what == 'disk':
        return d
    # Tries to access a partition
    mbr = MBR(d.read(PHYS_SECTOR), disksize=d.size, sector=PHYS_SECTOR)
    if DEBUG&2: log("Opened MBR: %s", mbr)
    valid_mbr=1
    n = mbr.partitions[0].size()
    if mbr.wBootSignature != 0xAA55:
        if DEBUG&2: log("Invalid Master Boot Record")
        valid_mbr=0
    elif mbr.partitions[0].bType != 0xEE and (not n or n > d.size):
        if DEBUG&2: log("Invalid Primary partition size in MBR")
        valid_mbr=0
    elif mbr.partitions[0].bStatus not in (0, 0x80):
        if DEBUG&2: log("Invalid Primary partition status in MBR")
        valid_mbr=0
    if not valid_mbr:
        if DEBUG&2: log("Invalid Master Boot Record")
        if what in ('auto', 'volume'):
            if DEBUG&2: log("Trying to open a File system (Volume) in plain disk")
            d.seek(0)
            d.mbr = None
            v = openvolume(d)
            if v != 'EINV':
                d.volume = v # link volume and device/partition each other
                v.parent = d
                return v
            if DEBUG&2: log("No known file system found, returning RAW disk")
            d.seek(0)
            return d
        else: # partition mode
            vclose(d)
            return 'EINVMBR'
    # Tries to open MBR or GPT partition
    if DEBUG&2: log("Ok, valid MBR")
    partition=0 # Windows 11 makes a MSR reserved part first
    if what.startswith('partition'):
        partition = int(re.match('partition(\\d+)', what).group(1))
    if DEBUG&2: log("Trying to open partition #%d", partition)
    part = None
    if mbr.partitions[0].bType == 0xEE: # GPT
        d.seek(PHYS_SECTOR)
        gpt = GPT(d.read(PHYS_SECTOR), PHYS_SECTOR)
        if DEBUG&2: log("Opened GPT Header: %s", gpt)
        d.seek(gpt.u64PartitionEntryLBA*PHYS_SECTOR)
        blk = d.read(gpt.dwNumberOfPartitionEntries * gpt.dwNumberOfPartitionEntries)
        gpt.parse(blk)
        # search for the 1st Windows BDP part
        partition=-1
        for part in gpt.partitions:
            partition+=1
            if part.sPartitionTypeGUID == b'\xa2\xa0\xd0\xeb\xe5\xb93D\x87\xc0h\xb6\xb7&\x99\xc7': break
        blocks = gpt.partitions[partition].u64EndingLBA - gpt.partitions[partition].u64StartingLBA + 1
        if DEBUG&2: log("Opening Partition #%d: %s", partition, gpt.partitions[partition])
        part = disk.partition(d, gpt.partitions[partition].u64StartingLBA*PHYS_SECTOR, blocks*PHYS_SECTOR)
        part.seek(0)
        part.mbr = mbr
        part.gpt = gpt
        # TODO: protect against invalid partition entries!
    else:
        index=0
        if partition > 0:
            index = 1 # opens Extended Partition
        if DEBUG&2: log("Opening partition @%016x (size %016x)", mbr.partitions[index].offset(), mbr.partitions[index].size())
        if DEBUG&2: log("Last sector CHS: %d-%d-%d", *utils.raw2chs(mbr.partitions[index].sLastSectorCHS))
        part = disk.partition(d, mbr.partitions[index].offset(), mbr.partitions[index].size())
        if DEBUG&2: log("Opened %s partition @%016xh (LBA %016xh) %s", ('Primary', 'Extended')[index], mbr.partitions[index].chsoffset(), mbr.partitions[index].lbaoffset(), utils.raw2chs(mbr.partitions[index].sFirstSectorCHS))
        if partition > 0:
            wanted = 1
            extpart = part
            while wanted <=partition:
                bs = extpart.read(PHYS_SECTOR)
                ebr = MBR(bs, disksize=d.size, sector=PHYS_SECTOR) # reads Extended Boot Record
                if DEBUG&2: log("Opened EBR: %s", ebr)
                if ebr.wBootSignature != 0xAA55:
                    if DEBUG&2: log("Invalid Extended Boot Record")
                    if what == 'auto':
                        return d
                    else:
                        vclose(extpart)
                        return 'EINV'
                if DEBUG&2: log("Got partition @%016xh (@%016xh rel.) %s", ebr.partitions[0].chsoffset(), ebr.partitions[0].lbaoffset(), utils.raw2chs(ebr.partitions[0].sFirstSectorCHS))
                if DEBUG&2: log("Next logical partition @%016xh (@%016xh rel.) %s", ebr.partitions[1].chsoffset(), ebr.partitions[1].lbaoffset(), utils.raw2chs(ebr.partitions[1].sFirstSectorCHS))
                if wanted == partition:
                    if DEBUG&2: log("Opening Logical Partition #%d @%016xh %s", partition, ebr.partitions[0].offset(), utils.raw2chs(ebr.partitions[0].sFirstSectorCHS))
                    part = disk.partition(d, ebr.partitions[0].offset(), ebr.partitions[0].size())
                    part.seek(0)
                    break
                if ebr.partitions[1].dwFirstSectorLBA and ebr.partitions[1].dwTotalSectors:
                    if DEBUG&2: log("Scanning next Logical Partition @%016xh %s size %.02f MiB", ebr.partitions[1].offset(), utils.raw2chs(ebr.partitions[1].sFirstSectorCHS), ebr.partitions[1].size()//(1<<20))
                    extpart = disk.partition(d, ebr.partitions[1].offset(), ebr.partitions[1].size())
                else:
                    break
                wanted+=1
        part.mbr = mbr
    def open(x): return openvolume(x)
    disk.partition.open = open # adds an open member to partition object
    if what in ('volume', 'auto'):
        v = part.open()
        part.volume = v # remember volume opened
        if DEBUG&2: log("Returning opened Volume %s", v)
        return v
    else:
        if DEBUG&2: log("Returning partition object")
        return part

    
# BUG: it assumes one partition per disk, real life might vary!
def vclose(obj):
    "Closes intelligently an object returned by vopen (=closes all child partitions/volumes, too)"
    if type(obj) in (disk.disk, vhdutils.Image, vhdxutils.Image, vdiutils.Image, vmdkutils.Image):
        if hasattr(obj, 'volume') and obj.volume:
            if DEBUG&2: log("Closing child volume %s", obj.volume)
            obj.volume.close()
        if DEBUG&2: log("Closing %s", obj)
        obj.close()
    elif type(obj) == disk.partition:
        if hasattr(obj, 'volume') and obj.volume:
            if DEBUG&2: log("Closing child volume %s", obj.volume)
            obj.volume.close()
        if DEBUG&2: log("Closing %s", obj)
        obj.close()
        if DEBUG&2: log("Closing %s", obj.disk)
        obj.disk.close()
    elif type(obj) in (FAT.Dirtable, exFAT.Dirtable):
        if DEBUG&2: log("Closing volume %s", obj)
        obj.close()
        if obj.parent:
            if DEBUG&2: log("Closing %s", obj.parent)
            obj.parent.close()
    else:
        raise BaseException('vclose cannot close such an object: %s' % obj)



def openvolume(part):
    """Opens a filesystem given a Python disk or partition object, guesses
    the file system and returns the root directory Dirtable"""
    part.seek(0)
    bs = part.read(512)
    
    if DEBUG&2: log("Boot sector:\n%s", FAT.boot_fat16(bs))
    fstyp = utils.FSguess(FAT.boot_fat16(bs)) # warning: if we call this a second time on the same Win32 disk, handle is unique and seek set already!
    if DEBUG&2: log("FSguess guessed FS type: %s", fstyp)

    if fstyp in ('FAT12', 'FAT16'):
        boot = FAT.boot_fat16(bs, stream=part)
    elif fstyp == 'FAT32':
        boot = FAT.boot_fat32(bs, stream=part)
    elif fstyp == 'EXFAT':
        boot = exFAT.boot_exfat(bs, stream=part)
    elif fstyp == 'NTFS':
        return ntfs_emu_dirtable(part)
    else:
        return 'EINV'

    fat = FAT.FAT(part, boot.fatoffs, boot.clusters(), bitsize={'FAT12':12,'FAT16':16,'FAT32':32,'EXFAT':32}[fstyp], exfat=(fstyp=='EXFAT'))

    if DEBUG&2:
        log("Inited BOOT object: %s", boot)
        log("Inited FAT object: %s", fat)

    if fstyp == 'EXFAT':
        mod = exFAT
    else:
        mod = FAT

    root = mod.Dirtable(boot, fat, boot.dwRootCluster)
    root.MBR = part.mbr

    if fstyp == 'EXFAT':
        for e in root.iterator():
            if e.type == 1: # Find & open Bitmap
                boot.bitmap = exFAT.Bitmap(boot, fat, e.dwStartCluster, e.u64DataLength)
                break

    root.parent = part # remember parent device/partition
    
    return root



def _preserve_attributes_in(attributes, st, target_dir, dst):
    if attributes: # bit mask: 0=preserve creation time, 1=last modification, 2=last access
        # 5=zero last modification & access times (MS-DOS <7)
        if attributes & 1:
            tm = time.localtime(st.st_ctime)
            if target_dir.fat.exfat:
                dw, ms = exFAT.exFATDirentry.MakeDosDateTimeEx((tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec))
                dst.Entry.dwCTime = dw
                dst.chmsCTime = ms
            else:
                dst.Entry.wCDate = FAT.FATDirentry.MakeDosDate((tm.tm_year, tm.tm_mon, tm.tm_mday))
                dst.Entry.wCTime = FAT.FATDirentry.MakeDosTime((tm.tm_hour, tm.tm_min, tm.tm_sec))

        if attributes & 2:
            tm = time.localtime(st.st_mtime)
            if target_dir.fat.exfat:
                dw, ms = exFAT.exFATDirentry.MakeDosDateTimeEx((tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec))
                dst.Entry.dwMTime = dw
                dst.chmsCTime = ms
            else:
                dst.Entry.wMDate = FAT.FATDirentry.MakeDosDate((tm.tm_year, tm.tm_mon, tm.tm_mday))
                dst.Entry.wMTime = FAT.FATDirentry.MakeDosTime((tm.tm_hour, tm.tm_min, tm.tm_sec))

        if attributes & 4:
            tm = time.localtime(st.st_atime)
            if target_dir.fat.exfat:
                dw, ms = exFAT.exFATDirentry.MakeDosDateTimeEx((tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec))
                dst.Entry.dwATime = dw
                dst.chmsCTime = ms
            else:
                dst.Entry.wADate = FAT.FATDirentry.MakeDosDate((tm.tm_year, tm.tm_mon, tm.tm_mday))
                #~ dst.Entry.wATime = FAT.FATDirentry.MakeDosTime((tm.tm_hour, tm.tm_min, tm.tm_sec)) # FAT does not support this!

        if attributes & 32:
            if not target_dir.fat.exfat:
                dst.Entry.wADate = 0
                dst.Entry.wCDate = 0
                dst.Entry.wCTime = 0

def _preserve_attributes_out(attributes, base, fpi, dst):
            if attributes: # bit mask: 1=preserve creation time, 2=last modification, 3=last access
                if attributes & 1:
                    pass # utime does not support this
                if attributes & 2:
                    if base.fat.exfat:
                        wTime = fpi.Entry.dwMTime & 0xFFFF
                        wDate = fpi.Entry.dwMTime >> 16
                        MTime = FAT.FATDirentry.ParseDosDate(wDate) + FAT.FATDirentry.ParseDosTime(wTime) + (0,0,0)
                        os.utime(dst, (0, time.mktime(MTime)))
                    else:
                        MTime = fpi.Entry.ParseDosDate(fpi.Entry.wMDate) + fpi.Entry.ParseDosTime(fpi.Entry.wMTime) + (0,0,0)
                        os.utime(dst, (0, time.mktime(MTime)))
                if attributes & 4:
                    if base.fat.exfat:
                        wTime = fpi.Entry.dwATime & 0xFFFF
                        wDate = fpi.Entry.dwATime >> 16
                        ATime = FAT.FATDirentry.ParseDosDate(wDate) + FAT.FATDirentry.ParseDosTime(wTime) + (0,0,0)
                        os.utime(dst, (0, time.mktime(ATime)))
                    else:
                        ATime = fpi.Entry.ParseDosDate(fpi.Entry.wADate) + (0,0,0,0,0,0)
                        os.utime(dst, (time.mktime(ATime), 0))



def copy_in(src_list, dest, callback=None, attributes=None, chunk_size=1<<20):
    """Copies files and directories in 'src_list' to virtual 'dest' directory
    table, 'chunk_size' bytes at a time, calling callback function if provided
    and preserving date and times if desired."""
    for it in src_list:
        # If item is a wildcard expression, expand it and push to sources list
        g = glob.glob(it)
        if len(g) > 1:
            src_list += g
            continue
        if os.path.isdir(it):
            subdir = dest.mkdir(os.path.basename(it)) # we want only file/dir name in target!
            copy_tree_in(it, subdir, callback, attributes, chunk_size)
        elif os.path.isfile(it):
            target_dir=dest
            st = os.stat(it)
            fp = open(it, 'rb')
            # Create target, preallocating all clusters
            it = os.path.basename(it) # we want only file/dir name in target!
            is_single_file = str(type(dest)).find('Handle') > -1
            if is_single_file:
                dst = dest
            else:
                dst = dest.create(it, (st.st_size+dest.boot.cluster-1)//dest.boot.cluster)
            if callback: callback(it)
            while 1:
                s = fp.read(chunk_size)
                if not s: break
                dst.write(s)
            target_dir=dest
            if not is_single_file:
                _preserve_attributes_in(attributes, st, target_dir, dst)
            fp.close()
            dst.close()
        else:
            pass

def copy_tree_in(base, dest, callback=None, attributes=None, chunk_size=1<<20, uppercase=0):
    """Copy recursively files and directories under real 'base' path into
    virtual 'dest' directory table, 'chunk_size' bytes at a time, calling callback function if provided
    and preserving date and times if desired."""

    for root, folders, files in os.walk(base):
        relative_dir = root[len(base)+1:]
        # Split subdirs in target path
        subdirs = []
        while 1:
            pro, epi = os.path.split(relative_dir)
            if pro == relative_dir: break
            relative_dir = pro
            subdirs += [epi]
        subdirs.reverse()

        # Recursively open path to dest, creating directories if necessary
        target_dir = dest
        for subdir in subdirs:
            target_dir = target_dir.mkdir(subdir)

        # Finally, copy files
        for file in files:
            src = os.path.join(root, file)
            fp = open(src, 'rb')
            st = os.stat(src)
            if uppercase: file = file.upper() # force name to upper case
            # Create target, preallocating all clusters
            dst = target_dir.create(file, (st.st_size+dest.boot.cluster-1)//dest.boot.cluster)
            if callback: callback(src[len(base)+1:]) # strip base path
            while 1:
                s = fp.read(chunk_size)
                if not s: break
                dst.write(s)

            _preserve_attributes_in(attributes, st, target_dir, dst)
            dst.close()
            fp.close()



def copy_out(base, src_list, dest, callback=None, attributes=None, chunk_size=1<<20):
    """Copies files and directories in virtual 'src_list' to real 'dest' directory
    'chunk_size' bytes at a time, calling callback function if provided
    and preserving date and times if desired."""
    for it in src_list:
        # wildcard? expand src_list with matching items in 'base'
        # AND, eventually, its sub-path
        if '*' in it or '?' in it:
            subp = os.path.dirname(it)
            if subp:
                base = base.opendir(subp)
                if not base:
                    raise FileNotFoundError("Source directory '%s' does not exist!"%subp)
            it = os.path.basename(it)
            if DEBUG&2: log("copy_out: searching for '%s' in '%s'", it, base.path)
            for name in base.listdir():
                if fnmatch.fnmatch(name, it):
                    src_list += [name]
            continue
        if DEBUG&2: log("copy_out: probing '%s' as file", it)
        fpi = base.open(it)
        if not fpi.IsValid:
            # if existent but invalid, it is a dir
            if DEBUG&2: log("copy_out: probing '%s' as directory", it)
            fpi = base.opendir(it)
            if not fpi:
                if DEBUG&2: log("copy_out: '%s' does not exist", it)
                if callback: callback('"%s" does not exist!'%it)
                continue
            it = os.path.join(dest, os.path.basename(it)) # we want only file/dir name in target!
            try:
                os.mkdir(it)
                if DEBUG&2: log("copy_out: mkdir '%s'", it)
            except FileExistsError:
                pass
            if DEBUG&2: log("copy_out: target is '%s'", it)
            copy_tree_out(fpi, it, callback, attributes, chunk_size)
            continue
        it = os.path.basename(it) # we want only file/dir name in target!
        if os.path.isdir(dest):
            dst = os.path.join(dest, it)
        else:
            if len(src_list) == 1:
                dst = dest
            else:
                raise FileNotFoundError("Can't copy in '%s', target is not a directory!"%dest)
        fpo = open(dst, 'wb')
        if DEBUG&2: log("copy_out: target is '%s'", dst)
        if callback: callback(dst)
        while True:
            s = fpi.read(chunk_size)
            if not s: break
            fpo.write(s)
        fpo.close()
        fpi.close()
        _preserve_attributes_out(attributes, base, fpi, dst)


def copy_tree_out(base, dest, callback=None, attributes=None, chunk_size=1<<20):
    """Copy recursively files and directories under virtual 'base' Dirtable into
    real 'dest' directory, 'chunk_size' bytes at a time, calling callback function if provided
    and preserving date and times if desired."""
    for root, folders, files in base.walk():
        for file in files:
            src = os.path.join(root, file)
            dst = os.path.join(dest, src[len(base.path)+1:])
            if base.path == os.path.dirname(src):
                fpi = base.open(file)
            else:
                fpi = base.opendir(os.path.dirname(src)[len(base.path)+1:]).open(file)
            assert fpi.IsValid != False
            try:
                os.makedirs(os.path.dirname(dst))
            except:
                pass
            fpo = open(dst, 'wb')
            if callback: callback(dst) # strip base path
            while True:
                s = fpi.read(chunk_size)
                if not s: break
                fpo.write(s)
            fpo.close()
            fpi.close() # If closing is deferred to atexit, massive KeyError exceptions are generated by disk.py in cache_flush: investigate!
            _preserve_attributes_out(attributes, base, fpi, dst)
