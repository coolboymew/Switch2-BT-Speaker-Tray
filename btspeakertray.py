#!/usr/bin/env python3
"""BT Speaker Tray - turns this PC's Bluetooth adapter into a speaker, so a
Switch 2 (or a phone) can play its sound through the PC while the PC's own
sound keeps playing.

Portable: keep this file and btspeakertray.json together in one folder.

Run with no arguments for the tray icon (needs PyQt6 or PyQt5).
Run with a command for the terminal version:  status | on | off | pair |
signal | fix | unfix | undo
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

APP_NAME = "BT Speaker Tray"
APP_DIR = Path(__file__).resolve().parent
USE_PKEXEC = False  # the tray always asks for the password in a window
CONFIG_PATH = APP_DIR / "btspeakertray.json"

# Installed once with the password. They let the tray switch the adapter
# between "computer" and "speaker" and read the signal strength without
# asking for the password every time.
SYS_DIR = Path("/usr/local/lib/btspeakertray")
CLASS_TOOL = SYS_DIR / "bt-class"
POLKIT_POLICY = Path("/usr/share/polkit-1/actions/local.btspeakertray.policy")
UDEV_RULE = Path("/etc/udev/rules.d/50-btspeakertray-bt-power.rules")
MODPROBE_CONF = Path("/etc/modprobe.d/btspeakertray-btusb.conf")
# M720 Tray installs the same power fix under its own name.
OTHER_POWER_FIXES = [Path("/etc/udev/rules.d/50-m720tray-bt-power.rules")]

WP_CONF = Path.home() / ".config/wireplumber/wireplumber.conf.d/80-btspeakertray.conf"

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
SPEAKER_MAJOR, SPEAKER_MINOR = 0x04, 0x14   # Audio/Video, loudspeaker
PAIR_SECONDS = 180
DROP_GAP = 60          # back within this many seconds = a drop, not a power-off
RECONNECT_EVERY = 5

LAG_MODES = {
    # name: (menu text, decode buffer in samples, node latency or None)
    "lowest": ("Lowest lag (needs a strong signal)", 1024, "256/48000"),
    "balanced": ("Balanced", 0, None),
    "steady": ("Steady at a distance (more lag)", 9600, None),
}

DEFAULT_CONFIG = {
    "speaker_on": False,
    "speaker_name": "",          # empty: "<computer name> Speaker"
    "adapters": [],              # addresses to use; empty means all
    "lag": "balanced",
    "codec": "auto",             # auto (SBC or AAC, the Switch picks) | sbc
    "switch_volume": True,       # the Switch's own volume buttons work
    "auto_reconnect": True,
    "switches": {},              # address: {"name", "adapter"}
    "saved_adapters": {},        # address: {"class", "alias"} before speaker mode
}


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=10, stdin=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=stdin)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def valid_mac(mac):
    return bool(mac) and bool(MAC_RE.match(mac))


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"Could not read {CONFIG_PATH}: {e}", file=sys.stderr)
    if cfg.get("lag") not in LAG_MODES:
        cfg["lag"] = "balanced"
    # Copied from another computer: keep the preferences, drop what belongs
    # to that computer (its adapters' original names and types, its Switch
    # pairing, speaker mode being on there).
    here = machine_id()
    if cfg.get("machine") != here:
        for key in ("speaker_on", "adapters", "switches", "saved_adapters"):
            cfg[key] = json.loads(json.dumps(DEFAULT_CONFIG[key]))
        cfg["machine"] = here
    return cfg


def machine_id():
    try:
        return Path("/etc/machine-id").read_text().strip()
    except OSError:
        return socket.gethostname()


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


class ActionError(Exception):
    pass


# ---------------------------------------------------------- BlueZ (D-Bus)

BLUEZ = "org.bluez"
PROPS = "org.freedesktop.DBus.Properties"
_bus = None


def bus():
    global _bus
    if _bus is None:
        _bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    return _bus


def bz_call(path, iface, method, args=None, sig=None, timeout=10000):
    try:
        v = bus().call_sync(BLUEZ, path, iface, method,
                            GLib.Variant(sig, args) if sig else None,
                            None, Gio.DBusCallFlags.NONE, timeout, None)
        return v.unpack() if v else ()
    except GLib.Error as e:
        raise ActionError(dbus_message(e))


def bz_call_async(path, iface, method, done=None):
    """Fire and forget. `done(ok)` is called later if given."""
    def finish(conn, res):
        try:
            conn.call_finish(res)
            ok = True
        except GLib.Error:
            ok = False
        if done:
            done(ok)
    bus().call(BLUEZ, path, iface, method, None, None,
               Gio.DBusCallFlags.NONE, 30000, None, finish)


def switch_node(mac):
    """PipeWire's id for the sound coming from a device, or None."""
    code, out, _ = run(["wpctl", "status"])
    name = "bluez_input." + mac.replace(":", "_")
    for line in out.splitlines():
        m = re.search(r"(\d+)\.\s+" + re.escape(name), line)
        if m:
            return m.group(1)
    return None


def bz_set(path, iface, name, variant):
    bz_call(path, PROPS, "Set", (iface, name, variant), "(ssv)")


def dbus_message(e):
    msg = e.message
    return msg.split(": ", 1)[-1] if msg.startswith("GDBus.Error") else msg


def bluez_objects():
    try:
        return bz_call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")[0]
    except ActionError:
        return {}


def usb_device(hci):
    """The USB device folder of an adapter, or None if it is not USB."""
    try:
        iface = (Path("/sys/class/bluetooth") / hci / "device").resolve()
    except OSError:
        return None
    dev = iface.parent
    return dev if (dev / "idVendor").exists() else None


def adapter_product(hci):
    dev = usb_device(hci)
    for f in ("product", "manufacturer"):
        try:
            if dev and (dev / f).exists():
                return (dev / f).read_text().strip()
        except OSError:
            pass
    return "built-in" if dev is None else "USB adapter"


def adapters(objs=None):
    objs = bluez_objects() if objs is None else objs
    out = []
    for path, ifaces in sorted(objs.items()):
        a = ifaces.get("org.bluez.Adapter1")
        if not a:
            continue
        hci = path.rsplit("/", 1)[-1]
        out.append({
            "path": path, "hci": hci, "index": int(hci[3:]),
            "address": a.get("Address", ""), "name": a.get("Name", ""),
            "alias": a.get("Alias", ""), "class": a.get("Class", 0),
            "powered": a.get("Powered", False),
            "discoverable": a.get("Discoverable", False),
            "pairable": a.get("Pairable", True),
            "product": adapter_product(hci),
        })
    return out


def devices(objs=None):
    objs = bluez_objects() if objs is None else objs
    out = {}
    for path, ifaces in objs.items():
        d = ifaces.get("org.bluez.Device1")
        if d:
            out[d.get("Address")] = dict(d, path=path)
    # A transport means the audio channel is set up; it is "active" while
    # sound is flowing.
    for path, ifaces in objs.items():
        t = ifaces.get("org.bluez.MediaTransport1")
        if t:
            for d in out.values():
                if path.startswith(d["path"] + "/"):
                    d["audio"] = True
                    d["streaming"] = d.get("streaming") or t.get("State") == "active"
    return out


def ignored_adapters():
    """How many adapters Linux found but BlueZ ignores. Two adapters with the
    same address (two TP-Link UB500s, for example) can't both be used."""
    kernel = [h for h in Path("/sys/class/bluetooth").glob("hci*") if h.name[3:].isdigit()]
    return max(0, len(kernel) - len(adapters()))


def is_speaker_class(cls):
    return (cls >> 8) & 0x1F == SPEAKER_MAJOR and cls & 0xFC == SPEAKER_MINOR


# ---------------------------------------------------- pairing agent (D-Bus)

AGENT_PATH = "/btspeakertray/agent"
AGENT_XML = """
<node><interface name="org.bluez.Agent1">
 <method name="Release"/>
 <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
 <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
 <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
 <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
 <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
 <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
 <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
 <method name="Cancel"/>
</interface></node>
"""


def bluez_owner():
    try:
        return bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", "GetNameOwner",
                               GLib.Variant("(s)", (BLUEZ,)), None,
                               Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
    except GLib.Error:
        return None


class Agent:
    """Answers pairing questions while speaker mode is on, so nothing has to
    be clicked on the PC. Says yes to anyone while the pairing window is open,
    and to the saved Switches at any time: they pair again on every
    connection (see README, "Pairing"). Anyone else is passed to `ask`."""

    def __init__(self, allow):
        self.reg_id = None
        self.allow = allow           # allow(address) -> True/False
        self.ask = None              # ask(name) -> True/False; None rejects

    def start(self):
        if self.reg_id is not None:
            return
        info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
        self.reg_id = bus().register_object(AGENT_PATH, info, self.on_call, None, None)
        self.owner = bluez_owner()
        try:
            bz_call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                    (AGENT_PATH, "NoInputNoOutput"), "(os)")
            bz_call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent",
                    (AGENT_PATH,), "(o)")
        except ActionError:
            self.stop()
            raise

    def lost(self):
        """BlueZ restarted and forgot us."""
        return self.reg_id is not None and bluez_owner() != self.owner

    def stop(self):
        if self.reg_id is None:
            return
        try:
            # BlueZ hands pairing back to the desktop's own agent.
            bz_call("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                    (AGENT_PATH,), "(o)")
        except ActionError:
            pass
        bus().unregister_object(self.reg_id)
        self.reg_id = None

    def on_call(self, conn, sender, path, iface, method, params, inv):
        if method in ("Release", "Cancel", "DisplayPinCode", "DisplayPasskey"):
            inv.return_value(None)
            return
        dev = params.unpack()[0]
        mac = dev.rsplit("dev_", 1)[-1].replace("_", ":")
        ok = self.allow(mac)
        if not ok and self.ask:
            name = mac
            try:
                name = bz_call(dev, PROPS, "Get", ("org.bluez.Device1", "Alias"), "(ss)")[0]
            except ActionError:
                pass
            ok = self.ask(name)
        if not ok:
            inv.return_dbus_error("org.bluez.Error.Rejected", "Not allowed")
        elif method == "RequestPinCode":
            inv.return_value(GLib.Variant("(s)", ("0000",)))
        elif method == "RequestPasskey":
            inv.return_value(GLib.Variant("(u)", (0,)))
        else:
            inv.return_value(None)


# --------------------------------------------------- PipeWire (user level)

def wireplumber_text(cfg):
    _, buffer, latency = LAG_MODES[cfg["lag"]]
    lines = [
        "# Written by BT Speaker Tray while speaker mode is on.",
        "# Delete this file and restart WirePlumber to undo.",
        "monitor.bluez.properties = {",
        "  # Sound sent to the PC plays on the speakers, mixed with everything else.",
        "  bluez5.media-source-role = playback",
        "  # Offer only what a speaker needs. The Switch hangs up right after pairing",
        "  # when it is offered the full list (about 30 formats, both directions).",
        "  bluez5.roles = [ a2dp_sink ]",
        "  bluez5.codecs = [ sbc ]" if cfg.get("codec") == "sbc" else "  bluez5.codecs = [ sbc aac ]",
    ]
    lines.append("}")
    props = []
    if buffer:
        props.append(f"bluez5.decode-buffer.latency = {buffer}")
    if latency:
        props.append(f'node.latency = "{latency}"')
    if props:
        lines += [
            "monitor.bluez.rules = [",
            "  {",
            '    matches = [ { node.name = "~bluez_input.*" } ]',
            "    actions = { update-props = { " + ", ".join(props) + " } }",
            "  }",
            "]",
        ]
    return "\n".join(lines) + "\n"


def restart_wireplumber():
    run(["systemctl", "--user", "restart", "wireplumber"], timeout=20)


def set_wireplumber(text):
    """Writes (or removes, if text is None) the drop-in. Restarts WirePlumber
    only if something changed. Returns True if it restarted."""
    old = WP_CONF.read_text() if WP_CONF.exists() else None
    if old == text:
        return False
    if text is None:
        WP_CONF.unlink(missing_ok=True)
    else:
        WP_CONF.parent.mkdir(parents=True, exist_ok=True)
        WP_CONF.write_text(text)
    restart_wireplumber()
    return True


# ------------------------------------------------ adapter power saving

def power_saving_problems():
    """Plain-words problems with adapter power saving (empty = fine)."""
    problems = []
    for a in adapters():
        dev = usb_device(a["hci"])
        try:
            if dev and (dev / "power" / "control").read_text().strip() != "on":
                problems.append(f"{a['product']} is allowed to power down")
        except OSError:
            pass
    fixed = (UDEV_RULE.exists() and MODPROBE_CONF.exists()) or any(f.exists() for f in OTHER_POWER_FIXES)
    if problems and not fixed:
        problems.append("the fix is not installed, so it comes back after a reboot")
    return problems


# ------------------------------------------------- root helper (pkexec/sudo)

AVRCP_DROPIN = Path("/etc/systemd/system/bluetooth.service.d/btspeakertray.conf")

CLASS_TOOL_TEXT = r"""#!/bin/sh
# Installed by BT Speaker Tray. Lets the tray change the Bluetooth device
# type (computer or speaker), read the signal strength, and turn BlueZ's
# remote control (AVRCP) off and on, without asking for the password each
# time. "Undo everything" in the tray removes it.
PATH=/usr/bin:/bin
exec </dev/null
DROPIN=/etc/systemd/system/bluetooth.service.d/btspeakertray.conf
case "$1" in
  avrcp-off)
    mkdir -p "${DROPIN%/*}"
    printf '# Written by BT Speaker Tray while speaker mode is on.\n[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --noplugin=avrcp\n' > "$DROPIN"
    systemctl daemon-reload && exec systemctl restart bluetooth ;;
  avrcp-on)
    rm -f "$DROPIN"; rmdir "${DROPIN%/*}" 2>/dev/null
    systemctl daemon-reload && exec systemctl restart bluetooth ;;
esac
idx=$2
case "$idx" in [0-9]|[0-9][0-9]) ;; *) echo "bad adapter" >&2; exit 2 ;; esac
num() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "$1" -le 255 ]; }
case "$1" in
  speaker) exec timeout 10 btmgmt --index "$idx" class 4 20 ;;
  set) num "$3" && num "$4" || { echo "bad class" >&2; exit 2; }
       exec timeout 10 btmgmt --index "$idx" class "$3" "$4" ;;
  signal) echo "$3" | grep -Eq '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$' || { echo "bad address" >&2; exit 2; }
       exec timeout 10 btmgmt --index "$idx" conn-info -t 0 "$3" ;;
  *) echo "unknown action" >&2; exit 2 ;;
esac
"""

POLKIT_TEXT = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<!-- Installed by BT Speaker Tray. "Undo everything" in the tray removes it. -->
<policyconfig>
  <action id="local.btspeakertray.bt-class">
    <description>Switch the Bluetooth adapter between computer and speaker</description>
    <message>BT Speaker Tray wants to change the Bluetooth adapter type</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{CLASS_TOOL}</annotate>
  </action>
</policyconfig>
"""


BT_MAIN_CONF = Path("/etc/bluetooth/main.conf")
# Our lines in BlueZ's settings, by section. Each goes under a marker line:
# BlueZ's format has no comments at the end of a line (a value "false  # x"
# is not "false", and BlueZ silently ignores it).
BT_CONF_MARK = "# Added by BT Speaker Tray (Undo everything removes it):"
BT_CONF_LINES = {
    # The Switch pairs again on every connection; BlueZ refuses a second
    # pairing with a known device by default.
    "General": "JustWorksRepairing = always",
}
# Lines earlier versions put under our marker.
BT_CONF_OLD = {"VolumeCategory = false", "RefreshDiscovery = false"}


def bt_conf_ok():
    try:
        lines = BT_MAIN_CONF.read_text().splitlines()
    except OSError:
        return False
    pairs = set(zip(lines, lines[1:]))
    return all((BT_CONF_MARK, line) in pairs for line in BT_CONF_LINES.values())


def set_bt_conf(on):
    """Adds or removes our lines in /etc/bluetooth/main.conf. Returns True if
    the file changed (BlueZ must restart). Runs as root."""
    try:
        lines = BT_MAIN_CONF.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    before = list(lines)
    ours = set(BT_CONF_LINES.values())
    # Drop our marker and the line under it, and older versions' lines.
    cleaned, under_mark = [], False
    for line in before:
        if under_mark:
            under_mark = False
            if line in ours or line in BT_CONF_OLD:
                continue
        if line == BT_CONF_MARK:
            under_mark = True
            continue
        if "# added by BT Speaker Tray" in line:
            continue
        cleaned.append(line)
    lines = cleaned
    if on:
        for section, line in BT_CONF_LINES.items():
            head = f"[{section}]"
            if head not in lines:
                lines.append(head)
            at = lines.index(head) + 1
            lines[at:at] = [BT_CONF_MARK, line]
    if lines == before:
        return False
    BT_MAIN_CONF.write_text("\n".join(lines) + "\n")
    return True


def tool_ready():
    """The helper, its permission and our BlueZ settings are installed and
    current (a newer version of the tray may need them updated)."""
    try:
        current = CLASS_TOOL.read_text() == CLASS_TOOL_TEXT
    except OSError:
        current = False
    return current and POLKIT_POLICY.exists() and bt_conf_ok()


def as_root(args, stdin=None):
    """Runs this script's root helper. Asks for the password."""
    script = str(Path(__file__).resolve())
    if os.geteuid() == 0:
        cmd = [sys.executable, script, "--helper"] + args
    elif not USE_PKEXEC and sys.stdin.isatty() and shutil.which("sudo"):
        cmd = ["sudo", sys.executable, script, "--helper"] + args
    else:
        cmd = ["pkexec", sys.executable, script, "--helper"] + args
    return run(cmd, timeout=120, stdin=stdin)


def class_tool(args):
    """Runs the installed class tool. No password once it is installed."""
    cmd = [str(CLASS_TOOL)] + args if os.geteuid() == 0 else ["pkexec", str(CLASS_TOOL)] + args
    return run(cmd, timeout=15)


def helper_main(args):
    """Root-only actions. Every argument is checked: this runs as root."""
    if os.geteuid() != 0:
        sys.exit("helper must run as root")
    action = args[0] if args else ""

    if action == "install":
        SYS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        CLASS_TOOL.write_text(CLASS_TOOL_TEXT)
        os.chown(CLASS_TOOL, 0, 0)
        os.chmod(CLASS_TOOL, 0o755)
        POLKIT_POLICY.write_text(POLKIT_TEXT)
        os.chmod(POLKIT_POLICY, 0o644)
        if set_bt_conf(True):
            run(["systemctl", "restart", "bluetooth"], timeout=30)
        print("ok")

    elif action == "fix-power":
        MODPROBE_CONF.write_text(
            "# Written by BT Speaker Tray: Bluetooth USB adapters never power down\n"
            "options btusb enable_autosuspend=0\n")
        UDEV_RULE.write_text(
            "# Written by BT Speaker Tray: Bluetooth USB adapters never power down\n"
            'ACTION=="add|bind", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_interface", '
            "DRIVER==\"btusb\", RUN+=\"/bin/sh -c 'echo on > /sys%p/../power/control'\"\n")
        try:
            Path("/sys/module/btusb/parameters/enable_autosuspend").write_text("N")
        except OSError:
            pass
        for hci in Path("/sys/class/bluetooth").glob("hci*"):
            dev = usb_device(hci.name)
            if dev:
                (dev / "power" / "control").write_text("on")
        run(["udevadm", "control", "--reload"])
        print("ok")

    elif action == "unfix-power":
        for f in (MODPROBE_CONF, UDEV_RULE):
            f.unlink(missing_ok=True)
        run(["udevadm", "control", "--reload"])
        print("ok")

    elif action == "uninstall":
        for f in (CLASS_TOOL, POLKIT_POLICY, MODPROBE_CONF, UDEV_RULE):
            f.unlink(missing_ok=True)
        if SYS_DIR.exists():
            SYS_DIR.rmdir()
        run(["udevadm", "control", "--reload"])
        changed = set_bt_conf(False)
        if AVRCP_DROPIN.exists():
            AVRCP_DROPIN.unlink()
            run(["systemctl", "daemon-reload"])
            changed = True
        if changed:
            run(["systemctl", "restart", "bluetooth"], timeout=30)
        print("ok")

    else:
        sys.exit(f"unknown helper action {action!r}")


# ------------------------------------------------------------ the speaker

def default_speaker_name():
    return socket.gethostname().split(".")[0] + " Speaker"


class Speaker:
    """Everything the tray and the terminal commands do. Call tick() every
    couple of seconds from a GLib or Qt main loop."""

    def __init__(self, notify=print):
        self.notify = notify
        self.cfg = load_config()
        self.agent = Agent(allow=lambda mac: self.pairing() or mac in self.cfg["switches"])
        self.pair_until = 0          # pairing window open until this time
        self.pair_known = set()      # devices paired before the window opened
        self.was_connected = {}      # address: last tick's connected state
        self.lost_at = {}            # address: when it disconnected
        self.last_try = 0
        self.drops = []              # times a Switch dropped and came back
        self.last_fix = 0
        self.volume_set = set()      # Switches set to full volume this connection
        self.objs = {}

    # -- state
    def reload(self):
        self.cfg = load_config()

    def save(self):
        save_config(self.cfg)

    def chosen_adapters(self, all_adapters=None):
        all_adapters = adapters(self.objs) if all_adapters is None else all_adapters
        want = self.cfg["adapters"]
        chosen = [a for a in all_adapters if not want or a["address"] in want]
        return chosen or all_adapters

    def other_devices(self, adapter_path, connected_only=False):
        """Names of devices other than the Switches paired with (or connected
        to) an adapter. They share its radio with the Switch."""
        out = []
        for mac, d in devices(self.objs).items():
            if d.get("Adapter") != adapter_path or mac in self.cfg["switches"]:
                continue
            if d.get("Connected") or (not connected_only and d.get("Paired")):
                out.append(d.get("Alias") or d.get("Name") or mac)
        return out

    def pick_free_adapters(self):
        """With several adapters and no choice made yet, use only the ones
        that no mouse, keyboard or headset is paired with: sharing a radio
        with a mouse makes the Switch's sound cut out."""
        all_a = adapters(self.objs)
        if self.cfg["adapters"] or len(all_a) < 2:
            return
        free = [a["address"] for a in all_a if not self.other_devices(a["path"])]
        if free and len(free) < len(all_a):
            self.cfg["adapters"] = free
            self.save()

    def sharing(self):
        """Devices connected right now to an adapter the Switch uses."""
        names = []
        for a in self.chosen_adapters():
            names += self.other_devices(a["path"], connected_only=True)
        return names

    def switches(self):
        """Saved Switches with their live state from BlueZ."""
        devs = devices(self.objs)
        out = []
        for mac, info in self.cfg["switches"].items():
            d = devs.get(mac)
            out.append({
                "address": mac,
                "name": (d or {}).get("Alias") or info.get("name") or mac,
                # BlueZ still knows it, so the PC can call it. Its pairing may
                # be gone ("no bonding"); calling it pairs again for good.
                "known": d is not None,
                "connected": bool(d and d.get("Connected")),
                "bonded": bool(d and d.get("Bonded")),
                "resolved": bool(d and d.get("ServicesResolved")),
                "audio": bool(d and d.get("audio")),
                "streaming": bool(d and d.get("streaming")),
                "path": (d or {}).get("path"),
            })
        return out

    def connected_switch(self):
        return next((s for s in self.switches() if s["connected"]), None)

    def pairing(self):
        return self.pair_until > time.time()

    # -- speaker mode
    def install_tool(self):
        if tool_ready():
            return
        code, out, err = as_root(["install"])
        if "ok" not in out:
            raise ActionError(err.strip() or "Cancelled.")
        time.sleep(2)  # polkit picks up the new rule, BlueZ restarts

    def make_speaker(self, a, number=None):
        saved = self.cfg["saved_adapters"]
        if a["address"] not in saved and not is_speaker_class(a["class"]):
            saved[a["address"]] = {"class": a["class"], "alias": a["alias"], "name": a["name"]}
            self.save()
        if not is_speaker_class(a["class"]):
            code, out, err = class_tool(["speaker", str(a["index"])])
            if code != 0:
                raise ActionError(f"Could not make {a['product']} a speaker: {(err or out).strip()}")
        name = self.cfg["speaker_name"] or default_speaker_name()
        if number:
            name += f" {number}"
        if a["alias"] != name:
            bz_set(a["path"], "org.bluez.Adapter1", "Alias", GLib.Variant("s", name))
        # The Switch never keeps a pairing: it pairs again on every connection,
        # like it does with headphones. If the PC kept one, the next connection
        # fails with "Authentication Failure" until the PC forgets it. With
        # Pairable off, Linux still pairs with the Switch but saves nothing.
        # Pairings made before (a mouse, say) keep working.
        if a["pairable"]:
            bz_set(a["path"], "org.bluez.Adapter1", "Pairable", GLib.Variant("b", False))

    def make_computer(self, a):
        saved = self.cfg["saved_adapters"].get(a["address"])
        if not saved:
            return
        cls = saved["class"]
        major, minor = (cls >> 8) & 0x1F, cls & 0xFC
        if is_speaker_class(a["class"]):
            code, out, err = class_tool(["set", str(a["index"]), str(major), str(minor)])
            if code != 0:
                raise ActionError(f"Could not change {a['product']} back: {(err or out).strip()}")
        alias = "" if saved["alias"] == saved.get("name") else saved["alias"]
        try:
            bz_set(a["path"], "org.bluez.Adapter1", "Alias", GLib.Variant("s", alias))
            bz_set(a["path"], "org.bluez.Adapter1", "Discoverable", GLib.Variant("b", False))
            bz_set(a["path"], "org.bluez.Adapter1", "Pairable", GLib.Variant("b", True))
        except ActionError:
            pass
        del self.cfg["saved_adapters"][a["address"]]
        self.save()

    def apply_adapters(self):
        """Chosen adapters become speakers, the others go back to computers."""
        self.objs = bluez_objects()
        all_a = adapters(self.objs)
        chosen = self.chosen_adapters(all_a)
        for a in all_a:
            if a not in chosen:
                self.make_computer(a)
        for i, a in enumerate(chosen):
            self.make_speaker(a, i + 1 if len(chosen) > 1 else None)

    def turn_on(self):
        if not adapters():
            raise ActionError("No Bluetooth adapter found. Is Bluetooth turned on?")
        self.install_tool()
        self.cfg["speaker_on"] = True
        self.save()
        self.objs = bluez_objects()
        self.pick_free_adapters()
        restarted = self.set_avrcp(not self.cfg["switch_volume"])
        self.apply_adapters()
        restarted = set_wireplumber(wireplumber_text(self.cfg)) or restarted
        self.agent.start()
        name = self.cfg["speaker_name"] or default_speaker_name()
        msg = f"Speaker mode is on. The PC shows up as “{name}”."
        if not self.cfg["switches"]:
            msg += " Use “Pair a Switch” to connect one."
        return msg + (" (Sound restarted for a second.)" if restarted else "")

    def turn_off(self):
        self.cfg["speaker_on"] = False
        self.stop_pairing()
        self.objs = bluez_objects()
        for s in self.switches():
            if s["connected"]:
                try:
                    bz_call(s["path"], "org.bluez.Device1", "Disconnect")
                except ActionError:
                    pass
        errors = []
        for a in adapters(self.objs):
            try:
                self.make_computer(a)
            except ActionError as e:
                errors.append(str(e))
        self.cfg["speaker_on"] = False
        self.save()
        set_wireplumber(None)
        try:
            self.set_avrcp(True)
        except ActionError as e:
            errors.append(str(e))
        if errors:
            raise ActionError("\n".join(errors))
        return "Speaker mode is off. The PC is a normal computer again."

    def update_sound_settings(self):
        if self.cfg["speaker_on"]:
            self.set_avrcp(not self.cfg["switch_volume"])
            set_wireplumber(wireplumber_text(self.cfg))

    # -- pairing
    def start_pairing(self, forget_old=False):
        if not self.cfg["speaker_on"]:
            self.turn_on()
        self.objs = bluez_objects()
        if forget_old:
            for s in self.switches():
                if s["path"] and not s["connected"]:
                    self.forget(s["address"], quiet=True)
            self.objs = bluez_objects()
        self.agent.start()
        chosen = self.chosen_adapters()
        for a in chosen:
            # Pairable stays off: the Switch's pairing is allowed without it.
            bz_set(a["path"], "org.bluez.Adapter1", "DiscoverableTimeout", GLib.Variant("u", PAIR_SECONDS))
            bz_set(a["path"], "org.bluez.Adapter1", "Discoverable", GLib.Variant("b", True))
        self.pair_known = {m for m, d in devices(self.objs).items() if d.get("Paired")}
        self.pair_until = time.time() + PAIR_SECONDS
        addrs = {a["address"] for a in chosen}
        names = " or ".join(f"“{a['alias']}”" for a in adapters() if a["address"] in addrs)
        return (f"Ready to pair for 3 minutes. On the Switch 2: System Settings → "
                f"Bluetooth Audio → Pair, then choose {names}. "
                f"If the Switch shows a number, just accept it.")

    def stop_pairing(self):
        self.pair_until = 0
        if not self.cfg["speaker_on"]:
            self.agent.stop()
        for a in adapters():
            if a["discoverable"]:
                try:
                    bz_set(a["path"], "org.bluez.Adapter1", "Discoverable", GLib.Variant("b", False))
                except ActionError:
                    pass

    def check_new_pairing(self):
        devs = devices(self.objs)
        chosen = {a["path"] for a in self.chosen_adapters()}
        for mac, d in devs.items():
            if not (d.get("Paired") and mac not in self.pair_known and d.get("Adapter") in chosen):
                continue
            if not d.get("Trusted"):
                try:
                    bz_set(d["path"], "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
                except ActionError:
                    pass
            # Keep the window open until the audio channel is set up: the
            # Switch still has questions to ask after pairing.
            if d.get("audio"):
                name = d.get("Alias") or d.get("Name") or mac
                self.cfg["switches"][mac] = {"name": name, "adapter": d.get("Adapter")}
                self.save()
                self.stop_pairing()
                self.notify(f"Paired with {name}. Its sound now plays on this PC. "
                            f"Next time it reconnects by itself.")
                return

    def set_avrcp(self, on):
        """Turns BlueZ's remote control (AVRCP) on or off. Off, the Switch
        keeps its own volume and its slider works (see README, "Volume").
        Restarts Bluetooth, so everything reconnects. Returns True if it
        changed anything."""
        if AVRCP_DROPIN.exists() != on:
            return False
        code, out, err = class_tool(["avrcp-on" if on else "avrcp-off", "0"])
        if code != 0:
            raise ActionError("Could not restart Bluetooth: " + (err or out).strip())
        time.sleep(2)
        return True

    def full_volume(self):
        """Once per connection, the PC plays the Switch at 100%, so the
        Switch's own slider is the volume you hear. (The two volumes can't be
        kept in sync without remote control.)"""
        if not self.cfg["switch_volume"]:
            return
        for s in self.switches():
            mac = s["address"]
            if not s["connected"]:
                self.volume_set.discard(mac)
            elif s["audio"] and mac not in self.volume_set:
                node = switch_node(mac)
                if node:
                    self.volume_set.add(mac)
                    run(["wpctl", "set-volume", node, "1.0"])

    def tidy_switches(self):
        """Keeps the saved Switches callable. A pairing the PC saved for one
        (from before speaker mode turned saving off) blocks its next
        connection, so it goes. Trusted keeps BlueZ from deleting the Switch
        between connections, so the PC can call it."""
        for mac, d in devices(self.objs).items():
            if mac not in self.cfg["switches"]:
                continue
            if d.get("Bonded") and not d.get("Connected"):
                adapter = d["path"].rsplit("/", 1)[0]
                try:
                    bz_call(adapter, "org.bluez.Adapter1", "RemoveDevice", (d["path"],), "(o)")
                    self.objs = bluez_objects()
                except ActionError:
                    pass
            elif not d.get("Trusted"):
                try:
                    bz_set(d["path"], "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
                except ActionError:
                    pass

    def forget(self, mac, quiet=False):
        d = devices().get(mac)
        if d:
            adapter = d["path"].rsplit("/", 1)[0]
            try:
                bz_call(adapter, "org.bluez.Adapter1", "RemoveDevice", (d["path"],), "(o)")
            except ActionError as e:
                if not quiet:
                    raise
        self.cfg["switches"].pop(mac, None)
        self.save()
        return "Forgotten. Also remove the PC from the Switch's list before pairing again."

    def reconnect(self):
        self.objs = bluez_objects()
        known = [s for s in self.switches() if s["known"]]
        if not known and self.cfg["switches"]:
            raise ActionError("The PC can't call the Switch right now. On the Switch, open "
                              "System Settings → Bluetooth Audio and choose this PC.")
        if not known:
            raise ActionError("No Switch paired yet. Use “Pair a Switch” first.")
        for s in known:
            try:
                bz_call(s["path"], "org.bluez.Device1", "Connect", timeout=20000)
                return f"Connected to {s['name']}."
            except ActionError:
                # The Switch often opens the sound channel itself as soon as
                # the PC calls, and BlueZ then says "ProfileUnavailable".
                self.objs = bluez_objects()
                if self.connected_switch():
                    return f"Connected to {s['name']}."
        raise ActionError("Could not connect. Make sure the Switch is awake (not in sleep "
                          "mode), then try again, or choose this PC in the Switch's "
                          "Bluetooth Audio settings.")

    def signal(self):
        """Signal strength of the connected Switch, in plain words."""
        self.objs = bluez_objects()
        s = self.connected_switch()
        if not s:
            raise ActionError("No Switch connected.")
        if not CLASS_TOOL.exists():
            raise ActionError("Turn speaker mode on first.")
        a = next(a for a in adapters(self.objs) if s["path"].startswith(a["path"] + "/"))
        code, out, err = class_tool(["signal", str(a["index"]), s["address"]])
        m = re.search(r"RSSI (-?\d+)", out)
        if not m:
            raise ActionError("Could not read the signal: " + (err or out).strip())
        rssi = int(m.group(1))
        try:
            maker = bz_call(a["path"], PROPS, "Get", ("org.bluez.Adapter1", "Manufacturer"), "(ss)")[0]
        except ActionError:
            maker = None
        if maker == 93:
            # Realtek gives the plain signal level in dBm. Measured with a
            # TP-Link UB500: -25 a few cm away, -30 at 1 m, -38 at 3 m.
            limits = (-60, -75)
        else:
            # The standard: 0 means "as strong as it needs to be" and the
            # number drops below 0 as the signal gets weaker.
            limits = (-5, -12)
        if rssi >= limits[0]:
            words = "Strong"
        elif rssi >= limits[1]:
            words = "OK, but drops are possible"
        else:
            words = "Weak: expect cut-outs"
        return f"Signal from {s['name']} on {a['product']}: {words} ({rssi})."

    def undo_everything(self):
        msgs = []
        if self.cfg["speaker_on"] or self.cfg["saved_adapters"]:
            if CLASS_TOOL.exists():
                msgs.append(self.turn_off())
        for mac in list(self.cfg["switches"]):
            self.forget(mac, quiet=True)
        set_wireplumber(None)
        if any(f.exists() for f in (CLASS_TOOL, POLKIT_POLICY, UDEV_RULE, MODPROBE_CONF)):
            code, out, err = as_root(["uninstall"])
            if "ok" not in out:
                raise ActionError(err.strip() or "Cancelled.")
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()
        return "Everything is undone: the PC is a normal computer, the Switch is forgotten, " \
               "and no system files are left."

    # -- every couple of seconds
    def tick(self):
        now = time.time()
        self.objs = bluez_objects()
        if self.agent.lost():
            self.agent.stop()
        if self.cfg["speaker_on"] and self.agent.reg_id is None:
            try:
                self.agent.start()
            except ActionError as e:
                print(e, file=sys.stderr)
        if self.pair_until:
            if now > self.pair_until:
                self.stop_pairing()
                self.notify("Nothing paired. Try “Pair a Switch” again.")
            else:
                self.check_new_pairing()

        # Bluetooth restarted or the PC rebooted: the adapter is a computer
        # again, and saves pairings again.
        if self.cfg["speaker_on"] and CLASS_TOOL.exists() and now - self.last_fix > 30:
            if any(not is_speaker_class(a["class"]) or a["pairable"]
                   for a in self.chosen_adapters()):
                self.last_fix = now
                try:
                    self.apply_adapters()
                except ActionError as e:
                    print(e, file=sys.stderr)

        if self.cfg["speaker_on"]:
            self.tidy_switches()
            self.full_volume()

        for s in self.switches():
            mac = s["address"]
            before = self.was_connected.get(mac)
            if before and not s["connected"]:
                self.lost_at[mac] = now
            elif s["connected"] and mac in self.lost_at:
                if now - self.lost_at.pop(mac) < DROP_GAP:
                    self.drops.append(now)
            self.was_connected[mac] = s["connected"]
            # Ask it to come back after a drop. If it was turned off, this
            # just fails quietly until DROP_GAP runs out.
            if (self.cfg["speaker_on"] and self.cfg["auto_reconnect"] and s["known"]
                    and mac in self.lost_at and now - self.lost_at[mac] < DROP_GAP
                    and now - self.last_try >= RECONNECT_EVERY):
                self.last_try = now
                bz_call_async(s["path"], "org.bluez.Device1", "Connect")
        self.drops = [t for t in self.drops if now - t < 3600]

    def status_lines(self):
        """First line is the headline."""
        lines = []
        s = self.connected_switch()
        if self.pairing():
            left = int(self.pair_until - time.time())
            lines.append(f"Waiting for the Switch to pair ({left} s left)")
        elif not self.cfg["speaker_on"]:
            lines.append("Speaker mode is off")
        elif s:
            lines.append(f"{s['name']} " + ("is playing on this PC" if s["streaming"] else "is connected"))
        else:
            lines.append("Speaker mode is on, no Switch connected")
        if self.drops:
            lines.append(f"Dropped {len(self.drops)}x in the last hour")
        if self.cfg["speaker_on"]:
            shared = self.sharing()
            if shared:
                lines.append(f"{', '.join(shared)} shares the Switch's adapter: "
                             f"the sound will cut out (see Range tips)")
        if ignored_adapters():
            lines.append("One adapter is ignored: it has the same address as another")
        if power_saving_problems():
            lines.append("Adapter power saving can cause drops")
        return lines


# ---------------------------------------------------------- terminal mode

RANGE_TIPS = """\
Range tips (most helpful first):
• Give the Switch an adapter of its own. A Bluetooth mouse on the same
  adapter makes the sound cut out badly, even right next to the PC: a
  Logitech M720 talks to the PC every 7.5 ms, and the adapter drops the
  Switch's sound to keep up. With two adapters, the tray picks the one with
  no mouse or keyboard on it. Or move the mouse to its USB receiver.
• Two TP-Link UB500s can't work together: they share one address, so Linux
  only uses one of them. Use two different models.
• Plug the adapter into a USB extension cable and put it where it can see the
  Switch: on the desk, not behind the PC. The metal case blocks the signal.
• Keep it away from USB 3 ports (the blue ones) and USB 3 drives and hubs:
  they give off noise on the same frequency as Bluetooth.
• Put the Wi-Fi on 5 GHz if you can. 2.4 GHz Wi-Fi shares the air with Bluetooth.
• Turn off adapter power saving (the “Fix power saving” item).
• If it still cuts, pick “Steady at a distance”: it keeps more sound in
  reserve so short gaps aren't heard, at the cost of some lag.
"""


def cli_main(cmd):
    speaker = Speaker()
    try:
        if cmd == "status":
            speaker.objs = bluez_objects()
            for line in speaker.status_lines():
                print(line)
            for a in adapters(speaker.objs):
                kind = "speaker" if is_speaker_class(a["class"]) else "computer"
                used = " (used)" if a in speaker.chosen_adapters() else ""
                print(f"Adapter {a['hci']}: {a['product']} “{a['alias']}”, shows up as a {kind}{used}")
            for s in speaker.switches():
                print(f"Switch: {s['name']} {s['address']}, "
                      + ("connected" if s["connected"] else "paired" if s["known"] else "not paired here"))
            print("Lag: " + LAG_MODES[speaker.cfg["lag"]][0])
            print("Switch volume buttons: " + ("work" if speaker.cfg["switch_volume"] else "handled by the PC"))
            return
        if cmd == "on":
            print(speaker.turn_on())
        elif cmd == "off":
            print(speaker.turn_off())
        elif cmd == "pair":
            print(speaker.start_pairing())
            loop = GLib.MainLoop()
            speaker.notify = lambda msg: print(msg)

            def tick():
                speaker.tick()
                if not speaker.pairing():
                    loop.quit()
                    return False
                return True
            GLib.timeout_add_seconds(2, tick)
            try:
                loop.run()
            except KeyboardInterrupt:
                speaker.stop_pairing()
        elif cmd == "signal":
            print(speaker.signal())
        elif cmd == "fix":
            code, out, err = as_root(["fix-power"])
            print("Power saving is off for the Bluetooth adapter." if "ok" in out else err)
        elif cmd == "unfix":
            code, out, err = as_root(["unfix-power"])
            print("Power fix removed." if "ok" in out else err)
        elif cmd == "undo":
            print(speaker.undo_everything())
        elif cmd == "tips":
            print(RANGE_TIPS)
        else:
            print(__doc__)
            sys.exit(2)
    except ActionError as e:
        sys.exit(str(e))


# --------------------------------------------------------------- tray mode

def tray_main():
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            sys.exit("The tray needs PyQt6 or PyQt5 (CachyOS/Arch: sudo pacman -S python-pyqt6, "
                     "Debian/MX: sudo apt install python3-pyqt5).\n"
                     "Without it, use the terminal commands:\n" + __doc__)

    global USE_PKEXEC
    USE_PKEXEC = True
    Qt = QtCore.Qt
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    def make_icon(color, waves):
        """A speaker with 0-2 sound waves."""
        pm = QtGui.QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        c = QtGui.QColor(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRect(6, 22, 12, 20)
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(18, 22), QtCore.QPointF(34, 8),
                                       QtCore.QPointF(34, 56), QtCore.QPointF(18, 42)]))
        p.setPen(QtGui.QPen(c, 5, cap=Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(waves):
            r = 12 + i * 11
            p.drawArc(QtCore.QRectF(34 - r, 32 - r, 2 * r, 2 * r), -50 * 16, 100 * 16)
        p.end()
        return QtGui.QIcon(pm)

    class Tray(QtWidgets.QSystemTrayIcon):
        def __init__(self):
            super().__init__()
            self.speaker = Speaker(notify=lambda msg: self.showMessage(APP_NAME, msg))
            self.speaker.agent.ask = lambda name: self.ask(
                f"{name} wants to pair with this PC. Allow it?")
            self.make_menu()
            self.activated.connect(self.on_click)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.refresh()
            self.show()
            self.timer.start(2000)
            # Speaker mode was left on but a newer version needs more set up
            # (or this folder was copied from another PC): finish it.
            if self.speaker.cfg["speaker_on"] and not (
                    tool_ready()):
                QtCore.QTimer.singleShot(1500, lambda: self.do(self.speaker.turn_on))

        # -- state
        def refresh(self):
            self.speaker.tick()
            self.update_icon()
            self.update_menu()

        def update_icon(self):
            sp = self.speaker
            s = sp.connected_switch()
            if sp.pairing():
                key = ("#2a7fd4", 1 + int(time.time()) % 2)   # blinks while pairing
            elif not sp.cfg["speaker_on"]:
                key = ("#888888", 0)
            elif s:
                key = ("#e08a00" if sp.drops else "#2e9e44", 2)
            else:
                key = ("#2a7fd4", 0)
            if key != getattr(self, "icon_key", None):
                self.icon_key = key
                self.setIcon(make_icon(*key))
            tip = "\n".join(sp.status_lines())
            if tip != self.toolTip():
                self.setToolTip(tip)

        def make_menu(self):
            """One flat menu, built once: Plasma crashes if submenus are
            rebuilt while it shows them."""
            m = self.menu = QtWidgets.QMenu()
            self.a_head = m.addAction("")
            self.a_head.setEnabled(False)
            self.a_info = m.addAction("")
            self.a_info.setEnabled(False)
            m.addSeparator()
            self.a_on = m.addAction("Speaker mode")
            self.a_on.setCheckable(True)
            self.a_on.triggered.connect(self.toggle_speaker)
            self.a_pair = m.addAction("Pair a Switch…", self.pair)
            self.a_stop = m.addAction("Stop pairing", lambda: self.do(lambda: (self.speaker.stop_pairing(), "Pairing stopped.")[1]))
            self.a_reconnect = m.addAction("Connect the Switch", lambda: self.do(self.speaker.reconnect))
            self.a_signal = m.addAction("Check signal strength", lambda: self.do(self.speaker.signal))

            m.addSection("Sound")
            group = QtGui.QActionGroup(m) if hasattr(QtGui, "QActionGroup") else QtWidgets.QActionGroup(m)
            self.a_lag = {}
            for key, (label, _, _) in LAG_MODES.items():
                a = m.addAction(label)
                a.setCheckable(True)
                group.addAction(a)
                a.triggered.connect(lambda _=False, k=key: self.set_sound("lag", k))
                self.a_lag[key] = a
            self.a_volume = m.addAction("Volume buttons on the Switch work")
            self.a_volume.setCheckable(True)
            self.a_volume.triggered.connect(lambda on: self.set_sound("switch_volume", on))
            self.a_auto = m.addAction("Reconnect after a drop")
            self.a_auto.setCheckable(True)
            self.a_auto.triggered.connect(lambda on: self.set_option("auto_reconnect", on))

            m.addSection("Bluetooth adapters")
            self.a_adapters = []
            for i in range(4):
                a = m.addAction("")
                a.setCheckable(True)
                a.triggered.connect(lambda on, i=i: self.toggle_adapter(i, on))
                self.a_adapters.append(a)
            self.a_adapter_note = m.addAction("A Switch connects to one adapter at a time")
            self.a_adapter_note.setEnabled(False)

            m.addSection("Drops and range")
            self.a_fix = m.addAction("Fix power saving (stops drops)…", lambda: self.root(["fix-power"],
                                     "Power saving is off for the Bluetooth adapter, now and after reboots."))
            self.a_unfix = m.addAction("Undo power fix", lambda: self.root(["unfix-power"],
                                       "Power fix removed. It goes back to normal after a reboot."))
            m.addAction("Range tips…", lambda: QtWidgets.QMessageBox.information(None, APP_NAME, RANGE_TIPS))
            m.addSeparator()
            self.a_forget = m.addAction("Forget the Switch…", self.forget)
            m.addAction("Undo everything…", self.undo)
            m.addAction("Open folder", lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(APP_DIR))))
            m.addSeparator()
            m.addAction("Quit", self.quit)
            self.setContextMenu(m)

        def update_menu(self):
            """Changes only what differs, so Plasma isn't sent needless updates."""
            def text(a, t):
                if a.text() != t:
                    a.setText(t)

            def show(a, on):
                if a.isVisible() != on:
                    a.setVisible(on)

            def check(a, on):
                if a.isChecked() != on:
                    a.setChecked(on)

            sp = self.speaker
            lines = sp.status_lines()
            text(self.a_head, lines[0])
            text(self.a_info, "  ·  ".join(lines[1:]))
            show(self.a_info, len(lines) > 1)
            on = sp.cfg["speaker_on"]
            check(self.a_on, on)
            pairing = sp.pairing()
            show(self.a_pair, not pairing)
            show(self.a_stop, pairing)
            switches = sp.switches()
            connected = any(s["connected"] for s in switches)
            show(self.a_reconnect, on and any(s["known"] for s in switches) and not connected)
            show(self.a_signal, connected)
            show(self.a_forget, bool(switches))
            for key, a in self.a_lag.items():
                check(a, sp.cfg["lag"] == key)
            check(self.a_volume, sp.cfg["switch_volume"])
            check(self.a_auto, sp.cfg["auto_reconnect"])

            all_a = adapters(sp.objs)
            chosen = sp.chosen_adapters(all_a)
            self.adapter_list = all_a
            for i, a in enumerate(self.a_adapters):
                if i < len(all_a):
                    ad = all_a[i]
                    text(a, f"{ad['product']} ({ad['hci']})")
                    check(a, ad in chosen)
                show(a, i < len(all_a) and len(all_a) > 1)
            show(self.a_adapter_note, len(all_a) > 1)

            problems = power_saving_problems()
            show(self.a_fix, bool(problems))
            show(self.a_unfix, not problems and UDEV_RULE.exists())

        # -- actions
        def do(self, fn):
            self.timer.stop()
            try:
                msg = fn()
                if msg:
                    self.showMessage(APP_NAME, msg)
            except ActionError as e:
                QtWidgets.QMessageBox.warning(None, APP_NAME, str(e))
            finally:
                self.refresh()
                self.timer.start(2000)

        def root(self, args, msg):
            def fn():
                code, out, err = as_root(args)
                if "ok" not in out:
                    raise ActionError(err.strip() or "Cancelled.")
                return msg
            self.do(fn)

        def toggle_speaker(self, on):
            if on:
                if not CLASS_TOOL.exists() and not self.ask(
                        "Speaker mode makes this PC show up as a Bluetooth speaker.\n\n"
                        "The first time, it asks for your password once to install a small "
                        "helper, so it never has to ask again. “Undo everything” removes it.\n\n"
                        "Turn it on?"):
                    self.refresh()
                    return
                self.do(self.speaker.turn_on)
            else:
                self.do(self.speaker.turn_off)

        def pair(self):
            sp = self.speaker
            forget = False
            old = [s for s in sp.switches() if s["known"] and not s["connected"]]
            if old:
                forget = self.ask(
                    f"{old[0]['name']} is already paired with this PC.\n\n"
                    "If you removed the PC from the Switch's list, the old pairing must go "
                    "too or the new one fails. Forget the old pairing?")
            self.do(lambda: sp.start_pairing(forget_old=forget))

        def forget(self):
            sp = self.speaker
            s = next(iter(sp.switches()), None)
            if s and self.ask(f"Forget {s['name']}? You will need to pair it again."):
                self.do(lambda: sp.forget(s["address"]))

        def undo(self):
            if self.ask("Undo everything?\n\n"
                        "• The PC goes back to being a normal computer\n"
                        "• The Switch is forgotten\n"
                        "• The sound settings and the installed helper are removed\n"
                        "• The power fix is removed\n\n"
                        "It asks for your password to remove the system files."):
                self.do(self.speaker.undo_everything)

        def set_sound(self, key, value):
            sp = self.speaker
            if sp.cfg[key] == value:
                return
            if sp.cfg["speaker_on"] and sp.connected_switch() and not self.ask(
                    "Changing this restarts the PC's sound for a second, and the Switch "
                    "reconnects. Go ahead?"):
                self.refresh()
                return
            sp.cfg[key] = value
            sp.save()
            self.do(sp.update_sound_settings)

        def set_option(self, key, value):
            self.speaker.cfg[key] = value
            self.speaker.save()

        def toggle_adapter(self, i, on):
            sp = self.speaker
            all_a = self.adapter_list
            chosen = {a["address"] for a in sp.chosen_adapters(all_a)}
            addr = all_a[i]["address"]
            chosen = chosen | {addr} if on else chosen - {addr}
            if not chosen:
                self.refresh()  # keep at least one
                return
            sp.cfg["adapters"] = [] if len(chosen) == len(all_a) else sorted(chosen)
            sp.save()
            if sp.cfg["speaker_on"]:
                self.do(lambda: sp.apply_adapters())
            else:
                self.refresh()

        def ask(self, text):
            return QtWidgets.QMessageBox.question(None, APP_NAME, text) \
                == QtWidgets.QMessageBox.StandardButton.Yes

        def on_click(self, reason):
            # Left click shows the status; right click opens the menu.
            if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
                self.showMessage(APP_NAME, self.toolTip())

        def quit(self):
            self.speaker.stop_pairing()
            app.quit()

    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit("No system tray found. Use the terminal commands instead:\n" + __doc__)
    # One tray at a time: two would fight over pairing.
    import fcntl
    lock = open(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "btspeakertray.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("BT Speaker Tray is already running. Look for the speaker icon in the tray.")
    tray = Tray()
    sys.exit(app.exec())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--helper":
        helper_main(sys.argv[2:])
    elif len(sys.argv) > 1:
        cli_main(sys.argv[1])
    else:
        tray_main()
