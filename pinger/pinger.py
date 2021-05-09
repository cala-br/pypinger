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

from pinger.ping_result import PingResult, PingStatus
from pinger.ping_wait_handle import PingWaitHandle


class Pinger:
  ICMP_ECHO_REQUEST = 8
  BYTES_IN_DOUBLE   = struct.calcsize("d")

  _socket     : Socket = None
  _currentId  : int    = 0
  _pingsQueue : Queue  = Queue()
  _pings : \
    Dict[int, PingWaitHandle] = {}

  _pingingThread : Thread = None
  _pongingThread : Thread = None


  @staticmethod 
  def _getId() -> int:
    Pinger._currentId += 1
    return Pinger._currentId


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
        socket.getprotobyname('icmp')
      )

      Pinger._socket.setblocking(False)


  @staticmethod
  def _buildPacket(packetId: int) -> bytes:
    header = \
      Pinger._getHeaderWithChecksum(packetId, checksum = 0)
    
    data = "abcdefghijklmnopqrstuvwxyz"
    data = struct.pack("d", time.time()) + bytes(data.encode('utf-8'))

    checksum = \
      Pinger.computeChecksum(header + data)

    header = \
      Pinger._getHeaderWithChecksum(packetId, socket.htons(checksum))
    
    packet = header + data
    return packet

  @staticmethod
  def computeChecksum(payload : str) -> int:
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

  @staticmethod
  def _getHeaderWithChecksum(packetId: int, checksum: int) -> bytes:
    return struct.pack(
      "bbHHh", 
      Pinger.ICMP_ECHO_REQUEST, 
      0, 
      checksum, 
      packetId, 1
    )


  @staticmethod
  def tryResolveHostname(host : str) -> Union[str, None]:
    """
    Returns
    -------
      str or None
        A string with the resolved IPAddress
        or None if the resolution failed.
    """
    try:
      return socket.gethostbyname(host)
    except:
      return None


  @staticmethod
  def ping(
    host    : str, 
    timeout : float = None,
    resolve : bool  = True) -> PingResult:
    """
    Parameters
    ----------
      timeout : float = None
        The ping's timeout, in seconds.
        Leave None to wait indefinetely.

      resolve : bool = True
        Tells whether the host name should 
        be resolved via DNS or not.
    """
    Pinger._tryStartLoops()

    packetId  = Pinger._getId()
    badResult = PingResult(
      hostname = host,
      remoteAddress = host, 
      packetId = packetId
    )

    if resolve:
      host = Pinger.tryResolveHostname(host)
    
    if host is None:
      return badResult

    Pinger._schedulePing(host, packetId)
    pingWH = \
      PingWaitHandle(badResult)

    # Setting the event and default result
    Pinger._pings[packetId] = pingWH

    # Waiting for the result
    pingWH.wait(timeout)
    result = pingWH.result

    del Pinger._pings[packetId]
    return result

  @staticmethod
  def _schedulePing(host: str, packetId: int):
    Pinger \
      ._pingsQueue \
      .put((host, packetId))


  @staticmethod
  async def pingAsync(
    host: str, 
    timeout: float = None, 
    resolve: bool = True
  ) -> PingResult:
    return Pinger.ping(host, timeout, resolve)


  @staticmethod
  def pingAll(
    hosts: List[str], 
    timeout: float = None, 
    resolve: bool = True
  ) -> List[PingResult]:
    return [
      Pinger.ping(host, timeout, resolve)
      for host in hosts
    ]


  @staticmethod
  async def pingAllAsync(
    hosts: List[str], 
    timeout: float = None, 
    resolve: bool = True
  ) -> List[PingResult]:
    pings = [
      Pinger.pingAsync(host, timeout, resolve)
      for host in hosts
    ]

    return await asyncio.gather(*pings)


  @staticmethod
  def _pingingLoop() -> None:
    """
    Takes care of pinging the hosts.
    It is run asynchronously inside a thread.
    """
    pq = Pinger._pingsQueue
    sock = Pinger._socket 

    # pylint: disable=not-context-manager
    with sock:
      while True:
        try:
          address, packetId = pq.get(True)
          payload = Pinger._buildPacket(packetId)

          sock.sendto(payload, (address, 0))
        except Exception:
          pass


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
        header = payload[20:28]

        _, _, _,  \
        packetId, \
        _ =       \
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
          hostname = pingWH.result.hostname,
          remoteAddress = addr[0],
          status = PingStatus.SUCCESS,
          packetId = packetId,
          delay = endTime - roundTripTime
        )
        
        # Setting the ping wait handle,
        # in order for it to complete
        pingWH.set()


  @staticmethod 
  def _tryStartLoops():
    """
    Starts the pinging and ponging loops, 
    if not done already.
    """
    if not Pinger._pingingThread:
      Pinger._tryInitSocket()

      def startThread(name, callback):
        result = Thread(
          name = name,
          target = callback,
          daemon = True
        )

        result.start()
        return result

      Pinger._pingingThread = startThread('pinging-thread', Pinger._pingingLoop)
      Pinger._pongingThread = startThread('ponging-thread', Pinger._pongingLoop)


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
