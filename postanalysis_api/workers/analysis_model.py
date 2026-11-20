"""Cancellable source-algorithm model adapter using existing crowd configuration."""
import asyncio
import os

import httpx


class AnalysisCancelled(BaseException):
    """Escape source retry guards immediately on owner-requested cancellation."""


class AnalysisModel:
    def __init__(self, checkpoint):
        from data_service.connectors.models import model_configs
        provider = model_configs()['GLM-5.3-Flash-8800']
        self.base = os.environ.get('CROWDSIM_LLM_BASE_URL', provider['base_url']).rstrip('/')
        self.model = os.environ.get('CROWDSIM_POST_LLM_MODEL') or os.environ.get('CROWDSIM_LLM_MODEL') or provider['model']
        self.token = os.environ.get('CROWDSIM_POST_LLM_API_KEY') or provider['api_key']
        self.checkpoint, self.unavailable = checkpoint, False
        self.calls = 0

    def __call__(self, prompt, *, timeout=60):
        self.checkpoint()
        if self.unavailable:
            raise ConnectionError('本次分析的模型端点不可达；保留数值结果与未完成标记')
        return asyncio.run(self._request(prompt, timeout))

    async def _request(self, prompt, timeout):
        from crowdsim.domain.llm_generation import generation_options
        body = {'model': self.model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': False,
                **generation_options(self.model, 4096, {'temperature': .15,
                    'chat_template_kwargs': {'enable_thinking': False}, 'response_format': {'type': 'json_object'}})}
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        self.calls += 1
        async with httpx.AsyncClient(timeout=min(float(timeout), 60), trust_env=False) as client:
            pending = asyncio.create_task(client.post(self.base + '/chat/completions', json=body, headers=headers))
            try:
                while not pending.done():
                    self.checkpoint()
                    await asyncio.wait([pending], timeout=.1)
                response = await pending
                response.raise_for_status()
                choice = response.json()['choices'][0]
                if choice.get('finish_reason') == 'length' or choice['message'].get('tool_calls'):
                    raise ValueError('模型答复截断或包含未执行的工具调用')
                from algorithms.powergame.extraction import decode_object
                return decode_object(choice['message']['content'])
            except (httpx.ConnectError, httpx.TimeoutException):
                self.unavailable = True
                raise ConnectionError('事后分析模型服务不可达或请求超时') from None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403, 404}:
                    self.unavailable = True
                raise ConnectionError(f'事后分析模型接口返回HTTP {exc.response.status_code}') from None
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


class ForecastClient:
    def __init__(self, caller):
        self.caller = caller

    def chat(self, prompt, *, parse_json=True):
        return self.caller(prompt, timeout=60)
