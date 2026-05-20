# 配置 systemd 自启动

在 WSL 终端里依次执行以下命令：

## 第一步：创建应用服务

```bash
sudo tee /etc/systemd/system/ai-news.service > /dev/null << 'EOF'
[Unit]
Description=AI News Monitor
After=network.target

[Service]
Type=simple
User=ziyun-pc
WorkingDirectory=/mnt/c/Users/liziy/Code/AI-News
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10
StandardOutput=append:/mnt/c/Users/liziy/Code/AI-News/logs/app.log
StandardError=append:/mnt/c/Users/liziy/Code/AI-News/logs/app.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

## 第二步：创建隧道服务

```bash
sudo tee /etc/systemd/system/ai-news-tunnel.service > /dev/null << 'EOF'
[Unit]
Description=Cloudflare Tunnel for AI News
After=network.target ai-news.service

[Service]
Type=simple
User=ziyun-pc
ExecStart=/usr/local/bin/cloudflared tunnel run ai-news
Restart=always
RestartSec=10
StandardOutput=append:/mnt/c/Users/liziy/Code/AI-News/logs/tunnel.log
StandardError=append:/mnt/c/Users/liziy/Code/AI-News/logs/tunnel.log

[Install]
WantedBy=multi-user.target
EOF
```

## 第三步：启用并启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-news ai-news-tunnel
sudo systemctl start ai-news ai-news-tunnel
```

## 第四步：验证状态

```bash
sudo systemctl status ai-news ai-news-tunnel --no-pager
```

---

## 日常运维

```bash
# 查看状态
sudo systemctl status ai-news
sudo systemctl status ai-news-tunnel

# 查看日志
tail -f /mnt/c/Users/liziy/Code/AI-News/logs/app.log
tail -f /mnt/c/Users/liziy/Code/AI-News/logs/tunnel.log

# 重启服务
sudo systemctl restart ai-news
sudo systemctl restart ai-news-tunnel

# 停止服务
sudo systemctl stop ai-news ai-news-tunnel
```

---

完成后访问（主域）：**https://yunflow.net**
旧域名仍可用：https://news.ziyunli.net
