#!/bin/bash
#
# ARC Setup Script v3.0
# GStreamer-based video routing system for Pi 5 controller + Pi Zero 2 W senders
#
# Usage:
#   sudo ./setup.sh controller   # for the receiver/controller Pi
#   sudo ./setup.sh sender       # for the camera/sender Pis
#
# Useful options:
#   sudo ./setup.sh sender --addr 0x12 --name sender-c --paired-fc 0x03
#   sudo ./setup.sh controller --senders "0x12:sender-c:arcpi2.local:0x03,0x13:sender-l1:arcpi3.local:0x04"
#
# Run as root.

set -e

# ----------------------------------------------------------------------
# Configuration -- edit these to match your environment
# ----------------------------------------------------------------------

# Lab/dev wifi networks (high priority, used on the bench)
# Format: SSID:PSK:priority. Higher priority wins when multiple are visible.
LAB_NETWORKS=(
    "TRT-ARC-1:IREC2025!:50"
    "TRT-ARC-2:IREC2025!:40"
)

# In-flight network (the AP that the controller hosts)
FLIGHT_SSID="TRT-ARC-FLIGHT"
FLIGHT_PSK="IREC2025!"

# UART baud rate (must match flight computer firmware)
UART_BAUD=115200

# mDNS host for the Controller. Senders use this in their config.
CONTROLLER_HOST="arcpi1.local"

# Default Controller fleet. Override with --senders for your actual bench.
# Format: addr:name:host:paired_fc, where paired_fc may be empty.
CONTROLLER_SENDERS="0x12:sender-c:arcpi2.local:0x03,0x13:sender-l1:arcpi3.local:0x04,0x14:sender-l2:arcpi4.local:,0x15:sender-ground:arcpi5.local:"
CONTROLLER_VIDEO_SINK="kmssink sync=false"

SENDER_ADDR=""
SENDER_NAME=""
SENDER_PAIRED_FC=""
FORCE_CONFIG=false

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[*]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*" >&2; }

require_root() {
    if [ $EUID -ne 0 ]; then
        error "Please run as root (sudo)"
        exit 1
    fi
}

usage() {
    cat >&2 <<EOF
Usage: sudo $0 {controller|sender} [options]

Controller options:
  --senders LIST          addr:name:host:paired_fc entries, comma-separated

Sender options:
  --addr ADDR             Sender ARC address, e.g. 0x12
  --name NAME             Sender name, e.g. sender-c
  --paired-fc ADDR        Paired FC address, e.g. 0x03. Use "none" for video-only.
  --controller-host HOST  Controller mDNS host (default: arcpi1.local)

General options:
  --force-config          Rewrite generated /etc/arc/*.toml config
EOF
}

# Idempotent apt install -- only installs missing packages
apt_install() {
    local missing=()
    for pkg in "$@"; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            missing+=("$pkg")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        info "Installing: ${missing[*]}"
        apt install -y "${missing[@]}"
    fi
}

# Idempotent NetworkManager connection add.
# Force ARC-managed client profiles onto 2.4 GHz (`bg`). This matters for
# Pi Zero 2 W senders, and it also keeps the Pi 5 controller from choosing a
# 5 GHz copy of the lab SSID when both bands share the same name.
nm_add_client() {
    local name="$1" ssid="$2" psk="$3" priority="$4"
    if nmcli -t -f NAME con show | grep -qx "$name"; then
        info "Connection '$name' already exists, updating priority and 2.4 GHz band"
        nmcli connection modify "$name" \
            connection.autoconnect-priority "$priority" \
            802-11-wireless.band bg
        return
    fi
    info "Adding client connection '$name' (SSID: $ssid)"
    nmcli connection add \
        type wifi \
        con-name "$name" \
        ssid "$ssid" \
        autoconnect yes \
        connection.autoconnect-priority "$priority" \
        802-11-wireless.band bg \
        wifi-sec.key-mgmt WPA-PSK \
        wifi-sec.psk "$psk"
}

nm_add_ap() {
    local name="$1" ssid="$2" psk="$3" autoconnect="$4"
    if nmcli -t -f NAME con show | grep -qx "$name"; then
        info "Connection '$name' already exists, skipping"
        return
    fi
    info "Adding AP connection '$name' (SSID: $ssid, autoconnect: $autoconnect)"
    nmcli connection add \
        type wifi \
        ifname wlan0 \
        con-name "$name" \
        ssid "$ssid" \
        autoconnect "$autoconnect"
    # Keep the flight AP on 2.4 GHz so every Zero 2 W sender can join it.
    nmcli connection modify "$name" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        ipv4.method shared \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$psk"
}

# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------

require_root

ROLE="${1:-}"
if [ "$ROLE" != "controller" ] && [ "$ROLE" != "sender" ]; then
    usage
    exit 1
fi
shift

while [ $# -gt 0 ]; do
    case "$1" in
        --addr)
            SENDER_ADDR="${2:-}"
            shift 2
            ;;
        --name)
            SENDER_NAME="${2:-}"
            shift 2
            ;;
        --paired-fc)
            SENDER_PAIRED_FC="${2:-}"
            shift 2
            ;;
        --controller-host)
            CONTROLLER_HOST="${2:-}"
            shift 2
            ;;
        --senders)
            CONTROLLER_SENDERS="${2:-}"
            shift 2
            ;;
        --force-config)
            FORCE_CONFIG=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================="
echo "  ARC Setup v3.0  --  role: $ROLE"
echo "=================================================="

# ----------------------------------------------------------------------
# Common dependencies (both roles)
# ----------------------------------------------------------------------

info "Updating apt..."
apt update

info "Installing core dependencies..."
apt_install \
    git \
    python3 \
    python3-pip \
    python3-gi \
    python3-gst-1.0 \
    python3-serial \
    python3-serial-asyncio \
    avahi-daemon \
    libnss-mdns \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-gl \
    gstreamer1.0-libcamera \
    libdrm-tests \
    tcpdump \
    v4l-utils \
    rpicam-apps \
    ffmpeg \
    chrony

# ----------------------------------------------------------------------
# UART setup (both roles -- senders may want it for debug, controller needs it)
# ----------------------------------------------------------------------

info "Configuring UART hardware..."

CONFIG_TXT="/boot/firmware/config.txt"
CMDLINE_TXT="/boot/firmware/cmdline.txt"

# Older Pi OS used /boot/ instead of /boot/firmware/
if [ ! -f "$CONFIG_TXT" ] && [ -f "/boot/config.txt" ]; then
    CONFIG_TXT="/boot/config.txt"
    CMDLINE_TXT="/boot/cmdline.txt"
fi

# Enable UART
if ! grep -q "^enable_uart=1" "$CONFIG_TXT"; then
    echo "enable_uart=1" >> "$CONFIG_TXT"
    info "Enabled UART in $CONFIG_TXT"
fi

# Disable Bluetooth's claim on the PL011 hardware UART
# (otherwise /dev/serial0 is the slower mini-UART)
if ! grep -q "^dtoverlay=disable-bt" "$CONFIG_TXT"; then
    echo "dtoverlay=disable-bt" >> "$CONFIG_TXT"
    info "Disabled Bluetooth to free hardware UART"
fi

# Remove serial console from kernel cmdline so it doesn't grab /dev/serial0
if grep -q "console=serial0" "$CMDLINE_TXT"; then
    sed -i 's/console=serial0,[0-9]\+ //g' "$CMDLINE_TXT"
    info "Removed serial console from kernel cmdline"
fi

# Disable the getty that listens on serial0
systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable --now hciuart.service 2>/dev/null || true

# ----------------------------------------------------------------------
# Camera setup (both roles -- controller may have local camera too)
# ----------------------------------------------------------------------

info "Ensuring camera is enabled..."
if ! grep -q "^camera_auto_detect=1" "$CONFIG_TXT"; then
    echo "camera_auto_detect=1" >> "$CONFIG_TXT"
fi

enable_composite_video() {
    local model overlay
    model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
    overlay="vc4-kms-v3d"
    if printf '%s' "$model" | grep -q "Raspberry Pi 5"; then
        overlay="vc4-kms-v3d-pi5"
    fi

    info "Enabling composite video output..."
    if grep -q "^display_auto_detect=" "$CONFIG_TXT"; then
        sed -i 's/^display_auto_detect=.*/display_auto_detect=0/' "$CONFIG_TXT"
    else
        echo "display_auto_detect=0" >> "$CONFIG_TXT"
    fi

    if grep -q "^enable_tvout=" "$CONFIG_TXT"; then
        sed -i 's/^enable_tvout=.*/enable_tvout=1/' "$CONFIG_TXT"
    else
        echo "enable_tvout=1" >> "$CONFIG_TXT"
    fi

    if grep -q "^dtoverlay=vc4-kms-v3d" "$CONFIG_TXT"; then
        sed -i "s/^dtoverlay=vc4-kms-v3d[^[:space:]]*/dtoverlay=${overlay},composite/" "$CONFIG_TXT"
    else
        echo "dtoverlay=${overlay},composite" >> "$CONFIG_TXT"
    fi

    if ! grep -qw "vc4.tv_norm=" "$CMDLINE_TXT"; then
        sed -i 's/$/ vc4.tv_norm=NTSC/' "$CMDLINE_TXT"
    fi
    if ! grep -qw "video=Composite-1:" "$CMDLINE_TXT"; then
        sed -i 's/$/ video=Composite-1:720x480i,tv_mode=NTSC/' "$CMDLINE_TXT"
    fi
}

if [ "$ROLE" = "controller" ]; then
    enable_composite_video
fi

# ----------------------------------------------------------------------
# Wifi power save off (both roles -- latency matters everywhere)
# ----------------------------------------------------------------------

info "Disabling wifi power save..."
cat > /etc/systemd/system/wifi-powersave-off.service <<EOF
[Unit]
Description=Disable wifi power save
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev wlan0 set power_save off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl enable wifi-powersave-off.service

# ----------------------------------------------------------------------
# Wifi network profiles
# ----------------------------------------------------------------------

info "Configuring wifi networks..."

# Lab/dev networks -- both roles auto-join these when in range
for net in "${LAB_NETWORKS[@]}"; do
    IFS=':' read -r ssid psk priority <<< "$net"
    nm_add_client "lab-${ssid}" "$ssid" "$psk" "${priority:-10}"
done

if [ "$ROLE" = "controller" ]; then
    # Controller hosts the flight network. autoconnect=yes so it comes up at boot.
    nm_add_ap "flight-ap" "$FLIGHT_SSID" "$FLIGHT_PSK" "yes"
    # Lower priority than lab networks: on the bench, prefer client mode on lab wifi.
    nmcli connection modify flight-ap connection.autoconnect-priority 5
else
    # Sender joins the flight network as a client. Lower priority than lab networks
    # so on the bench it prefers lab wifi; in flight, only the AP is around.
    nm_add_client "flight-client" "$FLIGHT_SSID" "$FLIGHT_PSK" 8
fi

# ----------------------------------------------------------------------
# Time sync
# ----------------------------------------------------------------------

if [ "$ROLE" = "controller" ]; then
    info "Configuring chrony as time server..."
    cat > /etc/chrony/conf.d/arc-server.conf <<EOF
# Allow flight network clients to sync from us
allow 10.42.0.0/16
# Serve local time even without upstream sync (we're the authority in flight)
local stratum 8
EOF
else
    info "Configuring chrony to sync from controller..."
    # The controller's IP on its own AP is 10.42.0.1 (NetworkManager 'shared' default)
    cat > /etc/chrony/conf.d/arc-client.conf <<EOF
server 10.42.0.1 iburst prefer
EOF
fi

systemctl restart chrony || warn "chrony restart failed (may need reboot)"
systemctl enable --now avahi-daemon || warn "avahi-daemon enable failed"

# ----------------------------------------------------------------------
# Application directory
# ----------------------------------------------------------------------

APP_DIR="/opt/arc"
info "Creating application directory at $APP_DIR..."
mkdir -p "$APP_DIR"
chown "${SUDO_USER:-pi}:${SUDO_USER:-pi}" "$APP_DIR"

if [ -d "${SCRIPT_DIR}/control-plane/arc" ]; then
    info "Installing control-plane package to ${APP_DIR}/control-plane..."
    rm -rf "${APP_DIR}/control-plane"
    mkdir -p "${APP_DIR}/control-plane"
    cp -a "${SCRIPT_DIR}/control-plane/arc" "${APP_DIR}/control-plane/"
    chown -R "${SUDO_USER:-pi}:${SUDO_USER:-pi}" "${APP_DIR}/control-plane"
else
    warn "Could not find ${SCRIPT_DIR}/control-plane/arc; place code in ${APP_DIR}/control-plane before starting services."
fi

CONFIG_DIR="/etc/arc"
mkdir -p "$CONFIG_DIR"

infer_sender_addr_from_hostname() {
    local host digit
    host="$(hostname)"
    digit="$(printf '%s' "$host" | grep -o '[0-9]' | tail -n 1 || true)"
    if [ -n "$digit" ]; then
        printf '0x1%s' "$digit"
    else
        printf '0x12'
    fi
}

sender_name_for_addr() {
    case "$1" in
        0x11|17) printf 'sender-n' ;;
        0x12|18) printf 'sender-c' ;;
        0x13|19) printf 'sender-l1' ;;
        0x14|20) printf 'sender-l2' ;;
        0x15|21) printf 'sender-ground' ;;
        *) printf 'sender-%s' "$1" ;;
    esac
}

default_paired_fc_for_addr() {
    case "$1" in
        0x12|18) printf '0x03' ;;
        0x13|19) printf '0x04' ;;
        *) printf '' ;;
    esac
}

configure_controller_video_sink() {
    local model
    model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
    if printf '%s' "$model" | grep -q "Raspberry Pi 5"; then
        # Pi 5 composite is exposed by the RP1 VEC DRM device, not vc4.
        CONTROLLER_VIDEO_SINK="kmssink driver-name=drm-rp1-vec sync=false"
    else
        CONTROLLER_VIDEO_SINK="kmssink sync=false"
    fi
    info "Controller video sink: ${CONTROLLER_VIDEO_SINK}"
}

first_controller_sender_addr() {
    local first addr _rest
    first="${CONTROLLER_SENDERS%%,*}"
    IFS=':' read -r addr _rest <<< "$first"
    if [ -n "$addr" ]; then
        printf '%s' "$addr"
    else
        printf '0x00'
    fi
}

write_controller_config() {
    local path="${CONFIG_DIR}/controller.toml"
    if [ -f "$path" ] && [ "$FORCE_CONFIG" != "true" ]; then
        info "$path already exists, leaving it unchanged"
        return
    fi
    local initial_remote_source
    initial_remote_source="$(first_controller_sender_addr)"
    info "Writing default Controller config to $path"
    cat > "$path" <<EOF
[node]
address = 0x10

[uart]
device = "/dev/serial0"
baud = ${UART_BAUD}

[overlay]
callsign = "KD3BBP"

[controller]
listen_port = 6000

[video]
mixer = "glvideomixer"
sink = "${CONTROLLER_VIDEO_SINK}"
startup_layout = "split"
switch_mode = "selector"
warm_remote_streams = false

[layouts.local_full]
slot_0 = { xpos = 40, ypos = 0, width = 640, height = 480, alpha = 1.0 }
slot_1 = { alpha = 0.0 }

[layouts.remote_full]
slot_0 = { alpha = 0.0 }
slot_1 = { xpos = 40, ypos = 0, width = 640, height = 480, alpha = 1.0 }

[layouts.split]
slot_0 = { xpos = 40, ypos = 0, width = 640, height = 480, alpha = 1.0, z = 1 }
slot_1 = { xpos = 420, ypos = 280, width = 240, height = 160, alpha = 1.0, z = 2 }

[sources]
slot_0 = 0x10
slot_1 = ${initial_remote_source}

EOF
    IFS=',' read -ra sender_entries <<< "$CONTROLLER_SENDERS"
    for entry in "${sender_entries[@]}"; do
        IFS=':' read -r addr name host paired_fc <<< "$entry"
        [ -n "$addr" ] || continue
        cat >> "$path" <<EOF
[[senders]]
id = ${addr}
name = "${name}"
ip = "${host}"
EOF
        if [ -n "$paired_fc" ] && [ "$paired_fc" != "none" ]; then
            echo "paired_fc = ${paired_fc}" >> "$path"
        fi
        echo "" >> "$path"
    done
}

write_sender_config() {
    local path="${CONFIG_DIR}/sender.toml"
    if [ -f "$path" ] && [ "$FORCE_CONFIG" != "true" ]; then
        info "$path already exists, leaving it unchanged"
        return
    fi
    SENDER_ADDR="${SENDER_ADDR:-$(infer_sender_addr_from_hostname)}"
    SENDER_NAME="${SENDER_NAME:-$(sender_name_for_addr "$SENDER_ADDR")}"
    if [ -z "$SENDER_PAIRED_FC" ]; then
        SENDER_PAIRED_FC="$(default_paired_fc_for_addr "$SENDER_ADDR")"
    fi
    info "Writing default Sender config to $path"
    cat > "$path" <<EOF
[node]
address = ${SENDER_ADDR}
name = "${SENDER_NAME}"
EOF
    if [ -n "$SENDER_PAIRED_FC" ] && [ "$SENDER_PAIRED_FC" != "none" ]; then
        echo "paired_fc = ${SENDER_PAIRED_FC}" >> "$path"
    fi
    cat >> "$path" <<EOF

[controller]
ip = "${CONTROLLER_HOST}"
port = 6000

[video]
width = 640
height = 480
framerate = 15
bitrate = 1200000
encoder = "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=15 bitrate=1200"
start_stream_on_boot = true

[recording]
path = "/var/arc/recordings/"
EOF
    if [ -n "$SENDER_PAIRED_FC" ] && [ "$SENDER_PAIRED_FC" != "none" ]; then
        cat >> "$path" <<EOF

[uart]
device = "/dev/serial0"
baud = ${UART_BAUD}
EOF
    fi
}

# Recording directory (senders only, but harmless on controller)
if [ "$ROLE" = "sender" ]; then
    mkdir -p /var/arc/recordings
    chown "${SUDO_USER:-pi}:${SUDO_USER:-pi}" /var/arc/recordings
fi

if [ "$ROLE" = "controller" ]; then
    configure_controller_video_sink
    write_controller_config
else
    write_sender_config
fi
info "Setting system to boot without the desktop so camera/KMS devices are available..."
systemctl set-default multi-user.target

# ----------------------------------------------------------------------
# Systemd service for the application
# ----------------------------------------------------------------------

if [ "$ROLE" = "controller" ]; then
    SERVICE_NAME="arc-controller"
    SERVICE_DESC="ARC Controller (video router and FC interface)"
    EXEC_START="/usr/bin/python3 -m arc.controller_main --config ${CONFIG_DIR}/controller.toml"
else
    SERVICE_NAME="arc-sender"
    SERVICE_DESC="ARC Sender (camera capture and stream)"
    EXEC_START="/usr/bin/python3 -m arc.sender_main --config ${CONFIG_DIR}/sender.toml"
fi

info "Installing systemd service: ${SERVICE_NAME}.service"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${SERVICE_DESC}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-pi}
WorkingDirectory=${APP_DIR}/control-plane
Environment=PYTHONUNBUFFERED=1
Environment=GST_GL_PLATFORM=egl
Environment=GST_GL_WINDOW=gbm
ExecStart=${EXEC_START}
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

warn "Service installed and enabled but NOT started."
warn "Reboot first to apply UART/camera config, then the service will start automatically."

# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------

echo ""
echo "=================================================="
echo "  Setup complete -- role: $ROLE"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Review generated config in ${CONFIG_DIR}/"
echo "  2. Reboot to apply UART, camera, wifi, and service config:"
echo "       sudo reboot"
echo "  3. Check service status:"
echo "       systemctl status ${SERVICE_NAME}"
echo "  4. Follow logs with:"
echo "       journalctl -u ${SERVICE_NAME} -f"
echo ""
if [ "$ROLE" = "controller" ]; then
    echo "  The flight AP '$FLIGHT_SSID' will come up automatically on boot."
    echo "  Senders will get IPs in the 10.42.0.0/24 range; this Pi is 10.42.0.1."
else
    echo "  This sender will auto-join '$FLIGHT_SSID' when in range."
    echo "  Recordings will be saved to /var/arc/recordings/"
fi
echo ""
