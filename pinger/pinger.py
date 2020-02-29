#!/usr/bin/python3
import struct
import time
import math
import asyncio
from threading import Thread, Event
from queue import Queue, Empty
from typing import Dict, List, Any
from dataclasses import dataclass, field
import logging
import sys
import socket 
from socket import \
    socket as Socket, AF_INET, SOCK_RAW


class PingStatus:
    SUCCESS = True
    FAILURE = False


#region Ping result
@dataclass
class PingResult:

    # The status of the ping.
    # Either SUCCESS or FAILURE
    status : PingStatus = \
        field(default = PingStatus.FAILURE)

    # The target's hostname.
    hostname : str = field(default = None)

    # The address of the host that
    # received the ping.
    remoteAddress : str = field(default = None)

    # The ID of this ping
    packetId : int = field(default = -1)

    # The ping's delay.
    # Its value will be math.inf if
    # the ping received no pong.
    delay : int = field(default = math.inf)
#endregion


#region Ping wait handle
class PingWaitHandle(Event):
    """
    An event that can be waited.
    It carries a ping result.
    """

    def __init__(
        self, 
        defaultResult : PingResult):
        """
        Creates a new PingWaitHandle

        Parameters
        ----------
            defaultResult : PingResult
                The default result for the ping.
        """
        super().__init__()
        self.result = defaultResult
#endregion


#region Pinger
class Pinger:

    #region Constants

    ICMP_ECHO_REQUEST = 8
    BYTES_IN_DOUBLE   = struct.calcsize("d")

    #endregion

    #region Static fields

    _socket     : Socket = None
    _currentId  : int    = 0
    _pingsQueue : Queue  = Queue()
    _pings : \
        Dict[int, PingWaitHandle] = {}

    _pingingThread : Thread = None
    _pongingThread : Thread = None

    #endregion

    #region Get id
    @staticmethod 
    def _getId() -> int:
        """
        Returns
        -------
            int
                The current ping ID
        """
        Pinger._currentId += 1
        return Pinger._currentId
    #endregion


    #region Try init socket
    @staticmethod
    def _tryInitSocket() -> None:
        """
        Initializes the underlying ping socket,
        if not already initialized.
        """
        if not Pinger._socket:
            Pinger._socket = Socket(
                AF_INET, 
                SOCK_RAW, 
                socket.getprotobyname('icmp'))

            Pinger._socket.setblocking(False)
    #endregion

    #region Compute checksum
    @staticmethod
    def computeChecksum(payload : str) -> int:
        """
        Computes the checksum over a payload.

        Parameters
        ----------
            payload : str
                
        Returns
        -------
            int
                The computed checksum.
        """
        sum = 0
        maxCount = (len(payload) / 2) * 2
        count = 0
        while count < maxCount:
            val = \
                payload[count + 1] * 256 + payload[count]

            sum = sum + val
            sum = sum & 0xffffffff 
            count = count + 2
     
        if maxCount < len(payload):
            sum = sum + ord(payload[len(payload) - 1])
            sum = sum & 0xffffffff 
     
        sum = (sum >> 16)  +  (sum & 0xffff)
        sum = sum + (sum >> 16)
        result = ~sum
        result = result & 0xffff
        result = result >> 8 | (result << 8 & 0xff00)
        return result
    #endregion

    #region Build packet
    @staticmethod
    def _buildPacket(packetId : int) -> bytes:
        # Create a dummy header with a 0 checksum.
        header = struct.pack(
            "bbHHh", 
            Pinger.ICMP_ECHO_REQUEST, 
            0, 0, 
            packetId, 1)

        data = "abcdefghijklmnopqrstuvwxyz"
        data = struct.pack("d", time.time()) + bytes(data.encode('utf-8'))
     
        # Get the checksum on the data and the dummy header.
        checksum = \
            Pinger.computeChecksum(header + data)

        header = struct.pack(
            "bbHHh", 
            Pinger.ICMP_ECHO_REQUEST, 
            0, 
            socket.htons(checksum), 
            packetId, 1)
            
        packet = header + data
        return packet
    #endregion

    #region Try resolve hostname
    @staticmethod
    def tryResolveHostname(host : str) -> str:
        """
        Tries to resolve an hostname.

        Parameters
        ----------
            host : str
                The hostname that has to be resolved.

        Returns
        -------
            str or None
                A string with the resolved IPAddress
                or None if the resolution failed.
        """
        try:
            return \
                socket.gethostbyname(host)
        except:
            # Error when getting address
            # info
            return None
    #endregion

    
    #region Ping
    @staticmethod
    def ping(
        host    : str, 
        timeout : float = None,
        resolve : bool  = True) -> PingResult:
        """
        Executes the ping.

        Parameters
        ----------
            host : str
                The host to ping

            timeout : float = None
                The ping's timeout, in seconds.
                Leave None to wait indefinetely.

            resolve : bool = True
                Tells whether the host name should 
                be resolved via DNS or not.

        Returns
        -------
            PingResult
                An object containing the ping
                result informations.
        """
        Pinger._tryStartLoops()

        packetId  = Pinger._getId()
        badResult = PingResult(
            hostname      = host,
            remoteAddress = host, 
            packetId      = packetId)

        
        if resolve:
            host = \
                Pinger.tryResolveHostname(host)
        
        if host is None:
            return badResult

        # Scheduling the ping
        Pinger \
            ._pingsQueue \
            .put((host, packetId))

        pingWH = \
            PingWaitHandle(badResult)

        # Setting the event and default result
        Pinger._pings[packetId] = pingWH

        # Waiting for the result
        pingWH.wait(timeout)
        result = pingWH.result

        del Pinger._pings[packetId]
        return result
    #endregion

    #region Ping async
    @staticmethod
    async def pingAsync(
        host    : str, 
        timeout : float = None, 
        resolve : bool  = True) -> PingResult:
        """
        Executes the ping asynchronously.

        Parameters
        ----------
            host : str
                The host to ping

            timeout : float = 5
                The ping's timeout, in seconds.

            resolve : bool = True
                Tells whether the host name should 
                be resolved via DNS or not.

        Returns
        -------
            PingResult
                An object containing the ping
                result informations.
        """
        return Pinger.ping(host, timeout, resolve)
    #endregion

    #region Ping all 
    @staticmethod
    def pingAll(
        hosts   : List[str], 
        timeout : float = None, 
        resolve : bool  = True) -> List[PingResult]:
        """
        Pings all the hosts in a list.

        Parameters
        ----------
            hosts : List[str]
                The hosts to ping.
            
            timeout : float = 5
                The timeout for each host.
                Doesn't take into account DNS resolution.

            resolve : bool = True
                Tells whether to resolve the hostname or not.

        Returns
        -------
            List[PingResult]
                The ping results
        """
        return [
            Pinger.ping(host, timeout, resolve)
            for host in hosts
        ]
    #endregion

    #region Ping all async
    @staticmethod
    async def pingAllAsync(
        hosts   : List[str], 
        timeout : float = None, 
        resolve : bool  = True) -> List[PingResult]:
        """
        Pings all the hosts in a list asynchronously.

        Parameters
        ----------
            hosts : List[str]
                The hosts to ping.
            
            timeout : float = 5
                The timeout for each host.
                Doesn't take into account DNS resolution.

            resolve : bool = True
                Tells whether to resolve the hostname or not.

        Returns
        -------
            List[PingResult]
                The ping results, wrapped in an 
                awaitable object.
        """
        pings = [
            Pinger.pingAsync(host, timeout, resolve)
            for host in hosts
        ]

        return await \
            asyncio.gather(*pings)
    #endregion

    
    #region Pinging loop
    @staticmethod
    def _pingingLoop() -> None:
        """
        Takes care of pinging the hosts.
        It is run asynchronously inside a thread.
        """
        pq   = Pinger._pingsQueue
        sock = Pinger._socket 

        # pylint: disable=not-context-manager
        with sock:
            while True:
                try:
                    address, packetId = pq.get(True)

                    payload = \
                        Pinger._buildPacket(packetId)

                    sock.sendto(payload, (address, 0))

                except Exception:
                    pass
    #endregion

    #region Ponging loop
    @staticmethod
    def _pongingLoop() -> None:
        """
        Takes care of receiving the pongs.
        It is run asynchronously inside a thread.
        """
        sock = Pinger._socket

        # pylint: disable=not-context-manager
        with sock:
            while True:
                try:
                    payload, addr = sock.recvfrom(256)
                except:
                    # Non-blocking sockets raise
                    # exceptions if no data is in the buffer.
                    continue
                
                endTime = time.time()
                header  = payload[20:28]

                _, _, _,    \
                packetId,   \
                _ =         \
                    struct.unpack("bbHHh", header)
                
                roundTripTime = \
                    struct.unpack("d", payload[28:28 + Pinger.BYTES_IN_DOUBLE])[0]

                # If the ping was deregistered
                # due to timeout
                if not packetId in Pinger._pings:
                    continue

                pingWH = Pinger._pings[packetId]

                # Setting the ping result
                pingWH.result = PingResult(
                    hostname      = pingWH.result.hostname,
                    remoteAddress = addr[0],
                    status        = PingStatus.SUCCESS,
                    packetId      = packetId,
                    delay         = endTime - roundTripTime)
                
                # Setting the ping wait handle,
                # in order for it to complete
                pingWH.set()
    #endregion

    #region Try start loops
    @staticmethod 
    def _tryStartLoops():
        """
        Starts the pinging and ponging loops, 
        if not done already.
        """
        if not Pinger._pingingThread:
            Pinger._tryInitSocket()

            Pinger._pingingThread = Thread(
                name   = 'pinging-thread',
                target = Pinger._pingingLoop,
                daemon = True)

            Pinger._pongingThread = Thread(
                name   = 'ponging-thread',
                target = Pinger._pongingLoop,
                daemon = True)

            Pinger._pingingThread.start()
            Pinger._pongingThread.start()
    #endregion

#endregion


#region Main
async def main():
    """
    Pings each host passed from 
    command line
    """
    import colorama
    from argparse import ArgumentParser
    from colorama import Fore, Style
    
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(message)s")

    colorama.init(autoreset=True)

    #region Argparse init
    parser = ArgumentParser(
        description = "Pings the given hosts.")

    parser.add_argument('hosts',
        help  = 'A list of hosts to ping.',
        nargs = '+',
        type  = str)

    parser.add_argument('-t', 
        default = 5,
        help    = 'Sets the timeout for each ping (not for DNS resolution).',
        dest    = 'timeout',
        type    = float)

    parser.add_argument('--async',
        default = False,
        action  = 'store_true',
        help    = 'Execute each ping concurrently.',
        dest    = 'pingAsync')

    parser.add_argument('-n',
        default = 1,
        help    = 'The number of pings that are executed for each host.',
        dest    = 'repeat',
        type    = int)

    parser.add_argument('-r',
        default = False,
        action  = 'store_true',
        help    = 'Whether to resolve the hosts using the DNS or not.',
        dest    = 'resolve')

    parser.add_argument('-c',
        default = False,
        action  = 'store_true',
        help    = 'Ping indefinetely.',
        dest    = 'continuous')

    parser.add_argument('--delay',
        default = 1,
        help    = 'Delay between continuous or repeated pings. Default is 1 second.',
        dest    = 'delay',
        type    = float)
    #endregion

    try:
        args = parser.parse_args()
    except:
        return

    #region Log ping result
    def logPingResult(res : PingResult) -> None:
        """
        Logs a formatted representation of a ping
        """
        status = \
            (Fore.GREEN + f"{'online':7}") if res.status else (Fore.RED + "offline")

        status += Style.RESET_ALL

        msg = \
            f"{f'({res.hostname:^16}, {res.remoteAddress:^16})':32} -> {status} [delay: {res.delay}]"

        logging.info(msg)
    #endregion

    #region Exec pings
    async def execPings(sleep : bool = True):
        # Async ping
        if args.pingAsync:
            results = await Pinger.pingAllAsync(
                args.hosts,
                args.timeout,
                args.resolve)

        # Synchronous ping
        else:
            results = Pinger.pingAll(
                args.hosts,
                args.timeout,
                args.resolve)

        for res in results:
            logPingResult(res)

        if (args.continuous or args.repeat > 1) and sleep: 
            time.sleep(args.delay)
    #endregion


    try:
        while args.continuous:
            await execPings()

        for i in range(args.repeat):
            await execPings(i != (args.repeat - 1))
    except:
        return
#endregion


if __name__ == '__main__':
    try:
        asyncio.run(main())
        
    except OSError as e:
        if e.errno == 1:
            print("You must be a root user to run this script.")
        
        exit(1)
    except:
        exit(0)
