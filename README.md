# 疫苗稳定性试验编排平台

协调恒温箱断电、样品提前取出、错放、破损等真实场景下的稳定性考察：维护**方案版本、储存条件、取样窗口、检测项目、箱体环境事件**，按方案版本快照计算每个样品**当前可执行的动作**，把**计划时间与实际时间严格分开**，在偏离影响评估完成前**冻结**相关结果，并向趋势 / 货架期分析只暴露当前**真正可用**的数据集。

基于 **Django 5.2 + SQLite**，无第三方运行时依赖；接口为基于会话认证的 JSON API（带标准 CSRF 校验）+ 服务端渲染页面。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed          # 写入演示方案/批次/断电/替代场景和三个角色账号
.venv/bin/python manage.py runserver     # http://127.0.0.1:8000/
```

演示账号（密码同用户名）：

| 账号 | 角色 | 能做什么 |
|------|------|----------|
| `coord` | 协调员 | 建方案/批次、生成计划、分配与**替代**样品、登记实际取出、登记异常与环境事件 |
| `analyst` | 分析员 | **仅提交检测结果** |
| `qa` | 质量人员 | 环境事件/样品异常的**影响评估**，结果的**纳入 / 排除 / 追加考察** |

所有登录用户都可以只读查看总览、批次时间轴和可用数据集。

`seed` 构造了题述场景：三批样品在 M3 取样前各经历一次 **3 小时 20 分断电**；
`B2026-001` 已由 QA 评估「无影响」并纳入 M3 结果；`B2026-002` 发生样品破损→替代；
`B2026-003` 发生提前取出→异常冻结→窗口内替代；`002/003` 的断电仍待评估，相关结果处于冻结状态。

## 核心业务规则（在代码中的落点）

1. **计划时间不可被实际时间伪装**
   `SamplingPoint`/`PlannedAction` 只存计划时间；`SampleUnit.withdrawn_at`、
   `TestResult.analyzed_at` 只存实际时间。生成计划时把计划日期与窗口**快照**到
   `PlannedAction`（`services.generate_plan`），方案改版或再编辑都不会改动在执行批次，
   重复生成直接报错。
2. **按方案版本计算可执行动作**
   `engine.build_action_view` / `batch_schedule` 根据当前时间、窗口、分配、结果与冻结原因，
   给出每个动作的状态（未分配 / 待取样 / 已取出待检测 / 待判定 / 已纳入 / 已排除 / 追加 / 失败）
   和 `can_withdraw`、`can_submit_result`、`can_substitute`、`can_decide` 标志。
3. **消耗即终态，不能重新分配**
   样品状态机：在箱 → 已分配 → 已取出 → 已检验消耗（终态），另有破损 / 错放终态。
   提交结果后样品立即 `consumed`，`_assert_sample_assignable` 拒绝任何再分配。
4. **替代不改变原计划**
   协调员为缺失/破损/提前取出的时间点安排替代时，**新增**一条 `substitute` 分配，
   旧分配置 `failed` 并通过 `substitutes` 链保留来源；`PlannedAction` 原样不动，
   时间轴明确标注「接替哪支样品、原因、不改变原计划」。
5. **错放/破损/温度偏离冻结结果，等待影响评估**
   - 环境事件（断电/温度偏离…）在「待评估/评估中」期间，凡**在箱期间与之有时间重叠**的样品，
     其结果按实际驻留时长计算暴露秒数（`engine.exposure_events`）并冻结；
   - 提前/逾期取出自动生成待评估异常；错放、破损可由协调员登记；
   - 冻结期间 QA 不能「纳入」或「追加考察」（服务层抛 `FrozenError`，API 返回 409），
     但始终可以先「排除」；评估「无影响」后解冻，评估「有影响」后由 QA 排除并安排追加。
   - **即使已经纳入**，若之后补报了覆盖该样品在箱时段的待评估事件，结果会自动从
     实时数据集中撤出，直到评估关闭。
6. **职责分离**：分析员只能 POST 结果；纳入/排除/追加只能 QA；协调与替代只能协调员。
   越权调用 API 返回 403（见 `permissions.require_roles`）。

## 页面

- `/dashboard/` 各批次一览：逾期动作数、冻结动作数、待评估偏离暴露合计、当前可用结果数、待评估事件与异常。
- `/batches/<id>/` 批次工作台：**时间轴**（计划/事件/取出/替代/结果合并，实际时间单列）、
  **逾期动作**与快速替代、按时间点的动作表、**样品偏离暴露时长**明细表，以及按角色显示的操作表单。
- `/events/` 环境事件登记（协调员）与影响评估（QA）。
- `/dataset/` **当前可用于趋势 / 货架期评估的数据集**：仅含 QA 已纳入且无待评估冻结的结果，
  并列示计划天、实际龄期及差值、是否替代来源、实际检测时间。

## JSON API 摘要

认证：会话 Cookie + `X-CSRFToken` 头（先 GET 任意页面或 `/api/me/` 获取 `csrftoken` Cookie）。

| 方法 & 路径 | 角色 | 说明 |
|---|---|---|
| `GET /api/me/` | 登录用户 | 当前角色与能力标志（同时设置 CSRF Cookie）|
| `GET /api/protocols/` · `POST /api/protocols/create/` | 读 / 协调员 | 方案版本（`supersedes_id` 生成新版本并自动作废旧版）|
| `GET /api/chambers/` | 登录用户 | 恒温箱 |
| `GET/POST /api/events/` · `POST /api/events/<id>/assess/` | 读 / 协调员 / QA | 环境事件登记与影响评估 |
| `GET/POST /api/batches/` | 读 / 协调员 | 批次（可随建样品并生成计划）|
| `GET /api/batches/<id>/` · `.../schedule/` · `.../overdue/` · `.../timeline/` · `.../exposure/` | 登录用户 | 动作状态、逾期、时间轴、暴露时长 |
| `POST /api/batches/<id>/generate-plan/` | 协调员 | 按方案版本快照生成计划（一次性）|
| `POST /api/actions/<id>/assign/` · `.../substitute/` | 协调员 | 原计划分配 / 替代（必须带 `reason`）|
| `POST /api/assignments/<id>/withdraw/` | 协调员 | 登记**实际**取出时间（窗口外自动立异常）|
| `POST /api/samples/<id>/exceptions/` · `POST /api/exceptions/<id>/assess/` | 协调员 / QA | 错放/破损等异常登记与评估 |
| `POST /api/assignments/<id>/results/` | 分析员 | 提交结果（样品随即消耗）|
| `POST /api/results/<id>/decision/` | QA | `included` / `excluded` / `additional`；冻结返回 409 |
| `GET /api/results/` · `GET /api/dataset/` | 登录用户 | 结果查询 / 当前可用于趋势分析的数据集 |

错误形态：`401` 未认证、`403` 角色或 CSRF 不足、`400` 输入非法、`409` 业务冲突
（`FrozenError` 冻结、`SampleUnavailableError` 样品已消耗/不可用），
响应体为 `{"error": ..., "detail": ...}`。

### 调用示例

```bash
# 1) 登录并取 CSRF token
curl -s -c jar http://localhost:8000/api/me/ -o /dev/null   # 未登录会 401，先经登录页
curl -s -c jar http://localhost:8000/accounts/login/ -o /dev/null
T=$(grep csrftoken jar | awk '{print $7}')
curl -s -b jar -c jar -e http://localhost:8000/accounts/login/ \
  -d "csrfmiddlewaretoken=$T&username=coord&password=coord" \
  http://localhost:8000/accounts/login/ -o /dev/null
T=$(grep csrftoken jar | awk '{print $7}')

# 2) 协调员登记断电
curl -s -b jar -H "X-CSRFToken: $T" -H "Content-Type: application/json" -d '{
  "chamber_id": 1, "event_type": "power_out",
  "started_at": "2026-09-18T02:00:00Z", "ended_at": "2026-09-18T05:20:00Z",
  "observed_max_temp_c": "11.8"}' http://localhost:8000/api/events/

# 3) QA 评估
curl -s -b jar -H "X-CSRFToken: $T" -H "Content-Type: application/json" \
  -d '{"assessment":"no_impact","note":"累积热暴露可接受"}' \
  http://localhost:8000/api/events/1/assess/
```

## 测试

```bash
.venv/bin/python manage.py test stability
```

覆盖：暴露时长的重叠/部分重叠/零暴露/进行中事件；提前/准时/逾期分类与逾期动作；
计划快照不可变；消耗样品拒绝再分配；替代链不改计划；实际取出时间独立存储；
断电与样品异常的冻结/解冻；QA 纳入-排除-追加；角色矩阵（403）、CSRF（403）、
冻结冲突（409）；替代来源在时间轴与数据集中的呈现；方案版本替代。共 48 个用例。

## 代码结构

```
stability/
  models.py        # 方案版本/条件/时间点/检测项、箱体事件、批次/样品、
                   # 计划动作快照、分配(含替代链)、异常、结果与QA判定
  engine.py        # 纯计算：暴露重叠、冻结原因、动作状态、逾期、时间轴、可用数据集
  services.py      # 全部状态机/事务边界（生成计划、分配、替代、取出、异常、判定）
  permissions.py   # coordinator/analyst/qa 三种 Django Group 角色
  api_views.py     # JSON API；api_urls.py 路由
  views.py + templates/  # 总览/批次/事件/数据集页面
  management/commands/seed.py
  tests/           # test_engine / test_services / test_api
```
