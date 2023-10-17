# -*- coding: cp1252 -*-
"Utilities to handle VDI disk images"

""" VDI IMAGE FILE FORMAT

Actually, all structures are aligned at 1 MB (they were at 512 bytes then 4K)
and integers are in Little-Endian format.

The normal image type is Dynamic VDI.

A Dynamic VDI starts with the header; then, at fist MB, the block allocation
table; finally the raw data blocks array.
Disk is virtually subdivided into blocks of equal size (1 MB or multiple) with
a corresponding DWORD BAT index showing what block in data area actually holds
a virtual block. Data area starts immediately after the BAT area.
Initially all BAT indexes are present and set to 0xFFFFFFFF, signaling an
unallocated block: reading it may return arbitrary contents.
Blocks are allocated on write and put at image's end, so they appear in
arbitrary order.
A value of 0xFFFFFFFE means a virtually allocated and zeroed block.

A Differencing VDI is a Dynamic VDI linked with its parent by the sUuidLinkage
and sUuidParentModify members: checking the latter ensures the base image was
not modified after creation of the delta image.

A Fixed VDI is like a Dynamic one, also: but all blocks are allocated at
creation time."""
import atexit, io, struct, uuid, zlib, ctypes, time, os, math, glob

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
import FATtools.utils as utils
from FATtools.debug import log
from FATtools.utils import myfile



class Header(object):
    "VDI 1.1 Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sDescriptor', '64s'),
    0x40: ('dwSignature', '<I'), # 0xBEDA107F
    0x44: ('dwVersion', '<I'), #0x10001
    0x48: ('dwHeaderSize', '<I'),
    0x4C: ('dwImageType', '<I'), # 1=dynamic 2=fixed 3=undo 4=differencing
    0x50: ('dwFlags', '<I'),
    0x54: ('sDescription', '256s'), # image description
    0x154: ('dwBATOffset', '<I'), # offset of blocks allocation table
    0x158: ('dwBlocksOffset', '<I'), # offset of data area
    0x15C: ('dwCylinders', '<I'), # geometry (not used in 1.1: see later)
    0x160: ('dwHeads', '<I'),
    0x164: ('dwSectors', '<I'),
    0x168: ('dwSectorSize', '<I'),
    0x16C: ('dwUnused', '<I'),
    0x170: ('u64CurrentSize', '<Q'), # virtual disk size
    0x178: ('dwBlockSize', '<I'), # min 1MB
    0x17C: ('dwBlockExtraSize', '<I'), # data (sector aligned) preceding a block, if any
    0x180: ('dwTotalBlocks', '<I'), # total disk blocks (BAT size)
    0x184: ('dwAllocatedBlocks', '<I'), # blocks allocated in data area
    0x188: ('sUuidCreate', '16s'), # set at image creation time
    0x198: ('sUuidModify', '16s'), # set at every image modification
    0x1A8: ('sUuidLinkage', '16s'), # parent's sUuidCreate
    0x1B8: ('sUuidParentModify', '16s'), # parent's sUuidModify at creation of this snapshot
    0x1C8: ('dwCylinders', '<I'), # disk geometry (1.1: actually zero, except dwSectorSize)
    0x1CC: ('dwHeads', '<I'),
    0x1D0: ('dwSectors', '<I'),
    0x1D4: ('dwSectorSize', '<I') # only 512 seems supported
    # REST IS UNUSED
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
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "VDI Header @%X\n" % self._pos)
    
    def isvalid(self):
        if self.dwSignature != 0xBEDA107F:
            return 0
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
        #~ self._isvalid() # performs self test

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
        slot = struct.unpack("<I", self.stream.read(4))[0]
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
        value = struct.pack("<I", value)
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
        raw_size =  self.bsize # RAW block size, including bitmap
        last_block = ssize - raw_size # theoretical offset of last block
        first_block = last_block%raw_size # theoretical address of first block
        bat_size = utils.roundMB(self.size*4)
        allocated = (ssize-bat_size)//raw_size
        unallocated = 0
        seen = []
        for i in range(self.size):
            a = self[i]
            if a == 0xFFFFFFFF or a == 0xFFFFFFFE:
                unallocated+=1
                continue
            if a in seen:
                self.isvalid = -2 # duplicated block address
                if DEBUG&16: log("%s: BAT[%d] offset (sector %X) was seen more than once", self, i, a)
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) was seen more than once" %(i, a))
            if a*self.bsize > last_block or a*self.bsize+raw_size > ssize:
                if DEBUG&16: log("%s: block %d offset (sector %X) exceeds allocated file size", self, i, a)
                self.isvalid = -3 # block address beyond file's end detected
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) exceeds allocated file size" %(i, a))
            if (a*self.bsize-first_block)%raw_size:
                if DEBUG&16: log("%s: BAT[%d] offset (sector %X) is not aligned", self, i, a)
                self.isvalid = -4 # block address not aligned
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) is not aligned, overlapping blocks" %(i, a))
            seen += [a]
        if unallocated + allocated != self.size:
            if DEBUG&16: log("%s: BAT has %d blocks allocated only, container %d", self, len(seen), allocated)
            self.isvalid = 0
            if selftest: return
            print("WARNING: BAT has %d blocks allocated only, container %d" % (len(seen), allocated))



class Image(object):
    def __init__ (self, name, mode='rb'):
        atexit.register(self.close)
        self.tstamp = os.stat(name).st_mtime # records last mod time stamp
        self._pos = 0 # offset in virtual stream
        self.size = 0 # size of virtual stream
        self.name = name
        self.stream = myfile(name, mode)
        self._file = self.stream
        self.mode = mode
        self.stream.seek(0, 2)
        size = self.stream.tell()
        self.Parent = None
        self.stream.seek(0)
        self.header = Header(self.stream.read(512), 512)
        if not self.header.isvalid():
            raise BaseException("VDI Image Dynamic Header is not valid!")
        self.block = self.header.dwBlockSize
        self.zero = bytearray(self.block)
        self.bat = BAT(self.stream, self.header.dwBATOffset, self.header.dwTotalBlocks, self.block)
        if self.bat.isvalid < 0:
            error = {-1: "insufficient container size", -2: "duplicated block address", -3: "block past end", -4: "misaligned block"}
            raise BaseException("VDI Image is not valid: %s", error[self.bat.isvalid])
        if self.header.dwImageType == 4: # Differencing VDI
            parent=''
            for vdi in glob.glob('./*.vdi'):
                if vdi.lower() == name.lower(): continue
                parent=vdi
                o=Image(vdi, "rb")
                if o.header.sUuidCreate == self.header.sUuidLinkage:
                    break
                parent=''
            if os.path.exists(parent):
                if DEBUG&16: log("Ok, parent image found.")
            if not parent:
                raise BaseException("VDI Differencing Image parent not found!")
            self.Parent = Image(parent, "rb")
            if self.Parent.header.sUuidCreate != self.header.sUuidLinkage:
                raise BaseException("VDI images not linked, sUuidLinkage!=sUuidCreate")
            if self.Parent.header.sUuidModify != self.header.sUuidParentModify:
                raise BaseException("VDI Image parent's was altered after snapshot!")
        self.size = self.header.u64CurrentSize
        self.seek(0)

    def type(self): return 'VDI'
    
    def has_block(self, i):
        "Tests if a block is effectively allocated by the image or its parent"
        if self.bat[i] != 0xFFFFFFFF: # unallocated
            return True
        if self.Parent:
            return self.Parent.has_block(i)
        return False

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
        if self.Parent:
            self.Parent.seek(self._pos) # propagate seek through a parents chain

    def tell(self):
        return self._pos
    
    def close(self):
        if self.stream.mode != "rb" and self.tstamp != os.stat(self.name).st_mtime:
            if DEBUG&16: log("%s: VDI container was modified, updating header", self.name)
            # Updates header once (dwAllocatedBlocks and sUuidModify) if written
            try:
                f=self.stream
                f.seek(0, 2)
                pos=f.tell()
                self.header.dwAllocatedBlocks = (pos-self.header.dwBlocksOffset)//self.block
                self.header.sUuidModify = uuid.uuid4().bytes
                f.seek(0)
                f.write(self.header.pack())
            except:
                if DEBUG&16: log("exception in vdiutils.Image.close!")
        self.stream.close()
        
    def read(self, size=-1):
        "Reads (Normal, Differencing image)"
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()
        while size:
            block = self.bat[self._pos//self.block]
            offset = self._pos%self.block
            leftbytes = self.block-offset
            if DEBUG&16: log("%s: reading at block %d, offset 0x%X (vpos=0x%X, epos=0x%X)", self.name, self._pos//self.block, offset, self._pos, self.stream.tell())
            if leftbytes <= size:
                got=leftbytes
                size-=leftbytes
            else:
                got=size
                size=0
            self._pos += got
            if block==0xFFFFFFFF or block==0xFFFFFFFE:
                if self.Parent and block==0xFFFFFFFF:
                    if DEBUG&16: log("%s: reading %d bytes from parent", self.name, got)
                    self.Parent.seek(self._pos-got)
                    buf += self.Parent.read(got)
                else:
                    if DEBUG&16: log("%s: block content is virtual (zeroed)", self.name)
                    buf+=bytearray(got)
                continue
            else:
                self.stream.seek(self.header.dwBlocksOffset+block*self.block+self.header.dwBlockExtraSize+offset)
                buf += self.stream.read(got)
        return buf

    def write(self, s):
        "Writes (Normal, Differencing image)"
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
            if block==0xFFFFFFFF or block==0xFFFFFFFE:
                if block==0xFFFFFFFF and self.Parent and self.Parent.has_block(self._pos//self.block):
                    # copies block from parent if it has one allocated
                    self.stream.seek(0, 2)
                    block = (self.stream.tell()-self.header.dwBlocksOffset)//self.block
                    self.bat[self._pos//self.block] = block
                    self.Parent.seek(self._pos//self.block*self.block)
                    self.stream.write(self.Parent.read(self.block))
                    if DEBUG&16: log("copied old block #%d @0x%X", self._pos//self.block, (block*self.block)+self.header.dwBlocksOffset)
                else:
                    # we keep a block virtualized until we write zeros
                    if s[i:i+put] == self.zero[:put]:
                        if block==0xFFFFFFFF:
                            self.bat[self._pos//self.block] = 0xFFFFFFFE
                        i+=put
                        self._pos+=put
                        if DEBUG&16: log("block #%d @0x%X is zeroed, virtualizing write", self._pos//self.block, (block*self.block)+self.header.dwBlocksOffset)
                        continue
                    else:
                        # allocates a new block at end before writing
                        self.stream.seek(0, 2)
                        block = (self.stream.tell()-self.header.dwBlocksOffset)//self.block
                        self.bat[self._pos//self.block] = block
                        self.stream.seek(self.block-1, 1)
                        self.stream.write(b'\x00') # force effective block allocation
                        if DEBUG&16: log("allocating new block #%d @0x%X", self._pos//self.block, (block*self.block)+self.header.dwBlocksOffset)
            self.stream.seek(self.header.dwBlocksOffset+block*self.block+self.header.dwBlockExtraSize+offset)
            if DEBUG&16: log("writing at block %d, offset 0x%X (0x%X), buffer[0x%X:0x%X]", self._pos//self.block, offset, self._pos, i, i+put)
            self.stream.write(s[i:i+put])
            i+=put
            self._pos+=put


def _mk_common(name, size, block, overwrite):
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VDI image!")
    if block < (1<<20):
        raise BaseException("Block size must be 1 MB or multiple!")
    h = Header()
    s='<<< Python3 vdiutils VDI Disk Image >>>\n'
    h.dwBlockSize = block
    h.dwTotalBlocks = (size+block-1)//block
    h.sDescriptor = s.encode()+bytearray(64-len(s))
    h.dwSignature = 0xBEDA107F
    h.dwVersion = 0x10001
    h.dwHeaderSize = 0x200
    h.dwBATOffset = (1<<20)
    h.dwBlocksOffset = h.dwBATOffset + utils.roundMB(h.dwTotalBlocks*4)
    h.u64CurrentSize = size
    h.sUuidCreate = uuid.uuid4().bytes
    h.sUuidModify = uuid.uuid4().bytes
    #~ h.dwHeads = 255
    #~ h.dwSectors = 63
    h.dwSectorSize = 512
    #~ h.dwCylinders = (size+8225279)//8225280
    return h


def mk_fixed(name, size, block=(1<<20), overwrite='no', sector=512):
    "Creates an empty fixed VDI or transforms a previous image"
    h = _mk_common(name, size, block, overwrite)
    h.dwAllocatedBlocks = size//block
    h.dwImageType = 2
       
    if DEBUG&16: log("making new Fixed VDI '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)

    f = myfile(name, 'wb')
    s = h.pack()
    f.write(s)
    f.write(bytearray((1<<20)-len(s)))
    
    bmpsize = h.dwTotalBlocks*4
    # Makes a consecutive array of DWORDs
    L = [struct.pack('<I', x) for x in range(h.dwTotalBlocks)]
    run = bytearray().join(L)
    # initializes BAT, MB aligned
    f.write(run)
    f.write(bytearray(h.dwBlocksOffset-bmpsize))
    # quickly allocates all blocks
    f.seek(h.dwTotalBlocks*h.dwBlockSize-1)
    f.write(b'\x00')
    f.flush(); f.close()


def mk_dynamic(name, size, block=(1<<20), overwrite='no', sector=512):
    "Creates an empty dynamic VDI"
    h = _mk_common(name, size, block, overwrite)
    h.dwImageType = 1
       
    if DEBUG&16: log("making new Dynamic VDI '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)

    f = myfile(name, 'wb')
    s = h.pack()
    f.write(s)
    f.write(bytearray((1<<20)-len(s)))
    
    # initializes BAT, MB aligned
    bmpsize = h.dwTotalBlocks*4
    f.write(bmpsize*b'\xFF')
    f.write(bytearray(h.dwBlocksOffset-bmpsize))
    f.flush(); f.close()


def mk_diff(name, base, overwrite='no', sector=512):
    "Creates an empty differencing VDI"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VDI image!")
    ima=Image(base)
    ima.header.dwImageType=4
    # Stores parent's UUIDs
    ima.header.sUuidLinkage = ima.header.sUuidCreate
    ima.header.sUuidParentModify = ima.header.sUuidModify
    # Makes new image's UUIDs
    ima.header.sUuidCreate=uuid.uuid4().bytes
    ima.header.sUuidModify=uuid.uuid4().bytes

    if DEBUG&16: log("making new Differencing VDI '%s' of %.02f MiB", name, float(ima.size//(1<<20)))

    f = myfile(name, 'wb')
    s=ima.header.pack()
    f.write(s)
    f.write(bytearray((1<<20)-len(s)))
    
    bmpsize = ima.header.dwTotalBlocks*4
    f.write(bmpsize*b'\xFF') # initializes BAT
    f.write(bytearray((ima.header.dwBlocksOffset-ima.header.dwBATOffset)-bmpsize))
    f.flush(); f.close()




if __name__ == '__main__':
    import os
    mk_fixed('test.vdi', 64<<20, overwrite='yes')
    vdi = Image('test.vdi'); vdi.close()
    mk_dynamic('test.vdi', 1<<30, overwrite='yes')
    vdi = Image('test.vdi', 'r+b')
    vdi.bat._isvalid(selftest=0)
    vdi.write(bytearray(4<<20))
    print('_isvalid returned', vdi.bat.isvalid)
    vdi.close()
    os.remove('test.vdi')
