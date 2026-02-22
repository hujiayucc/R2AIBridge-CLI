from typing import Optional, Tuple

from openai import OpenAI

from lib.config import DEFAULT_AI_TIMEOUT


class AIClientSingleton:
    _client: Optional[OpenAI] = None
    _key: Optional[Tuple[str, str, int]] = None

    @classmethod
    def get_client(cls, api_key: str, base_url: str, timeout: int = DEFAULT_AI_TIMEOUT) -> OpenAI:
        k = (str(api_key or ""), str(base_url or ""), int(timeout))
        if cls._client is None or cls._key != k:
            cls._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
            cls._key = k
        return cls._client
