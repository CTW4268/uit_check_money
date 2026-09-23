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

from .signer import generate_sign, get_timestamp, md5_encrypt, BASE_URL, DEFAULT_HEADERS, CHANNEL_ID


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
