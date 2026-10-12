#!/usr/bin/env python3
"""
ZipTie OBD-II Live Monitor + Anomaly Flagger
For "ZipTie" — 1998 Chevy Silverado 1500, fire-engine red, 5.7L Vortec L31 V8 (GMT-400).

Connects via USB ELM327-compatible adapter (or Bluetooth/WiFi).
Live TUI with sparklines, color thresholds, baseline deltas, smart flagging
(fuel trims, timing retard, stuck upstream O2, voltage, coolant, EGR, DTCs),
DTC read/clear, clean wide-format CSV logging, and a session summary on exit.

Oil pressure is NOT quantitative PSI via standard OBD-II on the L31
(FSM min hot: 6 psi @ 1k / 18 @ 2k / 24 @ 4k RPM — mechanical gauge required).
See README.md for full usage notes and PID research.
"""

import argparse
import csv
import json
import os
import select
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import obd
from rich import box
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align

# -----------------------------------------------------------------------------
# Configuration / Tuning
# -----------------------------------------------------------------------------

# Metrics we actively watch + display (must be valid obd.commands.* or handled specially)
# Using the friendly names from python-obd where possible.
INTERESTED: List[str] = [
    "RPM",
    "SPEED",
    "COOLANT_TEMP",
    "INTAKE_TEMP",
    "AMBIENT_AIR_TEMP",
    "TIMING_ADVANCE",
    "ENGINE_LOAD",
    "ABSOLUTE_LOAD",
    "THROTTLE_POS",
    "RELATIVE_THROTTLE_POS",
    "MAF",
    "INTAKE_PRESSURE",
    "BAROMETRIC_PRESSURE",
    "SHORT_FUEL_TRIM_1",
    "LONG_FUEL_TRIM_1",
    "SHORT_FUEL_TRIM_2",
    "LONG_FUEL_TRIM_2",
    "CONTROL_MODULE_VOLTAGE",
    "FUEL_STATUS",
    "FUEL_LEVEL",
    "FUEL_RATE",
    "FUEL_PRESSURE",
    "RUN_TIME",
    "O2_B1S1",
    "O2_B1S2",
    "O2_B2S1",
    "O2_B2S2",
    "COMMANDED_EQUIV_RATIO",
    "CATALYST_TEMP_B1S1",
    "CATALYST_TEMP_B2S1",
    # Extra Mode 01 PIDs useful on many 1996–1998 GM trucks (if PCM supports)
    "COMMANDED_EGR",
    "EGR_ERROR",
    "EVAP_VAPOR_PRESSURE",
    "DISTANCE_W_MIL",
    "WARMUPS_SINCE_DTC_CLEAR",
    "DISTANCE_SINCE_DTC_CLEAR",
    "TIME_SINCE_DTC_CLEARED",
    "OBD_COMPLIANCE",
    "O2_SENSORS",
    "ELM_VOLTAGE",
]

# Display labels and formatting hints
METRIC_META: Dict[str, Dict[str, Any]] = {
    "RPM":               {"label": "Engine RPM",          "unit": "rpm",   "fmt": ".0f", "min": 0,   "max": 5600, "warn_low": 450, "warn_high": 5000},
    "SPEED":             {"label": "Vehicle Speed",       "unit": "mph",   "fmt": ".0f", "min": 0,   "max": 100},
    "COOLANT_TEMP":      {"label": "Coolant Temp",        "unit": "°F",    "fmt": ".0f", "min": -40, "max": 260,  "warn_low": 160, "warn_high": 220},
    "INTAKE_TEMP":       {"label": "Intake Air Temp",     "unit": "°F",    "fmt": ".0f", "min": -40, "max": 180,  "warn_high": 140},
    "AMBIENT_AIR_TEMP":  {"label": "Ambient Air Temp",    "unit": "°F",    "fmt": ".0f", "min": -40, "max": 140},
    "TIMING_ADVANCE":    {"label": "Ignition Timing",     "unit": "°",     "fmt": ".1f", "min": -20, "max": 60,   "warn_low": 0},
    "ENGINE_LOAD":       {"label": "Engine Load",         "unit": "%",     "fmt": ".1f", "min": 0,   "max": 100},
    "ABSOLUTE_LOAD":     {"label": "Absolute Load",       "unit": "%",     "fmt": ".1f", "min": 0,   "max": 100},
    "THROTTLE_POS":      {"label": "Throttle Position",   "unit": "%",     "fmt": ".1f", "min": 0,   "max": 100},
    "RELATIVE_THROTTLE_POS": {"label": "Relative Throttle", "unit": "%",   "fmt": ".1f", "min": 0,   "max": 100},
    "MAF":               {"label": "Mass Air Flow",       "unit": "g/s",   "fmt": ".2f", "min": 0,   "max": 250},
    "INTAKE_PRESSURE":   {"label": "Intake MAP",          "unit": "kPa",   "fmt": ".1f", "min": 0,   "max": 110},
    "BAROMETRIC_PRESSURE": {"label": "Barometric Press",  "unit": "inHg",  "fmt": ".2f", "min": 20,  "max": 32},
    "SHORT_FUEL_TRIM_1": {"label": "ST Fuel Trim B1",     "unit": "%",     "fmt": ".1f", "min": -25, "max": 25,   "warn_low": -12, "warn_high": 12},
    "LONG_FUEL_TRIM_1":  {"label": "LT Fuel Trim B1",     "unit": "%",     "fmt": ".1f", "min": -25, "max": 25,   "warn_low": -10, "warn_high": 10},
    "SHORT_FUEL_TRIM_2": {"label": "ST Fuel Trim B2",     "unit": "%",     "fmt": ".1f", "min": -25, "max": 25,   "warn_low": -12, "warn_high": 12},
    "LONG_FUEL_TRIM_2":  {"label": "LT Fuel Trim B2",     "unit": "%",     "fmt": ".1f", "min": -25, "max": 25,   "warn_low": -10, "warn_high": 10},
    "CONTROL_MODULE_VOLTAGE": {"label": "Module Voltage", "unit": "V",     "fmt": ".2f", "min": 8,   "max": 16,   "warn_low": 13.0, "warn_high": 15.0},
    "FUEL_STATUS":       {"label": "Fuel System",         "unit": "",      "fmt": "s"},
    "FUEL_LEVEL":        {"label": "Fuel Level",          "unit": "%",     "fmt": ".0f", "min": 0,   "max": 100,  "warn_low": 15},
    "FUEL_RATE":         {"label": "Fuel Rate",           "unit": "gph",   "fmt": ".2f", "min": 0,   "max": 15},
    "RUN_TIME":          {"label": "Engine Run Time",     "unit": "s",     "fmt": ".0f", "min": 0,   "max": 86400},
    "O2_B1S1":           {"label": "O2 B1S1 (up)",        "unit": "V",     "fmt": ".3f", "min": 0,   "max": 1.2},
    "O2_B1S2":           {"label": "O2 B1S2 (down)",      "unit": "V",     "fmt": ".3f", "min": 0,   "max": 1.2},
    "O2_B2S1":           {"label": "O2 B2S1 (up)",        "unit": "V",     "fmt": ".3f", "min": 0,   "max": 1.2},
    "O2_B2S2":           {"label": "O2 B2S2 (down)",      "unit": "V",     "fmt": ".3f", "min": 0,   "max": 1.2},
    "COMMANDED_EQUIV_RATIO": {"label": "Cmd Equiv Ratio", "unit": "",      "fmt": ".3f", "min": 0.5, "max": 1.5, "warn_low": 0.9, "warn_high": 1.1},
    "CATALYST_TEMP_B1S1": {"label": "Cat Temp B1S1",     "unit": "°F",    "fmt": ".0f", "min": 0,   "max": 1650, "warn_high": 1560},
    "CATALYST_TEMP_B2S1": {"label": "Cat Temp B2S1",     "unit": "°F",    "fmt": ".0f", "min": 0,   "max": 1650, "warn_high": 1560},
    "FUEL_PRESSURE":      {"label": "Fuel Pressure",      "unit": "psi",   "fmt": ".0f", "min": 0,   "max": 100,  "warn_low": 35, "warn_high": 75},
    "COMMANDED_EGR":      {"label": "Commanded EGR",      "unit": "%",     "fmt": ".1f", "min": 0,   "max": 100},
    "EGR_ERROR":          {"label": "EGR Error",          "unit": "%",     "fmt": ".1f", "min": -100,"max": 100,  "warn_low": -15, "warn_high": 15},
    "EVAP_VAPOR_PRESSURE": {"label": "EVAP Vapor Press",  "unit": "Pa",    "fmt": ".0f", "min": -8192,"max": 8191},
    "DISTANCE_W_MIL":     {"label": "Distance w/ MIL On", "unit": "mi",    "fmt": ".0f", "min": 0,   "max": 40000, "warn_high": 1},
    "WARMUPS_SINCE_DTC_CLEAR": {"label": "Warm-ups Since Clear", "unit": "", "fmt": ".0f", "min": 0, "max": 255},
    "DISTANCE_SINCE_DTC_CLEAR": {"label": "Dist Since DTC Clear", "unit": "mi", "fmt": ".0f", "min": 0, "max": 40000},
    "TIME_SINCE_DTC_CLEARED": {"label": "Time Since DTC Clear", "unit": "min", "fmt": ".0f", "min": 0, "max": 65535},
    "OBD_COMPLIANCE":     {"label": "OBD Standard",       "unit": "",      "fmt": "s"},
    "O2_SENSORS":         {"label": "O2 Sensors Present", "unit": "",      "fmt": "s"},
    "ELM_VOLTAGE":        {"label": "Adapter Voltage",    "unit": "V",     "fmt": ".2f", "min": 8,   "max": 16,   "warn_low": 12.0},
}

# L31 Vortec 5.7 reference notes (1998 GMT-98 C/K FSM + common experience)
TRUCK_NAME = "ZipTie"
TRUCK_COLOR = "Fire Engine Red"
TRUCK_YEAR = "1998"
TRUCK_MODEL = "Silverado 1500"
TRUCK_ENGINE = "5.7L Vortec L31 V8"

OIL_PRESSURE_NOTE = (
    "OIL PRESSURE: NOT quantitative PSI via standard OBD-II on L31.\n"
    "Dash sender only — fuel-pump oil-pressure switch is on/off, not a PID stream.\n"
    "FSM MINIMUM HOT (L31): 6 psi @ 1,000 / 18 psi @ 2,000 / 24 psi @ 4,000 RPM.\n"
    "Mechanical gauge is the only reliable way to verify pump/bearing health."
)

TIMING_NOTE = (
    "L31 base timing is set at the distributor (FSM procedure).\n"
    "OBD spark advance: FSM warm idle typical ~21° BTDC (scan-tool data list).\n"
    "Large retard under load often = knock sensor, bad gas, carbon, or ICM/distributor."
)

VORTEC_NOTE = (
    "L31 watch-outs: intake manifold gaskets (coolant), FPR diaphragm, "
    "knock sensor, EGR, dirty MAF, weak fuel pump, distributor/ICM."
)

# Tech-2 / enhanced scan data from FSM Engine Controls 5.0/5.7L data list —
# these are NOT available over generic Mode 01 (need GM enhanced / Class 2 tool).
FSM_ENHANCED_ONLY = (
    "Desired Idle, IAC Position, Knock Retard, CMP Retard, per-cyl misfire, "
    "Injector PWM, EGR Actual/Desired, EVAP duty, Fuel Trim Cell, Engine Oil Level Low"
)

# Warm idle reference from FSM Scan Tool Data List (Engine Data 1)
FSM_IDLE_REF = {
    "rpm": 650,
    "spark_deg": 21,
    "map_kpa": 38,
    "maf_gps": 4.6,
    "iac_counts": 18,
    "load_idle_pct": 2,
    "load_2500_pct": 11,
    "module_v": 13.5,
    "afr": 14.7,
}

# How many historical samples to keep for deltas / sparklines (per metric)
HISTORY_LEN = 40
# Minimum samples before we trust "change" calculations
MIN_HISTORY_FOR_DELTA = 6

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _get_obd_command(name: str) -> Optional[obd.OBDCommand]:
    """Return the obd command object by its common name, or None."""
    if hasattr(obd.commands, name):
        return getattr(obd.commands, name)
    return None

def _format_fuel_status(value: Any) -> str:
    """Turn the (status1, status2) tuple into a short readable string."""
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)) and len(value) >= 1:
        primary = value[0] or ""
        # Shorten the long official strings
        mapping = {
            "Open loop due to insufficient engine temperature": "Open (cold)",
            "Closed loop, using oxygen sensor feedback to determine fuel mix": "Closed loop",
            "Open loop due to engine load OR fuel cut due to deceleration": "Open (load/decel)",
            "Open loop due to system failure": "Open (fault)",
            "Closed loop, using at least one oxygen sensor but there is a fault in the feedback system": "Closed (O2 fault)",
        }
        short = mapping.get(primary, primary[:28] if primary else "—")
        return short
    return str(value)[:40]


def _format_value(name: str, value: Any) -> str:
    """Human friendly string for a value."""
    if value is None:
        return "—"
    if name == "FUEL_STATUS":
        return _format_fuel_status(value)
    if name == "OBD_COMPLIANCE":
        return str(value)[:40] if value is not None else "—"
    if name == "O2_SENSORS":
        return str(value)[:40] if value is not None else "—"
    meta = METRIC_META.get(name, {})
    fmt = meta.get("fmt", "")
    unit = meta.get("unit", "")
    try:
        if isinstance(value, (int, float)):
            if fmt == ".0f":
                s = f"{value:.0f}"
            elif fmt == ".1f":
                s = f"{value:.1f}"
            elif fmt == ".2f":
                s = f"{value:.2f}"
            elif fmt == ".3f":
                s = f"{value:.3f}"
            else:
                s = str(value)
            return f"{s} {unit}".strip()
        if hasattr(value, "magnitude"):  # pint Quantity
            # Avoid unit conversion headaches for things without matching units
            try:
                q = value.to(unit) if unit else value
                mag = q.magnitude
            except Exception:
                mag = value.magnitude
            if fmt == ".0f":
                return f"{mag:.0f} {unit}".strip()
            if fmt == ".1f":
                return f"{mag:.1f} {unit}".strip()
            if fmt == ".2f":
                return f"{mag:.2f} {unit}".strip()
            if fmt == ".3f":
                return f"{mag:.3f} {unit}".strip()
            return f"{mag} {unit}".strip()
        return str(value)
    except Exception:
        return str(value)

def _sparkline(values: List[float], width: int = 18) -> str:
    """Very simple ascii sparkline from a list of numeric values."""
    if not values or len(values) < 2:
        return " " * width
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return " " * width
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return "▁" * width
    chars = "▁▂▃▄▅▆▇█"
    out = []
    step = (len(vals) - 1) / (width - 1) if width > 1 else 0
    for i in range(width):
        idx = int(round(i * step))
        idx = max(0, min(idx, len(vals) - 1))
        v = vals[idx]
        norm = (v - lo) / (hi - lo)
        cidx = int(norm * (len(chars) - 1))
        out.append(chars[max(0, min(cidx, len(chars) - 1))])
    return "".join(out)

def _color_for_value(name: str, val: Any, baseline: Optional[float] = None) -> str:
    """Return rich color name based on thresholds + optional baseline delta."""
    if val is None or not isinstance(val, (int, float)):
        return "white"
    meta = METRIC_META.get(name, {})
    low = meta.get("warn_low")
    high = meta.get("warn_high")

    # Fuel trim special: symmetric and important
    if name in ("SHORT_FUEL_TRIM_1", "LONG_FUEL_TRIM_1", "SHORT_FUEL_TRIM_2", "LONG_FUEL_TRIM_2"):
        if abs(val) >= 18:
            return "red"
        if abs(val) >= 12:
            return "yellow"
        if abs(val) >= 8:
            return "cyan"
        return "green"

    if low is not None and val <= low:
        return "red" if val < (low - 5) else "yellow"
    if high is not None and val >= high:
        return "red" if val > (high + 5) else "yellow"

    # Voltage special
    if name == "CONTROL_MODULE_VOLTAGE":
        if val < 13.0:
            return "red"
        if val < 13.5:
            return "yellow"
        return "green"

    # Timing: low/negative under load is often bad, but at idle low positive is normal
    if name == "TIMING_ADVANCE":
        if val < 2:
            return "yellow"
        if val < -3:
            return "red"
        return "green"

    # Coolant (°F)
    if name == "COOLANT_TEMP":
        if val > 226:       # ~108°C
            return "red"
        if val > 215:       # ~102°C
            return "yellow"
        return "green"

    # Commanded equivalence ratio (1.0 = stoich)
    if name == "COMMANDED_EQUIV_RATIO":
        if val < 0.85 or val > 1.15:
            return "red"
        if val < 0.92 or val > 1.08:
            return "yellow"
        return "green"

    # Catalyst temp (°F)
    if name in ("CATALYST_TEMP_B1S1", "CATALYST_TEMP_B2S1"):
        if val > 1616:      # ~880°C
            return "red"
        if val > 1508:      # ~820°C
            return "yellow"
        return "green"

    # Default: green if we have no strong opinion
    return "green"

def _severity_color(level: str) -> str:
    return {"CRIT": "red", "WARN": "yellow", "INFO": "cyan"}.get(level, "white")

# -----------------------------------------------------------------------------
# Main Monitor Class
# -----------------------------------------------------------------------------

class SilveradoMonitor:
    def __init__(self, port: Optional[str] = None, log_path: Optional[str] = None,
                 poll_interval: float = 0.5, demo: bool = False):
        self.port = port
        self.conn: Optional[obd.OBD] = None
        self.poll_interval = poll_interval
        self.demo = demo

        self.data: Dict[str, Any] = {}
        self.history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
        self.baseline: Dict[str, Any] = {}
        self.flags: List[Tuple[float, str, str]] = []  # (ts, level, msg)
        self.flag_cooldown: Dict[str, float] = {}  # simple spam control per message key

        self.running = False
        self.log_path = log_path
        self.log_file = None
        self.log_writer = None
        self._log_lock = threading.Lock()
        self._last_log_ts = 0.0
        self.log_interval = max(1.0, poll_interval)  # full snapshot every ~1s (or poll rate)

        # O2 switching trackers (upstream banks should cross ~0.45V when warm + closed loop)
        self.o2_last_val: Dict[str, Optional[float]] = {"O2_B1S1": None, "O2_B2S1": None}
        self.o2_last_cross_ts: Dict[str, float] = {"O2_B1S1": 0.0, "O2_B2S1": 0.0}
        self.o2_switch_count: int = 0

        # DTC / MIL state (Mode 03 / 07 / 01 STATUS)
        self.stored_dtcs: List[Tuple[str, str]] = []       # (code, description)
        self.pending_dtcs: List[Tuple[str, str]] = []
        self.freeze_dtc: Optional[Tuple[str, str]] = None
        self.mil_on: Optional[bool] = None
        self.dtc_count: Optional[int] = None
        self.monitor_status_text: str = "—"
        self.last_dtc_poll: float = 0.0

        # Session statistics for the exit summary
        self.stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"min": float("inf"), "max": float("-inf")})
        self.flag_counts: Dict[str, int] = defaultdict(int)  # level -> count

        self._lock = threading.Lock()  # protects data/history/flags/baseline
        self._last_flag_prune = 0.0
        self.session_start = time.time()
        self.samples = 0

        self.console = Console()
        self._input_thread: Optional[threading.Thread] = None
        self._live: Optional[Live] = None
        self._demo_thread: Optional[threading.Thread] = None
        self._demo_t0 = time.time()

    # ---------------- Connection & Watching ----------------

    def connect(self) -> bool:
        # Always surface the oil pressure limitation immediately — this is the #1 thing users asked for.
        self._print_oil_warning()

        if self.demo:
            self.console.print("[bold magenta]DEMO MODE[/bold magenta] — feeding realistic L31 Vortec 5.7 simulation data")
            self.console.print("[dim]No adapter required. All UI, flags, logging, and summary features work normally.[/dim]")
            return True

        self.console.print("[bold]Connecting to OBD-II adapter...[/bold]")
        try:
            # Many cheap clones are happier with fast=False and a reasonable timeout.
            # 1996–2002 GMT-400 / 1999–early GMT-800 use J1850 VPW (Class 2), not CAN.
            # python-obd auto-detect usually works; force protocol="2" if auto fails.
            self.conn = obd.OBD(
                portstr=self.port,
                baudrate=None,          # auto
                protocol=None,          # auto → expect J1850 VPW on 1998
                fast=False,
                timeout=1.0,            # VPW can be a bit slower than CAN
            )
        except Exception as e:
            self.console.print(f"[red]Connection error:[/red] {e}")
            return False

        status = self.conn.status()
        if status == obd.OBDStatus.NOT_CONNECTED or self.conn.port is None:
            self.console.print("[red]Failed to connect.[/red] Check cable, permissions (dialout group), and that the adapter is not already in use.")
            self.console.print("Try:  ls /dev/ttyUSB* /dev/ttyACM* ; python -m serial.tools.list_ports")
            # Show current candidates
            try:
                import serial.tools.list_ports as lp
                ports = [str(p.device) for p in lp.comports()]
                if ports:
                    self.console.print(f"Detected ports: {', '.join(ports)}")
            except Exception:
                pass
            return False

        self.console.print(f"[green]Connected[/green] via {self.conn.port}  |  Protocol: {self.conn.protocol.name if self.conn.protocol else 'unknown'}  |  Status: {status}")

        # Show what we can actually query
        supported = []
        for name in INTERESTED:
            cmd = _get_obd_command(name)
            if cmd and cmd in self.conn.supported_commands:
                supported.append(name)
        self.console.print(f"Supported of interest: [cyan]{', '.join(supported) or 'none (will still try)'}[/cyan]")

        return True

    def _print_oil_warning(self):
        self.console.print(Panel.fit(
            Text(OIL_PRESSURE_NOTE, style="bold yellow"),
            title="[red]OIL PRESSURE — READ THIS[/red]",
            border_style="red",
            box=box.HEAVY,
        ))

    def start_watching(self):
        if self.demo:
            self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True)
            self._demo_thread.start()
            self.console.print("[green]Demo data generator started[/green]")
            return

        if not self.conn:
            return
        self.conn.stop()  # in case
        for name in INTERESTED:
            cmd = _get_obd_command(name)
            if cmd and cmd in self.conn.supported_commands:
                # capture name in closure
                def make_cb(n: str):
                    def cb(response: obd.OBDResponse):
                        self._on_response(n, response)
                    return cb
                self.conn.watch(cmd, callback=make_cb(name))
        self.conn.start()
        self.console.print("[green]Live watch started[/green] (background queries)")

    def _demo_loop(self):
        """Generate realistic L31 Vortec 5.7 style values that evolve over time."""
        import math
        import random

        while self.running:
            try:
                self._demo_tick(math, random)
            except Exception:
                pass  # never let demo data kill the monitor
            time.sleep(0.35)

    def _demo_tick(self, math, random):
        t = time.time() - self._demo_t0
        # Phases: 0-30s cold idle, 30-90s warm-up, then mixed driving
        phase = "cold" if t < 30 else ("warmup" if t < 90 else "drive")

        # Base values by phase — L31 Vortec 5.7 V8 character
        if phase == "cold":
            rpm = 950 + random.uniform(-40, 50)   # cold idle a bit high
            speed = 0
            coolant = 35 + t * 0.9 + random.uniform(-1, 1)
            intake = 28 + random.uniform(-2, 2)
            load = 20 + random.uniform(-3, 4)
            throttle = 9 + random.uniform(-1, 2)
            timing = 12 + random.uniform(-1, 2)
            stft = random.uniform(-3, 5)
            ltft = random.uniform(0, 4)
            stft2 = stft + random.uniform(-1.5, 1.5)
            ltft2 = ltft + random.uniform(-1.0, 1.0)
            fuel_status = ("Open loop due to insufficient engine temperature", "")
            o2_up = 0.45 + 0.05 * math.sin(t * 0.8) + random.uniform(-0.03, 0.03)
            o2_up2 = 0.45 + 0.05 * math.sin(t * 0.75 + 0.4) + random.uniform(-0.03, 0.03)
        elif phase == "warmup":
            rpm = 650 + random.uniform(-25, 35)   # warm L31 idle ~550–700
            speed = 0
            coolant = min(92, 55 + (t - 30) * 0.7 + random.uniform(-1, 1))
            intake = 32 + random.uniform(-2, 2)
            load = 17 + random.uniform(-2, 3)
            throttle = 7 + random.uniform(-1, 1)
            timing = 18 + random.uniform(-1, 2)
            stft = random.uniform(-4, 4)
            ltft = random.uniform(1, 6)
            stft2 = stft + random.uniform(-1.5, 1.5)
            ltft2 = ltft + random.uniform(-1.0, 1.5)
            fuel_status = ("Closed loop, using oxygen sensor feedback to determine fuel mix", "")
            o2_up = 0.45 + 0.35 * math.sin(t * 2.2) + random.uniform(-0.04, 0.04)
            o2_up2 = 0.45 + 0.35 * math.sin(t * 2.1 + 0.6) + random.uniform(-0.04, 0.04)
        else:
            # Simulated drive cycle with some variation
            cycle = (t - 90) % 60
            if cycle < 15:          # light cruise
                rpm = 1600 + random.uniform(-80, 100)
                speed = 55 + random.uniform(-4, 6)
                load = 28 + random.uniform(-4, 6)
                throttle = 16 + random.uniform(-2, 3)
                timing = 28 + random.uniform(-2, 3)
            elif cycle < 30:        # acceleration
                rpm = 2600 + random.uniform(-150, 200)
                speed = 68 + random.uniform(-5, 8)
                load = 55 + random.uniform(-6, 10)
                throttle = 40 + random.uniform(-4, 6)
                timing = 18 + random.uniform(-3, 4)
            elif cycle < 45:        # harder pull
                rpm = 3200 + random.uniform(-100, 180)
                speed = 80 + random.uniform(-5, 7)
                load = 72 + random.uniform(-5, 8)
                throttle = 55 + random.uniform(-4, 5)
                timing = 12 + random.uniform(-4, 3)
            else:                   # deceleration / coast
                rpm = 1200 + random.uniform(-60, 80)
                speed = 45 + random.uniform(-6, 5)
                load = 12 + random.uniform(-3, 4)
                throttle = 5 + random.uniform(-1, 2)
                timing = 22 + random.uniform(-2, 3)

            coolant = 91 + random.uniform(-1.5, 2.5)
            intake = 38 + random.uniform(-3, 4)
            stft = random.uniform(-6, 7)
            ltft = 3.5 + random.uniform(-2, 3)
            stft2 = stft + random.uniform(-2, 2)
            ltft2 = ltft + random.uniform(-1.5, 2)
            fuel_status = ("Closed loop, using oxygen sensor feedback to determine fuel mix", "")
            o2_up = 0.45 + 0.38 * math.sin(t * 3.1) + random.uniform(-0.05, 0.05)
            o2_up2 = 0.45 + 0.38 * math.sin(t * 2.9 + 0.5) + random.uniform(-0.05, 0.05)

        # V8 MAF scales higher than a 2.7L four
        maf = max(3.5, (rpm / 1000) * (load / 30) * 7.5 + random.uniform(-1.5, 2.0))
        abs_load = min(95, load * 1.15 + random.uniform(-3, 4))
        voltage = 14.1 + random.uniform(-0.15, 0.18)
        o2_down = 0.65 + random.uniform(-0.08, 0.08)
        o2_down2 = 0.62 + random.uniform(-0.08, 0.08)
        equiv = 1.0 + random.uniform(-0.02, 0.02)
        cat_temp = min(780, 120 + (coolant - 30) * 6.5 + (load * 3) + random.uniform(-15, 20))
        cat_temp2 = cat_temp + random.uniform(-20, 20)

        # Occasionally inject a mild anomaly so flags can fire
        if phase == "drive" and random.random() < 0.04:
            which = random.choice(["b1", "b2", "both"])
            val = random.choice([14.5, -13.0, 16.0])
            if which in ("b1", "both"):
                ltft = val
            if which in ("b2", "both"):
                ltft2 = val + random.uniform(-1, 1)
        if phase == "drive" and random.random() < 0.03:
            timing = -1.5 + random.uniform(-1, 1)

        # Demo values in native OBD units (converted in _on_response)
        ambient_c = 22 + random.uniform(-3, 5)                 # ~72°F-ish Oklahoma day
        baro_kpa = 98 + random.uniform(-2, 2)                  # ~29 inHg
        fuel_level = max(8, 62 - t * 0.02 + random.uniform(-1, 1))
        run_time = max(0, int(t))
        map_kpa = max(20, 101 - load * 0.55 + random.uniform(-3, 3))
        rel_throttle = max(0, throttle * 0.85 + random.uniform(-2, 2))
        # rough L/h from load/rpm for a thirsty 5.7
        fuel_rate_lph = max(0.6, (rpm / 1000) * (load / 40) * 4.2 + random.uniform(-0.3, 0.4))

        fake = {
            "RPM": rpm,
            "SPEED": speed,
            "COOLANT_TEMP": coolant,
            "INTAKE_TEMP": intake,
            "AMBIENT_AIR_TEMP": ambient_c,
            "TIMING_ADVANCE": timing,
            "ENGINE_LOAD": load,
            "ABSOLUTE_LOAD": abs_load,
            "THROTTLE_POS": throttle,
            "RELATIVE_THROTTLE_POS": rel_throttle,
            "MAF": maf,
            "INTAKE_PRESSURE": map_kpa,
            "BAROMETRIC_PRESSURE": baro_kpa,
            "SHORT_FUEL_TRIM_1": stft,
            "LONG_FUEL_TRIM_1": ltft,
            "SHORT_FUEL_TRIM_2": stft2,
            "LONG_FUEL_TRIM_2": ltft2,
            "CONTROL_MODULE_VOLTAGE": voltage,
            "FUEL_STATUS": fuel_status,
            "FUEL_LEVEL": fuel_level,
            "FUEL_RATE": fuel_rate_lph,
            "RUN_TIME": run_time,
            "O2_B1S1": max(0.05, min(0.95, o2_up)),
            "O2_B1S2": max(0.1, min(0.9, o2_down)),
            "O2_B2S1": max(0.05, min(0.95, o2_up2)),
            "O2_B2S2": max(0.1, min(0.9, o2_down2)),
            "COMMANDED_EQUIV_RATIO": equiv,
            "CATALYST_TEMP_B1S1": cat_temp,
            "CATALYST_TEMP_B2S1": cat_temp2,
            # Extra PIDs (native OBD units before conversion)
            "FUEL_PRESSURE": 380 + random.uniform(-20, 20),   # kPa (~55 psi rail-ish)
            "COMMANDED_EGR": max(0, (load - 25) * 0.4 + random.uniform(-2, 2)) if phase == "drive" else random.uniform(0, 2),
            "EGR_ERROR": random.uniform(-4, 4),
            "EVAP_VAPOR_PRESSURE": random.uniform(-200, 200),
            "DISTANCE_W_MIL": 0 if random.random() > 0.1 else random.uniform(5, 80),
            "WARMUPS_SINCE_DTC_CLEAR": min(255, int(t / 120) + 3),
            "DISTANCE_SINCE_DTC_CLEAR": t * 0.02 + random.uniform(0, 2),
            "TIME_SINCE_DTC_CLEARED": int(t / 60) + 40,
            "OBD_COMPLIANCE": "OBD-II as defined by the CARB",
            "O2_SENSORS": "B1S1 B1S2 B2S1 B2S2",
            "ELM_VOLTAGE": voltage + random.uniform(-0.05, 0.05),
        }

        for name, val in fake.items():
            class FakeResp:
                def __init__(self, v):
                    self.value = v
                def is_null(self):
                    return False
            self._on_response(name, FakeResp(val))

    def _on_response(self, name: str, response: obd.OBDResponse):
        if response.is_null() or response.value is None:
            return
        val = response.value
        # Normalize pint quantities to plain float where sensible for history
        numeric = None
        if hasattr(val, "magnitude"):
            numeric = float(val.magnitude)
        elif isinstance(val, (int, float)):
            numeric = float(val)

        # Convert to US customary units for display / thresholds / logging
        # (python-obd returns temps in °C, speed in km/h, baro in kPa, fuel rate in L/h)
        if numeric is not None:
            if name in ("COOLANT_TEMP", "INTAKE_TEMP", "CATALYST_TEMP_B1S1", "CATALYST_TEMP_B2S1", "AMBIENT_AIR_TEMP"):
                numeric = numeric * 9.0 / 5.0 + 32.0          # °C → °F
                val = numeric
            elif name == "SPEED":
                numeric = numeric * 0.621371                   # km/h → mph
                val = numeric
            elif name == "BAROMETRIC_PRESSURE":
                numeric = numeric * 0.2953                     # kPa → inHg
                val = numeric
            elif name == "FUEL_RATE":
                numeric = numeric * 0.264172                   # L/h → gal/h
                val = numeric
            elif name == "FUEL_PRESSURE":
                numeric = numeric * 0.145038                   # kPa → psi
                val = numeric
            elif name in ("DISTANCE_W_MIL", "DISTANCE_SINCE_DTC_CLEAR"):
                numeric = numeric * 0.621371                   # km → mi
                val = numeric

        ts = time.time()
        with self._lock:
            self.data[name] = val
            if numeric is not None:
                self.history[name].append((ts, numeric))
                # Update session min/max
                st = self.stats[name]
                if numeric < st["min"]:
                    st["min"] = numeric
                if numeric > st["max"]:
                    st["max"] = numeric
            self.samples += 1

            # Track upstream O2 switching (crosses of ~0.45 V) for both banks
            if name in ("O2_B1S1", "O2_B2S1") and numeric is not None:
                last = self.o2_last_val.get(name)
                if last is not None:
                    if (last < 0.45 <= numeric) or (last > 0.45 >= numeric):
                        self.o2_last_cross_ts[name] = ts
                        self.o2_switch_count += 1
                self.o2_last_val[name] = numeric

            # Immediate light flagging (full analysis also runs in render)
            self._quick_flag_check(name, val, numeric)

    def _write_snapshot(self):
        """Write one clean wide-row CSV snapshot of all current metrics."""
        if not self.log_writer:
            return
        try:
            ts = time.time()
            with self._lock:
                data = dict(self.data)
                flags = list(self.flags)

            row = {
                "timestamp": datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
            }
            for name in INTERESTED:
                val = data.get(name)
                if val is None:
                    row[name] = ""
                elif hasattr(val, "magnitude"):
                    row[name] = f"{val.magnitude:.4f}"
                elif isinstance(val, (int, float)):
                    row[name] = f"{val:.4f}"
                else:
                    row[name] = str(val)

            if flags:
                recent = "; ".join(f[2] for f in flags[-3:])
                row["active_flags"] = recent[:200]
            else:
                row["active_flags"] = ""

            with self._log_lock:
                self.log_writer.writerow(row)
                self.log_file.flush()
        except Exception:
            pass  # never let logging kill the monitor

    # ---------------- Flagging Logic ----------------

    def _quick_flag_check(self, name: str, val: Any, numeric: Optional[float]):
        """Lightweight immediate checks to populate flags quickly."""
        now = time.time()
        if numeric is None:
            return

        key = None
        level = "WARN"
        msg = None

        if name in ("LONG_FUEL_TRIM_1", "LONG_FUEL_TRIM_2"):
            bank = "B1" if name.endswith("_1") else "B2"
            if abs(numeric) >= 18:
                key = f"ltft_crit_{bank}"
                level = "CRIT"
                msg = (
                    f"LTFT {bank} {numeric:+.1f}% — strong indication of problem "
                    f"(vacuum leak, MAF, exhaust leak, fuel, or bank-specific issue)"
                )
            elif abs(numeric) >= 12:
                key = f"ltft_warn_{bank}"
                msg = f"LTFT {bank} {numeric:+.1f}% — elevated, watch for drift"

        elif name in ("SHORT_FUEL_TRIM_1", "SHORT_FUEL_TRIM_2"):
            bank = "B1" if name.endswith("_1") else "B2"
            if abs(numeric) >= 20:
                key = f"stft_crit_{bank}"
                level = "CRIT"
                msg = f"STFT {bank} {numeric:+.1f}% — large correction happening right now"

        elif name == "TIMING_ADVANCE":
            # Very low/negative timing is often bad, especially if load is high
            load = self.data.get("ENGINE_LOAD")
            load_val = load.magnitude if hasattr(load, "magnitude") else (load if isinstance(load, (int,float)) else 0)
            if numeric < -2:
                key = "timing_retard"
                level = "CRIT" if load_val > 40 else "WARN"
                msg = f"Timing {numeric:.1f}° — significant retard (knock? bad fuel? sensor?)"
            elif numeric < 3 and load_val > 30:
                key = "timing_low_load"
                msg = f"Timing only {numeric:.1f}° at {load_val:.0f}% load"

        elif name == "COOLANT_TEMP":
            if numeric > 230:       # ~110°C
                key = "ect_overheat"
                level = "CRIT"
                msg = f"Coolant {numeric:.0f}°F — overheating risk"
            elif numeric > 217:     # ~103°C
                key = "ect_hot"
                msg = f"Coolant {numeric:.0f}°F — running hot"

        elif name == "CONTROL_MODULE_VOLTAGE":
            if numeric < 12.8:
                key = "voltage_low"
                level = "CRIT" if numeric < 12.0 else "WARN"
                msg = f"System voltage {numeric:.2f}V — charging or battery issue?"

        elif name == "FUEL_LEVEL":
            if numeric <= 10:
                key = "fuel_crit"
                level = "CRIT"
                msg = f"Fuel level {numeric:.0f}% — critically low"
            elif numeric <= 15:
                key = "fuel_low"
                msg = f"Fuel level {numeric:.0f}% — getting low"

        elif name == "DISTANCE_W_MIL":
            if numeric >= 1:
                key = "mil_miles"
                level = "CRIT"
                msg = f"Driven {numeric:.0f} mi with MIL on — diagnose soon"

        elif name == "EGR_ERROR":
            if abs(numeric) >= 20:
                key = "egr_err"
                level = "WARN"
                msg = f"EGR error {numeric:+.1f}% — pintle/sensor/clog?"

        elif name == "FUEL_PRESSURE":
            if numeric > 0 and numeric < 35:
                key = "fuel_press_low"
                level = "CRIT"
                msg = f"Fuel pressure {numeric:.0f} psi — weak pump/filter/regulator?"
            elif numeric > 75:
                key = "fuel_press_high"
                msg = f"Fuel pressure {numeric:.0f} psi — regulator stuck?"

        if key and msg:
            # Cooldown to avoid spam
            last = self.flag_cooldown.get(key, 0)
            if now - last > 8.0:   # seconds
                self.flag_cooldown[key] = now
                with self._lock:
                    self.flags.append((now, level, msg))
                    self.flag_counts[level] += 1
                    if len(self.flags) > 25:
                        self.flags = self.flags[-20:]

    def analyze_and_flag(self):
        """Deeper / trend-based analysis. Called from the render path."""
        now = time.time()
        with self._lock:
            current_flags = []
            # Copy so we can work
            hist = {k: list(v) for k, v in self.history.items()}
            data = dict(self.data)
            base = dict(self.baseline)

        # Sustained fuel trim deviation (using recent history avg)
        for trim_name in ("LONG_FUEL_TRIM_1", "SHORT_FUEL_TRIM_1", "LONG_FUEL_TRIM_2", "SHORT_FUEL_TRIM_2"):
            h = hist.get(trim_name, [])
            if len(h) >= MIN_HISTORY_FOR_DELTA:
                recent = [x[1] for x in h[-12:]]
                avg = sum(recent) / len(recent)
                if abs(avg) >= 15:
                    level = "CRIT" if abs(avg) >= 18 else "WARN"
                    current_flags.append((now, level,
                        f"{trim_name} avg {avg:+.1f}% over last samples — persistent correction"))

        # Timing change vs baseline or vs recent
        timing = data.get("TIMING_ADVANCE")
        if timing is not None:
            tval = timing.magnitude if hasattr(timing, "magnitude") else timing
            if "TIMING_ADVANCE" in base:
                bval = base["TIMING_ADVANCE"]
                if isinstance(bval, (int, float)) and abs(tval - bval) > 12:
                    current_flags.append((now, "WARN",
                        f"Timing {tval:.1f}° vs baseline {bval:.1f}° (Δ {tval-bval:+.1f}°)"))

            # Sudden change detector
            th = hist.get("TIMING_ADVANCE", [])
            if len(th) >= 8:
                recent = [x[1] for x in th[-8:]]
                delta = recent[-1] - recent[0]
                if abs(delta) > 10:
                    current_flags.append((now, "WARN",
                        f"Timing moved {delta:+.1f}° in recent window"))

        # Load vs RPM sanity (very rough)
        rpm = data.get("RPM")
        load = data.get("ENGINE_LOAD")
        if rpm and load:
            r = rpm.magnitude if hasattr(rpm, "magnitude") else rpm
            l = load.magnitude if hasattr(load, "magnitude") else load
            if r < 2000 and l > 85:
                current_flags.append((now, "INFO", "Very high load at low RPM — lugging?"))

        # Upstream O2 stuck detection (only after engine is warm + running a bit)
        coolant = data.get("COOLANT_TEMP")
        rpm_val = None
        if rpm:
            rpm_val = rpm.magnitude if hasattr(rpm, "magnitude") else rpm
        cool_val = None
        if coolant:
            cool_val = coolant.magnitude if hasattr(coolant, "magnitude") else coolant

        if (cool_val is not None and cool_val > 167 and   # ~75°C
                rpm_val is not None and rpm_val > 600):
            for o2_name, label in (("O2_B1S1", "B1S1"), ("O2_B2S1", "B2S1")):
                last_v = self.o2_last_val.get(o2_name)
                if last_v is None:
                    continue
                cross_ts = self.o2_last_cross_ts.get(o2_name, 0.0)
                time_since_cross = now - cross_ts if cross_ts > 0 else 999
                if time_since_cross > 45 and self.samples > 40:
                    stuck_side = "HIGH" if last_v > 0.55 else "LOW" if last_v < 0.35 else "MID"
                    level = "CRIT" if time_since_cross > 90 else "WARN"
                    current_flags.append((now, level,
                        f"Upstream O2 ({label}) stuck {stuck_side} ({last_v:.3f}V) — no switch in {int(time_since_cross)}s"))

        # Catalyst temp sanity (optional extra; may not be supported on 1998 OBD)
        for cat_name, cat_label in (("CATALYST_TEMP_B1S1", "B1"), ("CATALYST_TEMP_B2S1", "B2")):
            cat = data.get(cat_name)
            if cat is not None:
                cval = cat.magnitude if hasattr(cat, "magnitude") else cat
                if isinstance(cval, (int, float)) and cval > 1560:   # ~850°C
                    current_flags.append((now, "WARN", f"Cat temp {cat_label} {cval:.0f}°F — running very hot"))

        # Merge into main flag list (dedup similar recent messages)
        with self._lock:
            for f in current_flags:
                # simple dedup: don't add identical message if one is very recent
                if not any(abs(f[0] - ff[0]) < 12 and f[2] == ff[2] for ff in self.flags):
                    self.flags.append(f)
                    self.flag_counts[f[1]] += 1
            # prune old
            if now - self._last_flag_prune > 5:
                cutoff = now - 180  # keep last 3 minutes of flags
                self.flags = [f for f in self.flags if f[0] > cutoff]
                self._last_flag_prune = now
                if len(self.flags) > 30:
                    self.flags = self.flags[-25:]

    # ---------------- DTC / MIL (Mode 03 / 07 / 04 / 01 STATUS) ----------------

    def refresh_dtcs(self) -> None:
        """Poll stored + pending DTCs and MIL status. Works live or injects demo DTCs."""
        if self.demo:
            # Occasional demo codes so the panel isn't empty forever
            import random
            self.stored_dtcs = []
            self.pending_dtcs = []
            self.freeze_dtc = None
            self.mil_on = False
            self.dtc_count = 0
            if random.random() < 0.35:
                self.stored_dtcs = [
                    ("P0171", "System Too Lean (Bank 1)"),
                ]
                self.mil_on = True
                self.dtc_count = 1
                self.freeze_dtc = ("P0171", "System Too Lean (Bank 1)")
            if random.random() < 0.15:
                self.pending_dtcs = [("P0300", "Random/Multiple Cylinder Misfire Detected")]
            self.monitor_status_text = (
                f"MIL={'ON' if self.mil_on else 'OFF'}  DTCs={self.dtc_count}  "
                f"(demo)  | enhanced Tech2-only: {FSM_ENHANCED_ONLY[:48]}…"
            )
            self.last_dtc_poll = time.time()
            self.add_flag("INFO", f"DTC poll: {len(self.stored_dtcs)} stored, {len(self.pending_dtcs)} pending")
            return

        if not self.conn:
            self.console.print("[yellow]No OBD connection for DTC poll[/yellow]")
            return

        stored: List[Tuple[str, str]] = []
        pending: List[Tuple[str, str]] = []
        freeze = None
        mil_on = None
        dtc_count = None
        mon_txt = "—"

        try:
            r = self.conn.query(obd.commands.GET_DTC)
            if not r.is_null() and r.value:
                for item in r.value:
                    if isinstance(item, (list, tuple)) and len(item) >= 1:
                        code = str(item[0])
                        desc = str(item[1]) if len(item) > 1 else ""
                        stored.append((code, desc))
        except Exception as e:
            self.console.print(f"[dim]GET_DTC error: {e}[/dim]")

        try:
            r = self.conn.query(obd.commands.GET_CURRENT_DTC)
            if not r.is_null() and r.value:
                for item in r.value:
                    if isinstance(item, (list, tuple)) and len(item) >= 1:
                        code = str(item[0])
                        desc = str(item[1]) if len(item) > 1 else ""
                        pending.append((code, desc))
        except Exception:
            pass

        try:
            r = self.conn.query(obd.commands.FREEZE_DTC)
            if not r.is_null() and r.value:
                v = r.value
                if isinstance(v, (list, tuple)) and len(v) >= 1:
                    freeze = (str(v[0]), str(v[1]) if len(v) > 1 else "")
                else:
                    freeze = (str(v), "")
        except Exception:
            pass

        try:
            r = self.conn.query(obd.commands.STATUS)
            if not r.is_null() and r.value is not None:
                st = r.value
                # python-obd Status object: .MIL, .DTC_count, etc.
                mil_on = bool(getattr(st, "MIL", None))
                dtc_count = getattr(st, "DTC_count", None)
                mon_txt = f"MIL={'ON' if mil_on else 'OFF'}"
                if dtc_count is not None:
                    mon_txt += f"  stored={dtc_count}"
                # ignition_type etc. if present
                ign = getattr(st, "ignition_type", None)
                if ign:
                    mon_txt += f"  ign={ign}"
        except Exception:
            pass

        with self._lock:
            self.stored_dtcs = stored
            self.pending_dtcs = pending
            self.freeze_dtc = freeze
            self.mil_on = mil_on
            self.dtc_count = dtc_count
            self.monitor_status_text = mon_txt or "—"
            self.last_dtc_poll = time.time()

        if stored or pending:
            self.add_flag(
                "CRIT" if mil_on else "WARN",
                f"DTCs: stored={[c for c,_ in stored]} pending={[c for c,_ in pending]}",
            )
        else:
            self.add_flag("INFO", "DTC poll: no stored or pending codes")

        self.console.print(
            f"[cyan]DTC poll[/cyan] stored={len(stored)} pending={len(pending)} "
            f"MIL={mil_on} freeze={freeze[0] if freeze else '—'}"
        )

    def clear_dtcs(self) -> None:
        """Mode 04 clear DTCs — confirm carefully on a real truck."""
        if self.demo:
            self.stored_dtcs = []
            self.pending_dtcs = []
            self.freeze_dtc = None
            self.mil_on = False
            self.dtc_count = 0
            self.monitor_status_text = "MIL=OFF  DTCs=0  (demo cleared)"
            self.add_flag("INFO", "Demo DTCs cleared")
            self.console.print("[green]Demo DTCs cleared[/green]")
            return
        if not self.conn:
            return
        try:
            r = self.conn.query(obd.commands.CLEAR_DTC)
            self.console.print(f"[yellow]CLEAR_DTC sent[/yellow] response={r.value!r}")
            self.add_flag("WARN", "CLEAR_DTC issued — re-poll to confirm")
            time.sleep(0.5)
            self.refresh_dtcs()
        except Exception as e:
            self.console.print(f"[red]CLEAR_DTC failed:[/red] {e}")

    # ---------------- Baseline & Logging Control ----------------

    def set_baseline(self):
        with self._lock:
            snap = {}
            for k, v in self.data.items():
                if hasattr(v, "magnitude"):
                    snap[k] = float(v.magnitude)
                elif isinstance(v, (int, float)):
                    snap[k] = float(v)
            self.baseline = snap
        self.console.print("[bold green]Baseline captured[/bold green] from current readings.")
        self.add_flag("INFO", "Baseline set by user")

    def add_flag(self, level: str, msg: str):
        with self._lock:
            self.flags.append((time.time(), level, msg))
            self.flag_counts[level] += 1

    def toggle_logging(self):
        if self.log_file:
            self.stop_logging()
            self.console.print("[yellow]Logging stopped[/yellow]")
            self.add_flag("INFO", "CSV logging stopped")
        else:
            self.start_logging()
            if self.log_file:
                self.console.print(f"[green]Logging to[/green] {self.log_path}")
                self.add_flag("INFO", "CSV logging started")

    def start_logging(self):
        if self.log_file:
            return
        path = self.log_path or f"silverado_obd_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.log_path = path
        try:
            self.log_file = open(path, "w", newline="")
            # Wide format: one row per snapshot with every metric as its own column
            fieldnames = ["timestamp"] + INTERESTED + ["active_flags"]
            self.log_writer = csv.DictWriter(self.log_file, fieldnames=fieldnames)
            self.log_writer.writeheader()
            self.console.print(f"[green]CSV logging started (snapshot mode):[/green] {path}")
        except Exception as e:
            self.console.print(f"[red]Failed to start log:[/red] {e}")
            self.log_file = None
            self.log_writer = None

    def stop_logging(self):
        with self._log_lock:
            if self.log_file:
                try:
                    self.log_file.flush()
                    self.log_file.close()
                except Exception:
                    pass
            self.log_file = None
            self.log_writer = None

    # ---------------- Rendering (Rich) ----------------

    def _build_header(self) -> RenderableType:
        elapsed = str(timedelta(seconds=int(time.time() - self.session_start)))
        if self.demo:
            status = "DEMO MODE"
            status_style = "bold magenta"
        else:
            port = getattr(self.conn.port, "device", str(self.conn.port)) if self.conn and self.conn.port else "?"
            status = f"Connected: {port}"
            status_style = "bold white"
        title = Text.assemble(
            ("ZipTie  •  1998 Silverado 5.7 Vortec  •  ", "bold red"),
            (elapsed, "cyan"),
            ("  •  ", "dim"),
            (f"{self.samples} samples", "dim"),
        )
        return Panel(
            Text.assemble((status, status_style), "   ", title),
            style="bold white on black",
            box=box.SIMPLE,
        )

    def _build_oil_panel(self) -> RenderableType:
        with self._lock:
            oil_switch = self.data.get("OIL_PRESSURE_SWITCH") or self.data.get("OIL_PRESSURE") or None
            oil_str = _format_value("OIL_PRESSURE_SWITCH", oil_switch) if oil_switch else "UNAVAILABLE via OBD"
        txt = Text()
        txt.append(OIL_PRESSURE_NOTE + "\n", style="yellow")
        if oil_switch:
            txt.append(f"Reported switch value: {oil_str}\n", style="bold")
        txt.append("Real pressure requires mechanical gauge.\n", style="bold red")
        txt.append(VORTEC_NOTE, style="dim")
        return Panel(txt, title="[red]Oil Pressure — ZipTie (L31)[/red]", border_style="red", box=box.ROUNDED)

    def _build_dtc_panel(self) -> RenderableType:
        with self._lock:
            stored = list(self.stored_dtcs)
            pending = list(self.pending_dtcs)
            freeze = self.freeze_dtc
            mil = self.mil_on
            mon = self.monitor_status_text
            age = int(time.time() - self.last_dtc_poll) if self.last_dtc_poll else None

        t = Text()
        mil_style = "bold red" if mil else "bold green"
        mil_str = "ON" if mil is True else ("OFF" if mil is False else "—")
        t.append(f"MIL: {mil_str}", style=mil_style)
        t.append(f"   {mon}\n", style="dim")
        if age is not None:
            t.append(f"Last poll: {age}s ago  (press d to refresh)\n", style="dim")
        else:
            t.append("Press d + Enter to read DTCs\n", style="cyan")

        if freeze:
            t.append(f"Freeze frame DTC: {freeze[0]}", style="yellow")
            if freeze[1]:
                t.append(f" — {freeze[1]}", style="dim")
            t.append("\n")

        if not stored and not pending:
            t.append("No stored or pending codes loaded.\n", style="green")
        else:
            if stored:
                t.append("Stored:\n", style="bold")
                for code, desc in stored[:8]:
                    t.append(f"  {code}", style="bold red")
                    t.append(f"  {desc}\n" if desc else "\n", style="white")
            if pending:
                t.append("Pending:\n", style="bold")
                for code, desc in pending[:6]:
                    t.append(f"  {code}", style="bold yellow")
                    t.append(f"  {desc}\n" if desc else "\n", style="white")

        t.append(
            "\nTech2-only (not Mode 01): Desired Idle, IAC, Knock Retard, "
            "per-cyl misfire, Injector PWM, Oil Level…",
            style="dim",
        )
        border = "red" if mil or stored else "green"
        return Panel(t, title="[bold]DTCs & MIL[/bold]", border_style=border, box=box.ROUNDED)

    def _build_timing_panel(self) -> RenderableType:
        with self._lock:
            timing = self.data.get("TIMING_ADVANCE")
            timing_str = _format_value("TIMING_ADVANCE", timing) if timing is not None else "—"
            load = self.data.get("ENGINE_LOAD")
            load_str = _format_value("ENGINE_LOAD", load) if load is not None else "—"

            hist = list(self.history.get("TIMING_ADVANCE", []))
            spark = _sparkline([x[1] for x in hist]) if hist else ""

        body = Text()
        body.append(f"Current: {timing_str}    Load: {load_str}\n", style="bold")
        body.append(f"Trend: {spark}\n", style="cyan")
        body.append(TIMING_NOTE, style="dim")
        return Panel(body, title="Ignition Timing Advance", border_style="blue", box=box.ROUNDED)

    def _build_metrics_table(self) -> RenderableType:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE,
            padding=(0, 1),
        )
        table.add_column("Metric", style="white")
        table.add_column("Value", justify="right")
        table.add_column("Min", justify="right", style="dim")
        table.add_column("Max", justify="right", style="dim")
        table.add_column("ΔBase", justify="right")
        table.add_column("Trend", style="cyan")

        with self._lock:
            data = dict(self.data)
            hist = {k: list(v) for k, v in self.history.items()}
            base = dict(self.baseline)

        for name in INTERESTED:
            if name not in METRIC_META:
                continue
            meta = METRIC_META[name]
            val = data.get(name)
            val_str = _format_value(name, val)
            color = _color_for_value(name, val.magnitude if hasattr(val, "magnitude") else val)

            h = hist.get(name, [])
            nums = [x[1] for x in h]
            minv = f"{min(nums):.1f}" if nums else "—"
            maxv = f"{max(nums):.1f}" if nums else "—"

            # delta from baseline
            delta = "—"
            if name in base:
                b = base[name]
                v = val.magnitude if hasattr(val, "magnitude") else val
                if isinstance(v, (int, float)) and isinstance(b, (int, float)):
                    d = v - b
                    delta = f"{d:+.1f}"

            spark = _sparkline(nums, width=14) if len(nums) >= 3 else ""

            table.add_row(
                meta["label"],
                Text(val_str, style=color),
                minv,
                maxv,
                delta,
                spark,
            )
        return table

    def _build_alerts_panel(self) -> RenderableType:
        self.analyze_and_flag()
        with self._lock:
            flags = list(self.flags)

        if not flags:
            content = Text("No active flags. All monitored parameters within expected bands so far.", style="green")
        else:
            t = Table(show_header=False, box=box.SIMPLE, padding=(0, 0))
            for ts, level, msg in sorted(flags, key=lambda x: -x[0])[:12]:
                age = int(time.time() - ts)
                t.add_row(
                    Text(f"{level:4}", style=_severity_color(level)),
                    Text(f"{msg} ", style="white"),
                    Text(f"({age}s ago)", style="dim"),
                )
            content = t

        return Panel(content, title="Alerts & Flags (changes + thresholds)", border_style="yellow", box=box.ROUNDED)

    def _build_footer(self) -> RenderableType:
        cmds = "b=baseline  l=log  d=read DTCs  x=clear DTCs  c=clear flags  q=quit"
        log_status = f"Logging: {'ON' if self.log_file else 'OFF'}"
        return Text(f"{log_status}   |   {cmds}", style="dim")

    def get_renderable(self) -> RenderableType:
        try:
            header = self._build_header()
            oil = self._build_oil_panel()
            timing = self._build_timing_panel()
            dtc = self._build_dtc_panel()
            metrics = self._build_metrics_table()
            alerts = self._build_alerts_panel()
            footer = self._build_footer()

            # Layout: header, left stack (oil/timing/dtc) | metrics, alerts, footer
            left = Group(oil, timing, dtc)
            right = metrics

            top = Table.grid(expand=True)
            top.add_row(left, right)

            return Group(
                header,
                top,
                alerts,
                footer,
            )
        except Exception as e:
            return Panel(f"Dashboard render error: {e}", border_style="red")

    # ---------------- Input Handling (Linux-friendly) ----------------

    def _handle_input(self, line: str):
        line = line.strip().lower()
        if not line:
            return
        if line in ("q", "quit", "exit"):
            self.stop()
        elif line in ("b", "baseline"):
            self.set_baseline()
        elif line in ("l", "log"):
            self.toggle_logging()
        elif line in ("c", "clear"):
            with self._lock:
                self.flags.clear()
            self.console.print("[dim]Flags cleared[/dim]")
        elif line in ("d", "dtc", "codes"):
            self.refresh_dtcs()
        elif line in ("x", "clearcodes", "clear_dtc"):
            self.clear_dtcs()
        elif line in ("h", "help", "?"):
            self.console.print(Panel(
                "b / baseline   — capture current readings as your personal baseline\n"
                "l / log        — start or stop CSV logging\n"
                "d / dtc        — read stored + pending DTCs and MIL status\n"
                "x / clearcodes — Mode 04 clear DTCs (use carefully)\n"
                "c / clear      — clear the alerts panel\n"
                "q / quit       — exit cleanly\n"
                "Any other key shows this help.",
                title="ZipTie commands (type + Enter)",
            ))
        else:
            self.console.print("[dim]Unknown command. Type h for help.[/dim]")

    def _input_loop(self):
        """Non-blocking-ish input on Linux using select."""
        while self.running:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.15)
                if r:
                    line = sys.stdin.readline()
                    if line:
                        self._handle_input(line)
            except Exception:
                # stdin issues (e.g. when piped) — just sleep
                time.sleep(0.3)

    # ---------------- Main Run Loop ----------------

    def run(self):
        if not self.connect():
            return 1

        self.running = True

        # Optional auto-log from CLI
        if self.log_path:
            self.start_logging()

        self.start_watching()

        # Start input thread (best effort)
        self._input_thread = threading.Thread(target=self._input_loop, daemon=True)
        self._input_thread.start()

        # Live dashboard — update() pattern; screen=False is more stable on some terminals
        try:
            initial = self.get_renderable()
        except Exception:
            initial = Text("Starting…")

        self._live = Live(
            initial,
            console=self.console,
            refresh_per_second=4,
            screen=True,
            transient=False,
            auto_refresh=False,  # we drive updates ourselves
        )

        try:
            with self._live:
                while self.running:
                    try:
                        # Periodic full-row CSV snapshot
                        if self.log_writer:
                            now = time.time()
                            if now - self._last_log_ts >= self.log_interval:
                                self._write_snapshot()
                                self._last_log_ts = now
                        # Light periodic DTC refresh in demo so the panel isn't empty
                        if self.demo and (time.time() - self.last_dtc_poll > 45 or self.last_dtc_poll == 0):
                            try:
                                self.refresh_dtcs()
                            except Exception:
                                pass
                        # Refresh the dashboard with current data
                        self._live.update(self.get_renderable())
                        self._live.refresh()
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        # Never let a single bad frame kill the session
                        try:
                            self.console.print(f"[dim]render hiccup: {e}[/dim]")
                        except Exception:
                            pass
                    time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            try:
                self.console.print(f"[red]Live UI error:[/red] {e}")
            except Exception:
                pass
        finally:
            self.stop()
        return 0

    def _print_session_summary(self):
        """Pretty exit report of what we saw this run."""
        elapsed = timedelta(seconds=int(time.time() - self.session_start))
        lines = []
        lines.append(f"Session length : {elapsed}")
        lines.append(f"Samples        : {self.samples}")
        lines.append(f"O2 switches    : {self.o2_switch_count}")

        # Flag tally
        if self.flag_counts:
            parts = [f"{lvl}:{cnt}" for lvl, cnt in sorted(self.flag_counts.items())]
            lines.append(f"Flags raised   : {', '.join(parts)}")
        else:
            lines.append("Flags raised   : none")

        # Key min/max
        interesting = [
            ("COOLANT_TEMP", "Coolant"),
            ("INTAKE_TEMP", "Intake"),
            ("TIMING_ADVANCE", "Timing"),
            ("LONG_FUEL_TRIM_1", "LTFT B1"),
            ("LONG_FUEL_TRIM_2", "LTFT B2"),
            ("SHORT_FUEL_TRIM_1", "STFT B1"),
            ("SHORT_FUEL_TRIM_2", "STFT B2"),
            ("CONTROL_MODULE_VOLTAGE", "Voltage"),
            ("ABSOLUTE_LOAD", "Abs Load"),
            ("MAF", "MAF"),
            ("RPM", "RPM"),
        ]
        lines.append("")
        lines.append("Key ranges this session:")
        for key, label in interesting:
            st = self.stats.get(key)
            if st and st["min"] != float("inf"):
                unit = METRIC_META.get(key, {}).get("unit", "")
                fmt = METRIC_META.get(key, {}).get("fmt", ".1f")
                try:
                    mn = format(st["min"], fmt)
                    mx = format(st["max"], fmt)
                    lines.append(f"  {label:12}  {mn} → {mx} {unit}".rstrip())
                except Exception:
                    lines.append(f"  {label:12}  {st['min']:.2f} → {st['max']:.2f}")

        if self.log_path and self.log_file is None:  # already closed
            lines.append("")
            lines.append(f"Log written to : {self.log_path}")

        body = "\n".join(lines)
        self.console.print()
        self.console.print(Panel(
            body,
            title="[bold red]Session Summary — ZipTie (L31 5.7)[/bold red]",
            border_style="cyan",
            box=box.ROUNDED,
        ))
        self.console.print("[bold red]ZipTie monitor stopped. Drive safe.[/bold red]\n")

    def stop(self):
        self.running = False
        if self.conn:
            try:
                self.conn.stop()
                self.conn.close()
            except Exception:
                pass
        self.stop_logging()
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
        try:
            self._print_session_summary()
        except Exception:
            self.console.print("\n[bold]Monitor stopped. Drive safe.[/bold]")

    def add_manual_flag_for_test(self, msg: str):
        """Helper for development/testing."""
        self.add_flag("INFO", msg)

    def get_snapshot(self) -> Dict[str, Any]:
        """Return current state as a JSON-serializable dict for the web dashboard."""
        with self._lock:
            data = {}
            for name, val in self.data.items():
                if hasattr(val, "magnitude"):
                    data[name] = float(val.magnitude)
                elif isinstance(val, (int, float)):
                    data[name] = float(val)
                elif isinstance(val, (list, tuple)):
                    data[name] = [str(x) for x in val]
                else:
                    data[name] = str(val) if val is not None else None

            hist = {}
            for k, dq in self.history.items():
                hist[k] = [v for _, v in list(dq)[-40:]]

            flags = [
                {"ts": ts, "level": level, "msg": msg, "age": int(time.time() - ts)}
                for ts, level, msg in list(self.flags)[-15:]
            ]

            stats_out = {}
            for k, st in self.stats.items():
                if st["min"] != float("inf"):
                    stats_out[k] = {"min": st["min"], "max": st["max"]}

        with self._lock:
            dtc_payload = {
                "mil_on": self.mil_on,
                "dtc_count": self.dtc_count,
                "monitor": self.monitor_status_text,
                "stored": [{"code": c, "desc": d} for c, d in self.stored_dtcs],
                "pending": [{"code": c, "desc": d} for c, d in self.pending_dtcs],
                "freeze": (
                    {"code": self.freeze_dtc[0], "desc": self.freeze_dtc[1]}
                    if self.freeze_dtc else None
                ),
                "last_poll_age": int(time.time() - self.last_dtc_poll) if self.last_dtc_poll else None,
            }

        return {
            "demo": self.demo,
            "truck": {
                "name": TRUCK_NAME,
                "color": TRUCK_COLOR,
                "year": TRUCK_YEAR,
                "model": TRUCK_MODEL,
                "engine": TRUCK_ENGINE,
            },
            "samples": self.samples,
            "elapsed": int(time.time() - self.session_start),
            "o2_switches": self.o2_switch_count,
            "data": data,
            "history": hist,
            "flags": flags,
            "stats": stats_out,
            "baseline": {k: float(v) for k, v in self.baseline.items()} if self.baseline else {},
            "dtc": dtc_payload,
            "fsm_idle_ref": FSM_IDLE_REF,
            "enhanced_only": FSM_ENHANCED_ONLY,
        }


# -----------------------------------------------------------------------------
# Simple local web server (optional --web mode)
# -----------------------------------------------------------------------------

_HTML_PATH = Path(__file__).resolve().parent / "silverado_dashboard.html"
_monitor_ref: Optional[SilveradoMonitor] = None


class _DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # quieter than default
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/dashboard"):
            if not _HTML_PATH.exists():
                self.send_error(404, "silverado_dashboard.html not found next to the script")
                return
            body = _HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/data":
            if _monitor_ref is None:
                self.send_error(503, "Monitor not ready")
                return
            try:
                payload = json.dumps(_monitor_ref.get_snapshot()).encode("utf-8")
            except Exception as e:
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(404)


def _start_web_server(monitor: SilveradoMonitor, port: int = 8765):
    global _monitor_ref
    _monitor_ref = monitor
    server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monitor.console.print(
        f"[bold cyan]Web dashboard:[/bold cyan] http://127.0.0.1:{port}/  "
        f"(API: /api/data)"
    )
    return server


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="1998 Chevy Silverado 1500 5.7L Vortec (L31) OBD-II real-time monitor + issue flagging"
    )
    parser.add_argument("--port", "-p", default=None,
                        help="Serial port (e.g. /dev/ttyUSB0). Omit for auto-detect.")
    parser.add_argument("--log", "-l", action="store_true",
                        help="Start CSV logging immediately (file named with timestamp).")
    parser.add_argument("--log-file", default=None,
                        help="Explicit path for CSV log instead of auto name.")
    parser.add_argument("--interval", "-i", type=float, default=0.5,
                        help="Approximate poll / display interval (seconds). Default 0.5")
    parser.add_argument("--demo", action="store_true",
                        help="Run in demo mode with simulated L31 Vortec 5.7 data (no adapter needed)")
    parser.add_argument("--web", action="store_true",
                        help="Also start a local web dashboard (browser gauges)")
    parser.add_argument("--web-port", type=int, default=8765,
                        help="Port for the web dashboard (default 8765)")
    parser.add_argument("--no-color", action="store_true", help="Disable rich colors (rarely needed)")
    args = parser.parse_args()

    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    log_path = args.log_file
    if args.log and not log_path:
        log_path = f"silverado_obd_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    monitor = SilveradoMonitor(
        port=args.port,
        log_path=log_path,
        poll_interval=args.interval,
        demo=args.demo,
    )

    # Graceful signal handling
    def _sigint(sig, frame):
        monitor.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    if args.web:
        _start_web_server(monitor, port=args.web_port)

    sys.exit(monitor.run())


if __name__ == "__main__":
    main()
