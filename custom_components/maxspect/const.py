"""Constants for the Maxspect integration."""

DOMAIN = "maxspect"

CONF_DEVICE_NAME = "device_name"
DEFAULT_PORT = 12416
DEFAULT_SCAN_INTERVAL = 30

# Gizwits Cloud API
CONF_CLOUD_USERNAME = "cloud_username"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CLOUD_REGION = "cloud_region"
CONF_CLOUD_DID = "cloud_did"
CONF_MODEL_A = "model_a"
CONF_MODEL_B = "model_b"

# Pump model mapping (DP 20/21: 0=330, 1=350)
MODEL_NAMES: dict[int, str] = {
    0: "XF 330CE",
    1: "XF 350CE",
}

# Maxspect "漩影WiFi" (Cool Shadow) Gizwits application
GIZWITS_APP_ID = "b59fcef4de7a4d2ab7f4c26eb81a0537"
GIZWITS_PRODUCT_KEY = "cd01d1f3ab2647ea9da51e045cf53d61"
DEFAULT_CLOUD_REGION = "eu"

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
HEARTBEAT_INTERVAL = 20.0

# Discovery
DISCOVERY_TIMEOUT = 5.0
