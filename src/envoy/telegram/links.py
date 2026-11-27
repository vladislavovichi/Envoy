def topic_link(bot_username: str, thread_id: int) -> str:
    return f"https://t.me/{bot_username.removeprefix('@')}/{thread_id}"
