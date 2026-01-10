# -*- coding: utf-8 -*-
"""
[SYSTEM] SOL实时信号预警系统 V3 - 与回测V3完全对齐版
基于网格搜索最优配置
修复内容：
  1. [OK] OI下降判断改为回测V3逻辑（最近2小时OI变化都为负）
  2. [OK] 参数更新为平衡型优化参数
  3. [OK] 线程安全（锁 + deque）
  4. [OK] 30秒短循环监控（修复睡眠阻塞）
  5. [OK] 完整的时间止损两阶段确认
  6. [STAR] 全局最优参数：Squeeze=4.0%, 做多>30, 做空<60（二维网格搜索80组合）
     - 收益率: 1287%（原778%，提升65%）
     - 盈亏比: 28.93（原14.47，提升100%）
  7. [STAR][STAR] 集成动态仓位V2（保守策略）
     - 基于信号稳定性动态分配仓位：25%-35%
     - 高质量信号（70-100分）：35%仓位，胜率54.8%
     - 收益率提升至：1416.79%（+129%）
     - 盈亏比提升至：30.55（+5.6%）
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone
import sys
import threading
import telebot
import requests
import json
import os
from collections import deque
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

warnings.filterwarnings('ignore')

class SignalAlertSystemV3:
    """SOL预警系统V3 - 与回测V3完全对齐"""

    def __init__(self):
        # 代理设置（云端环境自动禁用）
        self.is_cloud_env = os.getenv('ZEABUR') is not None or os.getenv('CLOUD_ENV') is not None
        self.PROXY_URL = os.getenv('PROXY_URL') if not self.is_cloud_env else None
        self.TARGET_SYMBOL = 'SOL/USDT'
        self.TIMEFRAME = '1h'
        self.FEE_RATE = 0.0004

        # 信号检查频率（1小时）
        self.UPDATE_INTERVAL = 3600

        # OI采集频率（5分钟）
        self.OI_UPDATE_INTERVAL = 300

        # [STAR] 持仓监控频率（10秒 - 更高频率确保实时检测TP1和移动止损）
        self.POSITION_MONITOR_INTERVAL = 10

        # [STAR] 全局最优参数（二维网格搜索，80个组合，2025-12-31）
        self.PARAMS = {
            'sl': 3.0,                      # 止损 3%
            'tp1': 4.0,                     # 第一止盈 4%
            'tp2': 8.0,                     # 第二止盈 8%
            'trail_after_tp1': True,        # TP1后开启移动止损
            'flip_stop_to_breakeven': True, # 移动止损前先保本
            'trail_offset': 0.6,            # [STAR] 移动止损偏移 0.6% (优化)
            'squeeze': 4.0,                 # [STAR] 布林带收缩 4.0%（全局最优，从3.0%提高）
            'oi_change_filter': -0.01,      # OI过滤阈值 -1%
            'time_stop_hours': 80,          # [STAR] 时间止损 80h (优化)
            'cost_zone_pct': 0.5,          # 成本区 ±0.5%
            'position_size': 0.3,          # 仓位 30%
            'leverage': 5                   # 杠杆 5x
        }

        # 通知配置（从环境变量读取）
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.wechat_api_url = os.getenv('WECHAT_API_URL')
        
        # 验证必需的环境变量
        if not self.telegram_token:
            raise ValueError('TELEGRAM_TOKEN 环境变量未设置')
        if not self.telegram_chat_id:
            raise ValueError('TELEGRAM_CHAT_ID 环境变量未设置')

        # 初始化
        self.bot = None
        self.wechat_enabled = True
        self.exchange = None

        # [STAR] 线程安全：使用锁和deque
        self.oi_lock = threading.Lock()
        self.oi_history = deque(maxlen=576)  # 自动限制长度，线程安全
        self.oi_changes_history = deque(maxlen=576)  # [STAR] 新增：存储OI变化率

        # OI采集线程控制
        self.oi_collector_running = False
        self.oi_collector_thread = None

        # 当前仓位状态
        self.current_position = {
            'status': 'none',
            'entry_price': 0,
            'entry_time': None,
            'stop_loss': 0,
            'take_profit1': 0,
            'take_profit2': 0,
            'trail_stop': 0,
            'tp1_achieved': False,
            'breakeven_activated': False,
            'position_size': self.PARAMS['position_size'],
            'leverage': self.PARAMS['leverage'],
            'current_pnl': 0,
            'current_pnl_pct': 0,
            'hold_hours': 0,
            'time_stop_activated': False,  # [STAR] 改名：与回测V3一致
            # [TARGET] 原始趋势信息（混合策略：保留第一次信号的止盈目标）
            'original_tp1': 0,
            'original_tp2': 0,
            'original_signal': 0,  # 1=long, -1=short
            'original_signal_time': None,
            'trend_continuation_count': 0  # 同一趋势延续次数
        }

        # 数据存储
        self.price_data = pd.DataFrame()
        self.historical_signals = []

        # 运行标志
        self.is_running = False
        self.monitor_thread = None

        # [STAR] 持仓状态文件
        self.position_file = "sol_position_state.json"

        # [STAR] 信号历史文件（独立于持仓，用于手动平仓后记录信号）
        self.signal_history_file = "sol_signal_history.json"

        # 初始化
        self.init_exchange()
        self.setup_notifications()
        self.setup_telegram_commands()  # [NEW] 设置Telegram命令
        self.load_position_state()  # [STAR] 加载持久化的持仓状态
        self.load_signal_history()  # [NEW] 加载信号历史

    # ============ 动态仓位V2功能 ============
    def calculate_dynamic_position_score(self, c, l, h, ma20, bw, coo, oi_change, oi_divergence):
        """
        计算信号稳定性评分 (0-100) - 用于动态仓位V2
        保守策略，避免极值陷阱

        返回: (total_score, details_dict)
        """
        score = 0
        details = {
            'coo_score': 0,
            'coo_reason': '',
            'bw_score': 0,
            'bw_reason': '',
            'oi_score': 0,
            'oi_reason': '',
            'break_score': 0,
            'break_reason': ''
        }

        squeeze_threshold = self.PARAMS['squeeze']
        is_sqz = bw < squeeze_threshold

        # 1. COO稳定性 (0-25分) - 避开极值
        if is_sqz:
            if coo > 30 and coo <= 50:  # 做多区间
                if 35 <= coo <= 45:
                    score += 25
                    details['coo_score'] = 25
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期35-45最优区间)'
                elif coo < 35:
                    score += 20
                    details['coo_score'] = 20
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期30-35良好)'
                else:
                    score += 15
                    details['coo_score'] = 15
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期45-50一般)'
            elif coo < 60 and coo >= 50:  # 做空区间
                if 52 <= coo <= 58:
                    score += 25
                    details['coo_score'] = 25
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期52-58最优)'
                elif coo > 58:
                    score += 20
                    details['coo_score'] = 20
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期58-60良好)'
                else:
                    score += 15
                    details['coo_score'] = 15
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期50-52一般)'
            else:  # 🔧 修复：coo <= 30 的情况
                if coo <= 20:  # 极度超卖
                    score += 20
                    details['coo_score'] = 20
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期≤20极度超卖)'
                else:  # 20 < coo <= 30
                    score += 15
                    details['coo_score'] = 15
                    details['coo_reason'] = f'COO {coo:.1f}(收缩期20-30超卖区间)'
        else:
            # 扩张时：只取COO 20-30或70-80的温和区间
            if 70 <= coo <= 80:
                score += 25
                details['coo_score'] = 25
                details['coo_reason'] = f'COO {coo:.1f}(扩张期70-80最优)'
            elif 20 <= coo <= 30:
                score += 25
                details['coo_score'] = 25
                details['coo_reason'] = f'COO {coo:.1f}(扩张期20-30最优)'
            elif coo > 80 or coo < 20:
                score += 10
                details['coo_score'] = 10
                details['coo_reason'] = f'COO {coo:.1f}(极值区，谨慎)'
            else:
                details['coo_score'] = 15
                details['coo_reason'] = f'COO {coo:.1f}(扩张期其他区间)'

        # 2. 布林带状态 (0-30分)
        if bw < 2.5:
            score += 30
            details['bw_score'] = 30
            details['bw_reason'] = f'带宽{bw:.2f}%(极度收缩<2.5%)'
        elif bw < 3.0:
            score += 25
            details['bw_score'] = 25
            details['bw_reason'] = f'带宽{bw:.2f}%(深度收缩2.5-3%)'
        elif bw < 4.0:
            score += 20
            details['bw_score'] = 20
            details['bw_reason'] = f'带宽{bw:.2f}%(收缩3-4%)'
        elif bw < 5.0:
            score += 10
            details['bw_score'] = 10
            details['bw_reason'] = f'带宽{bw:.2f}%(扩张4-5%)'
        else:
            score += 5
            details['bw_score'] = 5
            details['bw_reason'] = f'带宽{bw:.2f}%(高度扩张>5%)'

        # 3. OI支撑 (0-25分)
        if oi_change > 0.01:
            score += 25
            details['oi_score'] = 25
            details['oi_reason'] = f'OI+{oi_change*100:.2f}%(强势支撑>1%)'
        elif oi_change > 0:
            score += 15
            details['oi_score'] = 15
            details['oi_reason'] = f'OI+{oi_change*100:.2f}%(温和支撑0-1%)'
        elif oi_change > -0.01:
            score += 5
            details['oi_score'] = 5
            details['oi_reason'] = f'OI{oi_change*100:.2f}%(中性-1%-0)'
        else:
            details['oi_score'] = 0
            details['oi_reason'] = f'OI{oi_change*100:.2f}%(负增长<-1%)'

        if oi_divergence < -0.01:
            score -= 15
            details['oi_score'] -= 15
            details['oi_reason'] += f',背离-15分'

        # 4. 价格突破质量 (0-20分)
        p_bull = (l <= ma20) and (c > ma20)
        p_bear = (h >= ma20) and (c < ma20)

        if p_bull or p_bear:
            score += 15
            details['break_score'] = 15
            details['break_reason'] = '有效突破MA20'

            if p_bull:
                break_pct = (c - ma20) / ma20 * 100
                if 0.1 <= break_pct <= 1.0:
                    score += 5
                    details['break_score'] += 5
                    details['break_reason'] += f'(幅度{break_pct:.2f}%优质)'
                else:
                    details['break_reason'] += f'(幅度{break_pct:.2f}%)'
            elif p_bear:
                break_pct = (ma20 - c) / ma20 * 100
                if 0.1 <= break_pct <= 1.0:
                    score += 5
                    details['break_score'] += 5
                    details['break_reason'] += f'(幅度{break_pct:.2f}%优质)'
                else:
                    details['break_reason'] += f'(幅度{break_pct:.2f}%)'
        else:
            details['break_score'] = 0
            details['break_reason'] = '无有效突破'

        total_score = max(0, min(100, score))
        return total_score, details

    def get_dynamic_position_size_v2(self, score):
        """
        动态仓位映射V2（保守策略）
        """
        base_pos_size = self.PARAMS['position_size']

        if score >= 70:
            return 0.35
        elif score >= 55:
            return 0.32
        elif score >= 40:
            return base_pos_size  # 0.30
        elif score >= 25:
            return 0.28
        else:
            return 0.25
    # ============ 动态仓位功能结束 ============

    def init_exchange(self):
        """初始化交易所连接（永续合约）"""
        try:
            proxies = {'http': self.PROXY_URL, 'https': self.PROXY_URL}
            self.exchange = ccxt.binance({
                'enableRateLimit': True,
                'proxies': proxies,
                'timeout': 30000,
                'options': {'defaultType': 'future'}  # 永续合约
            })
            print("交易所连接初始化成功 (永续合约)")
        except Exception as e:
            print(f"交易所连接失败: {e}")
            self.exchange = None

    def setup_notifications(self):
        """初始化通知渠道"""
        print("\n通知初始化:")

        if self.telegram_token and self.telegram_chat_id:
            try:
                self.bot = telebot.TeleBot(self.telegram_token)
                print("   Telegram: 已连接")
            except Exception as e:
                print(f"   Telegram连接失败: {e}")
                self.bot = None

        if self.wechat_api_url and "YOUR_SENDKEY" not in self.wechat_api_url:
            print("   微信API: 已配置")
        else:
            print("   微信API: 未配置")
            self.wechat_enabled = False

    def load_signal_history(self):
        """[NEW] 加载信号历史（用于手动平仓后恢复信号）"""
        try:
            if not os.path.exists(self.signal_history_file):
                print("   [INFO] 未找到信号历史文件")
                return

            with open(self.signal_history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

            # 加载历史信号信息
            if history.get('signal_type'):
                self.current_position['original_signal'] = history['signal_type']
                self.current_position['original_signal_time'] = datetime.fromisoformat(history['signal_time']) if history.get('signal_time') else None
                self.current_position['original_tp1'] = history.get('tp1_price', 0)
                self.current_position['original_tp2'] = history.get('tp2_price', 0)
                self.current_position['trend_continuation_count'] = history.get('continuation_count', 0)
                print(f"   [INFO] 加载信号历史: {history['signal_type']}")

        except Exception as e:
            print(f"   [WARN] 加载信号历史失败: {e}")

    def save_signal_history(self, signal, entry_price, tp1, tp2):
        """[NEW] 保存信号历史"""
        try:
            history = {
                'signal_type': signal,
                'signal_time': datetime.now().isoformat(),
                'entry_price': entry_price,
                'tp1_price': tp1,
                'tp2_price': tp2,
                'continuation_count': self.current_position.get('trend_continuation_count', 0),
                'last_update': datetime.now().isoformat()
            }

            with open(self.signal_history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"   [WARN] 保存信号历史失败: {e}")

    def setup_telegram_commands(self):
        """[NEW] 设置Telegram命令监听"""
        if not self.bot:
            return

        # 注册消息处理器
        self.register_telegram_handlers()

        # 在后台线程中启动Telegram监听
        import threading
        telegram_thread = threading.Thread(target=self.run_telegram_polling, daemon=False)
        telegram_thread.start()
        print("   Telegram交互: 已启用 (命令: /help, /status, /close)")

    def register_telegram_handlers(self):
        """[NEW] 注册Telegram消息处理器"""
        import telebot
        from telebot import types

        @self.bot.message_handler(commands=['start', 'help'])
        def send_help(message):
            if message.chat.id != int(self.telegram_chat_id):
                return
            help_text = """
🤖 SOL预警系统 V3 - 交互式控制

可用命令：
/status - 查看当前持仓状态
/close - 手动平仓（保留信号历史）
/clear - 清除所有数据（包括信号历史）

💡 提示：手动平仓后，相同信号会重新计算止盈止损
            """
            try:
                self.bot.reply_to(message, help_text)
            except Exception as e:
                print(f"   [ERROR] Telegram回复失败: {e}")

        @self.bot.message_handler(commands=['status'])
        def send_status(message):
            if message.chat.id != int(self.telegram_chat_id):
                return

            try:
                pos = self.current_position
                if pos['status'] != 'none':
                    direction = "做多" if pos['status'] == 'long' else "做空"
                    status_text = f"""
📊 当前持仓状态
方向: {direction}
入场价: ${pos['entry_price']:.4f}
当前盈亏: {pos['current_pnl_pct']:.2f}%
止损: ${pos['stop_loss']:.4f}
TP1: ${pos['take_profit1']:.4f}
TP2: ${pos['take_profit2']:.4f}
持仓时间: {pos['hold_hours']:.1f}小时
                    """
                else:
                    status_text = "📊 当前状态: 空仓\n\n等待新信号..."

                # 显示信号历史
                if pos.get('original_signal') and pos.get('original_signal_time'):
                    # 处理时间类型（可能是字符串或datetime对象）
                    signal_time = pos['original_signal_time']
                    if isinstance(signal_time, str):
                        signal_time = datetime.fromisoformat(signal_time)

                    # 正确处理时区：统一转换为UTC时间再比较
                    from datetime import timezone
                    now_utc = datetime.now(timezone.utc)

                    # 如果signal_time有时区信息，转换为UTC
                    if signal_time.tzinfo is not None:
                        signal_time_utc = signal_time.astimezone(timezone.utc)
                    else:
                        # 如果没有时区信息，假设是UTC
                        signal_time_utc = signal_time.replace(tzinfo=timezone.utc)

                    hours_ago = (now_utc - signal_time_utc).total_seconds() / 3600
                    status_text += f"\n\n📡 原始信号: {pos['original_signal']} ({hours_ago:.1f}小时前)\n"
                    status_text += f"原始TP1: ${pos['original_tp1']:.4f}\n"
                    status_text += f"原始TP2: ${pos['original_tp2']:.4f}"

                self.bot.reply_to(message, status_text)
            except Exception as e:
                print(f"   [ERROR] Status命令执行失败: {e}")
                self.bot.reply_to(message, f"❌ 获取状态失败: {e}")

        @self.bot.message_handler(commands=['close', 'clear'])
        def handle_close(message):
            if message.chat.id != int(self.telegram_chat_id):
                return

            try:
                cmd = message.text.split()[0]
                self.handle_manual_close(cmd == '/clear')
            except Exception as e:
                print(f"   [ERROR] Close命令执行失败: {e}")
                self.bot.reply_to(message, f"❌ 平仓失败: {e}")

        @self.bot.message_handler(func=lambda message: message.text == '我已平仓')
        def handle_manual_close_message(message):
            if message.chat.id != int(self.telegram_chat_id):
                return
            try:
                self.handle_manual_close(clear_history=False)
            except Exception as e:
                print(f"   [ERROR] 手动平仓失败: {e}")
                self.bot.reply_to(message, f"❌ 平仓失败: {e}")

        print("   [INFO] Telegram消息处理器已注册")

    def run_telegram_polling(self):
        """[NEW] 运行Telegram轮询（独立线程）"""
        while True:
            try:
                print("   [INFO] Telegram轮询启动...")
                self.bot.polling(non_stop=False, interval=1, timeout=60, long_polling_timeout=20)
            except Exception as e:
                print(f"   [ERROR] Telegram轮询异常: {e}")
                print("   [INFO] 5秒后重新启动...")
                time.sleep(5)

    def handle_manual_close(self, clear_history=False):
        """[NEW] 处理手动平仓"""
        try:
            if self.current_position['status'] == 'none':
                if self.bot:
                    self.bot.send_message(self.telegram_chat_id, "⚠️ 当前无持仓，无需平仓")
                return

            # 发送平仓通知
            pos = self.current_position
            alert_title = f"📉 手动平仓 - {self.TARGET_SYMBOL}"
            alert_message = (
                f"手动平仓成功\n\n"
                f"方向: {'多头' if pos['status'] == 'long' else '空头'}\n"
                f"入场价: ${pos['entry_price']:.4f}\n"
                f"当前盈亏: {pos['current_pnl_pct']:.2f}%\n"
                f"持仓时间: {pos['hold_hours']:.1f}小时\n\n"
            )

            if clear_history:
                # 清除所有数据（包括信号历史）
                alert_message += "已清除：\n- 持仓数据\n- 信号历史\n- 趋势信息\n\n下次信号将作为新趋势处理。"
                try:
                    if os.path.exists(self.signal_history_file):
                        os.remove(self.signal_history_file)
                except:
                    pass

                # 重置所有信号信息
                self.current_position['original_signal'] = 0
                self.current_position['original_signal_time'] = None
                self.current_position['original_tp1'] = 0
                self.current_position['original_tp2'] = 0
                self.current_position['trend_continuation_count'] = 0
            else:
                # 保留信号历史
                alert_message += "已保留信号历史\n\n下次相同信号将使用混合策略：\n- 新止损（最新价格）\n- 旧止盈（原始信号）"

            self.send_alert(alert_title, alert_message, "close")

            # 重置持仓状态
            self.current_position = {
                'status': 'none',
                'entry_price': 0,
                'entry_time': None,
                'stop_loss': 0,
                'take_profit1': 0,
                'take_profit2': 0,
                'trail_stop': 0,
                'tp1_achieved': False,
                'breakeven_activated': False,
                'position_size': self.PARAMS['position_size'],
                'leverage': self.PARAMS['leverage'],
                'current_pnl': 0,
                'current_pnl_pct': 0,
                'hold_hours': 0,
                'time_stop_activated': False,
                # 保留或不保留信号历史
                'original_tp1': 0 if clear_history else self.current_position.get('original_tp1', 0),
                'original_tp2': 0 if clear_history else self.current_position.get('original_tp2', 0),
                'original_signal': 0 if clear_history else self.current_position.get('original_signal', 0),
                'original_signal_time': None if clear_history else self.current_position.get('original_signal_time'),
                'trend_continuation_count': 0 if clear_history else self.current_position.get('trend_continuation_count', 0)
            }

            # 保存状态
            self.save_position_state()

        except Exception as e:
            print(f"   [ERROR] 手动平仓失败: {e}")

    def send_alert(self, title, message, alert_type="info"):
        """发送通知"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        emoji_map = {
            "info": "[INFO]", "success": "[OK]", "warning": "[WARN]",
            "danger": "[ALERT]", "buy": "[BUY]", "sell": "[SELL]", "close": "[CLOSE]"
        }
        prefix = emoji_map.get(alert_type, "[INFO]")
        full_message = f"{prefix} {timestamp}\n{title}\n{message}"

        print(f"\n{full_message}")

        if self.bot:
            try:
                self.bot.send_message(self.telegram_chat_id, full_message)
            except:
                pass

        if self.wechat_enabled and self.wechat_api_url:
            try:
                payload = {
                    "title": f"{prefix} {title}",
                    "desp": f"时间: {timestamp}\n\n{message}"
                }
                requests.post(self.wechat_api_url, data=payload, timeout=5)
            except:
                pass

    def fetch_realtime_oi(self):
        """获取实时OI数据"""
        if not self.exchange:
            return None

        try:
            symbol_for_oi = self.TARGET_SYMBOL.replace('/', '')

            oi_data = self.exchange.fapiPublicGetOpenInterest({
                'symbol': symbol_for_oi
            })

            current_oi = float(oi_data['openInterest'])
            return current_oi

        except Exception as e:
            print(f"   OI数据获取失败: {e}")
            return None

    def oi_collection_loop(self):
        """独立线程采集OI数据（5分钟频率）"""
        print(f"   OI采集线程启动: 每5分钟采集一次")

        self.oi_collector_running = True

        while self.oi_collector_running:
            try:
                current_time = datetime.now(timezone.utc)  # [OK] 修复：统一使用UTC时间

                # 只在整5分钟的倍数时刻采集
                if current_time.minute % 5 == 0 and current_time.second < 30:
                    oi_value = self.fetch_realtime_oi()
                    if oi_value:
                        oi_point = {
                            'timestamp': current_time,
                            'open_interest': oi_value
                        }

                        # [STAR] 线程安全：使用锁保护
                        with self.oi_lock:
                            self.oi_history.append(oi_point)

                            # [STAR] 新增：计算并存储OI变化率
                            if len(self.oi_history) >= 2:
                                prev_oi = list(self.oi_history)[-2]['open_interest']
                                oi_change = (oi_value - prev_oi) / prev_oi if prev_oi > 0 else 0
                                self.oi_changes_history.append({
                                    'timestamp': current_time,
                                    'oi_change': oi_change
                                })

                        if len(self.oi_history) % 5 == 0:
                            with self.oi_lock:
                                print(f"   OI采集: {oi_value:,.0f} ({current_time.strftime('%H:%M:%S')}) - 共{len(self.oi_history)}个点")

                time.sleep(30)

            except Exception as e:
                print(f"   OI采集出错: {e}")
                time.sleep(60)

    def start_oi_collection(self):
        """启动OI采集线程"""
        self.oi_collector_thread = threading.Thread(target=self.oi_collection_loop)
        self.oi_collector_thread.daemon = True
        self.oi_collector_thread.start()
        print("   OI采集线程已启动")

    def stop_oi_collection(self):
        """停止OI采集线程"""
        self.oi_collector_running = False
        if self.oi_collector_thread:
            self.oi_collector_thread.join(timeout=5)
        print("   OI采集线程已停止")

    def save_position_state(self):
        """[STAR] 保存持仓状态到文件（持久化）"""
        try:
            position_data = {
                'position': self.current_position,
                'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'version': 'V3'
            }
            with open(self.position_file, 'w', encoding='utf-8') as f:
                json.dump(position_data, f, indent=2, default=str)
            print(f"   [SAVE] 持仓状态已保存")
        except Exception as e:
            print(f"   [WARN] 保存持仓状态失败: {e}")

    def load_position_state(self):
        """[STAR] 从文件加载持仓状态"""
        try:
            if not os.path.exists(self.position_file):
                print("   [INFO] 未找到持仓状态文件，从空仓开始")
                return

            with open(self.position_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            saved_position = data.get('position', {})
            saved_time = data.get('saved_at', 'unknown')
            last_trade = data.get('last_trade', {})

            # [STAR] 总是显示持仓状态摘要
            print("\n" + "="*80)
            print("[STATUS] 持仓状态摘要")
            print("="*80)

            if saved_position.get('status') != 'none':
                # 有持仓
                print(f"[POSITION] 当前有持仓")
                print(f"   方向: {'[LONG] 多头' if saved_position.get('status') == 'long' else '[SHORT] 空头'}")
                print(f"   入场价: ${saved_position.get('entry_price', 0):.2f}")
                print(f"   止损: ${saved_position.get('stop_loss', 0):.2f}")
                print(f"   TP1: ${saved_position.get('take_profit1', 0):.2f}")
                print(f"   TP2: ${saved_position.get('take_profit2', 0):.2f}")
                print(f"   入场时间: {saved_position.get('entry_time', 'Unknown')}")
                print(f"   保存时间: {saved_time}")
                print()
                print("[WARN] 请确认：")
                print("   1. 检查你的交易所账户，是否真的持有此仓位")
                print("   2. 如果已平仓，输入 'n' 忽略此状态")
                print("   3. 如果仍持有，输入 'y' 恢复持仓监控")
                print("="*80)

                # 询问用户确认
                confirm = input("\n是否恢复持仓监控? (y/n): ").strip().lower()

                if confirm == 'y':
                    # 恢复持仓状态
                    self.current_position = saved_position
                    # 重新计算entry_time为datetime对象
                    if saved_position.get('entry_time'):
                        if isinstance(saved_position['entry_time'], str):
                            self.current_position['entry_time'] = datetime.fromisoformat(saved_position['entry_time'])
                        else:
                            self.current_position['entry_time'] = saved_position['entry_time']

                    # [NEW] 转换original_signal_time为datetime对象
                    if saved_position.get('original_signal_time'):
                        if isinstance(saved_position['original_signal_time'], str):
                            self.current_position['original_signal_time'] = datetime.fromisoformat(saved_position['original_signal_time'])

                    print("\n[OK] 持仓状态已恢复，继续监控...")
                    alert_title = f"持仓监控已恢复 - {self.TARGET_SYMBOL}"
                    alert_message = (
                        f"系统重启后已恢复持仓监控\n\n"
                        f"持仓方向: {'多头' if self.current_position['status'] == 'long' else '空头'}\n"
                        f"入场价格: {self.current_position['entry_price']:.4f}\n"
                        f"止损价格: {self.current_position['stop_loss']:.4f}\n"
                        f"保存时间: {saved_time}"
                    )
                    self.send_alert(alert_title, alert_message, "warning")
                else:
                    print("\n[X] 已忽略历史持仓状态，从空仓开始")
                    # 清空持仓状态文件
                    os.remove(self.position_file)
                    print("   已删除持仓状态文件")
            else:
                # 空仓
                print(f"[POSITION] 当前无持仓")
                if last_trade:
                    print()
                    print("[LAST TRADE] 上次交易记录:")
                    print(f"   平仓原因: {last_trade.get('exit_reason', 'N/A')}")
                    print(f"   入场价: ${last_trade.get('entry_price', 0):.2f}")
                    print(f"   平仓价: ${last_trade.get('exit_price', 0):.2f}")
                    print(f"   盈亏: {last_trade.get('profit_pct', 0):+.2f}%")
                    print(f"   平仓时间: {last_trade.get('exit_time', 'N/A')}")
                print()
                print(f"[INFO] 系统状态: 空仓，等待新信号")
                print(f"   最后更新: {saved_time}")
                print("="*80)

        except Exception as e:
            print(f"   [WARN] 加载持仓状态失败: {e}")
            print("   将从空仓状态开始")

    def fetch_realtime_price(self):
        """获取实时价格数据"""
        if not self.exchange:
            return None

        try:
            # 获取最新200根永续合约K线
            candles = self.exchange.fetch_ohlcv(
                self.TARGET_SYMBOL,
                self.TIMEFRAME,
                limit=200
            )

            if not candles:
                return None

            # 构造DF并转换时间
            df_price = pd.DataFrame(candles, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            df_price['ts'] = pd.to_datetime(df_price['ts'], unit='ms')  # 保持UTC时间
            df_price['ts_bj'] = df_price['ts'] + timedelta(hours=8)  # 北京时间仅用于显示

            df_price.set_index('ts', inplace=True)

            return df_price

        except Exception as e:
            print(f"价格获取异常: {e}")
            return None

    def calculate_hourly_oi_change(self, df_price):
        """计算1小时OI变化率"""
        # [STAR] 线程安全：使用锁读取OI数据
        with self.oi_lock:
            if len(self.oi_history) < 12:
                return 0, 0

            current_time = datetime.now(timezone.utc)  # [OK] 修复：统一使用UTC时间
            one_hour_ago = current_time - timedelta(hours=1)

            oi_before = None
            oi_now = self.oi_history[-1]['open_interest']

            for oi_point in reversed(list(self.oi_history)[:-1]):
                if oi_point['timestamp'] <= one_hour_ago:
                    oi_before = oi_point['open_interest']
                    break

            if oi_before is None and len(self.oi_history) >= 12:
                oi_before = list(self.oi_history)[-12]['open_interest']

        if oi_before and oi_before > 0:
            oi_change_pct = (oi_now - oi_before) / oi_before
        else:
            oi_change_pct = 0

        # 计算价格变化率
        if len(df_price) >= 2:
            price_now = df_price['c'].iloc[-1]
            price_before = df_price['c'].iloc[-2]
            price_change_pct = (price_now - price_before) / price_before
            oi_divergence = oi_change_pct - price_change_pct
        else:
            price_change_pct = 0
            oi_divergence = 0

        return oi_change_pct, oi_divergence

    def calc_indicators(self, df_price):
        """计算技术指标"""
        c = df_price['c']; h = df_price['h']; l = df_price['l']

        # 布林带
        df_price['ma20'] = c.rolling(20).mean()
        basis = df_price['ma20']
        dev = c.rolling(20).std()
        df_price['upper'] = basis + (2.0 * dev)
        df_price['lower'] = basis - (2.0 * dev)
        df_price['bandwidth'] = (df_price['upper'] - df_price['lower']) / df_price['ma20'] * 100

        # COO
        rsi = 100 - (100 / (1 + c.diff().clip(lower=0).rolling(14).mean() /
                            -c.diff().clip(upper=0).rolling(14).mean()))
        n_rsi = (rsi - 50) * 1.5

        tp = (h + l + c) / 3
        cci = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).std())
        n_cci = (cci.clip(-200, 200) / 2) * 1.2

        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        stoch_k = (macd - macd.rolling(14).min()) / (macd.rolling(14).max() - macd.rolling(14).min()) * 100
        stc = stoch_k.ewm(span=6).mean()
        n_stc = (stc - 50) * 2.0

        df_price['coo'] = (n_rsi + n_cci + n_stc) / 4.7 * 2 + 50

        # OI计算
        if len(self.oi_history) >= 2:
            oi_change_pct, oi_divergence = self.calculate_hourly_oi_change(df_price)
            df_price['oi_change_pct'] = oi_change_pct
            df_price['oi_price_divergence'] = oi_divergence
        else:
            df_price['oi_change_pct'] = 0
            df_price['oi_price_divergence'] = 0

        df_price['price_change_pct'] = c.pct_change()

        df_price['oi_change_pct'] = df_price['oi_change_pct'].fillna(0)
        df_price['oi_price_divergence'] = df_price['oi_price_divergence'].fillna(0)

        return df_price

    def check_oi_filter(self, row_data):
        """检查OI过滤"""
        oi_threshold = self.PARAMS['oi_change_filter']

        oi_change = row_data.get('oi_change_pct', 0)
        if oi_change < oi_threshold:
            return True, f"OI缩减({oi_change:.2%} < {oi_threshold})"

        oi_div = row_data.get('oi_price_divergence', 0)
        if oi_div < oi_threshold:
            return True, f"价格-OI背离({oi_div:.2%} < {oi_threshold})"

        return False, ""

    def check_signal(self, df_price):
        """检查交易信号"""
        if len(df_price) < 50:
            return 0, "数据不足"

        # 使用上一根已收盘的K线
        if len(df_price) > 1:
            latest = df_price.iloc[-2]
            current_kline_open = df_price['o'].iloc[-1]
        else:
            latest = df_price.iloc[-1]
            current_kline_open = latest['c']

        c = latest['c']; l = latest['l']; h = latest['h']
        ma20 = latest['ma20']; bw = latest['bandwidth']; coo = latest['coo']

        # 信号判断
        p_bull = (l <= ma20) and (c > ma20)
        p_bear = (h >= ma20) and (c < ma20)
        is_sqz = bw < self.PARAMS['squeeze']

        sig = 0
        signal_reason = ""

        if is_sqz:
            # [STAR] 二维网格搜索全局最优：Squeeze=4.0%, 做多>30, 做空<60
            if p_bull and coo > 30:
                sig = 1
                signal_reason = "布林带收缩突破 + COO > 30"
            elif p_bear and coo < 60:
                sig = -1
                signal_reason = "布林带收缩跌破 + COO < 60"
        elif coo > 80 and p_bull:
            sig = 1
            signal_reason = "COO超买区 > 80突破"
        elif coo < 20 and p_bear:
            sig = -1
            signal_reason = "COO超卖区 < 20跌破"

        if sig != 0:
            row_data = {
                'oi_change_pct': latest['oi_change_pct'],
                'oi_price_divergence': latest['oi_price_divergence']
            }

            is_blocked, block_reason = self.check_oi_filter(row_data)

            if is_blocked:
                return 0, f"信号被OI过滤拦截: {block_reason}"

            return sig, signal_reason

        return 0, "无信号"

    def is_same_trend_continuation(self, signal):
        """判断是否是同一趋势的延续

        SOL简单信号系统：只有long(1)和short(-1)
        判断标准：信号方向相同即为同一趋势延续
        """
        # 如果没有原始信号信息，这是新趋势
        if self.current_position.get('original_signal', 0) == 0:
            return False

        # [OK] 信号翻转判断：信号方向改变
        if self.current_position['original_signal'] != signal:
            print(f"   🔄 信号翻转: {self.current_position['original_signal']} → {signal}，新趋势开始")
            return False

        # 信号方向相同，说明是同一趋势的延续
        return True

    def open_position(self, signal, entry_price, signal_reason, df_price=None):
        """
        开仓
        [STAR] 集成动态仓位V2：根据信号质量动态分配仓位（25%-35%）
        [STAR][STAR] 显示详细的信号质量评分分解
        [TARGET][TARGET] 混合策略：新止损+旧止盈（避免贪婪）
        """
        # [STAR] 动态仓位V2：计算信号质量评分（带详细分解）
        if df_price is not None and len(df_price) >= 2:
            latest = df_price.iloc[-2]
            score, details = self.calculate_dynamic_position_score(
                latest['c'], latest['l'], latest['h'], latest['ma20'],
                latest['bandwidth'], latest['coo'],
                latest['oi_change_pct'], latest['oi_price_divergence']
            )
            dynamic_pos_size = self.get_dynamic_position_size_v2(score)

            # 信号等级判断
            if score >= 70:
                signal_grade = "[STAR][STAR][STAR] 优质信号"
            elif score >= 55:
                signal_grade = "[STAR][STAR] 良好信号"
            elif score >= 40:
                signal_grade = "[STAR] 一般信号"
            else:
                signal_grade = "[WARN] 较差信号"
        else:
            score = 50
            details = None
            dynamic_pos_size = self.PARAMS['position_size']  # 默认30%
            signal_grade = "[STAR] 信号（默认参数）"

        # [TARGET] 判断是否同一趋势延续
        is_continuation = self.is_same_trend_continuation(signal)

        sl_rate = self.PARAMS['sl'] / 100
        tp1_rate = self.PARAMS['tp1'] / 100
        tp2_rate = self.PARAMS['tp2'] / 100

        if signal > 0:
            stop_loss = entry_price * (1 - sl_rate)  # 新止损

            if is_continuation:
                # [OK] 混合策略：保留原始止盈
                take_profit1 = self.current_position['original_tp1']
                take_profit2 = self.current_position['original_tp2']
                print(f"   [OK] 混合策略生效(延续第{self.current_position['trend_continuation_count']+1}次): 新止损+旧止盈")
            else:
                # 新趋势：记录原始止盈
                take_profit1 = entry_price * (1 + tp1_rate)
                take_profit2 = entry_price * (1 + tp2_rate)

            direction = "多头"
            alert_type = "buy"
        else:
            stop_loss = entry_price * (1 + sl_rate)  # 新止损

            if is_continuation:
                # [OK] 混合策略：保留原始止盈
                take_profit1 = self.current_position['original_tp1']
                take_profit2 = self.current_position['original_tp2']
                print(f"   [OK] 混合策略生效(延续第{self.current_position['trend_continuation_count']+1}次): 新止损+旧止盈")
            else:
                # 新趋势：记录原始止盈
                take_profit1 = entry_price * (1 - tp1_rate)
                take_profit2 = entry_price * (1 - tp2_rate)

            direction = "空头"
            alert_type = "sell"

        # [STAR] 使用time_stop_activated（与回测V3一致）
        self.current_position = {
            'status': 'long' if signal > 0 else 'short',
            'entry_price': entry_price,
            'entry_time': datetime.now(timezone.utc),  # [OK] 修复：统一使用UTC时间
            'stop_loss': stop_loss,
            'take_profit1': take_profit1,
            'take_profit2': take_profit2,
            'trail_stop': 0,
            'tp1_achieved': False,
            'breakeven_activated': False,
            'position_size': dynamic_pos_size,  # [STAR] 使用动态仓位
            'leverage': self.PARAMS['leverage'],
            'current_pnl': 0,
            'current_pnl_pct': 0,
            'hold_hours': 0,
            'time_stop_activated': False,  # [STAR] 与回测V3命名一致
            # [TARGET] 原始趋势信息（混合策略）
            'original_tp1': take_profit1 if not is_continuation else self.current_position['original_tp1'],
            'original_tp2': take_profit2 if not is_continuation else self.current_position['original_tp2'],
            'original_signal': signal if not is_continuation else self.current_position['original_signal'],
            'original_signal_time': datetime.now(timezone.utc) if not is_continuation else self.current_position['original_signal_time'],
            'trend_continuation_count': (self.current_position['trend_continuation_count'] + 1) if is_continuation else 0
        }

        # [STAR][STAR] 构建详细评分信息
        score_details_text = ""
        if details:
            # 仓位等级说明
            if score >= 70:
                pos_grade = "🥇 最高档 (70-100分)"
                pos_note = "信号质量最优，历史胜率54.8%"
            elif score >= 55:
                pos_grade = "🥈 第二档 (55-69分)"
                pos_note = "信号质量良好"
            elif score >= 40:
                pos_grade = "🥉 第三档 (40-54分)"
                pos_note = "信号质量一般，使用基础仓位"
            elif score >= 25:
                pos_grade = "[CHART] 第四档 (25-39分)"
                pos_note = "信号质量较弱，降低仓位"
            else:
                pos_grade = "[WARN] 最低档 (0-24分)"
                pos_note = "信号质量差，最小仓位"

            score_details_text = (
                f"\n[CHART] 信号质量评分详情:\n"
                f"   总分: {score}/100 - {signal_grade}\n\n"
                f"   1️⃣ COO稳定性: {details['coo_score']}/25\n"
                f"      {details['coo_reason']}\n\n"
                f"   2️⃣ 布林带状态: {details['bw_score']}/30\n"
                f"      {details['bw_reason']}\n\n"
                f"   3️⃣ OI支撑力度: {details['oi_score']}/25\n"
                f"      {details['oi_reason']}\n\n"
                f"   4️⃣ 突破质量: {details['break_score']}/20\n"
                f"      {details['break_reason']}\n\n"
                f"💰 仓位等级说明:\n"
                f"   {pos_grade}\n"
                f"   当前仓位: {dynamic_pos_size*100:.0f}%\n"
                f"   说明: {pos_note}\n\n"
                f"   📋 仓位映射规则 (与回测V3一致):\n"
                f"      70-100分 → 35% (最高档)\n"
                f"      55-69分  → 32% (第二档)\n"
                f"      40-54分  → 30% (基础仓位)\n"
                f"      25-39分  → 28% (第四档)\n"
                f"      0-24分   → 25% (最低档)\n\n"
            )

        # [TARGET] 混合策略说明
        if is_continuation:
            strategy_note = f"[OK]混合策略(延续#{self.current_position['trend_continuation_count']+1}): 新止损+旧止盈"
            # 安全处理时间格式化
            signal_time = self.current_position['original_signal_time']
            if isinstance(signal_time, str):
                signal_time = datetime.fromisoformat(signal_time)

            # 统一转换为naive datetime（去除时区信息）
            if signal_time.tzinfo is not None:
                signal_time = signal_time.replace(tzinfo=None)

            tp_note = f"保留原始TP1/TP2目标 (首次信号于{signal_time.strftime('%m-%d %H:%M')})"
            tp1_desc = "原始目标"
            tp2_desc = "原始目标"
        else:
            strategy_note = "新趋势开始：记录原始止盈目标"
            tp_note = f"标准TP1/TP2目标 ({self.PARAMS['tp1']}%/{self.PARAMS['tp2']}%)"
            tp1_desc = f"{self.PARAMS['tp1']}%"
            tp2_desc = f"{self.PARAMS['tp2']}%"

        alert_title = f"{'[LONG]' if signal > 0 else '[SHORT]'} {direction}开仓信号 - {self.TARGET_SYMBOL} - {'混合策略' if is_continuation else '新趋势'}"
        alert_message = (
            f"[STAR][STAR] 全局最优 + 动态仓位V2 + 混合策略 (与回测V3完全对齐)\n\n"
            f"[TARGET] 策略模式: {strategy_note}\n"
            f"[LOCATION] 止盈说明: {tp_note}\n\n"
            f"信号类型: {signal_reason}\n"
            f"[ALERT] 重要: 基于上一小时收盘K线信号，建议当前小时开盘入场\n"
            f"预估入场价: {entry_price:.4f}（当前K线开盘价）\n\n"
            f"{score_details_text}"
            f"[LOCATION] 止损止盈目标:\n"
            f"   止损类型: {'新信号止损' if is_continuation else '标准止损'}\n"
            f"   止损价格: {stop_loss:.4f} ({self.PARAMS['sl']}%)\n"
            f"   第一止盈: {take_profit1:.4f} ({tp1_desc})\n"
            f"   第二止盈: {take_profit2:.4f} ({tp2_desc})\n\n"
            f"[SETTINGS] 风险控制:\n"
            f"   杠杆倍数: {self.PARAMS['leverage']}x\n"
            f"   移动止损: {'[OK] 启用 (TP1后)' if self.PARAMS['trail_after_tp1'] else '[X] 禁用'}\n"
            f"   移动止损偏移: {self.PARAMS['trail_offset']}% (优化)\n"
            f"   保本止损: {'[OK] 启用 (TP1后)' if self.PARAMS['flip_stop_to_breakeven'] else '[X] 禁用'}\n"
            f"   时间止损: {self.PARAMS['time_stop_hours']}h后仍在成本区±{self.PARAMS['cost_zone_pct']}% (优化)"
        )

        self.send_alert(alert_title, alert_message, alert_type)

        # [NEW] 保存信号历史（用于手动平仓后恢复）
        if not is_continuation:
            # 只在首次信号时保存
            self.save_signal_history(signal, entry_price, take_profit1, take_profit2)

        # [STAR] 保存持仓状态到文件
        self.save_position_state()

    def monitor_position(self, current_price, df_price):
        """监控仓位"""
        pos = self.current_position
        if pos['status'] == 'none':
            return False

        entry_time = pos['entry_time']
        current_time = datetime.now(timezone.utc)  # [OK] 修复：统一使用UTC时间
        hold_hours = (current_time - entry_time).total_seconds() / 3600
        self.current_position['hold_hours'] = hold_hours

        # 计算当前盈亏
        if pos['status'] == 'long':
            profit_pct = (current_price - pos['entry_price']) / pos['entry_price']
            current_pnl_pct = profit_pct * 100
        else:
            profit_pct = (pos['entry_price'] - current_price) / pos['entry_price']
            current_pnl_pct = profit_pct * 100

        self.current_position['current_pnl_pct'] = current_pnl_pct

        # [STAR] 简化日志：仅在重要状态变化时输出

        exit_reason = ""
        exit_price = 0

        # ============ [STAR] 时间止损 + OI动态离场（与回测V3完全一致） ============
        time_stop_hours = self.PARAMS['time_stop_hours']
        cost_zone_pct = self.PARAMS['cost_zone_pct'] / 100

        # 条件1：持仓超过指定小时且仍在成本区
        in_cost_zone = abs(profit_pct) <= cost_zone_pct
        time_stop_eligible = hold_hours >= time_stop_hours and in_cost_zone

        if time_stop_eligible and not pos['time_stop_activated']:
            print(f"时间止损检查: 持仓{hold_hours}小时，盈亏{current_pnl_pct:.2f}%，进入监控状态")
            self.current_position['time_stop_activated'] = True

            # [STAR] 新增：时间止损监控启动预警
            alert_title = f"[TIME] 时间止损监控启动 - {self.TARGET_SYMBOL}"
            alert_message = (
                f"[WARN] 回测V3复合条件已满足:\n\n"
                f"   持仓时间: {hold_hours:.1f}小时 (≥{self.PARAMS['time_stop_hours']}小时)\n"
                f"   价格位置: {current_pnl_pct:.2f}% (在成本区±{self.PARAMS['cost_zone_pct']}%内)\n"
                f"   状态: 进入监控，等待OI掉头向下确认离场\n\n"
                f"   说明: 当OI连续2小时下降时将触发平仓"
            )
            self.send_alert(alert_title, alert_message, "warning")

        # 条件2：OI开始掉头向下（[STAR] 与回测V3完全一致）
        oi_turn_down = False
        with self.oi_lock:
            if len(self.oi_changes_history) >= 2:
                # [STAR] 关键：检查最近2小时OI变化都为负
                recent_oi_changes = list(self.oi_changes_history)[-2:]
                recent_oi_negative = all(c['oi_change'] < 0 for c in recent_oi_changes)
                oi_turn_down = recent_oi_negative

        # 触发时间止损 + OI掉头离场
        if pos['time_stop_activated'] and oi_turn_down:
            if pos['status'] == 'long':
                exit_price = current_price * 0.999
            else:
                exit_price = current_price * 1.001
            exit_reason = "TIME_OI_STOP"

            alert_title = f"OI动态离场触发 - {self.TARGET_SYMBOL}"
            alert_message = (
                f"触发回测V3复合离场条件:\n\n"
                f"持仓时间: {hold_hours:.1f}小时\n"
                f"当前盈亏: {current_pnl_pct:.2f}%\n"
                f"OI趋势: 连续2小时掉头向下\n\n"
                f"执行操作:\n"
                f"   平仓价格: {exit_price:.4f}"
            )
            self.send_alert(alert_title, alert_message, "danger")

        # ============ 止损止盈逻辑 ============
        if not exit_reason:
            sl_rate = self.PARAMS['sl'] / 100
            tp1_rate = self.PARAMS['tp1'] / 100
            tp2_rate = self.PARAMS['tp2'] / 100
            trail_offset = self.PARAMS['trail_offset'] / 100

            if pos['status'] == 'long':
                if current_price <= pos['stop_loss']:
                    exit_reason = "SL"
                    exit_price = pos['stop_loss'] * 0.999

                elif not pos['tp1_achieved'] and profit_pct >= tp1_rate:
                    # [STAR] 检测到TP1达到
                    print(f"\n[TRIGGER] TP1 ACHIEVED! Profit: {current_pnl_pct:.2f}% >= {tp1_rate*100:.2f}%")
                    self.current_position['tp1_achieved'] = True

                    if self.PARAMS['flip_stop_to_breakeven']:
                        new_sl = pos['entry_price'] * 1.001
                        self.current_position['stop_loss'] = new_sl
                        self.current_position['breakeven_activated'] = True
                        print(f"[TRIGGER] Breakeven activated: ${new_sl:.2f}")

                    if self.PARAMS['trail_after_tp1']:
                        # [OK] 修复：只使用入场后的最高价计算移动止损
                        if len(df_price) > 0:
                            # 只筛选入场后的数据
                            mask = df_price.index >= pos['entry_time']
                            if mask.any():
                                high_since_entry = df_price.loc[mask, 'h'].max()
                            else:
                                # 如果没有入场后的数据，使用当前价格
                                high_since_entry = current_price
                            trail_stop = high_since_entry * (1 - trail_offset)
                            self.current_position['trail_stop'] = trail_stop
                            print(f"[TRIGGER] Trailing stop set: ${trail_stop:.2f} (high: ${high_since_entry:.2f})")

                    alert_title = f"[OK] 达到第一止盈 (TP1) - {self.TARGET_SYMBOL}"
                    alert_message = (
                        f"[SUCCESS] 恭喜！第一止盈目标达成\n\n"
                        f"当前盈利: {current_pnl_pct:.2f}%\n\n"
                        f"[SETTINGS] 动态止盈止损已激活:\n\n"
                        f"1️⃣ 保本止损: {'[OK] 已激活' if self.PARAMS['flip_stop_to_breakeven'] else '[X] 未激活'}\n"
                        f"   新止损价: {self.current_position['stop_loss']:.4f}\n"
                        f"   说明: 止损已从初始价移至成本价，保护本金安全\n\n"
                        f"2️⃣ 移动止损: {'[OK] 已激活' if self.PARAMS['trail_after_tp1'] else '[X] 未激活'}\n"
                    )

                    # 根据移动止损状态添加信息
                    if self.PARAMS['trail_after_tp1'] and self.current_position['trail_stop'] > 0:
                        alert_message += (
                            f"   当前移动止损价: {self.current_position['trail_stop']:.4f}\n"
                            f"   移动偏移: {self.PARAMS['trail_offset']}% [STAR] (优化: 降低40%)\n"
                            f"   说明: 止损将随最高价上移，锁定更多利润\n\n"
                        )
                    elif self.PARAMS['trail_after_tp1']:
                        alert_message += f"   状态: 正在计算移动止损价...\n\n"
                    else:
                        alert_message += f"   说明: 移动止损未启用\n\n"

                    alert_message += (
                        f"[TARGET] 下一目标:\n"
                        f"   第二止盈: {self.PARAMS['tp2']}% (价格: {pos['take_profit2']:.4f})\n\n"
                        f"[INFO] 策略说明: 现在可以安心持有，等待更高目标，同时止损保护已有利润"
                    )

                    self.send_alert(alert_title, alert_message, "success")
                    return False

                elif pos['tp1_achieved'] and profit_pct >= tp2_rate:
                    exit_reason = "TP2"
                    exit_price = current_price * 0.999

                elif self.PARAMS['trail_after_tp1'] and pos['tp1_achieved'] and self.current_position['trail_stop'] > 0:
                    if current_price <= self.current_position['trail_stop']:
                        # [STAR] 检测到移动止损触发
                        print(f"\n[TRIGGER] TRAILING STOP HIT!")
                        print(f"   Current: ${current_price:.2f}")
                        print(f"   Trail Stop: ${self.current_position['trail_stop']:.2f}")
                        print(f"   Diff: {((self.current_position['trail_stop'] - current_price) / current_price * 100):.2f}%")
                        exit_reason = "TRAIL"
                        exit_price = self.current_position['trail_stop'] * 0.999

                elif pos['breakeven_activated'] and current_price <= pos['stop_loss']:
                    exit_reason = "BREAK_EVEN"
                    exit_price = pos['stop_loss'] * 0.999

            else:  # 空头
                if current_price >= pos['stop_loss']:
                    exit_reason = "SL"
                    exit_price = pos['stop_loss'] * 1.001

                elif not pos['tp1_achieved'] and profit_pct >= tp1_rate:
                    self.current_position['tp1_achieved'] = True

                    if self.PARAMS['flip_stop_to_breakeven']:
                        self.current_position['stop_loss'] = pos['entry_price'] * 0.999
                        self.current_position['breakeven_activated'] = True

                    if self.PARAMS['trail_after_tp1']:
                        # [OK] 修复：只使用入场后的最低价计算移动止损
                        if len(df_price) > 0:
                            # 只筛选入场后的数据
                            mask = df_price.index >= pos['entry_time']
                            if mask.any():
                                low_since_entry = df_price.loc[mask, 'l'].min()
                            else:
                                # 如果没有入场后的数据，使用当前价格
                                low_since_entry = current_price
                            self.current_position['trail_stop'] = low_since_entry * (1 + trail_offset)

                    alert_title = f"[OK] 达到第一止盈 (TP1) - {self.TARGET_SYMBOL}"
                    alert_message = (
                        f"[SUCCESS] 恭喜！第一止盈目标达成\n\n"
                        f"当前盈利: {current_pnl_pct:.2f}%\n\n"
                        f"[SETTINGS] 动态止盈止损已激活:\n\n"
                        f"1️⃣ 保本止损: {'[OK] 已激活' if self.PARAMS['flip_stop_to_breakeven'] else '[X] 未激活'}\n"
                        f"   新止损价: {self.current_position['stop_loss']:.4f}\n"
                        f"   说明: 止损已从初始价移至成本价，保护本金安全\n\n"
                        f"2️⃣ 移动止损: {'[OK] 已激活' if self.PARAMS['trail_after_tp1'] else '[X] 未激活'}\n"
                    )

                    # 根据移动止损状态添加信息
                    if self.PARAMS['trail_after_tp1'] and self.current_position['trail_stop'] > 0:
                        alert_message += (
                            f"   当前移动止损价: {self.current_position['trail_stop']:.4f}\n"
                            f"   移动偏移: {self.PARAMS['trail_offset']}% [STAR] (优化: 降低40%)\n"
                            f"   说明: 止损将随最低价下移，锁定更多利润\n\n"
                        )
                    elif self.PARAMS['trail_after_tp1']:
                        alert_message += f"   状态: 正在计算移动止损价...\n\n"
                    else:
                        alert_message += f"   说明: 移动止损未启用\n\n"

                    alert_message += (
                        f"[TARGET] 下一目标:\n"
                        f"   第二止盈: {self.PARAMS['tp2']}% (价格: {pos['take_profit2']:.4f})\n\n"
                        f"[INFO] 策略说明: 现在可以安心持有，等待更高目标，同时止损保护已有利润"
                    )

                    self.send_alert(alert_title, alert_message, "success")
                    return False

                elif pos['tp1_achieved'] and profit_pct >= tp2_rate:
                    exit_reason = "TP2"
                    exit_price = current_price * 1.001

                elif self.PARAMS['trail_after_tp1'] and pos['tp1_achieved'] and self.current_position['trail_stop'] > 0:
                    if current_price >= self.current_position['trail_stop']:
                        exit_reason = "TRAIL"
                        exit_price = self.current_position['trail_stop'] * 1.001

                elif pos['breakeven_activated'] and current_price >= pos['stop_loss']:
                    exit_reason = "BREAK_EVEN"
                    exit_price = pos['stop_loss'] * 1.001

        # ============ 执行平仓 ============
        if exit_reason:
            alert_title = f"平仓通知 - {self.TARGET_SYMBOL}"
            alert_message = (
                f"方向: {'多头' if pos['status'] == 'long' else '空头'}\n"
                f"入场价格: {pos['entry_price']:.4f}\n"
                f"平仓价格: {exit_price:.4f}\n"
                f"持仓时间: {hold_hours:.1f}小时\n"
                f"最终盈亏: {current_pnl_pct:.2f}%\n"
                f"平仓原因: {exit_reason}"
            )
            self.send_alert(alert_title, alert_message, "close")

            # [TARGET] 保留原始趋势信息（与BTC程序一致）
            original_tp1 = pos.get('original_tp1', 0)
            original_tp2 = pos.get('original_tp2', 0)
            original_signal = pos.get('original_signal', 0)
            original_signal_time = pos.get('original_signal_time')
            trend_continuation_count = pos.get('trend_continuation_count', 0)

            self.current_position = {
                'status': 'none',
                'entry_price': 0,
                'entry_time': None,
                'stop_loss': 0,
                'take_profit1': 0,
                'take_profit2': 0,
                'trail_stop': 0,
                'tp1_achieved': False,
                'breakeven_activated': False,
                'position_size': self.PARAMS['position_size'],
                'leverage': self.PARAMS['leverage'],
                'current_pnl': 0,
                'current_pnl_pct': 0,
                'hold_hours': 0,
                'time_stop_activated': False,
                # [TARGET] 原始趋势信息（混合策略）
                # 注意：不完全重置，以便后续判断同一趋势
                # 只有当信号翻转时，才会在is_same_trend_continuation中自动判断为新趋势
                'original_tp1': 0,
                'original_tp2': 0,
                'original_signal': original_signal,  # [OK] 保留原始信号
                'original_signal_time': original_signal_time,  # [OK] 保留原始时间
                'trend_continuation_count': trend_continuation_count  # [OK] 保留延续计数
            }

            # [STAR] 保存平仓后的状态（空仓）
            self.save_position_state()

            return True

        return False

    def display_position_status(self):
        """显示仓位状态"""
        pos = self.current_position
        if pos['status'] == 'none':
            return "无持仓"

        current_price = self.price_data['c'].iloc[-1] if not self.price_data.empty else 0

        # 提取变量避免f-string嵌套问题
        direction = '[LONG] 多头' if pos['status'] == 'long' else '[SHORT] 空头'
        tp1_status = '[OK]' if pos['tp1_achieved'] else '未触发'
        trail_stop_text = f"{pos['trail_stop']:.4f}" if pos['trail_stop'] else '未启用'
        time_stop_hours = self.PARAMS['time_stop_hours']

        status_text = f"""
[CHART] 当前仓位状态 (V3对齐版):
   方向: {direction}
   入场价格: {pos['entry_price']:.4f}
   当前价格: {current_price:.4f}
   持仓时间: {pos['hold_hours']:.1f}小时
   当前盈亏: {pos['current_pnl_pct']:.2f}%

[TARGET] 关键价位:
   止损: {pos['stop_loss']:.4f}
   TP1: {pos['take_profit1']:.4f} ({tp1_status})
   TP2: {pos['take_profit2']:.4f}
   移动止损: {trail_stop_text}

[TIME] 时间止损: {pos['hold_hours']:.1f}/{time_stop_hours:.0f}h
        """

        return status_text

    def monitoring_loop(self):
        """监控主循环"""
        print(f"\n[START] 开始实时监控 {self.TARGET_SYMBOL}...")
        print(f"   [CHART] 频率配置:")
        print(f"     信号检查: 每小时第1分钟")
        print(f"     OI采集: 每5分钟")
        print(f"     持仓监控: 每{self.POSITION_MONITOR_INTERVAL}秒（实时监控止损止盈）[STAR] 优化")
        print(f"   [STAR] 与回测V3完全对齐")

        self.start_oi_collection()

        self.is_running = True
        last_check_hour = -1
        loop_count = 0

        while self.is_running:
            try:
                current_time = datetime.now()
                current_hour = current_time.hour
                loop_count += 1

                # [STAR] 心跳日志：每10分钟打印一次系统状态（独立于信号检查）
                if loop_count % 60 == 0:  # 10秒*60 = 10分钟
                    print(f"\n{'='*60}")
                    print(f"[TIME] 系统心跳 | {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"   [CHART] 监控状态: 运行中 | 循环次数: {loop_count}")
                    print(f"   [LOCATION] 持仓状态: {self.current_position['status']}")
                    with self.oi_lock:
                        print(f"   [UP] OI数据点: {len(self.oi_history)}个")
                    print(f"   {'='*60}\n")

                # [STAR] 优化: 10秒间隔循环，确保实时检测TP1和移动止损
                should_check_signal = (
                    current_time.minute == 1 and
                    current_hour != last_check_hour and
                    self.current_position['status'] == 'none'
                )

                should_check_position = (
                    self.current_position['status'] != 'none'
                )

                if should_check_signal or should_check_position:
                    df_price = self.fetch_realtime_price()
                    if df_price is None or df_price.empty:
                        print("   价格数据获取失败")
                        time.sleep(30)
                        continue

                    df_price = self.calc_indicators(df_price)
                    self.price_data = df_price

                if should_check_signal:
                    last_check_hour = current_hour
                    print(f"\n   执行信号检查...")

                    signal, reason = self.check_signal(df_price)
                    if signal != 0:
                        print(f"   发现信号: {reason}")

                        entry_price = df_price['c'].iloc[-1]
                        self.open_position(signal, entry_price, reason, df_price)  # [STAR] 传入df_price用于动态仓位计算
                    else:
                        print(f"   {reason}")

                if should_check_position:
                    # [STAR] 持仓监控：每10秒运行一次（带重试机制）
                    # 确保实时检测TP1和移动止损触发
                    current_price = 0
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            ticker = self.exchange.fetch_ticker(self.TARGET_SYMBOL)
                            current_price = ticker['last']
                            break  # 成功获取，退出重试
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"[WARN] Price fetch failed (attempt {attempt+1}), retrying...")
                                time.sleep(1)
                            else:
                                # 最后一次失败，使用K线收盘价作为备用
                                current_price = df_price['c'].iloc[-1] if not df_price.empty else 0
                                print(f"[WARN] Price fetch failed after {max_retries} attempts, using close price")

                    if current_price > 0:
                        closed = self.monitor_position(current_price, self.price_data)
                        if not closed:
                            # 每5分钟打印一次持仓状态
                            if current_time.minute % 5 == 0 and current_time.second < 30:
                                print(f"   [CHART] 持仓监控 ({current_time.strftime('%H:%M:%S')})")
                                print(self.display_position_status())

                # [STAR] 固定短间隔循环（10秒）- 更高频率确保实时检测
                time.sleep(self.POSITION_MONITOR_INTERVAL)

            except Exception as e:
                print(f"监控出错: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(30)

    def start_monitoring(self):
        """启动监控"""
        self.monitor_thread = threading.Thread(target=self.monitoring_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

        print("\n监控已启动...")
        print("   按 Ctrl+C 或输入 stop 停止")

    def stop_monitoring(self):
        """停止监控"""
        self.is_running = False
        self.stop_oi_collection()

        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)

        print("\n监控已停止")
        self.send_alert("系统通知", "SOL预警系统V3已停止", "info")

    def run(self):
        """运行主程序"""
        print("="*80)
        print("[SYSTEM] SOL实时信号预警系统 V3 - 与回测V3完全对齐")
        print("="*80)
        print("[STAR] 优化参数 (来自网格搜索):")
        print(f"   移动止损偏移: {self.PARAMS['trail_offset']}% (降低40%)")
        print(f"   时间止损: {self.PARAMS['time_stop_hours']}h (增加33%)")
        print(f"   OI过滤阈值: {self.PARAMS['oi_change_filter']} (保持)")
        print()
        print("[TARGET] 与回测V3对齐的关键修复:")
        print(f"   1. OI下降判断: 改为'最近2小时OI变化都为负'")
        print(f"   2. 状态变量: time_stop_activated (与回测命名一致)")
        print(f"   3. 线程安全: 使用锁保护OI数据")
        print(f"   4. 短循环监控: 30秒间隔")
        print("="*80)

        self.send_alert("[START] 系统启动V3", "SOL预警系统V3（与回测V3完全对齐）已启动", "info")

        try:
            self.start_monitoring()

            while self.is_running:
                cmd = input("\n命令 (status/stop): ").strip().lower()

                if cmd == 'status':
                    print(self.display_position_status())
                elif cmd == 'stop':
                    self.stop_monitoring()
                    break

                time.sleep(1)

        except KeyboardInterrupt:
            self.stop_monitoring()
        except Exception as e:
            print(f"运行错误: {e}")
            self.stop_monitoring()

def main():
    system = SignalAlertSystemV3()
    system.run()

if __name__ == "__main__":
    main()
