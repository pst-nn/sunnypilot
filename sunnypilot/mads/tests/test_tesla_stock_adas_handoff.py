"""Regression tests for Tesla stock-ADAS ownership above the Panda boundary."""

from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from cereal import car, custom, log
from openpilot.selfdrive.selfdrived.events import ET, Events
from openpilot.sunnypilot.mads.helpers import is_tesla_stock_adas_handoff
from openpilot.sunnypilot.mads.mads import ModularAssistiveDrivingSystem
from openpilot.sunnypilot.mads.state import StateMachine
from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

State = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState
EventName = log.OnroadEvent.EventName
EventNameSP = custom.OnroadEventSP.EventName


def make_car_params(brand: str) -> car.CarParams:
  CP = car.CarParams.new_message()
  CP.brand = brand
  return CP


def make_mads(mocker: MockerFixture, state: custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState):
  CP = make_car_params("tesla")
  CP.passive = False

  selfdrive = SimpleNamespace(
    CP=CP,
    enabled=False,
    enabled_prev=False,
    events=Events(),
    events_sp=EventsSP(),
    initialized=True,
    sm={"pandaStates": []},
    state_machine=mocker.MagicMock(current_alert_types=[]),
  )

  mads = ModularAssistiveDrivingSystem.__new__(ModularAssistiveDrivingSystem)
  mads.CP = CP
  mads.selfdrive = selfdrive
  mads.events = selfdrive.events
  mads.events_sp = selfdrive.events_sp
  mads.state_machine = StateMachine(mads)
  mads.state_machine.state = state
  mads.enabled_toggle = True
  mads.enabled = state != State.disabled
  mads.active = state == State.enabled
  mads.lateral_mismatch_counter = 0
  return mads


@pytest.mark.parametrize(
  ("brand", "blocked", "expected"),
  (
    ("tesla", True, True),
    ("tesla", False, False),
    ("hyundai", True, False),
  ),
)
def test_stock_handoff_scope(brand: str, blocked: bool, expected: bool):
  CP = make_car_params(brand)
  CS = car.CarState.new_message()
  CS.blockPcmEnable = blocked
  assert is_tesla_stock_adas_handoff(CP, CS) is expected


@pytest.mark.parametrize(("initial_state", "expect_disable_event"), ((State.enabled, True), (State.disabled, False)))
def test_stock_handoff_disables_mads_and_blocks_reenable(mocker: MockerFixture, initial_state, expect_disable_event: bool):
  mads = make_mads(mocker, initial_state)
  mads.events.add(EventName.pcmEnable)
  mads.events.add(EventName.buttonEnable)
  mads.events_sp.add(EventNameSP.lkasEnable)
  mads.events_sp.add(EventNameSP.silentLkasEnable)

  CS = car.CarState.new_message()
  CS.blockPcmEnable = True
  mads.update(CS)

  assert mads.state_machine.state == State.disabled
  assert not mads.enabled
  assert not mads.active
  assert not mads.events.has(EventName.pcmEnable)
  assert not mads.events.has(EventName.buttonEnable)
  assert not mads.events_sp.has(EventNameSP.lkasEnable)
  assert not mads.events_sp.has(EventNameSP.silentLkasEnable)
  assert mads.events_sp.has(EventNameSP.lkasDisable) is expect_disable_event
  assert ET.WARNING not in mads.selfdrive.state_machine.current_alert_types


def test_controlsd_fails_closed_before_stale_mads_state(mocker: MockerFixture):
  controls = ControlsExt.__new__(ControlsExt)
  controls.CP = make_car_params("tesla")
  controls.blinker_pause_lateral = mocker.MagicMock()

  CS = car.CarState.new_message()
  CS.blockPcmEnable = True
  ss_sp = custom.SelfdriveStateSP.new_message()
  ss_sp.mads.available = True
  ss_sp.mads.active = True
  ss = log.SelfdriveState.new_message()
  ss.active = True
  sm = {"carState": CS, "selfdriveStateSP": ss_sp, "selfdriveState": ss}

  assert not controls.get_lat_active(sm)
  controls.blinker_pause_lateral.update.assert_not_called()


def test_controlsd_keeps_non_tesla_block_pcm_behavior(mocker: MockerFixture):
  controls = ControlsExt.__new__(ControlsExt)
  controls.CP = make_car_params("hyundai")
  controls.blinker_pause_lateral = mocker.MagicMock()
  controls.blinker_pause_lateral.update.return_value = False

  CS = car.CarState.new_message()
  CS.blockPcmEnable = True
  ss_sp = custom.SelfdriveStateSP.new_message()
  ss_sp.mads.available = True
  ss_sp.mads.active = True
  sm = {"carState": CS, "selfdriveStateSP": ss_sp, "selfdriveState": log.SelfdriveState.new_message()}

  assert controls.get_lat_active(sm)
