# This file is part of nvitop, the interactive NVIDIA-GPU process viewer.
#
# Copyright 2022 Xuehai Pan. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Resource metrics collectors."""

import contextlib
import itertools
import math
import os
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Callable, Dict, Hashable, Iterable, List, NamedTuple, Optional, Tuple, Union
from weakref import WeakSet

from nvitop.api import host
from nvitop.api.device import CudaDevice, Device
from nvitop.api.process import GpuProcess, HostProcess
from nvitop.api.utils import GiB, MiB, Snapshot


__all__ = ['take_snapshots', 'collect_in_background', 'ResourceMetricCollector']


class SnapshotResult(NamedTuple):  # pylint: disable=missing-class-docstring
    devices: List[Snapshot]
    gpu_processes: List[Snapshot]


timer = time.monotonic


def _unique(iterable: Iterable[Hashable]) -> List[Hashable]:
    return list(OrderedDict.fromkeys(iterable).keys())


# pylint: disable-next=too-many-branches
def take_snapshots(
    devices: Optional[Union[Device, Iterable[Device]]] = None,
    *,
    gpu_processes: Optional[Union[bool, GpuProcess, Iterable[GpuProcess]]] = None,
) -> SnapshotResult:
    """Retrieve status of demanded devices and GPU processes.

    Args:
        devices (Optional[Union[Device, Iterable[Device]]]):
            Requested devices for snapshots. If not given, the devices will be determined from GPU
            processes: **(1)** All devices (no GPU processes are given); **(2)** Devices that used
            by given GPU processes.
        gpu_processes (Optional[Union[bool, GpuProcess, Iterable[GpuProcess]]]):
            Requested GPU processes snapshots. If not given, all GPU processes running on the
            requested device will be returned. The GPU process snapshots can be suppressed by
            specifying ``gpu_processes=False``.

    Returns: SnapshotResult
        A named tuple containing two lists of snapshots.

    Note:
        If not arguments are specified, all devices and all GPU processes will
        be returned.

    Examples:
        >>> from nvitop import take_snapshots, Device
        >>> import os
        >>> os.environ['CUDA_VISIBLE_DEVICES'] = '1,0'

        >>> take_snapshots()  # equivalent to `take_snapshots(Device.all())`
        SnapshotResult(
            devices=[
                PhysicalDeviceSnapshot(
                    real=PhysicalDevice(index=0, ...),
                    ...
                ),
                ...
            ],
            gpu_processes=[
                GpuProcessSnapshot(
                    real=GpuProcess(pid=xxxxxx, device=PhysicalDevice(index=0, ...), ...),
                    ...
                ),
                ...
            ]
        )

        >>> device_snapshots, gpu_process_snapshots = take_snapshots(Device.all())  # type: Tuple[List[DeviceSnapshot], List[GpuProcessSnapshot]]

        >>> device_snapshots, _ = take_snapshots(gpu_processes=False)  # ignore process snapshots

        >>> take_snapshots(Device.cuda.all())  # use CUDA device enumeration
        SnapshotResult(
            devices=[
                CudaDeviceSnapshot(
                    real=CudaDevice(cuda_index=0, physical_index=1, ...),
                    ...
                ),
                CudaDeviceSnapshot(
                    real=CudaDevice(cuda_index=1, physical_index=0, ...),
                    ...
                ),
            ],
            gpu_processes=[
                GpuProcessSnapshot(
                    real=GpuProcess(pid=xxxxxx, device=CudaDevice(cuda_index=0, ...), ...),
                    ...
                ),
                ...
            ]
        )

        >>> take_snapshots(Device.cuda(1))  # <CUDA 1> only
        SnapshotResult(
            devices=[
                CudaDeviceSnapshot(
                    real=CudaDevice(cuda_index=1, physical_index=0, ...),
                    ...
                )
            ],
            gpu_processes=[
                GpuProcessSnapshot(
                    real=GpuProcess(pid=xxxxxx, device=CudaDevice(cuda_index=1, ...), ...),
                    ...
                ),
                ...
            ]
        )
    """  # pylint: disable=line-too-long
    if isinstance(devices, Device):
        devices = [devices]
    if isinstance(gpu_processes, GpuProcess):
        gpu_processes = [gpu_processes]

    if gpu_processes is not None:
        if gpu_processes:  # is not False or is a non-empty list/tuple
            gpu_processes = list(gpu_processes)
            process_devices = _unique(process.device for process in gpu_processes)
            for device in process_devices:
                device.processes()  # update GPU status for requested GPU processes
            if devices is None:
                devices = process_devices
        else:
            gpu_processes = []  # False or empty list/tuple
            if devices is None:
                devices = Device.all()
    else:
        if devices is None:
            physical_devices = Device.all()
            devices = []
            leaf_devices = []
            for physical_device in physical_devices:
                devices.append(physical_device)
                mig_devices = physical_device.mig_devices()
                if len(mig_devices) > 0:
                    devices.extend(mig_devices)
                    leaf_devices.extend(mig_devices)
                else:
                    leaf_devices.append(physical_device)
        else:
            leaf_devices = devices = list(devices)
        gpu_processes = list(
            itertools.chain.from_iterable(device.processes().values() for device in leaf_devices)
        )

    devices = [device.as_snapshot() for device in devices]
    gpu_processes = GpuProcess.take_snapshots(gpu_processes, failsafe=True)

    return SnapshotResult(devices, gpu_processes)


# pylint: disable-next=too-many-arguments
def collect_in_background(
    on_collect: Callable[[Dict[str, float]], bool],
    collector: Optional['ResourceMetricCollector'] = None,
    interval: Optional[float] = None,
    *,
    on_start: Optional[Callable[['ResourceMetricCollector'], None]] = None,
    on_stop: Optional[Callable[['ResourceMetricCollector'], None]] = None,
    tag: str = 'metrics-daemon',
    start: bool = True,
) -> threading.Thread:
    """Start a background daemon thread that collect and call the callback function periodically.

    See also :func:`ResourceMetricCollector.daemonize`.

    Args:
        on_collect (Callable[[Dict[str, float]], bool]):
            A callback function that will be called periodically. It takes a dictionary containing
            the resource metrics and returns a boolean indicating whether to continue monitoring.
        collector (Optional[ResourceMetricCollector]):
            A :class:`ResourceMetricCollector` instance to collect metrics. If not given, it will
            collect metrics for all GPUs and subprocess of the current process.
        interval (Optional[float]):
            The collect interval. If not given, use ``collector.interval``.
        on_start (Optional[Callable[[ResourceMetricCollector], None]]):
            A function to initialize the daemon thread and collector.
        on_stop (Optional[Callable[[ResourceMetricCollector], None]]):
            A function that do some necessary cleanup after the daemon thread is stopped.
        tag (str):
            The tag prefix used for metrics results.
        start (bool):
            Whether to start the daemon thread on return.

    Returns: threading.Thread
        A daemon thread object.

    Examples:
        .. code-block:: python

            logger = ...

            def on_collect(metrics):  # will be called periodically
                if logger.is_closed():  # closed manually by user
                    return False
                logger.log(metrics)
                return True

            def on_stop(collector):  # will be called only once at stop
                if not logger.is_closed():
                    logger.close()  # cleanup

            # Record metrics to the logger in background every 5 seconds.
            # It will collect 5-second mean/min/max for each metric.
            collect_in_background(
                on_collect,
                ResourceMetricCollector(Device.cuda.all()),
                interval=5.0,
                on_stop=on_stop,
            )
    """
    if collector is None:
        collector = ResourceMetricCollector()
    if isinstance(interval, (int, float)) and interval > 0:
        interval = float(interval)
    elif interval is None:
        interval = collector.interval
    else:
        raise ValueError(f'Invalid argument interval={interval!r}')
    interval = min(interval, collector.interval)

    def target():
        if on_start is not None:
            on_start(collector)
        try:
            with collector(tag):
                try:
                    next_snapshot = timer() + interval
                    while on_collect(collector.collect()):
                        time.sleep(max(0.0, next_snapshot - timer()))
                        next_snapshot += interval
                except KeyboardInterrupt:
                    pass
        finally:
            if on_stop is not None:
                on_stop(collector)

    daemon = threading.Thread(target=target, name=tag, daemon=True)
    daemon.collector = collector
    if start:
        daemon.start()
    return daemon


class ResourceMetricCollector:  # pylint: disable=too-many-instance-attributes
    """A class for collecting resource metrics.

    Args:
        devices (Iterable[Device]):
            Set of Device instances for logging. If not given, all physical devices on board will be
            used.
        root_pids (Set[int]):
            A set of PIDs, only the status of the descendant processes on the GPUs will be collected.
            If not given, the PID of the current process will be used.
        interval (float):
            The snapshot interval for background daemon thread.

    Core methods:

    .. code-block:: python

        collector.activate(tag='<tag>')  # alias: start
        collector.deactivate()           # alias: stop
        collector.reset(tag='<tag>')
        collector.collect()

        with collector(tag='<tag>'):
            ...

        collector.daemonize(on_collect_fn)

    Examples:
        >>> import os
        >>> os.environ['CUDA_VISIBLE_DEVICES'] = '3,2,1,0'

        >>> from nvitop import ResourceMetricCollector, Device

        >>> collector = ResourceMetricCollector()                           # log all devices and descendant processes of the current process on the GPUs
        >>> collector = ResourceMetricCollector(root_pids={1})              # log all devices and all GPU processes
        >>> collector = ResourceMetricCollector(devices=Device.cuda.all())  # use the CUDA ordinal

        >>> with collector(tag='<tag>'):
        ...     # Do something
        ...     collector.collect()  # -> Dict[str, float]
        # key -> '<tag>/<scope>/<metric (unit)>/<mean/min/max>'
        {
            '<tag>/host/cpu_percent (%)/mean': 8.967849777683456,
            '<tag>/host/cpu_percent (%)/min': 6.1,
            '<tag>/host/cpu_percent (%)/max': 28.1,
            ...,
            '<tag>/host/memory_percent (%)/mean': 21.5,
            '<tag>/host/swap_percent (%)/mean': 0.3,
            '<tag>/host/memory_used (GiB)/mean': 91.0136418208109,
            '<tag>/host/load_average (%) (1 min)/mean': 10.251427386878328,
            '<tag>/host/load_average (%) (5 min)/mean': 10.072539414569503,
            '<tag>/host/load_average (%) (15 min)/mean': 11.91126970422139,
            ...,
            '<tag>/cuda:0 (gpu:3)/memory_used (MiB)/mean': 3.875,
            '<tag>/cuda:0 (gpu:3)/memory_free (MiB)/mean': 11015.562499999998,
            '<tag>/cuda:0 (gpu:3)/memory_total (MiB)/mean': 11019.437500000002,
            '<tag>/cuda:0 (gpu:3)/memory_percent (%)/mean': 0.0,
            '<tag>/cuda:0 (gpu:3)/gpu_utilization (%)/mean': 0.0,
            '<tag>/cuda:0 (gpu:3)/memory_utilization (%)/mean': 0.0,
            '<tag>/cuda:0 (gpu:3)/fan_speed (%)/mean': 22.0,
            '<tag>/cuda:0 (gpu:3)/temperature (C)/mean': 25.0,
            '<tag>/cuda:0 (gpu:3)/power_usage (W)/mean': 19.11166264116916,
            ...,
            '<tag>/cuda:1 (gpu:2)/memory_used (MiB)/mean': 8878.875,
            ...,
            '<tag>/cuda:2 (gpu:1)/memory_used (MiB)/mean': 8182.875,
            ...,
            '<tag>/cuda:3 (gpu:0)/memory_used (MiB)/mean': 9286.875,
            ...,
            '<tag>/pid:12345/host/cpu_percent (%)/mean': 151.34342772112265,
            '<tag>/pid:12345/host/host_memory (MiB)/mean': 44749.72373447514,
            '<tag>/pid:12345/host/host_memory_percent (%)/mean': 8.675082352111717,
            '<tag>/pid:12345/host/running_time (min)': 336.23803206741576,
            '<tag>/pid:12345/cuda:1 (gpu:4)/gpu_memory (MiB)/mean': 8861.0,
            '<tag>/pid:12345/cuda:1 (gpu:4)/gpu_memory_percent (%)/mean': 80.4,
            '<tag>/pid:12345/cuda:1 (gpu:4)/gpu_memory_utilization (%)/mean': 6.711118172407917,
            '<tag>/pid:12345/cuda:1 (gpu:4)/gpu_sm_utilization (%)/mean': 48.23283397736476,
            ...,
            '<tag>/duration (s)': 7.247399162035435,
            '<tag>/timestamp': 1655909466.9981883
        }
    """  # pylint: disable=line-too-long

    DEVICE_METRICS = [
        # (<attribute>, <name>, <unit>)
        # GPU memory metrics
        ('memory_used', 'memory_used (MiB)', MiB),
        ('memory_free', 'memory_free (MiB)', MiB),
        ('memory_total', 'memory_total (MiB)', MiB),
        ('memory_percent', 'memory_percent (%)', 1.0),
        # GPU utilization metrics
        ('gpu_utilization', 'gpu_utilization (%)', 1.0),
        ('memory_utilization', 'memory_utilization (%)', 1.0),
        # Miscellaneous
        ('fan_speed', 'fan_speed (%)', 1.0),
        ('temperature', 'temperature (C)', 1.0),
        ('power_usage', 'power_usage (W)', 1000.0),
    ]

    PROCESS_METRICS = [
        # (<attribute>, <scope>, <name>, <unit>)
        # Host resource metrics
        ('cpu_percent', 'host', 'cpu_percent (%)', 1.0),
        ('host_memory', 'host', 'host_memory (MiB)', MiB),
        ('host_memory_percent', 'host', 'host_memory_percent (%)', 1.0),
        ('running_time_in_seconds', 'host', 'running_time (min)', 60.0),
        # GPU memory metrics
        ('gpu_memory', None, 'gpu_memory (MiB)', MiB),
        ('gpu_memory_percent', None, 'gpu_memory_percent (%)', 1.0),
        ('gpu_memory_utilization', None, 'gpu_memory_utilization (%)', 1.0),
        # GPU utilization metrics
        ('gpu_sm_utilization', None, 'gpu_sm_utilization (%)', 1.0),
    ]

    def __init__(
        self,
        devices: Optional[Iterable[Device]] = None,
        root_pids: Optional[Iterable[int]] = None,
        interval: Union[int, float] = 1.0,
    ) -> None:
        """Initialize the resource metric collector."""
        if isinstance(interval, (int, float)) and interval > 0:
            interval = float(interval)
        else:
            raise ValueError(f'Invalid argument interval={interval!r}')

        if devices is None:
            devices = Device.all()

        if root_pids is None:
            root_pids = {os.getpid()}
        else:
            root_pids = set(root_pids)

        self.interval = interval

        self.devices = list(devices)
        self.all_devices = []
        self.leaf_devices = []
        for device in self.devices:
            self.all_devices.append(device)
            mig_devices = device.mig_devices()
            if len(mig_devices) > 0:
                self.all_devices.extend(mig_devices)
                self.leaf_devices.extend(mig_devices)
            else:
                self.leaf_devices.append(device)

        self.root_pids = root_pids
        self._positive_processes = WeakSet(HostProcess(pid) for pid in self.root_pids)
        self._negative_processes = WeakSet()

        self._last_timestamp = timer() - 2.0 * self.interval
        self._lock = threading.RLock()
        self._metric_buffer = None
        self._tags = set()

        self._daemon = threading.Thread(
            name='gpu_metric_collector_daemon', target=self._target, daemon=True
        )
        self._daemon_running = threading.Event()

    def activate(self, tag: str) -> 'ResourceMetricCollector':
        """Start a new metric collection with the given tag.

        Args:
            tag (str):
                The name of the new metric collection. The tag will be used to identify the metric
                collection. It must be a unique string.

        Examples:
            >>> collector = ResourceMetricCollector()

            >>> collector.activate(tag='train')  # key prefix -> 'train'
            >>> collector.activate(tag='batch')  # key prefix -> 'train/batch'
            >>> collector.deactivate()           # key prefix -> 'train'
            >>> collector.deactivate()           # the collector has been stopped
            >>> collector.activate(tag='test')   # key prefix -> 'test'
        """
        with self._lock:
            if self._metric_buffer is None or tag not in self._tags:
                self._tags.add(tag)
                self._metric_buffer = _MetricBuffer(tag, self, prev=self._metric_buffer)
                self._last_timestamp = timer() - 2.0 * self.interval
            else:
                raise RuntimeError(f'Resource metric collector is already started with tag "{tag}"')

        self._daemon_running.set()
        try:
            self._daemon.start()
        except RuntimeError:
            pass

        return self

    start = activate

    def deactivate(self, tag: Optional[str] = None) -> 'ResourceMetricCollector':
        """Stop the current collection with the given tag and remove all sub-tags.

        If the tag is not specified, deactivate the current active collection. For nested
        collections, the sub-collections will be deactivated as well.

        Args:
            tag (Optional[str]):
                The tag to deactivate. If :data:`None`, the current active collection will be used.
        """
        with self._lock:
            if self._metric_buffer is None:
                if tag is not None:
                    raise RuntimeError('Resource metric collector has not been not started yet.')
                return self

            if tag is None:
                tag = self._metric_buffer.tag
            elif tag not in self._tags:
                raise RuntimeError(
                    f'Resource metric collector has not been started with tag "{tag}".'
                )

            buffer = self._metric_buffer
            while True:
                self._tags.remove(buffer.tag)
                if buffer.tag == tag:
                    self._metric_buffer = buffer.prev
                    break
                buffer = buffer.prev

            if self._metric_buffer is None:
                self._daemon_running.clear()

        return self

    stop = deactivate

    @contextlib.contextmanager
    def context(self, tag: str) -> 'ResourceMetricCollector':
        """A context manager for starting and stopping resource metric collection.

        Args:
            tag (str):
                The name of the new metric collection. The tag will be used to identify the metric
                collection. It must be a unique string.

        Examples:
            >>> collector = ResourceMetricCollector()

            >>> with collector.context(tag='train'):  # key prefix -> 'train'
            ...     # Do something
            ...     collector.collect()  # -> Dict[str, float]
        """
        try:
            self.activate(tag=tag)
            yield self
        finally:
            self.deactivate(tag=tag)

    __call__ = context  # alias for `with collector(tag='<tag>')`

    def clear(self, tag: Optional[str] = None) -> None:
        """Reset the metric collection with the given tag.

        If the tag is not specified, reset the current active collection. For nested collections,
        the sub-collections will be reset as well.

        Args:
            tag (Optional[str]):
                The tag to reset. If :data:`None`, the current active collection will be reset.

        Examples:
            >>> collector = ResourceMetricCollector()

            >>> with collector(tag='train'):          # key prefix -> 'train'
            ...     time.sleep(5.0)
            ...     collector.collect()               # metrics within the 5.0s interval
            ...
            ...     time.sleep(5.0)
            ...     collector.collect()               # metrics within the cumulative 10.0s interval
            ...
            ...     collector.reset()                 # reset the active collection
            ...     time.sleep(5.0)
            ...     collector.collect()               # metrics within the 5.0s interval
            ...
            ...     with collector(tag='batch'):      # key prefix -> 'train/batch'
            ...         collector.reset(tag='train')  # reset both 'train' and 'train/batch'
        """
        with self._lock:
            if self._metric_buffer is None:
                if tag is not None:
                    raise RuntimeError('Resource metric collector has not been not started yet.')
                return

            if tag is None:
                tag = self._metric_buffer.tag
            elif tag not in self._tags:
                raise RuntimeError(
                    f'Resource metric collector has not been started with tag "{tag}".'
                )

            buffer = self._metric_buffer
            while True:
                buffer.clear()
                if buffer.tag == tag:
                    break
                buffer = buffer.prev

    def collect(self) -> Dict[str, float]:
        """Get the average resource consumption during collection."""
        with self._lock:
            if self._metric_buffer is None:
                raise RuntimeError('Resource metric collector has not been not started yet.')

            if timer() - self._last_timestamp > self.interval:
                self.take_snapshots()
            return self._metric_buffer.collect()

    # pylint: disable-next=too-many-arguments
    def daemonize(
        self,
        on_collect: Callable[[Dict[str, float]], bool],
        interval: Optional[float] = None,
        *,
        on_start: Optional[Callable[['ResourceMetricCollector'], None]] = None,
        on_stop: Optional[Callable[['ResourceMetricCollector'], None]] = None,
        tag: str = 'metrics-daemon',
        start: bool = True,
    ) -> threading.Thread:
        """Start a background daemon thread that collect and call the callback function periodically.

        See also :func:`collect_in_background`.

        Args:
            on_collect (Callable[[Dict[str, float]], bool]):
                A callback function that will be called periodically. It takes a dictionary containing
                the resource metrics and returns a boolean indicating whether to continue monitoring.
            interval (Optional[float]):
                The collect interval. If not given, use ``collector.interval``.
            on_start (Optional[Callable[[ResourceMetricCollector], None]]):
                A function to initialize the daemon thread and collector.
            on_stop (Optional[Callable[[ResourceMetricCollector], None]]):
                A function that do some necessary cleanup after the daemon thread is stopped.
            tag (str):
                The tag prefix used for metrics results.
            start (bool):
                Whether to start the daemon thread on return.

        Returns: threading.Thread
            A daemon thread object.

        Examples:
            .. code-block:: python

                logger = ...

                def on_collect(metrics):  # will be called periodically
                    if logger.is_closed():  # closed manually by user
                        return False
                    logger.log(metrics)
                    return True

                def on_stop(collector):  # will be called only once at stop
                    if not logger.is_closed():
                        logger.close()  # cleanup

                # Record metrics to the logger in background every 5 seconds.
                # It will collect 5-second mean/min/max for each metric.
                ResourceMetricCollector(Device.cuda.all()).daemonize(
                    on_collect,
                    ResourceMetricCollector(Device.cuda.all()),
                    interval=5.0,
                    on_stop=on_stop,
                )
        """
        return collect_in_background(
            on_collect,
            collector=self,
            interval=interval,
            on_start=on_start,
            on_stop=on_stop,
            tag=tag,
            start=start,
        )

    def __del__(self) -> None:
        """Clean up the demon thread on destruction."""
        self._daemon_running.clear()

    # pylint: disable-next=too-many-branches,too-many-locals,too-many-statements
    def take_snapshots(self) -> SnapshotResult:
        """Take snapshots of the current resource metrics and update the metric buffer."""
        if len(self.root_pids) > 0:
            all_gpu_processes = []
            for device in self.leaf_devices:
                all_gpu_processes.extend(device.processes().values())

            gpu_processes = []
            for process in all_gpu_processes:
                if process.host in self._negative_processes:
                    continue

                positive = True
                if process.host not in self._positive_processes:
                    positive = False
                    p = process.host
                    parents = []
                    while p is not None:
                        parents.append(p)
                        if p in self._positive_processes:
                            positive = True
                            break
                        try:
                            p = p.parent()
                        except host.PsutilError:
                            break
                    if positive:
                        self._positive_processes.update(parents)
                    else:
                        self._negative_processes.update(parents)

                if positive:
                    gpu_processes.append(process)
        else:
            gpu_processes = []

        timestamp = timer()
        metrics = {}
        devices = [device.as_snapshot() for device in self.all_devices]
        gpu_processes = GpuProcess.take_snapshots(gpu_processes, failsafe=True)

        metrics.update(
            {
                'host/cpu_percent (%)': host.cpu_percent(),
                'host/memory_percent (%)': host.memory_percent(),
                'host/swap_percent (%)': host.swap_percent(),
                'host/memory_used (GiB)': host.virtual_memory().used / GiB,
            }
        )
        load_average = host.load_average()
        if load_average is not None:
            metrics.update(
                {
                    'host/load_average (%) (1 min)': load_average[0],
                    'host/load_average (%) (5 min)': load_average[1],
                    'host/load_average (%) (15 min)': load_average[2],
                }
            )

        device_identifiers = {}
        for device in devices:
            identifier = f'gpu:{device.index}'
            if isinstance(device.real, CudaDevice):
                identifier = f'cuda:{device.cuda_index} ({identifier})'
            device_identifiers[device.real] = identifier

            for attr, name, unit in self.DEVICE_METRICS:
                value = float(getattr(device, attr)) / unit
                metrics[f'{identifier}/{name}'] = value

        for process in gpu_processes:
            device_identifier = device_identifiers[process.device]
            identifier = f'pid:{process.pid}'

            for attr, scope, name, unit in self.PROCESS_METRICS:
                scope = scope or device_identifier
                value = float(getattr(process, attr)) / unit
                metrics[f'{identifier}/{scope}/{name}'] = value

        with self._lock:
            if self._metric_buffer is not None:
                self._metric_buffer.add(metrics, timestamp=timestamp)
                self._last_timestamp = timestamp

        return SnapshotResult(devices, gpu_processes)

    def _target(self) -> None:
        self._daemon_running.wait()
        while self._daemon_running.is_set():
            self.take_snapshots()
            time.sleep(self.interval)


class _MetricBuffer:  # pylint: disable=missing-class-docstring,missing-function-docstring,too-many-instance-attributes
    def __init__(
        self, tag: str, collector: 'ResourceMetricCollector', prev: Optional['_MetricBuffer'] = None
    ) -> None:
        self.collector = collector
        self.prev = prev

        self.tag = tag
        if self.prev is not None:
            self.key_prefix = f'{self.prev.key_prefix}/{self.tag}'
        else:
            self.key_prefix = self.tag

        self.last_timestamp = self.start_timestamp = timer()
        self.buffer = defaultdict(lambda: _StatisticsMaintainer(self.last_timestamp))

        self.len = 0

    def add(self, metrics: Dict[str, float], timestamp: Optional[float] = None) -> None:
        if timestamp is None:
            timestamp = timer()

        for key in set(self.buffer).difference(metrics):
            self.buffer[key].add(math.nan, timestamp=timestamp)
        for key, value in metrics.items():
            self.buffer[key].add(value, timestamp=timestamp)
        self.len += 1
        self.last_timestamp = timestamp

        if self.prev is not None:
            self.prev.add(metrics, timestamp=timestamp)

    def clear(self) -> None:
        self.last_timestamp = self.start_timestamp = timer()
        self.buffer.clear()
        self.len = 0

    def collect(self) -> Dict[str, float]:
        metrics = {
            f'{self.key_prefix}/{key}/{name}': value
            for key, stats in self.buffer.items()
            for name, value in stats.items()
        }
        for key in tuple(metrics.keys()):
            if key.endswith('host/running_time (min)/max'):
                metrics[key[:-4]] = metrics[key]
                del metrics[key]
            elif key.endswith('host/running_time (min)/mean') or key.endswith(
                'host/running_time (min)/min'
            ):
                del metrics[key]
        metrics[f'{self.key_prefix}/duration (s)'] = timer() - self.start_timestamp
        metrics[f'{self.key_prefix}/timestamp'] = time.time()
        return metrics

    def __len__(self) -> int:
        return self.len


class _StatisticsMaintainer:  # pylint: disable=missing-class-docstring,missing-function-docstring
    def __init__(self, timestamp: float) -> None:
        self.start_timestamp = timestamp
        self.last_timestamp = None
        self.integral = None
        self.last_value = None
        self.min_value = None
        self.max_value = None
        self.has_nan = False

    def add(self, value: float, timestamp: Optional[float] = None) -> None:
        if timestamp is None:
            timestamp = timer()

        if math.isnan(value):
            self.has_nan = True
            return

        if self.last_value is None:
            self.integral = value * (timestamp - self.start_timestamp)
            self.last_value = self.min_value = self.max_value = value
        else:
            self.integral += (value + self.last_value) * (timestamp - self.last_timestamp) / 2.0
            self.last_value = value
            self.min_value = min(self.min_value, value)
            self.max_value = max(self.max_value, value)

        self.last_timestamp = timestamp

    def mean(self) -> float:
        if self.has_nan:
            if self.integral is None:
                return math.nan
            return self.integral / (self.last_timestamp - self.start_timestamp)

        timestamp = timer()
        integral = self.integral + self.last_value * (timestamp - self.last_timestamp)
        return integral / (timestamp - self.start_timestamp)

    def min(self) -> float:
        if self.has_nan and self.min_value is None:
            return math.nan
        return self.min_value

    def max(self) -> float:
        if self.has_nan and self.max_value is None:
            return math.nan
        return self.max_value

    def items(self) -> Iterable[Tuple[str, float]]:
        yield ('mean', self.mean())
        yield ('min', self.min())
        yield ('max', self.max())
