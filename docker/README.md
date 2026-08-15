# XWorker 一体化镜像构建文件

构建 XWorker 一体化容器镜像：**StaffDeck(后端 + 前端)+ Codex CLI + cc-switch-web + lark-channel-bridge** 同容器运行,supervisord 统一托管。

StaffDeck 本体 100% 由当前仓库源码构建(backend 源码 COPY、前端用本地已构建的 `frontend-enterprise/dist`),不拉取任何外部 StaffDeck 发布包/镜像;仅第三方 pip 依赖在镜像构建期从清华 PyPI 下载一次。

## 文件清单

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 镜像定义:Python 3.14 + Node 24 + Codex CLI + cc-switch-web + lark-channel-bridge + StaffDeck 后端(uv venv) |
| `docker-compose.yml` | 生产部署编排(xworker 容器、卷、端口、环境变量) |
| `docker-compose.dev.yml` | 开发 overlay:源码 bind mount + 热重载(频繁开发用) |
| `entrypoint-codex.sh` | 容器入口:权限修复、codex 配置模板初始化、只读加固、StaffDeck 数据目录初始化、DNS/hosts 固化 |
| `supervisord.conf` | cc-switch-web / lark-bridge / staffdeck 三个常驻进程托管 |
| `staffdeck-start.sh` | StaffDeck 启动包装:STAFFDECK_RELOAD=1 时启用 uvicorn --reload |
| `bridge-start.sh` | lark-bridge 包装脚本(未配置时保活等待) |
| `config.toml` | codex 配置模板(deepseek provider + model_catalog_json 引用),entrypoint 首次启动时幂等复制到 `~/.codex` |
| `model-catalog.json` | codex 自定义模型目录模板:deepseek-v4-pro / deepseek-v4-flash 条目 |

## 架构要点(同容器部署)

- StaffDeck 后端(FastAPI,5173)与 Codex CLI 在同一个容器内运行,backend 的 codex 适配器直接 `codex exec`,不再依赖 `docker exec` 或 `host.docker.internal`
- codex 调用企业工具走回环 MCP gateway:`TOOL_BASE_URL=http://127.0.0.1:5173` → `http://127.0.0.1:5173/api/mcp/{token}`
- 员工工作区:`CODEX_WORKSPACE_ROOT=/home/appuser/workspace/staffdeck`(对应宿主编 bind mount)
- `STAFFDECK_SITE_CHAT_UPSTREAM` 站点对话代理功能在容器内不可用(无 10187 上游服务)

## 双用户分离(安全边界)

StaffDeck 与 codex **分属不同 uid**,codex 的 shell 无法触碰后端数据:

| 用户 | 职责 | 可访问 |
|---|---|---|
| `staffdeck` | StaffDeck 后端(FastAPI) | 后端源码(750)、数据库/`.env`(700)、venv、tools-vendor |
| `appuser` | codex / cc-switch / lark-bridge | 自己的 `~/.codex`、workspace(staffwork 组协作写) |
| `staffwork` 组 | workspace 协作写 | `/home/appuser/workspace`(2770 setgid,双方会话目录互通) |

- 后端 spawn codex 的通道:`CODEX_CLI_PATH=/usr/local/bin/codex-run` → `sudo -H -u appuser /opt/node/bin/codex`(免密 sudo 规则仅收敛到这一条命令,见 Dockerfile 的 `/etc/sudoers.d/staffdeck-codex`)
- codex 的 shell 无法读 `.env`/数据库/后端源码,也无法杀后端进程(不同 uid)
- workspace 的协作写靠 `staffwork` 组 + `2770 setgid` + 后端 `umask 002`(staffdeck-start.sh)
- 取消/超时清理:后端以 `start_new_session` 启动 codex 包装链,`killpg` 杀整个进程组(sudo + codex 一起清)

## 构建

在**仓库根目录**执行(build context = 仓库根,靠 `.dockerignore` 排除大目录):

```bash
docker build -f docker/Dockerfile -t xworker .
```

依赖分层缓存:`backend/pyproject.toml` + `backend/uv.lock` 是独立镜像层,依赖不变时重建秒级。

## 数据安全(重要)

**`docker build` 只造镜像,不碰运行数据;recreate 容器不丢数据**,所有状态都在卷/bind mount:

| 数据 | 位置 | 机制 |
|---|---|---|
| StaffDeck 数据库(sqlite + WAL) | `/opt/staffdeck/staffdeck-data` | 卷 `xworker-staffdeck-data` |
| StaffDeck 日志/用户数据 | 同上(`ULTRARAG_DATA_DIR` 指向) | 同上 |
| codex 登录态/配置/会话 | `/home/appuser/.codex` | 卷 `xworker-codex-home` |
| 飞书 bridge 绑定 | `/home/appuser/.lark-channel` | 卷 `xworker-lark-home` |
| cc-switch-web 数据 | `/home/appuser/.cc-switch` | 卷 `xworker-ccswitch-home` |
| 员工工作区(代码任务产物) | `/home/appuser/workspace` | 宿主机 bind mount |

升级流程 `docker compose build && docker compose up -d` 全程保留数据。

**⚠ 会丢数据的情况:**

1. `docker compose down -v` 或手动 `docker volume rm` —— 显式删卷,勿在保留数据时使用
2. 从旧 `ai_emp_codex` 卷迁移(可选):旧 codex 登录态/飞书绑定在新卷下不存在。两条路:
   - 简单:重新 `codex login` + 飞书重新扫码(推荐)
   - 迁移:复制旧卷数据到新卷,例:
     ```bash
     docker run --rm -v ai_emp_codex-codex:/from -v xworker-codex-home:/to alpine \
       sh -c 'cp -a /from/. /to/'
     # 同理迁移 ai_emp_codex-lark → xworker-lark-home、ai_emp_codex-ccswitch → xworker-ccswitch-home
     ```

## 生产部署(Linux 服务器)

前置要求:Docker ≥ 20.10、Linux 内核 ≥ 5.13(workspace-write 沙箱依赖 landlock)。

```bash
# 1. 构建镜像(仓库根)
docker build -f docker/Dockerfile -t xworker .

# 2. 修改 docker/docker-compose.yml:
#    - /opt/staffdeck/workspace 改为实际工作区路径
#    - CORS_ORIGINS 改为 http://<服务器IP>:5173
#    - 密钥类配置取消 env_file 注释并准备 backend/.env(cp .env.example .env)
#    - 按需调整端口映射与资源 limits
cd docker && docker compose up -d

# 3. 首次登录 codex(容器内,写入 xworker-codex-home 持久卷)
docker exec -it -u appuser xworker codex login
```

访问:前端 `http://<服务器IP>:5173`(重定向到 /chat),Swagger `http://<服务器IP>:5173/docs`,cc-switch-web `http://<服务器IP>:3005`。

宿主机注意事项:

- **端口冲突**:宿主机 5173 若还有旧 backend 进程需先停
- **防火墙**:放行 5173(建议仅本机 + 反向代理/TLS);3005 建议仅本机访问
- **网络依赖**:容器内 codex 访问 deepseek API;entrypoint 已固化公共 DNS;容器内访问宿主服务不再需要(同容器回环)

## 生产部署(本地 Docker Desktop,Windows)

```bash
# 1. 构建镜像
docker build -f docker/Dockerfile -t xworker .

# 2. 改 docker-compose.yml 的 bind mount 为本地路径,例如:
#    - E:/My_Files/Project/StaffDeck/workspace:/home/appuser/workspace
# 3. 启动
cd docker && docker compose up -d
```

## 开发模式(频繁开发,推荐)

源码 bind mount + uvicorn 热重载,**改代码即时生效、零重建**;只有 backend 依赖变更才需要 rebuild(依赖层缓存,秒级)。

```bash
cd docker
# 首次:构建镜像(装依赖层)
docker compose build
# 叠加开发 overlay 启动
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

- 改 `backend/` Python 代码 → 容器内自动热重载(Windows 挂载靠 `WATCHFILES_FORCE_POLLING=true` 轮询,2~3 秒生效)
- 依赖变更(`backend/pyproject.toml` / `backend/uv.lock`)→ `docker compose build` 后重启容器
- 前端开发:改 `frontend-enterprise/src` 后本地执行 `npm run build`(或 `npm run dev` 起 vite dev server 单独联调),dist 已挂载,构建后刷新页面即可
- 数据(数据库/登录态)仍在命名卷,开发与生产可共用一套数据
- 结束开发切回生产模式:`docker compose up -d`(无 overlay 启动,镜像内源码)

## 环境变量清单(compose environment 注入)

| 变量 | 值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:////opt/staffdeck/staffdeck-data/skill_agent_loop.db` | 数据库(卷内,勿改) |
| `ULTRARAG_DATA_DIR` | `/opt/staffdeck/staffdeck-data/user-data` | 日志/用户数据目录 |
| `TOOL_BASE_URL` | `http://127.0.0.1:5173` | codex 回环访问 MCP gateway |
| `GENERAL_SKILL_RUNTIME_AUTO_INSTALL` | `false` | 运行时不联网装包(构建期已预装 runtime-venv) |
| `GENERAL_SKILL_RUNTIME_PYTHON` | `/opt/staffdeck/runtime-venv/bin/python` | 通用技能运行时解释器 |
| `CODEX_WORKSPACE_ROOT` | `/home/appuser/workspace/staffdeck` | 员工工作区容器端路径 |
| `CODEX_CLI_PATH` | `/usr/local/bin/codex-run` | 后端以 appuser 身份执行 codex 的 sudo 包装器(双用户分离,勿改) |
| `CORS_ORIGINS` | 部署者改 | 前端访问 origin |
| `APP_SECRET` / `DEMO_MODEL_API_KEY` / `CHANNEL_SECRET` 等 | 部署者改 | 密钥类,推荐经 `backend/.env` 注入 |

`.env` 注入方式:取消 compose 中 `env_file: ../backend/.env` 注释。`environment` 优先级高于 `env_file`,容器路径类配置不会被旧 `.env` 里的 Windows 路径覆盖。数据卷内也可放 `.env`(`/opt/staffdeck/staffdeck-data/.env`,即容器内 `ULTRARAG_DOTENV`),优先级低于环境变量。

## 前端配置 stdio MCP 工具(通用规则)

企业控制台新增 stdio MCP 工具时,容器内已固化 npm 国内镜像(npmmirror)+
npx 缓存卷,`command=npx` 形式**直接可用**(首次下载秒级,之后缓存命中)。
按以下规则填即可,无需改镜像:

| 字段 | 规则 |
|---|---|
| command | `npx` 或 `uvx`(均已固化国内镜像+缓存卷);`node`/`python` 直跑则依赖须在镜像内 |
| args | `["-y", "<包名>@<版本>"]`(npx)/ `["<包名>"]`(uvx),**务必带 @版本号**(npx),避免漂移 |
| env | 要连宿主编服务(MySQL/Redis/内网 API)时,host 一律填 `host.docker.internal`,端口用宿主端口;`localhost` 指向容器自身,连不上宿主编服务 |
| 保存后 | 点「同步」生成工具 → 给员工绑定(同步失败看 stderr 日志:`staffdeck-data/logs/`) |

说明:

- **首次下载**走 npmmirror 镜像,几秒到几十秒;缓存落在 `/home/staffdeck/.npm`(卷 `xworker-npx-cache`),recreate 不丢,之后每次调用 1-2 秒启动
- 追求最快调用延迟(每次 <1s 启动)的工具,可以走「预装」方式:包名加进 Dockerfile 的 tools-vendor 段落并重建镜像,配置填 `node` + 绝对入口(见下方 mysql 示例)
- 纯本地计算、不连宿主编服务的工具没有 host 问题,填完 command/args 即可

## mysql stdio MCP 工具(预装示例)

镜像已预装 `@benborla29/mcp-server-mysql@2.0.2` 到
`/opt/staffdeck/tools-vendor/node_modules/...`,避免运行时 npx 下载超时。
企业控制台配置该工具时:

- `command`: `node`
- `args`: `["/opt/staffdeck/tools-vendor/node_modules/@benborla29/mcp-server-mysql/dist/index.js"]`
- `env`: `MYSQL_HOST` 填 `host.docker.internal`(宿主 MySQL),其余按实际填

## 上线验证清单

1. `curl http://localhost:5173/api/health` 返回正常
2. `docker exec xworker getent hosts host.docker.internal` 返回宿主可达地址(cc-switch 兼容项)
3. 容器内解析 `api.deepseek.com` < 1s
4. 员工对话:新会话 + 续聊各一次正常
5. 工具调用:query_knowledge / run_general_skill / mysql 各一次
6. 双用户分离:`docker exec -u appuser xworker cat /opt/staffdeck/staffdeck-data/.env` 应报 Permission denied
7. 长任务中途取消 → 容器内 codex 进程被清理(`ps aux | grep codex` 无残留)
8. `docker compose restart` 后数据库数据仍在(验证持久化)

## model-catalog.json 的关键字段(务必保留)

codex 内置模型目录只含 gpt-5.6 系列,自定义 deepseek 条目基于内置
`gpt-5.6-luna` 模板修改,以下字段组合决定「自动评审模型名」与「MCP 工具注入」:

- `slug`: `deepseek-v4-pro` / `deepseek-v4-flash` —— 自动评审请求以此名发给
  deepseek API;若缺失会 fallback 到 gpt-5.6-luna,deepseek 拒绝导致工具调用
  被判定 unacceptable risk。
- `tool_mode`: `null` —— 不限制为 code_mode_only。
- `supports_search_tool`: `false` —— 官方 workaround([openai/codex#36382](https://github.com/openai/codex/issues/36382)):
  为 true 时 MCP 工具被延迟注册而模型不可见。
- `use_responses_lite`: `false` —— 关闭原生模型精简 responses 模式。
- `multi_agent_version`: `null` —— 关闭原生多 agent 模式。

## 配置模板的固化机制

`~/.codex` 是 volume,直接 COPY 进镜像会被挂载遮蔽,因此采用
「镜像内置模板 + entrypoint 幂等初始化」:

1. 文件不存在 → 从 `/opt/codex-templates/` 复制(新部署自动完成);
2. 已存在 → 不覆盖(保留登录态 auth.json 与用户自定义);
3. 旧 config.toml 缺少 `model_catalog_json` 引用 → entrypoint 自动在顶层补一行(升级兼容);
4. config.toml / model-catalog.json / skills 目录在每次启动时重置为 root 只读(机制 2 防线)。

## 已知容器内非固化项

`/etc/resolv.conf`(公共 DNS 优先)与 `/etc/hosts`(`host.docker.internal` 静态映射)
已由 entrypoint 每次启动幂等固化(见 `entrypoint-codex.sh`),容器 recreate 后自动恢复;
`/home/appuser/workspace` 的 bind mount 路径、端口映射与资源限制由运行参数
(compose / docker run)决定,部署时按实际路径填写。
