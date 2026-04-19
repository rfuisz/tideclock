// tideclock_sd: read pre-computed NOAA hi/lo tide tables from SD and drive
// a clock-hand angle (0 deg = high tide, 180 deg = low tide).
//
// Platform: ESP32-S3-A-SIM7670X-4G (Waveshare). Other ESP32-S3 boards
// work too; adjust SD_CS and the SPI pins below. Board's microSD uses
// SPI mode here (works with the Arduino core "SD" library). The
// SIM7670 modem can supply network time via AT+CCLK if you wire it up;
// this sketch just uses the Arduino RTC / compile-time fallback.
//
// SD card layout (exported by scripts/export_binary.py):
//   /tide/stations.bin     -- station index (72 B records)
//   /tide/offsets.bin      -- subordinate offsets (32 B records)
//   /tide/hilo/<id>.dat    -- per-station hi/lo events (8 B records)
//
// Binary formats (all little-endian):
//   stations.bin header: "STNS" u16 ver u16 flags u32 count 4B pad
//   stations.bin record: id[10] parent[10] state[2] type c f32 lat f32 lng name[40]
//   offsets.bin  header: "OFST" u16 ver u16 flags u32 count 4B pad
//   offsets.bin  record: sub[10] ref[10] i16 t_hi i16 t_lo i16 h_hi i16 h_lo 4B pad
//   <id>.dat     header: "TIDE" u16 ver u16 flags u32 count 4B pad
//   <id>.dat     record: u32 ts_since_2000 i16 height_cft u8 type u8 pad
//
// Libraries required (Arduino IDE Library Manager):
//   - SD (built-in)
//   - Adafruit NeoPixel (for the demo LED color)

#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_NeoPixel.h>
#include <time.h>

// --- Pin config (Waveshare ESP32-S3-A-SIM7670X-4G) ---------------------
// The Waveshare board exposes the SD card on a dedicated SPI bus. Adjust
// these if your wiring differs. See the board pinout PDF.
static const int SD_CS    = 10;
static const int SD_SCK   = 12;
static const int SD_MISO  = 13;
static const int SD_MOSI  = 11;

static const int NEOPIXEL_PIN = 38;

// Hardcoded station for now -- replace with a preferences/config read once
// the UI is wired up. "9414290" = San Francisco.
static const char DEFAULT_STATION_ID[] = "9414290";

// --- Binary record structs (must match export_binary.py) ---------------
#pragma pack(push, 1)
struct TideHeader {
  char     magic[4];   // "TIDE"
  uint16_t version;
  uint16_t flags;
  uint32_t count;
  uint8_t  _reserved[4];
};
struct TideRecord {
  uint32_t ts;       // seconds since 2000-01-01T00:00:00Z
  int16_t  height;   // hundredths of a foot, MSL-relative
  uint8_t  type;     // 'H' (0x48) or 'L' (0x4C)
  uint8_t  _pad;
};
struct StationsHeader {
  char     magic[4];   // "STNS"
  uint16_t version;
  uint16_t flags;
  uint32_t count;
  uint8_t  _reserved[4];
};
struct StationRecord {
  char  id[10];
  char  parent[10];
  char  state[2];
  char  type;       // 'R' or 'S'
  char  _pad;
  float lat;
  float lng;
  char  name[40];
};
struct OffsetRecord {
  char    sub[10];
  char    ref[10];
  int16_t t_hi;     // minutes
  int16_t t_lo;
  int16_t h_hi;     // hundredths of a foot (additive)
  int16_t h_lo;
  uint8_t _pad[4];
};
#pragma pack(pop)

// Seconds between 1970-01-01 (Unix epoch) and 2000-01-01 (our tide epoch)
static const uint32_t TIDE_EPOCH_OFFSET = 946684800UL;

Adafruit_NeoPixel pixel(1, NEOPIXEL_PIN, NEO_GRB + NEO_KHZ800);

// -----------------------------------------------------------------------
// Binary search into a per-station .dat file.
// Sets *prev to the last event with ts <= target, *next to the first with
// ts > target. Returns false if target is outside the file's range.

static bool findBracket(File& f, uint32_t target_ts,
                        TideRecord* prev, TideRecord* next) {
  TideHeader hdr;
  if (!f.seek(0)) return false;
  if (f.read((uint8_t*)&hdr, sizeof(hdr)) != sizeof(hdr)) return false;
  if (memcmp(hdr.magic, "TIDE", 4) != 0 || hdr.version != 1) return false;
  if (hdr.count < 2) return false;

  const size_t header_size = sizeof(hdr);
  const size_t rec_size = sizeof(TideRecord);
  uint32_t lo = 0, hi = hdr.count - 1;
  TideRecord r_lo, r_hi;

  f.seek(header_size + (uint32_t)lo * rec_size);
  f.read((uint8_t*)&r_lo, rec_size);
  f.seek(header_size + (uint32_t)hi * rec_size);
  f.read((uint8_t*)&r_hi, rec_size);

  if (target_ts < r_lo.ts || target_ts > r_hi.ts) return false;

  // Binary search for largest index with ts <= target.
  while (hi - lo > 1) {
    uint32_t mid = (lo + hi) / 2;
    TideRecord r;
    f.seek(header_size + (uint32_t)mid * rec_size);
    f.read((uint8_t*)&r, rec_size);
    if (r.ts <= target_ts) { lo = mid; r_lo = r; }
    else                   { hi = mid; r_hi = r; }
  }
  *prev = r_lo;
  *next = r_hi;
  return true;
}

// Convert (prev, next, now) to a clock-hand angle in degrees.
// 0 deg at high tide, 180 deg at low tide, 360 deg back at next high.
// Uses a cosine curve so the hand sweeps sinusoidally rather than linearly.
static float tideAngleDegrees(const TideRecord& prev, const TideRecord& next,
                              uint32_t now_ts) {
  double span = (double)(next.ts - prev.ts);
  if (span <= 0.0) return 0.0f;
  double frac = (double)(now_ts - prev.ts) / span;  // 0..1 between events
  if (frac < 0.0) frac = 0.0;
  if (frac > 1.0) frac = 1.0;

  // Cosine interp: at prev (frac=0) phase = 0 deg; at next (frac=1) = 180 deg.
  // Then offset depending on prev.type: H->L goes 0->180, L->H goes 180->360.
  double phase_in_half = 180.0 * frac;
  if (prev.type == 'L') return (float)(180.0 + phase_in_half);
  return (float)phase_in_half;
}

// Current tide height in hundredths of ft, interpolated with cosine.
static int16_t tideHeightCft(const TideRecord& prev, const TideRecord& next,
                             uint32_t now_ts) {
  double span = (double)(next.ts - prev.ts);
  if (span <= 0.0) return prev.height;
  double frac = (double)(now_ts - prev.ts) / span;
  if (frac < 0.0) frac = 0.0;
  if (frac > 1.0) frac = 1.0;
  // Amplitude mid + cos curve. cos(pi * frac) goes 1 -> -1 as frac 0->1.
  double a = 0.5 * (prev.height + next.height);
  double b = 0.5 * (prev.height - next.height);
  return (int16_t)(a + b * cos(M_PI * frac));
}

// Get current epoch seconds. Replace this with a network-time source
// (SIM7670 AT+CCLK, NTP over WiFi, or an RTC) before deploying.
static uint32_t currentEpochSeconds() {
  time_t now = time(nullptr);
  if (now < 1700000000) {
    // RTC not set yet; use compile-time as a safe minimum so the binary
    // search has *something* to bracket on.
    return (uint32_t)1700000000UL;
  }
  return (uint32_t)now;
}

// -----------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("=== tideclock_sd ===");

  pixel.begin();
  pixel.setBrightness(40);
  pixel.clear();
  pixel.show();

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  if (!SD.begin(SD_CS, SPI, 20000000)) {
    Serial.println("FATAL: SD.begin failed");
    while (1) delay(1000);
  }
  Serial.printf("SD mounted. Card size: %llu MB\n",
                SD.cardSize() / (1024ULL * 1024ULL));

  // Sanity-check: can we see the data directory?
  if (!SD.exists("/tide/hilo")) {
    Serial.println("FATAL: /tide/hilo not found on SD");
    while (1) delay(1000);
  }
  Serial.println("SD contents look ok.");
}

void loop() {
  char path[64];
  snprintf(path, sizeof(path), "/tide/hilo/%s.dat", DEFAULT_STATION_ID);

  File f = SD.open(path, FILE_READ);
  if (!f) {
    Serial.printf("cannot open %s\n", path);
    pixel.setPixelColor(0, pixel.Color(255, 0, 0));
    pixel.show();
    delay(2000);
    return;
  }

  uint32_t now_unix = currentEpochSeconds();
  uint32_t target_ts = now_unix - TIDE_EPOCH_OFFSET;

  TideRecord prev, next;
  bool ok = findBracket(f, target_ts, &prev, &next);
  f.close();

  if (!ok) {
    Serial.printf("time %lu out of range for %s\n",
                  (unsigned long)target_ts, DEFAULT_STATION_ID);
    pixel.setPixelColor(0, pixel.Color(128, 0, 128));
    pixel.show();
    delay(5000);
    return;
  }

  float angle = tideAngleDegrees(prev, next, target_ts);
  int16_t h_cft = tideHeightCft(prev, next, target_ts);

  // Demo: map the clock angle to a color.
  // Rising (270-360/0-90) = green, falling (90-270) = blue, with brightness
  // modulated by whether we're near a peak.
  uint8_t r = 0, g = 0, b = 0;
  if (prev.type == 'H') {
    // Falling: blue
    b = 255;
  } else {
    // Rising: green
    g = 255;
  }
  pixel.setPixelColor(0, pixel.Color(r, g, b));
  pixel.show();

  Serial.printf(
      "t=%lu  prev=%c@%lu(%.2fft)  next=%c@%lu(%.2fft)  "
      "angle=%.1f deg  h=%.2f ft\n",
      (unsigned long)now_unix,
      prev.type, (unsigned long)(prev.ts + TIDE_EPOCH_OFFSET),
      prev.height / 100.0f,
      next.type, (unsigned long)(next.ts + TIDE_EPOCH_OFFSET),
      next.height / 100.0f,
      angle, h_cft / 100.0f);

  delay(10000);  // re-sample every 10 s
}
