#!/usr/bin/python3
import os
import sys
import shutil
import argparse
import tempfile
import functools

# Elf64_Ehdr.e_type
ET_REL  = 1 # Relocatable file
ET_EXEC = 2 # Executable file
ET_DYN  = 3 # Shared object file

parser = argparse.ArgumentParser(description='prelink -r wrapper')
parser.add_argument('--binary',
    help='The binary to prelink',
    required=True
)
parser.add_argument('--address',
    help='Binary base address (prelink -r address)',
    type=functools.wraps(int)(lambda x: int(x,0)),
    default=0x0000555555554000, #BIN_START
)
parser.add_argument('--static',
    help='Rewrite the binary as static',
    action='store_true', default=False
)
parser.add_argument('--force',
    help='Only to be used on ET_DYN files or ET_EXEC the were previously prelinked with this script',
    action='store_true', default=False
)
parser.add_argument('--dest', # TODO: add also argument --inplace
    help='Destination binary (Can be the same as the input file)',
    required=True
)
args = parser.parse_args()

prelink = "prelink"
prelinkbin = shutil.which(prelink)
if not prelinkbin:
    sys.exit(f"Error: {prelink} not found in PATH. Please install it.")
st = os.stat(args.binary) # may raise OSError.
with open(args.binary, "rb") as din:
    original = din.read()
if args.force:
    assert(original[0x10] == ET_EXEC or original[0x10] == ET_DYN)


# Create a temporary binary copy as prelink modifies the original binary
tmp_fd, tmp_name = tempfile.mkstemp()
if args.force:
    os.write(tmp_fd, original[:0x10] + bytes([ET_DYN]) + original[0x11:]) # force ET_DYN so that prelink works
else:
    os.write(tmp_fd, original)
os.close(tmp_fd)
os.chmod(tmp_name, st.st_mode)

# The current prelink cannot handle this debugging section, so we remove it.
# (TODO: Verify that this is only for the case of ARMv8.5 ?)
os.system(f"/usr/bin/strip --remove-section=.debug_line_str {tmp_name}")

# prelink the binary
ret = os.system(f"{prelinkbin} -r {args.address} {tmp_name}")
if ret != 0:
    sys.exit("Error: Prelinking the binary failed.")

# write the prelinked binary to the final destination
with open(tmp_name, 'rb') as tmp:
    premapped = tmp.read()

    with open(args.dest, "wb") as fout:
        if args.static:
            fout.write(premapped[:0x10] + bytes([ET_EXEC]) + premapped[0x11:]) # Elf64_Ehdr.e_type = ET_EXEC
        else:
            fout.write(premapped)
    os.chmod(args.dest, st.st_mode)

os.remove(tmp_name)
