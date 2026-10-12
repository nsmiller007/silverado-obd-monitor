# ZipTie OBD-II Monitor — 1998 Silverado 1500 5.7L Vortec (L31)

Real-time OBD-II dashboard + anomaly flagging for **ZipTie** — fire-engine red **1998 Chevy Silverado 1500** with the **5.7L Vortec L31 V8** (GMT-400 / C/K).

Same family as the Tacoma monitor: USB ELM327 → live terminal UI + optional browser gauges, CSV logs, baseline deltas, smart flags, **and DTC read/clear**.

## Truck profile

| | |
|--|--|
| Name | **ZipTie** |
| Color | **Fire engine red** |
| Year / model | 1998 Silverado 1500 |
| Engine | 5.7L Vortec L31 V8 (350) |
| Bus | J1850 VPW (Class 2), not CAN |

## ⚠️ Critical limitations

### J1850 VPW (Class 2)

1996–early 2000s GM trucks speak **VPW**, not CAN. Use an adapter that actually supports protocol 2 (OBDLink USB recommended). Many cheap “ELM327 v2.1” clones are CAN-only garbage.

### Oil pressure — FSM numbers (mechanical only)

From the **1998 GMT-98 C/K Service Manual, Engine Mechanical – 5.0L/5.7L (L31)**:

| Condition | Minimum hot oil pressure |
|-----------|--------------------------|
| 1,000 RPM | **6.0 psig** (41.4 kPa) |
| 2,000 RPM | **18.0 psig** (124.1 kPa) |
| 4,000 RPM | **24.0 psig** (165.5 kPa) |

Standard OBD-II does **not** stream quantitative oil PSI on this truck. The dash uses a sender; there is also a **fuel-pump oil-pressure switch** (on/off backup path), not a live PID. **Mechanical gauge** is the real tool.

Later GM “Mode 22 oil pressure” PIDs floating around Torque Pro forums (e.g. `22115C`) are for **newer CAN trucks**, not reliable on 1998 VPW.

## What we added after FSM + web research

### Standard OBD modes (supported by python-obd, truck-dependent)

| Feature | Mode / PID | Notes |
|---------|------------|--------|
| **Read stored DTCs** | Mode 03 | Key `d` in TUI |
| **Read pending DTCs** | Mode 07 | Same poll |
| **Clear DTCs** | Mode 04 | Key `x` — use carefully |
| **MIL + DTC count** | Mode 01 PID 01 `STATUS` | |
| Freeze-frame DTC | Mode 01 PID 02 | |
| Distance with MIL on | PID 21 | Flagged if ≥ 1 mi |
| Warm-ups since clear | PID 30 | |
| Distance / time since clear | PID 31 / 4E | |
| Commanded EGR / EGR error | PID 2C / 2D | If PCM supports |
| EVAP vapor pressure | PID 32 | If supported |
| Fuel pressure (gauge) | PID 0A | Often **not** on 1998 L31 |
| OBD compliance / O2 present | PID 1C / 13 | |
| Adapter voltage | ATRV | ELM rail check |

### Already watched (V8 dual bank)

RPM, speed, coolant/IAT, spark advance, load, throttle, MAF, MAP, baro, **STFT/LTFT B1+B2**, O2 B1S1/B1S2/B2S1/B2S2, module voltage, fuel status/level/rate, cat temps (if present).

### FSM Tech 2 scan data — **not** generic Mode 01

From **Engine Controls – 5.0, 5.7L → Scan Tool Data List** (warm idle, closed loop, closed throttle):

| Parameter | Typical (FSM) | Over cheap ELM? |
|-----------|---------------|-----------------|
| Desired Idle Speed | **650 RPM** | No (enhanced) |
| IAC Position | **~18 counts** | No |
| Spark | **~21°** | Yes via timing advance PID |
| MAP | **~38 kPa / 1.46 V** | MAP yes; voltage often enhanced |
| MAF | **~4.6 g/s** | Yes |
| MAF Frequency | ~2285 Hz | Enhanced |
| Knock Retard | 0–2° | Enhanced |
| Knock Sensor | ~4.8 V | Enhanced |
| EGR Actual/Desired/Duty | 0% idle | Partial via commanded EGR if supported |
| Injector PWM B1/B2 | ~3.6 ms | Enhanced |
| Per-cylinder misfire counts | 0 | Enhanced |
| Engine Oil Level Low | No | Enhanced |
| Fuel Trim Cell | Cell 17 | Enhanced |
| EVAP Duty Cycle | ~17% | Enhanced |
| Ignition 1 voltage | **~13.5 V** | Module voltage PID close enough |

Those enhanced parameters need a **GM enhanced / Tech 2–class** tool (or reverse-engineered Class 2 extended requests), not plain SAE Mode 01. The app surfaces that note so nobody expects IAC/knock retard from a $15 dongle.

### Warm idle reference (app uses for context)

FSM Engine Data 1 (hot, closed loop, P/N, accessories off):

- Idle ~**650 RPM**, load ~**2%** idle / ~**11%** @ 2500
- AFR **14.7:1**, module **~13.5 V**

## Hardware

1. Quality USB OBD adapter with **J1850 VPW**
2. Linux dialout:

```bash
sudo usermod -a -G dialout $USER
# log out / back in
```

3. Port:

```bash
ls /dev/ttyUSB* /dev/ttyACM*
python3 -m serial.tools.list_ports
```

## Install & run

```bash
cd silverado-obd-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Demo (no truck):**

```bash
python3 silverado_monitor.py --demo
python3 silverado_monitor.py --demo --web   # http://127.0.0.1:8765/
```

**Live ZipTie:**

```bash
python3 silverado_monitor.py --port /dev/ttyUSB0 --log --web
```

### Terminal keys

| Key | Action |
|-----|--------|
| `b` | Capture baseline |
| `l` | Toggle CSV logging |
| **`d`** | **Read stored + pending DTCs + MIL** |
| **`x`** | **Clear DTCs (Mode 04)** |
| `c` | Clear alert panel |
| `q` | Quit |

## Monitoring strategy

1. Key off → connect → confirm **VPW** protocol  
2. Start, warm fully (~185–210°F coolant)  
3. Press **`d`** for codes before chasing live data  
4. Baseline at warm idle (~650 RPM target)  
5. Watch both banks LTFT/STFT, timing under load, voltage ~13.8–14.5 V  
6. Bank mismatch → bank-specific (O2, exhaust leak, injector)  
7. Log CSV for later

## Sources

- 1998 GMT-98 C/K Service Manual Vol. 3 — Engine Mechanical 5.0/5.7 (oil pressure specs), Engine Controls 5.0/5.7 (scan tool data list, spark/IAC/MAF/MAP typicals)
- SAE J1979 Mode 01–04 / 07 standard PIDs  
- python-obd command tables  
- Community notes: Mode 22 oil PIDs are later-GM / often CAN-era — not assumed for 1998 VPW

## Safety

Diagnostic aid only. Not a substitute for a mechanical oil gauge or proper repair. Eyes on the road. Mode 04 clear wipes readiness — don’t clear right before an emissions test unless you mean to.

---

**ZipTie** · Fire engine red · Drive safe.
