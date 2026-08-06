import copy
from opendbc.can import CANDefine, CANParser
from opendbc.car import Bus, DT_CTRL, structs
from opendbc.car.carlog import carlog
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.interfaces import CarStateBase
from opendbc.car.tesla.teslacan import get_steer_ctrl_type
from opendbc.car.tesla.values import DBC, CANBUS, GEAR_MAP, STEER_THRESHOLD, TeslaFlags

from opendbc.sunnypilot.car.tesla.carstate_ext import CarStateExt

ButtonType = structs.CarState.ButtonEvent.Type


def is_fsd14_autopark(flags: int, autopilot_state: int, waiting_for_brake: bool) -> bool:
  return bool(flags & TeslaFlags.FSD_14) and autopilot_state == 6 and waiting_for_brake


def is_fsd14_stock_autopilot(flags: int, autopilot_state: int, waiting_for_brake: bool) -> bool:
  return bool(flags & TeslaFlags.FSD_14) and autopilot_state in (3, 4, 5, 6) and not is_fsd14_autopark(flags, autopilot_state, waiting_for_brake)


def is_stock_lkas(flags: int, primary_type: int, secondary_type: int) -> bool:
  primary_lkas = primary_type == get_steer_ctrl_type(flags, 2)
  # FSD 14.26.8 on the tested Model Y reports stock steering ownership in
  # byte 2 bits 5:4 while the legacy bits 7:6 stay at NONE.
  fsd14_secondary_lkas = bool(flags & TeslaFlags.FSD_14) and secondary_type == 2
  return primary_lkas or fsd14_secondary_lkas


def invalid_lkas_setting(flags: int, autosteer_enabled: bool) -> bool:
  return not (flags & TeslaFlags.FSD_14) and autosteer_enabled


class CarState(CarStateBase, CarStateExt):
  def __init__(self, CP, CP_SP):
    CarStateBase.__init__(self, CP, CP_SP)
    CarStateExt.__init__(self, CP, CP_SP)
    self.can_define = CANDefine(DBC[CP.carFingerprint][Bus.party])
    self.shifter_values = self.can_define.dv["DI_systemStatus"]["DI_gear"]

    self.autopark = False
    self.autopark_prev = False
    self.cruise_enabled_prev = False
    self.fsd14_error_logged = False
    self.suspected_fsd14 = False
    self.autopilot_request_prev = False
    self.stock_handoff_active = False
    self.stock_handoff_cruise_seen = False
    self.stock_handoff_grace_frames = 0

    self.hands_on_level = 0
    self.das_control = None

  def update_autopark_state(self, autopark_state: str, cruise_enabled: bool):
    autopark_now = autopark_state in ("ACTIVE", "COMPLETE", "SELFPARK_STARTED")
    if autopark_now and not self.autopark_prev and not self.cruise_enabled_prev:
      self.autopark = True
    if not autopark_now:
      self.autopark = False
    self.autopark_prev = autopark_now
    self.cruise_enabled_prev = cruise_enabled

  def update_stock_handoff_state(self, autopilot_request: bool, stock_lkas: bool, stock_autopilot: bool, autopark: bool,
                                 cruise_enabled: bool) -> bool:
    request_rising = autopilot_request and not self.autopilot_request_prev
    self.autopilot_request_prev = autopilot_request

    stock_active = stock_lkas or stock_autopilot or autopark
    if request_rising or stock_active:
      self.stock_handoff_active = True
      self.stock_handoff_grace_frames = int(2.0 / DT_CTRL)

    if self.stock_handoff_active:
      if cruise_enabled:
        self.stock_handoff_cruise_seen = True

      if autopilot_request or stock_active:
        self.stock_handoff_grace_frames = int(2.0 / DT_CTRL)
      elif not self.stock_handoff_cruise_seen:
        self.stock_handoff_grace_frames = max(0, self.stock_handoff_grace_frames - 1)

      stock_inactive = not autopilot_request and not stock_active
      cruise_cycle_complete = stock_inactive and self.stock_handoff_cruise_seen and not cruise_enabled
      request_timed_out = stock_inactive and not self.stock_handoff_cruise_seen and self.stock_handoff_grace_frames == 0
      if cruise_cycle_complete or request_timed_out:
        self.stock_handoff_active = False
        self.stock_handoff_cruise_seen = False
        self.stock_handoff_grace_frames = 0

    return self.stock_handoff_active

  def update(self, can_parsers) -> tuple[structs.CarState, structs.CarStateSP]:
    cp_party = can_parsers[Bus.party]
    cp_ap_party = can_parsers[Bus.ap_party]
    ret = structs.CarState()
    ret_sp = structs.CarStateSP()

    # Vehicle speed
    ret.vEgoRaw = cp_party.vl["DI_speed"]["DI_vehicleSpeed"] * CV.KPH_TO_MS
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    # Gas pedal
    ret.gasPressed = cp_party.vl["DI_systemStatus"]["DI_accelPedalPos"] > 0

    # Brake pedal
    ret.brakePressed = cp_party.vl["ESP_status"]["ESP_driverBrakeApply"] == 2

    # Steering wheel
    epas_status = cp_party.vl["EPAS3S_sysStatus"]
    self.hands_on_level = epas_status["EPAS3S_handsOnLevel"]
    ret.steeringAngleDeg = -epas_status["EPAS3S_internalSAS"]
    ret.steeringRateDeg = -cp_ap_party.vl["SCCM_steeringAngleSensor"]["SCCM_steeringAngleSpeed"]
    ret.steeringTorque = -epas_status["EPAS3S_torsionBarTorque"]

    # stock handsOnLevel uses >0.5 for 0.25s, but is too slow
    ret.steeringPressed = self.update_steering_pressed(abs(ret.steeringTorque) > STEER_THRESHOLD, 5)

    eac_status = self.can_define.dv["EPAS3S_sysStatus"]["EPAS3S_eacStatus"].get(int(epas_status["EPAS3S_eacStatus"]), None)
    ret.steerFaultPermanent = eac_status == "EAC_FAULT"
    ret.steerFaultTemporary = eac_status == "EAC_INHIBITED"

    # FSD disengages using union of handsOnLevel (slow overrides) and high angle rate faults (fast overrides, high speed)
    eac_error_code = self.can_define.dv["EPAS3S_sysStatus"]["EPAS3S_eacErrorCode"].get(int(epas_status["EPAS3S_eacErrorCode"]), None)
    ret.steeringDisengage = self.hands_on_level >= 3 or (eac_status == "EAC_INHIBITED" and
                                                         eac_error_code == "EAC_ERROR_HIGH_ANGLE_RATE_SAFETY")

    # Cruise state
    cruise_state = self.can_define.dv["DI_state"]["DI_cruiseState"].get(int(cp_party.vl["DI_state"]["DI_cruiseState"]), None)
    speed_units = self.can_define.dv["DI_state"]["DI_speedUnits"].get(int(cp_party.vl["DI_state"]["DI_speedUnits"]), None)

    autopark_state = self.can_define.dv["DI_state"]["DI_autoparkState"].get(int(cp_party.vl["DI_state"]["DI_autoparkState"]), None)
    cruise_enabled = cruise_state in ("ENABLED", "STANDSTILL", "OVERRIDE", "PRE_FAULT", "PRE_CANCEL")
    self.update_autopark_state(autopark_state, cruise_enabled)

    fsd14 = bool(self.CP.flags & TeslaFlags.FSD_14)
    autopilot_state = cp_ap_party.vl["DAS_status"]["DAS_autopilotState"]
    waiting_for_brake = cp_ap_party.vl["DAS_status"]["DAS_autoparkWaitingForBrake"] == 1
    stock_lkas = is_stock_lkas(self.CP.flags,
                               cp_ap_party.vl["DAS_steeringControl"]["DAS_steeringControlType"],
                               cp_ap_party.vl["DAS_steeringControl"]["DAS_steeringControlType2"])
    fsd14_autopark = is_fsd14_autopark(self.CP.flags, autopilot_state, waiting_for_brake)
    fsd14_stock_autopilot = is_fsd14_stock_autopilot(self.CP.flags, autopilot_state, waiting_for_brake)
    autopilot_request = fsd14 and cp_party.vl["DI_state"]["DI_autopilotRequest"] == 1
    stock_handoff = fsd14 and self.update_stock_handoff_state(autopilot_request, stock_lkas, fsd14_stock_autopilot,
                                                              self.autopark or fsd14_autopark, cruise_enabled)

    # Match Panda's fail-closed stock ADAS handoff. During FSD14 Autopark or
    # stock Autopilot, keep selfdrived disengaged until Tesla cruise has gone
    # fully off; a held cruise state must never re-enable comma automatically.
    ret.cruiseState.enabled = cruise_enabled and not self.autopark and not stock_handoff
    ret.blockPcmEnable = stock_handoff
    if speed_units == "KPH":
      ret.cruiseState.speed = max(cp_party.vl["DI_state"]["DI_digitalSpeed"] * CV.KPH_TO_MS, 1e-3)
    elif speed_units == "MPH":
      ret.cruiseState.speed = max(cp_party.vl["DI_state"]["DI_digitalSpeed"] * CV.MPH_TO_MS, 1e-3)
    ret.cruiseState.available = cruise_state == "STANDBY" or cruise_enabled
    ret.cruiseState.standstill = False  # This needs to be false, since we can resume from stop without sending anything special
    ret.standstill = cp_party.vl["ESP_B"]["ESP_vehicleStandstillSts"] == 1
    ret.accFaulted = cruise_state == "FAULT"

    # Gear
    ret.gearShifter = GEAR_MAP[self.can_define.dv["DI_systemStatus"]["DI_gear"].get(int(cp_party.vl["DI_systemStatus"]["DI_gear"]), "DI_GEAR_INVALID")]

    # Doors
    ret.doorOpen = cp_party.vl["UI_warning"]["anyDoorOpen"] == 1

    # Blinkers
    ret.leftBlinker = cp_party.vl["UI_warning"]["leftBlinkerBlinking"] in (1, 2)
    ret.rightBlinker = cp_party.vl["UI_warning"]["rightBlinkerBlinking"] in (1, 2)

    # Seatbelt
    ret.seatbeltUnlatched = cp_party.vl["UI_warning"]["buckleStatus"] != 1

    # Blindspot
    ret.leftBlindspot = cp_ap_party.vl["DAS_status"]["DAS_blindSpotRearLeft"] != 0
    ret.rightBlindspot = cp_ap_party.vl["DAS_status"]["DAS_blindSpotRearRight"] != 0

    # AEB
    ret.stockAeb = cp_ap_party.vl["DAS_control"]["DAS_aebEvent"] == 1

    # LKAS
    # On FSD 14+, ANGLE_CONTROL behavior changed to allow user winddown while actuating.
    # FSD switched from using ANGLE_CONTROL to LANE_KEEP_ASSIST to likely keep the old steering override disengage logic.
    # LKAS switched from LANE_KEEP_ASSIST to ANGLE_CONTROL to likely allow overriding LKAS events smoothly
    ret.stockLkas = stock_lkas  # LANE_KEEP_ASSIST

    # Stock Autosteer should be off (includes FSD)
    # TODO: find for TESLA_MODEL_X and HW2.5 vehicles
    if not (self.CP.flags & TeslaFlags.MISSING_DAS_SETTINGS):
      # FSD14 has an explicit, Panda-enforced handoff for stock Autopilot, so
      # the Tesla Autosteer setting may remain enabled. Older firmware keeps
      # the upstream requirement to disable stock Autosteer.
      ret.invalidLkasSetting = invalid_lkas_setting(self.CP.flags, cp_ap_party.vl["DAS_settings"]["DAS_autosteerEnabled"] != 0)

      # Because we don't have FSD 14 detection outside of a set of FW, we should check if this FW is accidentally missing from FSD_14_FW
      # 1. If in Autosteer or FSD, already caught by invalidLkasSetting
      # 2. If in TACC and DAS ever sends ANGLE_CONTROL (1), we can infer it's trying to do LKAS on FSD 14+
      angle_control = cp_ap_party.vl["DAS_steeringControl"]["DAS_steeringControlType"] == 1  # ANGLE_CONTROL
      if not ret.invalidLkasSetting and angle_control and not self.CP.flags & TeslaFlags.FSD_14:
        self.suspected_fsd14 = True

      if self.suspected_fsd14:
        ret.invalidLkasSetting = True
        if not self.fsd14_error_logged:
          carlog.error("FSD 14 detected, but FW not in FSD_14_FW set")
          self.fsd14_error_logged = True

    # Buttons # ToDo: add Gap adjust button

    # Messages needed by carcontroller
    self.das_control = copy.copy(cp_ap_party.vl["DAS_control"])

    CarStateExt.update(self, ret, ret_sp, can_parsers)

    return ret, ret_sp

  @staticmethod
  def get_can_parsers(CP, CP_SP):
    return {
      Bus.party: CANParser(DBC[CP.carFingerprint][Bus.party], [], CANBUS.party),
      Bus.ap_party: CANParser(DBC[CP.carFingerprint][Bus.party], [], CANBUS.autopilot_party),
      **CarStateExt.get_parser(CP, CP_SP),
    }
