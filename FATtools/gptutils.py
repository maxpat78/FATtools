# -*- coding: cp1252 -*-
"Utilities to handle GPT partitions"

import struct, uuid, zlib, ctypes, os

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools.debug import log
from FATtools import utils

# Common Windows Partition GUIDs
partition_uuids = {
    uuid.UUID('00000000-0000-0000-0000-000000000000'): None,
    uuid.UUID('C12A7328-F81F-11D2-BA4B-00A0C93EC93B'): 'EFI System Partition',
    uuid.UUID('E3C9E316-0B5C-4DB8-817D-F92DF00215AE'): 'Microsoft Reserved Partition',
    uuid.UUID('EBD0A0A2-B9E5-4433-87C0-68B6B72699C7'): 'Microsoft Basic Data Partition',
    uuid.UUID('DE94BBA4-06D1-4D40-A16A-BFD50179D6AC'): 'Microsoft Windows Recovery Environment',
}



class GPT(object):
    "GPT Header Sector according to UEFI Specs"
    layout = { # { offset: (name, unpack string) }
    0x0: ('sEFISignature', '8s'), # EFI PART
    0x8: ('dwRevision', '<I'), # 0x10000
    0xC: ('dwHeaderSize', '<I'), # 92 <= size <= blksize
    0x10: ('dwHeaderCRC32', '<I'), # CRC32 on dwHeaderSize bytes (this field zeroed)
    0x14: ('dwReserved', '<I'), # must be zero
    0x18: ('u64MyLBA', '<Q'), # LBA of this structure
    0x20: ('u64AlternateLBA', '<Q'), # LBA of backup GPT Header (typically last block)
    0x28: ('u64FirstUsableLBA', '<Q'),
    0x30: ('u64LastUsableLBA', '<Q'),
    0x38: ('u64DiskGUID', '16s'),
    0x48: ('u64PartitionEntryLBA', '<Q'), # LBA of GUID Part Entry array
    0x50: ('dwNumberOfPartitionEntries', '<I'),
    0x54: ('dwSizeOfPartitionEntry', '<I'), # 128*(2**i)
    0x58: ('dwPartitionEntryArrayCRC32', '<I') # The CRC32 of the GUID Partition Entry array
    # REST IS RESERVED AND MUST BE ZERO
    } # Size = 0x200 (512 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512) # normal GPT Header  size
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        self.partitions = []
        self.raw_partitions = None
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
    
    __getattr__ = utils.common_getattr

    def pack(self, sector=512):
        "Updates internal buffer"
        for i in self.partitions:
            for k, v in list(i._kv.items()):
                self.raw_partitions[i._pos+k:i._pos+k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(i, v[0]))
        self._crc32a()
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        self._crc32()
        return self._buf+bytearray(sector-len(self._buf))

    def __str__ (self):
        return utils.class2str(self, "GPT Header @%X\n" % self._pos)

    def _crc32(self):
        s = self._buf
        s[0x10:0x14] = bytearray(4)
        #~ crc = zlib.crc32(''.join(map(chr, s[:self.dwHeaderSize])))
        crc = zlib.crc32(s[:self.dwHeaderSize])
        crc &= 0xFFFFFFFF
        self.dwHeaderCRC32 = crc
        if DEBUG&1: log("_crc32 returned %08X on GPT Header", crc)
        s[0x10:0x14] = [crc&0xFF, (crc&0x0000FF00)>>8, (crc&0x00FF0000)>>16, (crc&0xFF000000)>>24]
        return crc

    def _crc32a(self):
        crc = zlib.crc32(self.raw_partitions)
        crc &= 0xFFFFFFFF
        self.dwPartitionEntryArrayCRC32 = crc
        if DEBUG&1: log("_crc32a returned %08X on GPT Array", self.dwPartitionEntryArrayCRC32)
        return self.dwPartitionEntryArrayCRC32
    
    def parse(self, s=None):
        "Parses the GUID Partition Entry Array"
        if not s:
            s = self.raw_partitions
        else:
            self.raw_partitions = s
        for i in range(self.dwNumberOfPartitionEntries):
            j = i*self.dwSizeOfPartitionEntry
            k = j + self.dwSizeOfPartitionEntry
            self.partitions += [GPT_Partition(s[j:k], index=i, offset=self.dwSizeOfPartitionEntry*i)]

    def delpart(self, index):
        "Deletes a partition"
        self.partitions[index].sPartitionTypeGUID = 16*'\0'
        self.partitions[index].sUniquePartitionGUID = 16*'\0'
        self.partitions[index].u64StartingLBA = 0
        self.partitions[index].u64EndingLBA = 0
        self.partitions[index].u64Attributes = 0
        self.partitions[index].sPartitionName = 72*'\0'

    def setpart(self, index, start, size, name='New Basic Data Partition'):
        "Creates a partition, given the start offset and size in bytes"
        self.partitions[index].sPartitionTypeGUID = uuid.UUID('EBD0A0A2-B9E5-4433-87C0-68B6B72699C7').bytes_le
        self.partitions[index].sUniquePartitionGUID = uuid.uuid4().bytes_le
        self.partitions[index].u64StartingLBA = start
        self.partitions[index].u64EndingLBA = start+size
        self.partitions[index].sPartitionName = name.encode('utf-16le')



class GPT_Partition(object):
    "Partition entry in GPT Array (128 bytes)"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sPartitionTypeGUID', '16s'),
    0x10: ('sUniquePartitionGUID', '16s'),
    0x20: ('u64StartingLBA', '<Q'),
    0x28: ('u64EndingLBA', '<Q'),
    0x30: ('u64Attributes', '<Q'),
    0x38: ('sPartitionName', '72s')
    # REST IS RESERVED AND MUST BE ZERO
    } # Size = 0x80 (128 byte)

    def __init__ (self, s=None, offset=0, index=0):
        self.index = index
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(512)
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
        
    __getattr__ = utils.common_getattr

    def pack(self):
        "Update internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def __str__ (self):
        return utils.class2str(self, "GPT Partition Entry #%d\n" % self.index)

    def gettype(self):
        return uuid.UUID(bytes_le=self.sPartitionTypeGUID)
    
    def uuid(self):
        return uuid.UUID(bytes_le=self.sUniquePartitionGUID)
        
    def name(self):
        name = self.sPartitionName.decode('utf-16le')
        name = name[:name.find('\x00')]
        return name
