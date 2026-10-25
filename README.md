# Redmi AX6 全自动刷机脚本（ImmortalWrt + Argon + OpenClash + SmartDNS）

针对 **Redmi AX6（型号 RA69，IPQ807x，NAND/UBI）** 的一键式刷机脚本，
把原本需要手工执行的一整套流程自动化：

| 步骤 | 内容 | 是否需要人工 |
|------|------|--------------|
| 1 | 解锁/确认 SSH，降级到原厂 `1.0.18` 固件 | 不需要（需要路由器后台密码） |
| 2 | 刷过渡固件 `xiaomimtd12.bin`，重启后写入 MIBIB(mtd1) 与 U-Boot(mtd7) | 不需要 |
| — | **断电 → 按住 Reset → 上电**，进入 U-Boot Web | **唯一需要人工的一步** |
| 3 | 通过 U-Boot Web 上传并刷入 ImmortalWrt factory 固件 | 不需要 |
| 4 | SSH 进入 ImmortalWrt，安装 Argon 主题 / OpenClash / SmartDNS | 不需要 |

> 说明：教程里提到的 “breed” 对 AX6 并不适用。AX6 使用的是
> `ax6-uboot-mibib.bin` + `uboot-ax6.bin`（写入 mtd1 / mtd7），
> 本脚本就是按这个方式实现的。

⚠️ **刷机有风险**。写 MIBIB / U-Boot 属于不可逆操作，刷写过程中断电可能变砖。
请全程使用网线、保证供电稳定，并确认你愿意承担风险。

---

## 一、准备工作

1. **系统**：Windows 10/11，电脑必须用**网线**连接到 AX6 的 **LAN 口**
   （第 3 步 U-Boot 阶段不支持 Wi-Fi）。
2. **权限**：需要修改网卡（临时 IP）时会用到管理员权限，
   `run_ax6_auto.bat` 会自动申请。
3. **固件文件**（仓库不附带，请自行准备并放到 `files\` 目录）：

   ```
   files\miwifi_ra69_firmware_45a77_1.0.18.bin
   files\xiaomimtd12.bin
   files\ax6-uboot-mibib.bin
   files\uboot-ax6.bin
   files\immortalwrt-25.12.2-qualcommax-ipq807x-redmi_ax6-stock-squashfs-factory.ubi
   ```

   路径都可以在 `config.ini` 的 `[paths]` 中修改。

   📥 **每个文件从哪下载、SHA-256 校验值是多少、为什么仓库不直接附带**，
   见 [files/SOURCES.md](files/SOURCES.md)。

   ⚠️ ImmortalWrt 必须使用 **`redmi_ax6-stock`** 版本的 factory 固件
   （AX6 是 stock 分区布局）。

4. **路由器后台密码**：第 1 步自动解锁 SSH 需要小米路由器后台（Web）管理密码。
   可以提前写在 `config.ini` 的 `stock_web_password`，也可以在运行时按提示输入。

5. **第 1 步的 SSH 解锁**依赖 [XMiR-Patcher](https://github.com/openwrt-xiaomi/xmir-patcher)
   的 `connect.py`。如果设备 SSH 还没开启，可以：
   - 先用 XMiR-Patcher 的 `run.bat` → `2` 手动解锁；或
   - 把本目录放进 `xmir-patcher` 目录内再运行（此时脚本会自动调用）。

---

## 二、快速开始

### 方式 A：使用打包好的 exe（推荐，不需要装 Python）

```
双击运行：run_ax6_auto.bat
```

`run_ax6_auto.bat` 会优先调用同目录的 `ax6_auto.exe`；
没有 exe 时回退到 `..\python\python.exe`（放在 xmir-patcher 目录内时可用）。

也可以直接双击 `ax6_auto.exe`。

### 方式 B：从源码运行

需要 Python 3.8+（打包/验证环境为 3.14）：

```
pip install -r requirements.txt
python ax6_auto.py
```

脚本会自动：

1. 需要时申请管理员权限；
2. 检查固件文件（`--check` 逻辑始终会先执行）；
3. 探测设备当前状态（原厂 / 过渡系统 / U-Boot / ImmortalWrt），自动从对应步骤继续；
4. 一步步完成刷机，并在日志中记录每个关键操作与校验结果。

日志：`ax6_auto.log`　　状态：`state.json`

### 命令行参数

```
run_ax6_auto.bat                 # 自动检测并继续
run_ax6_auto.bat --check         # 只做预检查，不碰设备
run_ax6_auto.bat --stage 3       # 从第 3 步开始（例如已在 U-Boot Web）
run_ax6_auto.bat --stage 4       # 只做第 4 步（安装软件包）
run_ax6_auto.bat --yes           # 跳过所有确认
run_ax6_auto.bat --keep-ip       # 结束后保留临时 IP 192.168.1.10
run_ax6_auto.bat --fix-net       # 网卡 IP 异常时恢复 DHCP（见常见问题 8）
```

---

## 三、流程细节

### 第 1 步：降级到 1.0.18

- 连接 `192.168.31.1`，尝试 SSH（默认 `root/root`）。
- 如果 SSH 不可用，脚本会调用 XMiR-Patcher 的 `connect.py` 自动解锁。
- 读取设备信息，确认型号是 **RA69**、当前启动分区（`rootfs` / `rootfs_1`）。
- 若版本已经是 `1.0.18`，**直接跳过降级**。
- 否则：从 HDR1 固件中提取 `root.ubi`（缓存在 `cache/`）→ SSH 上传到 `/tmp`
  → `mtd -e <非活动分区> write` → 回读 MD5 校验 → 切换 NVRAM 启动标志 → 重启。

> 安全设计：脚本只写**非活动**分区，写入并校验成功后才切换启动标志。

### 第 2 步：过渡固件 + bootloader

**2A 刷过渡固件**：同样写非活动分区，MD5 校验通过后切启动标志并重启。
过渡系统地址为 `192.168.1.1`。脚本在确认原厂固件已经离开后，才会临时给电脑网卡
增加 `192.168.1.10/24`；结束时（包括异常退出）会自动恢复 DHCP。

**2B 写 MIBIB 与 U-Boot**：

- 通过 Telnet 登录过渡系统（默认免密），设置 root 密码、重启 dropbear，
  之后改用 SSH 上传文件。
- 校验分区表：mtd1 必须是 `0:MIBIB`，mtd7 必须是 `0:APPSBL`。
- 依次执行：

  ```
  mtd erase /dev/mtd1
  mtd write /tmp/ax6-uboot-mibib.bin /dev/mtd1
  mtd erase /dev/mtd7
  mtd write /tmp/uboot-ax6.bin /dev/mtd7
  ```

- 每个分区写完都会回读做 MD5 校验。完成后不会自动重启。

**唯一的人工步骤**

1. 拔掉 AX6 电源；
2. 按住 Reset 键不放；
3. 插上电源，继续按住约 10 秒；
4. 松开 Reset；
5. 保持网线连接。

脚本会持续探测 `http://192.168.1.1`（识别 `Server: uIP/0.9`），一检测到 U-Boot 就自动继续。

### 第 3 步：U-Boot 刷入 ImmortalWrt

- 按 U-Boot Web 的 multipart 协议（`name="firmware"`）上传 factory UBI。
- 识别页面中的 `UPDATE IN PROGRESS` / `UPDATE FAILED` 判断结果。
- 等待 ImmortalWrt 启动（首次启动会自动创建 rootfs_data，约 2–5 分钟）。

### 第 4 步：安装软件

- 设置 root 密码（默认 `root`，可在 `config.ini` 修改）：
  优先用 factory 固件的空密码 SSH 直接修改，其次用 ubus RPC。
- 同步系统时间（NTP 未就绪时用电脑时间兜底），避免 HTTPS 软件源证书报错。
- `apk update`（老版本自动用 `opkg`），然后安装：
  - `luci-theme-argon` + `luci-app-argon-config`，并把 LuCI 主题切到 Argon；
  - `luci-app-openclash`；
  - `smartdns` + `luci-app-smartdns`，并启用服务；
  - 可选中文语言包（`luci-i18n-*-zh-cn`）。

> OpenClash 只安装界面与依赖，**不会**自动订阅/下载内核。
> 首次使用时请在 LuCI → OpenClash 中更新内核并导入订阅。
> SmartDNS 默认监听 6053，上游 DNS 请在 LuCI 中按需配置。

---

## 四、配置文件 `config.ini`

```ini
[paths]         # 五个固件/文件的路径（相对本目录）
[network]       # stock_ip=192.168.31.1  router_ip=192.168.1.1  pc_ip=192.168.1.10
[credentials]   # 原厂 SSH 密码、后台密码、ImmortalWrt 新 root 密码
[options]       # 是否跳过降级、安装哪些包、是否确认写 bootloader、是否删除临时 IP
```

常用修改：

- 后台密码：`stock_web_password = 你的密码`
- 想要不同的 root 密码：`new_root_password = 你的密码`
- 不想装 OpenClash：`install_openclash = no`
- 结束时保留临时 IP：`remove_temp_ip = no`（或命令行 `--keep-ip`）

---

## 五、断点续跑

随时可以中断，重新运行即可。脚本会按当前设备状态自动选择步骤：

| 设备状态 | 脚本行为 |
|----------|----------|
| 原厂 `192.168.31.1` 在线 | 从第 1 步开始（版本已是 1.0.18 会自动跳过降级） |
| 过渡系统 / 已刷过渡固件 | 直接执行 2B（写 bootloader），不重复刷过渡固件 |
| U-Boot Web 页面 | 直接执行第 3 步 |
| ImmortalWrt 已启动 | 直接执行第 4 步 |

如果自动判断不准确，用 `--stage N` 手动指定。

---

## 六、常见问题

**1. “找不到 192.168.31.1 / 设备不在线”**

- 确认用网线连接 AX6 的 **LAN 口**（不是 WAN 口）；
- 确认电脑网卡不是“已禁用”状态；
- 原厂固件地址是 `192.168.31.1`；过渡固件/U-Boot/ImmortalWrt 是 `192.168.1.1`。

**2. 第 1 步解锁 SSH 失败**

- 检查 `config.ini` 里的 `stock_web_password` 是否正确；
- 手动运行 XMiR-Patcher 的 `run.bat`，选择 `2 - Connect to device`，
  确认能解锁后再运行本脚本；
- 部分固件版本需要手机验证码，无法全自动，请先手动完成解锁。

**3. 上传很慢或中途失败**

- 都是走 SSH 的 stdin 流式上传（原厂没有 scp），速度取决于网络与 CPU；
- 失败后重跑即可，写入前都会重新上传并校验 MD5。

**4. 等待 U-Boot 超时**

- 重新断电，按住 Reset 再上电；
- 确认电脑是用**网线**连接，且 `192.168.1.10` 已加到该网卡；
- 然后重新运行脚本（会自动进入第 3 步）。

**5. 第 4 步 `apk update` 失败**

- 路由器 WAN 口需要能上网（AX6 刷完后 WAN 口默认 DHCP）；
- 若是时间问题（证书 not yet valid），脚本会自动等 NTP/用电脑时间校正后重试；
- 仍失败可进入 LuCI → 系统 → 软件包 → 配置 换一个镜像，再用 `--stage 4` 重跑。

**6. 刷完后电脑打不开 192.168.1.1**

- ImmortalWrt 的 LAN 是 `192.168.1.1`；
- 如果电脑网卡还保留着 `192.168.31.x` 的静态地址，请改为“自动获取（DHCP）”。

**7. 第 4 步无法自动设置密码**

- 用浏览器打开 `http://192.168.1.1`，用户名 `root`、空密码登录，
  手动设置密码后，再用 `--stage 4` 重跑。

**8. 网卡变成 `169.254.x.x`，连不上路由器**

Windows 在给网卡添加临时静态 IP 时会关闭该网卡的 DHCP。正常情况下脚本
结束时会自动恢复；如果脚本被强制结束，可以手动恢复：

```
run_ax6_auto.bat --fix-net
```

等价的命令（需要管理员）：

```
netsh interface ipv4 set address name="以太网" source=dhcp
ipconfig /renew "以太网"
```

---

## 七、安全与风险提示

- 写 `mtd1`（MIBIB）和 `mtd7`（U-Boot）属于**不可逆操作**，脚本默认会二次确认
  （`confirm_bootloader = yes`；`--yes` 可跳过）。
- 每次写分区都会先做 MD5 上传校验，写完再回读校验，校验不过不会继续。
- 脚本永远不会写当前正在运行的分区（只写非活动分区）。
- 强烈建议全程使用有线连接，并保证供电稳定。

---

## 八、技术笔记：U-Boot Web 上传的 `Content-Length` 陷阱

AX6 的 U-Boot（基于 [pepe2k/u-boot_mod](https://github.com/pepe2k/u-boot_mod) 的 web failsafe）
在解析 POST 头时是这样取长度：

```c
start = strstr(uip_appdata, "Content-Length:");
start += sizeof("Content-Length:");   /* sizeof 含结尾 '\0' */
hs->upload_total = atoi(start);
```

`sizeof` 把结尾的 `'\0'` 也算进去了，所以真正的起点是冒号后**第 2 个字符**。
浏览器发送的 `Content-Length: 17301753` 中，那个空格正好被跳过；
如果写成 `Content-Length:17301753`（没有空格），首位 `1` 会被吃掉，
U-Boot 会认为文件只有 7,324,000 字节，传到 7.28 MiB 就判定“上传完成”、
关闭网口并刷入截断的镜像，客户端表现为 `WinError 10054`。

这就是“浏览器手动能刷、脚本却总在 7.28 MiB 失败”的原因。脚本现在固定发送
`Content-Length: %d`（**冒号后带空格**），并在代码里重点注释了这一点。

---

## 九、致谢

这个脚本能跑通，离不开前人的工作：

- **Das U-Boot（U-Boot 引导程序）** —— 作者 Wolfgang Denk、DENX Software Engineering
  及所有贡献者；以及 [pepe2k/u-boot_mod](https://github.com/pepe2k/u-boot_mod)
  提供的 web failsafe（本项目的 U-Boot 上传协议、`Content-Length` 解析细节都来自它）。
- **XMiR-Patcher** —— 作者 [remittor](https://github.com/remittor)
  （[openwrt-xiaomi/xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher)）。
  本项目的第 1 步 SSH 解锁直接复用它，降级与 NVRAM 标志的做法也参考了它。
- **ImmortalWrt / OpenWrt 社区** —— 固件、Argon 主题、OpenClash、SmartDNS
  以及无数踩坑记录。
- **广大网友** —— 各种教程、issue、论坛帖子里分享的 AX6 刷机经验与踩坑总结。

更多说明见 [THANKS.md](THANKS.md)。

---

## 十、许可证

本项目代码以 **MIT License** 发布，详见 [LICENSE](LICENSE)。

第三方组件仍遵循各自的许可证：U-Boot / u-boot_mod 与 ImmortalWrt 为 GPL-2.0，
paramiko / ssh2-python 为 LGPL-2.1，详见 [THANKS.md](THANKS.md) 与
[files/SOURCES.md](files/SOURCES.md)。
