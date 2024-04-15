import struct, os, sys, pprint, math, importlib, locale, optparse
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools import utils
from FATtools.FAT import *
from FATtools.exFAT import *

nodos_asm_5Ah = b'\xB8\xC0\x07\x8E\xD8\xBE\x73\x00\xAC\x08\xC0\x74\x09\xB4\x0E\xBB\x07\x00\xCD\x10\xEB\xF2\xF4\xEB\xFD\x4E\x4F\x20\x44\x4F\x53\x00'

""" fat_mkfs allowed params={}:

query_info

if set, no format is applied but a dictionary with all allowed combinations
of FAT/cluster sizes is returned.

show_info

if set, prints messages emitted by format to the console.

fat_bits

FAT slot size in bits (12, 16 or 32); if not specified, tries all sizes and
sets the field to report the chosen FAT type to the caller.

reserved_size

reserved sectors before FAT table. Default: 1 (FAT12/16), 9 (FAT32)
Windows 10/11 defaults to 8 for FAT12/16, probably to make FAT32 conversion
easier.

media_byte
medium type code to put in the boot sector (look at get_format_parameters
in utils.py)

oem_id
OEM 8-bytes identifier in FAT boot sector (default: "MSDOS5.0" for FAT12-16,
"MSWIN4.1" for FAT32)

fat_copies

number of FAT tables (default: 2).

root_entries

number of 32 bytes entries in the fixed root directory (ignored in FAT32).
They must fill exactly one ore more sectors.
Default: 224 (FAT12) or 512 (FAT16). Appropriate values are used for
recognized floppy types (i.e. 112 for 712K floppy).

wanted_cluster

cluster size wanted by user, from 2^9 to 2^16 bytes. If sector size is 4096
bytes, size is up to 2^18 (256K). If not specified, the best value is
selected automatically. Windows 11 seems able to read/write a FAT32 volume
formatted with a 512K cluster, the maximum size a boot sector can record.

fat_no_64K_cluster

if set, 64K clusters (or bigger) are not allowed (DOS, Windows 9x).

fat_ff0_reserved

if set, clusters in range ...FF0h-...FF6h are also treated as reserved: this
should be the default for old FAT12 floppies, at least. Modern Windows reserv
clusters from ...FF7h (clusters 0 and 1 are always reserved), so we can have:
    0x100 - 0xF0 + 3 = 18 reserved clusters or
    0x100 - 0xF7 + 3 = 11 reserved clusters

fat12_disabled

if set, FAT12 is not applied to hard disks (i.e. disks >2880KB).
fat_bits set to 12 always overrides this setting.
In recent Windows editions (10, 11), CHKDSK often does not work when it
finds unexpected formats. For example, we can successfully apply FAT16 to
a 1.44M floppy but CHKDSK won't recognize it!

fat32_forbids_low_clusters

if set, FAT32 is allowed only if FAT16 can't be applied (clusters > 65525).
FORMAT suggests this since Windows 2000 and Windows 10 CHKDSK wants at least
65526 clusters to work properly.
However, if FAT32 is applied to a smaller volume, Windows will access it
regularly.

fat32_forbids_high_clusters

if set, FAT32 can't be applied with 4177918 or more clusters (FORMAT has
such limit since Windows 9x).
Otherwise, FAT32 allows up to 268435445 clusters (2^28-11: the 4 upper bits
are reserved) and Windows (since 98, at least) can access it regularly.
Obviously this way a couple FAT tables can waste almost 2GB!
(Note that  a 300GB volume with more than 300 mil. x 0.5K clusters is mounted
by Windows 11 and recognized by CHKDSK, which reports all clusters - the DIR
command, instead, reports ~50GB free space only).

fat32_backup_sector

FAT32 Boot and FSI sectors backup copy (default: at sector 6).


Return codes:
 0  no errors
-1  invalid sector size 
-2  bad FAT bits
-3  bad volume size (160K < size < 2T [16T with 4K sectors])
-4  no possible format with current parameters (try exFAT?)
-5  specified FAT type can't be applied
-6  specified cluster can't be applied
-7  zero FAT copies
-8  invalid root entries """

def fat_mkfs(stream, size, sector=512, params={}):
    "Creates a FAT 12/16/32 File System on stream. Returns 0 for success."
    if sector not in (512, 4096):
        if verbose: print("Fatal: only 512 or 4096 bytes sectors are supported!")
        return -1
    sectors = size//sector
    verbose = params.get('show_info', 0)
    
    fat_bits = params.get('fat_bits', 0)
    if fat_bits:
        if fat_bits not in (12,16,32):
            if verbose: print("Fatal: FAT slot can be only 12, 16 or 32 bits long!")
            return -2
        fat_slot_sizes = [fat_bits]
    else:
        fat_slot_sizes = [12,16,32]
        if params.get('fat12_disabled'): del fat_slot_sizes[0]

    if sectors < 320 or sectors > 0xFFFFFFFF:
        if verbose: print("Fatal: can't apply FAT file system to a %d sectors disk!" % sectors) # min is 5.25" 160K floppy
        return -3

    fat_copies = params.get('fat_copies', 2)                # default: best setting
    if fat_copies < 1: return -7
    reserved_clusters = 11                                  # (0,1, ..F7h-..FFh are always reserved)
    if params.get('fat_ff0_reserved') or sectors < 5761:    # if floppy or requested
        reserved_clusters = 18
    max_cluster = 17
    if params.get('fat_no_64K_cluster'): max_cluster = 16
    if sector == 4096: max_cluster = 19
    
    media_byte = 0xF8 # (HDD)
    if sectors < 5761: # if floppy
        media_byte = 0xF0 # generic HD floppy
        p = utils.get_format_parameters(size)
        if p:
            if not params.get('cluster_size'): params['cluster_size'] = p['cluster_size']
            if not params.get('root_entries'): params['root_entries'] = p['root_entries']
            media_byte = p['media_byte']

    fat_fs = {} # {fat_slot_size : allowed}
    
    # Calculate possible combinations for each FAT and cluster size
    for fat_slot_size in fat_slot_sizes:
        allowed = {} # {cluster_size : fsinfo}
        for i in range(9, max_cluster): # cluster sizes 0.5K...32K (64K) or 128K-256K with 4Kn sectors
            fsinfo = {}
            root_entries = params.get('root_entries', {12:224,16:512,32:0}[fat_slot_size])
            if (root_entries*32) % sector: return -8
            root_entries_size = (root_entries*32)+(sector-1)//sector # translate into sectors
            reserved_size = params.get('reserved_size', 1)*sector
            if fat_slot_size == 32 and not params.get('reserved_size'): reserved_size = 9*sector
            rreserved_size = reserved_size + root_entries_size # in FAT12/16 this space resides outside the cluster area
            cluster_size = (2**i)
            clusters = (size - rreserved_size) // cluster_size
            if clusters > (2**fat_slot_size)-reserved_clusters: continue # too many clusters, increase size
            if fat_slot_size == 32:
                if clusters > (2**28)-reserved_clusters: continue # FAT32 uses 28 bits only
                if params.get('fat32_forbids_low_clusters') and clusters < 65526: continue
                if params.get('fat32_forbids_high_clusters') and clusters > 4177917: continue
            while 1:
                fat_size = ((fat_slot_size*(clusters+2))//8+sector-1)//sector * sector # FAT sectors according to slot size (12, 16 or 32 bit)
                required_size = cluster_size*clusters + fat_copies*fat_size + rreserved_size
                if required_size <= size or clusters==0: break
                clusters -= 1
            if not clusters: continue
            fsinfo['required_size'] = required_size # space occupied by FS
            fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
            fsinfo['cluster_size'] = cluster_size
            fsinfo['clusters'] = clusters
            fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
            fsinfo['root_entries'] = root_entries
            allowed[cluster_size] = fsinfo
        if allowed: fat_fs[fat_slot_size] = allowed

    if params.get('query_info'): return fat_fs
    
    if not fat_fs:
        if verbose: print("Fatal error, can't apply any FAT file system!")
        return -4
        
    if fat_bits and not fat_fs[fat_bits]:
        if verbose: print("Can't apply FAT%d file system with any cluster size, aborting."%fat_bits)
        return -5
    
    if not fat_bits:
        fat_bits = list(fat_fs.keys())[0]
    
    # Choose a cluster size or try the user requested one
    wanted_cluster = params.get('wanted_cluster')
    if wanted_cluster:
        if wanted_cluster not in fat_fs[fat_bits]:
            if not params.get('fat_bits'): # retry another FAT type
                for n in (12,16,32):
                    if fat_fs.get(n) and fat_fs[n].get(wanted_cluster):
                        fat_bits = n
                        break
            if wanted_cluster not in fat_fs[fat_bits]:
                if verbose: print("Specified cluster size of %d is not allowed!" % wanted_cluster)
                return -6
        fsinfo = fat_fs[fat_bits][wanted_cluster]
    else:
        # Pick the medium...
        allowed = fat_fs[fat_bits]
        K = list(allowed.keys())
        i = len(K) // 2
        # ...except with some well known floppy formats
        if sectors < 5761:
            if sectors == 320: i=0 # 512b cluster
            elif sectors == 360: i=0
            elif sectors == 640: i=1 # 1K
            elif sectors == 720: i=1
            elif sectors == 1440: i=1
            elif sectors == 2880: i=0
            elif sectors == 3360: i=2 # 2K
            elif sectors == 5760: i=0
            else: i=0 # if unknown, select 512b cluster
        fsinfo = allowed[K[i]]
            
        if verbose: print("%.01fK cluster selected." % (int(K[i])/1024.0))

    if not params.get('fat_bits') and verbose: print("Selected FAT%d file system."%fat_bits)
    params['fat_bits'] = fat_bits

    if fat_bits == 32:
        boot = boot_fat32()
    else:
        boot = boot_fat16()
    boot.chJumpInstruction = b'\xEB\x58\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x5A:0x5A+len(nodos_asm_5Ah)] = nodos_asm_5Ah # insert assembled boot code
    if fat_bits == 32:
        boot.chOemID = b'%-8s' % b'MSWIN4.1' # this makes MS-DOS 7 Scandisk happy
    else:
        # It should be investigated if pre-5.0 editions want a particular OEM ID
        boot.chOemID = b'%-8s' % b'MSDOS5.0' # makes some old DOS apps more happy
    boot.wBytesPerSector = sector
    boot.wReservedSectors = fsinfo['reserved_size']//sector
    boot.dwHiddenSectors = 63 # sectors preceding a partition (should be extracted from partition offset!)
    boot.uchSectorsPerCluster = fsinfo['cluster_size']//sector
    boot.uchFATCopies = fat_copies
    boot.wMaxRootEntries = fsinfo['root_entries'] # fixed root (not used in FAT32)
    boot.uchMediaDescriptor = media_byte
    if fat_bits == 12 and sectors < 5761: boot.dwHiddenSectors = 0 # assume NOT partitioned
    if sectors < 65536:
        boot.wTotalSectors = fsinfo['required_size']//sector # effective sectors occupied by FAT Volume
    else:
        boot.dwTotalSectors = fsinfo['required_size']//sector
    if fat_bits != 32:
        boot.wSectorsPerFAT = fsinfo['fat_size']//sector
    else:
        boot.dwSectorsPerFAT = fsinfo['fat_size']//sector
    if media_byte == 0xF8: # if HDD
        boot.chPhysDriveNumber = 0x80 # else zero
    if fat_bits != 32:
        boot.uchSignature = 0x29
    else:
        boot.chExtBootSignature = 0x29
    # Next 3 are optional, and set if uchSignature/chExtBootSignature is set
    boot.dwVolumeID = FATDirentry.GetDosDateTime(1)
    boot.sVolumeLabel = b'%-11s' % b'NO NAME'
    boot.sFSType = b'%-8s' % b'FAT%d' % fat_bits
    boot.wBootSignature = 0xAA55
    c,h,s = utils.get_geometry(size)
    if type(stream) == disk.partition:
        s = stream.mbr.sectors_per_cyl
        h = stream.mbr.heads_per_cyl
        boot.dwHiddenSectors = stream.offset//sector
    if DEBUG&1: log("fat_mkfs C=%d, H=%d, S=%d", c, h, s)
    boot.wSectorsPerTrack = s
    boot.wHeads = h

    if fat_bits == 32:
        boot.dwRootCluster = 2
        boot.wFSISector = 1
        boot.wBootCopySector = params.get('fat32_backup_sector', 6)
        
    buf = boot.pack()
    #~ print(boot)
    #~ print('FAT, root, cluster #2 offsets', hex(boot.fat()), hex(boot.fat(1)), hex(boot.root()), hex(boot.dataoffs))

    stream.seek(0)
    # Write boot sector
    stream.write(buf)
    
    if fat_bits == 32:
        stream.seek(sector)
        fsi = fat32_fsinfo(offset=sector)
        fsi.sSignature1 = b'RRaA'
        fsi.sSignature2 = b'rrAa'
        fsi.dwFreeClusters = fsinfo['clusters'] - 1 # root is #2
        fsi.dwNextFreeCluster = 3 #2 is root
        fsi.wBootSignature = 0xAA55
        # Write FSI sector
        stream.write(fsi.pack())
        # Write backup copies of Boot and FSI
        if boot.wBootCopySector:
            stream.seek(boot.wBootCopySector*boot.wBytesPerSector)
            stream.write(buf)
            stream.seek(boot.wBootCopySector*boot.wBytesPerSector+sector)
            stream.write(fsi.pack())

    # Erase FAT(s) area
    stream.seek(boot.fat())
    if fat_bits == 32:
        to_blank = boot.uchFATCopies * boot.dwSectorsPerFAT * boot.wBytesPerSector
    else:
        to_blank = boot.uchFATCopies * boot.wSectorsPerFAT * boot.wBytesPerSector
    blank = bytearray(2<<20)
    while to_blank: # 6x faster than sectored technique on large FAT!
        n = min(2<<20, to_blank)
        stream.write(blank[:n])
        to_blank -= n
    # Initialize FAT(s)
    if fat_bits == 12:
        clus_0_2 = b'%c'%boot.uchMediaDescriptor + b'\xFF\xFF' 
    elif fat_bits == 16:
        clus_0_2 = b'\xF8\xFF\xFF\xFF'
    else:
        clus_0_2 = b'\xF8\xFF\xFF\x0F\xFF\xFF\xFF\xFF\xF8\xFF\xFF\x0F'
    for i in range(boot.uchFATCopies):
        stream.seek(boot.fat(i))
        stream.write(clus_0_2)

    # Blank root (at fixed offset or cluster #)
    stream.seek(boot.root())
    if fat_bits != 32:
        stream.write(bytearray(boot.wMaxRootEntries*32))
    else:
        stream.write(bytearray(boot.cluster))

    stream.flush() # force committing to disk before reopening, or could be not useable!

    sizes = {0:'B', 10:'KiB',20:'MiB',30:'GiB',40:'TiB',50:'EiB'}
    k = 0
    for k in sorted(sizes):
        if (fsinfo['required_size'] // (1<<k)) < 1024: break

    free_clusters = fsinfo['clusters']
    if fat_bits == 32: free_clusters -= 1 # root belongs to clusters heap in FAT32
    if verbose: print("Successfully applied FAT%d to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fat_bits,fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    if verbose: print("\nFAT #1 @0x%X, Data Region @0x%X, Root @0x%X" % (boot.fatoffs, boot.cl2offset(2), boot.root()))

    return 0


# EXFAT CODE

# Note: expanded and compressed tables generated by this functions may differ
# from MS's FORMAT (different locales?), but Windows and CHKDSK accepts them!

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



"""The layout of an exFAT file system is far more complex than old FAT.

At start we have a Volume Boot Record of 12 sectors made of:
- a boot sector area of 9 sectors, where the first one contains the usual
  FS descriptors and boot code. However, the boot code can span sectors;
- an OEM parameter sector, which must be zeroed if unused;
- a reserved sector (MS FORMAT does not even blank it!);
- a checksum sector, filled with the same DWORD containing the calculated
  checksum of the previous 11 sectors.

A backup copy of these 12 sectors must follow immediately.

Then the FAT region with a single FAT (except in the -actually unsupported-
T-exFAT). It hasn't to be consecutive to the previous region; however, it can't
legally lay inside the clusters heap (like NTFS $MFT) nor after it.

Finally, the Data region (again, it can reside far from FAT area) where the
root directory is located.

But the root directory must contain (and is normally preceeded by):
- a special Bitmap file, where allocated clusters are set;
- a special Up-Case file (compressed or uncompressed) for Unicode file name
  comparisons.
Those are "special" since marked with single slots of special types (0x81, 0x82)
instead of standard file/directory slots group (0x85, 0xC0, 0xC1).

FAT is set and valid only for fragmented files. However, it must be always set for
Root, Bitmap and Up-Case, even if contiguous."""

""" Allowed params={}:

query_info

if set, no format is applied but a dictionary with all allowed combinations
of cluster sizes is returned

show_info

if set, prints some format informations to the console

reserved_size

sectors reserved to the Boot region and its backup copy (min. 24)

fat_copies

number of FAT tables (default: 1)

dataregion_padding

additional space (in bytes; default: 0) between the FAT and Data regions

wanted_cluster

bytes size of the cluster wanted by user. If not specified, the best value
is selected automatically between 0.5K and 32M """

def exfat_mkfs(stream, size, sector=512, params={}):
    "Make an exFAT File System on stream. Returns 0 for success."
    verbose = params.get('show_info', 0)
    
    if sector not in (512, 4096):
        if verbose: print("Fatal: only 512 or 4096 bytes sectors are supported!")
        return -1

    sectors = size//sector

    # 24 sectors are required for Boot region & its backup, but FORMAT defaults to 64K
    reserved_size = params.get('reserved_size', 128)
    reserved_size *= sector
    if reserved_size < 24*sector: reserved_size = 24*sector
    fat_copies = params.get('fat_copies', 1) # default: best setting
    dataregion_padding = params.get('dataregion_padding', 0) # additional space between FAT region and Data region

    allowed = {} # {cluster_size : fsinfo}

    for i in range(9, 25): # cluster sizes 0.5K...32M
        fsinfo = {}
        cluster_size = (2**i)
        clusters = (size - reserved_size) // cluster_size
        if clusters > 0xFFFFFFF6: continue
        fat_size = (4*(clusters+2)+sector-1)//sector * sector
        required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size + dataregion_padding
        while required_size > size:
            clusters -= (required_size-size+cluster_size-1)//cluster_size
            fat_size = (4*(clusters+2)+sector-1)//sector * sector
            required_size = cluster_size*clusters + fat_copies*fat_size + reserved_size + dataregion_padding
        if clusters < 1 or clusters > 0xFFFFFFF6: continue
        fsinfo['required_size'] = required_size # space occupied by FS
        fsinfo['reserved_size'] = reserved_size # space reserved before FAT#1
        fsinfo['cluster_size'] = cluster_size
        fsinfo['clusters'] = clusters
        fsinfo['fat_size'] = fat_size # space occupied by a FAT copy
        allowed[cluster_size] = fsinfo

    if not allowed:
        if clusters < 1:
            if verbose: print("Can't apply exFAT with less than 1 cluster!")
            return -2
        else:
            if verbose: print("Too many clusters to apply exFAT: aborting.")
            return -3

    #~ print "* MKFS exFAT INFO: allowed combinations for cluster size:"
    #~ pprint.pprint(allowed)

    fsinfo = None

    if 'wanted_cluster' in params:
        if params['wanted_cluster'] in allowed:
            fsinfo = allowed[params['wanted_cluster']]
        else:
            if verbose: print("Specified cluster size of %d is not allowed for exFAT: aborting..." % params['wanted_cluster'])
            return -4
    else:
        fsinfo = allowed[calc_cluster(size)]

    boot = boot_exfat()
    boot.chJumpInstruction = b'\xEB\x76\x90' # JMP opcode is mandatory, or CHKDSK won't recognize filesystem!
    boot._buf[0x78:0x78+len(nodos_asm_78h)] = nodos_asm_78h # insert assembled boot code
    boot.chOemID = b'%-8s' % b'EXFAT'
    if type(stream) == disk.partition:
        boot.u64PartOffset = stream.offset
    else:
        boot.u64PartOffset = 0x800
    boot.u64VolumeLength = sectors
    # We can put FAT far away from reserved area, if we want...
    boot.dwFATOffset = reserved_size//sector
    boot.dwFATLength = fsinfo['fat_size']//sector
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

    # Blank the FAT(s) area
    stream.seek(boot.fatoffs)
    to_blank = fat_copies * fsinfo['fat_size']
    blank = bytearray(2<<20)
    while to_blank: # 6x faster than sectored technique on large FAT!
        n = min(2<<20, to_blank)
        stream.write(blank[:n])
        to_blank -= n

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
    empty = bytearray(sector); empty[-2] = 0x55; empty[-1] = 0xAA
    for i in range(8):
        stream.write(empty)
    # OEM parameter sector must be totally blank if unused (=no 0xAA55 signature)
    stream.write(bytearray(sector))
    # This sector is reserved, can have any content
    stream.write(bytearray(sector))

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
    if verbose: print("Successfully applied exFAT to a %.02f %s volume.\n%d clusters of %.1f KB.\n%.02f %s free in %d clusters." % (fsinfo['required_size']/(1<<k), sizes[k], fsinfo['clusters'], fsinfo['cluster_size']/1024, free_clusters*boot.cluster/(1<<k), sizes[k], free_clusters))
    if verbose: print("\nFAT Region @0x%X, Data Region @0x%X, Root (cluster #%d) @0x%X" % (boot.fatoffs, boot.cl2offset(2), boot.dwRootCluster, boot.cl2offset(boot.dwRootCluster)))
    return 0
