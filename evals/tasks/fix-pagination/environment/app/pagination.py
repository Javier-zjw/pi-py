"""分页工具。"""


def paginate(items, page, per_page):
    """返回第 page 页（从 1 开始）的元素。

    - page 超出范围时返回空列表；
    - page < 1 或 per_page < 1 时抛 ValueError。
    """
    start = page * per_page
    return items[start:start + per_page]


def page_count(total, per_page):
    """total 个元素按每页 per_page 个分页，一共几页（没有元素时是 0 页）。"""
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    return total // per_page
