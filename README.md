# BSC 自动回购分红系统

基于 BSC 链的自动回购销毁 + 持仓分红系统，支持 Flap.sh 平台代币。

## 功能

- **自动回购销毁**：通过 Flap Portal 购买代币并销毁到黑洞地址
- **自动分红**：按持仓分红给代币持有者
- **Web 界面**：实时显示持仓排行、分红记录、回购记录

## 分红规则

每轮执行完成后等待 5 分钟再执行下一轮：

```
可用金额 = 余额 - 0.002 BNB (gas预留)

├── 50% → 分红（先执行）
│         └── 前30名均分
└── 50% → 回购销毁（后执行）
          └── 通过 Flap Portal 购买代币 → 转入黑洞
```

- 单笔分红低于 0.001 BNB 不发送（节省 gas）
- 新配置导入后有 5 分钟初始化期

## 安装

### 1. 安装依赖

```bash
pip3 install flask flask-cors web3 requests
```

### 2. 配置

复制配置模板并填写：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
    "wallet_address": "0x你的钱包地址",
    "private_key": "你的私钥",
    "contract_address": "0x代币合约地址"
}
```

⚠️ **警告**：私钥请妥善保管，不要泄露！

### 3. 运行

```bash
python3 api_server.py
```

服务将在 `http://localhost:5000` 启动。

## 使用 Systemd 管理（可选）

创建服务文件 `/etc/systemd/system/bsc-api.service`：

```ini
[Unit]
Description=BSC Dividend API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/bsc_dividend_site
ExecStart=/usr/bin/python3 /root/bsc_dividend_site/api_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
systemctl daemon-reload
systemctl enable bsc-api
systemctl start bsc-api
```

## API 接口

| 接口 | 说明 |
|------|------|
| `GET /api/status` | 获取状态和倒计时 |
| `GET /api/holders` | 获取持仓排行 |
| `GET /api/records` | 获取分红/回购记录 |

## 文件说明

| 文件 | 说明 |
|------|------|
| `api_server.py` | 主程序 |
| `index.html` | Web 界面 |
| `config.json` | 配置文件（需自行创建） |
| `config.example.json` | 配置模板 |

## 注意事项

1. 确保钱包有足够的 BNB 支付 gas
2. 代币必须是 Flap.sh 平台创建的（使用 Flap Portal 回购）
3. 私钥仅存储在本地 config.json，不会上传

## 更新日志

### 2025-12-23 重大更新

**🆕 初始化模式：**
- 首次启动或配置变更时进入 5 分钟初始化期
- 初始化期间不执行分红/回购，只更新持仓缓存
- 前端显示黄色倒计时和 "🔧 初始化中" 状态
- 通过配置哈希检测钱包/合约/私钥变更

**🆕 实时进度显示：**
- 新增进度面板，实时显示分红/回购执行过程
- 进度条显示当前进度 (如 15/30)
- 实时滚动日志，成功绿色/失败红色
- 分红日志显示在分红卡片，回购日志显示在回购卡片

**🆕 计时逻辑优化：**
- 改为每轮完成后才开始 5 分钟倒计时（不再基于固定时间点）
- 确保上一轮完全执行完毕后再开始下一轮

**📝 执行顺序调整：**
- 先分红 → 再回购（之前是先回购再分红）
- 前30名均分分红，不再随机抽奖

**🔧 线程安全修复：**
- 添加线程锁保护全局状态变量
- 修复 nonce 双重发送问题（追踪已发送交易哈希）
- ThreadPoolExecutor 正确捕获超时异常

**🔧 其他优化：**
- 持仓缓存超过 10 分钟自动刷新
- 新增分红失败记录 (failed_dividends)
- 全部 print 改为 logging 模块
- fetch_records.py 从 config.json 读取地址
- 删除旧版 auto_dividend.py

---

### 2025-12-23 BUG 修复（早期）

**RPC 故障转移：**
- 支持 7 个 BSC RPC 节点自动切换
- 节点故障时自动尝试下一个节点
- 避免单一节点被限流导致服务中断

**持仓查询优化：**
- 并发查询余额（10线程），避免串行卡死
- 单次查询超时 5 秒，总超时 60 秒
- 限制最多查询 200 个地址

**分红功能：**
- 修复 `nonce too low` 错误：批量发送30笔分红时手动管理 nonce 递增
- 提高 gas price：从 1 gwei 改为 3→5→7 gwei 递增重试
- 增加重试间隔：从 3 秒改为 5→7→9 秒递增
- 增加详细错误日志：打印 nonce、gas price、目标地址

**回购功能：**
- 新增重试机制：购买和销毁都支持 3 次重试
- 动态 gas price：3→5→7 gwei 递增
- 增加详细异常日志

**前端优化：**
- 状态轮询间隔从 1 秒改为 2 秒，减少服务器压力

## License

MIT
