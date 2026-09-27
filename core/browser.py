"""Shared Chromium launch helper. Points at the pre-installed browser in
this environment; override CUA_CHROMIUM_PATH if running elsewhere (a
normal `playwright install` Chromium works too)."""
import os

DEFAULT_CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def chromium_launch_kwargs(cdp_port: "int | None" = None, headless: bool = True) -> dict:
    path = os.environ.get("CUA_CHROMIUM_PATH", DEFAULT_CHROMIUM_PATH)
    args = ["--no-sandbox"]
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=0.0.0.0")
    kwargs = {"headless": headless, "args": args}
    if os.path.exists(path):
        kwargs["executable_path"] = path
    return kwargs
