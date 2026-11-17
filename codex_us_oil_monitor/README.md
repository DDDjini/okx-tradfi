# S28 欧意美股与原油信号监控

基于 OKX 已收盘 UTC 日线与 4H K 线，运行固定参数 S28 信号扫描，发送飞书交易卡片。只监控和通知，不连接交易账户、不自动下单。

## 当前监控范围

共 33 个合约：保留之前的 16 个在架合约，新增本轮欧意回测中先触率 ≥80%、已决样本 ≥10 的美国上市股票。80% 是加入名单的历史筛选门槛，不代表现有33个全部超过80%。BNO未上架，已移出配置。

AAPL, NVDA, AMD, TSLA, META, AMZN, GOOGL, COIN, MSTR, AVGO, NFLX, PLTR, COST, SMH, USO, CL=F, GLW, ADBE, BB, DKNG, ORCL, RKLB, ASML, BOT, GEV, HIMS, TSEM, TSM, CRWV, IBM, HOOD, CSCO, QCOM

CL=F 对应欧意 `CL-USDT-SWAP`，其余为 `<symbol>-USDT-SWAP`。

## 自动运行

- 工作流：`.github/workflows/s28-us-oil.yml`。
- UTC `17 */2 * * *`，北京时间每天 00:17、02:17、04:17、06:17、08:17、10:17、12:17、14:17、16:17、18:17、20:17、22:17；GitHub调度可能延迟。
- 支持 Actions 页面手动 Run workflow。
- 每次发送扫描汇总，没有新信号也发送；行情异常单列并使运行失败，避免误报扫描成功。
- 近48小时已确认信号用于断线补扫，按信号唯一键去重；历史确认时间显示在卡片上，不表示刚刚产生。
- 单条通知成功后立即保存回执，工作流将回执提交回仓库；中断发生在发送与保存之间仍可能导致重复通知。

## 飞书配置

仓库 Settings → Secrets and variables → Actions：

- `S28_FEISHU_WEBHOOK`：自定义机器人 Webhook，必填。
- `FEISHU_SIGN_SECRET`：机器人签名密钥，仅开启签名校验时填写。

无需个人 GitHub token 或欧意交易API密钥。行情使用欧意公共接口；工作流通过仓库自带 GITHUB_TOKEN 保存回执。不要将任何密钥写进代码、日志或README。

## 信号卡片

卡片展示方向、MID参考价、参考入场区间、SL、TP1、TP2、北京时间与历史先触统计，并提供欧意合约链接。单信号15%是名义仓位上限提示，程序不负责仓位执行。确认价已越过TP1会显示观察提示。

## 统计含义

`universe.json` 中 `okx_backtest` 是欧意固定参数回测，截止2026-11-16 08:00 UTC。先触率指信号确认后先碰TP1的次数占已决信号的比例，不要求挂单先成交。未结束不进入分母；同柱冲突以1分钟K线细分，仍不明时保守算失败。小样本、信号重叠、确认时已越过目标，均会影响解读；它不是实盘获利率。

旧筛选记录保留在部分标的字段中供追溯，不用于宣称欧意胜率。欧意版使用全天UTC K线，不等同于美股正常交易时段版。

## 本地运行

```bash
pip install -r codex_us_oil_monitor/requirements.txt
python codex_us_oil_monitor/scanner.py
python codex_us_oil_monitor/scanner.py --symbols AAPL NVDA
# 已在环境变量中设置FEISHU_WEBHOOK后才发送：
python codex_us_oil_monitor/scanner.py --send
```

无 `--send` 为只读扫描，不发送飞书。

## 文件结构

- `codex_us_oil_monitor/scanner.py`：欧意行情、扫描、通知与去重。
- `codex_us_oil_monitor/feishu_cards.py`：飞书交易卡与扫描汇总卡。
- `codex_us_oil_monitor/engine.py`、`entry_rules.py`、`single_point.py`：策略引擎及其必要依赖。
- `codex_us_oil_monitor/universe.json`：监控列表和回测筛选依据。
- `codex_us_oil_monitor/state/notifications.json`：已发送回执，请勿随意清空。
- 每次运行上传 `scan-report` artifact，包含扫描结果、错误和新增信号。

旧项目根目录监控、core、notify、scripts和废弃工作流已清理；历史版本可通过Git提交记录追溯。
