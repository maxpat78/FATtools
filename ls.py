# -*- coding: utf-8 -*-

import sys, os, argparse, fnmatch, logging
from datetime import datetime
from FATtools import vhdutils, vdiutils, vmdkutils
from FATtools import Volume

DEBUG = 0
from FATtools.debug import log

#~ logging.basicConfig(level=logging.DEBUG, filename='ls.log', filemode='w')



def ls(args, opts=0):
    "Simple, DOS style directory listing, with size and last modification time"
    for arg in args:
        filt = None # wildcard filter
        img = is_vdisk(arg) # image to open
        path = arg[len(img)+1:] # eventual path inside it
        v = Volume.vopen(img)
        # wildcard? expand src list with matching items
        if '*' in path or '?' in path:
            # wildcard MUST be in the normalized path last component 
            path = os.path.normpath(path)
            L = path.split(os.sep)
            filt = L.pop() # assumes jolly here
            path = os.sep.join(L) # rebuilds path part
        if path:
            v = v.opendir(path)
        if not v:
            print('Invalid path: "%s"'%arg)
            continue
        if not opts&1: print("\n Directory of %s\n"%v.path)
        tot_files = 0
        tot_bytes = 0
        tot_dirs = 0
        for it in v.iterator():
            isexfat = not hasattr(it,'IsLfn')
            if isexfat:
                if it.type != 5: continue
            else:
                if it.IsLabel(): continue
            if filt:
                if not fnmatch.fnmatch(it.Name(), filt):
                    continue
            if opts&1:
                print(it.Name())
            else:
                if isexfat:
                    tot_bytes += it.u64DataLength
                else:
                    tot_bytes += it.dwFileSize
                if it.IsDir(): tot_dirs += 1
                else: tot_files += 1
                if isexfat:
                    mtime = datetime(*(it.DatetimeParse(it.dwMTime))).isoformat()[:-3].replace('T','  ')
                    size = it.u64DataLength
                else:
                    mtime = datetime(*(it.ParseDosDate(it.wMDate) + it.ParseDosTime(it.wMTime))).isoformat()[:-3].replace('T','  ')
                    size = it.dwFileSize
                if size >= 10**10:
                    sizes = {0:'B', 10:'K',20:'M',30:'G',40:'T',50:'E'}
                    k = 0
                    for k in sorted(sizes):
                        if (size // (1<<k)) < 10**6: break
                    size = '%.02f%s' % (size/(1<<k), sizes[k])
                else:
                    size = str(size)
                print("%s  %10s  %s" % (mtime, (size,'<DIR>   ')[it.IsDir()], it.Name()))
        if not opts&1:
            print("%18s Files    %s bytes" % (tot_files, tot_bytes))
            print("%18s Directories %12s bytes free" % (tot_dirs, v.getdiskspace()[1]))


def is_vdisk(s):
    "Returns the base virtual disk image path if it contains a known extension or an empty string"
    image_path=''
    for ext in ('vhd', 'vdi', 'vmdk', 'img', 'dsk', 'raw', 'bin'):
        if '.'+ext in s.lower():
            i = s.lower().find(ext)
            image_path = s[:i+len(ext)]
            break
    return image_path



if __name__ == '__main__':
    help_s = """
    ls.py image.<vhd|vdi|vmdk|img|bin|raw|dsk>[/path] ...
    """
    par = argparse.ArgumentParser(usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Lists files and directories in a supported virtual disk image.\nWildcards accepted.",
    epilog="Examples:\nls.py image.vhd\nls.py image.vhd/*.exe image.vhd/python39/dlls/*.pyd\n")
    par.add_argument('items', nargs='*')
    args = par.parse_args()

    if len(args.items) < 1:
        print("ls error: you must specify at least one path to list!")
        par.print_help()
        sys.exit(1)

    ls(args.items)
