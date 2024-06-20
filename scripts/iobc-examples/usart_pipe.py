import asyncio
import struct
import json
import re
import errno
import threading

# Paths to Unix Domain Sockets used by the emulator
USART0_OBC = '\0/tmp/qemu_at91_usart0'
USART0_EMULATOR = '\0/tmp/qem2_at91_usart0'

# Request/response category and command IDs
IOX_CAT_DATA = 0x01
IOX_CAT_FAULT = 0x02

IOX_CID_DATA_IN = 0x01
IOX_CID_DATA_OUT = 0x02

IOX_CID_FAULT_OVRE = 0x01
IOX_CID_FAULT_FRAME = 0x02
IOX_CID_FAULT_PARE = 0x03
IOX_CID_FAULT_TIMEOUT = 0x04

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

    def __init__(self, name, addr):
        self.name = name
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

    async def read(self):
        result = b''
        while not self.dataq.empty():
            frame = await self.dataq.get()
            result += frame.data
        self.datab = result
        return result

    async def read_n(self, n):
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

async def forward_usart(source, target):
    while True:
        try:
            read = await source.read()
            if len(read) != 0:
                print("{} -> {} -> {}".format(source.name, read, target.name))
                await target.write(read)
            else:
                await asyncio.sleep(1)
        except UsartStatusException as e:
            if e.errno == errno.ENXIO:
                await asyncio.sleep(0.25)
            else:
                raise e

async def main():
    obc = Usart('obc', USART0_OBC)
    emulator = Usart('eps', USART0_EMULATOR)
    await obc.open()
    await emulator.open()

    print("Starting pipe...")
    try:
        await asyncio.gather(forward_usart(obc,emulator), forward_usart(emulator,obc))
    finally:
        obc.close()
        emulator.close()

if __name__ == '__main__':
    asyncio.run(main())

