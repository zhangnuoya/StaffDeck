#!/bin/bash
# 容器入口：以 supervisord 作为前台进程（PID 1）
# supervisord 以 root 运行，各 program 按配置降权到 appuser
# 这样能正确处理 volume 挂载点的权限（root 可 chown）
# 注意：本脚本为 codex-bridge 镜像专用（不含 Claude Code 专属逻辑）
set -e

# volume 首次挂载时 owner 可能是 root，确保 appuser 可写
# workspace 是 bind mount（宿主机目录），双用户分离下需要共享组协作写：
#   staffwork 组 + 2770 setgid，后端(staffdeck)创建的会话目录自动继承组，
#   codex(appuser) 才可在其中工作；反之 codex 的产物后端亦可读。
mkdir -p /home/appuser/.cc-switch /home/appuser/.lark-channel /home/appuser/.codex /home/appuser/workspace
chown -R appuser:appuser /home/appuser
# home 目录 751：staffdeck 需穿过 home 到达 workspace（有 x 即可），
# 但不能列读 appuser 的私有文件（~/.codex 登录态等）。
# 组改为 staffwork：产物下载的加固式逐级下钻（open_harness_artifact 从根
# 目录 O_NOFOLLOW|O_RDONLY 打开每一级）要求途经目录对后端用户可读，
# 仅 others 的 x 位不够；.codex/.lark-channel 等自身仍 700 appuser，内容不可读。
chmod 751 /home/appuser 2>/dev/null || true
chgrp staffwork /home/appuser 2>/dev/null || true
chgrp -R staffwork /home/appuser/workspace 2>/dev/null || true
chmod 2770 /home/appuser/workspace 2>/dev/null || true
# workspace 内所有子目录统一 2770（setgid + 组写）：覆盖旧目录/不同 umask 创建的目录，
# 保证后端(staffdeck)与 codex(appuser)在任意会话目录上都能协作写。
find /home/appuser/workspace -mindepth 1 -type d -exec chmod 2770 {} + 2>/dev/null || true

# StaffDeck 数据目录（volume 挂载点）与 backend 运行时目录权限
# staffdeck-data：数据库/日志/用户数据，recreate 容器不丢（不要 down -v）。
# 双用户分离：backend / staffdeck-data 归 staffdeck 私有，codex(appuser) 不可见。
# dev overlay 挂载时挂载目录属主可能是宿主 uid，chown 失败忽略，靠宿主机权限保证可写。
mkdir -p /opt/staffdeck/staffdeck-data/logs /opt/staffdeck/backend/connector-locks
chown -R staffdeck:staffdeck /opt/staffdeck/backend /opt/staffdeck/staffdeck-data 2>/dev/null || true
chmod 750 /opt/staffdeck/backend 2>/dev/null || true
# staffdeck-data 收紧到 700：数据库/.env 仅后端可见（双用户分离的关键验证点）
chmod 700 /opt/staffdeck/staffdeck-data 2>/dev/null || true
# npx / uv 缓存卷（staffdeck 的 npm 与 uv 缓存）：前端配置 npx/uvx 形式 stdio MCP
# 时下载的包缓存在此，容器 recreate 后仍命中；首次挂载 owner 为 root 时修正。
mkdir -p /home/staffdeck/.npm /home/staffdeck/.cache/uv
chown -R staffdeck:staffdeck /home/staffdeck/.npm /home/staffdeck/.cache/uv 2>/dev/null || true

# codex 配置模板初始化（幂等）：~/.codex 是 volume，
# 文件不存在才从镜像模板复制；已存在的（含登录态/用户自定义）不覆盖。
if [ ! -f /home/appuser/.codex/config.toml ]; then
    cp /opt/codex-templates/config.toml /home/appuser/.codex/config.toml
    echo "[entrypoint] 已初始化 codex config.toml（镜像模板）"
fi
if [ ! -f /home/appuser/.codex/model-catalog.json ]; then
    cp /opt/codex-templates/model-catalog.json /home/appuser/.codex/model-catalog.json
    echo "[entrypoint] 已初始化 codex model-catalog.json（镜像模板）"
fi

# 升级兼容（幂等）：旧 volume 里的 config.toml 可能缺少 model_catalog_json 引用，
# 缺失时在顶层（首个 [表] 之前）补一行；否则自定义模型 catalog 不生效，
# 自动评审模型名会 fallback 到 gpt-5.6 系列导致 deepseek 拒绝、MCP 工具不可见。
if [ -f /home/appuser/.codex/config.toml ] && \
   ! grep -q '^model_catalog_json' /home/appuser/.codex/config.toml; then
    tmp=$(mktemp)
    awk '/^\[/ { if (!done) { print "model_catalog_json = \"model-catalog.json\""; done=1 } } { print }' \
        /home/appuser/.codex/config.toml > "$tmp"
    if ! grep -q '^model_catalog_json' "$tmp"; then
        printf '\nmodel_catalog_json = "model-catalog.json"\n' >> "$tmp"
    fi
    mv "$tmp" /home/appuser/.codex/config.toml
    echo "[entrypoint] 已为旧 config.toml 注入 model_catalog_json"
fi

# codex 用户级配置只读加固（机制 2：文件系统权限，幂等）
# 上面 chown -R 会把 config.toml/catalog/skills 的属主改成 appuser，
# 这里在 chown 之后重设回 root 只读，保证会话中的 codex 无法修改
# 用户级 MCP（config.toml）与用户级 skills（skills/），只能在工作空间建项目级配置。
mkdir -p /home/appuser/.codex/skills
chown root:root /home/appuser/.codex/config.toml /home/appuser/.codex/model-catalog.json /home/appuser/.codex/skills 2>/dev/null || true
chmod 444 /home/appuser/.codex/config.toml /home/appuser/.codex/model-catalog.json 2>/dev/null || true
chmod 555 /home/appuser/.codex/skills 2>/dev/null || true

# DNS 与 host.docker.internal 固化（每次启动幂等执行，容器 recreate 后自动恢复）
# 1) /etc/resolv.conf：公共 DNS 优先 + 原 nameserver 兜底。
#    Docker 内嵌 DNS 转发外部域名可能超时（api.deepseek.com 曾实测 10s+），
#    公共 DNS 优先可消除；原 nameserver 保留为兜底。
# 2) /etc/hosts：探测容器默认网关写死 host.docker.internal（host-gateway 语义），
#    容器内 codex 通过该域名访问宿主 StaffDeck MCP gateway。
#    运行参数已注入（如 --add-host=host.docker.internal:host-gateway）则跳过探测。
# 环境变量：CODEX_DNS（逗号分隔，默认 223.5.5.5,119.29.29.29）、
#           CODEX_HOST_GATEWAY_FIX=0 可关闭 hosts 探测。
DNS_SERVERS="${CODEX_DNS:-223.5.5.5,119.29.29.29}"
if [ -f /etc/resolv.conf ]; then
    new_resolv=$(mktemp)
    for ns in $(echo "$DNS_SERVERS" | tr ',' ' '); do
        echo "nameserver $ns" >> "$new_resolv"
    done
    for ns in $(sed -n 's/^nameserver[[:space:]]*//p' /etc/resolv.conf); do
        case " $(echo "$DNS_SERVERS" | tr ',' ' ') " in
            *" $ns "*) ;;
            *) echo "nameserver $ns" >> "$new_resolv" ;;
        esac
    done
    cat "$new_resolv" > /etc/resolv.conf && rm -f "$new_resolv"
    echo "[entrypoint] resolv.conf 已固化：公共 DNS $DNS_SERVERS + 原 nameserver 兜底"
fi

if [ "${CODEX_HOST_GATEWAY_FIX:-1}" = "1" ] && ! grep -q 'host\.docker\.internal' /etc/hosts; then
    # 探测默认路由网关（host-gateway 语义，Linux 原生 docker 下即宿主）。
    # 注意：Docker Desktop 的容器默认网关是 VM 内网桥，并非宿主，直接写入会误指；
    # 因此写入前对网关做 TCP 连通性探测（宿主 StaffDeck 端口），不通则不写。
    gateway=""
    if command -v ip >/dev/null 2>&1; then
        gateway=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
    fi
    if [ -z "$gateway" ] && [ -r /proc/net/route ]; then
        gateway=$(python3 -c "
import socket, struct
try:
    with open('/proc/net/route') as f:
        f.readline()
        for line in f:
            parts = line.split()
            if parts[1] == '00000000':
                raw = bytes.fromhex(parts[2])  # /proc/net/route 小端字节序显示
                print(socket.inet_ntoa(raw[::-1]))
                break
except Exception:
    pass
")
    fi
    if [ -n "$gateway" ]; then
        probe_port="${CODEX_HOST_GATEWAY_PORT:-5173}"
        reachable=$(python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('$gateway', int('$probe_port')))
    print('yes')
except Exception:
    print('no')
finally:
    s.close()
")
        if [ "$reachable" = "yes" ]; then
            echo "$gateway host.docker.internal" >> /etc/hosts
            echo "[entrypoint] 已写入 /etc/hosts：$gateway host.docker.internal"
        else
            echo "[entrypoint] 警告：网关 $gateway 的 $probe_port 端口不可达，未写入 hosts" >&2
            echo "[entrypoint] 请通过 --add-host=host.docker.internal:host-gateway 注入" >&2
        fi
    else
        echo "[entrypoint] 警告：未探测到默认网关，host.docker.internal 请通过 --add-host 注入" >&2
    fi
fi

# cc-switch-web 密码预设：首次启动时写入默认密码 admin123
# （文件已存在则不覆盖，保留用户自定义的密码）
if [ ! -f /home/appuser/.cc-switch/web_password ]; then
    echo -n "admin123" > /home/appuser/.cc-switch/web_password
    chown appuser:appuser /home/appuser/.cc-switch/web_password
    chmod 600 /home/appuser/.cc-switch/web_password
fi

# Codex 的登录态（~/.codex/auth.json）与配置（~/.codex/config.toml）
# 直接落在 ~/.codex/ volume 内，无需像 Claude 版那样做软链接迁移。

# bridge workspace 路径自动修复
# bridge 绑定飞书时默认把 workspace 设为 ~/.lark-channel-workspaces/codex/default（不存在），
# 但实际工作目录应为宿主机 bind mount 的 /home/appuser/workspace。
# 每次启动时把 config.json 里所有 profile 的 default workspace 强制改成正确路径。
LARK_CONFIG="/home/appuser/.lark-channel/config.json"
if [ -f "$LARK_CONFIG" ]; then
    python3 -c "
import json, sys
p = '$LARK_CONFIG'
try:
    with open(p) as f:
        cfg = json.load(f)
    changed = False
    target = '/home/appuser/workspace'
    for pname, prof in cfg.get('profiles', {}).items():
        ws = prof.get('workspaces', {})
        if ws.get('default') != target:
            ws['default'] = target
            prof['workspaces'] = ws
            changed = True
    if changed:
        with open(p, 'w') as f:
            json.dump(cfg, f, indent=2)
        print('[entrypoint] bridge workspace 路径已修正为', target)
except Exception as e:
    print(f'[entrypoint] 跳过 bridge workspace 修复: {e}', file=sys.stderr)
" 2>&1 || true
fi

# 以非 daemon 模块运行 supervisord，作为容器前台进程
exec supervisord -n -c /etc/supervisor/supervisord.conf
