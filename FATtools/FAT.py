# -*- coding: cp1252 -*-
# Utilities to manage a FAT12/16/32 file system
#

import sys, copy, os, struct, time, io, atexit, functools, ctypes
from datetime import datetime
from collections import OrderedDict
from zlib import crc32
from FATtools import disk, utils
from FATtools.debug import log

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
if DEBUG&4: import hexdump

FS_ENCODING = sys.getfilesystemencoding()
VFS_ENCODING = 'cp1252' # set here encoding to use in virtual FAT FS

class FATException(Exception): pass

class boot_fat32(object):
    "FAT32 Boot Sector"
    layout = { # { offset: (name, unpack string) }
    0x00: ('chJumpInstruction', '3s'),
    0x03: ('chOemID', '8s'),
    0x0B: ('wBytesPerSector', '<H'),
    0x0D: ('uchSectorsPerCluster', 'B'),
    0x0E: ('wReservedSectors', '<H'), # reserved sectors before 1st FAT (min 9)
    0x10: ('uchFATCopies', 'B'),
    0x11: ('wMaxRootEntries', '<H'),
    0x13: ('wTotalSectors', '<H'), # volume sectors if < 65536, or zero
    0x15: ('uchMediaDescriptor', 'B'), # F8h HDD, F0h=1.44M floppy, F9h=720K floppy
    0x16: ('wSectorsPerFAT', '<H'), # not used, see 24h instead
    0x18: ('wSectorsPerTrack', '<H'),
    0x1A: ('wHeads', '<H'),
    0x1C: ('dwHiddenSectors', '<I'), # disk sectors preceding this boot sector
    0x20: ('dwTotalSectors', '<I'), # volume sectors if > 65535, or zero
    0x24: ('dwSectorsPerFAT', '<I'),
    0x28: ('wMirroringFlags', '<H'), # bits 0-3: active FAT, it bit 7 set; else: mirroring as usual
    0x2A: ('wVersion', '<H'),
    0x2C: ('dwRootCluster', '<I'), # usually 2
    0x30: ('wFSISector', '<H'), # usually 1
    0x32: ('wBootCopySector', '<H'), # 0x0000 or 0xFFFF if unused, usually 6
    0x34: ('chReserved', '12s'),
    0x40: ('chPhysDriveNumber', 'B'), # 00h=floppy, 80h=fixed (used by boot code)
    0x41: ('chFlags', 'B'),
    0x42: ('chExtBootSignature', 'B'), # 0x28 or 0x29 (zero if following id, label and FS type absent)
    0x43: ('dwVolumeID', '<I'),
    0x47: ('sVolumeLabel', '11s'),
    0x52: ('sFSType', '8s'),
    #~ 0x72: ('chBootstrapCode', '390s'),
    0x1FE: ('wBootSignature', '<H') # 55 AA
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal boot sector size
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.__init2__()

    def __init2__(self):
        if not self.wBytesPerSector: return
        # Cluster size (bytes)
        self.cluster = self.wBytesPerSector * self.uchSectorsPerCluster
        # Offset of the 1st FAT copy
        self.fatoffs = self.wReservedSectors * self.wBytesPerSector + self._pos
        # Data area offset (=cluster #2)
        self.dataoffs = self.fatoffs + self.uchFATCopies * self.dwSectorsPerFAT * self.wBytesPerSector + self._pos
        # Number of clusters represented in this FAT (if valid buffer)
        self.fatsize = self.dwTotalSectors//self.uchSectorsPerCluster
        if self.stream:
            self.fsinfo = fat32_fsinfo(stream=self.stream, offset=self.wFSISector*self.cluster)
        else:
            self.fsinfo = None

    __getattr__ = utils.common_getattr

    def __str__ (self):
        return utils.class2str(self, "FAT32 Boot Sector @%x\n" % self._pos)

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self.__init2__()
        return self._buf

    def clusters(self):
        "Returns the number of clusters in the data area"
        # Total sectors minus sectors preceding the data area
        return (self.dwTotalSectors - (self.dataoffs//self.wBytesPerSector)) // self.uchSectorsPerCluster

    def cl2offset(self, cluster):
        "Returns the real offset of a cluster"
        return self.dataoffs + (cluster-2)*self.cluster

    def root(self):
        "Returns the offset of the root directory"
        return self.cl2offset(self.dwRootCluster)

    def fat(self, fatcopy=0):
        "Returns the offset of a FAT table (the first by default)"
        return self.fatoffs + fatcopy * self.dwSectorsPerFAT * self.wBytesPerSector



class fat32_fsinfo(object):
    "FAT32 FSInfo Sector (usually sector 1)"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature1', '4s'), # RRaA
    0x1E4: ('sSignature2', '4s'), # rrAa
    0x1E8: ('dwFreeClusters', '<I'), # 0xFFFFFFFF if unused (may be incorrect)
    0x1EC: ('dwNextFreeCluster', '<I'), # hint only (0xFFFFFFFF if unused)
    0x1FE: ('wBootSignature', '<H') # 55 AA
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal FSInfo sector size
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "FAT32 FSInfo Sector @%x\n" % self._pos)



class boot_fat16(object):
    "FAT12/16 Boot Sector"
    layout = { # { offset: (name, unpack string) }
    0x00: ('chJumpInstruction', '3s'),
    0x03: ('chOemID', '8s'),
    0x0B: ('wBytesPerSector', '<H'),
    0x0D: ('uchSectorsPerCluster', 'B'),
    0x0E: ('wReservedSectors', '<H'), # reserved sectors before 1st FAT (min 1, the boot; Windows often defaults to 8)
    0x10: ('uchFATCopies', 'B'),
    0x11: ('wMaxRootEntries', '<H'),
    0x13: ('wTotalSectors', '<H'),
    0x15: ('uchMediaDescriptor', 'B'),
    0x16: ('wSectorsPerFAT', '<H'), #DWORD in FAT32
    0x18: ('wSectorsPerTrack', '<H'),
    0x1A: ('wHeads', '<H'),
    0x1C: ('dwHiddenSectors', '<I'),
    0x20: ('dwTotalSectors', '<I'),
    0x24: ('chPhysDriveNumber', 'B'), # 00h=floppy, 80h=fixed (used by boot code)
    0x25: ('uchCurrentHead', 'B'), # unused
    0x26: ('uchSignature', 'B'), # 0x28 or 0x29 (zero if following id, label and FS type absent)
    0x27: ('dwVolumeID', '<I'),
    0x2B: ('sVolumeLabel', '11s'),
    0x36: ('sFSType', '8s'),
    0x1FE: ('wBootSignature', '<H') # 55 AA
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal boot sector size
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.__init2__()

    def __init2__(self):
        if not self.wBytesPerSector: return
        # Cluster size (bytes)
        self.cluster = self.wBytesPerSector * self.uchSectorsPerCluster
        # Offset of the 1st FAT copy
        self.fatoffs = self.wReservedSectors * self.wBytesPerSector + self._pos
        # Number of clusters represented in this FAT
        # Here the DWORD field seems to be set only if WORD one is too small
        self.fatsize = (self.dwTotalSectors or self.wTotalSectors)//self.uchSectorsPerCluster
        # Offset of the fixed root directory table (immediately after the FATs)
        self.rootoffs = self.fatoffs + self.uchFATCopies * self.wSectorsPerFAT * self.wBytesPerSector + self._pos
        # Data area offset (=cluster #2)
        self.dataoffs = self.rootoffs + (self.wMaxRootEntries*32)
        # Set for compatibility with FAT32 code
        self.dwRootCluster = 0

    __getattr__ = utils.common_getattr

    def __str__ (self):
        return utils.class2str(self, "FAT12/16 Boot Sector @%x\n" % self._pos)

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self.__init2__()
        return self._buf

    def clusters(self):
        "Returns the number of clusters in the data area"
        # Total sectors minus sectors preceding the data area
        return ((self.dwTotalSectors or self.wTotalSectors) - (self.dataoffs//self.wBytesPerSector)) // self.uchSectorsPerCluster

    def cl2offset(self, cluster):
        "Returns the real offset of a cluster"
        return self.dataoffs + (cluster-2)*self.cluster

    def root(self):
        "Returns the offset of the root directory"
        return self.rootoffs

    def fat(self, fatcopy=0):
        "Returns the offset of a FAT table (the first by default)"
        return self.fatoffs + fatcopy * self.wSectorsPerFAT * self.wBytesPerSector



# NOTE: limit decoded dictionary size! Zero or {}.popitem()?
class FAT(object):
    "Decodes a FAT (12, 16, 32 o EX) table on disk"
    def __init__ (self, stream, offset, clusters, bitsize=32, exfat=0, sector=512):
        self.sector = sector # physical sector size
        self.stream = stream
        self.size = clusters # total clusters in the data area (max = 2^x - 11)
        self.bits = bitsize # cluster slot bits (12, 16 or 32)
        self.offset = offset # relative FAT offset (1st copy)
        # CAVE! This accounts the 0-1 unused cluster index?
        self.offset2 = offset + (((clusters*bitsize+7)//8)+(self.sector-1))//self.sector*self.sector # relative FAT offset (2nd copy)
        self.exfat = exfat # true if exFAT (aka FAT64)
        self.reserved = 0x0FF7
        self.bad = 0x0FF7
        self.last = 0x0FFF
        if bitsize == 32:
            self.fat_slot_size = 4
            self.fat_slot_fmt = '<I'
        else:
            self.fat_slot_size = 2
            self.fat_slot_fmt = '<H'
        if bitsize == 16:
            self.reserved = 0xFFF7
            self.bad = 0xFFF7
            self.last = 0xFFFF
        elif bitsize == 32:
            self.reserved = 0x0FFFFFF7 # FAT32 uses 28 bits only
            self.bad = 0x0FFFFFF7
            self.last = 0x0FFFFFF8
            if exfat: # EXFAT uses all 32 bits...
                self.reserved = 0xFFFFFFF7
                self.bad = 0xFFFFFFF7
                self.last = 0xFFFFFFFF
        # maximum cluster index effectively addressable
        # clusters ranges from 2 to 2+n-1 clusters (zero based), so last valid index is n+1
        self.real_last = min(self.reserved-1, self.size+2-1)
        self.decoded = {} # {cluster index: cluster content}
        self.last_free_alloc = 2 # last free cluster allocated (also set in FAT32 FSInfo)
        self.free_clusters = None # tracks free clusters
        # ordered (by disk offset) dictionary {first_cluster: run_length} mapping free space
        self.free_clusters_map = None
        self.map_free_space()
        self.free_clusters_flag = 1
        
    def __str__ (self):
        return "%d-bit %sFAT table of %d clusters starting @%Xh\n" % (self.bits, ('','ex')[self.exfat], self.size, self.offset)

    def __getitem__ (self, index):
        "Retrieves the value stored in a given cluster index"
        try:
            assert 2 <= index <= self.real_last
        except AssertionError:
            if DEBUG&4: log("Attempt to read unexistant FAT index #%d", index)
            #~ raise FATException("Attempt to read unexistant FAT index #%d" % index)
            return self.last
        slot = self.decoded.get(index)
        if slot: return slot
        pos = self.offset+(index*self.bits)//8
        self.stream.seek(pos)
        slot = struct.unpack(self.fat_slot_fmt, self.stream.read(self.fat_slot_size))[0]
        #~ print "getitem", self.decoded
        if self.bits == 12:
            # Pick the 12 bits we want
            if index % 2: # odd cluster
                slot = slot >> 4
            else:
                slot = slot & 0x0FFF
        self.decoded[index] = slot
        if DEBUG&4: log("Got FAT1[0x%X]=0x%X @0x%X", index, slot, pos)
        return slot

    # TFAT (transacted FAT, rare) should write on FAT#2, allowing recovering
    # from system failures, then update FAT#1
    def __setitem__ (self, index, value):
        "Set the value stored in a given cluster index"
        try:
            assert 2 <= index <= self.real_last
        except AssertionError:
            if DEBUG&4: log("Attempt to set invalid cluster index 0x%X with value 0x%X", index, value)
            return
            raise FATException("Attempt to set invalid cluster index 0x%X with value 0x%X" % (index, value))
        try:
            assert value <= self.real_last or value >= self.reserved
        except AssertionError:
            if DEBUG&4: log("Attempt to set invalid value 0x%X in cluster 0x%X", value, index)
            return
            raise FATException("Attempt to set invalid cluster index 0x%X with value 0x%X" % (index, value))
        self.decoded[index] = value
        dsp = (index*self.bits)//8
        pos = self.offset+dsp
        if self.bits == 12:
            # Pick and set only the 12 bits we want
            self.stream.seek(pos)
            slot = struct.unpack(self.fat_slot_fmt, self.stream.read(self.fat_slot_size))[0]
            if index % 2: # odd cluster
                # Value's 12 bits moved to top ORed with original bottom 4 bits
                #~ print "odd", hex(value), hex(slot), self.decoded
                value = (value << 4) | (slot & 0xF)
                #~ print hex(value), hex(slot)
            else:
                # Original top 4 bits ORed with value's 12 bits
                #~ print "even", hex(value), hex(slot)
                value = (slot & 0xF000) | value
                #~ print hex(value), hex(slot)
        if DEBUG&4: log("setting FAT1[0x%X]=0x%X @0x%X", index, value, pos)
        self.stream.seek(pos)
        value = struct.pack(self.fat_slot_fmt, value)
        self.stream.write(value)
        if self.exfat: return # exFAT has one FAT only (default)
        pos = self.offset2+dsp
        if DEBUG&4: log("setting FAT2[0x%X] @0x%X", index, pos)
        self.stream.seek(pos)
        self.stream.write(value)

    def isvalid(self, index):
        "Tests if index is a valid cluster number in this FAT"
        # Inline explicit test avoiding func call to speed-up
        if (index >= 2 and index <= self.real_last) or self.islast(index) or self.isbad(index):
            return 1
        if DEBUG&4: log("invalid cluster index: %x", index)
        return 0

    def islast(self, index):
        "Tests if index is the last cluster in the chain"
        return self.last <= index <= self.last+7 # *F8 ... *FF

    def isbad(self, index):
        "Tests if index is a bad cluster"
        return index == self.bad

    def count(self, startcluster):
        "Counts the clusters in a chain. Returns a tuple (<total clusters>, <last cluster>)"
        n = 1
        while not (self.last <= self[startcluster] <= self.last+7): # islast
            startcluster = self[startcluster]
            n += 1
        return (n, startcluster)

    def count_to(self, startcluster, clusters):
        "Finds the index of the n-th cluster in a chain"
        while clusters and not (self.last <= self[startcluster] <= self.last+7): # islast
            startcluster = self[startcluster]
            clusters -= 1
        return startcluster

    def count_run(self, start, count=0):
        """Returns the count of the clusters in a contiguous run from 'start'
        and the next cluster (or END CLUSTER mark), eventually limiting to the first 'count' clusters"""
        #~ print "count_run(%Xh, %d)" % (start, count)
        n = 1
        while 1:
            if self.last <= start <= self.last+7: # if end cluster
                break
            prev = start
            start = self[start]
            # If next LCN is not contig
            if prev != start-1:
                break
            # If max run length reached
            if count > 0:
                if  count-1 == 0:
                    break
                else:
                    count -= 1
            n += 1
        return n, start

    def findmaxrun(self):
        "Finds the greatest cluster run available. Returns a tuple (total_free_clusters, (run_start, clusters))"
        t = 1,0
        maxrun=(0,0)
        n=0
        while 1:
            t = self.findfree(t[0]+1)
            if t[0] < 0: break
            if DEBUG&4: log("Found %d free clusters from #%d", t[1], t[0])
            maxrun = max(t, maxrun, key=lambda x:x[1])
            n += t[1]
            t = (t[0]+t[1], t[1])
        if DEBUG&4: log("Found the biggest run of %d clusters from #%d on %d total free clusters", maxrun[1], maxrun[0], n)
        return n, maxrun

    def map_free_space(self):
        "Maps the free clusters in an ordered dictionary {start_cluster: run_length}"
        if self.exfat: return
        startpos = self.stream.tell()
        self.free_clusters_map = {}
        FREE_CLUSTERS=0
        # FAT16 is max 130K
        PAGE = self.offset2 - self.offset - (2*self.bits)//8
        if self.bits == 12:
            fat_slot = (ctypes.c_ubyte*3)
        elif self.bits == 16:
            fat_slot = ctypes.c_short
        else:
            # FAT32 could reach ~1GB!
            PAGE = 4<<20
            fat_slot = ctypes.c_int
        END_OF_CLUSTERS = self.offset + (self.size*self.bits+7)//8 + (2*self.bits)//8
        i = self.offset+(2*self.bits)//8 # address of cluster #2
        self.stream.seek(i)
        while i < END_OF_CLUSTERS:
            s = self.stream.read(min(PAGE, END_OF_CLUSTERS-i)) # slurp full FAT, or 1M page if FAT32
            s_len = len(s)
            fat_slots = s_len*8//self.bits
            if self.bits == 12:
                pad = s_len - (s_len+2)//3
                #~ print('dbg:', len(s), pad)
                fat_table = (fat_slot*((fat_slots+1)//2)).from_buffer(s+pad*b'\x00') # each 24-bit slot holds 2 clusters
            else:
                fat_table = (fat_slot*fat_slots).from_buffer(s) # convert buffer into array of (D)WORDs
            if DEBUG&4: log("map_free_space: loaded FAT page of %d slots @0x%X", fat_slots, i)
            j=0
            while j < fat_slots:
                first_free = -1
                run_length = -1
                while j < fat_slots:
                    if self.bits != 12:
                        if fat_table[j]:
                            j += 1
                            if run_length > 0: break
                            continue
                    else:
                        # Pick the 12 bits wanted from a 3-bytes group
                        odd = j%2 # is odd cluster?
                        ci = j*12//24 # map cluster index to 24-bit index
                        #~ print('dbg: %d/%d   %d/%d %d' % (j, fat_slots, ci, len(fat_table), odd))
                        if (not odd and (fat_table[ci][0] or fat_table[ci][1]&0xF0)) or (odd and (fat_table[ci][1]&0xF or fat_table[ci][2])):
                            j += 1
                            if run_length > 0: break
                            continue
                    if first_free < 0:
                        first_free = (i-self.offset)*8//self.bits + j
                        if DEBUG&4: log("map_free_space: found run from %d", first_free)
                        run_length = 0
                    run_length += 1
                    j+=1
                if first_free < 0: continue
                FREE_CLUSTERS+=run_length
                self.free_clusters_map[first_free] =  run_length
                if DEBUG&4: log("map_free_space: appended run (%d, %d)", first_free, run_length)
            i += s_len # advance to next FAT page to examine
        self.stream.seek(startpos)
        self.free_clusters = FREE_CLUSTERS
        if DEBUG&4: log("map_free_space: %d clusters free in %d runs", FREE_CLUSTERS, len(self.free_clusters_map))
        return FREE_CLUSTERS, len(self.free_clusters_map)

    def findfree(self, count=0):
        """Returns index and length of the first free clusters run beginning from
        'start' or (-1,0) in case of failure. If 'count' is given, limit the search
        to that amount."""
        if self.free_clusters_map == None:
            self.map_free_space()
        try:
            i, n = self.free_clusters_map.popitem()
        except KeyError:
            return -1, -1
        if DEBUG&4: log("got run of %d free clusters from #%x", n, i)
        if n-count > 0:
            self.free_clusters_map[i+count] = n-count # updates map
        self.free_clusters-=min(n,count)
        return i, min(n, count)
    
    def map_compact(self, strategy=0):
        "Compacts, eventually reordering, the free space runs map"
        if not self.free_clusters_flag: return
        #~ print "Map before:", sorted(self.free_clusters_map.iteritems())
        map_changed = 0
        while 1:
            d=copy.copy(self.free_clusters_map)
            for k,v in sorted(self.free_clusters_map.items()):
                while d.get(k+v): # while contig runs exist, merge
                    v1 = d.get(k+v)
                    if DEBUG&4: log("Compacting free_clusters_map: {%d:%d} -> {%d:%d}", k,v,k,v+v1)
                    d[k] = v+v1
                    del d[k+v]
                    #~ print "Compacted {%d:%d} -> {%d:%d}" %(k,v,k,v+v1)
                    #~ print sorted(d.iteritems())
                    v+=v1
            if self.free_clusters_map != d:
                self.free_clusters_map = d
                map_changed = 1
                continue
            break
        self.free_clusters_flag = 0
        #~ if strategy == 1:
            #~ self.free_clusters_map = OrderedDict(sorted(self.free_clusters_map.items(), key=lambda t: t[0])) # sort by disk offset
        #~ elif strategy == 2:
            #~ self.free_clusters_map = OrderedDict(sorted(self.free_clusters_map.items(), key=lambda t: t[1])) # sort by run size
        if DEBUG&4: log("Free space map - %d run(s): %s", len(self.free_clusters_map), self.free_clusters_map)
        #~ print "Map AFTER:", sorted(self.free_clusters_map.iteritems())
        
    # TODO: split very large runs
    # About 12% faster injecting a Python2 tree
    def mark_run(self, start, count, clear=False):
        "Marks a range of consecutive FAT clusters (optimized for FAT16/32)"
        if not count: return
        if DEBUG&4: log("mark_run(%Xh, %d, clear=%d)", start, count, clear)
        if start<2 or start>self.real_last:
            if DEBUG&4: log("attempt to mark invalid run, aborted!")
            return
        if self.bits == 12:
            if clear == True:
                self.free_clusters_flag = 1
                self.free_clusters_map[start] = count
            while count:
                self[start] = (start+1, 0)[clear==True]
                start+=1
                count-=1
            return
        dsp = (start*self.bits)//8
        pos = self.offset+dsp
        self.stream.seek(pos)
        if clear:
            for i in range(start, start+count):
                self.decoded[i] = 0
            run = bytearray(count*(self.bits//8))
            self.stream.write(run)
            self.free_clusters_flag = 1
            self.free_clusters_map[start] = count
            if self.exfat: return # exFAT has one FAT only (default)
            # updating FAT2, too!
            self.stream.seek(self.offset2+dsp)
            self.stream.write(run)
            return
        # consecutive values to set
        L = range(start+1, start+1+count)
        for i in L:
            self.decoded[i-1] = i
        self.decoded[start+count-1] = self.last
        # converted in final LE WORD/DWORD array
        L = [struct.pack(self.fat_slot_fmt, x) for x in L]
        L[-1] = struct.pack(self.fat_slot_fmt, self.last)
        run = bytearray().join(L)
        self.stream.write(run)
        if self.exfat: return # exFAT has one FAT only (default)
        # updating FAT2, too!
        pos = self.offset2+dsp
        self.stream.seek(pos)
        self.stream.write(run)

    def alloc(self, runs_map, count, params={}):
        """Allocates a set of free clusters, marking the FAT.
        runs_map is the dictionary of previously allocated runs
        count is the number of clusters to allocate
        params is an optional dictionary of directives to tune the allocation (to be done). 
        Returns the last cluster or raise an exception in case of failure"""
        self.map_compact()

        if self.free_clusters < count:
            if DEBUG&4: log("Couldn't allocate %d cluster(s), only %d free", count, self.free_clusters)
            raise FATException("FATAL! Free clusters exhausted, couldn't allocate %d, only %d left!" % (count, self.free_clusters))

        if DEBUG&4: log("Ok to allocate %d cluster(s), %d free", count, self.free_clusters)

        last_run = None
        
        while count:
            if runs_map:
                last_run = list(runs_map.items())[-1]
            i, n = self.findfree(count)
            self.mark_run(i, n) # marks the FAT
            if last_run:
                self[last_run[0]+last_run[1]-1] = i # link prev chain with last
            if last_run and i == last_run[0]+last_run[1]: # if contiguous
                runs_map[last_run[0]] = n+last_run[1]
            else:
                runs_map[i] = n
            last = i + n - 1 # last cluster in new run
            count -= n

        self[last] = self.last
        self.last_free_alloc = last

        if DEBUG&4: log("New runs map: %s", runs_map)
        return last

    def free(self, start, runs=None):
        "Frees a clusters chain, one run at a time (except FAT12)"
        if start < 2 or start > self.real_last:
            if DEBUG&4: log("free: attempt to free from invalid cluster %Xh", start)
            return
        self.free_clusters_flag = 1
        if runs:
            for run in runs:
                if DEBUG&4: log("free: directly zeroing run of %d clusters from %Xh", runs[run], run)
                self.mark_run(run, runs[run], True)
                if not self.exfat:
                    self.free_clusters += runs[run]
                    self.free_clusters_map[run] = runs[run]
            return

        while True:
            length, next = self.count_run(start)
            if DEBUG&4:
                log("free: count_run returned %d, %Xh", length, next)
                log("free: zeroing run of %d clusters from %Xh (next=%Xh)", length, start, next)
            self.mark_run(start, length, True)
            if not self.exfat:
                self.free_clusters += length
                self.free_clusters_map[start] = length
            start = next
            if self.last <= next <= self.last+7: break


class Chain(object):
    "Opens a cluster chain or run like a plain file"
    def __init__ (self, boot, fat, cluster, size=0, nofat=0, end=0):
        self.isdirectory=False
        self.stream = boot.stream
        self.boot = boot
        self.fat = fat
        self.start = cluster # start cluster or zero if empty
        self.end = end # end cluster
        self.nofat = nofat # 0=uses FAT (fragmented)
        self.size = (size+boot.cluster-1)//boot.cluster*boot.cluster
        # Size in bytes of allocated cluster(s)
        if self.start and (not nofat or not self.fat.exfat):
            if not size or not end:
                self.size, self.end = fat.count(cluster)
                self.size *= boot.cluster
        else:
            self.size = (size+boot.cluster-1)//boot.cluster*boot.cluster
            self.end = cluster + (size+boot.cluster-1)//boot.cluster
        self.filesize = size or self.size # file size, if available, or chain size
        self.pos = 0 # virtual stream linear pos
        # Virtual Cluster Number (cluster index in this chain)
        self.vcn = 0
        # Virtual Cluster Offset (current offset in VCN)
        self.vco = 0
        self.lastvlcn = (0, cluster) # last cluster VCN & LCN
        self.runs = OrderedDict() # RLE map of fragments
        if self.start:
            self._get_frags()
        if DEBUG&4: log("Cluster chain of %d%sbytes (%d bytes) @LCN %Xh:LBA %Xh", self.filesize, (' ', ' contiguous ')[nofat], self.size, cluster, self.boot.cl2offset(cluster))

    def __str__ (self):
        return "Chain of %d (%d) bytes from LCN %Xh (LBA %Xh)" % (self.filesize, self.size, self.start, self.boot.cl2offset(self.start))

    def _get_frags(self):
        "Maps the cluster runs composing the chain"
        start = self.start
        if self.nofat:
            self.runs[start] = self.size//self.boot.cluster
        else:
            while 1:
                length, next = self.fat.count_run(start)
                self.runs[start] = length
                if next == self.fat.last or next==start+length-1: break
                start = next
        if DEBUG&4: log("Runs map for %s: %s", self, self.runs)

    def _alloc(self, count):
        "Allocates some clusters and updates the runs map. Returns last allocated LCN"
        if self.fat.exfat:
            self.end = self.boot.bitmap.alloc(self.runs, count)
        else:
            self.end = self.fat.alloc(self.runs, count)
        if not self.start:
            self.start = list(self.runs.keys())[0]
        self.nofat = (len(self.runs)==1)
        self.size += count * self.boot.cluster
        return self.end

    def maxrun4len(self, length):
        "Returns the longest run of clusters, up to 'length' bytes, from current position"
        if not self.runs:
            self._get_frags()
        n = (length+self.boot.cluster-1)//self.boot.cluster # contig clusters searched for
        found = 0
        items = list(self.runs.items())
        for start, count in items:
            # if current LCN is in run
            if start <= self.lastvlcn[1] < start+count:
                found=1
                break
        if not found:
            raise FATException("FATAL! maxrun4len did NOT find current LCN!\n%s\n%s" % (self.runs, self.lastvlcn))
        left = start+count-self.lastvlcn[1] # clusters to end of run
        run = min(n, left)
        maxchunk = run*self.boot.cluster
        if n < left:
            next = self.lastvlcn[1]+n
        else:
            i = items.index((start, count))
            if i == len(items)-1:
                next = self.fat.last
            else:
                next = items[i+1][0] # first of next run
        # Updates VCN & next LCN
        self.lastvlcn = (self.lastvlcn[0]+n, next)
        if DEBUG&4:
            log("Chain%08X: maxrun4len(%d) on %s, maxchunk of %d bytes, lastvlcn=%s", self.start, length, self.runs, maxchunk, self.lastvlcn)
        return maxchunk

    def tell(self): return self.pos

    def realtell(self):
        return self.boot.cl2offset(self.lastvlcn[1])+self.vco

    def seek(self, offset, whence=0):
        if whence == 1:
            self.pos += offset
        elif whence == 2:
            if self.size:
                self.pos = self.size + offset
        else:
            self.pos = offset
        # allocate some clusters if needed (in write mode)
        if self.pos > self.size:
            if self.boot.stream.mode == 'r+b':
                clusters = (self.pos+self.boot.cluster-1)//self.boot.cluster - self.size//self.boot.cluster
                self._alloc(clusters)
                if DEBUG&4: log("Chain%08X: allocated %d cluster(s) seeking %Xh", self.start, clusters, self.pos)
            else:
                self.pos = self.size
        # Maps Virtual Cluster Number (chain cluster) to Logical Cluster Number (disk cluster)
        self.vcn = self.pos // self.boot.cluster # n-th cluster chain
        self.vco = self.pos % self.boot.cluster # offset in it

        vcn = 0
        for start, count in list(self.runs.items()):
            # if current VCN is in run
            if vcn <= self.vcn < vcn+count:
                lcn = start + self.vcn - vcn
                #~ print "Chain%08X: mapped VCN %d to LCN %Xh (LBA %Xh)"%(self.start, self.vcn, lcn, self.boot.cl2offset(lcn))
                if DEBUG&4:
                    log("Chain%08X: mapped VCN %d to LCN %Xh (%d), LBA %Xh", self.start, self.vcn, lcn, lcn, self.boot.cl2offset(lcn))
                    log("Chain%08X: seeking cluster offset %Xh (%d)", self.start, self.vco, self.vco)
                self.stream.seek(self.boot.cl2offset(lcn)+self.vco)
                self.lastvlcn = (self.vcn, lcn)
                #~ print "Set lastvlcn", self.lastvlcn
                return
            vcn += count
        if DEBUG&4: log("Chain%08X: reached chain's end seeking VCN %Xh", self.start, self.vcn)

    def read(self, size=-1):
        if DEBUG&4: log("Chain%08X: read(%d) called from offset %Xh (%d) of %d", self.start, size, self.pos, self.pos, self.filesize)
        # If negative size, set it to file size
        if size < 0:
            size = self.filesize
        # If requested size is greater than file size, limit to the latter
        if self.pos + size > self.filesize:
            size = self.filesize - self.pos
            if size < 0: size = 0
        if DEBUG&4: log("Chain%08X: adjusted size is %d", self.start, size)
        buf = bytearray()
        if not size:
            if DEBUG&4: log("Chain%08X: returning empty buffer", self.start)
            return buf
        self.seek(self.pos) # coerce real stream to the right position!
        if self.nofat: # contiguous clusters
            buf += self.stream.read(size)
            self.pos += size
            if DEBUG&4: log("Chain%08X: read %d contiguous bytes @VCN %Xh [%X:%X]", self.start, len(buf), self.vcn, self.vco, self.vco+size)
            return buf
        while 1:
            if not size: break
            n = min(size, self.maxrun4len(size)-self.vco)
            buf += self.stream.read(n)
            size -= n
            self.pos += n
            if DEBUG&4: log("Chain%08X: read %d (%d) bytes @VCN %Xh [%X:%X]", self.start, n, len(buf), self.vcn, self.vco, self.vco+n)
            self.seek(self.pos)
        return buf

    def write(self, s):
        if not s: return
        if DEBUG&4: log("Chain%08X: write(buf[:%d]) called from offset %Xh (%d), VCN %Xh(%d)[%Xh:]", self.start, len(s), self.pos, self.pos, self.vcn, self.vcn, self.vco)
        new_allocated = 0
        if self.pos + len(s) > self.size:
            # Alloc more clusters from actual last one
            # reqb=requested bytes, reqc=requested clusters
            reqb = self.pos + len(s) - self.size
            reqc = (reqb+self.boot.cluster-1)//self.boot.cluster
            if DEBUG&4:
                log("pos=%X(%d), len=%d, size=%d(%Xh)", self.pos, self.pos, len(s), self.size, self.size)
                log("required %d byte(s) [%d cluster(s)] more to write", reqb, reqc)
            self._alloc(reqc)
            new_allocated = 1
        # force lastvlcn update (needed on allocation)
        self.seek(self.pos)
        if self.nofat: # contiguous clusters
            self.stream.write(s)
            if DEBUG&4: log("Chain%08X: %d bytes fully written", self.start, len(s))
            self.pos += len(s)
            # file size is the top pos reached during write
            self.filesize = max(self.filesize, self.pos)
            return
        size=len(s) # bytes to do
        i=0 # pos in buffer
        while 1:
            if not size: break
            n = min(size, self.maxrun4len(size)-self.vco) # max bytes to complete run
            self.stream.write(s[i:i+n])
            size-=n
            i+=n
            self.pos += n
            if DEBUG&4: log("Chain%08X: written %d bytes (%d of %d) @VCN %d [%Xh:%Xh]", self.start, n, i, len(s), self.vcn, self.vco, self.vco+n)
            self.seek(self.pos)
        self.filesize = max(self.filesize, self.pos)
        if new_allocated and (not self.fat.exfat or self.isdirectory):
            # When allocating a directory table, it is strictly necessary that only the first byte in
            # an empty slot (the first) is set to NULL
            if self.pos < self.size:
                if DEBUG&4: log("Chain%08X: blanking newly allocated cluster tip, %d bytes @0x%X", self.start, self.size-self.pos, self.pos)
                self.stream.write(bytearray(self.size - self.pos))

    def trunc(self):
        "Truncates the clusters chain to the current one, freeing the rest"
        x = self.pos//self.boot.cluster # last VCN (=actual) to set
        n = (self.size+self.boot.cluster-1)//self.boot.cluster - x - 1 # number of clusters to free
        if DEBUG&4: log("%s: truncating @VCN %d, freeing %d clusters", self, x, n)
        if not n:
            if DEBUG&4: log("nothing to truncate!")
            return 1
        #~ print "%s: truncating @VCN %d, freeing %d clusters. %d %d" % (self, x, n, self.pos, self.size)
        #~ print "Start runs:\n", self.runs
        # Updates chain and virtual stream sizes
        self.size = (x+1)*self.boot.cluster
        self.filesize = self.pos
        while 1:
            if not n: break
            start, length = self.runs.popitem()
            if n >= length:
                #~ print "Zeroing %d from %d" % (length, start)
                if self.fat.exfat:
                    self.boot.bitmap.free1(start, length)
                else:
                    self.fat.mark_run(start, length, True)
                if n == length and (not self.fat.exfat or len(self.runs) > 1):
                    k = list(self.runs.keys())[-1]
                    self.fat[k+self.runs[k]-1] = self.fat.last
                n -= length
            else:
                #~ print "Zeroing %d from %d, last=%d" % (n, start+length-n, start+length-n-1)
                if self.fat.exfat:
                    self.boot.bitmap.free1(start+length-n, n)
                else:
                    self.fat.mark_run(start+length-n, n, True)
                if len(self.runs) or not self.fat.exfat:
                    # Set new last cluster
                    self.fat[start+length-n-1] = self.fat.last
                self.runs[start] = length-n
                n=0
        #~ print "Final runs:\n", self.runs
        #~ for start, length in self.runs.items():
            #~ for i in range(length):
                #~ print "Cluster %d=%d"%(start+i, self.fat[start+i])
        self.nofat = (len(self.runs)==1)
        return 0

    def frags(self):
        if DEBUG&4:
            log("Fragmentation of %s", self)
            log("Detected %d fragments for %d clusters", len(self.runs), self.size//self.boot.cluster)
            log("Fragmentation is %f", float(len(self.runs)-1) // float(self.size//self.boot.cluster))
        return len(self.runs)



class FixedRoot(object):
    "Handles the FAT12/16 fixed root table like a file"
    def __init__ (self, boot, fat):
        self.stream = boot.stream
        self.boot = boot
        self.fat = fat
        self.start = boot.root()
        self.size = 32*boot.wMaxRootEntries
        self.pos = 0 # virtual stream linear pos

    def __str__ (self):
        return "Fixed root @%Xh" % self.start

    def tell(self): return self.pos

    def realtell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        if whence == 1:
            pos = self.pos + offset
        elif whence == 2:
            if self.size:
                pos = self.size + offset
        else:
            pos = offset
        if pos > self.size:
            if DEBUG&4: log("Attempt to seek @%Xh past fixed root end @%Xh", pos, self.size)
            return
        self.pos = pos
        if DEBUG&4: log("FixedRoot: seeking @%Xh (@%Xh)", pos, self.start+pos)
        self.stream.seek(self.start+pos)

    def read(self, size=-1):
        if DEBUG&4: log("FixedRoot: read(%d) called from offset %Xh", size, self.pos)
        self.seek(self.pos)
        # If negative size, adjust
        if size < 0:
            size = 0
            if self.size: size = self.size
        # If requested size is greater than file size, limit to the latter
        if self.size and self.pos + size > self.size:
            size = self.size - self.pos
        buf = bytearray()
        if not size: return buf
        buf += self.stream.read(size)
        self.pos += size
        return buf

    def write(self, s):
        if DEBUG&4: log("FixedRoot: writing %d bytes at offset %Xh", len(s), self.pos)
        self.seek(self.pos)
        if self.pos + len(s) > self.size:
            return
        self.stream.write(s)
        self.pos += len(s)
        self.seek(self.pos)

    def trunc(self):
        return 0

    def frags(self):
        pass


class Handle(object):
    "Manages an open table slot"
    def __init__ (self):
        self.IsValid = False # determines whether update or not on disk
        self.File = None # file contents
        self.Entry = None # direntry slot
        self.Dir = None #dirtable owning the handle
        self.IsReadOnly = True # use this to prevent updating a Direntry on a read-only filesystem
        #~ atexit.register(self.close) # forces close() on exit if user didn't call it

    #~ def __del__ (self):
        #~ self.close()

    def update_time(self, i=0):
        cdate, ctime = FATDirentry.GetDosDateTime()
        if i == 0:
            self.Entry.wADate = cdate
        elif i == 1:
            self.Entry.wMDate = cdate
            self.Entry.wMTime = ctime

    def tell(self):
        return self.File.tell()

    def seek(self, offset, whence=0):
        self.File.seek(offset, whence)

        self.Entry.dwFileSize = self.File.filesize
        self.Dir._update_dirtable(self.Entry)

    def read(self, size=-1):
        self.update_time()
        return self.File.read(size)

    def write(self, s):
        self.File.write(s)
        self.update_time(1)
        self.IsReadOnly = False

        self.Entry.dwFileSize = self.File.filesize
        self.Dir._update_dirtable(self.Entry)

    # NOTE: FAT permits chains with more allocated clusters than those required by file size!
    # Distinguish a ftruncate w/deallocation and update Chain.__init__ and Handle flushing accordingly!
    def ftruncate(self, length, free=0):
        "Truncates a file to a given size (eventually allocating more clusters), optionally unlinking clusters in excess."
        self.File.seek(length)
        self.File.filesize = length

        self.Entry.dwFileSize = self.File.filesize
        self.Dir._update_dirtable(self.Entry)

        if not free:
            return 0
        return self.File.trunc()

    def close(self):
        # 20170608: RE-DESIGN CAREFULLY THE FULL READ-ONLY MATTER!
        if not self.IsValid:
            if DEBUG&4: log("Handle.close rejected %s (EINV)", self.File)
            return
        elif self.IsReadOnly:
            if DEBUG&4: log("Handle.close rejected %s (ERDO)", self.File)
            return

        # Force setting the start cluster if allocated on write
        self.Entry.Start(self.File.start)

        if not self.Entry.IsDir():
            if self.Entry._buf[-32] == 0xE5 and self.Entry.Start():
                if DEBUG&4: log("Deleted file: deallocating cluster(s)")
                self.File.fat.free(self.Entry.Start())
                # updates the Dirtable cache: mandatory if we allocated on write
                # (or start cluster won't be set)
                self.Dir._update_dirtable(self.Entry)
                return

            self.Entry.dwFileSize = self.File.filesize

        self.Dir.stream.seek(self.Entry._pos)
        if DEBUG&4: log('Closing Handle @%Xh(%Xh) to "%s", cluster=%Xh tell=%d chain=%d size=%d', \
        self.Entry._pos, self.Dir.stream.realtell(), os.path.join(self.Dir.path,self.Entry.Name()), self.Entry.Start(), self.File.pos, self.File.size, self.File.filesize)
        self.Dir.stream.write(self.Entry.pack())
        self.IsValid = False
        self.Dir._update_dirtable(self.Entry)
        if self in self.Dir.filetable: self.Dir.filetable.remove(self) # update list of opened files


class Direntry(object):
    pass

DirentryType = type(Direntry())
HandleType = type(Handle())


class FATDirentry(Direntry):
    "Represents a FAT direntry of one or more slots"

    "Represents a 32 byte FAT (not exFAT) slot"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sName', '8s'),
    0x08: ('sExt', '3s'),
    0x0B: ('chDOSPerms', 'B'), # bit: 0=R(ead only) 1=H(idden) 2=S(ystem) 3=Volume Label 4=D(irectory) 5=A(rchive)
    0x0C: ('chFlags', 'B'), # bit 3/4 set: lowercase basename/extension (NT)
    0x0D: ('chReserved', 'B'), # creation time fine resolution in 10 ms units, range 0-199
    0x0E: ('wCTime', '<H'),
    0x10: ('wCDate', '<H'),
    0x12: ('wADate', '<H'),
    0x14: ('wClusterHi', '<H'),
    0x16: ('wMTime', '<H'),
    0x18: ('wMDate', '<H'),
    0x1A: ('wClusterLo', '<H'),
    0x1C: ('dwFileSize', '<I') }

    "Represents a 32 byte FAT LFN slot"
    layout_lfn = { # { offset: (name, unpack string) }
    0x00: ('chSeqNumber', 'B'), # LFN slot #
    0x01: ('sName5', '10s'),
    0x0B: ('chDOSPerms', 'B'), # always 0xF
    0x0C: ('chType', 'B'), # always zero in VFAT LFN
    0x0D: ('chChecksum', 'B'),
    0x0E: ('sName6', '12s'),
    0x1A: ('wClusterLo', '<H'), # always zero
    0x1C: ('sName2', '4s') }

    def __init__ (self, s, pos=-1):
        self._i = 0
        self._buf = s
        self._pos = pos
        self._kv = {}
        for k in self.layout:
            self._kv[k-32] = self.layout[k]
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        s = b''
        keys = list(self._kv.keys())
        keys.sort()
        for k in keys:
            v = self._kv[k]
            s += struct.pack(v[1], getattr(self, v[0]))
        self._buf[-32:] = bytearray(s) # update always non-LFN part
        return self._buf

    def __str__ (self):
        s = "FAT %sDirentry @%Xh\n" % ( ('','LFN ')[self.IsLfn()], self._pos )
        return utils.class2str(self, s)

    def IsLfn(self):
        return self._buf[0x0B] == 0x0F and self._buf[0x0C] == self._buf[0x1A] == self._buf[0x1B] == 0

    def IsDeleted(self):
        return self._buf[0] == 0xE5

    def IsDir(self, value=-1):
        "Gets or sets the slot's Dir DOS permission"
        if value != -1:
            self._buf[-21] = value
        return (self._buf[-21] & 0x10) == 0x10

    def IsLabel(self, mark=0):
        "Gets or sets the slot's Label DOS permission"
        if mark:
            self._buf[0x0B] = 0x08
        return self._buf[0x0B] == 0x08

    def Start(self, cluster=None):
        "Gets or sets cluster WORDs in slot"
        if cluster != None:
            self.wClusterHi = cluster >> 16
            self.wClusterLo = cluster & 0xFFFF
        return (self.wClusterHi<<16) | self.wClusterLo

    def LongName(self):
        if not self.IsLfn():
            return ''
        i = len(self._buf)-64
        ln = b''
        while i >= 0:
            ln += self._buf[i+1:i+1+10] + \
            self._buf[i+14:i+14+12] + \
            self._buf[i+28:i+28+4]
            i -= 32
        ln = ln.decode('utf-16le')
        i = ln.find('\x00') # ending NULL may be omitted!
        if i < 0:
            return ln
        else:
            return ln[:i]

    def ShortName(self):
        return self.GetShortName(self._buf[-32:-21], self.chFlags)

    def Name(self):
        return self.LongName() or self.ShortName()

    @staticmethod
    def ParseDosDate(wDate):
        "Decodes a DOS date WORD into a tuple (year, month, day)"
        return (wDate>>9)+1980, (wDate>>5)&0xF, wDate&0x1F

    @staticmethod
    def ParseDosTime(wTime):
        "Decodes a DOS time WORD into a tuple (hour, minute, second)"
        return wTime>>11, (wTime>>5)&0x3F, wTime&0x1F

    @staticmethod
    def MakeDosTime(t):
        "Encodes a tuple (hour, minute, second) into a DOS time WORD"
        return (t[0] << 11) | (t[1] << 5) | (t[2]//2)

    @staticmethod
    def MakeDosDate(t):
        "Encodes a tuple (year, month, day) into a DOS date WORD"
        return ((t[0]-1980) << 9) | (t[1] << 5) | (t[2])

    @staticmethod
    def GetDosDateTime(format=0):
        "Returns a 2 WORDs tuple (DOSDate, DOSTime) or a DWORD, representing DOS encoding of current datetime"
        tm = time.localtime()
        cdate = ((tm[0]-1980) << 9) | (tm[1] << 5) | (tm[2])
        ctime = (tm[3] << 11) | (tm[4] << 5) | (tm[5]//2)
        if format:
            return cdate<<16 | ctime # DWORD
        else:
            return (cdate, ctime)

    @staticmethod
    def GenRawShortName(name):
        "Generates an old-style 8+3 DOS short name"
        name, ext = os.path.splitext(name)
        chFlags = 0
        if not ext and name in ('.', '..'): # special case
            name = '%-11s' % name
        elif 1 <= len(name) <= 8 and len(ext) <= 4:
            if ext and ext[0] == '.':
                ext = ext[1:]
            if name.islower():
                chFlags |= 8
            if ext.islower():
                chFlags |= 16
            name = '%-8s%-3s' % (name, ext)
            name = name.upper()
        if DEBUG&4: log("GenRawShortName returned %s:%d",name,chFlags)
        return name, chFlags

    @staticmethod
    def GetShortName(shortname, chFlags=0):
        "Makes a human readable short name from slot's one"
        if DEBUG&4: log("GetShortName got %s:%d",shortname,chFlags)
        if type(shortname) != str:
            shortname = shortname.decode(VFS_ENCODING) # fix b'XXXXXX~1\xfaTH'
        name = shortname[:8].rstrip()
        if chFlags & 0x8: name = name.lower()
        ext = shortname[8:].rstrip()
        if chFlags & 0x16: ext = ext.lower()
        if DEBUG&4: log("GetShortName returned %s:%s",name,ext)
        if not ext: return name
        return name + '.' + ext

    @staticmethod
    def GenRawShortFromLongName(name, id=1):
        "Generates a DOS 8+3 short name from a long one (Windows 95 style)"
        # Replaces valid LFN chars prohibited in short name
        nname = name.replace(' ', '')
        # CAVE! Multiple dots?
        for c in '[]+,;=':
            nname = nname.replace(c, '_')
        nname, ext = os.path.splitext(nname)
        #~ print nname, ext
        # If no replacement and name is short (LIBs -> LIBS)
        if len(nname) < 9 and nname in name and ext in name:
            short = ('%-8s%-3s' % (nname, ext[1:4])).upper()
            if DEBUG&4: log("GenRawShortFromLongName (0) returned %s",short)
            return short
        # Windows 9x: ~1 ... ~9999... as needed
        tilde = '~%d' % id
        i = 8 - len(tilde)
        if i > len(nname): i = len(nname)
        short = ('%-8s%-3s' % (nname[:i] + tilde, ext[1:4])).upper()
        if DEBUG&4: log("GenRawShortFromLongName (1) returned %s",short)
        return short

    @staticmethod
    def GenRawShortFromLongNameNT(name, id=1):
        "Generates a DOS 8+3 short name from a long one (NT style)"
        if id < 5: return FATDirentry.GenRawShortFromLongName(name, id)
        #~ There's an higher probability of generating an unused alias at first
        #~ attempt, and an alias mathematically bound to its long name
        crc = crc32(name.encode()) & 0xFFFF
        longname = name
        name, ext = os.path.splitext(name)
        tilde = '~%d' % (id-4)
        i = 6 - len(tilde)
        # Windows NT 4+: ~1...~4; then: orig chars (1 or 2)+some CRC-16 (4 chars)+~1...~9
        # Expands tilde index up to 999.999 if needed like '95
        shortname = ('%-8s%-3s' % (name[:2] + hex(crc)[::-1][:i] + tilde, ext[1:4])).upper()
        if DEBUG&4: log("Generated NT-style short name %s for %s", shortname, longname)
        return shortname

    def GenRawSlotFromName(self, shortname, longname=None):
        # Is presence of invalid (Unicode?) chars checked?
        shortname, chFlags = self.GenRawShortName(shortname)

        cdate, ctime = self.GetDosDateTime()

        self._buf = bytearray(struct.pack(b'<11s3B7HI', bytes(shortname, FS_ENCODING), 0x20, chFlags, 0, ctime, cdate, cdate, 0, ctime, cdate, 0, 0))

        if longname:
            longname = longname.encode('utf_16_le')
            if len(longname) > 510:
                raise FATException("Long name '%s' is >255 characters!" % longname)
            csum = self.Checksum(shortname)
            # If the last slot isn't filled, we must NULL terminate
            if len(longname) % 26:
                longname += b'\x00\x00'
            # And eventually pad with 0xFF, also
            if len(longname) % 26:
                longname += b'\xFF'*(26 - len(longname)%26)
            slots = len(longname)//26
            B=bytearray()
            while slots:
                b = bytearray(32)
                b[0] = slots
                j = (slots-1)*26
                b[1:11] = longname[j: j+10]
                b[11] = 0xF
                b[13] = csum
                b[14:27] = longname[j+10: j+22]
                b[28:32] = longname[j+22: j+26]
                B += b
                slots -= 1
            B[0] = B[0] | 0x40 # mark the last slot (first to appear)
            self._buf = B+self._buf

    @staticmethod
    def IsShortName(name):
        "Checks if name is an old-style 8+3 DOS short name"
        is_8dot3 = False
        name, ext = os.path.splitext(name)
        if not ext and name in ('.', '..'): # special case
            is_8dot3 = True
        # name.txt or NAME.TXT --> short
        # Name.txt or name.Txt etc. --> long (preserve case)
        # NT: NAME.txt or name.TXT or name.txt (short, bits 3-4 in 0x0C set accordingly)
        # tix8.4.3 --> invalid short (name=tix8.4, ext=.3)
        # dde1.3 --> valid short, (name=dde1, ext=.3)
        elif 1 <= len(name) <= 8 and len(ext) <= 4 and (name==name.upper() or name==name.lower()):
            if FATDirentry.IsValidDosName(name):
                is_8dot3 = True
        return is_8dot3

    special_short_chars = ''' "*/:<>?\|[]+.,;=''' + ''.join([chr(c) for c in range(32)])
    special_lfn_chars = '''"*/:<>?\|''' + ''.join([chr(c) for c in range(32)])

    @staticmethod
    def IsValidDosName(name, lfn=False):
        if name[0] == '\xE5': return False
        if lfn:
            special = FATDirentry.special_lfn_chars
        else:
            special = FATDirentry.special_short_chars
        for c in special:
            if c in name:
                return False
        return True

    def Match(self, name):
        "Checks if given short or long name matches with this slot's name"
        n =name.lower()
        # File.txt (LFN) == FILE.TXT == file.txt (short with special bits set) etc.
        if n == self.LongName().lower() or n == self.ShortName().lower(): return True
        return False

    @staticmethod
    def Checksum(name):
        "Calculates the 8+3 DOS short name LFN checksum"
        sum = 0
        for c in name:
            sum = ((sum & 1) << 7) + (sum >> 1) + ord(c)
            sum &= 0xff
        return sum



class Dirtable(object):
    "Manages a FAT12/16/32 directory table"
    def __init__(self, boot, fat, startcluster, size=0, path='.'):
        self.parent = None # parent device/partition container of root FS
        if type(boot) == HandleType:
            self.handle = boot # It's a directory handle
            self.boot = self.handle.File.boot
            self.fat = self.handle.File.fat
            self.start = self.handle.File.start
            self.stream = self.handle.File
        else:
            # non-zero size is a special case for fixed FAT12/16 root
            self.boot = boot
            self.fat = fat
            self.start = startcluster
        self.path = path
        self.needs_compact = 1
        if startcluster == size == 0: # FAT12/16 fixed root
            self.stream = FixedRoot(boot, fat)
            self.fixed_size = self.stream.size
        else:
            tot, last = fat.count(startcluster)
            self.stream = Chain(boot, fat, startcluster, (boot.cluster*tot, size)[size>0], end=last)
            self.stream.isdirectory = 1 # signals to blank cluster tips
        if path == '.':
            self.dirtable = {} # This *MUST* be propagated from root to descendants! 
            self.boot.dirtable = self.dirtable
            atexit.register(self.flush)
        else:
            self.dirtable = self.boot.dirtable
        if startcluster not in self.dirtable:
            self.dirtable[startcluster] = {'LFNs':{}, 'Names':{}, 'Handle':None, 'slots_map':{}, 'Open':[]} # LFNs key MUST be Unicode!
        #~ if DEBUG&4: log("Global directory table is '%s':", self.dirtable)
        self.map_slots()
        self.filetable = self.dirtable[startcluster]['Open']
        self.closed = False

    def __str__ (self):
        s = "Directory table @LCN %X (LBA %Xh)" % (self.start, self.boot.cl2offset(self.start))
        return s
        
    def _checkopen(self):
        if self.closed:
            raise FATException('Requested operation on a closed Dirtable!')

    def getdiskspace(self):
        "Returns the disk free space in a tuple (clusters, bytes)"
        free_bytes = self.fat.free_clusters * self.boot.cluster
        return (self.fat.free_clusters, free_bytes)

    def wipefreespace(self):
        "Zeroes free clusters"
        buf = (4<<20) * b'\x00'
        fourmegs = (4<<20)//self.boot.cluster
        for start, length in self.fat.free_clusters_map.items():
            if DEBUG&4: log("Wiping %d clusters from cluster #%d", length, start)
            self.boot.stream.seek(self.boot.cl2offset(start))
            while length:
                q = min(length, fourmegs)
                self.boot.stream.write(buf[:q*self.boot.cluster])
                length -= q
        
    def open(self, name):
        "Opens the chain corresponding to an existing file name"
        self._checkopen()
        res = Handle()
        if type(name) != DirentryType:
            root, fname = os.path.split(name)
            if root:
                root = self.opendir(root)
                if not root:
                    raise FATException('Could not open "%s", file not found!'%name)
                    #~ return res
            else:
                root = self
            e = root.find(fname)
        else: # We assume it's a Direntry if not a string
            e = name
        if e:
            # Ensure it is not a directory or volume label
            if e.IsDir() or e.IsLabel():
                return res
            res.IsValid = True
            # If cluster is zero (empty file), then we must allocate one:
            # or Chain won't work!
            res.File = Chain(self.boot, self.fat, e.Start(), e.dwFileSize)
            res.Entry = e
            res.Dir = self
            self.filetable += [res]
        return res

    def opendir(self, name):
        """Opens an existing relative directory path beginning in this table and
        return a new Dirtable object or None if not found"""
        self._checkopen()
        name = name.replace('/','\\')
        path = name.split('\\')
        found = self
        parent = self # records parent dir handle
        for com in path:
            e = found.find(com)
            if e and e.IsDir():
                parent = found
                found = Dirtable(self.boot, self.fat, e.Start(), path=os.path.join(found.path, com))
                continue
            found = None
            break
        if found:
            if DEBUG&4: log("Opened directory table '%s' @LCN %Xh (LBA %Xh)", found.path, found.start, self.boot.cl2offset(found.start))
            if self.dirtable[found.start]['Handle']:
                # Opened many, closed once!
                found.handle = self.dirtable[found.start]['Handle']
                if DEBUG&4: log("retrieved previous directory Handle %s", found.handle)
                # We must update the Chain stream associated with the unique Handle,
                # or size variations will be discarded!
                found.stream = found.handle.File
            else:
                res = Handle()
                res.IsValid = True
                res.IsReadOnly = (self.boot.stream.mode != 'r+b')
                res.IsDirectory = 1
                res.File = found.stream
                res.File.isdirectory = 1
                res.Entry = e
                res.Dir = parent
                found.handle = res
                self.dirtable[found.start]['Handle'] = res
        #~ if not found:
            #~ raise FATException('Could not open "%s", directory not found!'%name)
        return found

    def _alloc(self, name, clusters=0):
        "Allocates a new Direntry slot (both file/directory)"
        if len(os.path.join(self.path, name))+2 > 260:
            raise FATException("Can't add '%s' to directory table '%s', pathname >260!"%(name, self.path))
        dentry = FATDirentry(bytearray(32))
        # If name is a LFN, generate a short one valid in this table
        if not FATDirentry.IsShortName(name):
            i = 1
            short = FATDirentry.GetShortName(FATDirentry.GenRawShortFromLongNameNT(name, i))
            while self.find(short):
                i += 1
                short = FATDirentry.GetShortName(FATDirentry.GenRawShortFromLongNameNT(name, i))
            dentry.GenRawSlotFromName(short, name)
        else:
            dentry.GenRawSlotFromName(name)

        res = Handle()
        res.IsValid = True
        res.IsReadOnly = (self.boot.stream.mode != 'r+b')
        res.File = Chain(self.boot, self.fat, 0)
        if clusters:
            # Force clusters allocation
            res.File.seek(clusters*self.boot.cluster)
            res.File.seek(0)
        dentry._pos = self.findfree(len(dentry._buf))
        dentry.Start(res.File.start)
        res.Entry = dentry
        return res

    def create(self, name, prealloc=0):
        "Creates a new file chain and the associated slot. Erase pre-existing filename."
        e = self.open(name)
        if e.IsValid:
            e.IsValid = False
            self.erase(name)
        # Check if it is a supported name (=at least valid LFN)
        if not FATDirentry.IsValidDosName(name, True):
            raise FATException("Invalid characters in name '%s'" % name)
        handle = self._alloc(name, prealloc)
        self.stream.seek(handle.Entry._pos)
        self.stream.write(handle.Entry.pack())
        handle.Dir = self
        self._update_dirtable(handle.Entry)
        if DEBUG&4: log("Created new file '%s' @%Xh", name, handle.File.start)
        self.filetable += [handle]
        return handle

    def mkdir(self, name):
        "Creates a new directory slot, allocating the new directory table"
        r = self.opendir(name)
        if r:
            if DEBUG&4: log("mkdir('%s') failed, entry already exists!", name)
            return r
        # Check if it is a supported name (=at least valid LFN)
        if not FATDirentry.IsValidDosName(name, True):
            if DEBUG&4: log("mkdir('%s') failed, name contains invalid chars!", name)
            return None
        handle = self._alloc(name, 1)
        self.stream.seek(handle.Entry._pos)
        handle.File.isdirectory = 1
        if DEBUG&4: log("Making new directory '%s' @%Xh", name, handle.File.start)
        handle.Entry.chDOSPerms = 0x10
        self.stream.write(handle.Entry.pack())
        handle.Dir = self
        # PLEASE NOTE: Windows 10 opens a slot as directory and works regularly
        # even if table does not start with dot entries: but CHKDSK corrects it!
        # . in new table
        dot = FATDirentry(bytearray(32), 0)
        dot.GenRawSlotFromName('.')
        dot.Start(handle.Entry.Start())
        dot.chDOSPerms = 0x10
        handle.File.write(dot.pack())
        # .. in new table
        dot = FATDirentry(bytearray(32), 32)
        dot.GenRawSlotFromName('..')
        # Non-root parent's cluster # must be set
        if self.path != '.':
            dot.Start(self.stream.start)
        dot.chDOSPerms = 0x10
        handle.File.write(dot.pack())
        handle.File.write(bytearray(self.boot.cluster-64)) # blank table
        self._update_dirtable(handle.Entry)
        handle.close()
        # Records the unique Handle to the directory
        self.dirtable[handle.File.start] = {'LFNs':{}, 'Names':{}, 'Handle':handle, 'slots_map':{64:(2<<20)//32-2}, 'Open':[]}
        #~ return Dirtable(handle, None, path=os.path.join(self.path, name))
        return self.opendir(name)

    def rmtree(self, name=None):
        "Removes a full directory tree"
        self._checkopen()
        if name:
            if DEBUG&4: log("rmtree:opening %s", name)
            target = self.opendir(name)
        else:
            target = self
        if not target:
            if DEBUG&4: log("rmtree:target '%s' not found!", name)
            return 0
        for it in target.iterator():
            n = it.Name()
            if it.IsDir():
                if n in ('.', '..'): continue
                target.opendir(n).rmtree()
            if DEBUG&4: log("rmtree:erasing '%s'", n)
            target.erase(n)
        del target
        if name:
            if DEBUG&4: log("rmtree:erasing '%s'", name)
            self.erase(name)
        return 1

    def closeh(self, handle):
        "Updates a modified entry in the table"
        handle.close()

    def close(self):
        "Closes all files and directories belonging to this Dirtable"
        # NOTE: with root, implicitly prepares the filesystem dismounting
        self._checkopen()
        self.flush()
        self.closed = 1

    def flush(self):
        "Closes all open handles and commits changes to disk"
        if self.path != '.':
            if DEBUG&4: log("Flushing dirtable for '%s'", self.path)
            dirs = {self.start: self.dirtable[self.start]}
        else:
            if DEBUG&4: log("Flushing root dirtable")
            dirs = self.dirtable
            atexit.unregister(self.flush)
        if not dirs:
            if DEBUG&4: log("No directories to flush!")
        for i in dirs:
            if not self.dirtable[i]['Open']:
                if DEBUG&4: log("No opened files!")
            for h in copy.copy(self.dirtable[i]['Open']): # the original list gets shrinked
               if DEBUG&4: log("Closing file handle for opened file '%s'", h.Entry.Name())
               h.close()
            h = self.dirtable[i]['Handle']
            if h:
                h.close()
                h.IsValid = False

    def map_compact(self):
        "Compacts, eventually reordering, a slots map"
        if not self.needs_compact: return
        #~ print "Map before:", sorted(self.dirtable[self.start]['slots_map'].iteritems())
        map_changed = 0
        while 1:
            M = self.dirtable[self.start]['slots_map']
            d=copy.copy(M)
            for k,v in sorted(M.items()):
                while d.get(k+32*v): # while contig runs exist, merge
                    v1 = d.get(k+32*v)
                    if DEBUG&4: log("Compacting map: {%d:%d} -> {%d:%d}", k,v,k,v+v1)
                    d[k] = v+v1
                    del d[k+32*v]
                    #~ print "Compacted {%d:%d} -> {%d:%d}" %(k,v,k,v+v1)
                    #~ print sorted(d.iteritems())
                    v+=v1
            if self.dirtable[self.start]['slots_map'] != d:
                self.dirtable[self.start]['slots_map'] = d
                map_changed = 1
                continue
            break
        self.needs_compact = 0
        #~ print "Map after:", sorted(self.dirtable[self.start]['slots_map'].iteritems())

    def map_slots(self):
        "Fills the free slots map and file names table once at first access"
        if not self.dirtable[self.start]['slots_map']:
            self.stream.seek(0)
            pos = 0
            s = ''
            while True:
                first_free = -1
                run_length = -1
                buf = bytearray()
                while True:
                    s = self.stream.read(32)
                    if not s or not s[0]: break
                    if s[0] == 0xE5: # if erased
                        if first_free < 0:
                            first_free = pos
                            run_length = 0
                        run_length += 1
                        pos += 32
                        continue
                    # if not, and we record an erased slot...
                    if first_free > -1:
                        self.dirtable[self.start]['slots_map'][first_free] = run_length
                        first_free = -1
                    if s[0x0B] == 0x0F and s[0x0C] == s[0x1A] == s[0x1B] == 0: # LFN
                        buf += s
                        pos += 32
                        continue
                    # if normal, in-use slot
                    buf += s
                    pos += 32
                    self._update_dirtable(FATDirentry(buf, pos-len(buf)))
                    buf = bytearray()
                if not s or not s[0]:
                    # Maps unallocated space to max table size
                    if self.path == '.' and hasattr(self, 'fixed_size'): # FAT12/16 root
                        self.dirtable[self.start]['slots_map'][pos] = (self.fixed_size - pos)//32
                    else:
                        self.dirtable[self.start]['slots_map'][pos] = ((2<<20) - pos)//32
                    break
            self.map_compact()
            if DEBUG&4:
                log("%s collected slots map: %s", self, self.dirtable[self.start]['slots_map'])
                log("%s dirtable: %s", self, self.dirtable[self.start])
        
    # Assume table free space is zeroed
    def findfree(self, length=32):
        "Returns the offset of the first free slot or requested slot group size (in bytes)"
        length //= 32 # convert length in slots
        if DEBUG&4: log("%s: findfree(%d) in map: %s", self, length, self.dirtable[self.start]['slots_map'])
        for start in sorted(self.dirtable[self.start]['slots_map']):
            rl = self.dirtable[self.start]['slots_map'][start]
            if length > 1 and length > rl: continue
            del self.dirtable[self.start]['slots_map'][start]
            if length < rl:
                self.dirtable[self.start]['slots_map'][start+32*length] = rl-length # updates map
            if DEBUG&4: log("%s: found free slot @%d, updated map: %s", self, start, self.dirtable[self.start]['slots_map'])
            return start
        # FAT table limit is 2 MiB or 65536 slots (65534 due to "." and ".." entries)
        # So it can hold max 65534 files (all with short names)
        # FAT12&16 root have significantly smaller size (typically 224 or 512*32)
        raise FATException("Directory table of '%s' has reached its maximum extension!" % self.path)

    def iterator(self):
        "Iterates through directory table slots, generating a FATDirentry for each one"
        self._checkopen()
        told = self.stream.tell()
        buf = bytearray()
        s = 1
        pos = 0
        while s:
            self.stream.seek(pos)
            s = self.stream.read(32)
            pos += 32
            if not s or s[0] == 0: break
            if s[0] == 0xE5: continue # skip erased
            if s[0x0B] == 0x0F and s[0x0C] == s[0x1A] == s[0x1B] == 0: # LFN
                buf += s
                continue
            buf += s
            yield FATDirentry(buf, self.stream.tell()-len(buf))
            buf = bytearray()
        self.stream.seek(told)

    def _update_dirtable(self, it, erase=False):
        "Updates internal cache of object names and their associated slots"
        if DEBUG&4:
            log("_update_dirtable (erase=%d) for %s", erase, it)
            log("_update_dirtable: short alias is %s", it.ShortName().lower())
        if erase:
            del self.dirtable[self.start]['Names'][it.ShortName().lower()]
            ln = it.LongName()
            if ln:
                del self.dirtable[self.start]['LFNs'][ln.lower()]
            return
        self.dirtable[self.start]['Names'][it.ShortName().lower()] = it
        ln = it.LongName()
        if ln:
            self.dirtable[self.start]['LFNs'][ln.lower()] = it

    def find(self, name):
        "Finds an entry by name. Returns it or None if not found"
        # Create names cache
        if not self.dirtable[self.start]['Names']:
            self.map_slots()
        if DEBUG&4:
            log("find: searching for %s (%s lower-cased)", name, name.lower())
            log("find: LFNs=%s", self.dirtable[self.start]['LFNs'])
        name = name.lower()
        return self.dirtable[self.start]['LFNs'].get(name) or \
        self.dirtable[self.start]['Names'].get(name)

    def erase(self, name):
        "Marks a file's slot as erased and free the corresponding cluster chain"
        self._checkopen()
        if type(name) == DirentryType:
            e = name
        else:
            e = self.find(name)
            if not e:
                return 0
        if e.IsDir():
            it = self.opendir(e.Name()).iterator()
            next(it); next(it)
            if next in it:
                if DEBUG&4: log("Can't erase non empty directory slot @%d (pointing at #%d)", e._pos, e.Start())
                return 0
        start = e.Start()
        if start in self.dirtable and self.dirtable[start]['Handle']:
            if DEBUG&4: log("Marking open Handle for %Xh as invalid", start)
            self.dirtable[start]['Handle'].IsValid = False # 20190413: prevents post-mortem updating
        e.Start(0)
        e.dwFileSize = 0
        self._update_dirtable(e, True)
        for i in range(0, len(e._buf), 32):
            e._buf[i] = 0xE5
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        self.dirtable[self.start]['slots_map'][e._pos] = len(e._buf)//32 # updates slots map
        self.map_compact()
        if start:
            self.fat.free(start)
        if DEBUG&4:
            log("Erased slot '%s' @%Xh (pointing at LCN %Xh)", name, e._pos, start)
            log("Mapped new free slot {%d: %d}", e._pos, len(e._buf)//32)
        return 1

    def rename(self, name, newname):
        "Renames a file or directory slot"
        self._checkopen()
        if type(name) == DirentryType:
            e = name
        else:
            e = self.find(name)
            if not e:
                if DEBUG&4: log("Can't find file to rename: '%'s", name)
                return 0
        if self.find(newname):
            if DEBUG&4: log("Can't rename, file exists: '%s'", newname)
            return 0
        # Alloc new slot
        ne = self._alloc(newname)
        if not ne:
            if DEBUG&4: log("Can't alloc new file slot for '%s'", newname)
            return 0
        # Copy attributes from old to new slot
        ne.Entry._buf[-21:] = e._buf[-21:]
        # Write new entry
        self.stream.seek(ne.Entry._pos)
        self.stream.write(ne.Entry._buf)
        ne.IsValid = False
        if DEBUG&4: log("'%s' renamed to '%s'", name, newname)
        self._update_dirtable(ne.Entry)
        self._update_dirtable(e, True)
        # Mark the old one as erased
        for i in range(0, len(e._buf), 32):
            e._buf[i] = 0xE5
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        return 1

    def clean(self, shrink=False):
        "Compacts used slots and blanks unused ones, optionally shrinking the table"
        if DEBUG&4: log("Cleaning directory table %s with keep sort function", self.path)
        #~ return self.sort(lambda x:x, shrink) # keep order
        return self.sort(None, shrink) # keep order

    def stats(self):
        "Prints informations about slots in this directory table"
        in_use = 0
        count = 0
        for e in self.iterator():
            count+=1
            in_use+=len(e._buf)
        print("%s: %d entries in %d slots on %d allocated" % (self.path, count, in_use//32, self.stream.size//32))
        
    @staticmethod
    def _sortby(a, b):
        """Helper function for functools.cmp_to_key (Python 3): it sorts
        according to a user provided list in '_sortby.fix' member; order of
        unknown items is preserved."""
        X = Dirtable._sortby.fix
        if a not in X: return 1
        if b not in X: return -1
        if X.index(a) < X.index(b): return -1
        if X.index(a) > X.index(b): return 1
        return 0

    def sort(self, by_func=None, shrink=False):
        """Sorts the slot entries alphabetically or applying by_func, compacting
        them and zeroing unused ones. Optionally shrinks table. Returns a tuple
        (used slots, blank slots) or (-1, -1) if there are open handles."""
        self._checkopen()
        if self.filetable: return (-1, -1) # there are open handles, can't sort
        if DEBUG&4: log("%s: table size at beginning: %d", self.path, self.stream.size)
        d = {}
        names = []
        for e in self.iterator():
            if e.IsLabel(): d[0] = e # if label, assign a special key
            n = e.Name()
            if n in ('.', '..'): continue
            d[n] = e
            names+=[n]
        if by_func:
            names = sorted(names, key=functools.cmp_to_key(by_func))
        else:
            names = sorted(names, key=str.lower) # default sorting: alphabetical, case insensitive
        if self.path == '.':
            self.stream.seek(0)
            if 0 in d: self.stream.write(d[0]._buf) # write label
        else:
            self.stream.seek(64) # preserves dot entries
        for name in names:
            self.stream.write(d[name]._buf) # re-writes ordered slots
        last = self.stream.tell()
        unused = self.stream.size - last
        self.stream.write(bytearray(unused)) # blank unused area
        if DEBUG&4: log("%s: sorted %d slots, blanked %d", self.path, last//32, unused//32)
        if shrink:
            c_alloc = (self.stream.size+self.boot.cluster-1)//self.boot.cluster
            c_used = (last+self.boot.cluster-1)//self.boot.cluster
            if c_used < c_alloc:
                self.stream.seek(last)
                self.stream.trunc()
                if DEBUG&4: log("Shrank directory table freeing %d clusters", c_alloc-c_used)
                unused -= (c_alloc-c_used//32)
            else:
                if DEBUG&4: log("Can't shrink directory table, free space < 1 cluster!")
        # Rebuilds Dirtable caches
        #~ self.slots_map = {}
        # Rebuilds Dirtable caches
        self.dirtable[self.start] = {'LFNs':{}, 'Names':{}, 'Handle':None, 'slots_map':{}, 'Open':[]}
        self.map_slots()
        return last//32, unused//32

    def listdir(self):
        "Returns a list of file and directory names in this directory, sorted by on disk position"
        it = []
        for o in self.iterator():
            if o.IsLabel(): continue
            it += [o.Name()]
        return it

    def walk(self):
        """Walks across this directory and its childs. For each visited directory,
        returns a tuple (root, dirs, files) sorted in disk order. """
        dirs = []
        files = []
        for o in self.iterator():
            if o.IsLabel(): continue
            n = o.Name()
            if n in ('.', '..'): continue
            if o.IsDir():
                dirs += [n]
            else:
                files += [n]
        yield self.path, dirs, files
        for subdir in dirs:
            for a,b,c in self.opendir(subdir).walk():
                yield a, b, c

    def attrib(self, name, perms=('-A',)):
        "Changes the DOS permissions on a table entry. Accepts perms tuple [+-][AHRS]"
        self._checkopen()
        mask = {'R':0, 'H':1, 'S':2, 'A':5}
        e = self.find(name)
        if not e: return 0
        for perm in perms:
            if len(perm) < 2 or perm[0] not in ('+','-') or perm[1].upper() not in mask:
                raise FATException("Bad permission string", perm)
            if perm[0] == '-':
                e.chDOSPerms &= ~(1 << mask[perm[1].upper()])
            else:
                e.chDOSPerms |= (1 << mask[perm[1].upper()])
        if DEBUG&4: log("Updating permissions on '%s' with code=%X", name, e.chDOSPerms)
        self.stream.seek(e._pos)
        self.stream.write(e.pack())
        return 1

    def label(self, name=None):
        "Gets or sets volume label. Pass an empty string to clear."
        self._checkopen()
        if self.path != '.':
            raise FATException("A volume label can be assigned in root directory only")
        if name and len(name) > 11:
            raise FATException("A volume label can't be longer than 11 characters")
        if name and not FATDirentry.IsValidDosName(name):
            raise FATException("Volume label contains invalid characters")

        for e in self.iterator():
            if e.IsLabel():
                if name == None: # get mode
                    return e.Name()
                elif name == '':
                    e._buf[0] = 0xE5 # cleared
                else:
                    e._buf[:11] = bytes('%-11s' % name.upper(), 'ascii')
                # Writes new entry
                self.stream.seek(e._pos)
                self.stream.write(e._buf)
                return name

        if name == None: return
        e = FATDirentry(bytearray(32))
        e._pos = self.findfree(32) # raises or returns!
        e._buf[11] = 0x8 # Volume label attribute
        e._buf[:11] = bytes('%-11s' % name.upper(), 'ascii') # Label
        e._buf[22:26] = struct.pack('<I', e.GetDosDateTime(1)) # Creation time (CHKDSK)
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        self._update_dirtable(e)
        return name



         #############################
        # HIGH LEVEL HELPER ROUTINES #
        ############################


def fat_copy_clusters(boot, fat, start):
    """Duplicate a cluster chain copying the cluster contents to another position.
    Returns the first cluster of the new chain."""
    count = fat.count(start)[0]
    src = Chain(boot, fat, start, boot.cluster*count)
    target = fat.alloc(count)[0] # possibly defragmented
    dst = Chain(boot, fat, target, boot.cluster*count)
    if DEBUG&4: log("Copying %s to %s", src, dst)
    s = 1
    while s:
        s = src.read(boot.cluster)
        dst.write(s)
    return target
