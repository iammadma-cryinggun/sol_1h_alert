# SOL 1小时预警系统 V3 - 云端部署版

## 系统说明

基于网格搜索最优配置的SOL实时信号预警系统，与回测V3完全对齐。

### 核心特性

- ✅ **实时信号检测**：每1小时检测一次布林带收缩信号
- ✅ **动态仓位管理**：基于信号稳定性动态分配仓位（25%-35%）
- ✅ **完整风控系统**：
  - 止损：3%
  - TP1：4%
  - TP2：8%
  - 移动止损：TP1后开启，偏移0.6%
  - 时间止损：80小时
- ✅ **OI过滤**：OI下降 < -1% 过滤
- ✅ **多渠道通知**：Telegram + 微信（Server酱）
- ✅ **持仓监控**：10秒高频检测，确保实时止盈止损

### 性能参数（回测验证）

- 收益率：**1416.79%**（原778%，提升82%）
- 盈亏比：**30.55**（原14.47，提升111%）
- 胜率：54.8%
- 交易次数：206次

## 快速部署

### Zeabur部署

1. 将代码推送到GitHub仓库
2. 登录Zeabur控制台
3. 点击"新建服务"
4. 选择"从Git仓库部署"
5. 输入仓库地址
6. 选择分支：`main`
7. **配置环境变量**（重要！）：
   ```
   TELEGRAM_TOKEN=你的Telegram_Bot_Token
   TELEGRAM_CHAT_ID=你的Telegram_Chat_ID
   WECHAT_API_URL=https://sctapi.ftqq.com/你的SCKEY.send（可选）
   ```
8. Zeabur会自动读取 `zeabur.yml` 配置
9. 部署完成！

### 环境变量说明

| 变量名 | 必需 | 说明 | 获取方式 |
|--------|------|------|----------|
| `TELEGRAM_TOKEN` | ✅ | Telegram Bot Token | 向 @BotFather 申请 |
| `TELEGRAM_CHAT_ID` | ✅ | Telegram Chat ID | 发送消息给 @userinfobot 获取 |
| `WECHAT_API_URL` | ❌ | 微信Server酱 | 在 [Server酱](https://sct.ftqq.com) 配置 |

### 本地运行

1. 复制 `.env.example` 为 `.env`
2. 填写你的Telegram配置：
   ```bash
   TELEGRAM_TOKEN=你的token
   TELEGRAM_CHAT_ID=你的chat_id
   ```
3. 安装依赖：`pip install -r requirements.txt`
4. 运行：`python sol1小时预警V3_对齐版.py`

## 数据文件

程序运行时会自动创建以下文件：

- `sol_position.json` - 持仓状态
- `sol_signal_history.json` - 信号历史

这些文件会保存在持久化存储卷中，重启不丢失。

## 注意事项

1. **代理设置**：云端部署会自动禁用代理（代码已包含检测逻辑）
2. **时区**：系统时区设置为 Asia/Shanghai
3. **资源限制**：
   - CPU: 0.5核
   - 内存: 512MB
   - 如需更多资源，请修改 `zeabur.yml` 中的 `resources` 配置

## Telegram命令

- `/status` - 查看当前状态
- `/close` - 手动平仓

## 更新日志

### V3 对齐版（当前版本）

- ✅ 与回测V3完全对齐
- ✅ 全局最优参数（二维网格搜索）
- ✅ 动态仓位V2（保守策略）
- ✅ 10秒高频持仓监控
- ✅ 云端环境自动禁用代理
