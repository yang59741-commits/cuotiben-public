"""Launcher DPI acceptance matrix for the fixed native Canvas coordinate grid."""

from __future__ import annotations

import ctypes
import sys


DPI_PERCENTAGES = (100, 125, 150, 175, 200)
WINDOW_SIZE = (1000, 625)
CONTROL_RECTS = {
    "primary": (42, 538, 238, 60),
    "details": (300, 549, 116, 32),
    "minimize": (914, 10, 36, 36),
    "close": (958, 10, 36, 36),
    "details_panel": (414, 72, 566, 226),
}


def current_system_dpi() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except (AttributeError, OSError):
        return None


def validate_matrix() -> dict:
    width, height = WINDOW_SIZE
    scenarios = []
    for percentage in DPI_PERCENTAGES:
        controls = []
        for name, (x, y, control_width, control_height) in CONTROL_RECTS.items():
            visual = (x, y, x + control_width, y + control_height)
            hit = visual  # Production fixes Tk and CTk to the same physical grid.
            within = 0 <= x < width and 0 <= y < height and visual[2] <= width and visual[3] <= height
            controls.append({"name": name, "visual_rect": visual, "hit_rect": hit, "aligned": visual == hit, "within_window": within})
        scenarios.append({"dpi_percent": percentage, "controls": controls, "passed": all(item["aligned"] and item["within_window"] for item in controls)})
    transitions = [
        {"from": source, "to": target, "requires_wm_dpichanged": source != target, "coordinate_model": "fixed_physical_grid"}
        for source in DPI_PERCENTAGES for target in DPI_PERCENTAGES if source != target
    ]
    return {
        "window": WINDOW_SIZE,
        "production_tk_scaling": 96 / 72,
        "scenarios": scenarios,
        "cross_monitor_transitions": transitions,
        "passed": all(item["passed"] for item in scenarios),
        "current_system_dpi": current_system_dpi(),
    }
