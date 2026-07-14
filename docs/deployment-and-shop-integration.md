# SuperService 上线与商城数据接入方案

## 1. 上线前检查

- 生产环境必须设置 `ENVIRONMENT=production`。
- 必须设置强随机 `JWT_SECRET_KEY`，不要使用模板默认值。
- 必须设置 `CORS_ALLOW_ORIGINS=https://你的域名`，生产环境不要使用 `*`。
- 首次部署可设置 `BOOTSTRAP_ADMIN_USERNAME` 和 `BOOTSTRAP_ADMIN_PASSWORD` 创建第一个管理员；系统已有管理员后不会覆盖。
- 配置 `DATABASE_URL`、`REDIS_URL`、`MILVUS_HOST`、模型、Embedding、Rerank、天气、物流等真实密钥。
- 确认 Docker 依赖健康：PostgreSQL、Redis、Milvus、MinIO、etcd。
- 用管理员账号上传一份测试商品资料，进入知识库页面做检索诊断。

## 2. 部署方式

推荐拆成三层：

- 基础依赖：继续用 `docker compose up -d` 启动 PostgreSQL、Redis、Milvus、MinIO、etcd。
- 后端服务：用进程管理器或容器运行 `uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000`。
- 反向代理：用 Nginx/Caddy 做 HTTPS、域名、访问日志、请求体大小限制。

生产环境建议再补：

- 数据库、Milvus、MinIO 定时备份。
- 后端日志采集和异常告警。
- `/documents/upload`、登录、聊天接口的限流。
- 单独的对象存储或 CDN 承载商品/PDF 图片。

## 3. 商城数据应该怎么进 AI 客服

不要把所有商城能力都塞进普通文档 RAG。推荐采用“结构化商品库 + 向量知识库 + 实时工具”的混合方案。

### 适合入库检索的数据

- 商品标题、卖点、材质、规格、尺码、颜色、适用场景、保养方式。
- 商品详情页长文、FAQ、售后政策、质检说明、品牌故事。
- 商品图片的视觉描述。

这些数据可以同步到 PostgreSQL，并把文本化字段写入 Milvus，供 AI 回答“商品 A 是什么材质、规格如何、适合谁”等问题。

### 必须实时查询的数据

- 价格、库存、上下架状态、活动价。
- 订单状态、物流状态、退款状态。
- 用户专属优惠、会员等级、收货地址。

这些数据必须通过商城 API 实时查，不能只靠知识库，否则很容易答错。

## 4. 标准接入流程

1. 为每个商城创建一个 `shop_id`，保存授权信息和平台类型。
2. 写平台适配器：淘宝/天猫、京东、抖店、有赞、微店、Shopify、WooCommerce、自研商城都转成统一商品模型。
3. 全量同步商品：商品基础信息进入 PostgreSQL，详情文本生成 chunk 后写入 Milvus。
4. 增量同步：通过 webhook 或定时任务更新商品、库存、价格、上下架状态。
5. 查询路由：用户问商品 A 时，先做商品精确/模糊匹配，再拿结构化字段和 RAG 片段一起生成回答。
6. 实时校验：涉及价格、库存、订单、物流时，调用商城实时 API 或现有物流工具。

## 5. 建议新增模块

- `shop_integrations`：商城授权、平台类型、同步状态。
- `products`：标准商品表，包含 `shop_id`、`external_product_id`、`title`、`sku`、`price`、`stock`、`status`、`attributes_json`。
- `product_documents`：商品详情、FAQ、图片描述等可检索文本。
- `sync_jobs`：全量/增量同步任务记录。
- Agent 工具：
  - `search_product_catalog(query, shop_id)`
  - `get_product_detail(product_id)`
  - `get_product_realtime_status(product_id)`
  - `get_order_status(order_no, phone_tail)`

## 6. 分阶段落地

- 第 1 阶段：先支持 CSV/Excel 商品表导入，验证“商品 A 材质规格问答”。
- 第 2 阶段：接一个真实商城 API，完成商品全量同步和定时增量同步。
- 第 3 阶段：接 webhook，价格/库存/上下架实时更新。
- 第 4 阶段：接订单、物流、售后工具，形成完整售前售后客服闭环。
- 第 5 阶段：做多商户隔离、权限、审计、限流和监控。
