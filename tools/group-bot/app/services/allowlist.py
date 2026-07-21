"""Authorization: group allow-list + admin DM allow-list.

Ported from the Hermes gateway's `LINE_ALLOWED_GROUPS` +
`GATEWAY_ALLOW_ALL_USERS=true` combination: a group message is answerable
iff it comes from a group whose groupId is in the allow-list — any member
of that group can trigger a reply. Non-listed groups are always ignored.

DMs are separately gated by `LINE_ALLOWED_USERS` — only those userIds get
routed to the unrestricted admin agent; everyone else's DM is ignored.
"""

from app.config import get_settings


def is_group_allowed(group_id: str) -> bool:
    if not group_id:
        return False
    return group_id in get_settings().allowed_group_ids


def is_user_admin(user_id: str) -> bool:
    if not user_id:
        return False
    return user_id in get_settings().allowed_admin_user_ids
