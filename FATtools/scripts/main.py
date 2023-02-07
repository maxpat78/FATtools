import argparse
import importlib



def main():
    help_s = """
    fattools
    """
    par=argparse.ArgumentParser(usage=help_s)
    subparsers=par.add_subparsers(help="command to perform")
    scripts=["cp","ls","mkfat","mkvdisk","rm"]
    for x in scripts:
        mod=importlib.import_module("FATtools.scripts.%s"%x)
        subpar=mod.create_parser(subparsers.add_parser,[x])
        subpar.set_defaults(func=mod.call)
    
    args = par.parse_args()
    args.func(args)
