# -*- coding: cp1252 -*-
"""
Old CHS (Cylinder, Head, Sector) 24-bit sector numbering starts from sector 1.
The 3 bytes contain:

        H (8 bits)     S (6 bits)   C (8+2 bits)
        |             |           |
    HHHHHHHH -+- CC SSSSSS -+- CCCCCCCC

so that max values are: C=1024 (0-1023), H=256 (0-255), S=63 (1-63).

The old BIOS limit is (1024,16,63) or 1.032.192 x 512 byte sectors or 504 MiB.

New BIOSes permit (1024,256,63) or 16.515.072 sectors or 8.064 MiB: but a DOS
bug limits heads to 255 (16.450.560 sectors or 8.032,5 MiB).

MS-DOS 6.22/7.0 detect 8025 MiB on a 10 GiB raw disk and allow a 2047 MiB 
primary partition plus a 5977 MiB extended one (which can be splitted in
logical partitions up to 2047 MiB each).

MS-DOS 7.1 (Windows 95 OSR2) detects all 10 GiB and can set up a FAT32-LBA
primary partition (type 0x0C) with all the space.

LBA is limited to 2^32 sectors: that is 2 TiB (with 512b sectors) or 16 TiB
with modern OSes supporting 4 KiB sectors.

If the last LBA sector has no CHS representation, the triple (1023, 254, 63)
or FE FF FF is used.
GPT partitions use a protective MBR with the triple (1023, 255, 63) or
FF FF FF and a partition type of 0xEE."""

import struct, os
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools.mkfat import fat_mkfs
from FATtools.gptutils import *
from FATtools.utils import *
from FATtools.debug import log

mbr_types = {
    0x01: 'FAT12 Primary',
    0x04: 'FAT16 <32MB', # max 65535 sectors
    0x05: 'Extended CHS',
    0x06: 'FAT16B Primary',
    0x07: 'exFAT/NTFS',
    0x0B: 'FAT32 CHS', # Windows 95 OSR 2.1 (MS-DOS 7.0)
    0x0C: 'FAT32X LBA', # Windows 95 OSR2 (MS-DOS 7.1+)
    0x0E: 'FAT16B LBA',
    0x0F: 'Extended LBA',
    0xEF: 'EFI System',
    0xEE: 'GPT Protective MBR'
}

def get_min_mbrtype(size, compatibility=1):
    """Determines the minimum allowable FAT partition type according to its size.
    'compatibility' is 0 (old DOS), 1 (default, Win9x) or 2 (NT with 64K clusters available)."""
    MAX_CL = 32768
    if compatibility==2: MAX_CL*=2
    bType = 4 # FAT16 <32MiB (default)
    if compatibility == 0: bType = 1 # FAT12 (older default; but allowed up t0 127.6MB in theory)
    if size > (65535*512): bType = 6 # FAT16 >32MiB
    # Max FAT16 volume extension: clusters heap + 2 FATs + root + boot
    if size > (65525*MAX_CL+(256<<20)+(16<<20)+512): bType=0xB # FAT32 CHS (MS-DOS 7.0)
    if size > 1024*255*63*512: bType = 0xC # FAT32 LBA (MS-DOS 7.1+)
    # Max FAT32 (theoretical) volume extension: ((2**28-11)*65536) + (2*28-11)*4*2 + 9*512 or about 16 TB
    # But with 32-bit math and 0.5K sectors, no more than 2 TB are addressable
    if size > (2<<40): bType = 7 # exFAT/NTFS
    if DEBUG&1: log("calculated partition type %s (%02Xh)", mbr_types[bType], bType)
    return bType


class MBR_Partition(object):
    "Partition entry in MBR/EBR Boot record (16 bytes)"
    layout = { # { offset: (name, unpack string) }
    0x1BE: ('bStatus', 'B'), # 80h=bootable, 00h=not bootable, other=invalid
    0x1BF: ('sFirstSectorCHS', '3s'), # absolute (=disk relative) CHS address of 1st partition sector
    0x1C2: ('bType', 'B'), # partition type
    0x1C3: ('sLastSectorCHS', '3s'), # CHS address of last sector (or, if >8GB, FE FF FF [FF FF FF if GPT])
    0x1C6: ('dwFirstSectorLBA', '<I'), # LBA address of 1st sector
    # dwFirstSectorLBA in MBR/EBR 1st entry (logical partition) is relative to such partition start (typically 63 sectors);
    # in EBR *2nd* entry it's relative to *extended* partition start
    0x1CA: ('dwTotalSectors', '<I'), # number of sectors
    # 3 identical 16-byte groups corresponding to the other 3 primary partitions follow
    # DOS uses always 2 of the 4 slots
    # Modern Windows use LBA addressing (filled since DOS 3.30! - 26.04.2023)
    # PC-DOS 2 (1983) used *last* entry, and filled LBA
    } # Size = 0x10 (16 byte)

    def __init__ (self, s=None, offset=0, index=0, sector=512):
        self._sector = sector # physical sector size (512 or 4096)
        self.index = index
        self.heads_per_cyl = 255 # set default values
        self.sectors_per_cyl = 63
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(sector)
        self._kv = {} # { offset: name}
        for k, v in list(MBR_Partition.layout.items()):
            self._kv[k+index*16] = v
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k # partition 0...3
        
    __getattr__ = common_getattr

    def pack(self):
        "Update internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def __str__ (self):
        return class2str(self, "DOS %s Partition\n" % ('Primary','Extended','Unused','Unused')[self.index])

    def offset(self):
        "Returns partition offset"
        if self.dwFirstSectorLBA and self.dwTotalSectors: # safe at least back to DOS 3.30
            return self.lbaoffset()
        if self.bType in (0x7, 0xC, 0xE, 0xF): # if NTFS or FAT LBA
            return self.lbaoffset()
        else:
            return self.chsoffset()

    def chsoffset(self):
        "Returns partition absolute (=disk) byte offset"
        self.geometry()
        c, h, s = raw2chs(self.sFirstSectorCHS)
        if 0 in (h,s) or h>255 or s>63:
            if DEBUG&1: log("Invalid CHS data in Partition[%d]", self.index)
            return -1
        if DEBUG&1: log("chsoffset: returning %016Xh", chs2lba(c, h, s, self.heads_per_cyl, self.sectors_per_cyl)*self._sector)
        return chs2lba(c, h, s, self.heads_per_cyl, self.sectors_per_cyl)*self._sector

    def lbaoffset(self):
        "Returns partition relative byte offset (from this/extended partition start)"
        return self.dwFirstSectorLBA*self._sector
    
    def size(self):
        return self.dwTotalSectors*self._sector
        
    def geometry(self):
        "Returns effective partition Heads and Sectors, if available, or -1"
        c,h,s = raw2chs(self.sLastSectorCHS)
        if 0 in (h,s) or h>255 or s>63:
            if DEBUG&1: log("Invalid CHS data in Partition[%d]", self.index)
            return -1
        self.heads_per_cyl = h+1
        self.sectors_per_cyl = s
        return h+1, s


class MBR(object):
    "Master (or DOS Extended) Boot Record Sector"
    layout = { # { offset: (name, unpack string) }
    0x1FE: ('wBootSignature', '<H') # 55 AA
    } # Size = 0x200 (512 byte)

    # my universal boot code
    boot_code =  b'\x31\xC9\xFA\x8E\xD1\xBC\x00\x7C\x8E\xD9\x8E\xC1\xFB\x89\xE3\x89\xDE\xBF\x00\x06\xB9\x00\x01\xFC\xF3\xA5\xEA\x1F\x06\x00\x00\xBE\xBE\x07\x80\x3C\x80\x74\x1C\x83\xC6\x10\x81\xFE\xFE\x07\x7C\xF2\xBE\x86\x06\xAC\x3C\x00\x74\x08\x31\xDB\xB4\x0E\xCD\x10\xEB\xF3\xF4\xEB\xFD\xB4\x42\x87\xFE\xBE\x5C\x06\x8B\x4D\x08\x89\x4C\x08\x8B\x4D\x0A\x89\x4C\x0C\xCD\x13\x87\xFE\x73\x1D\x10\x00\x01\x00\x00\x7C\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xB8\x01\x02\x8B\x4C\x02\x8A\x74\x01\xCD\x13\x72\xB7\x81\x3E\xFE\x07\x55\xAA\x75\xAF\xEA\x00\x7C\x00\x00\x4E\x6F\x74\x68\x69\x6E\x67\x20\x74\x6F\x20\x62\x6F\x6F\x74\x2E\x00'

    def __init__ (self, s=None, offset=0, stream=None, disksize=0, sector=512):
        self._sector = sector # physical sector size (512 or 4096)
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal MBR size
        self.stream = stream
        self.heads_per_cyl = 0 # Heads Per Cylinder (max 255)
        self.sectors_per_cyl = 0 # Sectors Per Cylinder (max 63)
        self.is_lba = 0
        self.is_bootable = False # determine if add boot code and set bStatus
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        self.partitions = []
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        for i in range(4):
            self.partitions += [MBR_Partition(self._buf, index=i, sector=sector)]
            # try to detect disk geometry
            ret = self.partitions[-1].geometry()
            if ret == -1: continue
            self.heads_per_cyl = ret[0]
            self.sectors_per_cyl = ret[1]
    
    __getattr__ = common_getattr

    def pack(self, sector=512):
        "Update internal buffer"
        self.wBootSignature = 0xAA55 # set valid record signature
        if self.is_bootable:
            self._buf[0:len(self.boot_code)] = self.boot_code
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        for i in self.partitions:
            for k, v in list(i._kv.items()):
                self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(i, v[0]))
        return self._buf + bytearray(sector-len(self._buf))

    def __str__ (self):
        s = class2str(self, "Master/Extended Boot Record @%X\n" % self._pos)
        s += '\n' + str(self.partitions[0]) 
        s += '\n' + str(self.partitions[1])
        return s
        
    def delpart(self, index):
        "Deletes a partition, explicitly zeroing all fields"
        self.partitions[index].bStatus = 0
        self.partitions[index].sFirstSectorCHS = b'\0\0\0'
        self.partitions[index].bType = 0
        self.partitions[index].sLastSectorCHS = b'\0\0\0'
        self.partitions[index].dwFirstSectorLBA = 0
        self.partitions[index].dwTotalSectors = 0
    
    def setpart(self, index, start, size):
        "Creates a partition, given the start offset and size in bytes"
        pa = self.partitions[index]
        pa.dwFirstSectorLBA, pa.dwTotalSectors, pa.sFirstSectorCHS, pa.sLastSectorCHS = self.mkpart(start, size)
        #~ print("setpart(%d,%08Xh,%08Xh,%Xh): dwFirstSectorLBA=%08Xh, dwTotalSectors=%08Xh, sFirstSectorCHS=%s, sLastSectorCHS=%s"%(
        #~ index, start, size, self.heads_per_cyl, pa.dwFirstSectorLBA, pa.dwTotalSectors, pa.sFirstSectorCHS, pa.sLastSectorCHS))
        if DEBUG&1: log("setpart(%d,%08Xh,%08Xh,%Xh): dwFirstSectorLBA=%08Xh, dwTotalSectors=%08Xh, sFirstSectorCHS=%s, sLastSectorCHS=%s",
        index, start, size, self.heads_per_cyl, pa.dwFirstSectorLBA, pa.dwTotalSectors, pa.sFirstSectorCHS, pa.sLastSectorCHS)
        size = pa.dwTotalSectors*self._sector # final, rounded size
        if pa.sFirstSectorCHS[0] > 1023:
            pa.sFirstSectorCHS = chs2raw((0, 0, 1))
        else:
            pa.sFirstSectorCHS = chs2raw(pa.sFirstSectorCHS)
        if pa.sLastSectorCHS[0] > 1023:
            pa.sLastSectorCHS = chs2raw((1023, 254, 63))
        else:
            pa.sLastSectorCHS = chs2raw(pa.sLastSectorCHS)
        pa.bType = get_min_mbrtype(size) # effective type must be set after formatting
        if index > 0:
            pa.bType = 5 # Extended CHS
            if (start+size) > 1024*255*63*self._sector: pa.bType = 15 # Extended LBA

    def mkpart(self, offset, size):
        partsize=0
        c=0
        h=0
        s=0
        # First, try to span full cylinders (old DOS)
        # Heads and Sectors per Cylinder (Track) are useful to FAT format, too
        if self.heads_per_cyl:
            h = self.heads_per_cyl
            s = self.sectors_per_cyl
            c = size // self._sector // (h*s)
            if DEBUG&1: log("mkpart: CHS geometry %d-%d-%d (disk based)",c,h,s)
        if c > 1024 or not self.heads_per_cyl:
            c, h, s = get_geometry(size, self._sector)
            self.heads_per_cyl = h
            self.sectors_per_cyl = s
            if DEBUG&1: log("mkpart: CHS geometry %d-%d-%d (calculated)",c,h,s)
        cyl_size = h*s*self._sector
        partsize = (offset+c*cyl_size)//cyl_size*cyl_size - offset # partsize after cylinder alignment
        if DEBUG&1: log("mkpart: using %d heads and %d sectors per Cylinder", h, s)
        if DEBUG&1: log("mkpart: partition size after cylinder alignment: %d",partsize)
        if partsize > 1024*255*63*self._sector:
            # Modern Windows align at MB
            partsize = size // (1<<20) * (1<<20)
            if DEBUG&1: log("mkpart: can't use CHS, rounded size to MB align: %08Xh", partsize)
            h=255;s=63
        dwFirstSectorLBA = offset//self._sector
        dwTotalSectors = partsize//self._sector
        sFirstSectorCHS = lba2chs(dwFirstSectorLBA, h, s)
        if partsize > 1024*255*63*self._sector:
            sLastSectorCHS = (1023,254,63)
        else:
            sLastSectorCHS = lba2chs(dwFirstSectorLBA+dwTotalSectors-1, h, s)
        if DEBUG&1: log("mkpart: dwFirstSectorLBA=%08Xh, dwTotalSectors=%08Xh",dwFirstSectorLBA,dwTotalSectors)
        return dwFirstSectorLBA, dwTotalSectors, sFirstSectorCHS, sLastSectorCHS

def partition(disk, fmt='gpt', options={}):
    "Makes a single partition with all disk space (and makes it bootable if MBR)"
    disk.seek(0)
    SECTOR = options.get('phys_sector', 512)
    if fmt == 'mbr':
        part_size = disk.size
        if options.get('compatibility',1) == 0 and part_size > (2<<30): part_size = (2<<30)
        mbr = MBR(None, disksize=disk.size, sector=SECTOR)
        mbr.is_bootable = True
        if disk.type() == 'VHD':
            c, mbr.heads_per_cyl, mbr.sectors_per_cyl = struct.unpack('>HBB',disk.footer.dwDiskGeometry)
        elif disk.type() == 'VDI':
            c, mbr.heads_per_cyl, mbr.sectors_per_cyl = disk.header.dwCylinders, disk.header.dwHeads, disk.header.dwSectors
        else:
            c, mbr.heads_per_cyl, mbr.sectors_per_cyl = get_geometry(part_size)
        # Partitions are Cylinder-aligned in old MS-DOS scheme
        # They are 1 MB-aligned since Windows Vista
        # We can reserve 33 sectors at end, to allow later GPT conversion
        if options.get('lba_mode',0):
            mbr.setpart(0, 1<<20, part_size-(1<<20)-33*SECTOR)
        else:
            mbr.setpart(0, mbr.sectors_per_cyl*SECTOR, part_size-mbr.sectors_per_cyl*SECTOR)
        if options.get('mbr_type'):
            mbr.partitions[0].bType = options.get('mbr_type')
        else:
            options['mbr_type'] = mbr.partitions[0].bType
        if DEBUG&1: log("Made a MBR primary partition, type %X: %s", options['mbr_type'], mbr_types[options['mbr_type']])
        mbr.partitions[0].bStatus = 0x80 # mark as active (required by DOS)
        # Remove any previous GPT structure
        disk.write(32768*b'\x00')
        disk.seek(0)
        disk.write(mbr.pack(SECTOR))
        # Blank partition 1st sector (and any old boot sector)
        disk.seek(mbr.partitions[0].dwFirstSectorLBA*SECTOR)
        disk.write(SECTOR*b'\x00')
        # Blank any backup GPT header (and avoid pain to Windows disk changer?)
        disk.seek(part_size-SECTOR)
        disk.write(SECTOR*b'\x00')
        disk.close()
        return mbr

    if DEBUG&1: log("Making a GPT data partition on it\nWriting protective MBR")
    mbr = MBR(None, disksize=disk.size, sector=SECTOR)
    mbr.setpart(0, SECTOR, disk.size-SECTOR) # create primary partition
    mbr.partitions[0].bType = 0xEE # Protective GPT MBR
    mbr.partitions[0].dwTotalSectors = 0xFFFFFFFF
    disk.write(mbr.pack(SECTOR))
    if DEBUG&1: log('%s', mbr)

    if DEBUG&1: log("Writing GPT Header and 16K Partition Array")
    gpt = GPT(None)
    gpt.sEFISignature = b'EFI PART'
    gpt.dwRevision = 0x10000
    gpt.dwHeaderSize = 92
    gpt.u64MyLBA = 1
    gpt.u64AlternateLBA = (disk.size-SECTOR)//SECTOR
    gpt.u64FirstUsableLBA = 0x22
    gpt.dwNumberOfPartitionEntries = 0x80
    gpt.dwSizeOfPartitionEntry = 0x80
    # Windows stores a backup copy of the GPT array (16 KiB) before Alternate GPT Header
    gpt.u64LastUsableLBA = gpt.u64AlternateLBA - (gpt.dwNumberOfPartitionEntries*gpt.dwSizeOfPartitionEntry)//SECTOR - 1
    gpt.u64DiskGUID = uuid.uuid4().bytes_le
    gpt.u64PartitionEntryLBA = 2

    gpt.parse(ctypes.create_string_buffer(gpt.dwNumberOfPartitionEntries*gpt.dwSizeOfPartitionEntry))
    
    # Windows 11 does not like a start below 1MB nor an end in the last 2MB.
    # 11 even makes a MS reserved part in the first MB!
    gpt.setpart(0, (1<<20)//SECTOR, gpt.u64LastUsableLBA-((1<<20)//SECTOR))

    disk.write(gpt.pack(SECTOR))
    disk.seek(gpt.u64PartitionEntryLBA*SECTOR)
    disk.write(gpt.raw_partitions)

    # Blank partition 1st sector
    disk.seek(gpt.partitions[0].u64StartingLBA*SECTOR)
    disk.write(SECTOR*b'\x00') # clean old boot sector, if present

    if DEBUG&1: log("Writing backup of Partition Array and GPT Header at disk end")
    disk.seek((gpt.u64LastUsableLBA+1)*SECTOR)
    disk.write(gpt.raw_partitions) # writes backup
    disk.write(gpt._buf)
    if DEBUG&1: log('%s', gpt)
    disk.close()
    
    return gpt
