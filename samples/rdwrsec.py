from FATtools.Volume import vopen
import sys

# rdwrsec r|w disk_image sec_no hex_filler
# read/write (fill) an absolute disk sector

def hexdump(data):
    if isinstance(data, str):
        data = data.encode('utf-8')

    # Costruisce l'intestazione: "         00 01 02 03 04 05 06 07 - 08 09 0A 0B 0C 0D 0E 0F"
    header_nums = " ".join(f"{i:02X}" for i in range(16))
    header_formatted = f"{header_nums[:23]} - {header_nums[24:]}"
    
    border = "-" * 66
    print(f"  OFFSET   {header_formatted}   ASCII")
    print(f"  {border}")

    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_bytes = [f"{b:02x}" for b in chunk]
        left_side = " ".join(hex_bytes[:8])
        right_side = " ".join(hex_bytes[8:])
        if len(chunk) <= 8:
            left_side = left_side.ljust(23)
            right_side = "".ljust(23)
        else:
            right_side = right_side.ljust(23)
        ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        print(f"{i:08x}:  {left_side} - {right_side}  |{ascii_part}|")

    print(f"  {border}")
    print(f"  OFFSET   {header_formatted}   ASCII")

op = sys.argv[1].lower()
v = vopen(sys.argv[2],'r+b',what='disk')
offset = int(sys.argv[3])*512
v.seek(offset)
if op == 'w':
    v.write(512*bytes([int(sys.argv[4],16)]))
elif op == 'r':
    s = v.read(512)
    hexdump(s)
v.close()
