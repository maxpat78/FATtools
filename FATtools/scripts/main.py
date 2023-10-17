import argparse,importlib,sys


def main():
    scripts=["cat","cp","imgclone","ls","mkfat","mkvdisk","rm","reordergui","wipe"]
    help_s="Usage: fattools " + ''.join( ['%s|'%s for s in scripts])[:-1]

    if len(sys.argv) < 2:
        print("You must specify a command to perform!\n\n" + help_s)
        sys.exit(1)
        
    if sys.argv[1] not in scripts:
        print("Bad command specified!\n\n" + help_s)
        sys.exit(1)
    
    par=argparse.ArgumentParser(usage=help_s)
    subparsers=par.add_subparsers(help="command to perform")
    for x in scripts:
        mod=importlib.import_module("FATtools.scripts.%s"%x)
        subpar=mod.create_parser(subparsers.add_parser,[x])
        subpar.set_defaults(func=mod.call)
    args=par.parse_args()
    args.func(args)
