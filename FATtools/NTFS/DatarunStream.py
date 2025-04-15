# -*- coding: cp1252 -*-
import os
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from FATtools.debug import log

class DatarunStream:
	"Accesses a sequence of data runs as a continuous stream"
	def __init__ (self, boot, dataruns, size):
		self.boot = boot
		self._runs = dataruns # [(length, base_absolute_offset), (length, base_offset_delta), ...]
		self.curdatarun = 0 # datarun paired with current stream offset
		self.curdatarunpos = 0 # offset in current datarun
		self.size = size # virtual stream length
		self.seekpos = 0 # virtual stream offset
		self.IsValid = 1 # to make FATtools code happy
		self.seek(0)

	def close(self):
		pass

	def read(self, size=-1):
		buf = bytearray()
		if DEBUG&8: log("read() loop with size=%d", size)
		
		# reads all up to stream size
		if size < 0 or self.seekpos + size > self.size:
			size = self.size - self.seekpos
			if DEBUG&8: log("size adjusted to %d", size)
			
		while size > 0:
			self.seek(self.seekpos) # loads current data run
			if self.curdatarunpos + size <= self._runs[self.curdatarun]:
				if DEBUG&8: log("reading %d bytes streampos=@%d, datarunpos=%d", size, self.seekpos, self.curdatarunpos)
				buf += self.boot.stream.read(size)
				self.seekpos += size
				break
			else:
				readsize = self._runs[self.curdatarun] - self.curdatarunpos
				if not readsize:
					if DEBUG&8: log("readsize == 0 ending loop")
					break
				buf += self.boot.stream.read(readsize)
				self.seekpos += readsize
				size -= readsize
				if DEBUG&8: log("read truncated to %d bytes (%d byte last) @streampos=%d, datarunpos=%d", readsize, size, self.seekpos, self.curdatarunpos)
		return buf
		

	def tell(self):
		return self.seekpos
		
	def seek(self, offset, whence=0):
		if whence == 1:
			self.seekpos += offset
		elif whence == 2:
			self.seekpos = self.size - offset
		else:
			self.seekpos = offset
		i, todo = 0, self.seekpos
		for i in range(2, len(self._runs), 2):
			# if pos is >= first interval...
			self.curdatarun = i
			self.curdatarunpos = todo
			if todo >= self._runs[i]:
				todo -= self._runs[i] # next datarun
				continue
			else:
				break
		# Having found the datarun where the final position lays, we seek from its offset
		if DEBUG&8: log("seek @%x, datarun=%d, base=0x%x, offset=0x%x", self.seekpos, i-2, self._runs[i+1], todo)
		#~ print("seek @%x, datarun=%d, relpos=%x, absbase=%x"%(self.seekpos, i-2, todo, self._runs[i+1]))
		# BUG? Real disk stream has to seek with an Index, not the datarun stream?
		self.boot.stream.seek(self._runs[i+1] + todo)
