# -*- coding: utf-8 -*-
import sys, os, argparse, logging, fnmatch
from FATtools import Volume
from FATtools.utils import myfile, is_vdisk

from FATtools.debug import log

DEBUG = 0

#~ logging.basicConfig(level=logging.DEBUG, filename='rm.log', filemode='w')


def _rm(v, args):
    for it in args:
        is_file = 1
        fp = v.open(it)
        if not fp.IsValid:
            # if existent but invalid, it is a dir
            if DEBUG&2: log("rm: probing '%s' as directory", it)
            is_file = 0
            fp = v.opendir(it)
            if not fp:
                if DEBUG&2: log("rm: '%s' does not exist", it)
                print('"%s" does not exist!'%it)
                continue
        if is_file:
            print("Erasing file %s" % it)
            r = v.erase(it)
            if DEBUG&2: log("rm: erase('%s') returned %d", it, r)
        else:
            print("Erasing directory %s..." % it)
            r = v.rmtree(it)
            if DEBUG&2: log("rm: rmtree('%s') returned %d", it, r)



def rm(args):
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
                _rm(v, todo)
        else:
            _rm(v, [path])

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    rm.py <file1 or dir1> [file2 or dir2...]
    """
    par = parser_create_fn(*parser_create_args, usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Removes items from supported disk or images. Wildcards accepted.",
    epilog="Examples:\nrm.py image.vhd/texts/*.txt image.vhd/Dir1\n")
    par.add_argument('items', nargs='+')
    return par

def call(args):
    if len(args.items) < 1:
        print("rm error: you must specify at least one item to remove!")
        par.print_help()
        sys.exit(1)

    rm(args.items)
    
if __name__ == '__main__':
    par = create_parser()
    args = par.parse_args()
    call(args)
