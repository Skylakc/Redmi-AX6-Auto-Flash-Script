# 把固件放在这个目录

**最快**：下载 [Release](https://github.com/Skylakc/Redmi-AX6-Auto-Flash-Script/releases/latest)
里的 `ax6-firmware-bundle.zip`，解压到本目录即可（内含下面 5 个文件 + `SHA256SUMS.txt`）。

也可以自行准备下面 5 个文件（大小、校验值与来源见 [SOURCES.md](SOURCES.md)），
放到本目录后运行脚本：

| 文件 | 用途 | 备注 |
|------|------|------|
| `miwifi_ra69_firmware_45a77_1.0.18.bin` | 原厂 1.0.18 固件（第 1 步降级） | 小米官方固件，约 28 MB |
| `xiaomimtd12.bin` | 过渡 OpenWrt 固件（第 2 步） | 约 36 MB |
| `ax6-uboot-mibib.bin` | MIBIB 分区（mtd1），1 MB | 写 bootloader 用 |
| `uboot-ax6.bin` | U-Boot（mtd7） | 带 web failsafe 的 U-Boot |
| `immortalwrt-25.12.2-qualcommax-ipq807x-redmi_ax6-stock-squashfs-factory.ubi` | 要刷入的系统 | 约 16.5 MB，**必须是 redmi_ax6-stock 的 factory 固件** |

文件路径可以在 `config.ini` 的 `[paths]` 里改成别的名字或位置。

📥 **下载渠道、每个文件的 SHA-256 / MD5 校验值**，见 [SOURCES.md](SOURCES.md)。
固件不提交进 git 历史，配套固件包以 Release 附件形式提供。

> 校验：`ax6_auto.exe --check` 会在不碰设备的情况下检查这 5 个文件是否存在、
> 大小和格式是否正确（原厂固件的 HDR1 型号、过渡固件的 `UBI#` magic 等）。
