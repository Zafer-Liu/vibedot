# 固件烧录

`vibedot_firmware.bin` 是合并镜像（bootloader + 分区表 + 应用），
从地址 `0x0` 整片写入即可，无需分别烧录各段。

- 芯片：ESP32-C3（原生 USB，VID `0x303A`）
- Flash：4MB，DIO 模式，80MHz
- 大小：4MB（整片镜像，含空白填充）

## 方式一：esptool（推荐）

```bash
pip install esptool
esptool.py --chip esp32c3 write_flash 0x0 vibedot_firmware.bin
```

## 方式二：Flash Download Tools（图形界面）

乐鑫官方 Flash Download Tools，选择 ESP32-C3：

1. 添加文件：`vibedot_firmware.bin` @ `0x0`
2. SPI SPEED: 80MHz，SPI MODE: DIO
3. 选择 COM 口，点 START

## 常见问题

**Q: 找不到串口 / 烧录失败？**
设备深度睡眠时 USB 串口会从系统消失。拔插一次 USB 或等设备醒来
（低功耗循环每 20s 广播一次）再烧即可。

**Q: 烧完屏幕没画面？**
首次上电会全刷一次（约 2~3s），耐心等；仍无画面再拔插 USB 复位。

**Q: 需要按住 BOOT 键吗？**
不需要。ESP32-C3 走原生 USB-Serial-JTAG，esptool 自动进下载模式。
