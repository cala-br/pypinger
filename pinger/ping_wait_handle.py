class PingWaitHandle(Event):
  """
  An event that can be waited.
  It carries a ping result.
  """

  def __init__(self, defaultResult: PingResult):
    super().__init__()
    self.result = defaultResult