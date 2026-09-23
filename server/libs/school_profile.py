#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server/libs/school_profile.py

学校配置档案（school profile）。

本项目和「三一工学院版」共用同一套厂商接口（RuoYi 二次开发的水电缴费系统），
不同学校之间只有三处不同，全部集中在这里，避免散落在各处的硬编码：

  1. base_url                —— 学校 API 主机与路径前缀
  2. dorm_device_name_like   —— 宿管模式按楼栋筛设备时的表名 LIKE 模板
  3. dorm_buildings          —— 宿管模式前端可选的楼栋列表

切换学校：把 ACTIVE 改成下面的某个 key 即可（或用环境变量 SCHOOL_PROFILE 覆盖）。

新增学校：照抄一份字典，改这三处即可，其余代码无需改动。

楼栋列表怎么来的：跑 `python3 debug_utils/list_buildings.py`，
它会按学校 API 的分页把设备名扫一遍，打印出「楼栋前缀 + 数量」，
把结果填进 dorm_buildings 即可（设备多的学校建议一次只扫几页，服务端会限流/超时）。
"""

import os
import re

PROFILES = {
    # 湖南工业大学（本校，2026-09 实测适配）
    "hnuit": {
        "school_name": "湖南工业大学",
        "base_url": "https://sdjf.hnuit.edu.cn/prod-api/external",
        # 设备名形如：12-0101室电表 / 102-0101室水表
        "dorm_device_name_like": "{building}-%室电表",
        # 楼栋号只校验格式，不写死白名单（工位数 > 999 的学校可放宽）
        "building_pattern": r"^\d{1,3}$",
        # 兜底楼栋（HTML 静态按钮用）；运行时以 mode=list_buildings 的数据库统计为准。
        # 由 `python3 debug_utils/list_buildings.py` 扫描得到（服务端分页会超时，可能要扫几次）
        "dorm_buildings": ["102", "105", "104", "101", "91", "57", "17"],
        "login_note": (
            "本校使用统一身份认证（CAS）登录 sdjf 子系统；"
            "POST /appUser/login 仅对在校园 App 内注册过的账号可用，"
            "未注册会返回 code=202「该账号尚未注册」——此时用 get_user_by_phone() "
            "（GET /appUser/{学号}，无需密码）解析 appUserId/roleId。"
        ),
    },

    # 三一工学院（原版项目的目标学校，保留以便对照/回切）
    "sany": {
        "school_name": "三一工学院",
        "base_url": "http://sywap.funsine.com/prod-api/external",
        # 设备名形如：学1栋101室电表
        "dorm_device_name_like": "学{building}栋%电表",
        "building_pattern": r"^(1|2|3|5|6|7|8|9|10)$",
        "dorm_buildings": ["1", "2", "3", "5", "6", "7", "8", "9", "10"],
        "login_note": "原版：POST /appUser/login（手机号 + MD5 密码）直连登录。",
    },
}

# 当前使用的学校档案（可用环境变量覆盖，方便同一份代码在多校之间切换）
ACTIVE = os.environ.get("SCHOOL_PROFILE", "hnuit")

if ACTIVE not in PROFILES:
    raise RuntimeError(
        "未知的学校档案 %r，可选：%s" % (ACTIVE, ", ".join(sorted(PROFILES)))
    )


def get(key, default=None):
    """读取当前学校档案里的某个配置项。"""
    return PROFILES[ACTIVE].get(key, default)


def dorm_device_like(building):
    """
    返回宿管模式按楼栋筛选设备名时用的 SQL LIKE 模板。

    注意：本项目 SQL 里用的是 LIKE（区分大小写取决于排序规则），
    调用方需自行拼接 % 通配符，例如：
        pattern = school_profile.dorm_device_like("102")   # -> "102-%室电表"
    """
    return get("dorm_device_name_like").format(building=building)


_BUILDING_RE_CACHE = {}


def building_prefix_regex():
    """
    把 dorm_device_name_like 模板编译成「提取楼栋前缀」的正则（带缓存）。

        "{building}-%室电表"  ->  ^([0-9]+)\-.*室电表$
        "学{building}栋%电表" ->  ^学([0-9]+)栋.*电表$
    """
    template = get("dorm_device_name_like")
    if template not in _BUILDING_RE_CACHE:
        pattern = re.escape(template)
        pattern = pattern.replace(re.escape("{building}"), "([0-9]+)")
        pattern = pattern.replace("%", ".*")
        _BUILDING_RE_CACHE[template] = re.compile("^" + pattern + "$")
    return _BUILDING_RE_CACHE[template]


def parse_building(device_name):
    """从设备名里解析出楼栋前缀；不匹配返回 None。"""
    match = building_prefix_regex().match(device_name or "")
    return match.group(1) if match else None
