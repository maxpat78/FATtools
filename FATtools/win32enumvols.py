# -*- coding: cp1252 -*-
import os
DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))
from ctypes import *

if os.name == 'nt':
    from ctypes.wintypes import *

from FATtools.debug import log

# Required to avoid access violations
windll.kernel32.FindFirstVolumeA.restype = HANDLE
windll.kernel32.FindFirstVolumeA.argtypes = [LPVOID, DWORD]

windll.kernel32.FindNextVolumeA.restype = BOOL
windll.kernel32.FindNextVolumeA.argtypes = [HANDLE, LPVOID, DWORD]

windll.kernel32.FindVolumeClose.argtypes = [HANDLE]

windll.kernel32.GetVolumePathNamesForVolumeNameA.restype = BOOL
windll.kernel32.GetVolumePathNamesForVolumeNameA.argtypes = [LPVOID, LPVOID, DWORD, LPDWORD]

class DISK_EXTENT(Structure):
    _fields_ = [("DiskNumber", DWORD), ("StartingOffset", LARGE_INTEGER), ("ExtentLength", LARGE_INTEGER)]
    
class VOLUME_DISK_EXTENTS(Structure):
    _fields_ = [("NumberOfDiskExtents", DWORD), ("Extents", DISK_EXTENT)]



def get_phys_drive_num(volume):
    "Returns the number of the disk containing a given volume"
    h = windll.kernel32.CreateFileA(volume, DWORD(0xC0000000), DWORD(3), 0, DWORD(3), DWORD(0x80000000|0x10000000|0x20000000), 0)
    if h == -1:
        if DEBUG&1: log('CreateFileA could not open volume "%s"', volume.decode())
        return h
    vde = VOLUME_DISK_EXTENTS()
    cb = DWORD(0)
    # IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x560000
    res = windll.kernel32.DeviceIoControl(h, DWORD(0x560000), 0, DWORD(0), byref(vde), sizeof(vde), byref(cb), 0)
    if res == 0:
        if DEBUG&1: log('IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS failed on volume "%s"', volume.decode())
        return -1
    windll.kernel32.CloseHandle(h)
    return vde.Extents.DiskNumber

def get_volume_paths(volume):
    "Returns the drive paths (=letter) associated with a volume"
    s = create_string_buffer(4096)
    cb = DWORD(0)
    if not windll.kernel32.GetVolumePathNamesForVolumeNameA(volume, s, 4096, byref(cb)):
        return b''
    return s.value # intentionally returns only the FIRST value in the buffer
    
def enum_nt_volumes():
    "Enumerates all volumes and couple each with its physical drive and mount paths"
    volumes = {}
    paths = {}
    def couple(v):
        v1 = v[:-1]
        n = get_phys_drive_num(v1)
        if n != -1:
            k = b'\\\\.\\physicaldrive%d'%n
            if k not in volumes: volumes[k] = []
            volumes[b'\\\\.\\physicaldrive%d'%n] += [v1]
        p = get_volume_paths(v)
        if p:
            paths[v1] = p
    s = create_string_buffer(256)
    hffv = windll.kernel32.FindFirstVolumeA(s, 256)
    if hffv == -1: return None, None
    couple(s.value)
    while windll.kernel32.FindNextVolumeA(hffv, s, 256) != 0:
        couple(s.value)
    windll.kernel32.FindVolumeClose(hffv)
    return volumes, paths

def dismount_and_lock_all(device):
    "Dismounts all children volumes mounted on a device, allowing writes everywhere on it"
    return_handles=[]
    volumes, paths = enum_nt_volumes()
    if not volumes:
        if DEBUG&1: log('enum_nt_volumes did not find volumes to dismount!')
        return
    if DEBUG&1: log('enum_nt_volumes\n  volumes: %s\n  paths: %s', volumes, paths)
    #~ print (device, volumes, paths)
    if device in volumes:
        for volume in volumes[device]:
            h = windll.kernel32.CreateFileA(volume, DWORD(0xC0000000), DWORD(3), 0, DWORD(3), DWORD(0x80000000|0x10000000|0x20000000), 0)
            if h == -1:
                if DEBUG&1: log('Volume "%s" still mounted on device "%s", write operations might fail!', volume, device)
                continue
            #~ ioctls = {0x90020: 'FSCTL_DISMOUNT_VOLUME', 0x56C00C:'IOCTL_VOLUME_OFFLINE'}
            ioctls = {0x90020: 'FSCTL_DISMOUNT_VOLUME',0x90018:'FSCTL_LOCK_VOLUME'}
            status = c_int(0)
            for ioctl in ioctls:
                vpath = b'no mountpoint'
                if volume in paths:
                    vpath = paths[volume]
                locked=False
                # sometimes locking the volumes fails if it is done immediately after dismounting
                # so retry it
                for retries in range(20):
                    if windll.kernel32.DeviceIoControl(h, DWORD(ioctl), 0, DWORD(0), 0, DWORD(0), byref(status), 0):
                        err=GetLastError()
                        if err==21:
                            time.sleep(1)
                            print("Retry locking")
                            continue
                        if err:
                            raise BaseException('DeviceIoControl %s failed with code %d (%s)' % (ioctls[ioctl], err, FormatError()))
                    locked=True
                    break
                if not locked:
                    raise BaseException('Locking drive %s (%s) timed out' % (volume.decode(),vpath.decode()))                
            print ('Note: volume "%s" (%s) on %s dismounted.' % (volume.decode(),vpath.decode(),device.decode()))
            if DEBUG&1: log('Volume "%s" (%s) on %s dismounted.', volume.decode(), vpath.decode(), device.decode())
            return_handles.append(h)
    return return_handles

def unlock_volume_handles(handles):
    for h in handles:
        windll.kernel32.CloseHandle(h)
