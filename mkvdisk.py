import sys, optparse
from FATtools import vhdutils, vdiutils, vmdkutils

help_s = """
%prog -s size <image.[vhd|vdi|vmdk]>
"""
par = optparse.OptionParser(usage=help_s, version="%prog 1.0", description="Creates an empty VHD, VDI or VMDK dynamic virtual disk image.")
par.add_option("-s", "--size", dest="image_size", help="specify virtual disk size. K, M, G or T suffixes accepted.", metavar="SIZE", type="string")
opts, args = par.parse_args()

if not args:
    print("mkvdisk error: you must specify a virtual disk image file name!")
    par.print_help()
    sys.exit(1)

if not opts.image_size:
    print("mkvdisk error: you must specify a virtual disk image size!")
    par.print_help()
    sys.exit(1)

s = args[0].lower()
if not s.endswith('.vhd') and not s.endswith('.vdi') and not s.endswith('.vmdk'):
    print("mkvdisk error: unknown image extension, you must specify a known format!")
    par.print_help()
    sys.exit(1)

if s.endswith('.vhd'):
    fmt = vhdutils
elif s.endswith('.vdi'):
    fmt = vdiutils
else:
    fmt = vmdkutils

u = opts.image_size[-1].lower()
if u in ('k','m','g','t'):
    fssize = int(opts.image_size[:-1]) * (1<<{'k':10,'m':20,'g':30,'t':40}[u])
else:
    fssize = int(opts.image_size)

fmt.mk_dynamic(args[0], fssize, overwrite='yes')
print("Virtual disk image '%s' created."%args[0])
