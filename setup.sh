#!/bin/bash
#
# ARC Setup Script v3.0
# GStreamer-based video routing system for Raspberry Pi Zero 2 W
#
# Usage:
#   sudo ./setup.sh controller   # for the receiver/controller Pi
#   sudo ./setup.sh sender       # for the camera/sender Pis
#
# Run as root.

set -e

# ----------------------------------------------------------------------
# Configuration -- edit these to match your environment
# ----------------------------------------------------------------------

# Lab/dev wifi networks (high priority, used on the bench)
LAB_NETWORKS=(
    "TRT-ARC-1:IREC2025!"
    "TRT-ARC-2:IREC2025!"
)

# In-flight network (the AP that the controller hosts)
FLIGHT_SSID="TRT-ARC-FLIGHT"
FLIGHT_PSK="IREC2025!"

# UART baud rate (must match flight computer firmware)
UART_BAUD=115200

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

# Idempotent NetworkManager connection add
nm_add_client() {
    local name="$1" ssid="$2" psk="$3" priority="$4"
    if nmcli -t -f NAME con show | grep -qx "$name"; then
        info "Connection '$name' already exists, skipping"
        return
    fi
    info "Adding client connection '$name' (SSID: $ssid)"
    nmcli connection add \
        type wifi \
        con-name "$name" \
        ssid "$ssid" \
        autoconnect yes \
        connection.autoconnect-priority "$priority" \
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
    error "Usage: sudo $0 {controller|sender}"
    exit 1
fi

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
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-libcamera \
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
    ssid="${net%%:*}"
    psk="${net##*:}"
    nm_add_client "lab-${ssid}" "$ssid" "$psk" 10
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

# ----------------------------------------------------------------------
# Application directory
# ----------------------------------------------------------------------

APP_DIR="/opt/arc"
info "Creating application directory at $APP_DIR..."
mkdir -p "$APP_DIR"
chown "${SUDO_USER:-pi}:${SUDO_USER:-pi}" "$APP_DIR"

# Recording directory (senders only, but harmless on controller)
if [ "$ROLE" = "sender" ]; then
    mkdir -p /var/arc/recordings
    chown "${SUDO_USER:-pi}:${SUDO_USER:-pi}" /var/arc/recordings
fi

# ----------------------------------------------------------------------
# Systemd service for the application
# ----------------------------------------------------------------------

if [ "$ROLE" = "controller" ]; then
    SERVICE_NAME="arc-controller"
    SERVICE_DESC="ARC Controller (video router and FC interface)"
    EXEC_START="/usr/bin/python3 ${APP_DIR}/controller.py"
else
    SERVICE_NAME="arc-sender"
    SERVICE_DESC="ARC Sender (camera capture and stream)"
    EXEC_START="/usr/bin/python3 ${APP_DIR}/sender.py"
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
WorkingDirectory=${APP_DIR}
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

warn "Service installed but NOT started -- start it after you've placed your"
warn "Python code in ${APP_DIR}/ and rebooted to apply UART/camera config."

# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------

echo ""
echo "=================================================="
echo "  Setup complete -- role: $ROLE"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Place your Python code in ${APP_DIR}/"
echo "  2. Reboot to apply UART, camera, and wifi config:"
echo "       sudo reboot"
echo "  3. After reboot, start the service:"
echo "       sudo systemctl start ${SERVICE_NAME}"
echo "  4. Check logs with:"
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
