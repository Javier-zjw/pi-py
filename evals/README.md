# 评测（Harbor）

用 [Harbor](https://github.com/laude-institute/harbor)（Terminal-Bench 的官方运行器）评测**当前工作区**的 pi-py：每个任务一个 Docker 容器，pi 在里面干活，干完由隐藏测试判分（1 / 0），轨迹、token、费用都记下来。改了提示、压缩、工具之后跑一遍，和改之前比，就知道是变好还是变差。

```
evals/
├── run.sh            # 跑评测：evals/run.sh -m <模型> [harbor run 的其他参数]
├── compare.py        # 汇总一次结果 / 对比两次结果
├── pi_py_harbor.py   # Harbor 智能体类 PiPy（依赖 harbor）
├── pi_py_eval.py     # 打包、取密钥、容器里的命令（纯函数，不依赖 harbor）
├── tasks/            # 示范任务集（5 个）
├── tests/            # 离线测试：python evals/tests/test_pi_py_eval.py / test_compare.py
└── jobs/             # 运行结果（不进 git）
```

## 准备

1. **Docker**：Mac 上装 Docker Desktop 或 OrbStack，确认 `docker run --rm hello-world` 能跑。
2. **uv 和 Harbor**：

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh    # 已经有 uv 就跳过
   uv tool install harbor                             # 测过的版本：0.25.0
   ```

3. **模型配置**：直接用你平时 pi 的配置 `~/.pi/agent/models.json` 和 `auth.json`（或 `<PROVIDER>_API_KEY` 环境变量）。

先不花钱确认任务和环境没问题——用标准答案（oracle）和什么都不做的智能体（nop）各跑一遍：

```bash
harbor run -p evals/tasks -a oracle -o evals/jobs --job-name oracle   # 应该全部 1.0
harbor run -p evals/tasks -a nop    -o evals/jobs --job-name nop      # 应该全部 0.0
```

## 跑评测

```bash
evals/run.sh -m deepseek/deepseek-chat --job-name base                # 5 个任务各跑一次
evals/run.sh -m deepseek/deepseek-chat -k 3 --job-name base           # 各跑 3 次，看稳定性（pass@k）
evals/run.sh -m deepseek/deepseek-chat -p evals/tasks/fix-pagination  # 只跑一个任务
evals/run.sh -m anthropic/claude-sonnet-4-5 --effort high             # 思考档位（同 pi -t）
evals/run.sh -m ... --ak disable_web_search=true                      # 不给 web_search / web_fetch
evals/run.sh -m ... --ak settings=~/.pi/agent/settings.json           # 带上自己的设置（默认不带）
evals/run.sh -m ... --ak agent_dir=/别的/配置目录                     # 换一份 models.json / auth.json
evals/run.sh -m ... -n 2                                              # 同时跑几个容器（默认 4）
```

`-m` 的写法和 `pi -m` 一样（`provider/model-id`）。模型名写错时 pi 会直接报错退出，不会悄悄换成默认模型。

`run.sh` 只是帮你加上 `-a pi_py_harbor:PiPy -p evals/tasks -o evals/jobs` 和 `PYTHONPATH=evals`，其余参数原样交给 `harbor run`。也可以直接写：

```bash
PYTHONPATH=evals harbor run -p evals/tasks -a pi_py_harbor:PiPy -m deepseek/deepseek-chat -o evals/jobs
```

## 看结果

```bash
python evals/compare.py evals/jobs/base               # 按任务汇总
python evals/compare.py evals/jobs/base evals/jobs/new  # 改动前后对比，标出 ↑ 变好 / ↓ 退步
python evals/compare.py evals/jobs/base evals/jobs/new --strict   # 有退步就退出 1
harbor view evals/jobs                                 # 网页里逐步回放每次运行的轨迹
```

| 列 | 意思 |
|---|---|
| 通过 | 判分 1 的次数 / 运行次数 |
| 全过 | 每次都过（pass^k）：稳定性，比"至少过一次"更接近真实使用 |
| 异常 | 模型报错、超时、装不上等，**不是**"做错了"，要单独看 |
| 工具 / token / 费用 / 耗时 | 每次运行的平均值：同样的通过率，更省就是更好 |

每次运行的明细在 `evals/jobs/<名字>/<任务>__<编号>/`：

- `agent/pi-py.txt`：pi 的输出；
- `agent/pi/sessions/*.jsonl`：会话原文（和本机 `~/.pi/agent/sessions` 里的一样，pi-tui 可以 `--resume` 打开）；
- `agent/trajectory.json`：Harbor 统一格式的轨迹；
- `verifier/test-stdout.txt`：隐藏测试的输出，看哪条没过；
- `result.json`：判分、异常、token、费用。

**做对比时注意**：模型每次的结果不一样。每个任务至少跑 3 次（`-k 3`），差一两次可能只是运气；一次只改一个东西，别的（模型、思考档位、设置）保持不变。

## 怎么接进去的

- **测的是工作区里的代码**：每次运行先在本机把 `packages/` 下的 pi-ai、pi-agent、pi-coding-agent 连同依赖（httpx 等，都是纯 Python）打成一个目录（`uv pip install --target`，按源码内容缓存在临时目录，改了代码才重打）。然后上传到容器的 `/installed-agent/pi-py/lib`，用容器里的 Python 3.10+ 以 `python -I` 启动。
  - 不装进任务自己的 Python 环境，也不改 PATH 和 PYTHONPATH。
  - 任务目录里和标准库同名的文件也影响不到它。
  - 镜像里没有 Python 3.10+ 时，才联网用 uv 装一个独立的 Python。这个兜底分支在本仓库的开发环境里测不了（网络策略不允许访问 astral.sh / GitHub）。
- **密钥不落盘**：在本机按 pi 的规则（auth.json 优先，其次 `<PROVIDER>_API_KEY` 环境变量）只取当前模型那个 provider 的 key，作为环境变量交给容器里的 pi 进程。`auth.json` 不上传，日志里也不会出现密钥。`models.json` 会上传，自定义的 provider 照常能用。
- **轨迹**：pi-py 的会话文件格式和 TS 版 pi 一致，所以直接交给 Harbor 内置的 pi 转换器，生成轨迹并统计 token 和费用。
- **退出码**：模型名写错退出 2；模型调用失败（重试也没救回来）退出 1。Harbor 把这两种记成"异常"，和"做错了"分开统计。

## 加任务

```
tasks/<名字>/
├── task.toml              # 名字 pi-py/<名字>、难度、类别、标签、超时
├── instruction.md         # 给智能体的话（就像用户在输入框里说的）
├── environment/
│   ├── Dockerfile         # FROM python:3.12-slim，COPY app/ /app/
│   └── app/               # 初始代码
├── solution/solve.sh      # 标准答案：证明任务做得出来
└── tests/
    ├── test.sh            # 跑隐藏测试，全过写 1，否则写 0 到 /logs/verifier/reward.txt
    └── test_hidden.py     # 隐藏测试（unittest，只用标准库）
```

照抄一个现有任务改。几条规矩：

- **判分只看结果**：文件内容、测试是否通过、命令输出。不看过程，同一件事可以有多种做法。
- **堵住捷径**：
  - 不许改的测试要检查有没有被改（见 `fix-pagination` 的哈希检查）；
  - 不许留兼容旧名字（见 `rename-function`）；
  - 指令里给的例子要在隐藏测试里再加几个边界情况。
- **判分在独立容器里**（`environment_mode = "separate"`，`artifacts = ["/app"]`）：智能体看不到 `/tests`，也改不了判分用的环境。
- **任务本身不联网**：只用标准库，镜像构建和判分都不用下载东西，结果更稳。
- 写完先跑 `harbor lint evals/tasks/<名字>`，再用 oracle 跑应该是 1、nop 跑应该是 0，最后 `python evals/tests/test_pi_py_eval.py` 检查文件齐全。

## 公开基准、和别的智能体比

```bash
evals/run.sh -m anthropic/claude-sonnet-4-5 -d terminal-bench@2.0 -n 8     # Terminal-Bench 2.0
harbor datasets list                                                        # 还有 SWE-bench、Aider Polyglot 等
harbor run -p evals/tasks -a claude-code -m anthropic/claude-sonnet-4-5 -o evals/jobs --job-name cc
harbor run -p evals/tasks -a pi -m anthropic/claude-sonnet-4-5 -o evals/jobs --job-name pi-ts   # 上游 TS 版 pi
python evals/compare.py evals/jobs/pi-ts evals/jobs/base
```

公开基准的任务多、镜像大，一次全量要花不少钱和时间。先用 `-l 10`（只跑前 10 个）试。
