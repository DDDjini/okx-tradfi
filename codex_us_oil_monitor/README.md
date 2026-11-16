# 欧意 S-28 美股与原油扫描

当前数据源为 OKX 公共 SWAP API，使用 1Dutc 和 4H 已收盘 K 线。每四小时扫描，飞书每轮汇报新信号、无信号或异常。16 个对应合约可用，BNO 未上架时在汇总中列明。CL=F 映射 CL-USDT-SWAP。

历史 universe.json 的 70% 先触率属于此前 Yahoo/Alpaca 回测，不能视作欧意永续胜率。欧意交易日线采用 UTC 全天口径，与美股常规时段有差别。

部署路径 codex_us_oil_monitor，工作流位于仓库 .github/workflows/s28-us-oil.yml。Secret S28_FEISHU_WEBHOOK 为通知地址，可选 FEISHU_SIGN_SECRET 为签名密钥。公开行情无需 Alpaca 或欧意账户密钥。

本地 python scanner.py 只读，--send 启用通知。state/notifications.json 保存成功发送记录；网络结果不明确或回写失败时可能重发。scan_output.json 包含逐标的健康状态，Actions 上传为运行附件。行情不足或缺失时汇报异常并使任务失败。
