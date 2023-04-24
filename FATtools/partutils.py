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
LBA is limited to 2^32 sectors or 2 TiB.

If the last LBA sector has no CHS representation, the triple (1023, 254, 63)
or FE FF FF is used.
GPT partitions use a protective MBR with the triple (1023, 255, 63) or
FF FF FF and a partition type of 0xEE."""

import struct, os
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools import utils
from FATtools.gptutils import *

from FATtools.debug import log

def chs2lba(c, h, s, max_hpc=16, max_spc=63):
	# Max sectors per cylinder (track): 63
	if max_spc < 1 or max_spc > 63:
		return -1
	# Max heads per cyclinder (track): 255
	if max_hpc < 1 or max_hpc > 255:
		return -2
	if s < 1 or s > max_spc:
		return -0x10
	if h < 0 or h > max_hpc:
		return -0x20
	return (c*max_hpc+h)*max_spc + (s-1)

def lba2chs(lba, hpc=0):
    spc = 63
    if not hpc:
        for hpc in (16,32,64,128,255):
            if lba <= spc*hpc: break
    c = lba//(hpc*spc)
    h = (lba//spc)%hpc
    s = (lba%spc)+1
    return c, h, s

def size2chs(n, getgeometry=0):
    lba = n//512
    
    # Avoid computations with some well-known IBM PC floppy formats
    # Look at: https://en.wikipedia.org/wiki/List_of_floppy_disk_formats#Logical_formats
    if lba == 640:
        return (80, 1, 8) # 3.5in DS/DD 320KB
    elif lba == 720:
        return (80, 1, 9) # 3.5in DS/DD 360KB
    elif lba == 1280:
        return (80, 2, 8) # 3.5in DS/DD 640KB
    elif lba == 1440:
        return (80, 2, 9) # 3.5in DS/DD 720KB
    elif lba == 2880:
        return (80, 2, 18) # 3.5in DS/HD 1440KB
    elif lba == 3360:
        return (80, 2, 21) # 3.5in DS/HD 1680KB (MS-DMF)
    elif lba == 3440:
        return (82, 2, 21) # 3.5in DS/HD 1720KB
    elif lba == 5760:
        return (80, 2, 36) # 3.5in DS/XD 2880KB

    for hpc in (2,16,32,64,128,255):
        c,h,s = lba2chs(lba,hpc)
        if c < 1024: break
    if DEBUG&1: log("size2chs: calculated Heads Per Cylinder: %d", hpc)
    if not getgeometry:
        return c,h,s
    else:
        # partition that fits in the given space
        # full number of cylinders, heads per cyl and sectors per track to use
        return c+1, hpc, 63
    
def chs2raw(t):
    "A partire da una tupla (C,H,S) calcola i 3 byte nell'ordine registrato nel Master Boot Record"
    c,h,s = t
    if c > 1023:
        B1, B2, B3 = 254, 255, 255
    else:
        B1, B2, B3 = h, (c&768)>>2|s, c&255
    #~ print "DEBUG: MBR bytes for LBA %d (%Xh): %02Xh %02Xh %02Xh"%(lba, lba, B1, B2, B3)
    return b'%c%c%c' % (B1, B2, B3)

def raw2chs(t):
    "Converte i 24 bit della struttura CHS nel MBR in tupla"
    h,s,c = t[0], t[1], t[2]
    return ((s  & 192) << 2) | c, h, s & 63

def mkpart(offset, size, hpc=16):
    c, h, s = size2chs(size-1, 1)
    orig_size = size
    size = 512*c*h*s # adjust size
    if size > orig_size:
        c -= 1
        size = 512*c*h*s # re-adjust size
    #~ print ("Rounded CHS for %.02f MiB is %d-%d-%d (%.02f MiB)" % (orig_size/(1<<20), c,h,s, size/(1<<20)))
    dwFirstSectorLBA = offset//512
    sFirstSectorCHS = lba2chs(dwFirstSectorLBA, hpc)
    dwTotalSectors = ((size-offset)//512)
    sLastSectorCHS = lba2chs(dwFirstSectorLBA+dwTotalSectors-1, hpc)
    return dwFirstSectorLBA, dwTotalSectors, sFirstSectorCHS,sLastSectorCHS



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
    # Modern Windows use LBA addressing
    } # Size = 0x10 (16 byte)

    def __init__ (self, s=None, offset=0, index=0):
        self.index = index
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512)
        self._kv = {} # { offset: name}
        for k, v in list(MBR_Partition.layout.items()):
            self._kv[k+index*16] = v
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k # partition 0...3
        
    __getattr__ = utils.common_getattr

    def pack(self):
        "Update internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "DOS %s Partition\n" % ('Primary','Extended','Unused','Unused')[self.index])

    def offset(self):
        "Returns partition offset"
        if self.bType in (0x7, 0xC, 0xE, 0xF): # if NTFS or FAT LBA
            return self.lbaoffset()
        else:
            return self.chsoffset()

    def chsoffset(self):
        "Returns partition absolute (=disk) byte offset"
        c, h, s = raw2chs(self.sFirstSectorCHS)
        if DEBUG&1: log("chsoffset: returning %016X", chs2lba(c, h, s, self.heads_per_cyl)*512)
        return chs2lba(c, h, s, self.heads_per_cyl)*512

    def lbaoffset(self):
        "Returns partition relative byte offset (from this/extended partition start)"
        return 512 * self.dwFirstSectorLBA
    
    def size(self):
        return 512 * self.dwTotalSectors


class MBR(object):
    "Master (or DOS Extended) Boot Record Sector"
    layout = { # { offset: (name, unpack string) }
    0x1FE: ('wBootSignature', '<H') # 55 AA
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None, disksize=0):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal MBR size
        self.stream = stream
        self.heads_per_cyl = 0 # Heads Per Cylinder (disk based)
        self.is_lba = 0
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        self.partitions = []
        self.heads_per_cyl = size2chs(disksize, True)[1] # detects disk geometry, size based
        if DEBUG&1: log("Calculated Heads Per Cylinder: %d", self.heads_per_cyl)
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        for i in range(2): # Part. 2-3 unused in DOS
            self.partitions += [MBR_Partition(self._buf, index=i)]
            self.partitions[-1].heads_per_cyl = self.heads_per_cyl
    
    __getattr__ = utils.common_getattr

    def pack(self):
        "Update internal buffer"
        self.wBootSignature = 0xAA55 # set valid record signature
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        for i in self.partitions:
            for k, v in list(i._kv.items()):
                self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(i, v[0]))
        return self._buf

    def __str__ (self):
        s = utils.class2str(self, "Master/Extended Boot Record @%X\n" % self._pos)
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
    
    def setpart(self, index, start, size, hpc=16):
        "Creates a partition, given the start offset and size in bytes"
        part = MBR_Partition(index=index)
        dwFirstSectorLBA, dwTotalSectors, sFirstSectorCHS, sLastSectorCHS = mkpart(start, size, self.heads_per_cyl)
        if DEBUG&1: log("setpart(%d,%d,%d,%d): dwFirstSectorLBA=%08Xh, dwTotalSectors=%08Xh, sFirstSectorCHS=%s, sLastSectorCHS=%s",index, start, size, hpc, dwFirstSectorLBA, dwTotalSectors, sFirstSectorCHS, sLastSectorCHS)
        part.dwFirstSectorLBA = dwFirstSectorLBA
        part.dwTotalSectors = dwTotalSectors
        if sFirstSectorCHS[0] > 1023:
            part.sFirstSectorCHS = chs2raw((0, 0, 1))
        else:
            part.sFirstSectorCHS = chs2raw(sFirstSectorCHS)
        if sLastSectorCHS[0] > 1023:
            part.sLastSectorCHS = chs2raw((1023, 254, 63))
        else:
            part.sLastSectorCHS = chs2raw(sLastSectorCHS)
        #~ if index==0:
            #~ part.bStatus = 0x80
        part.bType = 6 # Primary FAT16 > 32MiB
        if index > 0:
            if (start+size) < 8<<30:
                part.bType = 5 # Extended CHS
            else:
                part.bType = 15 # Extended LBA
        if size < 32<<20:
            part.bType = 4 # FAT16 < 32MiB
        elif size > 8032<<20:
            part.bType = 0xC # FAT32 LBA
        if DEBUG&1: log("setpart: auto set partition type %02X", part.bType)
        self.partitions[index] = part


mbr_types = {
0x01: 'FAT12 Primary',
0x04: 'FAT16 <32MB',
0x05: 'Extended CHS',
0x06: 'FAT16 Primary',
0x0B: 'FAT32 CHS',
0x0C: 'FAT32 LBA',
0x0E: 'FAT16 LBA',
0x0F: 'Extended LBA',
0xEE: 'GPT'
}


#~ def partition(disk, fmt='gpt', part_name='My Partition', mbr_type=0xC):
def partition(disk, fmt='gpt', part_name='', mbr_type=0xC):
    "Makes a single partition with all disk space"
    disk.seek(0)
    if fmt == 'mbr':
        if DEBUG&1: log("Making a MBR primary partition, type %X: %s", mbr_type, mbr_types[mbr_type])
        mbr = MBR(None, disksize=disk.size)
        # Partitions are track-aligned (i.e., 32K-aligned) in old MS-DOS scheme
        # They are 1 MB-aligned since Windows Vista
        # We can reserve 33 sectors at end, to allow later GPT conversion
        if mbr_type in (0xC, 0xE):
            mbr.setpart(0, 1<<20, disk.size - ((1<<20)+33*512))
        elif mbr_type in (0x4, 0x6, 0xB):
            # MS-DOS < 7.1 has 2 GB limit
            if mbr_type < 0xB:
                size = min(disk.size, 520*128*63*512)
            else:
                size = disk.size
            if DEBUG&1: log("Adjusted part size for MS-DOS pre 7.1: %d", size)
            mbr.setpart(0, 63*512, size - 63*512)
        else:
            mbr.setpart(0, 63*512, disk.size - 97*512)
        mbr.partitions[0].bType = mbr_type # overwrites setpart guess
        # Remove any previous GPT structure
        disk.write(32768*b'\x00')
        disk.seek(0)
        disk.write(mbr.pack())
        # Blank partition 1st sector (and any old boot sector)
        disk.seek(mbr.partitions[0].dwFirstSectorLBA*512)
        disk.write(512*b'\x00')
        # Blank any backup GPT header (and avoid pain to Windows disk changer?)
        disk.seek(disk.size-512)
        disk.write(512*b'\x00')
        disk.close()
        return mbr

    if DEBUG&1: log("Making a GPT data partition on it\nWriting protective MBR")
    mbr = MBR(None, disksize=disk.size)
    mbr.setpart(0, 512, disk.size-512) # create primary partition
    mbr.partitions[0].bType = 0xEE # Protective GPT MBR
    mbr.partitions[0].dwTotalSectors = 0xFFFFFFFF
    disk.write(mbr.pack())
    if DEBUG&1: log('%s', mbr)

    if DEBUG&1: log("Writing GPT Header and 16K Partition Array")
    gpt = GPT(None)
    gpt.sEFISignature = b'EFI PART'
    gpt.dwRevision = 0x10000
    gpt.dwHeaderSize = 92
    gpt.u64MyLBA = 1
    gpt.u64AlternateLBA = (disk.size-512)//512
    gpt.u64FirstUsableLBA = 0x22
    gpt.dwNumberOfPartitionEntries = 0x80
    gpt.dwSizeOfPartitionEntry = 0x80
    # Windows stores a backup copy of the GPT array (16 KiB) before Alternate GPT Header
    gpt.u64LastUsableLBA = gpt.u64AlternateLBA - (gpt.dwNumberOfPartitionEntries*gpt.dwSizeOfPartitionEntry)//512 - 1
    gpt.u64DiskGUID = uuid.uuid4().bytes_le
    gpt.u64PartitionEntryLBA = 2

    gpt.parse(ctypes.create_string_buffer(gpt.dwNumberOfPartitionEntries*gpt.dwSizeOfPartitionEntry))
    gpt.setpart(0, gpt.u64FirstUsableLBA, gpt.u64LastUsableLBA-gpt.u64FirstUsableLBA+1, part_name)

    disk.write(gpt.pack())
    disk.seek(gpt.u64PartitionEntryLBA*512)
    disk.write(gpt.raw_partitions)

    # Blank partition 1st sector
    disk.seek(gpt.partitions[0].u64StartingLBA*512)
    disk.write(512*b'\x00') # clean old boot sector, if present

    if DEBUG&1: log("Writing backup of Partition Array and GPT Header at disk end")
    disk.seek((gpt.u64LastUsableLBA+1)*512)
    disk.write(gpt.raw_partitions) # writes backup
    disk.write(gpt._buf)
    if DEBUG&1: log('%s', gpt)
    disk.close()
    
    return gpt
