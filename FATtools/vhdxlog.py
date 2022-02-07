# -*- coding: cp1252 -*-
"Utilities to handle VHDX Log"

import io, struct, uuid, zlib, ctypes, time, os, math
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools.crc32c import crc_update
from FATtools.utils import myfile

import FATtools.utils as utils
from FATtools.debug import log

#~ import logging
#~ logging.basicConfig(level=logging.DEBUG, filename='vhdxlog.log', filemode='w')

LOG_RECORD = 4096 # default size of Log record

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


class ZeroDescriptor(object):
    "Log Zero descriptor"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # 'zero'
    0x04: ('dwReserved', '<I'), # 0
    0x08: ('u64ZeroLength', '<Q'), # length of zero section (4K multiple)
    0x10: ('u64FileOffset', '<Q'), # offset where to put zeros (4K multiple)
    0x18: ('u64SequenceNumber', '<Q'), # sequence number matching Log header
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
    
    def __str__ (self):
        return utils.class2str(self, "VHDX Log Zero Descriptor @%X\n" % self._pos)

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def raw_sector(self):
        "Reconstructs and returns raw data sector"
        return bytearray(self.u64ZeroLength)

    def isvalid(self):
        if self.sSignature != b'zero' or self.dwReserved != 0:
            return 0
        if self.u64ZeroLength%LOG_RECORD or self.u64FileOffset%LOG_RECORD:
            return 0
        return 1


class DataDescriptor(object):
    "Log Data descriptor"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # desc
    0x04: ('dwTrailingBytes', '<I'), # 4 trailing bytes removed from raw sector
    0x08: ('u64LeadingBytes', '<Q'), # 8 initial bytes removed from raw sector
    0x10: ('u64FileOffset', '<Q'), # offset where to put restored sector (4K multiple)
    0x18: ('u64SequenceNumber', '<Q'), # sequence number matching Log header
    } # Size = 0x20 (32 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self.sector = None # associated DataSsector object
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(32)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
    
    def __str__ (self):
        return utils.class2str(self, "VHDX Log Data Descriptor @%X\n" % self._pos)

    __getattr__ = utils.common_getattr

    def raw_sector(self):
        "Reconstructs and returns raw data sector"
        if not self.sector: return None
        s = bytearray(self.sector._buf) # clone sector
        s[:8] = self._buf[8:16] # leading...
        s[4092:4096] = self._buf[4:8] # ...and trailing bytes
        return s

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def isvalid(self):
        if self.sSignature != b'desc' or self.u64FileOffset%LOG_RECORD:
            return 0
        return 1


class DataSector(object):
    "Log Data sector"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # data
    0x04: ('dwSequenceHigh', '<I'), # 4 most significant bytes of sequence number
    0x08: ('sData', '4084s'), # raw sector (except first 8 bytes and last 4, stored in descriptor)
    0xFFC: ('dwSequenceLow', '<I'), # 4 least significant bytes of sequence number
    } # Size = 0x1000 (4096 bytes)

    def __init__ (self, s=None, offset=0, stream=None):
        self._i = 0
        self._pos = offset # base offset
        self._buf = s or bytearray(32)
        self.stream = stream
        self._kv = self.layout.copy()
        self._vk = {} # { name: offset}
        for k, v in list(self._kv.items()):
            self._vk[v[0]] = k
    
    def __str__ (self):
        return utils.class2str(self, "VHDX Log Data Sector @%X\n" % self._pos)

    __getattr__ = utils.common_getattr

    def pack(self):
        "Updates internal buffer"
        for k, v in list(self._kv.items()):
            self._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(self, v[0]))
        return self._buf

    def isvalid(self):
        if self.sSignature != b'data':
            return 0
        return 1


class LogEntryHeader(object):
    "Log Entry header and sequence"
    layout = { # { offset: (name, unpack string) }
    0x00: ('sSignature', '4s'), # loge
    0x04: ('dwChecksum', '<I'), # CRC-32C over the first dwEntryLength bytes, zeroed this field
    0x08: ('dwEntryLength', '<I'), # entry length (4K multiple)
    0x0C: ('dwTail', '<I'), # offset of the 1st entry in the sequence
    0x10: ('u64SequenceNumber', '<Q'), # entry index
    0x18: ('u64DescriptorCount', '<Q'), # number of descriptors in this entry
    0x20: ('sLogGuid', '16s'), # GUID present in the VHDX Header at write time (valid if they match)
    0x30: ('u64FlushedFileOffset', '<Q'), # VHDX file size at Log entry write time
    0x38: ('u64LastFileOffset', '<Q'), # min VHDX file size required to store all structures at write time
    } # Size = 0x1000 (4096 byte)

    def __init__ (self, s=None, offset=0, stream=None):
        self.descriptors = [] # associated data/zero descriptors
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
        return utils.class2str(self, "VHDX Log Entry @%X\n" % self._pos)

    def isvalid(self, crc_check=0):
        if self.sSignature != b'loge' or self.dwEntryLength%4096 or self.dwTail%4096:
            return 0
        if crc_check and self.dwChecksum != struct.unpack("<I", self.crc())[0]:
            if DEBUG&4: log("VHDX Log Entry checksum 0x%X stored != 0x%X calculated", self.dwChecksum, struct.unpack("<I", self.crc())[0])
            return 0
        return 1


class LogStream(object):
    "Initialize the Log stream"
    def __init__ (self, vhdx):
        self.vhdx = vhdx
        self.offset = vhdx.header.u64LogOffset
        self.size = vhdx.header.dwLogLength
        self.stream = vhdx.stream
        self.sequence = [] # sequence of Log offsets belonging to candidate Active sequence
        self.seqn = 1 # starting sequence number

    def dump_all(self):
        if DEBUG&4: log("\n\n--- Dumping all Log records ---\n\n")
        f = self.stream
        i = self.offset - LOG_RECORD

        while i < self.offset + self.size:
            i+=LOG_RECORD
            if i > self.offset + self.size: break
            f.seek(i)
            s = f.read(64)
            if s[:4] != b'loge':
                if DEBUG&8: log("No Log Entry @0x%08X", i)
                continue
            
            h = LogEntryHeader(s, i)
            if DEBUG&8: log("Parsed Log Entry %s", h)
            
            # Exclude entries formally invalid or not belonging to current Log session
            skip = 0
            if not h.isvalid():
                if DEBUG&4: log("Invalid Log Entry")
                skip = 1
            elif h.sLogGuid != self.vhdx.header.sLogGuid:
                if DEBUG&4: log("Log GUIDS do not match")
                skip = 1
            if skip:
                f.seek(4032,1)
                continue

            # Check the full entry
            h._buf += f.read(h.dwEntryLength-64)
            if not h.isvalid(1):
                if DEBUG&4: log("Invalid Log Entry, bad CRC")
                continue

    # Log is restarted at each mount
    # Sequence number is high and random?
    # A self-pointing empty sequence starts a new recording session
    # Wrapping is possible only in a long session (after flushing)
    # Sequence number MUST be looked at, not only tail (i.e.: tail FF000 empty->tail 0 consecutive)
    # Active sequence begins with the self-pointing entry with the highest sequence number
    def find_sequence(self):
        "Searches the full Log for the active sequence"
        f = self.stream
        tail = -1
        seq = -1
        i=0
        loop=-1
        
        while i < self.size:
            f.seek(i+self.offset)
            h = LogEntryHeader(f.read(LOG_RECORD), i)
            # Exclude entries formally invalid or not belonging to current Log session
            if not h.isvalid() or h.sLogGuid != self.vhdx.header.sLogGuid:
                if DEBUG&4: log("Invalid Log Entry @%X", i)
                i+=LOG_RECORD
                if i >= self.size:
                    i = self.size
                    loop+=1
                    if loop > 0: break # avoid infinite loop if corrupted Log
                continue
            if DEBUG&8: log("Parsed Log Entry %s", h)

            # Check the full entry
            h._buf += f.read(h.dwEntryLength-LOG_RECORD)
            if not h.isvalid(1):
                if DEBUG&4: log("Invalid Log Entry, bad CRC")
                continue
            
            # If self-pointing entry
            if h.dwTail == i:
                # If we reached the actual Tail again
                if self.sequence and self.sequence[0].u64SequenceNumber == h.u64SequenceNumber:
                    if DEBUG&4: log("Log scan completed")
                    break
                # If no tail entry or greater sequence number
                if tail < 0 or h.u64SequenceNumber > seq:
                    if DEBUG&4: log("new sequence started, dwTail=0x%X u64SequenceNumber=0x%08X ", i, h.u64SequenceNumber)
                    tail = i
                    seq = h.u64SequenceNumber
                    self.sequence = [h]
                    i+=LOG_RECORD
                    if i >= self.size:
                        i = self.size
                        loop+=1
                        if loop > 0: break
                    continue

            # Append entry if same tail and consecutive
            if self.sequence and self.sequence[-1].dwTail == h.dwTail and self.sequence[-1].u64SequenceNumber == h.u64SequenceNumber-1:
                if DEBUG&4: log("new entry @0x%X u64SequenceNumber=0x%08X appended to sequence", i, h.u64SequenceNumber)
                self.sequence += [h]
                seq = h.u64SequenceNumber

            i+=LOG_RECORD
            if i >= self.size:
                i = self.size
                loop+=1
                if loop > 0: break
          
        if not self.sequence:
            raise BaseException("No Active Sequence found, Log is corrupted!")

        if DEBUG&4: log("Found active sequence of %d starting with %s", len(self.sequence), self.sequence[0])
        self.seqn = self.sequence[-1].u64SequenceNumber # records highest sequence number
        
    def replay_log(self):
        "Finds the Active sequence and replays it"
        self.find_sequence()
        if not self.sequence:
            return 0
        # Checks and eventually expands container
        size = self.stream.seek(0, 2)
        size2 = self.sequence[-1].u64FlushedFileOffset
        if size < size2:
            if DEBUG&4: log("Expanding VHDX container from %d to %d bytes", size, size2)
            self.stream.seek(size2-1)
            self.stream.write(b'\x00')
        # Parse and validate descriptors in each entry
        for e in self.sequence:
            o = 64
            tot_data = 0
            for j in range(e.u64DescriptorCount):
                if e._buf[o:o+4] == b'zero':
                    d = ZeroDescriptor(e._buf[o:o+32], e._pos+o)
                    if DEBUG&4: log("Found Zero Descriptor @0x%08X", e._pos+o)
                    if not d.isvalid():
                        if DEBUG&4: log("Found invalid Zero Descriptor @0x%08X", e._pos)
                        raise BaseException("Invalid Zero Descriptor: %s"%e._buf[o:o+4]) # since CRC check passed, exceptions should NEVER occur!
                elif e._buf[o:o+4] == b'desc':
                    ds_base = ((64 + 32*e.u64DescriptorCount + 4095)//LOG_RECORD)*LOG_RECORD # 4K pages occupied by descriptors (typically 1)
                    d = DataDescriptor(e._buf[o:o+32], e._pos+o)
                    sec_base = ds_base + j*LOG_RECORD
                    d.sector = DataSector(e._buf[sec_base: sec_base+LOG_RECORD], e._pos+sec_base)
                    if not d.isvalid() or not d.sector.isvalid():
                        if DEBUG&4: log("Found invalid Data Desriptor (Sector) @0x%08X (0x%08X)", d._pos, d.sector._pos)
                        raise BaseException("Invalid Data Descriptor (Sector) @0x%08X (0x%08X)"%(d._pos, d.sector._pos))
                    if DEBUG&4: log("Found Data Descriptor/Sector @0x%08X (0x%08X)", d._pos, d.sector._pos)
                else:
                    raise BaseException("Invalid Log Descriptor: %s"%e._buf[o:o+4])
                if d.u64SequenceNumber != e.u64SequenceNumber:
                    if DEBUG&4: log("Unmatched Sequence Numbers in VHDX Header and Descriptor")
                    raise BaseException("Unmatched Sequence Numbers in VHDX Header and Descriptor")
                if d.sSignature == b'desc':
                    if d.sector.dwSequenceHigh<<32 | d.sector.dwSequenceLow != d.u64SequenceNumber:
                        if DEBUG&4: log("Unmatched Sequence Numbers in Descriptor and Sector")
                        raise BaseException("Unmatched Sequence Numbers in Descriptor and Sector")
                e.descriptors += [d]
                o+=32
            # Rewrites logged sectors
            for desc in e.descriptors:
                if DEBUG&4: log("Replaying sector @0x%08X", desc.u64FileOffset)
                self.stream.seek(desc.u64FileOffset)
                self.stream.write(desc.raw_sector())
        return 1
