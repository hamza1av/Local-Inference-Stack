---
title: Home Network Handbook
tags: [network, howto]
---

# Home Network Handbook

This handbook describes how the small home network is set up: one router, a mesh
access point upstairs, a NAS in the closet and a handful of laptops and phones.

## Router

The router is the only device that talks to the internet provider. Its admin page is
reachable at `http://192.168.1.1` from the wired network only; remote administration is
switched off on purpose.

### Firmware updates

Check for firmware updates on the first Sunday of every month. Download the image from
the vendor page, verify the checksum, then upload it through the admin page. Never update
during a video call: the router reboots and the connection drops for about three minutes.

### DHCP and fixed addresses

The DHCP pool hands out `192.168.1.100` to `192.168.1.199`. Devices that need a stable
address get a reservation instead of a static configuration on the device:

- NAS: `192.168.1.10`
- printer: `192.168.1.20`
- mesh access point: `192.168.1.2`

## Wi-Fi

There are two networks. The main network uses WPA3 and is for trusted devices. The guest
network is isolated from the LAN and is meant for visitors and smart plugs.

### Choosing channels

On 2.4 GHz only channels 1, 6 and 11 do not overlap, so pick the one with the fewest
neighbours. On 5 GHz prefer an 80 MHz channel outside the DFS range to avoid sudden
channel changes when radar is detected. Re-check channel congestion after neighbours move.

### Guest password rotation

The guest password is rotated every three months. Print the new QR code and replace the
card on the fridge.

## NAS and backups

The NAS keeps a nightly snapshot of the shared folders at 02:00. Snapshots are kept for
30 days. Every Sunday at 03:00 an encrypted copy is pushed to an external disk, which is
swapped with a second disk stored offsite once a month (3-2-1 rule: three copies, two
media, one offsite).

### Restoring a file

Open the snapshot browser, pick the date before the file was damaged and restore it into
a new folder first. Compare it with the current version before overwriting anything.

## Troubleshooting

If the internet is down, first check whether the router's WAN light is on. If it is off,
restart the modem, wait two minutes, then restart the router. If only Wi-Fi is slow, run
a speed test over a cable to rule out the provider.
