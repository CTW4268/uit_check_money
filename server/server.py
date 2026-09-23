#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import configparser
import pymysql
import random
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import sys
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
import atexit
from queue import Queue
import re
from datetime import datetime, date, timedelta
from contextlib import contextmanager

# 学校档案（宿管模式的楼栋规则随学校不同，集中放在 libs/school_profile.py）
from libs import school_profile

# 按天用量的抓取/入库（后端自动补数 worker 与命令行脚本共用）
from libs import usage_backfill as ub

# 导入 data_cleaner 清洗算法
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'data_cleaner'))
from hourly_report import (build_24h_hours, build_slot_max, fill_missing_slots,
                           compute_usage_series, smooth_zero_usage,
                           get_anchor_max, fetch_raw_readings)

# 读取配置文件
config = configparser.ConfigParser()
config.read(os.path.join(os.path.dirname(__file__), 'server.ini'))

# 数据库配置
DB_HOST = config.get('mysql', 'mysql_server')
DB_PORT = int(config.get('mysql', 'mysql_port'))
DB_USER = config.get('mysql', 'login_user')
DB_PASSWORD = config.get('mysql', 'login_passwd')
DB_NAME = config.get('mysql', 'db_schema')

# 服务器配置
SERVER_PORT = int(config.get('server', 'port'))

# 首页显示配置
def stats_table_to_csv(result):
    """把统计表格结果转成 CSV 文本（带 BOM，Excel 直接打开不乱码）。"""
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator='\n')
    days = result.get('days', 30)
    w.writerow(["楼层", "寝室号", "类型", "设备名", "安装位置", "单价", "余额",
                f"近{days}天用量", f"近{days}天有数据天数", "最近更新时间", "设备id"])
    for d in result.get("devices", []):
        w.writerow([
            "" if d.get("floor") is None else d["floor"],
            d.get("room") or "",
            d.get("kind") or "",
            d.get("equipmentName") or "",
            d.get("installationSite") or "",
            "" if d.get("rate") is None else d["rate"],
            "" if d.get("balance") is None else d["balance"],
            "" if d.get("usage_sum") is None else d["usage_sum"],
            d.get("usage_days", 0),
            d.get("last_time") or "",
            d.get("device_id") or "",
        ])
    return buf.getvalue()


def _s(v):
    """值转字符串，但 None 保持 None（发 JSON null）。

    原来宿管模式里直接写 str(device_row[7])，数据库里 NULL 会变成字符串 "None"
    传给前端，前端既认不出 null 也认不出 0，导致整栋楼只剩「有状态那一台」。
    """
    return None if v is None else str(v)


FIRST_SCREEN_COUNT = int(config.get('config', 'first_screen_count'))

# 可选：首屏固定显示这些设备 id（逗号分隔），一般用来钉住自己宿舍的那几台。
# 库里只有自己的表时首屏是「随机 6 台」= 全部；一旦入了整栋楼，随机就会挑到别人房间。
FIRST_SCREEN_DEVICES = [x.strip() for x in
                        config.get('config', 'first_screen_devices', fallback='').split(',')
                        if x.strip()]

# 创建线程池
executor = ThreadPoolExecutor(max_workers=10)

# 连接池设置
CONNECTION_POOL_SIZE = int(config.get('mysql', 'connection_pool_size', fallback=30))
connection_pool = Queue(maxsize=CONNECTION_POOL_SIZE)
connection_lock = threading.Lock()

class BackfillManager:
    """
    按天用量的后台自动抓取（单线程 + 限速 + 可中断 + 进度可查）。

    为什么放后端跑：本校的曲线接口永久超时，按天数据只能「一台设备一天一次请求」，
    整栋楼 30 天是几千次请求，不能在 HTTP 请求里同步跑完；所以丢进后台线程，
    前端轮询 backfill_status 看进度，跑完自动刷新表格。
    已入库的 (设备, 日期) 会跳过，所以中断/重启后是接着补。
    """

    def __init__(self):
        # 必须是可重入锁：start() 持锁时会顺带取状态，普通 Lock 会直接死锁
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self.state = {
            "running": False, "cancelled": False, "building": None, "days": 0,
            "start_day": None, "end_day": None, "total": 0, "done": 0,
            "ok": 0, "fail": 0, "elapsed": 0, "started_at": None,
            "finished_at": None, "last_error": None, "device_count": 0,
        }

    # ---------- 对外 ----------
    def status(self):
        with self._lock:
            st = dict(self.state)
        st["eta_seconds"] = None
        if st["running"] and st["done"] > 0 and st["total"] > st["done"]:
            per = st["elapsed"] / st["done"]
            st["eta_seconds"] = int(per * (st["total"] - st["done"]))
        return st

    def start(self, building, days=30, kinds=("electric",)):
        """kinds: electric / water / both；已有任务在跑就直接返回当前状态。"""
        with self._lock:
            if self.state["running"]:
                busy = True
            else:
                busy = False
                self._stop.clear()
                self.state.update({
                    "running": True, "cancelled": False, "building": str(building),
                    "days": int(days), "start_day": None, "end_day": None,
                    "total": 0, "done": 0, "ok": 0, "fail": 0, "elapsed": 0,
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": None, "last_error": None, "device_count": 0,
                })
        if busy:
            # 注意：状态查询要放在锁外面（即使锁可重入，也别在持有锁时做别的事）
            return {"started": False, "reason": "已有补数任务在跑", **self.status()}
        t = threading.Thread(target=self._run, args=(str(building), int(days), tuple(kinds)),
                             daemon=True, name="usage-backfill")
        self._thread = t
        t.start()
        return {"started": True, **self.status()}

    def stop(self):
        self._stop.set()
        return {"stopping": True, **self.status()}

    # ---------- 内部 ----------
    @staticmethod
    def _devices(building, kinds):
        """从库里取设备（不额外打接口）：电表 equipmentType=0，水表=1。"""
        like = school_profile.dorm_device_like(building).replace("电表", "%表")
        want = []
        if "electric" in kinds or "both" in kinds:
            want.append("0")
        if "water" in kinds or "both" in kinds:
            want.append("1")
        conn = DatabaseManager.create_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM device WHERE equipmentName LIKE %s "
                    "AND (%s) ORDER BY equipmentName" %
                    ("%s", " OR ".join(["equipmentType = %s"] * len(want))),
                    [like] + want)
                return [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    def _run(self, building, days, kinds):
        conn = None
        try:
            device_ids = self._devices(building, kinds)
            day_list = ub.last_n_days(days)
            with self._lock:
                self.state.update({"device_count": len(device_ids),
                                   "start_day": day_list[0], "end_day": day_list[-1]})
            print(f"[backfill] 楼栋 {building} 设备 {len(device_ids)} 台 × {len(day_list)} 天 "
                  f"({day_list[0]} ~ {day_list[-1]})")

            if not device_ids:
                with self._lock:
                    self.state.update({"last_error": "库里没有该楼栋的设备，先跑 data2sql 采集台账",
                                       "running": False, "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                return

            conn = DatabaseManager.create_connection()
            ub.ensure_table(conn)
            jobs = ub.missing_jobs(conn, device_ids, day_list[0], day_list[-1])
            with self._lock:
                self.state["total"] = len(jobs)
            print(f"[backfill] 需要补 {len(jobs)} 条（已存在的会跳过）")

            def on_progress(st):
                with self._lock:
                    self.state.update({"done": st["done"], "ok": st["ok"],
                                       "fail": st["fail"], "elapsed": st["elapsed"],
                                       "last_error": st["last_error"]})
                if st["done"] % 50 == 0:
                    print(f"[backfill] 进度 {st['done']}/{st['total']} 成功 {st['ok']} 失败 {st['fail']}")

            stats = ub.run_jobs(conn, jobs, pace=0.3, on_progress=on_progress,
                                should_stop=self._stop.is_set)
            with self._lock:
                self.state.update({"ok": stats["ok"], "fail": stats["fail"],
                                   "done": stats["done"], "elapsed": stats["elapsed"],
                                   "cancelled": bool(stats.get("stopped")),
                                   "last_error": stats.get("last_error")})
            print(f"[backfill] 结束：成功 {stats['ok']} 失败 {stats['fail']} 用时 {stats['elapsed']} 秒")
        except Exception as e:
            traceback.print_exc()
            with self._lock:
                self.state["last_error"] = f"{type(e).__name__}: {e}"
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            with self._lock:
                self.state["running"] = False
                self.state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


backfill_manager = BackfillManager()


class DatabaseManager:
    @staticmethod
    def create_connection():
        """创建一个新的数据库连接"""
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset='utf8mb4',
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
            autocommit=True
        )
    
    @staticmethod
    def initialize_connection_pool():
        """初始化数据库连接池"""
        print(f"[INFO] 初始化数据库连接池，大小: {CONNECTION_POOL_SIZE}")
        for _ in range(CONNECTION_POOL_SIZE):
            try:
                conn = DatabaseManager.create_connection()
                connection_pool.put(conn)
            except Exception as e:
                print(f"[ERROR] 创建连接池失败: {str(e)}")
                traceback.print_exc()
    
    @staticmethod
    def close_all_connections():
        """关闭所有数据库连接"""
        print("[INFO] 关闭所有数据库连接")
        with connection_lock:
            while not connection_pool.empty():
                try:
                    conn = connection_pool.get_nowait()
                    conn.close()
                except:
                    pass
    
    @staticmethod
    @contextmanager
    def get_connection():
        """获取数据库连接的上下文管理器"""
        conn = None
        try:
            # 获取连接池中的连接
            conn = connection_pool.get(timeout=2)  # 减少超时时间到2秒
            print("[INFO] 从连接池获取数据库连接")
            # 检查连接是否有效
            conn.ping(reconnect=True)
        except:
            print("[WARN] 连接池获取连接超时或连接无效，创建新连接")
            # 如果无法从连接池获取连接，创建新连接
            conn = DatabaseManager.create_connection()
        
        try:
            yield conn
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def release_connection(conn):
        """释放数据库连接"""
        if conn is None:
            return
        try:
            # 检查连接是否有效
            conn.ping(reconnect=True)
            # 将连接放回连接池
            if not connection_pool.full():
                connection_pool.put_nowait(conn)
                print("[INFO] 数据库连接已返回连接池")
            else:
                conn.close()
                print("[INFO] 连接池已满，直接关闭连接")
        except Exception as e:
            print(f"[WARN] 连接无效，丢弃: {str(e)}")
            # 连接无效，直接丢弃
            try:
                conn.close()
            except:
                pass

# 注册退出处理
atexit.register(DatabaseManager.close_all_connections)

# 初始化连接池
DatabaseManager.initialize_connection_pool()

# 数据查询类
class DataQuery:
    @staticmethod
    def get_first_screen_data():
        """首屏数据接口"""
        print(f"[INFO] 开始获取首屏数据，请求数量: {FIRST_SCREEN_COUNT}")
        try:
            with DatabaseManager.get_connection() as conn:
                print("[INFO] 数据库连接建立成功")
                with conn.cursor() as cursor:
                    # 验证FIRST_SCREEN_COUNT是否在安全范围内
                    safe_limit = min(FIRST_SCREEN_COUNT, 100)  # 限制最大返回数量
                    # 随机获取配置数量的设备ID
                    sql = "SELECT id FROM device ORDER BY RAND() LIMIT %s"
                    print(f"[INFO] 执行SQL查询: {sql}")
                    cursor.execute(sql, (safe_limit,))
                    results = cursor.fetchall()
                    device_ids = [str(row[0]) for row in results]
                    if FIRST_SCREEN_DEVICES:
                        # 配置了固定设备就把它们排在最前面，剩余名额继续用随机结果补足
                        device_ids = (FIRST_SCREEN_DEVICES
                                      + [d for d in device_ids if d not in FIRST_SCREEN_DEVICES])[:safe_limit]
                        print(f"[INFO] 已置顶固定设备: {FIRST_SCREEN_DEVICES}")
                    print(f"[INFO] 查询完成，获取到 {len(device_ids)} 个设备ID")
                    
                    response = {
                        "code": "200",
                        "total_num": len(device_ids),
                        "device_ids": device_ids
                    }
                    print(f"[INFO] 首屏数据响应: {response}")
                    return response
        except Exception as e:
            print(f"[ERROR] 获取首屏数据时出错: {str(e)}")
            traceback.print_exc()
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

    @staticmethod
    def get_device_info(cursor, device_id):
        """获取设备基本信息"""
        device_sql = """
            SELECT equipmentName, installationSite, equipmentType, ratio, rate, acctId, status, updated_at, id
            FROM device WHERE id = %s
        """
        print(f"[INFO] 查询设备信息，SQL: {device_sql.strip()}, 参数: {device_id}")
        cursor.execute(device_sql, (device_id,))
        return cursor.fetchone()

    @staticmethod
    def get_device_data(device_id, data_num):
        """检查设备数据接口"""
        print(f"[INFO] 开始获取设备数据，设备ID: {device_id}, 数据数量: {data_num}")
        try:
            with DatabaseManager.get_connection() as conn:
                print(f"[INFO] 数据库连接建立成功")
                with conn.cursor() as cursor:
                    # 获取设备信息
                    device_info = DataQuery.get_device_info(cursor, device_id)
                    
                    if not device_info:
                        print(f"[WARN] 未找到设备ID为 {device_id} 的设备")
                        return {"code": "404", "error": "设备未找到"}
                    print(f"[INFO] 设备信息查询完成")
                    
                    # 获取设备读数数据
                    data_sql = """
                        SELECT device_id, read_time, total_reading, remainingBalance
                        FROM data WHERE device_id = %s ORDER BY read_time DESC LIMIT %s
                    """
                    print(f"[INFO] 查询设备读数数据，SQL: {data_sql.strip()}, 参数: ({device_id}, {data_num})")
                    cursor.execute(data_sql, (device_id, data_num))
                    data_results = cursor.fetchall()
                    print(f"[INFO] 读数数据查询完成，获取到 {len(data_results)} 条记录")
                    
                    # 构造返回数据
                    rows = []
                    for row in data_results:
                        rows.append({
                            "device_id": str(row[0]),
                            "read_time": str(row[1]),
                            "total_reading": str(row[2]),
                            "remainingBalance": str(row[3])
                        })
                    
                    response = {
                        "equipmentName": device_info[0],
                        "device_id": str(device_info[8]),
                        "installationSite": device_info[1],
                        "equipmentType": device_info[2],
                        "ratio": str(device_info[3]),
                        "rate": str(device_info[4]),
                        "acctId": device_info[5],
                        "status": str(device_info[6]),
                        "updated_at": str(device_info[7]),
                        "total": len(rows),
                        "rows": rows,
                        "code": 200
                    }
                    print(f"[INFO] 设备数据响应构建完成")
                    return response
        except Exception as e:
            print(f"[ERROR] 获取设备数据时出错: {str(e)}")
            traceback.print_exc()
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

    @staticmethod
    def search_devices(keyword):
        """搜索设备接口"""
        print(f"[INFO] 开始搜索设备，关键词: {keyword}")
        # 检查关键词长度，2个字符以内包括两个字符不允许查询
        if len(keyword) < 2:
            print(f"[INFO] 关键词长度不足，返回错误提示")
            return {
                "search_status": 1,
                "error_talk": "请输入两个以上的字符。",
                "code": 418
            }
        
        try:
            with DatabaseManager.get_connection() as conn:
                print(f"[INFO] 数据库连接建立成功")
                with conn.cursor() as cursor:
                    # 搜索设备
                    sql = """
                        SELECT equipmentName, installationSite, id, equipmentType, status
                        FROM device WHERE equipmentName LIKE %s OR installationSite LIKE %s
                    """
                    search_term = f"%{keyword}%"
                    print(f"[INFO] 执行搜索查询，SQL: {sql.strip()}, 参数: ({search_term}, {search_term})")
                    cursor.execute(sql, (search_term, search_term))
                    results = cursor.fetchall()
                    print(f"[INFO] 搜索完成，找到 {len(results)} 条记录")
                    
                    # 构造返回数据
                    rows = []
                    for row in results:
                        rows.append({
                            "equipmentName": row[0],
                            "installationSite": row[1],
                            "device_id": str(row[2]).strip(),
                            "equipmentType": str(row[3]),
                            "status": row[4]
                        })
                    
                    response = {
                        "search_status": 0,
                        "total": len(rows),
                        "rows": rows,
                        "code": 200
                    }
                    print(f"[INFO] 搜索响应构建完成: total={len(rows)}")
                    return response
        except Exception as e:
            print(f"[ERROR] 搜索设备时出错: {str(e)}")
            traceback.print_exc()
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

    @staticmethod
    def get_device_daily_range_data(device_id, start_day, end_day):
        """检查设备每日最后数据接口"""
        print(f"[INFO] 开始获取设备每日数据，设备ID: {device_id}, 开始日期: {start_day}, 结束日期: {end_day}")
        try:
            with DatabaseManager.get_connection() as conn:
                print(f"[INFO] 数据库连接建立成功")
                with conn.cursor() as cursor:
                    # 获取设备信息
                    device_info = DataQuery.get_device_info(cursor, device_id)
                    
                    if not device_info:
                        print(f"[WARN] 未找到设备ID为 {device_id} 的设备")
                        return {"code": "404", "error": "设备未找到"}
                    print(f"[INFO] 设备信息查询完成")
                    
                    # 获取设备每日最后读数数据
                    data_sql = """
                        SELECT device_id, DATE(read_time) as read_date, 
                               SUBSTRING_INDEX(GROUP_CONCAT(read_time ORDER BY read_time DESC), ',', 1) as last_read_time,
                               SUBSTRING_INDEX(GROUP_CONCAT(total_reading ORDER BY read_time DESC), ',', 1) as last_total_reading,
                               SUBSTRING_INDEX(GROUP_CONCAT(remainingBalance ORDER BY read_time DESC), ',', 1) as last_remaining_balance
                        FROM data 
                        WHERE device_id = %s AND DATE(read_time) BETWEEN %s AND %s
                        GROUP BY device_id, DATE(read_time)
                        ORDER BY read_date DESC
                    """
                    print(f"[INFO] 查询设备每日最后读数数据，SQL: {data_sql.strip()}, 参数: ({device_id}, {start_day}, {end_day})")
                    cursor.execute(data_sql, (device_id, start_day, end_day))
                    data_results = cursor.fetchall()
                    print(f"[INFO] 读数数据查询完成，获取到 {len(data_results)} 条记录")
                    
                    # 构造返回数据
                    rows = []
                    for row in data_results:
                        rows.append({
                            "device_id": str(row[0]),
                            "read_time": str(row[2]),
                            "total_reading": str(row[3]),
                            "remainingBalance": str(row[4])
                        })
                    
                    response = {
                        "equipmentName": device_info[0],
                        "device_id": str(device_info[8]),
                        "installationSite": device_info[1],
                        "equipmentType": device_info[2],
                        "ratio": str(device_info[3]),
                        "rate": str(device_info[4]),
                        "acctId": device_info[5],
                        "status": str(device_info[6]),
                        "updated_at": str(device_info[7]),
                        "total": len(rows),
                        "rows": rows,
                        "code": 200
                    }
                    print(f"[INFO] 设备每日数据响应构建完成")
                    return response
        except Exception as e:
            print(f"[ERROR] 获取设备每日数据时出错: {str(e)}")
            traceback.print_exc()
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

    @staticmethod
    def get_building_list():
        """
        宿管模式：从 device 表统计楼栋前缀，供前端动态生成楼栋按钮。

        楼栋号随学校不同（本校为 "102-0101室电表" 这类前缀，三一为 "学1栋..."），
        写死在 HTML 里会过时，所以改成按已入库的设备名实时统计。
        匹配规则同样来自学校档案的 dorm_device_name_like。
        """
        try:
            with DatabaseManager.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT equipmentName FROM device WHERE equipmentType = 0")
                    rows = cursor.fetchall()
        except Exception as e:
            print(f"[ERROR] 获取楼栋列表失败: {str(e)}")
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

        counts = {}
        for (name,) in rows:
            building = school_profile.parse_building(name)
            if building:
                counts[building] = counts.get(building, 0) + 1

        buildings = [{"building": b, "count": c}
                     for b, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        print(f"[INFO] 楼栋列表统计完成，共 {len(buildings)} 个楼栋")
        return {"code": 200, "buildings": buildings}

    @staticmethod
    def get_device_daily(device_id, days=30):
        """
        单台设备的按天用量序列（来自 usage_daily），给统计表格「点寝室展开看日线」用。
        """
        print(f"[INFO] 查询按天用量，设备: {device_id}, 天数: {days}")
        try:
            since = (datetime.now().date() - timedelta(days=int(days))).isoformat()
            series, dev, latest, has_usage = [], None, None, False
            with DatabaseManager.get_connection() as conn:
                has_usage = ub.table_exists(conn)
                if has_usage:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT day, usage_amount, use_money FROM usage_daily "
                            "WHERE device_id = %s AND day >= %s ORDER BY day",
                            (str(device_id), since))
                        rows = cursor.fetchall()
                        cursor.execute("SELECT equipmentName, equipmentType, rate FROM device "
                                       "WHERE id = %s", (str(device_id),))
                        dev = cursor.fetchone()
                        cursor.execute("SELECT remainingBalance, read_time FROM data "
                                       "WHERE device_id = %s ORDER BY read_time DESC LIMIT 1",
                                       (str(device_id),))
                        latest = cursor.fetchone()
                    series = [{"day": str(r[0]),
                               "usage": float(r[1]) if r[1] is not None else None,
                               "money": float(r[2]) if r[2] is not None else None}
                              for r in rows]
            vals = [x["usage"] for x in series if x["usage"] is not None]
            return {
                "code": 200,
                "device_id": str(device_id),
                "name": dev[0] if dev else None,
                "kind": ("电" if dev and str(dev[1]) == "0" else "水" if dev else None),
                "rate": float(dev[2]) if dev and dev[2] is not None else None,
                "balance": float(latest[0]) if latest and latest[0] is not None else None,
                "last_time": str(latest[1]) if latest and latest[1] else None,
                "days": int(days), "since": since,
                "has_usage_table": has_usage,
                "point_count": len(series),
                "usage_sum": round(sum(vals), 4) if vals else None,
                "usage_avg": round(sum(vals) / len(vals), 4) if vals else None,
                "usage_max": max(vals) if vals else None,
                "usage_min": min(vals) if vals else None,
                "series": series,
            }
        except Exception as e:
            print(f"[ERROR] 按天用量查询失败: {e}")
            traceback.print_exc()
            return {"code": "500", "error": f"按天用量查询失败: {str(e)}"}

    @staticmethod
    def get_stats_table(building, days=30):
        """
        统计表格：该楼设备清单 + 最新余额 + 近 N 天用量，按「楼层 → 寝室号 → 表类型」排序，
        并按楼层分组返回，供前端表格（按楼层分类、按寝室号排序）直接渲染。

        数据来源：device（设备清单）+ data（最新一条读数 = 余额/时间）
                  + usage_daily（近 N 天每日用量之和，由 debug_utils/daily_backfill.py 回填）。
        usage_daily 表不存在时不影响，只是用量列会是空。
        """
        print(f"[INFO] 生成统计表格，楼栋: {building}, 天数: {days}")
        try:
            since = (datetime.now().date() - timedelta(days=int(days))).isoformat()
            with DatabaseManager.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 模板按学校档案生成（本校为 "72-%室电表"），水表一起要 → 把「电表」放宽为「%表」
                    like = school_profile.dorm_device_like(building).replace("电表", "%表")
                    cursor.execute("SHOW TABLES LIKE 'usage_daily'")
                    has_usage = cursor.fetchone() is not None

                    usage_sum = ("(SELECT SUM(u.usage_amount) FROM usage_daily u "
                                 "WHERE u.device_id = d.id AND u.day >= %s)") if has_usage else "NULL"
                    usage_days = ("(SELECT COUNT(*) FROM usage_daily u "
                                  "WHERE u.device_id = d.id AND u.day >= %s)") if has_usage else "0"
                    sql = f"""
                        SELECT d.id, d.equipmentName, d.installationSite, d.equipmentType, d.rate,
                               (SELECT dd.remainingBalance FROM data dd WHERE dd.device_id = d.id
                                 ORDER BY dd.read_time DESC LIMIT 1) AS balance,
                               (SELECT dd.read_time FROM data dd WHERE dd.device_id = d.id
                                 ORDER BY dd.read_time DESC LIMIT 1) AS last_time,
                               {usage_sum} AS usage_sum,
                               {usage_days} AS usage_days
                        FROM device d
                        WHERE d.equipmentName LIKE %s
                    """
                    sql_params = ([since, since] if has_usage else []) + [like]
                    print(f"[INFO] 统计表格SQL(含用量表={has_usage})")
                    cursor.execute(sql, sql_params)
                    rows = cursor.fetchall()
                    print(f"[INFO] 查到 {len(rows)} 台设备")

            devices = []
            for r in rows:
                info = school_profile.parse_room(r[1] or "") or {}
                devices.append({
                    "device_id": str(r[0]),
                    "equipmentName": r[1],
                    "installationSite": r[2],
                    "equipmentType": str(r[3]) if r[3] is not None else None,
                    "kind": ("电" if str(r[3]) == "0" else "水" if str(r[3]) == "1" else info.get("kind")),
                    "rate": float(r[4]) if r[4] is not None else None,
                    "balance": float(r[5]) if r[5] is not None else None,
                    "last_time": str(r[6]) if r[6] is not None else None,
                    "usage_sum": float(r[7]) if r[7] is not None else None,
                    "usage_days": int(r[8]) if r[8] is not None else 0,
                    "room": info.get("room"),
                    "room_no": info.get("room_no"),
                    "floor": info.get("floor"),
                })

            # 排序：楼层（未知排最后）→ 寝室号 → 表类型（电在前）
            devices.sort(key=lambda d: (
                d["floor"] if d["floor"] is not None else 999,
                d["room_no"] if d["room_no"] is not None else 99999,
                0 if d["kind"] == "电" else 1,
            ))

            # 按楼层分组
            floors, cur_floor, bucket = [], "___", []
            for d in devices:
                if d["floor"] != cur_floor:
                    if bucket:
                        floors.append({"floor": cur_floor, "count": len(bucket),
                                       "rooms": bucket})
                    cur_floor, bucket = d["floor"], []
                bucket.append(d)
            if bucket:
                floors.append({"floor": cur_floor, "count": len(bucket), "rooms": bucket})

            return {
                "code": 200,
                "building": building,
                "days": int(days),
                "since": since,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "has_usage_table": has_usage,
                "total": len(devices),
                "floor_count": len(floors),
                "columns": ["楼层", "寝室", "类型", "余额", "近%d天用量" % int(days), "最近更新"],
                "floors": floors,
                "devices": devices,
            }
        except Exception as e:
            print(f"[ERROR] 统计表格生成失败: {e}")
            traceback.print_exc()
            msg = str(e)
            if "Illegal mix of collations" in msg:
                msg = ("usage_daily 与 device/data 表的排序规则不一致，请执行："
                       "ALTER TABLE usage_daily CONVERT TO CHARACTER SET utf8mb4 "
                       "COLLATE utf8mb4_general_ci;")
            return {"code": "500", "error": f"统计表格生成失败: {msg}"}

    @staticmethod
    def get_building_hourly_data(building, start_day, end_day):
        """宿管模式：获取指定楼栋所有电表的小时级用电量数据"""
        print(f"[INFO] 开始获取楼栋小时数据，楼栋: {building}, 日期: {start_day} ~ {end_day}")
        try:
            with DatabaseManager.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 步骤1：查询该楼栋的所有电表设备
                    device_sql = """
                        SELECT id, equipmentName, installationSite, equipmentType,
                               ratio, rate, acctId, status, updated_at
                        FROM device
                        WHERE equipmentType = 0 AND equipmentName LIKE %s
                        ORDER BY equipmentName
                    """
                    # 表名匹配模板来自学校档案：
                    #   湖南工业大学 "102-0101室电表" -> "102-%室电表"
                    #   三一工学院   "学1栋101室电表" -> "学1栋%电表"
                    pattern = school_profile.dorm_device_like(building)
                    print(f"[INFO] 查询楼栋设备，模式: {pattern}")
                    cursor.execute(device_sql, (pattern,))
                    device_rows = cursor.fetchall()
                    print(f"[INFO] 查询到 {len(device_rows)} 个电表设备")

                    if not device_rows:
                        return {
                            "code": 200, "building": building,
                            "start_day": start_day, "end_day": end_day,
                            "total_devices": 0, "devices": []
                        }

                    device_ids = [row[0] for row in device_rows]

                # 步骤2：计算日期范围和查询时间范围
                start_date = datetime.strptime(start_day, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_day, '%Y-%m-%d').date()

                days = []
                cur = start_date
                while cur <= end_date:
                    days.append(cur)
                    cur += timedelta(days=1)

                # 查询范围包含第一天前一小时(锚点) ~ 最后一天 23:59:59
                query_start = datetime(start_date.year, start_date.month, start_date.day) - timedelta(hours=1)
                query_end = datetime(end_date.year, end_date.month, end_date.day) + timedelta(hours=24) - timedelta(seconds=1)
                print(f"[INFO] 原始数据查询范围: {query_start} ~ {query_end}")

                # 步骤3：一次性查询所有原始读数
                readings_map = fetch_raw_readings(conn, device_ids, query_start, query_end)

                # 步骤4：对每个设备每天调用清洗算法
                devices_result = []
                for device_row in device_rows:
                    did = device_row[0]
                    raw = readings_map.get(did, [])

                    all_rows = []
                    total_usage = 0.0
                    now = datetime.now()
                    today = now.date()

                    for day in days:
                        hours = build_24h_hours(day)
                        slot_vals = build_slot_max(raw, hours)
                        slot_vals = fill_missing_slots(slot_vals)

                        # 获取前一天23:00段锚点值
                        anchor_start = datetime(day.year, day.month, day.day) - timedelta(hours=1)
                        anchor_end = datetime(day.year, day.month, day.day)
                        anchor_val = get_anchor_max(raw, anchor_start, anchor_end)

                        usage_series = compute_usage_series(slot_vals, anchor_val)
                        usage_series = smooth_zero_usage(usage_series)

                        for i, (val, src) in enumerate(usage_series):
                            # 跳过今天尚未到达的小时，避免图表出现未来的0值
                            if day == today and hours[i].hour > now.hour:
                                continue
                            time_label = hours[i].strftime('%Y-%m-%d %H:%M')
                            usage_val = val if val is not None else 0
                            all_rows.append({
                                "device_id": str(did),
                                "read_time": time_label,
                                "total_reading": str(round(usage_val, 4)),
                                "remainingBalance": "0",
                                "source": src
                            })
                            if val is not None:
                                total_usage += val

                    # 倒序排列（与现有 check 接口一致，最新在前）
                    all_rows.reverse()

                    devices_result.append({
                        "equipmentName": device_row[1],
                        "device_id": str(did),
                        "installationSite": device_row[2],
                        "equipmentType": _s(device_row[3]),
                        "ratio": _s(device_row[4]),
                        "rate": _s(device_row[5]),
                        "acctId": device_row[6],
                        "status": _s(device_row[7]),
                        "updated_at": _s(device_row[8]),
                        "total": len(all_rows),
                        "total_usage": round(total_usage, 4),
                        "rows": all_rows,
                        "code": 200
                    })

                print(f"[INFO] 楼栋小时数据构建完成，共 {len(devices_result)} 个设备")
                return {
                    "code": 200,
                    "building": building,
                    "start_day": start_day,
                    "end_day": end_day,
                    "total_devices": len(devices_result),
                    "devices": devices_result
                }
        except Exception as e:
            print(f"[ERROR] 获取楼栋小时数据时出错: {str(e)}")
            traceback.print_exc()
            return {"code": "500", "error": f"数据库查询错误: {str(e)}"}

# 并行获取多个设备数据
def get_multiple_device_data(device_ids, data_num):
    def fetch_device_data(device_id):
        return device_id, DataQuery.get_device_data(device_id, data_num)
    
    # 使用线程池并行获取设备数据
    futures = [executor.submit(fetch_device_data, device_id) for device_id in device_ids]
    results = {}
    
    for future in futures:
        device_id, data = future.result()
        results[device_id] = data
    
    return results

# HTTP请求处理器
class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 获取真实客户端IP
        real_ip = self.headers.get('X-Real-IP') or self.headers.get('X-Forwarded-For') or self.client_address[0]
        print(f"[INFO] 收到GET请求 from {real_ip}: {self.path}")
        response_data = {"code": "400", "error": "请求参数错误"}
        try:
            # 解析URL和参数
            parsed_url = urlparse(self.path)
            params = parse_qs(parsed_url.query)
            print(f"[INFO] 解析参数完成: {params}")
            
            # 处理不同模式的请求
            mode = params.get('mode', [None])[0]
            print(f"[INFO] 请求模式: {mode}")
            
            if mode == 'first_screen':
                # 首屏数据
                print("[INFO] 处理首屏数据请求")
                response_data = DataQuery.get_first_screen_data()
            elif mode == 'check':
                # 检查设备数据
                device_id = params.get('device_id', [None])[0]
                data_num = params.get('data_num', [None])[0]  # 不再设置默认值
                print(f"[INFO] 处理设备检查请求，设备ID: {device_id}, 数据量: {data_num}")
                if device_id and data_num:  # 必须同时提供device_id和data_num
                    device_id = device_id.strip()  # 去除首尾空格
                    # 验证device_id格式（允许字母、数字和下划线，限制长度）
                    if not re.match(r'^[a-zA-Z0-9_]+$', device_id) or len(device_id) > 50:
                        print(f"[WARN] 无效的device_id参数: {device_id}")
                        response_data = {"code": "400", "error": "无效的device_id参数"}
                    else:
                        # 验证data_num是否为有效数字
                        try:
                            num = int(data_num)
                            if num < 1 or num > 1000:  # 限制数据量范围
                                print(f"[WARN] data_num参数超出范围: {num}")
                                response_data = {"code": "400", "error": "data_num参数超出范围(1-1000)"}
                            else:
                                response_data = DataQuery.get_device_data(device_id, num)
                        except ValueError:
                            print(f"[WARN] 无效的data_num参数: {data_num}")
                            response_data = {"code": "400", "error": "无效的data_num参数"}
                else:
                    print("[WARN] 缺少device_id参数")
                    response_data = {"code": "400", "error": "缺少device_id参数"}
            elif mode == 'check_daily_range':
                # 检查设备每日最后数据
                device_id = params.get('device_id', [None])[0]
                start_day = params.get('start_day', [None])[0]
                end_day = params.get('end_day', [None])[0]
                print(f"[INFO] 处理设备每日数据请求，设备ID: {device_id}, 开始日期: {start_day}, 结束日期: {end_day}")
                if device_id and start_day and end_day:
                    device_id = device_id.strip()  # 去除首尾空格
                    # 验证device_id格式（允许字母、数字和下划线，限制长度）
                    if not re.match(r'^[a-zA-Z0-9_]+$', device_id) or len(device_id) > 50:
                        print(f"[WARN] 无效的device_id参数: {device_id}")
                        response_data = {"code": "400", "error": "无效的device_id参数"}
                    else:
                        # 使用datetime模块验证日期格式和有效性
                        try:
                            datetime.strptime(start_day, '%Y-%m-%d')
                            datetime.strptime(end_day, '%Y-%m-%d')
                            start_date = datetime.strptime(start_day, '%Y-%m-%d').date()
                            end_date = datetime.strptime(end_day, '%Y-%m-%d').date()
                            if start_date > end_date:
                                print(f"[WARN] 开始日期 {start_day} 晚于结束日期 {end_day}")
                                response_data = {"code": "400", "error": "开始日期不能晚于结束日期"}
                            else:
                                response_data = DataQuery.get_device_daily_range_data(device_id, start_day, end_day)
                        except ValueError:
                            print(f"[WARN] 日期格式不正确，应为YYYY-MM-DD")
                            response_data = {"code": "400", "error": "日期格式不正确，应为YYYY-MM-DD"}
                else:
                    print("[WARN] 缺少必要参数 device_id, start_day 或 end_day")
                    response_data = {"code": "400", "error": "缺少必要参数 device_id, start_day 或 end_day"}
            elif mode == 'list_buildings':
                # 宿管模式：楼栋列表（前端据此动态生成楼栋按钮）
                print("[INFO] 处理楼栋列表请求")
                response_data = DataQuery.get_building_list()
            elif mode == 'check_hourly_building':
                # 宿管模式：按楼栋查询小时级用电量
                building = params.get('building', [None])[0]
                start_day = params.get('start_day', [None])[0]
                end_day = params.get('end_day', [None])[0]
                print(f"[INFO] 处理宿管模式请求，楼栋: {building}, 日期: {start_day} ~ {end_day}")
                if building and start_day and end_day:
                    # 验证楼栋号（规则来自学校档案；匹配不到的楼栋会查不到设备，不需要写死白名单）
                    building_pattern = school_profile.get("building_pattern")
                    if not re.match(building_pattern, building):
                        print(f"[WARN] 无效的楼栋号: {building}")
                        response_data = {"code": "400",
                                         "error": f"无效的楼栋号: {building}"}
                    else:
                        try:
                            start_date = datetime.strptime(start_day, '%Y-%m-%d').date()
                            end_date = datetime.strptime(end_day, '%Y-%m-%d').date()
                            if start_date > end_date:
                                print(f"[WARN] 开始日期 {start_day} 晚于结束日期 {end_day}")
                                response_data = {"code": "400", "error": "开始日期不能晚于结束日期"}
                            elif (end_date - start_date).days > 7:
                                print(f"[WARN] 日期范围超过7天")
                                response_data = {"code": "400", "error": "日期范围不能超过7天"}
                            else:
                                response_data = DataQuery.get_building_hourly_data(building, start_day, end_day)
                        except ValueError:
                            print(f"[WARN] 日期格式不正确，应为YYYY-MM-DD")
                            response_data = {"code": "400", "error": "日期格式不正确，应为YYYY-MM-DD"}
                else:
                    print("[WARN] 缺少必要参数 building, start_day 或 end_day")
                    response_data = {"code": "400", "error": "缺少必要参数 building, start_day 或 end_day"}
            elif mode == 'backfill_status':
                response_data = backfill_manager.status()

            elif mode == 'backfill_start':
                building = params.get('building', [None])[0]
                days_raw = params.get('days', ['30'])[0]
                kinds_raw = (params.get('kinds', ['electric'])[0] or 'electric').lower()
                kinds = ('electric', 'water') if kinds_raw == 'both' else \
                        (('electric',) if kinds_raw == 'electric' else ('water',))
                if not building or not re.match(school_profile.get("building_pattern"), building):
                    response_data = {"code": "400", "error": f"无效的楼栋号: {building}"}
                else:
                    try:
                        days_int = max(1, min(int(days_raw), 3650))
                    except (TypeError, ValueError):
                        days_int = 30
                    r = backfill_manager.start(building, days_int, kinds)
                    response_data = {"code": 200, **r}

            elif mode == 'backfill_stop':
                response_data = {"code": 200, **backfill_manager.stop()}

            elif mode == 'device_daily':
                device_id = params.get('device_id', [None])[0]
                days_raw = params.get('days', ['30'])[0]
                try:
                    days_int = max(1, min(int(days_raw), 3650))
                except (TypeError, ValueError):
                    days_int = 30
                if not device_id:
                    response_data = {"code": "400", "error": "缺少 device_id"}
                else:
                    response_data = DataQuery.get_device_daily(device_id, days_int)

            elif mode == 'stats_table':
                # 统计表格：按楼层分类、按寝室号排序（format=csv 直接下载表格文件）
                building = params.get('building', [None])[0]
                days_raw = params.get('days', ['30'])[0]
                fmt = (params.get('format', ['json'])[0] or 'json').lower()
                print(f"[INFO] 处理统计表格请求，楼栋: {building}, 天数: {days_raw}, 格式: {fmt}")
                building_pattern = school_profile.get("building_pattern")
                if not building or not re.match(building_pattern, building):
                    response_data = {"code": "400", "error": f"无效的楼栋号: {building}"}
                else:
                    try:
                        days_int = max(1, min(int(days_raw), 3650))
                    except (TypeError, ValueError):
                        days_int = 30
                    # 自动补按天用量（后端 worker 跑，前端轮询 backfill_status 看进度）
                    auto = (params.get('auto_backfill', ['1'])[0] or '1') != '0'
                    backfill_info = None
                    if auto and fmt == 'json':
                        kinds_raw = (params.get('kinds', ['electric'])[0] or 'electric').lower()
                        kinds = ('electric', 'water') if kinds_raw == 'both' else \
                                (('electric',) if kinds_raw == 'electric' else ('water',))
                        try:
                            backfill_info = backfill_manager.start(building, days_int, kinds)
                            if backfill_info.get('started'):
                                print(f"[INFO] 已触发按天用量自动补数：{building}栋 {days_int}天 {kinds}")
                        except Exception as e:
                            print(f"[WARN] 触发自动补数失败: {e}")
                            backfill_info = {"error": str(e)}
                    result = DataQuery.get_stats_table(building, days_int)
                    if result.get('code') == 200 and backfill_info is not None:
                        result['backfill'] = backfill_info
                    if fmt == 'csv' and result.get('code') == 200:
                        response_data = {"__csv__": stats_table_to_csv(result),
                                         "__filename__": f"stats_building{building}_{days_int}d.csv"}
                    else:
                        response_data = result
            elif mode == 'search':
                # 搜索设备
                keyword = params.get('key_word', [None])[0]
                print(f"[INFO] 处理搜索设备请求，关键词: {keyword}")
                if keyword:
                    # 验证keyword是否为有效格式，只允许字母、数字和中文
                    if not isinstance(keyword, str) or len(keyword) > 50 or not re.match(r'^[a-zA-Z0-9\u4e00-\u9fa5\s]+$', keyword):
                        print(f"[WARN] 搜索关键词包含非法字符: {keyword}")
                        response_data = {"code": "400", "error": "搜索关键词包含非法字符"}
                    else:
                        response_data = DataQuery.search_devices(keyword)
                else:
                    print("[WARN] 缺少key_word参数")
                    response_data = {"code": "400", "error": "缺少key_word参数"}
            else:
                print(f"[WARN] 无效的mode参数: {mode}")
                response_data = {"code": "400", "error": "无效的mode参数"}
            
            print(f"[INFO] 请求处理完成，响应数据: {response_data.get('code', 'N/A')}")
        except Exception as e:
            print(f"[ERROR] 处理请求时出错: {str(e)}")
            traceback.print_exc()
            response_data = {"code": "500", "error": f"服务器内部错误: {str(e)}"}
        finally:
            try:
                # 设置响应头
                self.send_response(200)
                if isinstance(response_data, dict) and "__csv__" in response_data:
                    # CSV 下载（带 BOM，Excel 直接打开不乱码）
                    self.send_header('Content-type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition',
                                     'attachment; filename=%s' % response_data.get("__filename__", "stats.csv"))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Access-Control-Expose-Headers', 'Content-Disposition')
                    self.end_headers()
                    csv_bytes = b'\xef\xbb\xbf' + response_data["__csv__"].encode('utf-8')
                    print(f"[INFO] 发送CSV响应，长度: {len(csv_bytes)}")
                    self.wfile.write(csv_bytes)
                else:
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')  # 允许跨域
                    self.end_headers()

                    # 发送响应
                    response_str = json.dumps(response_data, ensure_ascii=False)
                    print(f"[INFO] 发送响应，响应长度: {len(response_str)}")
                    self.wfile.write(response_str.encode('utf-8'))
                
                # 确保数据发送完成
                self.wfile.flush()
                print("[INFO] 响应发送完成")
            except Exception as e:
                print(f"[ERROR] 发送响应时出错: {str(e)}")
                # 不要在这里抛出异常，避免影响连接释放

# 启动服务器
if __name__ == '__main__':
    # 多线程模式：一个慢请求（大表查询/补数触发）不会把整个服务堵住
    server = ThreadingHTTPServer(('', SERVER_PORT), RequestHandler)
    server.daemon_threads = True
    print(f"服务器启动，监听端口 {SERVER_PORT}")
    server.serve_forever()