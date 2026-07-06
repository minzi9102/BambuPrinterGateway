# Bambu 打印机预约排队系统执行计划

## 1. 项目目标

开发一个基于 `bambu-connect` 的局域网 3D 打印机预约排队系统。

系统最终目标：

```text
所有用户
   │
   ├── 查看打印机实时状态
   ├── 查看当前打印任务
   ├── 查看打印进度和剩余时间
   ├── 查看完整排队列表
   └── 上传打印文件并加入队列

             │
             ▼

       Queue Server

             │
             ▼

       Printer Gateway

             │
             ▼

       Bambu Printer
```

项目采用迭代开发。

第一版只完成最简单且必要的业务闭环：

```text
上传已切片文件
      ↓
加入 FIFO 队列
      ↓
所有页面实时显示
      ↓
管理员确认打印板已清理
      ↓
启动队列第一任务
      ↓
监控打印状态
      ↓
打印完成
      ↓
等待管理员启动下一任务
```

---

# 2. 当前技术基础

`bambu-connect` 已经提供统一的 `BambuClient`，内部封装 Camera、状态监听、控制和文件访问 Client。

状态监听通过 MQTT 连接打印机 `8883` 端口，并订阅：

```text
device/{serial}/report
```

当前状态模型已经包含：

```text
mc_percent
mc_remaining_time
gcode_state
subtask_name
gcode_file
layer_num
total_layer_num
```

因此可以获得打印进度、剩余时间、当前任务和层数等状态。

项目已经提供：

```python
start_print(file)
```

其底层通过 MQTT `project_file` 指令启动打印机中已有的打印文件。

当前 `FileClient` 只有：

```python
get_files()
download_file()
```

尚未提供上传接口，因此第一阶段必须增加 `upload_file()`。

---

# 3. V1 产品定义

## 3.1 V1 必须实现的功能

第一版只支持：

| 功能                   | V1 |
| -------------------- | -- |
| 单台 Bambu 打印机         | ✅  |
| 局域网连接打印机             | ✅  |
| 公共实时状态页面             | ✅  |
| 查看打印进度               | ✅  |
| 查看剩余时间               | ✅  |
| 查看当前任务               | ✅  |
| 查看 FIFO 排队列表         | ✅  |
| 所有人提交预约              | ✅  |
| 上传 sliced 3MF        | ✅  |
| 文件基础验证               | ✅  |
| 队列持久化                | ✅  |
| 页面实时同步               | ✅  |
| 管理员启动下一任务            | ✅  |
| 自动上传打印文件             | ✅  |
| 自动调用 `start_print()` | ✅  |
| 打印开始确认               | ✅  |
| 打印完成检测               | ✅  |
| 服务重启后恢复队列            | ✅  |

---

## 3.2 V1 明确不做

第一版不做：

```text
用户注册
用户登录
Google / Microsoft SSO

多打印机

Camera 页面

普通 STL 上传

未切片 .3mf 自动切片

服务器自动生成 G-code

AMS 材料自动验证

管理员拖动调整队列

邮件通知

短信通知

打印完成自动启动下一任务

Redis

Kubernetes

微服务

Bambu Studio 修改
```

原则：

> V1 不解决所有问题，只证明“预约 → 排队 → 实时显示 → 上传 → 启动打印 → 状态回传 → 完成”的完整技术闭环。

---

# 4. V1 用户流程

普通用户打开：

```text
http://printer-server/
```

页面显示：

```text
3D Printer Queue

Printer
--------------------------------
Status        PRINTING
Progress      67%
Remaining     54 min
Current Job   Alice - Gearbox
Layer         145 / 320


Queue
--------------------------------
#1 Bob       Robot Arm
#2 Charlie   Case
#3 David     Prototype


[ Submit Print Job ]
```

点击：

```text
Submit Print Job
```

填写：

```text
Name
[ Alice ]

Project Name
[ Gearbox Prototype ]

Print File
[ gearbox.gcode.3mf ]

[ Join Queue ]
```

服务器执行：

```text
接收文件
    ↓
文件大小检查
    ↓
ZIP / 3MF 完整性检查
    ↓
寻找 Metadata/*.gcode
    ↓
生成内部 Job ID
    ↓
保存文件
    ↓
写入 SQLite
    ↓
加入 FIFO Queue
    ↓
广播 queue.changed
```

所有打开页面无需刷新：

```text
Queue

#1 Alice     Gearbox Prototype
```

---

# 5. V1 打印流程

打印机空闲时：

```text
Printer Status

READY FOR NEXT JOB

Next:
Alice - Gearbox Prototype
```

管理员确认：

```text
打印板已清理
打印机状态正常
```

然后进入管理员页面：

```text
/admin
```

点击：

```text
[ Start Next Job ]
```

系统执行：

```text
队列锁定
   ↓
获取 FIFO 第一任务
   ↓
Job → UPLOADING
   ↓
FTPS 上传
   ↓
检查远程文件存在
   ↓
Job → STARTING
   ↓
start_print()
   ↓
监听 MQTT
   ↓
确认打印状态进入 PRINTING
   ↓
Job → PRINTING
   ↓
公共页面实时更新
```

打印过程中：

```text
WatchClient
    │
    ▼
PrinterStatus
    │
    ▼
PrinterService
    │
    ▼
Realtime Broadcaster
    │
    ▼
所有浏览器
```

打印完成：

```text
PRINTING
    ↓
完成状态
    ↓
Job → COMPLETED
    ↓
记录 finished_at
    ↓
广播 job.changed
    ↓
显示下一任务
```

此时：

```text
READY FOR NEXT JOB
```

但是不会自动打印。

管理员取下模型、清理打印板以后，再次点击：

```text
Start Next Job
```

---

# 6. 为什么 V1 不自动打印下一任务

这是故意设计，不是技术限制。

打印完成以后可能还需要：

```text
取下打印件
清理打印板
检查打印板安装
检查喷嘴
检查材料
```

因此第一版把：

```text
管理员点击 Start Next Job
```

定义为：

> 操作员确认打印机已经具备启动下一任务的物理条件。

这样系统仍然自动完成：

```text
选取下一任务
上传文件
启动打印
确认打印状态
```

人工只做一个确认动作。

V2 再考虑自动启动策略。

---

# 7. V1 系统架构

V1 不采用微服务。

全部部署在一台局域网服务器：

```text
┌────────────────────────────────────┐
│ Printer Queue Server               │
│                                    │
│ FastAPI                            │
│                                    │
│ ├── Public API                     │
│ ├── Admin API                      │
│ ├── WebSocket                      │
│ ├── Queue Service                  │
│ ├── Printer Service                │
│ ├── Bambu Adapter                  │
│ └── SQLite                         │
│                                    │
│ Local File Storage                 │
└─────────────────┬──────────────────┘
                  │
                  │ LAN
                  ▼
            Bambu Printer
```

浏览器：

```text
Browser
   │
   ├── HTTP REST
   └── WebSocket
          │
          ▼
       FastAPI
```

打印机：

```text
FastAPI Application
        │
        ▼
PrinterService
        │
        ▼
BambuAdapter
        │
        ├── MQTT 8883
        └── FTPS
               │
               ▼
         Bambu Printer
```

---

# 8. V1 技术选型

## 后端

```text
Python
FastAPI
Uvicorn
SQLite
SQLAlchemy
Pydantic
```

## 打印机通信

```text
bambu-connect fork
```

修改并封装为：

```text
BambuAdapter
```

## 前端

第一版不使用 React。

使用：

```text
HTML
CSS
Vanilla JavaScript
```

原因：

```text
无需 Node
无需 Vite
无需单独部署 frontend
无需前后端工程拆分
```

FastAPI 直接提供：

```text
/static/index.html
/static/app.js
/static/admin.html
```

实时数据使用：

```text
WebSocket
```

第二版再升级 React。

---

# 9. 项目目录设计

```text
printer-queue/
│
├── app/
│   │
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   │
│   ├── models/
│   │   ├── job.py
│   │   └── printer.py
│   │
│   ├── schemas/
│   │   ├── job.py
│   │   └── printer.py
│   │
│   ├── api/
│   │   ├── public.py
│   │   ├── admin.py
│   │   └── websocket.py
│   │
│   ├── services/
│   │   ├── queue_service.py
│   │   ├── printer_service.py
│   │   └── realtime_service.py
│   │
│   ├── printer/
│   │   ├── bambu_adapter.py
│   │   └── file_client.py
│   │
│   └── static/
│       ├── index.html
│       ├── admin.html
│       ├── app.js
│       ├── admin.js
│       └── styles.css
│
├── uploads/
│
├── data/
│   └── queue.db
│
├── tests/
│   ├── test_file_validation.py
│   ├── test_queue_service.py
│   └── test_job_state.py
│
├── .env.example
├── requirements.txt
└── README.md
```

---

# 10. 核心数据模型

V1 只需要一个核心表：

```text
jobs
```

字段：

```text
id
display_name
project_name
original_filename
stored_filename
stored_path
remote_filename
status
queue_sequence
created_at
started_at
finished_at
error_message
```

SQL 概念模型：

```text
jobs

id                UUID
display_name      TEXT
project_name      TEXT
original_filename TEXT
stored_filename   TEXT
stored_path       TEXT
remote_filename   TEXT
status            TEXT
queue_sequence    INTEGER
created_at        DATETIME
started_at        DATETIME NULL
finished_at       DATETIME NULL
error_message     TEXT NULL
```

---

# 11. Job 状态机

V1 使用：

```text
QUEUED
UPLOADING
STARTING
PRINTING
COMPLETED
FAILED
CANCELLED
```

状态流程：

```text
QUEUED
   │
   ▼
UPLOADING
   │
   ├── upload error
   │        ↓
   │      FAILED
   │
   ▼
STARTING
   │
   ├── start timeout
   │        ↓
   │      FAILED
   │
   ▼
PRINTING
   │
   ├── printer error
   │        ↓
   │      FAILED
   │
   ▼
COMPLETED
```

不允许：

```text
COMPLETED → PRINTING
FAILED → PRINTING
CANCELLED → PRINTING
```

所有状态变化只能经过：

```python
JobStateService
```

不能在 API 中随意：

```python
job.status = "PRINTING"
```

---

# 12. FIFO 队列实现

V1 采用严格 FIFO。

提交任务时：

```text
MAX(queue_sequence) + 1
```

例如：

```text
1001 Alice
1002 Bob
1003 Charlie
```

获取下一任务：

```sql
SELECT *
FROM jobs
WHERE status = 'QUEUED'
ORDER BY queue_sequence ASC
LIMIT 1;
```

页面排队位置不存数据库。

动态计算：

```text
按照 queue_sequence 排序
```

这样取消一个任务后：

```text
Alice   sequence 1001
Charlie sequence 1003
```

页面直接显示：

```text
#1 Alice
#2 Charlie
```

无需修改数据库 position。

---

# 13. 打印文件规则

V1 接受：

```text
.gcode.3mf
```

服务器不只判断扩展名。

上传以后使用：

```python
zipfile.ZipFile
```

验证：

```text
文件是合法 ZIP
        ↓
能够正常读取目录
        ↓
存在 Metadata/*.gcode
```

例如：

```text
Metadata/plate_1.gcode
```

通过：

```text
ACCEPT
```

否则：

```text
REJECT
```

错误提示：

```text
The uploaded file does not contain sliced G-code.

Please slice the project in Bambu Studio and upload a sliced 3MF file.
```

V1 文件限制还需要：

```text
最大上传大小
文件名清理
禁止路径穿越
随机内部文件名
```

内部保存：

```text
uploads/
    2ae71f7e-....gcode.3mf
```

绝不直接使用：

```text
../../something
```

作为服务器路径。

远程文件名统一生成：

```text
queue_<JOB_ID>.gcode.3mf
```

例如：

```text
queue_2ae71f7e.gcode.3mf
```

---

# 14. Bambu Adapter 设计

禁止业务代码直接调用：

```python
BambuClient
```

增加统一 Adapter：

```python
class BambuAdapter:
    def connect(self):
        ...

    def disconnect(self):
        ...

    def request_full_status(self):
        ...

    def upload_file(self, local_path, remote_name):
        ...

    def file_exists(self, remote_name):
        ...

    def start_print(self, remote_name):
        ...

    def get_status(self):
        ...
```

业务层只能：

```text
PrinterService
      │
      ▼
BambuAdapter
```

这样以后可以替换：

```text
bambu-connect
```

或者升级其他打印机协议实现。

---

# 15. `upload_file()` 第一版实现

为了保持与当前 `FileClient` 一致，第一版继续使用系统 `curl`。

概念命令：

```text
curl
--ftp-pasv
--insecure
--upload-file LOCAL_FILE
ftps://PRINTER_IP/REMOTE_FILE
--user bblp:ACCESS_CODE
```

封装：

```python
def upload_file(
    self,
    local_path: str,
    remote_name: str,
) -> None:
    ...
```

必须检查：

```text
local file exists
curl return code
timeout
stderr
```

上传完成后不能直接开始打印。

必须：

```text
upload_file()
      ↓
get_files()
      ↓
确认 remote_name 存在
      ↓
start_print()
```

---

# 16. PrinterService

`PrinterService` 是 V1 最重要的业务组件。

职责：

```text
维护打印机连接
保存最新 PrinterStatus
处理 MQTT callback
规范化打印机状态
检测打印开始
检测打印完成
协调 QueueService
广播实时状态
```

示意：

```python
class PrinterService:
    def __init__(
        self,
        adapter,
        queue_service,
        realtime_service,
    ):
        self.adapter = adapter
        self.queue_service = queue_service
        self.realtime = realtime_service
        self.current_status = None
        self.current_job_id = None
```

MQTT callback：

```python
def on_printer_status(self, status):
    self.current_status = normalize_status(status)

    self.reconcile_job_state()

    self.realtime.broadcast(
        "printer.status",
        self.current_status,
    )
```

---

# 17. 打印机状态规范化

不要让整个系统直接判断：

```python
status.gcode_state == "SOME_STRING"
```

建立：

```python
class PrinterState:
    OFFLINE = "offline"
    IDLE = "idle"
    STARTING = "starting"
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"
    UNKNOWN = "unknown"
```

增加：

```python
def normalize_printer_state(
    status: PrinterStatus,
) -> PrinterState:
    ...
```

第 0 次技术验证时记录真实打印机：

```text
空闲时 gcode_state
启动时 gcode_state
打印中 gcode_state
暂停时 gcode_state
完成时 gcode_state
失败时 gcode_state
```

然后建立状态映射。

不要凭猜测硬编码。

---

# 18. Start Next Job 流程

管理员请求：

```text
POST /api/admin/start-next
```

后端：

```text
获取全局 printer operation lock
        ↓
检查 printer connected
        ↓
检查 printer state == IDLE
        ↓
检查没有 active job
        ↓
获取下一 QUEUED job
        ↓
更新 job = UPLOADING
        ↓
upload_file
        ↓
file_exists
        ↓
job = STARTING
        ↓
start_print
        ↓
等待 MQTT 状态确认
```

确认：

```text
PrinterState == PRINTING
```

则：

```text
job = PRINTING
started_at = now
```

否则超过内部启动确认窗口：

```text
job = FAILED
error_message = "Printer did not confirm print start"
```

V1 不自动 retry。

避免第一次版本出现：

```text
重复 start_print
重复打印
```

---

# 19. 防止重复启动

必须有一个全局打印机操作锁：

```python
asyncio.Lock()
```

例如：

```python
self.operation_lock = asyncio.Lock()
```

管理员同时点击两次：

```text
Request A
Request B
```

只能：

```text
Request A
    ↓
LOCK

Request B
    ↓
等待
```

A 完成以后，B 再检查：

```text
active job already exists
```

然后拒绝。

返回：

```text
409 Conflict
```

这是 V1 必须做的。

---

# 20. Public API

## 获取状态

```text
GET /api/status
```

响应：

```json
{
  "printer": {
    "connected": true,
    "state": "printing",
    "progress": 67,
    "remaining_minutes": 54,
    "current_job": {
      "id": "...",
      "display_name": "Alice",
      "project_name": "Gearbox"
    },
    "layer": 145,
    "total_layers": 320
  }
}
```

## 获取队列

```text
GET /api/queue
```

响应：

```json
{
  "jobs": [
    {
      "position": 1,
      "id": "...",
      "display_name": "Bob",
      "project_name": "Robot Arm"
    },
    {
      "position": 2,
      "id": "...",
      "display_name": "Charlie",
      "project_name": "Case"
    }
  ]
}
```

## 提交任务

```text
POST /api/jobs
```

Multipart：

```text
display_name
project_name
file
```

响应：

```json
{
  "id": "...",
  "status": "queued",
  "position": 3
}
```

---

# 21. Admin API

V1 管理员采用一个后台密码。

`.env`：

```text
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHANGE_ME
```

使用 HTTP Basic Auth。

API：

```text
POST /api/admin/start-next

POST /api/admin/jobs/{job_id}/cancel
```

V1 管理员只需要两个操作：

```text
Start Next Job
Cancel Job
```

不做拖动排序。

---

# 22. WebSocket

连接：

```text
/ws
```

服务器事件：

```text
printer.status
queue.changed
job.changed
system.notice
```

示例：

```json
{
  "type": "printer.status",
  "data": {
    "state": "printing",
    "progress": 68,
    "remaining_minutes": 53
  }
}
```

Queue：

```json
{
  "type": "queue.changed"
}
```

客户端收到：

```text
queue.changed
```

直接重新：

```text
GET /api/queue
```

V1 不需要在 WebSocket 中发送完整队列。

这样逻辑更简单。

---

# 23. V1 开发迭代顺序

## Iteration 0：打印机技术验证

这是整个项目的 Gate。

完成：

```text
连接真实打印机
MQTT 状态监听
dump_info
记录 gcode_state
FTPS 文件列表
FTPS 上传测试
确认上传文件存在
start_print
确认 MQTT 进入打印状态
确认打印完成状态
```

测试文件：

```text
一个非常小的已切片测试模型
```

验收标准：

```text
Gateway 可以在没有 Bambu Studio 操作的情况下：

上传 sliced 3MF
        ↓
发送 start_print
        ↓
打印机真实开始打印
        ↓
Gateway 收到 PRINTING 状态
        ↓
Gateway 收到完成状态
```

只有通过这一 Gate，才继续 Queue 系统。

---

## Iteration 1：Printer Gateway

开发：

```text
BambuAdapter
upload_file
file_exists
status normalization
PrinterService
```

完成：

```text
应用启动
    ↓
自动连接打印机
    ↓
dump_info
    ↓
维护当前状态
```

控制台能够显示：

```text
Printer connected

State: PRINTING
Progress: 41
Remaining: 63
Layer: 102 / 320
```

验收：

```text
状态变化无需重启服务
```

---

## Iteration 2：Queue 和 SQLite

开发：

```text
database.py
Job model
QueueService
JobStateService
```

实现：

```text
create_job
get_queue
get_next_job
cancel_job
change_job_state
```

编写测试：

```text
3 个任务 FIFO 顺序正确
取消中间任务后顺序正确
COMPLETED 不会重新进入 Queue
FAILED 不会进入 Queue
```

验收：

```text
服务重启以后队列仍存在
```

---

## Iteration 3：上传和公共页面

开发：

```text
POST /api/jobs
file validator
index.html
app.js
```

实现：

```text
用户上传文件
加入 Queue
显示打印机状态
显示 Queue
```

验收：

打开三个浏览器：

```text
Browser A
Browser B
Browser C
```

A 上传任务。

B 和 C 无需刷新，自动看到新队列。

---

## Iteration 4：Start Next Job

开发：

```text
Admin API
admin.html
operation lock
start next workflow
```

流程：

```text
Admin click
      ↓
QueueService.get_next_job
      ↓
upload
      ↓
verify
      ↓
start_print
      ↓
wait printer state
      ↓
PRINTING
```

验收：

```text
Queue:

#1 Alice
#2 Bob

管理员 Start Next

结果：

Alice → PRINTING
Bob   → Queue #1
```

页面自动显示：

```text
Current Job:
Alice

Queue:
#1 Bob
```

---

## Iteration 5：打印完成闭环

开发：

```text
PrinterService.reconcile_job_state()
```

逻辑：

```text
Current Job = PRINTING

Printer:
PRINTING → FINISHED / IDLE

        ↓

Current Job = COMPLETED
finished_at = now
current_job = None
```

页面：

```text
READY FOR NEXT JOB

Next:
Bob
```

管理员清板后：

```text
Start Next Job
```

Bob 开始。

验收：

连续完成：

```text
Alice
  ↓
Bob
  ↓
Charlie
```

三个真实测试任务。

每个任务：

```text
正确上传
正确开始
正确显示进度
正确完成
正确移动队列
```

---

## Iteration 6：错误处理和部署

必须处理：

```text
打印机离线
MQTT 断开
上传失败
文件不存在
start_print 未确认
Server 重启
重复点击 Start
非法 3MF
超大文件
SQLite 错误
```

公共页面状态：

```text
PRINTER OFFLINE

Printing is temporarily unavailable.
Existing queue has been preserved.
```

管理员页面显示错误原因：

```text
Upload failed

curl exited with code 28
```

部署使用：

```text
systemd
```

应用：

```text
printer-queue.service
```

服务器重启：

```text
自动启动 FastAPI
        ↓
连接打印机
        ↓
dump_info
        ↓
读取 SQLite Queue
        ↓
恢复 Web 服务
```

---

# 24. V1 启动配置

`.env`：

```text
PRINTER_HOST=192.168.1.101

PRINTER_ACCESS_CODE=XXXXXXXX

PRINTER_SERIAL=XXXXXXXX

ADMIN_USERNAME=admin

ADMIN_PASSWORD=CHANGE_ME

DATABASE_URL=sqlite:///./data/queue.db

UPLOAD_DIR=./uploads

MAX_UPLOAD_MB=500
```

打印机 Access Code：

```text
只能在服务器读取
```

绝对不能：

```text
返回 API
发送 WebSocket
写入 HTML
输出公共日志
```

---

# 25. V1 启动流程

```text
Application Start
        │
        ▼
Load Config
        │
        ▼
Open SQLite
        │
        ▼
Run DB Migration
        │
        ▼
Create PrinterService
        │
        ▼
Connect Printer
        │
        ▼
Start MQTT WatchClient
        │
        ▼
dump_info()
        │
        ▼
Refresh Printer State
        │
        ▼
Start FastAPI
```

注意：

服务器刚启动时：

```text
Printer State = UNKNOWN
```

在收到打印机状态以前：

```text
Start Next Job
```

必须禁用。

不能因为服务器重启就假设：

```text
Printer == IDLE
```

---

# 26. 服务重启恢复规则

例如：

```text
Alice 正在打印
```

此时 Queue Server 重启。

SQLite：

```text
Alice = PRINTING
```

服务器恢复：

```text
连接打印机
      ↓
dump_info
      ↓
读取 subtask_name
      ↓
读取 gcode_state
```

如果打印机仍然：

```text
PRINTING
```

继续：

```text
Alice = PRINTING
```

如果打印机已经：

```text
IDLE
```

但数据库：

```text
Alice = PRINTING
```

V1 不自动假设 Alice 已完成。

进入：

```text
RECONCILIATION REQUIRED
```

管理员页面提示：

```text
Printer and database state do not match.

Database:
Alice PRINTING

Printer:
IDLE

[ Mark Completed ]
[ Mark Failed ]
```

这个异常场景可以放在 Iteration 6 实现。

正常路径必须先跑通。

---

# 27. V1 测试清单

## 文件测试

```text
合法 .gcode.3mf
损坏 ZIP
普通文本改名 .3mf
不含 G-code 的 3MF
超大文件
危险文件名
```

## Queue 测试

```text
空 Queue
1 Job
10 Jobs
取消第一 Job
取消中间 Job
取消最后 Job
```

## 打印测试

```text
Printer Offline
Printer Idle
Printer Printing
Upload Failed
Start Failed
Print Completed
```

## 并发测试

```text
两个人同时上传

管理员连续点击两次 Start

管理员 Start 时打印机正在打印
```

---

# 28. V1 最终验收标准

V1 只有满足下面所有条件才算完成。

### 预约

```text
任意用户打开网页
      ↓
上传 sliced 3MF
      ↓
任务成功进入 Queue
```

### 实时 Queue

```text
用户 A 上传

用户 B 页面无需刷新

Queue 自动出现新任务
```

### 实时打印状态

打印开始以后：

```text
Status
Progress
Remaining Time
Current Job
```

自动变化。

### 打印控制

管理员点击：

```text
Start Next Job
```

系统自动：

```text
选择 FIFO 第一任务
上传到 Printer
启动 Print
确认 PRINTING
```

### 打印完成

打印结束：

```text
Job → COMPLETED
```

公共页面自动：

```text
Current Job 清空

Next Job 变为 Queue 第一名
```

### 数据恢复

Queue Server 重启：

```text
尚未打印的 Queue Job 不丢失
```

满足上述条件：

> V1 完成。

---

# 29. V2 计划

V2 再增加：

```text
多打印机

用户登录

用户自己的任务页面

取消自己的任务

Camera

管理员调整 Queue

打印历史

材料信息

AMS 状态

邮件通知

预计开始时间
```

架构升级：

```text
SQLite
   ↓
PostgreSQL
```

前端：

```text
Vanilla JS
   ↓
React
```

数据模型增加：

```text
users
printers
printer_jobs
job_events
```

---

# 30. V3 计划

V3 增加自动化。

```text
普通 .3mf 上传
        ↓
服务器切片
        ↓
获取 estimated print time
        ↓
获取 filament usage
        ↓
材料验证
        ↓
自动选择打印机
        ↓
智能 Queue
```

支持：

```text
PLA Job → PLA Printer

AMS Black PLA available
        ↓
选择对应 Printer
```

增加：

```text
Scheduler
```

考虑：

```text
预计打印时间
材料
打印机型号
喷嘴尺寸
AMS
优先级
```

---

# 31. V4 计划

V4 才考虑：

```text
无人连续打印
```

例如：

```text
Job A Completed
       ↓
自动检测清板条件
       ↓
自动启动 Job B
```

这需要解决物理打印件移除问题。

可能需要：

```text
自动清板设备
传送带
机械机构
人工确认 API
外部传感器
Computer Vision
```

因此不属于 V1。

---

# 32. 最终推荐开发原则

整个项目严格遵守：

```text
先打印链路
再 Queue
再 Web
再自动化
```

不要反过来：

```text
先开发漂亮网站
先开发复杂用户系统
先开发多打印机
最后才测试打印
```

正确顺序：

```text
Iteration 0

FTPS Upload
+
start_print
+
MQTT Status

        ↓

真实打印成功

        ↓

Queue

        ↓

Public Page

        ↓

Admin Start Next

        ↓

完整闭环

        ↓

V1 Release
```

项目第一里程碑不是：

```text
网页完成
```

而是：

> 一台 Gateway 电脑能够把一个 `.gcode.3mf` 上传到真实 Bambu 打印机，通过代码启动打印，并正确检测打印开始和结束。

这个技术 Gate 一旦通过，预约和实时排队部分就是标准 Web 系统开发问题。

V1 的最终产品应该保持非常简单：

```text
一个公共页面
一个管理员页面
一个 FastAPI 服务
一个 SQLite 数据库
一台打印机
一个 Printer Gateway
```

但它必须真正完成：

```text
预约
↓
排队
↓
实时查看
↓
上传
↓
开始打印
↓
监控
↓
完成
↓
下一任务
```

这就是第一版的完整最小闭环。
