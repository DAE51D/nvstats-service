#!/usr/bin/env python3
"""Start the daemon on a private session bus and verify Sample.Get()."""

import json
import shlex
import subprocess
import sys
import time

SERVICE = [sys.executable, "service/nvstats_service.py"]
CALL = [
    "busctl",
    "--user",
    "call",
    "com.daevid.nvstats",
    "/com/daevid/nvstats",
    "com.daevid.nvstats.Sample",
    "Get",
]


def main():
    service = subprocess.Popen(
        SERVICE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 12
    last_error = "service did not acquire its D-Bus name"
    try:
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(CALL, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    fields = shlex.split(result.stdout)
                    sample = json.loads(fields[1])
                    if not isinstance(sample.get("gpu"), dict) or not isinstance(
                        sample.get("sys"), dict
                    ):
                        raise RuntimeError("D-Bus returned an invalid sample shape")
                    llm = sample.get("llm", {})
                    if not all(
                        isinstance(llm.get(engine), list)
                        for engine in ("llama", "ollama", "vllm", "sglang", "tgi")
                    ):
                        raise RuntimeError("D-Bus returned an invalid LLM sample shape")
                    if not sample["sys"].get("ram_total"):
                        last_error = "waiting for the first telemetry poll"
                        time.sleep(0.25)
                        continue
                    gpu = sample["gpu"]
                    print(
                        "D-Bus sample OK: GPU {}% · GTT {}/{} MiB · {}°C".format(
                            gpu.get("util"),
                            gpu.get("mem_used"),
                            gpu.get("mem_total"),
                            gpu.get("temp"),
                        )
                    )
                    return
                last_error = result.stderr.strip() or "busctl call failed"
            except (
                OSError,
                subprocess.TimeoutExpired,
                IndexError,
                json.JSONDecodeError,
            ) as error:
                last_error = str(error)
            time.sleep(0.25)
        raise RuntimeError(last_error)
    finally:
        service.terminate()
        try:
            service.wait(timeout=5)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=5)


if __name__ == "__main__":
    main()
