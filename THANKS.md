# 致谢 / Acknowledgements

这个脚本只是把很多人的工作串了起来。感谢：

## Das U-Boot

- **项目**：Das U-Boot —— 通用引导程序
- **作者**：Wolfgang Denk、DENX Software Engineering 以及所有贡献者
- **站点**：https://www.denx.de/wiki/U-Boot 、 https://source.denx.de/u-boot/u-boot

Redmi AX6 通过 `uboot-ax6.bin` 进入的 web 恢复界面，让我们可以在没有串口的情况下
刷入 ImmortalWrt。没有 U-Boot，这个流程根本不存在。

## pepe2k/u-boot_mod

- **项目**：u-boot_mod —— 带 **web failsafe** 的 U-Boot 修改版
- **作者**：Piotr Dymacz (pepe2k) 及贡献者
- **仓库**：https://github.com/pepe2k/u-boot_mod

脚本里第 3 步的整个上传协议（multipart、`name="firmware"`、`UPDATE IN PROGRESS`
判断）都是按它的 `httpd.c` 实现的。README 第八节记录的 `Content-Length` 空格陷阱，
也是从这个项目的源码和反汇编里确认的——它帮我们省了很久的排查时间。

## XMiR-Patcher

- **项目**：XMiR-Patcher —— 小米路由器固件修补 / 解锁工具
- **作者**：**remittor**
- **仓库**：https://github.com/openwrt-xiaomi/xmir-patcher
- **捐赠**：https://github.com/remittor/donate

本项目的第 1 步直接复用它的 `connect.py` 来解锁 SSH；降级写分区、
`flag_last_success` / `flag_boot_rootfs` / `flag_boot_success` 这些 NVRAM 启动标志的
处理方式也参考了它。如果你觉得这个流程好用，也请去给 XMiR-Patcher 点个 star。

## ImmortalWrt / OpenWrt

- **ImmortalWrt**：https://github.com/immortalwrt/immortalwrt
- **OpenWrt**：https://openwrt.org

第 3、4 步刷入的固件、`apk` 包管理、LuCI 以及下面这些插件都来自它们：

| 组件 | 说明 |
|------|------|
| luci-theme-argon | Argon 主题 |
| luci-app-openclash | OpenClash 代理客户端 |
| smartdns | SmartDNS |

## 广大网友

- 各种 AX6 刷机教程、`xiaomimtd12` 过渡固件与 U-Boot 移植的分享者；
- 在论坛、Issue、博客里记录“7.28 MiB 上传失败”“uIP poll 上限”“U-Boot web failsafe
  只能接受一个连接”等细节的朋友；
- 所有把踩坑过程写下来的人——这些记录是本项目能全自动跑通的基础。

## 第三方依赖

| 依赖 | 许可证 |
|------|--------|
| Python | PSF License |
| paramiko | LGPL-2.1 |
| ssh2-python（可选，仅源码模式） | LGPL-2.1 |
| PyInstaller（仅打包时） | GPL-2.0 + Bootloader exception |

本仓库代码本身以 MIT 发布，见 [LICENSE](LICENSE)。
上面这些第三方依赖与固件仍遵循各自的许可证（LGPL-2.1 / GPL-2.0 等）。
