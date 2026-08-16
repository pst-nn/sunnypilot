from types import SimpleNamespace

import pytest

from opendbc.sunnypilot.car.tesla.coop_steering import (
  ANGLE_CONTROL,
  LANE_KEEP_ASSIST,
  STOCK_AP_ACTIVATION_FRAMES,
  CoopSteeringCarController,
)
from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP


def car_params_sp(coop_steering: bool):
  flags = TeslaFlagsSP.COOP_STEERING.value if coop_steering else 0
  return SimpleNamespace(flags=flags)


@pytest.mark.parametrize("cruise_enabled", [False, True])
def test_cooperative_steering_disabled_always_uses_angle_control(cruise_enabled):
  controller = CoopSteeringCarController(stock_ap_handoff_supported=True)

  controller.update(car_params_sp(False), cruise_enabled)

  assert controller.coop_steering.control_type == ANGLE_CONTROL


@pytest.mark.parametrize("cruise_enabled", [False, True])
def test_non_fsd14_preserves_immediate_cooperative_steering(cruise_enabled):
  controller = CoopSteeringCarController(stock_ap_handoff_supported=False)

  controller.update(car_params_sp(True), cruise_enabled)

  assert controller.coop_steering.control_type == LANE_KEEP_ASSIST


def test_fsd14_cooperative_steering_waits_for_stock_ap_activation_window():
  controller = CoopSteeringCarController(stock_ap_handoff_supported=True)
  params = car_params_sp(True)

  controller.update(params, False)
  assert controller.coop_steering.control_type == ANGLE_CONTROL

  controller.update(params, True)
  assert controller.coop_steering.control_type == ANGLE_CONTROL
  assert controller.stock_ap_activation_frames == STOCK_AP_ACTIVATION_FRAMES

  for _ in range(STOCK_AP_ACTIVATION_FRAMES - 1):
    controller.update(params, True)
    assert controller.coop_steering.control_type == ANGLE_CONTROL

  controller.update(params, True)
  assert controller.coop_steering.control_type == LANE_KEEP_ASSIST


def test_fsd14_cruise_off_cycle_rearms_activation_window():
  controller = CoopSteeringCarController(stock_ap_handoff_supported=True)
  params = car_params_sp(True)

  controller.update(params, True)
  for _ in range(STOCK_AP_ACTIVATION_FRAMES):
    controller.update(params, True)
  assert controller.coop_steering.control_type == LANE_KEEP_ASSIST

  controller.update(params, False)
  assert controller.coop_steering.control_type == ANGLE_CONTROL
  assert controller.stock_ap_activation_frames == 0

  controller.update(params, True)
  assert controller.coop_steering.control_type == ANGLE_CONTROL
  assert controller.stock_ap_activation_frames == STOCK_AP_ACTIVATION_FRAMES


def test_disabling_cooperative_steering_clears_activation_window():
  controller = CoopSteeringCarController(stock_ap_handoff_supported=True)

  controller.update(car_params_sp(True), True)
  assert controller.stock_ap_activation_frames == STOCK_AP_ACTIVATION_FRAMES

  controller.update(car_params_sp(False), True)
  assert controller.coop_steering.control_type == ANGLE_CONTROL
  assert controller.stock_ap_activation_frames == 0
