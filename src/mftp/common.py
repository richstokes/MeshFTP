"""Common utilities for MFTP including Meshtastic connection handling and device selection TUI."""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import meshtastic.serial_interface
import meshtastic.ble_interface
from bleak import BleakScanner
from loguru import logger
from serial.tools import list_ports
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import Header, Footer, Static, Button, ListView, ListItem, Label
from textual.binding import Binding

# Configure loguru format to show only seconds (not milliseconds)
logger.remove()  # Remove default handler
logger.add(
    lambda msg: print(msg, end=""),
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    colorize=True,
)


class ConnectionType(Enum):
    """Type of connection to Meshtastic device."""

    SERIAL = "serial"
    BLE = "ble"


@dataclass
class DeviceInfo:
    """Information about an available Meshtastic device."""

    name: str
    connection_type: ConnectionType
    address: str  # Port name for serial, MAC address for BLE

    def __str__(self):
        return f"{self.name} ({self.connection_type.value}: {self.address})"


class MeshtasticConnection:
    """Handler for Meshtastic device connections supporting both Serial and BLE."""

    def __init__(self):
        self.interface = None
        self.device_info: Optional[DeviceInfo] = None

    @staticmethod
    async def discover_devices() -> list[DeviceInfo]:
        """Discover available Meshtastic devices via Serial and BLE.

        Returns:
            List of DeviceInfo objects for available devices.
        """
        devices = []

        # Discover serial devices
        serial_ports = list_ports.comports()
        for port in serial_ports:
            # Filter for likely Meshtastic devices
            # Common USB serial chips used by Meshtastic devices
            if any(
                vid in str(port.vid) for vid in ["10c4", "1a86", "0403", "239a"]
            ) or any(
                name in port.description.lower()
                for name in ["cp210", "ch340", "usb serial", "meshtastic"]
            ):
                devices.append(
                    DeviceInfo(
                        name=port.description or port.device,
                        connection_type=ConnectionType.SERIAL,
                        address=port.device,
                    )
                )

        # Discover BLE devices
        try:
            ble_devices = await BleakScanner.discover(timeout=5.0)
            # Sort by signal strength (RSSI) - strongest first (highest/least negative value)
            sorted_ble = sorted(ble_devices, key=lambda d: d.rssi if d.rssi else -999, reverse=True)
            for device in sorted_ble:
                # Show all BLE devices with names (more permissive filtering)
                # Some Meshtastic devices may not have "mesh" in their name
                if device.name:
                    devices.append(
                        DeviceInfo(
                            name=device.name,
                            connection_type=ConnectionType.BLE,
                            address=device.address,
                        )
                    )
        except Exception as e:
            logger.error(f"BLE discovery error: {e}")

        return devices

    def connect(self, device_info: DeviceInfo) -> bool:
        """Connect to a Meshtastic device.

        Args:
            device_info: Information about the device to connect to.

        Returns:
            True if connection successful, False otherwise.
        """
        try:
            self.device_info = device_info

            if device_info.connection_type == ConnectionType.SERIAL:
                self.interface = meshtastic.serial_interface.SerialInterface(
                    devPath=device_info.address
                )
            elif device_info.connection_type == ConnectionType.BLE:
                self.interface = meshtastic.ble_interface.BLEInterface(
                    address=device_info.address
                )
            else:
                raise ValueError(
                    f"Unknown connection type: {device_info.connection_type}"
                )

            return True

        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from the Meshtastic device."""
        if self.interface:
            try:
                self.interface.close()
            except Exception as e:
                logger.error(f"Disconnect error: {e}")
            finally:
                self.interface = None
                self.device_info = None

    def send_message(self, text: str, destination: str = "^all") -> bool:
        """Send a text message via Meshtastic.

        Args:
            text: Message text to send.
            destination: Destination node ID (default: broadcast to all).

        Returns:
            True if send successful, False otherwise.
        """
        if not self.interface:
            logger.warning("Not connected to a device")
            return False

        try:
            self.interface.sendText(text, destinationId=destination)
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures disconnect."""
        self.disconnect()


class DeviceSelectionApp(App):
    """Textual TUI for selecting a Meshtastic device."""

    CSS = """
    Screen {
        background: $surface;
    }
    
    #title {
        height: 3;
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }
    
    #status {
        height: 3;
        content-align: center middle;
        color: $warning;
        text-style: bold;
    }
    
    .section-title {
        height: 2;
        padding: 0 2;
        text-style: bold;
        color: $secondary;
    }
    
    ListView {
        height: auto;
        max-height: 10;
        min-height: 3;
        border: solid $primary;
        margin: 0 2 1 2;
    }
    
    ListItem {
        padding: 1 2;
    }
    
    #button-container {
        height: auto;
        align: center middle;
        margin: 1 2;
    }
    
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select", "Select"),
    ]

    def __init__(self):
        super().__init__()
        self.serial_devices: list[DeviceInfo] = []
        self.ble_devices: list[DeviceInfo] = []
        self.selected_device: Optional[DeviceInfo] = None
        self.serial_list = ListView(id="serial-list")
        self.ble_list = ListView(id="ble-list")
        self.current_focus = "serial"

    def compose(self) -> ComposeResult:
        """Compose the UI."""
        yield Header()
        yield Static("Select Meshtastic Device", id="title")
        yield Static("Initializing...", id="status")
        yield Static("📟 Serial Devices:", classes="section-title")
        yield self.serial_list
        yield Static("📡 Bluetooth (BLE) Devices:", classes="section-title")
        yield self.ble_list
        yield Container(
            Button("Select", variant="primary", id="select-btn"),
            Button("Refresh", variant="default", id="refresh-btn"),
            Button("Quit", variant="error", id="quit-btn"),
            id="button-container",
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Called when app is mounted."""
        # Give UI time to render before scanning
        await asyncio.sleep(0.1)
        await self.refresh_devices()

    async def refresh_devices(self) -> None:
        """Refresh the list of available devices."""
        status = self.query_one("#status", Static)

        # Clear existing lists
        self.serial_list.clear()
        self.ble_list.clear()
        self.serial_devices = []
        self.ble_devices = []

        # Scan for serial devices
        status.update("🔍 Scanning for serial devices...")
        self.refresh()  # Force UI update
        await asyncio.sleep(0.05)  # Give UI time to update

        serial_ports = list_ports.comports()
        serial_count = 0
        for port in serial_ports:
            # Filter for likely Meshtastic devices
            if any(
                vid in str(port.vid) for vid in ["10c4", "1a86", "0403", "239a"]
            ) or any(
                name in port.description.lower()
                for name in ["cp210", "ch340", "usb serial", "meshtastic"]
            ):
                device = DeviceInfo(
                    name=port.description or port.device,
                    connection_type=ConnectionType.SERIAL,
                    address=port.device,
                )
                self.serial_devices.append(device)
                self.serial_list.append(ListItem(Label(str(device))))
                serial_count += 1

        if serial_count == 0:
            self.serial_list.append(ListItem(Label("(No serial devices found)")))

        status.update(f"✓ Found {serial_count} serial device(s), scanning BLE...")
        self.refresh()
        await asyncio.sleep(0.05)

        # Scan for BLE devices
        status.update("📡 Scanning for BLE devices (this may take a few seconds)...")
        self.refresh()
        await asyncio.sleep(0.05)

        try:
            ble_devices = await BleakScanner.discover(timeout=5.0)
            # Sort by signal strength (RSSI) - strongest first (highest/least negative value)
            sorted_ble = sorted(ble_devices, key=lambda d: d.rssi if d.rssi else -999, reverse=True)
            ble_count = 0
            for ble_device in sorted_ble:
                if ble_device.name:
                    device = DeviceInfo(
                        name=ble_device.name,
                        connection_type=ConnectionType.BLE,
                        address=ble_device.address,
                    )
                    self.ble_devices.append(device)
                    self.ble_list.append(ListItem(Label(str(device))))
                    ble_count += 1

                    # Update status every few devices
                    if ble_count % 5 == 0:
                        status.update(f"📡 Scanning BLE... found {ble_count} device(s)")
                        self.refresh()
                        await asyncio.sleep(0.05)

            if ble_count == 0:
                self.ble_list.append(ListItem(Label("(No BLE devices found)")))

        except Exception as e:
            status.update(f"⚠ BLE scan error: {e}")
            self.ble_list.append(ListItem(Label(f"(Error: {e})")))
            self.refresh()
            await asyncio.sleep(0.05)

        # Final status
        total = serial_count + len(self.ble_devices)
        if total > 0:
            status.update(
                f"✓ Scan complete: {serial_count} serial, {len(self.ble_devices)} BLE"
            )
        else:
            status.update("⚠ No devices found. Try refreshing or check connections.")

        self.refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "select-btn":
            await self.action_select()
        elif event.button.id == "refresh-btn":
            await self.action_refresh()
        elif event.button.id == "quit-btn":
            self.action_quit()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle list item selection."""
        await self.action_select()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Track which list is currently focused."""
        if event.list_view.id == "serial-list":
            self.current_focus = "serial"
        elif event.list_view.id == "ble-list":
            self.current_focus = "ble"

    async def action_select(self) -> None:
        """Select the highlighted device and exit."""
        status = self.query_one("#status", Static)

        # Determine which list is active and get the selection
        if self.current_focus == "serial":
            active_list = self.serial_list
            devices = self.serial_devices
        else:
            active_list = self.ble_list
            devices = self.ble_devices

        if active_list.index is not None and active_list.index < len(devices):
            self.selected_device = devices[active_list.index]
            self.exit(self.selected_device)
        else:
            status.update("⚠ Please select a device from the list")

    async def action_refresh(self) -> None:
        """Refresh the device list."""
        await self.refresh_devices()

    def action_quit(self) -> None:
        """Quit the application."""
        self.exit(None)


async def select_device() -> Optional[DeviceInfo]:
    """Run the device selection TUI and return the selected device.

    Returns:
        Selected DeviceInfo or None if cancelled.
    """
    app = DeviceSelectionApp()
    result = await app.run_async()
    return result


def select_device_sync() -> Optional[DeviceInfo]:
    """Synchronous wrapper for select_device().

    Returns:
        Selected DeviceInfo or None if cancelled.
    """
    return asyncio.run(select_device())
