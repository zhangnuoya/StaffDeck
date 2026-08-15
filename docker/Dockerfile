# XWorker 一体化容器：StaffDeck（后端 + 前端 dist）+ Codex CLI + cc-switch-web + lark-channel-bridge
# 包含：Python 3.14 + Node.js 24.14 + Codex CLI + StaffDeck 后端（uv venv）
# 进程管理由 supervisord 托管，无需 --privileged
# StaffDeck 本体 100% 由构建上下文（仓库根）源码 COPY，不从外部拉取；
# 仅第三方 pip 依赖在构建期从清华 PyPI 下载一次。
# 构建：docker build -f docker/Dockerfile -t xworker .（在仓库根执行）

FROM python:3.14-slim

# 系统依赖：
#   ca-certificates curl xz-utils - 下载与解压
#   git                  - Codex 任务的 diff/commit 等 git 操作（缺失会报 exit 127）
#   ripgrep              - Codex CLI 搜索功能依赖
#   procps               - supervisord 进程管理 (ps 等)
#   supervisor           - 进程托管
#   bash                 - Codex CLI / bridge 脚本依赖
#   libssl3 libssl-dev   - cc-switch-web (Rust 二进制 TLS)
# deb.debian.org 直连慢（国内网络下 apt 层会卡 10 分钟+），先换清华镜像源
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        git \
        ripgrep \
        procps \
        supervisor \
        bash \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# Node.js v24.14.0（官方二进制，解压到 /opt/node）
# 国内网络走 npmmirror 的 node 镜像，避免 nodejs.org 直连慢
# ============================================================
ENV NODE_VERSION=24.14.0
RUN curl -fsSL --connect-timeout 15 --max-time 600 \
        "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /opt \
    && ln -s "/opt/node-v${NODE_VERSION}-linux-x64" /opt/node
ENV PATH=/opt/node/bin:${PATH}

# ============================================================
# npm 国内镜像固化（对所有用户生效）：
# 前端企业控制台配置 stdio MCP 时可直接用 `command=npx` 形式，
# 容器内 npx 下载走 npmmirror 镜像（秒级），配合 npx 缓存卷
# （/home/staffdeck/.npm）持久化，recreate 后缓存仍命中。
# uv 同理：uvx 形式走清华 PyPI 镜像，缓存卷 /home/staffdeck/.cache/uv。
# ============================================================
RUN echo "registry=https://registry.npmmirror.com" > /etc/npmrc
ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

# ============================================================
# Codex CLI（OpenAI 官方 CLI，npm 全局安装，走 npmmirror 国内源加速）
# 安装后 codex 二进制位于 /opt/node/bin/codex（已在 PATH 中），
# lark-channel-bridge 的 codex runner 通过 PATH 探测到它即可工作。
# ============================================================
RUN npm install -g @openai/codex --registry=https://registry.npmmirror.com

# ============================================================
# cc-switch-web（预编译 Rust 二进制，Web 管理界面）
# 二进制已固化进仓库（docker/vendor/cc-switch-server），构建不依赖 GitHub 下载。
# 来源：https://github.com/Laliet/cc-switch-web/releases/latest/download/
#       cc-switch-server-linux-x86_64（v0.542.0+）
# 实测其二进制内置 Codex 支持：OAuth 设备登录、自定义 API 端点、
# 写入 ~/.codex/config.toml / ~/.codex/auth.json。
# 更新方法：从旧镜像 docker cp /opt/cc-switch/server 覆盖本文件。
# ============================================================
COPY docker/vendor/cc-switch-server /opt/cc-switch/server
RUN chmod +x /opt/cc-switch/server

# ============================================================
# lark-coding-agent-bridge（npm 全局安装，需 Node >= 20.12，满足）
# bridge 原生支持 codex：首次绑定时用 --agent codex 创建 codex profile
# 国内网络走 npmmirror 镜像，避免官方 registry 直连超时
# ============================================================
RUN npm install -g lark-channel-bridge --registry=https://registry.npmmirror.com

# ============================================================
# 飞书 CLI（lark-cli，larksuite 官方 CLI）
# bridge 的 PersonalAgent 授权流程依赖 lark-cli 进行 OAuth 登录。
# 通过 npx 安装到全局，生成 lark-cli 命令。
# ============================================================
RUN npx --yes --registry=https://registry.npmmirror.com @larksuite/cli@latest install

# ============================================================
# 非 root 用户运行服务（双用户分离：StaffDeck 与 codex 分属不同 uid）
#   appuser    - codex / cc-switch / lark-bridge：其 shell 执行不可信，
#                必须与后端数据隔离
#   staffdeck  - StaffDeck 后端：持有数据库 / .env / 源码，codex 不可见
#   staffwork  - 共享组：仅用于 workspace 协作写（员工会话目录两端都要写）
# 后端经免密 sudo 以 appuser 身份 spawn codex（见下方 sudoers 与 codex-run.sh）。
# ============================================================
RUN useradd -m -s /bin/bash appuser \
    && useradd -m -s /bin/bash staffdeck \
    && groupadd staffwork \
    && usermod -aG staffwork appuser \
    && usermod -aG staffwork staffdeck \
    && mkdir -p /home/appuser/.cc-switch /home/appuser/.lark-channel /home/appuser/.codex /home/appuser/workspace \
    && mkdir -p /home/staffdeck/.npm /home/staffdeck/.cache/uv \
    && chown -R appuser:appuser /home/appuser \
    && chown -R staffdeck:staffdeck /home/staffdeck \
    && chmod 751 /home/appuser
# Codex 工作目录设为 workspace（宿主机挂载，代码任务直接落到这里）
WORKDIR /home/appuser/workspace

# ============================================================
# sudo（免密）：staffdeck 后端仅可切换到 appuser 执行 codex CLI。
# 这是双用户分离下后端 spawn codex 子进程的通道；规则收敛到单一命令，
# 不开放其他 sudo 能力。sudo -H 由 codex-run.sh 传入，保证 HOME=/home/appuser。
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends sudo \
    && echo "staffdeck ALL=(appuser) NOPASSWD: /opt/node/bin/codex" > /etc/sudoers.d/staffdeck-codex \
    && echo 'Defaults:staffdeck secure_path="/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' >> /etc/sudoers.d/staffdeck-codex \
    && chmod 440 /etc/sudoers.d/staffdeck-codex \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# codex 用户级配置只读加固（机制 2：文件系统权限）
# config.toml（用户级 MCP 等）与 skills 目录改为 root 属主只读，
# 会话中的 codex（以 appuser 运行）无法修改用户级 MCP/skill 配置，
# 只能在工作空间创建项目级配置（.codex/config.toml 与 .codex/skills/）。
# config.toml 本体不再 touch 空文件：由 entrypoint 首次启动时从
# /opt/codex-templates/ 模板幂等初始化（不存在才复制，不覆盖已有配置）。
# ============================================================
RUN mkdir -p /home/appuser/.codex/skills \
    && chown root:root /home/appuser/.codex/skills \
    && chmod 555 /home/appuser/.codex/skills

# ============================================================
# codex 配置模板（固化进镜像，entrypoint 幂等初始化到 ~/.codex volume）
# config.toml      - deepseek provider + model_catalog_json 引用
# model-catalog.json - deepseek-v4-pro / deepseek-v4-flash 模型元数据
#   （内置 catalog 只含 gpt-5.6 系列：自定义条目修复自动评审模型名 fallback
#   与 MCP 工具注入，字段组合见文件内注释）
# ============================================================
COPY docker/config.toml /opt/codex-templates/config.toml
COPY docker/model-catalog.json /opt/codex-templates/model-catalog.json

# ============================================================
# StaffDeck 后端（与 codex 同容器部署）
# 依赖层单独前置（pyproject.toml + uv.lock 不变则整层命中缓存，
# 依赖不变时重建秒级；改业务代码完全不重建镜像，见 dev overlay）。
# venv 独立放 /opt/staffdeck/venv（不放 backend/.venv）：
# 开发 overlay 挂载 ../backend 覆盖源码时不会遮蔽已装依赖。
# --no-install-project：不把 project 本身安装进 venv，运行直接使用源码目录。
# ============================================================
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

COPY backend/pyproject.toml backend/uv.lock /opt/staffdeck/backend/
# 用 VIRTUAL_ENV + --active 显式指定目标 venv（实测 --python 指向 venv 解释器
# 时 uv 会把依赖装进 project 下的 .venv，而非指定 venv）。
RUN uv venv --python python3 /opt/staffdeck/venv \
    && cd /opt/staffdeck/backend \
    && VIRTUAL_ENV=/opt/staffdeck/venv \
       UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync \
       --frozen --no-dev --no-install-project --active \
    && rm -rf /opt/staffdeck/backend/.venv

# 通用技能运行时 venv：构建期预装默认包（requests,httpx）。
# 运行时 GENERAL_SKILL_RUNTIME_AUTO_INSTALL=false，容器运行期间不联网装包。
RUN /opt/staffdeck/venv/bin/python -m venv /opt/staffdeck/runtime-venv \
    && /opt/staffdeck/runtime-venv/bin/pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple requests httpx

# StaffDeck 源码与前端构建产物（dist 已在宿主机用 vite build 构建好）
# 与仓库一致的相对布局，single_port_app.py 的路径解析直接可用
# （dev 模式：resource_dir()==backend/，dist 取 ../frontend-enterprise/dist）。
COPY backend/ /opt/staffdeck/backend/
COPY frontend-enterprise/dist/ /opt/staffdeck/frontend-enterprise/dist/
# 双用户分离：backend 源码 / 数据卷 / venv 归 staffdeck 私有（750/700），
# codex 的 shell（appuser）完全不可见；connector-locks 是后端运行时锁目录。
# runtime-venv 保持 root 只读执行（通用技能 runner 由后端调用，无需写 venv）。
RUN mkdir -p /opt/staffdeck/staffdeck-data /opt/staffdeck/backend/connector-locks \
    && chown -R staffdeck:staffdeck /opt/staffdeck/backend /opt/staffdeck/staffdeck-data /opt/staffdeck/venv \
    && chmod 750 /opt/staffdeck/backend

# ============================================================
# mysql stdio MCP 工具包（预装进镜像，避免运行时 npx 下载超时）
# 后端 spawn `node <入口>` 执行；数据库配置的 command 指向：
#   /opt/staffdeck/tools-vendor/node_modules/@benborla29/mcp-server-mysql/dist/index.js
# ============================================================
RUN mkdir -p /opt/staffdeck/tools-vendor \
    && cd /opt/staffdeck/tools-vendor \
    && npm install --registry=https://registry.npmmirror.com @benborla29/mcp-server-mysql@2.0.2 \
    && chown -R staffdeck:staffdeck /opt/staffdeck/tools-vendor

# ============================================================
# supervisord 配置与入口脚本
# ============================================================
COPY docker/supervisord.conf /etc/supervisor/conf.d/app.conf
COPY docker/entrypoint-codex.sh /entrypoint.sh
COPY docker/bridge-start.sh /usr/local/bin/bridge-start.sh
COPY docker/staffdeck-start.sh /usr/local/bin/staffdeck-start.sh
COPY docker/codex-run.sh /usr/local/bin/codex-run
# Windows 上 Docker 构建时 COPY 的文本文件可能被转成 CRLF 行尾，
# 导致 shebang 失效报 "no such file or directory"，这里统一转回 LF。
RUN sed -i 's/\r$//' /entrypoint.sh /usr/local/bin/bridge-start.sh /usr/local/bin/staffdeck-start.sh /usr/local/bin/codex-run \
    && chmod +x /entrypoint.sh /usr/local/bin/bridge-start.sh /usr/local/bin/staffdeck-start.sh /usr/local/bin/codex-run

# cc-switch-web 暴露端口（bridge 通过飞书 WebSocket 出站，不需入站端口）
EXPOSE 3000
# StaffDeck 单端口（API + 前端静态资源）
EXPOSE 5173

# 持久化卷：供应商配置 / 飞书绑定 / Codex 凭证与配置
# 工作目录 /home/appuser/workspace 由宿主机 bind mount 挂载（见 README）
# Codex 的登录态（auth.json）、配置（config.toml）、会话日志均在 ~/.codex/，
# 该目录是 volume，容器删除/重建后不丢失。
VOLUME ["/home/appuser/.cc-switch", "/home/appuser/.lark-channel", "/home/appuser/.codex"]

# cc-switch-web 监听地址与端口
ENV HOST=0.0.0.0
ENV PORT=3000
# 容器内 HTTP 监听 0.0.0.0 需显式允许 HTTP 上的 Basic Auth
# （安全由宿主机端口映射/反向代理保证）
ENV ALLOW_HTTP_BASIC_OVER_HTTP=1

# entrypoint 以 root 运行（需 chown volume 挂载点），supervisord 内各 program 降权到 appuser
ENTRYPOINT ["/entrypoint.sh"]
