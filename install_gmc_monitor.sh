#!/bin/bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}GMC-300E Plus Home Assistant Monitor Installation${NC}"
echo "============================================="

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   echo -e "${RED}This script should not be run as root${NC}"
   exit 1
fi

# Check for required system packages
echo -e "${YELLOW}Checking system requirements...${NC}"
MISSING_PACKAGES=""

for pkg in python3 python3-pip python3-venv mosquitto mosquitto-clients; do
    if ! dpkg -l | grep -q "^ii  $pkg "; then
        MISSING_PACKAGES="$MISSING_PACKAGES $pkg"
    fi
done

if [ ! -z "$MISSING_PACKAGES" ]; then
    echo -e "${YELLOW}Installing missing packages:$MISSING_PACKAGES${NC}"
    sudo apt update
    sudo apt install -y $MISSING_PACKAGES
fi

# Setup swap if not configured
CURRENT_SWAP=$(grep CONF_SWAPSIZE /etc/dphys-swapfile | grep -o '[0-9]\+' || echo "100")
if [ "$CURRENT_SWAP" -lt "1024" ]; then
    echo -e "${YELLOW}Configuring swap for Pi Zero 2W...${NC}"
    sudo dphys-swapfile swapoff
    sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
    sudo dphys-swapfile setup
    sudo dphys-swapfile swapon
fi

# Create application directory
INSTALL_DIR="/opt/gmc-monitor"
echo -e "${YELLOW}Creating application directory: $INSTALL_DIR${NC}"
sudo mkdir -p $INSTALL_DIR
sudo chown $USER:$USER $INSTALL_DIR

# Setup Python virtual environment
echo -e "${YELLOW}Setting up Python environment...${NC}"
cd $INSTALL_DIR
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install paho-mqtt pyserial

# Configure Mosquitto
echo -e "${YELLOW}Configuring MQTT broker...${NC}"
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Enable serial port access
echo -e "${YELLOW}Configuring serial port access...${NC}"
sudo usermod -a -G dialout,uucp $USER

# Check for brltty conflict
if [ -f "/usr/lib/udev/rules.d/85-brltty.rules" ]; then
    echo -e "${YELLOW}Fixing brltty conflict for GMC device...${NC}"
    sudo sed -i 's/^.*idVendor=1a86.*idProduct=7523.*/#&/' /usr/lib/udev/rules.d/85-brltty.rules
fi

echo -e "${GREEN}Installation completed successfully!${NC}"
echo ""
echo "Next steps:"
echo "1. Copy gmc_monitor.py to $INSTALL_DIR/"
echo "2. Copy gmc-monitor.service to /etc/systemd/system/"
echo "3. Run: sudo systemctl daemon-reload"
echo "4. Run: sudo systemctl enable gmc-monitor"
echo "5. Connect your GMC-300E Plus device"
echo "6. Start the service: sudo systemctl start gmc-monitor"
echo "7. Check status: sudo systemctl status gmc-monitor"
echo ""
echo -e "${YELLOW}Note: You may need to log out and back in for group changes to take effect${NC}"