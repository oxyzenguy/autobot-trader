from utils.telegram_util import send_telegram, rest_market_sell
import pyupbit

def handle_command(cmd, upbit, STRATEGY_BUDGETS, POSITION_HISTORY, REALIZED_PNL):
    cmd = cmd.strip()

    if cmd == "/잔고":
        krw = upbit.get_balance("KRW")
        msg = f"💰 현재 KRW 잔고: {krw:,.0f}원\n\n📦 보유 코인:\n"
        for sym in ["KRW-BTC", "KRW-ETH", "KRW-TRX"]:
            bal = upbit.get_balance(sym)
            if bal and bal > 0:
                msg += f"- {sym}: {bal:.4f}\n"
        send_telegram(msg)

    elif cmd == "/실현손익":
        if not REALIZED_PNL:
            send_telegram("📉 아직 실현된 손익이 없습니다.")
            return
        msg = "📊 실현 손익:\n"
        for strategy, pnl in REALIZED_PNL.items():
            msg += f"- {strategy}: {pnl:.2f}%\n"
        send_telegram(msg)

    elif cmd == "/예산":
        msg = "💼 전략별 예산 설정:\n"
        for strategy, budget in STRATEGY_BUDGETS.items():
            msg += f"- {strategy}: {budget:,}원\n"
        send_telegram(msg)

    elif cmd == "/포지션":
        if not POSITION_HISTORY:
            send_telegram("📂 현재 보유 포지션이 없습니다.")
            return
        msg = "📌 현재 포지션:\n"
        for k, (entry_price, volume) in POSITION_HISTORY.items():
            symbol = k.split(":")[-1]
            cur_price = pyupbit.get_current_price(symbol)
            pnl = ((cur_price - entry_price) / entry_price) * 100
            msg += f"{symbol}: {volume:.4f}개 @ {entry_price:.0f}원 → {pnl:.2f}%\n"
        send_telegram(msg)

    elif cmd == "/전략":
        msg = "⚙️ 현재 사용 중인 전략:\n" + "\n".join(f"- {s}" for s in STRATEGY_BUDGETS)
        msg += "\n\n💬 사용 가능한 명령:\n/잔고 /포지션 /실현손익 /예산 /전부매도 /정지"
        send_telegram(msg)

    elif cmd == "/정지":
        send_telegram("🛑 자동매매 루프를 수동 종료하세요. (Ctrl+C or 프로세스 종료)")

    elif cmd == "/전부매도":
        if not POSITION_HISTORY:
            send_telegram("📂 현재 보유 포지션이 없습니다.")
            return

        msg = "🧨 <b>전 포지션 시장가 매도 시작</b>\n"
        for key, (buy_price, volume) in list(POSITION_HISTORY.items()):
            ticker = key.split(":")[-1]
            cur_price = pyupbit.get_current_price(ticker)
            pnl = (cur_price - buy_price) / buy_price

            result = rest_market_sell(ticker, volume)
            if result and float(result.get("executed_volume", 0)) > 0:
                msg += f"\n✅ {ticker} 매도 완료\n수익률: {pnl*100:.2f}%"
                REALIZED_PNL[key] += pnl * 100
                POSITION_HISTORY.pop(key, None)
            else:
                msg += f"\n⚠️ {ticker} 매도 실패 또는 미체결"

        send_telegram(msg)
