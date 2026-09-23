#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '../..', 'server'))

import json
import configparser
import smtplib
import time
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

from libs.api_client import login as api_login, get_account_list
from libs import school_profile

# 配置目录按脚本自身位置解析（原来写死 'config/xxx.ini'，只有 cd 到本目录才找得到）
_CFG_DIR = os.path.join(_root, 'config')


def _default_sender_name():
    """默认发件人名/主题前缀跟着学校档案走（原来是写死的「三一工学院」）。"""
    return f"{school_profile.get('school_name', '')}水电费监控系统"


def load_monitor_config():
    config = configparser.ConfigParser()
    config.read(os.path.join(_CFG_DIR, 'monitor_config.ini'), encoding='utf-8')
    return {
        'check_round': config.getint('data', 'check_round'),
        'ele_keyword': config.get('data', 'ele_keyword'),
        'ele_num': config.getfloat('data', 'ele_num'),
        'water_keyword': config.get('data', 'water_keyword'),
        'water_num': config.getfloat('data', 'water_num')
    }


def load_mail_config():
    config = configparser.ConfigParser()
    files_read = config.read(os.path.join(_CFG_DIR, 'mail_setting.ini'), encoding='utf-8')
    if not files_read:
        raise FileNotFoundError(f"无法读取 {os.path.join(_CFG_DIR, 'mail_setting.ini')}")
    if 'smtp' not in config:
        raise ValueError("配置文件中缺少 [smtp] 节点")
    receivers_raw = config.get('smtp', 'receivers')
    receivers = [r.strip() for r in receivers_raw.split(',') if r.strip()]
    return {
        'smtp_server': config.get('smtp', 'server'),
        'smtp_port': config.getint('smtp', 'port'),
        'username': config.get('smtp', 'username'),
        'password': config.get('smtp', 'password'),
        'sender': config.get('smtp', 'sender'),
        'sender_name': config.get('smtp', 'sender_name', fallback=_default_sender_name()),
        'receivers': receivers,
        'encryption': config.get('smtp', 'encryption', fallback='ssl')
    }


def load_mail_template():
    with open(os.path.join(_CFG_DIR, 'mail_texter.txt'), 'r', encoding='utf-8') as f:
        return f.read()


def check_threshold(data, config):
    rows = data.get('rows', [])
    for row in rows:
        acct_name = row.get('acctName', '')
        remaining_balance = row.get('remainingBalance', 0)
        if config['ele_keyword'] in acct_name and remaining_balance <= config['ele_num']:
            return True
        if config['water_keyword'] in acct_name and remaining_balance <= config['water_num']:
            return True
    return False


def format_mail_content(template, data):
    rows = data.get('rows', [])
    device_template = """设备名称：{acctName}
最后更新时间：{currentDealDate}
当前余额：{remainingBalance} 元
设备状态：{equipmentStatus}
"""
    data_content = ""
    for row in rows:
        item_content = device_template
        for key, value in row.items():
            if isinstance(value, (str, int, float)):
                item_content = item_content.replace(f'{{{key}}}', str(value))
        data_content += item_content + '\n'
    template_lines = [line for line in template.split('\n') if not line.strip().startswith('#')]
    clean_template = '\n'.join(template_lines)
    final = clean_template.replace('{{DATA_SECTION}}', data_content.strip())
    # 模板里可以用 {school_name} 占位，按学校档案替换
    return final.replace('{school_name}', school_profile.get('school_name', ''))


def send_mail(config, subject, content):
    try:
        message = MIMEText(content, 'plain', 'utf-8')
        sender_name = config.get('sender_name', _default_sender_name())
        message['From'] = formataddr((sender_name, config['sender']))
        message['To'] = ', '.join(config['receivers'])
        message['Subject'] = Header(subject, 'utf-8')
        if config.get('encryption', 'ssl').lower() == 'ssl':
            server = smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'])
        else:
            server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
            server.starttls()
        server.login(config['username'], config['password'])
        server.sendmail(config['sender'], config['receivers'], message.as_string())
        server.quit()
        print("邮件发送成功")
        return True
    except Exception as e:
        print(f"邮件发送失败: {e}")
        return False


def main():
    flags = [a for a in sys.argv[1:] if a.startswith('-')]
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    dry_run = '--dry-run' in flags
    if not args or len(args) > 2:
        print("用法: ./debug_utils/monitor_daemon/monitor_daemon.py <账号> [密码] [--dry-run]")
        print("  --dry-run  只跑一轮：打印会不会告警、邮件正文长什么样，不发信（不需要 SMTP 配置）")
        sys.exit(1)

    phone_num = args[0]
    # 密码可选：湖南工业大学走统一身份认证，不传密码时用学号直接换 appUserId/roleId
    password = args[1] if len(args) == 2 else None

    print("正在加载监控配置...")
    try:
        monitor_config = load_monitor_config()
    except Exception as e:
        print(f"加载监控配置失败: {e}")
        sys.exit(1)

    print(f"监控配置加载成功，检查周期: {monitor_config['check_round']} 秒"
          + ("（--dry-run：只跑一轮）" if dry_run else ""))

    def pause():
        """非 dry-run 时按周期休眠；dry-run 直接结束本轮。"""
        if dry_run:
            return
        time.sleep(monitor_config['check_round'])

    while True:
        print("开始检查水电费数据...")

        login_result = api_login(phone_num, password)
        if not login_result or login_result.get('code') != 200:
            print("登录失败")
            print(login_result)
            if dry_run:
                sys.exit(1)
            pause()
            continue

        user = login_result.get('user', {})
        app_user_id = user.get('appUserId')
        role_id = user.get('roleId')

        if not app_user_id or not role_id:
            print("无法获取用户ID或角色ID")
            if dry_run:
                sys.exit(1)
            pause()
            continue

        print(f"登录成功，用户ID: {app_user_id}，角色ID: {role_id}")

        data_result = get_account_list(app_user_id, str(role_id))
        if not data_result or data_result.get('code') != 200:
            print("数据获取失败")
            print(data_result)
            if dry_run:
                sys.exit(1)
            pause()
            continue

        print("数据获取成功")

        if check_threshold(data_result, monitor_config):
            print("检测到余额低于阈值，准备发送邮件通知...")
            try:
                mail_template = load_mail_template()
                mail_content = format_mail_content(mail_template, data_result)
                if dry_run:
                    print("=== --dry-run：下面这封邮件不会真的发出 ===")
                    print(f"主题: {school_profile.get('school_name', '')}宿舍水电费信息")
                    print(mail_content)
                    print("=== dry-run 结束 ===")
                    return
                mail_config = load_mail_config()
                send_mail(mail_config, f"{school_profile.get('school_name', '')}宿舍水电费信息", mail_content)
            except Exception as e:
                print(f"发送邮件失败: {e}")
        else:
            print("余额正常，无需发送邮件")

        if dry_run:
            print("--dry-run 只跑一轮，结束。")
            return
        print(f"等待 {monitor_config['check_round']} 秒后进行下一次检查...")
        pause()


if __name__ == "__main__":
    main()
