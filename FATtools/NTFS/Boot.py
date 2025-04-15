# -*- coding: cp1252 -*-
import FATtools.utils as utils

class Bootsector:
	layout = { # { offset: (name, unpack string) }
	0x00: ('chJumpInstruction', '3s'), # CHKDSK likes this
	0x03: ('chOemID', '8s'), # "NTFS    "
	0x0B: ('wBytesPerSec', '<H'), # typically, 512
	0x0D: ('uchSecPerClust', 'B'), # typically, 8 (4K cluster)
	0x0E: ('wReservedSectors', '<H'), # unused
	0x10: ('sUnused1', '3s'), # unused
	0x13: ('wUnused1', '<H'), # unused
	0x15: ('uchMediaDescriptor', 'B'), # typically 0xF8 (HDD)
	0x16: ('wUnused2', '<H'),
	0x18: ('wSecPerTrack', '<H'), # typically, 0x3F
	0x1A: ('wNumberOfHeads', '<H'), # typically, 0xFF
	0x1C: ('dwHiddenSectors', '<I'), # typically, 0x3F
	0x20: ('dwUnused1', '<I'), # typically 0
	0x24: ('dwUnused2', '<I'),# typically 0x800080
	0x28: ('u64TotalSectors', '<Q'), # count of volume sectors
	0x30: ('u64MFTLogicalClustNum', '<Q'), # cluster of the $MFT Record (and Master File Table start)
	0x38: ('u64MFTMirrLogicalClustNum', '<Q'), # $MFTMirr cluster (backup of first 4 $MFT Record)
	0x40: ('nClustPerMFTRecord', '<b'), # if n<0 then 2^-n. Typically, 0xF6 (-10) or 1K. 
	0x44: ('nClustPerIndexRecord', '<b'), # if n<0 then 2^-n.
	0x48: ('u64VolumeSerialNum', '<Q'),
	0x50: ('dwChecksum', '<I'), # typically zero
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
		# calculates and stores immediately some vital numbers
		self.LcnMFT = self.mft()
		self.cbCluster = self.cluster()
		self.cbRecord = self.record()
		self.cbIndx = self.index()
		
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
