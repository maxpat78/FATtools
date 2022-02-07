# -*- coding: windows-1252 -*-
from random import *
import sys, os, hashlib, logging, optparse, hexdump

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

from FATtools import Volume
from FATtools.debug import log

class Stress(BaseException):
    pass

def GenObjName(obj='file'):
    if obj == 'file':
        model = 'File Name %%0%dd.txt' % randint(1,32)
    elif obj == 'dir':
        model = 'Directory %%0%dd' % randint(1,10)
    else:
        raise Stress("Bad object type specified! You must use 'dir' or 'file'.")
    GenObjName.Index += 1
    return model % GenObjName.Index
GenObjName.Index = 0

def GenRandTree():
    tree = []
    for i in range(randint(1,8)):
        L = []
        for i in range(randint(2,8)):
            L += [GenObjName('dir')]
        tree += ['\\'.join(L)]
    return tree

def GetRandSubpath(tree):
    p = choice(tree)
    L = p.split('\\')
    q = choice(L)
    return '\\'.join(L[:L.index(q)]) or L[0]


class RandFile(object):
    Buffer = None
    
    def __init__(self, root, path, name, maxsize, hash=0):
        if not RandFile.Buffer:
            RandFile.Buffer = bytearray(128<<10)
            j=0
            for i in range(256):
                RandFile.Buffer[j:j+512] = 512*i.to_bytes(1,'little')
                j+=512
            RandFile.Buffer*=64 # 8M buf
        self.root = root
        self.path = path
        self.name = name
        # Random file size
        self.size = randint(1, maxsize)
        # Virtual dir file belongs to
        self.dirtable = None
        # Virtual handle to file
        self.fp = None

        if hash:
            self.sha1 = hashlib.sha1(RandFile.Buffer[:self.size]).hexdigest()
        
        # Random segments to write
        ops = randint(1,32)
        maxc = self.size//ops
        self.indexes = [0]
        for i in range(ops):
            # Indexes MUST be unique!
            j=0
            while j in self.indexes:
                j = randint(self.indexes[-1], self.indexes[-1]+maxc)
            self.indexes += [j]
        
        self.i = 0 # next segment to write
        self.IsWritten = 0
    
    def create(self):
        self.dirtable = self.root.opendir(self.path)
        self.fp = self.dirtable.create(self.name)
        
    def write(self):
        if self.IsWritten: return 0
        try:
            j = self.indexes[self.indexes.index(self.i)+1]
            self.fp.write(RandFile.Buffer[self.i:j])
            self.i = j
        except IndexError:
            self.fp.write(RandFile.Buffer[self.i:self.size])
            self.IsWritten = 1
        return 1



def stress(opts, args):
    "Randomly populates and erases a tree of random files and directories (for test purposes)"
    root = Volume.vopen(args[0], 'r+b') # auto-opens first useful filesystem
    
    dirs_made, files_created, files_erased = 0,0,0
    
    tree = GenRandTree()
    for pattern in tree:
        obj = root
        for subdir in pattern.split('\\'):
            if len(os.path.join(obj.path,subdir))+2 > 260: continue # prevents invalid pathnames
            obj = obj.mkdir(subdir)
            dirs_made+=1

    print("Random tree of %d directories generated" % dirs_made)

    free_bytes = root.getdiskspace()[1]
    threshold =free_bytes * (1.0-opts.threshold/100)
    files_set = []
    
    print("%d bytes free, threshold=%.02f" % (free_bytes, threshold))

    def rand_populate(root, files_set, tree, free_bytes):
        while 1:
            fpath = GetRandSubpath(tree)
            fname = GenObjName()
            if len(os.path.join(fpath,fname))+2 > 260: continue # prevents invalid pathnames
            o = RandFile(root, fpath, fname, min(opts.file_size, free_bytes), opts.sha1)
            if (free_bytes-o.size) < threshold: break
            files_set += [o]
            free_bytes -= o.size
        return free_bytes

    def rand_erase(files_set):
        n = randint(1, len(files_set)//2)
        cb = 0
        for i in range(n):
            f = choice(files_set)
            f.fp.close()
            f.dirtable.erase(f.name)
            cb += f.size
            del files_set[files_set.index(f)]
        return n, cb

    def rand_truncate(files_set):
        n = randint(1, len(files_set)//2)
        for i in range(n):
            f = choice(files_set)
            j = randint(f.fp.File.filesize//6, f.fp.File.filesize//2)
            if DEBUG&1: log("truncating %s from %d to %d",f.name,f.fp.File.filesize,j)
            f.fp.ftruncate(j, 1)
            if hasattr(f, 'sha1'):
                f.sha1 = hashlib.sha1(RandFile.Buffer[:j]).hexdigest()
        return n

    def rand_rewrite(files_set):
        cb = 0
        n = randint(1, len(files_set)//2)
        for i in range(n):
            f = choice(files_set)
            for j in range(randint(1, 16)):
                pos = randint(0, f.fp.File.filesize//2)
                q = randint(1, f.fp.File.filesize//4)
                if DEBUG&1: log("vcn=%d vco=%d lastvlcn=%s", f.fp.File.vcn, f.fp.File.vco, f.fp.File.lastvlcn)
                f.fp.seek(pos)
                if DEBUG&1: log("vcn=%d vco=%d lastvlcn=%s seek(%d)", f.fp.File.vcn, f.fp.File.vco, f.fp.File.lastvlcn,pos)
                s = f.fp.read(q)
                if DEBUG&1: log("vcn=%d vco=%d lastvlcn=%s read(%d)", f.fp.File.vcn, f.fp.File.vco, f.fp.File.lastvlcn, q)
                if s != RandFile.Buffer[pos:pos+q]:
                    if DEBUG&1: log("*** PROBLEM: bytes read in from %s differ from source buffer!",os.path.join(f.fp.Dir.path, f.fp.Entry.Name()))
                    if DEBUG&1: log("pos=%d, q=%d, len(s)=%d",pos, q, len(s))
                    if DEBUG&1: log(f.fp.File.runs)
                else:
                    if DEBUG&1: log("Data read ok for %s", os.path.join(f.fp.Dir.path, f.fp.Entry.Name()))
                f.fp.seek(pos)
                f.fp.write(s)
                cb+=q
        return n, cb


    cb = rand_populate(root, files_set, tree, free_bytes)
    print("Generated %d random files for %d bytes" % (len(files_set), free_bytes-cb))

    print("Creating their handles...")
    list(map(lambda x: x.create(), files_set))

    print("Randomly writing their contents...")
    while 1:
        L = [x.write() for x in files_set]
        if 1 not in L: break

    if opts.programs & 2:
        if DEBUG&1: log("----- Randomly erasing some files...")
        print("Randomly erasing some files...")
        n, cb = rand_erase(files_set)
        print("Erased %d bytes in %d files" % (cb, n))

    if opts.programs & 4:
        if DEBUG&1: log("----- Randomly truncating some files...")
        print("Randomly truncating some files...")
        n = rand_truncate(files_set)
        print("Truncated %d files" % (n))

    
    if opts.programs & 12  or opts.programs & 10:
        if DEBUG&1: log("----- Randomly filling free space with other files...")
        free_bytes = root.getdiskspace()[1]
        fset = []
        cb = rand_populate(root, fset, tree, free_bytes)
        files_set += fset
        print("Generated other %d random files for %d bytes" % (len(fset), free_bytes-cb))
        if DEBUG&1: log("Generated other %d random files for %d bytes", len(fset), free_bytes-cb)

        print("Creating their handles...")
        list(map(lambda x: x.create(), fset))

        print("Randomly writing their contents...")
        while 1:
            L = [x.write() for x in fset]
            if 1 not in L: break
    
    if opts.programs & 16:
        if DEBUG&1: log("----- Randomly re-writing some files...")
        n, cb = rand_rewrite(files_set)
        print("Rewritten %d bytes in %d files" % (cb,n))
        if DEBUG&1: log("Rewritten %d bytes in %d files",cb,n)

    if opts.programs & 32:
        if DEBUG&1: log("----- Cleaning & shrinking directory tables...")
        root.flush()
        #~ for o in files_set:
            #~ o.fp.close() # prevents altering the dirtable after the cleaning
        #~ if DEBUG&1: log("----- Closed all open handles")
        visited = set()
        for o in files_set:
            if o.path in visited: continue
            if DEBUG&1: log("----- Cleaning %s", o.path)
            visited.add(o.path)
            o.dirtable.clean(1)
        if DEBUG&1: log("----- End cleaning directory tables")
        print("Cleaned and shrinked all directory tables.")

    print("Done.")

    if opts.sha1:
        # Saves SHA-1 for all files, even erased ones!
        fp = root.create('hashes.sha1')
        cb=0
        for h in files_set:
            a = os.path.join(h.path, h.name)
            # D:\some 256-character path string<NUL>
            if len(a)+4 > 260:
                cb+=1
            #~ fp.write(bytearray('%s *%s\n' % (h.sha1.encode('ascii'), a.encode('ascii'))))
            fp.write(bytearray(b'%s *%s\n' % (h.sha1.encode(), a.encode())))
        print("Saved SHA-1 hashes (using %d clusters more) for %d generated files" % (fp.File.size//root.boot.cluster, len(files_set)))
        fp.close()
        if cb:
            print("WARNING: %d files have pathnames >260 chars!" % cb)
            if DEBUG&1: log("WARNING: %d files have pathnames >260 chars!", cb)
    if opts.sha1chk:
        print("Checking SHA-1 hash list...")
        cb=0
        for o in files_set:
            a = os.path.join(o.path, o.name)
            if DEBUG&7: log("%s %8d %s", o.sha1, o.size, a) # log file hash, size and pathname for debug purposes
            #~ if len(a)+4 > 260: continue
            o.fp.seek(0)
            s = o.fp.read()
            if o.sha1 != hashlib.sha1(s).hexdigest():
                print("SHA1 differ for", a)
                cb+=1
                if DEBUG&1: log("PROBLEM: wrong SHA-1 on %s (re-read %d bytes)",a,len(s))
                #~ open('BAD_'+a.replace('\\','_'),'wb').write(s)
        if cb:
            print("WARNING: %d files report wrong SHA-1!" % cb)
    
    root.flush()

            



if __name__ == '__main__':
    help_s = """
    %prog [options] <drive>
    """
    par = optparse.OptionParser(usage=help_s, version="%prog 1.0", description="Stress a FAT/exFAT file system randomly creating, filling and erasing files.")
    par.add_option("-t", "--threshold", dest="threshold", help="limit the stress test to a given percent of the free space. Default: 99%", metavar="PERCENT", default=99, type="float")
    par.add_option("-s", "--filesize", dest="file_size", help="set the maximum size of a random generated file. Default: 1M", metavar="FILESIZE", default=1<<20, type="int")
    par.add_option("-p", "--programs", dest="programs", help="selects tests to run (bit mask). See inside the script itself. Default: 63", metavar="PROGRAMS", default=63, type="int")
    par.add_option("--debug", dest="debug", help="turn on debug logging to stress.log for specified modules (may be VERY slow!). Default: 0. Use 1 (disk), 2 (Volume), 4 (FAT), 8 (exFAT).", metavar="DEBUG_LOG", default=0, type="int")
    par.add_option("--sha1", action="store_true", dest="sha1", help="turn on generating an hash list of generated files. Default: OFF", metavar="HASH_LOG", default=False)
    par.add_option("--sha1chk", action="store_true", dest="sha1chk", help="turn on checking generated hash list. Default: OFF", metavar="HASH_LOG_CHK", default=False)
    par.add_option("--fix", dest="fix", help="use a the specified random seed, making the test repeatable. Default: NO", metavar="FIX_RAND", default=0, type="int")
    par.add_option("--fixdriven", action="store_true", dest="fixdriven", help="use an incremental random seed, starting from, and updating, those stored in file seed.txt. Default: OFF", metavar="FIX_RAND_DRIVEN", default=False)
    opts, args = par.parse_args()

    if not args:
        print("You must specify a drive to test!\n")
        par.print_help()
        sys.exit(1)

    if opts.debug:
        logging.basicConfig(level=logging.DEBUG, filename='stress.log', filemode='w')
        Volume.DEBUG = opts.debug
        Volume.FAT.DEBUG = opts.debug
        Volume.FAT.hexdump = hexdump
        Volume.exFAT.DEBUG = opts.debug
        Volume.exFAT.hexdump = hexdump
        Volume.disk.DEBUG = opts.debug

    if opts.fix:
        print("Seeding the pseudo-random generator with %d"%opts.fix)
        if DEBUG&1: log("Seeding the pseudo-random generator with %d", opts.fix)
        seed(opts.fix) # so it repeates the same "random" sequences at every call

    if opts.fixdriven:
        n = int(open('seed.txt').read())
        print("Seeding the pseudo-random generator with %d"%n)
        if DEBUG&1: log("Seeding the pseudo-random generator with %d", n)
        seed(n)
        open('seed.txt','w').write(str(n+1))

    stress(opts, args)
