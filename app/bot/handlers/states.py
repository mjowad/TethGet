"""
Shared ConversationHandler state constants.

Pulled into their own module so alarm_flow.py and quota_flow.py (which
call into each other's handlers) don't need a circular import just to
reference the same state integers.
"""

# Alarm creation flow.
WAITING_CONDITION, WAITING_NUMBER, WAITING_FREQUENCY = range(3)

# Profile & Management Hub (v1.1) conversation states.
(
    PROFILE_MAIN,
    PROFILE_ALARMS,
    PROFILE_NEWS,
    PROFILE_EDIT_ALARM,
    PROFILE_EDIT_PRICE,
    PROFILE_EDIT_CONDITION,
    PROFILE_EDIT_FREQUENCY,
) = range(3, 10)

# News Flow States
NEWS_MAIN, NEWS_QUICK_EDIT = range(10, 12)

MAX_ACTIVE_ALARMS = 10