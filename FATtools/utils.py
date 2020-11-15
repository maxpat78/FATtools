# -*- coding: cp1252 -*-
import io, struct

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
