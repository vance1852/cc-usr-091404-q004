# 疫苗稳定性试验编排平台

基于 **Django 5 + Django REST Framework + SQLite** 的稳定性试验编排应用，帮助协调员、
分析员和质量人员在断电、样品错放/破损、提前取样等扰动下，仍然清楚判断：

- 每个批次按**方案版本**在各时间点**可执行哪些动作**；
- **计划取样时间与实际取样时间严格分列**，实际时间绝不回写计划；
- 错放、破损、温度偏离会**冻结**相关动作与检测结果，等待 QA 影响评估；
- **已消耗样品不可再分配**；
- 协调员可为缺失时间点安排**替代样品（不改变原计划行）**；
- 分析员**只提交结果**，质量人员决定**纳入 / 排除 / 追加考察**；
- 直观查询各批次**时间轴、逾期动作、偏离暴露时长、替代来源、趋势分析数据集**。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo           # 可选：写入演示场景并打印三个角色的 Token
python manage.py runserver
```

打开 <http://127.0.0.1:8000/>，在右上角粘贴 Token 即可看到仪表盘。
也可访问 <http://127.0.0.1:8000/admin/> 用演示账号（见下）登录后台。

演示账号（密码均为 `demo1234`，Token 由 `seed_demo` 打印）：

| 账号 | 角色 | 职责 |
|---|---|---|
| `coord_zhang` | 协调员 coordinator | 主数据/方案/批次/样品、生成计划、分配、取样、登记事件、安排替代/追加 |
| `analyst_li` | 分析员 analyst | 仅提交检测结果 |
| `qa_wang` | 质量人员 qa | 登记箱体事件、影响评估、对结果做纳入/排除/追加考察处置 |

## 领域模型与核心规则

```
Product ── Protocol ── ProtocolVersion ── TimePointDefinition（偏移天数+窗口+检测项目）
                                   │
Batch（入组时固定方案版本快像）─────┴── ScheduledAction（计划字段不可变）
  │                                        ├── SamplingEvent.pulled_at（实际时间）
  ├── SampleUnit（available/consumed/      ├── replaces → 原动作（替代链）
  │                damaged/misplaced/      └── AssayResult（frozen + QA disposition）
  │                withdrawn）
  └── 位于 Chamber ── EnvironmentEvent ── Exposure（暴露时长）── ImpactAssessment（QA）
```

关键不变量（均由 `core/services.py` 事务保证，并有测试覆盖）：

1. **方案版本驱动计划**：批次入组时绑定 `ProtocolVersion`，按其时间点定义生成
   `ScheduledAction`；生效版本的时间点不可修改（API 返回 409）。
2. **计划不可伪装**：`planned_at/window_start/window_end` 生成后不更新；
   实际取样只写入 `SamplingEvent.pulled_at`，并自动计算 `early/late/偏差小时`。
3. **样品状态机**：取样即 `consumed`（记录消耗时间），不能再分配给任何动作。
4. **冻结优先**：错放/破损/温度偏离把相关未终结动作与已有结果置为 `frozen`；
   冻结期间禁止取样、禁止 QA 处置，必须先完成影响评估。
5. **温度偏离暴露**：登记断电等事件时，按样品在箱区间与事件区间求交，计算
   每个样品的暴露时长；已取走样品只计取走前的暴露。
6. **QA 影响评估三结论**：`release`（放行解冻）/ `exclude`（未取样动作作废、
   可用样品退出、已有结果排除）/ `additional`（解冻并由协调员另建追加考察动作）。
7. **替代不改原计划**：替代是新的 `replacement` 动作（`replaces` 指向原行），
   原计划行的计划时间/状态保留；替代结果仍须 QA 单独处置后才进入趋势集。
8. **职责分离**：分析员不能处置结果，协调员不能提交结果/做 QA 结论，QA 不能建批次。
9. **趋势数据集** `/api/trend-dataset/` 仅包含 `disposition=included` 且未冻结的结果，
   每行同时给出计划时间与实际取样时间及其偏差。

## 主要 API（Token 认证：`Authorization: Token <key>`）

### 主数据 / 方案（协调员可写，全员可读）
- `GET/POST /api/storage-conditions/`、`/api/chambers/`、`/api/products/`、`/api/assays/`
- `GET/POST /api/protocols/`、`/api/protocol-versions/`
- `GET/POST /api/timepoints/`（仅草案版本可写，否则 409）
- `GET/POST /api/batches/`（建批次自动生成计划动作）
- `POST /api/batches/{id}/regenerate_schedule/`（补齐后加的时间点，不动已有行）
- `GET /api/batches/{id}/timeline/` —— **批次完整时间轴**（动作/取样/冻结/替代/结果/暴露）

### 样品与动作（协调员）
- `GET/POST /api/samples/?batch=&chamber=&status=&available=true`
- `POST /api/samples/{id}/problem/` `{"status":"misplaced|damaged","note":...}` → 自动冻结
- `POST /api/samples/{id}/found/` 错放找回、解冻
- `GET /api/actions/?batch=&status=&kind=&overdue=true`
- `POST /api/actions/{id}/assign/` `{"sample_id":N}`
- `POST /api/actions/{id}/sample/` `{"pulled_at":"ISO8601","note":...}` 登记实际取样
- `POST /api/actions/{id}/replace/` `{"sample_id":N,"planned_at":...}` 安排替代（原行不变）
- `POST /api/batches/{id}/extra/` 追加考察动作（协调员或 QA）
- `GET /api/actions-overdue/` —— **全部逾期待取样动作**

### 环境事件与影响评估（协调员/QA 可登记；仅 QA 评估）
- `GET/POST /api/events/` `{"chamber","event_type","started_at","ended_at","max_temp",...}`
  登记温度偏离/断电即自动生成暴露并冻结相关样品
- `GET /api/exposures/?open=true&chamber=&sample=` —— **偏离暴露时长与评估状态**
- `POST /api/exposures/{id}/assess/` `{"decision":"release|exclude|additional","comment":...}`（QA）

### 检测结果（分析员提交；QA 处置）
- `POST /api/results/` `{"action_id":A,"assay_id":X,"value":"..."}`（仅分析员）
- `GET /api/results/?batch=&assay=&disposition=&frozen=true&usable=true`
- `POST /api/results/{id}/dispose/` `{"disposition":"included|excluded|additional","comment":...}`（QA）

### 汇总
- `GET /api/me/` 当前用户与角色
- `GET /api/trend-dataset/?batch=&product=` —— **当前可用于货架期趋势分析的数据集**

## 演示场景（`seed_demo`）

- 批次 **VAX-2601**（零点 2026-06-01，1 号箱）：
  - 0M 按时取样，QA 纳入；
  - 1M 样品**提前约 68 小时**取出（实际时间如实记录、标记 early），QA 评估后纳入；
  - 3M 样品**破损冻结**，安排样品 S05 **替代**并完成检测、QA 纳入；
  - 2026-09-10 **断电 200 分钟、最高 11.4℃**：S06 放行、S07 排除并退出、
    S08 保留“等待影响评估”；
- 批次 **VAX-2602**（2 号箱）：时间点已逾期但尚未取样，用于演示逾期清单。

## 测试

```bash
python manage.py test core
```

26 个测试覆盖：计划生成与版本锁定、计划/实际时间不混淆、消耗样品不可再分配、
错放/破损/断电冻结与解冻、暴露时长交叠计算、QA 三种评估结论、
替代不改原计划、分析员/协调员/QA 三向权限隔离、时间轴与趋势数据集内容。

## 目录结构

```
stability/            Django 项目（settings/urls/wsgi）
core/
  models.py           领域模型与枚举
  services.py         事务化业务规则（分配/取样/冻结/评估/替代/处置）
  api.py / api_urls.py 角色化 REST 接口
  permissions.py      coordinator / analyst / qa 权限
  serializers.py      输出（时间轴/暴露/趋势数据集）与输入校验
  admin.py            后台
  management/commands/seed_demo.py
  tests.py
templates/dashboard.html  可视化仪表盘
```
