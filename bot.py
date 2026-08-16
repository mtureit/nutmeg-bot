"""Phase 2: the Slack bot (PLAN.md 9, 17.4, 17.6).

Socket Mode, so no public URL or inbound port is needed.

Three things shape the request path:

- Access control runs before anything else. app_mention is open to anyone in the
  workspace, including external guests, so channel and guest checks come first.
- Bolt acknowledges the event before the listener body runs, but embedding and
  generation are two network round trips, so the work is handed to a thread pool
  to keep the socket handler responsive.
- API failures produce an explicit error message. Silently answering "not found"
  would claim the knowledge base lacks an answer it may well have.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
import llm_client
import retriever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nutmeg-bot")

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_executor = ThreadPoolExecutor(max_workers=4)


def is_allowed_channel(channel_id: str) -> bool:
    if not config.ALLOWED_SLACK_CHANNELS:
        return True
    return channel_id in config.ALLOWED_SLACK_CHANNELS


def is_allowed_user(client, user_id: str) -> bool:
    """Refuse external guests unless explicitly allowed (PLAN.md 17.4)."""
    if config.ALLOW_GUEST_USERS:
        return True
    try:
        info = client.users_info(user=user_id)
    except Exception as exc:  # noqa: BLE001 - fail closed on lookup problems
        logger.warning("users.info failed for %s: %s", user_id, exc)
        return False

    user = info.get("user", {})
    if user.get("is_stranger") or user.get("is_restricted") or user.get("is_ultra_restricted"):
        return False
    return True


def answer(question: str) -> str:
    contexts = retriever.search(question)
    if contexts is None:
        return config.NOT_FOUND_MESSAGE
    return llm_client.generate_answer(question, contexts)


def _handle(event: dict, say, client) -> None:
    channel = event.get("channel", "")
    user = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")

    if not is_allowed_channel(channel):
        logger.info("channel not allowed: %s", channel)
        return
    if not is_allowed_user(client, user):
        logger.info("user not allowed: %s", user)
        return

    question = _MENTION_RE.sub("", event.get("text", "")).strip()
    if not question:
        say(text="質問を書いてメンションしてください。", thread_ts=thread_ts)
        return

    try:
        reply = answer(question)
    except Exception as exc:  # noqa: BLE001 - surface an honest error to the user
        logger.error("failed to answer: %s", exc)
        say(text=config.ERROR_MESSAGE, thread_ts=thread_ts)
        return

    # Log identifiers and outcome only. Question text is not recorded (PLAN.md 17.6).
    found = reply != config.NOT_FOUND_MESSAGE
    logger.info("answered user=%s channel=%s found=%s", user, channel, found)
    say(text=reply, thread_ts=thread_ts)


def build_app() -> App:
    """Construct the Bolt app.

    Built lazily rather than at import time so the module stays importable (and
    the access-control helpers stay testable) before any token exists.
    """
    config.require("SLACK_BOT_TOKEN")
    app = App(token=config.SLACK_BOT_TOKEN)

    @app.event("app_mention")
    def handle_mention(event, say, client):  # noqa: ANN001 - Bolt injects these
        _executor.submit(_handle, event, say, client)

    return app


def main() -> int:
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OPENAI_API_KEY"):
        config.require(name)

    logger.info("starting nutmeg-bot (model=%s)", config.GEN_MODEL)
    SocketModeHandler(build_app(), config.SLACK_APP_TOKEN).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
