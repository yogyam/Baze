/*
 * CycleSentinel — Arduino Uno R4 WiFi
 * Reads MMA7660FC (Grove 3-Axis ±1.5g) via I2C.
 * Computes Z-axis AC RMS over a rolling window to produce a
 * Road Roughness Index (RRI), classifies RED/ORANGE/GREEN,
 * streams JSON over Serial AND HTTP POST to FastAPI backend.
 *
 * Wiring (Grove → Arduino Uno R4):
 *   Yellow (SDA) → A4
 *   White  (SCL) → A5
 *   Red    (VCC) → 3.3V   ← MUST be 3.3V, not 5V
 *   Black  (GND) → GND
 */

#include <Wire.h>
#include <WiFiS3.h>

// ── User config ────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "HackDavis";
const char* WIFI_PASSWORD = "CowBoomBoomGo";
const char* SERVER_HOST   = "172.23.24.134";     // MacBook IP on HackDavis
const int   SERVER_PORT   = 8000;
// ───────────────────────────────────────────────────────────────────────────

// MMA7660FC
#define MMA7660_ADDR  0x4C
#define REG_XOUT      0x00
#define REG_YOUT      0x01
#define REG_ZOUT      0x02
#define REG_MODE      0x07
#define REG_SR        0x08
#define ALERT_BIT     0x40   // bit 6: data being written by sensor — discard
#define SIGN_BIT      0x20   // bit 5: MSB of 6-bit signed value
#define COUNTS_PER_G  21.33f // 32 counts / 1.5g

// RMS window — 50 samples @ ~100 Hz = 500 ms window
#define WINDOW_SIZE 50
#define SAMPLE_DELAY_MS 10

// No thresholds on Arduino — bridge.py owns all classification

// ── MMA7660FC init ─────────────────────────────────────────────────────────
bool initMMA7660() {
  // Put in standby first
  Wire.beginTransmission(MMA7660_ADDR);
  Wire.write(REG_MODE);
  Wire.write(0x00);
  if (Wire.endTransmission() != 0) return false;

  // Sample rate: 0x00 = 120 samples/sec (fastest)
  Wire.beginTransmission(MMA7660_ADDR);
  Wire.write(REG_SR);
  Wire.write(0x00);
  Wire.endTransmission();

  // Enter active mode
  Wire.beginTransmission(MMA7660_ADDR);
  Wire.write(REG_MODE);
  Wire.write(0x01);
  Wire.endTransmission();

  return true;
}

// ── Read one axis — retries until alert bit is clear ──────────────────────
float readAxisG(uint8_t reg) {
  uint8_t raw;
  uint8_t retries = 0;
  do {
    Wire.beginTransmission(MMA7660_ADDR);
    Wire.write(reg);
    Wire.endTransmission(false);
    Wire.requestFrom((uint8_t)MMA7660_ADDR, (uint8_t)1);
    raw = Wire.available() ? Wire.read() : 0;
    retries++;
  } while ((raw & ALERT_BIT) && retries < 5);

  // Sign-extend 6-bit two's complement → int8_t
  if (raw & SIGN_BIT) raw |= 0xC0;
  return (int8_t)raw / COUNTS_PER_G;
}

// ── Total vibration magnitude per sample (gravity-removed) ────────────────
// Uses all 3 axes so orientation doesn't matter when mounted on bike.
// Subtracts 1g (gravity magnitude) to get pure dynamic vibration.
float vibMagnitude(float x, float y, float z) {
  float mag = sqrt(x*x + y*y + z*z);
  return fabs(mag - 1.0f);  // remove 1g gravity component
}

// ── AC RMS over vibration magnitude window ────────────────────────────────
float computeRMS(float* buf, int n) {
  float sum = 0;
  for (int i = 0; i < n; i++) sum += buf[i] * buf[i];
  return sqrt(sum / n);
}

// ── Peak value in window ──────────────────────────────────────────────────
float computePeak(float* buf, int n) {
  float peak = 0;
  for (int i = 0; i < n; i++) if (buf[i] > peak) peak = buf[i];
  return peak;
}

// ── HTTP POST to FastAPI ───────────────────────────────────────────────────
void postToServer(float x, float y, float z, float rms, const char* severity) {
  if (WiFi.status() != WL_CONNECTED) return;

  WiFiClient client;
  if (!client.connect(SERVER_HOST, SERVER_PORT)) return;

  // Build JSON body
  char body[128];
  snprintf(body, sizeof(body),
    "{\"x\":%.4f,\"y\":%.4f,\"z\":%.4f,\"rms\":%.4f,\"severity\":\"%s\"}",
    x, y, z, rms, severity);

  int len = strlen(body);

  client.print("POST /api/imu HTTP/1.1\r\n");
  client.print("Host: "); client.print(SERVER_HOST); client.print("\r\n");
  client.print("Content-Type: application/json\r\n");
  client.print("Content-Length: "); client.print(len); client.print("\r\n");
  client.print("Connection: close\r\n\r\n");
  client.print(body);

  // Drain response (non-blocking, 1s timeout)
  unsigned long t = millis();
  while (millis() - t < 1000) {
    if (client.available()) { while (client.available()) client.read(); break; }
  }
  client.stop();
}

// ── Setup ──────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000);

  Wire.begin();
  Serial.println("[CycleSentinel] Initializing MMA7660FC...");
  if (!initMMA7660()) {
    Serial.println("[ERROR] MMA7660FC not found — check wiring and 3.3V supply.");
    while (true) delay(1000);
  }
  Serial.println("[CycleSentinel] Accelerometer OK.");

  Serial.println("[CycleSentinel] Serial-only mode — bridge.py handles posting.");
}

// ── Main loop ──────────────────────────────────────────────────────────────
void loop() {
  float vibBuf[WINDOW_SIZE];
  float xSum = 0, ySum = 0, zSum = 0;

  for (int i = 0; i < WINDOW_SIZE; i++) {
    float x = readAxisG(REG_XOUT);
    float y = readAxisG(REG_YOUT);
    float z = readAxisG(REG_ZOUT);
    vibBuf[i] = vibMagnitude(x, y, z);
    xSum += x; ySum += y; zSum += z;
    delay(SAMPLE_DELAY_MS);
  }

  float avgX = xSum / WINDOW_SIZE;
  float avgY = ySum / WINDOW_SIZE;
  float avgZ = zSum / WINDOW_SIZE;
  float rms  = computeRMS(vibBuf, WINDOW_SIZE);
  float peak = computePeak(vibBuf, WINDOW_SIZE);

  // Send rms + peak — bridge.py handles all classification
  char line[128];
  snprintf(line, sizeof(line),
    "{\"x\":%.4f,\"y\":%.4f,\"z\":%.4f,\"rms\":%.4f,\"peak\":%.4f}",
    avgX, avgY, avgZ, rms, peak);
  Serial.println(line);
}
