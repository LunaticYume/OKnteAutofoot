"""TJD-04：打包态用户数据目录 DACL 私有化（纯标准库 ctypes，无第三方依赖）。

用途（TJD-04）：W4b preflight 实机 icacls 实测确认，打包态用户数据目录
%LOCALAPPDATA%\\OKntePlanner\\data 含继承 ACE CodexSandboxUsers:(I)(OI)(CI)(RX)
——沙箱组对该目录（内含 tajiduo_token.json 等凭据）可读，产品缺陷 CONFIRMED。
本模块在应用创建/迁移用户数据目录后显式建立私有 DACL（断继承白名单），并在
凭据写盘前作硬闸（require_private），ACL 修复失败时绝不静默继续保存敏感凭据。

验收标准（六条；实现=PROTECTED 断继承 + 仅授予三主体的白名单 DACL）：
1. 当前用户 Full Control；2. SYSTEM Full Control；3. Administrators Full Control；
4. Users 组无可读权限；5. Authenticated Users 组无可读权限；
6. Everyone 组与 CodexSandboxUsers 组无可读权限。
token 文件及 .bak/.tmp 不重新继承宽权限：目录 DACL 被 PROTECTED（断继承）后，
SetNamedSecurityInfoW 会把子项既有继承 ACE 重写为新父 DACL 的继承（宽组 ACE
随之消失），此后新建的 token/.bak/.tmp 只继承三主体 ACE。

为何白名单不加 deny：deny ACE 反噬——Users 组（S-1-5-32-545）、Authenticated
Users（S-1-5-11）、Everyone（S-1-1-0）都含当前登录用户本人，对它们的 deny 会
连带拒绝用户自己读写自己的数据。普通组「无读权限」由 PROTECTED 断继承 + 不
授予达成（Windows 权限模型：未授予即无权限，无需 deny），因此本模块 SDDL 固定
形态绝不包含 deny ACE、也绝不授予宽组主体（selftest 静态守卫把守）。

幂等/自愈语义：harden_data_dir 先读回现 DACL，已满足验收形态（SE_DACL_PROTECTED
+ 恰 3 条 FA 全控 allow ACE、OBJECT|CONTAINER 继承位、主体恰为 SYSTEM/Administrators/
当前用户）返回 already 不重写；否则按固定 SDDL 应用（SetNamedSecurityInfoW 携带
PROTECTED_DACL_SECURITY_INFORMATION，owner/group 参数一律 NULL 不动所有权）并
写后重读验证。main._run_panel 每次启动幂等复查（覆盖「已有目录升级时收紧」与
「外部改坏后每次启动自愈」）；state.ensure_user_data 在目录创建/迁移后即时收紧；
require_private 凭据写盘前查缓存 _LAST_HARDEN、无记录懒加固——失败抛
DataDaclError 拒写（failed/unsupported 不写缓存，下次写盘自动重试=自愈）。

打包态判定：与 app/state.py 打包态口径一致（sys.frozen 或启动器注入
OKNTE_PLANNER_USER_DATA=1，state.py:14-19），但本模块自含实现（is_packaged）、
绝不 import app.state——state.py 在导入期被大量模块依赖，反向依赖会造成循环
导入。非打包态 require_private 直接放行（源码态/测试态豁免，仓库 data/ 不收紧）。
"""
import ctypes
import os
import sys
from ctypes import wintypes

# ---- Win32 常量（ MSDN：SE_OBJECT_TYPE / SECURITY_INFORMATION / ACE_HEADER 等）----
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PROTECTED = 0x1000
ACCESS_ALLOWED_ACE_TYPE = 0
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
SD_REVISION = 1          # SECURITY_DESCRIPTOR_REVISION
LMEM_FIXED = 0x0000
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NOT_SUPPORTED = 50  # 卷不支持 ACL（FAT/exFAT）——selftest SKIP 守卫依据

# 注：ACE 计数不取自 GetAclInformation(ACL_SIZE_INFORMATION)——本机实测该调用
# 对 1 条 ACE 的 ACL 误报 AceCount=2（2026-11-20 诊断，AclSize=28=8+20 头信息
# 可证），改直读 _ACL 文档化头部的 AceCount 字段（布局 MSDN 稳定，零额外 API）。

# Full Control 的标准权利全集（SDDL FA 展开后的标准段；specific file 位从宽不验）：
# DELETE | READ_CONTROL | WRITE_DAC | WRITE_OWNER | SYNCHRONIZE
_FULL_CONTROL_STANDARD = (0x00010000 | 0x00020000 | 0x00040000
                          | 0x00080000 | 0x00100000)

_SID_SYSTEM = "S-1-5-18"           # Local System
_SID_ADMINISTRATORS = "S-1-5-32-544"  # BUILTIN\Administrators

# 固定形态（绝不加 deny ACE——理由见模块 docstring；第三主体=当前用户 SID 字符串）
_SDDL_TEMPLATE = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;%s)"

# require_private 的模块级缓存：键 normcase(abspath) 后目录路径，值 harden_data_dir
# 结果 dict（harden_data_dir 成功后写入自己；failed/unsupported 不缓存——下次
# 写盘现场重试自愈）。每进程至多缓存少数生产目录（data/、log_dir），无界增长
# 仅见于测试态临时目录（进程退出随进程释放）。
_LAST_HARDEN = {}


class DataDaclError(OSError):
    """TJD-04 凭据写盘闸拒绝（require_private 在 DACL 私有化失败时抛出）。

    继承 OSError 的理由：本仓库对写盘类失败的既有错误映射链全部按 OSError 语义
    兜底——/api/tajiduo/bind 端点 except OSError → 500「凭证保存失败」、
    /api/tajiduo/sync·sync-gacha 的 except OSError 兜底分支、_ensure_panel_token
    写失败=启动失败进 crash.log、elev ensure() 失败链转 reason()+False——以
    OSError 子类抛出即可沿这些既有链上抛为可见错误，无需新增错误类型分支。
    """


class _DaclStepError(Exception):
    """单个 Win32 步骤失败的内部异常（detail 携带步骤名与 WinError 码/描述），
    由 harden_data_dir 捕获转 failed 结果 dict，绝不向上直接抛。"""


def _step_fail(step, code):
    """构造 _DaclStepError：detail=步骤名 + [WinError N] 描述（含 GetLastError
    码；SetNamedSecurityInfoW/GetNamedSecurityInfoW 的返回码本身即错误码）。"""
    detail = "%s 失败: %s" % (step, str(ctypes.WinError(code)).strip())
    if code == ERROR_NOT_SUPPORTED:
        detail += "（ERROR_NOT_SUPPORTED：卷不支持 ACL，非 NTFS 卷无法硬化）"
    return _DaclStepError(detail)


# ---- ctypes 结构体（布局按 MSDN；ACE 解析用 from_address 零拷贝读 ACL 内部）----

class ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", ctypes.c_ushort)]


class ACL(ctypes.Structure):
    """_ACL 头部（MSDN 文档化布局）：AceCount 直读用（见模块顶部注）。"""
    _fields_ = [("AclRevision", ctypes.c_ubyte),
                ("Sbz1", ctypes.c_ubyte),
                ("AclSize", ctypes.c_ushort),
                ("AceCount", ctypes.c_ushort),
                ("Sbz2", ctypes.c_ushort)]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    # ACCESS_DENIED_ACE 布局同构（Header+Mask+SidStart），deny ACE 也用本结构读
    _fields_ = [("Header", ACE_HEADER),
                ("Mask", ctypes.c_uint32),
                ("SidStart", ctypes.c_uint32)]


_ACE_SID_OFFSET = ACCESS_ALLOWED_ACE.SidStart.offset  # SID 起始在 ACE 内偏移


class _ApiBundle(object):
    """advapi32/kernel32 函数指针束（原型在 _load_apis 内按 MSDN 定义）。"""


def _load_apis():
    """加载 advapi32/kernel32 并按 MSDN 定义 argtypes/restype（指针一律
    c_void_p/结构体 POINTER——禁止默认 int 截断 64 位指针）；任一步失败返回
    None（harden_data_dir 据此返回 unsupported，selftest 据此 SKIP）。"""
    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD  # 返回码即错误码
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR,                  # pObjectName
            ctypes.c_int,                      # SE_OBJECT_TYPE
            wintypes.DWORD,                    # SECURITY_INFORMATION
            ctypes.c_void_p,                   # ppsidOwner（不取，恒 NULL）
            ctypes.c_void_p,                   # ppsidGroup（不取，恒 NULL）
            ctypes.c_void_p,                   # ppDacl（不取，恒 NULL）
            ctypes.c_void_p,                   # ppSacl（不取，恒 NULL）
            ctypes.POINTER(ctypes.c_void_p),   # ppSecurityDescriptor（出参）
        )

        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorControl.argtypes = (
            ctypes.c_void_p,                   # pSecurityDescriptor
            ctypes.POINTER(wintypes.WORD),     # pControl
            ctypes.POINTER(wintypes.DWORD),    # lpdwRevision
        )

        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorDacl.argtypes = (
            ctypes.c_void_p,                   # pSecurityDescriptor
            ctypes.POINTER(wintypes.BOOL),     # lpfDaclPresent
            ctypes.POINTER(ctypes.c_void_p),   # pDacl*
            ctypes.POINTER(wintypes.BOOL),     # lpfDaclDefaulted
        )

        advapi32.GetAce.restype = wintypes.BOOL
        advapi32.GetAce.argtypes = (
            ctypes.c_void_p,                   # pAcl
            wintypes.DWORD,                    # dwAceIndex
            ctypes.POINTER(ctypes.c_void_p),   # pAce*（ACL 内部指针，不释放）
        )

        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p,                   # Sid
            ctypes.POINTER(ctypes.c_void_p),   # StringSid*（LocalFree 释放）
        )

        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = \
            wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,                  # StringSecurityDescriptor
            wintypes.DWORD,                    # StringSDRevision
            ctypes.POINTER(ctypes.c_void_p),   # SecurityDescriptor*（LocalFree）
            ctypes.POINTER(wintypes.ULONG),    # SecurityDescriptorSize（出参）
        )

        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD  # 返回码即错误码
        advapi32.SetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR,                  # pObjectName
            ctypes.c_int,                      # SE_OBJECT_TYPE
            wintypes.DWORD,                    # SECURITY_INFORMATION
            ctypes.c_void_p,                   # psidOwner（恒 NULL，不动所有权）
            ctypes.c_void_p,                   # psidGroup（恒 NULL）
            ctypes.c_void_p,                   # pDacl
            ctypes.c_void_p,                   # pSacl（恒 NULL）
        )

        advapi32.GetUserNameW.restype = wintypes.BOOL
        advapi32.GetUserNameW.argtypes = (
            wintypes.LPWSTR,                   # lpBuffer
            ctypes.POINTER(wintypes.DWORD),    # pcbBuffer（含结尾 NUL，字符数）
        )

        advapi32.LookupAccountNameW.restype = wintypes.BOOL
        advapi32.LookupAccountNameW.argtypes = (
            wintypes.LPCWSTR,                  # lpSystemName（NULL=本机）
            wintypes.LPCWSTR,                  # lpAccountName
            ctypes.c_void_p,                   # Sid（首探 NULL 取 cbSid）
            ctypes.POINTER(wintypes.DWORD),    # cbSid
            wintypes.LPWSTR,                   # ReferencedDomainName
            ctypes.POINTER(wintypes.DWORD),    # cchReferencedDomainName
            ctypes.POINTER(ctypes.c_int),      # peUse（SID_NAME_USE）
        )

        kernel32.LocalAlloc.restype = ctypes.c_void_p
        kernel32.LocalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    except Exception:  # noqa: BLE001 —— ctypes 初始化失败走 unsupported 通道
        return None
    api = _ApiBundle()
    api.get_named_security_info = advapi32.GetNamedSecurityInfoW
    api.get_sd_control = advapi32.GetSecurityDescriptorControl
    api.get_sd_dacl = advapi32.GetSecurityDescriptorDacl
    api.get_ace = advapi32.GetAce
    api.sid_to_string = advapi32.ConvertSidToStringSidW
    api.sddl_to_sd = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    api.set_named_security_info = advapi32.SetNamedSecurityInfoW
    api.get_user_name = advapi32.GetUserNameW
    api.lookup_account_name = advapi32.LookupAccountNameW
    api.local_alloc = kernel32.LocalAlloc
    api.local_free = kernel32.LocalFree
    return api


_APIS = _load_apis()


def dacl_supported():
    """DACL 硬化能力可用（Windows 且 ctypes/API 原型初始化成功）。
    selftest SKIP 守卫与非 Windows 降级判定共用。"""
    return os.name == "nt" and _APIS is not None


def is_packaged():
    """打包态判定（与 app/state.py 打包态口径一致但自含实现防循环导入）：
    sys.frozen（PyInstaller 档）或启动器注入 OKNTE_PLANNER_USER_DATA=1
    （LAUNCHER_CS Run() 注入的启动器档）。"""
    return getattr(sys, "frozen", False) or \
        os.environ.get("OKNTE_PLANNER_USER_DATA") == "1"


def _local_free(ptr):
    if ptr:
        _APIS.local_free(ctypes.c_void_p(ptr))


def _sid_to_string(sid_addr):
    """SID（内存地址）→ "S-1-…" 字符串；输出 buffer 由 ConvertSidToStringSidW
    分配、须 LocalFree 配对释放。"""
    out = ctypes.c_void_p()
    if not _APIS.sid_to_string(ctypes.c_void_p(sid_addr), ctypes.byref(out)):
        raise _step_fail("ConvertSidToStringSidW", ctypes.get_last_error())
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _local_free(out.value)


def _current_user_sid_string():
    """当前进程用户 SID 字符串：GetUserNameW → LookupAccountNameW（两段式，
    LocalAlloc 按 cbSid 配缓冲）→ ConvertSidToStringSidW。任一步失败抛
    _DaclStepError（harden_data_dir 转 failed，detail 含步骤与 GetLastError）。"""
    # GetUserNameW：一次到位（UNLEN=256+'\0'），不足按返回 cb 扩配重试一次
    size = wintypes.DWORD(257)
    buf = ctypes.create_unicode_buffer(size.value)
    if not _APIS.get_user_name(buf, ctypes.byref(size)):
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER or size.value <= 1:
            raise _step_fail("GetUserNameW", err)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _APIS.get_user_name(buf, ctypes.byref(size)):
            raise _step_fail("GetUserNameW", ctypes.get_last_error())
    account = buf.value
    if not account:
        raise _DaclStepError("GetUserNameW 失败: 返回空用户名")
    # LookupAccountNameW 两段式：首探 NULL 取 cbSid/cchDomain，LocalAlloc 后重试
    cb_sid = wintypes.DWORD(0)
    cch_domain = wintypes.DWORD(0)
    use = ctypes.c_int(0)
    _APIS.lookup_account_name(None, account, None, ctypes.byref(cb_sid),
                              None, ctypes.byref(cch_domain), ctypes.byref(use))
    if cb_sid.value == 0:
        raise _step_fail("LookupAccountNameW（取 SID 尺寸）",
                         ctypes.get_last_error() or 1)
    sid_addr = _APIS.local_alloc(LMEM_FIXED, ctypes.c_size_t(cb_sid.value))
    if not sid_addr:
        raise _step_fail("LocalAlloc(SID)", ctypes.get_last_error())
    try:
        cch = max(int(cch_domain.value), 1)
        domain = ctypes.create_unicode_buffer(cch)
        ok = _APIS.lookup_account_name(
            None, account, ctypes.c_void_p(sid_addr), ctypes.byref(cb_sid),
            domain, ctypes.byref(wintypes.DWORD(cch)), ctypes.byref(use))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_INSUFFICIENT_BUFFER:
                raise _step_fail("LookupAccountNameW", err)
            cch = max(int(cch_domain.value), 1)  # 失败时回填所需尺寸
            domain = ctypes.create_unicode_buffer(cch)
            if not _APIS.lookup_account_name(
                    None, account, ctypes.c_void_p(sid_addr),
                    ctypes.byref(cb_sid), domain,
                    ctypes.byref(wintypes.DWORD(cch)), ctypes.byref(use)):
                raise _step_fail("LookupAccountNameW", ctypes.get_last_error())
        return _sid_to_string(sid_addr)
    finally:
        _local_free(sid_addr)


def _read_dacl(path):
    """读回对象 DACL 摘要（harden 写后验证与 selftest 回归共用解析入口）。

    返回 {"present": bool, "control": int, "aces": [{"type","flags","mask","sid"}]}；
    对象无 DACL 时 present=False、aces=[]。GetNamedSecurityInfoW 返回的 SD 须
    LocalFree 配对释放；GetAce 返回 ACL 内部指针不释放。API 失败抛 _DaclStepError。
    """
    sd = ctypes.c_void_p()
    rc = _APIS.get_named_security_info(
        str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(sd))
    if rc != 0:
        raise _step_fail("GetNamedSecurityInfoW", rc)
    try:
        control = wintypes.WORD(0)
        revision = wintypes.DWORD(0)
        if not _APIS.get_sd_control(sd, ctypes.byref(control),
                                    ctypes.byref(revision)):
            raise _step_fail("GetSecurityDescriptorControl",
                             ctypes.get_last_error())
        f_present = wintypes.BOOL(0)
        f_default = wintypes.BOOL(0)
        pdacl = ctypes.c_void_p()
        if not _APIS.get_sd_dacl(sd, ctypes.byref(f_present),
                                 ctypes.byref(pdacl), ctypes.byref(f_default)):
            raise _step_fail("GetSecurityDescriptorDacl", ctypes.get_last_error())
        aces = []
        if f_present.value and pdacl.value:
            acl = ACL.from_address(pdacl.value)  # 直读头部 AceCount（见模块顶部注）
            for index in range(acl.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not _APIS.get_ace(pdacl, index, ctypes.byref(ace_ptr)):
                    raise _step_fail("GetAce(%d)" % index, ctypes.get_last_error())
                body = ACCESS_ALLOWED_ACE.from_address(ace_ptr.value)
                sid_addr = ace_ptr.value + _ACE_SID_OFFSET
                try:
                    sid_str = _sid_to_string(sid_addr)
                except _DaclStepError:
                    sid_str = None  # 罕见坏 SID：保留 ACE 其余字段供诊断
                aces.append({"type": int(body.Header.AceType),
                             "flags": int(body.Header.AceFlags),
                             "mask": int(body.Mask),
                             "sid": sid_str})
        return {"present": bool(f_present.value), "control": int(control.value),
                "aces": aces}
    finally:
        _local_free(sd.value)


def _is_hardened(info, user_sid):
    """幂等判定：SE_DACL_PROTECTED + 恰 3 条 FA 全控 allow ACE（Mask 含
    DELETE|READ_CONTROL|WRITE_DAC|WRITE_OWNER|SYNCHRONIZE 全集，从宽不验
    specific file 位）+ OBJECT|CONTAINER 继承位 + 主体恰为 SYSTEM/Administrators/
    当前用户。"""
    if not info or not info["present"] or not (info["control"] & SE_DACL_PROTECTED):
        return False
    aces = info["aces"]
    if len(aces) != 3:
        return False
    sids = set()
    for ace in aces:
        if ace["type"] != ACCESS_ALLOWED_ACE_TYPE:
            return False
        if (ace["mask"] & _FULL_CONTROL_STANDARD) != _FULL_CONTROL_STANDARD:
            return False
        inherit = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        if (ace["flags"] & inherit) != inherit:
            return False
        sids.add(ace["sid"])
    return sids == {_SID_SYSTEM, _SID_ADMINISTRATORS, user_sid}


def _apply_sddl(path, sddl, protected):
    """把 SDDL 应用为对象 DACL（selftest 宽 DACL 预置与 harden 共用）：
    ConvertStringSecurityDescriptorToSecurityDescriptorW → GetSecurityDescriptorDacl
    → SetNamedSecurityInfoW（owner/group 恒 NULL 不动所有权；protected 时携带
    PROTECTED_DACL_SECURITY_INFORMATION 断继承）。失败抛 _DaclStepError。"""
    sd = ctypes.c_void_p()
    sd_len = wintypes.ULONG(0)
    if not _APIS.sddl_to_sd(sddl, SD_REVISION, ctypes.byref(sd),
                            ctypes.byref(sd_len)):
        raise _step_fail("ConvertStringSecurityDescriptorToSecurityDescriptorW",
                         ctypes.get_last_error())
    try:
        f_present = wintypes.BOOL(0)
        f_default = wintypes.BOOL(0)
        pdacl = ctypes.c_void_p()
        if not _APIS.get_sd_dacl(sd, ctypes.byref(f_present),
                                 ctypes.byref(pdacl), ctypes.byref(f_default)):
            raise _step_fail("GetSecurityDescriptorDacl", ctypes.get_last_error())
        info = DACL_SECURITY_INFORMATION
        if protected:
            info |= PROTECTED_DACL_SECURITY_INFORMATION
        rc = _APIS.set_named_security_info(
            str(path), SE_FILE_OBJECT, info, None, None,
            pdacl if (f_present.value and pdacl.value) else None, None)
        if rc != 0:
            raise _step_fail("SetNamedSecurityInfoW", rc)
    finally:
        _local_free(sd.value)


def harden_data_dir(path):
    """把目录 DACL 收紧为私有白名单（TJD-04）；幂等，可对任意自建目录安全调用
    （创建者对自己的目录有 WRITE_DAC）。

    返回 {"status": "ok"|"already"|"failed"|"unsupported", "detail": str}：
    - already：现 DACL 已满足验收形态（见 _is_hardened），不重写（结果入缓存）；
    - ok：已应用固定 SDDL 且写后重读验证通过（结果入缓存）；
    - failed：任一 Win32 步骤失败或验证不过（detail 含步骤与 WinError；不入缓存，
      下次写盘 require_private 现场重试自愈）；
    - unsupported：非 Windows 或 ctypes 初始化失败。
    不创建目录、不动所有权；成功/幂等结果写入 _LAST_HARDEN 供 require_private 放行。
    """
    if not dacl_supported():
        return {"status": "unsupported",
                "detail": "非 Windows 或 ctypes 初始化失败，DACL 硬化不可用"
                          "（os.name=%s）" % os.name}
    path = str(path)
    try:
        user_sid = _current_user_sid_string()
    except _DaclStepError as exc:
        return {"status": "failed", "detail": str(exc)}
    try:
        cur = _read_dacl(path)
    except _DaclStepError as exc:
        return {"status": "failed", "detail": "读取现有 DACL：%s" % exc}
    if _is_hardened(cur, user_sid):
        result = {"status": "already",
                  "detail": "已是私有 DACL（PROTECTED+SY/BA/%s 三 allow）" % user_sid}
        _LAST_HARDEN[os.path.normcase(os.path.abspath(path))] = result
        return result
    try:
        _apply_sddl(path, _SDDL_TEMPLATE % user_sid, protected=True)
        after = _read_dacl(path)
    except _DaclStepError as exc:
        return {"status": "failed", "detail": str(exc)}
    if not _is_hardened(after, user_sid):
        return {"status": "failed",
                "detail": "DACL 写入后重读验证不符（PROTECTED 位或三主体 FA allow"
                          " 缺失），SetNamedSecurityInfoW 未生效"}
    result = {"status": "ok",
              "detail": "已建立私有 DACL（PROTECTED+SY/BA/%s 三 allow，"
                        "宽组继承已断）" % user_sid}
    _LAST_HARDEN[os.path.normcase(os.path.abspath(path))] = result
    return result


def require_private(target):
    """凭据写盘闸（TJD-04）：确保写盘目标所在目录已私有化，否则拒绝写盘。

    - target=写盘目标：目录路径为规范入参（三处接线均传写盘目录）；传文件路径
      时取其所在目录（存在性以 isdir 判定，目录不存在则按其上级解析——结果只
      会更严格地 fail-closed）。
    - 非打包态直接放行（源码态/测试态豁免，仓库 data/ 不收紧）。
    - 打包态查缓存 _LAST_HARDEN（键 normcase 目录路径）；无记录现场调
      harden_data_dir 懒加固；结果 failed 抛 DataDaclError（消息含目录、失败
      detail 与修复指引），ok/already/unsupported 放行（unsupported 仅非 Windows
      或 ctypes 初始化失败形态，打包态为 Windows EXE 正常不会出现）。
    """
    dir_path = str(target)
    if not os.path.isdir(dir_path):
        dir_path = os.path.dirname(dir_path) or os.curdir
    if not is_packaged():
        return
    key = os.path.normcase(os.path.abspath(dir_path))
    cached = _LAST_HARDEN.get(key)
    if cached is None:
        cached = harden_data_dir(dir_path)
    if cached["status"] == "failed":
        raise DataDaclError(
            "TJD-04：目录 %s 的 DACL 私有化失败（%s），已拒绝写入敏感凭据。"
            "修复指引：检查安全软件/组策略后重启面板可自动重试加固；"
            "也可管理员运行 icacls 重置后重启。" % (dir_path, cached["detail"]))
