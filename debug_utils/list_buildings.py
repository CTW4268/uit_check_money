#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug_utils/list_buildings.py

扫描学校设备台账，统计「设备名前缀（楼栋）」分布，用于填写
server/libs/school_profile.py 里的 dorm_buildings（宿管模式可选楼栋）。

用法：
    ./debug_utils/list_buildings.py [roleKey] [页数] [每页条数]

    默认: roleKey=2, 页数=5, 每页=500（服务端对大的 pageSize 会超时，建议 <= 500）

输出示例：
    楼栋前缀     数量   示例前缀匹配规则
    102         637    102-%室电...
    105         447    ...

只统计前缀，不打印任何具体房间的读数。
"""

import sys
import os
import re
import json
from collections import Counter

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '..', 'server'))

from libs.api_client import get_device_list          # noqa: E402
from libs.school_profile import ACTIVE, get as profile_get   # noqa: E402


def main():
    role_key = sys.argv[1] if len(sys.argv) > 1 else "2"
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    etype = sys.argv[4] if len(sys.argv) > 4 else "0"     # 0=电表, 1=水表

    prefix = Counter()
    seen = 0
    failed = 0
    for page in range(1, pages + 1):
        result = get_device_list("0", role_key, page, size)
        rows = result.get("rows") or []
        if result.get("code") != 200 or not rows:
            failed += 1
            break
        for row in rows:
            name = row.get("equipmentName") or ""
            match = re.match(r"^(\d+)-", name)
            if match:
                prefix[match.group(1)] += 1
        seen += len(rows)

    print(f"学校档案: {ACTIVE}   roleKey={role_key}  设备类型={'电表' if etype == '0' else '水表'}")
    print(f"扫描 {seen} 条设备（失败页 {failed}）")
    print(f"{'楼栋前缀':<10}{'数量':<8}建议写入 dorm_buildings")
    for key, count in prefix.most_common():
        print(f"{key:<12}{count:<8}\"{key}\"")
    print()
    print("把上面的前缀填进 server/libs/school_profile.py 的 dorm_buildings 即可；")
    print("宿管模式的 SQL 匹配模板是: %s" % profile_get("dorm_device_name_like"))
    print(json.dumps({"profile": ACTIVE, "scanned": seen, "prefixes": dict(prefix)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
