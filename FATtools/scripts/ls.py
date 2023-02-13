# -*- coding: utf-8 -*-
import sys, os, argparse, fnmatch, locale, logging
from datetime import datetime
from operator import itemgetter

from FATtools import Volume
from FATtools.utils import is_vdisk

DEBUG = 0
from FATtools.debug import log

#~ logging.basicConfig(level=logging.DEBUG, filename='ls.log', filemode='w')


def _ls(v, filt, opts, depth=0):
    "Scans an opened DirHandle"
    def _fmt_size(size):
        "Internal function to format sizes"
        if size >= 10**12:
            sizes = {0:'B', 10:'K',20:'M',30:'G',40:'T',50:'E'}
            k = 0
            for k in sorted(sizes):
                if (size // (1<<k)) < 10**6: break
            size = locale.format_string('%.02f%s', (size/(1<<k), sizes[k]), grouping=1)
        else:
            size = locale.format_string('%d', size, grouping=1)
        return size
    def _prn_line(name, mtime, size):
        "Internal function to print a line of output"
        print("%s  %16s  %s" % (mtime.isoformat()[:-3].replace('T','  '), size, name))

    isexfat = 'exFAT' in str(type(v))

    if not opts.bare: print("\n Directory of %s\n"%v.path)
    tot_files = 0
    tot_bytes = 0
    tot_dirs = 0
    table = [] # used to sort
    dirs = [] # directories to traverse in recursive mode
    for it in v.iterator():
        if isexfat:
            if it.type != 5: continue
        else:
            if it.IsLabel(): continue
        if filt:
            if not fnmatch.fnmatch(it.Name(), filt):
                continue
        if opts.recursive and it.IsDir():
            name = it.Name()
            dirs += [name]
        if opts.bare and not opts.sort:
            if opts.recursive:
                print (os.path.join(v.path, it.Name()))
            else:
                print(it.Name())
        else:
            if isexfat:
                tot_bytes += it.u64DataLength
            else:
                tot_bytes += it.dwFileSize
            if it.IsDir(): tot_dirs += 1
            else: tot_files += 1
            if isexfat:
                mtime = datetime(*(it.DatetimeParse(it.dwMTime)))
                size = it.u64DataLength
            else:
                mtime = datetime(*(it.ParseDosDate(it.wMDate) + it.ParseDosTime(it.wMTime)))
                size = it.dwFileSize
            if opts.sort:
                name = it.Name()
                # 0=Name, 1=DIR?, 2=Size, 3=Date, 4=Ext, 5=name
                table += [(name, not it.IsDir(), size, mtime, os.path.splitext(name)[1].lower(), name.lower())]
                continue
            _prn_line(it.Name(), mtime, (_fmt_size(size),'<DIR>   ')[it.IsDir()])
    if opts.sort:
        for it in sorted(table, key=itemgetter(*opts.sort), reverse=opts.sort_reverse):
            if opts.bare:
                print(it[0])
            else:
                _prn_line(it[0], it[3], (_fmt_size(it[2]),'<DIR>   ')[not it[1]])
    if not opts.bare:
        print("%18s Files    %s bytes" % (_fmt_size(tot_files), _fmt_size(tot_bytes)))
    if opts.recursive:
        for d in dirs:
            if d == '.' or d == '..': continue
            ff, dd, bb = _ls(v.opendir(d), filt, opts, depth+1)
            tot_files += ff
            tot_dirs += dd
            tot_bytes += bb
    if not opts.bare and not depth:
        if opts.recursive:
            print ("\n     Total items listed:")
            print("%18s Files    %s bytes" % (_fmt_size(tot_files), _fmt_size(tot_bytes)))
        print("%18s Directories %12s bytes free" % (_fmt_size(tot_dirs), _fmt_size(v.getdiskspace()[1])))
    return tot_files, tot_dirs, tot_bytes

    
def ls(args, opts):
    "Simple, DOS style directory listing, with size and last modification time"
    for arg in args:
        filt = None # wildcard filter
        img = is_vdisk(arg) # object to open
        if not img: img = arg
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
        _ls(v, filt, opts)


def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    fattools ls [-b -r -s NSDE-!] image.<vhd|vhdx|vdi|vmdk|img|bin|raw|dsk>[/path] ...

    """
    par = parser_create_fn(*parser_create_args,usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Lists files and directories in a supported disk or image.\nWildcards accepted.",
    epilog="Examples:\nfattools ls image.vhd\nfattools ls image.vhd/*.exe image.vhd/python39/dlls/*.pyd\n")
    par.add_argument('items', nargs='+')
    par.add_argument('-b', help='prints items names only', dest='bare', action="count", default=0)
    par.add_argument('-r', help='recursive (descends into subdirectories)', dest='recursive', action="count", default=0)
    par.add_argument('-s', help='sorts by name/size/date/ext (N/S/D/E), - (reverse order), ! (directories first)', dest='sort', type=str, default='')
    return par

def call(args):
    if len(args.items) < 1:
        print("ls error: you must specify at least one path to list!")
        par.print_help()
        sys.exit(1)

    class opts():
        pass

    opts = opts()
    opts.bare = args.bare
    opts.recursive = args.recursive
    opts.sort = args.sort
    opts.sort_reverse = 0
    opts.sort_dirfirst = 0
    
    if args.sort:
        opts.sort = []
        for c in args.sort:
            if  c == '-':
                opts.sort_reverse = 1
                continue
            if c == '!':
                opts.sort_dirfirst = 1
                continue
            if c not in 'NSDE':
                print("ls error: unknown sort method specified '%s'!"%c)
                par.print_help()
                sys.exit(1)
            opts.sort += [{'N':5,'S':2,'D':3,'E':4}[c]]
        if opts.sort_dirfirst:
            opts.sort.insert(0, 1)
        opts.sort = tuple(opts.sort)

    ls(args.items, opts)


if __name__ == '__main__':
    locale.setlocale(locale.LC_ALL, locale.getdefaultlocale()[0])
    
    par = create_parser()
    args = par.parse_args()
    call(args)
