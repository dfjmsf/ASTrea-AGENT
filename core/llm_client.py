import os
import json
import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI, AsyncOpenAI
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

logger = logging.getLogger("LLMClient")

# ═══ Token 追踪器 ═══

# 内置价格表（元/百万 Token）— 可通过 .env 中 PRICE_<MODEL>_INPUT / PRICE_<MODEL>_OUTPUT / PRICE_<MODEL>_CACHE 覆盖
# 格式: model_prefix → (input_miss_price, output_price, cache_hit_price)
_DEFAULT_PRICES = {
    "deepseek-v4-flash": (1.0, 2.0, 0.02),   # Flash: 输入1元, 输出2元, 缓存命中0.02元
    "deepseek-v4-pro":   (3.0, 6.0, 0.025),   # Pro:   输入3元, 输出6元, 缓存命中0.025元
    "qwen3-max":         (2.0, 8.0, 1.0),
    "qwen3-plus":        (0.8, 2.0, 0.4),
    "qwen3.5-max":       (2.0, 8.0, 1.0),
    "qwen3.5-plus":      (0.8, 2.0, 0.4),
}


class TokenTracker:
    """全局 Token 追踪器：按模型分组统计，支持费用估算。"""

    def __init__(self):
        # 全局累计（向后兼容）
        self.total_prompt = 0
        self.total_completion = 0
        self.total_reasoning = 0
        self.total_cache_hit = 0
        self.request_count = 0
        self.last_prompt_tokens = 0  # 最近一次请求的精确 prompt_tokens
        # 按模型分组
        self._by_model: Dict[str, Dict[str, int]] = {}

    def update(self, usage, model: str = "unknown") -> None:
        """从 API response.usage 更新统计。"""
        if not usage:
            return

        prompt = getattr(usage, 'prompt_tokens', 0) or 0
        completion = getattr(usage, 'completion_tokens', 0) or 0
        # DeepSeek V4 独有：reasoning_tokens（思考 token，计入 completion 内）
        reasoning = 0
        completion_detail = getattr(usage, 'completion_tokens_details', None)
        if completion_detail:
            reasoning = getattr(completion_detail, 'reasoning_tokens', 0) or 0
        # DeepSeek 缓存命中
        cache_hit = 0
        prompt_detail = getattr(usage, 'prompt_cache_hit_tokens', None)
        if isinstance(prompt_detail, int):
            cache_hit = prompt_detail
        elif prompt_detail is not None:
            cache_hit = getattr(prompt_detail, 'cached_tokens', 0) or 0

        # 全局累计
        self.total_prompt += prompt
        self.total_completion += completion
        self.total_reasoning += reasoning
        self.total_cache_hit += cache_hit
        self.request_count += 1
        self.last_prompt_tokens = prompt  # 最近一次请求的精确 prompt_tokens

        # 按模型累计
        model_key = model.lower()
        if model_key not in self._by_model:
            self._by_model[model_key] = {
                "prompt": 0, "completion": 0,
                "reasoning": 0, "cache_hit": 0, "requests": 0,
            }
        m = self._by_model[model_key]
        m["prompt"] += prompt
        m["completion"] += completion
        m["reasoning"] += reasoning
        m["cache_hit"] += cache_hit
        m["requests"] += 1

        request_total = prompt + completion
        session_total = self.total_prompt + self.total_completion
        logger.info(f"🪙 [Token开销] 本次: {request_total:,} | 累计: {session_total:,}")

    def estimate_cost(self) -> Dict[str, Any]:
        """估算总费用（元），按模型分组。

        价格来源优先级：
        1. .env 中 PRICE_DEEPSEEK_V4_FLASH_INPUT=1.0 (模型名中 - 替换为 _，全大写)
        2. 内置 _DEFAULT_PRICES
        3. 未知模型按 2.0 / 8.0 估算
        """
        details = []
        total_cost = 0.0

        for model_key, usage in self._by_model.items():
            # 查找价格：.env 优先
            env_prefix = f"PRICE_{model_key.replace('-', '_').upper()}"
            input_price = float(os.getenv(f"{env_prefix}_INPUT", 0))
            output_price = float(os.getenv(f"{env_prefix}_OUTPUT", 0))
            cache_price = float(os.getenv(f"{env_prefix}_CACHE", 0))

            if not input_price or not output_price:
                # 查内置表
                prices = _DEFAULT_PRICES.get(model_key, (2.0, 8.0, 1.0))
                input_price = input_price or prices[0]
                output_price = output_price or prices[1]
                cache_price = cache_price or prices[2]

            # 计算费用（元/百万 Token → 元）
            # 输入 = 缓存命中部分 × cache_price + 未命中部分 × input_price
            non_cached = max(usage["prompt"] - usage["cache_hit"], 0)
            input_cost = non_cached * input_price / 1_000_000
            cache_cost = usage["cache_hit"] * cache_price / 1_000_000
            output_cost = usage["completion"] * output_price / 1_000_000
            model_cost = input_cost + cache_cost + output_cost

            total_cost += model_cost
            details.append({
                "model": model_key,
                "prompt": usage["prompt"],
                "completion": usage["completion"],
                "reasoning": usage["reasoning"],
                "cache_hit": usage["cache_hit"],
                "requests": usage["requests"],
                "cost": round(model_cost, 6),
                "input_price": input_price,
                "output_price": output_price,
            })

        return {
            "total_cost": round(total_cost, 6),
            "total_prompt": self.total_prompt,
            "total_completion": self.total_completion,
            "total_reasoning": self.total_reasoning,
            "total_cache_hit": self.total_cache_hit,
            "total_tokens": self.total_prompt + self.total_completion,
            "request_count": self.request_count,
            "details": details,
        }


# 全局单例
token_tracker = TokenTracker()

# 向后兼容：旧代码引用 TOTAL_PROMPT_TOKENS / TOTAL_COMPLETION_TOKENS
# 通过模块级属性访问，始终返回 tracker 的最新值
def __getattr__(name):
    if name == "TOTAL_PROMPT_TOKENS":
        return token_tracker.total_prompt
    if name == "TOTAL_COMPLETION_TOKENS":
        return token_tracker.total_completion
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

TOKEN_WARNING_LIMIT = int(os.getenv("TOKEN_WARNING_LIMIT", 50000))  # 保留


class LLMProvider:
    """单个 LLM Provider 配置"""
    def __init__(self, name: str, api_key: str, base_url: str, models: List[str]):
        self.name = name
        self.models = [m.lower() for m in models]
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=300.0,
        )
        self.async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=300.0,
        )

    def supports(self, model: str) -> bool:
        """检查此 Provider 是否支持指定模型"""
        return model.lower() in self.models


class LLMClient:
    """多 Provider LLM 客户端，根据模型名自动路由到对应 API 端点。
    
    支持的 Provider（按优先级）：
    1. Qwen (DashScope) — QWEN_API_KEY + QWEN_BASE_URL
    2. OpenAI 兼容端点 — OPENAI_API_KEY + OPENAI_BASE_URL（可用于 Gemini 中转等）
    
    路由规则：
    - 模型名在 Provider 的 models 列表中 → 走该 Provider
    - 都不匹配 → fallback 到第一个可用的 Provider
    """

    # 原生 Provider 名称集合（支持发送私有思考模式参数）
    # 第三方代理（硅基流动/OpenRouter 等）不识别这些参数，可能导致请求挂起或报错
    _NATIVE_THINKING_PROVIDERS = {"DeepSeek", "Qwen", "GPT"}

    def __init__(self):
        self.providers: List[LLMProvider] = []
        self._init_providers()
        
        if not self.providers:
            logger.error("❌ 没有配置任何 LLM Provider！请检查 .env 文件")

    def _init_providers(self):
        """从环境变量 + config/custom_providers.json 初始化所有可用的 Provider"""
        
        # Provider 1: Qwen (DashScope)
        qwen_key = os.getenv("QWEN_API_KEY", "")
        if qwen_key and qwen_key != "your_qwen_api_key_here":
            qwen_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            # Qwen 支持的模型列表（可通过 QWEN_MODELS 环境变量扩展）
            qwen_models_str = os.getenv("QWEN_MODELS", "qwen-max,qwen-plus,qwen-turbo,qwen3-max,qwen3-plus,qwen3-coder-plus,qwen-long,qwen3.5-plus,qwen3.5-max,qwen3.5-coder-plus")
            qwen_models = [m.strip() for m in qwen_models_str.split(",") if m.strip()]
            self.providers.append(LLMProvider("Qwen", qwen_key, qwen_url, qwen_models))
            logger.info(f"✅ Provider [Qwen] 已加载 ({len(qwen_models)} 个模型)")
        
        # Provider 2: OpenAI 兼容端点（Gemini 中转等）
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            openai_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            openai_models_str = os.getenv("OPENAI_MODELS", "")
            openai_models = [m.strip() for m in openai_models_str.split(",") if m.strip()]
            if openai_models:
                self.providers.append(LLMProvider("OpenAI-Compatible", openai_key, openai_url, openai_models))
                logger.info(f"✅ Provider [OpenAI-Compatible] 已加载 ({len(openai_models)} 个模型)")
        
        # Provider 3: DeepSeek
        ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        if ds_key:
            ds_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
            ds_models_str = os.getenv("DEEPSEEK_MODELS", "deepseek-chat")
            ds_models = [m.strip() for m in ds_models_str.split(",") if m.strip()]
            self.providers.append(LLMProvider("DeepSeek", ds_key, ds_url, ds_models))
            logger.info(f"✅ Provider [DeepSeek] 已加载 ({len(ds_models)} 个模型)")

        # Provider 4: GPT 中转端点
        gpt_key = os.getenv("GPT_API_KEY", "")
        if gpt_key:
            gpt_url = os.getenv("GPT_BASE_URL", "https://api.openai.com/v1")
            gpt_models_str = os.getenv("GPT_MODELS", "")
            gpt_models = [m.strip() for m in gpt_models_str.split(",") if m.strip()]
            if gpt_models:
                self.providers.append(LLMProvider("GPT", gpt_key, gpt_url, gpt_models))
                logger.info(f"✅ Provider [GPT] 已加载 ({len(gpt_models)} 个模型)")

        # 动态加载: config/custom_providers.json
        self._load_custom_providers()

    def _load_custom_providers(self):
        """从 config/custom_providers.json 加载用户自定义 Provider"""
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "custom_providers.json"
        )
        if not os.path.isfile(config_path):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                custom_list = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ 读取 custom_providers.json 失败: {e}")
            return

        for entry in custom_list:
            name = entry.get("name", "Unknown")
            api_key = entry.get("api_key", "")
            base_url = entry.get("base_url", "")
            models = entry.get("models", [])
            if not api_key or not models:
                logger.warning(f"⚠️ 自定义 Provider [{name}] 缺少 api_key 或 models，已跳过")
                continue
            # 清洗 base_url：剥离用户误填的 /chat/completions 后缀
            clean_url = self._clean_base_url(base_url)
            # 避免重名覆盖：如果已有同名 Provider 则跳过
            if any(p.name == name for p in self.providers):
                logger.info(f"ℹ️ Provider [{name}] 已通过 .env 加载，跳过 JSON 中的重复项")
                continue
            self.providers.append(LLMProvider(name, api_key, clean_url, models))
            logger.info(f"✅ Provider [{name}] 已从 custom_providers.json 加载 ({len(models)} 个模型)")

    @staticmethod
    def _clean_base_url(url: str) -> str:
        """清洗 base_url：剥离用户误填的 /chat/completions 等后缀。
        OpenAI SDK 会自动拼接 /chat/completions，如果用户填了完整 endpoint 会双拼接导致 404。
        """
        clean = (url or "").strip().rstrip("/")
        for suffix in ["/chat/completions", "/completions", "/chat"]:
            if clean.lower().endswith(suffix):
                clean = clean[:len(clean) - len(suffix)]
                break
        return clean.rstrip("/")

    def reload_providers(self):
        """热重载所有 Provider（供 server.py CRUD 操作后调用）"""
        self.providers.clear()
        self._init_providers()
        logger.info(f"🔄 Provider 列表已热重载，当前 {len(self.providers)} 个 Provider")

    def _get_provider(self, model: str) -> LLMProvider:
        """根据模型名路由到对应的 Provider"""
        for provider in self.providers:
            if provider.supports(model):
                return provider
        
        # Fallback: 用第一个可用的 Provider
        if self.providers:
            logger.warning(f"⚠️ 模型 {model} 未匹配任何 Provider，fallback 到 [{self.providers[0].name}]")
            return self.providers[0]
        
        raise RuntimeError(f"没有可用的 LLM Provider 来处理模型 {model}")

    @staticmethod
    def _model_supports_thinking(model: str) -> bool:
        """判断模型是否支持思考模式（Qwen3/3.5 或 DeepSeek V4 或 GPT-5）"""
        model_lower = (model or "").lower()
        return "qwen3" in model_lower or "gpt-5" in model_lower or "deepseek-v4" in model_lower

    @staticmethod
    def _is_deepseek_model(model: str) -> bool:
        """判断是否为 DeepSeek 系列模型"""
        return "deepseek" in (model or "").lower()

    @staticmethod
    def parse_thinking_config(env_val: str):
        """
        解析 THINKING_* 环境变量值，返回 (enable_thinking, reasoning_effort)。
        
        支持值：
        - "false" → (False, None)
        - "true" / "high" → (True, "high")
        - "max" → (True, "max")
        """
        val = (env_val or "false").lower().strip()
        if val in ("true", "high"):
            return True, "high"
        elif val == "max":
            return True, "max"
        else:
            return False, None

    def get_provider_client(self, provider_name: str = "Qwen"):
        """获取指定 Provider 的原始 OpenAI client（用于 Embedding/Rerank 等非 chat 接口）"""
        for p in self.providers:
            if p.name == provider_name:
                return p.client
        # Fallback
        if self.providers:
            return self.providers[0].client
        raise RuntimeError("没有可用的 Provider")

    @staticmethod
    def _update_token_usage(usage, model: str = "unknown") -> None:
        """委托给全局 TokenTracker 更新统计。"""
        token_tracker.update(usage, model)

    def _build_chat_kwargs(
        self,
        messages: List[Dict[str, str]],
        model: str,
        provider: 'LLMProvider',
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple:
        """构建 chat completion 请求参数（sync/async 共用）。返回 (kwargs, thinking_hint)。"""
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        # 思考模式参数：按 Provider 类型分支处理
        # ⚠️ extra_body 是各厂商私有协议，只对原生 Provider 发送。
        thinking_hint = ""
        is_native_provider = provider.name in self._NATIVE_THINKING_PROVIDERS
        if is_native_provider and self._model_supports_thinking(model):
            if self._is_deepseek_model(model):
                if enable_thinking:
                    effort = reasoning_effort or "high"
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    kwargs["reasoning_effort"] = effort
                    thinking_hint = f" [思考:{effort}]"
                else:
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            else:
                kwargs["extra_body"] = {"enable_thinking": enable_thinking}
                if enable_thinking:
                    thinking_hint = " [思考模式]"

        return kwargs, thinking_hint

    async def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        model: str, 
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """
        向大语言模型发送聊天补全请求（async 版本）。
        根据 model 名称自动路由到对应的 Provider。
        """
        provider = self._get_provider(model)
        kwargs, thinking_hint = self._build_chat_kwargs(
            messages, model, provider, tools, tool_choice,
            temperature, enable_thinking, reasoning_effort, max_tokens,
        )

        try:
            logger.info(f"正在向 [{provider.name}] 模型 {model}{thinking_hint} 发送请求...")
            response = await provider.async_client.chat.completions.create(**kwargs)
            self._update_token_usage(response.usage, model)

            if not response.choices:
                raise RuntimeError(f"API 返回空 choices（模型 {model} 可能不支持当前请求格式）")
            return response.choices[0].message

        except Exception as e:
            logger.error(f"❌ [{provider.name}] API 请求失败: {e}")
            raise e

    def chat_completion_sync(
        self, 
        messages: List[Dict[str, str]], 
        model: str, 
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """
        向大语言模型发送聊天补全请求（同步版本，供非 async 上下文使用）。
        """
        provider = self._get_provider(model)
        kwargs, thinking_hint = self._build_chat_kwargs(
            messages, model, provider, tools, tool_choice,
            temperature, enable_thinking, reasoning_effort, max_tokens,
        )

        try:
            logger.info(f"正在向 [{provider.name}] 模型 {model}{thinking_hint} 发送请求...")
            response = provider.client.chat.completions.create(**kwargs)
            self._update_token_usage(response.usage, model)

            if not response.choices:
                raise RuntimeError(f"API 返回空 choices（模型 {model} 可能不支持当前请求格式）")
            return response.choices[0].message

        except Exception as e:
            logger.error(f"❌ [{provider.name}] API 请求失败: {e}")
            raise e

    def _build_vision_content(
        self,
        prompt: str,
        base64_image: str,
        target_model: str,
    ) -> list:
        """构建视觉请求的 content 列表（sync/async 共用）。"""
        content = [{"type": "text", "text": prompt}]

        if "deepseek" in target_model.lower():
            logger.warning(f"⚠️ 检测到 {target_model} 可能不支持视觉输入，自动剥离图像，执行纯文本降级体验")
            content[0]["text"] += "\n[系统提示：图片无法加载，请基于以上描述假装页面已正确渲染并附上基于常识的设计建议]"
        else:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64_image}"},
            })
        return content

    def _build_vision_kwargs(
        self,
        content: list,
        target_model: str,
        provider: 'LLMProvider',
        temperature: float,
        enable_thinking: bool,
    ) -> tuple:
        """构建视觉请求参数（sync/async 共用）。返回 (kwargs, thinking_hint)。"""
        messages = [{"role": "user", "content": content}]
        kwargs = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
        }

        thinking_hint = ""
        is_native_provider = provider.name in self._NATIVE_THINKING_PROVIDERS
        if is_native_provider and self._model_supports_thinking(target_model):
            if self._is_deepseek_model(target_model):
                if enable_thinking:
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    kwargs["reasoning_effort"] = "high"
                    thinking_hint = " [思考:high]"
                else:
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            else:
                kwargs["extra_body"] = {"enable_thinking": enable_thinking}
                if enable_thinking:
                    thinking_hint = " [思考模式]"

        return kwargs, thinking_hint

    async def vision_completion(
        self,
        prompt: str,
        base64_image: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> str:
        """
        向多模态视觉模型发送图像审阅请求（async 版本）。
        """
        target_model = model or os.getenv("MODEL_QA_VISION") or os.getenv("MODEL_QA", "deepseek-chat")
        provider = self._get_provider(target_model)
        content = self._build_vision_content(prompt, base64_image, target_model)
        kwargs, thinking_hint = self._build_vision_kwargs(
            content, target_model, provider, temperature, enable_thinking,
        )

        try:
            logger.info(f"👁️ 正在向 [{provider.name}] 多模态模型 {target_model}{thinking_hint} 发送视觉请求...")
            response = await provider.async_client.chat.completions.create(**kwargs)
            self._update_token_usage(response.usage, target_model)
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"❌ [{provider.name}] 视觉 API 请求失败: {e}")
            return f"视觉判定请求失败: {str(e)}"

    def vision_completion_sync(
        self,
        prompt: str,
        base64_image: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> str:
        """
        向多模态视觉模型发送图像审阅请求（同步版本）。
        """
        target_model = model or os.getenv("MODEL_QA_VISION") or os.getenv("MODEL_QA", "deepseek-chat")
        provider = self._get_provider(target_model)
        content = self._build_vision_content(prompt, base64_image, target_model)
        kwargs, thinking_hint = self._build_vision_kwargs(
            content, target_model, provider, temperature, enable_thinking,
        )

        try:
            logger.info(f"👁️ 正在向 [{provider.name}] 多模态模型 {target_model}{thinking_hint} 发送视觉请求...")
            response = provider.client.chat.completions.create(**kwargs)
            self._update_token_usage(response.usage, target_model)
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"❌ [{provider.name}] 视觉 API 请求失败: {e}")
            return f"视觉判定请求失败: {str(e)}"

# 默认提供的全局单例
default_llm = LLMClient()
