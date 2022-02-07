import logging

#~ DEBUG bits (turn on logging in a specified module):
#~ 0=disk, partition
#~ 1=Volume
#~ 2=FAT
#~ 3=exFAT
#~ 4=Image handler

def log(*a):
    # log buffer gets nuked in case of exceptions?
    #~ print('FATtools:', *a)
    logging.getLogger('FATtools').debug(*a)
