# -*- coding: cp1252 -*-
import io, struct, os, re
from FATtools.debug import log
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

class myfile(io.FileIO):
    "Wrapper for file object whose read member returns a bytearray"
    def __init__ (self, *args, **kwargs):
        super(myfile, self).__init__ (*args, **kwargs)

    def read(self, size=-1):
        return bytearray(super(myfile, self).read(size))

def is_vdisk(s):
    "Returns the base virtual disk image path if it contains a known extension or an empty string"
    image_path=''
    for ext in ('vhdx', 'vhd', 'vdi', 'vmdk', 'img', 'dsk', 'raw', 'bin'):
        if '.'+ext in s.lower():
            i = s.lower().find(ext)
            image_path = s[:i+len(ext)]
            break
    return image_path

def class2str(c, s):
    "Pretty-prints class contents"
    keys = list(c._kv.keys())
    keys.sort()
    for key in keys:
        o = c._kv[key][0]
        v = getattr(c, o)
        if type(v) in (type(0), type(0)):
            v = hex(v)
        s += '%x: %s = %s\n' % (key, o, v)
    return s

def common_getattr(c, name):
    "Decodes and stores an attribute following special class layout"
    i = c._vk[name]
    fmt = c._kv[i][1]
    cnt = struct.unpack_from(fmt, c._buf, i+c._i) [0]
    setattr(c, name,  cnt)
    return cnt

# Use hasattr to determine is value was previously unpacked, or avoid repacking?
def pack(c):
    "Updates internal buffer"
    for k in list(c._kv.keys()):
        v = c._kv[k]
        c._buf[k:k+struct.calcsize(v[1])] = struct.pack(v[1], getattr(c, v[0]))
    return c._buf

def common_setattr(c, name, value):
    "Imposta e codifica un attributo in base al layout di classe"
    object.__setattr__(c, name,  value)
    i = c._vk[name]
    fmt = c._kv[i][1]
    struct.pack_into(fmt, c._buf, i+c._i, value)

def FSguess(boot):
    "Try to guess the file system type between FAT12/16/32, exFAT and NTFS examining the boot sector"
    # if no signature or JMP opcode, it is not valid
    #~ print boot._buf[0], ord(boot._buf[0]), hex(ord(boot._buf[0]))
    if boot.wBootSignature != 0xAA55 or boot._buf[0] not in ('\xEB', 0xEB, '\xE9', 0xE9):
        return 'NONE'
    if boot.chOemID.startswith(b'NTFS'):
        return 'NTFS'
    if boot.wBytesPerSector == 0:
        return 'EXFAT'
    if boot.wMaxRootEntries == 0:
        return 'FAT32'
    if boot.sFSType.rstrip() in ('FAT12', 'FAT16'):
        return boot.sFSType.rstrip().decode()
    if boot.wMaxRootEntries < 512:
        return 'FAT12'
    return 'FAT16'

def get_format_parameters(size, id=None, sector=512):
    "Returns the format parameters for a given floppy size or type"
    # { id: {total_sectors, media_byte, sector_size, cluster_size, root_entries} }
    sectors = size//sector
    params = {
    "fd160": {"total_sectors":320, "media_byte":0xFE, "cluster_size":512, "root_entries":64}, # 5.25" SS/DD 160KB
    "fd180": {"total_sectors":360, "media_byte":0xFC, "cluster_size":512, "root_entries":64}, # 5.25" SS/DD 180KB
    "fd320": {"total_sectors":640, "media_byte":0xFF, "cluster_size":1024, "root_entries":112}, # 5.25" DS/DD 320KB
    "fd360": {"total_sectors":720, "media_byte":0xFD, "cluster_size":1024, "root_entries":112}, # 3.5" DS/DD 360KB
   "fd640": {"total_sectors":1280, "media_byte":0xFB, "cluster_size":512, "root_entries":112}, # 3.5" DS/DD 640KB
   "fd720": {"total_sectors":1440, "media_byte":0xF9, "cluster_size":1024, "root_entries":112}, # 3.5" DS/DD 720KB
   "fd1200": {"total_sectors":2400,"media_byte":0xF9, "cluster_size":512, "root_entries":224}, # 5.25" DS/HD 1200KB
   "fd1440": {"total_sectors":2880,"media_byte":0xF0, "cluster_size":512, "root_entries":224}, # 3.5" DS/HD 1440KB
   "msmdf1": {"total_sectors":3360, "media_byte":0xF0, "cluster_size":1024, "root_entries":16}, # 3.5" DS/HD 1680KB (MS-DMF, 1K cluster)
   "msmdf2": {"total_sectors":3360, "media_byte":0xF0, "cluster_size":2048, "root_entries":16}, # 3.5" DS/HD 1680KB (MS-DMF, 2K cluster)
   "fd1722": {"total_sectors":3444,"media_byte":0xF0, "cluster_size":512, "root_entries":224}, # 3.5" DS/HD 1720KB
   "fd1840": {"total_sectors":3680,"media_byte":0xF0, "cluster_size":512, "root_entries":224}, # 3.5" DS/HD 1840KB (IBM XDF)
   "fd2880": {"total_sectors":5760,"media_byte":0xF0, "cluster_size":1024, "root_entries":240}, # 3.5" DS/ED 2880KB
    }
    ret = None
    if id:
        ret = params.get(id)
        if ret: return ret
    for k, v in params.items():
        if v["total_sectors"] == sectors: return v
    return None
    
def get_geometry(size, sector=512):
    "Returns the CHS geometry that fits a disk size"
    # Heads and Sectors Per track are always needed in a FAT boot sector.
    # But LBA access *must* be used for more than 1024 cylinders.
    sectors = size // sector
    c=0
    h=0
    s=0
    # Avoid computations with some well-known IBM PC floppy formats and ST HDD
    # Look at: https://en.wikipedia.org/wiki/List_of_floppy_disk_formats#Logical_formats
    geometries = {
    320: (40, 1, 8), # 5.25" SS/DD 160KB
    360: (40, 1, 9), # 5.25" SS/DD 180KB
    #~ 640: (40, 2, 8), # 5.25" DS/DD 320KB
    640: (80, 1, 8), # 3.5" SS/DD 320KB
    #~ 720: (40, 2, 9), # 5.25" DS/DD 360KB
    720: (80, 1, 9), # 5.25" SS/DD 360KB
    1280: (80, 2, 8), # 3.5" DS/DD 640KB
    1440: (80, 2, 9), # 3.5" DS/DD 720KB
    2400: (80, 2, 15), # 5.25" DS/HD 1200KB
    2880: (80, 2, 18), # 3.5" DS/HD 1440KB
    3360: (80, 2, 21), # 3.5" DS/HD 1680KB (MS-DMF)
    3444: (82, 2, 21), # 3.5" DS/HD 1722KB
    3680: (80, 2, 23), # 3.5" DS/HD 1860KB (IBM XDF)
    5760: (80, 2, 36), # 3.5" DS/ED 2880KB
    10404: (306, 2, 17), # ST406 5MB 5.25" (HDD)
    41820: (615, 4, 17), # ST225 21MB 5.25"
    83640: (820, 6, 17) # ST251 43MB 5.25"
    }
    geometry = geometries.get(sectors)
    if geometry: return geometry

    # Calculates the number of full cylinders that fits the given size
    # Please note: Windows 11 uses X-255-63 geometry even for a 20 MiB drive!
    # DOS approach is more conservative
    for s in (17,36,48,52,63): # sectors per cylinder
        ch = sectors // s
        if ch > 1024*255: continue
        for h in (2,4,6,8,9,10,16,32,64,128,240,255): # heads
            c = ch//h 
            if c > 1024: continue
            break
    # If size exceeds 1024 cylinders, returns maximum Heads and Sectors per Cylinder
    if not h:
        s = 63
        h = 255
        c = sectors // (h*s)
    if DEBUG&1: log("%d cylinders with %d heads and %d sectors (CxHxSx%d=%d bytes)",c,h,s,sector,c*h*s*sector)
    return c, h, s

def chs2lba(c, h, s, hpc, spc):
    "Converts a CHS into LBA sector index, given the heads and sectors per cylinder"
    if hpc==0 or hpc>255 or spc==0 or spc>63: return None
    return (c*hpc+h)*spc + (s-1)

def lba2chs(lba, hpc, spc):
    "Converts a LBA into CHS sector index, given the heads and sectors per cylinder"
    if hpc==0 or hpc>255 or spc==0 or spc>63: return None
    c = lba//(hpc*spc)
    h = (lba//spc)%hpc
    s = (lba%spc)+1
    return c, h, s

def chs2raw(t):
    "Converts a CHS address from tuple to raw 24-bit MBR format"
    c,h,s = t
    if c > 1023:
        B1, B2, B3 = 254, 255, 255
    else:
        B1, B2, B3 = h, (c&768)>>2|s, c&255
    #~ print "DEBUG: MBR bytes for LBA %d (%Xh): %02Xh %02Xh %02Xh"%(lba, lba, B1, B2, B3)
    return b'%c%c%c' % (B1, B2, B3)

def raw2chs(t):
    "Converts a raw 24-bit CHS address into tuple"
    h,s,c = t[0], t[1], t[2]
    return ((s  & 192) << 2) | c, h, s & 63

def roundMB(n):
    "Round n at MiB"
    return  (n+(1<<20)-1) // (1<<20) * (1<<20)

def calc_rel_path(base, child):
    "returns the path of base relative to child"
    base_parts = re.split(r'[\\/]+', os.path.abspath(base))
    child_parts = re.split(r'[\\/]+', os.path.abspath(child))
    # strips common subpath, if any
    i=0
    while base_parts[i] == child_parts[i]: i += 1
    # returns base if they don't share anything
    if not i: return base
    n = len(child_parts) - 1 - i # counts path separators
    if n:
        relpath = ''
    else:
        relpath = '.\\'
    while n:
        relpath += '..\\'
        n -= 1
    relpath += '\\'.join(base_parts[i:])
    return relpath
