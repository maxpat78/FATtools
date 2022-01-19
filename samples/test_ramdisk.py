# -*- coding: cp1252 -*-
from FATtools.Volume import vopen, vclose
from FATtools.mkfat import exfat_mkfs
from FATtools.disk import disk
import io

BIO = io.BytesIO((8<<20)*b'\x00')

# Reopen and format with EXFAT
o = vopen(BIO, 'r+b', what='disk')
print('Formatting...')
exfat_mkfs(o, o.size)
vclose(o)

print('Writing...')
o = vopen(BIO, 'r+b')
T = ('c','a','b','d')
for t in T:
   f = o.create(t+'.txt')
   f.write(b'This is a sample "%s.txt" file.'%bytes(t,'ascii'))
   f.close()
o.sort()
vclose(o)

open('BIO.IMG','wb').write(BIO.getbuffer())
