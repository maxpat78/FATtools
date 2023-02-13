# -*- coding: utf-8 -*-
import sys, os, argparse, logging, fnmatch
from FATtools import Volume
from FATtools.utils import myfile, is_vdisk

from FATtools.debug import log

DEBUG = 0

#~ logging.basicConfig(level=logging.DEBUG, filename='cat.log', filemode='w')


def _cat(v, args):
    for it in args:
        chomp = 1
        fp = v.open(it)
        while chomp:
            s = fp.read(16<<10) # read in 16K chunk
            if not s: break
            sys.stdout.buffer.write(s)


def cat(args):
    for arg in args:
        filt = None # wildcard filter
        img = is_vdisk(arg) # object to open
        if not img: img = arg
        path = arg[len(img)+1:] # eventual path inside it
        v = Volume.vopen(img, 'rb+')
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
        if filt:
            todo=[]
            for it in v.listdir():
                if fnmatch.fnmatch(it, filt):
                    todo += [it]
            if not todo:
                print('No matches for', filt)
            else:
                _cat(v, todo)
        else:
            _cat(v, [path])

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    cat.py file1 [file2 ...]
    """
    par = parser_create_fn(*parser_create_args, usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Reads data from one or more files and outputs their contents.",
    epilog="Examples:\ncat.py image.vhd/readme.txt\ncat.py image.vhd/readme.bz2 | bzip2 -d\n")
    par.add_argument('items', nargs='+')
    return par

def call(args):
    if len(args.items) < 1:
        print("cat error: you must specify at least one item to read!")
        par.print_help()
        sys.exit(1)

    cat(args.items)
    
if __name__ == '__main__':
    par = create_parser()
    args = par.parse_args()
    call(args)
