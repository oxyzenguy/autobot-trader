import os
import importlib.util
import pyupbit
import pandas as pd
from jinja2 import Environment, FileSystemLoader
import matplotlib.pyplot as plt
from datetime import datetime

plt.rcParams['font.family'] = 'Malgun Gothic'

STRATEGY_DIR = r"C:\Users\oxyze\autobot-trader\src\autobot_trader\strategies"
TEMPLATE_DIR = "backtest"
CHART_PATH = os.path.join(TEMPLATE_DIR, "strategy_comparison.png")

def generate_html_report(strategy_results):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("multi_strategy_report_template.html")

    # ✅ 전략명과 날짜 조합
    first_strategy = strategy_results[0]["strategy"]
    run_date = strategy_results[0]["date"]
    safe_name = first_strategy.replace(" ", "_")
    report_filename = f"interactive_report_{safe_name}_{run_date}.html"

    html = template.render(strategy_results=strategy_results, chart_path=os.path.basename(CHART_PATH))
    report_path = os.path.join(TEMPLATE_DIR, report_filename)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ 리포트 저장 완료: {report_path}")

def list_strategies():
    files = [f for f in os.listdir(STRATEGY_DIR) if f.endswith(".py") and f != "__init__.py"]
    print("\n📂 선택 가능한 전략 목록:")
    for idx, fname in enumerate(files, start=1):
        print(f"  {idx}. {fname.replace('.py', '')}")
    print(f"  {len(files)+1}. __init__ → 모든 전략 실행")
    return files

def list_durations():
    options = {1: 365, 2: 730, 3: 1095, 4: 1460, 5: 1825}
    print("\n⏳ 백테스트 기간 선택:")
    for k, v in options.items():
        print(f"  {k}. {k}년 ({v}일)")
    return options

def get_market():
    top9 = [
        ("KRW-BTC", "비트코인"),
        ("KRW-ETH", "이더리움"),
        ("KRW-SOL", "솔라나"),
        ("KRW-XRP", "리플"),
        ("KRW-TRX", "트론"),
    ]
    print("\n📈 백테스트 대상 종목 선택:")
    for i, (symbol, name) in enumerate(top9, 1):
        print(f"  {i}. {symbol} ({name})")
    print("  ⏎ 엔터 → 5개 전체 종목 대상으로 실행")
    choice = input("✅ 종목 번호 선택 (1~9, 엔터=전체): ").strip()
    if not choice:
        return [symbol for symbol, _ in top9]
    elif choice.isdigit() and 1 <= int(choice) <= 9:
        return [top9[int(choice) - 1][0]]
    else:
        print("❌ 잘못된 입력입니다. 기본값 KRW-BTC로 진행합니다.")
        return ["KRW-BTC"]

def load_strategy_module(filepath):
    name = os.path.splitext(os.path.basename(filepath))[0]
    spec = importlib.util.spec_from_file_location(name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def run_backtest(strategy_func, strategy_name, count=365, market="KRW-BTC"):
    import pyupbit
    import pandas as pd
    from datetime import datetime

    df = pyupbit.get_ohlcv(market, interval="day", count=count)
    if df is None or df.empty:
        print("❌ 데이터 로드 실패")
        return []

    signals = strategy_func(df)
    trades = []
    positions = []
    investment = 5000

    for sig in signals:
        i = sig["index"]
        date = df.index[i]
        price = sig["price"]
        action = sig["action"]

        if action == "buy":
            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "type": "buy",
                "price": price
            })
            positions.append({
                "buy_price": price,
                "buy_index": i,
                "buy_date": date
            })
        elif action == "sell" and positions:
            matched = positions.pop(0)
            profit_rate = (price - matched["buy_price"]) / matched["buy_price"]
            earned = round(profit_rate * investment, 2)
            holding_days = (date - matched["buy_date"]).days

            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "type": "sell",
                "price": price,
                "profit": round(profit_rate * 100, 2),
                "earned": earned,
                "holding_days": holding_days
            })

    # 저장
    df_trades = pd.DataFrame(trades)
    df_trades.to_csv(f"C:/Users/oxyze/autobot-trader/backtest/trades_{strategy_name}.csv", index=False, encoding="utf-8-sig")
    print(f"📄 거래 내역 CSV 저장 완료: trades_{strategy_name}.csv")
    return trades

def analyze_trades(trades):
    sells = [t for t in trades if t["type"] == "sell"]
    profits = [t["profit"] for t in sells]
    earned_list = [t.get("earned", 0) for t in sells]

    winrate = sum(1 for p in profits if p > 0) / len(profits) * 100 if profits else 0
    avg_profit = sum(profits) / len(profits) if profits else 0
    cumulative = sum(profits)
    total_earned = sum(earned_list)

    # ✅ 수익곡선 계산
    curve = pd.Series([0] + list(pd.Series(profits).cumsum()))
    peak = curve.cummax()

    # ✅ MDD 계산 방어 처리
    if len(curve) > 1 and peak.max() != 0:
        drawdown = (curve - peak) / peak
        mdd = drawdown.min() * 100
    else:
        mdd = 0

    return {
        "total_trades": len(profits),
        "winrate": round(winrate, 2),
        "avg_profit": round(avg_profit, 2),
        "cumulative_return": round(cumulative, 2),
        "cumulative_earned": round(total_earned, 2),
        "mdd": round(mdd, 2),
        "profits": profits,
        "curve": curve[1:].tolist()
    }

def plot_strategy_comparison(results):
    plt.figure(figsize=(10, 6))
    for r in results:
        plt.plot(r["curve"], label=r["strategy"])
    plt.title("📈 전략별 누적 수익률 비교")
    plt.xlabel("거래 순서")
    plt.ylabel("누적 수익률 (%)")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(CHART_PATH)
    print(f"📊 수익률 그래프 저장 완료: {CHART_PATH}")

def save_trades_to_csv(trades, strategy_name):
    df = pd.DataFrame(trades)
    path = os.path.join("C:/Users/oxyze/autobot-trader/backtest", f"trades_{strategy_name}.csv")
    df.to_csv(path, index=False, encoding='utf-8-sig')
    print(f"📄 거래 내역 CSV 저장 완료: {path}")

if __name__ == "__main__":
    files = list_strategies()
    file_choice = int(input("\n✅ 전략 번호를 선택하세요: "))
    durations = list_durations()
    dur_choice = int(input("\n🕒 기간 번호를 선택하세요 (1~5): "))
    count = durations.get(dur_choice, 365)
    markets = get_market()

    results = []
    targets = files if file_choice == len(files) + 1 else [files[file_choice - 1]]

    for market in markets:
        for filename in targets:
            filepath = os.path.join(STRATEGY_DIR, filename)
            module = load_strategy_module(filepath)
            strategy_func = getattr(module, "get_signal", None)

            if strategy_func is None:
                print(f"❌ {filename} → get_signal(df) 함수가 모듈에 없습니다. 전략 로딩 실패.")
                print(f"   🔍 확인: 파일 경로 → {filepath}")
                print(f"   🔍 모듈 로딩 상태: {module.__dict__.keys()}")
                continue
            else:
                print(f"✅ {filename} → get_signal(df) 함수 로드 성공. 전략 적용 시작.")
            
            strategy_name = f"{filename.replace('.py', '')}_{market.replace('KRW-', '')}"
            trades = run_backtest(strategy_func, strategy_name, count, market)
            stats = analyze_trades(trades)
            results.append({
                "strategy": strategy_name,
                "date": datetime.now().strftime("%Y-%m-%d"),  # ✅ 실제 필드로 추가됨
                "buy_count": stats["total_trades"],
                "sell_count": stats["total_trades"],
                "trade_count": stats["total_trades"],
                "cumulative_return": stats["cumulative_return"],
                "cumulative_earned": stats["cumulative_earned"],
                "winrate": stats["winrate"],
                "avg_profit": stats["avg_profit"],
                "mdd": stats["mdd"],
                "trades": trades,
                "curve": stats["curve"]
            })

    if results:
        plot_strategy_comparison(results)
        generate_html_report(results)
    else:
        print("❌ 실행 가능한 전략이 없습니다.")
