from __future__ import annotations

from collections.abc import Callable

import pyray as rl
from cereal import car

from openpilot.common.swaglog import cloudlog
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
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigConfirmationDialog, BigDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import gui_label
from openpilot.system.ui.widgets.scroller import NavScroller


PROFILE_CONFIRM_TITLES = {
  DrivingProfile.STOCK_DASHCAM: "slide for\nTesla / Dashcam",
  DrivingProfile.SUNNY_TACC: "slide for\nsunny + TACC",
  DrivingProfile.SUNNY_LONG_EXPERIMENTAL: "slide for Long + Exp\nAEB unavailable",
}


class DrivingProfileHomeButton(Widget):
  WIDTH = 258
  HEIGHT = 50
  FEEDBACK_SECONDS = 4.0

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, self.WIDTH, self.HEIGHT))
    self._feedback_message: str | None = None
    self._feedback_until = 0.0

    # MiciHomeLayout routes this badge to profiles and the remaining home screen
    # to Settings, matching the original whole-screen Settings touch behavior.
    self.set_touch_valid_callback(lambda: False)

  def show_applied_feedback(self, was_started: bool):
    self._feedback_message = "RESTARTING CONTROLS" if was_started else "SAVED / NEXT DRIVE"
    self._feedback_until = rl.get_time() + self.FEEDBACK_SECONDS

  def _render(self, _):
    bg = rl.Color(46, 46, 49, 255)
    rl.draw_rectangle_rounded(self._rect, 0.35, 8, bg)
    rl.draw_rectangle_rounded_lines_ex(self._rect, 0.35, 8, 1, rl.Color(255, 255, 255, 35))

    label = get_driving_profile_label(ui_state.params)
    feedback_active = self._feedback_message is not None and rl.get_time() < self._feedback_until
    kicker = self._feedback_message if feedback_active else "DRIVING PROFILE"
    kicker_rect = rl.Rectangle(self._rect.x + 14, self._rect.y + 1, self._rect.width - 48, 16)
    profile_rect = rl.Rectangle(self._rect.x + 14, self._rect.y + 15, self._rect.width - 50, 33)
    arrow_rect = rl.Rectangle(self._rect.x + self._rect.width - 34, self._rect.y, 24, self._rect.height)

    gui_label(kicker_rect, kicker, font_size=13, color=rl.Color(180, 180, 184, 255), font_weight=FontWeight.MEDIUM)
    gui_label(profile_rect, label, font_size=23, font_weight=FontWeight.BOLD)
    gui_label(arrow_rect, ">", font_size=28, color=rl.Color(205, 205, 208, 255), alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)


class DrivingProfilesLayout(NavScroller):
  CARD_WIDTH = 402
  CARD_SPACING = 20

  def __init__(self):
    super().__init__()
    self._profile_applied_callback: Callable[[bool], None] | None = None

    self._buttons: dict[DrivingProfile, BigButton] = {}
    for profile in DrivingProfile:
      button = BigButton(PROFILE_LABELS[profile], PROFILE_DETAILS[profile])
      button.set_click_callback(lambda selected=profile: self._select_profile(selected))
      self._buttons[profile] = button
      self._scroller.add_widget(button)

    # Center one native 402x180 card on the 536x240 mici viewport and snap between cards.
    self._scroller._pad = max(0, int((gui_app.width - self.CARD_WIDTH) / 2))
    self._scroller._snap_items = True
    self._center_pending = False

    self._confirm_icon = gui_app.texture("icons_mici/settings/device/lkas.png", 100, 64)
    self._warning_icon = gui_app.texture("icons_mici/setup/warning.png", 64, 64)

  def set_profile_applied_callback(self, callback: Callable[[bool], None] | None):
    self._profile_applied_callback = callback

  def show_event(self):
    super().show_event()
    self._refresh_cards()
    self._center_pending = True

  def _render(self, rect):
    super()._render(rect)

    # Scroller show_event resets to its first card. Center the current profile after
    # one layout pass so its neighboring alternatives are each one swipe away.
    if self._center_pending:
      center_offset = -(self.CARD_WIDTH + self.CARD_SPACING)
      self._scroller.scroll_panel.set_offset(center_offset)
      self._scroller._scrolling_to_filter.x = center_offset
      self._center_pending = False

  def _refresh_cards(self):
    current = get_driving_profile(ui_state.params)
    order = get_profile_carousel_order(current)
    self._scroller._items[:] = [self._buttons[profile] for profile in order]

    for profile, button in self._buttons.items():
      button.set_value("ACTIVE" if profile == current else PROFILE_DETAILS[profile])

  @staticmethod
  def _change_allowed() -> bool:
    CS = ui_state.sm["carState"]
    return profile_change_allowed(
      started=ui_state.started,
      engaged=ui_state.engaged,
      car_state_alive=ui_state.sm.alive["carState"],
      car_state_valid=ui_state.sm.valid["carState"],
      standstill=CS.standstill,
      gear_is_park=CS.gearShifter == car.CarState.GearShifter.park,
    )

  @staticmethod
  def _show_change_blocked():
    gui_app.push_widget(BigDialog("profile change blocked", "Park, stop completely, and disengage first."))

  def _select_profile(self, profile: DrivingProfile):
    if profile == get_driving_profile(ui_state.params):
      return

    if not driving_profiles_available(ui_state.CP):
      gui_app.push_widget(BigDialog("profiles unavailable", "Tesla Alpha Longitudinal support was not detected."))
      return

    if not self._change_allowed():
      self._show_change_blocked()
      return

    warning = profile == DrivingProfile.SUNNY_LONG_EXPERIMENTAL
    dialog = BigConfirmationDialog(
      PROFILE_CONFIRM_TITLES[profile],
      self._warning_icon if warning else self._confirm_icon,
      confirm_callback=lambda: self._apply_profile(profile),
      red=warning,
    )
    gui_app.push_widget(dialog)

  def _apply_profile(self, profile: DrivingProfile):
    # Re-check after the confirmation slider: vehicle state may have changed while
    # the dialog was open.
    if not self._change_allowed():
      self._show_change_blocked()
      return

    was_started = ui_state.started
    try:
      apply_driving_profile(ui_state.params, profile)
    except Exception:
      cloudlog.exception("driving profile change failed")
      gui_app.push_widget(BigDialog("profile change failed", "Do not drive until the settings are checked."))
      return

    cloudlog.info(f"driving profile changed to {profile.value}; onroad cycle requested")
    try:
      ui_state.update_params()
    except Exception:
      # Params were already committed and the cycle was requested. A refresh
      # failure must not invite the user to repeat the write mid-cycle.
      cloudlog.exception("driving profile UI refresh failed")
    self._refresh_cards()
    if self._profile_applied_callback:
      self._profile_applied_callback(was_started)
    self.dismiss()
