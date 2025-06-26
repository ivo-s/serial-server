# Serial server

A simple Python network server for communication with a [PySerial](https://pyserial.readthedocs.io) device over sockets. Each client has an individual communication channel talking to the serial device.

Made for labs using equipment that communicates over serial ports using SCPI or similar delimited commands. Solves the problem of multi-client access and essentially demultiplexes all queries to the device. That way, you can run a logging script that periodically polls the device, and connect to it yourself in parallel.

Tested with Python 3.9+ on a GNU/Linux (POSIX) system.

## How to use

The easiest way is to set up a config file `serial_server.conf` in INI format. Sections correspond to device names. Then simply run `python serial_server.py my_device_name` as a service or in a terminal. Ctrl+C or a termination signal are used to quit. For help, run `python serial_server.py --help`. To communicate with the serial device, use network sockets or a client such as netcat; `nc localhost 4001`.

## Config file

Parameters:
* `host` - host address to listen at; default `''` is good for listening on every interface
* `listen_port` - pick an unused port that the clients can connect to
* `eol_ser` - the command delimiter that your serial device expects, such as `\n`, `\r`, or `\r\n`
* `eol_sock` - the delimiter for socket communication; ideally use the default `\n`
* `selector_timeout` - waiting time for I/O before checking for main loop termination; good default is `0.2` s
* all other parameters are passed to the [PySerial constructor `Serial()`](https://pyserial.readthedocs.io/en/latest/pyserial_api.html#serial.Serial)

Example:
```
[my_device_name]
listen_port = 4001
port = /dev/ttyS1
baudrate = 115200
bytesize = 8
stopbits = 1
parity = none
xonxoff = false
eol_ser = \r
timeout = 0.5
# more devices or parameters
```
