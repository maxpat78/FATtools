# -*- coding: cp1252 -*-
"Utilities to handle VMDK disk images"

""" VMDK SPARSE IMAGE FILE FORMAT

It is described by a textual file, typically standalone.
This descriptor contains a 32-bit content identifier (CID), altered every time
the disk is written to; and a parent CID if it is a differential disk.
Most important, the descriptor tells where the binary disk chunks reside and
how they are composed and accessed.

A sparse file is made by fixed raw chunks or growable VMDK extents.

A VMDK extent contains an header sector, a redundant Grain Directory (GD), a
GD, a Grain Table (GT) for each entry in a GD and, finally, an array of Grains
representing raw disk data.
A GD entry is a 32-bit sector offset of a GT; a GT is a table of 32-bit Grain
Table Entries (GTEs) or sector offsets of Grains: it contains always 512
entries, so it's 2K fixed.
A Grain is a block containing raw disk data and is typically 64K (must be
a power of 2 and at least 4K).
There are 2 copies of GD with a GT each.
All these structures (metadata) are initialized when the extent is created, so
the GD is not really needed to access a GT: an array of GTs follows a GD.
A GTE set to 0 signals an unallocated Grain (reading returns zeros or the
parent's grain contents if any). A GTE set to 1 means the Grain is allocated
but still zeroed.
Grains are allocated on write and appear in any order (SPARSE extent) or are
allocated at extent creation (FLAT).
A virtual disk can be contained in a single monolithic file or span multiple
files (a disk can actually reach 62TB but a GTE can address sectors in a 2 TB
range only)."""
import atexit, io, struct, uuid, zlib, ctypes, time
import os, math, re, random

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
import FATtools.utils as utils
from FATtools.debug import log
from FATtools.utils import myfile



def parse_ddf(f, size=-1):
    "Parses a VMDK Disk DescriptorFile"
    params={'raw':'', 'version':0, 'CID':0, 'parentCID':0, 'parentFileNameHint':'', 'createType':'', 'extents':[]}
    max_offset = 0
    for r in f.read(size).split('\n'):
        if r:
            params['raw'] += r + '\n'
        if r.startswith('#'): continue
        m=re.match('(version)\s*[=]+\s*([0-9]{1})', r)
        if m: params[m.group(1)] = int(m.group(2))
        m=re.match('(CID)\s*[=]+\s*([0-9A-Fa-f]{8})', r)
        if m: params[m.group(1)] = int(m.group(2), 16)
        m=re.match('(parentCID)\s*[=]+\s*([0-9A-Fa-f]{8})', r)
        if m: params[m.group(1)] = int(m.group(2), 16)
        m=re.match('(parentFileNameHint)\s*[=]+\s*"(.+)"', r)
        if m: params[m.group(1)] = m.group(2)
        m=re.match('(createType)\s*[=]+\s*(.+)', r)
        if m: params[m.group(1)] = m.group(2)
        m=re.match('(RW|RDONLY)\s+(\d+)\s+(SPARSE|FLAT)\s+"(.+)"', r)
        if m:
            ext={}
            ext['start'] = max_offset # starting virtual offset in this extent, relative to global stream
            ext['mode']=m.group(1)
            ext['size']=int(m.group(2))*512 # convert sectors in bytes
            ext['type']=m.group(3)
            ext['name']=m.group(4)
            max_offset+=ext['size']
            ext['end']=max_offset-1 # ending virtual offset in this extent
            params['extents'] += [ext]
    #~ print('DEBUG:ddf:%s'%params)
    return params

def calc_ext_meta_size(size, grainsize=65536):
    "Calculates Extent and metadata size given the virtual disk size"
    # Extent max size *MUST* be <2TB since sectors offsets are 32-bit!
    grains = (size+grainsize-1)//grainsize # grains to represent the extent
    gtsize = (grains*4+511)//512 # sectors occupied by a Grain Tables array
    gtn = (gtsize+3)//4 # number of Grain Tables needed
    gdsize = (gtn*4+511)//512 # sectors occupied by a Grain Directory
    grainsizes = grainsize//512 # Grain size in sectors
    # Since an Extent must be a Grain multiple, overhead must be too!
    overhead = (2*(gdsize+gtsize)+grainsizes)//grainsizes

    block, meta = 0,0
    for i in range(20, 41):
        meta=0
        if i==40:
            meta = 1 << int(math.log(grainsize*overhead, 2))
        block = (2<<i)-meta
        if size < block: break
    return [block, gdsize, gtsize, overhead]



class Header(object):
    "VMDK Sparse Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('dwMagicNumber', '<I'), # VMDK
    0x04: ('dwVersion', '<I'), # 1, 2 or 3
    0x08: ('dwFlags', '<I'), # default 3 (0=valid new line detection 1=redundant GT used 16=compressed grains 17=meta markers)
    0x0C: ('u64Capacity', '<Q'), # virtual size of the extent, in sectors
    0x14: ('u64GrainSize', '<Q'), # default 0x80 (128) sectors or 64K (should be power of 2 >=4K)
    0x1C: ('u64DescriptorOffset', '<Q'), # next 2 used if the textual VMDK descriptor is embedded
    0x24: ('u64DescriptorSize', '<Q'),
    0x2C: ('dwGTEsPerGT', '<I'), # always 0x200 (512) or 2K
    0x30: ('u64RGDOffset', '<Q'), # sector of redundant Grain Directory (typically 1)
    0x38: ('u64GDOffset', '<Q'), # sector of Grain Directory
    0x40: ('u64OverHead', '<Q'), # sectors occupied by metadata
    0x48: ('bUncleanShutdown', 'B'),
    0x49: ('cSingleEndLineChar', 'B'), # 0xA
    0x4A: ('cNonEndLineChar', 'B'), # 0x20
    0x4B: ('cDoubleEndLineChar1', 'B'), # 0xD
    0x4C: ('cDoubleEndLineChar2', 'B'), # 0xA
    0x4D: ('usCompressAlgorithm', '<H') # 1=Deflate
    # 433 bytes padding
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
        return utils.class2str(self, "VMDK Header @%X\n" % self._pos)
    
    def isvalid(self):
        if self.dwMagicNumber!=0x564D444B or self.dwVersion!=1 or self.cSingleEndLineChar!=0xA \
        or self.cNonEndLineChar!=0x20 or self.cDoubleEndLineChar1!=0xD or self.cDoubleEndLineChar2!=0xA:
            return 0
        return 1



class BAT(object):
    "Implements the Grain Tables array as indexable object"
    def __init__ (self, stream, offset, blocks, block_size):
        self.stream = stream
        self.size = blocks # total blocks in the data area
        self.bsize = block_size # block size
        self.offset = offset # relative BAT offset
        self.decoded = {} # {block index: block effective sector}
        self.isvalid = 1 # self test result
        self._isvalid() # performs self test
        x = calc_ext_meta_size(blocks*block_size, block_size)
        self.offset2 = offset + x[1]*512 + x[2]*512

    def __str__ (self):
        return "Grain Table of %d blocks starting @%Xh\n" % (self.size, self.offset)

    def __getitem__ (self, index):
        "Retrieves the value stored in a given block index"
        if index < 0:
            index += self.size
        if DEBUG&16: log("%s: requested to read GT[0x%X]", self.stream.name, index)
        if not (0 <= index <= self.size-1):
            raise BaseException("Attempt to read a #%d block past disk end"%index)
        slot = self.decoded.get(index)
        if slot: return slot
        pos = self.offset + index*4
        opos = self.stream.tell()
        self.stream.seek(pos)
        slot = struct.unpack("<I", self.stream.read(4))[0]
        self.decoded[index] = slot
        if DEBUG&16: log("%s: got GT[0x%X]=0x%X @0x%X", self.stream.name, index, slot, pos)
        self.stream.seek(opos) # rewinds
        return slot

    def __setitem__ (self, index, value):
        "Sets the value stored in a given block index"
        if index < 0:
            index += self.size
        if index > self.size-1:
            raise BaseException("Can't set a BAT index beyond its size!")
        self.decoded[index] = value
        dsp = index*4
        pos = self.offset+dsp
        opos = self.stream.tell()
        # Sets copy #1
        self.stream.seek(pos)
        value = struct.pack("<I", value)
        self.stream.write(value)
        if DEBUG&16: log("%s: set GT1[0x%X]=0x%s @0x%X", self.stream.name, index, value, pos)
        # Sets copy #2
        pos = self.offset2+dsp
        if DEBUG&16: log("%s: set GT2[0x%X]=0x%s @0x%X", self.stream.name, index, value, pos)
        self.stream.seek(pos)
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
        raw_size =  self.bsize # RAW block size
        last_block = ssize - raw_size # theoretical offset of last block
        first_block = last_block%raw_size # theoretical address of first block
        bat_size = self.size*4+511//512*512
        allocated = (ssize-bat_size)//raw_size
        unallocated = 0
        seen = []
        for i in range(self.size):
            a = self[i]
            if not a:
                unallocated+=1
                continue
            if a in seen:
                self.isvalid = -2 # duplicated block address
                if DEBUG&16: log("%s: BAT[%d] offset (sector %X) was seen more than once", self, i, a)
                if selftest: break
                print("ERROR: BAT[%d] offset (sector %X) was seen more than once" %(i, a))
            if a*512 > last_block or a*512+raw_size > ssize:
                if DEBUG&16: log("%s: block %d offset (sector 0x%X) exceeds allocated file size", self, i, a)
                self.isvalid = -3 # block address beyond file's end detected
                if selftest: break
                print("ERROR: BAT[%d] offset (sector 0x%X) exceeds allocated file size" %(i, a))
            if (a*512-first_block)%raw_size:
                if DEBUG&16: log("%s: BAT[%d] offset (sector 0x%X) is not aligned", self, i, a)
                self.isvalid = -4 # block address not aligned
                if selftest: break
                print("ERROR: BAT[%d] offset (sector 0x%X) is not aligned, overlapping blocks" %(i, a))
            seen += [a]
        if unallocated + allocated != self.size:
            if DEBUG&16: log("%s: BAT has %d blocks allocated only, container %d", self, len(seen), allocated)
            self.isvalid = 0
            if selftest: return
            print("WARNING: BAT has %d blocks allocated only, container %d" % (len(seen), allocated))



class Extent(object):
    "Handles a VMDK disk chunk (at most ~2TB)"
    def __init__ (self, name, mode='rb'):
        self.tstamp = os.stat(name).st_mtime # records last mod time stamp
        self.closed = False
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
        self.header = Header(self.stream.read(512), 0)
        if not self.header.isvalid():
            raise BaseException("VMDK Header is not valid in Extent '%s'!"%name)
        self.block = self.header.u64GrainSize*512
        self.zero = bytearray(self.block)
        h=self.header
        blocks = h.u64Capacity//h.u64GrainSize
        # Grain Tables in this extent
        gts = (blocks+h.dwGTEsPerGT-1)//h.dwGTEsPerGT
        # Grain Directory size
        gdsize = ((gts*4+511)//512)*512 # rounding at sect size
        self.bat = BAT(self.stream, h.u64RGDOffset*512+gdsize, blocks, self.block)
        if self.bat.isvalid < 0:
            error = {-1: "insufficient container size", -2: "duplicated block address", -3: "block past end", -4: "misaligned block"}
            raise BaseException("VMDK Extent is not valid: %s", error[self.bat.isvalid])
        self.size = h.u64Capacity*512
        self.seek(0)
        if DEBUG: log("Inited VMDK Extent '%s': %s", self.name, self.header)

    def has_block(self, i):
        "Tests if a block is effectively allocated by the Extent or its parent"
        if self.Parent:
            return self.Parent.has_block(i)
        if self.bat[i]:
            return True
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

    def tell(self):
        return self._pos
    
    def close(self):
        self.stream.close()
        self.closed = True

    def read(self, size=-1):
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()
        while size:
            block = self.bat[self._pos//self.block]
            offset = self._pos%self.block
            leftbytes = self.block-offset
            if DEBUG&16: log("%s: reading Grain %d, offset 0x%X (vpos=0x%X, epos=0x%X)", self.name, self._pos//self.block, offset, self._pos, self.stream.tell())
            if leftbytes <= size:
                got=leftbytes
                size-=leftbytes
            else:
                got=size
                size=0
            self._pos += got
            if not block:
                if self.Parent:
                    if DEBUG&16: log("%s: reading %d bytes from parent", self.name, got)
                    self.Parent.seek(self._pos-got)
                    buf += self.Parent.read(got)
                else:
                    if DEBUG&16: log("%s: grain content is virtual (zeroed)", self.name)
                    buf+=bytearray(got)
                continue
            self.stream.seek(block*512+offset)
            buf += self.stream.read(got)
        return buf

    def write(self, s):
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
            if block==0 or block==1:
                if not block and self.Parent and self.Parent.has_block(self._pos//self.block):
                    # copies block from parent if it has one allocated
                    self.stream.seek(0, 2)
                    block = self.stream.tell()//512
                    self.bat[self._pos//self.block] = block
                    self.Parent.seek(self._pos//self.block*self.block)
                    self.stream.write(self.Parent.read(self.block))
                    if DEBUG&16: log("copied old block #%d @0x%X", self._pos//self.block, block*self.block)
                else:
                    # we keep a block virtualized until we write zeros
                    if s[i:i+put] == self.zero[:put]:
                        if not block:
                            self.bat[self._pos//self.block] = 1
                        i+=put
                        self._pos+=put
                        if DEBUG&16: log("Grain #%d @0x%X is zeroed, virtualizing write", self._pos//self.block, block*self.block)
                        continue
                    else:
                        # allocates a new grain at end before writing
                        self.stream.seek(0, 2)
                        block = self.stream.tell()//512
                        self.bat[self._pos//self.block] = block
                        if DEBUG&16: log("allocating new Grain #%d @0x%X", self._pos//self.block, block*512)
                        self.stream.seek(self.block-1, 1)
                        self.stream.write(b'\x00')
            self.stream.seek(block*512+offset)
            if DEBUG&16: log("%s: writing grain #%d, offset 0x%X (0x%X), buffer[0x%X:0x%X]", self.name, self._pos//self.block, offset, self._pos, i, i+put)
            self.stream.write(s[i:i+put])
            i+=put
            self._pos+=put



class Image(object):
    "Handles a VMDK disk image made by extents"
    def __init__ (self, name, mode='rb'):
        atexit.register(self.close)
        self.closed = False
        self._pos = 0 # offset in virtual stream
        self.size = 0 # size of virtual stream
        self._file = open(name, 'r+', newline='\n')
        self.Parent = None
        if self._file.read(4) == 'KDMV':
            raise BaseException('Embedded disk descriptor files are not supported!')
        else: # assumes a descriptor file
            if DEBUG: log("opening as DDF...")
            self._file.seek(0)
            ddf=parse_ddf(self._file)
            if ddf['CID']==0 or not ddf['extents']:
                raise BaseException('"%s" is not a VMDK DiskDescriptor nor a monolithic file!'%name)
            self.ddf = ddf
            if ddf['parentCID'] != 0xFFFFFFFF: # has parent
                if DEBUG: log("has parentCID=%x", ddf['parentCID'])
                if not os.path.exists(ddf['parentFileNameHint']):
                    raise BaseException('"%s": could not find parent disk image "%s"!'%(name,ddf['parentFileNameHint']))
                self.Parent = Image(ddf['parentFileNameHint'])
                if DEBUG: log("opened parent with CID=%x", self.Parent.ddf['CID'])
                if self.Parent.ddf['CID'] != ddf['parentCID']:
                    raise BaseException('"%s" is not a valid parent for this disk image, CIDs do not match!'%ddf['parentFileNameHint'])
            ext_id = 0
            for ext in ddf['extents']:
                ext['stream'] = Extent(os.path.join(os.path.dirname(name), ext['name']), mode)
                self.size += ext['size']
                if self.Parent:
                    ext['stream'].Parent = self.Parent.ddf['extents'][ext_id]['stream']
                ext_id += 1
        self.name = name
        self.mode = mode

    def type(self): return 'VMDK'
    
    def cache_flush(self):
        self.flush()

    def flush(self):
        for ext in self.ddf['extents']:
            ext['stream'].flush()

    def close(self):
        if self.closed: return
        changed=0
        for ext in self.ddf['extents']:
            e = ext['stream']
            e.close()
            # If an extent was written to, we must update the CID
            if e.stream.mode != "rb":
                for ext in self.ddf['extents']:
                    if ext['stream'].tstamp != os.stat(e.name).st_mtime:
                        changed=1
        if changed and e.stream.mode != "rb" and not self.closed:
            if DEBUG: log("%s_%x: timestamp changed, updating Image's CID", self.name, self.__hash__())
            i = self.ddf['raw'].index('CID=')
            s = self.ddf['raw']
            s = s.replace(s[i:i+12], 'CID=%x'%random.randint(1, 0xfffffffd))
            self._file = open(self._file.name, 'w', newline='\n')
            self._file.write(s)
        self._file.close()
        self.closed = True

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

    # To read from parent an extent has to know its parent
    def read(self, size=-1):
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()
        while size:
            extent = None
            # Finds the Extent containing current offset
            for extent in self.ddf['extents']:
                if self._pos <= extent['end']: break
            f = extent['stream']
            # Seeks the starting position in such Extent
            f.seek(self._pos-extent['start'])
            # Reads the full quantity or up to extent's end
            subbuf = f.read(size)
            size -= len(subbuf)
            self._pos += len(subbuf)
            buf += subbuf
            if DEBUG&16: log("%s: read %d bytes from 0x%X (-0x%X)", self.name, len(subbuf), self._pos, extent['start'])
        return buf

    def write(self, s):
        size = len(s)
        if not size: return
        i=0
        while size:
            extent = None
            # Finds the Extent containing current offset
            for extent in self.ddf['extents']:
                if self._pos <= extent['end']: break
            f = extent['stream']
            # Seeks the starting position in such Extent
            f.seek(self._pos-extent['start'])
            # Writes the full quantity or up to extent's end
            put = min(size, extent['end']+1-f._pos)
            f.write(s[i:i+put])
            size-=put
            i+=put
            self._pos+=put



def _mk_common(name, size, block):
    "Makes an empty Extent as part of a VMDK disk image"
    x = calc_ext_meta_size(size, block)
    h = Header()
    h.dwMagicNumber=0x564D444B
    h.dwVersion = 1
    h.dwFlags = 3
    h.u64Capacity=size//512
    h.u64GrainSize = block//512
    h.dwGTEsPerGT = 0x200
    h.u64RGDOffset = 1
    h.u64GDOffset = h.u64RGDOffset+x[1]+x[2]
    h.u64OverHead = (x[3]*block)//512
    h.cSingleEndLineChar = 0xA
    h.cNonEndLineChar = 0x20
    h.cDoubleEndLineChar1 = 0xD
    h.cDoubleEndLineChar2 = 0xA

    f = myfile(name, 'wb')
    s = h.pack()
    f.write(s)
    # Makes a consecutive array of DWORDs for redundant GD
    L = [struct.pack('<I', x) for x in range(h.u64RGDOffset+x[1], h.u64RGDOffset+x[1]+x[2], 4)]
    run = bytearray().join(L)
    assert f.tell() == h.u64RGDOffset*512
    f.write(run) # writes RGD
    f.write(bytearray(x[2]*512)) #writes blank RGTs
    L = [struct.pack('<I', x) for x in range(h.u64GDOffset+x[1], h.u64GDOffset+x[1]+x[2], 4)]
    run = bytearray().join(L)
    f.seek(h.u64GDOffset*512)
    assert f.tell() == h.u64GDOffset*512
    f.write(run) # writes GD
    f.write(bytearray(x[2]*512)) #writes blank GTs
    f.write(bytearray(x[3]*block-f.tell())) # pads to a full Grain
    f.close()



def mk_dynamic(name, size, block=(64<<10), overwrite='no', sector=512):
    "Creates an empty dynamic VMDK"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VMDK image!")
    if block < (4<<10) or math.log(block,2)%2!=0:
        raise BaseException("Grain size must be a power of 2 and at least 4K!")
    
    basename = name.lower().replace('.vmdk','')
    extent_size = calc_ext_meta_size(size)[0]
    
    s='''# Disk DescriptorFile\nversion=1\nencoding="windows-1252"\nCID=fffffffe\nparentCID=ffffffff\ncreateType="twoGbMaxExtentSparse"\n\n# Extent description\n'''
    i=1; cb=size
    while cb:
        seg=min(cb, extent_size)
        ename = basename+'-s%03d.vmdk'%i
        s+='RW %s SPARSE "%s"\n' % (seg//512, ename)
        _mk_common(ename, seg, block)
        cb-=seg
        i+=1
        
    s+='''\n# The Disk Data Base\n#DDB\n\nddb.geometry.cylinders = "%d"\nddb.geometry.heads = "255"\nddb.geometry.sectors = "63"\n'''
    s = s%(size//(63*255*512))
    f = myfile(name, "wb")
    f.write(s.encode()); f.close()
       
    if DEBUG&16: log("making new Dynamic VMDK '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)



def mk_diff(name, base, overwrite='no', sector=512):
    "Creates an empty differencing VMDK"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VMDK image!")
    ima=Image(base)

    if DEBUG&16: log("making new Differencing VMDK '%s' of %.02f MiB", name, float(ima.size//(1<<20)))

    mk_dynamic(name, ima.size, ima.ddf['extents'][0]['stream'].block, overwrite)

    # updates parentCID and adds parentFileNameHint
    # TODO: we should merely *update* parent's DDF
    s = open(name).read()
    s = s.replace('parentCID=ffffffff', 'parentCID=%08x\nparentFileNameHint="%s"'%(ima.ddf['CID'], base))
    open(name,'w').write(s)



if __name__ == '__main__':
    import os
    mk_dynamic('test.vmdk', 4<<30, overwrite='yes')
    vd = Image('test.vmdk')
    vd.close()
    os.remove('test.vmdk')
    os.remove('test-s001.vmdk')
