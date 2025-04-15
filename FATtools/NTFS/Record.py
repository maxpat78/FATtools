# -*- coding: cp1252 -*-
import array, logging, struct, os
from .Attribute import *
from .Commons import *
import FATtools.utils as utils
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

__all__ = ['Record']

class Record:
	layout = {
	0x00: ('fileSignature', '4s'),
	0x04: ('wUSAOffset', '<H'), # Update Sequence Array offset
	0x06: ('wUSASize', '<H'), # Array size (in sectors)
	0x08: ('u64LogSeqNumber', '<Q'),
	0x10: ('wSequence', '<H'),
	0x12: ('wHardLinks', '<H'),
	0x14: ('wAttribOffset', '<H'),
	0x16: ('wFlags', '<H'), # 1=Record in use
	0x18: ('dwRecLength', '<I'),
	0x1C: ('dwAllLength', '<I'),
	0x20: ('u64BaseMftRec', '<Q'),
	0x28: ('wNextAttrID', '<H'),
	0x2A: ('wFixupPattern', '<H')
	} # Size = 0x2C (44 byte)

	def __init__ (self, boot, mftstream):
		#~ mftrecord.boot.stream --> NTFS Volume stream
		#~ mftrecord._stream --> $MFT stream
		self.boot = boot
		record_size = boot.cbRecord
		self._i = 0 # buffer offset
		self._pos = mftstream.tell() # MFT offset
		self._buf = mftstream.read(record_size)
		self._stream = mftstream
		self._attributes = {} # dictionary { attribute type: [items] }
		if len(self._buf) != record_size:
			raise EndOfStream
		self._kv = Record.layout.copy()
		self._vk = {} # { name: offset}
		for k, v in self._kv.items():
			self._vk[v[0]] = k
		
		if not self.wFlags & 0x1: return # Unused record

		if self.wUSAOffset == 0x30: # NTFS v3.1
			self.layout[0x2C] = ('dwMFTRecNumber', '<I') # redundant Record number
		
		if self.fileSignature != b'FILE':
			print("Malformed NTFS Record @%x!" % self._pos)
			return
		
		self.fixup() # verifies and applies the NTFS fixup
		
		# Decode Attributes
		offset = self.wAttribOffset
		while offset < record_size:
			dwType = struct.unpack_from('<I', self._buf, offset)[0]
			if dwType == 0xFFFFFFFF: break
			elif dwType == 0x10:
				a = Standard_Information(self, offset)
			elif dwType == 0x20:
				a = Attribute_List(self, offset)
				self._expand_attribute_list(a)
			elif dwType == 0x30:
				a = File_Name(self, offset)
			elif dwType == 0x50:
				a = Security_Descriptor(self, offset)
			elif dwType == 0x60:
				a = Volume_Name(self, offset)
			elif dwType == 0x70:
				a = Volume_Information(self, offset)
			elif dwType == 0x80:
				a = Data(self, offset)
			elif dwType == 0x90:
				a = Index_Root(self, offset)
			elif dwType == 0xA0:
				a = Index_Allocation(self, offset)
			elif dwType == 0xB0:
				a = Bitmap(self, offset)
			else:
				a = Attribute(self, offset)
			if DEBUG&8: log("Decoded Attribute:\n%s", a)
			if a.dwType in self._attributes:
				self._attributes[a.dwType] += [a]
			else:
				self._attributes[a.dwType] = [a]
			# If Attribute exceeds Record length, something's wrong...
			if a.dwFullLength + offset > record_size-6:
				if DEBUG&8: log("Attribute > Record!!!\n%s", self)
				break
			offset += a.dwFullLength
		if DEBUG&8: log("Parsed MFT Record #%x @%x:\n%s", self.dwMFTRecNumber, self._pos, self)
		#~ print("Parsed MFT Record #%x @%x:\n%s"%( self.dwMFTRecNumber, self._pos, self))
		#~ for a in self._attributes.values():
			#~ print(a[0])

	__getattr__ = utils.common_getattr
	fixup = common_fixup
		
	def __str__ (self): return utils.class2str(self, "MFT Record 0x%X @%x\n" % (self._pos//self.boot.cbRecord, self._pos))

	def iterator(self):
		"Iterates through all MFT Records, from start"
		offset = 0
		while offset < self._stream.size:
			self._stream.seek(offset)
			yield Record(self.boot, self._stream)
			offset += self.boot.cbRecord

	def next(self, index=1):
		"Parses the next or n-th Record"
		off = index * self.boot.cbRecord
		if index > 1:
			if off > self._stream.size:
				raise NTFSException("MFT Record #%d does not exist!" % index)
			self._stream.seek(off)
		else:
			if self._pos + off > self._stream.size:
				raise NTFSException("MFT Record #%d does not exist!" % index)
			self._stream.seek(self._pos + off)
		if DEBUG&8: log("next MFT Record @0x%X, index=%d", self._stream.tell(), index)
		#~ print("next MFT Record @0x%X, index=%d"%( self._stream.tell(), index))
		return Record(self.boot, self._stream)

	def find_attribute(self, typ):
		if type(typ) == type(''):
			typ = attributes_by_name[typ]
		if typ in self._attributes:
			return self._attributes[typ]
		else:
			return None

	def _expand_attribute_list(self, al):
		i = al._i + 24
		expanded = ()
		while i < al._i + al.dwFullLength:
			alis = Attribute_Listed(self, i)
			expanded += (alis,)
			if DEBUG&8: log(alis)
			i += alis.wEntryLength # next list item
		al.list = expanded
		for a in expanded:
			n = a.u64BaseMFTFileRef & 0x0000FFFFFFFFFFFF
			# this listed attributed resides in this same Record
			if n*self.boot.cbRecord == self._pos:
				continue
			if DEBUG&8: log("loading attributes in Record %08X", n)
			# loads the referenced Record and updates with contained attributes
			rcrd = self.next(n)
			self._attributes.update(rcrd._attributes)
