def trading_logic(avg_price: float = None, buy_orders: list = None):
    """
    매수 주문 가격 리스트 생성 로직
    
    Parameters
    ----------
    avg_price : float
        Case3 상황 → 현재 평단가(시장가 매수 이후 기준)
    buy_orders : list
        Case4 상황 → 미체결 매수 주문 리스트
    
    Returns
    -------
    list[int]
        매수 주문 가격 리스트 (1~3개)
    """
    buy_price_list = []

    # Case 3: 평단가를 기준으로 신규 매수 리스트 생성
    if avg_price is not None and buy_orders is None:
        price1 = round(avg_price * 0.96)   # -4%
        price2 = round(price1 * 0.96)      # -4%
        price3 = round(price2 * 0.96)      # -4%
        buy_price_list = [price1, price2, price3]

    # Case 4: 기존 미체결 매수 주문이 있는 경우
    elif buy_orders is not None:
        prices = [float(o["price"]) for o in buy_orders if "price" in o]

        if len(prices) == 2:
            # 두 주문 중 더 낮은 가격에서 4% 낮은 가격 하나만 리턴
            min_price = min(prices)
            new_price = round(min_price * 0.96)
            buy_price_list = [new_price]

        elif len(prices) == 1:
            # 해당 주문 가격에서 -4%씩 2개 추가
            base_price = float(prices[0])
            price1 = round(base_price * 0.96)
            price2 = round(price1 * 0.96)
            buy_price_list = [price1, price2]

    return buy_price_list
