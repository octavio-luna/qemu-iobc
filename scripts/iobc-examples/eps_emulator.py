#!/usr/bin/env python3
#
# Example for peripheral simulation for the I2C test task on the isis-obc
# board.
#
# Copyright (c) 2019-2020 KSat e.V. Stuttgart
#
# This work is licensed under the terms of the GNU GPL, version 2 or, at your
# option, any later version. See the COPYING file in the top-level directory.

"""
Example framework for testing the twiTestTask of the OBSW.

See usart_test_task.py for more information.
"""


import asyncio
import struct
import json
import re
import errno
import time

# Paths to Unix Domain Sockets used by the emulator
QEMU_ADDR_QMP = '\0/tmp/qemu'
QEMU_ADDR_AT91_TWI = '\0/tmp/qemu_at91_twi'

# Request/response category and command IDs
IOX_CAT_DATA = 0x01
IOX_CAT_FAULT = 0x02

IOX_CID_DATA_IN = 0x01
IOX_CID_DATA_OUT = 0x02
IOX_CID_CTRL_START = 0x03
IOX_CID_CTRL_STOP = 0x04

IOX_CID_FAULT_OVRE = 0x01
IOX_CID_FAULT_NACK = 0x02
IOX_CID_FAULT_ARBLST = 0x03


class QmpException(Exception):
    """An exception caused by the QML/QEMU as response to a failed command"""

    def __init__(self, ret, *args, **kwargs):
        Exception.__init__(self, f'QMP error: {ret}')
        self.ret = ret  # the 'return' structure provided by QEMU/QML


class QmpConnection:
    """A connection to a QEMU machine via QMP"""

    def __init__(self, addr=QEMU_ADDR_QMP):
        self.transport = None
        self.addr = addr
        self.dataq = asyncio.Queue()
        self.initq = asyncio.Queue()
        self.proto = None

    def _protocol(self):
        """The underlying transport protocol"""

        if self.proto is None:
            self.proto = QmpProtocol(self)

        return self.proto

    async def _wait_check_return(self):
        """
        Wait for the status return of a command and raise an exception if it
        indicates a failure
        """

        resp = await self.dataq.get()
        if resp['return']:
            raise QmpException(resp['return'])

    async def open(self):
        """
        Open this connection. Connect to the machine ensure that the
        connection is ready to use after this call.
        """

        loop = asyncio.get_event_loop()
        await loop.create_unix_connection(self._protocol, self.addr)

        # wait for initial capabilities and version
        init = await self.initq.get()
        print(init)

        # negotioate capabilities
        cmd = '{ "execute": "qmp_capabilities" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

        return self

    def close(self):
        """Close this connection"""

        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.proto = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()

    async def cont(self):
        """Continue machine execution if it has been paused"""

        cmd = '{ "execute": "cont" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

    async def stop(self):
        """Stop/pause machine execution"""

        cmd = '{ "execute": "stop" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

    async def quit(self):
        """
        Quit the emulation. This causes the emulator to (non-gracefully)
        shut down and close.
        """

        cmd = '{ "execute": "quit" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()


class QmpProtocol(asyncio.Protocol):
    """The QMP transport protocoll implementation"""

    def __init__(self, conn):
        self.conn = conn

    def connection_made(self, transport):
        self.conn.transport = transport

    def connection_lost(self, exc):
        self.conn.transport = None
        self.conn.proto = None

    def data_received(self, data):
        data = str(data, 'utf-8')
        decoder = json.JSONDecoder()
        nows = re.compile(r'[^\s]')

        pos = 0
        while True:
            match = nows.search(data, pos)
            if not match:
                return

            pos = match.start()
            obj, pos = decoder.raw_decode(data, pos)

            if 'return' in obj:
                self.conn.dataq.put_nowait(obj)
            elif 'QMP' in obj:
                self.conn.initq.put_nowait(obj)
            elif 'event' in obj:
                pass
            else:
                print("qmp:", obj)


class DataFrame:
    """
    Basic protocol unit for communication via the IOX API introduced for
    external device emulation
    """

    def __init__(self, seq, cat, id, data=None):
        self.seq = seq
        self.cat = cat
        self.id = id
        self.data = data

    def bytes(self):
        """Convert this protocol unit to raw bytes"""
        data = self.data if self.data is not None else []
        return bytes([self.seq, self.cat, self.id, len(data)]) + bytes(data)

    def __repr__(self):
        return f'{{ seq: 0x{self.seq:02x}, cat: 0x{self.cat:02x}, id: 0x{self.id:02x}, data: {self.data} }}'


def parse_dataframes(buf):
    """Parse a variable number of DataFrames from the given byte buffer"""

    while len(buf) >= 4 and len(buf) >= 4 + buf[3]:
        frame = DataFrame(buf[0], buf[1], buf[2], buf[4:4+buf[3]])
        buf = buf[4+buf[3]:]
        yield buf, frame

    return buf, None


class I2cStatusException(Exception):
    """An exception returned by the TWI send command"""
    def __init__(self, errn, *args, **kwargs):
        Exception.__init__(self, f'TWI error: {errno.errorcode[errn]}')
        self.errno = errn   # a UNIX error code indicating the reason


class I2cStartFrame:
    def __init__(self, read, dadr, iadr):
        self.read = read != 0
        self.dadr = dadr
        self.iadr = iadr

    def __repr__(self):
        return f'{{ read: {self.read}, dadr: 0x{self.dadr:02x}, iadr: 0x{self.iadr:02x} }}'


class I2cSlave:
    """Connection to emulate a TWI/I2C device for a given QEMU/At91 instance"""

    def __init__(self, addr):
        self.addr = addr
        self.respd = dict()
        self.respc = asyncio.Condition()
        self.dataq = asyncio.Queue()
        self.transport = None
        self.proto = None
        self.seq = 0

    def _protocol(self):
        """The underlying transport protocol"""

        if self.proto is None:
            self.proto = I2cProtocol(self)

        return self.proto

    async def open(self):
        """Open this connection"""

        print(f"opening {self.addr}")
        loop = asyncio.get_event_loop()
        await loop.create_unix_connection(self._protocol, self.addr)
        return self

    def close(self):
        """Close this connection"""

        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.proto = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()

    def _send_new_frame(self, cat, cid, data=None):
        """
        Send a DataFrame with the given parameters and auto-increase the
        sequence counter. Return its sequence number.
        """
        self.seq = (self.seq + 1) & 0x7f

        frame = DataFrame(self.seq, cat, cid, data)
        self.transport.write(frame.bytes())

        return frame.seq

    async def read_frame(self):
        """Wait for a new data-frame and return it"""

        return await self.dataq.get()

    async def wait_start(self):
        """Wait for a new start frame from the I2C master"""

        while True:
            print("i2c waiting for start")
            frame = await self.read_frame()

            if frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_CTRL_START:
                read = frame.data[0] & 0x80
                dadr = frame.data[0] & 0x7f
                iadr = frame.data[2] | (frame.data[3] << 8) | (frame.data[4] << 16)
                return I2cStartFrame(read, dadr, iadr)
            else:
                print(f"i2c warning: discarding frame {frame}")

    async def wait_stop(self):
        """Wait for a stop frame from the I2C master"""

        while True:
            frame = await self.read_frame()

            if frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_CTRL_STOP:
                return
            else:
                print(f"i2c warning: discarding frame {frame}")

    async def write(self, data):
        """
        Write data to the I2C master. Should only be called after a start
        frame has been received. Does not wait for a stop-frame.
        """

        seq = self._send_new_frame(IOX_CAT_DATA, IOX_CID_DATA_IN, data)
        print("i2c write seq: ", seq, "Len: ", len(data))

        async with self.respc:
            while seq not in self.respd.keys():
                await self.respc.wait()

            resp = self.respd[seq]
            del self.respd[seq]

        status = struct.unpack('I', resp.data)[0]
        if status != 0:
            raise I2cStatusException(status)

    async def read(self):
        """
        Wait for and read bytes sent by the I2C master until the master
        sends a stop frame. Should only be called after a start frame has
        been received.
        """

        buf = bytes()
        while True:
            print("i2c waiting for data")
            frame = await self.read_frame()
            print("i2c frame: ", frame, "size: ", len(frame.data))

            if frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_DATA_OUT:
                buf += frame.data
            elif frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_CTRL_STOP:
                return buf
            else:
                print(f"i2c warning: unexpected frame {frame}")


class I2cProtocol(asyncio.Protocol):
    """The TWI/I2C transport protocoll implementation"""

    def __init__(self, conn):
        self.conn = conn
        self.buf = bytes()

    def connection_made(self, transport):
        self.conn.transport = transport

    def connection_lost(self, exc):
        self.conn.transport = None
        self.conn.proto = None

    def data_received(self, data):
        self.buf += data

        for buf, frame in parse_dataframes(self.buf):
            self.buf = buf

            if frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_DATA_OUT:
                # data from CPU/board to device
                self.conn.dataq.put_nowait(frame)
            elif frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_CTRL_START:
                self.conn.dataq.put_nowait(frame)
            elif frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_CTRL_STOP:
                self.conn.dataq.put_nowait(frame)
            elif frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_DATA_IN:
                # response for data from device to CPU/board
                loop = asyncio.get_event_loop()
                loop.create_task(self._data_response_received(frame))

    async def _data_response_received(self, frame):
        async with self.conn.respc:
            self.conn.respd[frame.seq] = frame
            self.conn.respc.notify_all()

#RESPONSE CODES
ACC=b'\x00'
REJ=b'\x01'
REJ_INV_CODE=b'\x02'
REJ_MISS_PAR=b'\x03'
REJ_PAR_INV=b'\x04'
REJ_UNAV=b'\x05'
REJ_INV_SYS=b'\x06'
INTERAL_ERR=b'\x07'
NEW=b'\x80'

#COMMANDS HANDLING UTILS
def default_resp(data: bytes):
    if len(data) < 4:
        return b'\x00\x00\x00\x00'
    resp = data[:2] + (data[2]+1).to_bytes(1, byteorder='little') + data[3:]
    return resp

##triplet of current, voltage and power values.
def build_VIPD(voltage, current, power):
    #All three engineering values, for both raw and engineering values, are in twos-complement format. Any multi-byte value
    #is returned in little endian byte order. Sign allows values to indicate directionality for current and power flows, and indicate
    #bias/offset for voltages that are slightly below zero. 

    #Build voltage to 2 bytes
    voltage_eng = voltage.to_bytes(2, byteorder='little', signed=True)
    current_eng = current.to_bytes(2, byteorder='little', signed=True)
    power_eng = power.to_bytes(4, byteorder='little', signed=True)

    return voltage_eng + current_eng + power_eng


#COMMANDS HANDLING
def watchDogReset(data):
    print("watchDogReset", data)
    resp = default_resp(data) + ACC
    return resp

def get_PIU_housekeeping_data(data):
    print("get_PIU_housekeeping_data", data)
    resp = default_resp(data) + ACC
    
    temp = 5
    VOLT_BRDSUP = (5).to_bytes(2, byteorder='little', signed=True) #TODO: Should this be little endian too?
    TEMP = temp.to_bytes(2, byteorder='little', signed=True) #TODO: Should this be little endian too?
    
    volt = 5
    current = 2
    power = 10 #TODO: What these values should be? Also, they should be in raw format

    VIP_DIST_INPUT = build_VIPD(volt, current, power)
    VIP_BATT_INPUT = build_VIPD(volt, current, power)

    #channel-on status for the output bus channels.
    STAT_CH_ON = (0).to_bytes(2, byteorder='little', signed=True) #TODO: What this value should be?
    STAT_CH_OCF = (0).to_bytes(2, byteorder='little', signed=True) #TODO: What this value should be?

    #All signed engineering values are in two’s complement format. Any multi-byte value is returned in little endian byte order. Page 26
    #x0001 battery cell 1 under voltage, x0002 battery cell 2 under voltage, x0004 battery cell  under voltage, x0008 battery cell 4 under voltage
    #x0010 battery cell 1 over voltage, x0020 battery cell 2 over voltage, x0040 battery cell 3 over voltage, x0080 battery cell 4 over voltage
    #x0100 battery cell 1 balancing, x0200 battery cell 2 balancing, x0400 battery cell 3 balancing, x0800 battery cell 4 balancing
    #x1000 heaters active, 0x8000 battery pack enabled
    
    BAT_STAT = b'\x10\x00' #TODO: we can randomiza these values
    
    #Battery pack temperature in between the center battery cells.
    BAT_TEMP2 = (5).to_bytes(2, byteorder='little', signed=True) #TODO: What this value should be?

    #Battery pack temperature in the frontof the battery cells.
    BAT_TEMP3 = (5).to_bytes(2, byteorder='little', signed=True) #TODO: What this value should be?

    #Voltage of voltage domain N° x
    VOLT_VD0 = (3).to_bytes(2, byteorder='little', signed=True) 
    VOLT_VD1 = (3).to_bytes(2, byteorder='little', signed=True) 
    VOLT_VD2 = (3).to_bytes(2, byteorder='little', signed=True) #TODO: What these values should be?

    #Output V, I and P of output bus channel
    VIP_CH00 = build_VIPD(volt, current, power) 
    VIP_CH01 = build_VIPD(volt, current, power)
    VIP_CH02 = build_VIPD(volt, current, power)
    VIP_CH03 = build_VIPD(volt, current, power)
    VIP_CH04 = build_VIPD(volt, current, power)
    VIP_CH05 = build_VIPD(volt, current, power)
    VIP_CH06 = build_VIPD(volt, current, power)
    VIP_CH07 = build_VIPD(volt, current, power)
    VIP_CH08 = build_VIPD(volt, current, power) #TODO: What these values should be?

    data_fields =  b'\x00' + VOLT_BRDSUP + TEMP + VIP_DIST_INPUT + VIP_BATT_INPUT + STAT_CH_ON + STAT_CH_OCF + BAT_STAT + BAT_TEMP2 + BAT_TEMP3 + VOLT_VD0 + VOLT_VD1 + VOLT_VD2 + VIP_CH00 + VIP_CH01 + VIP_CH02 + VIP_CH03 + VIP_CH04 + VIP_CH05 + VIP_CH06 + VIP_CH07 + VIP_CH08
    return resp + data_fields

#Maps every channel to a tuple of two values: the first one indicates if the channel is on, and the second one indicates if the channel is force-enabled
channels_status = {
    0: [False, False],
    1: [False, False],
    2: [False, False],
    3: [False, False],
    4: [False, True],
    5: [False, False]
}

def outputBusChannelOn(data):
    print("outputBusChannelOn", data)
    print("Turning on channel: ", data[4])
    #TODO: We have to set the channel's supported range
    status = channels_status[data[4]]
    if len(status) < 2:
        status = [False, False]
    status[0] = True
    channels_status[data[4]] = status
    resp = default_resp(data) + ACC
    return resp

def outputBusChannelOff(data):
    print("outputBusChannelOff", data)
    print("Turning off channel: ", data[4])
    #TODO: We have to set the channel's supported range
    status = channels_status[data[4]]
    if len(status) < 2:
        status = [False, False]
    if status[1]:
        #If the channel is force-enabled, we can't turn it off and we have to return a rejection
        print("Channel is force-enabled, rejecting it")
        resp = default_resp(data) + REJ
        return resp
    status[0] = False
    channels_status[data[4]] = status
    resp = default_resp(data) + ACC
    return resp

command_map = {
    b'\x06': (4, watchDogReset),
    b'\xA2': (4, get_PIU_housekeeping_data),
    b'\x16': (5, outputBusChannelOn),
    b'\x17': (5, outputBusChannelOff)
}

def parse_message(data):
    """Parse a message from the given byte buffer"""

    if len(data) < 4:
        print("Invalid message: ", data)
        return (default_resp(data) + REJ_MISS_PAR)

    cmd = data[2]
    print("Comand code: ", cmd.to_bytes(1, byteorder='little'))
    if cmd.to_bytes(1, byteorder='little') in command_map:
        length, handler = command_map[cmd.to_bytes(1, byteorder='little')]
        if len(data) != length:
            print("Invalid message, expected length ", length)
            return (default_resp(data) + REJ_MISS_PAR)
        print("Handling command")
        resp = handler(data)
        print("Response from handler: ", resp)
        return resp

    print("Invalid message, unknown command: ", cmd)
    return (default_resp(data) + REJ_INV_CODE)

def buildUartRsp(data):
    return b'<rsp>' + data + b'</rsp>'

#!/usr/bin/env python3
#
# Example for peripheral simulation for the USART test task on the isis-obc
# board.
#
# Copyright (c) 2019-2020 KSat e.V. Stuttgart
#
# This work is licensed under the terms of the GNU GPL, version 2 or, at your
# option, any later version. See the COPYING file in the top-level directory.

"""
Example framework for testing the USARTTestTask of the OBSW.

This file is intended to showcase how a USART device would be emulated in
python and connected to the QEMU emulator. The datastructures and functions in
this file should indicate what a simulation framework for the QEMU emulator
might look like.

Requirements:
  Python >= 3.7 (asyncio support)

Instructions:
  Run QEMU (modified for OBSW) via

  qemu-system-arm -M isis-obc -monitor stdio \
      -bios path/to/sourceobsw-at91sam9g20_ek-sdram.bin \
      -qmp unix:/tmp/qemu,server -S

  Then run this script.
"""

import time
import asyncio
import struct
import json
import re
import errno

# Paths to Unix Domain Sockets used by the emulator
QEMU_ADDR_QMP = '/tmp/qemu'
QEMU_ADDR_AT91_USART0 = '\0/tmp/qemu_at91_usart0'
QEMU_ADDR_AT91_USART2 = '\0/tmp/qemu_at91_usart2'

# Request/response category and command IDs
IOX_CAT_DATA = 0x01
IOX_CAT_FAULT = 0x02

IOX_CID_DATA_IN = 0x01
IOX_CID_DATA_OUT = 0x02

IOX_CID_FAULT_OVRE = 0x01
IOX_CID_FAULT_FRAME = 0x02
IOX_CID_FAULT_PARE = 0x03
IOX_CID_FAULT_TIMEOUT = 0x04


class QmpException(Exception):
    """An exception caused by the QML/QEMU as response to a failed command"""

    def __init__(self, ret, *args, **kwargs):
        Exception.__init__(self, f'QMP error: {ret}')
        self.ret = ret  # the 'return' structure provided by QEMU/QML


class QmpConnection:
    """A connection to a QEMU machine via QMP"""

    def __init__(self, addr=QEMU_ADDR_QMP):
        self.transport = None
        self.addr = addr
        self.dataq = asyncio.Queue()
        self.initq = asyncio.Queue()
        self.proto = None

    def _protocol(self):
        """The underlying transport protocol"""

        if self.proto is None:
            self.proto = QmpProtocol(self)

        return self.proto

    async def _wait_check_return(self):
        """
        Wait for the status return of a command and raise an exception if it
        indicates a failure
        """

        resp = await self.dataq.get()
        if resp['return']:
            raise QmpException(resp['return'])

    async def open(self):
        """
        Open this connection. Connect to the machine ensure that the
        connection is ready to use after this call.
        """

        loop = asyncio.get_event_loop()
        await loop.create_unix_connection(self._protocol, self.addr)

        # wait for initial capabilities and version
        init = await self.initq.get()
        print(init)

        # negotioate capabilities
        cmd = '{ "execute": "qmp_capabilities" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

        return self

    def close(self):
        """Close this connection"""

        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.proto = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()

    async def cont(self):
        """Continue machine execution if it has been paused"""

        cmd = '{ "execute": "cont" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

    async def stop(self):
        """Stop/pause machine execution"""

        cmd = '{ "execute": "stop" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()

    async def quit(self):
        """
        Quit the emulation. This causes the emulator to (non-gracefully)
        shut down and close.
        """

        cmd = '{ "execute": "quit" }'
        self.transport.write(bytes(cmd, 'utf-8'))
        await self._wait_check_return()


class QmpProtocol(asyncio.Protocol):
    """The QMP transport protocoll implementation"""

    def __init__(self, conn):
        self.conn = conn

    def connection_made(self, transport):
        self.conn.transport = transport

    def connection_lost(self, exc):
        self.conn.transport = None
        self.conn.proto = None

    def data_received(self, data):
        data = str(data, 'utf-8')
        decoder = json.JSONDecoder()
        nows = re.compile(r'[^\s]')

        pos = 0
        while True:
            match = nows.search(data, pos)
            if not match:
                return

            pos = match.start()
            obj, pos = decoder.raw_decode(data, pos)

            if 'return' in obj:
                self.conn.dataq.put_nowait(obj)
            elif 'QMP' in obj:
                self.conn.initq.put_nowait(obj)
            elif 'event' in obj:
                pass
            else:
                print("qmp:", obj)


class DataFrame:
    """
    Basic protocol unit for communication via the IOX API introduced for
    external device emulation
    """

    def __init__(self, seq, cat, id, data=None):
        self.seq = seq
        self.cat = cat
        self.id = id
        self.data = data

    def bytes(self):
        """Convert this protocol unit to raw bytes"""
        data = self.data if self.data is not None else []
        return bytes([self.seq, self.cat, self.id, len(data)]) + bytes(data)

    def __repr__(self):
        return f'{{ seq: 0x{self.seq:02x}, cat: 0x{self.cat:02x}, id: 0x{self.id:02x}, data: {self.data} }}'


def parse_dataframes(buf):
    """Parse a variable number of DataFrames from the given byte buffer"""

    while len(buf) >= 4 and len(buf) >= 4 + buf[3]:
        frame = DataFrame(buf[0], buf[1], buf[2], buf[4:4+buf[3]])
        buf = buf[4+buf[3]:]
        yield buf, frame

    return buf, None


class UsartStatusException(Exception):
    """An exception returned by the USART send command"""
    def __init__(self, errn, *args, **kwargs):
        Exception.__init__(self, f'USART error: {errno.errorcode[errn]}')
        self.errno = errn   # a UNIX error code indicating the reason


class Usart:
    """Connection to emulate a USART device for a given QEMU/At91 instance"""

    def __init__(self, addr):
        self.addr = addr
        self.respd = dict()
        self.respc = asyncio.Condition()
        self.dataq = asyncio.Queue()
        self.datab = bytes()
        self.transport = None
        self.proto = None
        self.seq = 0

    def _protocol(self):
        """The underlying transport protocol"""

        if self.proto is None:
            self.proto = UsartProtocol(self)

        return self.proto

    async def open(self):
        """Open this connection"""

        loop = asyncio.get_event_loop()
        await loop.create_unix_connection(self._protocol, self.addr)
        return self

    def close(self):
        """Close this connection"""

        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.proto = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()

    def _send_new_frame(self, cat, cid, data=None):
        """
        Send a DataFrame with the given parameters and auto-increase the
        sequence counter. Return its sequence number.
        """
        self.seq = (self.seq + 1) & 0x7f

        frame = DataFrame(self.seq, cat, cid, data)
        self.transport.write(frame.bytes())

        return frame.seq

    async def write(self, data):
        """Write data (bytes) to the USART device"""

        seq = self._send_new_frame(IOX_CAT_DATA, IOX_CID_DATA_IN, data)

        async with self.respc:
            while seq not in self.respd.keys():
                await self.respc.wait()

            resp = self.respd[seq]
            del self.respd[seq]

        status = struct.unpack('I', resp.data)[0]
        if status != 0:
            raise UsartStatusException(status)

    async def read(self, n):
        """Wait for 'n' bytes to be received from the USART"""

        while len(self.datab) < n:
            frame = await self.dataq.get()
            self.datab += frame.data

        data, self.datab = self.datab[:n], self.datab[n:]
        return data

    def inject_overrun_error(self):
        """Inject an overrun error (set CSR_OVRE)"""
        self._send_new_frame(IOX_CAT_FAULT, IOX_CID_FAULT_OVRE)

    def inject_frame_error(self):
        """Inject a frame error (set CSR_FRAME)"""
        self._send_new_frame(IOX_CAT_FAULT, IOX_CID_FAULT_FRAME)

    def inject_parity_error(self):
        """Inject a parity error (set CSR_PARE)"""
        self._send_new_frame(IOX_CAT_FAULT, IOX_CID_FAULT_PARE)

    def inject_timeout_error(self):
        """Inject a timeout (set CSR_TIMEOUT)"""
        self._send_new_frame(IOX_CAT_FAULT, IOX_CID_FAULT_TIMEOUT)


class UsartProtocol(asyncio.Protocol):
    """The USART transport protocoll implementation"""

    def __init__(self, conn):
        self.conn = conn
        self.buf = bytes()

    def connection_made(self, transport):
        self.conn.transport = transport

    def connection_lost(self, exc):
        self.conn.transport = None
        self.conn.proto = None

    def data_received(self, data):
        self.buf += data

        for buf, frame in parse_dataframes(self.buf):
            self.buf = buf

            if frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_DATA_OUT:
                # data from CPU/board to device
                self.conn.dataq.put_nowait(frame)
            elif frame.cat == IOX_CAT_DATA and frame.id == IOX_CID_DATA_IN:
                # response for data from device to CPU/board
                loop = asyncio.get_event_loop()
                loop.create_task(self._data_response_received(frame))

    async def _data_response_received(self, frame):
        async with self.conn.respc:
            self.conn.respd[frame.seq] = frame
            self.conn.respc.notify_all()

async def readCommand(usart):
    commandBuffer = bytes()
    while True:
        temp = await usart.read(1)
        commandBuffer += temp
        if len(commandBuffer) >= 15 and commandBuffer[-6:] == b'</cmd>':
            #find <cmd> in the buffer to return only the data
            init = commandBuffer.find(b'<cmd>')
            return commandBuffer[init+5:len(commandBuffer) - 6]


async def usart_task():
    # Instantiate the connection classes we need. The connections are not
    # opened automatically. Use '.open()' or 'async with machine as m'.
    #machine = QmpConnection()
    usart = Usart(QEMU_ADDR_AT91_USART0)

    # Open a connection to the QEMU emulator via QMP.
    #await machine.open()
    try:
        # Open the connection to the USART.
        await usart.open()
        print("pre cont")
        #await machine.cont()
        print("post cont")
        # Re-try until the USART has been set up by the OBSW. The USART will
        # return ENXIO until the receiver has been enabled. This may take a
        # bit. We just send it data until it indicates success.
        # TODO: Should we introduce an API to query the rx/tx-enabled status?
        while True:
            try:
                print("UART trying read")
                data = await readCommand(usart)
                print("UART Cmd: ", data)
                resp = parse_message(data)
                print("UART Resp: ", resp)
                builtRsp = buildUartRsp(resp)

                print("Uart trying write: ", builtRsp)

                await usart.write(builtRsp)
                #break
            except UsartStatusException as e:
                if e.errno == errno.ENXIO:
                    await asyncio.sleep(0.25)
                else:
                    raise e

        # Provide data for the USARTTestTask
        #for _ in range(4):
            #await usart.write(b'abcd')
            #print(await usart.read(6))

    finally:
        # Shutdown and close the emulator. Useful for automatic cleanup. We
        # don't want to leave any unsupervised QEMU instances running.
        #await machine.quit()

        # Close connections to USART and machine, if they are still open. The
        # quit command above should automatically close those, but let's be
        # explicit here.
        usart.close()
        #machine.close()



async def i2c_task():
    #machine = QmpConnection()
    dev = I2cSlave(QEMU_ADDR_AT91_TWI)

    #await machine.open()
    try:
        await dev.open()
        #await machine.cont()

        # Wait for start condition from master. We expect a write here.
        start = await dev.wait_start()
        while True:
            assert start.read is False

            # Read data from master until stop condition
            data = await dev.read()
            print(f"READ[0x{start.dadr:02x}]:  {data}")

            resp = parse_message(data)
            # There's an issue that the master does not recognize the response 
            #unless the status code gets the value NEW
            if resp[-1] == ACC:
                resp = resp[:-1] + NEW
                print("Resp: ", resp)

            # Wait for start condition from master. We expect a read here.
            # start = await dev.wait_start()
            # assert start.read is True

            # Write data to master.
            print(f"WRITE[0x{start.dadr:02x}]: {resp}")
            try:
                for i in range(0, 10):
                    print(i)
                    
                    await dev.write(resp)
                    time.sleep(1)

            except:
                print("Error writing")
                

            # Wait for stop condition from master.
            #await dev.wait_stop()

    finally:
        #await machine.quit()

        dev.close()
        #machine.close()

async def main():
    await asyncio.gather(usart_task(), i2c_task())

if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
        except:
            print("Error starting, sleeping 3 seconds")
            time.sleep(3)
            continue