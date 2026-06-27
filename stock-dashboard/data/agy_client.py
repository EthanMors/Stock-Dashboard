"""Antigravity (`agy`) CLI client.

Replaces the deprecated `gemini.cmd` invocation. The old Gemini CLI accepted a
prompt on stdin and printed the answer to stdout, so it could be driven with a
plain ``subprocess.run``. The Antigravity CLI (``agy``) that replaced it is an
agentic TUI: in print mode (``-p``) it only renders its response to a real
terminal. When stdout is redirected to a pipe it produces *no* output, and when
stdin reaches EOF it shuts down mid-stream before the answer is written.

To capture the response we therefore run ``agy`` inside a Windows pseudo-console
(ConPTY, via ``pywinpty``) so it behaves exactly as it does in an interactive
terminal, then strip the terminal control sequences from what it draws.

Authentication uses the user's logged-in Antigravity / Gemini subscription
(keyring OAuth, ``authMethod=consumer``) — there is no API key involved, which
matches the project requirement of using the Pro subscription via the CLI.
"""

import re
import time

try:
    from winpty import PtyProcess
except ImportError:  # pragma: no cover - surfaced as an error string at call time
    PtyProcess = None


# Model labels exactly as reported by ``agy models``.
FLASH_MODEL = "Gemini 3.5 Flash (Medium)"  # default; equivalent to old flash tier
PRO_MODEL = "Gemini 3.1 Pro (High)"        # replaces the old gemini-2.5-pro tier

# Terminal escape sequences emitted by the CLI's TUI renderer.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"            # CSI sequences (cursor, color, mode)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[=>]"                          # keypad mode
    r"|\x1b\([AB0]"                       # charset selection
)
# Braille glyphs used by the progress spinner.
_SPINNER_RE = re.compile(r"[⠀-⣿]")


def _clean(raw: str) -> str:
    """Strip terminal control sequences and spinner noise from PTY output."""
    text = _ANSI_RE.sub("", raw)
    text = _SPINNER_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    # Drop transient progress lines the TUI may leave in the scrollback.
    drop = ("available models...", "Fetching", "Thinking", "Loading")
    lines = [ln for ln in text.split("\n") if ln.strip() and not ln.strip().startswith(drop)]
    return "\n".join(lines).strip()


def run_agy(prompt: str, model: str | None = None, timeout: int = 120,
            width: int = 1000) -> tuple[str, str]:
    """Run a single prompt through the Antigravity CLI.

    Args:
        prompt: The full prompt text.
        model: An ``agy`` model label (see ``FLASH_MODEL`` / ``PRO_MODEL``).
            ``None`` uses the CLI default (Gemini 3.5 Flash).
        timeout: Hard wall-clock cap in seconds.
        width: PTY column width; kept wide so long JSON lines are not wrapped.

    Returns:
        ``(stdout, stderr)``. On success ``stdout`` is the cleaned response text
        and ``stderr`` is ``""``. On timeout or failure ``stdout`` is ``""`` and
        ``stderr`` holds an error message.
    """
    if PtyProcess is None:
        return "", "pywinpty is not installed (pip install pywinpty)"

    args = ["agy", "--dangerously-skip-permissions"]
    if model:
        args += ["--model", model]
    args += ["-p", prompt]

    try:
        proc = PtyProcess.spawn(args, dimensions=(50, width))
    except Exception as exc:  # spawn failure (agy missing, ConPTY error, ...)
        return "", f"Failed to start agy: {exc}"

    buf: list[str] = []
    start = last = time.time()
    timed_out = False
    idle_cutoff = 8.0  # seconds of silence after some output => turn complete

    try:
        while True:
            try:
                data = proc.read(8192)
            except EOFError:
                break
            now = time.time()
            if data:
                buf.append(data)
                last = now
            else:
                time.sleep(0.1)
            # Print mode exits on its own once the answer is rendered.
            if not proc.isalive() and now - last > 1:
                break
            # Safety net: the spinner keeps the stream active while the model
            # thinks, so silence this long means the turn really has finished.
            if buf and now - last > idle_cutoff:
                break
            if now - start > timeout:
                timed_out = True
                break
    finally:
        try:
            proc.terminate(force=True)
        except Exception:
            pass

    out = _clean("".join(buf))
    if timed_out and not out:
        return "", f"agy timed out after {timeout}s"
    return out, ("" if out else "agy returned no output")
