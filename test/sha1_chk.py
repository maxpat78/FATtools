# -*- coding: cp1252 -*-
import sys, hashlib
from FATtools.Volume import vopen

if len(sys.argv) == 1:
    print ("Usage: sha1_chk DRIVE")
    sys.exit(1)

f = vopen(sys.argv[1], 'rb')

if 'hashes.sha1' not in f.listdir():
    print ("Aborting, SHA-1 hashes list not found!")
    sys.exit(2)

bad=0
L = f.open('hashes.sha1').read()
for o in L.split(b'\n'):
 sha, pname = o[:40].decode(), o[42:]
 pname = pname.strip().decode() # kills CR in Win
 if len(pname)<1: continue
 try:
    s = f.open(pname).read()
 except:
    print ("Exception on", pname)
    bad+=1
    continue
 test = hashlib.sha1(s).hexdigest()==sha
 if not test:
    bad+=1
    print ('bad', pname)

if bad:
    print (bad, "wrong file checksums detected!")
else:
    print ("all checksums were ok")
