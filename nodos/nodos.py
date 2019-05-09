print "Stringifying NODOS code (to put at offset 5Ah/78h)...\n", ''.join(['\\x%02X' % ord(c) for c in open('nodos','rb').read()])
