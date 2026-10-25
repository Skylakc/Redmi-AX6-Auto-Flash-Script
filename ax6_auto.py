#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AX6 全自动刷机脚本
==================

适用设备：Redmi AX6 (RA69, IPQ807x, NAND/UBI)

流程：
  第 1 步  开启/确认 SSH，把固件降级到 1.0.18（已经是 1.0.18 则跳过）
  第 2 步  刷过渡固件 xiaomimtd12.bin，重启进入过渡 OpenWrt，
           再写入 ax6-uboot-mibib.bin (mtd1) 与 uboot-ax6.bin (mtd7)
  第 3 步  断电按住 Reset 进入 U-Boot Web，上传并刷入 ImmortalWrt factory UBI
  第 4 步  SSH 登录 ImmortalWrt，安装 Argon 主题 / OpenClash / SmartDNS

用法：
  - 双击 run_ax6_auto.bat（优先用同目录的 ax6_auto.exe，不需要装 Python）
  - 或直接运行 ax6_auto.exe / python ax6_auto.py
  - 需要修改网卡（添加临时 IP）时必须以管理员身份运行，
    run_ax6_auto.bat 会自动申请；网卡上已有 192.168.1.x 地址时则不需要。

    ax6_auto.exe               # 自动检测状态并继续
    ax6_auto.exe --check       # 只做检查，不碰设备
    ax6_auto.exe --stage 3     # 从第 3 步开始
    ax6_auto.exe --yes         # 跳过所有确认

日志：ax6_auto.log        状态：state.json
"""

import argparse
import configparser
import ctypes
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import traceback
from datetime import datetime

# PyInstaller 打包后 __file__ 指向临时解包目录，配置/日志/固件都要跟着 exe 走
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "cache")
LOG_PATH = os.path.join(BASE_DIR, "ax6_auto.log")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

# 优先使用 vendor/ 里的新版 ssh2（libssh2 1.10）。
# 仓库自带 Python 里的 ssh2 0.26 无法与 ImmortalWrt 24.10+/25.x 的
# dropbear 完成密钥交换（握手报 KexFailureError）。
_VENDOR_DIR = os.path.join(BASE_DIR, "vendor")
if os.path.isdir(_VENDOR_DIR):
    sys.path.insert(0, _VENDOR_DIR)

IS_WIN = (os.name == "nt")

RA69_HDR_MODEL_ID = 32
XD = 0x100000          # 1 MiB，mtd1 / mtd7 以及 MIBIB 文件大小
ROOTFS_PART_SIZE = 0x23C0000   # 原厂 rootfs / rootfs_1 分区大小


# ============================================================================
#  日志 / 输出
# ============================================================================

_log_fp = None
_progress_active = False


def log_open():
    global _log_fp
    try:
        _log_fp = open(LOG_PATH, "a", encoding="utf-8")
    except Exception:
        _log_fp = None


def log(msg=""):
    global _progress_active
    if _progress_active:
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        _progress_active = False
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)
        except Exception:
            pass
    if _log_fp is not None:
        try:
            _log_fp.write(msg + "\n")
            _log_fp.flush()
        except Exception:
            pass


def log_section(title):
    log("")
    log("=" * 70)
    log("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), title))
    log("=" * 70)


def log_note(msg):
    log("  - " + msg)


def progress(msg):
    global _progress_active
    try:
        sys.stdout.write("\r" + msg + "        ")
        sys.stdout.flush()
        _progress_active = True
    except Exception:
        pass


def progress_end():
    global _progress_active
    if _progress_active:
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        _progress_active = False


class FlashError(Exception):
    pass


def die(msg, code=1):
    progress_end()
    log("")
    log("!! 失败: " + str(msg))
    log("!! 详细日志：" + LOG_PATH)
    raise SystemExit(code)


def ask_yes(question, default_yes=True):
    try:
        s = input(question).strip().lower()
    except Exception:
        return default_yes
    if not s:
        return default_yes
    return s in ("y", "yes", "是", "好", "1", "确认")


def ask_text(question, default=""):
    try:
        s = input(question).strip()
    except Exception:
        return default
    return s if s else default


# ============================================================================
#  基础工具
# ============================================================================

def human_size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024.0 or unit == "GiB":
            return ("%.0f %s" if unit == "B" else "%.2f %s") % (n, unit)
        n /= 1024.0
    return "%.2f GiB" % n


def md5_file(path, size=None):
    h = hashlib.md5()
    left = size
    with open(path, "rb") as f:
        while True:
            n = 1024 * 1024
            if left is not None:
                if left <= 0:
                    break
                n = min(n, left)
            buf = f.read(n)
            if not buf:
                break
            h.update(buf)
            if left is not None:
                left -= len(buf)
    return h.hexdigest()


def read_head(path, n=16):
    with open(path, "rb") as f:
        return f.read(n)


def sleep_s(sec):
    time.sleep(sec)


def shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def shadow_root_is_empty(text):
    """判断 grep '^root:' /etc/shadow 的输出中 root 密码是否仍为空"""
    m = re.search(r"(?m)^root:([^:\r\n]*)", text or "")
    return bool(m) and m.group(1) == ""


def is_admin():
    if not IS_WIN:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def tcp_open(ip, port, timeout=1.5, bind_ip=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        if bind_ip:
            s.bind((bind_ip, 0))
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def local_ip_for(ip, port=80):
    """返回访问 ip 时本机会使用的源地址（不会真的发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, port))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def wait_until(desc, fn, timeout, interval=2.0, on_tick=None):
    """轮询 fn()，返回其第一个真值；超时返回 None"""
    t0 = time.time()
    last_print = 0.0
    while True:
        try:
            val = fn()
        except Exception:
            val = None
        if val:
            progress_end()
            return val
        if timeout and (time.time() - t0) > timeout:
            progress_end()
            return None
        now = time.time()
        if now - last_print >= 5:
            progress("等待 %s ... (%d 秒)" % (desc, int(now - t0)))
            last_print = now
            if on_tick:
                try:
                    on_tick()
                except Exception:
                    pass
        sleep_s(interval)


# ============================================================================
#  Windows 网卡处理（加/删 192.168.1.x 临时地址）
# ============================================================================

def _enum_adapters():
    if not IS_WIN:
        return []

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p),
                    ("iSockaddrLength", ctypes.c_int)]

    class UCAST(ctypes.Structure):
        pass

    UCAST._fields_ = [
        ("Length", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Next", ctypes.POINTER(UCAST)),
        ("Address", SOCKET_ADDRESS),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", ctypes.c_ulong),
        ("PreferredLifetime", ctypes.c_ulong),
        ("LeaseLifetime", ctypes.c_ulong),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]

    class ADDR(ctypes.Structure):
        pass

    ADDR._fields_ = [
        ("Length", ctypes.c_ulong),
        ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.POINTER(ADDR)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(UCAST)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", ctypes.c_ulong),
        ("ZoneIndices", ctypes.c_ulong * 16),
        ("FirstPrefix", ctypes.c_void_p),
    ]

    size = ctypes.c_ulong(16000)
    buf = ctypes.create_string_buffer(size.value)
    ret = ctypes.windll.iphlpapi.GetAdaptersAddresses(
        0, 0, None, ctypes.byref(buf), ctypes.byref(size))
    if ret == 111:  # ERROR_BUFFER_OVERFLOW
        buf = ctypes.create_string_buffer(size.value)
        ret = ctypes.windll.iphlpapi.GetAdaptersAddresses(
            0, 0, None, ctypes.byref(buf), ctypes.byref(size))
    if ret != 0:
        return []

    out = []
    p = ctypes.cast(buf, ctypes.POINTER(ADDR))
    while p:
        a = p.contents
        ips = []
        ua = a.FirstUnicastAddress
        while ua:
            sa = ua.contents.Address
            if sa.lpSockaddr:
                try:
                    raw = ctypes.string_at(sa.lpSockaddr, 16)
                    fam = struct.unpack("<H", raw[0:2])[0]
                    if fam == socket.AF_INET:
                        ips.append(socket.inet_ntoa(raw[4:8]))
                except Exception:
                    pass
            ua = ua.contents.Next
        out.append({
            "name": a.FriendlyName or "",
            "desc": a.Description or "",
            "iftype": a.IfType,
            "up": (a.OperStatus == 1),
            "ips": ips,
            "index": a.IfIndex,
        })
        p = a.Next
    return out


def find_adapter(a, bind_ip=None):
    for ad in _enum_adapters():
        if ad["name"] == a and ad["up"]:
            return ad
    return None


def adapter_has_ip(adapter_name, ip):
    ad = find_adapter(adapter_name)
    return bool(ad and ip in ad["ips"])


def _run_ps(command, timeout=40):
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as e:
        return -1, str(e)


def adapter_dhcp_enabled(adapter_name):
    """网卡是否启用了 DHCP：True / False / None（查询失败）"""
    q = ("(Get-NetIPInterface -InterfaceAlias '%s' -AddressFamily IPv4 "
         "-ErrorAction Stop).Dhcp") % adapter_name.replace("'", "''")
    rc, out = _run_ps(q)
    s = out.strip().lower()
    if "disabled" in s:
        return False
    if "enabled" in s:
        return True
    return None


def netsh_add_ip(adapter_name, ip, mask):
    """添加临时地址，返回 (rc, output, was_dhcp)。

    实测：Windows（netsh 和 New-NetIPAddress 都一样）会把 DHCP 网卡切成
    “静态”，原有的 192.168.31.x 地址立即消失。因此调用本函数前必须已经
    不再需要原厂地址；cleanup_adapter() 会用 was_dhcp 恢复 DHCP。
    """
    was_dhcp = adapter_dhcp_enabled(adapter_name)
    try:
        p = subprocess.run(
            ["netsh", "interface", "ipv4", "add", "address",
             adapter_name, ip, mask],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace")
        rc, out = p.returncode, (p.stdout or "").strip()
    except Exception as e:
        rc, out = -1, str(e)
    return rc, out, was_dhcp


def enable_adapter_dhcp(adapter_name):
    """把网卡恢复为 DHCP 并续租，返回最终 DHCP 状态"""
    try:
        subprocess.run(
            ["netsh", "interface", "ipv4", "set", "address",
             "name=%s" % adapter_name, "source=dhcp"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace")
    except Exception:
        pass
    if adapter_dhcp_enabled(adapter_name) is not True:
        q = ("Set-NetIPInterface -InterfaceAlias '%s' -AddressFamily IPv4 "
             "-Dhcp Enabled -ErrorAction Stop") % adapter_name.replace("'", "''")
        _run_ps(q)
    try:
        subprocess.run(
            ["ipconfig", "/renew", adapter_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=90)
    except Exception:
        pass
    return adapter_dhcp_enabled(adapter_name)


def netsh_del_ip(adapter_name, ip, was_dhcp=None):
    q = ("Remove-NetIPAddress -InterfaceAlias '%s' -IPAddress '%s' "
         "-Confirm:$false -ErrorAction SilentlyContinue") % (
        adapter_name.replace("'", "''"), ip)
    _run_ps(q)

    if adapter_has_ip(adapter_name, ip):
        try:
            subprocess.run(
                ["netsh", "interface", "ipv4", "delete", "address",
                 adapter_name, ip],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace")
        except Exception:
            pass

    # 如果添加前网卡是 DHCP，确保清理后恢复 DHCP
    if was_dhcp and adapter_dhcp_enabled(adapter_name) is not True:
        q2 = ("Set-NetIPInterface -InterfaceAlias '%s' -AddressFamily IPv4 "
              "-Dhcp Enabled -ErrorAction Stop") % adapter_name.replace("'", "''")
        rc, _ = _run_ps(q2)
        if adapter_dhcp_enabled(adapter_name) is not True:
            try:
                subprocess.run(
                    ["netsh", "interface", "ipv4", "set", "address",
                     "name=%s" % adapter_name, "source=dhcp"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace")
            except Exception:
                pass
    if was_dhcp:
        try:
            subprocess.run(
                ["ipconfig", "/renew", adapter_name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=60)
        except Exception:
            pass


_VIRTUAL_HINTS = ("vmware", "virtualbox", "hyper-v", "vethernet", "tap-windows",
                  "openvpn", "tailscale", "zerotier", "wintun", "wireguard",
                  "loopback", "bluetooth", "npcap", "wsl", "vpn")


def _is_virtual(a):
    s = (a.get("name", "") + " " + a.get("desc", "")).lower()
    return any(h in s for h in _VIRTUAL_HINTS)


def _subnet_prefix(ip):
    return ".".join(ip.split(".")[:3])


def _has_real_ip(a):
    return any(not ip.startswith("169.254.") for ip in a["ips"])


def pick_router_adapter(stock_ip, router_ip, state):
    """找出与路由器相连的那块网卡（优先有线物理网卡）"""
    adapters = [a for a in _enum_adapters() if a["up"]]
    by_name = dict((a["name"], a) for a in adapters)

    # 1) 之前记录过的网卡
    prev = state.get("adapter") if state else None
    if prev and prev in by_name:
        return by_name[prev]

    with_ip = [a for a in adapters if _has_real_ip(a)]
    physical = [a for a in with_ip if not _is_virtual(a)]

    def by_subnet(target, pool):
        pfx = _subnet_prefix(target)
        for a in pool:
            if any(_subnet_prefix(ip) == pfx for ip in a["ips"]):
                return a
        return None

    # 2) 已经和原厂固件 / 路由器在同一网段
    for target in (stock_ip, router_ip):
        a = by_subnet(target, physical) or by_subnet(target, with_ip)
        if a:
            return a

    # 3) 按路由探测本机源地址（只认物理网卡，避免选中 VPN/虚拟网卡）
    for target in (stock_ip, router_ip):
        lip = local_ip_for(target)
        if lip and lip != "0.0.0.0":
            for a in physical:
                if lip in a["ips"]:
                    return a

    # 4) 有地址的有线物理网卡
    cands = [a for a in physical if a["iftype"] == 6]
    if cands:
        cands.sort(key=lambda x: x["index"])
        return cands[0]

    # 5) 任意有地址的物理网卡
    if physical:
        return physical[0]

    # 6) 兜底
    cands = [a for a in with_ip if a["iftype"] == 6] or with_ip
    if cands:
        cands.sort(key=lambda x: x["index"])
        return cands[0]
    return None


# ============================================================================
#  HTTP 探测 / ubus RPC
# ============================================================================

def http_probe(ip, bind_ip=None, timeout=3.0, path="/"):
    """最小化 HTTP GET，返回原始响应字节（读到响应头结束为止）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if bind_ip:
            s.bind((bind_ip, 0))
        s.settimeout(timeout)
        s.connect((ip, 80))
        req = ("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: ax6-auto\r\n"
               "Connection: close\r\n\r\n" % (path, ip))
        s.sendall(req.encode("latin-1"))
        data = b""
        while len(data) < 16384:
            try:
                buf = s.recv(4096)
            except socket.timeout:
                break
            if not buf:
                break
            data += buf
            if b"\r\n\r\n" in data:
                break
        return data
    except Exception:
        return None
    finally:
        # 用 RST 而不是正常 FIN 关闭：U-Boot 的 uIP 协议栈连接槽很少，
        # 正常关闭会让槽位在 TIME_WAIT 停留约 2 分钟，探测几次后
        # 真正的上传连接就会因为没有空槽而超时。
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


def http_status(raw):
    if not raw:
        return 0
    m = re.match(rb"HTTP/1\.[01] (\d{3})", raw)
    return int(m.group(1)) if m else 0


def http_header(raw, name):
    if not raw:
        return ""
    m = re.search((r"(?im)^" + re.escape(name) + r":\s*(.+?)\s*$").encode("latin-1"), raw)
    return m.group(1).decode("latin-1") if m else ""


def is_uboot_web(raw):
    return bool(raw) and b"Server: uIP/0.9" in raw


def is_http_up(raw):
    return bool(raw) and http_status(raw) in (200, 301, 302, 303, 401, 403)


def ubus_call(ip, session, obj, method, args, bind_ip=None, timeout=15):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "call",
        "params": [session, obj, method, args],
    }).encode("utf-8")
    try:
        conn = http.client.HTTPConnection(
            ip, 80, timeout=timeout,
            source_address=((bind_ip, 0) if bind_ip else None))
        conn.request("POST", "/ubus", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return {"error": str(e)}


def ubus_code(res):
    if not isinstance(res, dict):
        return -1
    r = res.get("result")
    if not isinstance(r, list) or not r:
        return -1
    try:
        return int(r[0])
    except Exception:
        return -1


def ubus_login(ip, password, bind_ip=None, timeout=15):
    res = ubus_call(ip, "00000000000000000000000000000000",
                    "session", "login",
                    {"username": "root", "password": password},
                    bind_ip, timeout)
    if ubus_code(res) != 0:
        return None
    try:
        return res["result"][1]["ubus_rpc_session"]
    except Exception:
        return None


# ============================================================================
#  SSH
# ============================================================================

class SSHClient(object):
    def __init__(self, ip, user, password, bind_ip=None, port=22, timeout=20):
        self.ip = ip
        self.user = user
        self.password = password
        self.bind_ip = bind_ip
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.sess = None
        self.transport = None      # paramiko 后端（打包成 exe 或没有 ssh2 时使用）
        self.kind = "ssh2"

    @staticmethod
    def _ssh2_available():
        """检测能否 import 到兼容当前 Python 的 ssh2。

        仓库自带的 python/ 目录里是 cp38 的 .pyd，用别的 Python 版本
        import 会报 "python38.dll conflicts"，这种情况下必须退回 paramiko。
        """
        try:
            import ssh2.session  # noqa: F401
            return True
        except ImportError:
            pass
        fallback = os.path.join(REPO_DIR, "python")
        if not os.path.isdir(fallback):
            return False
        added = fallback not in sys.path
        if added:
            sys.path.insert(0, fallback)
        try:
            import ssh2.session  # noqa: F401
            return True
        except Exception:
            if added:
                try:
                    sys.path.remove(fallback)
                except ValueError:
                    pass
            for m in list(sys.modules):
                if m == "ssh2" or m.startswith("ssh2."):
                    del sys.modules[m]
            return False

    def connect(self):
        if not self._ssh2_available():
            # 没有可用的 ssh2（例如 PyInstaller 打包后用 3.14 运行），
            # 改用纯 Python 的 paramiko
            return self._connect_paramiko()
        import ssh2
        import ssh2.session  # noqa: F401
        self.kind = "ssh2"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.bind_ip:
            self.sock.bind((self.bind_ip, 0))
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.ip, self.port))
        self.sock.settimeout(None)
        self.sess = ssh2.session.Session()
        self.sess.set_timeout(self.timeout * 1000)
        self.sess.handshake(self.sock)
        self.sess.userauth_password(self.user, self.password)
        self.sess.set_blocking(True)
        self.sess.set_timeout(self.timeout * 1000)
        return self

    # ---- paramiko 后端 ----
    def _connect_paramiko(self):
        import paramiko
        self.kind = "paramiko"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.bind_ip:
            sock.bind((self.bind_ip, 0))
        sock.settimeout(self.timeout)
        sock.connect((self.ip, self.port))
        sock.settimeout(None)
        t = paramiko.Transport(sock)
        t.start_client(timeout=self.timeout)
        t.auth_password(self.user, self.password)
        self.transport = t
        self.sock = sock
        return self

    def _paramiko_exec(self, cmd, timeout=60):
        ch = self.transport.open_session()
        try:
            ch.settimeout(timeout)
            ch.exec_command(cmd)
            chunks = []
            deadline = time.time() + timeout
            while True:
                while ch.recv_ready():
                    chunks.append(ch.recv(65536))
                while ch.recv_stderr_ready():
                    chunks.append(ch.recv_stderr(65536))
                if ch.exit_status_ready():
                    break
                if time.time() > deadline:
                    break
                time.sleep(0.05)
            # 收尾：退出后可能还有剩余数据
            while ch.recv_ready():
                chunks.append(ch.recv(65536))
            while ch.recv_stderr_ready():
                chunks.append(ch.recv_stderr(65536))
            try:
                status = ch.recv_exit_status()
            except Exception:
                status = -1
            return status, b"".join(chunks).decode("utf-8", "replace")
        except Exception as e:
            return -1, "SSH 执行失败: %s" % e
        finally:
            try:
                ch.close()
            except Exception:
                pass

    def _paramiko_upload(self, local_path, remote_path, timeout=600):
        total = os.path.getsize(local_path)
        ch = self.transport.open_session()
        sent = 0
        t0 = time.time()
        last = 0.0
        try:
            ch.settimeout(timeout)
            ch.exec_command("cat > %s" % shq(remote_path))
            with open(local_path, "rb") as f:
                while True:
                    buf = f.read(32768)
                    if not buf:
                        break
                    ch.sendall(buf)
                    sent += len(buf)
                    now = time.time()
                    if now - last >= 0.5 or sent >= total:
                        last = now
                        speed = sent / max(0.001, now - t0) / 1024.0 / 1024.0
                        progress("  上传 %s: %5.1f%%  (%s / %s, %.2f MiB/s)" % (
                            os.path.basename(local_path),
                            sent * 100.0 / max(1, total),
                            human_size(sent), human_size(total), speed))
            ch.shutdown_write()
            while True:
                if ch.recv_ready():
                    ch.recv(65536)
                    continue
                if ch.recv_stderr_ready():
                    ch.recv_stderr(65536)
                    continue
                if ch.exit_status_ready():
                    break
                time.sleep(0.05)
            try:
                ch.recv_exit_status()
            except Exception:
                pass
        except Exception as e:
            raise FlashError("SSH 上传失败: %s" % e)
        finally:
            progress_end()
            try:
                ch.close()
            except Exception:
                pass

    def close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None
        try:
            if self.sess:
                self.sess.disconnect()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sess = None
        self.sock = None

    def _session(self):
        if not self.sess:
            raise FlashError("SSH 未连接")
        return self.sess

    def exec_cmd(self, cmd, timeout=60):
        """执行命令，返回 (exit_status, 输出文本)"""
        if self.transport is not None:
            return self._paramiko_exec(cmd, timeout)
        sess = self._session()
        sess.set_timeout(int(timeout * 1000))
        ch = None
        try:
            ch = sess.open_session()
            try:
                ch.handle_extended_data2(2)   # MERGE：stderr 合并到 stdout
            except Exception:
                pass
            ch.execute(cmd)
            data = b""
            while True:
                try:
                    n, buf = ch.read(32768)
                except Exception:
                    break
                if n <= 0:
                    break
                data += buf
            try:
                status = ch.get_exit_status()
            except Exception:
                status = -1
            return status, data.decode("utf-8", "replace")
        except FlashError:
            raise
        except Exception as e:
            return -1, "SSH 执行失败: %s" % e
        finally:
            if ch is not None:
                try:
                    ch.close()
                    ch.wait_closed()
                except Exception:
                    pass

    def exec_ok(self, cmd, timeout=60):
        st, out = self.exec_cmd(cmd, timeout)
        return st == 0, out

    def upload(self, local_path, remote_path, timeout=600):
        """通过 SSH 标准输入上传文件（不依赖 scp/sftp）"""
        if self.transport is not None:
            return self._paramiko_upload(local_path, remote_path, timeout)
        sess = self._session()
        sess.set_timeout(int(timeout * 1000))
        total = os.path.getsize(local_path)
        cmd = "cat > %s" % shq(remote_path)
        ch = sess.open_session()
        sent = 0
        t0 = time.time()
        last = 0.0
        try:
            try:
                ch.handle_extended_data2(2)
            except Exception:
                pass
            ch.execute(cmd)
            with open(local_path, "rb") as f:
                while True:
                    buf = f.read(32768)
                    if not buf:
                        break
                    off = 0
                    while off < len(buf):
                        rc, n = ch.write(buf[off:])
                        if n is None or n <= 0:
                            raise FlashError("SSH 上传中断（写入失败）")
                        off += n
                    sent += len(buf)
                    now = time.time()
                    if now - last >= 0.5 or sent >= total:
                        last = now
                        speed = sent / max(0.001, now - t0) / 1024.0 / 1024.0
                        progress("  上传 %s: %5.1f%%  (%s / %s, %.2f MiB/s)" % (
                            os.path.basename(local_path),
                            sent * 100.0 / max(1, total),
                            human_size(sent), human_size(total), speed))
            ch.send_eof()
            while True:
                try:
                    n, buf = ch.read(4096)
                except Exception:
                    break
                if n <= 0:
                    break
            try:
                ch.get_exit_status()
            except Exception:
                pass
        except FlashError:
            raise
        except Exception as e:
            raise FlashError("SSH 上传失败: %s" % e)
        finally:
            progress_end()
            try:
                ch.close()
                ch.wait_closed()
            except Exception:
                pass

    def md5_remote(self, remote_path, timeout=60):
        st, out = self.exec_cmd("md5sum %s" % shq(remote_path), timeout)
        m = re.search(r"\b([0-9a-fA-F]{32})\b", out)
        return m.group(1).lower() if m else None


def ssh_try(ip, user, password, bind_ip=None, timeout=20):
    c = SSHClient(ip, user, password, bind_ip, 22, timeout)
    try:
        c.connect()
        return c
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        return None


# ============================================================================
#  Telnet（过渡固件登录用）
# ============================================================================

class TelnetShell(object):
    """极简 telnet 客户端（只用 TCP + IAC 协商，兼容 Python 3.8 且支持绑定源地址）"""

    IAC = 255

    def __init__(self, ip, bind_ip=None, timeout=10):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            if bind_ip:
                self.sock.bind((bind_ip, 0))
            self.sock.connect((ip, 23))
        except Exception:
            try:
                self.sock.close()
            except Exception:
                pass
            raise
        self.buf = b""
        self.rawbuf = b""
        self.timeout = timeout

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_raw(self, data):
        try:
            self.sock.sendall(data)
        except Exception:
            pass

    def _feed(self, data):
        """剔除 IAC 选项协商，把纯数据追加到 self.buf"""
        d = self.rawbuf + data
        out = bytearray()
        i, n = 0, len(d)
        while i < n:
            b = d[i]
            if b != self.IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:
                break
            cmd = d[i + 1]
            if cmd == self.IAC:            # 转义的 0xFF
                out.append(self.IAC)
                i += 2
                continue
            if cmd == 250:                 # SB ... IAC SE
                j = i + 2
                found = -1
                while j + 1 < n:
                    if d[j] == self.IAC and d[j + 1] == 240:
                        found = j
                        break
                    j += 1
                if found < 0:
                    break
                i = found + 2
                continue
            if cmd in (251, 252, 253, 254):   # WILL/WONT/DO/DONT
                if i + 2 >= n:
                    break
                opt = d[i + 2]
                if cmd in (251, 252):
                    self._send_raw(bytes([self.IAC, 254, opt]))   # DONT
                else:
                    self._send_raw(bytes([self.IAC, 252, opt]))   # WONT
                i += 3
                continue
            i += 2
        self.rawbuf = d[i:]
        if out:
            self.buf += bytes(out)

    def _recv(self, timeout):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return False
        except Exception:
            return False
        if not data:
            return False
        self._feed(data)
        return True

    def expect(self, patterns, timeout=10):
        """等待正则（bytes），返回 (index, match, 含匹配的已读数据)"""
        t0 = time.time()
        while True:
            for idx, p in enumerate(patterns):
                m = re.search(p, self.buf)
                if m:
                    data = self.buf[:m.end()]
                    self.buf = self.buf[m.end():]
                    return idx, m, data
            remain = timeout - (time.time() - t0)
            if remain <= 0:
                data = self.buf
                self.buf = b""
                return -1, None, data
            self._recv(min(1.0, max(0.2, remain)))

    def run(self, cmd, timeout=20):
        # 标记拆成 __AX6"RC"__ 发送：shell 拼接后是 __AX6RC__，
        # 而回显的命令行里不会出现完整标记，避免误匹配。
        self._send_raw(cmd.encode("utf-8") + b'; echo __AX6"RC"__$?\n')
        pat = re.compile(rb"__AX6RC__(\d+)")
        idx, m, data = self.expect([pat], timeout)
        if not m:
            return -1, self._clean(data.decode("utf-8", "replace"))
        rc = int(m.group(1))
        out = data[:m.start()].decode("utf-8", "replace")
        return rc, self._clean(out)

    @staticmethod
    def _clean(text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
        # 去掉最前面的提示符
        text = re.sub(r"^[^\n]*?[#\$] ", "", text, count=1)
        lines = text.split("\n")
        # 去掉回显的命令行（其中包含拆开的标记字面量）
        if lines and '__AX6"RC"__' in lines[0]:
            lines = lines[1:]
        return "\n".join(lines).strip()

    def wait_ready(self, timeout=10):
        # 过渡固件 telnetd 用 /bin/login.sh 直接给 root shell
        idx, m, data = self.expect(
            [re.compile(rb"[#\$] "), re.compile(rb"(?i)login:")], timeout)
        if idx < 0:
            return False
        if idx == 1:
            self._send_raw(b"\n")
            idx2, m2, data2 = self.expect([re.compile(rb"[#\$] ")], 3)
            return idx2 == 0
        return True


def telnet_try(ip, bind_ip=None, timeout=8):
    try:
        t = TelnetShell(ip, bind_ip, timeout)
        if t.wait_ready(8):
            return t
        t.close()
    except Exception:
        pass
    return None


# ============================================================================
#  配置 / 状态
# ============================================================================

DEFAULT_CFG = {
    "paths": {
        "stock_firmware": "../miwifi_ra69_firmware_45a77_1.0.18.bin",
        "transition_firmware": "../xiaomimtd12.bin",
        "mibib": "../ax6-uboot-mibib.bin",
        "uboot": "../uboot-ax6.bin",
        "immortalwrt_factory": "../immortalwrt-25.12.2-qualcommax-ipq807x-redmi_ax6-stock-squashfs-factory.ubi",
    },
    "network": {
        "stock_ip": "192.168.31.1",
        "router_ip": "192.168.1.1",
        "pc_ip": "192.168.1.10",
        "netmask": "255.255.255.0",
    },
    "credentials": {
        "stock_ssh_user": "root",
        "stock_ssh_password": "root",
        "stock_web_password": "",
        "new_root_password": "root",
    },
    "options": {
        "skip_downgrade_if_version_matches": "yes",
        "install_argon": "yes",
        "install_openclash": "yes",
        "install_smartdns": "yes",
        "install_zh_cn": "yes",
        "confirm_bootloader": "yes",
        "remove_temp_ip": "yes",
        "reboot_timeout": "600",
    },
}


def _cfg_bool(v):
    return str(v).strip().lower() in ("1", "yes", "true", "on", "y", "是")


class Cfg(object):
    def __init__(self, path=None):
        self.cp = configparser.ConfigParser()
        self.cp.optionxform = str
        if path and os.path.exists(path):
            try:
                self.cp.read(path, encoding="utf-8")
            except Exception:
                pass

    def get(self, sec, key):
        try:
            return self.cp.get(sec, key).strip()
        except Exception:
            return DEFAULT_CFG.get(sec, {}).get(key, "")

    def get_bool(self, sec, key):
        return _cfg_bool(self.get(sec, key))

    def get_int(self, sec, key):
        try:
            return int(self.get(sec, key))
        except Exception:
            return 0

    def path(self, key):
        p = self.get("paths", key)
        if not os.path.isabs(p):
            p = os.path.normpath(os.path.join(BASE_DIR, p))
        return p


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================================
#  小米 HDR1 固件解析
# ============================================================================

def parse_hdr1(path):
    with open(path, "rb") as f:
        data = f.read(0x1000)
    if data[:4] != b"HDR1":
        raise FlashError("不是 HDR1 格式的小米固件：%s" % path)
    model_id = int.from_bytes(data[14:16], "little")
    sections = []
    for i in range(8):
        off = int.from_bytes(data[0x10 + i * 4:0x14 + i * 4], "little")
        if not off:
            continue
        if data[off:off + 4] != b"\xBE\xBA\x00\x00":
            raise FlashError("HDR1 段头损坏（offset 0x%X）" % off)
        size = int.from_bytes(data[off + 8:off + 12], "little")
        name = data[off + 16:off + 80].split(b"\x00")[0].decode("latin-1")
        sections.append({"name": name, "data_offset": off + 0x30, "size": size})
    return model_id, sections


def extract_hdr_section(bin_path, section_name, out_path):
    """把 HDR1 中的某个段原样抽取到 out_path，返回其 md5"""
    model_id, sections = parse_hdr1(bin_path)
    sec = None
    for s in sections:
        if s["name"] == section_name:
            sec = s
            break
    if not sec:
        raise FlashError("固件中找不到段 \"%s\"" % section_name)
    h = hashlib.md5()
    with open(bin_path, "rb") as fin:
        fin.seek(sec["data_offset"])
        left = sec["size"]
        with open(out_path, "wb") as fout:
            while left > 0:
                buf = fin.read(min(1024 * 1024, left))
                if not buf:
                    raise FlashError("固件读取失败（段数据不完整）")
                fout.write(buf)
                h.update(buf)
                left -= len(buf)
    return h.hexdigest(), sec["size"]


def get_stock_version_info(bin_path):
    """读取 HDR1 内 xiaoqiang_version 段里的 ROM/HARDWARE 信息"""
    tmp = os.path.join(CACHE_DIR, "_stock_version.txt")
    try:
        extract_hdr_section(bin_path, "xiaoqiang_version", tmp)
        with open(tmp, "r", encoding="latin-1") as f:
            txt = f.read()
        rom = re.search(r"option ROM '(.*?)'", txt)
        hw = re.search(r"option HARDWARE '(.*?)'", txt)
        return (rom.group(1) if rom else None,
                hw.group(1) if hw else None)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ============================================================================
#  设备端信息解析
# ============================================================================

def parse_proc_mtd(text):
    parts = {}
    for line in text.splitlines():
        m = re.match(r'\s*mtd(\d+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+"(.*)"', line)
        if not m:
            continue
        num = int(m.group(1))
        parts[m.group(4)] = {
            "num": num,
            "size": int(m.group(2), 16),
            "erase": int(m.group(3), 16),
            "name": m.group(4),
        }
    return parts


def get_rootfs_index(cmdline, parts):
    """返回当前启动的 rootfs 序号：0 = rootfs, 1 = rootfs_1"""
    m = re.search(r"ubi\.mtd=(\S+)", cmdline or "")
    if m:
        name = m.group(1)
        if name == "rootfs":
            return 0
        if name == "rootfs_1":
            return 1
    # 退化方案：对比 mtd 名
    p0 = parts.get("rootfs")
    p1 = parts.get("rootfs_1")
    if p0 and p1:
        m2 = re.search(r"root=mtd:(\S+)", cmdline or "")
        if m2:
            return 0
    return None


def part_name(idx):
    return "rootfs" if idx == 0 else "rootfs_1"


def read_stock_info(ssh):
    info = {}
    st, out = ssh.exec_cmd("cat /etc/xiaoqiang_version 2>/dev/null || "
                           "cat /usr/share/xiaoqiang/xiaoqiang_version 2>/dev/null", 20)
    txt = out or ""
    m = re.search(r"option ROM '(.*?)'", txt)
    info["version"] = m.group(1) if m else None
    m = re.search(r"option HARDWARE '(.*?)'", txt)
    info["hardware"] = m.group(1) if m else None

    st, out = ssh.exec_cmd("nvram get model 2>/dev/null", 20)
    info["nvram_model"] = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""

    st, out = ssh.exec_cmd("cat /proc/mtd", 20)
    info["parts"] = parse_proc_mtd(out or "")

    st, out = ssh.exec_cmd("cat /proc/cmdline", 20)
    info["cmdline"] = (out or "").strip()
    info["rootfs_idx"] = get_rootfs_index(info["cmdline"], info["parts"])
    return info


def check_stock_device(info):
    hw = (info.get("hardware") or "").upper()
    nm = (info.get("nvram_model") or "").upper()
    if hw and "RA69" not in hw:
        raise FlashError("设备硬件为 %s，不是 RA69（AX6）" % hw)
    if nm and "RA69" not in nm:
        raise FlashError("NVRAM 中设备型号为 %s，不是 RA69（AX6）" % nm)
    if info.get("rootfs_idx") is None:
        raise FlashError("无法判断当前启动的分区（/proc/cmdline: %s）" % info.get("cmdline"))
    for name in ("rootfs", "rootfs_1"):
        p = info["parts"].get(name)
        if not p:
            raise FlashError("分区表中缺少 \"%s\"" % name)
        if p["size"] < ROOTFS_PART_SIZE:
            raise FlashError("分区 \"%s\" 大小异常（0x%X）" % (name, p["size"]))


# ============================================================================
#  xmir-patcher 解锁流程（connect.py）
# ============================================================================

def python_exe():
    """优先使用仓库自带的 Python（含 ssh2 等依赖）"""
    cand = os.path.join(REPO_DIR, "python", "python.exe")
    if IS_WIN and os.path.isfile(cand):
        return cand
    return sys.executable


def run_connect_py(ctx):
    """调用仓库自带的 connect.py 解锁 SSH；需要路由器后台密码"""
    web_pw = ctx.cfg.get("credentials", "stock_web_password")
    if not web_pw:
        web_pw = ctx.web_password
    if not web_pw:
        log("")
        log("需要路由器后台（Web）管理密码来执行自动解锁 SSH。")
        log("如果不想现在输入，可先手动运行 run.bat -> 2（Connect to device）。")
        web_pw = ask_text("请输入路由器后台管理密码（输入后不会保存到文件）: ", "")
        if not web_pw:
            raise FlashError("没有提供后台密码，无法自动解锁 SSH")
        ctx.web_password = web_pw

    script = os.path.join(REPO_DIR, "connect.py")
    if not os.path.exists(script):
        raise FlashError("找不到 %s\n"
                         "自动解锁 SSH 依赖 XMiR-Patcher 的 connect.py。可以：\n"
                         "  1) 先用 XMiR-Patcher 的 run.bat → 2 手动解锁 SSH，再运行本脚本；\n"
                         "  2) 或把本目录放进 xmir-patcher 目录内再运行（推荐）；\n"
                         "  3) 若设备 SSH 已经开启（root/root 能登录），无需解锁，"
                         "直接确认型号/版本后继续。" % script)

    log("调用 xmir-patcher 的 connect.py 解锁 SSH（这可能需要 1-2 分钟）...")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.Popen(
            [python_exe(), "connect.py", ctx.stock_ip],
            cwd=REPO_DIR, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        raise FlashError("无法启动 connect.py: %s" % e)

    try:
        for _ in range(4):
            try:
                p.stdin.write(web_pw + "\n")
                p.stdin.flush()
            except Exception:
                break
        for line in p.stdout:
            line = line.rstrip("\r\n")
            if line:
                log("   [xmir] " + line)
        p.wait()
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
    return p.returncode


# ============================================================================
#  刷机上下文
# ============================================================================

class Ctx(object):
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.state = load_state()
        self.stock_ip = cfg.get("network", "stock_ip")
        self.router_ip = cfg.get("network", "router_ip")
        self.pc_ip = cfg.get("network", "pc_ip")
        self.netmask = cfg.get("network", "netmask")
        self.adapter = None
        self.bind_ip = None
        self.stock_bind = None
        self.added_ip = False
        self.dhcp_was_enabled = self.state.get("dhcp_was_enabled")
        self.uboot_ready = False
        self.web_password = ""
        self.new_password = cfg.get("credentials", "new_root_password") or "root"
        self.stock_user = cfg.get("credentials", "stock_ssh_user") or "root"
        self.stock_pass = cfg.get("credentials", "stock_ssh_password") or "root"

        self.path_stock = cfg.path("stock_firmware")
        self.path_trans = cfg.path("transition_firmware")
        self.path_mibib = cfg.path("mibib")
        self.path_uboot = cfg.path("uboot")
        self.path_imm = cfg.path("immortalwrt_factory")

        self.stock_ubi = os.path.join(CACHE_DIR, "root_1.0.18.ubi")
        self.stock_ubi_md5 = None
        self.reboot_timeout = cfg.get_int("options", "reboot_timeout") or 600

    # ---------------- 网卡准备 ----------------

    def prepare_adapter(self, need_router_ip):
        ad = pick_router_adapter(self.stock_ip, self.router_ip, self.state)
        if not ad:
            raise FlashError("找不到可用的网卡，请确认已插好网线")
        self.adapter = ad["name"]
        self.state["adapter"] = self.adapter
        save_state(self.state)
        log("使用网卡: %s  (当前地址: %s)" % (ad["name"], ", ".join(ad["ips"]) or "无"))

        # 原厂固件只通过这块网卡访问，避免多网卡时误连到别的 192.168.31.1
        self.stock_bind = None
        for ip in ad["ips"]:
            if _subnet_prefix(ip) == _subnet_prefix(self.stock_ip):
                self.stock_bind = ip
                break
        if ad["iftype"] == 71:
            log("")
            log("!! 警告：当前使用的是无线网卡。")
            log("!! 第 2 步之后（U-Boot 阶段）必须使用网线连接 AX6 的 LAN 口！")
            log("")

        if not _has_real_ip(ad) and adapter_dhcp_enabled(ad["name"]) is False:
            log("")
            log("!! 注意：网卡 \"%s\" 的 DHCP 已被关闭，目前只有 169.254 地址。" % ad["name"])
            log("!! 如果原厂固件在 %s，请先以管理员身份执行：" % self.stock_ip)
            log("!!   netsh interface ipv4 set address name=\"%s\" source=dhcp" % ad["name"])
            log("")

        if not need_router_ip:
            return

        # 已经在这个网卡上有一个 192.168.1.x 地址？直接用它
        for ip in ad["ips"]:
            if ip.startswith("192.168.1."):
                self.bind_ip = ip
                if ip == self.pc_ip and self.state.get("added_ip"):
                    self.added_ip = True
                    self.dhcp_was_enabled = self.state.get("dhcp_was_enabled")
                log("网卡已有 192.168.1.x 地址，使用: %s" % ip)
                return

        if IS_WIN and not is_admin():
            raise FlashError("需要管理员权限才能添加临时 IP 地址，请右键以管理员身份运行")

        log("在网卡 \"%s\" 上添加临时地址 %s/%s ..." % (self.adapter, self.pc_ip, self.netmask))
        rc, out, was_dhcp = netsh_add_ip(self.adapter, self.pc_ip, self.netmask)
        if not adapter_has_ip(self.adapter, self.pc_ip):
            raise FlashError("添加临时 IP 失败：%s\n%s" % (self.pc_ip, out))
        if was_dhcp:
            log("  网卡原本使用 DHCP，切换为静态地址后原 DHCP 地址会暂时消失")
            log("  （这是 Windows 的行为；结束时脚本会自动恢复 DHCP）")
        self.bind_ip = self.pc_ip
        self.added_ip = True
        self.dhcp_was_enabled = was_dhcp
        st = self.state
        st["adapter"] = self.adapter
        st["bind_ip"] = self.pc_ip
        st["added_ip"] = True
        st["dhcp_was_enabled"] = was_dhcp
        save_state(st)
        sleep_s(1)

    def ensure_router_bind(self):
        """确保已有访问 192.168.1.1 的源地址（只在真正需要时添加临时 IP）"""
        if self.bind_ip is None:
            self.prepare_adapter(need_router_ip=True)

    def cleanup_adapter(self):
        if not self.added_ip:
            return
        if not self.cfg.get_bool("options", "remove_temp_ip"):
            log("保留临时 IP %s（config.ini: remove_temp_ip = no）" % self.pc_ip)
            return
        try:
            log("移除临时地址 %s ..." % self.pc_ip)
            netsh_del_ip(self.adapter, self.pc_ip, self.dhcp_was_enabled)
        except Exception:
            pass
        st = self.state
        st["added_ip"] = False
        st["dhcp_was_enabled"] = None
        save_state(st)
        self.added_ip = False
        self.dhcp_was_enabled = None


# ============================================================================
#  预检查
# ============================================================================

def preflight(ctx):
    log_section("预检查")
    errors = []

    def need(path, desc):
        if not os.path.exists(path):
            errors.append("缺少%s：%s" % (desc, path))
            return False
        return True

    ok = True
    ok &= need(ctx.path_stock, "原厂固件")
    ok &= need(ctx.path_trans, "过渡固件")
    ok &= need(ctx.path_mibib, "MIBIB 文件")
    ok &= need(ctx.path_uboot, "U-Boot 文件")
    ok &= need(ctx.path_imm, "ImmortalWrt factory 固件")
    if not ok:
        raise FlashError("文件缺失：\n  " + "\n  ".join(errors))

    # 原厂固件
    try:
        mid, secs = parse_hdr1(ctx.path_stock)
        names = [s["name"] for s in secs]
        size = os.path.getsize(ctx.path_stock)
        log_note("原厂固件: %s (%s)" % (os.path.basename(ctx.path_stock), human_size(size)))
        log_note("  HDR1 model_id = %d %s" % (mid, "(RA69)" if mid == RA69_HDR_MODEL_ID else "(未知!)"))
        if mid != RA69_HDR_MODEL_ID:
            errors.append("原厂固件 model_id=%d，不是 RA69 的固件" % mid)
        if "root.ubi" not in names:
            errors.append("原厂固件中没有 root.ubi 段")
        else:
            sec = [s for s in secs if s["name"] == "root.ubi"][0]
            log_note("  root.ubi 段大小: %s" % human_size(sec["size"]))
            if sec["size"] > ROOTFS_PART_SIZE:
                errors.append("root.ubi 大于 rootfs 分区（0x%X）" % ROOTFS_PART_SIZE)
        ver, hw = get_stock_version_info(ctx.path_stock)
        log_note("  固件版本: %s   硬件: %s" % (ver, hw))
        if hw and hw.upper() != "RA69":
            errors.append("原厂固件硬件为 %s，不是 RA69" % hw)
    except FlashError as e:
        errors.append(str(e))

    # 过渡固件
    try:
        sz = os.path.getsize(ctx.path_trans)
        magic = read_head(ctx.path_trans, 8)
        log_note("过渡固件: %s (%s, magic=%r)" % (
            os.path.basename(ctx.path_trans), human_size(sz), magic[:4]))
        if magic[:4] != b"UBI#":
            errors.append("过渡固件不是 UBI 格式")
        if sz != ROOTFS_PART_SIZE:
            log_note("  注意：过渡固件大小 0x%X，分区大小 0x%X" % (sz, ROOTFS_PART_SIZE))
            if sz > ROOTFS_PART_SIZE:
                errors.append("过渡固件大于 rootfs 分区")
    except Exception as e:
        errors.append("过渡固件检查失败: %s" % e)

    # MIBIB
    try:
        sz = os.path.getsize(ctx.path_mibib)
        log_note("MIBIB: %s (%s)" % (os.path.basename(ctx.path_mibib), human_size(sz)))
        if sz != XD:
            errors.append("MIBIB 大小应为 1 MiB，实际 0x%X" % sz)
    except Exception as e:
        errors.append("MIBIB 检查失败: %s" % e)

    # U-Boot
    try:
        sz = os.path.getsize(ctx.path_uboot)
        log_note("U-Boot: %s (%s)" % (os.path.basename(ctx.path_uboot), human_size(sz)))
        if sz <= 0 or sz > XD:
            errors.append("U-Boot 大小异常: 0x%X" % sz)
    except Exception as e:
        errors.append("U-Boot 检查失败: %s" % e)

    # ImmortalWrt
    try:
        sz = os.path.getsize(ctx.path_imm)
        magic = read_head(ctx.path_imm, 8)
        log_note("ImmortalWrt: %s (%s)" % (os.path.basename(ctx.path_imm), human_size(sz)))
        if magic[:4] != b"UBI#":
            errors.append("ImmortalWrt 不是 UBI 格式")
        base = os.path.basename(ctx.path_imm).lower()
        if "redmi_ax6" not in base:
            log_note("  警告：文件名中不含 redmi_ax6，请确认固件型号")
        if "stock" not in base:
            log_note("  警告：文件名中不含 stock —— AX6 必须使用 stock 版本固件！")
            log_note("         (从 U-Boot 刷入的 factory 必须是 ...redmi_ax6-stock...factory.ubi)")
    except Exception as e:
        errors.append("ImmortalWrt 检查失败: %s" % e)

    # root.ubi 抽取（缓存）
    try:
        ctx.stock_ubi_md5 = extract_stock_root_ubi(ctx)
        log_note("已准备 root.ubi: %s (md5 %s)" % (ctx.stock_ubi, ctx.stock_ubi_md5))
    except Exception as e:
        errors.append("抽取 root.ubi 失败: %s" % e)

    log_note("Python: %s" % sys.version.split()[0])
    log_note("管理员权限: %s" % ("是" if is_admin() else "否"))

    if errors:
        log("")
        for e in errors:
            log("  !! " + e)
        raise FlashError("预检查未通过")

    log("")
    log("预检查通过。")
    return True


def extract_stock_root_ubi(ctx):
    src = ctx.path_stock
    out = ctx.stock_ubi
    meta_path = out + ".json"
    try:
        st = os.stat(src)
        meta = {
            "src": src,
            "src_size": st.st_size,
            "src_mtime": int(st.st_mtime),
        }
        if os.path.exists(out) and os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("src_size") == meta["src_size"] and old.get("src_mtime") == meta["src_mtime"]:
                with open(out, "rb") as f:
                    if f.read(4) == b"UBI#":
                        return md5_file(out)
        md5, size = extract_hdr_section(src, "root.ubi", out)
        meta["md5"] = md5
        meta["size"] = size
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return md5
    except FlashError:
        raise
    except Exception as e:
        raise FlashError(str(e))


# ============================================================================
#  SSH 获取（原厂）
# ============================================================================

def try_stock_ssh(ctx, timeout=20):
    candidates = []
    for pw in (ctx.stock_pass, "root", ctx.web_password):
        if pw is not None and pw not in candidates:
            candidates.append(pw)
    for pw in candidates:
        c = ssh_try(ctx.stock_ip, ctx.stock_user, pw, ctx.stock_bind, timeout)
        if c:
            ctx.stock_pass = pw
            return c
    return None


def ensure_stock_ssh(ctx):
    log("尝试用 SSH 连接原厂固件 %s ..." % ctx.stock_ip)
    c = try_stock_ssh(ctx, 15)
    if c:
        log("SSH 已连接（密码: %s）" % ("<空>" if ctx.stock_pass == "" else ctx.stock_pass))
        return c

    log("SSH 不可用，尝试通过 xmir-patcher 解锁 ...")
    rc = run_connect_py(ctx)
    for i in range(20):
        c = try_stock_ssh(ctx, 10)
        if c:
            log("SSH 解锁成功")
            return c
        sleep_s(3)
    raise FlashError("SSH 解锁失败（connect.py 返回 %s）。请检查后台密码是否正确，"
                     "或手动运行 run.bat 选择 2 后重试。" % rc)


# ============================================================================
#  第 1 步：降级到 1.0.18
# ============================================================================

def wait_device_down(ip, bind_ip=None, timeout=90):
    """等待设备真正下线，避免把重启前的残留响应误判为“已启动”"""

    def alive():
        if http_probe(ip, bind_ip, 2.0):
            return True
        if tcp_open(ip, 22, 1.0, bind_ip):
            return True
        return False

    t0 = time.time()
    while time.time() - t0 < timeout:
        if not alive():
            sleep_s(2)
            if not alive():
                return True
        sleep_s(2)
    return False


def wait_stock_online(ctx, timeout=None):
    timeout = timeout or ctx.reboot_timeout

    def fn():
        if http_probe(ctx.stock_ip, ctx.stock_bind, 2.0):
            if tcp_open(ctx.stock_ip, 22, 1.0, ctx.stock_bind):
                return True
            return "web"
        return None

    r = wait_until("原厂固件重启（%s）" % ctx.stock_ip, fn, timeout, 3)
    if not r:
        return False
    return True


def stage1(ctx):
    log_section("第 1 步 / 4：原厂固件降级到 1.0.18")

    if not wait_stock_online(ctx, 120):
        raise FlashError("无法连接原厂固件 %s，请确认电脑已连接到 AX6 的 LAN 口" % ctx.stock_ip)

    ssh = ensure_stock_ssh(ctx)
    try:
        info = read_stock_info(ssh)
        check_stock_device(info)
        ver = info.get("version")
        log("当前固件版本: %s，硬件: %s，当前启动分区: %s (mtd%d)" % (
            ver, info.get("hardware"), part_name(info["rootfs_idx"]),
            info["parts"][part_name(info["rootfs_idx"])]["num"]))

        target_ver, target_hw = get_stock_version_info(ctx.path_stock)
        log("目标固件版本: %s" % target_ver)

        if (ctx.cfg.get_bool("options", "skip_downgrade_if_version_matches")
                and ver and target_ver and ver == target_ver):
            log("")
            log("当前已经是 %s，跳过降级。" % target_ver)
            return

        active = info["rootfs_idx"]
        tgt = 1 - active
        tname = part_name(tgt)
        part = info["parts"][tname]
        img_size = os.path.getsize(ctx.stock_ubi)
        if img_size > part["size"]:
            raise FlashError("root.ubi (%s) 大于分区 %s (%s)" % (
                human_size(img_size), tname, human_size(part["size"])))

        log("")
        log("将把 1.0.18 写入非活动分区 \"%s\" (mtd%d, 当前启动的是 \"%s\")" % (
            tname, part["num"], part_name(active)))

        remote = "/tmp/root_1.0.18.ubi"
        log("上传 root.ubi (%s) ..." % human_size(img_size))
        ssh.upload(ctx.stock_ubi, remote, timeout=900)
        rmd5 = ssh.md5_remote(remote)
        if rmd5 != ctx.stock_ubi_md5:
            raise FlashError("上传校验失败：远端 %s，本地 %s" % (rmd5, ctx.stock_ubi_md5))
        log("上传完成，校验通过 (%s)" % rmd5)

        cmd = 'mtd -e "%s" write "%s" "%s"' % (tname, remote, tname)
        log("写入分区: %s" % cmd)
        ssh.exec_cmd(cmd, timeout=300)

        log("校验分区数据 ...")
        md5_part = mtd_md5(ssh, part["num"], img_size, timeout=120)
        if md5_part != ctx.stock_ubi_md5:
            raise FlashError("分区 \"%s\" 校验失败：%s != %s" % (tname, md5_part, ctx.stock_ubi_md5))
        log("分区校验通过 (%s)" % md5_part)

        set_nvram_flags(ssh, tgt)
        log("已设置从 \"%s\" 启动" % tname)
        ssh.exec_cmd("rm -f %s" % shq(remote), 20)

        log("")
        log("重启进入 1.0.18 ...")
        reboot_ssh(ssh)
    finally:
        try:
            ssh.close()
        except Exception:
            pass

    wait_device_down(ctx.stock_ip, ctx.stock_bind, 90)
    if not wait_stock_online(ctx, ctx.reboot_timeout):
        raise FlashError("降级后等待设备重启超时")
    log("设备已重新上线，准备下一步。")

    # 降级后 overlay 是全新的，SSH 可能被关闭 —— 这里顺手重新解锁，
    # 这样第 2 步可以直接使用。
    ssh2c = try_stock_ssh(ctx, 10)
    if ssh2c:
        ssh2c.close()
        log("SSH 仍然可用。")
    else:
        log("降级后 SSH 已关闭，重新执行初始化（解锁 SSH）...")
        ensure_stock_ssh(ctx).close()


def mtd_md5(ssh, mtd_num, size, timeout=120):
    blocks = size // 4096
    # 与 xmir-patcher 相同：只校验完整的 4096 字节块
    cmd = "dd if=/dev/mtd%d bs=4096 count=%d 2>/dev/null | md5sum" % (mtd_num, blocks)
    st, out = ssh.exec_cmd(cmd, timeout)
    m = re.search(r"\b([0-9a-fA-F]{32})\b", out or "")
    return m.group(1).lower() if m else None


def set_nvram_flags(ssh, idx):
    cmds = [
        "nvram set flag_ota_reboot=0",
        "nvram set flag_boot_success=1",
        "nvram set flag_last_success=%d" % idx,
        "nvram set flag_try_sys1_failed=0",
        "nvram set flag_try_sys2_failed=0",
        "nvram set flag_boot_rootfs=%d" % idx,
        "nvram set boot_wait=on",
        "nvram set uart_en=1",
        "nvram set telnet_en=1",
        "nvram set ssh_en=1",
        "nvram commit",
    ]
    ssh.exec_cmd(";".join(cmds), 40)


def reboot_ssh(ssh):
    try:
        ssh.exec_cmd("sync; reboot -f", 10)
    except Exception:
        pass
    try:
        ssh.close()
    except Exception:
        pass
    sleep_s(5)


# ============================================================================
#  第 2 步 A：刷过渡固件并重启
# ============================================================================

def stage2_transition(ctx):
    log_section("第 2 步 / 4：刷入过渡固件")

    if not wait_stock_online(ctx, 120):
        log("原厂固件暂时无响应，仍尝试 SSH ...")
    ssh = ensure_stock_ssh(ctx)
    try:
        info = read_stock_info(ssh)
        check_stock_device(info)
        active = info["rootfs_idx"]
        tgt = 1 - active
        tname = part_name(tgt)
        part = info["parts"][tname]
        img_size = os.path.getsize(ctx.path_trans)
        if img_size > part["size"]:
            raise FlashError("过渡固件大于分区 %s" % tname)

        log("当前启动: %s；将写入过渡固件到 \"%s\" (mtd%d, %s)" % (
            part_name(active), tname, part["num"], human_size(part["size"])))

        remote = "/tmp/xiaomimtd12.bin"
        log("上传过渡固件 (%s) ..." % human_size(img_size))
        local_md5 = md5_file(ctx.path_trans)
        ssh.upload(ctx.path_trans, remote, timeout=1200)
        rmd5 = ssh.md5_remote(remote)
        if rmd5 != local_md5:
            raise FlashError("上传校验失败：远端 %s，本地 %s" % (rmd5, local_md5))
        log("上传完成，校验通过")

        cmd = 'mtd -e "%s" write "%s" "%s"' % (tname, remote, tname)
        log("写入分区: %s" % cmd)
        ssh.exec_cmd(cmd, timeout=300)

        log("校验分区数据（这个分区较大，可能需要 1 分钟）...")
        md5_part = mtd_md5(ssh, part["num"], img_size, timeout=300)
        if md5_part != local_md5:
            raise FlashError("分区校验失败：%s != %s" % (md5_part, local_md5))
        log("分区校验通过")
        ssh.exec_cmd("rm -f %s" % shq(remote), 20)

        set_nvram_flags(ssh, tgt)
        log("已设置从 \"%s\" 启动" % tname)

        log("")
        log("重启进入过渡系统（192.168.1.1）...")
        reboot_ssh(ssh)
    finally:
        try:
            ssh.close()
        except Exception:
            pass

    # 设备重启后再加 192.168.1.10：原厂阶段不能动 DHCP 网卡的地址配置
    wait_device_down(ctx.stock_ip, None, 60)
    ctx.ensure_router_bind()

    log("等待过渡系统启动 ...")
    sh = wait_transition_shell(ctx, 420)
    if not sh:
        raise FlashError("过渡系统没有启动成功（等待 192.168.1.1 的 SSH/Telnet 超时）")
    try:
        sh[1].close()
    except Exception:
        pass
    log("过渡系统已就绪。")
    return True


def wait_transition_shell(ctx, timeout):
    def fn():
        t = telnet_try(ctx.router_ip, ctx.bind_ip, 5)
        if t:
            return ("telnet", t)
        c = ssh_try(ctx.router_ip, "root", ctx.new_password, ctx.bind_ip, 8)
        if c:
            return ("ssh", c)
        c = ssh_try(ctx.router_ip, "root", "root", ctx.bind_ip, 8)
        if c:
            return ("ssh", c)
        return None

    r = wait_until("过渡系统 (telnet/ssh %s)" % ctx.router_ip, fn, timeout, 4)
    return r


# ============================================================================
#  第 2 步 B：设置密码，写 MIBIB / U-Boot
# ============================================================================

def obtain_transition_shell(ctx):
    """返回 (kind, conn)；kind 为 'ssh' 或 'telnet'"""
    # 先试 SSH
    for pw in (ctx.new_password, "root", ""):
        c = ssh_try(ctx.router_ip, "root", pw, ctx.bind_ip, 12)
        if c:
            log("SSH 已连接过渡系统（密码: %s）" % ("<空>" if pw == "" else pw))
            return "ssh", c
    # 再试 telnet
    t = telnet_try(ctx.router_ip, ctx.bind_ip, 8)
    if t:
        return "telnet", t
    return None, None


def trans_exec(sh, cmd, timeout=30):
    kind, conn = sh
    if kind == "ssh":
        return conn.exec_cmd(cmd, timeout)
    return conn.run(cmd, timeout)


def trans_upload(ctx, sh, local, remote, timeout=300):
    kind, conn = sh
    if kind == "ssh":
        conn.upload(local, remote, timeout)
        return True
    # telnet 上传：base64 + 分块 echo（仅用于小文件，尽量走 SSH）
    raise FlashError("过渡系统当前只有 telnet，无法高速上传；请检查 SSH 是否已启动")


def setup_transition_password(ctx):
    log("检查过渡系统登录方式 ...")
    # 先看 SSH 是否已经可用
    c = ssh_try(ctx.router_ip, "root", ctx.new_password, ctx.bind_ip, 12)
    if c:
        c.close()
        log("SSH 已可用。")
        return

    log("通过 telnet 设置 root 密码 ...")
    t = None
    for i in range(30):
        t = telnet_try(ctx.router_ip, ctx.bind_ip, 6)
        if t:
            break
        sleep_s(3)
    if not t:
        raise FlashError("无法通过 telnet 登录过渡系统")

    try:
        t.run("stty -echo 2>/dev/null; true", timeout=8)
        rc, out = t.run("uname -a", 10)
        log("  uname: %s" % (out.strip() or "(无输出)"))
        pw = ctx.new_password
        rc, out = t.run("printf '%%s\\n%%s\\n' %s %s | passwd root" % (shq(pw), shq(pw)), 20)
        log("  passwd 返回码: %s" % rc)
        rc, out = t.run("grep '^root:' /etc/shadow", 10)
        if shadow_root_is_empty(out):
            raise FlashError("root 密码设置失败（shadow 仍为空）")
        log("  root 密码已设置")
        t.run("/etc/init.d/dropbear restart", 20)
    finally:
        try:
            t.close()
        except Exception:
            pass


def stage2_uboot_write(ctx, sh=None):
    log_section("第 2 步 / 4：写入 MIBIB 与 U-Boot")

    ctx.ensure_router_bind()
    setup_transition_password(ctx)
    sh = obtain_transition_shell(ctx)
    if not sh[0]:
        raise FlashError("无法登录过渡系统（SSH/Telnet 均失败）")
    log("已登录过渡系统（%s）" % sh[0])

    try:
        rc, out = trans_exec(sh, "cat /proc/mtd", 20)
        parts = parse_proc_mtd(out or "")
        log("过渡系统分区表:")
        for name in sorted(parts, key=lambda k: parts[k]["num"]):
            log("  mtd%-3d %-18s %s" % (parts[name]["num"], name, human_size(parts[name]["size"])))
        mtd1 = parts.get("0:MIBIB")
        mtd7 = parts.get("0:APPSBL")
        if not mtd1 or mtd1["num"] != 1:
            raise FlashError("过渡系统中 mtd1 不是 \"0:MIBIB\"，分区表异常，已停止")
        if not mtd7 or mtd7["num"] != 7:
            raise FlashError("过渡系统中 mtd7 不是 \"0:APPSBL\"，分区表异常，已停止")
        if mtd1["size"] != XD or mtd7["size"] != XD:
            raise FlashError("mtd1/mtd7 分区大小异常，已停止")

        mibib_md5 = md5_file(ctx.path_mibib)
        uboot_md5 = md5_file(ctx.path_uboot)
        uboot_sz = os.path.getsize(ctx.path_uboot)
        uboot_blocks = uboot_sz // 4096
        uboot_padded_md5 = md5_file(ctx.path_uboot, uboot_blocks * 4096)

        # 已经有正确的 U-Boot？可跳过
        rc, out = trans_exec(sh, "dd if=/dev/mtd7 bs=4096 count=%d 2>/dev/null | md5sum"
                             % uboot_blocks, 60)
        m = re.search(r"\b([0-9a-fA-F]{32})\b", out or "")
        if m and m.group(1).lower() == uboot_padded_md5:
            log("检测到 mtd1/mtd7 已经是目标 bootloader，跳过写入。")
        else:
            if sh[0] == "telnet":
                raise FlashError("当前只有 telnet 连接，无法上传大文件。请重启后重试。")

            log("上传 MIBIB (%s) ..." % human_size(os.path.getsize(ctx.path_mibib)))
            ssh = sh[1]
            ssh.upload(ctx.path_mibib, "/tmp/ax6-uboot-mibib.bin", 300)
            if ssh.md5_remote("/tmp/ax6-uboot-mibib.bin") != mibib_md5:
                raise FlashError("MIBIB 上传校验失败")

            log("上传 U-Boot (%s) ..." % human_size(uboot_sz))
            ssh.upload(ctx.path_uboot, "/tmp/uboot-ax6.bin", 300)
            if ssh.md5_remote("/tmp/uboot-ax6.bin") != uboot_md5:
                raise FlashError("U-Boot 上传校验失败")

            need_confirm = ctx.cfg.get_bool("options", "confirm_bootloader") and not ctx.args.yes
            if need_confirm:
                log("")
                log("即将写入 bootloader（这是不可逆操作）：")
                log("   mtd erase /dev/mtd1 ; mtd write /tmp/ax6-uboot-mibib.bin /dev/mtd1")
                log("   mtd erase /dev/mtd7 ; mtd write /tmp/uboot-ax6.bin /dev/mtd7")
                if not ask_yes("确认继续？[Y/n] ", True):
                    raise FlashError("用户取消")

            log("写入 mtd1 (MIBIB) ...")
            trans_exec(sh, "mtd erase /dev/mtd1", 120)
            rc, out = trans_exec(sh, "mtd write /tmp/ax6-uboot-mibib.bin /dev/mtd1", 180)
            log("  返回码 %s" % rc)
            rc, out = trans_exec(sh, "dd if=/dev/mtd1 bs=4096 count=256 2>/dev/null | md5sum", 120)
            m = re.search(r"\b([0-9a-fA-F]{32})\b", out or "")
            if not m or m.group(1).lower() != mibib_md5:
                raise FlashError("mtd1 校验失败：%s != %s" % (
                    m.group(1) if m else None, mibib_md5))
            log("  mtd1 校验通过")

            log("写入 mtd7 (U-Boot) ...")
            trans_exec(sh, "mtd erase /dev/mtd7", 120)
            rc, out = trans_exec(sh, "mtd write /tmp/uboot-ax6.bin /dev/mtd7", 180)
            log("  返回码 %s" % rc)
            rc, out = trans_exec(sh, "dd if=/dev/mtd7 bs=4096 count=%d 2>/dev/null | md5sum"
                                 % uboot_blocks, 120)
            m = re.search(r"\b([0-9a-fA-F]{32})\b", out or "")
            if not m or m.group(1).lower() != uboot_padded_md5:
                raise FlashError("mtd7 校验失败：%s != %s" % (
                    m.group(1) if m else None, uboot_padded_md5))
            log("  mtd7 校验通过")

        trans_exec(sh, "sync", 20)
    finally:
        try:
            if sh[0] == "ssh":
                sh[1].close()
            else:
                sh[1].close()
        except Exception:
            pass

    # ---- 手动进入 U-Boot ----
    log("")
    log("=" * 70)
    log("Bootloader 写入完成！现在需要手动进入 U-Boot Web 界面：")
    log("")
    log("   1) 拔掉 AX6 的电源")
    log("   2) 按住路由器背面的 Reset 键不要松开")
    log("   3) 插上电源，继续按住 Reset 约 10 秒")
    log("   4) 松开 Reset 键")
    log("   5) 电脑必须用网线连接 AX6 的 LAN 口")
    log("")
    log("等待 U-Boot Web (http://%s) 出现，最长等待 30 分钟 ..." % ctx.router_ip)
    log("=" * 70)

    def fn():
        raw = http_probe(ctx.router_ip, ctx.bind_ip, 3.0)
        return True if is_uboot_web(raw) else None

    if not wait_until("U-Boot Web 界面", fn, 1800, 3):
        raise FlashError("等待 U-Boot 超时。请断电后按住 Reset 再上电，然后重新运行本脚本"
                         "（会自动从第 3 步继续）。")
    # 告诉第 3 步：已经确认过 U-Boot，不要再重复探测（会占用 uIP 连接槽）
    ctx.uboot_ready = True
    sleep_s(2)
    log("")
    log("U-Boot 已就绪！")


# ============================================================================
#  第 3 步：U-Boot 刷 ImmortalWrt
# ============================================================================

def uboot_post_upload(ctx, path):
    ip = ctx.router_ip
    boundary = "----AX6AutoBoundary%d" % int(time.time())
    fname = os.path.basename(path)
    head = ("--%s\r\n"
            "Content-Disposition: form-data; name=\"firmware\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fname)).encode("latin-1")
    tail = ("\r\n--%s--\r\n" % boundary).encode("latin-1")
    size = os.path.getsize(path)
    total = len(head) + size + len(tail)
    # ！！必须保留 Content-Length 冒号后的空格！！
    #
    # 这个 U-Boot 的 httpd 解析 Content-Length 时，是从 "Content-Length:"
    # 字符串起始处 +16 开始取数字的（源码里 start += sizeof("Content-Length:")，
    # sizeof 含结尾的 '\0'，等于跳过“1 个冒号 + 1 个字符”）。浏览器发的
    # "Content-Length: 17324000" 里那个空格正好被跳过，得到正确值；
    # 如果写成 "Content-Length:17324000"（无空格），第一位数字 '1' 会被
    # 吃掉，U-Boot 认为文件只有 7324000 字节 —— 收到 7.3 MB 就判定“上传
    # 完成”，停止网口开始刷写，客户端表现为在 7.28 MiB 处收到 RST
    # (WinError 10054)，并且会把截断的固件写进 flash。
    hdr = ("POST / HTTP/1.0\r\n"
           "Host: %s\r\n"
           "Content-Type: multipart/form-data; boundary=%s\r\n"
           "Content-Length: %d\r\n"
           "Connection: close\r\n\r\n" % (ip, boundary, total)).encode("latin-1")
    log("  Content-Length: %d（固件 %d 字节）" % (total, size))

    # 浏览器上传是满速的，这里也满速发送：uIP 的连接有约 10000 次 poll 的
    # 上限，速率越快，整个上传占用的 poll 次数越少。
    CHUNK = 65536

    last_err = None
    max_try = 4
    for up_try in range(1, max_try + 1):
        if up_try > 1:
            log("重新上传（第 %d/%d 次）..." % (up_try, max_try))
            sleep_s(15)

        # ---- 连接（U-Boot 的连接槽可能还没释放，做重试）----
        # 上一轮中断后 U-Boot 需要十几秒才释放连接槽，这里等满 2 分钟再放弃。
        s = None
        attempts = 40
        for attempt in range(1, attempts + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if ctx.bind_ip:
                sock.bind((ctx.bind_ip, 0))
            try:
                sock.settimeout(5)
                sock.connect((ip, 80))
                s = sock
                break
            except Exception as e:
                last_err = e
                try:
                    sock.close()
                except Exception:
                    pass
                if attempt == 1 or attempt % 5 == 0:
                    log("U-Boot 暂时不接受新连接（连接槽可能未释放），重试 %d/%d ..."
                        % (attempt, attempts))
                sleep_s(3)

        if s is None:
            if up_try < max_try:
                continue
            raise FlashError("无法连接 U-Boot Web (http://%s)：%s\n"
                             "解决办法：\n"
                             "  1) 路由器可能正在写 flash 或重启，等 2-3 分钟\n"
                             "  2) 断电重插，开机时按住 Reset 进入 U-Boot web 恢复页\n"
                             "  3) 确认浏览器能打开 http://192.168.1.1 后再执行 --stage 3"
                             % (ip, last_err))

        sent = 0
        body_done = False
        try:
            s.settimeout(240)

            with open(path, "rb") as f:
                first = f.read(1000)
                # 重要：U-Boot 的 httpd（pepe2k u-boot_mod）在收到第一个 POST 包时
                # 只解析 Content-Length 和 boundary，然后在“下一个包”里用 strstr
                # 找 boundary 来定位数据起点。如果 HTTP 头和 multipart 头在同一个
                # TCP 包里，下一包就是纯固件数据，找不到 boundary 会直接
                # uip_abort()（客户端看到 10054 连接被重置）。
                # 所以这里必须分两次 send，让边界头单独成为下一包。
                s.sendall(hdr)
                sleep_s(0.3)
                s.sendall(head + first)
                sent = len(first)
                t0 = time.time()
                last = 0.0
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    s.sendall(buf)
                    sent += len(buf)
                    now = time.time()
                    if now - last >= 0.5 or sent >= size:
                        last = now
                        speed = sent / max(0.001, now - t0) / 1024.0 / 1024.0
                        progress("  上传固件: %5.1f%%  (%s / %s, %.2f MiB/s)" % (
                            sent * 100.0 / max(1, size), human_size(sent),
                            human_size(size), speed))
                # 文件数据已全部发出（尾部的 multipart 结束标记不影响刷写）
                body_done = True
                try:
                    s.sendall(tail)
                except Exception:
                    pass
            progress_end()

            log("上传完成，等待 U-Boot 响应 ...")
            s.settimeout(180)
            data = b""
            while len(data) < 65536:
                try:
                    buf = s.recv(8192)
                except socket.timeout:
                    break
                if not buf:
                    break
                data += buf
            return data
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
            progress_end()
            last_err = e
            if body_done:
                # 数据已全部发出，U-Boot 可能已经开始写 flash，不再重试
                log("!! 上传完成后连接被重置（U-Boot 可能已开始写 flash）。")
                return b""
            log("!! 上传中断（已发送 %s / %s）：%r，重试 ..."
                % (human_size(sent), human_size(size), e))
            sleep_s(5)
        except socket.timeout as e:
            progress_end()
            last_err = e
            if body_done:
                log("!! 等待 U-Boot 响应超时（数据已全部发出，可能已在写 flash）。")
                return b""
            log("!! 上传超时（已发送 %s / %s）：%r，重试 ..."
                % (human_size(sent), human_size(size), e))
            sleep_s(5)
        finally:
            progress_end()
            try:
                s.close()
            except Exception:
                pass

    raise FlashError("上传失败（已自动重试 %d 次，最后错误：%r）\n"
                     "解决办法：\n"
                     "  1) 等 2-3 分钟，路由器会重新回到 U-Boot web 恢复页\n"
                     "  2) 确认 http://192.168.1.1 能打开，然后执行 --stage 3\n"
                     "  3) 若一直打不开：断电，按住 Reset 上电进入恢复页\n"
                     "  4) 网线必须接 AX6 的 LAN 口，电脑网卡设为 192.168.1.10/24"
                     % (max_try, last_err))


def stage3_uboot_flash(ctx):
    log_section("第 3 步 / 4：U-Boot 刷入 ImmortalWrt")

    ctx.ensure_router_bind()

    if ctx.uboot_ready:
        # 第 2 步刚刚探测成功，直接上传。再探一次会占用 U-Boot 的连接槽。
        log("U-Boot 已就绪（第 2 步已确认），直接上传。")
    else:
        # 等待 / 确认 U-Boot
        def fn():
            raw = http_probe(ctx.router_ip, ctx.bind_ip, 3.0)
            return True if is_uboot_web(raw) else None

        if not fn():
            log("当前没有检测到 U-Boot Web，等待中 ...")
            log("请断电，按住 Reset 再上电，看到 U-Boot Web 后会自动继续。")
            if not wait_until("U-Boot Web 界面", fn, 900, 3):
                raise FlashError("等待 U-Boot 超时（15 分钟）\n"
                                 "解决办法：\n"
                                 "  1) 断电，按住 Reset 键再上电，等网口灯亮后访问 http://192.168.1.1\n"
                                 "  2) 电脑网卡设为 192.168.1.10/255.255.255.0，网线接 LAN 口\n"
                                 "  3) 看到 U-Boot 页面后执行 --stage 3")
        log("U-Boot 已就绪。")
        sleep_s(2)

    img = ctx.path_imm
    log("准备上传: %s (%s)" % (os.path.basename(img), human_size(os.path.getsize(img))))
    log("注意：上传过程中千万不要断电或关闭窗口！")

    resp = uboot_post_upload(ctx, img)
    text = (resp or b"").decode("latin-1", "replace")
    if not text:
        # 数据已完整发出，但连接在响应前被断开：U-Boot 很可能已经开始
        # 写 flash（它会先停掉网口），继续等待 ImmortalWrt 启动即可。
        log("!! U-Boot 没有返回响应（连接在写 flash 前被重置）。")
        log("!! 按已接受处理，继续等待 ImmortalWrt 启动 ...")
    elif "UPDATE IN PROGRESS" in text.upper():
        log("U-Boot 已接受固件，正在写 flash（需要几分钟）...")
    elif "UPDATE FAILED" in text.upper():
        raise FlashError("U-Boot 报告固件写入失败（UPDATE FAILED）\n"
                         "解决办法：\n"
                         "  1) 确认刷的是 .ubi / factory 固件（不是 sysupgrade 包）\n"
                         "  2) 重新进入 U-Boot 页面（断电 + Reset 上电）后再执行 --stage 3\n"
                         "  3) NAND 坏块导致反复失败时，先在 U-Boot 里擦除并重写 MIBIB，再刷固件")
    else:
        log("U-Boot 响应（前 300 字节）：")
        log(text[:300])
        raise FlashError("U-Boot 返回了无法识别的响应\n"
                         "解决办法：重新进入 U-Boot web 恢复页（断电 + Reset 上电）后"
                         "执行 --stage 3；若页面显示固件太大，确认用的是 16.5 MB 的 "
                         "factory.ubi")

    log("等待 ImmortalWrt 启动 ...")
    log("首次启动会自动创建 rootfs_data，可能需要 2-5 分钟。")

    def fn2():
        raw = http_probe(ctx.router_ip, ctx.bind_ip, 3.0)
        if raw and not is_uboot_web(raw) and is_http_up(raw):
            return True
        return None

    if not wait_until("ImmortalWrt (http://%s)" % ctx.router_ip, fn2, 900, 4):
        raise FlashError("ImmortalWrt 启动超时（15 分钟）\n"
                         "解决办法：\n"
                         "  1) 再等 3-5 分钟（首次启动要创建 rootfs_data，可能较慢）\n"
                         "  2) 浏览器打开 http://%s 看是否出现 LuCI\n"
                         "  3) 若回到 U-Boot 页面：说明固件没写进去，重新执行 --stage 3\n"
                         "  4) 电脑网卡若还有 192.168.31.x 地址，改成自动获取(DHCP)"
                         % ctx.router_ip)
    log("ImmortalWrt 已启动。")


# ============================================================================
#  第 4 步：安装 Argon / OpenClash / SmartDNS
# ============================================================================

def immortal_set_password(ctx):
    log("设置 ImmortalWrt root 密码 ...")
    ip = ctx.router_ip
    bind = ctx.bind_ip
    pw = ctx.new_password

    # 1) 已经能用目标密码登录
    c = ssh_try(ip, "root", pw, bind, 10)
    if c:
        c.close()
        log("root 密码已经是目标密码。")
        return

    # 2) 等待 SSH 端口，尝试 factory 固件的默认空密码
    for _ in range(40):
        if tcp_open(ip, 22, 2.0, bind):
            break
        sleep_s(5)

    c = ssh_try(ip, "root", "", bind, 10)
    if c:
        try:
            log("通过空密码 SSH 登录成功，正在设置密码 ...")
            cmd = "printf '%%s\\n%%s\\n' %s %s | passwd root" % (shq(pw), shq(pw))
            st, out = c.exec_cmd(cmd, 30)
            log("  passwd 返回码: %s" % st)
            st2, out2 = c.exec_cmd("grep '^root:' /etc/shadow", 15)
            if shadow_root_is_empty(out2):
                raise FlashError("passwd 执行后 root 密码仍为空")
        except FlashError:
            raise
        except Exception as e:
            log("  通过 SSH 设置密码出错: %s" % e)
        finally:
            try:
                c.close()
            except Exception:
                pass
        for i in range(30):
            c2 = ssh_try(ip, "root", pw, bind, 10)
            if c2:
                c2.close()
                log("root 密码设置成功。")
                return
            sleep_s(2)
        raise FlashError("密码已设置，但用新密码 SSH 登录失败")

    # 3) 通过 ubus RPC 设置（需要 luci-mod-rpc）
    log("SSH 空密码登录不可用，尝试通过 ubus 设置密码 ...")
    sess = None
    for attempt in range(40):
        sess = ubus_login(ip, "", bind, 10) or ubus_login(ip, pw, bind, 10)
        if sess:
            break
        sleep_s(5)
    if not sess:
        raise FlashError(
            "无法登录 ImmortalWrt（ubus session.login 失败）。\n"
            "请用浏览器打开 http://%s ，用用户名 root、空密码登录，手动设置密码为 \"%s\"，"
            "然后重新运行本脚本（会自动从第 4 步继续）。" % (ip, pw))

    res = ubus_call(ip, sess, "luci", "setPassword",
                    {"username": "root", "password": pw}, bind, 20)
    code = ubus_code(res)
    if code != 0:
        log("  luci.setPassword 返回: %s" % json.dumps(res, ensure_ascii=False)[:300])
        raise FlashError(
            "自动设置 root 密码失败。\n"
            "请用浏览器打开 http://%s ，用用户名 root、空密码登录，手动设置密码为 \"%s\"，"
            "然后重新运行本脚本（会自动从第 4 步继续）。" % (ip, pw))
    log("root 密码设置成功。")

    for i in range(30):
        c = ssh_try(ip, "root", pw, bind, 10)
        if c:
            try:
                c.close()
            except Exception:
                pass
            log("SSH 登录成功。")
            return
        sleep_s(2)
    raise FlashError("密码已设置，但 SSH 仍无法登录")


def immortal_ssh(ctx):
    ip = ctx.router_ip
    for i in range(60):
        c = ssh_try(ip, "root", ctx.new_password, ctx.bind_ip, 12)
        if c:
            return c
        sleep_s(3)
    raise FlashError("无法通过 SSH 连接 ImmortalWrt (%s)" % ip)


def pkg_manager(ssh):
    st, out = ssh.exec_cmd("command -v apk 2>/dev/null | head -n1", 20)
    if (out or "").strip():
        return "apk"
    st, out = ssh.exec_cmd("command -v opkg 2>/dev/null | head -n1", 20)
    if (out or "").strip():
        return "opkg"
    return None


def router_clock_skew(ssh):
    """返回路由器与电脑的时间差（秒）；取不到时返回 None。"""
    st, out = ssh.exec_cmd("date -u +%s", 20)
    try:
        remote = int((out or "").strip().splitlines()[-1])
    except Exception:
        return None
    return remote - int(time.time())


def sync_router_clock(ssh, wait=150):
    """确保路由器时间正确。

    刚刷完第一次启动时，ImmortalWrt 还没完成 NTP 同步，系统时间可能还停留在
    编译时间附近；此时 apk 走 HTTPS 会报
    "wgetSSL verify error: certificate is not yet valid"，apk update 全挂。
    这里先等 NTP（WAN 已通的情况下一般 30-60 秒），等不到就用电脑时间兜底。
    """
    skew = router_clock_skew(ssh)
    if skew is not None and abs(skew) <= 300:
        return True

    log("  路由器时间未同步（偏差 %s 秒），等待 NTP ..." %
        ("未知" if skew is None else skew))
    deadline = time.time() + wait
    while time.time() < deadline:
        sleep_s(5)
        skew = router_clock_skew(ssh)
        if skew is not None and abs(skew) <= 300:
            log("  时间已同步。")
            return True

    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    log("  NTP 仍未同步，改用电脑时间：%s UTC" % ts)
    ssh.exec_cmd('date -u -s "%s" >/dev/null 2>&1; hwclock -w 2>/dev/null; true' % ts, 30)
    skew = router_clock_skew(ssh)
    if skew is not None and abs(skew) <= 300:
        log("  时间已设置。")
        return True
    log("!! 无法校正路由器时间，软件源可能下载失败。")
    return False


def pkg_advice(out):
    """根据软件包操作的输出给出对应的解决办法。"""
    low = (out or "").lower()
    if "certificate" in low or "not yet valid" in low:
        return ("路由器系统时间不对（证书校验不通过）。脚本会自动等待 NTP 并重试；"
                "若仍失败，在 SSH 里执行 date 检查时间，或重启路由器等 NTP 同步后再跑一次。")
    if "no such package" in low or "unable to select packages" in low:
        return ("软件源里没有这个包：确认 /etc/apk/repositories.d 里的版本与固件一致"
                "（登录 LuCI → 系统 → 软件包 → 配置，或换一个可用镜像）。")
    if "no space left" in low or "disk full" in low:
        return ("路由器 ROM 空间不足：先卸载不用的插件，或重刷更精简的 ImmortalWrt 固件。")
    if ("network" in low or "timed out" in low or "could not connect" in low
            or "resolve" in low or "unreachable" in low or "dns" in low):
        return ("路由器 WAN 没有联网或 DNS 不通：检查 WAN 口网线/上级路由，"
                "在 LuCI → 网络 → 诊断里 ping 一下 223.5.5.5。")
    if "checksum" in low or "signature" in low or "bad" in low:
        return "下载的软件包损坏：清理缓存（apk cache clean / rm -rf /tmp/apk*）后重跑本步骤。"
    return ("可在 LuCI → 系统 → 软件包 里手动安装，或检查路由器是否能访问软件源。"
            "确认 WAN 已联网后可单独重跑：--stage 4")


def pkg_update(ssh, mgr, timeout=300, attempts=3, delays=(5, 15, 30)):
    """更新软件源；失败时按时间/网络问题自动延时重试。"""
    cmd = "apk update" if mgr == "apk" else "opkg update"
    out = ""
    for i in range(attempts):
        if i == 0:
            log("  执行: %s" % cmd)
        else:
            log("  重试: %s（第 %d/%d 次）" % (cmd, i + 1, attempts))
        st, out = ssh.exec_cmd(cmd, timeout)
        low = (out or "").lower()
        if st == 0:
            tail = "\n".join((out or "").strip().splitlines()[-8:])
            for line in tail.splitlines():
                log("    " + line)
            return True

        if ("certificate" in low or "not yet valid" in low or "ssl" in low):
            log("  证书校验失败（系统时间问题），先校正时间 ...")
            sync_router_clock(ssh)
        elif ("unable to connect" in low or "timed out" in low or "network" in low
                or "resolve" in low or "unreachable" in low):
            log("  连不上软件源（WAN/DNS 问题）...")

        if i < attempts - 1:
            d = delays[min(i, len(delays) - 1)]
            log("  软件源更新失败，%d 秒后重试 ..." % d)
            sleep_s(d)

    tail = "\n".join((out or "").strip().splitlines()[-8:])
    for line in tail.splitlines():
        log("    " + line)
    log("!! 解决办法: " + pkg_advice(out))
    return False


def pkg_install(ssh, mgr, pkgs, required, timeout=600, attempts=2, delay=5):
    """安装软件包；网络类错误自动延时重试，软件源里确实没有的包不重试。"""
    cmd = "apk add %s" % " ".join(pkgs) if mgr == "apk" else "opkg install %s" % " ".join(pkgs)
    out = ""
    for i in range(attempts):
        if i == 0:
            log("  执行: %s" % cmd)
        else:
            log("  重试: %s（第 %d/%d 次）" % (cmd, i + 1, attempts))
        st, out = ssh.exec_cmd(cmd, timeout)
        if st == 0:
            tail = "\n".join((out or "").strip().splitlines()[-12:])
            for line in tail.splitlines():
                log("    " + line)
            return True

        low = (out or "").lower()
        # 这类错误重试没有意义，直接给出建议
        if ("no such package" in low or "unable to select packages" in low
                or "no space left" in low or "checksum" in low):
            break
        if i < attempts - 1:
            log("  安装失败，%d 秒后重试 ..." % delay)
            sleep_s(delay)
            delay *= 2

    tail = "\n".join((out or "").strip().splitlines()[-12:])
    for line in tail.splitlines():
        log("    " + line)
    low = (out or "").lower()
    if not required and ("no such package" in low or "unable to select packages" in low):
        log("  可选软件包 %s 在软件源里不存在，跳过。" % ", ".join(pkgs))
        return False
    log("!! 安装 %s 失败（返回码 %s）" % (", ".join(pkgs), st))
    log("!! 解决办法: " + pkg_advice(out))
    if required:
        raise FlashError("安装 %s 失败" % ", ".join(pkgs))
    return False


SMARTDNS_UPSTREAMS = ("223.5.5.5", "119.29.29.29", "114.114.114.114")


def configure_smartdns(ssh):
    """启用 SmartDNS（6053），让 dnsmasq 转发给它，并添加默认上游。"""
    add_servers = "if ! uci -q show smartdns | grep -q '@server\\['; then %s fi; " % " ".join(
        "uci add smartdns server >/dev/null; uci set 'smartdns.@server[-1].ip=%s';" % ip
        for ip in SMARTDNS_UPSTREAMS)
    cmd = (
        "uci set smartdns.@smartdns[0].enabled='1'; "
        "uci set smartdns.@smartdns[0].port='6053'; "
        "uci set smartdns.@smartdns[0].auto_set_dnsmasq='1'; "
        + add_servers +
        " uci commit smartdns; "
        "/etc/init.d/smartdns enable 2>/dev/null; "
        "/etc/init.d/smartdns restart 2>/dev/null; "
        "sleep 5; "
        "pidof smartdns >/dev/null && echo SDNS-RUNNING || echo SDNS-STOPPED"
    )
    out = ""
    for i in range(3):
        if i:
            log("  SmartDNS 未运行，5 秒后重试启动 ...")
            sleep_s(5)
        st, out = ssh.exec_cmd(cmd, 120)
        if "SDNS-RUNNING" in (out or ""):
            log("SmartDNS 已启用：监听 6053，dnsmasq 已转发，上游 %s"
                % " / ".join(SMARTDNS_UPSTREAMS))
            return True
    log("!! SmartDNS 没有运行起来（不影响上网，dnsmasq 会继续用自己的上游）。")
    for line in (out or "").strip().splitlines()[-6:]:
        log("    " + line)
    log("!! 解决办法：LuCI → 服务 → SmartDNS 里点“启用/保存并应用”；"
        "或 SSH 执行 /etc/init.d/smartdns enable && /etc/init.d/smartdns restart，"
        "然后看 logread | grep smartdns 排查配置错误。")
    return False


def stage4_packages(ctx):
    log_section("第 4 步 / 4：安装 Argon 主题 / OpenClash / SmartDNS")

    ctx.ensure_router_bind()
    immortal_set_password(ctx)
    ssh = immortal_ssh(ctx)
    try:
        st, rel = ssh.exec_cmd("cat /etc/openwrt_release 2>/dev/null; cat /etc/os-release 2>/dev/null", 20)
        log("系统信息:")
        for line in (rel or "").strip().splitlines()[:8]:
            log("  " + line)

        mgr = None
        for _i in range(3):
            mgr = pkg_manager(ssh)
            if mgr:
                break
            log("  未检测到包管理器，5 秒后重试 ...")
            sleep_s(5)
        if not mgr:
            raise FlashError("找不到 apk/opkg 包管理器\n"
                             "解决办法：确认刷入的是完整的 ImmortalWrt 固件"
                             "（SSH 里执行 apk --version 或 opkg --version 检查），"
                             "必要时重新刷写固件后再跑 --stage 4")
        log("包管理器: %s" % mgr)

        sync_router_clock(ssh)

        if not pkg_update(ssh, mgr, 300):
            raise FlashError("apk/opkg update 失败（已自动重试 3 次，每次间隔递增）\n"
                             "解决办法（按顺序排查）：\n"
                             "  1) 登录 LuCI → 网络 → 诊断，ping 223.5.5.5 和 mirrors.vsean.net\n"
                             "  2) 时间不对会导致 HTTPS 证书报错：SSH 执行 date，必要时重启等 NTP\n"
                             "  3) 换一个可用镜像：LuCI → 系统 → 软件包 → 配置\n"
                             "  4) 处理完成后单独重跑：--stage 4")

        results = {}

        # ---- 依赖 ----
        for p in ("luci-compat", "luci-lua-runtime"):
            pkg_install(ssh, mgr, [p], False, 300)

        # ---- Argon ----
        if ctx.cfg.get_bool("options", "install_argon"):
            log("")
            log("安装 Argon 主题 ...")
            ok = pkg_install(ssh, mgr, ["luci-theme-argon"], True, 600)
            ok2 = pkg_install(ssh, mgr, ["luci-app-argon-config"], False, 600)
            results["argon"] = ok
            if ok:
                st, out = ssh.exec_cmd(
                    "test -d /www/luci-static/argon && echo YES || echo NO", 20)
                if "YES" not in (out or ""):
                    log("!! 主题目录不存在，Argon 可能未正确安装")
                else:
                    ssh.exec_cmd("uci set luci.main.mediaurlbase='/luci-static/argon'; "
                                 "uci commit luci", 30)
                    log("已把 LuCI 主题设置为 Argon")

        # ---- OpenClash ----
        if ctx.cfg.get_bool("options", "install_openclash"):
            log("")
            log("安装 OpenClash ...")
            ok = pkg_install(ssh, mgr, ["luci-app-openclash"], True, 900)
            results["openclash"] = ok
            if ok:
                st, out = ssh.exec_cmd("test -d /etc/openclash && echo YES || echo NO", 20)
                if "YES" in (out or ""):
                    log("OpenClash 已安装。首次使用时请在 LuCI 中下载/更新内核。")
                else:
                    log("!! 未找到 /etc/openclash，请到 LuCI 里确认 OpenClash 是否正常。")

        # ---- SmartDNS ----
        if ctx.cfg.get_bool("options", "install_smartdns"):
            log("")
            log("安装 SmartDNS ...")
            ok1 = pkg_install(ssh, mgr, ["smartdns"], True, 600)
            ok2 = pkg_install(ssh, mgr, ["luci-app-smartdns"], True, 600)
            results["smartdns"] = ok1 and ok2
            if ok1 and ok2:
                configure_smartdns(ssh)

        # ---- 中文语言包（可选） ----
        if ctx.cfg.get_bool("options", "install_zh_cn"):
            log("")
            log("安装中文语言包（可选，失败不影响使用）...")
            for p in ("luci-i18n-base-zh-cn", "luci-i18n-argon-config-zh-cn",
                      "luci-i18n-openclash-zh-cn", "luci-i18n-smartdns-zh-cn"):
                pkg_install(ssh, mgr, [p], False, 300)
            ssh.exec_cmd("uci set luci.main.lang='zh_cn'; uci commit luci", 30)

        ssh.exec_cmd("/etc/init.d/uhttpd restart 2>/dev/null; true", 60)

        # ---- 最终检查 ----
        log("")
        log("安装结果:")
        st, out = ssh.exec_cmd(
            "echo -n 'argon: '; test -d /www/luci-static/argon && echo OK || echo MISSING; "
            "echo -n 'openclash: '; test -f /usr/share/luci/menu.d/luci-app-openclash.json && echo OK || "
            "(test -f /usr/lib/lua/luci/controller/openclash.lua && echo OK || echo MISSING); "
            "echo -n 'smartdns: '; test -x /usr/sbin/smartdns && "
            "pidof smartdns >/dev/null && echo OK || echo MISSING", 30)
        log(out or "(无输出)")

        if "MISSING" in (out or ""):
            log("")
            if "argon: MISSING" in out:
                log("!! Argon 未生效：LuCI → 系统 → 系统属性 → 语言和界面 里选择 Argon；"
                    "仍不行就重跑 --stage 4")
            if "openclash: MISSING" in out:
                log("!! OpenClash 未生效：确认软件源里有 luci-app-openclash，"
                    "重跑 --stage 4；如报 no such package，换镜像后重试")
            if "smartdns: MISSING" in out:
                log("!! SmartDNS 未生效：LuCI → 服务 → SmartDNS 启用；"
                    "或 SSH 执行 /etc/init.d/smartdns restart 后看 logread | grep smartdns")

        return results
    finally:
        try:
            ssh.close()
        except Exception:
            pass


# ============================================================================
#  状态检测
# ============================================================================

def detect_state(ctx):
    """返回 stock / uboot / transition / immortal / none"""
    # 原厂 192.168.31.1
    if (http_probe(ctx.stock_ip, ctx.stock_bind, 2.0) or
            tcp_open(ctx.stock_ip, 22, 1.0, ctx.stock_bind)):
        return "stock"

    # 过渡/U-Boot/ImmortalWrt 都在 192.168.1.1
    raw = http_probe(ctx.router_ip, ctx.bind_ip, 2.5)
    if raw and is_uboot_web(raw):
        return "uboot"
    if tcp_open(ctx.router_ip, 23, 1.5, ctx.bind_ip):
        return "transition"
    c = ssh_try(ctx.router_ip, "root", ctx.new_password, ctx.bind_ip, 8) or \
        ssh_try(ctx.router_ip, "root", "root", ctx.bind_ip, 8) or \
        ssh_try(ctx.router_ip, "root", "", ctx.bind_ip, 8)
    if c:
        try:
            st, out = c.exec_cmd("cat /etc/openwrt_release 2>/dev/null", 15)
        finally:
            c.close()
        if "ImmortalWrt" in (out or ""):
            return "immortal"
        return "transition"
    if raw and is_http_up(raw):
        return "immortal"
    return "none"


# ============================================================================
#  主流程
# ============================================================================

def print_banner():
    log("")
    log("=" * 70)
    log("  Redmi AX6 (RA69) 全自动刷机脚本")
    log("  ImmortalWrt + Argon + OpenClash + SmartDNS")
    log("=" * 70)


def main():
    ap = argparse.ArgumentParser(description="Redmi AX6 全自动刷机脚本")
    ap.add_argument("--stage", choices=["auto", "1", "2", "3", "4"], default="auto",
                    help="从指定步骤开始（默认自动检测）")
    ap.add_argument("--check", action="store_true", help="只做预检查，不操作设备")
    ap.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    ap.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    ap.add_argument("--keep-ip", action="store_true", help="结束时保留临时 IP")
    ap.add_argument("--fix-net", action="store_true",
                    help="把网卡恢复为 DHCP 后退出（修复被关闭的 DHCP）")
    ap.add_argument("--no-color", action="store_true", help="（预留）")
    args = ap.parse_args()

    log_open()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    print_banner()

    if IS_WIN and not is_admin():
        log("")
        log("!! 当前不是管理员权限：如果需要给网卡添加临时 IP（192.168.1.10）")
        log("!! 或修改网卡设置，会失败。网卡上已有 192.168.1.x 地址时不受影响。")
        log("!! 需要提权时请用 run_ax6_auto.bat 启动（会自动申请管理员权限）。")
        if not args.check:
            log("")

    cfg = Cfg(args.config)
    if args.keep_ip:
        try:
            if not cfg.cp.has_section("options"):
                cfg.cp.add_section("options")
            cfg.cp.set("options", "remove_temp_ip", "no")
        except Exception:
            pass
    ctx = Ctx(cfg, args)

    try:
        preflight(ctx)

        if args.check:
            log("")
            log("--check 完成（未操作设备）。")
            return 0

        # 不在这里一刀切要求管理员：网卡上已经有 192.168.1.x 地址时
        # （例如上一次运行留下的），第 3、4 步完全可以不提权运行。
        # 真正需要改网卡的地方（prepare_adapter）会自己检查并给出提示。
        if not is_admin() and IS_WIN:
            log("提示：当前不是管理员。若需要添加临时 IP 或修改网卡，会提示改用 run_ax6_auto.bat。")

        if args.fix_net:
            ad = pick_router_adapter(ctx.stock_ip, ctx.router_ip, ctx.state)
            if not ad:
                raise FlashError("找不到可用的网卡")
            log("网卡: %s  当前地址: %s" % (ad["name"], ", ".join(ad["ips"]) or "无"))
            log("恢复 DHCP ...")
            st = enable_adapter_dhcp(ad["name"])
            sleep_s(3)
            ad2 = find_adapter(ad["name"]) or ad
            log("DHCP: %s" % ("已启用" if st else ("未启用" if st is False else "未知")))
            log("当前地址: %s" % (", ".join(ad2["ips"]) or "无"))
            log("")
            log("--fix-net 完成。")
            return 0

        # ---- 先判断是不是已经完全刷好 / 处于中间状态 ----
        # 选好网卡（不添加临时 IP），之后所有原厂连接都只走这块网卡
        ctx.prepare_adapter(need_router_ip=False)
        stock_online = bool(http_probe(ctx.stock_ip, ctx.stock_bind, 2.0) or
                            tcp_open(ctx.stock_ip, 22, 1.0, ctx.stock_bind))

        do1 = do2a = do2b = do3 = do4 = False

        if args.stage != "auto":
            s = int(args.stage)
            do1 = (s <= 1)
            do2a = do2b = (s <= 2)
            do3 = (s <= 3)
            do4 = True
            log("按 --stage %d 执行。" % s)

        elif stock_online:
            # 原厂状态
            log("检测到原厂固件在线 (%s)。" % ctx.stock_ip)

            # 如果 192.168.1.1 已经有设备响应，提醒一下（可能是别的设备）
            pre = http_probe(ctx.router_ip, None, 2.0)
            if pre and not args.yes:
                log("")
                log("!! 注意：192.168.1.1 当前已有设备响应！")
                log("!! 如果那是别的设备，刷机过程会连错设备。")
                if not ask_yes("确认继续？[y/N] ", False):
                    raise FlashError("用户取消")

            do1 = do2a = do2b = do3 = do4 = True

        else:
            # 先不改动网卡地址，再确认一次原厂是不是只是瞬时无响应
            if (http_probe(ctx.stock_ip, ctx.stock_bind, 2.0) or
                    tcp_open(ctx.stock_ip, 22, 1.0, ctx.stock_bind)):
                log("检测到原厂固件在线 (%s)。" % ctx.stock_ip)
                do1 = do2a = do2b = do3 = do4 = True
            else:
                ctx.prepare_adapter(need_router_ip=True)
                state = detect_state(ctx)
                log("自动检测到设备状态: %s" % state)
                if state == "stock":
                    do1 = do2a = do2b = do3 = do4 = True
                elif state == "uboot":
                    # 刚刚已经探测到 U-Boot，第 3 步不用再探一次（省一个连接槽）
                    ctx.uboot_ready = True
                    do3 = do4 = True
                elif state == "transition":
                    # 过渡系统已就绪：只需要写 bootloader（不要重复刷过渡固件）
                    do2b = do3 = do4 = True
                elif state == "immortal":
                    do4 = True
                else:
                    log("")
                    log("没有检测到设备。请检查：")
                    log("  1) 电脑用网线连接到 AX6 的 LAN 口")
                    log("  2) 路由器已通电")
                    log("  3) 原厂固件地址是 %s，ImmortalWrt/U-Boot 地址是 %s" % (
                        ctx.stock_ip, ctx.router_ip))
                    raise FlashError("设备不在线，无法自动判断步骤\n"
                                     "（如果已知当前处于哪一步，可用 --stage 指定）")

        # 注意：不要在这里统一添加 192.168.1.10。
        # 第 1 步和过渡固件阶段访问的是 192.168.31.1，此时改动网卡地址
        # 可能破坏 DHCP；需要 192.168.1.1 的阶段会在开始时自行 ensure_router_bind()。

        # ---- 执行 ----
        if do1:
            stage1(ctx)
        if do2a:
            stage2_transition(ctx)
        if do2b:
            stage2_uboot_write(ctx)
        if do3:
            stage3_uboot_flash(ctx)
        if do4:
            stage4_packages(ctx)

        log("")
        log("=" * 70)
        log("  全部完成！")
        log("=" * 70)
        log("")
        log("  后台地址: http://%s" % ctx.router_ip)
        log("  用户名:   root")
        log("  密码:     %s" % ctx.new_password)
        log("")
        log("  Argon 主题 / OpenClash / SmartDNS 已安装。")
        log("  OpenClash 首次使用需要在 LuCI 里更新/下载内核。")
        log("  SmartDNS 已启用（6053），dnsmasq 已转发；上游可在 LuCI 的 SmartDNS 页面调整。")
        log("")
        log("提示：如果电脑网卡上还有 192.168.31.x 的静态地址，请改为“自动获取(DHCP)”，")
        log("      否则可能无法通过 192.168.1.1 访问新系统。")
        log("")
        return 0

    except KeyboardInterrupt:
        log("")
        log("已取消。")
        return 130
    except FlashError as e:
        progress_end()
        log("")
        log("!! 失败: %s" % e)
        log("!! 详细日志: %s" % LOG_PATH)
        return 1
    except SystemExit:
        raise
    except Exception as e:
        progress_end()
        log("")
        log("!! 发生未预期的错误: %s" % e)
        if _log_fp is not None:
            try:
                _log_fp.write(traceback.format_exc() + "\n")
                _log_fp.flush()
            except Exception:
                pass
        log("!! 详细日志: %s" % LOG_PATH)
        return 1
    finally:
        try:
            ctx.cleanup_adapter()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
