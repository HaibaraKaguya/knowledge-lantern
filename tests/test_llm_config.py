"""配置默认值 / OpenAI-compatible 客户端构造 — 可配置端点 + 不继承系统代理。

覆盖：
  - 默认端点是本机 OpenAI-compatible /v1 示例；
  - 环境变量覆盖 config.yaml / 类型强转；
  - resolve_api_key：本地留空→占位符；远程留空→报错（无云端回退）；
  - 客户端：httpx trust_env=False —— 绝不继承系统代理。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# 让 src/ 可被当作包导入（src.config），同时顶层包 access/paper_qa 也可用。
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config as config_mod  # noqa: E402
from config import _DEFAULTS, _Settings, is_local_endpoint, resolve_api_key  # noqa: E402


class TestDefaults(unittest.TestCase):
    def test_default_endpoint_is_local_openai_compatible(self):
        base = _DEFAULTS["api_base_url"]
        self.assertIn("127.0.0.1", base, "默认 base_url 必须是本地端点")
        self.assertTrue(base.endswith("/v1"), "OpenAI-compatible 端点以 /v1 结尾")
        self.assertTrue(_DEFAULTS["model"])

    def test_no_cloud_endpoint_in_defaults(self):
        base = _DEFAULTS["api_base_url"]
        self.assertNotIn("api.", base)
        self.assertNotIn("https://", base)

    def test_env_overrides_config_yaml(self):
        with mock.patch.dict(os.environ, {
            "SRC_API_BASE_URL": "http://127.0.0.1:8080/v1",
            "SRC_MODEL": "test-model",
            "SRC_API_KEY": "env-key",
        }, clear=True):
            s = _Settings()
        self.assertEqual(s.api_base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(s.model, "test-model")
        self.assertEqual(s.api_key, "env-key")

    def test_env_type_coercion(self):
        with mock.patch.dict(os.environ, {
            "SRC_TEMPERATURE": "0.5",
            "SRC_TIMEOUT": "30",
            "SRC_MAX_TOKENS": "4096",
        }, clear=True):
            s = _Settings()
        self.assertEqual(s.temperature, 0.5)
        self.assertEqual(s.timeout, 30)
        self.assertEqual(s.max_tokens, 4096)


class TestEndpointHelpers(unittest.TestCase):
    def test_is_local_endpoint(self):
        self.assertTrue(is_local_endpoint("http://127.0.0.1:8080/v1"))
        self.assertTrue(is_local_endpoint("http://localhost:8080/v1"))
        self.assertTrue(is_local_endpoint("unix:///tmp/llm.sock"))
        self.assertTrue(is_local_endpoint("http://0.0.0.0:8080/v1"))
        self.assertFalse(is_local_endpoint("https://api.example.com/v1"))
        self.assertFalse(is_local_endpoint(""))

    def test_resolve_api_key_local_empty_uses_placeholder(self):
        # 本地服务：空 key + 本地端点 → "not-needed"
        self.assertEqual(resolve_api_key("", "http://127.0.0.1:8080/v1"), "not-needed")

    def test_resolve_api_key_remote_empty_raises(self):
        # 空 key + 远程端点 → 报错，绝不做云端回退
        with self.assertRaises(RuntimeError):
            resolve_api_key("", "https://api.example.com/v1")

    def test_resolve_api_key_provided(self):
        self.assertEqual(resolve_api_key("sk-abc", "https://api.example.com/v1"), "sk-abc")
        self.assertEqual(resolve_api_key("sk-abc", "http://127.0.0.1:8080/v1"), "sk-abc")


class TestModelClientNoProxy(unittest.TestCase):
    """模型服务请求不得继承系统代理。"""

    def test_client_uses_trust_env_false(self):
        from paper_qa.client import OpenAICompatibleClient

        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8080/v1", model="m", api_key="not-needed",
            temperature=0.3, timeout=30, max_tokens=1024,
        )
        try:
            self.assertFalse(client._http.trust_env,
                             "httpx trust_env 必须为 False，禁止读取系统代理")
            # 显式设置系统代理后，客户端也不应认为自己在走代理。
            with mock.patch.dict(os.environ, {
                "HTTP_PROXY": "http://proxy.example.com:3128",
                "HTTPS_PROXY": "http://proxy.example.com:3128",
                "http_proxy": "http://proxy.example.com:3128",
                "https_proxy": "http://proxy.example.com:3128",
            }):
                self.assertFalse(client._http.trust_env)
        finally:
            client.close()

    def test_get_llm_produces_local_client(self):
        from paper_qa.client import get_llm

        with mock.patch.dict(config_mod.settings._data, {
            "api_key": "", "api_base_url": "http://127.0.0.1:8080/v1",
            "model": "test-model", "temperature": 0.3,
            "timeout": 120, "max_tokens": 8192,
        }, clear=False):
            client = get_llm()
        try:
            self.assertEqual(client.api_key, "not-needed")
            self.assertEqual(client.base_url, "http://127.0.0.1:8080/v1")
            self.assertFalse(client._http.trust_env)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
