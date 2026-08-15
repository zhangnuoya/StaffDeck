#!/bin/bash
# StaffDeck 后端(staffdeck)以 appuser 身份执行 codex CLI 的包装器。
#
# 单容器双用户分离的通道：后端进程与 codex 的 shell 隔离——
# 后端文件（源码/.env/数据库）对 codex 不可见，codex home（登录态）对后端不可见。
# 免密 sudo 规则见 Dockerfile 的 /etc/sudoers.d/staffdeck-codex（仅收敛到本命令）。
# -H：sudo 将 HOME 设为 appuser 的 home（/home/appuser），
#     保证 codex 读取 /home/appuser/.codex 的登录态与配置。
exec sudo -H -u appuser /opt/node/bin/codex "$@"
