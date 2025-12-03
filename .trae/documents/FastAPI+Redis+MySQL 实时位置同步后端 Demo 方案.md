## 项目目标
- 实现多人房间的实时位置同步后端，满足注册/登录、JWT 鉴权、WebSocket 通讯、Redis 持久位置、房间广播、心跳踢人、多房间结构。

## 项目结构
- `main.py`：FastAPI 应用入口，HTTP 路由（注册/登录）、WebSocket 路由 `/ws/{room_id}`、启动事件（DB 表创建、Redis 连接检查）。
- `database.py`：MySQL 异步引擎与会话（SQLAlchemy 2.0 + asyncmy），`User` ORM 模型与表初始化。
- `models.py`：Pydantic v2 模型（`UserCreate`、`UserLogin`、`Token`、`PlayerState`、`BroadcastState`、`JoinLeaveMessage` 等）。
- `deps.py`：依赖函数（`get_redis`、`get_current_user`），JWT 解析、密码校验工具。
- `websocket_manager.py`：房间管理类，管理连接、心跳、广播、加入/离开、Redis 同步。
- `requirements.txt`：完整依赖清单。
- `.env.example`：Redis/MySQL/JWT 最小配置示例，支持 `python-dotenv` 加载。

## 技术选型
- Web 框架：FastAPI（最新版）+ Uvicorn。
- Redis 客户端：`redis.asyncio`。
- MySQL：SQLAlchemy 2.0 异步，驱动 `asyncmy`（或兼容 `pymysql` 但默认使用 `asyncmy`）。
- 鉴权：PyJWT；密码哈希使用 `passlib[bcrypt]`（更安全，避免明文）。
- 数据校验：Pydantic v2。
- 全链路异步：HTTP（注册/登录）与 WebSocket 均采用异步；Redis 操作使用异步接口。

## 数据库设计
- `User` 表（MySQL）：
  - `id`（主键，自增或雪花/UUID，方案选自增 INT）
  - `username`（唯一）
  - `password_hash`（bcrypt 哈希）
  - `created_at`（时间戳）
- 启动时自动创建表；提供会话依赖用于注册/登录。

## 认证与授权
- 注册：`POST /register`，接收 `username`、`password`，写入哈希后的密码。
- 登录：`POST /login`，校验用户名密码，返回 `JWT token`（载荷含 `sub=user_id`、`exp`）。
- WebSocket 鉴权：
  - 连接 `/ws/{room_id}` 时携带 JWT：优先读取 `Authorization: Bearer <token>`，其次读查询参数 `?token=`。
  - 解析失败或过期则拒绝连接（`1008 Policy Violation`）。

## Redis 数据结构
- 成员集合：`room:{room_id}:members`（Set，成员为 `player_id`）。
- 玩家状态：`room:{room_id}:player:{player_id}`（Hash：`x`、`y`、`color`）。
- 加入：`SADD members`、初始化 `HSET player`。
- 移动：`HSET player x y`。
- 离开：`SREM members`、`DEL player`。

## WebSocket 流程
- 加入：
  - 生成 `player_id`（采用用户 `id`），分配随机颜色，初始坐标 `(0,0)`；写入 Redis。
  - 将连接加入房间管理映射；广播 `{"type":"join","player_id":...}`。
  - 立即广播一次当前房间 `state`（聚合所有玩家）。
- 收到消息：
  - `{"type":"move","x":...,"y":...}`：更新 Redis；更新内存状态；广播 `{"type":"state","players":[...]}` 给房间所有连接。
  - `{"type":"ping"}`：回复 `{"type":"pong"}` 并刷新心跳时间。
- 断开或超时：
  - 从房间移除；清理 Redis；广播 `{"type":"leave","player_id":...}`。

## 心跳与掉线
- 客户端每 30s 发送 `ping`；服务端收到后回复 `pong`。
- 连接维护 `last_seen` 时间戳；后台心跳检查任务每 10s 扫描房间连接，超过 60s 未收到任何消息则踢掉（关闭 WebSocket）。
- 断开时清理资源并广播离开消息。

## 房间管理实现要点
- `WebSocketRoomManager`：
  - 字典 `rooms: Dict[str, RoomState]`；`RoomState` 包含：连接集合、玩家字典（含颜色、坐标、last_seen）、异步锁。
  - 方法：`join()`、`leave()`、`broadcast()`、`broadcast_state()`、`update_position()`、`kick_inactive()`。
  - 所有广播与集合操作均在房间级锁下进行，避免竞态。

## 错误处理与健壮性
- DB/Redis 操作使用 `try/except` 包装，记录错误并给出友好响应（HTTP 400/401/500）。
- WebSocket 期间捕获 `WebSocketDisconnect` 与通用异常，确保资源清理与离开广播。
- JWT 解析与过期校验严格处理；密码比对安全。

## 配置与环境
- `.env` 支持（通过 `python-dotenv`）示例变量：
  - `DATABASE_URL=mysql+asyncmy://user:pass@localhost:3306/game`
  - `REDIS_URL=redis://localhost:6379/0`
  - `SECRET_KEY=your-secret-key`
  - `JWT_ALGORITHM=HS256`
  - `ACCESS_TOKEN_EXPIRE_MINUTES=60`
- 代码在启动时加载 `.env`，若未配置则回退到合理默认但打印警告。

## 交付内容
- 按文件完整输出：`main.py`、`database.py`、`models.py`、`deps.py`、`websocket_manager.py`、`requirements.txt`、`.env.example`。全部包含详细中文注释与异常处理。

## 启动与验证
- 安装依赖：`pip install -r requirements.txt`
- 启动命令：`uvicorn main:app --reload`
- 验证流程：
  - 调用 `/register`、`/login` 获取 `JWT`。
  - 使用 `JWT` 连接 `ws://localhost:8000/ws/default`（Header `Authorization: Bearer <token>`）。
  - 发送 `move` 消息观察 `state` 广播；定时 `ping` 收到 `pong`；断开后收到 `leave`。

请确认该方案，确认后我将直接输出完整可运行的项目代码（按文件分隔且包含中文注释）。