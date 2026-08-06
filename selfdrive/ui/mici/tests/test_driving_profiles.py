from dataclasses import dataclass

import pytest

from openpilot.selfdrive.ui.mici.driving_profiles import (
  DrivingProfile,
  PROFILE_DETAILS,
  PROFILE_LABELS,
  apply_driving_profile,
  driving_profiles_available,
  get_driving_profile,
  get_driving_profile_label,
  get_profile_carousel_order,
  profile_change_allowed,
)


class FakeParams:
  def __init__(self, values: dict[str, bool] | None = None):
    self.values = values or {}
    self.writes: list[tuple[str, bool, bool]] = []

  def get_bool(self, key: str) -> bool:
    return self.values.get(key, False)

  def put_bool(self, key: str, value: bool, block: bool = False) -> None:
    self.values[key] = value
    self.writes.append((key, value, block))


@dataclass
class FakeCarParams:
  brand: str
  alphaLongitudinalAvailable: bool


@pytest.mark.parametrize(
  ("profile", "expected"),
  [
    (DrivingProfile.STOCK_DASHCAM, (False, False, False)),
    (DrivingProfile.SUNNY_TACC, (True, False, False)),
    (DrivingProfile.SUNNY_LONG_EXPERIMENTAL, (True, True, True)),
  ],
)
def test_apply_and_detect_profile(profile: DrivingProfile, expected: tuple[bool, bool, bool]):
  params = FakeParams(
    {
      "OpenpilotEnabledToggle": not expected[0],
      "AlphaLongitudinalEnabled": not expected[1],
      "ExperimentalMode": not expected[2],
    }
  )

  apply_driving_profile(params, profile)

  assert get_driving_profile(params) == profile
  assert (
    params.get_bool("OpenpilotEnabledToggle"),
    params.get_bool("AlphaLongitudinalEnabled"),
    params.get_bool("ExperimentalMode"),
  ) == expected
  assert params.get_bool("DoReboot")
  assert not params.get_bool("OnroadCycleRequested")
  assert all(block for _, _, block in params.writes)


def test_experimental_profile_uses_conservative_write_order_and_records_confirmation():
  params = FakeParams()

  apply_driving_profile(params, DrivingProfile.SUNNY_LONG_EXPERIMENTAL)

  assert params.writes == [
    ("ExperimentalMode", False, True),
    ("AlphaLongitudinalEnabled", True, True),
    ("OpenpilotEnabledToggle", True, True),
    ("ExperimentalModeConfirmed", True, True),
    ("ExperimentalMode", True, True),
    ("DoReboot", True, True),
  ]


def test_non_experimental_profiles_do_not_clear_prior_confirmation():
  params = FakeParams({"ExperimentalModeConfirmed": True})

  apply_driving_profile(params, DrivingProfile.SUNNY_TACC)

  assert params.get_bool("ExperimentalModeConfirmed")
  assert not any(key == "ExperimentalModeConfirmed" for key, _, _ in params.writes)


def test_inconsistent_params_are_reported_as_custom():
  params = FakeParams(
    {
      "OpenpilotEnabledToggle": True,
      "AlphaLongitudinalEnabled": True,
      "ExperimentalMode": False,
    }
  )

  assert get_driving_profile(params) is None
  assert get_driving_profile_label(params) == "custom settings"


@pytest.mark.parametrize("current", list(DrivingProfile))
def test_current_profile_is_centered_with_both_alternatives_one_swipe_away(current: DrivingProfile):
  order = get_profile_carousel_order(current)

  assert order[1] == current
  assert set(order) == set(DrivingProfile)


def test_custom_state_defaults_carousel_to_tacc_in_center():
  assert get_profile_carousel_order(None)[1] == DrivingProfile.SUNNY_TACC


def test_profile_copy_fits_native_card_without_ellipsis():
  assert all(len(label) <= 18 for label in PROFILE_LABELS.values())
  assert all(len(detail) <= 18 for detail in PROFILE_DETAILS.values())


@pytest.mark.parametrize(
  ("CP", "expected"),
  [
    (None, False),
    (FakeCarParams("tesla", False), False),
    (FakeCarParams("toyota", True), False),
    (FakeCarParams("tesla", True), True),
  ],
)
def test_profiles_are_limited_to_tesla_alpha_longitudinal_platforms(CP, expected: bool):
  assert driving_profiles_available(CP) is expected


@pytest.mark.parametrize(
  ("started", "engaged", "alive", "valid", "standstill", "park", "expected"),
  [
    (False, False, False, False, False, False, True),
    (True, False, True, True, True, True, True),
    (True, True, True, True, True, True, False),
    (True, False, False, True, True, True, False),
    (True, False, True, False, True, True, False),
    (True, False, True, True, False, True, False),
    (True, False, True, True, True, False, False),
  ],
)
def test_profile_change_guard(started: bool, engaged: bool, alive: bool, valid: bool, standstill: bool, park: bool, expected: bool):
  assert profile_change_allowed(started, engaged, alive, valid, standstill, park) is expected
