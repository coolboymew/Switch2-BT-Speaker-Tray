# BT Speaker Tray

A tray app that makes this PC's Bluetooth adapter act as a speaker, so a Switch 2 can play its sound through the PC. The PC's own sound (YouTube, music) keeps playing at the same time, mixed together.

The folder is portable. Copy `btspeakertray.py`, and `btspeakertray.json` if you want to keep your choices (lag, volume, reconnect). The tray recognises a new computer and starts there with speaker mode off and no Switch, so nothing from the old PC (adapter names, pairing) carries over. Everything else is done from the tray: turning speaker mode on the first time asks for the password once and installs what it needs.

## Run it

```
python3 btspeakertray.py
```

Needs PyQt6 (`sudo pacman -S python-pyqt6`) and PyGObject (`python-gobject`, already on CachyOS). To start it with KDE, add it under System Settings → Autostart. Only one copy runs at a time.

Right-click the icon for the menu. Left-click shows the status.

**Icon:** grey speaker means speaker mode is off. Blue means it is on and waiting. Green with sound waves means the Switch is connected. Orange means it dropped in the last hour. While pairing, it blinks blue.

Terminal commands: `status`, `on`, `off`, `pair`, `signal`, `fix`, `unfix`, `undo`, `tips`.

## Using it

1. Turn on **Speaker mode**. The first time, it asks for your password once to install a small helper and one BlueZ setting (see below), so it never has to ask again. Bluetooth restarts, so a mouse or headset reconnects.
2. Click **Pair a Switch…**. On the Switch 2, go to System Settings → Bluetooth Audio → Pair, and choose “*computer name* Speaker”. Nothing has to be clicked on the PC. The tray says when it is paired.
3. Later, wake the Switch and click **Connect the Switch** in the tray. The PC and the Switch pair again quietly each time; nothing has to be clicked. Choosing the PC in the Switch's Bluetooth Audio settings works too.

If pairing fails, remove “*computer name* Speaker” from the Switch's list first, then pair again.

To stop, turn **Speaker mode** off. The PC goes back to being a normal computer. **Undo everything…** also forgets the Switch and removes every file the tray installed.

## Give the Switch its own adapter

This matters more than distance. **A Bluetooth mouse on the same adapter makes the Switch's sound cut out badly, even right next to the PC.** A Logitech M720 talks to the PC every 7.5 ms, and the adapter drops the Switch's sound to keep up. Measured with a TP-Link UB500: with the mouse asleep, every packet arrived; within seconds of the mouse connecting, half of them were lost, and the sound came back the second the mouse disconnected.

- **Two adapters:** the tray picks the one that no mouse, keyboard or headset is paired with. You can change it under **Bluetooth adapters**. A Switch connects to one adapter at a time, so two adapters don't add up their range.
- **One adapter:** move the mouse to its USB receiver (the M720 also works with a Logitech Unifying receiver), or use another mouse while playing.
- **Two TP-Link UB500s don't work together.** Units can share the same address, built into the adapters, and then Linux uses only one. The tray says so when this happens. Use two different models.
- Small “wireless receivers” that come with a keyboard, remote or air mouse are not Bluetooth adapters, even if they use Bluetooth inside. Linux sees them as a keyboard and mouse.

The tray's status shows a warning when another device is connected to the Switch's adapter.

## Menu options

- **Lowest lag / Balanced / Steady at a distance.** Bluetooth sound arrives in bursts, and part of it is held back so the gaps aren't heard. Lowest lag (the setting to use once the Switch has its own adapter) holds back about 20 ms. Steady at a distance holds back about 200 ms, so short signal losses aren't heard. Balanced lets PipeWire decide: it grows the buffer every time packets go missing, and it reached 300 ms with the mouse sharing the adapter.
- **Volume buttons on the Switch work.** The Switch's own slider sets the volume. Each time the Switch connects, the PC plays it at 100%, so what you set on the Switch is what you hear. The KDE mixer still works too, but the two don't follow each other (see “Volume” below). Turning this off gives the volume back to the PC and restarts Bluetooth.
- **Connect the Switch.** Shown while the Switch isn't connected. The PC calls the Switch, the same as clicking Connect on KDE's Bluetooth page. The Switch must be awake.
- **Reconnect after a drop.** If the Switch drops, the PC calls it back for one minute.
- **Bluetooth adapters.** Shown when there is more than one. Tick the adapters the Switch may use.
- **Check signal strength.** Shows how strong the signal from the connected Switch is, in plain words.
- **Fix power saving.** Shown when the adapter is allowed to power down when idle. Realtek adapters such as the TP-Link UB500 often drop the connection when this happens. If M720 Tray already fixed it, this item stays hidden.
- **Range tips.** What helps, most helpful first.

## What it changes, and how it is undone

| Change | When | Undone by |
|---|---|---|
| The adapter shows up as a loudspeaker instead of a computer, under the name “*computer name* Speaker” | Speaker mode on | Speaker mode off. A reboot also resets the type, and the tray sets it again if speaker mode is still on |
| `~/.config/wireplumber/wireplumber.conf.d/80-btspeakertray.conf`: sound settings | Speaker mode on | Speaker mode off deletes it |
| The tray answers pairing questions for the PC | Speaker mode on | Speaker mode off or Quit hands them back to KDE |
| `/etc/systemd/system/bluetooth.service.d/btspeakertray.conf`: Bluetooth runs without remote control (`--noplugin=avrcp`), so the Switch keeps its own volume. Bluetooth headphones on this PC lose their play/pause buttons meanwhile | Speaker mode on (with “Volume buttons on the Switch work”) | Speaker mode off |
| `/usr/local/lib/btspeakertray/bt-class` and `/usr/share/polkit-1/actions/local.btspeakertray.policy`: a small helper allowed to change the adapter type, read the signal and turn remote control off and on without a password | First time speaker mode is turned on | Undo everything |
| `JustWorksRepairing = always` under `[General]` in `/etc/bluetooth/main.conf`, under a line `# Added by BT Speaker Tray (Undo everything removes it):` | First time speaker mode is turned on | Undo everything |
| The Switch's adapter stops saving new pairings (“Pairable” off). Pairings made before, like a mouse, keep working | Speaker mode on | Speaker mode off |
| `/etc/modprobe.d/btspeakertray-btusb.conf` and `/etc/udev/rules.d/50-btspeakertray-bt-power.rules`: power fix | Fix power saving | Undo power fix, or Undo everything |

While speaker mode is on, the PC can't send its own sound to Bluetooth headphones (see “Fewer formats” below).

If the tray is gone and you want to undo by hand: delete the files above, remove the setting under the `# Added by BT Speaker Tray` line (and that line) from `/etc/bluetooth/main.conf`, run `sudo systemctl daemon-reload`, run `bluetoothctl pairable on`, run `systemctl --user restart wireplumber`, and reboot.

## How it works, and what was found

Found with a Bluetooth capture (`sudo btmon -w file`) while pairing and playing a Switch 2 with a TP-Link UB500 on CachyOS.

- **Speaker type.** The Switch only lists Bluetooth devices that call themselves audio devices. Linux adapters call themselves computers, so the tray changes the adapter's device type to “loudspeaker” (`btmgmt class 4 20`). This lasts until Bluetooth restarts; the tray sets it again when needed.
- **Fewer formats.** Right after pairing, the Switch asks which audio formats the PC accepts. PipeWire offers about 30 (every codec, for sending and receiving), and the Switch hung up 0.1 s after reading the list. With speaker mode on, the PC only offers receiving, in SBC and AAC (`bluez5.roles = [ a2dp_sink ]`, `bluez5.codecs = [ sbc aac ]`). The Switch then picks AAC at 192 kbps.
- **AAC, not SBC.** Forcing SBC made things worse: the Switch used it at about 420 kbps, more than twice AAC's airtime, and split every packet in two.
- **Pairing that doesn't stick.** The Switch never keeps a pairing: it asks for a one-time pairing (“no bonding”) every time and quietly pairs again on the next connection, as it does with headphones. The Switch's list still shows the PC, so from the outside it looks kept. Two things on the PC side break that:
  - BlueZ refuses a second pairing with a device it knows (`JustWorksRepairing = never` by default). The tray sets it to `always`.
  - When the PC starts a connection, Linux saves the pairing by default. The next time, the PC offers that key, the Switch has thrown its copy away, and every attempt fails with “Authentication Failure” until the PC forgets it. That's what happened after using **Connect the Switch** once. So while speaker mode is on, the Switch's adapter doesn't save pairings (BlueZ's “Pairable” off: Linux still allows the Switch's kind of pairing, it just keeps nothing), the tray removes any old saved pairing for the Switch, and keeps the Switch marked as trusted so BlueZ doesn't delete it between connections. Then both ways work every time: the Switch choosing the PC, and **Connect the Switch** from the PC.
- **BlueZ settings need a line of their own.** BlueZ's settings file has no comments at the end of a line: `JustWorksRepairing = always  # note` reads as the value “always  # note”, which BlueZ silently ignores. The tray's marker goes on the line above.
- **Both sounds at once.** The Switch's sound comes in as a normal playback stream, like a browser tab, so it mixes with everything else and has its own volume in the KDE mixer (`bluez5.media-source-role = playback`).
- **Volume.** The PC advertises remote control (AVRCP) with volume, so the Switch hands its slider over to the PC: each press asks the PC to set the volume. BlueZ refused every request (“SetAbsoluteVolume not supported”) and the slider got stuck. BlueZ's debug log (`sudo kill -USR2 $(pidof bluetoothd)` turns it on) shows why: `target_init() version 0x0000`. The Switch publishes three service records (audio, remote control, remote-control target) without the record ID each one should carry. BlueZ files records by that ID, so all three get the same default ID and only the first, audio, is kept. Without the remote-control record, BlueZ treats the Switch as not supporting volume. Since the Switch pairs again on every connection, BlueZ reads the records again every time, so neither a saved copy of the records nor `RefreshDiscovery = false` nor `VolumeCategory = false` gets around it. Short of changing BlueZ itself, the fix is to not offer remote control at all: Bluetooth runs with `--noplugin=avrcp` while speaker mode is on, the Switch then keeps its own volume like it does with a simple speaker, and its slider works. Without remote control, the Switch's volume and the PC's volume can't follow each other; the tray sets the PC side to 100% when the Switch connects.
- **Lag.** `bluez5.decode-buffer.latency` sets how much sound the PC holds back: 1024 samples (about 20 ms) for lowest lag, 9600 (about 200 ms) for steady. The PC tells the Switch its total delay; the capture showed 98 ms when the link was clean and 300 ms on Balanced with the mouse sharing the adapter. The Switch adds its own delay on top, which the PC can't change.
- **Signal.** Realtek adapters report the plain signal level: about −25 a few centimetres away, −30 at 1 m, −38 at 3 m, which is strong. Other adapters use the standard scale, where 0 means strong enough. The signal was strong during every drop, so the drops were not about distance.
- **Moving the Switch.** Drops that seemed to come from moving were the mouse waking up: the M720 wakes at the slightest bump, while the signal stayed strong (−34). With the mouse switched off, moving around was fine.

## Testing

Tested on CachyOS (BlueZ 5.87, PipeWire 1.6.10, WirePlumber 0.5.18) with a TP-Link UB500 and a Switch 2: speaker mode on, the Switch finding and pairing with the PC, AAC sound mixed with the PC's sound, reconnecting through re-pairing, the signal check, the Switch's own volume slider with remote control off, the tray turning remote control off and on without a password, picking a free adapter (with two pretend adapters), and a settings file from another computer starting clean.

Not tested yet:

- [ ] A fresh computer: speaker mode on (password once), then pairing, all from the tray
- [ ] Two different adapters: the tray picks the one without the mouse, and the Switch pairs with it
- [ ] No drops in game with the Switch on its own adapter, including while moving the Switch
- [ ] A built-in Wi-Fi + Bluetooth card as the Switch's adapter (its Wi-Fi antennas must be screwed on)
- [ ] The PC sets the Switch to 100% when it connects
- [ ] Lowest lag: how the lag feels in game (the PC's share measured at 74 ms, down from 300 ms)
- [ ] Reconnect after a drop: walk out of range and back
- [ ] Speaker mode off: adapter is a computer again, remote control back on, KDE can still pair other devices, Bluetooth headphones work again
- [ ] Undo everything removes all files and the BlueZ line
- [ ] Fix power saving, then count drops
