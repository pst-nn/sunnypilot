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
STOCK_AP_HANDOFF_GRACE = 0.35
STOCK_AP_HANDOFF_GRACE_FRAMES = round(STOCK_AP_HANDOFF_GRACE / DT_CTRL)

CoopSteeringDataSP = namedtuple("CoopSteeringDataSP",
                                ["control_type", "steering_allowed"])


class CoopSteeringCarController:
  def __init__(self, stock_ap_handoff_supported: bool = False):
    self.stock_ap_handoff_supported = stock_ap_handoff_supported
    self.cruise_enabled_prev = False
    self.stock_ap_handoff_grace_frames = 0
    self.coop_steering = CoopSteeringDataSP(ANGLE_CONTROL, True)

  def update(self, CP_SP: structs.CarParamsSP, cruise_enabled: bool, stock_autopilot_available: bool = False) -> None:
    coop_steering_requested = bool(CP_SP.flags & TeslaFlagsSP.COOP_STEERING.value)
    stock_ap_handoff_possible = self.stock_ap_handoff_supported and stock_autopilot_available

    if not coop_steering_requested or not stock_ap_handoff_possible:
      self.stock_ap_handoff_grace_frames = 0
    elif not cruise_enabled:
      # A complete cruise-off cycle re-arms the handoff grace period.
      self.stock_ap_handoff_grace_frames = 0
    elif not self.cruise_enabled_prev:
      # Do not actuate briefly after the first stalk pull. Sending ANGLE_CONTROL
      # here latches FSD 14 EPAS into EAC_ACTIVE and changes the driver's
      # override/disengagement behavior even after we switch to cooperative
      # steering. A NONE frame keeps EPAS available while a fast second pull
      # can activate Stock Autopilot; Panda then owns the fail-closed handoff.
      self.stock_ap_handoff_grace_frames = STOCK_AP_HANDOFF_GRACE_FRAMES
    elif self.stock_ap_handoff_grace_frames > 0:
      self.stock_ap_handoff_grace_frames -= 1

    steering_allowed = not (coop_steering_requested and stock_ap_handoff_possible and cruise_enabled and
                            self.stock_ap_handoff_grace_frames > 0)
    control_type = LANE_KEEP_ASSIST if coop_steering_requested else ANGLE_CONTROL

    self.cruise_enabled_prev = cruise_enabled
    self.coop_steering = CoopSteeringDataSP(control_type, steering_allowed)
