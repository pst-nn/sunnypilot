import re
import unittest

from opendbc.car import DT_CTRL, gen_empty_fingerprint
from opendbc.car.structs import CarParams
from opendbc.car.tesla.carstate import CarState, invalid_lkas_setting, is_fsd14_autopark, is_fsd14_stock_autopilot, is_stock_lkas
from opendbc.car.tesla.interface import CarInterface
from opendbc.car.tesla.fingerprints import FW_VERSIONS
from opendbc.car.tesla.radar_interface import RADAR_START_ADDR
from opendbc.car.tesla.values import CAR, FSD_14_FW, TeslaFlags

Ecu = CarParams.Ecu

# Fields prefixed unknown_* we observe structurally but don't know the meaning of.
# Only `platform` has evidence-backed semantic meaning (matches car_model in FW_VERSIONS).
#
# unknown_prefix is everything before the comma; we don't split it because we don't know what its
# parts mean, but observed shape is: <family>_<package>_<triplet> (<build>), e.g.
#   TeMYG4 _ Main     _ 0.0.0 (78)     or     TeM3 _ SP_XP002p2 _ 0.0.0 (23)
#   family   package    triplet build           family  package    triplet build
#
# After the comma, the version string decomposes into:
#   platform             : E/Y/X = car model (Model 3 / Y / X). The only field with known meaning.
#   variant_code         : differentiator WITHIN a platform — hardware/trim/calibration bits packed
#                          into <digit?><letters?><3-digit series>, e.g. '4HP015', '4003', 'L014',
#                          'PR003'. We don't fully know what the parts mean individually, but the
#                          whole string identifies a specific variant within the car model.
#   software_major/minor : numeric components after the first '.' — conventional release numbers.
#                          minor is optional (e.g. 'E4S014.27' has no minor).
#
# Suspected (not confirmed): for M3/MY, `TeM3_*` outer + no-leading-digit variant_code == HW3, and
# `TeMYG4_*` outer + leading-'4' variant_code == HW4 (the 'G4' in TeMYG4 likely denotes Gen 4).
#
# Example full parse of 'TeMYG4_Main_0.0.0 (78),E4HP015.05.0':
#   unknown_prefix='TeMYG4_Main_0.0.0 (78)'
#   platform=E  variant_code=4HP015  software_major=05  software_minor=0
FW_RE = re.compile(
  rb'^(?P<unknown_prefix>.+),' +
  rb'(?P<platform>[EYX])' +
  rb'(?P<variant_code>\d?[A-Z]*\d{3})' +
  rb'\.(?P<software_major>\d+)' +
  rb'(?:\.(?P<software_minor>\d+))?$'
)

PLATFORM_TO_CAR = {
  b'E': CAR.TESLA_MODEL_3,
  b'Y': CAR.TESLA_MODEL_Y,
  b'X': CAR.TESLA_MODEL_X,
}

# Hypothesized FSD 14 profile, in terms of variant_code bookends (given software_major >= 4):
#   M3: variant_code starts with '4H',  ends with '015'
#   MY: variant_code starts with '4',   ends with '003'
# Older series (M3 '014', MY '002') are never FSD 14.
FSD_14_FW_RULE = {
  CAR.TESLA_MODEL_3: (b'4H', b'015'),
  CAR.TESLA_MODEL_Y: (b'4',  b'003'),
}


class TestTeslaFingerprint(unittest.TestCase):
  def test_fw_platform_code(self):
    # Every EPS FW must parse and its platform letter must match the car it's filed under.
    for car_model, ecus in FW_VERSIONS.items():
      for fw in ecus.get((Ecu.eps, 0x730, None), []):
        m = FW_RE.match(fw)

        assert m is not None, f"Unparsable FW: {fw}"
        assert PLATFORM_TO_CAR[m['platform']] == car_model, f"Platform letter {m['platform']!r} != {car_model.value}: {fw}"

  def test_fsd_14_fw(self):
    for car_model, ecus in FW_VERSIONS.items():
      if car_model not in FSD_14_FW_RULE:
        continue

      variant_prefix, variant_suffix = FSD_14_FW_RULE[car_model]
      for fw in ecus.get((Ecu.eps, 0x730, None), []):
        m = FW_RE.match(fw)
        assert m is not None, f"Unparsable FW: {fw}"

        is_fsd_14 = fw in FSD_14_FW.get(car_model, [])
        expected = (
          m['variant_code'].startswith(variant_prefix)
          and m['variant_code'].endswith(variant_suffix)
          and int(m['software_major']) >= 4
        )
        assert is_fsd_14 == expected, f"{fw}"

  def test_radar_detection(self):
    # Test radar availability detection for cars with radar DBC defined
    for radar in (True, False):
      fingerprint = gen_empty_fingerprint()
      if radar:
        fingerprint[1][RADAR_START_ADDR] = 8
      CP = CarInterface.get_params(CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
      assert CP.radarUnavailable != radar

  def test_no_radar_car(self):
    # Model X doesn't have radar DBC defined, should always be unavailable
    for radar in (True, False):
      fingerprint = gen_empty_fingerprint()
      if radar:
        fingerprint[1][RADAR_START_ADDR] = 8
      CP = CarInterface.get_params(CAR.TESLA_MODEL_X, fingerprint, [], False, False, False)
      assert CP.radarUnavailable  # Always unavailable since no radar DBC


class TestTeslaStockAdasHandoff(unittest.TestCase):
  @staticmethod
  def _carstate() -> CarState:
    cs = CarState.__new__(CarState)
    cs.autopilot_request_prev = False
    cs.stock_handoff_active = False
    cs.stock_handoff_cruise_seen = False
    cs.stock_handoff_grace_frames = 0
    return cs

  def test_fsd14_autopark_requires_waiting_for_brake(self):
    self.assertTrue(is_fsd14_autopark(TeslaFlags.FSD_14, 6, True))
    self.assertFalse(is_fsd14_autopark(TeslaFlags.FSD_14, 6, False))
    self.assertFalse(is_fsd14_autopark(TeslaFlags.FSD_14, 3, True))
    self.assertFalse(is_fsd14_autopark(0, 6, True))

  def test_fsd14_stock_autopilot_active_states_exclude_autopark(self):
    for state in (3, 4, 5):
      self.assertTrue(is_fsd14_stock_autopilot(TeslaFlags.FSD_14, state, False))
    self.assertTrue(is_fsd14_stock_autopilot(TeslaFlags.FSD_14, 6, False))
    self.assertFalse(is_fsd14_stock_autopilot(TeslaFlags.FSD_14, 6, True))
    for state in (0, 1, 2, 8, 9, 14, 15):
      self.assertFalse(is_fsd14_stock_autopilot(TeslaFlags.FSD_14, state, False))
    self.assertFalse(is_fsd14_stock_autopilot(0, 3, False))

  def test_stock_lkas_uses_observed_fsd14_secondary_field(self):
    self.assertTrue(is_stock_lkas(0, 2, 0))
    self.assertFalse(is_stock_lkas(0, 0, 2))
    self.assertTrue(is_stock_lkas(TeslaFlags.FSD_14, 1, 0))
    self.assertTrue(is_stock_lkas(TeslaFlags.FSD_14, 0, 2))
    self.assertFalse(is_stock_lkas(TeslaFlags.FSD_14, 0, 0))

  def test_fsd14_allows_autosteer_setting_only_with_handoff_support(self):
    self.assertFalse(invalid_lkas_setting(TeslaFlags.FSD_14, True))
    self.assertFalse(invalid_lkas_setting(TeslaFlags.FSD_14, False))
    self.assertTrue(invalid_lkas_setting(0, True))
    self.assertFalse(invalid_lkas_setting(0, False))

  def test_request_handoff_latches_through_cruise_cycle(self):
    cs = self._carstate()

    self.assertTrue(cs.update_stock_handoff_state(True, False, False, False, False))
    self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, False))
    self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, True))
    self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, True))
    self.assertFalse(cs.update_stock_handoff_state(False, False, False, False, False))

  def test_request_without_stock_activation_expires(self):
    cs = self._carstate()
    self.assertTrue(cs.update_stock_handoff_state(True, False, False, False, False))

    for _ in range(int(2.0 / DT_CTRL) - 1):
      self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, False))
    self.assertFalse(cs.update_stock_handoff_state(False, False, False, False, False))

  def test_stock_activity_refreshes_handoff_without_request(self):
    cs = self._carstate()
    self.assertTrue(cs.update_stock_handoff_state(False, True, False, False, True))
    self.assertTrue(cs.update_stock_handoff_state(False, True, False, False, False))
    self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, True))
    self.assertFalse(cs.update_stock_handoff_state(False, False, False, False, False))

  def test_status_activity_refreshes_handoff_without_request(self):
    cs = self._carstate()
    self.assertTrue(cs.update_stock_handoff_state(False, False, True, False, True))
    self.assertTrue(cs.update_stock_handoff_state(False, False, True, False, False))
    self.assertTrue(cs.update_stock_handoff_state(False, False, False, False, True))
    self.assertFalse(cs.update_stock_handoff_state(False, False, False, False, False))
