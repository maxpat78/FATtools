import utils, struct, disk, os, sys, pprint, optparse
from FAT import *
from mkexfat import exfat_mkfs

""" FROM https://support.microsoft.com/en-us/kb/140365

Default cluster sizes for FAT32
The following table describes the default cluster sizes for FAT32.
Volume size	    Windows NT 3.51	    Windows NT 4.0	    Windows 2000+
7 MB-16MB 	    Not supported 	    Not supported	    Not supported
16 MB-32 MB 	512 bytes	        512 bytes	        Not supported
32 MB-64 MB 	512 bytes	        512 bytes	        512 bytes
64 MB-128 MB 	1 KB	            1 KB	            1 KB
128 MB-256 MB	2 KB	            2 KB	            2 KB
256 MB-8GB	    4 KB	            4 KB	            4 KB
8GB-16GB 	    8 KB	            8 KB	            8 KB
16GB-32GB 	    16 KB	            16 KB	            16 KB
32GB-2TB 	    32 KB	            Not supported 	    Not supported
> 2TB	        Not supported 	    Not supported	    Not supported """

"""
Default cluster sizes for FAT16
The following table describes the default cluster sizes for FAT16.
Volume size 	Windows NT 3.51	    Windows NT 4.0	    Windows 2000+
7 MB-8 MB 	    Not supported 	    Not supported	    Not supported
8 MB-32 MB 	    512 bytes	        512 bytes	        512 bytes
32 MB-64 MB 	1 KB             	1 KB 	            1 KB
64 MB-128 MB 	2 KB             	2 KB            	2 KB
128 MB-256 MB	4 KB            	4 KB            	4 KB
256 MB-512 MB	8 KB            	8 KB            	8 KB
512 MB-1 GB 	16 KB            	16 KB            	16 KB
1 GB-2 GB 	    32 KB           	32 KB           	32 KB
2 GB-4 GB 	    64 KB	            64 KB           	64 KB
4 GB-8 GB 	    Not supported 	    128 KB*         	Not supported
8 GB-16 GB 	    Not supported 	    256 KB*         	Not supported
> 16 GB	        Not supported 	    Not supported	    Not supported """

nodos_asm_5Ah = b'\xB8\xC0\x07\x8E\xD8\xBE\x73\x00\xAC\x08\xC0\x74\x09\xB4\x0E\xBB\x07\x00\xCD\x10\xEB\xF2\xF4\xEB\xFD\x4E\x4F\x20\x44\x4F\x53\x00'

"""
TRACKS     SPT     HEADS    MEDIA
80         36      2        F0      (3 1/2" DS/HD 2.88 MB)
80         18      2        F0      (3 1/2" DS/HD 1.44 MB, 2880x512 bytes sectors)
80          9      2        F9      (3 1/2" DS/DD 720 KB, 2 sectors/cluster)
80         15      2        F9      (5 25"  1.2 MB)
40          9      2        FD      (5 25"  360 KB)
40          8      2        FF      (5 25"  320 KB)
40          9      1        FC      (5 25"  180 KB)
40          8      1        FE      (5 25"  160 KB)"""

def fat12_mkfs(stream, size, sector=512, params={}):
    "Make a FAT12 File System on stream. Returns 0 for success."
    sectors = size//sector

    if sectors < 16 or sectors > 0xFFFFFFFF:
        print("Fatal: can't apply file system to a %d sectors disk!" % sectors)
        return 1

    # NOTE: Windows 10 CHKDSK assumes a 2847 clustered floppy even if fat12_mkfs formated a smaller one!!! 

    # Minimum is 1 (Boot)
    if 'reserved_size' in params:
        reserved_size = params['reserved_size']*sector
    else:
        reserved_size = 1*sector

    if 'fat_copies' in params:
        fat_copies = params['fat_copies']
    else:
        fat_copies = 2 # default: best setting

    if 'root_entries' in params:
        root_entries = params['root_entries']
    else:
        root_entries = 224

    reserved_size += root_entries*32 # in FAT12/16 this space resides outside the cluster area

    allowed = {} # {cluster_size : fsinfo}

    for i in range(9, 17): # cluster sizes 0.5K...64K
        fsinfo = {}
        cluster_size = (2**i)
        clusters = (size - reserved_size) // cluster_size
        fat_size = ((12*(clusters+2))//8+sector-1)//sector * sector # 12-bit slot
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 1
            fat_size = ((12*(clusters+2))//8+sector-1)//sector * sector # 12-bit slot
            required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        if clusters > 4085:
            continue
        fsinfo['required_size'] = required_size # space occupied by FS
        fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
        fsinfo['cluster_size'] = cluster_size
        fsinfo['clusters'] = clusters
        fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
        fsinfo['root_entries'] = root_entries
        allowed[cluster_size] = fsinfo

    if not allowed:
        if clusters > 4085: # switch to FAT16
            print("Too many clusters to apply FAT12: trying FAT16...")
            return fat16_mkfs(stream, size, sector, params)
        print("ERROR: can't apply any FAT12/16/32 format!")
        return 1

    #~ print "* MKFS FAT12 INFO: allowed combinations for cluster size:"
    #~ pprint.pprint(allowed)

    fsinfo = None

    if 'wanted_cluster' in params:
        if params['wanted_cluster'] in allowed:
            fsinfo = allowed[params['wanted_cluster']]
        else:
            print("Specified cluster size of %d is not allowed!" % params['wanted_cluster'])
            return -1
    else:
        # MS-inspired selection
        if size <= 2<<20:
            fsinfo = allowed[512] # < 2M
        elif 2<<20 < size <= 4<<20:
            fsinfo = allowed[1024]
        elif 4<<20 < size <= 8<<20:
            fsinfo = allowed[2048]
        elif 8<<20 < size <= 16<<20:
            fsinfo = allowed[4096]
        elif 16<<20 < size <= 32<<20:
            fsinfo = allowed[8192] # 16M-32M
        elif 32<<20 < size <= 64<<20:
            fsinfo = allowed[16384]
        elif 64<<20 < size <= 128<<20:
            fsinfo = allowed[32768]
        else:
            fsinfo = allowed[65536]

    boot = boot_fat16()
    boot.chJumpInstruction = b'\xEB\x58\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x5A:0x5A+len(nodos_asm_5Ah)] = nodos_asm_5Ah # insert assembled boot code
    boot.chOemID = b'%-8s' % b'NODOS'
    boot.wBytesPerSector = sector
    boot.wSectorsCount = 1
    boot.dwHiddenSectors = 0
    boot.uchSectorsPerCluster = fsinfo['cluster_size']//sector
    boot.uchFATCopies = fat_copies
    boot.wMaxRootEntries = fsinfo['root_entries'] # not used in FAT32 (fixed root)
    boot.uchMediaDescriptor = 0xF0 # floppy
    if sectors < 65536: # Is it right?
        boot.wTotalSectors = sectors
    else:
        boot.dwTotalLogicalSectors = sectors
    boot.wSectorsPerFAT = fsinfo['fat_size']//sector
    boot.dwVolumeID = FATDirentry.GetDosDateTime(1)
    boot.sVolumeLabel = b'%-11s' % b'NO NAME'
    boot.sFSType = b'%-8s' % b'FAT12'
    boot.chPhysDriveNumber = 0
    boot.uchSignature = 0x29
    boot.wBootSignature = 0xAA55
    boot.wSectorsPerTrack = 18
    boot.wHeads = 2

    boot.pack()
    #~ print boot
    #~ print 'FAT, root, cluster #2 offsets', hex(boot.fat()), hex(boot.fat(1)), hex(boot.root()), hex(boot.dataoffs)

    stream.seek(0)
    # Write boot sector
    stream.write(boot.pack())
    # Blank FAT1&2 area
    stream.seek(boot.fat())
    blank = bytearray(boot.wBytesPerSector)
    for i in range(boot.wSectorsPerFAT*2):
        stream.write(blank)
    # Initializes FAT1...
    clus_0_2 = b'\xF0\xFF\xFF'
    stream.seek(boot.wSectorsCount*boot.wBytesPerSector)
    stream.write(clus_0_2)
    # ...and FAT2
    if boot.uchFATCopies == 2:
        stream.seek(boot.fat(1))
        stream.write(clus_0_2)

    # Blank root at fixed offset
    stream.seek(boot.root())
    stream.write(bytearray(boot.wMaxRootEntries*32))

    stream.flush() # force committing to disk before reopening, or could be not useable!

    sizes = {0:'B', 10:'KiB',20:'MiB',30:'GiB',40:'TiB',50:'EiB'}
    k = 0
    for k in sorted(sizes):
        if (fsinfo['required_size'] // (1<<k)) < 1024: break

    free_clusters = fsinfo['clusters'] # root is outside clusters heap
    print("Successfully applied FAT12 to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    print("\nFAT #1 @0x%X, Data Region @0x%X, Root @0x%X" % (boot.fatoffs, boot.cl2offset(2), boot.root()))

    return 0



def fat16_mkfs(stream, size, sector=512, params={}):
    "Make a FAT16 File System on stream. Returns 0 for success."
    sectors = size//sector

    if sectors < 16 or sectors > 0xFFFFFFFF:
        print("Fatal: can't apply file system to a %d sectors disk!" % sectors)
        return 1

    # Minimum is 1 (Boot)
    if 'reserved_size' in params:
        reserved_size = params['reserved_size']*sector
    else:
        reserved_size = 8*sector # fixed or variable?

    if 'fat_copies' in params:
        fat_copies = params['fat_copies']
    else:
        fat_copies = 2 # default: best setting

    if 'root_entries' in params:
        root_entries = params['root_entries']
    else:
        root_entries = 512

    reserved_size += root_entries*32 # in FAT12/16 this space resides outside the cluster area

    allowed = {} # {cluster_size : fsinfo}

    for i in range(9, 17): # cluster sizes 0.5K...64K
        fsinfo = {}
        cluster_size = (2**i)
        clusters = (size - reserved_size) // cluster_size
        fat_size = (2*(clusters+2)+sector-1)//sector * sector
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 1
            fat_size = (2*(clusters+2)+sector-1)//sector * sector
            required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        # Should switch to FAT12?
        if clusters < 4086 or clusters > 65525: # MS imposed limits
            continue
        fsinfo['required_size'] = required_size # space occupied by FS
        fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
        fsinfo['cluster_size'] = cluster_size
        fsinfo['clusters'] = clusters
        fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
        fsinfo['root_entries'] = root_entries
        allowed[cluster_size] = fsinfo

    if not allowed:
        if clusters > 65525: # switch to FAT32
            print("Too many clusters to apply FAT16: trying FAT32...")
            return fat32_mkfs(stream, size, sector, params)
        if clusters < 4086: # switch to FAT12
            print("Too few clusters to apply FAT16: trying FAT12...")
            return fat12_mkfs(stream, size, sector, params)
        return 1

    #~ print "* MKFS FAT16 INFO: allowed combinations for cluster size:"
    #~ pprint.pprint(allowed)

    fsinfo = None

    if 'wanted_cluster' in params:
        if params['wanted_cluster'] in allowed:
            fsinfo = allowed[params['wanted_cluster']]
        else:
            if 'wanted_fs' in params and params['wanted_fs'] == 'fat16':
                print("Specified cluster size of %d is not allowed!" % params['wanted_cluster'])
                return -1
            else:
                print("Too many %d clusters to apply FAT16: trying FAT32..." % params['wanted_cluster'])
                return fat32_mkfs(stream, size, sector, params)
    else:
        # MS-inspired selection
        if size <= 32<<20:
            fsinfo = allowed[512] # < 32M
        elif 32<<20 < size <= 64<<20:
            fsinfo = allowed[1024]
        elif 64<<20 < size <= 128<<20:
            fsinfo = allowed[2048]
        elif 128<<20 < size <= 256<<20:
            fsinfo = allowed[4096]
        elif 256<<20 < size <= 512<<20:
            fsinfo = allowed[8192] # 256M-512M
        elif 512<<20 < size <= 1<<30:
            fsinfo = allowed[16384]
        elif 1<<30 < size <= 2<<30:
            fsinfo = allowed[32768]
        else:
            fsinfo = allowed[65536]

    boot = boot_fat16()
    boot.chJumpInstruction = b'\xEB\x58\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x5A:0x5A+len(nodos_asm_5Ah)] = nodos_asm_5Ah # insert assembled boot code
    boot.chOemID = b'%-8s' % b'NODOS'
    boot.wBytesPerSector = sector
    boot.wSectorsCount = (reserved_size - fsinfo['root_entries']*32)//sector
    boot.dwHiddenSectors = 1
    boot.uchSectorsPerCluster = fsinfo['cluster_size']//sector
    boot.uchFATCopies = fat_copies
    boot.wMaxRootEntries = fsinfo['root_entries'] # not used in FAT32 (fixed root)
    boot.uchMediaDescriptor = 0xF8
    if sectors < 65536: # Is it right?
        boot.wTotalSectors = sectors
    else:
        boot.dwTotalLogicalSectors = sectors
    boot.wSectorsPerFAT = fsinfo['fat_size']//sector
    boot.dwVolumeID = FATDirentry.GetDosDateTime(1)
    boot.sVolumeLabel = b'%-11s' % b'NO NAME'
    boot.sFSType = b'%-8s' % b'FAT16'
    boot.chPhysDriveNumber = 0x80
    boot.uchSignature = 0x29
    boot.wBootSignature = 0xAA55
    boot.wSectorsPerTrack = 63 # not used w/o disk geometry!
    boot.wHeads = 16 # not used

    boot.pack()
    #~ print boot
    #~ print 'FAT, root, cluster #2 offsets', hex(boot.fat()), hex(boot.fat(1)), hex(boot.root()), hex(boot.dataoffs)

    stream.seek(0)
    # Write boot sector
    stream.write(boot.pack())
    # Blank FAT1&2 area
    stream.seek(boot.fat())
    blank = bytearray(boot.wBytesPerSector)
    for i in range(boot.wSectorsPerFAT*2):
        stream.write(blank)
    # Initializes FAT1...
    clus_0_2 = b'\xF8\xFF\xFF\xFF'
    stream.seek(boot.wSectorsCount*boot.wBytesPerSector)
    stream.write(clus_0_2)
    # ...and FAT2
    if boot.uchFATCopies == 2:
        stream.seek(boot.fat(1))
        stream.write(clus_0_2)

    # Blank root at fixed offset
    stream.seek(boot.root())
    stream.write(bytearray(boot.wMaxRootEntries*32))

    stream.flush() # force committing to disk before reopening, or could be not useable!

    sizes = {0:'B', 10:'KiB',20:'MiB',30:'GiB',40:'TiB',50:'EiB'}
    k = 0
    for k in sorted(sizes):
        if (fsinfo['required_size'] // (1<<k)) < 1024: break

    free_clusters = fsinfo['clusters'] # root is outside clusters heap
    print("Successfully applied FAT16 to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    print("\nFAT #1 @0x%X, Data Region @0x%X, Root @0x%X" % (boot.fatoffs, boot.cl2offset(2), boot.root()))

    return 0



def fat32_mkfs(stream, size, sector=512, params={}):
    "Make a FAT32 File System on stream. Returns 0 for success, required additional clusters in case of failure."

#~ Windows CHKDSK wants at least 65526 clusters (512 bytes min).
#~ In fact, we can successfully apply FAT32 with less than 65526 clusters to
#~ a small drive (i.e., 32M with 1K/4K cluster) and Windows 10 will read and
#~ write it: but CHKDSK WON'T WORK!
#~ 4177918 (FAT32 limit where exFAT available)
#~ 2^16 - 11 = 65525 (FAT16)
#~ 2^12 - 11 = 4085 (FAT12)
#~ Also, we can successfully apply FAT16 to a 1.44M floppy (2855 clusters): but,
#~ again, we'll waste FAT space and, more important, CHKDSK won't recognize it!

    sectors = size//sector

    if sectors > 0xFFFFFFFF: # switch to exFAT where available
        print("Fatal: can't apply file system to a %d sectors disk!" % sectors)
        return -1

    # reserved_size auto adjusted according to unallocable space
    # Minimum is 2 (Boot & FSInfo)
    if 'reserved_size' in params:
        reserved_size = params['reserved_size']*sector
    else:
        reserved_size = 32*sector # fixed or variable?

    if 'fat_copies' in params:
        fat_copies = params['fat_copies']
    else:
        fat_copies = 2 # default: best setting

    allowed = {} # {cluster_size : fsinfo}

    for i in range(9, 17): # cluster sizes 0.5K...64K
        fsinfo = {}
        cluster_size = (2**i)
        clusters = (size - reserved_size) // cluster_size
        fat_size = (4*(clusters+2)+sector-1)//sector * sector
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 1
            fat_size = (4*(clusters+2)+sector-1)//sector * sector
            required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        if (clusters < 65526 and not params.get('fat32_allows_few_clusters')) or clusters > 0x0FFFFFF6: # MS imposed limits
            continue
        fsinfo['required_size'] = required_size # space occupied by FS
        fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
        fsinfo['cluster_size'] = cluster_size
        fsinfo['clusters'] = clusters
        fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
        allowed[cluster_size] = fsinfo

    if not allowed:
        if clusters < 65526:
            print("Too few clusters to apply FAT32: trying with FAT16...")
            return fat16_mkfs(stream, size, sector, params)
        if 'wanted_fs' in params and params['wanted_fs'] == 'fat32':
            print("Too many clusters to apply FAT32: aborting.")
            return -1
        else:
            print("Too many %d clusters to apply FAT32: trying exFAT..." % params['wanted_cluster'])
            return exfat_mkfs(stream, size, sector, params)

    #~ print "* MKFS FAT32 INFO: allowed combinations for cluster size:"
    #~ pprint.pprint(allowed)

    fsinfo = None

    if 'wanted_cluster' in params:
        if params['wanted_cluster'] > 65536:
            if 'wanted_fs' in params and params['wanted_fs'] == 'fat32':
                print("This version of FAT32 doesn't handle clusters >64K: aborting.")
                return -1
            else:
                print("This version of FAT32 doesn't handle clusters >64K: trying exFAT...")
                return exfat_mkfs(stream, size, sector, params)
        if params['wanted_cluster'] in allowed:
            fsinfo = allowed[params['wanted_cluster']]
        else:
            print("Specified cluster size of %d is not allowed for FAT32: retrying with FAT16..." % params['wanted_cluster'])
            return fat16_mkfs(stream, size, sector, params)
    else:
        # MS-inspired selection
        if size <= 64<<20:
            fsinfo = allowed[512] # < 64M
        elif 64<<20 < size <= 128<<20:
            fsinfo = allowed[1024]
        elif 128<<20 < size <= 256<<20:
            fsinfo = allowed[2048]
        elif 256<<20 < size <= 8<<30:
            fsinfo = allowed[4096] # 256M-8G
        elif 8<<30 < size <= 16<<30:
            fsinfo = allowed[8192]
        elif 16<<30 < size <= 32<<30:
            fsinfo = allowed[16384]
        elif 32<<30 < size <= 2048<<30:
            fsinfo = allowed[32768]
        # Windows 10 supports 128K and 256K, too!
        else:
            fsinfo = allowed[65536]

    boot = boot_fat32()
    boot.chJumpInstruction = b'\xEB\x58\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x5A:0x5A+len(nodos_asm_5Ah)] = nodos_asm_5Ah # insert assembled boot code
    boot.chOemID = b'%-8s' % b'NODOS'
    boot.wBytesPerSector = sector
    boot.wSectorsCount = reserved_size//sector
    boot.wHiddenSectors = 1
    boot.uchSectorsPerCluster = fsinfo['cluster_size']//sector
    boot.uchFATCopies = fat_copies
    boot.uchMediaDescriptor = 0xF8
    boot.dwTotalLogicalSectors = sectors
    boot.dwSectorsPerFAT = fsinfo['fat_size']//sector
    boot.dwRootCluster = 2
    boot.wFSISector = 1
    if 'backup_sectors' in params:
        boot.wBootCopySector = params['backup_sectors'] # typically 6
    else:
        #~ boot.wBootCopySector = 0
        boot.wBootCopySector = 6
    boot.dwVolumeID = FATDirentry.GetDosDateTime(1)
    boot.sVolumeLabel = b'%-11s' % b'NO NAME'
    boot.sFSType = b'%-8s' % b'FAT32'
    boot.chPhysDriveNumber = 0x80
    boot.chExtBootSignature = 0x29
    boot.wBootSignature = 0xAA55
    boot.wSectorsPerTrack = 63 # not used w/o disk geometry!
    boot.wHeads = 16 # not used

    fsi = fat32_fsinfo(offset=sector)
    fsi.sSignature1 = b'RRaA'
    fsi.sSignature2 = b'rrAa'
    fsi.dwFreeClusters = fsinfo['clusters'] - 1 # root is #2
    fsi.dwNextFreeCluster = 3 #2 is root
    fsi.wBootSignature = 0xAA55

    stream.seek(0)
    # Write boot & FSI sectors
    stream.write(boot.pack())
    stream.write(fsi.pack())
    if boot.wBootCopySector:
        # Write their backup copies
        stream.seek(boot.wBootCopySector*boot.wBytesPerSector)
        stream.write(boot.pack())
        stream.write(fsi.pack())
    # Blank FAT1&2 area
    stream.seek(boot.fat())
    blank = bytearray(boot.wBytesPerSector)
    for i in range(boot.dwSectorsPerFAT*2):
        stream.write(blank)
    # Initializes FAT1...
    clus_0_2 = b'\xF8\xFF\xFF\x0F\xFF\xFF\xFF\xFF\xF8\xFF\xFF\x0F'
    stream.seek(boot.wSectorsCount*boot.wBytesPerSector)
    stream.write(clus_0_2)
    # ...and FAT2
    if boot.uchFATCopies == 2:
        stream.seek(boot.fat(1))
        stream.write(clus_0_2)

    # Blank root at cluster #2
    stream.seek(boot.root())
    stream.write(bytearray(boot.cluster))

    #~ fat = FAT(stream, boot.fatoffs, boot.clusters(), bitsize=32)
    stream.flush() # force committing to disk before reopening, or could be not useable!

    sizes = {0:'B', 10:'KiB',20:'MiB',30:'GiB',40:'TiB',50:'EiB'}
    k = 0
    for k in sorted(sizes):
        if (fsinfo['required_size'] // (1<<k)) < 1024: break

    free_clusters = fsinfo['clusters'] - 1
    print("Successfully applied FAT32 to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    print("\nFAT #1 @0x%X, Data Region @0x%X, Root (cluster #%d) @0x%X" % (boot.fatoffs, boot.cl2offset(2), 2, boot.cl2offset(2)))

    return 0



if __name__ == '__main__':
    help_s = """
    %prog [options] <drive>
    """
    par = optparse.OptionParser(usage=help_s, version="%prog 1.0", description="Applies a FAT12/16/32 or exFAT File System to a disk device or file image.")
    par.add_option("-t", "--fstype", dest="fs_type", help="try to apply the specified File System between FAT12, FAT16, FAT32 or EXFAT. Default: based on medium size.", metavar="FSTYPE", type="string")
    par.add_option("-c", "--cluster", dest="cluster_size", help="force a specified cluster size between 512, 1024, 2048, 4096, 8192, 16384, 32768 (since MS-DOS) or 65536 bytes (Windows NT+) for FAT. exFAT permits up to 32M. Default: based on medium size. Accepts 'k' and 'm' postfix for Kibibytes and Mebibytes.", metavar="CLUSTER")
    opts, args = par.parse_args()

    if not args:
        print("mkfat error: you must specify a target volume to apply a FAT12/16/32 or exFAT file system!")
        par.print_help()
        sys.exit(1)

    if os.name == 'nt' and len(args[0])==2 and args[0][1]==':':
        disk_name = '\\\\.\\'+args[0]
    else:
        disk_name = args[0]
    dsk = disk.disk(disk_name, 'r+b')

    params = {}

    if opts.fs_type:
        t = opts.fs_type.lower()
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

    if opts.cluster_size:
        t = opts.cluster_size.lower()
        if t[-1] == 'k':
            params['wanted_cluster'] = int(opts.cluster_size[:-1])<<10
        elif t[-1] == 'm':
            params['wanted_cluster'] = int(opts.cluster_size[:-1])<<20
        else:
            params['wanted_cluster'] = int(opts.cluster_size)
        if params['wanted_cluster'] not in [512<<i for i in range(0,17)]:
            print("mkfat error: bad cluster size specified!")
            par.print_help()
            sys.exit(1)

    format(dsk, dsk.size, params=params)
