# uit_check_money（湖南工业大学版）

宿舍水电费自动查询 / 监控 / 预警脚本。

> 本仓库 fork 自 [xmb505/SANY_check_money](https://github.com/xmb505/SANY_check_money)（三一工学院版），
> 已把学校接口适配到**湖南工业大学**（`sdjf.hnuit.edu.cn`）。
> 两校用的是同一套厂商接口，只有「主机、登录方式、宿管楼栋规则」三处不同，
> 全部集中在 `server/libs/school_profile.py`，改一处即可切换学校。

## 项目简介

本项目用于自动查询宿舍水电费信息（湖南工业大学）。该项目通过模拟学校网站的登录和数据查询过程，实现了自动化的水电费信息获取，并提供邮件预警、数据库存储、Web可视化界面等丰富功能。

对于经常忘记查询水电费余额而导致停电停水的同学，这个项目可以帮助你实时监控余额并在低于设定阈值时自动发送邮件提醒，让你及时充值，避免影响正常生活。

## 本校接口适配说明（湖南工业大学，2026-09 实测）

| 项目 | 湖南工业大学 | 说明 |
|------|--------------|------|
| API 基础地址 | `https://sdjf.hnuit.edu.cn/prod-api/external` | 与三一版同厂商，接口路径完全一致 |
| 签名密钥 | `DJKSBNW123` | 相同 |
| 登录 | 统一身份认证（CAS）；`POST /appUser/login` 仅对 App 内注册过的账号有效 | 见下方「取参」 |
| 查询鉴权 | **查询接口不校验登录态** | 不带 Cookie / Admin-Token 也返回同样结果 |

### 1. 取 appUserId / roleId（无需密码）

```bash
python3 debug_utils/login.py <学号>
# {"code": 200, "appUserId": "...", "roleId": 2, "phoneNum": "...", "direct_login": false}
```

原理：`GET /external/appUser/{学号}` 不需要登录就能返回 `appUserId` / `roleId`
（前端登录后也是先调这个接口拿 `appUserId` 存进 localStorage）。
传了密码时会先试 `POST /appUser/login`；本校账号走统一身份认证，
若该账号没在校园 App 里注册过，服务端返回 `202 该账号尚未注册`，
脚本自动回落到上面的无密码方式（输出里 `direct_login=false` 表示走了回落）。

### 2. 原版签名算法少了一个 `&`（已修）

前端拼接的是 `"大写KEY=大写VALUE&"`，**每个键值对都以 `&` 结尾（含最后一个）**：

```js
// js 里的拦截器
Object.keys(e).sort().map(a => { n += `${a.toUpperCase()}=${t[a].toUpperCase()}&` });
md5(n + "DJKSBNW123")
```

原版 Python 用的是 `"&".join(...)`，最后一个键值对后面没有 `&`，得到的签名是错的。
`/external/appUserAcct/list` 不校验签名，所以一直没暴露；
但 `/external/equipment/list` 会校验，直接返回 `201 验证签名失败！`。
已修 `server/libs/signer.py`，并加了 `python3 server/libs/signer.py` 自检（含回归护栏）。

### 3. `equipment/list` 的语义差异

本校 `/external/equipment/list` 返回该 `roleKey` 下的**全部设备台账**（实测 14072 台），
传进去的 `appUserId` 会被忽略（与三一版不同）。
所以：

* 只想看**自己的**表 → `get_account_list(appUserId, roleId)`（`appUserAcct/list`）；
* 需要**全校/学院台账**（宿管模式）→ `get_device_list(...)`。

单页条数建议 ≤ 500：本校实测 `pageSize=1000` 时第 3 页起会服务端超时。

### 4. 宿管模式（楼栋）改动

* 表名规则：本校是 `102-0101室电表`，三一是 `学1栋101室电表`
  → 统一交给 `school_profile.dorm_device_name_like` / `parse_building()`；
* 楼栋列表不再写死：后端新增 `?mode=list_buildings`，按 `device` 表里的设备名实时统计，
  前端 `refreshBuildingButtons()` 动态生成按钮（接口失败则沿用 HTML 里的兜底按钮）；
* 楼栋号校验只校验格式，不再用写死的白名单。

### 5. 切换/新增学校

```bash
SCHOOL_PROFILE=sany python3 debug_utils/get_data.py ...   # 临时切回三一档案
```

新增学校：在 `server/libs/school_profile.py` 的 `PROFILES` 里复制一份，
改 `base_url` / `dorm_device_name_like` / `dorm_buildings` 三处即可，其余代码不用动。

## 核心功能

### 🔍 自动查询
- **用户登录**：通过 `login.py` 脚本自动完成用户登录，获取必要凭证
- **数据获取**：通过 `get_data.py` 脚本查询详细的水电费使用情况和当前余额
- **分页查询**：通过 `check_data.py` 脚本支持分页查询所有设备数据

### 📧 智能预警
- **邮件通知**：支持两种邮件发送方式：
  - 基于SMTP协议的传统邮件发送 (`mail_sender.py`)
  - 基于Aoksend API的现代化邮件服务 (`monitor_aoksender.py`)
- **后台监控**：通过守护进程脚本实现无人值守的周期性监控：
  - SMTP方式：`monitor_daemon.py`
  - Aoksend API方式：`monitor_aoksender.py`
- **个性化配置**：支持自定义预警阈值、邮件模板和发送参数

### 💾 数据持久化
- **数据库存储**：通过 `data2sql.py` 脚本将查询到的数据存储到MySQL数据库
- **数据去重**：自动检测并避免重复插入相同时间点的数据
- **异常处理**：能够识别并标记异常数据（如时间为空的记录）
- **一键建表**：通过 `import.sql` 文件快速创建所需的数据库表结构

### 🌐 Web可视化
- **数据展示**：提供基于Web的图形化界面，直观展示水电费数据
- **多种模式**：支持用量、用钱、总量、余额等多种数据展示模式
- **交互查询**：支持设备搜索和自定义数据点数量
- **图表可视化**：通过图表直观展示数据变化趋势
- **邮件订阅**：支持用户订阅和解绑邮件通知服务
- **邮件余额显示**：实时显示剩余邮件数量

### ⚙️ 自动化执行
- **守护进程**：通过 `daemon.sh` 脚本实现周期性自动执行任务
- **灵活配置**：支持通过配置文件自定义执行间隔和命令

## 技术亮点

- **完全模拟**：精确模拟学校网站的登录和查询流程，包括参数加密和签名算法
- **无会话依赖**：通过URL参数维持用户验证，无需维护复杂的会话状态
- **模块化设计**：各功能模块独立，支持命令行调用，返回标准JSON格式数据，便于集成
- **高性能API**：Web后端采用连接池和线程池技术，显著提升查询性能
- **安全防护**：支持只读用户访问，输入参数验证，防止SQL注入
- **反向代理支持**：支持Nginx、Apache等反向代理环境

## 项目结构

```
uit_check_money/
├── debug_utils/              # 开发工具脚本
│   ├── login.py              # 用户登录脚本
│   ├── get_data.py           # 水电费数据查询脚本
│   ├── check_data.py         # 分页设备数据查询脚本
│   ├── data2sql/             # 数据库存储脚本
│   │   ├── data2sql.py
│   │   └── config/
│   │       └── example_mysql.ini
│   ├── mail_sender/          # SMTP邮件发送脚本
│   │   ├── mail_sender.py
│   │   └── config/
│   │       ├── example_mail_setting.ini
│   │       └── mail_texter.txt
│   ├── monitor_daemon/       # SMTP监控守护进程
│   │   ├── monitor_daemon.py
│   │   └── config/
│   │       ├── example_monitor_config.ini
│   │       ├── example_mail_setting.ini
│   │       └── mail_texter.txt
│   └── monitor_aoksender/    # Aoksend监控守护进程
│       ├── monitor_aoksender.py
│       └── config/
│           └── example_aoksender.ini
├── daemon/                   # Shell守护进程
│   ├── daemon.sh
│   └── config/
│       └── example_daemon.ini
├── data_cleaner/             # 数据清洗/按小时聚合（server 会调用）
│   └── hourly_report.py
├── doc/
│   ├── API_DOC.md            # 接口说明
│   ├── IFLOW_old.md          # 项目开发过程（历史文档，不再维护）
│   └── sql/                  # 建表脚本
│       ├── import.sql        # device + data 两张核心表
│       ├── email_table.sql   # 邮件订阅表
│       └── ..._table.sql
├── start.sh                 # 按需启动（MySQL→采集→后端→前端，不设自启）
├── stop.sh                  # 停止 start.sh 拉起的进程
├── server/                  # Web后端服务
│   ├── server.py            # RESTful API服务
│   ├── email_api.py         # 邮件订阅API
│   ├── aokbalance_get.py    # Aoksend余额查询服务
│   └── *.ini                # 服务配置文件
└── web/                     # Web前端界面
    ├── index.html           # 主页面
    ├── main.js              # 主逻辑
    ├── styles.css           # 样式文件
    ├── example_config.js    # 前端配置模板
    └── config.js            # (gitignored) 运行时配置
```

## 快速开始

### 1. 环境准备

需要 **Python 3.10 或更高**——`data_cleaner/hourly_report.py` 用了 `str | None` 这类写法，
Python 3.9 在导入阶段就会报 `TypeError: unsupported operand type(s) for |`。
推荐用虚拟环境（Homebrew 的 Python 受 PEP 668 保护，直接 pip install 会被拒）：

```bash
python3 -m venv .venv
.venv/bin/pip install requests pymysql     # 全项目只依赖这两个第三方库
.venv/bin/python --version                 # 实测 3.14 可用
```

MySQL 8+ / 26.x 均可，只需 `device`、`data`（以及邮件订阅要用的 `email`）三张表：

```bash
# macOS + Homebrew 实测流程
brew install mysql
mysqld --datadir=/opt/homebrew/var/mysql --socket=/tmp/mysql.sock --port=3306 &
# 若数据目录为空，先执行一次：mysqld --initialize-insecure --datadir=/opt/homebrew/var/mysql
mysql --socket=/tmp/mysql.sock -u root -e "CREATE DATABASE hnuit_check_money"
mysql --socket=/tmp/mysql.sock -u root hnuit_check_money < doc/sql/import.sql   # 改掉文件里的 your_database_name
```

### 2. 数据库配置

创建MySQL数据库并导入表结构：

```bash
mysql -h [服务器地址] -u [用户名] -p < import.sql
```

配置数据库连接信息：

```ini
# debug_utils/data2sql/config/mysql.ini
[mysql]
mysql_server = your_mysql_host
mysql_port = 3306
login_user = your_username
login_passwd = your_password
db_schema = hnuit_check_money
```

### 3. 基础查询

获取 appUserId 与 roleId（后续所有脚本都要用）：

```bash
python3 debug_utils/login.py <学号>          # 本校推荐：无需密码
python3 debug_utils/login.py <学号> <密码>    # 走厂商直连登录接口（未注册会自动回落）
```

扫描楼栋号（填 school_profile.py 的 dorm_buildings 用）：

```bash
python3 debug_utils/list_buildings.py [roleKey] [页数] [每页条数]
```

查询水电费数据：

```bash
python3 debug_utils/get_data.py <appUserId> <roleId>
```

### 4. 数据存储

将数据存储到数据库（只入库**自己**的表）：

```bash
./debug_utils/data2sql/data2sql.py <appUserId> <roleId> [pageNum] [pageSize]
```

整栋楼批量入库（宿管模式的前提，含电表与水表）：

```bash
./debug_utils/data2sql/data2sql.py --building 72          # 72 栋全部（实测 260 电表 + 260 水表）
./debug_utils/data2sql/data2sql.py --building 72 100      # 第二个参数是每页条数
```

> 用的是 `equipment/list?equipmentName=72-` 前缀过滤（实测该校接口支持），
> 比扫全院台账（14072 台）快得多，也不会把别的楼数据带进来；每页别超过 100，
> 实测 pageSize ≥ 1000 时第 3 页起服务端会读超时。

**注意本校设备台账的数据范围**：`equipment/list` 每台只返回**余额**（`remainingBalance`），
不返回示数（`equipmentCurrentLarge`）、结算时间（`currentDealDate`）和开关状态（`equipmentStatus`）。
所以整栋楼入库后：余额是真实的，而**用电量（示数差）算不出来**——只有
`appUserAcct/list`（也就是「自己的表」）才给示数。想按电量监控整栋楼，学校的接口本身不支持，
能批量看的是余额。

### 5. 邮件预警

先复制配置（阈值 / SMTP 凭据，都是 gitignored，不会进仓库）：

```bash
cp debug_utils/monitor_daemon/config/example_monitor_config.ini debug_utils/monitor_daemon/config/monitor_config.ini
cp debug_utils/monitor_daemon/config/example_mail_setting.ini  debug_utils/monitor_daemon/config/mail_setting.ini
```

**先用 dry-run 看会不会告警、正文长什么样——不发信、不需要 SMTP 配置**：

```bash
./debug_utils/monitor_daemon/monitor_daemon.py <学号> --dry-run
./debug_utils/monitor_aoksender/monitor_aoksender.py <学号> --dry-run   # Aoksend API 方式
./debug_utils/mail_sender/mail_sender.py <学号> --dry-run               # 只发一封
```

确认无误后去掉 `--dry-run` 正式跑（本校密码可省略，走统一身份认证；这些循环都是**手动启动**、
不会开机自启）：

```bash
./debug_utils/monitor_daemon/monitor_daemon.py <学号> [密码]      # SMTP，轮询 + 低于阈值发信
./debug_utils/monitor_aoksender/monitor_aoksender.py <学号> [密码] # Aoksend API
```

阈值在 `monitor_config.ini`（`ele_num` 电费 / `water_num` 水费，单位元）、检查周期 `check_round` 秒；
邮件正文模板是 `config/mail_texter.txt`，其中 `{school_name}` 会按学校档案自动替换，
发件人名与主题也取自学校档案（不再写死某一所学校）。

### 6. Web服务

先起后端 API（监听 8080）：

```bash
cd server
../.venv/bin/python server.py
```

前端是纯静态文件，**不**由 `server.py` 托管，需要另起一个静态服务器（后端已带
`Access-Control-Allow-Origin: *`，跨端口没问题）：

```bash
cd web
../.venv/bin/python -m http.server 3000
```

再把 `web/config.js` 的 `API_BASE_URL` 指向后端，然后浏览器打开 `http://127.0.0.1:3000`：

```js
API_BASE_URL: 'http://127.0.0.1:8080',
```

### 8. 统计表格（按楼层分类、按寝室号排序）

面板顶部新增「统计表格」页签：选楼栋 + 天数 → 生成，即可看到整栋楼按**楼层分组**、楼层内按**寝室号排序**的表格，
列包含 楼层 / 寝室 / 类型 / 余额 / 近 N 天用量 / 单价 / 最近更新时间；余额 ≤ 10 元标红。
支持导出 CSV（`导出 CSV` 导当前楼栋、`导出全部楼栋 CSV` 逐栋下载），文件带 BOM，Excel 直接打开不乱码。

后端接口：

```
GET /?mode=stats_table&building=72&days=30            # JSON（按楼层分组）
GET /?mode=stats_table&building=72&days=30&format=csv # CSV 下载
```

楼层 / 寝室号从设备名解析（见 `server/libs/school_profile.py` 的 `room_name_pattern` / `parse_room()`）：
本校 `72-0101室电表` → 1 楼、0101 室（寝室号前两位是楼层）；命名规则不同的学校只需改档案里的正则。

#### 8.1 按天用量回填（前 N 天）

```
./debug_utils/daily_backfill.py --device 6166 --days 30        # 单台
./debug_utils/daily_backfill.py --mine <学号> --days 30        # 自己名下的表
./debug_utils/daily_backfill.py --building 72 --days 30 --only-electric --yes   # 整栋（注意请求量）
```

结果写进 `usage_daily` 表（一次跑完可中断续跑，已存在的 (设备,日期) 会跳过）。

**为什么是一天一次请求：** 本校部署上的能耗曲线接口 `/external/bill/getUsageListByNodeId` 长期无响应
（每次都是网关超时，东八区工作日/晚间都试过），能用的只有 `POST /external/bill`，而它只返回**某个区间**的
合计（`totalUsage` / `lastDayUsage` / `lastTwoDayUsage`），没有日序列。所以按天数据只能一天发一次请求换来。

**请求量 = 设备数 × 天数**，实测单次约 2 秒：

| 范围 | 请求数 | 约需时间 |
| --- | --- | --- |
| 自己 1 台 × 30 天 | 30 | 1 分钟 |
| 整栋 260 台电表 × 30 天 | 7800 | 4 小时以上 |
| 整栋 260 台电表 × 1 次（当月合计） | 260 | 约 10 分钟 |

超过 60 次请求脚本会先停下要你加 `--yes`，避免不小心给学校服务器压力。

> 正确性校验：逐日回填的 30 天合计（241.66）与直接查整月区间的 `totalUsage`（241.66）完全一致。

**建表注意：** `usage_daily` 的排序规则必须是 `utf8mb4_general_ci`（与 `device`/`data` 一致）。
MySQL 26.7 默认建表是 `utf8mb4_0900_ai_ci`，与旧表 join 会报 `1267 Illegal mix of collations`；
`doc/sql/usage_daily_table.sql` 已显式指定，`daily_backfill.py` 也会自动检查并改正。

### 7. 一键按需启停（推荐）

`start.sh` / `stop.sh` 把 MySQL、采集、后端、前端串起来，**不注册任何开机自启或常驻服务**，
关掉即停、需要时再拉起：

```bash
./start.sh <学号>              # MySQL → 采集一次 → 后端(8080) → 前端(3000)
./start.sh                    # 只启服务（沿用库里已有数据），不采集
./start.sh <学号> --no-collect
./stop.sh                     # 全部停止；也可 ./stop.sh api|web|mysql
```

端口可用 `PORT_API=8090 PORT_WEB=3001 ./start.sh <学号>` 覆盖；学号只用于当次采集，
不落盘（也可先 `export UIT_USER=<学号>`）。

**关于采集频率**：`data2sql` 按「结算时间」去重，同一读数重复采集不会新增数据点，
所以采得再勤既不会灌水、也不会凭空多出曲线点——点是跟着学校那边的结算时间长的。

**要它按周期自动采集**：用项目自带的 `daemon/daemon.sh`（从仓库根目录执行即可，
内部按脚本自身位置找配置）：

```bash
cp daemon/config/example_daemon.ini daemon/config/daemon.ini
# 编辑 daemon.ini：
#   rec_time = 3600                                    # 周期，秒
#   command = '/绝对路径/.venv/bin/python /绝对路径/debug_utils/data2sql/data2sql.py <appUserId> <roleId>'
bash daemon/daemon.sh      # 手动启动，Ctrl+C 停止（同样不注册开机自启）
```

需要余额预警的话，项目自带两套（都需手动启动）：
`debug_utils/monitor_daemon/monitor_daemon.py`（`while True` 轮询 + SMTP 预警，
需先填 `config/monitor_config.ini` 与 `config/mail_setting.ini`）和
`debug_utils/monitor_aoksender/monitor_aoksender.py`（Aoksend API 方式）。

## 配置说明

项目使用多个配置文件来管理不同功能的参数。为了保护隐私和便于部署，所有配置文件都提供了示例模板（以`example_`开头的文件）。

### 配置文件列表

- `debug_utils/data2sql/config/mysql.ini`：数据库连接配置
- `debug_utils/mail_sender/config/mail_setting.ini`：SMTP邮件发送配置
- `debug_utils/monitor_aoksender/config/aoksender.ini`：Aoksend API配置
- `debug_utils/monitor_daemon/config/monitor_config.ini`：数据监控配置
- `debug_utils/mail_sender/config/mail_texter.txt`：邮件模板文件
- `debug_utils/monitor_daemon/config/mail_texter.txt`：邮件模板文件（副本）
- `daemon/config/daemon.ini`：守护进程配置
- `server/server.ini`：Web后端API服务配置
- `server/email_api.ini`：邮件订阅API配置
- `server/aokbalance_get.ini`：Aoksend余额查询服务配置
- `web/config.js`：Web前端配置

### 配置文件使用方法

1. 复制示例配置文件并重命名为实际使用的文件名：
   ```bash
   cp debug_utils/data2sql/config/example_mysql.ini debug_utils/data2sql/config/mysql.ini
   cp debug_utils/monitor_aoksender/config/example_aoksender.ini debug_utils/monitor_aoksender/config/aoksender.ini
   cp debug_utils/mail_sender/config/example_mail_setting.ini debug_utils/mail_sender/config/mail_setting.ini
   cp debug_utils/monitor_daemon/config/example_monitor_config.ini debug_utils/monitor_daemon/config/monitor_config.ini
   cp debug_utils/monitor_daemon/config/example_mail_setting.ini debug_utils/monitor_daemon/config/mail_setting.ini
   cp daemon/config/example_daemon.ini daemon/config/daemon.ini
   cp server/config_examples/example_server.ini server/server.ini
   cp server/config_examples/example_email_api.ini server/email_api.ini
   cp web/example_config.js web/config.js
   ```

2. 根据实际环境修改配置文件中的参数

3. 项目会自动忽略实际配置文件，确保敏感信息不会被上传到版本控制系统

详细配置说明请参考各配置文件内的注释。

## 使用场景

1. **个人监控**：学生个人使用，定期检查宿舍水电费余额
2. **宿舍管理**：宿舍管理员批量监控多个房间的水电费情况
3. **数据分析**：通过数据库存储的历史数据进行用量趋势分析
4. **系统集成**：作为其他自动化系统的一部分，提供水电费数据接口

## 注意事项

- 本项目仅供学习和研究目的使用
- 请遵守学校网站的使用条款和相关法律法规
- 不建议在生产环境中频繁使用本脚本，可能对学校服务器造成压力
- 使用分页查询时请合理设置pageSize，避免请求过大数据量
- 项目维护者不对因使用本脚本导致的任何后果承担责任

## 贡献

欢迎提交 Issue 和 Pull Request 来改进项目。

## 许可证

本项目仅供个人学习和研究使用。