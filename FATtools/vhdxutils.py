# -*- coding: cp1252 -*-
"Utilities to handle VHDX disk images"

""" VHDX IMAGE FILE FORMAT V2

VHDX images can be Dynamic (growable), Fixed (static) or Differencing disks.

The typical layout is made of 4 system regions followed by data blocks:
- 1 MB Header region with 5 64KB structures (File identifier, 2 Headers, 2
copies of Region table)
- 1 MB (min, default) Log Region
- 1 MB (fixed) Metadata region
- variable Blocks Allocation Table (BAT) region (64-bit payload and bitmap
  indexes)
- payload (and, for differencing disks, bitmap) blocks

A Dynamic VHDX initially contains only the 4 system regions and a zeroed BAT.
Disk is virtually subdivided into blocks of equal size (from 1 to 256 MiB, 32
by default) with a corresponding BAT entry with the MB address where the
block resides or a special status code if the block is virtual.
Blocks are allocated on write and put at image's end, so they appear in
arbitrary order.

A Fixed VHDX is like a Dynamic one, except all BAT entries are set with valid
offsets and PAYLOAD_BLOCK_FULLY_PRESENT state at creation time.

A Differencing VHDX is a dynamic image containing only new or modified sectors
of a parent VHDX image (any type). The BAT reveals if blocks are fully or
partly present in the child image and, in the latter case, the bitmap must be
checked to determine sectors belonging to the child. Block size is typically
smaller in child images (2MB instead of 32MB).

The Log records in a circular buffer modified metadata sectors before they are
committed to disk and must be replayed on image opening if sLogGuid is not
zero in the active header (this occurs in case of a system failure event).
This enforces writes in BAT and Metadata regions (not the Headers),
plus Bitmap blocks.

Metadata region records small amounts of system (like sectors and disk sizes,
image parameters, parent's path) or user-defined data. Each record is denoted
by a GUID and stored in an indexed table between offsets 64KB and 1MB.
The File Parameters metadata shows if an image is Fixed (LeaveBlockAllocated
bit set) or Differencing (HasParent bit set). Moreover, a differencing image
has a Parent Locator metadata carrying parent's GUID and path.

BAT contains 64-bit entries for payload and bitmap blocks (a bitmap block is
always 1 MB or 2^23 logical sectors). A bitmap block entry always follows
represented payload block entries. An entry contains a 3-bit block status flag
plus a 44-bit block absolute offset in MB.

A Dynamic image has only zeroed BAT bitmap entries and bitmap blocks are never
allocated whilst a Differencing one can have PAYLOAD_BLOCK_PARTIALLY_PRESENT
and, thus, corresponding bitmap blocks showing sectors that reside in child image.

Payload and bitmap blocks can be placed in any order inside the image.
Sectors may be 512 or 4096 bytes.

TODO:
- Log (Metadata and BAT regions, bitmap sectors)"""

import io, struct, uuid, zlib, ctypes, time, os, math
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools.crc32c import crc_update
from FATtools.vhdxlog import LogStream
from FATtools.utils import myfile

import FATtools.utils as utils
from FATtools.debug import log

#~ import logging
#~ logging.basicConfig(level=logging.DEBUG, filename='vhdxutils.log', filemode='w')

def mk_crc(s):
    "Returns the CRC-32C for bytes 's'"
    crc = crc_update(0xffffffff, s, len(s)) ^ 0xffffffff
    return struct.pack('<I', crc)

def global_crc(self):
    "Pluggable helper class member to get CRC from various VHDX structures"
    crc = self._buf[4:8]
    self._buf[4:8] = b'\0\0\0\0'
    c_crc = mk_crc(self._buf)
    self._buf[4:8] = crc
    return c_crc

def writea(f, s, n):
    "Writes a buffer 's' to a stream 'f' aligned at 'n' bytes"
    f.write(s)
    l = len(s)
    if l < n:
        f.write((n-l) * b'\x00')

def get_bat_facts(disk_size, block_size, logical_sector_size, is_dynamic=1):
    "Calculates and returns BAT size in bytes, its entries and chunk ratio"
    # Payload blocks a fixed 1MB bitmap block can represent
    chunk_ratio = ((1<<23)*logical_sector_size)//block_size
    tot_data_blocks = math.ceil(disk_size/block_size)
    tot_bitmap_blocks = math.ceil(tot_data_blocks/chunk_ratio)
    # In a Dynamic disk, the last BAT entry locates the last payload block
    tot_bat_entries_dy = tot_data_blocks + math.floor((tot_data_blocks-1)/chunk_ratio)
    # In a Differencing one, the last BAT entry locates the last bitmap block
    # (a bitmap entry follows the 'chunk_ratio' payload entries it represents)
    tot_bat_entries_di = tot_bitmap_blocks*(chunk_ratio+1)
    if is_dynamic:
        entries = tot_bat_entries_dy
    else:
        entries = tot_bat_entries_di
    bat_size_mb = ((entries*8) + ((1<<20)-1)) // (1<<20)
    bat_size_mb = bat_size_mb * (1<<20)
    return (bat_size_mb, entries, chunk_ratio)


# Actually, Metadata and BAT
RegionGUIDs = (uuid.UUID('8b7ca206-4790-4b9a-b8fe-575f050f886e'), uuid.UUID('2dc27766-f623-4200-9d64-115e9bfd4a08'))

#
# Known Metadata parsers
#
def file_param_parser(cl, s):
    v = struct.unpack('<I', s[:4])[0] # DWORD 1
    cl.block_size = v
    if v < (1<<20) or v > 256<<20 or not math.log(v,2).is_integer():
        raise BaseException('Invalid block size %d for VHDX image!'%v)
    v = struct.unpack('<I', s[4:])[0] # DWORD 2
    cl.file_params = v # bit 1: LeaveBlockAllocated  bit 2: HasParent

def vdisk_size_parser(cl, s):
    v = struct.unpack('<Q', s)[0]
    cl.disk_size = v

def ls_size_parser(cl, s):
    v = struct.unpack('<I', s)[0]
    if v not in (512, 4096):
        raise BaseException('VHDX Logical sector size not 512 nor 4096 bytes!')
    cl.logical_sector_size = v

def ps_size_parser(cl, s):
    v = struct.unpack('<I', s)[0]
    if v not in (512, 4096):
        raise BaseException('VHDX Physical sector size not 512 nor 4096 bytes!')
    cl.physical_sector_size = v

def vdisk_id_parser(cl, s):
    v = uuid.UUID(bytes_le=bytes(s))
    cl.disk_GUID = v

def parent_locator(cl, s):
    p = ParentLocator(s)
    p.parse()
    cl.ParentLocator = p


MetadataGUIDs = {
uuid.UUID('caa16737-fa36-4d43-b3b6-33f0aa44e76b'): ('File Parameters', file_param_parser),
uuid.UUID('2fa54224-cd1b-4876-b211-5dbed83bf4b8'): ('Virtual Disk Size', vdisk_size_parser), 
uuid.UUID('8141bf1d-a96f-4709-ba47-f233a8faab5f'): ('Logical Sector Size', ls_size_parser), 
uuid.UUID('cda348c7-445d-4471-9cc9-e9885251c556'): ('Physical Sector Size', ps_size_parser), 
uuid.UUID('beca12ab-b2e6-4523-93ef-c309e000c746'): ('Virtual Disk Id', vdisk_id_parser), 
uuid.UUID('a8d35f2d-b30b-454d-abf7-d3d84834ab0c'): ('Parent Locator', parent_locator), 
}



class FileTypeIdentifier(object):
    "File Type Identifier"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '8s'), # vhdxfile
    0x08: ('sCreator', '512s'), # creator app (UTF-16), optional
    } # Size = 0x10000 (aligned 64K)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(65536)
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
        return utils.class2str(self, "VHDX File Type Identifier @%X\n" % self._pos)
    
    def isvalid(self):
        if self.sSignature == b'vhdxfile':
            return 1
        return 0


class VHDXHeader(object):
    "VHDX Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # head
    0x04: ('dwChecksum', '<I'), # CRC-32C, zeroed this field
    0x08: ('u64SequenceNumber', '<Q'), # the greatest signals the current (=in use) header
    # NOTE: after mounting disk and initing a GPT part, Windows 10 does NOT change such number!
    0x10: ('sFileWriteGuid', '16s'), # GUID changed the first time the file is written after open
    0x20: ('sDataWriteGuid', '16s'), # GUID changed the first time user-visible data are modified after open (make a link with child images)
    0x30: ('sLogGuid', '16s'), # GUID of current valid entries in the log stream, or zero if no log to replay
    0x40: ('wLogVersion', '<H'), # actually zero
    0x42: ('wVersion', '<H'), # 1 = VHDX version format 2
    0x44: ('dwLogLength', '<I'), # Log length (1 MB multiple)
    0x48: ('u64LogOffset', '<Q'), # absolute Log offset (1 MB multiple)
    } # Size = 0x1000 (4096 byte), 64K aligned

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(4096)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
    
    __getattr__ = utils.common_getattr

    crc = global_crc
    
    def pack(self):
        "Updates internal buffer"
        self.dwChecksum = 0
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self._buf[4:8] = mk_crc(self._buf) # updates checksum
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "VHDX Header @%X\n" % self._pos)

    def isvalid(self):
        if self.sSignature != b'head':
            return 0
        if self.dwChecksum != struct.unpack("<I", self.crc())[0]:
            if DEBUG&16: log("VHDX Header checksum 0x%X stored != 0x%X calculated", self.dwChecksum, struct.unpack("<I", self.crc())[0])
            return 0
        return 1


class RegionTableHeader(object):
    "Region Table Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # regi
    0x04: ('dwChecksum', '<I'), # CRC-32C over the full 64K region, zeroed this field
    0x08: ('dwEntryCount', '<I'), # valid entries to follow (<2048)
    0x0C: ('dwReserved', '<I'), # zero
    } # Size = 0x10 (16 bytes) [0x10000 (65536 bytes) the full table]

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(65536)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.entries = []
        self.metadata_offset = 0
        self.BAT_offset = 0
    
    __getattr__ = utils.common_getattr

    crc = global_crc

    def parse(self):
        "Parses entries in the 64K table"
        if not self.stream: return
        f = self.stream; p = f.tell()
        f.seek(self._pos+16) # seek RT entries array
        for i in range(self.dwEntryCount):
            pos=f.tell()
            rte = RegionTableEntry(f.read(32), offset=pos)
            if rte.dwRequired and uuid.UUID(bytes_le=rte.sGuid) not in RegionGUIDs:
                raise BaseException("VHDX has an unknown required region with GUID=%s"%uuid.UUID(bytes_le=rte.sGuid))
            self.entries += [rte]
            if rte.sGuid == RegionGUIDs[0].bytes_le:
                self.metadata_offset = rte.u64FileOffset
                self.metadata_length = rte.dwLength
            elif rte.sGuid == RegionGUIDs[1].bytes_le:
                self.BAT_offset = rte.u64FileOffset
                self.BAT_length = rte.dwLength
        if not self.metadata_offset or not self.BAT_offset:
            raise BaseException("VHDX image corrupted, no Metadata or BAT region found!")
        f.seek(p)

    def pack(self):
        "Updates internal buffer"
        self.dwChecksum = 0
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self._buf[4:8] = mk_crc(self._buf) # updates checksum
        # Pack self.entries if any!!!
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "VHDX Region Table Header @%X\n" % self._pos)

    def isvalid(self):
        if self.sSignature != b'regi' or self.dwEntryCount > 2047:
            return 0
        if self.dwChecksum != struct.unpack("<I", self.crc())[0]:
            if DEBUG&16: log("VHDX Region Table Header checksum 0x%X stored != 0x%X calculated", self.dwChecksum, struct.unpack("<I", self.crc())[0])
            return 0
        return 1


class RegionTableEntry(object):
    "Region Table Entry"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sGuid', '16s'), # BAT 2DC27766-F623-4200-9D64-115E9BFD4A08;
                            # Metadata 8B7CA206-4790-4B9A-B8FE-575F050F886E
    0x10: ('u64FileOffset', '<Q'), # offset of the region in the VHDX
    0x18: ('dwLength', '<I'), # region length
    0x1C: ('dwRequired', '<I'), # 1 if this region must be recognized to load the VHDX
    } # Size = 0x20 (32 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(32)
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
        return utils.class2str(self, "VHDX Region Table Entry @%X\n" % self._pos)


class MetadataTableHeader(object):
    "Metadata Table Header"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '8s'), # metadata
    0x08: ('wReserved', '<H'), # zero
    0x0A: ('wEntryCount', '<H'), # valid entries to follow (<2048)
    0x0C: ('sReserved2', '20s'), # zero
    } # Size = 0x20 (32 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(32)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.entries = []

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def parse(self):
        "Parses entries in the 64K table"
        if not self.stream: return
        f = self.stream; p = f.tell()
        f.seek(self._pos+32) # seek entries array
        for i in range(self.wEntryCount):
            pos=f.tell()
            me = MetadataEntry(f.read(32), offset=pos)
            self.entries += [me]
            uid = uuid.UUID(bytes_le=me.sItemId)
            if uid in MetadataGUIDs:
                MetadataGUIDs[uid][1](self, self.parse_raw(me)) # calls appropriate parser
            else:
                if DEBUG&16: log("note: unknown metadata entry %s", uid)
        f.seek(p)

    def parse_raw(self, entry):
        "Parses a Metadata Entry returning the raw contents"
        f = self.stream; p = f.tell()
        f.seek(self._pos+entry.dwOffset) # seeks metadata offset inside region
        s = f.read(entry.dwLength)
        f.seek(p)
        return s

    def __str__ (self):
        return utils.class2str(self, "VHDX Metadata Table Header @%X\n" % self._pos)

    def isvalid(self):
        if self.sSignature != b'metadata' or self.wEntryCount > 2047:
            return 0
        return 1


class MetadataEntry(object):
    "Metadata Entry"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sItemId', '16s'), # metadata GUID
    0x10: ('dwOffset', '<I'), # offset (>=64K) inside the region, or zero
    0x14: ('dwLength', '<I'), # length (must reside in region's 1MB boundary), or zero
    0x18: ('dwFlags', '<I'), # bit 1=IsUser, 2=IsVirtualDisk, 3=IsRequired
    } # Size = 0x20 (32 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(32)
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
        return utils.class2str(self, "VHDX Metadata Entry @%X\n" % self._pos)

    def isvalid(self):
        # CHECK UUID & IsRequired here?
        if not self.dwOffset and not self.dwLength: return 1
        if self.dwOffset < 0x10000 or self.dwOffset+self.dwLength > 0x100000:
            return 0
        return 1


class ParentLocator(object):
    "Parent Locator"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sLocatorType', '16s'), # {B04AEFB7-D19E-4A81-B789-25B8E9445913}
    0x10: ('wReserved', '<H'), # zero
    0x12: ('wKeyValueCount', '<H'), # number of key-value pairs
    } # Size = 0x14 (20 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(20)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        self.entries = {}

    __getattr__ = utils.common_getattr

    def __str__ (self):
        return utils.class2str(self, "Parent Locator @%X\n" % self._pos)

    # PLEASE NOTE: KeyLength and ValueLength are 2 bytes long
    # not 4 like stated in MS-VHDX v20180912
    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        buf = bytearray(12*len(self.entries))
        i = 0
        # Converts entries in a key-value buffer
        for k, v in self.entries.items():
            ke = k.encode('utf-16le')
            ve = v.encode('utf-16le')
            ko = 20 + len(buf)
            buf[i:i+4] = struct.pack('<I', ko) # key offset is at buffer's end
            buf[i+4:i+8] = struct.pack('<I', ko+len(ke)) # value offset is next to key
            buf[i+8:i+10] = struct.pack('<H', len(ke))
            buf[i+10:i+12] = struct.pack('<H', len(ve))
            i += 12
            buf += ke + ve
        self._buf += buf
        return self._buf

    def parse(self):
        "Parses Locator entries in a dictionary"
        for j in range(self.wKeyValueCount):
            i = 20 + j*12 # each entry is 12 bytes
            ko = struct.unpack('<I', self._buf[i:i+4])[0] # offsets relative to Locator start
            vo = struct.unpack('<I', self._buf[i+4:i+8])[0]
            kl = struct.unpack('<H', self._buf[i+8:i+10])[0]
            vl = struct.unpack('<H', self._buf[i+10:i+12])[0]
            k = self._buf[ko:ko+kl].decode('utf-16le') # strings are UTF-16 (LE) encoded
            v = self._buf[vo:vo+vl].decode('utf-16le')
            self.entries[k] = v


class BAT(object):
    "Implements the Block Address Table as indexable object"
    def __init__ (self, stream, offset, blocks, block_size):
        self.stream = stream
        self.size = blocks # total blocks in the data area
        self.bsize = block_size # block size
        self.offset = offset # relative BAT offset
        self.decoded = {} # {block index: block effective offset}
        self.isvalid = 1 # self test result
        # Windows 10 does NOT seem to check the BAT on mounting!
        #~ self._isvalid() # performs self test

    def __str__ (self):
        return "VHDX BAT table of %d blocks starting @%Xh\n" % (self.size, self.offset)

    def __getitem__ (self, index):
        "Retrieves the value stored in a given block index"
        if index < 0:
            index += self.size
        if DEBUG&16: log("%s: requested to read BAT[0x%X]", self.stream.name, index)
        if not (0 <= index <= self.size-1):
            raise BaseException("Attempt to read a #%d block past disk end"%index)
        slot = self.decoded.get(index)
        if slot: return slot
        pos = self.offset + index*8
        opos = self.stream.tell()
        self.stream.seek(pos)
        slot = struct.unpack("<Q", self.stream.read(8))[0]
        self.decoded[index] = slot
        if DEBUG&16: log("%s: got BAT[0x%X]=0x%X @0x%X", self.stream.name, index, slot, pos)
        self.stream.seek(opos) # rewinds
        return slot

    def __setitem__ (self, index, value):
        "Sets the value stored in a given block index"
        if index < 0:
            index += self.size
        self.decoded[index] = value
        dsp = index*8
        pos = self.offset+dsp
        if DEBUG&16: log("%s: set BAT[0x%X]=0x%X @0x%X", self.stream.name, index, value, pos)
        opos = self.stream.tell()
        self.stream.seek(pos)
        value = struct.pack("<Q", value)
        self.stream.write(value)
        self.stream.seek(opos) # rewinds
        
    def _isvalid(self, selftest=1):
        "Checks BAT for invalid entries setting .isvalid member"
        self.stream.seek(0, 2)
        ssize = self.stream.tell() # container actual size
        unallocated = 0
        seen = []
        for i in range(self.size):
            a = self[i]
            if a == 0:
                unallocated+=1
                continue
            blk_s = a & 0xFFFFF # block status (3 of 20 bits)
            blk_ea = (a>>20)<<20 # effective 1MB offset (44 bits)
            if blk_s != 0 and blk_ea == 0:
                self.isvalid = -1 # status w/ zero address
                if DEBUG&16: log("%s: BAT[%d] has status %d without a valid address", self, i, blk_s)
                if selftest: break
                print("ERROR: BAT[%d] has status %d without a valid address" %(i, blk_s))
            if blk_ea in seen:
                self.isvalid = -2 # duplicated block address
                if DEBUG&16: log("%s: BAT[%d] offset (block 0x%08X) was seen more than once", self, i, blk_ea)
                if selftest: break
                print("ERROR: BAT[%d] offset (block 0x%08X) was seen more than once" %(i, blk_ea))
            if blk_ea > ssize:
                if DEBUG&16: log("%s: BAT[%d] offset (block 0x%08X) exceeds allocated file size", self, i, blk_ea)
                self.isvalid = -3 # block address beyond file's end detected
                if selftest: break
                print("ERROR: BAT[%d] offset (block 0x%08X) exceeds allocated file size" %(i, blk_ea))
            if blk_ea % (1<<20):
                self.isvalid = -4 # unaligned block address
                if DEBUG&16: log("%s: BAT[%d] has unaligned block address 0x%08X", self, i, blk_ea)
                if selftest: break
                print("%s: BAT[%d] has unaligned block address 0x%08X"%(i, blk_ea))
            if blk_ea < (4<<20):
                self.isvalid = -5 # block address below 4MB
                if DEBUG&16: log("%s: BAT[%d] has invalid block address 0x%08X", self, i, blk_ea)
                if selftest: break
                print("%s: BAT[%d] has ibvalid block address 0x%08X"%(i, blk_ea))
            seen += [blk_ea]


class BlockBitmap(object):
    "Handles a VHDX block bitmap (always 1 MB or 2^23 sectors)"
    def __init__ (self, stream, i):
        self.stream = stream
        self.stream.seek(i)
        self.bmp = self.stream.read(1<<20)
        self.i = i
        self.modified = 0
        
        if DEBUG&16: log("Inited Bitmap block @0x%08X", i)
    
    def flush(self):
        if not self.modified: return
        self.stream.seek(self.i)
        self.stream.write(self.bmp)
        self.modified = 0

    def isfull(self):
        "Checks if all sectors are in use"
        return self.bmp == bytes((1<<20)*'\xFF')

    def isset(self, sector):
        "Tests if the bit corresponding to a given sector is set"        
        # CAVE! VHD has inverted endianness (BE) in respect of VHDX (LE)
        return (self.bmp[sector//8] & (1 << (sector%8))) != 0
    
    def set(self, sector, length=1, clear=False):
        "Sets or clears a bit or bits run"
        pos = sector//8
        rem = sector%8
        # Avoids unnecessary single settings
        if not clear and length==1 and (self.bmp[pos] & (1 << (rem))): return
        self.modified = 1
        if DEBUG&16: log("set(%Xh,%d%s) start @0x%X:%d", sector, length, ('',' (clear)')[clear!=False], pos, rem)
        if rem:
            B = self.bmp[pos]
            if DEBUG&16: log("got byte {0:08b}".format(B))
            todo = min(8-rem, length)
            if clear:
                B &= ~(((0xFF>>(8-todo))&0xFF) << rem)
            else:
                B |= (((0xFF>>(8-todo))&0xFF) << rem)
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
                B &= ~((0xFF>>(8-rem))&0xFF)
            else:
                B |= ((0xFF>>(8-rem))&0xFF)
            self.bmp[pos] = B
            if DEBUG&16: log("set B={0:08b}".format(B))


class Image(object):
    def __init__ (self, name, mode='rb', _fparams=0):
        # Flags for GUID updates at first write operation
        self.updated_file_guid = 0
        self.updated_data_guid = 0
        self.updated_log_guid = 0
        self._pos = 0 # offset in virtual stream
        self.size = 0 # size of virtual stream
        self.name = name
        self.stream = myfile(name, mode)
        self.bmp = None # bitmap chunk loaded, if any
        self._file = self.stream
        self.Parent = None # Parent image, if any
        f = self.stream
        self.mode = mode
        f.seek(0, 2)
        size = f.tell()
        # Check minimum image size
        if size < 4<<20:
            raise BaseException("VHDX Image size is less than minimum!")
        self.stream.seek(0)
        # Check vhdxfile signature
        fti = FileTypeIdentifier(f.read(65536), 0)
        if not fti.isvalid():
            raise BaseException("VHDX Image signature not found!")
        # Check headers
        h1 = VHDXHeader(f.read(4096), 64<<10)
        f.seek(128<<10)
        h2 = VHDXHeader(f.read(4096), 128<<10)
        if not h1.isvalid() and not h2.isvalid():
            raise BaseException("VHDX headers are both invalid!")
        # Selects active header (might be equal)
        hi = max(h1.u64SequenceNumber, h2.u64SequenceNumber)
        if hi == h1.u64SequenceNumber:
            self.header = h1
        else:
            self.header = h2
        # Initializes Log and replays it if valid GUID found
        self.Log = LogStream(self)
        if self.header.sLogGuid != bytes(16):
            if '+' not in self.mode:
                raise BaseException("Can't replay the Log in a read-only VHDX Image!")
            self.Log.replay_log() # Success or fatal exception
            # Set NULL Log GUID
            self._update_headers(9)
        # Parses Region Table
        f.seek(192<<10)
        r = RegionTableHeader(f.read(65536), 192<<10, stream=f)
        if not r.isvalid():
            r = RegionTableHeader(f.read(65536), 256<<10)
            if not r.isvalid():
                raise BaseException("VHDX Region Table headers are both invalid!")
        r.parse()
        # Parses known Metadata
        f.seek(r.metadata_offset) # seek Metadata
        mh = MetadataTableHeader(f.read(32), r.metadata_offset, stream=f)
        if not mh.isvalid():
            raise BaseException("VHDX Metadata Table not valid!")
        mh.parse()
        self.metadata = mh
        self.block = mh.block_size
        self.size = mh.disk_size
        self.zero = bytearray(self.block)
        self.chunk_ratio = ((1<<23)*mh.logical_sector_size)//self.block
        # Initializes the BAT
        self.bat = BAT(self.stream, r.BAT_offset, r.BAT_length//8, self.block)
        if self.bat.isvalid < 0:
            error = {-1: "invalid block address", -2: "duplicated block address", -3: "block past end",
            -4: "misaligned block", -5: "block below 4MB"}
            raise BaseException("VHDX Image is not valid: %s", error[self.bat.isvalid])
        if mh.file_params == 2 and _fparams != 2: # HasParent == Is Differencing, not mk_diff call
            base = ''
            for ptype in ('relative_path', 'volume_path', 'absolute_win32_path'):
                s = mh.ParentLocator.entries[ptype]
                if os.path.exists(s):
                    base = s
                    break
                s = os.path.basename(s)
                if os.path.exists(s):
                    base = s
                    break
            if not base:
                raise BaseException("Could not locate parent VHDX Image!")
            ima = Image(base)
            # If GUIDs do not match, perhaps the parent was modified after linkage
            if '{%s}' % uuid.UUID(bytes_le=ima.header.sDataWriteGuid) != mh.ParentLocator.entries['parent_linkage']:
                raise BaseException("%s (Parent) Data Write GUID does not match!"%base)
            self.Parent = ima
            if DEBUG&16: log("Opened Parent VHDX %s", ima.name)
        self.seek(0)

    def type(self): return 'VHDX'
    
    def _update_headers(self, op):
        "Updates and writes back the VHDX headers once if write/log operations occurred"
        h = self.header
        h.u64SequenceNumber += 1
        h.u64SequenceNumber &= 0xFFFFFFFFFFFFFFFF
        if op & 1:
            h.sFileWriteGuid = uuid.uuid4().bytes_le
            self.updated_file_guid = 1
        if op & 2:
            h.sDataWriteGuid = uuid.uuid4().bytes_le
            self.updated_data_guid = 1
        if op & 4:
            h.sLogGuid = uuid.uuid4().bytes_le
            self.updated_log_guid = 1
        if op & 8:
            h.sLogGuid = bytes(16)
            self.updated_log_guid = 1
        f = self.stream
        f.seek(64<<10); f.write(h.pack())
        h.u64SequenceNumber += 1
        h.u64SequenceNumber &= 0xFFFFFFFFFFFFFFFF
        f.seek(128<<10); f.write(h.pack())

    def _blk_alloc(self, blk_s, bitmap=0):
        "Allocates a new block (payload or bitmap) and sets the BAT. Returns the block address."
        self.stream.seek(0, 2)
        blk_ea = self.stream.tell() # offset in MB
        blk_i = self._pos//self.block
        if bitmap:
            sz = 1<<20
            bat_i = ((blk_i+self.chunk_ratio)//self.chunk_ratio) * self.chunk_ratio +blk_i//self.chunk_ratio # associated Bitmap chunk index
        else:
            sz = self.block
            bat_i = blk_i + blk_i//self.chunk_ratio # effective BAT index
        self.bat[bat_i] = blk_ea | blk_s
        if DEBUG&16: log("%s: allocating new %s block @0x08%X (EA=0x%08X, status=%d)", self.name, ('payload','bitmap')[bitmap], self._pos, blk_ea, blk_s)
        self.stream.seek(sz-1, 1)
        self.stream.write(b'\x00')
        return blk_ea

    def has_block(self, offset):
        """Checks if a given virtual offset belongs to a payload block allocated
        in any parent of a VHDX chain (NOT to call in last child!)"""
        blk_i = offset//self.block # Absolute index
        bat_i = blk_i + blk_i//self.chunk_ratio # BAT index
        blk_s = self.bat[bat_i] & 0xFFFFF
        if blk_s != 0:
            return True
        if self.Parent:
            return self.Parent.has_block(offset)
        return False
        
    def _offset_info(self, offset, what=0):
        "Returns various informations about a virtual stream address with respect to some VHDX structures"
        blk_i = offset//self.block # block index in virtual stream (size may change from Parent to Child Differencing VHDX)
        bat_i = blk_i + blk_i//self.chunk_ratio # BAT index
        bat_e = self.bat[bat_i] # BAT entry
        blk_s = bat_e & 0xFFFFF # status of payload block (3 of 20 bits)
        blk_ea = (bat_e>>20)<<20 # effective 1MB offset (44 bits)
        if blk_ea % (1<<20):
            raise BaseException("Invalid block offset %0x08X, MUST be multiple of 1MB!" % blk_ea)
        blk_o = offset%self.block # relative offset position in the payload block
        
        if what == 0: # Basic informations
            return blk_ea, blk_o, blk_s
        
        # BAT entry for bitmap block associated with chunk containing the current payload
        # There's a Bitmap entry after chunk_ratio payload entries
        CR = self.chunk_ratio
        bmp_i = ((blk_i+CR)//CR) * CR + blk_i//CR
        bmp_e = self.bat[bmp_i]
        bmp_s = bmp_e & 0xFFFFF
        bmp_ea = (bmp_e >> 20) << 20
        if bmp_ea % (1<<20):
            raise BaseException("Invalid Bitmap block offset %0x08X, MUST be multiple of 1MB!" % bmp_ea)

        sec_i = blk_o // self.metadata.logical_sector_size # sector index in block (sector size may be 512 or 4096)
        sec_bi = (offset//self.metadata.logical_sector_size) % (1<<23) # sector index in Bitmap chunk (a 1MB chunks represents 2^23 sectors)
        
        return bmp_ea, bmp_s, sec_i, sec_bi

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
        return self._pos

    def tell(self):
        return self._pos
    
    def close(self):
        self.stream.close()

    def read(self, size=-1):
        if size == -1 or self._pos + size > self.size:
            size = self.size - self._pos # reads all
        buf = bytearray()

        while size:
            blk_ea, offset, blk_s = self._offset_info(self._pos)

            # Current Payload block
            leftbytes = self.block-offset # remaining block bytes
            if leftbytes <= size:
                got=leftbytes # max bytes we can read in current block
                size-=leftbytes
            else:
                got=size
                size=0
            if DEBUG&16: log("reading %d bytes from %s @0x%08X (EA=0x%08X, status=%d)", got, self.name, self._pos, blk_ea+offset, blk_s)
            
            if blk_s == 0: # PAYLOAD_BLOCK_NOT_PRESENT
                if not self.Parent:
                    blk_s = 2 # In a Dynamic image, treat as a zeroed block
                else:
                    if DEBUG&16: log("reading all %d bytes from Parent %s", got, self.Parent.name)
                    self.Parent.seek(self._pos)
                    buf += self.Parent.read(got)
            elif blk_s in (1,2,3): # PAYLOAD_BLOCK_UNDEFINED, PAYLOAD_BLOCK_ZERO, PAYLOAD_BLOCK_UNMAPPED
                if DEBUG&16: log("reading %d virtual (zero) bytes from Self %s", got, self.name)
                buf+=bytearray(got)
            elif blk_s == 6: # PAYLOAD_BLOCK_FULLY_PRESENT
                if DEBUG&16: log("reading all %d bytes from Self %s", got, self.name)
                self.stream.seek(blk_ea + offset)
                buf += self.stream.read(got)
            elif blk_s == 7: # PAYLOAD_BLOCK_PARTIALLY_PRESENT
                if not self.Parent:
                    raise BaseException("Can't have a PAYLOAD_BLOCK_PARTIALLY_PRESENT in %s without a Parent VHDX!" % self.name)
                
                bmp_ea, bmp_s, sec_i, sec_bi = self._offset_info(self._pos, 1)
                
                # Acquires Block bitmap once
                if bmp_s == 6: # SB_BLOCK_PRESENT
                    if not bmp_ea:
                        raise BaseException("Can't have a SB_BLOCK_PRESENT Bitmap block in %s without an effective address!"%self.name)
                    if not self.bmp or self.bmp.i != bmp_ea:
                        if DEBUG&16: log("Loading Bitmap chunk @0x%08X for %s", bmp_ea, self.name)
                        self.bmp = BlockBitmap(self.stream, bmp_ea)
                if not self.bmp:
                    raise BaseException("Can't have a PAYLOAD_BLOCK_PARTIALLY_PRESENT in %s without a chunk bitmap!"%self.name)
                
                LSS = self.metadata.logical_sector_size
                # Align streams positions
                self.stream.seek(blk_ea + offset)
                self.Parent.seek(self._pos) # ignore effective block size, if different from child
                while got:
                    cb = LSS # max bytes to read from sector
                    if self._pos % LSS: # if middle 1st sector, align read
                        cb = LSS - self._pos % LSS
                    cb = min(cb, got) # effective bytes to read, no more than got

                    if self.bmp.isset(sec_bi):
                        if DEBUG&16: log("reading %d bytes @0x%08X (Block EA=0x%08X) from Self %s", cb, self._pos, blk_ea, self.name)
                        buf += self.stream.read(cb)
                        self.Parent.seek(cb, 1) # keep Parent stream aligned
                    else:
                        if DEBUG&16: log("reading %d bytes @0x%08X from Parent %s", cb, self._pos, self.Parent.name)
                        buf += self.Parent.read(cb)
                        self.stream.seek(cb, 1) # keep self stream aligned

                    got-=cb # left to read in block
                    sec_bi+=1 # next Bitmap index
                    self._pos += cb
            else:
                raise BaseException("Invalid VHDX payload block status %d in %s" % (blk_s, self.name))
            self._pos += got
        return buf

    def write(self, s):
        size = len(s)
        if not size: return

        if not self.updated_data_guid:
            self._update_headers(3)

        i=0
        
        start_pos = self._pos
        end_pos = self._pos + size

        while size:
            blk_ea, offset, blk_s = self._offset_info(self._pos)
            leftbytes = self.block - offset # bytes to block's end

            if leftbytes <= size:
                put=leftbytes # max bytes to write to fill a block
                size-=leftbytes
            else:
                put=size
                size=0

            if blk_s == 6: # PAYLOAD_BLOCK_FULLY_PRESENT
                if DEBUG&16: log("%s: writing %d bytes @0x%08X (EA=0x%08X, status=%d)", self.name, put, self._pos, blk_ea+offset, blk_s)
                self.stream.seek(blk_ea + offset)
                self.stream.write(s[i:i+put])
            elif blk_s in (1,2,3): # PAYLOAD_BLOCK_UNDEFINED, PAYLOAD_BLOCK_ZERO, PAYLOAD_BLOCK_UNMAPPED
                # we keep a block virtualized until we write zeros
                if s[i:i+put] == self.zero[:put]:
                    if blk_s != 2: # set PAYLOAD_BLOCK_ZERO
                        blk_s = 2
                        blk_i = self._pos//self.block
                        bat_i = blk_i + blk_i//self.chunk_ratio # effective BAT index
                        self.bat[bat_i] = blk_s
                    if DEBUG&16: log("%s: writing %d virtual (zero) bytes", self.name, got)
                else:
                    # allocates a new block at end before writing
                    # PAYLOAD_BLOCK_FULLY_PRESENT since we allocate it for the 1st time
                    blk_s = 6
                    blk_ea = self._blk_alloc(blk_s, bitmap=0)
                    # writes content
                    self.stream.seek(blk_ea + offset)
                    if DEBUG&16: log("%s: writing %d bytes @0x%08X", self.name, blk_ea+offset)
                    self.stream.write(s[i:i+put])
            elif blk_s in (0, 7): # PAYLOAD_BLOCK_NOT_PRESENT, PAYLOAD_BLOCK_PARTLY_PRESENT
                # Check if any parent in the parents chain has current position
                # associated with an allocated payload block
                in_parent = False
                if self.Parent:
                    in_parent = self.Parent.has_block(self._pos)
                bmp_ea, bmp_s, sec_i, sec_bi = self._offset_info(self._pos, 1)

                if not blk_s:
                    # allocates a new block at end before writing
                    if in_parent:
                        blk_s = 7 # PAYLOAD_BLOCK_PARTLY_PRESENT
                    else:
                        blk_s = 6 # PAYLOAD_BLOCK_FULLY_PRESENT, block will be here only
                    # NOTE: status should CHANGE from 7 to 6 if all represented blocks
                    # become fully present! Check at mount time? At Bitmap flush?
                    blk_ea = self._blk_alloc(blk_s)

                # Acquires or creates Block bitmap once, only if block is partly present here
                if blk_s == 7:
                    if bmp_s == 6: # SB_BLOCK_PRESENT
                        if not self.bmp or self.bmp.i != bmp_ea:
                            if self.bmp: # flush bitmap to disk if modified
                                if DEBUG&16: log("%s: flushing Bitmap chunk @0x%08X", self.name, self.bmp.i)
                                self.bmp.flush()
                            if DEBUG&16: log("%s: loading new Bitmap chunk @0x%08X", self.name, bmp_ea)
                            self.bmp = BlockBitmap(self.stream, bmp_ea)
                    else:
                        # allocates a new bitmap block at end before writing
                        bmp_ea = self._blk_alloc(6, 1)
                        self.bmp = BlockBitmap(self.stream, bmp_ea)
                    # We must copy from parent only first and last sector if
                    # partly to overwrite and not copied yet
                    if in_parent:
                        LSS = self.metadata.logical_sector_size
                        if start_pos == self._pos and start_pos%LSS and not self.bmp.isset(sec_bi):
                            self.Parent.seek((self._pos//LSS)*LSS) # seek sector start: in parent...
                            self.stream.seek(blk_ea + sec_i*LSS) # ...and child
                            self.stream.write(self.Parent.read(LSS))
                            self.bmp.set(sec_bi)
                        sec_i2 = ((self._pos+put-1)%self.block) // LSS # last sector to write: in block...
                        sec_bi2 = ((self._pos+put-1)//LSS) % (1<<23) # ...and Bitmap chunk
                        if end_pos == self._pos+put and end_pos%LSS and not self.bmp.isset(sec_bi2):
                            self.Parent.seek(((self._pos+put-1)//LSS)*LSS) # seek sector start: in parent...
                            self.stream.seek(blk_ea + sec_i2*LSS) # ...and child
                            self.stream.write(self.Parent.read(LSS))
                            self.bmp.set(sec_bi2)
                    self.bmp.set(sec_bi, sec_bi2-sec_bi+1)
                # finally, writes content
                self.stream.seek(blk_ea + offset)
                if DEBUG&16: log("writing %d bytes at block 0x%08X, offset 0x%08X", put, blk_ea, offset)
                self.stream.write(s[i:i+put])
            else:
                raise BaseException("Invalid VHDX payload block status %d" % blk_s)
            i+=put
            self._pos+=put
        if self.bmp: # flush bitmap to disk if modified
            if DEBUG&16: log("Flushing Bitmap chunk 0x%08X at end of write loop", self.bmp.i)
            self.bmp.flush()


def mk_dynamic(name, size, block=(32<<20), upto=0, overwrite='no', _fparams=0, sector=512):
    "Creates an empty dynamic VHDX"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VHDX image!")

    if block < (1<<20) or block > (256<<20) or not math.log(block,2).is_integer():
        raise BaseException("Invalid block size: must be a power of 2, at least 1MB and at most 256 MB")

    f = myfile(name, 'wb')
    if DEBUG&16: log("making new Dynamic VHDX '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)

    # Identifier
    fti = FileTypeIdentifier()
    fti.sSignature = b'vhdxfile'
    fti.sCreator = 'Python 3'.encode('utf-16le')
    f.write(fti.pack()) # stores File Type Identifier

    # Headers 1 & 2
    h = VHDXHeader()
    h.sSignature = b'head'
    h.sFileWriteGuid = uuid.uuid4().bytes_le
    h.sDataWriteGuid = uuid.uuid4().bytes_le
    h.wVersion = 1
    h.dwLogLength = 0x100000
    h.u64LogOffset = 0x100000 # at 1st MB
    writea(f, h.pack(), 65536) # stores 1st header, aligned at 64K
    h.u64SequenceNumber = 1
    writea(f, h.pack(), 65536) # stores 2nd header, aligned at 64K

    # Region table and its copy
    regi_start = f.tell()
    r = RegionTableHeader()
    r.sSignature = b'regi'
    r.dwEntryCount = 2 # BAT and Metadata

    rte = RegionTableEntry(offset=16)
    rte.sGuid = RegionGUIDs[0].bytes_le # Metadata
    rte.u64FileOffset = 0x200000
    rte.dwLength = 0x100000
    rte.dwRequired = 1
    r._buf[16:48] = rte.pack()
    rte.sGuid = RegionGUIDs[1].bytes_le # BAT
    rte.u64FileOffset = 0x300000
    rte.dwLength = get_bat_facts(size, block, sector, _fparams!=2)[0]
    r._buf[48:80] = rte.pack()
    writea(f, r.pack(), 65536) # stores Region Table header, aligned at 64K
    writea(f, r.pack(), 65536) # stores its copy
      
    # Log is initially empty (sLogGuid set to 0), 1 MB aligned
    # A well-closed disk has zeroed log always?
    f.seek(0x100000-1)
    f.write(b'\x00')

    # Metadata region, 1 MB aligned
    f.seek(0x200000-1)
    f.write(b'\x00')
    
    m = MetadataTableHeader()
    m.sSignature = b'metadata'
    m.wEntryCount = 5
    f.write(m.pack())
    
    m = MetadataEntry()
    uids = list(MetadataGUIDs.keys())
    # File Parameters
    # 32-bit block size (1MB <= size <= 256 MB, must be a power of 2)
    # Windows 10 defaults to 32MB even for a small 1 GB image
    # bit 1: LeaveBlockAllocated (fixed VHDX); bit 2: HasParent
    m.sItemId = uids[0].bytes_le
    m.dwOffset = 0x10000
    m.dwLength = 8
    m.dwFlags = 4 # IsRequired
    f.write(m.pack())

    f.seek(0x210000)
    f.write(struct.pack('<I', block))
    f.write(struct.pack('<I', _fparams))

    f.seek(0x200000+64)
    # Virtual Disk Size
    m.sItemId = uids[1].bytes_le
    m.dwOffset = 0x10008
    m.dwFlags = 6 # IsRequired, IsVirtualDisk
    f.write(m.pack())

    f.seek(0x210008)
    f.write(struct.pack('<Q', size)) # Virtual disk size
    
    f.seek(0x200000+96)
    # Logical Sector Size
    m.sItemId = uids[2].bytes_le
    m.dwOffset = 0x10010
    m.dwLength = 4
    f.write(m.pack())

    f.seek(0x210010)
    f.write(struct.pack('<I', sector)) # Must be 512 or 4096 bytes
    
    f.seek(0x200000+128)
    # Physical Sector Size
    m.sItemId = uids[3].bytes_le
    m.dwOffset = 0x10014
    f.write(m.pack())

    f.seek(0x210014)
    f.write(struct.pack('<I', sector)) # Must be 512 or 4096 bytes
    # NOTE: Windows 10 sets this to 4096 for 1 TB disk

    f.seek(0x200000+160)
    # Virtual Disk Id
    m.sItemId = uids[4].bytes_le
    m.dwOffset = 0x10018
    m.dwLength = 16
    f.write(m.pack()) # Windows 10 puts it TWICE (?)

    f.seek(0x210018)
    f.write(uuid.uuid4().bytes_le) # A random GUID for the virtual disk

    # Seeks the BAT region end
    f.seek(0x300000 + rte.dwLength - 1)
    # Windows 10 sets the first BAT entry as a zeroed block with status
    # PAYLOAD_BLOCK_ZERO
    f.write(b'\x00')
    
    f.close()

    
def mk_fixed(name, size, block=(32<<20), upto=0, overwrite='no', sector=512):
    "Creates an empty fixed VHDX"
    if DEBUG&16: log("making new Fixed VHDX '%s' of %.02f MiB with block of %d bytes", name, float(size//(1<<20)), block)
    mk_dynamic(name, size, block, upto, overwrite, _fparams=1) # LeaveBlocksAllocated flag
    if DEBUG&16: log("converting Dynamic into Fixed VHDX...")
    f = Image(name, 'rb+')
    f.stream.seek(0,2)
    start = f.stream.tell()
    for i in range(f.bat.size):
        if i % f.chunk_ratio == 0: continue # skip bitmap entries
        f.bat[i] = ((start//(1<<20)) << 20) | 6 # stores block MB offset with PAYLOAD_BLOCK_FULLY_PRESENT status
        start += f.block
    # Allocate effective disk space
    f.stream.seek(size-1, 1)
    f.stream.write(b'\x00')
    f.close()


def mk_diff(name, base, block=(2<<20), overwrite='no', sector=512):
    "Creates an empty differencing VHDX"
    if os.path.exists(name) and overwrite!='yes':
        raise BaseException("Can't silently overwrite a pre-existing VHDX image!")
    if not os.path.exists(base):
        raise BaseException("Can't create a differencing VHDX image, parent does not exist!")
    parent = Image(base)
    if DEBUG&16: log("making new Differencing VHDX '%s' of %.02f MiB with block of %d bytes", name, float(parent.size//(1<<20)), block)
    # Image size must match parent, block may be different
    mk_dynamic(name, parent.size, block, 0, overwrite, _fparams=2) # HasParent flag
    if DEBUG&16: log("converting Dynamic into Differencing VHDX...")

    pl = ParentLocator()
    pl.sLocatorType = uuid.UUID('B04AEFB7-D19E-4A81-B789-25B8E9445913').bytes_le
    # Mandatory to recognize parent
    pl.entries['parent_linkage'] = '{%s}' % uuid.UUID(bytes_le=parent.header.sDataWriteGuid)
    # Only one of relative_path, volume_path or absolute_win32_path is required
    # Windows 10 completes the other paths on mount and adds parent_linkage2 key
    pl.entries['relative_path'] = utils.calc_rel_path(parent.name, name)
    pl.wKeyValueCount = len(pl.entries)
    buf = pl.pack()

    f = Image(name, 'rb+', _fparams=2)
    # Updates Metadata header wEntryCount
    mh = f.metadata
    mh.wEntryCount += 1
    f.stream.seek(mh._pos)
    f.stream.write(mh.pack())
    # Adds the Parent Locator Metadata Entry
    uids = list(MetadataGUIDs.keys())
    m = MetadataEntry(offset=mh._pos+32+32*len(mh.entries))
    m.sItemId = uids[5].bytes_le
    m.dwOffset = mh.entries[-1].dwOffset + mh.entries[-1].dwLength # Free metadata space is after last entry
    m.dwLength = len(buf)
    m.dwFlags = 4 # IsRequired
    f.stream.seek(m._pos) # abs offset of new entry
    f.stream.write(m.pack())
    # Puts the Parent Locator raw data
    f.stream.seek(mh._pos+m.dwOffset)
    f.stream.write(buf)
    
    f.close()
