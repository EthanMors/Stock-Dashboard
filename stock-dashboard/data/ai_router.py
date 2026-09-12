import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

from data.gemini_tracker import record_call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model Configuration & Defaults
# ---------------------------------------------------------------------------
# Default flagship AGY models:
# - Flash: gemini-3.8-flash-medium (Ultra-fast ~5s latency, high quota, robust)
# - Pro: gemini-3.1-pro-low (Deep reasoning ~15s latency, thorough analysis)
# - Fallback: gemini-3.8-flash-high (When Pro times out or hits limits)
FLASH_MODEL = os.getenv("AGY_FLASH_MODEL", "gemini-3.8-flash-medium")
PRO_MODEL = os.getenv("AGY_PRO_MODEL", "gemini-3.1-pro-low")
PRO_FALLBACK_MODEL = os.getenv("AGY_PRO_FALLBACK_MODEL", "gemini-3.8-flash-high")

DEFAULT_TIMEOUT_FLASH = int(os.getenv("AGY_TIMEOUT_FLASH", "60"))
DEFAULT_TIMEOUT_PRO = int(os.getenv("AGY_TIMEOUT_PRO", "180"))

# Short in-memory cache to avoid burning daily quota on page re-renders / re-computes
_CACHE_TTL_SECONDS = int(os.getenv("AGY_CACHE_TTL", "900"))  # 15 minutes default
_MEMORY_CACHE: dict[str, tuple[float, str, str]] = {}  # key -> (timestamp, stdout, stderr)


# ---------------------------------------------------------------------------
# CLI Executable Discovery
# ---------------------------------------------------------------------------
def _find_cli_executable() -> tuple[str, str]:
    """Find the best available AI CLI executable on the system.

    Returns:
        (cli_path, cli_type) where cli_type is 'agy', 'gemini', or 'missing'.
    """
    env_override = os.getenv("AGY_CLI_PATH")
    if env_override and os.path.exists(env_override):
        cli_type = "agy" if "agy" in os.path.basename(env_override).lower() else "gemini"
        return env_override, cli_type

    # 1. Search PATH for agy
    agy_path = shutil.which("agy") or shutil.which("agy.exe")
    if agy_path:
        return agy_path, "agy"

    # 2. Check standard Windows AppData location for agy
    local_appdata = os.getenv("LOCALAPPDATA", "")
    if local_appdata:
        standard_agy = os.path.join(local_appdata, "agy", "bin", "agy.exe")
        if os.path.exists(standard_agy):
            return standard_agy, "agy"

    # 3. Fallback: Check for legacy gemini CLI
    gemini_path = shutil.which("gemini.cmd") or shutil.which("gemini")
    if gemini_path:
        return gemini_path, "gemini"

    return "", "missing"


_CLI_PATH, _CLI_TYPE = _find_cli_executable()


def get_active_models() -> dict[str, Any]:
    """Return dictionary of current AI configuration and discovered CLI."""
    global _CLI_PATH, _CLI_TYPE
    if not _CLI_PATH:
        _CLI_PATH, _CLI_TYPE = _find_cli_executable()
    return {
        "cli_path": _CLI_PATH,
        "cli_type": _CLI_TYPE,
        "flash_model": FLASH_MODEL,
        "pro_model": PRO_MODEL,
        "fallback_model": PRO_FALLBACK_MODEL,
        "available": bool(_CLI_PATH),
    }


# ---------------------------------------------------------------------------
# Cache Helpers
# ---------------------------------------------------------------------------
def _cache_key(model: str, prompt: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"::")
    h.update(prompt.strip().encode("utf-8"))
    return h.hexdigest()


def _get_from_cache(key: str) -> tuple[str, str] | None:
    if key in _MEMORY_CACHE:
        ts, stdout, stderr = _MEMORY_CACHE[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            return stdout, stderr
        del _MEMORY_CACHE[key]
    return None


def _save_to_cache(key: str, stdout: str, stderr: str) -> None:
    _MEMORY_CACHE[key] = (time.time(), stdout, stderr)
    # Prune expired entries if cache grows large
    if len(_MEMORY_CACHE) > 200:
        now = time.time()
        expired = [k for k, (ts, _, _) in _MEMORY_CACHE.items() if now - ts >= _CACHE_TTL_SECONDS]
        for k in expired:
            _MEMORY_CACHE.pop(k, None)


# Maximum safe command-line character length on Windows (OS limit is 32,767)
_MAX_WINDOWS_CMD_LEN = 25000


def _safe_trim_prompt(prompt: str, max_chars: int = _MAX_WINDOWS_CMD_LEN) -> str:
    """Ensure prompt does not exceed Windows command-line limit (WinError 206).

    Preserves the beginning (task context) and end (schema & rules).
    """
    if len(prompt) <= max_chars:
        return prompt

    head_len = int(max_chars * 0.55)
    tail_len = int(max_chars * 0.40)
    omitted = len(prompt) - (head_len + tail_len)
    logger.warning(
        f"Prompt length ({len(prompt)} chars) exceeds Windows cmd limit ({max_chars} chars). "
        f"Trimming {omitted} middle characters."
    )
    return (
        prompt[:head_len]
        + f"\n\n[... {omitted} characters of detailed data omitted to comply with OS command limits ...]\n\n"
        + prompt[-tail_len:]
    )


# ---------------------------------------------------------------------------
# Core Subprocess Runner
# ---------------------------------------------------------------------------
def _execute_cli(
    prompt: str,
    model: str,
    timeout: int,
    _retried_trim: bool = False,
) -> tuple[str, str, int]:
    """Execute low-level CLI command and return (stdout, stderr, returncode)."""
    global _CLI_PATH, _CLI_TYPE
    if not _CLI_PATH or not os.path.exists(_CLI_PATH):
        _CLI_PATH, _CLI_TYPE = _find_cli_executable()

    if not _CLI_PATH:
        return "", "Neither agy nor gemini CLI found in PATH or standard directories.", -1

    # Guard against Windows OS command-line character limit [WinError 206]
    safe_prompt = _safe_trim_prompt(prompt)

    try:
        if _CLI_TYPE == "agy":
            # agy accepts the prompt via -p and model via --model
            cmd = [
                _CLI_PATH,
                "-p",
                safe_prompt,
                "--model",
                model,
                "--disable-slash-commands",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode

        else:
            # Legacy gemini.cmd: passes prompt via stdin, headless flag -p ""
            cmd = [_CLI_PATH, "-p", ""]
            if model:
                # Map newer model names to legacy format if running on old gemini.cmd
                legacy_model = "gemini-2.5-pro" if "pro" in model.lower() else "gemini-1.5-flash"
                cmd = [_CLI_PATH, "-m", legacy_model, "-p", ""]

            result = subprocess.run(
                cmd,
                input=safe_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode

    except subprocess.TimeoutExpired:
        return "", f"CLI command timed out after {timeout}s", -2
    except OSError as exc:
        # Catch Windows [WinError 206] The filename or extension is too long
        is_win_206 = getattr(exc, "winerror", None) == 206 or "206" in str(exc) or "too long" in str(exc).lower()
        if is_win_206 and not _retried_trim:
            logger.warning("WinError 206 encountered. Aggressively trimming prompt to 16,000 chars and retrying.")
            aggressively_trimmed = _safe_trim_prompt(prompt, max_chars=16000)
            return _execute_cli(aggressively_trimmed, model, timeout, _retried_trim=True)
        return "", str(exc), -3
    except Exception as exc:
        return "", str(exc), -3



# ---------------------------------------------------------------------------
# High-Level AI Runner & JSON Parser
# ---------------------------------------------------------------------------
def run_ai(
    prompt: str,
    tier: str = "flash",
    timeout: int | None = None,
    model_override: str | None = None,
    fallback_to_flash: bool = True,
    use_cache: bool = True,
) -> tuple[str, str]:
    """Execute an AI analysis prompt using the configured CLI router.

    Args:
        prompt: The complete prompt to send.
        tier: 'flash' for fast tasks or 'pro' for deep reasoning.
        timeout: Timeout in seconds (defaults to tier setting).
        model_override: Specific model string (e.g. 'gemini-3.8-flash-high').
        fallback_to_flash: If Pro fails/times out, automatically retry with Flash.
        use_cache: Check and save response in short-term memory cache.

    Returns:
        tuple of (stdout_text, stderr_text).
    """
    tier = tier.lower().strip()
    if tier not in ("flash", "pro"):
        tier = "flash"

    selected_model = model_override or (PRO_MODEL if tier == "pro" else FLASH_MODEL)
    selected_timeout = timeout or (DEFAULT_TIMEOUT_PRO if tier == "pro" else DEFAULT_TIMEOUT_FLASH)

    # 1. Check cache
    cache_k = _cache_key(selected_model, prompt)
    if use_cache:
        cached = _get_from_cache(cache_k)
        if cached is not None:
            return cached

    # 2. Execute primary call
    t0 = time.time()
    stdout, stderr, rc = _execute_cli(prompt, selected_model, selected_timeout)
    latency = round(time.time() - t0, 2)

    # 3. Check for success
    if rc == 0 and stdout:
        record_call(tier)
        if use_cache:
            _save_to_cache(cache_k, stdout, stderr)
        return stdout, stderr

    # 4. Fallback logic: if Pro failed and fallback is enabled, try Flash fallback
    if tier == "pro" and fallback_to_flash:
        fallback_model = PRO_FALLBACK_MODEL
        logger.warning(
            f"Pro model ({selected_model}) failed (rc={rc}, err='{stderr}'). Falling back to {fallback_model}."
        )
        fb_stdout, fb_stderr, fb_rc = _execute_cli(prompt, fallback_model, DEFAULT_TIMEOUT_FLASH)
        if fb_rc == 0 and fb_stdout:
            record_call("flash")
            notice = f"[Note: Pro timed out/failed ({stderr or 'no response'}); completed via {fallback_model}]"
            if use_cache:
                _save_to_cache(cache_k, fb_stdout, notice)
            return fb_stdout, notice

    # Return whatever error output occurred
    err_msg = stderr if stderr else f"CLI exited with return code {rc} and empty output."
    return "", err_msg


def extract_json_from_text(raw: str) -> dict | list | None:
    """Extract and parse a JSON object or list from text, stripping markdown fences."""
    if not raw or not raw.strip():
        return None

    cleaned = raw.strip()

    # If wrapped in markdown code fence ```json ... ```, extract content
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Search for first { ... } or [ ... ]
    obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
    if obj_match:
        try:
            return json.loads(obj_match.group(1))
        except json.JSONDecodeError:
            pass

    arr_match = re.search(r"(\[[\s\S]*\])", cleaned)
    if arr_match:
        try:
            return json.loads(arr_match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def run_ai_json(
    prompt: str,
    tier: str = "flash",
    timeout: int | None = None,
    model_override: str | None = None,
    fallback_to_flash: bool = True,
    use_cache: bool = True,
    required_keys: list[str] | None = None,
) -> tuple[dict | list | None, str]:
    """Execute AI prompt and parse output as JSON.

    Returns:
        (parsed_json_dict_or_list, error_message_if_any)
    """
    raw_output, err = run_ai(
        prompt=prompt,
        tier=tier,
        timeout=timeout,
        model_override=model_override,
        fallback_to_flash=fallback_to_flash,
        use_cache=use_cache,
    )

    if not raw_output:
        return None, err or "Empty response from AI CLI"

    data = extract_json_from_text(raw_output)
    if data is None:
        return None, f"Failed to parse valid JSON from AI output: {raw_output[:200]}..."

    if required_keys and isinstance(data, dict):
        missing = [k for k in required_keys if k not in data]
        if missing:
            logger.warning(f"AI JSON missing expected keys: {missing}")

    return data, err
