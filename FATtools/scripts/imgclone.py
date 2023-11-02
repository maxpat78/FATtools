# -*- coding: utf-8 -*-
import sys, os, argparse, datetime, time, logging
from FATtools.scripts.mkvdisk import call as mkvdisk
from FATtools import Volume
from FATtools.FAT import Handle
from FATtools.utils import is_vdisk

DEBUG = 0
from FATtools.debug import log

#~ logging.basicConfig(level=logging.DEBUG, filename='imgclone.log', filemode='w')

def print_progress(start_time, totalBytes, totalBytesToDo):
    "Prints a progress string"
    T = time.time()
    pct_done = 100.0*totalBytes/totalBytesToDo
    # Limits console output to 1 print per second, beginning after 3"
    if (T - start_time) < 3 or (T - print_progress.last_print) < 1: return
    print_progress.last_print = T
    avg_secs_remaining = (print_progress.last_print - start_time) / pct_done * 100.0 - (print_progress.last_print - start_time)
    avg_secs_remaining = int(avg_secs_remaining)
    avg_speed = (totalBytes/(1<<20)) / (print_progress.last_print - start_time)
    if avg_secs_remaining < 61:
        s = '%d"' % avg_secs_remaining
    else:
        s = "%d:%02d'" % (avg_secs_remaining/60, avg_secs_remaining%60)
    print_progress.fu('%d%% done (%.02f MiB/s), %s left         \r' % (pct_done, avg_speed, s))

print_progress.last_print = 0
if 'linux' in sys.platform:
	def fu(s):
		sys.stdout.write(s)
		sys.stdout.flush()
	print_progress.fu = fu
else:
	print_progress.fu = sys.stdout.write


def print_timings(start, stop):
	print ("Done. %s elapsed.            " % datetime.timedelta(seconds=int(stop-start)))

class Arguments:
    pass


def imgclone(src, dest, force=0):
    if not os.path.exists(src):
        print("Source '%s' does not exist!"%src)
        sys.exit(1)
    img1 = Volume.vopen(src, what='disk')
    if not os.path.exists(src):
        print("Couldn't open source '%s'"%src)
        sys.exit(1)
    if os.path.exists(dest):
        # if block device, access directly
        if os.path.isfile(dest):
            if not force:
                #~ print("imgclone error: destination disk image already exists, use -f to force overwriting")
                print("imgclone error: destination disk image already exists")
                sys.exit(1)
    else:
        o = Arguments()
        o.base_image = None
        o.large_sectors = None
        o.monolithic = None
        o.image_size = str(img1.size)
        o.image_file = dest
        o.force = force
        mkvdisk(o)
    img2 = Volume.vopen(dest, 'r+b', what='disk')
    print('Transferring %.02f MiB...' % (img1.size/(1<<20)))
    start_time = time.time()
    done = 0
    while True:
        s = img1.read(2<<20)
        if not s: break
        img2.write(s)
        done+=len(s)
        print_progress(start_time,done,img1.size)
    img1.close()
    img2.close()
    print_timings(start_time, time.time())

def printn(s): print(s)

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    fattools imgclone src dest
    """
    par = parser_create_fn(*parser_create_args,usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Clones a drive/virtual disk image into another one, copying its blocks one by one.",
    epilog="Examples:\nfattools imgclone raw.img dynamic.vhd\nfattools imgclone dos.vhd \\\\.\\PhysicalDrive2\n\nUseful for optimizing or converting between supported image formats.\n")
    par.add_argument('items', nargs='+')
    par.add_argument("-f", "--force", dest="force", help="overwrite a pre-existing image", action="store_true", default=False)
    return par

def call(args):
    if len(args.items) != 2:
        print("imgclone error: you must specify a source and a target!")
        sys.exit(1)
    imgclone(args.items[0], args.items[1], args.force)

if __name__ == '__main__':
    par=create_parser()
    args = par.parse_args()
    call(args)
