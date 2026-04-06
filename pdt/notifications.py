import subprocess


def send_notification(title: str, msg: str):
    """Desktop notification — tries plyer, then notify-send, then osascript."""
    try:
        from plyer import notification  # type: ignore
        notification.notify(title=title, message=msg, app_name="PDT", timeout=30)
        return
    except Exception:
        pass
    try:
        subprocess.run(["notify-send", "-t", "30000", title, msg], check=False)
        return
    except FileNotFoundError:
        pass
    try:
        # Escape double-quotes to prevent AppleScript injection
        safe_title = title.replace('"', '\\"')
        safe_msg   = msg.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            check=False,
        )
    except FileNotFoundError:
        pass
