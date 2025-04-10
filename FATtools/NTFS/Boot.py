# -*- coding: cp1252 -*-
import FATtools.utils as utils

class Bootsector:
	layout = { # { offset: (name, unpack string) }
	0x00: ('chJumpInstruction', '3s'),
	0x03: ('chOemID', '4s'),
	0x07: ('chDummy', '4s'),
	0x0B: ('wBytesPerSec', '<H'),
	0x0D: ('uchSecPerClust', 'B'),
	0x0E: ('wReservedSec', '<H'),
	0x11: ('uchReserved', '3s'),
	0x14: ('wUnused1', '<H'),
	0x16: ('uchMediaDescriptor', 'B'),
	0x17: ('wUnused2', '<H'),
	0x19: ('wSecPerTrack', '<H'),
	0x1B: ('wNumberOfHeads', '<H'),
	0x1D: ('dwHiddenSec', '<I'),
	0x21: ('dwUnused3', '<I'),
	0x25: ('dwUnused4', '<I'),
	0x29: ('u64TotalSec', '<Q'),
	0x30: ('u64MFTLogicalClustNum', '<Q'),
	0x38: ('u64MFTMirrLogicalClustNum', '<Q'),
	0x40: ('nClustPerMFTRecord', 'b'), # if <0, 2^-n
	0x44: ('nClustPerIndexRecord', 'b'),
	0x48: ('u64VolumeSerialNum', '<Q'),
	0x50: ('dwChecksum', '<I'),
	0x54: ('chBootstrapCode', '426s'),
	0x1FE: ('wSecMark', '<H') } # Size = 0x100 (512 byte)
	
	def __init__ (self, stream):
		self.stream = stream
		self._i = 0
		self._pos = stream.tell() # object start
		self._buf = stream.read(512) # standard Boot size
		if len(self._buf) != 512: raise EndOfStream
		self._kv = Bootsector.layout.copy()
		self._vk = {} # { name: offset}
		for k, v in self._kv.items():
			self._vk[v[0]] = k

	__getattr__ = utils.common_getattr
		
	def __str__ (self): return utils.class2str(self, "NTFS Boot @%x\n" % self._pos)
		
	def mft(self):
		"Gets the Master File Table (MFT) absolute offset."
		return self.u64MFTLogicalClustNum * self.wBytesPerSec * self.uchSecPerClust

	def cluster(self):
		"Gets the volume cluster size (typically, 4K)"
		return self.wBytesPerSec * self.uchSecPerClust

	def record(self):
		"Gets the NTFS Record size"
		if self.nClustPerMFTRecord < 0:
			return 1 << -self.nClustPerMFTRecord
		else:
			return self.nClustPerMFTRecord * self.wBytesPerSec * self.uchSecPerClust

	def index(self):
		"Gets the NTFS directory Index record size"
		if self.nClustPerIndexRecord < 0:
			return 1 << -self.nClustPerIndexRecord
		else:
			return self.nClustPerIndexRecord * self.wBytesPerSec * self.uchSecPerClust
