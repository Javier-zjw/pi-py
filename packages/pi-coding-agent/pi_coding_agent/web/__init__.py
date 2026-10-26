"""上网：读网页（fetch）和搜索（search）。纯 Python（httpx + 标准库），打包后不用另装工具。

工具本身在内置扩展 ``builtin_extensions/web.py`` 里注册；这里只放可离线测试的逻辑：
HTML → Markdown（markup）、取网页（fetch）、各搜索后端（search）。网络请求都经过一个
可替换的 ``Http`` 函数（http.py），测试用假的返回预设内容。
"""
