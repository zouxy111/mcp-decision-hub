# MCP 决策中台部署状态报告

**生成时间**: 2026-10-03  
**服务器**: 170.106.192.161 (硅谷 - 腾讯云 2核4G)

---

## ✅ 部署状态：运行中

### 应用信息
- **访问地址**: https://hub.tdp-demo.work
- **内部端口**: 18000
- **运行时长**: 13天 2小时 43分钟
- **进程ID**: 621086
- **Python版本**: 3.12.3
- **启动命令**: `/home/ubuntu/mcp-decision-hub/.venv/bin/python .venv/bin/uvicorn hub.main:app --host 127.0.0.1 --port 18000`

### 技术栈版本
- **FastAPI**: 0.141.1
- **Uvicorn**: 0.52.4
- **SQLAlchemy**: 2.0.52
- **Pydantic**: 2.13.5
- **LangGraph**: 未检测到（可能未安装或被移除）

### 代码版本
```
* main fca4dda [ahead 1] feat(web): 顶栏 Skills 后加 wiki-knowledgebase-share-kit 入口
```

最近4次提交：
1. `fca4dda` - feat(web): 顶栏 Skills 后加 wiki-knowledgebase-share-kit 入口
2. `3c39ce5` - fix(web): skills 分发点补入口 —— 顶栏 Skills + 静态挂载 html=True
3. `d77f669` - feat(web,llm): 前端「素印 → 朱印」重做 + 运行时模型配置（迁移 v11）+ skills 分发点
4. `d03bf1f` - fix(rate-limit): REST 通道补 PRD 9.1 配额（token/account/submit 三层）

### 环境配置
```
DATABASE_URL=sqlite:///./hub.db
ADMIN_USERNAME=admin
LLM_PROVIDER_NAME=DeepSeek
LLM_MODEL=deepseek-flash
TASK_TIMEOUT_SECONDS=259200 (3天)
MAX_ROUNDS=6
```

### 反向代理 (Caddy)
- **监听端口**: 80 (HTTP) + 443 (HTTPS)
- **SSL证书**: 自动管理 (Let's Encrypt)
- **Caddy运行时长**: 2个月10天（自 2026-07-23 启动）
- **配置**:
  ```
  hub.tdp-demo.work {
    encode gzip
    reverse_proxy 127.0.0.1:18000
  }
  ```

### 系统资源
- **磁盘使用**: 47G / 59G (82%) ⚠️
- **内存使用**: 2.9G / 3.6G (80%) ⚠️
- **Swap使用**: 1.5G / 3.9G (38%)
- **CPU**: 2核
- **带宽**: 30M

---

## ⚠️ 发现的问题

### 1. 未配置 systemd 服务（🔴 高优先级）
**风险**: 进程可能因意外退出或服务器重启而停止，需要手动重启。

**现状**: 应用通过某种方式启动并已运行13天，但：
- ❌ 没有 systemd 服务配置
- ❌ 找不到启动脚本 (start.sh)
- ❌ 没有 screen/tmux 会话
- ❌ 没有 nohup.out 日志

**影响**: 
- 服务器重启后应用不会自动启动
- 进程崩溃后不会自动重启
- 缺少标准化的日志管理
- 运维困难

**建议**: 立即配置 systemd 服务（见下方解决方案）

### 2. LangGraph 缺失（🟡 中优先级）
**现状**: README 和技术栈中提到使用 LangGraph，但运行环境中未检测到该包。

**可能原因**:
1. 该功能已被移除或替换
2. 依赖管理出现问题
3. 使用了其他编排方案

**建议**: 确认是否需要 LangGraph，如需要则重新安装。

### 3. uv 未安装（🟡 中优先级）
**现状**: 项目 README 要求使用 `uv` 管理依赖，但服务器上未安装。

**影响**: 无法使用标准流程更新依赖或重新部署。

**建议**: 安装 uv 以保持开发和生产环境一致。

### 4. 磁盘空间偏紧（🟡 中优先级）
**现状**: 磁盘已使用 82%

**建议**: 
- 清理旧日志、备份文件
- 设置自动清理策略
- 考虑扩容

### 5. 内存使用较高（🟡 中优先级）
**现状**: 内存使用 80%，Swap 已启用

**建议**: 
- 监控内存使用趋势
- 优化应用内存占用
- 必要时升级配置到 4核8G

### 6. Git 仓库有未推送提交（🟢 低优先级）
**现状**: 本地有 1 个未推送的提交

**建议**: 推送到远程仓库以避免代码丢失。

---

## 📋 推荐的改进措施

### 优先级 1: 配置 systemd 服务 ✨

创建服务配置文件：

```bash
sudo tee /etc/systemd/system/mcp-decision-hub.service << 'EOF'
[Unit]
Description=MCP Decision Hub - 决策中台
After=network.target
Documentation=https://github.com/your-org/mcp-decision-hub

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/mcp-decision-hub
Environment="PATH=/home/ubuntu/mcp-decision-hub/.venv/bin:/usr/local/bin:/usr/bin:/bin"

# 启动命令
ExecStart=/home/ubuntu/mcp-decision-hub/.venv/bin/uvicorn hub.main:app --host 127.0.0.1 --port 18000

# 自动重启策略
Restart=always
RestartSec=10
StartLimitInterval=200
StartLimitBurst=5

# 资源限制
LimitNOFILE=65535
MemoryLimit=1.5G

# 日志配置
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mcp-decision-hub

[Install]
WantedBy=multi-user.target
EOF
```

停止当前进程并启用服务：

```bash
# 停止当前进程（记录PID）
sudo kill 621086

# 重载配置
sudo systemctl daemon-reload

# 启用并启动服务
sudo systemctl enable mcp-decision-hub
sudo systemctl start mcp-decision-hub

# 检查状态
sudo systemctl status mcp-decision-hub

# 查看日志
sudo journalctl -u mcp-decision-hub -f
```

### 优先级 2: 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version
```

### 优先级 3: 配置日志轮转

```bash
sudo tee /etc/logrotate.d/mcp-decision-hub << 'EOF'
/var/log/mcp-decision-hub/*.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    missingok
    create 0640 ubuntu ubuntu
    sharedscripts
    postrotate
        systemctl reload mcp-decision-hub > /dev/null 2>&1 || true
    endscript
}
EOF
```

### 优先级 4: 清理磁盘空间

```bash
# 清理旧备份
cd ~/
rm -f hub-backup-*.db hub-db-backup-*.db hub-*-backup-*.tar.gz

# 清理 APT 缓存
sudo apt clean
sudo apt autoremove -y

# 清理 journald 日志（保留最近7天）
sudo journalctl --vacuum-time=7d

# 检查大文件
du -sh ~/* | sort -h | tail -10
```

### 优先级 5: 设置监控告警

使用简单的 cron 任务监控：

```bash
# 创建监控脚本
cat > ~/monitor.sh << 'EOF'
#!/bin/bash
DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
MEM_USAGE=$(free | grep Mem | awk '{print int($3/$2 * 100)}')

if [ $DISK_USAGE -gt 85 ]; then
    echo "⚠️ 磁盘使用率: ${DISK_USAGE}%"
fi

if [ $MEM_USAGE -gt 90 ]; then
    echo "⚠️ 内存使用率: ${MEM_USAGE}%"
fi

# 检查服务状态
if ! systemctl is-active --quiet mcp-decision-hub; then
    echo "🔴 mcp-decision-hub 服务未运行"
fi
EOF

chmod +x ~/monitor.sh

# 添加到 crontab（每小时执行）
(crontab -l 2>/dev/null; echo "0 * * * * ~/monitor.sh") | crontab -
```

### 优先级 6: 数据库备份

```bash
# 创建备份脚本
cat > ~/backup_db.sh << 'EOF'
#!/bin/bash
BACKUP_DIR=~/backups
mkdir -p $BACKUP_DIR
DATE=$(date +%Y%m%d_%H%M%S)
cp ~/mcp-decision-hub/hub.db $BACKUP_DIR/hub_$DATE.db
# 保留最近30天的备份
find $BACKUP_DIR -name "hub_*.db" -mtime +30 -delete
EOF

chmod +x ~/backup_db.sh

# 添加到 crontab（每天凌晨3点备份）
(crontab -l 2>/dev/null; echo "0 3 * * * ~/backup_db.sh") | crontab -
```

---

## ✨ 访问测试结果

- ✅ 外部 HTTPS 访问正常 (https://hub.tdp-demo.work)
- ✅ 登录页面加载成功
- ✅ Caddy SSL 证书有效
- ✅ 响应时间正常 (~1.5s)
- ✅ HTTP/303 重定向正常工作

---

## 📊 其他服务

服务器上还运行着其他服务（都通过 Caddy 反向代理）：

| 域名 | 内部端口 | 说明 |
|------|---------|------|
| tdp-demo.work, app.tdp-demo.work | 3000 | 主应用 |
| api.tdp-demo.work | 4000 | API服务 |
| id.tdp-demo.work | 8080 | 身份认证服务 |
| mcp.tdp-demo.work | 3102 | MCP服务 |
| minio.tdp-demo.work | 9000 | MinIO对象存储 |
| wiki.tdp-demo.work | 静态文件 | Wiki介绍页 |

---

## 📝 总结

### 当前状态
- ✅ 应用正常运行13天，无重大问题
- ✅ 外部访问通畅，SSL证书有效
- ⚠️ 缺少生产环境必需的服务管理和监控
- ⚠️ 系统资源使用率较高需要关注

### 建议行动
1. **立即执行**: 配置 systemd 服务（10分钟）
2. **本周完成**: 安装 uv、配置日志轮转、清理磁盘（30分钟）
3. **持续优化**: 监控资源使用、定期备份、代码推送

---

**报告生成者**: Claude (MCP Decision Hub 部署检查)  
**联系方式**: 如有问题请联系运维团队
