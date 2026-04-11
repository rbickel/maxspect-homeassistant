"""Constants for the Maxspect integration."""

DOMAIN = "maxspect"

DEFAULT_PORT = 12416
DEFAULT_SCAN_INTERVAL = 30

# Gizwits Cloud API
CONF_CLOUD_USERNAME = "cloud_username"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CLOUD_REGION = "cloud_region"
CONF_CLOUD_DID = "cloud_did"

# Maxspect Gizwits application
GIZWITS_APP_ID = "b59fcef4de7a4d2ab7f4c26eb81a0537"
# Known product keys for Maxspect devices (Gizwits cloud)
GIZWITS_PRODUCT_KEY = "cd01d1f3ab2647ea9da51e045cf53d61"  # XF330CE Gyre pump (漩影WiFi)
GIZWITS_KNOWN_PRODUCT_KEYS: frozenset[str] = frozenset({
    "cd01d1f3ab2647ea9da51e045cf53d61",  # XF330CE Gyre pump (漩影WiFi)
    "401dff8180744f02b071f476edf6363b",  # wifi灯 LED light (L165 series)
    "5dc78a56545d49259d294dbddcd948ec",  # MJ_L265_L290 LED light (L260/L265/L290)
    "53a6a71bb6164ee1a0c230b01d20c03e",  # E8 LED light
    "254085a8db274ffaa4add3be7f8f2af6",  # 20缸 aquarium controller
    "11c81d63c4194a81aa05e297b94bd493",  # 套缸 integrated aquarium system
})
DEFAULT_CLOUD_REGION = "eu"

# Config entry key for the discovered device product_key
CONF_CLOUD_PRODUCT_KEY = "cloud_product_key"

# Device type identifiers
DEVICE_TYPE_GYRE         = "gyre"          # cd01d1f3… XF330CE pump
DEVICE_TYPE_LED_6CH      = "led_6ch"       # 401dff81… wifi灯 / L165 (6 ch)
DEVICE_TYPE_LED_8CH      = "led_8ch"       # 5dc78a56… MJ_L265_L290 (8 ch)
DEVICE_TYPE_LED_E8       = "led_e8"        # 53a6a71b… E8 (8 ch)
DEVICE_TYPE_AQUARIUM_20  = "aquarium_20"   # 254085a8… 20缸
DEVICE_TYPE_AQUARIUM_SYS = "aquarium_sys"  # 11c81d63… 套缸

PRODUCT_KEY_TO_DEVICE_TYPE: dict[str, str] = {
    "cd01d1f3ab2647ea9da51e045cf53d61": DEVICE_TYPE_GYRE,
    "401dff8180744f02b071f476edf6363b": DEVICE_TYPE_LED_6CH,
    "5dc78a56545d49259d294dbddcd948ec": DEVICE_TYPE_LED_8CH,
    "53a6a71bb6164ee1a0c230b01d20c03e": DEVICE_TYPE_LED_E8,
    "254085a8db274ffaa4add3be7f8f2af6": DEVICE_TYPE_AQUARIUM_20,
    "11c81d63c4194a81aa05e297b94bd493": DEVICE_TYPE_AQUARIUM_SYS,
}

PRODUCT_KEY_TO_MODEL_NAME: dict[str, str] = {
    "cd01d1f3ab2647ea9da51e045cf53d61": "Gyre XF330CE",
    "401dff8180744f02b071f476edf6363b": "LED L165 (wifi灯)",
    "5dc78a56545d49259d294dbddcd948ec": "LED MJ-L265/L290",
    "53a6a71bb6164ee1a0c230b01d20c03e": "LED E8",
    "254085a8db274ffaa4add3be7f8f2af6": "Aquarium 20缸",
    "11c81d63c4194a81aa05e297b94bd493": "Aquarium 套缸",
}

# Per-device-type cloud control: attr name, on value, off value
DEVICE_CONTROL: dict[str, dict] = {
    DEVICE_TYPE_GYRE:         {"attr": "Mode",       "on": 5, "off": 3},
    DEVICE_TYPE_LED_6CH:      {"attr": "mode",       "on": 0, "off": 3},
    DEVICE_TYPE_LED_8CH:      {"attr": "MODE",       "on": 0, "off": 2},
    DEVICE_TYPE_LED_E8:       {"attr": "MODE",       "on": 0, "off": 2},
    DEVICE_TYPE_AQUARIUM_20:  {"attr": "Mode",       "on": 0, "off": 1},
    DEVICE_TYPE_AQUARIUM_SYS: {"attr": "Switch_All", "on": 1, "off": 0},
}

# Mode name maps per device type
LED_6CH_MODE_NAMES:      dict[int, str] = {0: "Manual", 1: "Auto", 2: "Preset", 3: "Off"}
LED_8CH_MODE_NAMES:      dict[int, str] = {0: "Manual", 1: "Auto", 2: "Off", 3: "Interaction"}
AQUARIUM_20_MODE_NAMES:  dict[int, str] = {0: "Running", 1: "Standby"}

# Number of light channels per LED device type
LED_CHANNEL_COUNT: dict[str, int] = {
    DEVICE_TYPE_LED_6CH: 6,
    DEVICE_TYPE_LED_8CH: 8,
    DEVICE_TYPE_LED_E8:  8,
}

# Gizwits LAN frame header
FRAME_HEADER = b"\x00\x00\x00\x03"

# Gizwits LAN command codes
CMD_DEV_INFO_REQ = 0x0006
CMD_DEV_INFO_RESP = 0x0007
CMD_BIND_REQ = 0x0008
CMD_BIND_ACK = 0x0009
CMD_HEARTBEAT_REQ = 0x000C
CMD_HEARTBEAT_RESP = 0x000D
CMD_DATA_SEND = 0x0090
CMD_DATA_RECV = 0x0091

# Data point protocol actions (first byte of 0x0090/0x0091 payload)
ACTION_READ = 0x11
ACTION_WRITE = 0x12
ACTION_WRITE_ACK = 0x13
ACTION_DEVICE_REPORT = 0x14

# Attr flags length (6 bytes = 48 bits for data points 0-47)
ATTR_FLAGS_LEN = 6

# Verified Gyre XF330CE operational modes (DP 18)
MODE_WATER_FLOW = 0   # Manual pump mode
MODE_PROGRAMMING = 1  # Schedule/auto mode
MODE_FEED = 2         # Feeding mode (pumps paused)
MODE_OFF = 3          # Power OFF
MODE_EXIT_FEED = 4    # Resume from feeding
MODE_ON = 5           # Power ON

MODE_NAMES = {
    MODE_WATER_FLOW: "Water Flow",
    MODE_PROGRAMMING: "Programming",
    MODE_FEED: "Feed",
    MODE_OFF: "Off",
    MODE_EXIT_FEED: "Exit Feed",
    MODE_ON: "On",
}

# Non-bool data point byte lengths (for offset calculation in device reports)
DP_LENGTHS: dict[int, int] = {
    17: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1,   # uint8
    33: 12, 34: 7, 35: 62, 36: 781, 37: 3, 38: 4, 39: 2, 40: 2,  # binary
}

# Polling / heartbeat intervals (seconds)
POLL_INTERVAL = 3.0
CONFIG_POLL_INTERVAL = 60.0
HEARTBEAT_INTERVAL = 20.0

# Discovery
DISCOVERY_TIMEOUT = 5.0

CMD_HEARTBEAT_RESP = 0x000D
CMD_DATA_SEND = 0x0090
CMD_DATA_RECV = 0x0091

# Data point protocol actions (first byte of 0x0090/0x0091 payload)
ACTION_READ = 0x11
ACTION_WRITE = 0x12
ACTION_WRITE_ACK = 0x13
ACTION_DEVICE_REPORT = 0x14

# Attr flags length (6 bytes = 48 bits for data points 0-47)
ATTR_FLAGS_LEN = 6

# Verified Gyre XF330CE operational modes (DP 18)
MODE_WATER_FLOW = 0   # Manual pump mode