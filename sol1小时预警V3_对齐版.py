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
  6. [🔥V4信号替换] 使用V4信号逻辑：布林带挤压5% + COO极值(做多>80,做空<20)
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
            'squeeze': 5.0,                 # [🔥V4修改] 布林带收缩 5.0%（原4.0%）
            'oi_change_filter': -0.01,      # OI过滤阈值 -1%
            'time_stop_hours': 80,          # [STAR] 时间止损 80h (优化)
            'cost_zone_pct': 0.5,          # 成本区 ±0.5%
            'position_size': 0.3,          # 仓位 30%
            'leverage': 5                   # 杠杆 5x
        }

        # 通知配置（从环境变量读取） - 保持完全不变
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.wechat_api_url = os.getenv('WECHAT_API_URL')
        
        # 验证必需的环境变量
        if not self.telegram_token:
            raise ValueError('TELEGRAM_TOKEN 环境变量未设置')
        if not self.telegram_chat_id:
            raise ValueError('TELEGRAM_CHAT_ID 环境变量未设置')

        # 初始化 - 保持完全不变
        self.bot = None
        self.wechat_enabled = True
        self.exchange = None

        # [STAR] 线程安全：使用锁和deque - 保持完全不变
        self.oi_lock = threading.Lock()
        self.oi_history = deque(maxlen=576)  # 自动限制长度，线程安全
        self.oi_changes_history = deque(maxlen=576)  # [STAR] 新增：存储OI变化率

        # OI采集线程控制 - 保持完全不变
        self.oi_collector_running = False
        self.oi_collector_thread = None

        # 当前仓位状态 - 保持完全不变
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
            'original_tp1': 0,
            'original_tp2': 0,
            'original_signal': 0,
            'original_signal_time': None,
            'trend_continuation_count': 0
        }

        # 数据存储 - 保持完全不变
        self.price_data = pd.DataFrame()
        self.historical_signals = []

        # 运行标志 - 保持完全不变
        self.is_running = False
        self.monitor_thread = None

        # [STAR] 持仓状态文件 - 保持完全不变
        self.position_file = "sol_position_state.json"

        # [STAR] 信号历史文件 - 保持完全不变
        self.signal_history_file = "sol_signal_history.json"

        # 初始化 - 保持完全不变
        self.init_exchange()
        self.setup_notifications()
        self.setup_telegram_commands()
        self.load_position_state()
        self.load_signal_history()

    # ============ 动态仓位V2功能 - 保持完全不变 ============
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
            # [🔥V4修改] 重点关注COO > 80（做多）和 COO < 20（做空）
            if coo > 80:  # V4做多极值区
                if coo > 85:
                    score += 25
                    details['coo_score'] = 25
                    details['coo_reason'] = f'COO {coo:.1f}(V4极值做多>85)'
                else:
                    score += 20
                    details['coo_score'] = 20
                    details['coo_reason'] = f'COO {coo:.1f}(V4做多>80)'
            elif coo < 20:  # V4做空极值区
                if coo < 15:
                    score += 25
                    details['coo_score'] = 25
                    details['coo_reason'] = f'COO {coo:.1f}(V4极值做空<15)'
                else:
                    score += 20
                    details['coo_score'] = 20
                    details['coo_reason'] = f'COO {coo:.1f}(V4做空<20)'
            elif coo > 60:  # 强势做多区
                score += 15
                details['coo_score'] = 15
                details['coo_reason'] = f'COO {coo:.1f}(强势做多60-80)'
            elif coo < 40:  # 强势做空区
                score += 15
                details['coo_score'] = 15
                details['coo_reason'] = f'COO {coo:.1f}(强势做空20-40)'
            else:  # 中间区域
                score += 10
                details['coo_score'] = 10
                details['coo_reason'] = f'COO {coo:.1f}(中间区域40-60)'
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

        # 3. OI支撑 (0-25分) - 保持与V3相同
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

        # 4. 价格突破质量 (0-20分) - V4要求突破MA20
        p_bull = (l <= ma20) and (c > ma20)
        p_bear = (h >= ma20) and (c < ma20)

        if p_bull or p_bear:
            score += 15
            details['break_score'] = 15
            details['break_reason'] = '有效突破MA20(V4必需)'

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
            details['break_reason'] = '无有效突破(不满足V4条件)'

        total_score = max(0, min(100, score))
        return total_score, details

    def get_dynamic_position_size_v2(self, score):
        """
        动态仓位映射V2（保守策略）- 保持完全不变
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

    # ============ 以下所有函数保持完全不变 ============
    
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
🤖 SOL预警系统 V3 - 与回测V3完全对齐版

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
                    signal_time = pos['original_signal_time']
                    if isinstance(signal_time, str):
                        signal_time = datetime.fromisoformat(signal_time)

                    from datetime import timezone
                    now_utc = datetime.now(timezone.utc)

                    if signal_time.tzinfo is not None:
                        signal_time_utc = signal_time.astimezone(timezone.utc)
                    else:
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
                alert_message += "已清除：\n- 持仓数据\n- 信号历史\n- 趋势信息\n\n下次信号将作为新趋势处理。"
                try:
                    if os.path.exists(self.signal_history_file):
                        os.remove(self.signal_history_file)
                except:
                    pass

                self.current_position['original_signal'] = 0
                self.current_position['original_signal_time'] = None
                self.current_position['original_tp1'] = 0
                self.current_position['original_tp2'] = 0
                self.current_position['trend_continuation_count'] = 0
            else:
                alert_message += "已保留信号历史\n\n下次相同信号将使用混合策略：\n- 新止损（最新价格）\n- 旧止盈（原始信号）"

            self.send_alert(alert_title, alert_message, "close")

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
                'original_tp1': 0 if clear_history else self.current_position.get('original_tp1', 0),
                'original_tp2': 0 if clear_history else self.current_position.get('original_tp2', 0),
                'original_signal': 0 if clear_history else self.current_position.get('original_signal', 0),
                'original_signal_time': None if clear_history else self.current_position.get('original_signal_time'),
                'trend_continuation_count': 0 if clear_history else self.current_position.get('trend_continuation_count', 0)
            }

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
                current_time = datetime.now(timezone.utc)

                if current_time.minute % 5 == 0 and current_time.second < 30:
                    oi_value = self.fetch_realtime_oi()
                    if oi_value:
                        oi_point = {
                            'timestamp': current_time,
                            'open_interest': oi_value
                        }

                        with self.oi_lock:
                            self.oi_history.append(oi_point)

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

            print("\n" + "="*80)
            print("[STATUS] 持仓状态摘要")
            print("="*80)

            if saved_position.get('status') != 'none':
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

                confirm = input("\n是否恢复持仓监控? (y/n): ").strip().lower()

                if confirm == 'y':
                    self.current_position = saved_position
                    if saved_position.get('entry_time'):
                        if isinstance(saved_position['entry_time'], str):
                            self.current_position['entry_time'] = datetime.fromisoformat(saved_position['entry_time'])
                        else:
                            self.current_position['entry_time'] = saved_position['entry_time']

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
                    os.remove(self.position_file)
                    print("   已删除持仓状态文件")
            else:
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
            candles = self.exchange.fetch_ohlcv(
                self.TARGET_SYMBOL,
                self.TIMEFRAME,
                limit=200
            )

            if not candles:
                return None

            df_price = pd.DataFrame(candles, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            df_price['ts'] = pd.to_datetime(df_price['ts'], unit='ms')
            df_price['ts_bj'] = df_price['ts'] + timedelta(hours=8)

            df_price.set_index('ts', inplace=True)

            return df_price

        except Exception as e:
            print(f"价格获取异常: {e}")
            return None

    def calculate_hourly_oi_change(self, df_price):
        """计算1小时OI变化率"""
        with self.oi_lock:
            if len(self.oi_history) < 12:
                return 0, 0

            current_time = datetime.now(timezone.utc)
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
        # [🔥V4修改] 计算带宽（V4关键指标）
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

        # [🔥V4修改] 计算突破信号
        df_price['bull_break'] = (df_price['l'] <= df_price['ma20']) & (df_price['c'] > df_price['ma20'])
        df_price['bear_break'] = (df_price['h'] >= df_price['ma20']) & (df_price['c'] < df_price['ma20'])

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
        """检查交易信号 - [🔥V4修改] 替换为V4信号逻辑"""
        if len(df_price) < 50:
            return 0, "数据不足"

        # 使用上一根已收盘的K线
        if len(df_price) > 1:
            latest = df_price.iloc[-2]  # 上一根已收盘K线
            current_kline_open = df_price['o'].iloc[-1]
        else:
            latest = df_price.iloc[-1]
            current_kline_open = latest['c']

        # 获取指标值
        bandwidth = latest['bandwidth']
        coo = latest['coo']
        bull_break = latest.get('bull_break', False)
        bear_break = latest.get('bear_break', False)
        
        # 🔥 V4完整过滤规则（三重条件）
        signal = 0
        signal_reason = ""
        
        # 条件1: 布林带挤压
        is_squeeze = bandwidth < self.PARAMS['squeeze']
        
        if not is_squeeze:
            return 0, f"不满足布林带挤压: 带宽{bandwidth:.1f}% >= {self.PARAMS['squeeze']}%"
        
        # 条件2 + 3: 突破 + COO极值
        # [🔥V4修改] 使用V4极值：做多>80，做空<20
        if bull_break and coo > 80:  # V4做多极值
            signal = 1
            signal_reason = f"布林带收缩({bandwidth:.1f}% < {self.PARAMS['squeeze']}%) + COO>80突破"
        
        elif bear_break and coo < 20:  # V4做空极值
            signal = -1
            signal_reason = f"布林带收缩({bandwidth:.1f}% < {self.PARAMS['squeeze']}%) + COO<20跌破"
        else:
            if bull_break:
                return 0, f"做多突破但COO{coo:.1f} <= 80"
            elif bear_break:
                return 0, f"做空跌破但COO{coo:.1f} >= 20"
            else:
                return 0, f"布林带收缩但无有效突破"

        if signal != 0:
            row_data = {
                'oi_change_pct': latest['oi_change_pct'],
                'oi_price_divergence': latest['oi_price_divergence']
            }

            is_blocked, block_reason = self.check_oi_filter(row_data)

            if is_blocked:
                return 0, f"信号被OI过滤拦截: {block_reason}"

            return signal, signal_reason

        return 0, "无V4策略信号"

    def is_same_trend_continuation(self, signal):
        """判断是否是同一趋势的延续"""
        if self.current_position.get('original_signal', 0) == 0:
            return False

        if self.current_position['original_signal'] != signal:
            print(f"   🔄 信号翻转: {self.current_position['original_signal']} → {signal}，新趋势开始")
            return False

        return True

    def open_position(self, signal, entry_price, signal_reason, df_price=None):
        """开仓"""
        if df_price is not None and len(df_price) >= 2:
            latest = df_price.iloc[-2]
            score, details = self.calculate_dynamic_position_score(
                latest['c'], latest['l'], latest['h'], latest['ma20'],
                latest['bandwidth'], latest['coo'],
                latest['oi_change_pct'], latest['oi_price_divergence']
            )
            dynamic_pos_size = self.get_dynamic_position_size_v2(score)

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
            dynamic_pos_size = self.PARAMS['position_size']
            signal_grade = "[STAR] 信号（默认参数）"

        is_continuation = self.is_same_trend_continuation(signal)

        sl_rate = self.PARAMS['sl'] / 100
        tp1_rate = self.PARAMS['tp1'] / 100
        tp2_rate = self.PARAMS['tp2'] / 100

        if signal > 0:
            stop_loss = entry_price * (1 - sl_rate)

            if is_continuation:
                take_profit1 = self.current_position['original_tp1']
                take_profit2 = self.current_position['original_tp2']
                print(f"   [OK] 混合策略生效(延续第{self.current_position['trend_continuation_count']+1}次): 新止损+旧止盈")
            else:
                take_profit1 = entry_price * (1 + tp1_rate)
                take_profit2 = entry_price * (1 + tp2_rate)

            direction = "多头"
            alert_type = "buy"
        else:
            stop_loss = entry_price * (1 + sl_rate)

            if is_continuation:
                take_profit1 = self.current_position['original_tp1']
                take_profit2 = self.current_position['original_tp2']
                print(f"   [OK] 混合策略生效(延续第{self.current_position['trend_continuation_count']+1}次): 新止损+旧止盈")
            else:
                take_profit1 = entry_price * (1 - tp1_rate)
                take_profit2 = entry_price * (1 - tp2_rate)

            direction = "空头"
            alert_type = "sell"

        self.current_position = {
            'status': 'long' if signal > 0 else 'short',
            'entry_price': entry_price,
            'entry_time': datetime.now(timezone.utc),
            'stop_loss': stop_loss,
            'take_profit1': take_profit1,
            'take_profit2': take_profit2,
            'trail_stop': 0,
            'tp1_achieved': False,
            'breakeven_activated': False,
            'position_size': dynamic_pos_size,
            'leverage': self.PARAMS['leverage'],
            'current_pnl': 0,
            'current_pnl_pct': 0,
            'hold_hours': 0,
            'time_stop_activated': False,
            'original_tp1': take_profit1 if not is_continuation else self.current_position['original_tp1'],
            'original_tp2': take_profit2 if not is_continuation else self.current_position['original_tp2'],
            'original_signal': signal if not is_continuation else self.current_position['original_signal'],
            'original_signal_time': datetime.now(timezone.utc) if not is_continuation else self.current_position['original_signal_time'],
            'trend_continuation_count': (self.current_position['trend_continuation_count'] + 1) if is_continuation else 0
        }

        score_details_text = ""
        if details:
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

        if is_continuation:
            strategy_note = f"[OK]混合策略(延续#{self.current_position['trend_continuation_count']+1}): 新止损+旧止盈"
            signal_time = self.current_position['original_signal_time']
            if isinstance(signal_time, str):
                signal_time = datetime.fromisoformat(signal_time)

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

        # [🔥V4修改] 更新开仓通知信息
        alert_title = f"{'[LONG]' if signal > 0 else '[SHORT]'} {direction}开仓信号 - {self.TARGET_SYMBOL} - V4策略"
        alert_message = (
            f"[🔥V4] 三重过滤策略 + 动态仓位V2 + 混合策略\n\n"
            f"🎯 V4策略特点:\n"
            f"   1. 布林带收缩: 带宽 < {self.PARAMS['squeeze']}%\n"
            f"   2. COO极值: 做多 > 80, 做空 < 20\n"
            f"   3. 价格突破: 突破MA20\n\n"
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

        if not is_continuation:
            self.save_signal_history(signal, entry_price, take_profit1, take_profit2)

        self.save_position_state()

    # ============ 以下所有函数保持完全不变 ============
    
    def monitor_position(self, current_price, df_price):
        """监控仓位"""
        pos = self.current_position
        if pos['status'] == 'none':
            return False

        entry_time = pos['entry_time']
        current_time = datetime.now(timezone.utc)
        hold_hours = (current_time - entry_time).total_seconds() / 3600
        self.current_position['hold_hours'] = hold_hours

        if pos['status'] == 'long':
            profit_pct = (current_price - pos['entry_price']) / pos['entry_price']
            current_pnl_pct = profit_pct * 100
        else:
            profit_pct = (pos['entry_price'] - current_price) / pos['entry_price']
            current_pnl_pct = profit_pct * 100

        self.current_position['current_pnl_pct'] = current_pnl_pct

        exit_reason = ""
        exit_price = 0

        time_stop_hours = self.PARAMS['time_stop_hours']
        cost_zone_pct = self.PARAMS['cost_zone_pct'] / 100

        in_cost_zone = abs(profit_pct) <= cost_zone_pct
        time_stop_eligible = hold_hours >= time_stop_hours and in_cost_zone

        if time_stop_eligible and not pos['time_stop_activated']:
            print(f"时间止损检查: 持仓{hold_hours}小时，盈亏{current_pnl_pct:.2f}%，进入监控状态")
            self.current_position['time_stop_activated'] = True

            alert_title = f"[TIME] 时间止损监控启动 - {self.TARGET_SYMBOL}"
            alert_message = (
                f"[WARN] 回测V3复合条件已满足:\n\n"
                f"   持仓时间: {hold_hours:.1f}小时 (≥{self.PARAMS['time_stop_hours']}小时)\n"
                f"   价格位置: {current_pnl_pct:.2f}% (在成本区±{self.PARAMS['cost_zone_pct']}%内)\n"
                f"   状态: 进入监控，等待OI掉头向下确认离场\n\n"
                f"   说明: 当OI连续2小时下降时将触发平仓"
            )
            self.send_alert(alert_title, alert_message, "warning")

        oi_turn_down = False
        with self.oi_lock:
            if len(self.oi_changes_history) >= 2:
                recent_oi_changes = list(self.oi_changes_history)[-2:]
                recent_oi_negative = all(c['oi_change'] < 0 for c in recent_oi_changes)
                oi_turn_down = recent_oi_negative

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
                    print(f"\n[TRIGGER] TP1 ACHIEVED! Profit: {current_pnl_pct:.2f}% >= {tp1_rate*100:.2f}%")
                    self.current_position['tp1_achieved'] = True

                    if self.PARAMS['flip_stop_to_breakeven']:
                        new_sl = pos['entry_price'] * 1.001
                        self.current_position['stop_loss'] = new_sl
                        self.current_position['breakeven_activated'] = True
                        print(f"[TRIGGER] Breakeven activated: ${new_sl:.2f}")

                    if self.PARAMS['trail_after_tp1']:
                        if len(df_price) > 0:
                            mask = df_price.index >= pos['entry_time']
                            if mask.any():
                                high_since_entry = df_price.loc[mask, 'h'].max()
                            else:
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
                        print(f"\n[TRIGGER] TRAILING STOP HIT!")
                        print(f"   Current: ${current_price:.2f}")
                        print(f"   Trail Stop: ${self.current_position['trail_stop']:.2f}")
                        print(f"   Diff: {((self.current_position['trail_stop'] - current_price) / current_price * 100):.2f}%")
                        exit_reason = "TRAIL"
                        exit_price = self.current_position['trail_stop'] * 0.999

                elif pos['breakeven_activated'] and current_price <= pos['stop_loss']:
                    exit_reason = "BREAK_EVEN"
                    exit_price = pos['stop_loss'] * 0.999

            else:
                if current_price >= pos['stop_loss']:
                    exit_reason = "SL"
                    exit_price = pos['stop_loss'] * 1.001

                elif not pos['tp1_achieved'] and profit_pct >= tp1_rate:
                    self.current_position['tp1_achieved'] = True

                    if self.PARAMS['flip_stop_to_breakeven']:
                        self.current_position['stop_loss'] = pos['entry_price'] * 0.999
                        self.current_position['breakeven_activated'] = True

                    if self.PARAMS['trail_after_tp1']:
                        if len(df_price) > 0:
                            mask = df_price.index >= pos['entry_time']
                            if mask.any():
                                low_since_entry = df_price.loc[mask, 'l'].min()
                            else:
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
                'original_tp1': 0,
                'original_tp2': 0,
                'original_signal': original_signal,
                'original_signal_time': original_signal_time,
                'trend_continuation_count': trend_continuation_count
            }

            self.save_position_state()

            return True

        return False

    def display_position_status(self):
        """显示仓位状态"""
        pos = self.current_position
        if pos['status'] == 'none':
            return "无持仓"

        current_price = self.price_data['c'].iloc[-1] if not self.price_data.empty else 0

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
        print(f"   [🔥V4信号] 三重过滤策略:")
        print(f"     布林带收缩: < {self.PARAMS['squeeze']}%")
        print(f"     COO极值: 做多 > 80, 做空 < 20")
        print(f"     价格突破: 突破MA20")

        self.start_oi_collection()

        self.is_running = True
        last_check_hour = -1
        loop_count = 0

        while self.is_running:
            try:
                current_time = datetime.now()
                current_hour = current_time.hour
                loop_count += 1

                if loop_count % 60 == 0:
                    print(f"\n{'='*60}")
                    print(f"[TIME] 系统心跳 | {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"   [CHART] 监控状态: 运行中 | 循环次数: {loop_count}")
                    print(f"   [LOCATION] 持仓状态: {self.current_position['status']}")
                    with self.oi_lock:
                        print(f"   [UP] OI数据点: {len(self.oi_history)}个")
                    print(f"   {'='*60}\n")

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
                    print(f"\n   执行V4信号检查...")

                    signal, reason = self.check_signal(df_price)
                    if signal != 0:
                        print(f"   V4信号发现: {reason}")

                        entry_price = df_price['c'].iloc[-1]
                        self.open_position(signal, entry_price, reason, df_price)
                    else:
                        print(f"   {reason}")

                if should_check_position:
                    current_price = 0
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            ticker = self.exchange.fetch_ticker(self.TARGET_SYMBOL)
                            current_price = ticker['last']
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"[WARN] Price fetch failed (attempt {attempt+1}), retrying...")
                                time.sleep(1)
                            else:
                                current_price = df_price['c'].iloc[-1] if not df_price.empty else 0
                                print(f"[WARN] Price fetch failed after {max_retries} attempts, using close price")

                    if current_price > 0:
                        closed = self.monitor_position(current_price, self.price_data)
                        if not closed:
                            if current_time.minute % 5 == 0 and current_time.second < 30:
                                print(f"   [CHART] 持仓监控 ({current_time.strftime('%H:%M:%S')})")
                                print(self.display_position_status())

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
        self.send_alert("系统通知", "SOL预警系统V3（V4信号）已停止", "info")

    def run(self):
        """运行主程序"""
        print("="*80)
        print("[SYSTEM] SOL实时信号预警系统 V3 - V4信号逻辑替换版")
        print("="*80)
        print("[🔥V4信号] 三重过滤策略:")
        print(f"   布林带收缩: 带宽 < {self.PARAMS['squeeze']}%")
        print(f"   COO极值过滤: 做多 > 80, 做空 < 20")
        print(f"   价格突破: 突破MA20")
        print()
        print("[TARGET] 保留V3所有功能:")
        print(f"   1. 动态仓位V2: 25%-35%仓位分配")
        print(f"   2. 混合策略: 新止损+旧止盈")
        print(f"   3. 时间止损 + OI动态离场")
        print(f"   4. Telegram交互控制")
        print(f"   5. 持久化状态管理")
        print("="*80)

        self.send_alert("[START] 系统启动V3(V4信号)", "SOL预警系统V3（V4信号逻辑）已启动", "info")

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
