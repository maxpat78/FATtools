print ("Stringifying NODOS code (to put at offset 5Ah/78h)...\n", ''.join(['\\x%02X' % c for c in open('nodos','rb').read()]))
