#!/usr/bin/env python
import tango
import queue
import time
import datetime

from sardana import State, DataAccess
from sardana.pool import AcqSynch
from sardana.pool.controller import OneDController, Type, Access, \
    Description, DefaultValue, Memorize, Memorized, NotMemorized
from sardana.sardanavalue import SardanaValue
from sardana.tango.core.util import from_tango_state_to_state
from functools import wraps, partial
import six

def debug_it(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):

        self._log.debug("Entering {} with args={}, kwargs={}".format(
            func.__name__, args, kwargs))
        output = func(self, *args, **kwargs)
        self._log.debug("Leaving without error {} with output {}".format(func.__name__, output))
        return output
    return wrapper


def handle_error(func=None, msg="Error with DelayGeneratorCtrl"):
    if func is None:
        return partial(handle_error, msg=msg)
    else:
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                six.raise_from(RuntimeError(msg), e)
        return wrapper

class ListenerDataReady(object):

    def __init__(self, queue_obj, log=None):
        self.queue = queue_obj
        self.log = log

    def push_event(self, event):

        if not event.err:
            new_index = event.ctr - 1
            self.queue.put(new_index)
            if self.log:
                self.log.debug('DataReadyEvent number %r received', new_index)
        else:
            e = event.errors[0]
            msg = ('Event error (reason: %s; desc: %s)' % (e.reason, e.desc))
            self.log.error(msg)

            # TODO: analyze if raise an exception could crash the system
            # raise Exception('ListenerDataReady event with error')


class AdlinkAIOneDCtrl(OneDController):
    """
    This class is the Sardana CounterTimer controller for the Adlink adc
    based counters.

    The only way to use this controller is to define up to 5 channels and
    create a measurement group where the first channel is a master channel.
    The Adlink card works in a way where acquisition for all channels is
    started only once and in controller this is done when StartsAll()
    method was called for this controller, only when PreStartOne() was
    called for master channel.

    Configuration of Adlink card is done in LoadOne() method where size of
    acquisition buffer is calculated from acquisition time and SampleRate
    property.

    Value returned by a channggel is an average of buffer values. If you need
    also standard deviation of these values you can read it from extra
    attribute SD right after reading value of the channel.

    If you need SD value in measurement group you have two options:
       1- Add one tango attribute counter per each SD and place it in a
          measurement group after corresponding Adlink counter.
       2- Add an ExtraColumn with the attribute SD.
    """

    MaxDevice = 5

    ctrl_properties = {'AdlinkAIDeviceName': {Description: 'AdlinkAI Tango device',
                                         Type: str},
                  'SampleRate': {Description: 'SampleRate set for AIDevice',
                                 Type: int},
                  'SkipStart': {Description: 'Flag to skip if DS does not '
                                             'start',
                                Type: str,
                                DefaultValue: 'true'}}

    ctrl_attributes = {
        'PointsPerStep': {
            Type: int,
            Description: "Points to generate or Triggers to expect per step. \
                          Only applicable for the step scan. \
                          For multiple points per step.",
            Access: DataAccess.ReadWrite,
            Memorize: Memorized
        },
    }

    axis_attributes = {"SD":
                       {Type: float,
                        Description: 'Standard deviation',
                        Access: DataAccess.ReadWrite
                        },
                       "FORMULA":
                       {Type: str,
                        Description: 'The formula to get the real value.\n '
                                     'e.g. "(VALUE/10)*1e-06"',
                        Access: DataAccess.ReadWrite
                        },
                       "SHAREDFORMULA":
                       {Type: bool,
                        Description: 'If you want to share the same formula '
                                     'for all the channels set it to true"',
                        Access: DataAccess.ReadWrite
                        },
                       }

    @handle_error(msg="__init__: Unable to init!")
    def __init__(self, inst, props, *args, **kwargs):
        #        self._log.setLevel(logging.DEBUG)
        OneDController.__init__(self, inst, props, *args, **kwargs)
        self._log.debug("__init__(%s, %s): Entering...", repr(inst),
                        repr(props))

        self.AIDevice = tango.DeviceProxy(self.AdlinkAIDeviceName)
        cmdlist = [c.cmd_name for c in self.AIDevice.command_list_query()]
        if 'ClearBuffer' not in cmdlist:
            msg = ("__init__(): Looks like ADlink device server "
                    "version is too old for this controller version. "
                    "Please upgrade Device server\n")
            raise RuntimeError(msg)

        # TODO: Change the names of the variables to _name_without_capital_case
        self.sd = {}
        self.formulas = {}
        self.sharedFormula = {}
        self.intTime = 0
        self.dataBuff = {}

        self._apply_formulas = {}
        self._master_channel = None
        self._id_callback = None
        self._index_queue = queue.Queue()
        self._last_index_read = -1
        self._hw_state = None
        self._new_data = False
        self._state = State.On
        self._status = 'The Device is in ON.'
        self._synchronization = None
        self._repetitions = 0
        self._latency_time = 1e-6  # 1 us
        self._start_wait_time = 0.05
        self._skip_start = self.SkipStart.lower() == 'true'
        self._pointsperstep = 1

    @debug_it
    @handle_error(msg="_unsubcribe_data_ready: Unable to unsubscribe!")
    def _unsubcribe_data_ready(self):
        if self._id_callback is not None:
            self.AIDevice.unsubscribe_event(self._id_callback)
            self._id_callback = None

    @debug_it
    @handle_error(msg="_clean_acquisition: Unable to clear buffer!")
    def _clean_acquisition(self):
        self._last_index_read = -1
        self._repetitions = 0
        self._unsubcribe_data_ready()
        self._index_queue.__init__()
        self._master_channel = None
        self._new_data = False
        self.AIDevice.ClearBuffer()

    @debug_it
    @handle_error(msg="_stop_device: Unable to stop the device!")
    def _stop_device(self):
        self.StateAll()
        if self._hw_state != tango.DevState.STANDBY:
            # Randomly device may take more than 3 seconds to stop.
            # The probability raises when acquisitions are done frequently
            # step scan, frequent executions of ct, etc.
            # Temporarily set a higher timeout.
            self.AIDevice.set_timeout_millis(10000)
            self.AIDevice.stop()
            self.AIDevice.set_timeout_millis(3000)

    @debug_it
    def AddDevice(self, axis):
        self.sd[axis] = 0
        self.formulas[axis] = 'value'
        self._apply_formulas[axis] = False
        self.sharedFormula[axis] = False
        # buffer for the continuous scan
        self.dataBuff[axis] = []

    @debug_it
    def DeleteDevice(self, axis):
        self.sd.pop(axis)
        self.formulas.pop(axis)
        self.sharedFormula.pop(axis)
        self.dataBuff.pop(axis)
        self._unsubcribe_data_ready()

    @debug_it
    @handle_error(msg="StateAll: Unable to read state from the device!")
    def StateAll(self):
        self._hw_state = self.AIDevice.state()
        if self._hw_state == tango.DevState.RUNNING:
            self._state = State.Moving
            self._status = 'The Adlink is acquiring hehe'

        elif self._hw_state == tango.DevState.ON:
            self._state = State.On
            self._status = 'The Adlink is ready to acquire'
        else:
            self._state = from_tango_state_to_state(self._hw_state)
            self._status = 'The Adlink state is haha: %s' % self._hw_state

    @debug_it
    def StateOne(self, axis):
        return self._state, self._status

    @debug_it
    @handle_error(msg="StateAll: Unable to configure the device!")
    def LoadOne(self, axis, value, repetitions, latency):
        self._stop_device()
        self._clean_acquisition()

        self.intTime = value
        self._repetitions = repetitions

        sample_rate = self.AIDevice['SampleRate'].value
        chn_samp_per_trigger = int(self.intTime * sample_rate)

        if self._synchronization == AcqSynch.SoftwareTrigger:
            if value <= self._start_wait_time:
                msg = 'It is not possible to integrate less than %r in ' \
                      'software synchronization' % self._start_wait_time
                raise ValueError(msg)
            source = "SOFT"
        elif self._synchronization == AcqSynch.HardwareTrigger:
            source = "ExtD:+"
            if self._pointsperstep > 1:
                self._repetitions = self._pointsperstep
        else:
            raise ValueError("Adlink daq2005 allows only Software or "
                             "Hardware triggering")

        self.AIDevice["TriggerInfinite"] = 0
        self.AIDevice["TriggerSources"] = source
        self.AIDevice["NumOfTriggers"] = self._repetitions
        self.AIDevice['ChannelSamplesPerTrigger'] = chn_samp_per_trigger

    @debug_it
    def PreStartOne(self, axis, value=None):
        if axis != 1:
            self._master_channel = axis
        return True
    
    @debug_it
    @handle_error(msg="StartAll: Unable to start acquisition on the device!")
    def StartAll(self):
        """
        Starting the acquisition is done only if before was called
        PreStartOne for master channel.
        """

        if self._synchronization == AcqSynch.HardwareTrigger:
            attr_name = 'C0%s_MeanValues' % (self._master_channel - 2)
            cb = ListenerDataReady(self._index_queue, log=self._log)
            event_type = tango.EventType.DATA_READY_EVENT
            self._id_callback = self.AIDevice.subscribe_event(attr_name,
                                                              event_type, cb)

        # AdlinkAI Tango device has two aleatory bugs:
        # * Start command changes state to ON without passing through RUNNING
        # * Start command changes state to RUNNING after a while
        # For these reasons we either wait or retry 3 times the Start command.
        self.AIDevice.set_timeout_millis(15000)
        for i in range(1, 4):
            self._log.debug('StartAll: Try to start AIDevice: times ...%r'
                            % i)
            self.AIDevice.start()
            time.sleep(self._start_wait_time)
            self.StateAll()
            if self._hw_state == tango.DevState.RUNNING:
                break
            self._log.debug('StartAll: stopping AIDevice')
            self._stop_device()
        self.AIDevice.set_timeout_millis(3000)

        if self._hw_state != tango.DevState.RUNNING:
            if not self._skip_start:
                raise Exception('Could not start acquisition')
    
    @debug_it
    def StartOne(self, axis, value=None):	
        pass

    @debug_it
    @handle_error(msg="ReadAll: Could not read from the device!")
    def ReadAll(self):
        self._new_data = True
        if self._synchronization == AcqSynch.SoftwareTrigger:
            if self._hw_state != tango.DevState.ON:
                self._new_data = False
                return

            for axis in self.dataBuff.keys():
                if axis == 1:
                    self.dataBuff[axis] = [self.intTime]
                else:
                    mean_attr = "C0%s_MeanLast" % (axis - 2)
                    std_attr = "C0%s_StdDevLast" % (axis - 2)
                    mean = self.AIDevice[mean_attr].value
                    self.sd[axis] = self.AIDevice[std_attr].value
                    if self._apply_formulas[axis]:
                        formula = self.formulas[axis]
                        mean = eval(formula, {'value': mean})
                    self.dataBuff[axis] = [mean]

        elif self._synchronization == AcqSynch.HardwareTrigger:
            new_index = self._last_index_read
            if self._hw_state == tango.DevState.ON:
                self._log.debug('ReadAll HW Synch: Adlinkg State ON')
                new_index = self._repetitions-1
            else:

                # Read last index received by the data ready event
                try:
                    while self._index_queue.get() >= self._iterations:
                        data_ready_index = self._index_queue.get()
                        if data_ready_index > new_index:
                            new_index = data_ready_index
                except Exception as e:
                    print(e)

            if new_index == self._last_index_read:
                self._new_data = False
                return

            self._last_index_read += 1
            self._log.debug('ReadAll HW Synch: reading indexes [%r, %r]',
                            self._last_index_read, new_index)

            for axis in self.dataBuff.keys():
                if axis == 1:
                    new_datas = (new_index - self._last_index_read) + 1
                    if new_index == 0:
                        new_datas = 1
                    self.dataBuff[axis] = [self.intTime] * new_datas
                else:
                    mean_attr = 'C0%s_MeanValues' % (axis - 2)
                    raw_data = self.AIDevice.getData(([self._last_index_read,
                                                       new_index], [mean_attr])
                                                     )
                    means = raw_data
                    if self._apply_formulas[axis]:
                        formula = self.formulas[axis]
                        means = eval(formula, {'value': raw_data})
                    self.dataBuff[axis] = means.tolist()

            self._last_index_read = new_index

    @debug_it
    def ReadOne(self, axis):
        if self._synchronization == AcqSynch.SoftwareTrigger:
            if not self._new_data:
                raise Exception("Acquisition did not finish correctly. Adlink "
                                "State %r" % self._hw_state)
            return [self.dataBuff[axis][0]]

        elif self._synchronization == AcqSynch.HardwareTrigger:
            if not self._new_data:
                return []
            else:
                return [self.dataBuff[axis]]
        else:
            raise Exception("Unknown synchronization mode.")

    @debug_it
    @handle_error(msg="ReadAll: Could not stop the device!")
    def AbortOne(self, axis):
        self.StateAll()
        if self._hw_state != tango.DevState.STANDBY:
            self.AIDevice.stop()
        self._clean_acquisition()

    def GetAxisExtraPar(self, axis, name):
        name = name.lower()
        if name == "sd":
            return self.sd[axis]
        elif name == "formula":
            return self.formulas[axis]
        elif name == "sharedformula":
            return self.sharedFormula[axis]

    def SetAxisExtraPar(self, axis, name, value):
        name = name.lower()
        if name == "formula":
            formula = value.lower()
            self.formulas[axis] = formula
            self._apply_formulas[axis] = False
            if formula != 'value' or formula != '(value)':
                self._apply_formulas[axis] = True
        elif name == "sharedformula":
            self.sharedFormula[axis] = value
            if value:
                for i in self.formulas:
                    self.formulas[i] = self.formulas[axis]

    def GetPar(self, name):
        name = name.lower()
        if name == "pointspertrigger":
            return self._pointsperstep

    def SetPar(self, name, value):
        name = name.lower()
        if name == "pointspertrigger":
            self._pointsperstep = value