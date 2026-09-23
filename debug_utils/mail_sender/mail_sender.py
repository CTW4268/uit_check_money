#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, '../..', 'server'))

import json
import configparser
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

from libs.api_client import login as api_login, get_account_list
from libs import school_profile

# 配置/模板目录按脚本自身位置解析
_CFG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config')


def _default_sender_name():
    return f"{school_profile.get('school_name', '')}水电费监控系统"


def load_mail_config():
    try:
        config = configparser.ConfigParser()
        files_read = config.read(os.path.join(_CFG_DIR, 'mail_setting.ini'), encoding='utf-8')
        if not files_read:
            raise FileNotFoundError("无法读取 config/mail_setting.ini 配置文件，请检查文件是否存在且可访问")
        if 'smtp' not in config:
            raise ValueError("配置文件中缺少 [smtp] 节点")
        try:
            receivers_raw = config.get('smtp', 'receivers')
            receivers = [r.strip() for r in receivers_raw.split(',') if r.strip()]
        except configparser.NoOptionError:
            raise ValueError("配置文件中缺少 receivers 配置项")
        return {
            'smtp_server': config.get('smtp', 'server'),
            'smtp_port': config.getint('smtp', 'port'),
            'username': config.get('smtp', 'username'),
            'password': config.get('smtp', 'password'),
            'sender': config.get('smtp', 'sender'),
            'sender_name': (config.get('smtp', 'sender_name', fallback='') or _default_sender_name()),
            'receivers': receivers,
            'encryption': config.get('smtp', 'encryption', fallback='ssl')
        }
    except Exception as e:
        print(f"加载邮件配置时发生错误: {e}")
        raise


def load_mail_template():
    try:
        with open(os.path.join(_CFG_DIR, 'mail_texter.txt'), 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundError("无法找到邮件模板文件 config/mail_texter.txt，请检查文件是否存在")
    except Exception as e:
        print(f"加载邮件模板时发生错误: {e}")
        raise


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
    final_content = clean_template.replace('{{DATA_SECTION}}', data_content.strip())
    # 模板里可以用 {school_name} 占位，按学校档案替换（默认档案为湖南工业大学）
    return final_content.replace('{school_name}', school_profile.get('school_name', ''))


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
        print("用法: ./debug_utils/mail_sender/mail_sender.py <账号> [密码] [--dry-run]")
        print("  --dry-run  只打印邮件主题和正文，不发送（不需要 SMTP 配置）")
        sys.exit(1)

    phone_num = args[0]
    # 密码可选：湖南工业大学走统一身份认证，不传密码时用学号直接换 appUserId/roleId
    password = args[1] if len(args) == 2 else None

    print("正在登录...")
    login_result = api_login(phone_num, password)
    if not login_result or login_result.get('code') != 200:
        print("登录失败")
        print(login_result)
        sys.exit(1)

    user = login_result.get('user', {})
    app_user_id = user.get('appUserId')
    role_id = user.get('roleId')

    if not app_user_id or not role_id:
        print("无法获取用户ID或角色ID")
        sys.exit(1)

    print(f"登录成功，用户ID: {app_user_id}，角色ID: {role_id}")

    print("正在获取水电费数据...")
    data_result = get_account_list(app_user_id, str(role_id))

    if not data_result or data_result.get('code') != 200:
        print("数据获取失败")
        print(data_result)
        sys.exit(1)

    print("数据获取成功")

    print("正在加载邮件模板...")
    try:
        mail_template = load_mail_template()
    except Exception as e:
        print(f"加载邮件模板失败: {e}")
        sys.exit(1)

    mail_content = format_mail_content(mail_template, data_result)
    mail_subject = f"{school_profile.get('school_name', '')}宿舍水电费信息"

    if dry_run:
        print("=== --dry-run：下面这封邮件不会真的发出 ===")
        print(f"主题: {mail_subject}")
        print(mail_content)
        return

    print("正在加载邮件配置...")
    try:
        mail_config = load_mail_config()
    except Exception as e:
        print(f"加载邮件配置失败: {e}")
        sys.exit(1)

    print("正在发送邮件...")
    if send_mail(mail_config, mail_subject, mail_content):
        print("邮件已成功发送")
    else:
        print("邮件发送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
