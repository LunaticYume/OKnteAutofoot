"""url_guard —— B2.5/XMOD-01 共享出站 URL 防线（三模块唯一实现；纯标准库）。

背景（audit/2026-11-12/REMEDIATION-PLAN.md 批次1B XMOD-01 + .tmp/b25-ssrf/policy.md）：
sites_fetch / wiki_fetch / tajiduo_client 三处各自维护一份 _validate_url/_NoRedirect/
_OPENER 重复防线（勘察矩阵核实：_NoRedirect 三份逐字重复；_validate_url 三份 =
两同构变体 + wiki 实质分歧版——缺端口/凭据校验），且存在两个共性缺口：
① getaddrinfo 返回空列表时 for 循环 no-op 放行（probe c23 三模块全实证）；
② 校验期解析与连接期二次解析之间无任何绑定（TOCTOU/DNS 重绑定，probe c16
NOT_ASSERTABLE，probe_toctou_binding.py 结构性实证）。
本模块把防线收敛为唯一实现并修复两缺口；三模块以薄包装接入（常量白名单各自保留，
子域开关参数化——sites 子域放行 / wiki、tajiduo 精确相等）。

防线口径（与 AGENTS.md「安全红线」逐条对应）：
- 仅 https（urlsplit 对 scheme 自动小写化，HTTPS:// 同样过闸）；
- 主机白名单参数化：归一化 = rstrip 全部尾点 + lower 后比对（尾点/大小写放行是
  现行设计口径，selftest 断言保持；钉扎查找键同款归一化，消除 c19 实证的
  「校验名=去尾点名 vs 连接名=原始点名」错位）；
- 端口仅默认或 443（越界 :99999 / 不可解析 :bad 的解析器 ValueError 显式化为
  ERR_PORT，保留「拒绝」防线文案）；
- URL 内嵌用户信息一律拒绝（ERR_CREDENTIALS）；校验错误消息永不携带 userinfo——
  消息中的 URL 重建为 scheme://host[:port]path，有凭据时代以 ***（现状反例
  sites_fetch 旧实现把 user:pass 原串入消息，probe c03 异常原文实证，本层收口消除）；
- 解析：getaddrinfo 抛 OSError 或返回空列表 → ERR_DNS（fail-closed，修 no-op 放行；
  与连接期 socket.create_connection 对空记录 raise 的既有行为一致，校验期提前拒
  只是消除窗口）；全部记录逐一查六类受限段（loopback/private/link-local/reserved/
  multicast/unspecified；v4-mapped v6 的 is_private/is_loopback 在 3.13 判定链为
  True，天然覆盖）→ ERR_PRIVATE_IP。is_private 语义随 Python 版本浮动
  （3.13.13 含 TEST-NET-3），引用覆盖面时必须注明解释器版本；
- 禁止跟随重定向（_NoRedirect.redirect_request 一律 raise，重定向目标无法预校验）；
- TOCTOU：open_pinned 在传输期把校验期解析到的公网 IP 集钉扎到连接期解析
  （进程级 getaddrinfo 分发器，首次 open_pinned 幂等安装；first-wins 保并发语义：
  并发同主机时后到者沿用先到者已过六类段检查的公网集，攻击面不大于无钉扎状态，
  详见 policy §5.2/5.4）。钉扎不动 TLS：SNI/证书校验主机名仍是 URL 主机名。

六类错误形态（policy §1.4）：ERR_SCHEME / ERR_HOST / ERR_PORT / ERR_CREDENTIALS /
ERR_DNS / ERR_PRIVATE_IP——每类一个 URLGuardError 子类，code 属性机器可读，
消息含「拒绝/仅允许」（探针/selftest 显式防线判定兼容）。URLGuardError 继承
ValueError——三模块调用方既有 except (OSError, ValueError) 零改动兼容。
"""
import ipaddress
import re
import socket
import threading
import urllib.parse
import urllib.request
from typing import NamedTuple

__all__ = ["URLGuardError", "ERR_SCHEME", "ERR_HOST", "ERR_PORT",
           "ERR_CREDENTIALS", "ERR_DNS", "ERR_PRIVATE_IP", "PinnedURL",
           "validate_url", "host_allowed", "open_pinned", "OPENER", "_NoRedirect"]


# ---------- 六类错误形态（code 机器可读；消息含「拒绝/仅允许」防线文案） ----------

class URLGuardError(ValueError):
    """共享 URL 防线错误基类（ValueError 子类：调用方既有 except 兼容）。"""

    code = "ERR_URLGUARD"


class ERR_SCHEME(URLGuardError):
    """非 https scheme。"""

    code = "ERR_SCHEME"


class ERR_HOST(URLGuardError):
    """主机层不通过：空主机 / malformed（urlsplit ValueError 折叠于此）/
    白名单不匹配 / 子域开关不符。"""

    code = "ERR_HOST"


class ERR_PORT(URLGuardError):
    """端口非默认或 443；越界/不可解析端口的解析器 ValueError 显式化于此。"""

    code = "ERR_PORT"


class ERR_CREDENTIALS(URLGuardError):
    """URL 内嵌用户信息（username/password）。"""

    code = "ERR_CREDENTIALS"


class ERR_DNS(URLGuardError):
    """解析失败（getaddrinfo OSError）或解析结果为空（fail-closed）。"""

    code = "ERR_DNS"


class ERR_PRIVATE_IP(URLGuardError):
    """任一解析记录命中受限段（含 v4-mapped v6）。"""

    code = "ERR_PRIVATE_IP"


class PinnedURL(NamedTuple):
    """validate_url 的返回：原始 URL 原样透传 + 归一化主机 + 校验期通过的全部公网 IP。"""

    url: str
    host: str
    ips: tuple


# ---------- 日志脱敏（错误消息永不携带 userinfo） ----------

_RAW_CRED_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@]*@")


def _mask_raw_url(url):
    """urlsplit 失败（malformed）时的兜底表示：仅把 scheme:// 后第一段 @ 前的
    userinfo 代以 ***，其余原样（此时无法结构化解析，按无凭据也先掩）。"""
    return _RAW_CRED_RE.sub(lambda m: m.group(0).split("://")[0] + "://***@", str(url))


def _safe_urlrepr(parsed, raw):
    """把已解析 URL 重建为 scheme://host[:port]path 的诊断表示——userinfo 一律
    代以 ***，凭据绝不进任何错误消息（policy §1.5）。"""
    host = parsed.hostname or ""
    if not host:
        return _mask_raw_url(raw)
    shown_host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{shown_host}:{port}" if port is not None else shown_host
    if parsed.username or parsed.password:
        netloc = f"***@{netloc}"
    return f"{parsed.scheme}://{netloc}{parsed.path}"


# ---------- 主机白名单（参数化；归一化 = rstrip 全部尾点 + lower） ----------

def host_allowed(hostname, *, allowed_hosts, allow_subdomains=True):
    """主机归一化后是否在白名单：allow_subdomains=True 时严格等于允许域或为其
    子域（.域 结尾），False 时仅严格相等。前缀伪装（evil-example.com）与后缀
    伪装（example.com.evil.com）都不匹配；空主机/None 一律 False。"""
    host = (hostname or "").rstrip(".").lower()
    if not host:
        return False
    if allow_subdomains:
        return any(host == h or host.endswith("." + h) for h in allowed_hosts)
    return host in allowed_hosts


def validate_url(url, *, allowed_hosts, allow_subdomains=True, action="抓取"):
    """权威校验（共享层唯一实现）：https + 白名单主机 + 443 端口 + 无用户信息 +
    全记录六类段检查 + 空解析 fail-closed。通过则返回 PinnedURL（钉扎输入）。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as e:  # malformed（如 Invalid IPv6 URL）折叠进 ERR_HOST
        raise ERR_HOST(f"URL 无法解析，拒绝{action}：{_mask_raw_url(url)}") from e
    if parsed.scheme != "https":
        raise ERR_SCHEME(f"仅允许 https，拒绝{action}：{_safe_urlrepr(parsed, url)}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or not host_allowed(host, allowed_hosts=allowed_hosts,
                                    allow_subdomains=allow_subdomains):
        raise ERR_HOST(f"仅允许{action}白名单主机的 https 资源，"
                       f"拒绝：{_safe_urlrepr(parsed, url)}")
    try:
        port = parsed.port
    except ValueError as e:  # :99999 越界 / :bad 不可解析 → 显式防线文案
        raise ERR_PORT(f"端口非法（仅允许默认 443），"
                       f"拒绝{action}：{_safe_urlrepr(parsed, url)}") from e
    if port not in (None, 443):
        raise ERR_PORT(f"仅允许默认 443 端口，拒绝{action}：{_safe_urlrepr(parsed, url)}")
    if parsed.username or parsed.password:
        raise ERR_CREDENTIALS(f"URL 不得携带用户信息，"
                              f"拒绝{action}：{_safe_urlrepr(parsed, url)}")
    try:
        records = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as e:  # gaierror 等 → 显式 ERR_DNS（不再放任解析器异常穿透）
        raise ERR_DNS(f"{host} 解析失败，拒绝{action}") from e
    if not records:  # fail-closed：空记录原为 no-op 放行（probe c23 实证），修掉
        raise ERR_DNS(f"{host} 解析结果为空，拒绝{action}")
    ips = []
    for info in records:  # 全记录逐一查，任一命中即拒（无短路放行）
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ERR_PRIVATE_IP(f"{host} 解析到受限地址 {ip}，拒绝{action}")
        ips.append(str(ip))
    return PinnedURL(url, host, tuple(ips))


# ---------- TOCTOU 钉扎（传输期把校验期公网 IP 集钉到连接期解析） ----------

_PIN_LOCK = threading.Lock()
_PINS = {}                    # 归一化主机 → [ips tuple, active_count]（first-wins）
_ORIG_GETADDRINFO = None      # 安装时刻的 socket.getaddrinfo（生产=真实版）


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    """进程级分发器：钉扎在册的主机仅以校验期公网 IP 构造 records（形态自照仓库
    fake-DNS 口径 (AF_INET, SOCK_STREAM, 6, "", (ip, port)) / AF_INET6 四元组）；
    未在册主机委托安装时保存的原版。查找键与校验层同款归一化（rstrip 尾点 + lower），
    覆盖 c19 实证的「校验名=去尾点名 vs 连接名=原始点名」错位。"""
    key = (host or "").rstrip(".").lower()
    with _PIN_LOCK:
        entry = _PINS.get(key)
    if entry is None:
        return _ORIG_GETADDRINFO(host, port, *args, **kwargs)
    ips, _count = entry
    records = []
    for ip in ips:
        if ipaddress.ip_address(ip).version == 6:
            records.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                            (ip, port, 0, 0)))
        else:
            records.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
    return records


def _install_dispatcher():
    """幂等安装进程级分发器：socket.getaddrinfo 已是分发器则 no-op，否则安装并
    保存当时原版引用（生产=真实版；selftest 的 finally-restore 会把属性还原成
    它自己的桩，故幂等判据必须看属性现值而非一次性标志，重装时重取原版）。
    只装不卸：卸载会让并发在钉窗口内的请求失去钉扎保护。"""
    global _ORIG_GETADDRINFO
    with _PIN_LOCK:
        if socket.getaddrinfo is _pinned_getaddrinfo:
            return
        _ORIG_GETADDRINFO = socket.getaddrinfo
        socket.getaddrinfo = _pinned_getaddrinfo


def _register_pin(host, ips):
    with _PIN_LOCK:
        entry = _PINS.get(host)
        if entry is None:
            _PINS[host] = [tuple(ips), 1]
        else:  # first-wins：并发同主机沿用先到者已过六类段检查的公网集
            entry[1] += 1


def _unregister_pin(host):
    with _PIN_LOCK:
        entry = _PINS.get(host)
        if entry is not None:
            entry[1] -= 1
            if entry[1] <= 0:
                del _PINS[host]


# ---------- 禁重定向 + 唯一传输入口 ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """重定向目标无法预校验，一律拒绝跟随（三模块原逐字重复实现收敛于此）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"拒绝跟随重定向（{code}）：{newurl}")


OPENER = urllib.request.build_opener(_NoRedirect())


def open_pinned(url, *, timeout, opener, allowed_hosts, allow_subdomains=True,
                action="抓取", method="GET", headers=None, data=None):
    """唯一传输入口：完整校验（validate_url）→ 登记 IP 钉扎 → opener.open →
    读全量 bytes 返回。HTTPError 不就地吞（tajiduo 在调用侧包装为 TajiduoError，
    sites/wiki 维持 OSError 语义上抛）；解码/UA/错误包装差异留在各模块薄委托层。
    opener 由调用方传入模块级 _OPENER 别名（调用期晚绑定，兼容 selftest 以
    tc._OPENER = … 替换桩 opener 的既有口径）。"""
    pinned = validate_url(url, allowed_hosts=allowed_hosts,
                          allow_subdomains=allow_subdomains, action=action)
    _install_dispatcher()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    _register_pin(pinned.host, pinned.ips)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    finally:
        _unregister_pin(pinned.host)
