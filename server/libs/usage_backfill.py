# -*- coding: utf-8 -*-
"""
按天用量的抓取与入库（后端 worker 与命令行脚本共用）。

为什么要一天一次请求：本校部署的能耗曲线接口 /external/bill/getUsageListByNodeId
长期无响应（每次都是网关超时），仅有 POST /external/bill 可用，而它只返回「某个区间」的
合计（totalUsage / lastDayUsage / lastTwoDayUsage），没有日序列。所以按天数据只能一天一次请求。

本模块不做任何限流策略决策，只暴露：
    query_day_usage(device_id, day)                -> dict  单天用量
    missing_jobs(conn, device_ids, start, end)     -> list[(device_id, day)]  还缺哪些
    run_jobs(conn, jobs, ...)                      -> dict  顺序跑完，带回调进度
"""

import time
from datetime import date, datetime, timedelta

import requests

from .signer import generate_sign, get_timestamp, CHANNEL_ID, BASE_URL, DEFAULT_HEADERS

# 单次请求超时（实测正常约 2 秒，偶发几十秒；太长会把 worker 卡住）
REQUEST_TIMEOUT = 60


def query_day_usage(device_id, day, timeout=REQUEST_TIMEOUT):
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


def daterange(start, end):
    """['2026-08-24', ...] 闭区间。"""
    if isinstance(start, str):
        start = datetime.strptime(start, '%Y-%m-%d').date()
    if isinstance(end, str):
        end = datetime.strptime(end, '%Y-%m-%d').date()
    out, cur = [], start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def last_n_days(days, end=None):
    """最近 days 天（含 end，默认昨天）。"""
    end = end or (date.today() - timedelta(days=1))
    if isinstance(end, str):
        end = datetime.strptime(end, '%Y-%m-%d').date()
    return daterange(end - timedelta(days=int(days) - 1), end)


def table_exists(conn, name='usage_daily'):
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (name,))
        return cur.fetchone() is not None


def ensure_table(conn):
    """表不存在就建；排序规则必须与 device/data 一致（MySQL 26.7 默认 0900_ai_ci 会 join 报 1267）。"""
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
        cur.execute("SELECT TABLE_COLLATION FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'usage_daily'")
        row = cur.fetchone()
        if row and row[0] != 'utf8mb4_general_ci':
            print(f"[usage_backfill] usage_daily 排序规则为 {row[0]}，对齐到 utf8mb4_general_ci")
            cur.execute("ALTER TABLE usage_daily CONVERT TO CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_general_ci")
    conn.commit()


def existing_map(conn, device_ids, start=None, end=None):
    """返回 {device_id: {day(date), ...}}，避免重复请求。"""
    ids = [str(d) for d in device_ids]
    out = {d: set() for d in ids}
    if not ids:
        return out
    sql = "SELECT device_id, day FROM usage_daily WHERE device_id IN (%s)" % \
          ",".join(["%s"] * len(ids))
    params = list(ids)
    if start:
        sql += " AND day >= %s"
        params.append(start)
    if end:
        sql += " AND day <= %s"
        params.append(end)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for dev, day in cur.fetchall():
            out.setdefault(str(dev), set()).add(day)
    return out


def missing_jobs(conn, device_ids, start, end):
    """还缺哪些 (device_id, day)：按设备顺序、天数升序。"""
    days = daterange(start, end)
    have = existing_map(conn, device_ids, start, end)
    jobs = []
    for dev in [str(d) for d in device_ids]:
        h = have.get(dev, set())
        for d in days:
            if datetime.strptime(d, '%Y-%m-%d').date() not in h:
                jobs.append((dev, d))
    return jobs


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


def run_jobs(conn, jobs, pace=0.3, on_progress=None, should_stop=None,
             max_consecutive_failures=15):
    """
    顺序跑完 jobs。backs off：连续失败到阈值就停下（避免对方限流时继续硬打）。

    on_progress(dict) 每次拿到 OK/失败都会回调一次，便于前端看进度。
    should_stop() 返回 True 时优雅停止（服务退出/用户取消）。
    """
    stats = {'ok': 0, 'fail': 0, 'skipped': 0, 'total': len(jobs),
             'consecutive_failures': 0, 'stopped': False, 'last_error': None}
    t0 = time.time()
    for i, (dev, day) in enumerate(jobs, 1):
        if should_stop and should_stop():
            stats['stopped'] = True
            break
        try:
            j = query_day_usage(dev, day)
        except Exception as e:
            stats['fail'] += 1
            stats['consecutive_failures'] += 1
            stats['last_error'] = f"{type(e).__name__}: {e}"
        else:
            if j.get('code') == 200:
                upsert_usage(conn, dev, day, j.get('totalUsage'),
                             j.get('useMoney') or j.get('totalMoney'),
                             j.get('lastTwoDayUsage'))
                stats['ok'] += 1
                stats['consecutive_failures'] = 0
            else:
                stats['fail'] += 1
                stats['consecutive_failures'] += 1
                stats['last_error'] = f"code={j.get('code')} {j.get('msg')}"
        stats['done'] = i
        stats['elapsed'] = round(time.time() - t0, 1)
        if on_progress:
            try:
                on_progress(dict(stats))
            except Exception as e:            # 回调不该影响抓取
                print(f"[usage_backfill] 进度回调异常: {e}")
        if stats['consecutive_failures'] >= max_consecutive_failures:
            stats['stopped'] = True
            stats['last_error'] = (stats['last_error'] or '') + \
                f"（连续失败 {stats['consecutive_failures']} 次，已停止）"
            break
        if i < len(jobs) and pace:
            time.sleep(pace)
    stats['elapsed'] = round(time.time() - t0, 1)
    return stats
