# -*- coding: cp1252 -*-
import io, os, sys, atexit, platform
from io import BytesIO
from ctypes import *

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

if os.name == 'nt':
    from ctypes.wintypes import *
    from FATtools.win32enumvols import dismount_and_lock_all,unlock_volume_handles
else:
    import fcntl
    if platform.mac_ver() != ('', ('', '', ''), ''): # macOS X
        def get_size(name):
            DKIOCGETBLOCKCOUNT=0x40086419
            DKIOCGETBLOCKSIZE = 0x40046418
            count = 0
            size = 0
            with open(name) as dev:
                n = c_ulong(0) # 64-bit
                fcntl.ioctl(dev.fileno(), DKIOCGETBLOCKCOUNT, n)
                count = n.value
                fcntl.ioctl(dev.fileno(), DKIOCGETBLOCKSIZE, n)
                size = n.value
            return count*size
    else: # assume Linux-like
        # os.stat does not work with Linux block devices
        # use lseek, ioctl or read 512 blocks # from /sys/block/<device>/size
        def get_size(name):
            fd = os.open(name, os.O_RDONLY)
            try:
                return os.lseek(fd, 0, os.SEEK_END)
            finally:
                os.close(fd)

from FATtools.debug import log
from FATtools.utils import myfile
#~ import hexdump


class win32_disk(object):
    "Handles a Win32 disk. PLEASE NOTE: due to locking mechanism, the Win32 HANDLE is here UNIQUE - closing once, closes it everywhere."
    open_handles = {}

    def type(self): return 'win32disk'
    
    def __str__ (self):
        return "Win32 Disk Handle %Xh for %s, mode '%s'" % (self.handle, self.name, self.mode)

    def __init__(self, name, mode='rb', buffering=0):
        status = c_int(0)
        name = name.lower()
        # First try to unmount volumes belonging to a given \\.\PhysicalDriveN
        self.volume_handles=[]
        if 'physicaldrive' in name and mode != 'rb':
            self.volume_handles=dismount_and_lock_all(bytes(name,'ascii'))
        # Open a new write handle
        if name in win32_disk.open_handles:
            handle = win32_disk.open_handles[name]
        else:
            handle = windll.kernel32.CreateFileA(name.encode(), DWORD(0xC0000000), DWORD(3), 0, DWORD(3), DWORD(0x80000000|0x10000000|0x20000000), 0)
        if handle == -1:
            raise BaseException('CreateFileA failed with code %d (%s)' % (GetLastError(), FormatError()))
        win32_disk.open_handles[name] = handle
        # Dismount volume, gaining exclusive access with FSCTL_DISMOUNT_VOLUME (0x90020)
        # Dismount volume, locking it for exclusive access with FSCTL_LOCK_VOLUME (0x90018)
        # IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
        # THIS IS MANDATORY FOR WRITE ACCESS! (Windows Vista+)
        # Also it could require Admin rights for non-removable volumes!
        # Suggest: open NON-CACHED
        #  To read or write to the last few sectors of the volume, you must call DeviceIoControl and specify FSCTL_ALLOW_EXTENDED_DASD_IO
        ioctls ={0x90020:'FSCTL_DISMOUNT_VOLUME', 0x90018:'FSCTL_LOCK_VOLUME', 0x90083: 'FSCTL_ALLOW_EXTENDED_DASD_IO'}
        for ioctl in (0x90020, 0x90083):
            if windll.kernel32.DeviceIoControl(handle, DWORD(ioctl), 0, DWORD(0), 0, DWORD(0), byref(status), 0):
                # 5 = ACCESS DENIED 6= INVALID HANDLE
                if GetLastError():
                    raise BaseException('DeviceIoControl %s failed with code %d (%s)' % (ioctls[ioctl], GetLastError(), FormatError()))
        GET_LENGTH_INFORMATION = c_ulonglong(0)
        if windll.kernel32.DeviceIoControl(handle,  DWORD(0x7405C), 0, DWORD(0), byref(GET_LENGTH_INFORMATION), DWORD(8), byref(status), 0):
            self.size = GET_LENGTH_INFORMATION.value
        else:
            self.size = 0
        self.handle = handle
        self.name = name
        self.mode = mode
        if DEBUG&1: log("Successfully opened HANDLE to Win32 Disk %s (size %d MB) for exclusive access", name, self.size//(1<<20))
        self._pos = 0
        
    def close(self):
        unlock_volume_handles(self.volume_handles)
        self.volume_handles=[]
        self.closed = True
        if self.name in win32_disk.open_handles:
            windll.kernel32.CloseHandle(self.handle)
            del win32_disk.open_handles[self.name]
        
    def seek(self, offset, whence=0):
        if whence == 1:
            npos = self._pos + offset
        elif whence == 2:
            npos = self.size + offset
        else:
            npos = offset
        n = c_int(offset>>32)
        if 0xFFFFFFFF == windll.kernel32.SetFilePointer(self.handle, LONG(offset&0xFFFFFFFF), byref(n), DWORD(whence)):
            if GetLastError():
                raise BaseException('SetFilePointer failed with code %d (%s)' % (GetLastError(), FormatError()))
        self._pos = npos

    def tell(self):
        n = c_int(0)
        offset = windll.kernel32.SetFilePointer(self.handle, LONG(0), byref(n), DWORD(1))
        if offset == 0xFFFFFFFF:
            if GetLastError() != 0:
                raise BaseException('SetFilePointer failed with code %d (%s)' % (GetLastError(), FormatError()))
        return (n.value<<32) | offset

    def readinto(self, buf):
        assert len(buf) > 0
        n = c_int(0)
        z = c_char * len(buf)
        z = z.from_buffer(buf)
        if not windll.kernel32.ReadFile(self.handle, z, DWORD(len(buf)), byref(n), 0):
            raise BaseException('ReadFile failed with code %d (%s)' % (GetLastError(), FormatError()))
        if n.value < len(buf):
            if DEBUG&1: log("NOTE: ReadFile (readinto) read %d bytes instead of %d", n.value, len(buf))
        self._pos += n.value

    def read(self, size=-1):
        assert size > -1
        n = c_int(0)
        s = create_string_buffer(size)
        if not windll.kernel32.ReadFile(self.handle, s, DWORD(size), byref(n), 0):
            raise BaseException('ReadFile failed with code %d (%s)' % (GetLastError(), FormatError()))
        if n.value < size:
            if DEBUG&1: log("NOTE: ReadFile read %d bytes instead of %d", n.value, size)
        self._pos += n.value
        return bytearray(s)

    def write(self, s):
        if not len(s): return
        s = bytes(s)
        if not windll.kernel32.WriteFile(self.handle, s, DWORD(len(s)), 0, 0):
            raise BaseException('WriteFile failed with code %d (%s)' % (GetLastError(), FormatError()))
        self._pos += len(s)



class disk(object):
    """Let a device or file act in a manner similar to a Python file object. Please
    note that under Windows: 1) read, write and seek MUST be sector aligned (512
    bytes offsets); 2) seek FROM disk's end does not work; 3) seek PAST disk's
    end followed by read returns no error."""
    def __str__ (self):
        return "Python disk '%s' (mode '%s') @%016Xh" % (self._file.name, self.mode, self.pos)
        
    def type(self): return 'disk'

    def __init__(self, name, mode='rb', buffering=0):
        "'name' is the name of a file or device to open or, if mode is 'ramdisk', a BytesIO object with raw disk data"
        self.mode = mode
        self.pos = 0 # linear pos in the virtual stream
        self.si = 0 # disk sector index
        self.so = 0 # sector offset
        self.lastsi = 0 # last sector read from *disk*
        self.buf = None # read buffer
        self.blocksize = 512 # fixed sector size
        # Cache only small 512 sectors
        self.rawcache = bytearray(1024<<10) # 1M cache buffer
        self.cache = memoryview(self.rawcache)
        self.cache_index = 0 # offset of next cache slot
        self.cache_hits = 0 # sectors retrieved from cache
        self.cache_misses = 0 # sectors not retrieved
        self.cache_extras = 0 # direct, non-cacheable I/O
        self.cache_dirties = {} # dirty sectors
        self.cache_table = {} # { sector: cache offset }
        self.cache_tableR = {} # reversed: { cache offset:sector }
        if mode == 'ramdisk':
            if not isinstance(name, BytesIO):
                raise BaseException('Ramdisk can be built from BytesIO only, not from ', type(name))
            self._file = name
            self.size = name.getbuffer().nbytes
            self.mode = 'r+b'
        elif os.name == 'nt' and '\\\\.\\' in name:
            self._file = win32_disk(name, mode, buffering)
            self.size = self._file.size
        else:
            self._file = open(name, mode, buffering)
            if os.name == 'nt': 
                self.size = os.stat(name).st_size
            else:
                self.size = get_size(name)
        atexit.register(self.cache_flush)

    def close(self):
        "Flush internal disk cache and close its handle"
        self.cache_flush()
        atexit.unregister(self.cache_flush)
        if not isinstance(self._file, BytesIO): # closing BytesIO == KILL DATA!
            self._file.close()

    def seek(self, offset, whence=0):
        if whence == 1:
            self.pos += offset
        elif whence == 2:
            if self.size:
                self.pos = self.size + offset
        else:
            self.pos = offset
        if self.pos > self.size: self.pos = self.size
        if self.pos < 0: self.pos = 0
        self.si = self.pos // self.blocksize
        self.so = self.pos % self.blocksize
        if DEBUG&1: log("disk pointer to set @%Xh", self.si*self.blocksize)
        self._file.seek(self.si*self.blocksize)
        if DEBUG&1: log("si=%Xh lastsi=%Xh so=%Xh", self.si,self.lastsi,self.so)

    def tell(self):
        return self.pos

    def cache_stats(self):
        if DEBUG&1: log("Cache items/hits/misses: %d/%d/%d", len(self.cache_table), self.cache_hits, self.cache_misses)
        
    def flush(self):
        self.cache_flush()

    def cache_flush(self, sector=None):
        self.cache_stats()
        if not self.cache_dirties:
            # 21.04.17: must ANYWAY reset (read-only) cache, or higher cached slots could get silently overwritten!
            if DEBUG&1: log("resetting cache (no dirty sectors)")
            self.cache_table = {}
            self.cache_tableR = {}
            return
        if sector != None: # assume it is called by cache_retrieve only, with the right sector #
            self._file.seek(sector*self.blocksize)
            i = self.cache_table[sector]
            self._file.write(self.cache[i:i+self.blocksize])
            del self.cache_dirties[sector]
            if DEBUG&1: log("%s: dirty sector #%d committed to disk from cache[%d]", self, sector,i//512)
            return
        if DEBUG&1: log("%s: committing %d dirty sectors to disk", self, len(self.cache_dirties))
        for sec in sorted(self.cache_dirties):
            self._file.seek(sec*self.blocksize)
            try:
                i = self.cache_table[sec]
                self._file.write(self.cache[i:i+self.blocksize])
            except:
                if DEBUG&1: log("ERROR! Sector %d in cache_dirties not in cache_table!", sec)
                if sec in list(self.cache_tableR.values()):
                    if DEBUG&1: log("(but sector %d is in cache_tableR)", sec)
                else:
                    if DEBUG&1: log("(and sector %d is neither in cache_tableR)", sec)
                continue
        self.cache_dirties = {}
        self.cache_table = {}
        self.cache_tableR = {}

    def cache_retrieve(self):
        "Retrieve a sector from cache. Returns True if hit, False if missed."
        # If we are retrieving a single block...
        if self.asize ==self.blocksize:
            if self.si not in self.cache_table:
                self.cache_misses += 1
                if DEBUG&1: log("%s: cache_retrieve missed #%d", self, self.si)
                return False
            self.cache_hits += 1
            i = self.cache_table[self.si]
            self.buf = self.cache[i:i+self.asize]
            if DEBUG&1: log("%s: cache_retrieve hit #%d", self, self.si)
            return True

        # If we are retrieving multiple blocks...
        for i in range(self.asize//self.blocksize):
            # If one block is not cached...
            if self.si+i not in self.cache_table:
                if DEBUG&1: log("%s: cache_retrieve (multisector) miss-not cached %d", self, self.si+i)
                self.cache_misses += 1
                continue
            # If one block is dirty, first flush it...
            if self.si+i in self.cache_dirties:
                if DEBUG&1: log("%s: cache_retrieve (multisector) flush %d", self, self.si+i)
                self.cache_flush(self.si+i)
                if DEBUG&1: log("%s: seeking back @%Xh after flush", self, self.pos)
                self.seek(self.pos)
                continue
        return False # consider a miss

    def cache_readinto(self):
        # If we should read beyond the cache's end...
        if self.cache_index + self.asize > len(self.cache):
            # Free space, flushing dirty sectors & updating cache index
            self.cache_flush()
            self.cache_index = 0
            self.seek(self.pos)
        pos = self.cache_index
        if DEBUG&1: log("loading disk sector #%d into cache[%d]", self.si, pos//512)
        self._file.readinto(self.cache[pos:pos+self.asize])
        self.buf = self.cache[pos:pos+self.asize]
        self.cache_index += self.asize
        # Update dictionary of cached sectors and their position
        # Invalidate accordingly if we are recycling pool from zero?
        k = self.si
        v = pos
        # If a previously cached sector is pointing to the same buffer,
        # unlink it
        if v in self.cache_tableR:
            del self.cache_table[self.cache_tableR[v]]
        self.cache_table[k] = v
        self.cache_tableR[v] = k
        return pos

    def read(self, size=-1):
        if DEBUG&1: log("read(%d) bytes @%Xh", size, self.pos)
        self.seek(self.pos)
        # If size is negative
        if size < 0:
            size = 0
            if self.size: size = self.size
        # If size exceeds disk size
        if self.size and self.pos + size > self.size:
            size = self.size - self.pos
        se = (self.pos+size)//self.blocksize
        if (self.pos+size)%self.blocksize:
            se += 1
        self.asize = (se - self.si) * self.blocksize # full sectors to read in
        # If sectors are already cached...
        if self.cache_retrieve():
            if DEBUG&1: log("%d bytes read from cache", self.asize)
            self.lastsi = self.si
            self.pos += size
            return bytearray(self.buf[self.so : self.so+size])
        # ...else, read them from disk...
        # if larger than cache limit, read directly into a new buffer
        if self.asize > self.blocksize:
            self.buf = bytearray(self.asize)
            self._file.seek(self.si*self.blocksize)
            if DEBUG&1: log("reading %d bytes directly from disk @%Xh", self.asize, self._file.tell())
            self._file.readinto(self.buf)
            # Direct read (bypass) DON'T advance lastsi? Or file pointer?
            self.si += self.asize//self.blocksize # 11.01.2016: fix mkexfat flaw
            self.pos += size
            self.cache_extras += 1
            return bytearray(self.buf[self.so : self.so+size])
        # ...else, update the cache
        self.cache_readinto()
        self.lastsi = self.si
        self.pos += size
        return bytearray(self.buf[self.so : self.so+size])

    def write(self, s): # s MUST be of type bytearray/memoryview
        if DEBUG&1: log("request to write %d bytes @%Xh", len(s), self.pos)
        if len(s) == 0: return
        # If we have to complete a sector...
        if self.so:
            j = min(self.blocksize - self.so, len(s))
            if DEBUG&1: log("writing middle sector %d[%d:%d]", self.si, self.so, self.so+j)
            self.asize = 512
            if not self.cache_retrieve():
                self.cache_readinto()
            # We assume buf is pointing to rawcache
            self.buf[self.so : self.so+j] = s[:j]
            s = s[j:] # slicing penalty if buffer?
            if DEBUG&1: log("len(s) is now %d", len(s))
            self.cache_dirties[self.si] = True
            self.pos += j
            self.seek(self.pos)
        # if we have full sectors to write...
        if len(s) > self.blocksize:
            full_blocks = len(s)//512
            if DEBUG&1: log("writing %d sector(s) directly to disk", full_blocks)
            # Directly write full sectors to disk
            # Invalidate eventually cached data
            for si in range(self.si, self.si+full_blocks):
                if si in self.cache_table:
                    if DEBUG&1: log("removing sector #%d from cache", si)
                    Ri = self.cache_table[si]
                    del self.cache_tableR[Ri]
                    del self.cache_table[si]
                    if si in self.cache_dirties:
                        if DEBUG&1: log("removing sector #%d from dirty sectors", si)
                        del self.cache_dirties[si]
            self._file.write(s[:full_blocks*512])
            self.pos += full_blocks*512
            self.seek(self.pos)
            s = s[full_blocks*512:] # slicing penalty if buffer?
            if DEBUG&1: log("len(s) is now %d", len(s))
        if len(s):
            if DEBUG&1: log("writing sector %d[%d:%d] from start", self.si, self.so, self.so+len(s))
            self.asize = 512
            if not self.cache_retrieve():
                self.cache_readinto()
            self.buf[self.so : self.so+len(s)] = s
            self.cache_dirties[self.si] = True
            self.pos += len(s)
        self.seek(self.pos)
        self.lastsi = self.si


class partition(object):
    "Emulates a partition using disk object"
    def __str__ (self):
        return "Python partition '%s' (offset=%016Xh, size=%d, mode '%s') @%016Xh" % (self.disk._file.name, self.offset, self.size, self.disk.mode, self.pos)
        
    def type(self): return 'partition'

    def __init__(self, disk, offset, size):
        assert size != 0
        self.disk = disk
        self.volume = None # contained file system, if opened
        self.closed = False
        self.mode = disk.mode
        self.offset = offset # partition offset
        self.size = size #partition size
        self.pos = 0
        self.seek(0) # force disk to partition start

    def close(self):
        self.flush()
        self.closed = True

    def seek(self, offset, whence=0):
        if DEBUG&1: log("partion.seek(%016Xh, %d)", offset, whence)
        if whence == 1:
            self.pos += offset
        elif whence == 2:
            if self.size:
                self.pos = self.size + offset
        else:
            self.pos = offset
        if self.pos < 0: self.pos = 0
        if self.pos > self.size: self.pos = self.size
        self.disk.seek(self.pos+self.offset)

    def tell(self):
        return self.pos

    def read(self, size=-1):
        return self.disk.read(size)
        
    def write(self, s): # s MUST be of type bytearray/memoryview
        self.disk.write(s)

    def flush(self):
        if self.volume:
            self.volume.flush() # propagates flush to opened file system
        self.disk.flush()



if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.DEBUG, filename='test_disk.log', filemode='w')
    log = logging.getLogger().debug
    
    from random import randint, shuffle, seed

    FAILURES=0
    #~ DEBUG=255
    #~ seed(1)
    
    #~ open('TESTIMAGE.BIN', 'wb').write(bytearray(4<<20))
    #~ d = disk('TESTIMAGE.BIN', 'r+b')
    d = disk('\\\\.\\G:', 'r+b')
    d.rawcache = bytearray(4<<20)
    d.cache = memoryview(d.rawcache)

    log("Testing cached random writes & reads...")
    print("Testing cached random writes & reads...")
    # Blank first 10K
    d.write(bytearray(16*512))
    sectors = list(range(16))
    shuffle(sectors)
    ok=0
    while not ok:
        values = [randint(0,255) for i in range(16)]
        if len(set(values)) == 16: ok = 1
    ok=0
    while not ok:
        offsets = [randint(0,255) for i in range(16)]
        if len(set(offsets)) == 16: ok = 1
    print('sectors', sectors)
    print('offsets', offsets)
    print('values', values)
    # Write a random byte at a random position in 16 sectors
    # write them in random order; then read full block
    log("sectors, offsets, values\n%s\n%s\n%s\n", sectors, offsets, values)
    for i in sectors:
        log("\nWriting byte %X at sector %d:%Xh", values[i], i, offsets[i])
        d.seek(i*512+offsets[i])
        d.write(bytes([values[i]]))
    log("Checking written sectors...")
    d.seek(0)
    s = d.read(16*512)
    for i in sectors:
        log("Reading byte %X at sector %d:%Xh", values[i], i, offsets[i])
        if s[i*512+offsets[i]] != values[i]:
            FAILURES+=1
            print ('FAILURE! (1) Expected byte %X at sector %d:%d, read %s!' % (values[i], i, offsets[i], c))
            
    # Overwrite area with F8, write full block then read sectors
    log("\nOverwriting the same area...")
    d.seek(0)
    d.write(16*512*b'\xF8')
    d.seek(0)
    d.write(s)
    assert len(s) == 16*512
    for i in sectors:
        d.seek(i*512+offsets[i])
        c = d.read(1)
        log("Read byte %s at sector %d:%Xh", c, i, offsets[i])
        if c != bytes([values[i]]):
            FAILURES+=1
            print ('FAILURE! (2) Expected byte %X in sector %d, read %s!' % (values[i], i, c))
            log("ERROR! Expected byte %X", values[i])
            assert 0

    if not FAILURES:
        log("Caching tests passed!")
        print("Caching tests passed!")

    print("Testing sequential writing & reading byte-for-byte of 2 sectors...")
    log("Testing sequential writing & reading byte-for-byte of 2 sectors...")
    d.seek(0)
    for i in range(1024):
        d.write(bytes([i&0xFF]))
    d.seek(0)
    for i in range(1024):
        c = d.read(1)
        if bytes([i&0xFF]) != c:
            FAILURES+=1
            print ('FAILURE! Expected byte %d at %d, read %s!' % (i&0xFF, i, c))

    print("Testing cross sector writing & reading...")
    for i in range(16):
        d.seek((i+1)*512-3)
        d.write(b'ABCDEF')
        d.seek((i+1)*512-3)
        if d.read(6) != b'ABCDEF':
            FAILURES+=1

    d.seek(0)
    d.write(16*512*2*b'\xF1')
    d.seek(0)
    import struct
    for i in range(16*512):
        orig = d.read(2)
        try:
            assert orig == b'\xF1\xF1'
        except:
            print(i, "'%s' differ from F1F1h at %Xh" % (orig, d.tell()-2))
            FAILURES+=1
        d.seek(-2, 1)
        d.write(struct.pack('<H', i))
        d.seek(-2, 1)
        if struct.pack('<H', i) != d.read(2): FAILURES+=1
        d.seek(-2, 1)
        d.write(orig)
        d.seek(-2, 1)
        if orig != bytearray(d.read(2)): FAILURES+=1

    print("Testing sequential writing & reading 2byte-for-2byte of 2 sectors...")
    d.seek(3)
    for i in range(512):
        d.write(2*bytes([i&0xFF]))
    d.seek(3)
    for i in range(512):
        c = d.read(2)
        if 2*bytes([i&0xFF]) != c:
            FAILURES+=1
            print ('FAILURE! Expected bytes %d %d at %d, read %s!' % (i&0xFF, i&0xFF, i, c))

    if not FAILURES:
        print("All tests passed!")
