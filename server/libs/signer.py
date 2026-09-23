#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server/libs/signer.py

学校 API 签名算法和工具函数。
所有调用学校接口的模块都应从此导入，以确保签名逻辑集中统一。

签名算法（来自学校前端 JS）：
  1. 按参数名字典序排序
  2. 拼接为 KEY=VALUE& 格式（KEY 和 VALUE 均大写）
  3. 末尾追加签名密钥 SIGN_KEY
  4. 整体 MD5 加密得到 sign 值
"""

import hashlib
import time

try:                                   # 作为包导入（正常情况）
    from .school_profile import get as _profile
except ImportError:                    # 直接运行本文件做签名自检时
    from school_profile import get as _profile

# 签名密钥（硬编码，不可泄漏）
SIGN_KEY = "DJKSBNW123"

# 学校 API 基础地址（来自学校档案，切换学校只改 school_profile.py）
BASE_URL = _profile("base_url")

# 固定请求头
DEFAULT_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 Edg/141.0.0.0"
}

# 固定渠道 ID
CHANNEL_ID = "1003"


def generate_sign(params: dict) -> dict:
    """
    为参数字典生成签名（直接加到原字典返回）。

    算法（与前端 axios 拦截器逐字符一致）：
        sign = md5( "大写KEY=大写VALUE&" 按 KEY 排序拼接 + SIGN_KEY )
    注意每个键值对后面都带 &，包括最后一个 —— 漏掉结尾的 & 会导致
    /external/equipment/list 这类**校验签名**的接口返回 201 验证签名失败！
    （/external/appUserAcct/list 不校验签名，所以这个 bug 很容易被忽略。）

    Args:
        params: 请求参数字典

    Returns:
        包含 sign 字段的完整参数字典
    """
    params_copy = params.copy()
    sorted_params = sorted(params_copy.items())
    param_str = "".join([f"{k.upper()}={str(v).upper()}&" for k, v in sorted_params])
    sign_str = param_str + SIGN_KEY
    md5 = hashlib.md5()
    md5.update(sign_str.encode('utf-8'))
    params_copy['sign'] = md5.hexdigest()
    return params_copy


def get_timestamp() -> str:
    """返回当前时间戳字符串，格式 YYYYMMDDHHmmss"""
    return time.strftime("%Y%m%d%H%M%S")


def md5_encrypt(text: str) -> str:
    """对输入文本做 UTF-8 MD5 加密，返回十六进制字符串"""
    md5 = hashlib.md5()
    md5.update(text.encode('utf-8'))
    return md5.hexdigest()

if __name__ == "__main__":
    # 自检：钉住「待签名串」的格式（这正是本项目原来出错的地方）
    #   md5( "大写KEY=大写VALUE&" 排序拼接 + SIGN_KEY )   ← 每个键值对都带 &，含最后一个
    _key = "DJKSBNW123"
    _cases = [
        ({"channelid": "1003", "timestamp": "20260923104642"},
         "CHANNELID=1003&TIMESTAMP=20260923104642&DJKSBNW123",
         "5d7238bcc6174135b7955915a334da2f"),
        ({"appUserId": "20240000000000", "channelid": "1003", "roleId": "2",
          "timestamp": "20260923104642"},
         "APPUSERID=20240000000000&CHANNELID=1003&ROLEID=2&TIMESTAMP=20260923104642&DJKSBNW123",
         "8fda8eca28f78aaddb0672c13d46fcf2"),
        ({"channelid": "1003", "isPayfee": "0", "roleId": "2",
          "timestamp": "20260923104642", "treeType": "0"},
         "CHANNELID=1003&ISPAYFEE=0&ROLEID=2&TIMESTAMP=20260923104642&TREETYPE=0&DJKSBNW123",
         "724b7a3f159755fbc21c0038e6341dd4"),
    ]
    _ok = True
    for _params, _expect_str, _expect_sign in _cases:
        _str = "".join(f"{k.upper()}={str(v).upper()}&"
                       for k, v in sorted(_params.items())) + _key
        _got = generate_sign(_params)["sign"]
        _good = (_str == _expect_str) and (_got == _expect_sign)
        _ok &= _good
        print(f"{'OK  ' if _good else 'FAIL'} {sorted(_params)}")
        if not _good:
            print(f"     待签名串: {_str!r}")
    # 回归护栏：漏掉结尾 & 的旧实现必须产出不同的签名
    _old = "&".join(f"{k.upper()}={str(v).upper()}"
                    for k, v in sorted(_cases[0][0].items())) + _key
    _old_sign = hashlib.md5(_old.encode("utf-8")).hexdigest()
    print(f"{'OK  ' if _old_sign != _cases[0][2] else 'FAIL'} 旧实现（漏结尾&）签名不同: {_old_sign}")
    raise SystemExit(0 if _ok and _old_sign != _cases[0][2] else 1)
