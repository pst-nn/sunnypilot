"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from collections import namedtuple

from opendbc.car import DT_CTRL, structs
from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP

ANGLE_CONTROL = 1
LANE_KEEP_ASSIST = 2
STOCK_AP_ACTIVATION_WINDOW = 1.0
STOCK_AP_ACTIVATION_FRAMES = round(STOCK_AP_ACTIVATION_WINDOW / DT_CTRL)

CoopSteeringDataSP = namedtuple("CoopSteeringDataSP",
                                ["control_type"])


class CoopSteeringCarController:
  def __init__(self, stock_ap_handoff_supported: bool = False):
    self.stock_ap_handoff_supported = stock_ap_handoff_supported
    self.cruise_enabled_prev = False
    self.stock_ap_activation_frames = 0
    self.coop_steering = CoopSteeringDataSP(ANGLE_CONTROL)

  def update(self, CP_SP: structs.CarParamsSP, cruise_enabled: bool) -> None:
    coop_steering_requested = bool(CP_SP.flags & TeslaFlagsSP.COOP_STEERING.value)

    if not coop_steering_requested:
      self.stock_ap_activation_frames = 0
    elif not self.stock_ap_handoff_supported:
      # Preserve the existing immediate Cooperative Steering behavior on
      # platforms without the FSD 14 Stock Autopilot handoff.
      self.stock_ap_activation_frames = 0
    elif not cruise_enabled:
      # A complete cruise-off cycle re-arms the activation window.
      self.stock_ap_activation_frames = 0
    elif not self.cruise_enabled_prev:
      # Keep normal angle control briefly after the first stalk pull so a
      # second pull can activate Stock Autopilot. If Tesla takes ownership,
      # the Panda handoff latch blocks all subsequent openpilot actuation.
      self.stock_ap_activation_frames = STOCK_AP_ACTIVATION_FRAMES
    elif self.stock_ap_activation_frames > 0:
      self.stock_ap_activation_frames -= 1

    activation_window_complete = self.stock_ap_activation_frames == 0
    coop_steering_active = coop_steering_requested and (
      not self.stock_ap_handoff_supported or (cruise_enabled and activation_window_complete)
    )
    control_type = LANE_KEEP_ASSIST if coop_steering_active else ANGLE_CONTROL

    self.cruise_enabled_prev = cruise_enabled
    self.coop_steering = CoopSteeringDataSP(control_type)
