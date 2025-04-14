# -*- coding: cp1252 -*-
import fnmatch, os, sys, struct, time
#~ from FATtools.FAT import Direntry
from .Boot import *
from .Index import *
from .Record import *
from .Commons import *
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))


class _Empty: pass

class NTFSHandle(object):
	"Manages an open file NTFS record"
	def __init__ (self, rcrd):
		self.IsValid = 0
		self.File = None # file contents
		data = rcrd.find_attribute("$DATA")
		if data:
			self.File = data[-1].file
			self.IsValid = 1
		self.Dir = None
		self.IsReadOnly = True # use this to prevent updating a Direntry on a read-only filesystem
		ie = _Empty()
		fn = rcrd.find_attribute("$FILE_NAME")[0]
		ie.u64lastModified = fn.u64MTime
		ie.u64fileFlags = fn.dwFlags
		ie.u64realFileSize = fn.u64RealSize
		self.Entry = NTFSDirentry(ie) # (ex)FAT-like Direntry slot

	def close(self):
		pass

	def read(self, size=-1):
		return self.File.read(size)

	def write(self, s):
		raise NTFSException('Writing NTFS volume is not implemented (yet)')
		
	def tell(self):
		return self.File.tell()

	def seek(self, offset, whence=0):
		return self.File.seek(offset, whence)

class NTFSDirentry:
	"Dummy, FATDirentry like class"
	# Please note: a NTFS entry can represent devices, sparse and temp files, reparse points!
	# (ex)FAT has no knowledge of those!
	def __init__ (p, ixe):
		p.entry = ixe
		p.dwFileSize = ixe.u64realFileSize
		p.wMDate = ixe.u64lastModified
		p.wMTime = ixe.u64lastModified
	def ShortName(p):
		return p.entry.FileName
	def LongName(p):
		return p.entry.FileName
	def Name(p):
		return p.entry.FileName
	def IsLabel(p):
		return False
	def IsDir(p):
		return p.entry.u64fileFlags & 0x10000000 > 0
	def ParseDosDate(p, d):
		ux = nt2uxtime(d)
		return (ux.year, ux.month, ux.day)
	def ParseDosTime(p, t):
		ux = nt2uxtime(t)
		return (ux.hour, ux.minute, ux.second)


class Dirtable:
	"(ex)FAT Dirtable-like class, to integrate with existing FATtools code"
	def __init__ (p, mft, path='.'):
		p.mft = mft
		p.path = path
		p.rcrd = ntfs_open_file(mft, path) # MFT Record
		p.index = ntfs_open_dir(p.rcrd) # Index
		if not p.index:
			raise NTFSException('Could not open directory "%s"!'%path)
		p.fat = _Empty() # dummy
		p.fat.exfat = None

	def __str__(p):
		return 'Directory of "%s"' % (p.path)

	def listdir(p):
		"Returns a list of file and directory names in this directory, sorted by on disk position"
		assert 0

	def opendir(p, name):
		"Opens an existing relative directory path beginning in this table and return a new Dirtable object or None if not found"
		npath = os.path.join(p.path, name)
		rcrd = ntfs_open_file(p.rcrd, npath)
		if not rcrd:
			raise NTFSException('Could not open directory "%s"!'%npath)
		if not rcrd.find_attribute('$FILE_NAME')[0].dwFlags & 0x10000000 > 0:
			raise NTFSException('"%s" is not a directory!'%npath)
		return Dirtable(rcrd, npath)

	def open(p, name):
		"Opens a file name existing in this directory"
		npath = os.path.join(p.path, name)
		rcrd = ntfs_open_file(p.rcrd, npath)
		if not rcrd:
			raise NTFSException('Could not open "%s"!'%npath)
		if rcrd.find_attribute('$FILE_NAME')[0].dwFlags & 0x10000000 > 0:
			raise NTFSException('"%s" is not a file!'%npath)
		return NTFSHandle(rcrd)

	def find(self, name):
		"Finds an entry by name. Returns it or None if not found"
		assert 0

	def walk(p):
		"Walks across this directory and its childs. For each visited directory, returns a tuple (root, dirs, files)"
		dirs = []
		files = []
		for e in p.iterator():
			if e.IsDir():
				dirs.append(e.Name())
			else:
				files.append(e.Name())
		yield p.path, dirs, files
		for dirn in dirs:
			dt = Dirtable(p.mft, os.path.join(p.path, dirn))
			yield from dt.walk()

	def iterator(p):
		"Iterates through directory table slots, generating a NTFSDirentry for each one"
		L=[]
		ref={}
		for e in p.index.iterator():
			if e.FileName in NTFSReservedNames: continue
			prev_e = ref.get(e.u64mftReference)
			if not prev_e:
				L.append(e)
				ref[e.u64mftReference] = e
			else:
				if e.chfilenameNamespace == 1: # DOS Namespace
					i = L.index(prev_e)
					if DEBUG&8: log(f'using "{e.FileName}" instead of short DOS name "{prev_e.FileName}"')
					L[i] = e
					ref[e.u64mftReference] = e
		for e in L:
			yield NTFSDirentry(e)

	def getdiskspace(p):
		"Returns the disk free space in a tuple (clusters, bytes)"
		#~ return (0, 0)
		freec = ntfs_get_free_clusters(p.mft)
		return (freec, freec*p.mft.boot.cbCluster)



#
# NTFS volume low level routines
#

def ntfs_emu_dirtable(stream):
	"Returns a root (ex)FAT Dirtable-like object, to play with a NTFS volume"
	mft = ntfs_open_volume(stream)
	return Dirtable(mft)

def ntfs_open_volume(stream):
	"Opens an NTFS volume returning its MFT Record object"
	stream.seek(0)
	boot = Bootsector(stream)
	assert boot.chOemID == b'NTFS    '
	assert boot.wSecMark == 0xaa55
	stream.seek(boot.LcnMFT)
	mft = Record(boot, stream)
	if mft.find_attribute("$FILE_NAME")[0].FileName == '$MFT':
		mft = Record(boot, mft.find_attribute(0x80)[0].file)
	else:
		raise NTFSException("The NTFS Master File Table $MFT was not found!")
	return mft

def ntfs_get_free_clusters(mft):
	# Precalcola il numero di zero per ogni byte
	bit_zero_table = [8 - bin(i).count('1') for i in range(256)]
	bmp = ntfs_open_record(mft, "$Bitmap") # NTFS volume bitmap
	f = bmp.find_attribute("$DATA")[0].file # CAVE! 2 $DATA possible sometimes!
	totc = mft.boot.u64TotalSectors // mft.boot.uchSecPerClust # volume clusters
	freec = 0
	excb = 8 - totc%8 # excedent bits
	while totc > 0:
		cb = min(totc//8 or 1, 1<<20) # process min 1b, max 1MB bitmap
		totc -= cb*8
		buf = f.read(cb)
		zeros = sum(bit_zero_table[b] for b in buf)
		freec += zeros
		if totc < 0:
			# excedent bits are 1 already?
			pass
	return freec

def ntfs_open_dir(rcrd):
	"Opens a directory record returning its associated Index"
	# Entries can be both resident and not resident
	ir = rcrd.find_attribute("$INDEX_ROOT")[0]
	ixr = Index(ir.file, None, rcrd.boot.cbIndx, 1)
	ia = rcrd.find_attribute("$INDEX_ALLOCATION")
	# An allocation, if present, extends the root index
	if ia:
		ia = ia[0]
		bm = rcrd.find_attribute("$BITMAP")[0]
		ixa = Index(ia.file, bm, rcrd.boot.cbIndx, 0)
		return IndexGroup(ixr, ixa)
	return ixr

def ntfs_open_root(rcrd):
	"Opens the root directory Index"
	# Hack for NTFS v.1.2, whose Record #5 has no $FILE_NAME
	return ntfs_open_dir(rcrd.next(5))

def ntfs_open_record(mftrecord, record):
	"Opens a MFT Record by name or number, directly searching the MFT"
	if type(record) == int:
		mftrecord._stream.seek(record*mftrecord.boot.cbRecord)
		return Record(mftrecord.boot, mftrecord._stream)
	elif type(record) == str:
		mftrecord._stream.seek(0)
		r = Record(mftrecord.boot, mftrecord._stream)
		while 1:
			if fnmatch(ntfs_get_filename(r), record):
				return r
			try:
				r = r.next()
			except EndOfStream:
				break
	return None

def ntfs_get_filename(mftrecord):
	"Finds the (longest) name associated with a MFT record"
	names = mftrecord.find_attribute("$FILE_NAME")
	n = 0
	wanted = ''
	if not names: return wanted
	for name in names:
		if len(name.FileName) > n:
			n = len(name.FileName)
			wanted = name.FileName
	return wanted
	
def ntfs_copy_file(mftrecord, outfile=None):
	"Copies a MFT record contents (=file) to the current folder or the given path or file stream"
	selected = mftrecord.find_attribute("$DATA")[-1].file
	if not outfile:
		outfile = ntfs_get_filename(mftrecord)
	if type(outfile) != str:
		outstream = outfile
	else:
		outstream = open(outfile,'wb')
	while 1:
		s = selected.read(4096*1024)
		if not s:
			break
		outstream.write(s)
	outstream.close()

def ntfs_open_file(mftrecord, abspathname):
	"Opens a file given its absolute path, going through Index(es). Returns its Record"
	#~ print('DBG: ntfs_open_file', abspathname)
	tail = ' '
	head = abspathname
	path = []
	while tail != '':
		head, tail = os.path.split(head)
		path += [tail]
	if len(path) < 2:
		raise NTFSException('Invalid absolute pathname specified "%s"' % abspathname)
	path.reverse()
	#~ print('Searching for', path)
	path = path[2:]
	if not path: # means root
		return mftrecord.next(5)
	entry = ntfs_open_root(mftrecord)
	i=0
	for e in path:
		#~ print('loop:', e)
		i+=1
		entry = entry.find(e)
		if not entry: return None
		if i == len(path): break
		# opens the intermediate directory record
		entry = ntfs_open_dir(mftrecord.next(entry.u64mftReference & 0x0000FFFFFFFFFFFF))
	#~ print('hit', entry)
	rec = mftrecord.next(entry.u64mftReference & 0x0000FFFFFFFFFFFF)
	#~ print('DBG: ntfs_open_file returning', ntfs_get_filename(rec))
	return rec

#~ def ntfs_split_path(path):
	#~ path = path.replace('\\','/')
	#~ L = path.split('/')
	#~ if L:
		#~ if L[0] == '': L[0] = '.'
		#~ if L[0] != '.': L.insert(0, '.')
	#~ L.insert(0, '')
	#~ print('ntfs_split_path returns', L)
	#~ return L
