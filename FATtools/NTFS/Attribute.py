# -*- coding: cp1252 -*-
import array, struct, os
from io import BytesIO
from .Commons import *
from .DatarunStream import *
import FATtools.utils as utils
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

__all__ = ['Attribute', 'Standard_Information', 'Attribute_List', 'Attribute_Listed', 'File_Name', 'Data',
'Index_Root', 'Index_Allocation', 'Bitmap', 'Volume_Name', 'Volume_Information', 'Security_Descriptor',
'attributes_by_id', 'attributes_by_name']


attributes_by_id ={
0x10: "$STANDARD_INFORMATION",
0x20: "$ATTRIBUTE_LIST",
0x30: "$FILE_NAME",
0x40: "$VOLUME_VERSION", # NT
0x50: "$SECURITY_DESCRIPTOR",
0x60: "$VOLUME_NAME",
0x70: "$VOLUME_INFORMATION",
0x80: "$DATA",
0x90: "$INDEX_ROOT",
0xA0: "$INDEX_ALLOCATION",
0xB0: "$BITMAP",
0xC0: "$REPARSE_POINT", # NT: $SYMBOLIC_LINK
0xD0: "$EA_INFORMATION",
0xE0: "$EA",
0xF0: "$PROPERTY_SET", # NT
0x100: "$LOGGED_UTILITY_STREAM" # 2K
}

attributes_by_name = {}
for id, name in attributes_by_id.items(): attributes_by_name[name] = id

class Attribute:
	layout = {
	0x00: ('dwType', '<I'),
	0x04: ('dwFullLength', '<I'),
	0x08: ('uchNonResFlag', 'B'),
	0x09: ('uchNameLength', 'B'),
	0x0A: ('wNameOffset', '<H'),
	0x0C: ('wFlags', '<H'),
	0x0E: ('wInstanceID', '<H') } # standard Attribute header: 0x10 (16) bytes
	
	layout_resident = { # additional layout (resident content)
	0x10: ('dwLength', '<I'),
	0x14: ('wAttrOffset', '<H'),
	0x16: ('uchFlags', 'B'),
	0x17: ('uchPadding', 'B') } # Size = 0x18 (24) bytes (total)

	layout_nonresident = {  # additional layout (non resident contents)
	0x10: ('u64StartVCN', '<Q'), # first Virtual Cluster Number of contents
	0x18: ('u64EndVCN', '<Q'), # last VCN of contents
	0x20: ('wDatarunOffset', '<H'),
	0x22: ('wCompressionSize', '<H'), 
	0x24: ('uchPadding', '4s'),
	0x28: ('u64AllocSize', '<Q'), # bytes occupied by allocated clusters
	0x30: ('u64RealSize', '<Q'), # bytes occupied by true contents
	0x38: ('u64StreamSize', '<Q') # always equal to u64RealSize?
	} # Size = 0x40 (64) bytes (total)
	
	def __init__(self, parent, offset):
		self._parent = parent # parent Record
		self._buf = parent._buf
		self._i = offset
		self._kv = Attribute.layout.copy()
		self._vk = {}
		for k, v in self._kv.items():
			self._vk[v[0]] = k
		if self.uchNonResFlag:
			upd = Attribute.layout_nonresident
		else:
			upd = Attribute.layout_resident
		self._kv.update(upd)
		for k, v in upd.items():
			self._vk[v[0]] = k

	__getattr__ = utils.common_getattr

	def __str__ (self): return utils.class2str(self, "Attribute @%x\n" % self._i)


class Standard_Information(Attribute):
	# Always resident
	specific_layout = {
	0x00: ('u64CTime', '<Q'),
	0x08: ('u64ATime', '<Q'),
	0x10: ('u64MTime', '<Q'),
	0x18: ('u64RTime', '<Q'), 
	0x20: ('dwDOSperm', '<I'),
	0x24: ('dwMaxVerNum', '<I'),
	0x28: ('dwVerNum', '<I'),
	0x2C: ('dwClassId', '<I'),
	0x30: ('dwOwnerId', '<I'),  # next 4 since NTFS 3.0 (Windows 2000)
	0x34: ('dwSecurityId', '<I'),
	0x38: ('u64QuotaCharged', '<Q'),
	0x40: ('u64USN', '<Q') } # 0x30 (48) - 0x48 (72) bytes

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)

	def __str__ (self):
		s = ''
		L1 = utils.class2str(self, "$STANDARD_INFORMATION @%x\n" % self._i).split('\n')
		L2 = []
		if self.dwLength == 0x30: # NTFS <3.0
			del L1[-5:-1]
		for key in (0x18, 0x20, 0x28, 0x30):
			o = self._kv[key][0]
			v = getattr(self, o)
			v = nt2uxtime(v)
			L2 += ['%x: %s = %s' % (key, o, v)]
		L1[12:16] = L2
		return '\n'.join(L1)


class Attribute_List(Attribute):
	# Always resident
	specific_layout = {}

	def __init__(self, parent, offset):
		self.list = ()
		self._i = offset
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)

	def __str__ (self):
		s = utils.class2str(self, "$ATTRIBUTE_LIST @%x\n" % self._i)
		for o in self.list:
			s = s + '\n' + str(o)
		return s + '\n'

class Attribute_Listed:
	layout = { # NTFS V3+
	0x00: ('dwListedAttrType', '<I'),
	0x04: ('wEntryLength', '<H'),
	0x06: ('ucbNameLen', 'B'),
	0x07: ('ucbNameOffs', 'B'), # Reserved
	0x08: ('u64StartVCN', '<Q'),
	0x10: ('u64BaseMFTFileRef', '<Q'), # Parent Record
	0x18: ('Reserved', '<H')
	} # 0x20 (32) bytes

	def __init__(self, parent, offset):
		self._parent = parent # parent Record
		self._buf = parent._buf
		self._i = offset
		self._kv = Attribute_Listed.layout.copy()
		self._vk = {}
		for k, v in self._kv.items():
			self._vk[v[0]] = k
		self.Name = ''
		if self.ucbNameLen:
			i = self._i+self.ucbNameOffs
			self.Name = (self._buf[i:i+self.ucbNameLen*2]).decode('utf-16le')

	__getattr__ = utils.common_getattr


	def __str__ (self): return utils.class2str(self, "$ATTRIBUTE_LIST Item @%x\n" % self._i)


class File_Name(Attribute):
	# Always resident
	specific_layout = {
	0x00: ('u64FileReference', '<Q'),
	0x08: ('u64CTime', '<Q'),
	0x10: ('u64ATime', '<Q'),
	0x18: ('u64MTime', '<Q'), 
	0x20: ('u64RTime', '<Q'),
	0x28: ('u64AllocatedSize', '<Q'),
	0x30: ('u64RealSize', '<Q'),
	0x38: ('dwFlags', '<I'),
	0x3C: ('dwEA', '<I'),
	0x40: ('ucbFileName', 'B'),
	0x41: ('uFileNameNamespace', 'B') } # 0x42 (66) bytes

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		# Name is at (24+66) bytes from start
		i = self._i+90
		self.FileName = (self._buf[i:i+self.ucbFileName*2]).decode('utf-16le')

	def __str__ (self):
		s = ''
		L1 = utils.class2str(self, "$FILE_NAME @%x\n" % self._i).split('\n')
		L2 = []
		for key in (0x20, 0x28, 0x30, 0x38):
			o = self._kv[key][0]
			v = getattr(self, o)
			v = nt2uxtime(v)
			L2 += ['%x: %s = %s' % (key, o, v)]
		L1[13:17] = L2
		return '\n'.join(L1) + '%x: FileName = %s\n' % (self._i+90, self.FileName)


class Data(Attribute):
	specific_layout = {}
	
	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		if self.uchNonResFlag:
			self.boot = parent.boot
			self.decode() # defer to effective stream access?
			self.file = DatarunStream(parent.boot, self.dataruns, self.u64RealSize)
		else:
			i = self._i + self.wAttrOffset
			self.file = BytesIO(self._buf[i: i+self.dwLength])
			self.file.size = self.dwLength
			if DEBUG&8: log("resident $DATA @%x", i)
		
	def __str__ (self): return utils.class2str(self, "$DATA @%x\n" % self._i)

	decode = common_dataruns_decode


class Index_Root(Attribute):
	specific_layout = {
	0x00: ('dwIndexedAttrType', '<I'),
	0x04: ('dwCollation', '<I'),
	0x08: ('dwAllocEntrySize', '<I'),
	0x0C: ('bClusPerIndexRec', 'B'), 
	0x0D: ('sPadding', '3s') } # 0x10 (16) bytes

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		if self.uchNonResFlag:
			self.boot = parent.boot
			self.decode()
			if DEBUG&8: log("non resident $INDEX_ROOT @%x", self._i)
			self.file = DatarunStream(self.boot, self.dataruns, self.u64RealSize)
		else:
			i = self._i + self.wAttrOffset
			self.file = BytesIO(self._buf[i: i+self.dwLength])
			self.file.size = self.dwLength
			if DEBUG&8: log("resident $INDEX_ROOT @%x", i)
			self.file.seek(0)

	def __str__ (self): return utils.class2str(self, "$INDEX_ROOT @%x\n" % self._i)

	decode = common_dataruns_decode


class Index_Allocation(Attribute):
	specific_layout = {}

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		if self.uchNonResFlag:
			self.boot = parent.boot
			self.decode()
			self.file = DatarunStream(self.boot, self.dataruns, self.u64RealSize)
		else:
			i = self._i + self.wAttrOffset
			self.file = BytesIO(self._buf[i: i+self.dwLength])
			self.file.size = self.dwLength
			if DEBUG&8: log("resident $INDEX_ALLOCATION @%x", i)

	def __str__ (self):
		return utils.class2str(self, "$INDEX_ALLOCATION @%x\n" % self._i)

	decode = common_dataruns_decode

""" A Bitmap is typically found:
- as $MFT Record Attribute (tracking used FILE records)
- as directory record attribute (tracking INDX blocks used in an $INDEX_ALLOCATION)
- as $Bitmap file contents (tracking free/used volume clusters)"""
class Bitmap(Attribute):
	specific_layout = {}

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		if self.uchNonResFlag:
			self.boot = parent.boot
			self.decode()
			self.file = DatarunStream(self.boot, self.dataruns, self.u64RealSize)
		else:
			i = self._i + self.wAttrOffset
			self.file = BytesIO(self._buf[i: i+self.dwLength])
			self.file.size = self.dwLength
			if DEBUG&8: log("resident $BITMAP @%x", i)

	def __str__ (self): return utils.class2str(self, "$BITMAP @%x\n" % self._i)

	decode = common_dataruns_decode

	def isset(self, bit):
		byte = bit//8
		bit = bit%8
		self.file.seek(byte)
		b = self.file.read(1)
		return ord(b) & (1 << bit) != 0

class Volume_Name(Attribute):
	# Always resident
	specific_layout = {}

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)
		i = self._i + self.wAttrOffset
		self.VolumeName = (self._buf[i:i+self.dwLength]).decode('utf-16le')

	def __str__ (self):
		L1 = utils.class2str(self, "$VOLUME_NAME @%x\n" % self._i).split('\n')
		return '\n'.join(L1) + '%x: VolumeName = %s\n' % (self.wAttrOffset, self.VolumeName)

class Volume_Information(Attribute):
	# Always resident
	specific_layout = {
	0x00: ('u64Reserved', '<Q'),
	0x08: ('bMajorVersion', 'B'), #  1.1/1.2=NT 3.5/4, 3.0=2K, 3.1=XP+
	0x09: ('bMinorVersion', 'B'),
	0x0A: ('wFlags', '<H'), # 1=dirty, 2=log resize, 4=to upgrade, 8=mounted on NT4, 10h=del USN, 20h=repair OIDS, 8000h=modified by CHKDSK
	0x0C: ('dwReserved', '<I') } # 0x10 (16) bytes

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)

	def __str__ (self): return utils.class2str(self, "$VOLUME_INFORMATION @%x\n" % self._i)

	def is_dirty(self):
		return self.wFlags & 1

class Security_Descriptor(Attribute):
	# At least since Windows NT 3.51
	specific_layout = {}

	def __init__(self, parent, offset):
		Attribute.__init__(self, parent, offset)
		common_update_and_swap(self)

	def __str__ (self): return utils.class2str(self, "$SECURITY_DESCRIPTOR @%x\n" % self._i)
