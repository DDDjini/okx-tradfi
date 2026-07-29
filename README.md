# OKX TradFi 股票代币 分型策略实时监控系统

**数据源**：Yahoo Finance 2 年+完整 K 线（Yahoo 映射股票 + ETF + 商品期货）  
**策略**：高低点分型 + PSY 心理线 + EMA7/34 双均线 + 同级别多空冲突抑制 + 1d ATR 异动过滤  
**止盈**：双止盈位（TP1 保守锁利 + TP2 激进目标）  
**推送**：飞书自定义机器人 Webhook（交互式卡片）  
**部署**：GitHub Actions 24x7 自动运行

> ⚠️ 本项目只做**监控 + 信号推送 + 回测**，不包含实盘自动下单。所有信号仅供参考，不构成投资建议。

---

## 监控交易对（17 个，Yahoo Finance 数据）

AAPL / MSFT / GOOGL / AMZN / TSLA / NVDA / INTC / MU / TSM / SPCX / SAMSUNG / SKHYNIX / XAU / CL / XAG / SNDK / OPENAI

## 回测结果（2 年完整数据，67 组）

| 周期 | 平均胜率 | 平均收益 | 盈利占比 |
|-----:|--------:|--------:|--------:|
| 4h | **65.3%** | **+19.3%** | 12/17 |
| 1h | **68.6%** | **+22.1%** | 16/17 |
| 1d | 56.1% | +23.3% | 11/16 |
| **全部** | **62.6%** | **+16.8%** | **81%** |

> 对比上一版本（OKX 1年数据）：胜率 46% → 63%，收益 +5% → +17%，盈利 50% → 81%

---

## 监控交易对

SNDK / MU / SPCX / OPENAI / SKHYNI / XAU / CL / XAG / INTC / TSLA / SAMSUNG / NVDA / GOOGL / AAPL / TSM / MSFT / AMZN（USDT 交易对）

程序启动时会**自动校验**哪些已在 OKX 上线，未上线的剔除并提示，不会崩溃。列表在 `config.yaml` 中可自由增删。

---

## 策略说明

### 分型定义

以 `left=2, right=2` 为例：

- **低点分型**：某根 K 线最低价是左右各 2 根（共 5 根）中**唯一的最低点** → 确认后做多
- **高点分型**：某根 K 线最高价是左右各 2 根中**唯一的最高点** → 确认后做空

### 严格时序（无未来函数）

```
分型出现：第 i 根
分型确认：第 i + right 根收盘
实际进场：第 i + right + 1 根开盘价
```

### 四周期参数

| 周期 | left | right | 盈亏比 RR | 止损缓冲 |
|------|-----:|------:|----------:|---------:|
| 30m  | 2    | 2     | 1.4       | 0.10%    |
| 1h   | 2    | 2     | 1.6       | 0.15%    |
| 4h   | 3    | 3     | 1.8       | 0.20%    |
| 1d   | 3    | 3     | 2.0       | 0.30%    |

- **做多**：止损 = 分型低点 × (1 − 缓冲)；止盈 = 入场 + RR × (入场 − 止损)
- **做空**：止损 = 分型高点 × (1 + 缓冲)；止盈 = 入场 − RR × (止损 − 入场)
- 手续费按双边 0.05% 计入回测（`config.yaml` 可改）

---

## 一、安装（在你的电脑上）

### 1. 环境要求

Python 3.10 或 3.11（Windows 在 [python.org](https://www.python.org/downloads/) 下载，安装时勾选 Add to PATH）。

### 2. 创建虚拟环境

Windows（PowerShell）：

```powershell
cd okx_tradfi_monitor
python -m venv .venv
.venv\Scripts\activate
```

Mac / Linux：

```bash
cd okx_tradfi_monitor
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

然后编辑 `.env`：

```ini
# 飞书机器人地址（下方有创建教程）
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的机器人地址
FEISHU_SECRET=

# 国内网络访问 OKX 需要代理，填你代理软件的本地端口
#  Clash 默认 7890；v2ray 默认 10809
PROXY_URL=http://127.0.0.1:7890
```

> **关于代理**：OKX API 在国内网络无法直连。程序支持 http / socks5 代理（socks5 写法：`PROXY_URL=socks5://127.0.0.1:1080`）。如果你的网络本身可直连（如海外服务器），把 `PROXY_URL` 留空即可。

---

## 二、飞书机器人创建（约 2 分钟）

1. 打开**飞书** → 进入一个群聊（没有群可新建一个只有自己的群）
2. 群右上角 **「···」→「设置」→「群机器人」→「添加机器人」**
3. 选择 **「自定义机器人」**，名字随意（如"交易信号"）
4. 安全设置建议勾选 **「自定义关键词」**，填：`信号`
   （程序发送的消息卡片标题含"做多信号/做空信号"，可命中该关键词；
   若选了「签名校验」则把密钥填入 `.env` 的 `FEISHU_SECRET`）
5. 复制 **Webhook 地址**，填入 `.env` 的 `FEISHU_WEBHOOK`
6. 测试连通性：

```bash
python scripts/test_feishu.py
```

群里收到一条文本 + 一张策略卡片即成功。

---

## 三、使用

### 1. 回测（先看胜率，再决定是否跟单）

```bash
# 回测单个交易对（四个周期全跑）
python backtest.py --symbol TSLA-USDT

# 指定周期和起始时间
python backtest.py --symbol NVDA-USDT --timeframe 1h --since 2025-06-01

# 回测 config.yaml 中全部有效交易对
python backtest.py --all

# 数据变化后强制重新拉取（忽略缓存）
python backtest.py --symbol TSLA-USDT --no-cache
```

输出：交易次数 / 多空胜率 / 累计收益 / 盈亏因子 / 最大回撤；明细存 `results/`，K线缓存存 `data/`。

### 2. 实时监控（信号触发 → 飞书推送）

```bash
# 持续监控（推荐挂着跑，Ctrl+C 停止）
python monitor.py

# 只扫一轮就退出（调试用，或配合系统定时任务每分钟执行）
python monitor.py --once
```

收到信号时飞书卡片示例内容：

```
📈 做多信号 | TSLA-USDT | 1h
方向: 做多 🟢      周期: 1h
建议入场: 248.50    盈亏比: 1:1.6
止损价: 244.83 (-1.48%)
止盈价: 254.37 (+2.36%)
建议仓位: ≈ 675 USDT
```

### 3. 后台常驻（可选）

- **Windows**：用 PowerShell `Start-Process python -ArgumentList "monitor.py" -WindowStyle Hidden`，或直接用「任务计划程序」
- **Mac/Linux**：`nohup python monitor.py > monitor.log 2>&1 &`

---

## 四、风控说明（config.yaml → risk 段）

| 参数 | 默认值 | 含义 |
|------|-------:|------|
| `risk_per_trade` | 0.01 | 单笔最大风险占本金 1%，用于反推建议仓位 |
| `account_size` | 10000 | 模拟本金（USDT） |
| `signal_cooldown_sec` | 14400 | 同交易对同方向 4 小时内不重复推送 |
| `max_signals_per_day` | 6 | 单周期每日信号上限 |

建议仓位公式：`建议仓位 = 本金 × 单笔风险% ÷ 止损距离%`

---

## 五、目录结构

```
okx_tradfi_monitor/
├── config.yaml            # 交易对/周期/风控配置
├── .env / .env.example    # 飞书webhook、代理
├── monitor.py             # 实时监控主程序
├── backtest.py            # 回测入口
├── core/                  # 数据、分型、策略、回测、风控核心
├── notify/feishu.py       # 飞书推送
├── scripts/test_feishu.py # 飞书连通性测试
├── data/                  # K线缓存 + 推送状态
├── results/               # 回测明细 + 信号日志
└── demo_output/           # 演示报告（模拟数据生成）
```

---

## 六、常见问题

**Q1：报错「无法连接 OKX 拉取市场列表」？**
国内网络无法直连 OKX。检查：① 代理软件已开启；② `.env` 中 `PROXY_URL` 端口与代理软件一致（Clash 默认 7890）；③ 测试 `curl -x http://127.0.0.1:7890 https://www.okx.com/api/v5/public/time`。

**Q2：提示某些交易对"未找到"？**
说明该代币尚未在 OKX 现货上线（如部分新股代币会陆续上线），程序已自动剔除，属正常现象。可后续关注上线情况后在 `config.yaml` 确认。

**Q3：飞书发送失败，提示关键词不匹配？**
机器人安全设置的「自定义关键词」必须填 `信号`，或改用「签名校验」并把密钥填入 `FEISHU_SECRET`。

**Q4：回测数据和实盘有差异？**
回测已计入双边手续费和分型确认延迟（无未来函数），但实盘还有滑点；股票代币在美股休市时段流动性差、点差大，信号质量会下降，建议优先关注美股交易时段的信号。

**Q5：想改监控周期/参数？**
直接编辑 `config.yaml` 的 `timeframes` 段即可，支持 OKX 标准周期（1m/5m/15m/30m/1h/4h/1d 等）。

---

## 免责声明

本项目仅供学习与技术研究使用，不构成任何投资建议。代币化股票价格波动剧烈，杠杆交易风险极高，请根据自身风险承受能力谨慎决策，盈亏自负。
