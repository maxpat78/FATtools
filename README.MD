FATtools3
=========

Manually install it under site-packages.

Born to re-sort directory entries in a FAT32 root table to cope with some hardware MP3 players' limits, it now provides full read/write support in Python 3 (both 32- and 64-bit) for FAT12/16/32 and exFAT filesystems, for hacking and recovering purposes.

It is able to open disk partitioned with both MBR or GPT and to help in partitioning.
It can transparently create, read and write Dynamic and Differencing VHD, VHDX[1], VDI and VMDK disk images.
It can handle RAW disk images and BytesIO "RamDisk" images, also.

Actually, following features are implemented (mostly in Python, with a few ctypes calls to handle Win32 disks natively; compatibility with Linux is not regularly tested):
- sector aligned read/writes with both file images and real disks
- sector based caching mechanism (for both reading and writing) to speed-up FAT and directory table operations
- run length encoded map (with tuples and dictionaries) of free clusters, free directory slots, allocated cluster chains
- transparent reading and writing of FAT12/16/32 and exFAT filesystems with FS boot-sector auto recognizer
- MBR and GPT partitions handling
- Long File Name and Unicode support
- tools to open, create, rename, list and delete files and directories, and to partition disks
- facilities to sort, clean and shrink directory tables
- file fragmentation calculator
- mkfat tool to properly (partition and) apply a FAT12/16/32 or exFAT filesystem to a block device (file or disk) and let CHKDSK be happy with it (included exFAT compressed Up-Case table generator)

*Obviously, since a filesystem is an extremely complex and delicate matter, and big bugs may lay around, you'll USE IT TOTALLY AT YOUR OWN RISK!* But it seems quite stable and useable, now.

The most fragile area (and, thus, subject to bugs) was the caching mechanism, that operates in different ways:
- intercepting small I/O (<= 512 bytes), which is cached in a small circular buffer. Bigger I/O bypasses the cache; when the cache is full, all dirty sectors are committed to disk and the cache buffer is zeroed. Sectors and buffers are paired with Python builtin dictionaries: this permits a good (from a Pythonic perspective) I/O speed during FAT and directory tables access;
- mantaining a dictionary of pre-decoded FAT indexes, to improve the speed of repetitive access to cluster chains;
- mantaining a dictionary of short and long names (paired with their respective directory slots) for each directory table, to speed up searches and updates in directory tables;
- mantaining a RLE map of free clusters, free directory slots and allocated cluster chains, to dramatically improve speed of allocation and file access. 

*Actually, the I/O speed is closer to system's one.*

Code is GPLed (look at GPL.TXT inside folder).


[1] VHDX Log support is actually limited to replaying capability.



Sample usage (see inside 'test' directory for more samples).


At a glance

- to create a dynamic 1TB VHD disk image with a single GPT partition and format it with exFAT from command line:
```
py -m mkvdisk -s 1T image.vhd
py -m mkfat -t exfat -p gpt image.vhd
```

- to create a differencing VDI disk image from command line:
```
py -m mkvdisk -b image.vdi delta.vdi
```

- to list contents in a disk image, copy items to/from it and erase them (command line, too):
```
py -m ls image1.vhd/py* image2.vdi/py*
py -m cp C:\Python39\Lib\site-packages image.vhd/Python39/Lib
py -m cp image.vhd/Python39 C:\ProgramData
py -m rm image.vhd/Python39
```

- to open an existing plain or VHD disk image, or real disk:
```
# -*- coding: cp1252 -*-
from FATtools.Volume import *
disk = vopen('MyDiskImage.img', 'r+b', 'disk')
```

- to make a single GPT partition from all disk space:
```
from FATtools import partutils
gpt = partutils.partition(disk)
```

- to format such partition with the exFAT file system:
```
from FATtools import mkfat, Volume
part = Volume.vopen('MyDiskImage.img', 'r+b', 'partition0')
mkfat.exfat_mkfs(part, part.size)
```

- to order root directory table in USB drive X:
```
# -*- coding: cp1252 -*-
from FATtools.Volume import *

# Assuming we have DirA, DirB, DirC in this disk order into X:
root = vopen('X:', 'r+b')

new_order = '''DirB
DirC
DirA'''

root._sortby.fix = new_order.split('\n') # uses built-in directory sort algorithm
root.sort(root._sortby) # user-defined order, in _sortby.fix list
root.sort() # default ordering (alphabetical)
```

- mixed access with Python and FATtools from the same script:
```
# -*- coding: cp1252 -*-
from FATtools.Volume import vopen, vclose
from FATtools.mkfat import exfat_mkfs
from os.path import join
import os

real_fat_fs = 'F:' # replace with mount point of your file system

# Open and format with FATtools
fs = vopen(real_fat_fs, 'r+b',what='disk')
exfat_mkfs(fs, fs.size)
vclose(fs)

# Write some files with Python and list them
T = ('c','a','b','d')
for t in T:
   open(join(real_fat_fs, t+'.txt'), 'w').write('This is a sample "%s.txt" file.'%t)

print(os.listdir(real_fat_fs+'/'))

# Open again, and sort root with FATtools
fs = vopen(real_fat_fs, 'r+b')
fs.sort()
vclose(fs)

# Check new table order with Python
print(os.listdir(real_fat_fs+'/'))
```

- (almost) same as above:
```
# -*- coding: cp1252 -*-
from FATtools.Volume import vopen, vclose
from FATtools.mkfat import exfat_mkfs
from FATtools.partutils import partition

# Open & create GPT partition
o = vopen('\\\\.\\PhysicalDrive1', 'r+b',what='disk')
print('Partitioning...')
partition(o, 'mbr')
vclose(o)

# Reopen and format with EXFAT
o = vopen('\\\\.\\PhysicalDrive1', 'r+b',what='partition0')
print('Formatting...')
exfat_mkfs(o, o.size)
vclose(o) # auto-close partition AND disk

# Reopen FS and write
print('Writing...')
o = vopen('\\\\.\\PhysicalDrive1', 'r+b')

# Write some files with FATtools and sort them
T = ('c','a','b','d')
for t in T:
   f = o.create(t+'.txt')
   f.write(b'This is a sample "%s.txt" file.'%bytes(t,'ascii'))
   f.close()
o.sort()
vclose(o)
```
