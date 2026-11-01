# 闲置物品一键上架 · IdleLister

拍照 → 自动抠图 → 白底商品图排版 → 物品识别 → 成色自评 → 参考定价 → **标题与卖点文案生成** → 登录 → 发布上架 → **闲置商城购买** → 我的商品 / 我的订单。  
一个面向「二手闲置物品快速上架」的端到端 Web 工具，核心是**确定性、不依赖模型**的商品图排版引擎，配合可插拔的 AI 能力完成自动化上架前处理。

> 本仓库实现图一中要求的任务 **1 / 2 / 3 / 4 / 5 / 6 / 9**（共 9 项中）。任务 7（瑕疵标注）、8（多件批量）不在本次范围，已在文末说明扩展位。

---

## ✅ 功能清单（对应图一任务）

| 任务 | 名称        | 实现                                                               |
| -- | --------- | ---------------------------------------------------------------- |
| 1  | 主体分割与背景移除 | `backend/matting.py` — rembg + U2Net，可插拔后端，支持 alpha matting 精细边缘 |
| 2  | 白底商品图生成   | `backend/layout.py` — **自研排版引擎**，规格化白底图 + 质检报告                   |
| 3  | 物品识别与类目推断 | `backend/recognition.py` — YOLO11 检测 + COCO→二手类目映射               |
| 4  | 标题与描述文案生成 | `backend/copywriting.py` — 识别结果 + 成色问卷 + 少量用户输入 → 结构化标题候选与分组卖点   |
| 5  | 成色自评引导    | `backend/pricing.py` — 问卷式引导，评分推导成色等级（结果同时作为文案输入）                |
| 6  | 参考定价建议    | `backend/pricing.py` — 类目基准价 × 成色系数 → 参考区间，支持手动覆盖                |
| 9  | 上架草稿管理    | `backend/drafts.py` + `app.py` — SQLite 草稿增删改查 + 图片文件管理          |

**任务 2「商品图排版引擎」是本项目最扎实的自主开发部分**：不依赖任何模型，  
把抠图得到的 RGBA 主体确定性地合成为平台规范白底图（1:1 800×800 / 1:1 1200×1200 / 3:4 900×1200），  
并输出质检报告（主体占比、缩放倍数、边缘裁切/低清/占比过小等告警），直接决定成品是否可用。

### 任务 4「标题与描述文案生成」

输入是三类**已确认事实**，输出是可直接粘贴到二手平台的结构化文案：

| 输入   | 来源                            | 用途            |
| ---- | ----------------------------- | ------------- |
| 识别结果 | YOLO11 检测（物品名 + 置信度）          | 物品名兜底、识别信息卖点  |
| 成色问卷 | 步骤 4 的 4 道题（使用时长/外观/功能/配件）    | 成色前缀、逐题卖点短语   |
| 少量信息 | 品牌型号、颜色规格、购入渠道、转手原因（4 个可留空字段） | 标题关键词、渠道/原因卖点 |

产出：

- **标题候选 4 个**（标准 / 品类优先 / 卖点前置 / 简洁），点击即可填入商品名称；
- **分组卖点**（使用情况 / 外观状态 / 功能状态 / 配件清单 / 品类要点 / 购入渠道 / 转手原因 / 商品信息 / 识别信息），  
  每条标注 `source`（成色问卷 / 用户填写 / 类目特征 / 识别结果）与 `detail`（生成依据原文）；
- **描述全文**（可直接编辑，随草稿与商品一同存储）；
- **兜底提示**：字段缺失时明确告诉用户还差什么（对应图三「文案字段缺失时的兜底」测试重点）。

工程约束（有意为之，非能力缺失）：

- **不臆造事实**。文案只允许使用上述三类输入，**绝不**编造「无拆修 / 原厂包装 / 专柜正品」等未提供的信息，  
  单元测试对此有专项断言；低成色商品标题也不会出现误导性的「X 成新」。
- **默认零外部依赖**。默认走纯规则引擎（Python 模板拼装），离线可跑、结果确定可测；  
  配置 `COPYWRITING_LLM_BASE_URL` / `COPYWRITING_LLM_API_KEY` / `COPYWRITING_LLM_MODEL` 后，  
  会把规则引擎产出的草稿交给 OpenAI 兼容接口润色，**调用失败自动降级回规则结果**并给出提示。

### 账号与商品体系（增量需求）

| 能力           | 说明                                                        | 实现                                 |
| ------------ | --------------------------------------------------------- | ---------------------------------- |
| 注册 / 登录 / 退出 | PBKDF2-HMAC-SHA256 密码哈希（120000 轮）+ 服务端会话 token（7 天有效，可吊销） | `backend/auth.py`                  |
| 我的商品         | 草稿发布为商品；商品可改名、改价、标记在售/已售出/已下架、删除                          | `backend/products.py` + 前端「我的商品」视图 |
| 商品名称         | 工作台发布前填写，草稿与商品详情均可随时修改                                    | 草稿 `title` 字段 / 商品 `title` 字段      |
| 草稿归属         | 登录后保存的草稿归属本人；匿名草稿仍可见                                      | 草稿 `owner_id` 字段                   |

数据流：`上传处理 → 草稿（可编辑） → 发布上架 → 商品（对外展示）`。  
商品图片在发布时复制为独立副本 `p_<id>.png`，因此**删除草稿不会让已发布商品图片失效**。

### 闲置商城与购买（增量需求）

| 能力        | 说明                                         | 实现                                    |          |
| --------- | ------------------------------------------ | ------------------------------------- | -------- |
| 商城浏览      | **无需登录**即可浏览全部在售 / 已售出商品，支持关键词搜索、类目筛选、价格排序 | `backend/market.py` + `GET /api/shop` |          |
| 下单购买      | 需登录；不能买自己发布的商品；已售出 / 已下架的不可购买              | `POST /api/shop/{id}/buy`             |          |
| 巨大「已售出」标签 | 售出后商品图被压暗 + 覆盖 46px 斜向红色印章，商城与「我的商品」两处均可见  | `.sold-stamp` / `.sold-banner` 样式     |          |
| 我的订单      | 「我买到的」/「我卖出的」双视角，订单内冗余快照商品名与成交价            | \`GET /api/orders?role=buyer\\        | seller\` |

**并发安全**：购买不是「先查后改」，而是用单条  
`UPDATE products SET status='sold' WHERE id=? AND status='on_sale'` 完成状态迁移，  
再靠 `rowcount` 判断是否抢到 —— 8 线程并发购买同一件商品时**有且仅有一笔成交**  
（`tests/test_market.py` 有专项用例）。

**隐私**：商城接口不向其他用户返回买家身份（`sold_to` / `last_buyer_id` 在序列化层剥离），  
只对买家本人返回 `bought_by_me = true`。

**筛选响应竞态**：前端 `loadShop()` 用「请求序号」而非「加载中就 return」来防抖 ——  
后者的写法会在用户于请求飞行期间切换筛选时**静默丢弃新条件**（表现为点了没反应），  
序号方案只丢弃过期响应，最新筛选条件一定会生效。

---

## 🧱 技术架构

```
浏览器 (原生 HTML/JS 单页)
        │  REST (multipart / JSON)
        ▼
FastAPI 后端 (backend/app.py)
   ├─ 抠图   matting.py      ── rembg (U2Net / BiRefNet)
   ├─ 排版   layout.py       ── PIL / numpy（确定性，无模型）
   ├─ 识别   recognition.py  ── Ultralytics YOLO11
   ├─ 定价   pricing.py      ── 规则引擎
   ├─ 文案   copywriting.py  ── 规则引擎（默认）+ 可选 LLM 润色
   ├─ 草稿   drafts.py       ── SQLite + 文件存储
   ├─ 认证   auth.py         ── PBKDF2 + 服务端会话
   ├─ 商品   products.py     ── 已上架商品数据访问层
   └─ 商城   market.py       ── 商城列表 + 订单 + 原子购买
        │
   数据存储 (data/ : uploads / outputs / rgba / images / app.db)
```

三层职责清晰：路由层（`app.py`）只做校验/编排，业务逻辑下沉到各 service 模块，  
数据访问统一走 Repository（`drafts.py` / `products.py` / `market.py`）。

`products.py` 与 `market.py` 的分工是**卖家视角 vs 买家视角**：  
前者管「我的商品」（改名 / 改价 / 改状态 / 删除），后者管「商城浏览 + 下单 + 订单查询」。

---

## 🚀 运行方式

### 1. 安装依赖（自动创建受控虚拟环境）

```bash
# 在仓库根目录执行（Windows / macOS / Linux 通用）
bash scripts/install.sh
```

> 脚本会创建 venv 并安装全部依赖。PyTorch 默认安装 **CPU 版**（体积远小于 CUDA 版，课堂演示足够）。

### 2. 预置模型权重（强烈建议先执行）

```bash
bash scripts/fetch_models.sh
# 内网/受限网络可换源：HF_BASE=https://huggingface.co bash scripts/fetch_models.sh
```

| 文件                     | 用途        | 许可       |
| ---------------------- | --------- | -------- |
| `models/u2net.onnx`    | 默认抠图后端    | 宽松       |
| `models/yolo11n.pt`    | YOLO11 识别 | AGPL-3.0 |
| `models/rmbg-1.4.onnx` | 可选高精度抠图   | **非商用**  |

预置后全流程**不依赖外网**；未预置时程序会在首次调用时自动下载（需外网）。

### 3. 启动服务

```bash
bash scripts/dev.sh
# 默认监听 http://127.0.0.1:8765
# 可用 PORT=9000 bash scripts/dev.sh 覆盖端口
```

打开浏览器访问 `http://127.0.0.1:8765` 即可使用。

> 可选环境变量：  
> `MATTING_BACKEND=rmbg14`（启用 RMBG 高精度抠图）、`MATTING_BACKEND=birefnet`（BiRefNet，需 GPU）、  
> `MAX_UPLOAD_MB=20`、`DEFAULT_SPEC=square_800`、`CORS_ORIGINS=*`。
>
> 可选：启用大模型润色文案（不配置则用规则引擎，功能完整可用）
>
> ```bash
> export COPYWRITING_LLM_BASE_URL="https://your-endpoint/v1"
> export COPYWRITING_LLM_API_KEY="sk-xxx"
> export COPYWRITING_LLM_MODEL="your-model"
> ```
>
> 接口需兼容 OpenAI 的 `POST /chat/completions`；调用失败会自动降级回规则引擎文案。

### 4. 运行排版引擎单元测试

```bash
python tests/test_layout.py      # 7 项测试，覆盖规格/居中/底部锚点/空主体兜底/告警
python tests/test_copywriting.py # 46 项测试，覆盖标题候选/卖点分组/事实边界/字段缺失兜底/LLM 降级
```

---

## 🔌 API 一览

| 方法             | 路径                                                        | 说明                                                                                                 |
| -------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| GET            | `/api/meta`                                               | 规格、类目分组、成色等级、问卷、文案输入字段、服务状态                                                                        |
| POST           | `/api/process`                                            | 上传图片 → 抠图 → 排版 → 识别（返回图片 URL + 识别结果 + 质检报告）                                                        |
| POST           | `/api/layout`                                             | 用不同规格/占比/锚点/投影重排版                                                                                  |
| POST           | `/api/reprocess`                                          | 用「精细边缘」重新抠图（毛发/透明物体）                                                                               |
| POST           | `/api/condition`                                          | 问卷答案 → 成色等级                                                                                        |
| POST           | `/api/pricing`                                            | 类目 + 成色 → 参考价区间                                                                                    |
| POST           | `/api/copywriting`                                        | 类目 + 成色 + 问卷 + 少量信息 → 标题候选 + 分组卖点 + 描述全文                                                           |
| GET/POST       | `/api/drafts`                                             | 草稿列表 / 新建（自动归属当前登录用户，可带文案）                                                                         |
| GET/PUT/DELETE | `/api/drafts/{id}`                                        | 草稿详情 / 更新（含商品名称与文案） / 删除                                                                           |
| POST           | `/api/auth/register` `/api/auth/login` `/api/auth/logout` | 注册 / 登录 / 退出，返回会话 token                                                                            |
| GET            | `/api/auth/me`                                            | 当前登录用户                                                                                             |
| GET/POST       | `/api/products`                                           | 我的商品列表 / 从草稿发布商品（**需登录**）                                                                          |
| GET/PUT/DELETE | `/api/products/{id}`                                      | 商品详情 / 改名改价改状态 / 下架删除                                                                              |
| GET            | `/api/shop`                                               | 商城列表（**公开**，支持 `keyword` / `category_id` / `status` / `sort` / `only_bought` / `limit` / `offset`） |
| GET            | `/api/shop/{id}`                                          | 商城商品详情（公开）                                                                                         |
| POST           | `/api/shop/{id}/buy`                                      | 下单购买（**需登录**；`OWN_ITEM` / `ALREADY_SOLD` 会被拒）                                                      |
| GET            | `/api/orders?role=buyer\|seller`                          | 我的订单（买到的 / 卖出的，**需登录**）                                                                            |
| GET            | `/health` `/ready`                                        | 健康检查 / 服务就绪状态                                                                                      |

认证方式：登录后拿到会话 token，客户端通过 `Authorization: Bearer <token>` 携带（前端自动附带）。

---

## 🧪 自动化测试

```bash
python tests/test_layout.py      # 排版引擎单测（7 项）
python tests/test_copywriting.py  # 文案引擎单测（46 项，含"不臆造事实"与 LLM 降级专项断言）
python tests/test_market.py       # 商城数据层单测（42 项，含 8 线程并发抢购专项断言）
python tests/test_frontend.py    # 前端静态校验（JS 语法 + DOM id 一致性 + 关键功能接入）
python scripts/smoke_test.py     # API 端到端冒烟（60 项，需服务已启动）
python tests/ui_test.py          # 真实 Chrome(CDP) 端到端 UI 验证（38 项，需服务已启动）
```

- `test_copywriting.py` 覆盖：标题候选生成与去重、卖点分组与来源标注、  
  **事实可追溯性（不臆造「无拆修 / 原厂包装 / 专柜正品」等未提供信息）**、  
  字段缺失兜底、非功能类目措辞适配、低成色标题处理、用户输入清洗、  
  **LLM 润色通道（本地假端点，校验 prompt 携带事实）与失败/异常响应自动降级**。
- `test_market.py` 覆盖：购买主流程（商品状态 / `sold_at` / `sold_to` / 买家标记）、  
  **购买约束（买自己的 / 买不存在的 / 重复买 / 买已下架的）**、  
  **并发抢购（8 线程仅 1 笔成交、订单表仅 1 条记录）**、  
  订单查询（买/卖双视角 + 快照字段）、商城筛选与排序、分页、卖点 JSON 解析。
- `smoke_test.py` 覆盖：注册 → 登录 → 处理图片 → 建草稿 → **生成文案（含字段缺失兜底与非法类目拒绝）** →  
  草稿列表 → 发布商品（带文案）→ 我的商品列表 → 改名改价 → 未登录保护 →  
  **商城浏览（公开可访问 / 卖家名 / 不泄露买家身份）** →  
  **第二账号购买（买自己的被拒 409 / 未登录 401 / 下单成功 / 商品转 sold）** →  
  **商城筛选（已售出 / 我买到的）→ 重复购买被拒 → 订单双视角查询** →  
  **删除草稿（200 + 后续 404）** → 商品图片独立可访问 → 删除商品 → 登出 → token 失效。
- `ui_test.py` 覆盖：注册登录 → 处理图片 → 草稿箱卡片渲染 → **删除二次确认与删除中反馈** →  
  我的商品视图 → 发布商品 → 商品改名 → 商品删除 →  
  **文案卡片出现 → 填写少量信息 → 生成标题候选 → 卖点分组渲染 → 描述全文 → 标题一键填入/替换** →  
  **商城视图 → 自己的商品显示「我发布的」→ 买家下单 → 巨大「已售出」标签出现** →  
  **标签覆盖整图 / 字号 ≥26px / 有旋转 / 粗体高层级（真实 computed style 断言）** →  
  **已售商品无购买按钮 → 已售出筛选 → 订单双视角** → 未登录提示登录 → 未登录仍可浏览商城。  
  依赖 `websocket-client`（`pip install websocket-client`）与本机 Chrome。

---

## 🧠 模型与许可说明（部署必读）

| 能力       | 模型                          | 许可              | 备注                                                                            |
| -------- | --------------------------- | --------------- | ----------------------------------------------------------------------------- |
| 抠图（默认）   | rembg + **U2Net**           | 宽松（MIT 等）       | 即插即用、CPU 可跑，本仓库默认后端                                                           |
| 抠图（可选）   | **RMBG-1.4**（BriaAI）        | **非商用许可**       | `MATTING_BACKEND=rmbg14` 启用（本地 ONNX 推理，无需外网）；课堂演示不涉及商用分发，但部署文档须标注             |
| 抠图（可选）   | **BiRefNet**                | 代码 MIT，权重使用前请确认 | 通过环境变量 `MATTING_BACKEND=birefnet` 启用，边缘更精细、需 GPU                              |
| 抠图（可选）   | **RMBG-2.0**（BriaAI）        | **非商用许可**       | 需自行准备权重手动接入                                                                   |
| 识别       | **YOLO11**（Ultralytics）     | AGPL-3.0        | 课堂演示不涉及商用分发；如用于商业产品需注意 AGPL 义务                                                |
| 文案（默认）   | 无模型，纯规则引擎                   | 本仓库自有代码         | 任务 4 默认路径，零外部依赖、离线可跑                                                          |
| 文案（可选增强） | 任意 OpenAI 兼容大模型（如 Qwen3-8B） | 视具体模型           | 配置 `COPYWRITING_LLM_BASE_URL` / `_API_KEY` / `_MODEL` 后启用润色；**调用失败自动降级回规则引擎** |

> ⚠️ 部署文档需注明：若启用 RMBG 系列（非商用许可）模型，产物仅限非商业/课堂演示用途；  
> 默认的 U2Net 后端许可宽松，可自由使用。  
> 文案默认路径不涉及任何第三方模型权重，无额外许可约束。

---

## 🧪 测试重点与兜底策略（对应图三）

- **复杂边缘（毛发、透明物体）**：提供「精细边缘」按钮，使用 alpha matting 重抠；BiRefNet 可进一步提升。
- **多主体干扰**：识别阶段取面积最大的非人物目标作为待售主体；工作台提供「框选主体」裁剪工具，先裁后抠。
- **字段缺失兜底**：
  - 识别不可用 / 未识别到类目 → 前端提示并**引导手动选择类目**，不中断流程；
  - 成色问卷未答 → 按中间档估算并提示；
  - 抠图后端初始化失败 → 自动降级回退，并返回明确错误；
  - **文案输入缺失** → 仍能基于已有事实产出标题与卖点，并列出「建议补充哪些字段」（品牌/规格/问卷完成度/售价）；
  - **文案不臆造** → 用户未提供的信息（如「无拆修」「原厂包装」）绝不出现在文案中；
  - 可选 LLM 润色失败 → 自动降级回规则引擎文案，并在 `warnings` 中说明。
- **质检报告**：排版引擎输出主体占比、缩放倍数、裁切/低清/占比过小告警，便于现场判断是否需重拍。

---

## 📁 目录结构

```
idle-item-lister/
├─ backend/
│  ├─ app.py          # FastAPI 路由 + 中间件 + 静态资源
│  ├─ config.py       # 环境变量配置（fail fast）
│  ├─ layout.py       # 商品图排版引擎（自研）
│  ├─ matting.py      # 抠图后端（rembg，可插拔）
│  ├─ recognition.py  # 物品识别（YOLO11 + 类目映射）
│  ├─ pricing.py      # 成色自评 + 参考定价
│  ├─ copywriting.py  # 标题与描述文案生成（规则引擎 + 可选 LLM）
│  ├─ drafts.py       # 草稿数据访问层（SQLite）
│  ├─ auth.py         # 用户与会话（PBKDF2 + 服务端 token）
│  ├─ products.py     # 已上架商品数据访问层（卖家视角）
│  ├─ market.py       # 商城浏览 + 订单 + 原子购买（买家视角）
│  └─ requirements.txt
├─ frontend/
│  ├─ index.html      # 单页应用结构（工作台 / 商城 / 我的商品 / 草稿箱 / 登录弹层）
│  ├─ styles.css      # 浅色卡片式样式（含巨大「已售出」印章）
│  └─ app.js          # 上传→管线可视化→问卷→定价→文案→草稿→发布→商城购买→我的商品 全流程
├─ tests/
│  ├─ test_layout.py  # 排版引擎单元测试（7 项）
│  ├─ test_copywriting.py # 文案引擎单元测试（46 项）
│  ├─ test_market.py  # 商城数据层单元测试（42 项，含并发抢购）
│  ├─ test_frontend.py# 前端静态校验
│  ├─ ui_test.py      # 真实 Chrome UI 端到端测试（38 项）
│  └─ assets/         # 测试用图
├─ scripts/
│  ├─ install.sh      # 一键安装依赖
│  ├─ fetch_models.sh # 预置模型权重
│  ├─ smoke_test.py   # API 端到端冒烟脚本（60 项）
│  ├─ screenshot.py   # 真实浏览器跑完整流程并生成截图
│  └─ dev.sh          # 启动开发服务器
├─ data/              # 运行时生成：uploads / outputs / rgba / images / app.db（不入库）
├─ models/            # 模型权重：u2net.onnx / yolo11n.pt（不入库，用 fetch_models.sh 预置）
├─ .gitignore         # 精确到文件级的忽略规则
├─ .gitattributes     # 统一换行符：仓库内 LF，脚本保持 LF 以免 shebang 失效
└─ README.md
```

---

## ☁️ 从 GitHub 克隆后如何跑起来

仓库里**不含**模型权重和运行时数据（体积大且可再生），克隆后按下面顺序补齐即可：

```bash
git clone <你的仓库地址>
cd idle-item-lister

bash scripts/install.sh        # 1. 装依赖（创建 .venv）
bash scripts/fetch_models.sh   # 2. 预置权重（约 174MB，走 hf-mirror 镜像）
bash scripts/dev.sh            # 3. 启动，访问 http://127.0.0.1:8765
```

| 目录        | 是否入库   | 说明                                              |
| ----------- | --------- | ------------------------------------------------- |
| `backend/` `frontend/` `tests/` `scripts/` | ✅ | 源码本体，约 360KB                                |
| `data/`     | ❌        | 运行时自动生成（`config.ensure_dirs()` 建目录、建表） |
| `models/`   | ❌        | 需手动执行 `fetch_models.sh` 预置                  |

> `data/` 与 `models/` 目录内各保留了一个 `.gitkeep`，克隆后目录结构依然可见，
> 直接把权重放进 `models/`、让程序自建 `data/` 即可，无需手动 mkdir。

**为什么权重不入库**：`u2net.onnx` 单文件约 168MB，超过 GitHub 单文件 100MB 硬上限，
即便用 LFS 也会让仓库体积和克隆时间显著膨胀。`fetch_models.sh` 已支持镜像源，
在受限网络环境下也能完成预置。



---

## 🐛 踩坑记录（供后续维护参考）

1. **删除接口偶发 500**：部分运行环境（WorkBuddy 沙箱）会把 `Path.unlink()` 改写为「移入回收站」，
   文件已不存在时抛的是 `OSError` 而非 `FileNotFoundError`，`missing_ok=True` 并不生效。
   统一改用 `backend/app.py` 的 `safe_unlink()`（先判断存在性 + 吞掉 OSError）。
2. **删除「点了没反应」**：回收站操作耗时 1–3 秒，前端必须给出「删除中…」的即时反馈（`markDeleting()`），
   并用**行内二次确认**替代原生 `confirm()`（内嵌预览可能拦截原生对话框）。
3. **草稿图路径不能直接给浏览器**：接口返回前须把服务端绝对路径转成 `/outputs/xxx.png`（`_public_draft()`）。
4. **FastAPI `StaticFiles` 在导入期挂载**：数据目录必须在模块导入时创建，不能只放在 startup 钩子里。
5. **PIL `ellipse` 描边含端点**：200px 圆的实际包围盒是 201px，写单测时别把尺寸断言死。
6. **GitHub / huggingface.co 直连被拦截**：改用 `hf-mirror.com` 预置权重，见 `scripts/fetch_models.sh`。
7. **端口被占用时 uvicorn 报 10048**：先 `netstat -ano | grep :端口` 查占用 PID 再决定是否终止；
   本机曾有一个长期运行的 `python -m http.server` 占着 8765 的通配地址，
   我们的服务绑**具体地址 127.0.0.1** 可与之共存（Windows 下具体地址优先命中）。
8. **文案类生成要守住「事实边界」**：模板引擎最容易出的错不是写不出来，而是**写得太顺**——
   把「无拆修 / 原厂包装 / 专柜正品」这类用户没提供的信息顺手写进卖点。
   解法是把每条文案都绑定一个可追溯的 `source`，并对禁用词做单元测试断言（见 `tests/test_copywriting.py`）。
9. **同一句文案要按类目调整措辞**：成色问卷里的「功能状态」对手机可写「开机运行无异常」，
   对鞋子就荒谬。引擎用 `NON_FUNCTION_CATEGORIES` 白名单切换到中性表述。
10. **CDP 的 `DOM.setFileInputFiles` 在本机不生效**：`files.length` 仍为 0，前端收不到文件，
    表现为 `处理失败：Failed to fetch`。改用 `base64 + DataTransfer` 注入 `<input type=file>` 并派发 `change`，
    行为与用户手动选文件一致。
11. **onnxruntime 在 Python 3.13 上导入即段错误**（`Segmentation fault`，退出码 139）：
    服务启动后 2 秒内静默退出，`/ready` 无响应。定位关键在于**分段验证**：
    - `onnxruntime.dll` 用 ctypes 直调 C API 完全正常（`OrtGetApiBase` 返回有效指针）
    - 问题只出在 pybind11 绑定层 `onnxruntime_pybind11_state.pyd`
    - 版本实测：1.31.1 段错误 / 1.22.0 `DLL 初始化例程失败` / **1.20.1 正常**
    - 与 CPU 指令集（AVX2 齐全）、MSVC 运行库、numpy 版本均无关
    已在 `requirements.txt` 锁定 `onnxruntime>=1.20,<1.21`，并在 `install.sh` 中加导入校验 + 自动回退。
12. **pip 官方源在国内慢到无法安装**：实测 PyPI 仅 **10 KB/s**，腾讯云镜像 **960 KB/s**（相差 90 倍）。
    `torch` 这类 200MB 级别的包，按官方源需数小时。`install.sh` 默认走腾讯云镜像，
    海外环境可用 `PIP_INDEX_URL=https://pypi.org/simple` 覆盖。
13. **不要直接 `pip install torch`**：会连带拉入整套 NVIDIA CUDA 运行时（数 GB），
    无 GPU 环境下既慢又无用。`install.sh` 会装完 torch 后检查 `site-packages/nvidia` 目录，
    若存在则改用官方 CPU 源重装（实测镜像版 torch 2.15.0+cpu 无 nvidia 依赖，体积仅约 200MB）。
14. **模型权重不要入库**：`u2net.onnx` 单文件约 168MB，超过 GitHub 单文件 100MB 硬上限。
    用 `scripts/fetch_models.sh` 预置（已实测镜像源三个权重均可下载，
    官方 huggingface.co 当时返回 502，故脚本内置多源回退）。
15. **GitHub 网页版上传会隐藏 `.gitignore`**：这是平台的设计行为（避免上传的忽略规则
    把同批文件过滤掉），导致点文件永远传不上去。解法是用 git push 而非网页拖拽，
    见 `scripts/push_to_github.sh`；或网页端用 `Add file → Create new file` 手动创建。
11. **「加载中就 return」的防抖写法会吞掉筛选条件**：商城列表早期用
    `if (state.shop.loading) return;` 防止重复请求，结果用户在请求飞行期间点「已售出」筛选时，
    **新条件被静默丢弃**，界面毫无反应。正确写法是**请求序号**：每次调用自增 `seq`，
    响应回来后若 `seq` 已过期就丢弃本次结果，只有最新请求才渲染 ——
    过期的是响应，不是用户的意图。
12. **抢购要用条件 UPDATE，不能「先查后改」**：`SELECT` 查状态再 `UPDATE` 存在竞态窗口，
    两个买家可能同时通过检查。改用单条
    `UPDATE products SET status='sold' WHERE id=? AND status='on_sale'` 并检查 `rowcount`，
    把「检查 + 修改」压进一条语句，由数据库保证原子性。
13. **商城接口不能泄露买家身份**：`sold_to` / `last_buyer_id` 必须在序列化层剥离，
    否则任何人都能看到「这件商品是谁买的」，只对买家本人返回 `bought_by_me = true`。
11. **测试断言不要写死 `True`**：曾用 `check("xxx", True)` 紧跟 `cdp.wait(...)`，
    导致 wait 失败也被判通过（假绿）。应始终断言 wait 的返回值。

---

## 🔜 未在本次实现（扩展位）

- **任务 7 瑕疵标注**：在识别结果图上叠加可拖拽标注框 + 文字，结果存草稿（可在 `drafts` 表扩展 `defects` 字段）。
- **任务 8 多件批量**：`/api/process` 支持多文件，循环复用当前管线，批量生成草稿。
- **任务 4 进阶方向**：当前为规则引擎 + 可选 LLM 润色；接入本地图生式模型（如 Qwen3-8B 本地部署）
  可进一步提升标题多样性，同步把 `copywriting.py` 的 `generate_by_rules` 换成模型直出即可，接口契约不变。

---

## 📝 已知限制

- 定价依据为**内置同类目参考价表**，用于课堂演示；接入真实行情数据源后即可达到生产级参考价。
- CPU 下首张图处理较慢（约 10–30s，主要为模型推理），后续图片因模型已加载会明显加快。
- YOLO11 基于 COCO 80 类，对部分二手垂直品类（如特定品牌款型）识别有限，此时走手动选择类目兜底。
- 文案引擎为**规则模板**，长句表达的多样性不如大模型直出；已预留 LLM 润色通道，
  但默认路径刻意不依赖外部模型，以保证课堂演示环境（可能离线）稳定可用。
- 商城是**单机演示级**交易：无支付网关、无物流、无卖家发货状态流转，
  「购买」只完成商品状态流转与订单记录；接入真实支付需另加订单超时/退款等状态机。
- 已售出商品**仍保留在商城里**并打上「已售出」印章（而非从列表移除），
  这是二手市场的常见做法（已成交记录本身也是信任信号），但如需纯在售列表可传 `status=on_sale`。
