#!/bin/bash
# StaffDeck 后端启动包装脚本
# STAFFDECK_RELOAD=1 时启用 uvicorn 热重载（开发 overlay 用，见 docker-compose.dev.yml），
# 否则纯前台运行（生产）。监听 0.0.0.0:5173 以便宿主机端口映射访问。
# umask 002：workspace 共享（staffwork 组）下新建的会话目录组可写，
# 让 codex(appuser) 能在后端(staffdeck)创建的会话目录里工作。
set -e
umask 002

ARGS=(--host 0.0.0.0 --port 5173)
if [ "${STAFFDECK_RELOAD:-0}" = "1" ]; then
    echo "[staffdeck] 开发模式：uvicorn --reload（热重载已启用，依赖 WATCHFILES_FORCE_POLLING）"
    ARGS+=(--reload)
fi

exec /opt/staffdeck/venv/bin/python -m uvicorn single_port_app:app "${ARGS[@]}"
