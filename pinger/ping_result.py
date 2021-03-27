from dataclasses import field
import math

class PingStatus:
  SUCCESS = True
  FAILURE = False


@dataclass
class PingResult:
  status : PingStatus = \
    field(default = PingStatus.FAILURE)

  hostname : str = field(default = None)
  remoteAddress : str = field(default = None)
  packetId : int = field(default = -1)
  delay : int = field(default = math.inf)