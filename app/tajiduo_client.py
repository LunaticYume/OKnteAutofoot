"""塔吉多个人角色数据通道（手动触发 only；全仓库第三个服务端外联点）。

把官方社区 App「塔吉多」的个人数据链路直连复刻进本面板（无任何第三方代理/网关，
用户凭证只落本机）。协议依据侦察实证 + 两份独立开源实现逐行核对（双源一致）：
- liangcka/astrbot_plugin_nanally core/client.py（NTEUID SDK 单文件移植）
- tyql688/NTEUID NTEUID/utils/sdk/{tajiduo,laohu}.py + tajiduo_model.py
登录链：老虎账号 SDK（user.laohu.com 手机号+短信验证码）→ 塔吉多用户中心
（bbs-api.tajiduo.com /usercenter/api/login，appId=10551）→ accessToken/refreshToken；
续期：POST /usercenter/api/refreshToken（authorization 传 refreshToken），令牌轮换。
会话恢复链（手动同步时自动，恰一次）：① refreshToken 续期 → ② 失败且有落盘的
laohu 根凭据（bind 时随会话一并落 data/tajiduo_token.json，绝不回传前端）→
同 device_id 重走 /usercenter/api/login 重建会话（NTEUID login_service.
refresh_user_token 同语义：会把服务端同 center_uid 的其它会话顶掉）→
③ 仍失败才抛友好重绑错误（官方原文进 raw）。
个人数据端点（均需 authorization 头）：/usercenter/api/v2/getGameRoles（游戏角色列表）、
/apihub/awapi/yh/roleHome（总览）、/apihub/awapi/yh/characters（逐角色面板：
属性/技能/城市技能/弧盘fork/套装）、/apihub/awapi/yh/gacha（抽卡分析：三池
S 级出货明细+每池窗口统计，无 query 服务端反查角色；details 只含已出 S，
官方窗口 180 天——本地 data/gacha_history.json 并集累积保留更早记录）。

安全边界（与 sites_fetch 同级防线，全为常量白名单，不接受前端传 URL）：
- 仅 https + 主机白名单 user.laohu.com / bbs-api.tajiduo.com（子域归一化，前缀/
  后缀伪装拒绝）；解析出的全部 IP 不得为私网/环回/链路本地/保留/组播/未指定段；
- 禁止跟随重定向（重定向目标无法预校验）；
- token/refreshToken/deviceId 只存 data/tajiduo_token.json（.gitignore 排除），
  绝不写入日志、绝不回传前端（前端只见绑定时间与脱敏 uid）。

运行边界：只被 main.py 的 /api/tajiduo/{send-captcha,bind,logout,sync,apply,
sync-gacha} 端点调用（手动按钮触发，绝无定时/自动同步；同步失败走恢复链）。
签名/ds/AES 为纯函数可离线自测（app/selftest.py 用 NIST 公开向量与观测值断言）。

已知风险（跟踪项）：appversion/ds salt 随官方 App 升级可能失效；验证码发送有冷却
（实测 2 分钟）；自动化调用受完美世界用户协议自动化条款约束——本模块仅手动触发、
低频（每次同步 3~4 个请求）并复用 refreshToken，不提供任何自动/定时入口。

版本常量观测：nanally=1.2.2、NTEUID=1.2.4、taygedo=1.2.5（多值并存说明服务端
接受区间），此处取 nanally（与侦察实证主值一致，最近验证）；失效时改此处即可。
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import uuid

from pathlib import Path

from . import data_dacl
from . import url_guard
from .state import (_replace_bounded_retry, DATA_DIR, _path_lock, load_json,
                    save_json_atomic)

# ---------- 常量白名单（URL 唯一来源；前端/请求体传入的 URL 一律不采信） ----------

ALLOWED_HOSTS = ("user.laohu.com", "bbs-api.tajiduo.com")

LAOHU_BASE_URL = "https://user.laohu.com"
TAJIDUO_BASE_URL = "https://bbs-api.tajiduo.com"

# 老虎账号 SDK（塔吉多 App 内置）；appKey 取后 16 字节作 AES-128-ECB 密钥
LAOHU_APP_ID = "10550"
LAOHU_APP_KEY = "89155cc4e8634ec5b1b6364013b23e3e"
LAOHU_SDK_VERSION = "4.273.0"
LAOHU_UA = "okhttp/4.9.0"
LAOHU_PACKAGE = "com.pwrd.htassistant"
LAOHU_VERSION_CODE = "12"

# 塔吉多用户中心 / apihub
TAJIDUO_APP_VERSION = "1.2.2"
TAJIDUO_DS_SALT = "pUds3dfMkl"
TAJIDUO_DS_NONCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
TAJIDUO_USER_CENTER_APP_ID = "10551"
TAJIDUO_UA = "okhttp/4.12.0"
TAJIDUO_CLIENT_UID = "0"  # 观测值：uid 头恒为 "0"（两份开源实现一致）

GAME_ID_YIHUAN = "1289"  # 异环（幻塔=1256，与本面板无关）
GAME_KEY_YIHUAN = "yh"

SEND_SMS_PATH = "/m/newApi/sendPhoneCaptchaWithOutLogin"
CHECK_SMS_PATH = "/m/newApi/checkPhoneCaptchaWithOutLogin"
SMS_LOGIN_PATH = "/openApi/sms/new/login"
USER_CENTER_LOGIN_PATH = "/usercenter/api/login"
REFRESH_TOKEN_PATH = "/usercenter/api/refreshToken"
GET_GAME_ROLES_PATH = "/usercenter/api/v2/getGameRoles"
GACHA_SUMMARY_PATH = "/apihub/awapi/yh/gacha"

TOKEN_FILE = os.path.join(DATA_DIR, "tajiduo_token.json")
GACHA_HISTORY_FILE = os.path.join(DATA_DIR, "gacha_history.json")

# B2.8-A（TJD-01/CONC-05）恢复链互斥锁：/api/tajiduo/sync 与 /api/tajiduo/
# sync-gacha 两路由均不持 main._TJD_LOCK（B2.8-A 用户裁定不在路由层补整段锁），
# 并发双 sync 同时进入 _recover_auth 时 refreshToken 一次性轮换互踩——两线程各
# 持同一旧 refreshToken 并发续期、后到者被服务端拒绝，且失败方升级 laohu 重登
# 会顶掉先到者会话（_relogin_via_laohu 语义），persist 写回交错可把输掉会话
# 落盘。模块级互斥锁把恢复链全程（refresh/relogin/persist/数据重试）串行化。
# 锁内网络请求有界：续期恰一次＋失败后 laohu 重登恰一次＝≤2 请求×20 秒
# （_http timeout=20），不长时间霸锁。锁序单向无环：main._TJD_LOCK（bind/
# logout/sync-gacha 路由）→ 本锁 _AUTH_LOCK（_recover_auth）→ state._path_lock
# （persist_session/clear_token_store 的 per-path 叶锁）；本锁内绝不回调 main 层。
_AUTH_LOCK = threading.Lock()

PHONE_RE = re.compile(r"^1\d{10}$")  # 大陆手机号（areaCodeId=1）
CAPTCHA_RE = re.compile(r"^\d{4,8}$")


class TajiduoError(Exception):
    """协议/网络错误；auth 标记供同步流程判定「续期一次后重试」。"""

    def __init__(self, message, raw=None, status_code=0, auth=False):
        super().__init__(message)
        self.raw = raw if isinstance(raw, dict) else {}
        self.status_code = status_code
        self.auth = auth or status_code in (401, 402, 403) or self._auth_keyword(message)

    @staticmethod
    def _auth_keyword(message):
        msg = str(message).lower()
        # touch/session：官方会话失效时的原始报文（如 "FAILED TO TOUCH"）——
        # 这类错误续期救不回，必须归入 auth 引导重新绑定
        return any(kw in msg for kw in ("登录", "token", "未授权", "令牌", "过期",
                                        "请先", "login", "invalid_token", "touch",
                                        "session"))


# ---------- 纯函数：签名 / ds / AES（selftest 用公开向量断言） ----------

def laohu_sign(params, app_key=LAOHU_APP_KEY):
    """老虎签名：参数按 key 排序拼接 value + appKey 后 MD5（观测算法，双源一致）。

    MD5 在此是官方协议规定的互操作算法（服务端按同规则校验），不是本面板的
    安全原语——不可单方面升级为 SHA-256，否则签名不被服务端接受。
    selftest 向量：{"appId":"10550","t":"1700000000","cellphone":"13800138000",
    "type":"16"} → c9c505637da493c8d4ecbad281e08b13。"""
    raw = "".join(str(params[k]) for k in sorted(params)) + app_key
    return hashlib.md5(raw.encode()).hexdigest()


def ds_sign(app_version=TAJIDUO_APP_VERSION, salt=TAJIDUO_DS_SALT, ts=None, nonce=None):
    """ds 头：md5(ts + nonce8 + appversion + salt)，格式 "ts,nonce,md5hex"。

    同上：拼接公式与 MD5 是官方客户端的固定算法（多份开源实现一致观测），
    服务端按它校验，本面板只做协议兼容复刻。
    ts/nonce 可注入（selftest 向量：ts=1700000000, nonce=AB12cd34 →
    791feba72a770fb7c1807b063a2d303f）。"""
    ts = str(int(time.time())) if ts is None else str(ts)
    # nonce 非安全敏感（仅防重放去重），仍用 secrets 以获得密码学安全随机源
    nonce = "".join(secrets.choice(TAJIDUO_DS_NONCE_ALPHABET) for _ in range(8)) \
        if nonce is None else nonce
    digest = hashlib.md5(f"{ts}{nonce}{app_version}{salt}".encode()).hexdigest()
    return f"{ts},{nonce},{digest}"


# ----- AES-128-ECB（仅加密；纯标准库实现。老虎登录需 AES 加密手机号/验证码，
# 而本面板约束为零第三方依赖（selftest/build_exe 口径），故内嵌最小实现。
# 正确性由 FIPS-197 与 NIST SP800-38A 公开测试向量断言（selftest）。）-----

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _aes128_expand_key(key):
    """AES-128 密钥扩展（FIPS-197 §5.2）：16 字节 → 44 个 32 位字。"""
    if len(key) != 16:
        raise ValueError("AES-128 密钥必须 16 字节")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:  # RotWord + SubWord + Rcon
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return w


def _aes128_encrypt_block(block, w):
    """AES-128 单块加密（FIPS-197 §5.1）：16 字节 → 16 字节。"""
    state = [list(block[i::4]) for i in range(4)]  # 列主序

    def add_round_key(rnd):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[rnd * 4 + c][r]

    def sub_shift():
        for r in range(4):
            row = [_SBOX[state[r][(c + r) % 4]] for c in range(4)]
            state[r] = row

    def mix_columns():
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = _xtime(a[0]) ^ (_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2]) ^ a[2]) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3]) ^ a[3])
            state[3][c] = (_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])

    add_round_key(0)
    for rnd in range(1, 10):
        sub_shift()
        mix_columns()
        add_round_key(rnd)
    sub_shift()
    add_round_key(10)
    return bytes(state[r][c] for c in range(4) for r in range(4))


def _pkcs7_pad(data):
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def aes128_ecb_encrypt(plaintext, key):
    """AES-128-ECB + PKCS7 加密（base64 由调用方处理）。"""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    if isinstance(key, str):
        key = key.encode("utf-8")
    w = _aes128_expand_key(key)
    padded = _pkcs7_pad(plaintext)
    return b"".join(_aes128_encrypt_block(padded[i:i + 16], w)
                    for i in range(0, len(padded), 16))


def laohu_aes_encrypt(plain, app_key=LAOHU_APP_KEY):
    """老虎登录字段加密：AES-128-ECB(appKey 后 16 字节) + PKCS7 + base64。"""
    import base64
    return base64.b64encode(aes128_ecb_encrypt(plain, app_key[-16:])).decode()


# ---------- SSRF 防线（https + 主机白名单 + 私网 IP 阻断 + 禁重定向） ----------
# 2026-11-14 B2.5/XMOD-01：防线唯一实现收敛进 app/url_guard.py；本模块只保留两域
# 常量白名单与薄包装。白名单口径收紧为精确相等（allow_subdomains=False，policy
# §2.2：全部出站 URL 仅由两 BASE 常量拼装、无子域业务，endswith 子域规则移除）；
# 凭据只走请求头不走 URL 的口径不变（本通道凭据外泄面大于 A/B 匿名通道）。

def host_allowed(hostname):
    """主机归一化后是否在白名单：严格等于允许域（2026-11-14 起不再放行子域）。
    前缀伪装（evil-user.laohu.com）、后缀伪装（user.laohu.com.evil.com）与子域
    形态（www.bbs-api.tajiduo.com）都不匹配。"""
    return url_guard.host_allowed(hostname, allowed_hosts=ALLOWED_HOSTS,
                                  allow_subdomains=False)


def _validate_url(url):
    """委托共享层（返回 .url 字符串，保持既有返回类型）：仅允许 https + 白名单
    主机（精确相等）+ 443 端口 + 无用户信息；解析出的全部 IP 都不得落在私网/
    环回/链路本地/保留/组播/未指定段，空解析一律拒绝。"""
    return url_guard.validate_url(url, allowed_hosts=ALLOWED_HOSTS,
                                  allow_subdomains=False, action="请求").url


_NoRedirect = url_guard._NoRedirect
_OPENER = url_guard.OPENER


# B2.7A（FETCH-08）HTTPError 异常体读取上限（字节）：与 FETCH-06 正常响应体
# read(MAX+1) 有界读（B2.6B，url_guard.py MAX_RESPONSE_BYTES 收敛面）同族异位——
# 异常体读点在 _wrap_http_error 本地、不在该收敛面内。展示仅需前 200 字节，
# 读取同样必须有界：读 MAX+1（+1 为溢出探测惯用法）后只留 MAX，绝不全量读入
# 内存（B2.7A 取证轮实测 33MiB 错误体曾被 e.read() 全量消费，34,603,008 字节）。
ERROR_BODY_MAX_BYTES = 200


def _wrap_http_error(e, url):
    """urllib.error.HTTPError → TajiduoError（2026-11-09 会话恢复可达修复）：
    此前 HTTP 401 原生异常击穿 do_POST（HTTPError 是 OSError 子类，sync 端点
    兜成 500），三级恢复链 _recover_auth 对 HTTP 层 401 完全不可达（死代码）。
    401/402/403 → auth=True（TajiduoError 构造器按 status_code 归 auth），
    恢复链①续期→②laohu 重登→③友好重绑从此自然可达；其余状态码包装为带
    路径的非 auth 错误。响应体有界读（B2.7A FETCH-08）：只读
    ERROR_BODY_MAX_BYTES+1 字节后切片前 200 字节再 decode——展示与旧实现
    完全一致（字节 0..199），但超大错误体不再全量读入内存；finally e.close()
    缩短 fp 存活窗口（HTTPError 为上下文管理器，关不上按尽力而为不掩盖主错）。
    官方错误原文不透传给用户，由调用方的友好文案兜底。"""
    path = urllib.parse.urlsplit(url).path or "/"
    try:
        body = e.read(ERROR_BODY_MAX_BYTES + 1)[:ERROR_BODY_MAX_BYTES] \
            .decode("utf-8", errors="replace")
    except (OSError, ValueError, AttributeError):
        body = ""  # fp 缺失/已消费等极端形态：诊断体拿不到就置空，错误本身照抛
    finally:
        try:
            e.close()
        except (OSError, ValueError, AttributeError):
            pass
    return TajiduoError(f"接口 HTTP {e.code}（{path}）",
                        raw={"body": body} if body else None,
                        status_code=int(e.code))


def _http(url, *, method="GET", headers=None, data=None, timeout=20):
    """单次 HTTP（bytes 进出；URL/响应端点由本模块常量决定，前端无法指定）。
    传输委托共享层 open_pinned（校验 + IP 钉扎 + 禁重定向）；HTTPError 就地转
    TajiduoError（_wrap_http_error：401/402/403 标 auth，恢复链可达）；URLError
    等其余网络错误仍按 OSError 语义上抛。"""
    try:
        return url_guard.open_pinned(
            url, timeout=timeout, opener=_OPENER, allowed_hosts=ALLOWED_HOSTS,
            allow_subdomains=False, action="请求", method=method,
            headers=headers or {}, data=data)
    except urllib.error.HTTPError as e:
        raise _wrap_http_error(e, url) from e


def _form(body):
    return urllib.parse.urlencode(body).encode("utf-8")


# ---------- 老虎账号 SDK（user.laohu.com；信封 code/result，签名 POST） ----------

class LaohuDevice:
    """伪装机型（观测自开源实现；deviceId 生成后持久复用，避免风控画像漂移）。"""

    def __init__(self, device_id=""):
        self.device_id = device_id or ("HT" + uuid.uuid4().hex[:14].upper())
        self.device_type = "Pixel 6"
        self.device_model = "Pixel 6"
        self.device_name = "Pixel 6"
        self.device_sys = "Android 14"
        self.adm = self.device_id
        self.imei = ""
        self.idfa = ""
        self.mac = ""

    def to_dict(self):
        return {"device_id": self.device_id, "device_type": self.device_type,
                "device_model": self.device_model, "device_name": self.device_name,
                "device_sys": self.device_sys}

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}
        dev = cls(str(data.get("device_id") or ""))
        for attr in ("device_type", "device_model", "device_name", "device_sys"):
            value = str(data.get(attr) or "").strip()
            if value:
                setattr(dev, attr, value[:64])
        dev.adm = dev.device_id
        return dev


class LaohuClient:
    """老虎账号 SDK 短信登录（send → check → login；POST form + sign 字段）。

    响应信封 {code, message, result}；code!=0 抛 TajiduoError。"""

    def __init__(self, device=None, app_id=LAOHU_APP_ID, app_key=LAOHU_APP_KEY):
        self.device = device or LaohuDevice()
        self.app_id = str(app_id)
        self.app_key = app_key

    def _common_fields(self, use_millis):
        dev = self.device
        ts = int(time.time() * 1000) if use_millis else int(time.time())
        base = {
            "appId": self.app_id,
            "channelId": "1",
            "deviceId": dev.device_id,
            "deviceType": dev.device_type,
            "deviceModel": dev.device_model,
            "deviceName": dev.device_name,
            "deviceSys": dev.device_sys,
            "adm": dev.adm,
            "idfa": dev.idfa,
            "sdkVersion": LAOHU_SDK_VERSION,
            "bid": LAOHU_PACKAGE,
            "t": str(ts),
        }
        if use_millis:
            base["version"] = LAOHU_VERSION_CODE
            base["mac"] = dev.mac
        else:
            base["versionCode"] = LAOHU_VERSION_CODE
            base["imei"] = dev.imei
        return base

    def _submit(self, path, params, keep_empty=False):
        signed = dict(params)
        signed["sign"] = laohu_sign(signed, self.app_key)
        # 空串字段剔除（keep_empty 时保留——sms/new/login 观测要求保留空值参与签名）
        cleaned = {k: v for k, v in signed.items()
                   if v is not None and (keep_empty or v != "")}
        url = LAOHU_BASE_URL + path
        raw = _http(url, method="POST", data=_form(cleaned), headers={
            "User-Agent": LAOHU_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        return self._extract(path, raw, "result")

    @staticmethod
    def _extract(path, raw, data_key):
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            raise TajiduoError(f"[{path}] 响应不是 JSON", status_code=0)
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
            message = payload.get("message") if isinstance(payload, dict) else str(payload)[:120]
            raise TajiduoError(f"[{path}] {message or '接口返回异常'}", payload)
        data = payload.get(data_key)
        return data if data is not None else {}

    def send_sms_code(self, cellphone, area_code_id="1", sms_type=16):
        """发短信验证码（冷却/风控由服务端返回，错误消息原样透出）。"""
        params = self._common_fields(use_millis=False)
        params["cellphone"] = cellphone
        params["areaCodeId"] = area_code_id
        params["type"] = str(sms_type)
        return self._submit(SEND_SMS_PATH, params)

    def check_sms_code(self, cellphone, captcha):
        params = self._common_fields(use_millis=False)
        params["cellphone"] = cellphone
        params["captcha"] = captcha
        return self._submit(CHECK_SMS_PATH, params)

    def login_by_sms(self, cellphone, captcha, area_code_id="1", sms_type=16):
        """验证码登录：cellphone/captcha AES-128-ECB 加密；返回 {userId, token}。"""
        self.check_sms_code(cellphone, captcha)
        params = self._common_fields(use_millis=True)
        params["cellphone"] = laohu_aes_encrypt(cellphone, self.app_key)
        params["captcha"] = laohu_aes_encrypt(captcha, self.app_key)
        params["areaCodeId"] = area_code_id
        params["type"] = str(sms_type)
        result = self._submit(SMS_LOGIN_PATH, params, keep_empty=True)
        user_id, token = result.get("userId"), result.get("token")
        if user_id is None or token is None or not str(token):
            raise TajiduoError("老虎登录返回缺少 userId/token", result)
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            raise TajiduoError("老虎登录返回 userId 格式错误", result)
        if user_id <= 0:
            raise TajiduoError("老虎登录返回 userId 无效", result)
        return {"user_id": user_id, "token": str(token)}


# ---------- 塔吉多用户中心 + 个人数据（bbs-api.tajiduo.com；信封 code/data） ----------

class TajiduoClient:
    """塔吉多用户中心会话（headers: authorization/uid/deviceid/appversion/platform/ds）。"""

    def __init__(self, device_id, access_token="", refresh_token="", center_uid=""):
        if not device_id:
            raise ValueError("device_id 不能为空")
        self.device_id = device_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.center_uid = str(center_uid)

    def _headers(self, token=""):
        headers = {
            "User-Agent": TAJIDUO_UA,
            "platform": "android",
            "deviceid": self.device_id,
            "appversion": TAJIDUO_APP_VERSION,
            "uid": TAJIDUO_CLIENT_UID,
            "ds": ds_sign(),
        }
        if token:
            headers["authorization"] = token
        return headers

    def _request(self, path, *, method="GET", body=None, query=None, token=""):
        url = TAJIDUO_BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        raw = _http(url, method=method, data=_form(body) if body else None,
                    headers=self._headers(token) | (
                        {"Content-Type": "application/x-www-form-urlencoded"}
                        if body else {}))
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            raise TajiduoError(f"[{path}] 响应不是 JSON")
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
            message = payload.get("msg") or payload.get("message") if isinstance(payload, dict) \
                else str(payload)[:120]
            raise TajiduoError(f"[{path}] {message or '接口返回异常'}", payload)
        data = payload.get("data")
        return data if data is not None else {}

    def user_center_login(self, laohu_token, laohu_user_id):
        """老虎 token 换塔吉多会话：POST /usercenter/api/login（appId=10551）。"""
        data = self._request(USER_CENTER_LOGIN_PATH, method="POST", body={
            "token": laohu_token,
            "userIdentity": str(laohu_user_id),
            "appId": TAJIDUO_USER_CENTER_APP_ID,
        })
        access = data.get("accessToken")
        refresh = data.get("refreshToken")
        uid = data.get("uid")
        if not access or not refresh or uid is None:
            raise TajiduoError("塔吉多登录返回缺少 accessToken/refreshToken/uid", data)
        self.access_token, self.refresh_token = str(access), str(refresh)
        self.center_uid = str(uid)
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "center_uid": self.center_uid}

    def refresh_session(self):
        """续期并轮换双 token（authorization 传 refreshToken）。"""
        if not self.refresh_token:
            raise TajiduoError("refresh_token 为空，无法续期", auth=True)
        data = self._request(REFRESH_TOKEN_PATH, method="POST",
                             token=self.refresh_token)
        access = data.get("accessToken")
        refresh = data.get("refreshToken")
        if not access or not refresh:
            raise TajiduoError("塔吉多 refreshToken 未下发 accessToken/refreshToken", data,
                               auth=True)
        self.access_token, self.refresh_token = str(access), str(refresh)
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "center_uid": self.center_uid}

    def get_game_roles(self, game_id=GAME_ID_YIHUAN):
        """全部游戏角色：GET /usercenter/api/v2/getGameRoles?gameId=。

        v2 返回 {bindRole, roles:[…]}，旧版可能直接返回数组（双兼容）。"""
        data = self._request(GET_GAME_ROLES_PATH, query={"gameId": game_id},
                             token=self.access_token)
        roles = data if isinstance(data, list) else (data.get("roles") or [])
        out = []
        for r in roles:
            if not isinstance(r, dict):
                continue
            out.append({
                "roleId": str(r.get("roleId", r.get("role_id", "")) or ""),
                "roleName": str(r.get("roleName", r.get("role_name", "")) or ""),
                "serverName": str(r.get("serverName", r.get("server_name", "")) or ""),
                "lev": r.get("lev") if isinstance(r.get("lev"), int) else None,
            })
        return [r for r in out if r["roleId"]]

    def get_role_home(self, role_id):
        return self._request(f"/apihub/awapi/{GAME_KEY_YIHUAN}/roleHome",
                             query={"roleId": role_id}, token=self.access_token)

    def get_role_characters(self, role_id):
        return self._request(f"/apihub/awapi/{GAME_KEY_YIHUAN}/characters",
                             query={"roleId": role_id}, token=self.access_token)

    def get_game_record_card(self):
        """战绩卡列表（bindRoleInfo 含 roleId/roleName/lev/serverName）。"""
        data = self._request("/apihub/api/getGameRecordCard", query={"uid": self.center_uid},
                             token=self.access_token)
        return data if isinstance(data, list) else []

    def get_gacha_summary(self):
        """抽卡分析：GET /apihub/awapi/yh/gacha（无 query 参数——服务端凭
        authorization=accessToken 反查 user→role，头与本类其余端点完全一致）。

        实现依据 tyql688/NTEUID utils/sdk/tajiduo.py get_gacha_summary()
        （WebFetch 核对：同域同签名体系唯一 gacha 端点），复用本类 _request
        （含 ds/续期链）。返回 data 段原始 dict，解析交给 parse_gacha_summary。"""
        return self._request(GACHA_SUMMARY_PATH, token=self.access_token)


# ---------- 响应解析（纯函数；按 payload 实际字段防御性解析，缺失不编造） ----------

# 四技能实名锚点（name fallback 用）：payload 技能名实测是个性化名（2026-11-07 用户
# 实测四技能按 UI 分类标签全部落空），故 name 锚点降为次选。
SKILL_ANCHORS = (("普通攻击", "normal"), ("变轨技能", "variable"),
                 ("极轨终结", "orbit"), ("援护技", "assist"))

# skills[].type 判别（首选）：2026-11-07 WebFetch 核对 tyql688/NTEUID tajiduo_model.py
# —— CharacterSkill.type 为无文档 str（无公开枚举语义），故下表候选值是【推断】；
# 判别顺序：type 直配本表 → name 实名锚点 → 列表顺序兜底（见 parse_characters）。
SKILL_TYPE_CANDIDATES = {
    "normal": ("normal", "normalattack", "普通攻击"),
    "variable": ("variable", "variableskill", "变轨技能"),
    "orbit": ("orbit", "orbitultimate", "极轨终结"),
    "assist": ("assist", "assistskill", "援护技"),
}

# 顺序兜底的实证依据（仍属推断，返回行标注「顺序推断」）：TaJiDuo-plugin
# apps/profile.js renderPanelSkills 的展示名兜底即按列表顺序
# ['普通攻击','变轨技能','极轨终结','援护技','被动技能','被动技能']——前 4 位与本表一致。
SKILL_ORDER_KEYS = ("normal", "variable", "orbit", "assist")

LAST_PAYLOAD_FILE = os.path.join(DATA_DIR, "tajiduo_last_payload.json")


def _as_int(value, lo=None, hi=None):
    """防御性取整：int/纯数字串 → int；其余（None/空串/浮点噪声）→ None。
    塔吉多字段实测量测：CharacterFork.alev/blev/slev 是字符串，CharacterDetail.alev 是 int。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        out = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        out = int(value.strip())
    else:
        return None
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def parse_role_home(payload):
    """roleHome → 总览摘要（缺失字段一律 None/[]，绝不编造）。"""
    data = payload if isinstance(payload, dict) else {}
    achieve = data.get("achieveProgress") if isinstance(data.get("achieveProgress"), dict) else {}
    return {
        "roleid": str(data.get("roleid") or ""),
        "rolename": str(data.get("rolename") or ""),
        "servername": str(data.get("servername") or ""),
        "lev": _as_int(data.get("lev")),
        "worldlevel": _as_int(data.get("worldlevel")),
        "charid_cnt": _as_int(data.get("charidCnt")),
        "rolelogin_days": _as_int(data.get("roleloginDays")),
        "achievement": _as_int(achieve.get("achievementCnt")),
        "achievement_total": _as_int(achieve.get("total")),
    }


def parse_gacha_summary(payload):
    """gacha 端点 payload → 抽卡分析汇总（纯函数，selftest 覆盖；缺字段 None/[] 不编造）。

    字段实证（2026-11-07 WebFetch tyql688/NTEUID tajiduo_model.py，逐字段）：
    - 顶层 TajiduoGachaSummary：roleid(游戏UID)/rolename/lev/userid('9_'+老虎ID)/
      luckType(玄学评分 1-12)/luckTitle(玄学称号)/gachaDetails[池列表]；
    - 每池 TajiduoGachaPool：tab(限定卡池/常驻卡池/弧盘池)/m(软保底：角色池 90/
      弧盘池 60)/drawCount(该池窗口内总抽数)/rareCount(S 出货数)/average(平均出 S
      抽数字符串)/playerOver(战胜玩家百分位如 '45%')/details[]；
    - 每发 S 明细 TajiduoGachaDetail：charid(角色数字 ID 或 fork_* 武器 ID)/
      rareCount(本发 S 的保底计数=第几抽出)/luckyType(0-3 单抽运气)/
      time(YYYY-MM-DD)/timeStamp(毫秒时间戳)。
    【关键限制】details 只含已出 S——垫抽数不返回（模型 docstring 明示），
    非 S 逐抽流水不存在；官方窗口仅 180 天（靠本地累积合并保留更早数据）。"""
    data = payload if isinstance(payload, dict) else {}
    pools = []
    for pool in data.get("gachaDetails") or []:
        if not isinstance(pool, dict):
            continue
        details = []
        for det in pool.get("details") or []:
            if not isinstance(det, dict):
                continue
            details.append({
                "charid": str(det.get("charid") or "").strip(),
                "pity": _as_int(det.get("rareCount")),
                "lucky_type": _as_int(det.get("luckyType"), 0, 3),
                "time": str(det.get("time") or "").strip(),
                "time_ms": _as_int(det.get("timeStamp")),
            })
        pools.append({
            "tab": str(pool.get("tab") or "").strip(),
            "m": _as_int(pool.get("m")),
            "draw_count": _as_int(pool.get("drawCount")),
            "rare_count": _as_int(pool.get("rareCount")),
            "average": str(pool.get("average") or "").strip(),
            "player_over": str(pool.get("playerOver") or "").strip(),
            "details": details,
        })
    return {
        "roleid": str(data.get("roleid") or "").strip(),
        "rolename": str(data.get("rolename") or "").strip(),
        "lev": _as_int(data.get("lev")),
        "userid": str(data.get("userid") or "").strip(),
        "luck_type": _as_int(data.get("luckType")),
        "luck_title": str(data.get("luckTitle") or "").strip(),
        "pools": pools,
    }


def _parse_skill_type(skill):
    """skills[].type 判别（首选，候选值表为推断——见 SKILL_TYPE_CANDIDATES 注释）：
    type 归一化后含候选子串即映射对应键；判不出返回 None（交由 name/顺序兜底）。"""
    tval = _ocr_like_norm(str(skill.get("type") or ""))
    if not tval:
        return None
    for key, candidates in SKILL_TYPE_CANDIDATES.items():
        if any(cand in tval for cand in candidates):
            return key
    return None


def _ocr_like_norm(s):
    """技能 type/名 匹配归一化：去空白、去「」、小写（type 候选值大小写未知，推断）。"""
    return re.sub(r"\s+", "", s).replace("「", "").replace("」", "").lower()


def _parse_cassette(item):
    """suit/cartridge 字段 → 卡带摘要（键名实证自 NTEUID tajiduo_model.CharacterSuit
    【name/core/pie/mainProperties/properties】；cartridge 字段仅按任务口径防御性探测，
    实际键以 data/tajiduo_last_payload.json 落盘的真实 payload 为准，未证实前视为推断）。

    返回 {set, main, affixes, activate_num}；缺字段一律 None/[]，绝不编造。affixes
    仅用于预览展示（state 无对应落库字段，stigma_groups 用户自维护绝不写）。"""
    suit = item.get("suit") if isinstance(item.get("suit"), dict) else {}
    cartridge = item.get("cartridge") if isinstance(item.get("cartridge"), dict) else {}
    source = suit or cartridge  # 两个来源同一形状处理；suit 为模型实证键
    if not source:
        return {"set": None, "main": None, "affixes": [], "activate_num": None}
    # 装备位条目（core/pie 列表）：主属性取首位 main_properties 的名，词条取 properties 名
    pieces = [p for p in (source.get("core") or []) + (source.get("pie") or [])
              if isinstance(p, dict)]
    main = None
    affixes = []
    for piece in pieces:
        if main is None:
            mp = piece.get("mainProperties") if isinstance(piece.get("mainProperties"), list) else []
            for prop in mp:
                if isinstance(prop, dict) and str(prop.get("name") or "").strip():
                    main = str(prop["name"]).strip()
                    break
        for prop in piece.get("properties") or []:
            if isinstance(prop, dict) and str(prop.get("name") or "").strip():
                name = str(prop["name"]).strip()
                if name not in affixes:
                    affixes.append(name)
    activate = source.get("suitActivateNum")
    return {
        "set": str(source.get("name") or "").strip() or None,
        "main": main,
        "affixes": affixes[:8],
        "activate_num": _as_int(activate),
    }


def parse_characters(payload):
    """characters 数组 → 逐角色规整行（纯函数，selftest 覆盖）。

    行结构：{id, name, level(alev), awakening(awakenLev), slev(混频),
    skills:{normal/variable/orbit/assist}, skills_inferred(顺序推断标记),
    city_skills:[实名…], fork:{id,name,level(alev 字串转 int)}, suit_num,
    cassette:{set,main,affixes,activate_num}}。
    四技能判别顺序（2026-11-07 修复）：skills[].type（推断候选表）→ name 实名锚点 →
    仍未判满时按列表顺序映射 normal→variable→orbit→assist 并置 skills_inferred=True。
    payload 里没有的材料库存类字段不存在对应解析（调用方如实标注「无此数据」）。"""
    items = payload if isinstance(payload, list) else []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        skills = {key: None for key in SKILL_ORDER_KEYS}
        pool = []  # 判不出归属的技能（保序，供顺序兜底）
        for skill in item.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            key = _parse_skill_type(skill)
            if key is None or skills[key] is not None:
                name = str(skill.get("name") or "")
                for anchor, akey in SKILL_ANCHORS:  # name 锚点 fallback
                    if anchor in name and skills[akey] is None:
                        key = akey
                        break
            if key is not None and skills.get(key) is None:
                skills[key] = _as_int(skill.get("level"), 0, 99)
            else:
                pool.append(skill)
        skills_inferred = False
        if any(v is None for v in skills.values()) and pool:
            # 顺序兜底（推断，返回行标注「顺序推断」）：保序把未判技能填进空槽
            empty = [k for k in SKILL_ORDER_KEYS if skills[k] is None]
            for key, skill in zip(empty, pool):
                skills[key] = _as_int(skill.get("level"), 0, 99)
                skills_inferred = True
        city_skills = [str(s.get("name") or "").strip()
                       for s in item.get("citySkills") or [] if isinstance(s, dict)]
        fork = item.get("fork") if isinstance(item.get("fork"), dict) else {}
        rows.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "").strip(),
            "level": _as_int(item.get("alev"), 0, 999),
            "awakening": _as_int(item.get("awakenLev"), 0, 6),
            "slev": _as_int(item.get("slev"), 0, 99),
            "skills": skills,
            "skills_inferred": skills_inferred,
            "city_skills": [n for n in city_skills if n][:4],
            "fork": {
                "id": str(fork.get("id") or ""),
                "name": str(fork.get("name") or "").strip() or None,
                "level": _as_int(fork.get("alev"), 0, 999),
            },
            "suit_num": _as_int((item.get("suit") or {}).get("suitActivateNum")
                                if isinstance(item.get("suit"), dict) else None),
            "cassette": _parse_cassette(item),
        })
    return rows


# ---------- 凭证存取（data/tajiduo_token.json；绝不回传前端/写日志） ----------

def load_token_store():
    """读取本机凭证（损坏/缺失返回 None）。"""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("access_token") \
            or not data.get("refresh_token") or not data.get("device_id"):
        return None
    return data


def save_token_store(store):
    """原子落盘凭证（复用 state.save_json_atomic 的 tmp→bak→replace 链）。

    TJD-04：写盘前 require_private 闸（打包态目标目录 DACL 私有化失败 →
    DataDaclError，继承 OSError——沿 /api/tajiduo/bind 端点既有 except OSError
    →500「凭证保存失败」映射上抛为可见错误），绝不静默落凭据。"""
    data_dacl.require_private(os.path.dirname(TOKEN_FILE))
    store = dict(store)
    store["schema"] = 1
    save_json_atomic(TOKEN_FILE, store)


# B2.7A（TJD-09）uuid 双形态残留匹配（严格收窄，不过宽 glob）：仅 save_json_atomic
# 崩溃/硬杀残留的两形态（命名见 state.py:147 主 tmp / :154 bak tmp；其 finally
# 自清 state.py:165-178 只覆盖「本次写者存活至 finally」形态，进程硬杀/崩溃残留
# 超出该防线）——basename 前缀精确匹配「tajiduo_token.json.」，uuid 段收窄为
# 32 位 hex；gacha_history/last_payload/state 等同形态他人 tmp 一概不命中。
_TOKEN_UUID_TMP_RE = re.compile(
    r"^tajiduo_token\.json(?:\.bak)?\.[0-9a-f]{32}\.tmp$")


def clear_token_store():
    """解绑：删除凭证文件（.bak 一并，防半截残留被 load_json 链路捡回）。

    B2.7A（TJD-09）uuid 双形态残留清理：save_json_atomic 的主 tmp
    tajiduo_token.json.<32位hex>.tmp 与 bak tmp tajiduo_token.json.bak.<32位hex>.tmp
    （state.py:147/:154）在进程崩溃/硬杀时会残留明文凭证，既有 finally 自清
    只覆盖「本次写者存活至 finally」形态——清理面按 basename 前缀精确匹配
    ＋32 位 hex 收窄为上述两形态（不过宽 glob：同名形他人 tmp 一概不碰）；
    固定名三件套（本体/.bak/旧 .tmp 死路径兼容）照删。
    全程持 state._path_lock(TOKEN_FILE)：与并发 save_json_atomic(TOKEN_FILE)
    的「tmp 写全→.bak 复制→replace 发布」整序列互斥——活跃写者刚创建、尚在
    写入中的 uuid tmp 绝不会被本轮清理误删（锁序＝调用方 _TJD_LOCK 业务锁→
    path 叶锁，与 bind 路径 main.py _TJD_LOCK→save_token_store 单向序一致）。
    逐文件删除失败沿用既有 except OSError: pass 尽力而为口径：单个 unlink
    失败不中断其余清理，解绑主流程照常完成（logout 不 500 的前提）。
    """
    token_dir = os.path.dirname(TOKEN_FILE) or "."
    with _path_lock(TOKEN_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                os.remove(TOKEN_FILE + suffix)
            except OSError:
                pass
        try:
            names = os.listdir(token_dir)
        except OSError:
            names = []  # 目录不可读等极端形态：uuid 残留清扫放弃，固定名已尽力
        for name in names:
            if _TOKEN_UUID_TMP_RE.fullmatch(name):
                try:
                    os.remove(os.path.join(token_dir, name))
                except OSError:
                    pass


def build_client():
    """从本机凭证构建会话客户端（未绑定返回 None）。"""
    store = load_token_store()
    if not store:
        return None
    return TajiduoClient(str(store.get("device_id")),
                         access_token=str(store.get("access_token")),
                         refresh_token=str(store.get("refresh_token")),
                         center_uid=str(store.get("center_uid") or ""))


def _write_token_store_locked(store):
    """（persist_session 临界区专用）持锁版原子发布：调用方必须已持
    state._path_lock(TOKEN_FILE)——threading.Lock 不可重入，持锁序列内不能再经
    save_token_store→save_json_atomic 落盘（同线程二次取锁即死锁），故内联同一
    发布链、仅省去其自取的 per-path 锁。其余逐行复刻 save_json_atomic
    （state.py:177-208）：tmp 写全 → 复制当前主文件为 .bak → replace 发布；
    发布步沿用 state._replace_bounded_retry（BIZ-03 白名单 WinError 5/32 有界
    重试，耗尽原样外抛）；.bak 复制/发布失败吞掉（备份落后一版可接受）；
    finally 只清本次写者自己的 tmp。tmp/bak tmp 命名与 save_json_atomic 同形
    （<name>.<uuid4.hex>.tmp / <name>.bak.<uuid4.hex>.tmp）——崩溃/硬杀残留仍落
    clear_token_store 的 _TOKEN_UUID_TMP_RE（TJD-09）精确清理口径内。"""
    # TJD-04：与 save_token_store 同款写盘前闸（persist_session 临界区内首次调用
    # 才可能触发懒加固，有 _LAST_HARDEN 缓存后零开销）；失败 DataDaclError 沿
    # sync/sync-gacha 端点既有 except OSError 映射上抛为可见错误。
    data_dacl.require_private(os.path.dirname(TOKEN_FILE))
    tmp = Path(str(TOKEN_FILE) + f".{uuid.uuid4().hex}.tmp")
    bak_tmp = None
    try:
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        if os.path.exists(TOKEN_FILE):
            bak_tmp = Path(str(TOKEN_FILE) + f".bak.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(TOKEN_FILE, bak_tmp)
                _replace_bounded_retry(bak_tmp, TOKEN_FILE + ".bak")
                bak_tmp = None  # 已被 replace 消费
            except OSError:
                pass  # .bak 落后一版可接受；绝不因备份失败丢掉本次写入
        _replace_bounded_retry(tmp, TOKEN_FILE)
        tmp = None  # 已被 replace 消费
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        if bak_tmp is not None:
            try:
                bak_tmp.unlink()
            except OSError:
                pass


def persist_session(session, device_id, bound_at=None):
    """把（登录或续期后的）会话写回本机凭证文件（写回边界守卫，B2.8-A/TJD-01）。

    B2.8-A（TJD-01，2026-11-17 用户裁定方案）：全程持 state._path_lock(TOKEN_FILE)
    ——load→update→save 全序列与并发 clear_token_store（logout 路由同 per-path 锁）
    互斥；临界区入口（＝写回前检查：删除者需同锁，临界区内文件不可能消失）检查
    TOKEN_FILE 是否仍存在——不存在＝并发 logout 已删（或从未绑定），跳过落盘并
    抛 auth 类 TajiduoError（「凭据已删除」口径），sync 链据此得到 needs_rebind
    重绑引导；绝不新建文件（修复前 load→save 窄窗与文件缺失双形态会把已删除的
    旧凭据整体复活，登记 audit/2026-11-12/BUG-REGISTER.md:43 TJD-01）。
    实现注记：save_json_atomic 自取 per-path 锁（state.py:179-180）而
    threading.Lock 不可重入，持锁序列内不能再走 save_token_store→save_json_atomic
    （同线程二次取锁死锁）——临界区内改经 _write_token_store_locked 内联同一
    tmp→bak→replace 发布链（仅省去重复取锁，BIZ-03/TJD-09 语义不变）。bind 首绑
    创建文件走 save_token_store（无 gate，main.py:2511），不受本守卫影响。
    """
    with _path_lock(TOKEN_FILE):
        if not os.path.exists(TOKEN_FILE):
            raise TajiduoError("塔吉多本机凭据已删除，请重新绑定", auth=True)
        store = load_token_store() or {}
        store.update({
            "device_id": str(device_id),
            "access_token": str(session["access_token"]),
            "refresh_token": str(session["refresh_token"]),
            "center_uid": str(session.get("center_uid") or store.get("center_uid") or ""),
            "bound_at": bound_at or store.get("bound_at") or "",
            "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        store["schema"] = 1
        _write_token_store_locked(store)


def _relogin_via_laohu():
    """会话恢复链第②级：refresh_token 死后，用落盘的 laohu 根凭据重建塔吉多会话。

    语义核对（2026-11-08 WebFetch tyql688/NTEUID nte_login/login_service.py
    refresh_user_token）：laohu 根凭据【不回】user.laohu.com 刷新，而是直接作为
    /usercenter/api/login 的 token/userIdentity 重放换取新会话——NTEUID 同样只以
    同一持久化 dev_code 构造 TajiduoClient（其 laohu accessToken 登录路径亦然），
    不构造 LaohuClient、不触 laohu 域。laohu_token 比 refreshToken 活得久，
    refreshToken 一次性轮换死后由此续命；副作用：会把服务端同 center_uid 的
    其它会话顶掉。device_id 复用 token store 持久化值（不新生成，避免风控画像
    漂移）。成功即落盘并返回新会话 dict；token 文件无 laohu 凭据（旧版文件，
    向后兼容走现有链）返回 None；重登失败抛 TajiduoError。"""
    store = load_token_store() or {}
    laohu_token = str(store.get("laohu_token") or "")
    laohu_user_id = str(store.get("laohu_user_id") or "")
    device_id = str(store.get("device_id") or "")
    if not laohu_token or not laohu_user_id or not device_id:
        return None
    fresh = TajiduoClient(device_id)  # 同一 device_id（NTEUID refresh_user_token 同构）
    session = fresh.user_center_login(laohu_token, laohu_user_id)
    persist_session(session, device_id)
    return session


def _recover_auth(client, first_error, retry):
    """sync_all/sync_gacha 共用的三级恢复链（每次同步恰恢复一次，不递归）：
    ① refreshToken 续期恰一次，成功即落盘并重试（重试仍失败按现状原样上抛）；
    ② 续期失败且有落盘 laohu 根凭据 → 同 device_id 重走 user_center_login 重建
      会话并落盘（见 _relogin_via_laohu）后重试恰一次；
    ③ 仍失败抛现有友好重绑错误——官方原文进 raw 供诊断，绝不透传给用户；
      混合路径例外（TJD-02/B3-B）：laohu 重登成功后重试仍抛非 auth 错误（如
      接口 HTTP 500）时原样上抛，保留 status_code/raw 与原始消息（HTTP 层据此
      回 502 而非 401，不误导用户重绑账号）。

    B2.8-A（TJD-01/CONC-05 恢复链互斥）：全程持模块级 _AUTH_LOCK——并发双 sync
    （sync/sync-gacha 路由均不持 main._TJD_LOCK，用户裁定不在路由层补整段锁）
    同时进入恢复链时，refreshToken 一次性轮换互踩与 persist 写回交错由此串行化
    消除；后到者看到的是先到者已落盘的最新会话状态，输掉一次性轮换即走②级
    laohu 重登重试，不误报重绑。锁序单向无环：main._TJD_LOCK → _AUTH_LOCK →
    state._path_lock；锁内网络请求有界（续期恰一次＋失败后 laohu 重登恰一次
    ＝≤2 请求×20 秒 _http 超时），不长时间霸锁。"""
    with _AUTH_LOCK:
        try:
            session = client.refresh_session()
        except TajiduoError as e2:
            origin = str(e2)
            session = None
            try:
                session = _relogin_via_laohu()
            except TajiduoError as e3:
                origin = f"{origin}；laohu重登：{e3}"
            if session is not None:
                # 新会话换进调用方 client（retry 闭包持有它）后重试恰一次
                client.access_token = session["access_token"]
                client.refresh_token = session["refresh_token"]
                client.center_uid = session["center_uid"]
                try:
                    return retry()
                except TajiduoError as e3:
                    # TJD-02/B3-B：laohu 重登成功后重试仍失败——按 e3.auth 分流。
                    # 非 auth 错误（如「接口 HTTP 500」）原样上抛（raise e3 from e2，
                    # 保留 status_code/raw 与原始消息；auth 判定在构造器按原始
                    # status_code/消息算得，此处不可信包装文案反推）；auth 错误
                    # 维持现状：origin 累积后走下方友好重绑错误（auth=True）。
                    if not e3.auth:
                        raise e3 from e2
                    origin = f"{origin}；laohu重登后仍失败：{e3}"
            raise TajiduoError(
                "登录已过期，请重新绑定（在塔吉多卡片输入手机号获取验证码）",
                raw={"origin": origin, "origin_error": str(first_error)},
                auth=True) from e2
        persist_session(session, client.device_id)
        return retry()


# ---------- 手动同步编排（main.py /api/tajiduo/sync 唯一调用方） ----------

def sync_all(client, *, max_roles=10):
    """拉全部游戏角色 → 每角色 roleHome + characters。auth 失效走三级恢复链
    （_recover_auth：①续期→②laohu 根凭据重建→③友好重绑错误），恰恢复一次。

    返回 {roles, role_home, rows, warnings}；rows 为规整后的角色行（parse_characters）。
    网络失败/恢复链耗尽抛 TajiduoError（调用方转 502/401）。"""
    try:
        return _sync_all_inner(client, max_roles)
    except TajiduoError as e:
        if not e.auth:
            raise
        # 官方会话彻底失效（如 "FAILED TO TOUCH" 会话拒绝）最终转换为明确的
        # 重新绑定引导——官方原文进 raw 供诊断，不透传给用户
        return _recover_auth(client, e, lambda: _sync_all_inner(client, max_roles))


def _dump_last_payload(dump):
    """原始 payload 诊断落盘（A1）：写失败只告警不阻断同步（诊断用途非关键路径）。"""
    try:
        save_json_atomic(LAST_PAYLOAD_FILE, dump)
        return True
    except OSError:
        return False


def _sync_all_inner(client, max_roles):
    roles = client.get_game_roles(GAME_ID_YIHUAN)
    warnings = []
    if not roles:
        warnings.append("账号下未查询到异环游戏角色（先在游戏内创建角色并绑定）")
    home, rows = None, []
    # 原始 payload 诊断留存（data/tajiduo_last_payload.json，.gitignore 排除）：
    # 解析与真实键不一致时以落盘 payload 为核对依据（只存最近一次同步）
    dump = {"schema": 1,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "per_role": {}}
    for role in roles[:max_roles]:
        role_id = role["roleId"]
        home_payload = None
        chars_payload = None
        try:
            home_payload = client.get_role_home(role_id)
            home = parse_role_home(home_payload) or home
        except TajiduoError as e:
            warnings.append(f"角色 {role_id} 总览拉取失败：{e}")
        try:
            chars_payload = client.get_role_characters(role_id)
            rows.extend(parse_characters(chars_payload))
        except TajiduoError as e:
            warnings.append(f"角色 {role_id} 角色面板拉取失败：{e}")
        if home_payload is not None or chars_payload is not None:
            # 只留 awapi/yh 数据段原始 JSON（getGameRoles 列表已在上层 roles）
            dump["per_role"][role_id] = {"role_home": home_payload,
                                         "characters": chars_payload}
    if dump["per_role"] and not _dump_last_payload(dump):
        warnings.append("原始 payload 诊断落盘失败（data/tajiduo_last_payload.json）")
    # 多区服重复角色按 (区服, 名字, 等级) 去重（同区同名的重复面板只保留一条）
    seen, deduped = set(), []
    for row in rows:
        key = (row["name"], row["level"], row["slev"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return {"roles": roles, "role_home": home, "rows": deduped, "warnings": warnings}


# ---------- 抽卡历史本地累积（data/gacha_history.json；手动同步 only） ----------
# 官方窗口仅 180 天且 details 只含已出 S（见 parse_gacha_summary 注释）——
# 每次同步把新快照与本地存档做并集合并，累积保留更早的 S 记录。

GACHA_HISTORY_SCHEMA = 1

# 官方三池 tab 实名（2026-11-07 WebFetch 核对 NTEUID nte_gacha/archive_model.py
# POOL_NAMES：limited=限定卡池 / permanent=常驻卡池 / weapon=弧盘池）。未识别 tab
# 不丢弃——以 "tab_" 前缀键照常累积（防官方加池丢数据），不进三池 UI、sync 返回
# warnings 提示。
GACHA_POOL_NAMES = (("limited", "限定卡池"), ("permanent", "常驻卡池"),
                    ("weapon", "弧盘池"))
GACHA_POOL_BY_TAB = {name: key for key, name in GACHA_POOL_NAMES}


def _gacha_valid_key(item_id, time_ms, pity):
    """S 明细去重键合法性（口径同 NTEUID nte_gacha/archive_model.NTEGachaRecord
    Field：item_id 非空、time_ms>0、pity≥1；bool 按 Python int 子类特判拒绝）。
    合法返回 (item_id, time_ms, pity) 三元组，否则 None。"""
    item_id = str(item_id or "").strip()
    if not item_id or isinstance(time_ms, bool) or not isinstance(time_ms, int) \
            or time_ms <= 0 or isinstance(pity, bool) or not isinstance(pity, int) \
            or pity < 1:
        return None
    return (item_id, time_ms, pity)


def _gacha_record(rec_key, lucky_type=None, time_str=""):
    """键三元组 → 存档记录 dict（lucky_type/time 为展示字段，不参与去重）。"""
    return {"item_id": rec_key[0], "time_ms": rec_key[1], "pity": rec_key[2],
            "lucky_type": lucky_type, "time": time_str}


def merge_gacha_history(existing, summary, updated_at=""):
    """新抽卡快照 ∪ 本地存档（纯函数——不落盘，selftest 幂等/并集用例直测）。

    去重键 = (item_id, time_ms, pity)，即 charid+timeStamp+保底计数（2026-11-07
    WebFetch 核对 NTEUID nte_gacha/archive_model.NTEGachaRecord.key 与
    gacha_history._merge 语义：新快照记录先行入索引（同键快照胜出——展示字段
    lucky_type/time 以最新拉取为准），旧存档记录 setdefault 并入绝不覆盖）。
    合法性不满足（缺键/pity<1/time_ms≤0）的记录跳过，绝不编造键。
    existing 形态不限（损坏/旧版一律按空档处理）。返回 (archive, new_counts,
    unknown_tabs)：new_counts 按池计「本次快照里本地没有的新记录数」，
    unknown_tabs 为未识别 tab 原文名列表。"""
    existing = existing if isinstance(existing, dict) else {}
    old_pools = existing.get("pools") if isinstance(existing.get("pools"), dict) else {}
    # 快照按池归位（tab 缺失的池无处安放，跳过——不编造归属）
    snap, unknown_tabs = {}, []
    for pool in summary.get("pools") or []:
        tab = str(pool.get("tab") or "")
        if not tab:
            continue
        key = GACHA_POOL_BY_TAB.get(tab) or ("tab_" + _ocr_like_norm(tab))
        if key not in GACHA_POOL_BY_TAB.values() and tab not in unknown_tabs:
            unknown_tabs.append(tab)
        snap.setdefault(key, []).extend(pool.get("details") or [])
    keys = [key for key, _name in GACHA_POOL_NAMES]
    keys += [k for k in snap if k not in keys]
    keys += [k for k in old_pools if k not in keys]
    pools_out, new_counts = {}, {}
    for key in keys:
        stored_keys, stored_records = set(), []
        for rec in (old_pools.get(key) or {}).get("records") or []:
            if not isinstance(rec, dict):
                continue
            rec_key = _gacha_valid_key(rec.get("item_id"), rec.get("time_ms"),
                                       rec.get("pity"))
            if rec_key is None:
                continue
            stored_keys.add(rec_key)
            stored_records.append((rec_key, _gacha_record(
                rec_key, rec.get("lucky_type"), str(rec.get("time") or ""))))
        records, fresh = {}, 0
        for det in snap.get(key) or []:
            rec_key = _gacha_valid_key(det.get("charid"), det.get("time_ms"),
                                       det.get("pity"))
            if rec_key is None:
                continue
            if rec_key not in stored_keys:
                fresh += 1
            records.setdefault(rec_key, _gacha_record(
                rec_key, det.get("lucky_type"), str(det.get("time") or "")))
        for rec_key, rec in stored_records:
            records.setdefault(rec_key, rec)  # 同键不重复；快照记录已先行入表
        new_counts[key] = fresh
        pools_out[key] = {"records": sorted(
            records.values(), key=lambda r: (r["time_ms"], r["item_id"], r["pity"]))}
    archive = {
        "schema": GACHA_HISTORY_SCHEMA,
        "roleid": str(summary.get("roleid") or "").strip()
                  or str(existing.get("roleid") or ""),
        "rolename": str(summary.get("rolename") or "").strip()
                    or str(existing.get("rolename") or ""),
        "updated_at": str(updated_at or ""),
        "pools": pools_out,
    }
    return archive, new_counts, unknown_tabs


def load_gacha_history():
    """读取本地累积存档（state.load_json 容错链：损坏留档 .corrupt 回退 .bak；
    缺失/损坏/形态不对一律返回空骨架，绝不抛异常阻塞同步）。"""
    data = load_json(GACHA_HISTORY_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("pools"), dict):
        return {"schema": GACHA_HISTORY_SCHEMA, "roleid": "", "rolename": "",
                "updated_at": "", "pools": {}}
    return data


def save_gacha_history(archive):
    """原子落盘（复用 state.save_json_atomic 的 tmp→bak→replace 链；
    data/gacha_history.json 已在 .gitignore 排除）。"""
    save_json_atomic(GACHA_HISTORY_FILE, archive)


def sync_gacha(client):
    """拉抽卡分析 → 解析 → 本地并集累积 → 落盘。auth 失效走三级恢复链
    （同 sync_all 共用 _recover_auth：①续期→②laohu 根凭据重建→③友好重绑错误，
    恢复恰一次不递归）。

    返回 {summary, archive, new_counts, unknown_tabs, warnings}；
    网络失败/恢复链耗尽抛 TajiduoError（调用方转 502/401）。"""
    try:
        return _sync_gacha_inner(client)
    except TajiduoError as e:
        if not e.auth:
            raise
        # 同 sync_all：官方原文进 raw 供诊断，不透传给用户
        return _recover_auth(client, e, lambda: _sync_gacha_inner(client))


def _sync_gacha_inner(client):
    summary = parse_gacha_summary(client.get_gacha_summary())
    archive, new_counts, unknown_tabs = merge_gacha_history(
        load_gacha_history(), summary, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    warnings = []
    if unknown_tabs:
        warnings.append("官方返回未识别卡池（已照常累积，不进三池卡片）："
                        + "、".join(unknown_tabs))
    try:
        save_gacha_history(archive)
    except OSError:
        warnings.append("抽卡本地存档保存失败（data/gacha_history.json）"
                        "——本次结果未持久化")
    return {"summary": summary, "archive": archive, "new_counts": new_counts,
            "unknown_tabs": unknown_tabs, "warnings": warnings}
