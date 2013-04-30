""" Sample resource consumption statistics for processes using psutil """

from operator import attrgetter
from time import time

from psutil import Process
from psutil.error import NoSuchProcess, AccessDenied, Error as PsutilError

from twitter.common import log

from .process import ProcessSample


def process_to_sample(process):
  """ Given a psutil.Process, return a current ProcessSample """
  try:
    # the nonblocking get_cpu_percent call is stateful on a particular Process object, and hence
    # >2 consecutive calls are required before it will return a non-zero value
    rate = process.get_cpu_percent(0.0) / 100.0
    user, system = process.get_cpu_times()
    rss, vms = process.get_memory_info()
    nice = process.nice
    status = process.status
    threads = process.get_num_threads()
    return ProcessSample(rate, user, system, rss, vms, nice, status, threads)
  except (AccessDenied, NoSuchProcess) as e:
    log.warning('Error during process sampling [pid=%s]: %s' % (process.pid, e))
    return ProcessSample.empty()


class ProcessTreeCollector(object):
  """ Collect resource consumption statistics for a process and its children """
  def __init__(self, pid):
    """ Given a pid """
    self._pid = pid
    self._process = None  # psutil.Process
    self._sampled_tree = {} # pid => ProcessSample
    self._sample = ProcessSample.empty()
    self._stamp = None
    self._rate = 0.0
    self._procs = 1

  def sample(self):
    """ Collate and aggregate ProcessSamples for process and children
        Returns None: result is stored in self.value
    """
    try:
      last_sample, last_stamp = self._sample, self._stamp
      if self._process is None:
        self._process = Process(self._pid)
      parent = self._process
      parent_sample = process_to_sample(parent)
      new_samples = dict(
          (proc.pid, process_to_sample(proc))
          for proc in parent.get_children(recursive=True)
      )
      new_samples[self._pid] = parent_sample

    except PsutilError as e:
      log.warning('Error during process sampling: %s' % e)
      self._sample = ProcessSample.empty()
      self._rate = 0.0

    else:
      last_stamp = self._stamp
      self._stamp = time()
      # for most stats, calculate simple sum to aggregate
      self._sample = sum(new_samples.values(), ProcessSample.empty())
      # cpu consumption is more complicated
      # We require at least 2 generations of a process before we can calculate rate, so for all
      # current processes that were not running in the previous sample, compare to an empty sample
      if self._sampled_tree and last_stamp:
        new = new_samples.values()
        old = [self._sampled_tree.get(pid, ProcessSample.empty()) for pid in new_samples.keys()]
        new_user_sys = sum(map(attrgetter('user'), new)) + sum(map(attrgetter('system'), new))
        old_user_sys = sum(map(attrgetter('user'), old)) + sum(map(attrgetter('system'), old))
        self._rate = (new_user_sys - old_user_sys) / (self._stamp - last_stamp)
        log.debug("Calculated rate for pid=%s and children: %s" % (self._process.pid, self._rate))
      self._sampled_tree = new_samples

  @property
  def value(self):
    """ Aggregated ProcessSample representing resource consumption of the tree """
    # Since we don't trust the CPU consumption returned by psutil, replace it with our own in the
    # exported ProcessSample
    return self._sample._replace(rate=self._rate)

  @property
  def procs(self):
    """ Number of active processes in the tree """
    return len(self._sampled_tree)

