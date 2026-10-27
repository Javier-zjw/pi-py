"""把 pi-py 接进 Harbor（https://github.com/laude-institute/harbor）当被测智能体。

    PYTHONPATH=evals harbor run -p evals/tasks -a pi_py_harbor:PiPy -m deepseek/deepseek-chat

测的是**当前工作区**的 pi-py：每次运行先在宿主机把 packages/ 下的 pi-ai、pi-agent、pi-coding-agent
连同依赖（httpx 等，全是纯 Python）装进一个目录（``uv pip install --target``，按源码指纹缓存，
改了代码才重打），上传到任务容器的 ``/installed-agent/pi-py/lib``，用容器里的 python3（>=3.10）
以 ``-I`` 启动——不装进任务自己的 Python 环境，不改 PATH / PYTHONPATH，智能体跑的测试看不到它。
镜像里没有合适的 Python 时才联网用 uv 装一个独立的。

模型和凭证沿用本机 pi 的配置（``agent_dir``，默认 ``~/.pi/agent``）：

- ``models.json`` 上传到容器里的配置目录（自定义 provider 照常能用）；
- 密钥不落盘：按 pi 的规则（auth.json > 环境变量 ``<PROVIDER>_API_KEY``）在宿主机取出**当前模型那个
  provider** 的 key，只作为环境变量交给 pi 进程；auth.json 不上传；
- settings.json 默认不带（评测用默认设置，结果可复现）；要带就传 ``--ak settings=路径``。

轨迹：pi-py 的会话 JSONL 和 TS 版 pi 格式一致，跑完复制到 ``/logs/agent/pi/sessions/``，交给 Harbor
内置的 pi 转换器生成 ATIF 轨迹（trajectory.json）并统计 token / 费用——``harbor view`` 能逐步回放。

pi 的退出码：模型名写错 2、模型调用出错（重试也没救回来）1——Harbor 记成异常，和"做错了"分开统计。

按 PyPI 上发布的 Harbor（0.25）的接口写：那一版的 BaseInstalledAgent 还没有 configure() 钩子。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.pi import Pi
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pi_py_eval import (
    FIND_PYTHON,
    INSTALL_PYTHON,
    REMOTE_BIN,
    REMOTE_CONFIG,
    REMOTE_LIB,
    REMOTE_ROOT,
    ThinkingLevel,
    build_bundle,
    key_env_name,
    launcher,
    resolve_api_key,
    run_command,
)
from pydantic import Field

WEB_TOOLS = ("web_search", "web_fetch")


class PiPyOptions(InstalledAgentOptions):
    reasoning_effort: ThinkingLevel | None = Field(
        default=None, description="pi 的思考档位（-t）；harbor 的 --effort 也落到这里。")
    agent_dir: str | None = Field(
        default=None, description="本机 pi 配置目录（读 models.json、auth.json），默认 ~/.pi/agent。")
    settings: str | None = Field(
        default=None, description="要带进容器的 settings.json；不写就用默认设置。")


class PiPy(BaseInstalledAgent):
    """pi-py（本仓库当前工作区的代码）"""

    capabilities = AgentCapabilities(reasoning_effort=True, atif=True)
    options_model = PiPyOptions
    options: PiPyOptions

    _OUTPUT_FILENAME = "pi-py.txt"
    _SESSIONS_DIRECTORY = "pi/sessions"     # Harbor 的 pi 转换器从这里读会话

    @staticmethod
    def name() -> str:
        return "pi-py"

    def get_version_command(self) -> str | None:
        return f"cat {REMOTE_LIB}/VERSION"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip() if stdout.strip() else "unknown"

    def _agent_dir(self) -> Path:
        return Path(self.options.agent_dir or "~/.pi/agent").expanduser()

    async def install(self, environment: BaseEnvironment) -> None:
        bundle = await asyncio.to_thread(build_bundle)
        await self.exec_as_root(environment,
                                command=f"rm -rf {REMOTE_ROOT} && mkdir -p {REMOTE_ROOT}/bin")
        await environment.upload_dir(bundle, str(REMOTE_LIB))
        found = await environment.exec(command=FIND_PYTHON)
        python = (found.stdout or "").strip().splitlines()[-1] if found.return_code == 0 else ""
        if not python:
            await self.ensure_system_dependencies(environment, ("curl",))
            done = await self.exec_as_root(environment, command=INSTALL_PYTHON)
            python = (done.stdout or "").strip().splitlines()[-1]
        script = launcher(python)
        await self.exec_as_root(
            environment,
            command=f"printf '%s' {shlex.quote(script)} > {REMOTE_BIN} && chmod 755 {REMOTE_BIN} "
                    f"&& chmod -R a+rX {REMOTE_ROOT}",
        )
        await self.exec_as_agent(environment, command=f"{REMOTE_BIN} --help >/dev/null")

    async def _write_config(self, environment: BaseEnvironment) -> None:
        """配置目录：models.json（自定义 provider）、可选的 settings.json；密钥不写进来。

        每次运行前写（不放 install 里：Harbor 的 skip_install 会跳过 install）"""
        await self.exec_as_agent(
            environment, command=f"rm -rf {REMOTE_CONFIG} && mkdir -p {REMOTE_CONFIG} "
                                 f"&& chmod 700 {REMOTE_CONFIG}")
        uploads = {"models.json": self._agent_dir() / "models.json"}
        if self.options.settings:
            uploads["settings.json"] = Path(self.options.settings).expanduser()
            if not uploads["settings.json"].is_file():
                raise FileNotFoundError(f"settings 文件不存在：{uploads['settings.json']}")
        for name, source in uploads.items():
            if source.is_file():
                await self._upload_config_text(environment, content=source.read_text("utf-8"),
                                               remote_path=f"{REMOTE_CONFIG}/{name}",
                                               filename=name)

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        if not self.model_name:
            raise ValueError("要用 -m 指定模型，格式 provider/model-id（同 pi -m）")
        await self._write_config(environment)
        env = {"PYTHONUNBUFFERED": "1"}
        provider = self.model_name.split("/", 1)[0] if "/" in self.model_name else None
        if provider:
            key = resolve_api_key(provider, self._agent_dir())
            if key:
                env[key_env_name(provider)] = key
            else:
                self.logger.warning(
                    f"没找到 {provider} 的密钥（auth.json 或 {key_env_name(provider)}）")
        logs = self.environment_logs_dir
        await self.exec_as_agent(
            environment,
            command=run_command(
                self.model_name, instruction,
                output=str(logs / self._OUTPUT_FILENAME),
                sessions_out=str(logs / self._SESSIONS_DIRECTORY),
                thinking=self.options.reasoning_effort,
                exclude_tools=WEB_TOOLS if self.options.disable_web_search else ()),
            env=env,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """会话 JSONL → ATIF 轨迹 + token / 费用（借 Harbor 内置 pi 的转换器，格式相同）"""
        try:
            Pi(logs_dir=self.logs_dir, model_name=self.model_name).populate_context_post_run(context)
        except Exception:
            self.logger.exception("转换 pi-py 会话失败（不影响判分）")
            return
        trajectory = self.logs_dir / "trajectory.json"
        try:
            data = json.loads(trajectory.read_text())
            data.setdefault("agent", {}).update(name=self.name(),
                                                version=self.version() or "unknown")
            trajectory.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        except (OSError, ValueError):
            pass
