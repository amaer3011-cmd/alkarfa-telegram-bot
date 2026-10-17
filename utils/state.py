"""
utils/state.py
================
A single, shared "what is this user in the middle of doing" store.

Now that the bot is fully button-driven, several flows need one follow-up
message from the user (a photo, a caption template, a channel id, a new
filename). Rather than have every handler module keep its own private
dict and risk two of them fighting over the same incoming message, all
modules read/write through this one state store. The generic text/photo
router in `handlers/common.py` looks at the current state and dispatches
to the right processing function.

In-memory only — fine for a single-process deployment. For a
multi-worker deployment, back this with Redis instead.
"""

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict


class UserState(Enum):
    NONE = auto()
    AWAITING_FILENAME = auto()
    AWAITING_THUMB_PHOTO = auto()
    AWAITING_THUMB_CONFIRM = auto()
    AWAITING_CAPTION_TEXT = auto()
    AWAITING_WATERMARK_PATTERNS = auto()
    AWAITING_WATERMARK_PREVIEW_PDF = auto()
    AWAITING_CHANNEL_BIND_ID = auto()
    AWAITING_CHANNEL_UNBIND_ID = auto()
    AWAITING_CHANNEL_THUMB_ID = auto()
    AWAITING_CHANNEL_THUMB_PHOTO = auto()
    AWAITING_CHANNEL_CAPTION_ID = auto()
    AWAITING_CHANNEL_CAPTION_TEXT = auto()
    AWAITING_CHANNEL_WATERMARK_ID = auto()
    AWAITING_CHANNEL_WATERMARK_PATTERNS = auto()
    AWAITING_COVER_PHOTO = auto()
    AWAITING_FILE_COVER = auto()
    AWAITING_CHANNEL_COVER_ID = auto()
    AWAITING_CHANNEL_COVER_PHOTO = auto()
    AWAITING_CHANNEL_DELCOVER_ID = auto()
    AWAITING_ADMIN_USER_QUERY = auto()
    AWAITING_ADMIN_BAN_ID = auto()
    AWAITING_ADMIN_UNBAN_ID = auto()
    AWAITING_ADMIN_BROADCAST = auto()
    AWAITING_ADMIN_WELCOME = auto()
    AWAITING_ADMIN_DM = auto()
    AWAITING_ADMIN_DURATION = auto()
    AWAITING_ADMIN_LIMIT = auto()
    AWAITING_ADMIN_BANTEXT = auto()
    ADMIN_CHAT = auto()  # owner is chatting with a user through the bot
    AWAITING_VIDEO_WM_TEXT = auto()
    AWAITING_VIDEO_WM_IMAGE = auto()
    AWAITING_VIDEO_TITLE_TEMPLATE = auto()
    AWAITING_FILE_VIDEO_TITLE = auto()
    AWAITING_CUSTOM_LINK = auto()
    # PDF studio
    STUDIO_COLLECT = auto()   # collecting several PDFs (merge) or images (to PDF)
    STUDIO_WAIT_PDF = auto()  # waiting for the single PDF a tool works on
    STUDIO_WAIT_TEXT = auto() # waiting for page numbers / a number


@dataclass
class StateEntry:
    state: UserState = UserState.NONE
    data: Dict[str, Any] = field(default_factory=dict)


_states: Dict[int, StateEntry] = {}


def _remove_tmp(paths) -> None:
    """Delete the temp files a flow registered under data["_tmp"] (a list)."""
    for path in list(paths or ()):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def set_state(user_id: int, state: UserState, **data: Any) -> None:
    """Set the user's current state, optionally attaching arbitrary data
    (e.g. a channel_id gathered in an earlier step of a multi-step flow).

    A flow may keep downloaded files in data["_tmp"]; when the state is
    replaced or cleared those files are deleted, unless the new state carries
    the very same list over (a flow moving on to its next step)."""
    old = _states.get(user_id)
    if old is not None and old.data.get("_tmp") is not data.get("_tmp"):
        _remove_tmp(old.data.get("_tmp"))
    _states[user_id] = StateEntry(state=state, data=data)


def all_states() -> Dict[int, StateEntry]:
    """Every live state (used to expire abandoned flows). Don't mutate the returned dict."""
    return _states


def get_state(user_id: int) -> StateEntry:
    """Always returns a StateEntry — defaults to UserState.NONE if unset."""
    return _states.get(user_id, StateEntry())


def clear_state(user_id: int) -> None:
    old = _states.pop(user_id, None)
    if old is not None:
        _remove_tmp(old.data.get("_tmp"))
