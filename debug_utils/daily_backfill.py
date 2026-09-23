#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按天回填用量（前 N 天 / 指定区间），写进 usage_daily 表。

实际抓取逻辑在 server/libs/usage_backfill.py，与后端 worker 共用同一份代码
（面板「统计表格」页签也会自动触发同一个后台任务，不用手工跑这个脚本）。

为什么要一天一次请求：本校部署的能耗曲线接口 /external/bill/getUsageListByNodeId
长期无响应（每次都是网关超时），能用的只有 POST /external/bill ——
它只返回「某个区间」的合计（totalUsage / lastDayUsage / lastTwoDayUsage），没有日序列。
所以想要按天数据，只能一天发一次请求。

用法：
    ./debug_utils/daily_backfill.py --mine <学号> --days 30
    ./debug_utils/daily_backfill.py --device 6166 --days 30
    ./debug_utils/daily_backfill.py --building 72 --days 30 --only-electric
    ./debug_utils/daily_backfill.py --device 6166 --start 2026-08-24 --end 2026-09-23

请求量 = 设备数 × 天数；已存在的 (设备, 日期) 会跳过，可随时中断再跑、接着补。
"""

import sys
import os
import time
import configparser
import argparse
from datetime import datetime

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '..', 'server'))

import pymysql

from libs import usage_backfill as ub
from libs.api_client import fetch_devices_by_building, get_account_list, login


def load_mysql_config():
    path = os.path.join(_root, 'data2sql', 'config', 'mysql.ini')
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding='utf-8')
    if not cfg.has_section('mysql'):
        raise FileNotFoundError(f"没读到 MySQL 配置: {path}")
    return {
        'host': cfg.get('mysql', 'mysql_server'),
        'port': cfg.getint('mysql', 'mysql_port'),
        'user': cfg.get('mysql', 'login_user'),
        'password': cfg.get('mysql', 'login_passwd'),
        'database': cfg.get('mysql', 'db_schema'),
    }


def build_targets(args):
    """返回 [(device_id, device_name), ...]"""
    if args.device:
        return [(args.device, args.device)]
    if args.building:
        res = fetch_devices_by_building(args.building)
        rows = res.get('rows') or []
        if args.only_electric:
            rows = [r for r in rows if str(r.get('equipmentType')) == '0']
        return [(str(r.get('id')), r.get('equipmentName')) for r in rows]
    if args.mine:
        lg = login(args.mine)
        user = lg.get('user') or {}
        acc = get_account_list(user.get('appUserId'), str(user.get('roleId')))
        rows = acc.get('rows') or []
        return [(str(r.get('id')), r.get('equipmentName') or r.get('acctName')) for r in rows]
    raise SystemExit("要指定 --device / --building / --mine 中的一个")


def main():
    ap = argparse.ArgumentParser(description="按天回填用量到 usage_daily")
    ap.add_argument('--device', help='单个设备 id')
    ap.add_argument('--building', help='楼栋号，如 72（整栋）')
    ap.add_argument('--mine', help='学号：只处理自己名下的表')
    ap.add_argument('--days', type=int, default=30, help='回填最近多少天（默认 30，含昨天）')
    ap.add_argument('--start', help='起始日期 YYYY-MM-DD（给则忽略 --days）')
    ap.add_argument('--end', help='结束日期 YYYY-MM-DD（默认昨天）')
    ap.add_argument('--only-electric', action='store_true', help='只处理电表')
    ap.add_argument('--sleep', type=float, default=0.3, help='每次请求之间的间隔秒数（默认 0.3）')
    ap.add_argument('--yes', action='store_true',
                    help='（保留参数：请求量只提示、不再拦截）')
    args = ap.parse_args()

    if args.start:
        days = ub.daterange(args.start, args.end or datetime.now().date().isoformat())
    else:
        days = ub.last_n_days(args.days, args.end)

    targets = build_targets(args)
    print(f"目标设备 {len(targets)} 台 × 天数 {len(days)} = 最多 {len(targets) * len(days)} 次请求"
          f"（区间 {days[0]} ~ {days[-1]}，实测单次约 2 秒）")

    cfg = load_mysql_config()
    conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                           password=cfg['password'], database=cfg['database'],
                           charset='utf8mb4', autocommit=True)
    ub.ensure_table(conn)

    jobs = ub.missing_jobs(conn, [t[0] for t in targets], days[0], days[-1])
    names = {str(i): n for i, n in targets}
    print(f"其中需要补 {len(jobs)} 条（已存在的会跳过）")

    t0 = time.time()

    def on_progress(st):
        dev, day = jobs[st['done'] - 1]
        print(f"  [{st['done']}/{st['total']}] {names.get(dev, dev)} ({dev}) {day} "
              f"成功 {st['ok']} 失败 {st['fail']} 已用 {st['elapsed']}s")

    stats = ub.run_jobs(conn, jobs, pace=args.sleep, on_progress=on_progress)
    conn.close()
    print(f"\n完成：成功 {stats['ok']} 条，失败 {stats['fail']} 条，"
          f"用时 {stats['elapsed'] or round(time.time() - t0, 1)} 秒"
          + (f"，最后错误：{stats['last_error']}" if stats.get('last_error') else ""))


if __name__ == '__main__':
    main()
