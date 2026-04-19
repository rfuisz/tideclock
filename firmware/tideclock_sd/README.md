# tideclock_sd firmware

ESP32-S3 sketch that reads pre-computed NOAA tide events from a microSD
card and computes a clock-hand angle (0 deg = high tide at 12, 180 deg =
low tide at 6) for the current moment.

## Target hardware

- Waveshare ESP32-S3-A-SIM7670X-4G (tested)
- Any ESP32-S3 with a microSD slot should work; adjust the `SD_CS`,
  `SD_SCK`, `SD_MISO`, `SD_MOSI` pins at the top of the sketch.

## Prep the SD card

1. Run `python3 scripts/export_binary.py` from the repo root to generate
   the binary files under `data/sdcard/`.
2. Copy the contents of `data/sdcard/` to a `/tide/` directory at the
   root of a microSD card (FAT32 or exFAT). Final layout on card:
   ```
   /tide/stations.bin
   /tide/offsets.bin
   /tide/hilo/9414290.dat
   /tide/hilo/9447130.dat
   ...
   ```
3. Total payload is ~500 MB (884 reference stations, 50 years each).
   Any card >= 1 GB is plenty; the project uses a 256 GB card.

## Arduino IDE setup

- Board: ESP32S3 Dev Module (or the Waveshare variant if installed)
- Tools > USB CDC On Boot: Disabled
- Tools > Partition Scheme: default is fine
- Libraries (Library Manager): **Adafruit NeoPixel**

## Setting the time

The sketch reads the system RTC via `time(nullptr)`. Before it returns
meaningful values you must set the clock. Options:

- **NTP over WiFi** (simplest, if your deployment has WiFi):
  ```c
  configTzTime("UTC0", "pool.ntp.org", "time.google.com");
  ```
- **SIM7670 modem** (the Waveshare board's 4G module):
  `AT+CCLK?` returns network time once registered.
- **External RTC chip** (DS3231 etc.) over I2C.

If the RTC isn't set yet, the sketch falls back to a safe lower bound so
the binary search doesn't crash.

## Selecting the station

Create `/tide/config.txt` on the SD card with one line containing the
NOAA station ID:

```
9447130
```

The sketch reads this at boot. If the file is missing it falls back to
`DEFAULT_STATION_ID` at the top of the .ino (currently San Francisco
9414290). Find station IDs by browsing `data/stations.json` or
NOAA's map: https://tidesandcurrents.noaa.gov/.

Future work: GPS auto-select using the SIM7670 GNSS + `stations.bin`.

## Output (demo mode)

Until the clock hands are wired up, the sketch drives the on-board
NeoPixel:
- **blue** during a falling tide (H -> L)
- **green** during a rising tide (L -> H)

And prints a serial line every 10 s:

```
t=1745012345  prev=L@1745008020(+1.60ft)  next=H@1745031420(+6.29ft)  angle=218.3 deg  h=+2.14 ft
```

## Driving a motor (TBD)

Replace the NeoPixel block in `loop()` with whatever actuator you're
using. The `angle` variable is 0..360 degrees and can be mapped directly
to a stepper or servo position. For a stepper with 4096 steps/rev,
`target_step = (uint32_t)(angle * 4096.0 / 360.0);`.
