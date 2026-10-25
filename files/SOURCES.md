# 固件来源与校验值

本仓库**不存放**固件二进制文件。下面是每个文件的作用、获取渠道和校验值，
下载后请先比对，再放进 `files/` 目录。

> 为什么不直接放进来：**注明出处不等于获得分发许可**。版权授权（或权利人允许再分发）
> 才是能不能把文件放进仓库的依据，写“来源：XX”并不会改变这一点。
> 具体到这里的 5 个文件，原因各不相同，见下表。

## 文件清单

| 文件 | 大小（字节） | MD5 | SHA-256 | 能否随仓库分发 |
|------|-------------|-----|---------|----------------|
| `miwifi_ra69_firmware_45a77_1.0.18.bin` | 29,361,084 | `148b1ba175a445166990ec1755445a77` | `f25660739ad3b44fe8230af1f7c5f9e8e2cd7f3c67c7576457cffcf5dbe8f108` | ❌ 小米官方固件，版权归小米 |
| `xiaomimtd12.bin` | 37,486,592 | `a0675b1d70bfaca3043c63f6423bf708` | `2bd4ae18e56416712f374122b8e34c63b20ef055baec83ba8253f2b0eb24e68a` | ⚠️ 社区过渡固件，来源与授权不明确 |
| `ax6-uboot-mibib.bin` | 1,048,576 | `9096fde233e672eacbe502332df54160` | `64aa1d26558bb4d5bce172291e2768284991a83ae891c33aebb50ad3bf4f42d7` | ⚠️ 设备分区表镜像（厂商/社区产物） |
| `uboot-ax6.bin` | 642,838 | `ccd96596d6b9a5ec3fa06ec4d6effad3` | `dbf6ecc8598fec39e4697bb0dc73b8cc451c323abfa8a57fb6894026943aed85` | ⚠️ 基于 GPL-2.0 的 U-Boot，理论上可分发，但需能提供对应源码 |
| `immortalwrt-25.12.2-qualcommax-ipq807x-redmi_ax6-stock-squashfs-factory.ubi` | 17,301,504 | `83061bd84e8225dd918d660e5183c2bb` | `547bb89b2015610a708975ec07e3fdb7f218d8788fe73b1a3c118e8edd5816d1` | ✅ GPL-2.0，可从官方镜像直接下载（推荐用官方源） |

## 各文件的获取方式

### 1. 原厂固件 `miwifi_ra69_firmware_45a77_1.0.18.bin`

- **来源**：小米官方固件下载页 <https://www.miwifi.com/miwifi_download.html>
  （选择 Redmi AX6 / RA69，版本 1.0.18）。
- **授权**：小米的专有固件，未授予再分发许可。所以仓库不放，也不建议你在公开仓库里放——
  这类文件被投诉时会直接 DMCA 下架整个仓库。
- **校验**：下载后核对上表的 MD5/SHA-256，再确认脚本预检查能读出
  `HDR1 model_id = 32 (RA69)` 和版本 `1.0.18`。

### 2. 过渡固件 `xiaomimtd12.bin`

- **来源**：AX6 刷机社区流传的过渡 OpenWrt 镜像（通常来自恩山论坛、相关 GitHub 仓库或
  教程附件）。
- **授权**：来源和授权状态不明确，不同转载版本可能不一致。**请保留你下载时的原始出处**，
  不再分发。
- **特征**：文件头是 `UBI#`，大小约 36 MB。

### 3. MIBIB `ax6-uboot-mibib.bin`

- **来源**：AX6 的 MIBIB 分区镜像，随社区 U-Boot 移植包一起发布。
- **授权**：属于设备厂商/高通的二进制产物，不适合再分发。
- **特征**：固定 1 MiB（1,048,576 字节），写入 `mtd1`。

### 4. U-Boot `uboot-ax6.bin`

- **来源**：AX6 社区 U-Boot 移植，基于
  [pepe2k/u-boot_mod](https://github.com/pepe2k/u-boot_mod)（web failsafe）+ Das U-Boot。
- **授权**：Das U-Boot 与 u-boot_mod 是 **GPL-2.0**，二进制可以再分发，
  但 GPL 要求你能提供对应的完整源码（或书面的源码获取方式）。本仓库没有附源码，
  因此也不把该二进制放进来；如果你要分发，请同时给出你使用的 U-Boot 源码仓库与
  编译配置。
- **特征**：32 位 ARM EABI5 ELF，约 628 KB，写入 `mtd7`。

### 5. ImmortalWrt 系统 `immortalwrt-25.12.2-...-factory.ubi`

- **来源**（二选一）：
  - 固件选择器：<https://firmware-selector.immortalwrt.org/>
    （设备选 **Redmi AX6 (stock layout)**，版本 25.12.2，下载 **factory** 镜像）；
  - 官方下载目录：<https://downloads.immortalwrt.org/releases/25.12.2/targets/qualcommax/ipq807x/>
- **授权**：ImmortalWrt/OpenWrt 是 **GPL-2.0**，官方镜像本身就可以自由下载和再分发
  （其中包含的闭源 blobs 由上游统一分发）。即便如此，**推荐直接用官方源下载**：
  更新、校验和更快，也避免仓库体积膨胀。
- **注意**：必须是 **`redmi_ax6-stock`** 的 **factory** 镜像（AX6 是 stock 分区布局），
  不是 `sysupgrade` 包，也不是 `ubootmod` 版本。

## 校验方法

Windows PowerShell：

```powershell
Get-FileHash .\files\uboot-ax6.bin -Algorithm SHA256
```

Linux / macOS：

```bash
sha256sum files/*.bin files/*.ubi
```

或者直接让脚本检查（不会操作设备）：

```
ax6_auto.exe --check
```

它会检查文件是否存在、大小、原厂固件的 HDR1 型号、过渡固件的 `UBI#` magic 等。

## 关于 GitHub Releases

把固件传到 Releases 只是换了存放位置，**不改变授权问题**：小米原厂固件、
来源不明的过渡固件和 MIBIB 同样不适合公开再分发。
如果你确实想让别人“开箱即用”，比较稳妥的做法是：

1. 在 README 里放官方下载页链接（本文件已给出）；
2. 提供校验值（本文件已给出），让别人确认自己拿到的是对的版本；
3. GPL 的 U-Boot / ImmortalWrt 若要随 Release 分发，同时附上源码获取方式。
