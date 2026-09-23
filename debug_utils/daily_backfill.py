#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按天回填用量（前 N 天 / 指定区间），写进 usage_daily 表。

为什么要一天一次请求：本校部署上的能耗曲线接口 /external/bill/getUsageListByNodeId
长期无响应（每次都是网关超时），能用的只有 POST /external/bill ——
它只返回「某个区间」的合计（totalUsage / lastDayUsage / lastTwoDayUsage），没有日序列。
所以想要按天数据，只能一天发一次请求。

用法：
    # 自己的表（先 login.py 拿 appUserId/roleId，或直接给设备 id）
    ./debug_utils/daily_backfill.py --mine <学号> --days 30
    ./debug_utils/daily_backfill.py --device 6166 --days 30
    # 整栋楼（注意请求量 = 设备数 × 天数，务必看提示）
    ./debug_utils/daily_backfill.py --building 72 --days 30 --only-electric
    # 指定区间
    ./debug_utils/daily_backfill.py --device 6166 --start 2026-08-24 --end 2026-09-23

注意：
  * 请求量 = 设备数 × 天数，超过 60 次会要求加 --yes 确认（默认只打提示不跑）；
  * 每次请求实测 0.6 ~ 60 秒不等，脚本按 --sleep 间隔限速，避免给学校服务器压力；
  * 已存在的 (device_id, day) 会跳过，可以中断后再跑，接着补。
"""

import sys
import os
import time
import argparse
from datetime import datetime, timedelta

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '..', 'server'))

import pymysql

from libs.signer import generate_sign, get_timestamp, CHANNEL_ID, BASE_URL, DEFAULT_HEADERS
from libs.api_client import fetch_devices_by_building, get_account_list
import requests


def load_mysql_config():
    import configparser
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


def q_usage(device_id, day, timeout=75):
    """查某台设备某一天的用量（POST /external/bill，签名放 body）。"""
    body = {
        "id": str(device_id),
        "startDate": f"{day} 00:00:00",
        "endDate": f"{day} 23:59:59",
    }
    body.update({"channelid": CHANNEL_ID, "timestamp": get_timestamp()})
    r = requests.post(f"{BASE_URL}/bill", json=generate_sign(body),
                      headers=DEFAULT_HEADERS, timeout=timeout)
    return r.json()


def ensure_table(conn):
    """表不存在就建（等同 doc/sql/usage_daily_table.sql）。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS `usage_daily` (
      `id` INT NOT NULL AUTO_INCREMENT,
      `device_id` VARCHAR(32) NOT NULL,
      `day` DATE NOT NULL,
      `usage_amount` DECIMAL(12,4) DEFAULT NULL,
      `use_money` DECIMAL(12,4) DEFAULT NULL,
      `last_two_day_usage` DECIMAL(12,4) DEFAULT NULL,
      `source` VARCHAR(16) NOT NULL DEFAULT 'bill',
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uk_device_day` (`device_id`, `day`),
      KEY `idx_day` (`day`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
        # 排序规则必须与 device/data 表一致（MySQL 26.7 默认 0900_ai_ci 会 join 报 1267）
        cur.execute("SELECT TABLE_COLLATION FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'usage_daily'")
        row = cur.fetchone()
        if row and row[0] != 'utf8mb4_general_ci':
            print(f"usage_daily 排序规则为 {row[0]}，对齐到 utf8mb4_general_ci")
            cur.execute("ALTER TABLE usage_daily CONVERT TO CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_general_ci")
    conn.commit()


def existing_days(conn, device_id):
    with conn.cursor() as cur:
        cur.execute("SELECT day FROM usage_daily WHERE device_id = %s", (str(device_id),))
        return {row[0] for row in cur.fetchall()}


def upsert_usage(conn, device_id, day, usage, use_money=None, last_two=None, source='bill'):
    sql = """
    INSERT INTO usage_daily (device_id, day, usage_amount, use_money, last_two_day_usage, source)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE usage_amount=VALUES(usage_amount), use_money=VALUES(use_money),
        last_two_day_usage=VALUES(last_two_day_usage), source=VALUES(source)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (str(device_id), day, usage, use_money, last_two, source))
    conn.commit()


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
        sys.path.insert(0, os.path.join(_root, '..', 'server'))
        from libs.api_client import login
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
    ap.add_argument('--days', type=int, default=30, help='回填最近多少天（默认 30，含今天）')
    ap.add_argument('--start', help='起始日期 YYYY-MM-DD（给则忽略 --days）')
    ap.add_argument('--end', help='结束日期 YYYY-MM-DD（默认昨天）')
    ap.add_argument('--only-electric', action='store_true', help='只处理电表')
    ap.add_argument('--sleep', type=float, default=1.0, help='每次请求之间的间隔秒数（默认 1）')
    ap.add_argument('--yes', action='store_true', help='请求数超过 60 时确认执行')
    args = ap.parse_args()

    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end \
        else (datetime.now().date() - timedelta(days=1))
    if args.start:
        start = datetime.strptime(args.start, '%Y-%m-%d').date()
    else:
        start = end - timedelta(days=args.days - 1)
    days = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)

    targets = build_targets(args)
    total_calls = len(targets) * len(days)
    print(f"目标设备 {len(targets)} 台 × 天数 {len(days)} = 最多 {total_calls} 次请求"
          f"（区间 {days[0]} ~ {days[-1]}）")
    if total_calls > 60 and not args.yes:
        print("请求数较多：如果这不是你想要的量，先缩小 --days 或加 --only-electric；")
        print("确认要跑就加 --yes（比如整栋楼 260 台 × 30 天 = 7800 次，实测单次 0.6~60 秒）。")
        sys.exit(2)

    cfg = load_mysql_config()
    conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'],
                           password=cfg['password'], database=cfg['database'], charset='utf8mb4')
    ensure_table(conn)

    ok = skip = fail = 0
    t0 = time.time()
    for idx, (dev, name) in enumerate(targets, 1):
        have = existing_days(conn, dev)
        todo = [d for d in days if datetime.strptime(d, '%Y-%m-%d').date() not in have]
        if not todo:
            print(f"[{idx}/{len(targets)}] {name} ({dev}) 已齐全，跳过")
            skip += len(days)
            continue
        print(f"[{idx}/{len(targets)}] {name} ({dev}) 待补 {len(todo)} 天")
        for d in todo:
            try:
                j = q_usage(dev, d)
            except Exception as e:
                fail += 1
                print(f"    {d} 请求失败: {type(e).__name__}")
                continue
            if j.get('code') != 200:
                fail += 1
                print(f"    {d} 接口返回 code={j.get('code')} {j.get('msg')}")
                continue
            upsert_usage(conn, dev, d, j.get('totalUsage'),
                         j.get('useMoney') or j.get('totalMoney'),
                         j.get('lastTwoDayUsage'))
            ok += 1
            print(f"    {d} 用量={j.get('totalUsage')}（前一日 {j.get('lastTwoDayUsage')}）")
            time.sleep(args.sleep)
    conn.close()

    print(f"\n完成：写入/更新 {ok} 条，跳过（已存在）{skip} 条，失败 {fail} 条，"
          f"用时 {time.time()-t0:.1f} 秒")


if __name__ == '__main__':
    main()
