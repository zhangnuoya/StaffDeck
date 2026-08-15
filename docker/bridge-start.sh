#!/bin/bash
# lark-bridge wrapper：避免未配置时反复崩溃重启
# 首次运行前 ~/.lark-channel/config.json 不存在，bridge 会立即退出。
# 此时让进程保持存活（sleep 循环），等待用户 docker exec 完成首次绑定后
# 手动 supervisorctl restart lark-bridge 即可正常常驻。
set -e

# 切换到 Claude 工作区（宿主机 bind mount 目录），
# 飞书对话中 Claude 的文件操作会直接落在宿主机 workspace
cd /home/appuser/workspace

CONFIG_FILE="$HOME/.lark-channel/config.json"
REGISTRY_DIR="$HOME/.lark-channel/registry"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[lark-bridge-wrapper] 未检测到配置文件 $CONFIG_FILE"
    echo "[lark-bridge-wrapper] 请先执行首次初始化："
    echo "[lark-bridge-wrapper]   docker exec -it --user appuser <容器> lark-channel-bridge run"
    echo "[lark-bridge-wrapper] 初始化完成后将自动启动。当前保持存活等待..."
    # 保持进程存活，supervisor 不会判为崩溃
    while [ ! -f "$CONFIG_FILE" ]; do
        sleep 5
    done
    echo "[lark-bridge-wrapper] 检测到配置文件，启动 bridge..."
fi

# bridge workspace 路径自动修复（绑定时机修正）
# 绑定向导生成 config.json 时会把 default workspace 写成 ~/.lark-channel-workspaces/claude/default，
# 但实际工作目录应为宿主机 bind mount 的 /home/appuser/workspace。
# entrypoint.sh 的同款修复只在容器启动那一刻跑，而 config.json 是绑定（晚于启动）才生成的，
# 所以必须在「config.json 出现之后、bridge 启动之前」再修正一次——这里正是那个时机。
# 每次 wrapper 启动都执行，幂等：已是正确路径则不改动。
if [ -f "$CONFIG_FILE" ]; then
    python3 -c "
import json, sys
p = '$CONFIG_FILE'
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
        print('[lark-bridge-wrapper] bridge workspace 路径已修正为', target)
except Exception as e:
    print(f'[lark-bridge-wrapper] 跳过 workspace 修复: {e}', file=sys.stderr)
" 2>&1 || true
fi

# 清理可能残留的进程锁文件
# 前台 bridge 被 Ctrl+C 退出时，registry 里的进程占用记录可能未被清理，
# 导致 supervisord 拉起的新实例报「已有 bridge 进程占用」。
# 锁文件结构：*.lock.lock（目录）+ *.lock.meta.json（文件），整体删除。
if [ -d "$REGISTRY_DIR" ]; then
    rm -rf "$REGISTRY_DIR"/locks/profile/*.lock.lock \
           "$REGISTRY_DIR"/locks/profile/*.lock.meta.json \
           "$REGISTRY_DIR"/locks/app/*.lock.lock \
           "$REGISTRY_DIR"/locks/app/*.lock.meta.json 2>/dev/null
    echo "[lark-bridge-wrapper] 已清理残留进程锁"
fi

exec lark-channel-bridge run
