# -*- coding: cp1252 -*-
import os, time, sys
import disk, utils, FAT, exFAT, partutils, vhdutils

DEBUG = 0
from debug import log



def openpart(path, mode='rb', partition=0):
    "Open a partition returning a partition handle"
    if os.name =='nt' and len(path)==2 and path[1] == ':':
        path = '\\\\.\\'+path
    if path.lower().endswith('.vhd'): # VHD image
        d = vhdutils.Image(path, mode)
    else:
        d = disk.disk(path, mode)
    d.seek(0)
    mbr = partutils.MBR(d.read(512), disksize=d.size)

    if DEBUG&2: log("Opened MBR: %s", mbr)

    if mbr.wBootSignature != 0xAA55:
        print("Invalid Master Boot Record. Aborted.")
        sys.exit(1)

    part = None

    if mbr.partitions[0].bType == 0xEE: # GPT
        d.seek(512)
        gpt = partutils.GPT(d.read(512), 512)
        if DEBUG&2: log("Opened GPT Header: %s", gpt)
        d.seek(gpt.u64PartitionEntryLBA*512)
        blk = d.read(gpt.dwNumberOfPartitionEntries * gpt.dwNumberOfPartitionEntries)
        gpt.parse(blk)
        blocks = gpt.partitions[partition].u64EndingLBA - gpt.partitions[partition].u64StartingLBA + 1
        if DEBUG&2: log("Opening Partition #%d: %s", partition, gpt.partitions[partition])
        part = disk.partition(d, gpt.partitions[partition].u64StartingLBA*512, blocks*512)
        part.seek(0)
        part.mbr = mbr
        part.gpt = gpt
    else:
        index=0
        if partition > 0:
            index = 1 # opens Extended Partition
        part = disk.partition(d, mbr.partitions[index].offset(), mbr.partitions[index].size())
        if DEBUG&2: log("Opened %s partition @%016x (LBA %016x) %s", ('Primary', 'Extended')[index], mbr.partitions[index].chsoffset(), mbr.partitions[index].lbaoffset(), partutils.raw2chs(mbr.partitions[index].sFirstSectorCHS))
        if partition > 0:
            wanted = 1
            extpart = part
            while wanted <=partition:
                bs = extpart.read(512)
                ebr = partutils.MBR(bs, disksize=d.size) # reads Extended Boot Record
                if DEBUG&2: log("Opened EBR: %s", ebr)
                if ebr.wBootSignature != 0xAA55:
                    print("Invalid Extended Boot Record. Aborted.")
                    sys.exit(1)
                if DEBUG&2: log("Got partition @%016x (@%016x rel.) %s", ebr.partitions[0].chsoffset(), ebr.partitions[0].lbaoffset(), partutils.raw2chs(ebr.partitions[0].sFirstSectorCHS))
                if DEBUG&2: log("Next logical partition @%016x (@%016x rel.) %s", ebr.partitions[1].chsoffset(), ebr.partitions[1].lbaoffset(), partutils.raw2chs(ebr.partitions[1].sFirstSectorCHS))
                #~ print wanted, partition
                if wanted == partition:
                    if DEBUG&2: log("Opening Logical Partition #%d @%016x %s", partition, ebr.partitions[0].offset(), partutils.raw2chs(ebr.partitions[0].sFirstSectorCHS))
                    part = disk.partition(d, ebr.partitions[0].offset(), ebr.partitions[0].size())
                    part.seek(0)
                    break
                if ebr.partitions[1].dwFirstSectorLBA and ebr.partitions[1].dwTotalSectors:
                    if DEBUG&2: log("Scanning next Logical Partition @%016x %s size %.02f MiB", ebr.partitions[1].offset(), partutils.raw2chs(ebr.partitions[1].sFirstSectorCHS), ebr.partitions[1].size()//(1<<20))
                    extpart = disk.partition(d, ebr.partitions[1].offset(), ebr.partitions[1].size())
                else:
                    break
                wanted+=1
        part.mbr = mbr
    def open(x): return openvolume(x)
    disk.partition.open = open # adds an open member to partition object
    return part



def openvolume(part):
    "Opens a filesystem given a Python partition object, returning the root directory Dirtable"
    part.seek(0)
    bs = part.read(512)
    
    fstyp = utils.FSguess(FAT.boot_fat16(bs)) # warning: if we call this a second time on the same Win32 disk, handle is unique and seek set already!
    if DEBUG&2: log("FSguess guessed FS type: %s", fstyp)

    if fstyp in ('FAT12', 'FAT16'):
        boot = FAT.boot_fat16(bs, stream=part)
    elif fstyp == 'FAT32':
        boot = FAT.boot_fat32(bs, stream=part)
    elif fstyp == 'EXFAT':
        boot = exFAT.boot_exfat(bs, stream=part)
    elif fstyp == 'NTFS':
        print("NTFS file system not supported. Aborted.")
        sys.exit(1)
    else:
        print("File system not recognized. Aborted.")
        sys.exit(1)

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

    return root



def openimage(path, mode='rb', obj_type='fs'):
    "Opens a disk or image and returns it as root directory (if obj_type is 'fs') or as raw blocks"
    if os.name =='nt' and len(path)==2 and path[1] == ':':
        path = '\\\\.\\'+path
    if path.lower().endswith('.vhd'): # VHD image
        d = vhdutils.Image(path, mode)
    else:
        d = disk.disk(path, mode)
    d.seek(0)
    part = disk.partition(d, 0, d.size)
    part.seek(0)
    part.mbr = None
    if obj_type == 'fs':
        return openvolume(part)
    else:
        return part



def copy_tree_in(base, dest, callback=None, attributes=None, chunk_size=1<<20):
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
            # Create target, preallocating all clusters
            dst = target_dir.create(file, (st.st_size+dest.boot.cluster-1)//dest.boot.cluster)
            if callback: callback(src)
            while 1:
                s = fp.read(chunk_size)
                if not s: break
                dst.write(s)

            if attributes: # bit mask: 1=preserve creation time, 2=last modification, 3=last access
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
            dst.close()



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
            if callback: callback(src)
            while True:
                s = fpi.read(chunk_size)
                if not s: break
                fpo.write(s)
            fpo.close()
            fpi.close() # If closing is deferred to atexit, massive KeyError exceptions are generated by disk.py in cache_flush: investigate!

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
    