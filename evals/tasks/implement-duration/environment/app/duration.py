"""时长解析。"""


def parse_duration(text):
    """把 "1h30m" 这样的时长转成秒数（int）。

    规则：
    - 单位：d（天）、h（小时）、m（分钟）、s（秒），大小写都行；
    - 数字是非负整数，单位之间可以有空格："1h 5m 10s"；首尾空白忽略；
    - 单位可以按任意顺序出现，但同一个单位只能出现一次；
    - 空字符串、只有数字没有单位、未知单位、重复单位、其他字符：抛 ValueError。

    例子：
        parse_duration("45s") == 45
        parse_duration("1h30m") == 5400
        parse_duration("2D 1s") == 172801
    """
    raise NotImplementedError
