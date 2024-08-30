# -*- coding: cp1252 -*-
# Utilities to manage an exFAT  file system
#

import sys, copy, os, struct, time, io, atexit, functools
from datetime import datetime
from collections import OrderedDict
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

from FATtools.debug import log
from FATtools import utils
from FATtools.FAT import FAT, Chain

if DEBUG&8: import hexdump

FS_ENCODING = sys.getfilesystemencoding()

class exFATException(Exception):
    pass


class boot_exfat(object):
    "exFAT boot sector"
    layout = { # { offset: (nome, stringa di unpack) }
    0x00: ('chJumpInstruction', '3s'),
    0x03: ('chOemID', '8s'),
    0x0B: ('chDummy', '53s'),
    0x40: ('u64PartOffset', '<Q'),
    0x48: ('u64VolumeLength', '<Q'), # length of partition (sectors) where FS is applied (unlike FAT)
    0x50: ('dwFATOffset', '<I'), # sectors
    0x54: ('dwFATLength', '<I'), # sectors
    0x58: ('dwDataRegionOffset', '<I'), # sectors
    0x5C: ('dwDataRegionLength', '<I'), # clusters
    0x60: ('dwRootCluster', '<I'), # cluster index
    0x64: ('dwVolumeSerial', '<I'),
    0x68: ('wFSRevision', '<H'), # 0x100 or 1.00
    # bit 0: active FAT & Bitmap (0=first, 1=second)
    # bit 1: volume is dirty? (0=clean)
    # bit 2: media failure (0=none, 1=some I/O failed)
    0x6A: ('wFlags', '<H'), # field not included in VBR checksum
    0x6C: ('uchBytesPerSector', 'B'), # 2 exponent
    0x6D: ('uchSectorsPerCluster', 'B'), # 2 exponent
    0x6E: ('uchFATCopies', 'B'), # 1 by default
    0x6F: ('uchDriveSelect', 'B'),
    0x70: ('uchPercentInUse', 'B'), # field not included in VBR checksum
    0x71: ('chReserved', '7s'),
    0x1FE: ('wBootSignature', '<H') } # Size = 0x200 (512 byte)

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
        if not self.uchBytesPerSector: return
        # Cluster size (bytes)
        self.cluster = (1 << self.uchBytesPerSector) * (1 << self.uchSectorsPerCluster)
        # FAT offset
        self.fatoffs = self.dwFATOffset * (1 << self.uchBytesPerSector) + self._pos
        # Clusters in the Data region
        self.fatsize = self.dwDataRegionLength
        # Data region offset (=cluster #2)
        self.dataoffs = self.dwDataRegionOffset * (1 << self.uchBytesPerSector) + self._pos
        self.checkvbr()

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self.__init2__()
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "exFAT Boot sector @%x\n" % self._pos)

    def clusters(self):
        "Returns the number of clusters in the data area"
        # Total sectors minus sectors preceding the data area
        return self.fatsize

    def cl2offset(self, cluster):
        "Returns a real cluster offset"
        return self.dataoffs + (cluster-2)*self.cluster

    def root(self):
        "Root offset"
        return self.cl2offset(self.dwRootCluster)

    def checkvbr(self):
        "Calculates and compares VBR checksum to test VBR integrity"
        if not self.stream: return 0
        sector = 1 << self.uchBytesPerSector
        self.stream.seek(0)
        s = self.stream.read(sector*11)
        calc_crc = self.GetChecksum(s)
        s = self.stream.read(sector) # checksum sector
        stored_crc = struct.unpack('<I',s[:4])[0]
        if calc_crc != stored_crc:
            raise exFATException("FATAL: exFAT Volume Boot Region is corrupted, bad checksum!")
        
    @staticmethod
    def GetChecksum(s, UpCase=False):
        "Computates the checksum for the VBR sectors (the first 11) or the UpCase table"
        hash = 0
        for i in range(len(s)):
            if not UpCase and i in (106, 107, 112): continue
            hash = (((hash<<31) | (hash >> 1)) & 0xFFFFFFFF) + s[i] # 10.3.19: when called from test_inject (VBR read into) it is a *string* buffer instead of bytearray: investigate!
            hash &= 0xFFFFFFFF
        return hash


def upcase_expand(s):
    "Expands a compressed Up-Case table"
    i = 0
    expanded_i = 0
    tab = []
    # print "Processing compressed table of %d bytes" % len(s)
    while i < len(s):
        word = struct.unpack('<H', s[i:i+2])[0]
        if word == 0xFFFF and i+2 < len(s):
            # print "Found compressed run at 0x%X (%04X)" % (i, expanded_i)
            word = struct.unpack('<H', s[i+2:i+4])[0]
            # print "Expanding range of %04X chars from %04X to %04X" % (word, expanded_i, expanded_i+word)
            for j in range(expanded_i, expanded_i+word):
                tab += [struct.pack('<H', j)]
            i += 4
            expanded_i += word
        else:
            # print "Decoded uncompressed char at 0x%X (%04X)" % (i, expanded_i)
            tab += [s[i:i+2]]
            i += 2
            expanded_i += 1
    return bytearray().join(tab)



class Bitmap(Chain):
    def __init__ (self, boot, fat, cluster, size=0):
        self.isdirectory=False
        self.runs = OrderedDict() # RLE map of fragments
        self.stream = boot.stream
        self.boot = boot
        self.fat = fat
        self.start = cluster # start cluster or zero if empty
        # Size in bytes of allocated cluster(s)
        if self.start:
            self.size = fat.count(cluster)[0]*boot.cluster
        self.filesize = size or self.size # file size, if available, or chain size
        self.pos = 0 # virtual stream linear pos
        # Virtual Cluster Number (cluster index in this chain)
        self.vcn = -1
        # Virtual Cluster Offset (current offset in VCN)
        self.vco = -1
        self.lastvlcn = (0, cluster) # last cluster VCN & LCN
        self.last_free_alloc = 2
        self.nofat = False
        # Bitmap always uses FAT, even if contig, but is fixed size
        self.size == self.maxrun4len(self.size)
        self.free_clusters = None # tracks free clusters number
        self.free_clusters_map = None
        self.free_clusters_flag = 0 # set if map needs compacting
        self.map_free_space()
        if DEBUG&8: log("exFAT Bitmap of %d bytes (%d clusters) @%Xh", self.filesize, self.boot.dwDataRegionLength, self.start)

    def __str__ (self):
        return "exFAT Bitmap of %d bytes (%d clusters) @%Xh" % (self.filesize, self.boot.dwDataRegionLength, self.start)

    def map_free_space(self):
        "Maps the free clusters in an ordered dictionary {start_cluster: run_length}"
        self.free_clusters_map = {}
        FREE_CLUSTERS=0
        # Bitmap could reach 512M!
        PAGE = 1<<20
        END_OF_CLUSTERS = (self.boot.dwDataRegionLength+7)//8
        REMAINDER = 8*END_OF_CLUSTERS - self.boot.dwDataRegionLength
        i = 0 # address of cluster #2
        self.seek(i)
        while i < END_OF_CLUSTERS:
            s = self.read(min(PAGE, END_OF_CLUSTERS-i)) # slurp full bitmap, or 1M page
            if DEBUG&8: log("map_free_space: loaded Bitmap page of %d bytes @0x%X", len(s), i)
            j=0
            LENGTH = len(s)*8
            while j < LENGTH:
                first_free = -1
                run_length = -1
                while j < LENGTH:
                    # Most common case should be all-0|1
                    Q = j//8; R = j%8
                    if not R: # if byte start
                        if not s[Q]: # if empty byte
                            if first_free < 0:
                                first_free = j+2+i*8
                                run_length = 0
                            run_length += 8
                            j+=8
                            continue
                        if s[Q]==0xFF: # if full byte
                            if run_length > 0: break
                            j+=8
                            continue
                    if s[Q] & (1 << R): # test middle bit
                        if run_length > 0: break
                        j+=1
                        continue
                    if first_free < 0:
                        first_free = j+2+i*8
                        run_length = 0
                    run_length += 1
                    j+=1
                if first_free < 0: continue
                FREE_CLUSTERS+=run_length
                self.free_clusters_map[first_free] =  run_length
                if DEBUG&8: log("map_free_space: appended run (%d, %d)", first_free, run_length)
            i += len(s) # advance to next Bitmap page to examine
        if REMAINDER:
            if DEBUG&8: log("map_free_space: Bitmap rounded by %d bits, correcting total and last run count", REMAINDER)
            FREE_CLUSTERS -= REMAINDER
            last = self.free_clusters_map.popitem()
            run_length = last[1]-REMAINDER # subtracts bits processed in excess
            if run_length > 0:
                self.free_clusters_map[last[0]] =  run_length
        self.free_clusters = FREE_CLUSTERS
        if DEBUG&8: log("map_free_space: %d clusters free in %d run(s)", FREE_CLUSTERS, len(self.free_clusters_map))
        return FREE_CLUSTERS, len(self.free_clusters_map)

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
                    if DEBUG&8: log("Compacting free_clusters_map: {%d:%d} -> {%d:%d}", k,v,k,v+v1)
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
        if DEBUG&8: log("Free space map - %d run(s): %s", len(self.free_clusters_map), self.free_clusters_map)
        #~ print "Map AFTER:", sorted(self.free_clusters_map.iteritems())
        
    def isset(self, cluster):
        "Tests if the bit corresponding to a given cluster is set"
        assert cluster > 1
        cluster-=2
        self.seek(cluster//8)
        B = self.read(1)[0]
        return (B & (1 << (cluster%8))) != 0

    def set(self, cluster, length=1, clear=False):
        "Sets or clears a bit or bits run"
        assert cluster > 1
        cluster-=2 # since bit zero represents cluster #2
        pos = cluster//8
        rem = cluster%8
        if DEBUG&8: log("set(%Xh,%d%s) start @0x%X:%d", cluster+2, length, ('',' (clear)')[clear!=False], pos, rem)
        self.seek(pos)
        if rem:
            B = self.read(1)[0]
            if DEBUG&8: log("got byte 0x%X", B)
            todo = min(8-rem, length)
            if clear:
                B &= ~((0xFF>>(8-todo)) << rem)
            else:
                B |= ((0xFF>>(8-todo)) << rem)
            self.seek(-1, 1)
            self.write(struct.pack('B',B))
            length -= todo
            if DEBUG&8: log("set byte 0x%X, remaining=%d", B, length)
        octets = length//8
        while octets:
            i = min(32768, octets)
            octets -= i
            if clear:
                self.write(bytearray(i))
            else:
                self.write(i*b'\xFF')
        rem = length%8
        if rem:
            if DEBUG&8: log("last bits=%d", rem)
            B = self.read(1)[0]
            if DEBUG&8: log("got B=0x%X", B)
            if clear:
                B &= ~(0xFF>>(8-rem))
            else:
                B |= (0xFF>>(8-rem))
            self.seek(-1, 1)
            self.write(struct.pack('B',B))
            if DEBUG&8: log("set B=0x%X", B)
    
    def findfree(self, count=0):
        """Returns index and length of the first free clusters run beginning from
        'start' or (-1,-1) in case of failure. If 'count' is given, limit the search
        to that amount."""
        if self.free_clusters_map == None:
            self.map_free_space()
        try:
            i, n = self.free_clusters_map.popitem()
        except KeyError:
            return -1, -1
        if DEBUG&8: log("Got run of %d free clusters from %d (%Xh)", n, i, i)
        if n-count > 0:
            self.free_clusters_map[i+count] = n-count # updates map
            if DEBUG&8: log("New free clusters map: %s", self.free_clusters_map)
        self.free_clusters-=min(n,count)
        return i, min(n, count)

    def findmaxrun(self, count=0):
        "Finds a run of at least count clusters or the greatest run available. Returns a tuple (total_free_clusters, (run_start, clusters))"
        t = self.last_free_alloc,0
        maxrun=(0,0)
        n=0
        while 1:
            t = self.findfree(t[0]+1, count)
            if t[0] < 0: break
            if DEBUG&8: log("Found %d free clusters from #%d", t[1], t[0])
            maxrun = max(t, maxrun, key=lambda x:x[1])
            n += t[1]
            if count and maxrun[1] >= count: break # break if we found the required run
            t = (t[0]+t[1], t[1])
        if DEBUG&8: log("Found the biggest run of %d clusters from #%d on %d total clusters", maxrun[1], maxrun[0], n)
        return n, maxrun

    def alloc(self, runs_map, count, params={}):
        """Allocates a set of free clusters, marking the FAT and/or the Bitmap.
        runs_map is the dictionary of previously allocated runs
        count is the number of clusters to allocate
        params is an optional dictionary of directives to tune the allocation (to be done). 
        Returns the last cluster or raise an exception in case of failure"""
        self.map_compact()

        if self.free_clusters < count:
            if DEBUG&8: log("Couldn't allocate %d cluster(s), only %d free", count, self.free_clusters)
            raise exFATException("FATAL! Free clusters exhausted, couldn't allocate %d, only %d left!" % (count, self.free_clusters))

        if DEBUG&8: log("Ok to allocate %d cluster(s), %d free", count, self.free_clusters)

        last_run = None
        
        while count:
            if runs_map:
                last_run = list(runs_map.items())[-1]
            i, n = self.findfree(count)
            if last_run and i == last_run[0]+last_run[1]: # if contiguous
                runs_map[last_run[0]] = n+last_run[1]
            else:
                runs_map[i] = n
            self.set(i, n) # sets the bitmap
            if len(runs_map) > 1: # if fragmented
                self.fat.mark_run(i, n) # marks the FAT also
                # if just got fragmented...
                if len(runs_map) == 2:
                    if not last_run:
                        last_run = list(runs_map.items())[0]
                    if DEBUG&8: log("Chain got fragmented, setting FAT for first fragment {%d (%Xh):%d}", last_run[0], last_run[0], last_run[1])
                    self.fat.mark_run(last_run[0], last_run[1]) # marks the FAT for 1st frag
                self.fat[last_run[0]+last_run[1]-1] = i # linkd prev chain with last
            last = i + n - 1 # last cluster in new run
            count -= n

        if len(runs_map) > 1:
            self.fat[last] = self.fat.last

        self.last_free_alloc = last

        if DEBUG&8: log("New runs map: %s", runs_map)
        return last

    def free1(self, start, length):
        "Frees the Bitmap only"
        self.free_clusters_flag = 1
        self.free_clusters += length
        self.free_clusters_map[start] = length
        self.set(start, length, True)
        #~ print "free set %X:%d clear" % (start, length)
        if DEBUG&8: log("free1: zeroing run of %d clusters from %Xh", length, start)
        
    def free(self, start, runs=None):
        "Frees the Bitmap following a clusters chain"
        if DEBUG&8: log("freeing cluster chain from %Xh", start)
        if runs:
            for start, count in list(runs.items()):
                self.free1(start, count)
            return
        while True:
            length, next = self.fat.count_run(start)
            #~ print "free: count_run(%Xh) returned %d, %Xh" %(start,length, next)
            if DEBUG&8:
                log("free: count_run returned %d, %Xh", length, next)
                log("free: zeroing run of %d clusters from %Xh (next=%Xh)", length, start, next)
            self.free1(start, length) # clears bitmap only, FAT can be dirty
            if next==self.fat.last: break
            start = next



class Handle(object):
    "Manage an open table slot"
    def __init__ (self):
        self.IsValid = False # determine whether update or not on disk
        self.File = None # file contents
        self.Entry = None # direntry slot
        self.Dir = None #dirtable owning the handle
        self.IsReadOnly = True
        self.IsDirectory = False
        #~ atexit.register(self.close)
        if DEBUG&8: log("Registering new empty Handle")

    def update_time(self, i=0):
        cdatetime, ms = exFATDirentry.GetDosDateTimeEx()
        if i == 0:
            self.Entry.dwATime = cdatetime
            self.Entry.chmsATime = ms
        elif i == 1:
            self.Entry.dwMTime = cdatetime
            self.Entry.chmsMTime = ms

    def tell(self):
        return self.File.tell()

    def seek(self, offset, whence=0):
        self.File.seek(offset, whence)
        # If alloc on write
        if not self.Entry.dwStartCluster:
            self.Entry.Start(self.File.start)
        # If it gets fragmented
        self.Entry.IsContig(self.File.nofat)
        self.Dir._update_dirtable(self.Entry)

    def read(self, size=-1):
        self.update_time()
        return self.File.read(size)

    def write(self, s):
        if self.IsReadOnly:
            raise exFATException("Can't write, filesystem was opened in Read-Only mode!")
        if not self.IsValid:
            raise exFATException("Can't write, invalid Handle!")
        self.File.write(s)
        self.update_time(1)
        #~ self.IsReadOnly = False
        # If alloc on write
        if not self.Entry.dwStartCluster:
            self.Entry.Start(self.File.start)
        # If it gets fragmented
        self.Entry.IsContig(self.File.nofat)
        self.Dir._update_dirtable(self.Entry)

    def ftruncate(self, length, free=0):
        "Truncates a file to a given size (eventually allocating more clusters), optionally unlinking clusters in excess."
        if self.IsReadOnly:
            raise exFATException("Can't truncate, filesystem was opened in Read-Only mode!")
        self.File.seek(length)
        self.File.filesize = length

        self.Entry.u64ValidDataLength = self.File.filesize
        self.Entry.u64DataLength = self.File.filesize
        # Here we don't know if it is contig!
        #~ self.IsReadOnly = False
        self.Dir._update_dirtable(self.Entry)
        if not free:
            return 0
        return self.File.trunc()

    def close(self):
        # 20170608: RE-DESIGN CAREFULLY THE FULL READ-ONLY MATTER!
        if not self.IsValid:
            if DEBUG&8: log("Handle.close rejected %s (EINV)", self.File)
            return
        if self.IsReadOnly:
            if DEBUG&8: log("Handle.close rejected %s (ERDO)", self.File)
            return
        # Force setting the start cluster if allocated on write
        self.Entry.Start(self.File.start)
        # Force setting the fragmentation bit
        self.Entry.IsContig(self.File.nofat)

        # If got fragmented at run time
        if self.File.nofat:
            self.Entry.chSecondaryFlags |= 2
        else:
            if self.Entry.chSecondaryFlags & 2:
                self.Entry.chSecondaryFlags ^= 2

        if not self.Entry.IsDir():
            if self.Entry.IsDeleted() and self.Entry.Start():
                if DEBUG&8: log("Deleted file: deallocating cluster(s)")
                if self.File.nofat:
                    self.File.boot.bitmap.free1(self.Entry.Start(), (self.File.filesize+self.File.boot.cluster-1)//self.File.boot.cluster)
                else:
                    self.File.boot.bitmap.free(self.Entry.Start(), self.File.runs)
                self.IsValid = False
                return

            self.Entry.u64ValidDataLength = self.File.filesize
            self.Entry.u64DataLength = self.File.filesize
        else:
            self.Entry.u64ValidDataLength = self.File.size
            self.Entry.u64DataLength = self.File.size

        self.Dir.stream.seek(self.Entry._pos)
        if DEBUG&8: log('Closing Handle @%Xh(%Xh) to %s "%s", cluster=%Xh tell=%d chain=%d size=%d', \
        self.Entry._pos, self.Dir.stream.realtell(), ('file','directory')[self.Entry.IsDir()], os.path.join(self.Dir.path,self.Entry.Name()), self.Entry.Start(), self.File.pos, self.File.size, self.File.filesize)
        self.Dir.stream.write(self.Entry.pack())
        if not self.Entry.IsDir():
            self.IsValid = False
        if DEBUG&8 > 1: log("Handle close wrote:\n%s", hexdump.hexdump(self.Entry._buf,'return'))
        self.Dir._update_dirtable(self.Entry)
        if self in self.Dir.filetable: self.Dir.filetable.remove(self) # update list of opened files



class Direntry(object):
    pass

DirentryType = type(Direntry())
HandleType = type(Handle())


class exFATDirentry(Direntry):
    "Represent an exFAT direntry of one or more slots"

    "Represent a 32 byte exFAT slot"
    # chEntryType bit 7: 0=unused entry, 1=active entry
    volume_label_layout = {
    0x00: ('chEntryType', 'B'), # 0x83, 0x03
    0x01: ('chCount', 'B'), # Label length (max 11 chars)
    0x02: ('sVolumeLabel', '22s'),
    0x18: ('sReserved', '8s') }

    bitmap_layout = {
    0x00: ('chEntryType', 'B'), # 0x81, 0x01
    0x01: ('chFlags', 'B'), # bit 0: 0=1st bitmap, 1=2nd bitmap (T-exFAT only)
    0x02: ('sReserved', '18s'),
    0x14: ('dwStartCluster', '<I'), # typically cluster #2
    0x18: ('u64DataLength', '<Q')	} # bitmap length in bytes

    upcase_layout = {
    0x00: ('chEntryType', 'B'), # 0x82, 0x02
    0x01: ('sReserved1', '3s'),
    0x04: ('dwChecksum', '<I'),
    0x08: ('sReserved2', '12s'),
    0x14: ('dwStartCluster', '<I'),
    0x18: ('u64DataLength', '<Q')	}

    volume_guid_layout = {
    0x00: ('chEntryType', 'B'), # 0xA0, 0x20
    0x01: ('chSecondaryCount', 'B'),
    0x02: ('wChecksum', '<H'),
    0x04: ('wFlags', '<H'),
    0x06: ('sVolumeGUID', '16s'),
    0x16: ('sReserved', '10s') }

    texfat_padding_layout = {
    0x00: ('chEntryType', 'B'), # 0xA1, 0x21
    0x01: ('sReserved', '31s') }

    # A file entry slot group is made of a File Entry slot, a Stream Extension slot and
    # one or more Filename Extension slots
    file_entry_layout = {
    0x00: ('chEntryType', 'B'), # 0x85, 0x05
    0x01: ('chSecondaryCount', 'B'), # other slots in the group (2 minimum, max 18)
    0x02: ('wChecksum', '<H'), # slots group checksum
    0x04: ('wFileAttributes', '<H'), # usual MS-DOS file attributes (0x10 = DIR, etc.)
    0x06: ('sReserved2', '2s'),
    0x08: ('dwCTime', '<I'), # date/time in canonical MS-DOS format
    0x0C: ('dwMTime', '<I'),
    0x10: ('dwATime', '<I'),
    0x14: ('chmsCTime', 'B'), # 10-milliseconds unit (0...199)
    0x15: ('chmsMTime', 'B'),
    0x16: ('chtzCTime', 'B'), # Time Zone in 15' increments (0x80=UTC, ox84=CET, 0xD0=DST)
    0x17: ('chtzMTime', 'B'),
    0x18: ('chtzATime', 'B'),
    0x19: ('sReserved2', '7s') }

    stream_extension_layout = {
    0x00: ('chEntryType', 'B'), # 0xC0, 0x40
    # bit 0: 1=can be allocated
    # bit 1: 1=contiguous contents, FAT is not used
    0x01: ('chSecondaryFlags', 'B'),
    0x02: ('sReserved1', 's'),
    0x03: ('chNameLength', 'B'), # max 255 (but Python 2.7.10 Win32 can't access more than 242!)
    0x04: ('wNameHash', '<H'), # hash of the UTF-16, uppercased filename
    0x06: ('sReserved2', '2s'),
    0x08: ('u64ValidDataLength', '<Q'), # should be real file size
    0x10: ('sReserved3', '4s'),
    0x14: ('dwStartCluster', '<I'),
    0x18: ('u64DataLength', '<Q') } # should be allocated size: in fact, it seems they MUST be equal

    file_name_extension_layout = {
    0x00: ('chEntryType', 'B'), # 0xC1, 0x41
    0x01: ('chSecondaryFlags', 'B'),
    0x02: ('sFileName', '30s') }

    slot_types = {
    0x00: ({0x00: ('sRAW','32s')}, "Unknown"),
    0x01: (bitmap_layout, "Allocation Bitmap"),
    0x02: (upcase_layout, "Upcase Table"),
    0x03: (volume_label_layout, "Volume Label"),
    0x05: (file_entry_layout, "File Entry"),
    0x20: (volume_guid_layout, "Volume GUID"),
    0x21: (texfat_padding_layout, "T-exFAT padding"),
    0x40: (stream_extension_layout, "Stream Extension"),
    0x41: (file_name_extension_layout, "Filename Extension") }

    def __init__ (self, s, pos=-1):
        self._i = 0
        self._buf = s
        self._pos = pos
        self._kv = {}
        self.type = self._buf[0] & 0x7F
        if self.type == 0 or self.type not in self.slot_types:
            if DEBUG&8: log("Unknown slot type: %Xh", self.type)
        self._kv = self.slot_types[self.type][0].copy() # select right slot type
        self._name = self.slot_types[self.type][1]
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        if self.type == 5:
            for k in (1,3,4,8,0x14,0x18):
                self._kv[k+32] = self.stream_extension_layout[k]
                self._vk[self.stream_extension_layout[k][0]] = k+32
        #~ if DEBUG&8: log("Decoded %s", self)

    __getattr__ = utils.common_getattr

    def __str__ (self):
        return utils.class2str(self, "%s @%x\n" % (self._name, self._pos))

    def pack(self):
        "Update internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        if self.type == 5:
            self.wChecksum = self.GetSetChecksum(self._buf) # update the slots set checksum
            self._buf[2:4] = struct.pack('<H', self.wChecksum)
        if DEBUG&8 > 1: log("Packed %s", self)
        return self._buf

    @staticmethod
    def DatetimeParse(dwDatetime):
        "Decodes a datetime DWORD into a tuple"
        wDate = (dwDatetime & 0xFFFF0000) >> 16
        wTime = (dwDatetime & 0x0000FFFF)
        return (wDate>>9)+1980, (wDate>>5)&0xF, wDate&0x1F, wTime>>11, (wTime>>5)&0x3F, wTime&0x1F, 0, None

    @staticmethod
    def MakeDosDateTimeEx(t):
        "Encode a tuple into a DOS datetime DWORD"
        cdate = ((t[0]-1980) << 9) | (t[1] << 5) | (t[2])
        ctime = (t[3] << 11) | (t[4] << 5) | (t[5]//2)
        tms = 0
        if t[5] % 2: tms += 100 # odd DOS seconds
        return (cdate<<16 | ctime), tms

    @staticmethod
    def GetDosDateTimeEx():
        "Return a tuple with a DWORD representing DOS encoding of current datetime and 10 milliseconds exFAT tuning"
        tm = datetime.now()
        cdate = ((tm.year-1980) << 9) | (tm.month << 5) | (tm.day)
        ctime = (tm.hour << 11) | (tm.minute << 5) | (tm.second//2)
        tms = tm.microsecond//10000
        if tm.second % 2: tms += 100 # odd DOS seconds
        return (cdate<<16 | ctime), tms

    def IsContig(self, value=0):
        if value:
            self.chSecondaryFlags |= 2
        else:
            return bool(self.chSecondaryFlags & 2)

    def IsDeleted(self):
        return self._buf[0] & 0x80 != 0x80

    def IsDir(self, value=-1):
        "Get or set the slot's Dir DOS permission"
        if value != -1:
            self.wFileAttributes = value
        return (self.wFileAttributes & 0x10) == 0x10

    def IsLabel(self, mark=0):
        "Get or set the slot's Label DOS permission"
        return self.type == 3

    special_lfn_chars = r'"*/:<>?\|' + ''.join([chr(c) for c in range(32)])

    @staticmethod
    def IsValidDosName(name):
        for c in exFATDirentry.special_lfn_chars:
            if c in name:
                return False
        return True

    def Start(self, cluster=None):
        "Get or set cluster WORDs in slot"
        if cluster != None:
            self.dwStartCluster = cluster
        return self.dwStartCluster

    def Name(self):
        "Decodes the file name"
        ln = ''
        if self.type == 5:
            i = 64
            while i < len(self._buf):
                ln += self._buf[i+2:i+32].decode('utf-16le')
                i += 32
            return ln[:self.chNameLength]
        return ln

    @staticmethod
    def GetNameHash(name):
        "Computate the Stream Extension file name hash (UTF-16 LE encoded)"
        hash = 0
        # 'à' == 'à'.upper() BUT u'à' != u'à'.upper()
        # NOTE: UpCase table SHOULD be used to determine upper cased chars
        # valid in a volume. Windows 10 leaves Unicode surrogate pairs untouched,
        # thus allowing to represent more than 64K chars. Windows 10 Explorer
        # and PowerShell ISE can display such chars, CMD and PowerShell only
        # handle them.
        name = name.decode('utf_16_le').upper().encode('utf_16_le') 
        for c in name:
            hash = (((hash<<15) | (hash >> 1)) & 0xFFFF) + c
            hash &= 0xFFFF
        return hash

    @staticmethod
    def GetSetChecksum(s):
        "Computate the checksum for a set of slots (primary and secondary entries)"
        hash = 0
        for i in range(len(s)):
            if i == 2 or i == 3: continue
            hash = (((hash<<15) | (hash >> 1)) & 0xFFFF) + s[i]
            hash &= 0xFFFF
        return hash

    def GenRawSlotFromName(self, name):
        "Generate the exFAT slots set corresponding to a given file name"
        # File Entry part
        # a Stream Extension and a File Name Extension slot are always present
        self.chSecondaryCount = 1 + (len(name)+14)//15
        self.wFileAttributes = 0x20
        ctime, cms = self.GetDosDateTimeEx()
        self.dwCTime = self.dwMTime = self.dwATime = ctime
        self.chmsCTime = self.chmsMTime = self.chmsATime = cms
        # Stream Extension part
        self.chSecondaryFlags = 1 # base value, to show the entry could be allocated
        name = name.encode('utf_16_le')
        self.chNameLength = len(name)//2
        self.wNameHash = self.GetNameHash(name)

        self.pack()

        # File Name Extension(s) part
        i = len(name)
        k = 0
        while i:
            b = bytearray(32)
            b[0] = 0xC1
            j = min(30, i)
            b[2:2+j] = name[k:k+j]
            i-=j
            k+=j
            self._buf += b

        #~ if DEBUG&8: log("GenRawSlotFromName returned:\n%s", hexdump.hexdump(str(self._buf),'return'))

        return self._buf



class Dirtable(object):
    "Manages an exFAT directory table"
    def __init__(self, boot, fat, startcluster=0, size=0, nofat=0, path='.'):
        self.parent = None # parent device/partition container of root FS
        if type(boot) == HandleType:
            self.handle = boot # It's a directory handle
            self.boot = self.handle.File.boot
            self.fat = self.handle.File.fat
            self.start = self.handle.File.start
            self.stream = self.handle.File
        else:
            self.boot = boot
            self.fat = fat
            self.start = startcluster
            self.stream = Chain(boot, fat, startcluster, size, nofat)
        self.stream.isdirectory = 1 # signals to blank cluster tips (root too!)
        self.closed = 0
        self.path = path
        self.needs_compact = 1
        if path == '.':
            self.dirtable = {} # These *MUST* be propagated from root to descendants!
            self.boot.dirtable = self.dirtable
            atexit.register(self.flush)
        else:
            self.dirtable = self.boot.dirtable
        if self.start not in self.dirtable:
            # Names maps lowercased names and Direntry slots
            # Handle contains the unique Handle to the directory table
            # Open lists opened files
            self.dirtable[self.start] = {'Names':{}, 'Handle':None, 'slots_map':{}, 'Open':[]} # Names key MUST be Python Unicode!
            #~ if DEBUG&8: log("Global directory table is '%s':", self.dirtable)
            self.map_slots()
        #~ print self.dirtable
        self.filetable = self.dirtable[self.start]['Open']

    def __str__ (self):
        s = "Directory table @LCN %X (LBA %Xh)" % (self.start, self.boot.cl2offset(self.start))
        return s

    def _checkopen(self):
        if self.closed:
            raise exFATException('Requested operation on a closed Dirtable!')
            
    def getdiskspace(self):
        "Returns the disk free space in a tuple (clusters, bytes)"
        free_bytes = self.boot.bitmap.free_clusters * self.boot.cluster
        return (self.boot.bitmap.free_clusters, free_bytes)

    def wipefreespace(self):
        "Zeroes free clusters"
        buf = (4<<20) * b'\x00'
        fourmegs = (4<<20)//self.boot.cluster
        for start, length in self.boot.bitmap.free_clusters_map.items():
            if DEBUG&4: log("Wiping %d clusters from cluster #%d", length, start)
            self.boot.stream.seek(self.boot.cl2offset(start))
            while length:
                q = min(length, fourmegs)
                self.boot.stream.write(buf[:q*self.boot.cluster])
                length -= q

    def open(self, name):
        "Opens the slot corresponding to an existing file name"
        self._checkopen()
        res = Handle()
        if type(name) != DirentryType:
            if len(name) > 242: return res
            root, fname = os.path.split(name)
            if root:
                root = self.opendir(root)
                if not root:
                    return res
            else:
                root = self
            e = root.find(fname)
        else:
            e = name
        if e:
            # Ensure it is not a directory or volume label
            if e.IsDir() or e.IsLabel():
                return res
            res.IsValid = True
            res.File = Chain(self.boot, self.fat, e.Start(), e.u64DataLength, nofat=e.IsContig())
            res.IsReadOnly = (self.boot.stream.mode != 'r+b')
            res.IsDirectory = False
            res.Entry = e
            res.Dir = self
            self.filetable += [res]
            if DEBUG&8: log("open() made a new handle for file starting @%Xh", e.Start())
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
            if len(com) > 242: return None
            e = found.find(com)
            if e and e.IsDir():
                parent = found
                found = Dirtable(self.boot, self.fat, e.Start(), e.u64ValidDataLength, e.IsContig(), path=os.path.join(found.path, com))
                continue
            found = None
            break
        if found:
            if DEBUG&8: log("opened directory table '%s' @0x%X (cluster 0x%X)", found.path, self.boot.cl2offset(found.start), found.start)
            if self.dirtable[found.start]['Handle']:
                # Opened many, closed once!
                found.handle = self.dirtable[found.start]['Handle']
                if DEBUG&8: log("retrieved previous directory Handle %s", found.handle)
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
        return found

    def _alloc(self, name, clusters=0):
        "Allocates a new Direntry slot (both file/directory)"
        res = Handle()
        res.IsValid = True
        res.File = Chain(self.boot, self.fat, 0)
        res.IsReadOnly = (self.boot.stream.mode != 'r+b')
        if clusters:
            # Force clusters allocation
            res.File.seek(clusters*self.boot.cluster)
            res.File.seek(0)
        b = bytearray(64); b[0] = 0x85; b[32] = 0xC0
        dentry = exFATDirentry(b, -1)
        dentry.GenRawSlotFromName(name)
        dentry._pos = self.findfree(len(dentry._buf))
        dentry.Start(res.File.start)
        dentry.IsContig(res.File.nofat)
        res.Entry = dentry
        return res

    def create(self, name, prealloc=0):
        "Creates a new file chain and the associated slot. Erase pre-existing filename."
        if not exFATDirentry.IsValidDosName(name):
            raise exFATException("Invalid characters in name '%s'" % name)
        e = self.open(name)
        if e.IsValid:
            e.IsValid = False
            self.erase(name)
        handle = self._alloc(name, prealloc)
        self.stream.seek(handle.Entry._pos)
        self.stream.write(handle.Entry.pack())
        handle.Dir = self
        self._update_dirtable(handle.Entry)
        if DEBUG&8: log("Created new file '%s' @%Xh", name, handle.File.start)
        self.filetable += [handle]
        return handle

    def mkdir(self, name):
        "Creates a new directory slot, allocating the new directory table"
        r = self.opendir(name)
        if r:
            if DEBUG&8: log("mkdir('%s') failed, entry already exists!", name)
            return r
        # Check if it is a supported name
        if not exFATDirentry.IsValidDosName(name):
            if DEBUG&8: log("mkdir('%s') failed, name contains invalid chars!", name)
            return None
        handle = self._alloc(name, 1)
        handle.File.isdirectory = 1
        handle.IsDirectory = True
        self.stream.seek(handle.Entry._pos)
        if DEBUG&8: log("Making new directory '%s' @%Xh", name, handle.File.start)
        handle.Entry.wFileAttributes = 0x10
        handle.Entry.chSecondaryFlags |= 2 # since initially it has 1 cluster only
        handle.Entry.u64ValidDataLength = handle.Entry.u64DataLength = self.boot.cluster
        self.stream.write(handle.Entry.pack())
        handle.Dir = self
        handle.write(bytearray(self.boot.cluster)) # blank table
        self._update_dirtable(handle.Entry)
        # Records the unique Handle to the directory
        self.dirtable[handle.File.start] = {'Names':{}, 'Handle':handle, 'slots_map':{0:(256<<20)//32}, 'Open':[]}
        return Dirtable(handle, None, path=os.path.join(self.path, name))

    def rmtree(self, name=None):
        "Removes a full directory tree"
        self._checkopen()
        if name:
            if DEBUG&8: log("rmtree:opening %s", name)
            target = self.opendir(name)
        else:
            target = self
            if DEBUG&8: log("rmtree:using self: %s", target.path)
        if not target:
            if DEBUG&8: log("rmtree:target '%s' not found!", name)
            return 0
        for it in target.iterator():
            n = it.Name()
            if it.IsDir():
                target.opendir(n).rmtree()
            if DEBUG&8: log("rmtree:erasing '%s'", n)
            target.erase(n)
        #~ del target
        if name:
            if DEBUG&8: log("rmtree:finally erasing '%s'", name)
            self.erase(name)
        return 1

    def closeh(self, handle):
        "Updates a modified entry in the table"
        self._checkopen()
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
            if DEBUG&8: log("Flushing dirtable for '%s'", self.path)
            dirs = {self.start: self.dirtable[self.start]}
        else:
            if DEBUG&8: log("Flushing root dirtable")
            atexit.unregister(self.flush)
            dirs = self.dirtable
        if not dirs:
            if DEBUG&8: log("No directories to flush!")
        for i in dirs:
            if not self.dirtable[i]['Open']:
                if DEBUG&8: log("No opened files!")
            for h in copy.copy(self.dirtable[i]['Open']): # the original list gets shrinked
               if DEBUG&8: log("Closing file handle for opened file '%s'", h)
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
                    if DEBUG&8: log("Compacting map: {%d:%d} -> {%d:%d}", k,v,k,v+v1)
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
                count = 0
                while True:
                    s = self.stream.read(32)
                    if not s or not s[0]: break
                    if s[0] & 0x80 != 0x80: # if inactive
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
                    if s[0] & 0x7F in (0x5, 0x20): # composite slot
                        count = s[1] # slots to collect
                        buf += s
                        pos += 32
                        continue
                    if count:
                        count -= 1
                        buf += s
                        pos += 32
                        if count: continue
                    else:
                        buf += s
                        pos += 32
                    self._update_dirtable(exFATDirentry(buf, pos-len(buf)))
                    buf = bytearray()
                if not s or not s[0]:
                    # Maps unallocated space to max table size (256 MiB)
                    self.dirtable[self.start]['slots_map'][pos] = ((256<<20) - pos)//32
                    break
            self.needs_compact = 1
            self.stream.seek(0)
            if DEBUG&8:
                log("%s collected slots map: %s", self, self.dirtable[self.start]['slots_map'])
                log("%s dirtable: %s", self, self.dirtable[self.start])
        
    def findfree(self, length=32):
        "Returns the offset of the first free slot or requested slot group size (in bytes)"
        length //= 32 # convert length in slots
        if DEBUG&8: log("%s: findfree(%d) in map: %s", self, length, self.dirtable[self.start]['slots_map'])
        if self.needs_compact:
            self.map_compact()
        for start in sorted(self.dirtable[self.start]['slots_map']):
            rl = self.dirtable[self.start]['slots_map'][start]
            if length > 1 and length > rl: continue
            del self.dirtable[self.start]['slots_map'][start]
            if length < rl:
                self.dirtable[self.start]['slots_map'][start+32*length] = rl-length # updates map
            if DEBUG&8: log("%s: found free slot @%d, updated map: %s", self, start, self.dirtable[self.start]['slots_map'])
            return start
        # exFAT table limit is 256 MiB, about 2.8 mil. slots of minimum size (96 bytes)
        if DEBUG&8: log("%s: maximum table size reached!",self)
        raise exFATException("Directory table of '%s' has reached its maximum extension!" % self.path)

    def iterator(self):
        self._checkopen()
        told = self.stream.tell()
        buf = bytearray()
        s = 1
        pos = 0
        count = 0
        while s:
            self.stream.seek(pos)
            s = self.stream.read(32)
            pos += 32
            if not s or s[0] == 0: break
            if s[0] & 0x80 != 0x80: continue # unused slot
            if s[0] & 0x7F in (0x5, 0x20): # composite slot
                count = s[1] # slots to collect
                buf += s
                continue
            if count:
                count -= 1
                buf += s
                if count: continue
            else:
                buf += s
            yield exFATDirentry(buf, self.stream.tell()-len(buf))
            buf = bytearray()
            count = 0
        self.stream.seek(told)

    def _update_dirtable(self, it, erase=False):
        k = it.Name().lower()
        if erase:
            del self.dirtable[self.start]['Names'][k]
            return
        if DEBUG&8: log("updating Dirtable name cache with '%s'", k)
        self.dirtable[self.start]['Names'][k] = it

    def find(self, name):
        "Finds an entry by name. Returns it or None if not found"
        # Creates names cache
        if not self.dirtable[self.start]['Names']:
            self.map_slots()
        if DEBUG&8:
            log("find: searching for %s (%s lower-cased)", name, name.lower())
        name = name.lower()
        return self.dirtable[self.start]['Names'].get(name)

    def dump(self, n, range=3):
        "Returns the n-th slot in the table for debugging purposes"
        self.stream.seek(n*32)
        return self.stream.read(range*32)

    def erase(self, name):
        "Marks a file's slot as erased and free the corresponding clusters"
        self._checkopen()
        if type(name) == DirentryType:
            e = name
        else:
            e = self.find(name)
            if not e:
                return 0
        if e.IsDir():
            it = self.opendir(e.Name()).iterator()
            if next in it:
                if DEBUG&8: log("Can't erase non empty directory slot @%d (pointing at %Xh)", e._pos, e.Start())
                return 0
        start = e.Start()
        if DEBUG&8: log("Erasing slot @%d (pointing at %Xh)", e._pos, start)
        if start in self.dirtable:
            if DEBUG&8: log("Marking open Handle for %Xh as invalid", start)
            self.dirtable[start]['Handle'].IsValid = False # 20190413: prevents post-mortem updating
        #~ elif start in self.filetable:
            #~ if DEBUG&8: log("Removing Handle for %Xh from filetable", start)
            #~ del self.filetable[start]
        if start:
            if e.IsContig():
                # Free Bitmap directly
                
                if DEBUG&8: log("Erasing contig run of %d clusters from %Xh", (e.u64DataLength+self.boot.cluster-1)//self.boot.cluster, start)
                self.boot.bitmap.free1(start, (e.u64DataLength+self.boot.cluster-1)//self.boot.cluster)
            else:
                # Free Bitmap following the FAT
                if DEBUG&8: log("Fragmented contents, freeing FAT chain from %Xh", start)
                self.boot.bitmap.free(start)
        e.Start(0)
        e.chEntryType = 5 # set this, or pack resets to 0x85
        for i in range(0, len(e._buf), 32):
            e._buf[i] ^= (1<<7)
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        self.dirtable[self.start]['slots_map'][e._pos] = len(e._buf)//32 # updates slots map
        self.needs_compact = 1
        if DEBUG&8: log("Erased slot '%s' @%Xh (pointing at #%d)", name, e._pos, start)
        self._update_dirtable(e, True)
        return 1

    def rename(self, name, newname):
        "Renames a file or directory slot"
        self._checkopen()
        if type(name) == DirentryType:
            e = name
        else:
            e = self.find(name)
            if not e:
                if DEBUG&8: log("Can't find file to rename: '%'s", name)
                return 0
        if self.find(newname):
            if DEBUG&8: log("Can't rename, file exists: '%s'", newname)
            return 0
        # Alloc new slot
        ne = self._alloc(newname)
        if not ne:
            if DEBUG&8: log("Can't alloc new file slot for '%s'", newname)
            return 0
        # Copy attributes from old to new slot
        for k, v in list(e._kv.items()):
            if k in (1, 0x23, 0x24): continue # skip chSecondaryCount, chNameLength and wNameHash
            setattr(ne.Entry, v[0], getattr(e, v[0]))
        ne.Entry.pack()
        ne.IsValid = False
        e.chEntryType = 5 # set this, or pack resets to 0x85 (Open Handle)
        # Write new entry
        self.stream.seek(ne.Entry._pos)
        self.stream.write(ne.Entry._buf)
        if DEBUG&8: log("'%s' renamed to '%s'", name, newname)
        self._update_dirtable(ne.Entry)
        self._update_dirtable(e, True)
        # Mark the old one as erased
        for i in range(0, len(e._buf), 32):
            e._buf[i] ^= (1<<7)
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        return 1

    def clean(self, shrink=False):
        "Compacts used slots and blanks unused ones, optionally shrinking the table"
        self._checkopen()
        if DEBUG&8: log("Cleaning directory table %s with keep sort function", self.path)
        return self.sort(None, shrink=shrink) # keep order

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
        d = {}
        names = []
        for e in self.iterator():
            if e.chEntryType > 0x80 and e.chEntryType < 0x84:
                d[e.chEntryType] = e # handle special entries
                continue
            n = e.Name()
            d[n] = e
            names+=[n]
        if by_func:
            names = sorted(names, key=functools.cmp_to_key(by_func))
        else:
            names = sorted(names, key=str.lower) # default sorting: alphabetical, case insensitive
        self.stream.seek(0)
        if self.path == '.':
            if 0x83 in d: self.stream.write(d[0x83]._buf) # write Label
            if 0x81 in d: self.stream.write(d[0x81]._buf) # write Bitmap
            if 0x82 in d: self.stream.write(d[0x82]._buf) # write Upcase
        for name in names:
            if not name: continue
            self.stream.write(d[name]._buf) # re-writes ordered slots
        last = self.stream.tell()
        unused = self.stream.size - last
        self.stream.write(bytearray(unused)) # blanks unused area
        if DEBUG&8: log("%s: sorted %d slots, blanked %d", self, last//32, unused//32)
        if shrink:
            c_alloc = (self.stream.size+self.boot.cluster-1)//self.boot.cluster
            c_used = (last+self.boot.cluster-1)//self.boot.cluster
            if c_used < c_alloc:
                self.handle.ftruncate(last, 1)
                self.handle.IsValid = 1 # forces updating directory entry sizes
                self.handle.close()
                if DEBUG&8: log("Shrank directory table freeing %d clusters", c_alloc-c_used)
                unused -= (c_alloc-c_used//32)
            else:
                if DEBUG&8: log("Can't shrink directory table, free space < 1 cluster!")
        # Rebuilds Dirtable caches
        self.slots_map = {}
        self.dirtable[self.start] = {'Names':{}, 'Handle':None, 'slots_map':{}, 'Open':[]}
        self.map_slots()
        return last//32, unused//32

    def listdir(self):
        "Returns a list of file and directory names in this directory, sorted by on disk position"
        return [o.Name() for o in [o for o in [o for o in self.iterator()] if o.type==5]]

    def walk(self):
        """Walks across this directory and its childs. For each visited directory,
        returns a tuple (root, dirs, files) sorted in disk order. """
        dirs = []
        files = []
        for o in self.iterator():
            if o.type != 5: continue
            if o.IsDir():
                dirs += [o.Name()]
            else:
                files += [o.Name()]
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
                raise exFATException("Bad permission string", perm)
            if perm[0] == '-':
                e.wFileAttributes &= ~(1 << mask[perm[1].upper()])
            else:
                e.wFileAttributes |= (1 << mask[perm[1].upper()])
        if DEBUG&8: log("Updating permissions on '%s' with code=%X", name, e.wFileAttributes)
        self.stream.seek(e._pos)
        self.stream.write(e.pack())
        return 1

    def label(self, name=None):
        "Gets or sets volume label. Pass an empty string to clear."
        self._checkopen()
        if self.path != '.':
            raise exFATException("A volume label can be assigned in root directory only")
        if name and len(name) > 11:
            raise exFATException("A volume label can't be longer than 11 characters")
        if name and not exFATDirentry.IsValidDosName(name):
            raise exFATException("Volume label contains invalid characters")

        for e in self.iterator():
            if e.IsLabel():
                if name == None: # get mode
                    return e.sVolumeLabel.decode('utf-16le')[:e.chCount]
                elif name == '':
                    e._buf[0] = 3 # cleared label
                else:
                    e._buf[0] = 0x83 # active label
                    e._buf[1] = len(name) # label length (chars)
                    e._buf[2:] = bytes('%s' % name, 'utf-16le')
                # Writes new entry
                self.stream.seek(e._pos)
                self.stream.write(e._buf)
                return name

        if name == None: return
        e = exFATDirentry(bytearray(32))
        e._pos = self.findfree()
        e._buf[0] = 0x83
        e._buf[1] = len(name)
        e._buf[2:] = bytes('%s' % name, 'utf-16le')
        self.stream.seek(e._pos)
        self.stream.write(e._buf)
        self._update_dirtable(e)
        return name


         #############################
        # HIGH LEVEL HELPER ROUTINES #
        ############################

def fat_copy_clusters(boot, fat, start):
    """Duplicates a cluster chain copying the cluster contents to another position.
    Returns the first cluster of the new chain."""
    count = fat.count(start)[0]
    src = Chain(boot, fat, start, boot.cluster*count)
    #~ if fat.exfat:
        #~ src.bitmap = ...
    target = fat.alloc(count) # possibly defragmented
    dst = Chain(boot, fat, target, boot.cluster*count)
    if DEBUG&8: log("Copying %s to %s", src, dst)
    s = 1
    while s:
        s = src.read(boot.cluster)
        dst.write(s)
    return target
