#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server/libs/api_client.py

学校 API 调用封装。提供登录、账户查询、设备列表查询等接口。

用法：
    from server.libs.api_client import login, get_account_list, get_device_list

所有函数返回格式均为 API 原始 JSON 响应（dict），网络异常时返回 {"code": -1, "msg": ...}

本校（湖南工业大学）实测要点（2026-09）：
  * 这套部署的登录走统一身份认证（CAS），厂商标准的 POST /appUser/login
    只对「在校园 App 内注册过」的账号有效，其余账号返回
    {"code": 202, "msg": "该账号尚未注册,请先注册！"}；
  * 但 GET /appUser/{学号} 无需登录即可返回 appUserId / roleId，
    因此 login() 在收到 202 时会自动回落到 get_user_by_phone()，拿到同样的两个值；
  * 查询类接口（appUserAcct/list、equipment/list）不校验登录态，
    带不带 Cookie / Admin-Token 返回一致，所以本模块全程不需要维护会话。
"""

import requests
import time

from .signer import generate_sign, get_timestamp, md5_encrypt, BASE_URL, DEFAULT_HEADERS, CHANNEL_ID
from . import school_profile


# ─────────────────────────────────────────────
# API 端点
# ─────────────────────────────────────────────

_LOGIN_URL = f"{BASE_URL}/appUser/login"
_APP_USER_URL = f"{BASE_URL}/appUser"          # /appUser/{学号}
_ACCOUNT_LIST_URL = f"{BASE_URL}/appUserAcct/list"
_DEVICE_LIST_URL = f"{BASE_URL}/equipment/list"


# ─────────────────────────────────────────────
# API 函数
# ─────────────────────────────────────────────

def _get(url: str, params: dict) -> dict:
    """带签名的 GET，异常统一收敛成 {"code": -1}"""
    signed = generate_sign(params)
    try:
        resp = requests.get(url, params=signed, headers=DEFAULT_HEADERS, timeout=20)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": f"请求失败: {str(e)}"}


def get_user_by_phone(phone_num: str) -> dict:
    """
    用学号/工号换取 appUserId 与 roleId（无需密码、无需登录态）。

    这是本校最省事的取参方式：前端登录后也是先 GET /appUser/{phoneNum} 拿 appUserId。

    Args:
        phone_num: 学号 / 工号（本校接口里 phoneNum 就是学号）

    Returns:
        API 响应 dict，user 字段含 appUserId / phoneNum / roleId
    """
    params = {
        "channelid": CHANNEL_ID,
        "timestamp": get_timestamp(),
    }
    result = _get(f"{_APP_USER_URL}/{phone_num}", params)
    if result.get("code") == 200 and isinstance(result.get("user"), dict):
        user = result["user"]
        # 抹掉服务端顺带返回的密码字段，避免它被日志/数据库带出去
        user.pop("password", None)
        result["appUserId"] = user.get("appUserId")
        result["roleId"] = user.get("roleId")
    return result


def login(phone_num: str, password: str = None) -> dict:
    """
    用户登录。

    * password 为空：直接调 get_user_by_phone()（本校无需密码）；
    * 传了 password：先走厂商标准的直连接口 POST /appUser/login（手机号 + MD5 密码）；
      若该账号未在校园 App 内注册过（code=202），自动回落到 get_user_by_phone()，
      用学号解析出 appUserId / roleId —— 返回值结构保持一致。

    Args:
        phone_num: 手机号（本校即学号）
        password: 密码（明文，内部做 MD5 加密）；可选

    Returns:
        API 响应 dict，含 appUserId、roleId（本校还带 rolePhoneNum）
    """
    if not password:
        return get_user_by_phone(phone_num)

    params = {
        "phoneNum": phone_num,
        "password": md5_encrypt(password),
        "channelid": CHANNEL_ID,
        "timestamp": get_timestamp()
    }
    signed = generate_sign(params)
    try:
        resp = requests.post(_LOGIN_URL, json=signed, headers=DEFAULT_HEADERS, timeout=20)
        result = resp.json()
    except Exception as e:
        return {"code": -1, "msg": f"请求失败: {str(e)}"}

    if result.get("code") == 200:
        result = _normalize_login_result(result)
        return result

    # code=202「该账号尚未注册」→ 本校账号走统一身份认证，用学号直接换 appUserId/roleId
    if result.get("code") == 202:
        fallback = get_user_by_phone(phone_num)
        if fallback.get("code") == 200:
            fallback["direct_login"] = False
            fallback["login_msg"] = result.get("msg", "")
            return fallback
        return fallback
    return result


def _normalize_login_result(result: dict) -> dict:
    """把直连登录返回里的 appUserId/roleId 提到顶层，便于调用方统一取值。"""
    for key in ("user", "data"):
        node = result.get(key)
        if isinstance(node, dict):
            result.setdefault("appUserId", node.get("appUserId"))
            result.setdefault("roleId", node.get("roleId"))
            node.pop("password", None)
    result.setdefault("direct_login", True)
    return result


def get_account_list(app_user_id: str, role_id: str) -> dict:
    """
    查询水电费账户列表（每个设备的余额信息）。

    Args:
        app_user_id: 登录返回的 appUserId
        role_id: 登录返回的 roleId

    Returns:
        API 响应 dict，rows 字段含设备余额列表
             每行关键字段：equipmentLatestLarge 结算示数、equipmentCurrentLarge 当前示数、
             remainingBalance 余额、equipmentStatus 开关状态、currentDealDate 结算时间
    """
    params = {
        "appUserId": app_user_id,
        "channelid": CHANNEL_ID,
        "roleId": role_id,
        "timestamp": get_timestamp()
    }
    return _get(_ACCOUNT_LIST_URL, params)


def get_device_list(app_user_id: str, role_key: str,
                    page_num: int = 1, page_size: int = 15) -> dict:
    """
    查询设备分页列表（设备完整信息，含电表/水表类型）。

    Args:
        app_user_id: 登录返回的 appUserId（本校该接口不按它过滤，仅为兼容保留）
        role_key: 登录返回的 roleId（字段名 roleKey）
        page_num: 页码，默认 1
        page_size: 每页条数，默认 15（本校实测每页 1000 也能返回，但页数多时服务端会超时，
                   建议 <= 500）

    Returns:
        API 响应 dict，含 rows 设备列表与 total 总数

    注意（本校实测差异）：本接口返回的是**该 roleKey 下的全部设备台账**，
    appUserId 参数会被忽略（与三一版不同）。只想看自己的表请用 get_account_list()。
    """
    params = {
        "appUserId": app_user_id,
        "channelid": CHANNEL_ID,
        "pageNum": page_num,
        "pageSize": page_size,
        "roleKey": role_key,
        "timestamp": get_timestamp()
    }
    return _get(_DEVICE_LIST_URL, params)


def get_equipment_by_id(equipment_id: str) -> dict:
    """
    按设备 id 查单台设备（含完整字段：equipmentName / installationSite / equipmentType /
    ratio / rate / equipmentStatus / equipmentLatestLarge / equipmentCurrentLarge 等）。

    本校实测：该查询不需要登录态，多带一个 id 参数即可精确定位一台设备。
    """
    params = {
        "roleKey": _profile_role_key(),
        "id": equipment_id,
        "pageNum": 1,
        "pageSize": 1,
        "channelid": CHANNEL_ID,
        "timestamp": get_timestamp(),
    }
    url = f"{BASE_URL}{school_profile.get('equipment_by_id_path', '/equipment/list')}"
    result = _get(url, params)
    rows = result.get("rows") or []
    return rows[0] if rows else {}


def _profile_role_key() -> str:
    """设备台账接口用的 roleKey，默认 2（本校为信息学院），可用学校档案覆盖。"""
    return str(school_profile.get("role_key", "2"))


def fetch_my_devices(app_user_id: str, role_id: str,
                     page_num: int = 1, page_size: int = 100,
                     enrich: bool = True) -> dict:
    """
    取「自己的」设备列表（含入库所需的完整字段）。

    不同学校这套接口的行为不一样，按学校档案的 device_list_scoped_by_user 分流：

      * True （如三一）：get_device_list(appUserId, roleKey, 分页) 本身就只返回该用户的设备；
      * False（如本校）：equipment/list 会忽略 appUserId、返回学院全部台账（实测 1.4 万台），
                         所以改用 appUserAcct/list 取自己的缴费对象，
                         再用 equipment/equipment/list?id= 逐台补齐名称/类型/单价等字段。

    Returns:
        {"code": 200, "rows": [...]} —— rows 为可直接入库的设备 dict 列表
    """
    if school_profile.get("device_list_scoped_by_user", True):
        return get_device_list(app_user_id, role_id, page_num, page_size)

    account = get_account_list(app_user_id, role_id)
    if account.get("code") != 200:
        return account
    rows = account.get("rows") or []
    if not enrich:
        return {"code": 200, "rows": rows, "total": len(rows)}

    full_rows = []
    for item in rows:
        equipment_id = item.get("id")
        detail = get_equipment_by_id(equipment_id) if equipment_id else {}
        merged = dict(item)
        # 设备接口的字段更全（名/位置/类型/单价/表底），缺失的用缴费对象那边的值兜底
        for key, value in detail.items():
            if value not in (None, ""):
                merged[key] = value
        for key in ("equipmentCurrentLarge", "remainingBalance", "equipmentStatus",
                    "currentDealDate", "currentDealTime"):
            if item.get(key) not in (None, ""):
                merged[key] = item[key]
        merged.setdefault("total", len(rows))
        full_rows.append(merged)
    return {"code": 200, "rows": full_rows, "total": len(full_rows)}


def fetch_devices_by_building(building: str, page_size: int = 100, role_key: str = None) -> dict:
    """
    按楼栋号拉取该楼「全部设备」（电表 + 水表），用于整栋楼入库/宿管模式。

    本校实测：equipment/list 支持 equipmentName 前缀过滤
        ?roleKey=2&equipmentName=72-&pageNum=1&pageSize=100  ->  total=520（72 栋所有水电表）
    比扫全院台账（14072 台）快得多，也不会把别的楼的数据带进来。

    注意：pageSize 别开太大，实测 >=1000 时第 3 页起服务端会读超时，默认 100 稳妥。

    Returns:
        {"code": 200, "rows": [...], "total": n}
    """
    prefix = f"{str(building).strip().rstrip('-')}-"
    role = role_key or _profile_role_key()
    rows_all, page = [], 1
    while True:
        params = {
            "roleKey": role,
            "equipmentName": prefix,
            "pageNum": page,
            "pageSize": page_size,
            "channelid": CHANNEL_ID,
            "timestamp": get_timestamp(),
        }
        result = _get(_DEVICE_LIST_URL, params)
        if result.get("code") not in (200, None):
            return result
        rows = result.get("rows") or []
        if not rows:
            break
        rows_all.extend(rows)
        total = result.get("total") or 0
        if total and len(rows_all) >= total:
            break
        if len(rows) < page_size:
            break
        page += 1
        time.sleep(0.3)   # 别把学校服务器打得太狠
    return {"code": 200, "rows": rows_all, "total": len(rows_all)}
