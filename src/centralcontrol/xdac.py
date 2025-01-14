#!/usr/bin/env python3

import zmq
import json
import pathlib
import logging
import numpy as np

class xdac(object):
  # zmq comms params
  topicfilter = b""
  req_port = 5555
  sub_port = 5556

  cal_file_name = "xdac_calibration.json"

  n_chans = 8

  # in mA
  min_current = 0
  max_current = 500

  min_voltage = -20
  max_voltage = 20

  cal_data = None

  def __init__(self, context, ip="169.254.38.99"):
    self.lg = logging.getLogger(self.__class__.__name__)
    ch = logging.StreamHandler()
    logFormat = logging.Formatter(("%(asctime)s|%(name)s|%(levelname)s|%(message)s"))
    ch.setFormatter(logFormat)
    self.lg.addHandler(ch)

    self.req_socket = context.socket(zmq.REQ)
    self.req_socket.connect(f"tcp://{ip}:{self.req_port}")
    self.sub_socket = context.socket(zmq.SUB)
    self.sub_socket.connect(f"tcp://{ip}:{self.sub_port}")
    self.sub_socket.setsockopt(zmq.SUBSCRIBE, self.topicfilter)

    # try to load calibration data
    plo = pathlib.Path(self.cal_file_name)
    if plo.is_file():
      with open(str(plo)) as fh:
        try:
          cal_data = json.load(fh)
        except Exception as e:
          raise(ValueError(f"Error loading {self.cal_file_name} as json"))
        if type(cal_data) != dict:
          raise(ValueError(f"{self.cal_file_name} has incorrect json format"))
      self.cal_data = cal_data

  # setChannel: set voltage of each channel
  # setChannelVoltage(ch, voltage (in V))
  def setChannelVoltage(self, channel, voltageVal):
    if (channel > self.n_chans) or (channel < 1):
      raise(ValueError('Invalid channel number: {channel}'))

    if voltageVal > self.max_voltage:
      voltageVal = self.max_voltage
      self.lg.warn('Voltage setpoint {voltageVal} is too high. Clipping to upper limit {self.max_voltage}')
    elif voltageVal < self.min_voltage:
      voltageVal = self.min_voltage
      self.lg.warn('Voltage setpoint {voltageVal} is too low. Clipping to lower limit {self.min_voltage}')
    #Send Request to XDAC (Server)
    msgV = "SETV:" + ("%d" % channel) + ":" + ("%.3f" % voltageVal)
    self.req_socket.send(msgV.encode('utf-8'))
    message = self.req_socket.recv()
    return 0

  # setChannel: set current of each channel
  # setChannelCurrent(ch, current (in mA))
  def setChannelCurrent(self, channel, currentVal):
    if (channel > self.n_chans) or (channel < 1):
      raise(ValueError('Invalid channel number: {channel}'))

    #Set threeshold fo Current and Voltage
    if currentVal > self.max_current:
      currentVal = self.max_current
      self.lg.warn('Current setpoint {currentVal} is too high. Clipping to upper limit {self.min_current}')
    elif currentVal < self.min_current:
      currentVal = self.min_current
      self.lg.warn('Current setpoint {currentVal} is too low. Clipping to lower limit {self.min_current}')
    #Send Request to XDAC (Server)
    msgC = "SETC:" + ("%d" % channel) + ":" + ("%.3f" % currentVal)
    self.req_socket.send(msgC.encode('utf-8'))
    message = self.req_socket.recv()
    return 0

  # setVoltageAllChannels: set voltage of all channels
  # AllVValues = [8, 8, -1, 2, 3, 4, 5, 7]
  # setVoltageAllChannels(AllVValues)


  def setVoltageAllChannels(self, AllVValues):
    for channel, value in enumerate(AllVValues):
      channel = channel + 1 #Channel start from 1
      self.setChannelVoltage(channel, value)
    return 0

  # setCurrentAllChannels: set current of all channels
  # AllCValues = [200, 200, 300, 50, 300, 400, 450, 250]
  # setCurrentAllChannels(AllCValues)
  def setCurrentAllChannels(self, AllCValues):
    for channel, value in enumerate(AllCValues):
      channel = channel + 1 #Channel start from 1
      self.setChannelCurrent(channel, value)
    return 0

  # setOff: set one channel to zero
  # setOff(1) -> set 0 V, 0 mA, to channel 1.
  def setOff(self, channel):
    if (channel > self.n_chans) or (channel < 1):
      raise(ValueError('Invalid channel number: {channel}'))
    #Send Request to XDAC (Server)
    msg = "ZERO:" + ("%d" % channel)
    self.req_socket.send(msg.encode('utf-8'))
    message = self.req_socket.recv()
    return 0

  #Read Current on all channels, return list
  def readAllChannelCurrent(self,):
    current = []
    offsets = [0]*self.n_chans
    if self.cal_data is not None:
      if 'current_offsets' in self.cal_data:
        offsets = self.cal_data['current_offsets']
    #Wait for Message V for 10 mS
    stop= " "
    while stop[0] != "C":
      msg = self.sub_socket.recv().decode('utf-8')
      stop = msg[0]
      if msg[0] == "C":
        msg = msg[1:]
        for i in range(self.n_chans):
          current.append(float(msg[0:msg.find(",")]) - offsets[i])
          msg = msg[msg.find(",")+1:]
          msg = msg[msg.find(",")+1:]
    
    return current

  #Read Current on all voltages, return list
  def readAllChannelVoltage(self):
    voltage = []
    #Wait for Message V for 10 mS
    stop= " "
    while stop[0] != "V":
      msg = self.sub_socket.recv().decode('utf-8')
      stop = msg[0]
      if msg[0] == "V":
        msg = msg[1:]
        for _ in range(8):
          voltage.append(msg[0:msg.find(",")])
          msg = msg[msg.find(",")+1:]
          msg = msg[msg.find(",")+1:]

    return voltage

  def do_current_zero_offset_cal(self):
    self.lg.info('Initiating current zero-offset calibration.')
    self.lg.info('All channels must be disconnected now.')
    self.lg.info('Performing calibration. Please wait...')

    for ch in range(self.n_chans):
      self.setOff(ch+1)

    # number of readings to average
    n_readings = 500

    c = np.zeros((n_readings, self.n_chans))

    for i in range(n_readings):
      c[i,:] = x.readAllChannelCurrent()
    
    offsets = c.mean(0)

    if self.cal_data is None:
      self.cal_data = {}
    self.cal_data['current_offsets'] = list(offsets)

    plo = pathlib.Path(self.cal_file_name)
    with open(str(plo), 'w') as fp:
      json.dump(self.cal_data, fp)

    self.lg.info('Calibration complete!')
    self.lg.info(f'Current zero-offsets = {offsets} mA')

# testing
if __name__ == "__main__":
  context = zmq.Context()  # make a zmq context object
  x = xdac(context, ip="169.254.38.99")
  x.lg.setLevel(logging.DEBUG)

  # edit me
  do_cal = False

  if do_cal == True:
    x.do_current_zero_offset_cal()

  x.setVoltageAllChannels([-10, 3, -4, 5, 9, -1, 7, -4])
  while True:
    val_string = str([f"{v:5.2f}" for v in x.readAllChannelCurrent()])
    x.lg.debug(f"Current = {val_string} mA")
