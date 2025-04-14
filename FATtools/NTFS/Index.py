# -*- coding: cp1252 -*-
import os
from fnmatch import fnmatch
from .Commons import *
import FATtools.utils as utils
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

class IndexGroup:
	def __init__ (self, index_root, index_allocation):
		self.index_root = index_root
		self.index_allocation = index_allocation
		
	def __str__ (self): return "IndexGroup of %s and %s" % (self.index_root, self.index_allocation)

	__getattr__ = utils.common_getattr
	
	def iterator(self):
		if self.index_root:
			for o in self.index_root.iterator():
				if DEBUG&8: log("Internal entry", o.FileName)
				yield o
		if self.index_allocation and self.index_allocation.valid:
			for o in self.index_allocation.iterator():
				if DEBUG&8: log("External entry", o.FileName)
				yield o

	def find(self, name):
		for e in self.iterator():
			if fnmatch(e.FileName, name):
				return e
		return None

class Index:
	def __init__ (self, indxstream, bitmap, block_size, resident=0):
		#~ print('Index.init called',indxstream, bitmap, block_size, resident)
		self.valid = False
		self.block_size = block_size
		self._stream = indxstream
		self._bitmap = bitmap # in $INDEX_ALLOCATION (bit set if INDX in use)
		self._pos = self._stream.tell()
		self._resident = resident
		self._buf = self._stream.read(self.block_size)
		if resident:
			#~ print('DBG: initing resident INDX')
			self._indxh = Index_Header(self._buf)
			if self._indxh.dwEntriesOffset != 0x10: # hack: why a first invalid entry was found in a case?
				if DEBUG&8: log("WARNING: first INDEX_HEADER not valid:\n%s", self._indxh)
				#~ print("WARNING: first INDEX_HEADER not valid:\n%s"%self._indxh)
				self._buf = self._buf[16:]
				self._indxh = Index_Header(self._buf)
				#~ print("New INDEX_HEADER:", self._indxh)
		else:
			#~ print('DBG: initing non resident INDX')
			# Sometimes an $INDEX_ALLOCATION has empty stream: full dir contents deletion?
			if not self._buf:
				if DEBUG&8: log("Empty INDX block @%X/%X", self._pos, self._stream.size)
				#~ print('Empty INDX block', self._pos, self._stream.size, self._stream)
				return
			block = Index_Block(self._buf)
			if not block:
				raise NTFSException('INDX initialization failed')
			if DEBUG&8: log("decoded INDEX_BLOCK:\n%s", block)
			#~ print("decoded INDEX_BLOCK:\n%s"% block)
			self._indxh = Index_Header(self._buf, 24)
		if DEBUG&8: log("decoded INDEX_HEADER:\n%s", self._indxh)
		#~ print("decoded INDEX_HEADER:\n%s"% self._indxh)
		self.valid = True

	def __str__ (self): return "Index @%x\n%s" % (self._pos, self._indxh)

	__getattr__ = utils.common_getattr

	def find(self, name):
		for e in self.iterator():
			if fnmatch(e.FileName, name):
				#~ print('matching entry:', e)
				return e
		return None

	def iterator(self):
		"Iterates through index entries"
		while 1:
			i = self._indxh.dwEntriesOffset
			while i < self._indxh.dwIndexLength:
				e = Index_Entry(self._buf, i+self._indxh._i)
				if e:
					# An entry with wFlags & 2, or unnamed, signals the INDX block end
					if e.FileName: yield e
					if e.wFlags & 0x2:
						if DEBUG&8: log("last entry in current INDX block")
						#~ print("last entry in current INDX block")
				else:
					if DEBUG & 8: log("no INDX entry")
					#~ print("DBG: no INDX entry")
				i += e.wsizeOfIndexEntry
			if self._resident:
				#~ print("DBG: end of resident INDX")
				break
			if not self._resident:
				# Stops at end of Index stream
				if self._stream.tell() >= self._stream.size:
					#~ print("DBG: end of non resident INDX stream")
					break
				# Searches for the next used INDX
				while not self._bitmap.isset(self._stream.tell() // self.block_size):
					self._stream.seek(self.block_size, 1)
					if self._stream.tell() >= self._stream.size:
						return
				if self._stream.tell() >= self._stream.size:
					#~ print("DBG: end of non resident INDX stream")
					break
				# Loads the next INDX block and continues iteration
				#~ print('DBG: tell', self._stream.tell())
				#~ print('DBG: loading next INDX @', self._stream.tell(), self._stream.size)
				self.__init__(self._stream, self._bitmap, self.block_size, 0)

class Index_Block:
	layout = {
	0x00: ('sMagic', '4s'),
	0x04: ('wUSAOffset', '<H'), # Update Sequence Array offset
	0x06: ('wUSASize', '<H'), # Array size (in sectors)
	0x08: ('u64LSN', '<Q'),
	0x10: ('u64IndexVCN', '<Q') } # 0x18 (24) bytes

	def __init__ (self, indx):
		self._i = 0
		self._buf = indx
		self._kv = Index_Block.layout.copy()
		self._vk = {} # { nome: offset}
		for k, v in self._kv.items():
			self._vk[v[0]] = k
		# Unused blocks can be zeroed (=no INDX marker)
		if self.sMagic != b'INDX':
			raise BadIndex
		self.fixup()

	__getattr__ = utils.common_getattr
	fixup = common_fixup
		
	def __str__ (self): return utils.class2str(self, "Index Block @%x\n" % self._i)

class Index_Header:
	layout = {
	0x00: ('dwEntriesOffset', '<I'), 
	0x04: ('dwIndexLength', '<I'),
	0x08: ('dwAllocatedSize', '<I'),
	0x0C: ('bIsLeafNode', 'B'),
	0x0D: ('sPadding', '3s') } # Size = 0x10 (16 byte)
	
	def __init__ (self, indx, offset=0):
		self._buf = indx
		self._i = offset
		self._kv = Index_Header.layout.copy()
		self._vk = {} # { nome: offset}
		for k, v in self._kv.items():
			self._vk[v[0]] = k

	__getattr__ = utils.common_getattr
		
	def __str__ (self): return utils.class2str(self, "Index Header @%x\n" % self._i)

class Index_Entry:
	layout = {
	0x00: ('u64mftReference', '<Q'),
	0x08: ('wsizeOfIndexEntry', '<H'),
	0x0A: ('wfilenameOffset', '<H'),
	0x0C: ('wFlags', '<H'),
	0x0E: ('sPadding', '2s'),
	0x10: ('u64mftFileReferenceOfParent', '<Q'),
	0x18: ('u64creationTime', '<Q'),
	0x20: ('u64lastModified', '<Q'),
	0x28: ('u64lastModifiedForFileRecord', '<Q'),
	0x30: ('u64lastAccessTime', '<Q'),
	0x38: ('u64allocatedSizeOfFile', '<Q'),
	0x40: ('u64realFileSize', '<Q'),
	0x48: ('u64fileFlags', '<Q'), # 1,2,4,20h ->  DOS RHSA; 0x10000000 Directory
	0x50: ('ucbFileName', 'B'),
	0x51: ('chfilenameNamespace', 'B') # 0=POSIX, 1=Win32, 2=DOS, 3=Win32&DOS
	} # Size = 0x52 (82 byte)
	
	def __init__ (self, buffer, index):
		self._kv = Index_Entry.layout.copy()
		self._vk = {} # { nome: offset}
		for k, v in self._kv.items():
			self._vk[v[0]] = k
		self._buf = buffer
		self._i = index
		self.FileName = ''
		if self.wfilenameOffset:
			j = index + 82
			self.FileName = (self._buf[j: j+self.ucbFileName*2]).decode('utf-16le')
		if DEBUG & 8: log('Decoded INDEX_ENTRY @%x\n%s', index, self)

	__getattr__ = utils.common_getattr
		
	def __str__ (self):
		s = ''
		L1 = utils.class2str(self, "Index Entry @%x\n" % self._i).split('\n')
		L2 = []
		for key in (0x18, 0x20, 0x28, 0x30):
			o = self._kv[key][0]
			v = getattr(self, o)
			v = nt2uxtime(v)
			L2 += ['%x: %s = %s' % (key, o, v)]
		L1[7:11] = L2
		return '\n'.join(L1) + '52: FileName = "%s"\n' % self.FileName
