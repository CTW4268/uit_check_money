#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取 appUserId 与 roleId（后续所有查询脚本都用这两个值）。

用法：
    ./debug_utils/login.py <学号/工号>              # 推荐：无需密码
    ./debug_utils/login.py <学号/工号> <密码>        # 走厂商直连登录接口

说明（湖南工业大学实测）：
  * 不传密码时，用 GET /prod-api/external/appUser/{学号} 直接换出 appUserId / roleId，
    该接口不需要登录、不需要 cookie，也不会记录任何凭据；
  * 传密码时会先试 POST /appUser/login（厂商标准直连登录）；本校账号走统一身份认证，
    若该账号没在校园 App 里注册过，服务端返回 202「该账号尚未注册」，
    脚本自动回落到上面的无密码方式，并在输出里标注 direct_login=false。

输出为 JSON（便于管道/脚本消费）：
    {"code": 200, "appUserId": "...", "roleId": "...", "phoneNum": "...",
     "rolePhoneNum": "...", "direct_login": false}
"""

import sys
import os

# 添加 server/ 到模块搜索路径，使 libs.* 成为顶层包
_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '..', 'server'))

import json
from libs.api_client import login as api_login, get_user_by_phone


def resolve(account, password=None):
    """统一返回 {code, appUserId, roleId, phoneNum, rolePhoneNum, direct_login}。"""
    result = api_login(account, password) if password else get_user_by_phone(account)
    if result.get("code") != 200:
        return {"code": result.get("code", -1),
                "msg": result.get("msg", "获取用户信息失败")}
    user = result.get("user") or {}
    return {
        "code": 200,
        "appUserId": result.get("appUserId") or user.get("appUserId"),
        "roleId": result.get("roleId") or user.get("roleId"),
        "phoneNum": user.get("phoneNum") or result.get("phoneNum"),
        "rolePhoneNum": user.get("rolePhoneNum") or result.get("rolePhoneNum"),
        "direct_login": result.get("direct_login", False),
    }


def main():
    if len(sys.argv) not in (2, 3):
        print(json.dumps({"code": -1, "msg": "用法: ./debug_utils/login.py <学号/工号> [密码]"},
                         ensure_ascii=False))
        sys.exit(1)

    result = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("code") == 200 else 1)


if __name__ == "__main__":
    main()
