# 岩芯化验供数底表系统

把长短不一、按样号管理的岩芯化验段，核算成固定深度统计段的品位底表。
Flask + SQLite，支持 `docker compose` 一键启动。

## 快速开始

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

数据持久化在命名卷 `geology_data`（容器内 `/data/geology.db`）。

本地开发（无需 Docker）：

```bash
python3 -m pip install -r requirements.txt
DATABASE_PATH=./geo.db python3 wsgi.py        # 开发服务器
# 或
DATABASE_PATH=./geo.db gunicorn wsgi:app -b 0.0.0.0:8000
```

运行测试：

```bash
python3 -m pip install pytest
python3 -m pytest
```

## 操作流程

1. **新建钻孔**：首页录入孔号。
2. **定义统计段**：钻孔页每行写 `起厘米 止厘米`，保存即固定深度段（不可重叠）。
3. **上传化验 CSV 换交**：列固定为
   `样号, 起厘米, 止厘米, 品位ppm, 复验标记`，样号在一份 CSV 内唯一，
   品位为整数 ppm；复验标记兼容 `是/否、Y/N、1/0、true/false`
   （表头兼容中英文常见写法，模板见 `/template.csv`，示例见 `examples/`）。
   一次上传代表该孔当前全套化验，系统**按样号**与上一版比对出
   新增 / 消失 / 变更。
4. **查看逐段核查**与**下载供数底表 CSV**（`/holes/<id>/supply.csv`）。

## 核算规则（对应业务口径）

- 只有**完全落在统计段内**的化验段可参与；合格铺法要求化验段首尾相接、
  恰好铺满统计段，不允许越界或留缝。
- 先比**复验覆盖长度**（复验段长度之和），只在复验覆盖**最长**的铺法之间
  再比品位；复验覆盖更短的铺法整体淘汰。
- 品位按化验段长度加权，全程**整数精确运算**：存「加权和分子 / 段长分母」，
  除不尽时底表保留分数，不引入浮点。
- 结果三态：
  - `供数（ok）`：铺满且最长复验覆盖下所有铺法精确品位一致，列入供数 CSV，
    页面展示一套样号；
  - `品位分歧（diverged）`：铺满但最长复验覆盖下加权品位不一致，**不**供数，
    列出最小/最大加权品位对应的两套样号及各自品位；
  - `待补样（incomplete）`：任何铺法都铺不满，标注已覆盖长度与缺口。

### 复杂度保证

每个统计段把化验段按端点建成坐标 DAG 做动态规划，每个坐标只维护
「最长复验覆盖」以及该覆盖下加权和的最小、最大前驱各一个，
时间 O(m log m)、空间 O(m)（m 为段内化验段数）。**核算开销与铺法总数
无关**——即使可行铺法呈指数级，也不会枚举铺法（见
`tests/test_tiling.py::test_exponential_tilings_linear_time`，并附 3000 组
随机暴力对拍脚本可复跑）。

## 换交原子性与重算期间可用性

- 每次上传或重算生成一个**不可变版本快照**：化验行、逐段核查结果、底表
  在**同一 SQLite 事务**内写入，成功后才原子翻转 `hole.effective_version_id`；
  旧版本标记 `superseded` 并完整保留。
- 换交失败（CSV 解析错误、缺段等）整体回滚，不残留半成品版本，
  上次生效底表照常下载。
- SQLite 启用 **WAL**：重算写事务进行时，下载/浏览等读请求不被阻塞，
  读到的始终是上一个已提交版本。

## 目录结构

```
app/
  __init__.py     Flask 路由与应用工厂
  db.py           SQLite 连接、WAL、建表
  tiling.py       铺排核算引擎（DAG 动态规划，精确整数运算）
  csvio.py        化验 CSV 解析校验、供数 CSV 导出
  services.py     版本快照、按样号换交、原子重算
  templates/      中文页面
tests/            引擎/CSV/端到端测试（30 项）
docker-compose.yml / Dockerfile / entrypoint.sh / wsgi.py
```

## 说明与约束

- 统计段之间不允许重叠；不同统计段的化验数据互不干扰（越界化验段不参与）。
- 生产部署请通过环境变量 `SECRET_KEY` 注入密钥，并按需调整
  `GUNICORN_WORKERS / GUNICORN_THREADS`。
