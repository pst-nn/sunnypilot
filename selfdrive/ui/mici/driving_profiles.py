from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ParamsLike(Protocol):
  def get_bool(self, key: str) -> bool: ...
  def put_bool(self, key: str, value: bool, block: bool = False) -> None: ...


class DrivingProfile(Enum):
  STOCK_DASHCAM = "stock_dashcam"
  SUNNY_TACC = "sunny_tacc"
  SUNNY_LONG_EXPERIMENTAL = "sunny_long_experimental"


@dataclass(frozen=True)
class DrivingProfileConfig:
  openpilot_enabled: bool
  alpha_longitudinal_enabled: bool
  experimental_mode: bool


PROFILE_CONFIGS = {
  DrivingProfile.STOCK_DASHCAM: DrivingProfileConfig(False, False, False),
  DrivingProfile.SUNNY_TACC: DrivingProfileConfig(True, False, False),
  DrivingProfile.SUNNY_LONG_EXPERIMENTAL: DrivingProfileConfig(True, True, True),
}

PROFILE_LABELS = {
  DrivingProfile.STOCK_DASHCAM: "Tesla / Dashcam",
  DrivingProfile.SUNNY_TACC: "sunny + TACC",
  DrivingProfile.SUNNY_LONG_EXPERIMENTAL: "sunny Long + Exp",
}

PROFILE_DETAILS = {
  DrivingProfile.STOCK_DASHCAM: "Tesla control",
  DrivingProfile.SUNNY_TACC: "Tesla TACC",
  DrivingProfile.SUNNY_LONG_EXPERIMENTAL: "AEB OFF",
}


def get_driving_profile(params: ParamsLike) -> DrivingProfile | None:
  current = DrivingProfileConfig(
    params.get_bool("OpenpilotEnabledToggle"),
    params.get_bool("AlphaLongitudinalEnabled"),
    params.get_bool("ExperimentalMode"),
  )
  return next((profile for profile, config in PROFILE_CONFIGS.items() if config == current), None)


def get_driving_profile_label(params: ParamsLike) -> str:
  profile = get_driving_profile(params)
  return PROFILE_LABELS[profile] if profile is not None else "custom settings"


def get_profile_carousel_order(current: DrivingProfile | None) -> tuple[DrivingProfile, DrivingProfile, DrivingProfile]:
  if current is None:
    return (DrivingProfile.STOCK_DASHCAM, DrivingProfile.SUNNY_TACC, DrivingProfile.SUNNY_LONG_EXPERIMENTAL)

  other_profiles = [profile for profile in DrivingProfile if profile != current]
  return (other_profiles[0], current, other_profiles[1])


def driving_profiles_available(CP) -> bool:
  return CP is not None and CP.brand == "tesla" and CP.alphaLongitudinalAvailable


def profile_change_allowed(started: bool, engaged: bool, car_state_alive: bool, car_state_valid: bool, standstill: bool, gear_is_park: bool) -> bool:
  if not started:
    return True
  return not engaged and car_state_alive and car_state_valid and standstill and gear_is_park


def apply_driving_profile(params: ParamsLike, profile: DrivingProfile) -> None:
  config = PROFILE_CONFIGS[profile]

  # Move through a conservative intermediate state. This matters if a write fails:
  # Experimental mode is disabled before changing the startup-only controls.
  params.put_bool("ExperimentalMode", False, block=True)
  params.put_bool("AlphaLongitudinalEnabled", config.alpha_longitudinal_enabled, block=True)
  params.put_bool("OpenpilotEnabledToggle", config.openpilot_enabled, block=True)

  if config.experimental_mode:
    # The profile confirmation includes the Experimental/Alpha warning.
    params.put_bool("ExperimentalModeConfirmed", True, block=True)
    params.put_bool("ExperimentalMode", True, block=True)

  # Alpha longitudinal and OpenpilotEnabledToggle are consumed at controls startup.
  params.put_bool("OnroadCycleRequested", True, block=True)
