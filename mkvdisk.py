import os, sys, optparse
from FATtools import vhdutils, vhdxutils, vdiutils, vmdkutils
from FATtools.utils import is_vdisk
help_s = """
%prog -s size <image[.vhd|.vhdx|.vdi|.vmdk]>
%prog -b base_image[.vhd|.vhdx|.vdi|.vmdk] <delta_image[.vhd|.vhdx|.vdi|.vmdk]>
"""
par = optparse.OptionParser(usage=help_s, version="%prog 1.0", description="Creates an empty VHD, VHDX, VDI or VMDK dynamic or differencing virtual disk image or a RAW image if no or unknown extension specified.")
par.add_option("-s", "--size", dest="image_size", help="specify virtual disk size. K, M, G or T suffixes accepted", metavar="SIZE", type="string")
par.add_option("-b", "--base", dest="base_image", help="specify a virtual disk image base to create a differencing image with default parameters", metavar="BASE", type="string")
par.add_option("-f", "--force", dest="force", help="overwrites a pre-existing image", action="store_true", default=False)
opts, args = par.parse_args()

if not args:
    print("mkvdisk error: you must specify a disk image file name!")
    par.print_help()
    sys.exit(1)

if opts.base_image:
    modules = {'.vhd':vhdutils, '.vhdx':vhdxutils, '.vdi':vdiutils, '.vmdk':vmdkutils}
    delta = is_vdisk(args[0])
    if not delta:
        print("mkvdisk error: you must specify a differencing disk image file name (invalid extension?)")
        par.print_help()
        sys.exit(1)
    base = is_vdisk(opts.base_image)
    if not base:
        print("mkvdisk error: you must specify a valid base image to create a differencing disk!")
        par.print_help()
        sys.exit(1)
    m = modules[os.path.splitext(base)[1].lower()]
    m.mk_diff(delta, base, overwrite=('no','yes')[opts.force])
    print("Differencing image '%s' created and linked with base '%s'"%(delta,base))
    sys.exit(0)

if not opts.image_size:
    print("mkvdisk error: you must specify a virtual disk image size!")
    par.print_help()
    sys.exit(1)

u = opts.image_size[-1].lower()
if u in ('k','m','g','t'):
    fssize = int(opts.image_size[:-1]) * (1<<{'k':10,'m':20,'g':30,'t':40}[u])
else:
    fssize = int(opts.image_size)

s = args[0].lower()
if not s.endswith('.vhd') and not s.endswith('.vhdx') and not s.endswith('.vdi') and not s.endswith('.vmdk'):
    print("Creating RAW disk image '%s'... "%args[0], end='')
    f=open(args[0], 'wb');f.seek(fssize-1);f.write(b' ');f.close()
    print("OK!")
    sys.exit(0)

if s.endswith('.vhd'):
    fmt = vhdutils
elif s.endswith('.vhdx'):
    fmt = vhdxutils
elif s.endswith('.vdi'):
    fmt = vdiutils
else:
    fmt = vmdkutils

fmt.mk_dynamic(args[0], fssize, overwrite='yes')
print("Virtual disk image '%s' created."%args[0])
