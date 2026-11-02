import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from nvstats_poll import _classify_llm, _llm_model


class LlmDetectionTests(unittest.TestCase):
    def test_existing_engines(self):
        self.assertEqual(_classify_llm(["/opt/llama/llama-server", "--model", "m.gguf"]), "llama")
        self.assertEqual(_classify_llm(["ollama", "serve"]), "ollama")

    def test_vllm_console_and_python_module(self):
        self.assertEqual(_classify_llm(["vllm", "serve", "org/model"]), "vllm")
        argv = ["python", "-m", "vllm.entrypoints.openai.api_server", "--model", "org/model"]
        self.assertEqual(_classify_llm(argv), "vllm")
        self.assertEqual(_llm_model("vllm", argv), "org/model")

    def test_sglang_launch_forms(self):
        argv = ["sglang", "serve", "--model-path", "org/model", "--port", "30000"]
        self.assertEqual(_classify_llm(argv), "sglang")
        self.assertEqual(_llm_model("sglang", argv), "org/model")
        module_argv = ["python", "-m", "sglang.launch_server", "--model-path=org/model"]
        self.assertEqual(_classify_llm(module_argv), "sglang")
        self.assertEqual(_llm_model("sglang", module_argv), "org/model")

    def test_tgi_launcher(self):
        argv = ["text-generation-launcher", "--model-id", "org/model", "--port", "8080"]
        self.assertEqual(_classify_llm(argv), "tgi")
        self.assertEqual(_llm_model("tgi", argv), "org/model")

    def test_non_model_clients_are_ignored(self):
        self.assertIsNone(_classify_llm(["code", "--extension", "vllm-helper"]))
        self.assertIsNone(_classify_llm(["python", "my_app.py", "--model", "org/model"]))
        self.assertIsNone(_classify_llm([]))


if __name__ == "__main__":
    unittest.main()
