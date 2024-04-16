print ("Stringifying MBR code...\n", ''.join(['\\x%02X' % c for c in open('mymbr','rb').read()]))
