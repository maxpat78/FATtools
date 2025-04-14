# -*- coding: cp1252 -*-
import datetime, struct, os
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

class NTFSException(Exception): pass

# List of MFT record names designing special NTFS system files
NTFSReservedNames = ('.','$AttrDef','$BadClus','$Bitmap','$Boot','$Extend','$LogFile','$MFT','$MFTMirr','$Secure','$UpCase','$Volume','$Quota')

class EndOfStream(Exception): pass

class BadRecord(Exception): pass

class BadIndex(Exception): pass

def common_update_and_swap(c):
	"Updates class dictionaries with specific Attribute informations"
	if c.uchNonResFlag:
		ko = 64
	else:
		ko = 24
	for k in c.specific_layout.keys(): # update table with effective offsets
		c._kv[k+ko] = c.specific_layout[k]
	for k, v in c.specific_layout.items():
		c._vk[v[0]] = k+ko
	
def common_dataruns_decode(self):
	self.dataruns = (0,0) # decoded data run(s)
	firstrun = 1
	i = self._i + self.wDatarunOffset
	while 1:
		# First byte is a couple 4-bit nibble index
		c = self._buf[i]
		if not c: break
		# Least significant nibble tells how many bytes 
		# to allocate for the clusters run length
		n_length = c & 0xF
		# Most significant nibble refers to starting cluster offset
		n_offset = c >> 4
		if DEBUG&8: log("n_length=%d, n_offset=%d", n_length, n_offset)
		#~ print("n_length=%d, n_offset=%d"%( n_length, n_offset))
		# Loads and computates the run length in clusters
		i += 1
		length = self._buf[i:i+n_length] + bytearray((8-n_length)*b'\x00')
		length = struct.unpack_from("<Q", length)[0] # always expands to an unsigned QWORD (128-bit)
		# Loads and computates the offset of the first cluster
		# Each following offset is relative to its previous offset, and can be negative!
		i += n_length
		offset = self._buf[i:i+n_offset] + bytearray((8-n_offset)*b'\x00')
		if not firstrun: # from 2nd datarun onward, possible negative offsets are detected
			if offset[n_offset-1] >= 0x80: # sign is in the last byte (LE order)
				offset = self._buf[i:i+n_offset] + bytearray((8-n_offset)*b'\xFF')
			# expand to...
			if n_offset == 1: # ...BYTE
				offset = struct.unpack_from("<b", offset[:1])[0]
			elif n_offset == 2: # ...WORD
				offset = struct.unpack_from("<h", offset[:2])[0]
			elif n_offset in (3,4):  # ...DWORD
				offset = struct.unpack_from("<i", offset[:4])[0]
			else: # ...QWORD
				offset = struct.unpack_from("<q", offset)[0]
		else:
			offset = struct.unpack_from("<Q", offset)[0]
			firstrun = 0
		if not offset:
			if DEBUG&8: log("Sparse files are non supported (yet)!")
		# computates and stores run length and offset according to cluster size
		if DEBUG&8: log("length=%d offset=%d prevoffset=%d", length, offset, self.dataruns[-1])
		# CAVE! EFFECTIVE cluster size MUST be used!
		self.dataruns += (length* self.boot.cbCluster, (offset* self.boot.cbCluster+self.dataruns[-1]))
		i += n_offset
	if DEBUG&8: log("decoded dataruns @%d:\n%s", self._i, self.dataruns)

def nt2uxtime(t):
	"Converts date and time from NT to Python (Unix) format"
	# NT: 100 ns lapses since midnight of 1/1/1601
	# Unix: seconds since 1/1/1970
	# Delta is 134774 days o 11.644.473.600 seconds
	t = t//10000000 - 11644473600
	if t < 0: t = 0
	return datetime.datetime.fromtimestamp(t)

def common_fixup(self):
	"Verifies and applies the NTFS record fixup"
	# The fixup WORD is at the beginning of the Update Sequence Array (USA)
	fixupn = self._buf[self.wUSAOffset:self.wUSAOffset+2]
	for i in range(1, self.wUSASize):
		fixuppos = i*512 - 2 # last sector WORD
		if fixupn != self._buf[fixuppos:fixuppos+2]: print("Bad Fixup!")
		offs = self.wUSAOffset+2*i # offset of the WORD to replace in the USA
		self._buf[fixuppos:fixuppos+2] = self._buf[offs:offs+2]
