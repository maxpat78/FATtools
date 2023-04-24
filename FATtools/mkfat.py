import struct, os, sys, pprint, math, importlib, locale, optparse
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools import utils, partutils
from FATtools.FAT import *
from FATtools.exFAT import *
from FATtools.Volume import vopen

nodos_asm_5Ah = b'\xB8\xC0\x07\x8E\xD8\xBE\x73\x00\xAC\x08\xC0\x74\x09\xB4\x0E\xBB\x07\x00\xCD\x10\xEB\xF2\xF4\xEB\xFD\x4E\x4F\x20\x44\x4F\x53\x00'


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
        if clusters%2: clusters-=1 # get always an even number
        fat_size = ((12*(clusters+2))//8+sector-1)//sector * sector # 12-bit slot
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 2
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
        elif 2<<20 < size <= 4085<<10:
            fsinfo = allowed[1024]
        elif 4<<20 < size <= 8170<<10:
            fsinfo = allowed[2048]
        elif 8<<20 < size <= 16340<<10:
            fsinfo = allowed[4096]
        elif 16<<20 < size <= 32680<<10:
            fsinfo = allowed[8192]
        elif 32<<20 < size <= 65360<<10:
            fsinfo = allowed[16384]
        elif 64<<20 < size <= 130720<<10:
            fsinfo = allowed[32768]
        else:
            fsinfo = allowed[65536]

    boot = boot_fat16()
    boot.chJumpInstruction = b'\xEB\x58\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x5A:0x5A+len(nodos_asm_5Ah)] = nodos_asm_5Ah # insert assembled boot code
    boot.chOemID = b'%-8s' % b'MSDOS5.0' # makes some old DOS apps more happy
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
    c,h,s = partutils.size2chs(size,1)
    if DEBUG&1: log("fat12_mkfs C=%d, H=%d, S=%d", c, h, s)
    boot.wSectorsPerTrack = s
    boot.wHeads = h # not used with LBA

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
        reserved_size = sector # MS-DOS 6.22 & 7.1 want this

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
        if clusters%2: clusters-=1 # get always an even number
        fat_size = (2*(clusters+2)+sector-1)//sector * sector
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 2
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
    boot.chOemID = b'%-8s' % b'MSDOS5.0' # makes some old DOS apps more happy
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
    c,h,s = partutils.size2chs(size,1)
    if DEBUG&1: log("fat16_mkfs C=%d, H=%d, S=%d", c, h, s)
    boot.wSectorsPerTrack = s
    boot.wHeads = h # not used with LBA

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
        if clusters%2: clusters-=1 # get always an even number
        fat_size = (4*(clusters+2)+sector-1)//sector * sector
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size
        while required_size > size:
            clusters -= 2
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
    boot.chOemID = b'%-8s' % b'MSWIN4.1' # this makes MS-DOS 7 Scandisk happy
    boot.wBytesPerSector = sector
    boot.wSectorsCount = reserved_size//sector
    #~ boot.wHiddenSectors = 1
    boot.wHiddenSectors = 0x3f # 63 for standard DOS part
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
    c,h,s = partutils.size2chs(size,1)
    if DEBUG&1: log("fat32_mkfs C=%d, H=%d, S=%d", c, h, s)
    boot.wSectorsPerTrack = s
    boot.wHeads = h # not used with LBA

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



# EXFAT CODE

# Note: expanded and compressed tables generated by this functions may differ
# from MS's FORMAT (different locales?), but Windows and CHKDSK accept them!

# Experimenting with wrong compressed Up-Case tables showed that in many cases
# CHKDSK accepts them and signals no error, but Windows puts the filesystem
# in Read-Only mode instead!
def gen_upcase(internal=0):
    "Generates the full, expanded (128K) UpCase table"
    tab = []
    # Dumps the Windows ANSI Code Page (locally variable)
    # In Western Europe, typically CP850 (DOS) and CP1252 (Windows)
    pref_enc = locale.getpreferredencoding()
    if pref_enc == 'UTF-8': # Linux hack
        pref_enc = 'cp850'
    d_tab = importlib.import_module('encodings.'+pref_enc).decoding_table
    for i in range(256):
        C = d_tab[i].upper().encode('utf_16_le')
        if len(bytearray(C)) > 2:
            C = struct.pack('<H', i)
        tab += [C]
    for i in range(256, 65536):
        try:
            C = chr(i).upper().encode('utf_16_le')
        except UnicodeEncodeError:
            C = struct.pack('<H', i)
        if len(bytearray(C)) > 2:
            C = struct.pack('<H', i)
        tab += [C]
    if internal: return tab
    return bytearray().join(tab)

def gen_upcase_compressed():
    "Generates a compressed UpCase table"
    tab = []
    run = -1
    upcase = gen_upcase(1)
    for i in range(65536):
        u = struct.pack('<H',i)
        U = upcase[i]
        if u != U:
            rl = i-run
            if run > -1 and rl > 2:
                # Replace chars with range
                del tab[len(tab)-rl:]
                tab += [b'\xFF\xFF', struct.pack('<H',rl)]
            run = -1
        else:
            if run < 0: run = i
        tab += [U]
    return bytearray().join(tab)


nodos_asm_78h = b'\xB8\xC0\x07\x8E\xD8\xBE\x93\x00\xAC\x08\xC0\x74\x0A\xB4\x0E\xBB\x07\x00\xCD\x10\xE9\xF1\xFF\xF4\xE9\xFC\xFF\x4E\x4F\x20\x44\x4F\x53\x00'


def calc_cluster(size):
    "Returns a cluster adequate to volume size, MS FORMAT style (exFAT)"
    c = 9 # min cluster: 512 (2^9)
    v = 26 # min volume: 64 MiB (2^26)
    for i in range(17):
        if size <= 2**v: return 2**c
        c+=1
        v+=1
        if v == 29: v+=4
        if v == 39: v+=1
    return (2<<25) # Maximum cluster: 32 MiB



#~ #####
#~ The layout of an exFAT file system is far more complex than old FAT.
#
#~ At start we have a Volume Boot Record of 12 sectors made of:
#~ - a boot sector area of 9 sectors, where the first one contains the usual
#~   FS descriptors and boot code. However, the boot code can span sectors;
#~ - an OEM parameter sector, which must be zeroed if unused;
#~ - a reserved sector (MS FORMAT does not even blank it!);
#~ - a checksum sector, filled with the same DWORD containing the calculated
#~   checksum of the previous 11 sectors.
#
#~ A backup copy of these 12 sectors must follow immediately.
#
#~ Then the FAT region with a single FAT (except in the -actually unsupported-
#~ T-exFAT). It hasn't to be consecutive to the previous region; however, it can't
#~ legally lay inside the clusters heap (like NTFS $MFT) nor after it.
#
#~ Finally, the Data region (again, it can reside far from FAT area) where the
#~ root directory is located.
#
#~ But the root directory must contain (and is normally preceeded by):
#~ - a special Bitmap file, where allocated clusters are set;
#~ - a special Up-Case file (compressed or uncompressed) for Unicode file name
#~   comparisons.
#~ Those are "special" since marked with single slots of special types (0x81, 0x82)
#~ instead of standard file/directory slots group (0x85, 0xC0, 0xC1).
#
#~ FAT is set and valid only for fragmented files. However, it must be always set for
#~ Root, Bitmap and Up-Case, even if contiguous.
#####
def exfat_mkfs(stream, size, sector=512, params={}):
    "Make an exFAT File System on stream. Returns 0 for success."

    sectors = size//sector

    if 'reserved_size' in params:
        reserved_size = params['reserved_size']*sector
        if reserved_size < 24*sector:
            reserved_size = 24*sector
    else:
        # At least 24 sectors required for Boot region & its backup
        #~ reserved_size = 24*sector
        reserved_size = 65536 # FORMAT default

    if 'fat_copies' in params:
        fat_copies = params['fat_copies']
    else:
        fat_copies = 1 # default: best setting

    if 'dataregion_padding' in params:
        dataregion_padding = params['dataregion_padding']
    else:
        dataregion_padding = 0 # additional space between FAT region and Data region

    allowed = {} # {cluster_size : fsinfo}

    for i in range(9, 25): # cluster sizes 0.5K...32M
        fsinfo = {}
        cluster_size = (2**i)
        clusters = (size - reserved_size) // cluster_size
        # cluster_size increase? FORMAT seems to reserve more space than minimum
        fat_size = (4*(clusters+2)+sector-1)//sector * sector
        # round it to cluster_size, or memory page size or something?
        fat_size = (fat_size+cluster_size-1)//cluster_size * cluster_size
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size + dataregion_padding
        while required_size > size:
            clusters -= 1
            fat_size = (4*(clusters+2)+sector-1)//sector * sector
            fat_size = (fat_size+cluster_size-1)//cluster_size * cluster_size
            required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size + dataregion_padding
        if clusters < 1 or clusters > 0xFFFFFFFF:
            continue
        fsinfo['required_size'] = required_size # space occupied by FS
        fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
        fsinfo['cluster_size'] = cluster_size
        fsinfo['clusters'] = clusters
        fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
        allowed[cluster_size] = fsinfo

    if not allowed:
        if clusters < 1:
            print("Can't apply exFAT with less than 1 cluster!")
            return -1
        else:
            print("Too many clusters to apply exFAT: aborting.")
            return -1

    #~ print "* MKFS exFAT INFO: allowed combinations for cluster size:"
    #~ pprint.pprint(allowed)

    fsinfo = None

    if 'wanted_cluster' in params:
        if params['wanted_cluster'] in allowed:
            fsinfo = allowed[params['wanted_cluster']]
        else:
            print("Specified cluster size of %d is not allowed for exFAT: aborting..." % params['wanted_cluster'])
            return -1
    else:
        fsinfo = allowed[calc_cluster(size)]

    boot = boot_exfat()
    boot.chJumpInstruction = b'\xEB\x76\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x78:0x78+len(nodos_asm_78h)] = nodos_asm_78h # insert assembled boot code
    boot.chOemID = b'%-8s' % b'EXFAT'
    boot.u64PartOffset = 0x3F
    boot.u64VolumeLength = sectors
    # We can put FAT far away from reserved area, if we want...
    boot.dwFATOffset = (reserved_size+sector-1)//sector
    boot.dwFATLength = (fsinfo['fat_size']+sector-1)//sector
    # Again, we can put clusters heap far away from usual
    boot.dwDataRegionOffset = boot.dwFATOffset + boot.dwFATLength + dataregion_padding
    boot.dwDataRegionLength = fsinfo['clusters']
    # We'll calculate this after writing Bitmap and Up-Case
    boot.dwRootCluster = 0
    boot.dwVolumeSerial = exFATDirentry.GetDosDateTimeEx()[0]
    boot.wFSRevision = 0x100
    boot.wFlags = 0
    boot.uchBytesPerSector = int(math.log(sector)/math.log(2))
    boot.uchSectorsPerCluster = int(math.log(fsinfo['cluster_size']//sector) / math.log(2))
    boot.uchFATCopies = fat_copies
    boot.uchDriveSelect = 0x80
    boot.wBootSignature = 0xAA55
    
    if DEBUG&1: log("Inited Boot Sector\n%s", boot)

    boot.__init2__()

    # Blank the FAT area
    stream.seek(boot.fatoffs)
    blank = bytearray(sector)
    for i in range(boot.dwFATLength):
        stream.write(blank)

    # Initialize the FAT
    clus_0_2 = b'\xF8\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
    stream.seek(boot.fatoffs)
    stream.write(clus_0_2)

    # Make a Bitmap slot
    b = bytearray(32); b[0] = 0x81
    bitmap = exFATDirentry(b, 0)
    bitmap.dwStartCluster = 2 # default, but not mandatory
    bitmap.u64DataLength = (boot.dwDataRegionLength+7)//8
    if DEBUG&1: log("Inited Bitmap table of %d bytes @%Xh", bitmap.u64DataLength, 2)

    # Blank the Bitmap Area
    stream.seek(boot.cl2offset(bitmap.dwStartCluster))
    for i in range((bitmap.u64DataLength+boot.cluster-1)//boot.cluster):
        stream.write(bytearray(boot.cluster))

    # Make the Up-Case table and its file slot (following the Bitmap)
    start = bitmap.dwStartCluster + (bitmap.u64DataLength+boot.cluster-1)//boot.cluster

    # Write the compressed Up-Case table
    stream.seek(boot.cl2offset(start))
    table = gen_upcase_compressed()
    stream.write(table)

    # Make the Up-Case table slot
    b = bytearray(32); b[0] = 0x82
    upcase = exFATDirentry(b, 0)
    upcase.dwChecksum = boot.GetChecksum(table, True)
    upcase.dwStartCluster = start
    upcase.u64DataLength = len(table)
    if DEBUG&1: log("Inited UpCase table of %d bytes @%Xh", len(table), start)

    # Finally we can fix the root cluster!
    boot.dwRootCluster = upcase.dwStartCluster + (upcase.u64DataLength+boot.cluster-1)//boot.cluster

    # Write the VBR area (first 12 sectors) and its backup
    stream.seek(0)
    # Write boot & VBR sectors
    stream.write(boot.pack())
    # Since we haven't large boot code, all these are empty
    empty = bytearray(512); empty[-2] = 0x55; empty[-1] = 0xAA
    for i in range(8):
        stream.write(empty)
    # OEM parameter sector must be totally blank if unused (=no 0xAA55 signature)
    stream.write(bytearray(512))
    # This sector is reserved, can have any content
    stream.write(bytearray(512))

    # Read the first 11 sectors and get their 32-bit checksum
    stream.seek(0)
    vbr = stream.read(sector*11)
    checksum = struct.pack('<I', boot.GetChecksum(vbr))

    # Fill the checksum sector
    checksum = sector//4 * checksum

    # Write it, then the backup of the 12 sectors
    stream.write(checksum)
    stream.write(vbr)
    stream.write(checksum)

    # Blank the root directory cluster
    stream.seek(boot.root())
    stream.write(bytearray(boot.cluster))

    # Initialize root Dirtable
    boot.stream = stream
    fat = FAT(stream, boot.fatoffs, boot.clusters(), bitsize=32, exfat=True)

    if DEBUG&1: log("Root Cluster @%Xh", boot.dwRootCluster)

    # Mark the FAT chain for Bitmap, Up-Case and Root
    fat.mark_run(bitmap.dwStartCluster, (bitmap.u64DataLength+boot.cluster-1)//boot.cluster)
    fat.mark_run(upcase.dwStartCluster, (upcase.u64DataLength+boot.cluster-1)//boot.cluster)
    fat[boot.dwRootCluster] = fat.last

    # Initialize the Bitmap and mark the allocated clusters so far
    bmp = Bitmap(boot, fat, bitmap.dwStartCluster)
    bmp.set(bitmap.dwStartCluster, (bitmap.u64DataLength+boot.cluster-1)//boot.cluster)
    bmp.set(upcase.dwStartCluster, (upcase.u64DataLength+boot.cluster-1)//boot.cluster)
    bmp.set(boot.dwRootCluster)

    boot.bitmap = bmp
    root = Dirtable(boot, fat, boot.dwRootCluster)
    if DEBUG&1: log("Inited Root Dirtable\n%s", root)

    # Write Bitmap and UpCase slots (mandatory)
    root.stream.write(bitmap.pack())
    root.stream.write(upcase.pack())
    #~ # Write an empty Volume Label (optional)
    #~ b = bytearray(32); b[0] = 0x3
    #~ label = exFATDirentry(b, 0)
    #~ root.stream.write(label.pack())
    
    root.flush() # commit all changes to disk immediately, or volume won't be usable!

    sizes = {0:'B', 10:'KiB',20:'MiB',30:'GiB',40:'TiB',50:'EiB'}
    k = 0
    for k in sorted(sizes):
        if (fsinfo['required_size'] // (1<<k)) < 1024: break

    free_clusters = boot.dwDataRegionLength - (bitmap.u64DataLength+boot.cluster-1)//boot.cluster - (upcase.u64DataLength+boot.cluster-1)//boot.cluster - 1
    print("Successfully applied exFAT to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    print("\nFAT Region @0x%X, Data Region @0x%X, Root (cluster #%d) @0x%X" % (boot.fatoffs, boot.cl2offset(2), boot.dwRootCluster, boot.cl2offset(boot.dwRootCluster)))

    return 0



if __name__ == '__main__':
    help_s = """
    %prog [options] <drive>
    """
    par = optparse.OptionParser(usage=help_s, version="%prog 1.0", description="Applies a FAT12/16/32 or exFAT File System to a disk device or file image.")
    par.add_option("-t", "--fstype", dest="fs_type", help="try to apply the specified File System between FAT12, FAT16, FAT32 or EXFAT. Default: based on medium size.", metavar="FSTYPE", type="string")
    par.add_option("-c", "--cluster", dest="cluster_size", help="force a specified cluster size between 512, 1024, 2048, 4096, 8192, 16384, 32768 (since MS-DOS) or 65536 bytes (Windows NT+) for FAT. exFAT permits up to 32M. Default: based on medium size. Accepts 'k' and 'm' postfix for Kibibytes and Mebibytes.", metavar="CLUSTER")
    par.add_option("-p", "--partition", dest="part_type", help="create a single MBR or GPT partition from all disk space before formatting", metavar="PARTTYPE", type="string")
    opts, args = par.parse_args()

    if not args:
        print("mkfat error: you must specify a target to apply a FAT12/16/32 or exFAT file system!")
        par.print_help()
        sys.exit(1)

    dsk = vopen(args[0], 'r+b', 'disk')
    if dsk == 'EINV':
        print('Invalid disk or image file specified!')
        sys.exit(1)

    # Windows 10 Shell happily auto-mounts a VHD ONLY IF partitioned and formatted
    if opts.part_type:
        t = opts.part_type.lower()
        if t not in ('mbr', 'gpt'):
            print('You must specify MBR or GPT to auto partition disk space!')
            sys.exit(1)
        print("Creating a %s partition with all disk space..."%t)
        if t=='mbr':
            if dsk.size > (2<<40): 
                print('You must specify GPT partition scheme with disks >2TB!')
                sys.exit(1)
            partutils.partition(dsk, 'mbr', mbr_type=0xC)
        else:
            partutils.partition(dsk)
        dsk.close()
        dsk = vopen(args[0], 'r+b', 'partition0')

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
