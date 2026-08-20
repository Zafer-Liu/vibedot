/*
 * VibeDot — 思维重置 Quote/0 水墨屏改造固件 v9
 * 硬件: ESP32-C3 + UC8251D 2.9" 黑白电子纸 (296x152)
 *
 * v9: 电池电量检测 (ADC1 CH0-2 多通道探测, 状态特征第 4 字节带出电量);
 *     广播自愈 (看门狗式每 10s 重启广播 + 断连即重播, 排查"蓝牙自动关");
 *     启动打印复位原因 (brownout/panic 等意外掉电可见)
 *
 * 所有引脚 / 初始化序列 / LUT 波形表均逆向自原厂固件
 * (flash dump ota_0@0x10000, 驱动代码 0x42015f00-0x42016600)
 *
 * BLE 协议 (写 RX 特征 8e400002-...):
 *   0x00                -> 复位帧接收计数
 *   0x02 <payload...>   -> 位图数据块 (1bpp, 5624 字节, 19B/行 x 296 行)
 *   0x03                -> 全刷显示 (原厂 LUT, 慢但干净)
 *   0x04                -> 快速刷新 (partial LUT, ~0.5s, 旧帧=上次画面)
 *   0x05                -> 面板深度睡眠
 *   0x06                -> 立即进入深度睡眠循环 (远程关闭蓝牙/低功耗)
 *   0x07                -> 常开模式: 退出睡眠循环, 保持持续广播 (电脑随时可搜)
 *
 * 低功耗: 常开模式下 BLE 断连 10 分钟后才进入深度睡眠循环
 *   (睡 55s -> 醒来广播 20s -> 无人连接继续睡, 平均电流 ~1/3)
 *   PC 推送时扫描重试即可等到广播窗口; 0x06 可远程触发立即入睡
 *   0x07 可远程恢复常开 (wokeFromTimer 清零, 断连 10 分钟才再睡)
 */

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLESecurity.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <SPI.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include <driver/adc.h>
#include "lut.h"

// ============ 引脚 (逆向确认: SPI bus/dev config + gpio 0x100070 三路验证) ============
#define EPD_SCK   10   // IO10
#define EPD_MOSI  7    // IO7
#define EPD_CS    6    // IO6 (原固件为硬件 CS)
#define EPD_DC    5    // IO5
#define EPD_RST   4    // IO4
#define EPD_BUSY  3    // IO3, 高电平 = 空闲
#define EPD_PWR   20   // IO20 面板电源使能, 高 = 开; 关断时置输入态

// ============ 面板参数 (TRES 命令 0x61: 0x98 0x01 0x28; 代码内联常量 296x152) ============
#define EPD_W 296
#define EPD_H 152
#define FRAME_SZ ((EPD_W * EPD_H) / 8)   // 5624 字节 (原固件循环计数 0x15F8)

static uint8_t framebuf[FRAME_SZ];
static uint8_t prev_frame[FRAME_SZ];     // 上次显示的画面 (partial 旧帧)
static bool prevValid = false;           // prev_frame 与面板物理画面一致 (v4)
                                         // 任何启动/复位后为 false: RAM 里 prev
                                         // 丢失而面板保留旧画面, 快刷以全黑当旧帧
                                         // -> 黑色内容像素被判 B2B 不驱动 -> 白屏。
                                         // 首次刷新强制全刷重建基准
volatile uint32_t frame_received = 0;
static uint16_t fast_count = 0;          // 连续快刷次数, 超限自动全刷清残影

// ============ 低功耗参数 ============
// v5: 默认永久常开——断连后不再自动深睡 (BLE 持续广播, 电脑随时可连)。
// 只保留 0x06 远程关闭 (网页"立即深睡"按钮) 进入 55s/20s 睡眠循环
#define WAKE_ADVERTISE_MS          (20UL * 1000UL)        // (仅 0x06 睡后) 定时唤醒广播窗口
#define DEEP_SLEEP_US              (55ULL * 1000000ULL)   // 每次睡 55s

// ============ BLE ============
#define SERVICE_UUID      "8e400001-1f31-4a3a-9a2f-3d1c0a5b7e01"
#define CHAR_RX_UUID      "8e400002-1f31-4a3a-9a2f-3d1c0a5b7e01"
#define CHAR_STATUS_UUID  "8e400003-1f31-4a3a-9a2f-3d1c0a5b7e01"

BLEServer*         pServer = nullptr;
BLECharacteristic* pRxChar = nullptr;
BLECharacteristic* pStatusChar = nullptr;
// 多连接: Windows 系统面板可能保持连接, 单连接会被其抢占导致 PC 推送断连;
// 改用 getConnectedCount() 支持多连接共存 (系统连接反可帮设备保活)
volatile bool needRefresh = false;
volatile bool needFastRefresh = false;
volatile bool needSleep = false;
volatile bool needDeepSleep = false;
volatile uint32_t lastActivityMs = 0;
volatile uint32_t serLastRxMs = 0;         // 最近一次串口数据到达 (主机心跳/推送): USB 链路存活的可靠判据
bool wokeFromTimer = false;              // 定时唤醒 (非上电): 跳过自检, 窗口内无人连则继续睡
volatile bool advActive = true;          // 广播是否进行中 (仅深睡前手动清零)

// ============ 电池采样 (v9) ============
// 原厂固件有电池检测 (逆向: [BATTERY]/VBUS/adc_oneshot 字符串) 但采样代码
// 不在此固件变体, 电池 ADC 引脚未知。已用引脚 3/4/5/6/7/10/20, 空闲 ADC1
// 通道仅 CH0-2 (GPIO0/1/2): 三路全采, 取落在合理锂电范围的通道。
// 支持分压 (读数 1.4-2.3V 视为二分频), 串口打印全部读数供标定
static int     battChannel = -1;         // 命中的 ADC1 通道, -1 = 未识别
static int     battDivider = 1;          // 分压倍数
static uint8_t batt_pct    = 100;        // 对外电量 (状态特征第 4 字节)

// 锂电电压 -> 百分比 (空载近似, 4.2V 满 / 3.3V 空)
static uint8_t batt_mv_to_pct(uint32_t mv) {
  if (mv >= 4150) return 100;
  if (mv >= 4000) return 90;
  if (mv >= 3900) return 75;
  if (mv >= 3800) return 60;
  if (mv >= 3700) return 40;
  if (mv >= 3600) return 20;
  if (mv >= 3500) return 10;
  if (mv >= 3300) return 5;
  return 0;
}

static uint32_t batt_read_mv(uint8_t ch) {
  uint32_t raw = 0;
  for (int i = 0; i < 16; i++) raw += adc1_get_raw((adc1_channel_t)ch);
  raw >>= 4;
  return (uint32_t)(raw * 3100 / 4095);   // 12bit; atten 12dB 官方量程 150-2450mV,
                                          // 实测非线性可读到 ~3.1V, 超出部分限幅
}

// 探测三路空闲 ADC, 识别电池通道 (直连 2.8-4.3V 或二分频 1.4-2.3V)
static void batt_probe() {
  adc1_config_width(ADC_WIDTH_BIT_12);
  adc1_config_channel_atten(ADC1_CHANNEL_0, ADC_ATTEN_DB_12);
  adc1_config_channel_atten(ADC1_CHANNEL_1, ADC_ATTEN_DB_12);
  adc1_config_channel_atten(ADC1_CHANNEL_2, ADC_ATTEN_DB_12);
  uint32_t mv[3];
  for (int ch = 0; ch < 3; ch++) {
    mv[ch] = batt_read_mv(ch);
    Serial.printf("[BATT] ADC1_CH%d (GPIO%d): %lu mV\n", ch, ch, (unsigned long)mv[ch]);
  }
  battChannel = -1; battDivider = 1;
  // 优先分压范围 (真实信号稳定); 接近饱和 (~3100mV) 的读数多为悬空引脚,
  // 仅当无分压通道时才当作直连电池候选
  int directCh = -1;
  for (int ch = 0; ch < 3; ch++) {
    if (mv[ch] >= 1400 && mv[ch] <= 2300) {
      battChannel = ch; battDivider = 2; break;
    }
    if (mv[ch] >= 2800 && mv[ch] <= 3000 && directCh < 0) directCh = ch;
  }
  if (battChannel < 0 && directCh >= 0) { battChannel = directCh; battDivider = 1; }
  if (battChannel >= 0) {
    uint32_t v = mv[battChannel] * battDivider;
    batt_pct = batt_mv_to_pct(v);
    Serial.printf("[BATT] CH%d %s: %lu mV -> %u%%\n", battChannel,
                  battDivider == 2 ? "(x2 分压)" : "(直连)",
                  (unsigned long)v, batt_pct);
  } else {
    batt_pct = 100;   // 未接电池/未识别: 视为 USB 供电满电
    Serial.println("[BATT] 未识别电池通道 (USB 供电?), 电量按 100% 上报");
    Serial.println("[BATT] 提示: 若蓝牙曾莫名消失, 留意启动横幅 reset 是否为 BROWNOUT");
  }
}

static void batt_update() {
  if (battChannel < 0) return;
  uint32_t v = batt_read_mv(battChannel) * battDivider;
  batt_pct = batt_mv_to_pct(v);
  Serial.printf("[BATT] %lu mV -> %u%%\n", (unsigned long)v, batt_pct);
}

// 复位原因 (排查"蓝牙莫名关闭": brownout=欠压掉电, PANIC=固件崩溃...)
static const char* reset_reason_str() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_EXT:       return "external";
    case ESP_RST_SW:        return "software";
    case ESP_RST_PANIC:     return "PANIC(异常/断言)";
    case ESP_RST_INT_WDT:   return "中断看门狗";
    case ESP_RST_TASK_WDT:  return "任务看门狗";
    case ESP_RST_WDT:       return "其他看门狗";
    case ESP_RST_DEEPSLEEP: return "深睡唤醒";
    case ESP_RST_BROWNOUT:  return "BROWNOUT(欠压掉电)";
    case ESP_RST_SDIO:      return "SDIO";
    default:                return "unknown";
  }
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override {
    lastActivityMs = millis();
    Serial.printf("[BLE] connected (%d)\n", s->getConnectedCount());
    // NOTE: 连接参数更新 (esp_ble_gap_update_conn_params) 不可用:
    // 3.3.11 Arduino 预编译库不暴露 esp_gap_ble_api.h; 有响应写每块耗时由
    // Windows 中心设备参数决定, 无法从外设侧优化
  }
  void onDisconnect(BLEServer* s) override {
    lastActivityMs = millis();
    Serial.printf("[BLE] disconnected (%d)\n", s->getConnectedCount());
    if (s->getConnectedCount() == 0) {
      advActive = true;
      s->getAdvertising()->start();   // 断连即重新广播 (v9: loop 中另有自愈兜底)
    }
  }
};

// ============ 底层 (对应原固件 0x42015274/0x42015294) ============
// ESP32-C3 只有 SPI2 一条可用总线: FSPI=0 映射 SPI2_HOST; HSPI=1 在 C3 上指向不存在的 SPI3
SPIClass epdSpi(FSPI);

static inline void epd_cmd(uint8_t b) {        // DC=0
  digitalWrite(EPD_DC, LOW);
  digitalWrite(EPD_CS, LOW);
  epdSpi.transfer(b);
  digitalWrite(EPD_CS, HIGH);
}
static inline void epd_data(uint8_t b) {       // DC=1
  digitalWrite(EPD_DC, HIGH);
  digitalWrite(EPD_CS, LOW);
  epdSpi.transfer(b);
  digitalWrite(EPD_CS, HIGH);
}

// 面板电源 (原固件 0x420151e8 上电 / 0x42015218 断电置输入)
static void epd_power_on() {
  pinMode(EPD_PWR, OUTPUT);
  digitalWrite(EPD_PWR, HIGH);
  delay(10);
}
static void epd_power_off() {
  digitalWrite(EPD_PWR, LOW);
  pinMode(EPD_PWR, INPUT);
}

// 等待 BUSY (原固件 0x42015f52: 循环 CMD 0x71 + 读 GPIO3, 高=完成, 500ms 超时)
static void epd_wait_busy(uint32_t timeout_ms = 500) {
  uint32_t t0 = millis();
  while (true) {
    epd_cmd(0x71);
    delayMicroseconds(100);
    if (digitalRead(EPD_BUSY) == HIGH) return;   // 高 = 空闲
    if (millis() - t0 > timeout_ms) return;
    delay(5);
  }
}

// ============ UC8251D 初始化 (逆向自 0x42016030, 逐字复刻) ============
static void epd_init() {
  uint32_t t0 = millis();
  // RST 复位脉冲 (0x42015f1e): 1,10ms / 0,10ms / 1,10ms
  digitalWrite(EPD_RST, HIGH); delay(10);
  digitalWrite(EPD_RST, LOW);  delay(10);
  digitalWrite(EPD_RST, HIGH); delay(10);
  epd_wait_busy(500);

  epd_cmd(0x00); epd_data(0xF3); epd_data(0x0E);                 // Panel Setting
  epd_cmd(0x01); epd_data(0x03); epd_data(0x00);                 // Power Setting
                epd_data(0x3F); epd_data(0x3F); epd_data(0x03);
  epd_cmd(0x06); epd_data(0x17); epd_data(0x17); epd_data(0x17); // Booster
  epd_cmd(0x61); epd_data(0x98); epd_data(0x01); epd_data(0x28); // TRES 152 x 296
  epd_cmd(0x30); epd_data(0x1B);                                 // PLL
  epd_cmd(0x60); epd_data(0x22);                                 // Gate/Source
  epd_cmd(0x82); epd_data(0x00);                                 // VCOM_DC
  epd_cmd(0x03); epd_data(0x10);                                 // PWS
  epd_cmd(0x50); epd_data(0x01);                                 // CDI (NVS epd_config=1)
  epd_cmd(0x04);                                                 // Power On
  delay(100);
  epd_wait_busy(3000);
  Serial.printf("[EPD] init done (%lu ms), busy=%d\n", millis() - t0, digitalRead(EPD_BUSY));
}

// ============ 全刷 LUT (逆向自 0x420162ea, 表 @ DROM 0x3c0f37a4) ============
static void epd_write_lut() {
  epd_cmd(0x20); for (int i = 0;   i < 80;  i++) epd_data(EPD_LUT[i]);  // VCOM
  epd_cmd(0x21); for (int i = 80;  i < 136; i++) epd_data(EPD_LUT[i]);  // W2W (56B)
  epd_cmd(0x22); for (int i = 136; i < 216; i++) epd_data(EPD_LUT[i]);  // B2B
  epd_cmd(0x23); for (int i = 216; i < 296; i++) epd_data(EPD_LUT[i]);  // B2W
  epd_cmd(0x24); for (int i = 296; i < 376; i++) epd_data(EPD_LUT[i]);  // W2B
}

// ============ 快速刷新 LUT (GoodDisplay UC8x51 标准单相位 partial 波形) ============
// 帧位: 1=白 0=黑; 仅驱动的过渡: B2W(0x23)=0x80, W2B(0x24)=0x40
// 若快刷后变化像素方向相反, 交换 0x80/0x40 即可
// 格式: 每相位 [电平, 帧数(0x19=25), ...]; 0x20 VCOM 44B, 0x21-0x24 各 42B
static const uint8_t LUTP_VCOM[44] = {
  0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0,
};
static const uint8_t LUTP_W2W[42] = {   // 白->白: 不驱动
  0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
};
static const uint8_t LUTP_B2B[42] = {   // 黑->黑: 不驱动
  0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
};
static const uint8_t LUTP_B2W[42] = {   // 黑->白: 驱动
  0x80, 0x19, 0x01, 0x00, 0x00, 0x01,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
};
static const uint8_t LUTP_W2B[42] = {   // 白->黑: 驱动
  0x40, 0x19, 0x01, 0x00, 0x00, 0x01,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
};

static void epd_write_lut_partial() {
  epd_cmd(0x20); for (int i = 0; i < 44; i++) epd_data(LUTP_VCOM[i]);
  epd_cmd(0x21); for (int i = 0; i < 42; i++) epd_data(LUTP_W2W[i]);
  epd_cmd(0x22); for (int i = 0; i < 42; i++) epd_data(LUTP_B2B[i]);
  epd_cmd(0x23); for (int i = 0; i < 42; i++) epd_data(LUTP_B2W[i]);
  epd_cmd(0x24); for (int i = 0; i < 42; i++) epd_data(LUTP_W2B[i]);
}

// ============ 全刷一帧 (逆向自 0x4201653e) ============
static void epd_display() {
  uint32_t t0 = millis();
  epd_power_on();
  epd_init();
  epd_write_lut();
  epd_cmd(0x10);                              // 旧帧 = 全白
  for (uint32_t i = 0; i < FRAME_SZ; i++) epd_data(0xFF);
  epd_cmd(0x13);                              // 新帧
  for (uint32_t i = 0; i < FRAME_SZ; i++) epd_data(framebuf[i]);
  epd_cmd(0x12);                              // Display Refresh (0x420163c8)
  delay(100);
  epd_wait_busy(20000);
  Serial.printf("[EPD] FULL refresh %lu ms\n", millis() - t0);
  epd_power_off();
  memcpy(prev_frame, framebuf, FRAME_SZ);
  fast_count = 0;
}

// ============ 快速刷新 (partial LUT, 旧帧=上次画面, 无黑白色闪烁) ============
static void epd_display_fast() {
  if (fast_count >= 16) {                     // 残影保护: 连续快刷 16 次后全刷
    Serial.printf("[EPD] fast_count=%d -> force full\n", fast_count);
    epd_display();
    return;
  }
  uint32_t t0 = millis();
  epd_power_on();
  epd_init();
  epd_write_lut_partial();
  epd_cmd(0x10);                              // 旧帧 = 上次画面
  for (uint32_t i = 0; i < FRAME_SZ; i++) epd_data(prev_frame[i]);
  epd_cmd(0x13);                              // 新帧
  for (uint32_t i = 0; i < FRAME_SZ; i++) epd_data(framebuf[i]);
  epd_cmd(0x12);
  delay(50);
  epd_wait_busy(8000);
  Serial.printf("[EPD] FAST refresh %lu ms (#%d)\n", millis() - t0, fast_count);
  epd_power_off();
  memcpy(prev_frame, framebuf, FRAME_SZ);
  fast_count++;
}

// 面板深度睡眠 (逆向自 0x420162c8)
static bool panelSlept = false;   // 面板深睡过: 唤醒后首次刷新必须全刷(VCOM 已丢失)
static void epd_panel_sleep() {
  epd_cmd(0x07); epd_data(0xA5);
  epd_power_off();
  panelSlept = true;
}

// 深度睡眠循环 (定时唤醒广播, 等待 PC 连接)
static void enter_deep_sleep_cycle() {
  Serial.println("[PWR] 10 分钟无连接, 进入深度睡眠循环 (55s 睡 / 20s 广播)");
  Serial.flush();
  epd_power_off();
  panelSlept = true;            // 深睡掉电, 唤醒后首次刷新必须全刷
  BLEDevice::stopAdvertising();
  advActive = false;
  esp_sleep_enable_timer_wakeup(DEEP_SLEEP_US);
  esp_deep_sleep_start();
}

// ============ BLE 接收回调 ============
class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* ch) override {
    String v = ch->getValue();
    if (!v.length()) return;
    const uint8_t* d = (const uint8_t*)v.c_str();
    lastActivityMs = millis();
    switch (d[0]) {
      case 0x00:  // 复位帧接收计数
        frame_received = 0;
        break;
      case 0x02: {  // 位图数据块
        uint32_t len = v.length() - 1;
        if (frame_received + len <= FRAME_SZ) {
          memcpy(framebuf + frame_received, d + 1, len);
          frame_received += len;
        } else {
          Serial.printf("[BLE] overflow: +%lu > %d\n", (unsigned long)(frame_received + len), FRAME_SZ);
        }
        break;
      }
      case 0x03:
        Serial.printf("[BLE] full refresh cmd, rx=%lu/%d\n", (unsigned long)frame_received, FRAME_SZ);
        needRefresh = true; break;
      case 0x04:
        Serial.printf("[BLE] fast refresh cmd, rx=%lu/%d\n", (unsigned long)frame_received, FRAME_SZ);
        needFastRefresh = true; break;
      case 0x05: needSleep = true; break;
      case 0x06: needDeepSleep = true; break;   // 远程关闭蓝牙: 立即深度睡眠循环
      case 0x07:                                 // 常开: 退出睡眠循环, 保持广播
        wokeFromTimer = false;
        lastActivityMs = millis();
        Serial.println("[BLE] always-on mode");
        break;
      default: break;
    }
  }
};

// ============ 串口帧接收 (USB 直连推送, 快于 BLE) ============
// 串口是字节流无包边界, 协议与 BLE 一致但数据块带长度前缀:
//   0x00 复位 | 0x02 <len16 LE> <data> | 0x03 全刷 | 0x04 快刷
//   0x05 面板睡眠 | 0x06 深睡 | 0x07 常开
static uint8_t  serState = 0;   // 0 命令 / 1 块长低 / 2 块长高 / 3 块数据
static uint16_t serLen = 0, serGot = 0;

static void serial_handle() {
  while (Serial.available()) {
    uint8_t c = Serial.read();
    lastActivityMs = millis();   // USB 直连时设备保持常开
    serLastRxMs = millis();       // 串口数据 = USB 链路确定存活 (isPlugged/SOF 检测不可靠)
    switch (serState) {
      case 0:  // 命令
        switch (c) {
          case 0x00: frame_received = 0; break;
          case 0x02: serState = 1; break;
          case 0x03: needRefresh = true; break;
          case 0x04: needFastRefresh = true; break;
          case 0x05: needSleep = true; break;
          case 0x06: needDeepSleep = true; break;
          case 0x07:
            wokeFromTimer = false;
            lastActivityMs = millis();
            Serial.println("[SER] always-on mode");
            break;
          default: break;
        }
        break;
      case 1: serLen = c; serState = 2; break;
      case 2:
        serLen |= (uint16_t)(c << 8);
        serGot = 0;
        serState = serLen ? 3 : 0;
        break;
      case 3:
        if (frame_received + serGot < FRAME_SZ)
          framebuf[frame_received + serGot] = c;
        if (++serGot >= serLen) {
          frame_received += serLen;
          serState = 0;
        }
        break;
    }
  }
}

// 深睡唤醒后 USB-Serial-JTAG 外设半死 (可枚举但数据端点 NAK/不通),
// 干净复位一次让外设走完整上电初始化; RTC 标志防无限复位循环
RTC_DATA_ATTR static bool usbReinitDone = false;

void setup() {
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER && !usbReinitDone) {
    usbReinitDone = true;
    esp_restart();
  }
  usbReinitDone = false;

  Serial.begin(115200);
  wokeFromTimer = (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER);
  if (wokeFromTimer) panelSlept = true;   // 定时唤醒: 面板已掉电, 首次刷新强制全刷
  Serial.printf("\n[VIBEDOT] v9 boot (%s, reset: %s)\n",
                wokeFromTimer ? "timer wake" : "power on", reset_reason_str());
  Serial.flush();

  batt_probe();   // 电池采样 (ADC, 不依赖外设)

  pinMode(EPD_CS, OUTPUT);  digitalWrite(EPD_CS, HIGH);
  pinMode(EPD_DC, OUTPUT);
  pinMode(EPD_RST, OUTPUT); digitalWrite(EPD_RST, HIGH);
  pinMode(EPD_BUSY, INPUT);

  epdSpi.begin(EPD_SCK, -1, EPD_MOSI, EPD_CS);
  epdSpi.beginTransaction(SPISettings(15000000, MSBFIRST, SPI_MODE0));  // 原固件 15MHz

  if (!wokeFromTimer) {
    // v4: 不再刷棋盘格自检 (反复复位时闪棋盘格难看, 且刷完 prev=棋盘格
    // 与后续内容帧差分剧烈)。仅串口报告就绪; 首次推送由 prevValid 机制
    // 强制全刷上屏, 画面由 PC 端接管
    Serial.println("[EPD] boot ok, awaiting first frame (force full on first refresh)");
  }

  BLEDevice::init("VibeDot");
  BLEDevice::setMTU(517);   // 默认 ATT MTU 23B -> 296 笔分片写 ~4.3s;
                           // 517 后 12 块, 推送 ~2.5s (配合 PC exchange_mtu)

  // v7: 不注册配对能力 (BLESecurity) —— bleak 直连 GATT 不需要配对,
  // 而 Just Works 配对能力会让 Windows 系统蓝牙面板反复自动重连设备、
  // 抢占连接槽 (0x800704C7 写取消) 并用系统 GATT 枚举污染 WinRT 缓存
  // (invalid literal '0000None' 反复出现的根源)。系统连不上 = 永绝后患

  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService* svc = pServer->createService(SERVICE_UUID);
  pRxChar = svc->createCharacteristic(
      CHAR_RX_UUID, BLECharacteristic::PROPERTY_WRITE_NR | BLECharacteristic::PROPERTY_WRITE);
  pRxChar->setCallbacks(new RxCallbacks());

  pStatusChar = svc->createCharacteristic(
      CHAR_STATUS_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  pStatusChar->addDescriptor(new BLE2902());

  svc->start();

  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  advActive = true;
  lastActivityMs = millis();
  Serial.println("[VIBEDOT] BLE advertising as 'VibeDot'");
}

void loop() {
  serial_handle();
  if (needRefresh || needFastRefresh) {
    bool fast = needFastRefresh;
    needRefresh = needFastRefresh = false;
    // v8: 首刷一律全刷。v6 的白色基准同步有缺陷——断电/掉电重启后
    // panelSlept=false 但面板 VCOM 实际已丢 (复位期间 PWR 引脚悬停面板掉电),
    // 无 VCOM 的快刷驱动不动任何像素, 基准同步静默失败 (241ms 白刷不动),
    // prevValid 却被置真 -> 此后所有快刷全部无效、屏幕冻结, 只有全刷能救。
    // 启动时无法区分 VCOM 是否存活, 统一强制首刷全刷重建 (VCOM+prev 一次到位)
    if (frame_received >= FRAME_SZ) {
      if (panelSlept || !prevValid) {
        fast = false;
        Serial.println("[EPD] prev unknown/asleep -> force full refresh");
      }
      if (fast) epd_display_fast();
      else      epd_display();
      prevValid = true;
      panelSlept = false;
      frame_received = 0;
      uint8_t st[4] = {1, 0, 0, batt_pct};   // state=1 完成; 第 4 字节 = 电量%
      pStatusChar->setValue(st, 4);
      pStatusChar->notify();
    } else {
      // 帧不完整: 通知 PC 当前接收量, 由 PC 重发整帧
      Serial.printf("[VIBEDOT] incomplete frame: %lu/%d\n",
                    (unsigned long)frame_received, FRAME_SZ);
      uint8_t st[4] = {0, (uint8_t)(frame_received >> 8), (uint8_t)(frame_received & 0xFF), batt_pct};
      pStatusChar->setValue(st, 4);
      pStatusChar->notify();
    }
  }
  if (needSleep) {
    needSleep = false;
    Serial.println("[VIBEDOT] panel sleep");
    epd_panel_sleep();
  }
  if (needDeepSleep) {
    needDeepSleep = false;
    Serial.println("[VIBEDOT] deep sleep (remote off)");
    epd_panel_sleep();
    enter_deep_sleep_cycle();
  }

  // ---- 广播自愈 (v9): 兜底任何意外停播 (协议栈异常/被抢占/默认广播超时) ----
  // NimBLE 库 stopAdvertising 状态无法查询, 用看门狗式重启: 未连接时每 10s
  // 无条件重启一次广播 (重复 start() 无害); 深睡循环前 advActive 已清零不受影响
  static uint32_t lastAdvHealMs = 0;
  if (advActive && pServer->getConnectedCount() == 0 &&
      millis() - lastAdvHealMs > 10000) {
    lastAdvHealMs = millis();
    BLEDevice::startAdvertising();
  }

  // ---- 电量周期刷新 (30s) ----
  static uint32_t lastBattMs = 0;
  if (millis() - lastBattMs > 30000) {
    lastBattMs = millis();
    batt_update();
  }

  // ---- 低功耗 (v5: 常开) ----
  if (pServer->getConnectedCount() == 0) {
    uint32_t idle = millis() - lastActivityMs;
    uint32_t serIdle = millis() - serLastRxMs;
    // 判据: SOF 检测 (isPlugged) 在深睡唤醒后不可靠; 串口收到过主机数据
    // (服务器 keeper 每 2-5s 心跳) 才确信 USB 链路活着
    if (Serial.isPlugged() || serIdle < 5000) {
      // USB 主机已连接: 保持常开 (PC 串口直推需随时可用)
      if (wokeFromTimer && serIdle < 5000) {
        wokeFromTimer = false;
        Serial.println("[PWR] USB host connected, stay awake");
      }
    } else if (wokeFromTimer && idle > WAKE_ADVERTISE_MS) {
      // 仅 0x06 触发的睡眠循环: 唤醒窗口内无人连, 继续睡
      enter_deep_sleep_cycle();
    }
    // v5: 普通断连 (掉线/复位/无连接) 不再自动入睡 —— 持续广播等 PC 重连,
    // 服务器守护 25s 内会自动接回。要关闭设备只能发 0x06 (网页"立即深睡")
  }
  delay(20);
}
