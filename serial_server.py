#!/usr/bin/env python3

# Serial server
# by Ivo Straka

# This file provides the SerialServer class, and can also be run directly.
# For help on arguments and configuration, run with --help

# A SerialServer object can be configured by passing arguments
# to the constructor, or by loading the parameters from a config file.
# The server has three states: closed <-> open <-> serving.
# When opening the server, the serial device and a listening socket are opened.
# Use server.open() or a with statement.
# Serving starts the loop that accepts clients and handles their queries.
# It is recommended to register the SIGINT and SIGTERM signals for graceful
# shutdown using server.register_signals()
# Config file can store multiple settings for multiple named devices.
# server.name then serves for loading only the corresponding config section.


import sys
import inspect
import configparser
from serial import Serial, PARITY_NAMES
from socket import socket, create_server, SHUT_RDWR
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE
from time import time, sleep
from contextlib import suppress
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from errno import EBUSY

import signal

### UTILITIY FUNCTIONS ###

# string to bytes
def parse_bytes(s):
    return s.encode() if isinstance(s, str) else bytes(s)

# method for getting the initial argument names of a class
def get_init_argnames(cls):
    specs = inspect.getfullargspec(cls.__init__)
    return specs.args[1:] + specs.kwonlyargs

# parse parity string into serial.PARITY_* constants
def parse_parity(par):
    par = par.casefold()
    for parity, parity_name in PARITY_NAMES.items():
        if par == parity.casefold() or par == parity_name.casefold():
            return parity

# parse a string to bool
def parse_bool(boo):
    boo = boo.lower()
    true_keys = ('true', 't', 'yes', 'y', '1')
    return (boo in true_keys)

# default string parsing that interprets escape sequences
def parse_str(s):
    return eval(f'"{s}"')

### CLASS DEFINITION ###

class SerialServer:

    def __init__(self,
            name='', host='', listen_port=None, ser_kwargs=dict(), *,
            eol_ser=b'\n', eol_sock=b'\n', selector_timeout=0.2):
        self._open = False # status indicator
        self._serving = False # status indicator
        self._shutdown_request = False # termination request indicator

        self.name = name # for config loading
        self.addr = (host, listen_port) # for listening socket
        self.eol_sock = parse_bytes(eol_sock) # end of line - sockets
        self.eol_ser = parse_bytes(eol_ser) # end of line - serial
        self.selector_timeout = selector_timeout # shutdown check period

        self.serial = None
        self.ser_kwargs = {'timeout': 1., 'exclusive': True} | ser_kwargs
        self.timeout = self.ser_kwargs['timeout']
        self.sock = None # listening socket
        self.sel = None # read/write selector
        self.ser_queue = [] # queue for the serial port
        self.clients = set() # connected client sockets


    # serial port timeout - important because the port is used in blocking mode
    @property
    def timeout(self):
        return self._timeout
    @timeout.setter
    def timeout(self, val):
        timeout = float(val)
        if timeout < 0.:
            raise ValueError(f"Timeout cannot be negative: {timeout}")
        if self.serial:
            self.serial.timeout = timeout
            self.serial.write_timeout = timeout
        self._timeout = timeout

    # initialisation and connection to ports
    # needs to correct for possibly inconsistent initial state
    # of the variables because of a previous failed opening
    def open(self):
        if not self._open:
            if self.sel is not None:
                self.sel.close()
            self.sel = DefaultSelector()
            if getattr(self.serial, 'is_open', False):
                self.serial.close()
            self.serial = None
            self.ser_queue.clear()
            exc = Exception()
            time_i = time()
            # Try to open the serial port with some grace period
            while time() < (time_i + self.timeout):
                try:
                    self.serial = Serial(**self.ser_kwargs)
                    break
                except OSError as e: # If busy, wait for availability
                    exc = e
                    if e.errno == EBUSY:
                        sleep(0.1)
            if not getattr(self.serial, 'is_open', False):
                print("Failed to open the serial port. Please check the "
                     f"configuration.\nself.serial: {self.serial}",
                     file=sys.stderr)
                raise exc
            self.sock = create_server(self.addr)
            self.sock.setblocking(False)
            self.sel.register(self.sock, EVENT_READ, None)
            self._open = True
        return self

    # Close connections and clean up queues
    def close(self):
        if self._serving:
            raise RuntimeError("Cannot perform a close on a running server;"
                               "call stop() first")
        if self._open:
            # don't raise exceptions if something is already closed
            with suppress(OSError):
                self.sel.close()
                self.sel = None
                self.serial.close()
                self.serial = None
                self.ser_queue.clear()
                self.sock.shutdown(SHUT_RDWR)
                self.sock.close()
                self.sock = None
                # should not have any clients listed; just in case
                for client in self.clients:
                    client.close()
                self.clients.clear()
            self._open = False

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type=None, exc_value=None, traceback=None):
        self.close()

    # stop serving by setting the flag
    def stop(self):
        if self._serving:
            self._shutdown_request = True

    # input/output queues for a client socket
    # is passed as a data structure to the selector
    class ClientBuffers():
        def __init__(self, sock):
            self.sock = sock
            self.in_buf = bytearray()
            self.out_buf = bytearray()

    # process socket I/O; accepts a selector key and an event mask
    def process_socket(self, key, mask):
        sock = key.fileobj
        data = key.data # ClientBuffers object
        if data is None: # listening socket
            if mask & EVENT_READ:
                s, _ = sock.accept() # get client socket
                self.clients.add(s)
                self.sel.register(s, EVENT_READ, data=self.ClientBuffers(s))
        elif mask & EVENT_READ: # client socket reading
            received = sock.recv(1024)
            if received:
                data.in_buf += received # append to previously received data
                chunks = data.in_buf.split(self.eol_sock) # split into queries
                data.in_buf = chunks.pop() # put any trailing data back
                if chunks: # if there are any whole queries left
                    # if the serial queue is empty, register the serial device
                    # for writing so the queue can be processed.
                    if not self.ser_queue:
                        self.sel.register(self.serial, EVENT_WRITE,
                                          data=self.ser_queue)
                    for cmd in chunks:
                        # submit a query and the corresponding ClientBuffers
                        self.ser_queue.append((cmd, data))
            else: # if empty bytes read, the socket is dead
                self.sel.unregister(sock)
                self.clients.remove(sock)
                sock.shutdown(SHUT_RDWR)
                sock.close()
        elif mask & EVENT_WRITE: # client socket writing
            sent = sock.send(data.out_buf)
            data.out_buf = data.out_buf[sent:] # keep data that was not sent
            if not data.out_buf:
                # everything sent; register for reading only
                self.sel.modify(sock, EVENT_READ, data=data)

    # process serial I/O; accepts a selector key and an event mask
    def process_serial(self, key, mask):
        ser = key.fileobj
        data = key.data # self.ser_queue
        if mask & EVENT_WRITE:
            cmd, clientbuf = data.pop(0) # get the query submitted first
            if not data: # if no queries remain, unregister for writing
                self.sel.unregister(ser)
            query = cmd + self.eol_ser
            while query: # write the whole query
                written = ser.write(query)
                query = query[written:]
            ser.flush()
            reply = ser.read_until(self.eol_ser) # blocking read
            reply = reply.replace(self.eol_ser, self.eol_sock)
            # if the socket output buffer is empty, register for writing
            if (not clientbuf.out_buf) and (clientbuf.sock in self.clients):
                self.sel.modify(clientbuf.sock, EVENT_READ | EVENT_WRITE,
                                data=clientbuf)
            clientbuf.out_buf.extend(reply)

    # main serving loop
    def serve_forever(self):
        if not self._open:
            self.open()
        self._serving = True
        try:
            while not self._shutdown_request:
                events = self.sel.select(self.selector_timeout)
                for key, mask in events:
                    if self._shutdown_request:
                        break
                    elif isinstance(key.fileobj, socket):
                        self.process_socket(key, mask)
                    elif isinstance(key.fileobj, Serial):
                        self.process_serial(key, mask)
        finally: # clean up at the end
            with suppress(OSError):
                for client in self.clients:
                    client.shutdown(SHUT_RDWR)
                    client.close()
            self.clients.clear()
            self._serving = False

    # signal handler to be called at any point in the program
    def handle_signals(self, signum, frame):
        if signum in (signal.SIGINT, signal.SIGTERM) and self._serving:
            print(f"\n{signal.Signals(signum).name} caught, waiting to exit...")
            self.stop()

    # use to register signals to be handled
    def register_signals(self, *signals):
        for sig in signals:
            signal.signal(sig, self.handle_signals)

    # functions for parsing config parameters
    parselib = {
        'listen_port': int,
        'selector_timeout': int,
        'baudrate': int,
        'bytesize': int,
        'parity': parse_parity,
        'stopbits': int,
        'timeout': float,
        'xonxoff': parse_bool,
        'rtscts': parse_bool,
        'dsrdtr': parse_bool,
        'write_timeout': float,
        'inter_byte_timeout': float,
        'exclusive': parse_bool,
    }

    # load configuration according to server name
    # if there is no name, load [DEFAULT] section from the config file
    def loadconfig(self, file='serial_server.conf'):
        if self._open:
            raise RuntimeError("Cannot configure server with open connections")
        cfg = configparser.ConfigParser()
        cfg.read(file)
        section = self.name if self.name else configparser.DEFAULTSECT
        cfgpars = cfg[section]

        initvars = get_init_argnames(self)
        serialvars = get_init_argnames(Serial)

        # collect all config parameters and distribute them into class arguments
        # and serial arguments
        ser_kwargs = dict()
        args = {'name': section, 'ser_kwargs': ser_kwargs}
        for par, val in cfgpars.items():
            parse = self.parselib.get(par, parse_str)
            if par in initvars:
                args[par] = parse(val)
            elif par in serialvars:
                ser_kwargs[par] = parse(val)

        self.__init__(**args)

### MAIN EXECUTION ###

if __name__ == '__main__':

    # argument parsing

    parser = ArgumentParser(
        formatter_class=RawDescriptionHelpFormatter,
        description="A network server that relays clients' queries to"
            " a serial device",
        epilog=
"""
INI config file syntax example:

[my_device_name]
listen_port = 4001
port = /dev/ttyS1
baudrate = 115200
bytesize = 8
stopbits = 1
parity = none
xonxoff = false
eol_ser = \\r
timeout = 0.5
# other devices or parameters...
"""
        )

    parser.add_argument('name', nargs='?', default='',
        help="device name to look for in the config file"
            f" (default loads [{configparser.DEFAULTSECT}])")

    parser.add_argument('-c', '--config', default='serial_server.conf',
        help="config file (INI format) specifying server and serial parameters"
            " (default: %(default)s)")

    args = parser.parse_args()

    # program body

    server = SerialServer(name=args.name) # declare empty server with a name
    server.loadconfig(args.config) # configure server based on its name
    with server: # context manager handles opening and closing
        server.register_signals(signal.SIGINT, signal.SIGTERM)
        server.serve_forever()