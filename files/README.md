# 把固件放在这个目录

仓库**不附带**任何固件文件（体积大，且版权归各自作者）。请准备下面 5 个文件，
放到本目录后运行脚本：

| 文件 | 用途 | 备注 |
|------|------|------|
| `miwifi_ra69_firmware_45a77_1.0.18.bin` | 原厂 1.0.18 固件（第 1 步降级） | 小米官方固件，约 28 MB |
| `xiaomimtd12.bin` | 过渡 OpenWrt 固件（第 2 步） | 约 36 MB |
| `ax6-uboot-mibib.bin` | MIBIB 分区（mtd1），1 MB | 写 bootloader 用 |
| `uboot-ax6.bin` | U-Boot（mtd7） | 带 web failsafe 的 U-Boot |
| `immortalwrt-25.12.2-qualcommax-ipq807x-redmi_ax6-stock-squashfs-factory.ubi` | 要刷入的系统 | 约 16.5 MB，**必须是 redmi_ax6-stock 的 factory 固件** |

文件路径可以在 `config.ini` 的 `[paths]` 里改成别的名字或位置。

📥 **下载渠道、每个文件的 SHA-256 / MD5 校验值，以及“为什么仓库不直接附带固件”
（版权授权 ≠ 注明出处）**，见 [SOURCES.md](SOURCES.md)。

> 校验：`ax6_auto.exe --check` 会在不碰设备的情况下检查这 5 个文件是否存在、
> 大小和格式是否正确（原厂固件的 HDR1 型号、过渡固件的 `UBI#` magic 等）。
