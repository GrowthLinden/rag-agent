# RAG Agent

本项目基于 [SuperMew](https://github.com/icey1287/SuperMew) 做二次开发，目标是提供一个对新手也比较友好的本地完整 RAG Agent 项目学习示例：前端可直接聊天，后端支持文档入库、混合检索、流式回答和基础账号权限。

## 这套系统能做什么

- 支持上传 PDF、Word、Excel 文档，并写入知识库。
- 支持普通聊天和基于知识库的问答。
- 支持流式输出，回答会像打字一样实时显示。
- 支持登录鉴权和简单权限控制。
- 支持会话历史保存。


## RAG 核心能力

- 分块策略：文档会做三级滑动窗口分块。
  - L1、L2 作为父块，保存在 PostgreSQL。
  - L3 作为叶子块，写入 Milvus 做向量检索。
- 检索策略：同时做稠密向量检索和 BM25 稀疏检索，再做融合排序。
- 重排策略：召回结果支持 Rerank 精排，提高最终命中质量。
- 上下文拼装：优先召回 L3，小块命中足够多时会自动回收父块，减少上下文碎片。

## 技术栈

- 后端：FastAPI、LangChain、SQLAlchemy
- 前端：Vue 3 单页静态页面
- 数据库：PostgreSQL
- 缓存：Redis
- 向量库：Milvus
- 容器：Docker Compose

## 目录说明

- `backend/`：后端接口、Agent、RAG 逻辑
- `frontend/`：前端页面
- `docker-compose.yml`：PostgreSQL、Redis、Milvus 相关依赖
- `.env.example`：环境变量模板

## 项目说明

- 基于supermew二开，与原项目不同的是页面做了整体的优化，更贴近于市面主流AI对话工具
- 用户查询天气时可以指定查询某天的具体天气。
- 同时可以查询具体的县级市天气，不在局限于市区天气等

## 小白启动教程

下面这套流程按顺序执行，基本就能跑起来。

### 1. 准备环境

你需要先安装：

- Docker Desktop
- Python 3.12+
- `uv`

如果你还没装 `uv`：

```bash
pip install uv
```

### 2. 克隆项目

```bash
git clone https://gitee.com/Kaelan/rag-agent.git
cd rag-agent
```

### 3. 创建配置文件

先复制一份环境变量模板：

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS / Linux:

```bash
cp .env.example .env
```

然后打开 `.env`，至少把下面 4 项改成你自己的真实值：

- `ARK_API_KEY`
- `MODEL`
- `BASE_URL`
- `EMBEDDER`

如果你要开启重排，再补：

- `RERANK_MODEL`
- `RERANK_BINDING_HOST`
- `RERANK_API_KEY`

如果你只是先把系统跑起来，不上传文档、不做正式问答，也可以先保留其他默认值。

### 4. 启动 Docker 依赖

项目里的 Docker 只负责启动基础依赖服务，不直接启动 Python 应用。

```bash
docker compose up -d
```

查看容器状态：

```bash
docker compose ps
```

看到下面这些服务正常启动就可以继续：

- `postgres`
- `redis`
- `etcd`
- `minio`
- `standalone`
- `attu`

### 5. 安装 Python 依赖

```bash
uv sync
```

### 6. 启动后端

```bash
uv run python backend/app.py
```

```bash
uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```
```bash
uv run python backend/app.py
```

### 7. 打开页面

浏览器访问：

- 首页：[http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- 接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 首次使用建议

- 先注册一个账号并登录。
- 如果你要上传文档，建议注册管理员账号。
- 管理员邀请码来自 `.env` 里的 `ADMIN_INVITE_CODE`。
- 上传文档后，再去聊天页面测试知识库问答。

## 默认端口

- FastAPI：`8000`
- PostgreSQL：`5432`
- Redis：`6379`
- Milvus：`19530`
- Milvus Health：`9091`
- MinIO API：`9000`
- MinIO Console：`9001`
- Attu：`8080`

## 常见问题

### 1. 页面能打开，但聊天失败

通常是 `.env` 里的模型配置不对。优先检查：

- `ARK_API_KEY`
- `MODEL`
- `BASE_URL`
- `EMBEDDER`

### 2. 文档上传失败

先确认：

- 你当前登录的是管理员账号
- `docker compose ps` 里 `postgres`、`redis`、`standalone` 都是正常状态

### 3. Milvus 没起来

可以看日志：

```bash
docker compose logs -f standalone
```

### 4. 想清空 Docker 依赖数据

停止服务：

```bash
docker compose down
```

项目数据默认会落在 `volumes/` 目录下。

## 主要接口

- `POST /auth/register`：注册
- `POST /auth/login`：登录
- `GET /auth/me`：获取当前用户
- `POST /chat/stream`：流式聊天
- `GET /sessions`：查看会话列表
- `POST /documents/upload`：上传文档
- `GET /documents`：查看文档列表

## 部分运行图片
- 首页
![输入图片说明](uploads/%E9%A6%96%E9%A1%B5.png)
- 知识库上传
![输入图片说明](%E7%9F%A5%E8%AF%86%E5%BA%93.png)
- 天气查询
![输入图片说明](uploads/%E5%A4%A9%E6%B0%94.png)
- 知识库查询检索过程
![输入图片说明](uploads/image.png)
![输入图片说明](uploads/%E6%A3%80%E7%B4%A2.png)
