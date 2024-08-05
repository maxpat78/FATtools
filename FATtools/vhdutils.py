# -*- coding: cp1252 -*-
"Utilities to handle VHD disk images"

""" VHD IMAGE FILE FORMAT
A FIXED VHD is a simple RAW image with all disk sectors and a VHD footer
appended.

A DYNAMIC VHD initially contains only the VHD footer in the last sector and
a copy of it in the first, a dynamic disk header in second and third sector
followed by one or more sectors with the BAT (Blocks Allocation Table).
Disk is virtually subdivided into blocks of equal size (2 MiB default) with a
corresponding 32-bit BAT index showing the 512-byte sector where the block
resides in VHD file.
Initially, all BAT indexes are present and set to 0xFFFFFFFF; the blocks are
allocated on write and put at image's end, so they appear in arbitrary order.
More BAT space can be allocated at creation time for future size expansion.
Each block starts with one or more sectors containing a bitmap, indicating
which sectors are in use. A zeroed bit means sector is not in use, and zeroed.
The default block requires a 1-sector bitmap since it is 4096 sectors long.

A DIFFERENCING VHD is a dynamic image containing only new or modified blocks
of a parent VHD image (fixed, dynamic or differencing itself). The block
bitmap must be checked to determine which sectors are in use.

Since offsets are represented in sectors, the BAT can address sectors in a
range up to 2^32-1 or about 2 TiB.
The disk image itself is shorter due to VHD internal structures (assuming 2^20
blocks of default size, the first 3 sectors are occupied by heaeders, 4 MiB
by the BAT and 512 MiB by bitmap sectors.
In fact, Windows 11 refuses to mount a VHD >2040 GiB.

A BAT index of 0xFFFFFFFF signals a zeroed block (Dynamic VHD) or a block not
allocated on a given child (Differencing VHD): in the latter case, zeroing a 
block alredy present in an ancestor requires allocating a new zeroed block
in the child image.

PLEASE NOTE THAT ALL NUMBERS ARE IN BIG ENDIAN FORMAT! """
import io, struct, uuid, zlib, ctypes, time, os, math

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
import FATtools.utils as utils
from FATtools.debug import log
from FATtools.utils import myfile, calc_rel_path


MAX_VHD_SIZE = 2040<<30 # Windows 11 won't mount bigger VHDs



class Footer(object):
    "VHD Footer"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sCookie', '8s'), # conectix
    0x08: ('dwFeatures', '>I'), # 0=None, 1=Temporary, 2=Reserved (default)
    0x0C: ('dwFileFormatVersion', '>I'), #0x10000
    0x10: ('u64DataOffset', '>Q'), # absolute offset of next structure, or 0xFFFFFFFFFFFFFFFF for fixed disks
    0x18: ('dwTimestamp', '>I'), # creation time, in seconds since 1/1/2000 12:00 AM UTC
    0x1C: ('dwCreatorApp', '4s'), # creator application, here Py
    0x20: ('dwCreatorVer', '>I'), # its version, here 0x3000A (3.10)
    0x24: ('dwCreatorHost', '4s'), # Wi2k or Mac
    0x28: ('u64OriginalSize', '>Q'), # Initial size of the emulated disk
    0x30: ('u64CurrentSize', '>Q'), # Current size of the emulated disk
    0x38: ('dwDiskGeometry', '4s'), # pseudo CHS
    0x3C: ('dwDiskType', '>I'), # 0=None,2=Fixed,3=Dynamic,4=Differencing
    0x40: ('dwChecksum', '>I'), # footer checksum
    0x44: ('sUniqueId', '16s'), # image UUID
    0x54: ('bSavedState', 'B'), # 1=is in saved state
    # REST IS RESERVED AND MUST BE ZERO
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
    
    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        self.dwChecksum = 0
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self._buf[64:68] = mk_crc(self._buf) # updates checksum
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "VHD Footer @%X\n" % self._pos)
    
    def crc(self):
        crc = self._buf[64:68]
        self._buf[64:68] = b'\0\0\0\0'
        c_crc = mk_crc(self._buf)
        self._buf[64:68] = crc
        return c_crc

    def isvalid(self):
        if self.sCookie != b'conectix' or self.dwCreatorHost not in (b'Wi2k',b'Mac'):
            return 0
        if self.dwChecksum != struct.unpack(">I", self.crc())[0]:
            if DEBUG&16: log("Footer checksum 0x%X calculated != 0x%X stored", self.dwChecksum, struct.unpack(">I", self.crc())[0])
        return 1



class DynamicHeader(object):
    "Dynamic Disk Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sCookie', '8s'), # cxsparse
    0x08: ('u64DataOffset', '>Q'), # 0xFFFFFFFFFFFFFFFF
    0x10: ('u64TableOffset', '>Q'), # absolute offset of Block Table Address
    0x18: ('dwVersion', '>I'), # 0x10000
    0x1C: ('dwMaxTableEntries', '>I'), # entries in BAT (=total disk blocks)
    0x20: ('dwBlockSize', '>I'), # block size (default 2 MiB)
    0x24: ('dwChecksum', '>I'),
    0x28: ('sParentUniqueId', '16s'), # UUID of parent disk in a differencing disk
    0x38: ('dwParentTimeStamp', '>I'), # Timestamp in parent's footer
    0x3C: ('dwReserved', '>I'),
    0x40: ('sParentUnicodeName', '512s'),  # Windows 10 stores the parent's absolute pathname (Big-Endian)
    0x240: ('sParentLocatorEntries', '192s'), # Parent Locators array (see later)
    # REST (256 BYTES) IS RESERVED AND MUST BE ZERO
    } # Size = 0x400 (1024 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(1024)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.locators = []
        for i in range(8):
            j = 0x240+i*24
            self.locators += [ParentLocator(self._buf[j:j+24])]
    
    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        self.dwChecksum = 0
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        for i in range(8):
            j = 0x240+i*24
            self._buf[j:j+24] = self.locators[i].pack()
        self._buf[0x24:0x28] = mk_crc(self._buf) # updates checksum
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "VHD Dynamic Header @%X\n" % self._pos)

    def crc(self):
        crc = self._buf[0x24:0x28]
        self._buf[0x24:0x28] = b'\0\0\0\0'
        c_crc = mk_crc(self._buf)
        self._buf[0x24:0x28] = crc
        return c_crc

    def isvalid(self):
        if self.sCookie != b'cxsparse':
            return 0
        if self.dwChecksum != struct.unpack(">I", self.crc())[0]:
            if DEBUG&16: log("Dynamic Header checksum 0x%X calculated != 0x%X stored", self.dwChecksum, struct.unpack(">I", self.crc())[0])
        return 1



class BAT(object):
    "Implements the Block Address Table as indexable object"
    def __init__ (self, stream, offset, blocks, block_size):
        self.stream = stream
        self.size = blocks # total blocks in the data area
        self.bsize = block_size # block size
        self.offset = offset # relative BAT offset
        self.decoded = {} # {block index: block effective sector}
        self.isvalid = 1 # self test result
        self._isvalid() # performs self test

    def __str__ (self):
        return "BAT table of %d blocks starting @%Xh\n" % (self.size, self.offset)

    def __getitem__ (self, index):
        "Retrieves the value stored in a given block index"
        if index < 0:
            index += self.size
        if DEBUG&16: log("%s: requested to read BAT[0x%X]", self.stream.name, index)
        if not (0 <= index <= self.size-1):
            raise BaseException("Attempt to read a #%d block past disk end"%index)
        slot = self.decoded.get(index)
        if slot: return slot
        pos = self.offset + index*4
        opos = self.stream.tell()
        self.stream.seek(pos)
        slot = struct.unpack(">I", self.stream.read(4))[0]
        self.decoded[index] = slot
        if DEBUG&16: log("%s: got BAT[0x%X]=0x%X @0x%X", self.stream.name, index, slot, pos)
        self.stream.seek(opos) # rewinds
        return slot

    def __setitem__ (self, index, value):
        "Sets the value stored in a given block index"
        if index < 0:
            index += self.size
        self.decoded[index] = value
        dsp = index*4
        pos = self.offset+dsp
        if DEBUG&16: log("%s: set BAT[0x%X]=0x%X @0x%X", self.stream.name, index, value, pos)
        opos = self.stream.tell()
        self.stream.seek(pos)
        value = struct.pack(">I", value)
        self.stream.write(value)
        self.stream.seek(opos) # rewinds
        
    def _isvalid(self, selftest=1):
        "Checks BAT for invalid entries setting .isvalid member"
        self.stream.seek(0, 2)
        ssize = self.stream.tell() # container actual size
        if self.offset+4*self.size > ssize:
            if DEBUG&16: log("%s: container size (%d) is shorter than expected minimum (%d), truncated BAT", self, ssize, self.offset+4*self.size)
            self.isvalid = -1 # invalid container size
            return
        raw_size = self.bsize + max(512, (self.bsize//512)//8) # RAW block size, including bitmap
        last_block = ssize - 512 - raw_size # theoretical offset of last block
        first_block = last_block%raw_size # theoretical address of first block
        allocated = (last_block+raw_size-first_block)//raw_size
        unallocated = 0
        seen = []
        # Windows 10 does NOT check padding BAT slots for FFFFFFFF,
        # only used indexes have to be valid (DiscUtils VHDDump does!)
        for i in range(self.size):
            a = self[i]
            if a == 0xFFFFFFFF:
                unallocated+=1
                continue
            if a in seen:
                self.isvalid = -2 # duplicated block address
                if DEBUG&16: log("%s: BAT[%d] offset (sector %X) was seen more than once", self, i, a)
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) was seen more than once" %(i, a))
            if a*512 > last_block or a*512+raw_size > ssize:
                if DEBUG&16: log("%s: block %d offset (sector %X) exceeds allocated file size", self, i, a)
                self.isvalid = -3 # block address beyond file's end detected
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) exceeds allocated file size" %(i, a))
            if (a*512-first_block)%raw_size: # it's valid, i.e. when a missing Parent Locator sector get fixed!
                if DEBUG&16: log("%s: BAT[%d] offset (sector %X) is not aligned", self, i, a)
                #~ self.isvalid = -4 # block address not aligned
                #~ if selftest: break
                print("WARNING: BAT[%d] offset (sector %X) is not aligned, overlapping blocks" %(i, a))
            if a*512 > last_block or a*512+raw_size > (ssize-512):
                if DEBUG&16: log("%s: block %d offset (sector %X) overlaps Footer", self, i, a)
                self.isvalid = -5
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) overlaps Footer" %(i, a))
            seen += [a]

        # Neither Windows 10 nor VHDDump detects this case
        if unallocated + allocated != self.size:
            if DEBUG&16: log("%s: BAT has %d blocks allocated only, container %d", self, len(seen), allocated)
            self.isvalid = 0
            if selftest: return
            print("WARNING: BAT has %d blocks allocated only, container %d" % (len(seen), allocated))



class ParentLocator(object):
    "Element in the Dynamic Header Parent Locators array"
    layout = { # { offset: (name, unpack string) }
    0x00: ('dwPlatformCode', '4s'), # W2ru, W2ku in Windows
    0x04: ('dwPlatformDataSpace', '>I'), # bytes needed to store the Locator sector(s)
    0x08: ('dwPlatformDataLength', '>I'), # locator length in bytes
    0x0C: ('dwReserved', '>I'),
    0x10: ('u64PlatformDataOffset', '>Q'), # absolute file offset where locator is stored
    } # Size = (24 byte)
    
    def __init__ (self, s):
        self._i = 0
        self._pos = 0
        self._buf = s
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
        return utils.class2str(self, "Parent Locator @%X\n" % self._pos)



class BlockBitmap(object):
    "Handles the block bitmap"
    def __init__ (self, s, i):
        if DEBUG&16: log("inited Bitmap for block #%d", i)
        self.bmp = s
        self.i = i

    def isset(self, sector):
        "Tests if the bit corresponding to a given sector is set"        
        # CAVE! BIT ORDER IS LSB FIRST!
        return (self.bmp[sector//8] & (128 >> (sector%8))) != 0
    
    def set(self, sector, length=1, clear=False):
        "Sets or clears a bit or bits run"
        pos = sector//8
        rem = sector%8
        if DEBUG&16: log("set(%Xh,%d%s) start @0x%X:%d", sector, length, ('',' (clear)')[clear!=False], pos, rem)
        if rem:
            B = self.bmp[pos]
            if DEBUG&16: log("got byte {0:08b}".format(B))
            todo = min(8-rem, length)
            if clear:
                B &= ~(((0xFF<<(8-todo))&0xFF) >> rem)
            else:
                B |= (((0xFF<<(8-todo))&0xFF) >> rem)
            self.bmp[pos] = B
            length -= todo
            if DEBUG&16: log("set byte {0:08b}, left={1}".format(B, length))
            pos+=1
        octets = length//8
        while octets:
            i = min(32768, octets)
            octets -= i
            if clear:
                self.bmp[pos:pos+i] = bytearray(i)
            else:
                self.bmp[pos:pos+i] = i*b'\xFF'
            pos+=i
        rem = length%8
        if rem:
            if DEBUG&16: log("last bits=%d", rem)
            B = self.bmp[pos]
            if DEBUG&16: log("got B={0:08b}".format(B))
            if clear:
                B &= ~((0xFF<<(8-rem))&0xFF)
            else:
                B |= ((0xFF<<(8-rem))&0xFF)
            self.bmp[pos] = B
            if DEBUG&16: log("set B={0:08b}".format(B))



class Image(object):
    def __init__ (self, name, mode='rb'):
        self._pos = 0 # offset in virtual stream
        self.size = 0 # size of virtual stream
        self.name = name
        self.stream = myfile(name, mode)
        self._file = self.stream
        self.mode = mode
        self.stream.seek(0, 2)
        size = self.stream.tell()
        self.stream.seek(size-512)
        self.footer = Footer(self.stream.read(512), size-512)
        self.Parent = None
        if not self.footer.isvalid():
            raise BaseException("VHD Image Footer is not valid!")
        if self.footer.dwDiskType not in (2, 3, 4):
            raise BaseException("Unknown VHD Image type!")
        if self.footer.dwDiskType in (3, 4):
            self.stream.seek(0)
            self.footer_copy = Footer(self.stream.read(512))
            if not self.footer_copy.isvalid():
                raise BaseException("VHD Image Footer (copy) is not valid!")
            if self.footer._buf != self.footer_copy._buf:
                raise BaseException("Main Footer and its copy differ!")
            self.header = DynamicHeader(self.stream.read(1024), 512)
            if not self.header.isvalid():
                raise BaseException("VHD Image Dynamic Header is not valid!")
            self.block = self.header.dwBlockSize
            self.zero = bytearray(self.block)
            self.bat = BAT(self.stream, self.header.u64TableOffset, self.header.dwMaxTableEntries, self.block)
            self.bitmap_size = max(512, (self.block//512)//8) # bitmap sectors size
            if self.bat.isvalid < 0:
                error = {-1: "insufficient container size", -2: "duplicated block address", -3: "block past end", -4: "misaligned block"}
                raise BaseException("VHD Image is not valid: %s", error[self.bat.isvalid])
        if self.footer.dwDiskType == 4: # Differencing VHD
            parent = ''
            loc = None
            for i in range(8):
                loc = self.header.locators[i]
                if loc.dwPlatformCode == b'W2ku': break # prefer absolute pathname
            if not loc:
                for i in range(8):
                    loc = self.header.locators[i]
                    if loc.dwPlatformCode == b'W2ru': break
            if loc:
                    self.stream.seek(loc.u64PlatformDataOffset)
                    parent = self.stream.read(loc.dwPlatformDataLength)
                    parent = parent.decode('utf_16_le') # This in Windows format!
                    if DEBUG&16: log("%s: init trying to access parent image '%s'", self.name, parent)
                    if os.path.exists(parent):
                        if DEBUG&16: log("Ok, parent image found.")
            if not parent:
                hparent = self.header.sParentUnicodeName.decode('utf-16be')
                hparent = hparent[:hparent.find('\0')]
                raise BaseException("VHD Differencing Image parent '%s' not found!" % hparent)
            self.Parent = Image(parent, "rb")
            # Windows 11 does NOT check stored timestamps (nor effective creation time)!
            #~ parent_ts = int(time.mktime(time.gmtime(os.stat(parent).st_mtime)))-946681200
            #~ if parent_ts != self.header.dwParentTimeStamp:
                #~ if DEBUG&16: log("TimeStamps: parent=%d self=%d",  parent_ts, self.header.dwParentTimeStamp)
                #~ raise BaseException("Differencing Image timestamp not matched: parent was modified after link!")
            if self.Parent.footer.sUniqueId != self.header.sParentUniqueId:
                raise BaseException("Differencing Image parent's UUID not matched!")
            self.read = self.read1 # assigns special read and write functions
            self.write = self.write1
        if self.footer.dwDiskType == 2: # Fixed VHD
            self.read = self.read0 # assigns special read and write functions
            self.write = self.write0
            self.stream.seek(0, 2)
            if self.stream.tell() - 512 != self.footer.u64CurrentSize:
                raise BaseException("VHD Fixed Image actual size does not match that stored in Footer!")
        self.size = self.footer.u64CurrentSize
        self.seek(0)

    def type(self): return 'VHD'
    
    def cache_flush(self):
        self.stream.flush()

    def flush(self):
        self.stream.flush()

    def seek(self, offset, whence=0):
        # "virtual" seeking, real is performed at read/write time!
        if DEBUG&16: log("%s: seek(0x%X, %d) from 0x%X", self.name, offset, whence, self._pos)
        if not whence:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = self.size + offset
        if self._pos < 0:
            self._pos = 0
        if DEBUG&16: log("%s: final _pos is 0x%X", self.name, self._pos)
        if self._pos >= self.size:
            raise BaseException("%s: can't seek @0x%X past disk end!" % (self.name, self._pos))

    def tell(self):
        return self._pos
    
    def close(self):
        self.stream.close()
        if self.Parent:
            self.Parent.close()
    
    def has_block(self, i):
        "Returns True if the caller or some ascendant has got allocated a block"
        if self.bat[i] != 0xFFFFFFFF or \
        (self.Parent and self.Parent.has_block(i)):
            return True
        return False

    def read0(self, size=-1):
        "Reads (Fixed image)"
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        self.stream.seek(self._pos)
        self._pos += size
        return self.stream.read(size)

    def read(self, size=-1):
        "Reads (Dynamic, non-Differencing image)"
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()
        while size:
            block = self.bat[self._pos//self.block]
            offset = self._pos%self.block
            leftbytes = self.block-offset
            if DEBUG&16: log("reading at block %d, offset 0x%X (vpos=0x%X, epos=0x%X)", self._pos//self.block, offset, self._pos, self.stream.tell())
            if leftbytes <= size:
                got=leftbytes
                size-=leftbytes
            else:
                got=size
                size=0
            self._pos += got
            if block == 0xFFFFFFFF:
                if DEBUG&16: log("block content is virtual (zeroed)")
                buf+=bytearray(got)
                continue
            self.stream.seek(block*512+self.bitmap_size+offset) # ignores bitmap sectors
            buf += self.stream.read(got)
        return buf

    def read1(self, size=-1):
        "Reads (Differencing image)"
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()
        bmp = None
        while size:
            batind = self._pos//self.block
            sector = (self._pos-batind*self.block)//512
            offset = self._pos%512
            leftbytes = 512-offset
            block = self.bat[batind]
            if DEBUG&16: log("%s: reading %d bytes at block %d, offset 0x%X (vpos=0x%X, epos=0x%X)", self.name, size, batind, offset, self._pos, self.stream.tell())
            if leftbytes <= size:
                got=leftbytes
                size-=leftbytes
            else:
                got=size
                size=0
            self._pos += got
            # Acquires Block bitmap once
            if not bmp or bmp.i != block:
                if block != 0xFFFFFFFF:
                    self.stream.seek(block*512)
                    bmp = BlockBitmap(self.stream.read(self.bitmap_size), block)
            if block == 0xFFFFFFFF or not bmp.isset(sector):
                if DEBUG&16: log("reading %d bytes from parent", got)
                self.Parent.seek(self._pos-got)
                buf += self.Parent.read(got)
            else:
                if DEBUG&16: log("reading %d bytes", got)
                self.stream.seek(block*512+self.bitmap_size+sector*512+offset)
                buf += self.stream.read(got)
        return buf

    def write0(self, s):
        "Writes (Fixed image)"
        if DEBUG&16: log("%s: write 0x%X bytes from 0x%X", self.name, len(s), self._pos)
        size = len(s)
        if not size: return
        self.stream.seek(self._pos)
        self._pos += size
        self.stream.write(s)

    def write(self, s):
        "Writes (Dynamic, non-Differencing image)"
        if DEBUG&16: log("%s: write 0x%X bytes from 0x%X", self.name, len(s), self._pos)
        size = len(s)
        if not size: return
        i=0
        while size:
            block = self.bat[self._pos//self.block]
            offset = self._pos%self.block
            leftbytes = self.block-offset
            if leftbytes <= size:
                put=leftbytes
                size-=leftbytes
            else:
                put=size
                size=0
            if block == 0xFFFFFFFF:
                # we keep a block virtualized until we write zeros
                if s[i:i+put] == self.zero[:put]:
                    i+=put
                    self._pos+=put
                    if DEBUG&16: log("block #%d @0x%X is zeroed, virtualizing write", self._pos//self.block, (block*self.block)+self.header.u64DataOffset)
                    continue
                # allocates a new block at end before writing
                self.stream.seek(-512, 2) # overwrites old footer
                block = self.stream.tell()//512
                self.bat[self._pos//self.block] = block
                if DEBUG&16: log("allocating new block #%d @0x%X", self._pos//self.block, block*512)
                self.stream.write(self.bitmap_size*b'\xFF')
                self.stream.seek(self.block, 1)
                self.stream.write(self.footer.pack())
            self.stream.seek(block*512+self.bitmap_size+offset) # ignores bitmap sectors
            if DEBUG&16: log("writing at block %d, offset 0x%X (0x%X), buffer[0x%X:0x%X]", self._pos//self.block, offset, self._pos, i, i+put)
            self.stream.write(s[i:i+put])
            i+=put
            self._pos+=put

    def write1(self, s):
        "Writes (Differencing image)"
        if DEBUG&16: log("%s: write 0x%X bytes from 0x%X", self.name, len(s), self._pos)
        size = len(s)
        if not size: return
        i=0
        bmp = None
        while size:
            block = self.bat[self._pos//self.block]
            offset = self._pos%self.block
            leftbytes = self.block-offset
            if leftbytes <= size:
                put=leftbytes
                size-=leftbytes
            else:
                put=size
                size=0
            if block == 0xFFFFFFFF:
                # we can keep a block virtual until we write zeros and no parent holds it
                if not self.has_block(self._pos//self.block) and s[i:i+put] == self.zero[:put]:
                    i+=put
                    self._pos+=put
                    if DEBUG&16: log("block #%d @0x%X is zeroed, virtualizing write", self._pos//self.block, (block*self.block)+self.header.u64TableOffset)
                    continue
                # allocates a new block at end before writing
                self.stream.seek(-512, 2) # overwrites old footer
                block = self.stream.tell()//512
                self.bat[self._pos//self.block] = block
                if DEBUG&16: log("%s: allocating new block #%d @0x%X", self.name, self._pos//self.block, block*512)
                self.stream.write(bytearray(self.bitmap_size)) # all sectors initially zeroed and unused
                self.stream.write(bytearray(self.block))
                # instead of copying partial sectors from parent, we copy the full block
                #~ self.stream.write(self.bitmap_size*'\xFF')
                #~ self.Parent.seek((self._pos//self.block)*self.block)
                #~ self.stream.write(self.Parent.read(self.block))
                self.stream.write(self.footer.pack())
            if not bmp or bmp.i != block:
                if bmp: # commits bitmap
                    if DEBUG&16: log("%s: flushing bitmap for block #%d before moving", self.name, bmp.i)
                    self.stream.seek(bmp.i*512)
                    self.stream.write(bmp.bmp)
                self.stream.seek(block*512)
                bmp = BlockBitmap(self.stream.read(self.bitmap_size), block)
            def copysect(vpos, sec):
                self.Parent.seek((vpos//512)*512) # src sector offset
                blk = self.bat[vpos//self.block]
                offs = sec*512 # dest sector offset
                if DEBUG&16: log("%s: copying parent sector @0x%X to 0x%X", self.name, self.Parent.tell(), blk*512+self.bitmap_size+offs)
                self.stream.seek(blk*512+self.bitmap_size+offs)
                self.stream.write(self.Parent.read(512))
            start = offset//512
            if offset%512 and not bmp.isset(start): # if middle sector, copy from parent
                copysect(self._pos, start)
                bmp.set(start)
            stop = (offset+put-1)//512
            if (offset+put)%512 and not bmp.isset(stop):
                copysect(self._pos+put-1, stop)
                bmp.set(stop)
            bmp.set(start, stop-start+1) # sets the bitmap range corresponding to sectors written to
            self.stream.seek(block*512+self.bitmap_size+offset)
            if DEBUG&16: log("%s: writing block #%d:0x%X (vpos=0x%X, epos=0x%X), buffer[0x%X:0x%X]", self.name, self._pos//self.block, offset, self._pos, self.stream.tell(), i, i+put)
            self.stream.write(s[i:i+put])
            i+=put
            self._pos+=put
        if bmp: # None if virtual writes only!
            if DEBUG&16: log("%s: flushing bitmap for block #%d at end", self.name, bmp.i)
            self.stream.seek(bmp.i*512)
            self.stream.write(bmp.bmp)

    def merge(self):
        """Merges a Differencing VHD with its parent and erase the image on success.
        Returns None if unsupported image, or a tuple (sectors_merged, blocks_merged)."""
        if not self.Parent: return None
        i = 0
        tot_blocks=0
        tot_sectors=0
        while i < self.bat.size:
            # check block presence
            blkoff = self.bat[i]
            if blkoff == 0xFFFFFFFF:
                i += 1
                continue
            self.Parent.close()
            self.Parent = Image(self.Parent.name, "r+b") # reopen Parent in RW mode
            # load and scan the block bitmap
            j = 0
            self.stream.seek(blkoff*512)
            bmp = BlockBitmap(self.stream.read(self.bitmap_size), i)
            copied=0
            while j < self.bitmap_size*8:
                if bmp.isset(j):
                    # read the sector
                    self.stream.seek(blkoff*512 + self.bitmap_size + j*512)
                    s = self.stream.read(512)
                    # seek absolute position in parent and copy
                    self.Parent.seek(i*self.block + j*512)
                    self.Parent.write(s)
                    tot_sectors+=1
                    copied=1
                j += 1
            if copied: tot_blocks+=1
            i += 1
        if DEBUG&16: log("%s: merged %d sectors in %d blocks",self.name,tot_sectors,tot_blocks)
        self.close()
        os.remove(self.name)
        return (tot_sectors, tot_blocks)

def mk_chs(size):
    "Given a disk size, computates and returns as a string the pseudo CHS for VHD Footer"
    sectors = size//512
    if sectors > 65535 * 16 * 255:
        sectors = 65535 * 16 * 255
    if sectors >= 65535 * 16 * 63:
        spt = 255
        hh = 16
        cth = sectors // spt
    else:
        spt = 17
        cth = sectors // spt
        hh = (cth+1023)//1024
        if hh < 4: hh = 4
        if cth >= hh*1024 or hh > 16:
            spt = 31
            hh = 16
            cth = sectors // spt
        if cth >= hh*1024:
            spt = 63
            hh = 16
            cth = sectors // spt
    cyls = cth // hh
    return struct.pack('>HBB', cyls, hh, spt)



def mk_crc(s):
    "Computates and returns as a string the CRC for some disk structures"
    crc = 0
    for b in s: crc += b
    return struct.pack('>i', ~crc)


def mk_fixed(name, size, overwrite='no', sector=512):
    "Creates an empty fixed VHD or transforms a previous image if 'size' is -1"
    if size > MAX_VHD_SIZE:
        raise BaseException("Can't create a VHD >2040 GiB!")
    if os.path.exists(name):
        if size != -1 and overwrite!='yes':
            raise BaseException("Can't silently overwrite a pre-existing VHD image!")
    if os.path.exists(name):
        f = myfile(name, 'r+b')
        if size == -1:
            f.seek(0, 2)
            size = f.tell()
        f.seek(size)
        f.truncate()
        if DEBUG&16: log("making new Fixed VHD '%s' of %.02f MiB from pre-existant image", name, float(size//(1<<20)))
    else:
        if DEBUG&16: log("making new Fixed VHD '%s' of %.02f MiB", name, float(size//(1<<20)))
        f = myfile(name, 'wb')
        f.seek(size) # quickly allocates space

    ft = Footer()
    ft.sCookie = b'conectix'
    ft.dwFeatures = 2
    ft.dwFileFormatVersion = 0x10000
    ft.u64DataOffset = 0xFFFFFFFFFFFFFFFF
    ft.dwTimestamp = int(time.mktime(time.gmtime()))-946681200
    ft.dwCreatorApp = b'Py  '
    ft.dwCreatorVer = 0x3000A
    ft.dwCreatorHost = b'Wi2k'
    ft.u64OriginalSize = size
    ft.u64CurrentSize = size
    ft.dwDiskGeometry = mk_chs(size)
    ft.dwDiskType = 2
    ft.sUniqueId = uuid.uuid4().bytes
    
    f.write(ft.pack()) # stores Footer
    f.flush(); f.close()



def mk_dynamic(name, size, block=(2<<20), upto=0, overwrite='no', sector=512):
    "Creates an empty dynamic VHD"
    if size > MAX_VHD_SIZE:
        raise BaseException("Can't create a VHD >2040 GiB!")
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VHD image!")

    ft = Footer()
    ft.sCookie = b'conectix'
    ft.dwFeatures = 2
    ft.dwFileFormatVersion = 0x10000
    ft.u64DataOffset = 512
    ft.dwTimestamp = int(time.mktime(time.gmtime()))-946681200
    ft.dwCreatorApp = b'Py  '
    ft.dwCreatorVer = 0x3000A
    ft.dwCreatorHost = b'Wi2k'
    ft.u64OriginalSize = size
    ft.u64CurrentSize = size
    ft.dwDiskGeometry = mk_chs(size)
    ft.dwDiskType = 3
    ft.sUniqueId = uuid.uuid4().bytes
    
    if DEBUG&16: log("making new Dynamic VHD '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)

    f = myfile(name, 'wb')
    f.write(ft.pack()) # stores footer copy
    
    h=DynamicHeader()
    h.sCookie = b'cxsparse'
    h.u64DataOffset = 0xFFFFFFFFFFFFFFFF
    h.u64TableOffset = 1536
    h.dwVersion = 0x10000
    h.dwMaxTableEntries = (size+block-1)//block # blocks needed
    h.dwBlockSize = block
    
    f.write(h.pack()) # stores dynamic header
    bmpsize = (4*h.dwMaxTableEntries+511)//512*512 # must span full sectors
    # Given a maximum virtual size in upto, the BAT is enlarged
    # for future VHD expansion
    if upto > size:
        bmpsize = (4*((upto+block-1)//block)+511)//512*512
        if DEBUG&16: log("BAT extended to %d blocks, VHD is resizable up to %.02f MiB", bmpsize//4, float(upto//(1<<20)))
    f.write(bmpsize*b'\xFF') # initializes BAT
    f.write(ft.pack()) # stores footer
    f.flush(); f.close()



def mk_diff(name, base, overwrite='no', sector=512):
    "Creates an empty differencing VHD"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VHD image!")
    ima = Image(base)
    # Parent's unique references: UUID and creation time
    parent_uuid = ima.footer.sUniqueId
    parent_ts = ima.footer.dwTimestamp
    ima.footer.dwDiskType = 4
    ima.footer.dwCreatorApp = b'Py  '
    ima.footer.dwCreatorVer = 0x3000A
    ima.footer.dwCreatorHost = b'Wi2k'
    ima.footer.dwTimestamp = int(time.mktime(time.gmtime()))-946681200
    ima.footer.sUniqueId = uuid.uuid4().bytes
   
    if DEBUG&16: log("making new Differencing VHD '%s' of %.02f MiB", name, float(ima.size//(1<<20)))

    f = myfile(name, 'wb')
    f.write(ima.footer.pack()) # stores footer copy

    rel_base = calc_rel_path(base, name) # gets the path of base image relative to its child
    if rel_base[0] != '.': rel_base = '.\\'+rel_base
    rel_base = rel_base.encode('utf_16_le')
    abs_base = os.path.abspath(base).encode('utf_16_le')
    be_base = os.path.abspath(base).encode('utf_16_be')+b'\0\0'
    
    ima.header.sParentUniqueId = parent_uuid
    ima.header.dwParentTimeStamp = parent_ts # Windows 11, however, does NOT check stored timestamps (nor effective creation time)!
    ima.header.sParentUnicodeName = be_base # Windows fixes this, but is it not mandatory (see below)
    
    loc = ima.header.locators

    for i in range(8):
        loc[i].dwPlatformCode = b'\0\0\0\0'
        loc[i].dwPlatformDataSpace = 0
        loc[i].dwPlatformDataLength = 0
        loc[i].u64PlatformDataOffset = 0

    bmpsize=((ima.header.dwMaxTableEntries*4+511)//512)*512

    # Windows needs *at least* a valid *file* name in W2ru field!
    #
    # Windows 10 stores the relative pathname with '.\' for current dir
    # It stores both absolute and relative pathnames, tough it isn't
    # strictly necessary (but disk manager silently fixes this)
    #
    # Old ASCII Wi2k and Wi2r locators are not recognized
    loc[0].dwPlatformCode = b'W2ru'
    loc[0].dwPlatformDataSpace = ((len(rel_base)+511)//512)*512
    loc[0].dwPlatformDataLength = len(rel_base)
    loc[0].u64PlatformDataOffset = 1536+bmpsize

    # Windows 11 detects a VHD as damaged if absolute pathname stored here is wrong!!
    loc[1].dwPlatformCode = b'W2ku'
    loc[1].dwPlatformDataSpace = ((len(abs_base)+511)//512)*512
    loc[1].dwPlatformDataLength = len(abs_base)
    loc[1].u64PlatformDataOffset = loc[0].u64PlatformDataOffset+loc[0].dwPlatformDataSpace
        
    f.write(ima.header.pack()) # stores dynamic header

    f.write(bmpsize*b'\xFF') # initializes BAT

    f.write(rel_base+b'\0'*(loc[0].dwPlatformDataSpace-len(rel_base))) # stores relative parent locator sector
    f.write(abs_base+b'\0'*(loc[1].dwPlatformDataSpace-len(abs_base))) # stores absolute parent locator sector

    f.write(ima.footer.pack()) # stores footer
    f.flush(); f.close()



if __name__ == '__main__':
    import os
    mk_fixed('test.vhd', 64<<20)
    vhd = Image('test.vhd'); vhd.close()
    mk_dynamic('test.vhd', 1<<30, upto=40<<30, overwrite='yes')
    vhd = Image('test.vhd'); vhd.close()
    print('_isvalid returned', vhd.bat.isvalid)
    mk_diff('testd.vhd', 'test.vhd', overwrite='yes')
    vhd = Image('testd.vhd'); vhd.close()
    print('_isvalid returned', vhd.bat.isvalid)
    os.remove('testd.vhd')
    os.remove('test.vhd')
