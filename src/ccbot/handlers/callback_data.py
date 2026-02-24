"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>

# Profile system
CB_PROFILE_LAUNCH = "pf:launch:"   # pf:launch:<slug>
CB_PROFILE_SUSPEND = "pf:suspend:" # pf:suspend:<slug>
CB_PROFILE_INFO = "pf:info:"       # pf:info:<slug>

# Launch builder wizard
CB_LB_BACKEND = "lb:b:"     # lb:b:claude / lb:b:opencode
CB_LB_MODEL = "lb:m:"       # lb:m:opus / lb:m:sonnet / lb:m:default / lb:m:haiku
CB_LB_DIR = "lb:d:"         # lb:d:<index> into known dirs list
CB_LB_DIR_BROWSE = "lb:d:browse"
CB_LB_FLAG = "lb:f:"        # lb:f:skip / lb:f:resume — toggles
CB_LB_GO = "lb:go"          # launch with current settings
CB_LB_SAVE = "lb:save"      # prompt to save as profile
CB_LB_PROFILE = "lb:pf:"    # lb:pf:<slug> — quick launch saved profile
