"""价格计算。"""


def calc_total(items, discount=0.0):
    """items 是 (单价, 数量) 列表；discount 是折扣比例（0.1 = 打九折）。结果保留两位小数。"""
    subtotal = sum(price * qty for price, qty in items)
    return round(subtotal * (1 - discount), 2)
