---
name: New device support
about: Help add support for a Maxspect device not yet confirmed working
title: "[Device] <your device model here>"
labels: new-device
assignees: ''
---

## Device information

**Model name:** <!-- e.g. Maxspect LED L165 -->
**Device type:** <!-- Pump / LED light / Aquarium controller -->
**Gizwits product key:** <!-- shown in HA logs on first setup — see below -->

## How to find your product key

Enable debug logging in Home Assistant (`configuration.yaml`):

```yaml
logger:
  default: warning
  logs:
    custom_components.maxspect: debug
```

Restart HA, then go to **Settings → System → Logs**. Look for a line like:

```
Discovered device did=XXXX product_key=<YOUR_KEY_HERE> (online=True)
```

or (if your key isn't known yet):

```
No known Maxspect device found … Falling back to first bound device: did=XXXX product_key=<YOUR_KEY_HERE>
```

## Setup result

- [ ] Device was discovered (DID appeared in logs)
- [ ] Entities appeared in Home Assistant
- [ ] On/Off control worked
- [ ] State updates reflected correctly

## Debug log

Paste the relevant section of your debug log here (from HA startup through at least one on/off action):

<details>
<summary>Debug log</summary>

```
paste log here
```

</details>

## Additional context

<!-- Anything else that might help: firmware version, region, Syna-G+ app version, etc. -->
