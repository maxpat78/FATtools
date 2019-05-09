import logging

#~ DEBUG bits (turn on logging in a specified module):
#~ 1=disk
#~ 2=Volume
#~ 3=FAT
#~ 4=exFAT

def log(*a):
    logging.getLogger('FATtools').debug(*a)
