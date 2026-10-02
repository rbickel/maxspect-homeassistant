"""Decode Gyre program settings using the Syna-G field layout."""

PATTERNS = {
    0: "Constant speed", 1: "Pulsing", 2: "Gradual pulsing", 3: "Alternating",
    4: "Random", 5: "Synchronized", 6: "Anti-synchronized",
    7: "Timed delay", 8: "Reversed timed delay",
}


def decode_program(value: object, *, scheduled: bool) -> list[dict]:
    """Return complete entries; reject incomplete or unsupported layouts."""
    if not isinstance(value, str):
        return []
    try:
        data = bytes.fromhex(value)
        end = data[0] + 1
        if end > len(data):
            return []
        data = data[:end]
        count, offset = (data[1], 2) if scheduled else (1, 1)
        entries = []
        for _ in range(count):
            entry = {}
            if scheduled:
                number, hour, minute = data[offset:offset + 3]
                if hour > 23 or minute > 59:
                    return []
                entry.update(number=number, time=f"{hour:02}:{minute:02}", minute=hour * 60 + minute)
                offset += 3
            a_pattern = data[offset]
            for channel in ("a", "b"):
                pattern = data[offset]
                if pattern not in PATTERNS or (channel == "a" and pattern > 4):
                    return []
                length = {0: 3, 1: 4, 2: 4, 3: 9, 4: 3}.get(pattern, 5 if a_pattern == 3 else 3)
                if offset + length > len(data):
                    return []
                block = data[offset:offset + length]
                direction, speed = block[1:3]
                if direction not in (0, 1) or speed > 10:
                    return []
                settings = {
                    "pattern": PATTERNS[pattern], "pattern_code": pattern,
                    "power_percent": speed * 10,
                    "direction": "Forward" if direction else "Reverse",
                    "signed_power_percent": speed * 10 * (1 if direction else -1),
                    "raw_hex": block.hex(),
                }
                if pattern == 3 or length == 5:
                    low_direction, low_speed = block[5:7] if pattern == 3 else block[3:5]
                    if low_direction not in (0, 1) or low_speed > 10:
                        return []
                    settings["alternate_power_percent"] = low_speed * 10 * (1 if low_direction else -1)
                entry[channel] = settings
                offset += length
            entries.append(entry)
        # Some firmware includes one trailing zero in the declared manual length.
        if any(data[offset:]):
            return []
        return entries
    except (ValueError, IndexError, TypeError):
        return []


def active_entry(entries: list[dict], minute: int) -> dict | None:
    """Select a daily schedule entry, including the midnight wrap."""
    if not entries:
        return None
    entries = sorted(entries, key=lambda entry: entry["minute"])
    return next((entry for entry in reversed(entries) if entry["minute"] <= minute), entries[-1])


def decode_serial_number(value: object) -> str | None:
    """Decode a nonempty printable ASCII serial from the reported bytes."""
    if not isinstance(value, str):
        return None
    try:
        serial = bytes.fromhex(value).rstrip(b"\x00").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    return serial if serial and serial.isprintable() else None
