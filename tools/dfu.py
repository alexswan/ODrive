#!/usr/bin/env python
"""
Tool for flashing .hex files to the ODrive via the STM built-in USB DFU mode.
"""

import argparse
import sys
import time
import threading
import platform
import struct
import array
import fractions
import dfuse
import usb.core
import odrive.core

# We are interactively printing status messages, so flush by default
import functools
print = functools.partial(print, flush=True)

try:
    from intelhex import IntelHex
except:
    sudo_prefix = "" if platform.system() == "Windows" else "sudo "
    print("You need intelhex for this ({}pip install IntelHex)".format(sudo_prefix), file=sys.stderr)
    sys.exit(1)


SIZE_MULTIPLIERS = {' ': 1, 'K': 1024, 'M' : 1024*1024}
MAX_TRANSFER_SIZE = 2048


def get_device_sectors(dfudev):
    """
    Returns a list of all sectors on the device.
    Each sector is represented as a dictionary with the following keys:
     - name: name of the associated memory region (e.g. "Internal Flash")
     - alt: USB alternate setting associated with this memory region
     - addr: Start address of the sector (e.g. 0x08004000 for the second flash sectors)
     - baseaddr: Start address of the memory region associated with the sector
                 (e.g. 0x08000000 for all flash sectors)
     - len: Number of bytes in the sector
    """
    for name, alt in dfudev.alternates():
        # example for name:
        # '@Internal Flash  /0x08000000/04*016Kg,01*064Kg,07*128Kg'
        label, baseaddr, layout = name.split('/')
        baseaddr = int(baseaddr, 0) # convert hex to decimal
        addr = baseaddr

        for sector in layout.split(','):
            repeat, size = map(int, sector[:-2].split('*'))
            size *= SIZE_MULTIPLIERS[sector[-2].upper()]
            mode = sector[-1]

            while repeat > 0:
                # TODO: verify if the section is writable
                yield {
                    'name': label.strip().strip('@'),
                    'alt': alt,
                    'baseaddr': baseaddr,
                    'addr': addr,
                    'len': size,
                    'mode': mode
                }

                addr += size
                repeat -= 1

def populate_sectors(sectors, hexfile):
    """
    Checks for which on-device sectors there is data in the hex file and
    returns a (sector, data) tuple for each touched sector where data
    is a byte array of the same size as the sector.
    """
    for sector in sectors:
        addr = sector['addr']
        size = sector['len']
        # check if any segment from the hexfile overlaps with this sector
        touched = False
        for (start, end) in hexfile.segments():
            if start < addr and end > addr:
                touched = True
                break
            elif start >= addr and start < addr + size:
                touched = True
                break

        if touched:
            # TODO: verify if the section is writable
            yield (sector, hexfile.tobinarray(addr, addr + size - 1))

def set_alternate_safe(dfudev, alt):
    dfudev.set_alternate(alt)
    if dfudev.get_state() == dfuse.DfuState.DFU_ERROR:
        dfudev.clear_status()
        dfudev.wait_while_state(dfuse.DfuState.DFU_ERROR)

#def clear_error(dfudev)
def set_address_safe(dfudef, addr):
    dfudev.set_address(addr)
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
    if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
        raise RuntimeError("An error occured. Device Status: %r" % status)
    # take device out of DFU_DOWNLOAD_SYNC and into DFU_IDLE
    dfudev.abort()
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_SYNC)
    if status[1] != dfuse.DfuState.DFU_IDLE:
        raise RuntimeError("An error occured. Device Status: %r" % status)
    

def erase(dfudev, sector):
    set_alternate_safe(dfudev, sector['alt'])
    dfudev.erase(sector['addr'])
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY, timeout=sector['len']/32)
    if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
        raise RuntimeError("An error occured. Device Status: %r" % status)

def flash(dfudev, sector, data):
    set_alternate_safe(dfudev, sector['alt'])
    set_address_safe(dfudev, sector['addr'])

    transfer_size = fractions.gcd(sector['len'], MAX_TRANSFER_SIZE)
    
    blocks = [data[i:i + transfer_size] for i in range(0, len(data), transfer_size)]
    for blocknum, block in enumerate(blocks):
        #print('write to {:08X} ({} bytes)'.format(
        #        sector['addr'] + blocknum * TRANSFER_SIZE, len(block)))
        dfudev.write(blocknum, block)
        status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
        if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
            raise RuntimeError("An error occured. Device Status: %r" % status)

def read(dfudev, sector):
    """
    Reads data from the specified sector
    Returns: a byte array containing the data
    """
    set_alternate_safe(dfudev, sector['alt'])
    set_address_safe(dfudev, sector['addr'])

    transfer_size = fractions.gcd(sector['len'], MAX_TRANSFER_SIZE)
    #blocknum_offset = int((sector['addr'] - sector['baseaddr']) / transfer_size)

    
    data = array.array(u'B')
    for blocknum in range(int(sector['len'] / transfer_size)):
        #print('read at {:08X}'.format(sector['addr'] + blocknum * TRANSFER_SIZE))
        deviceBlock = dfudev.read(blocknum, transfer_size)
        data.extend(deviceBlock)
    dfudev.abort() # take device into DFU_IDLE
    return data

def get_first_mismatch_index(array1, array2):
    """
    Compares two arrays and returns the index of the
    first unequal item or None if both arrays are equal
    """
    if len(array1) != len(array2):
        raise Exception("arrays must be same size")
    for pos in range(len(array1)):
        if (array1[pos] != array2[pos]):
            return pos
    return None


def jump_to_application(dfudev, address):
    set_address_safe(dfudev, address)
    #dfudev.set_address(address)
    #status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
    #if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
    #    raise RuntimeError("An error occured. Device Status: {}".format(status[1]))

    dfudev.leave()
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_MANIFEST_SYNC)
    if status[1] != dfuse.DfuState.DFU_MANIFEST:
        raise RuntimeError("An error occured. Device Status: {}".format(status[1]))

def str_to_uuid(uuid):
    uuid = bytearray.fromhex(uuid.replace('-', ''))
    return struct.unpack('>I', uuid[0:4]), struct.unpack('>I', uuid[4:8]), struct.unpack('>I', uuid[8:12])

def uuid_to_str(uuid0, uuid1, uuid2):
    return "{:08X}-{:08X}-{:08X}".format(struct.pack('>I', uuid0), struct.pack('>I', uuid1), struct.pack('>I', uuid2))

def uuid_to_serial(uuid0, uuid1, uuid2):
    return (struct.pack('>I', uuid0 + uuid2) + struct.pack('>I', uuid1)[0:2]).hex().upper()


### THREADS ###

def show_deferred_message(message, cancellation_token):
    """
    Shows a message after 10s, unless cancellation_token gets set.
    """
    def show_message_thread(message, cancellation_token):
        for i in range(1,10):
            if cancellation_token.is_set():
                return
            time.sleep(1)
        if not cancellation_token.is_set():
            print(message)
    t = threading.Thread(target=show_message_thread, args=(message, cancellation_token))
    t.daemon = True
    t.start()

def put_odrive_into_dfu_mode_thread(cancellation_token):
    """
    Waits for an ODrive with a matching serial number and puts
    it into DFU mode once it's found. The thread continues to put
    matching devices into DFU mode until cancellation_token
    is set.
    """
    global app_cancellation_token
    while not cancellation_token.is_set():
        constraints = {} if serial_number == None else {'serial_number': serial_number}
        my_drive = odrive.core.find_any(consider_usb=True, consider_serial=False,
                                        cancellation_token=cancellation_token,
                                        **constraints)
        if cancellation_token.is_set():
            return
        if not hasattr(my_drive, "enter_dfu_mode"):
            print("The firmware on device {} does not support DFU. You need to \n"
                  "flash the firmware once using STLink (`make flash`), after that \n"
                  "DFU with this script should work fine."
                  .format(my_drive.__channel__.usb_device.serial_number))
            # Terminate script, otherwise it would try to reconnect to the same
            # incompatible device
            app_cancellation_token.set() # TODO: implement a more sensible discorvery mechanism to fix this
            return
        print("Putting device {} into DFU mode...".format(my_drive.__channel__.usb_device.serial_number))
        try:
            my_drive.enter_dfu_mode()
        except usb.core.USBError as ex:
            pass # this is expected because the device reboots
        if platform.system() == "Windows":
            show_deferred_message("Still waiting for the device to reappear.\n"
                                  "Use the Zadig utility to set the driver of 'STM32 BOOTLOADER' to libusb-win32.",
                                  cancellation_token)
        # If we immediately continue we might still pick up the device that was
        # just rebooted. This isn't an issue but will display a distracting
        # error message.
        time.sleep(1)

### BEGINNING OF APPLICATION ###

# parse arguments
parser = argparse.ArgumentParser(description="Program an STM32 in DFU mode. The device can be identified either by it's serial number or UUID."
                                             "You can list all connected devices by running"
                                             "(lsusb -d 1209:0d32 -v; lsusb -d 0483:df11 -v) | grep iSerial")
parser.add_argument("-v", "--verbose", action="store_true",
                    help="print debug information")
parser.add_argument('file', metavar='HEX', help='the .hex file to be flashed')
parser.add_argument("-u", "--uuid",
                    help="The 12-byte UUID of the device. This is a hexadecimal number of the format"
                         "00000000-00000000-00000000")
parser.add_argument("-s", "--serial-number",
                    help="The 12-digit serial number of the device. This is a string consisting of 12 upper case hexadecimal digits as displayed in lsusb"
                         "example: 385F324D3037")
args = parser.parse_args()

# load hex file
hexfile = IntelHex(args.file)

#print("Contiguous segments in hex file:")
#for start, end in hexfile.segments():
#    print(" {:08X} to {:08X}".format(start, end - 1))

if args.uuid != None:
    serial_number = uuid_to_serial(*str_to_uuid(args.uuid))
elif args.serial_number != None:
    serial_number = args.serial_number
else:
    serial_number = None


app_cancellation_token = threading.Event()
find_odrive_cancellation_token = threading.Event()
try:
    print("Waiting for ODrive...")

    # Scan for ODrives not in DFU mode and put them into DFU mode once they appear
    threading.Thread(target=put_odrive_into_dfu_mode_thread, args=(find_odrive_cancellation_token,)).start()

    # Poll libUSB until a device in DFU mode is found
    while not app_cancellation_token.is_set():
        params = {} if serial_number == None else {'serial_number': serial_number}
        stm_device = usb.core.find(idVendor=0x0483, idProduct=0xdf11, **params)
        if stm_device != None:
            break
        time.sleep(1)
    find_odrive_cancellation_token.set() # we don't need this thread anymore
    if app_cancellation_token.is_set():
        sys.exit(1)
    print("Found device {} in DFU mode".format(stm_device.serial_number))

    dfudev = dfuse.DfuDevice(stm_device)

    sectors = list(get_device_sectors(dfudev))

    if (args.verbose):
        print("Sectors on device: ")
        for sector in sectors:
            print(" {:08X} to {:08X} ({})".format(
                sector['addr'],
                sector['addr'] + sector['len'] - 1,
                sector['name']))

    # fill sectors with data
    touched_sectors = list(populate_sectors(sectors, hexfile))

    if (args.verbose):
        print("The following sectors will be flashed: ")
        for sector,_ in touched_sectors:
            print(" {:08X} to {:08X}".format(sector['addr'], sector['addr'] + sector['len'] - 1))

    # Erase
    try:
        for i, (sector, data) in enumerate(touched_sectors):
            print("Erasing... (sector {}/{})  \r".format(i, len(touched_sectors)), end='', flush=True)
            erase(dfudev, sector)
        print('Erasing... done            \r', end='', flush=True)
    finally:
        print('', flush=True)

    # Flash
    try:
        for i, (sector, data) in enumerate(touched_sectors):
            print("Flashing... (sector {}/{})  \r".format(i, len(touched_sectors)), end='', flush=True)
            flash(dfudev, sector, data)
        print('Flashing... done            \r', end='', flush=True)
    finally:
        print('', flush=True)

    # Verify
    try:
        for i, (sector, expected_data) in enumerate(touched_sectors):
            print("Verifying... (sector {}/{})  \r".format(i, len(touched_sectors)), end='', flush=True)
            observed_data = read(dfudev, sector)
            mismatch_pos = get_first_mismatch_index(observed_data, expected_data)
            if not mismatch_pos is None:
                mismatch_pos -= mismatch_pos % 16
                observed_snippet = ' '.join('{:02X}'.format(x) for x in observed_data[mismatch_pos:mismatch_pos+16])
                expected_snippet = ' '.join('{:02X}'.format(x) for x in expected_data[mismatch_pos:mismatch_pos+16])
                raise RuntimeError("Verification failed around address 0x{:08X}:\n".format(sector['addr'] + mismatch_pos) +
                                   "  expected: " + expected_snippet + "\n"
                                   "  observed: " + observed_snippet)
        print('Verifying... done            \r', end='', flush=True)
    finally:
        print('', flush=True)


    # If the flash operation failed for some reason, your device is bricked now.
    # You can unbrick it as long as the device remains powered on.
    # (or always with an STLink)
    # So for debugging you should comment this last part out.

    # Jump to application
    jump_to_application(dfudev, 0x08000000)
finally:
    find_odrive_cancellation_token.set()


# Note: the flashed image can be verified using: (0x12000 is the number of bytes to read)
# $ openocd -f interface/stlink-v2.cfg -f target/stm32f4x.cfg -c init -c flash\ read_bank\ 0\ image.bin\ 0\ 0x12000 -c exit
# $ hexdump -C image.bin > image.bin.txt
#
# If you compare this with a reference image that was flashed with the STLink, you will see
# minor differences. This is because this script fills undefined sections with 0xff.
# $ diff image_ref.bin.txt image.bin.txt
# 21c21
# < *
# ---
# > 00000180  d9 47 00 08 d9 47 00 08  ff ff ff ff ff ff ff ff  |.G...G..........|
# 2553c2553
# < 00009fc0  9e 46 70 47 00 00 00 00  52 20 96 3c 46 76 50 76  |.FpG....R .<FvPv|
# ---
# > 00009fc0  9e 46 70 47 ff ff ff ff  52 20 96 3c 46 76 50 76  |.FpG....R .<FvPv|


