把 `/app` 项目里的函数 `calc_total` 改名为 `order_total`。

- 所有用到它的地方（代码、测试、文档）都要跟着改，项目里不能再出现 `calc_total`；
- 不要留兼容用的旧名字；
- 行为不变，改完 `python -m unittest` 要能通过。
