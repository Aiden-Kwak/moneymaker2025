import pyupbit
import time
import pandas as pd
import numpy as np
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

# ================ 너는 로그야 ================
logger = logging.getLogger("AutoTradingLogger")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# CONSOLE HANDLER
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# FILE HANDLER (매일 새로운 파일 생성)
log_filename = datetime.now().strftime("trading_log_%Y%m%d.log")
file_handler = logging.FileHandler(log_filename)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ================ API ================
API_KEY = os.getenv("ACCESS_KEY")
API_SECRET = os.getenv("SECRET_KEY")
upbit = pyupbit.Upbit(API_KEY, API_SECRET)

# 각 종목별 매수 시 사용할 원화 주문 금액
ORDER_AMOUNT = float(os.getenv("ORDER_AMOUNT", "5000"))

# ================ 보조용임: 안전하게 현재가 조회 ================
def safe_get_current_price(ticker):
    try:
        price = pyupbit.get_current_price(ticker)
        # price가 None 또는 값이 없을 경우를 확인
        if price is None:
            raise ValueError("가격 데이터 없음")
        return price
    except Exception as e:
        logger.warning(f"{ticker}: 현재가 조회 오류 - {e}")
        return None

# ================ 여기부터 주요함수야 ================
def get_top_50_tickers():
    tickers = pyupbit.get_tickers(fiat="KRW")
    top_50 = tickers[:50]
    logger.info(f"상위 50개 티커 조회: {top_50}")
    return top_50

def get_top_100_tickers():
    tickers = pyupbit.get_tickers(fiat="KRW")
    top_100 = tickers[:100]
    logger.info(f"상위 100개 티커 조회: {top_100}")
    return top_100

def get_atr(ticker, interval="minute60", period=14):
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=period+1)
    if df is None or len(df) < period + 1:
        logger.warning(f"{ticker}: ATR 계산을 위한 데이터 부족")
        return None
    df['prev_close'] = df['close'].shift(1)
    df['high_low'] = df['high'] - df['low']
    df['high_prev_close'] = abs(df['high'] - df['prev_close'])
    df['low_prev_close'] = abs(df['low'] - df['prev_close'])
    df['tr'] = df[['high_low', 'high_prev_close', 'low_prev_close']].max(axis=1)
    atr = df['tr'].iloc[1:].rolling(window=period).mean().iloc[-1]
    logger.debug(f"{ticker}: ATR 계산 완료 - {atr}")
    return atr

def filter_high_volatility_tickers(tickers, threshold=0.02):
    high_volatility_tickers = []
    logger.info("변동성 높은 종목 필터링 시작")
    for ticker in tickers:
        atr = get_atr(ticker)
        if atr is None:
            continue
        last_price = safe_get_current_price(ticker)
        if last_price is None:
            logger.warning(f"{ticker}: 현재가 조회 실패")
            continue
        if (atr / last_price) > threshold:
            high_volatility_tickers.append(ticker)
            logger.info(f"{ticker}: 변동성 기준 통과 (ATR: {atr}, 현재가: {last_price})")
    logger.info(f"필터링 결과 - 변동성 높은 티커: {high_volatility_tickers}")
    return high_volatility_tickers

def get_intrinsic_time_events(ticker, threshold=0.01, interval="minute60"):
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=100)
    if df is None:
        logger.warning(f"{ticker}: 내재적 시간 이벤트 데이터 없음")
        return []
    df["delta"] = df["close"].pct_change()
    events = []
    for i in range(1, len(df)):
        if abs(df["delta"].iloc[i]) >= threshold:
            event = (df.index[i], df["close"].iloc[i], "DC")
            events.append(event)
            logger.debug(f"{ticker}: 이벤트 발생 - {event}")
    logger.info(f"{ticker}: 총 {len(events)}개의 이벤트 감지")
    return events

def check_breakout_signal(ticker):
    events = get_intrinsic_time_events(ticker, threshold=0.01)
    if len(events) < 10:
        logger.info(f"{ticker}: 이벤트 부족 (갯수: {len(events)}), HOLD 신호")
        return "HOLD"
    
    recent_events = events[-10:]
    support = min([e[1] for e in recent_events])
    resistance = max([e[1] for e in recent_events])
    current_price = safe_get_current_price(ticker)
    if current_price is None:
        logger.warning(f"{ticker}: 현재가 조회 실패, HOLD 신호")
        return "HOLD"
    
    if current_price > resistance:
        logger.info(f"{ticker}: 저항선 돌파 (현재가: {current_price} > 저항: {resistance}) → BUY 신호")
        return "BUY"
    elif current_price < support:
        logger.info(f"{ticker}: 지지선 붕괴 (현재가: {current_price} < 지지: {support}) → SELL 신호")
        return "SELL"
    logger.info(f"{ticker}: 특별한 신호 없음, HOLD")
    return "HOLD"

# 포트폴리오: {티커: {"entry_price": 매수가격, "quantity": 매수량}}
portfolio = {}

def manage_portfolio():
    global portfolio
    tickers = get_top_100_tickers()
    high_volatility_tickers = filter_high_volatility_tickers(tickers)
    
    # 신규 진입: 보유 종목이 5개 미만이면 신규 진입 시도
    if len(portfolio) < 5:
        logger.info("포트폴리오 신규 진입 검토 시작")
        for ticker in high_volatility_tickers:
            if ticker not in portfolio and len(portfolio) < 10:
                signal = check_breakout_signal(ticker)
                if signal == "BUY":
                    buy_price = safe_get_current_price(ticker)
                    if buy_price is not None:
                        # 시장가 매수 주문 실행
                        order = upbit.buy_market_order(ticker, ORDER_AMOUNT)
                        logger.info(f"✅ 신규 진입 주문 요청: {ticker} 주문금액 {ORDER_AMOUNT}원")
                        time.sleep(1)  # 주문 체결 대기
                        # 잔고 조회하여 매수량 결정
                        quantity = upbit.get_balance(ticker)
                        if quantity is None or quantity <= 0:
                            logger.warning(f"{ticker}: 매수 후 잔고 조회 실패")
                            continue
                        portfolio[ticker] = {"entry_price": buy_price, "quantity": quantity}
                        logger.info(f"✅ 신규 진입 완료: {ticker} at {buy_price}, 수량: {quantity}")
    
    # 포트폴리오 정리: 보유 종목이 10개 초과 시, 가장 수익률이 낮은 종목 매도
    if len(portfolio) > 10:
        sorted_tickers = sorted(
            portfolio.keys(),
            key=lambda t: (safe_get_current_price(t) - portfolio[t]["entry_price"]) / portfolio[t]["entry_price"]
        )
        worst_ticker = sorted_tickers[0]
        quantity = portfolio[worst_ticker]["quantity"]
        logger.info(f"🚨 포트폴리오 정리: {worst_ticker} 매도 (수익률 최저), 수량: {quantity}")
        order = upbit.sell_market_order(worst_ticker, quantity)
        logger.info(f"🚨 포트폴리오 정리 완료: {worst_ticker} 매도 주문 실행")
        del portfolio[worst_ticker]

def execute_trading():
    global portfolio
    logger.info("자동매매 실행 시작")
    while True:
        # 보유 중인 각 종목에 대해 청산 조건 확인
        for ticker in list(portfolio.keys()):
            current_price = safe_get_current_price(ticker)
            entry_price = portfolio[ticker]["entry_price"]
            atr = get_atr(ticker)
            if current_price is None or atr is None:
                logger.warning(f"{ticker}: 현재가 또는 ATR 조회 실패, 다음 종목으로 넘어감")
                continue

            # 트레일링 스탑: 입장가 대비 5% 하락 시 매도
            if current_price < entry_price * 0.95:
                quantity = portfolio[ticker]["quantity"]
                logger.info(f"📉 {ticker}: 트레일링 스탑 발동 (입장가: {entry_price}, 현재가: {current_price}) → 매도, 수량: {quantity}")
                order = upbit.sell_market_order(ticker, quantity)
                logger.info(f"📉 {ticker}: 매도 주문 실행 완료")
                del portfolio[ticker]
                continue

            # 목표 수익률: 1.5×(ATR/입장가) 이상 수익 발생 시 매도
            if (current_price - entry_price) / entry_price >= 1.5 * (atr / entry_price):
                quantity = portfolio[ticker]["quantity"]
                logger.info(f"🎯 {ticker}: 목표 수익 도달 (입장가: {entry_price}, 현재가: {current_price}, ATR: {atr}) → 매도, 수량: {quantity}")
                order = upbit.sell_market_order(ticker, quantity)
                logger.info(f"🎯 {ticker}: 매도 주문 실행 완료")
                del portfolio[ticker]
                continue

            # 반전 신호 발생 시 매도
            if check_breakout_signal(ticker) == "SELL":
                quantity = portfolio[ticker]["quantity"]
                logger.info(f"🔄 {ticker}: 반전 신호 감지 → 매도, 수량: {quantity}")
                order = upbit.sell_market_order(ticker, quantity)
                logger.info(f"🔄 {ticker}: 매도 주문 실행 완료")
                del portfolio[ticker]

        # 포트폴리오 신규 진입 및 정리
        manage_portfolio()
        logger.info(f"현재 포트폴리오 상태: {portfolio}")
        time.sleep(60)  # 1분 간격 실행

if __name__ == "__main__":
    logger.info("🚀 자동매매 프로그램 시작!")
    try:
        execute_trading()
    except KeyboardInterrupt:
        logger.info("프로그램이 종료되었습니다.")
