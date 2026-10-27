from shop import pricing


def receipt(items, discount=0.0):
    total = pricing.calc_total(items, discount)
    lines = [f"{qty} x {price:.2f}" for price, qty in items]
    lines.append(f"total: {total:.2f}")
    return "\n".join(lines)
