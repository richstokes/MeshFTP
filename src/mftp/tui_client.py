"""MFTP Textual TUI Client - Modern interface for file transfers."""

import argparse
import asyncio
import base64
import hashlib
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from pubsub import pub
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, Horizontal
from textual.widgets import (
    Header,
    Footer,
    Static,
    Button,
    DataTable,
    ProgressBar,
    Log,
    Label,
)
from textual.reactive import reactive

from mftp.common import MeshtasticConnection, select_device


# Timeout for chunk requests (seconds)
CHUNK_TIMEOUT = 20


class FileTransferClient:
    """Client for transferring files over Meshtastic."""

    def __init__(
        self, mesh_interface, server_id: str, download_dir: Path, debug: bool = False
    ):
        self.interface = mesh_interface
        self.server_id = server_id
        self.download_dir = download_dir
        self.debug = debug
        self.file_list = []
        self.current_transfer = None
        self.received_chunk = None
        self.chunk_event = asyncio.Event()
        self.checksum_response = None
        self.checksum_event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        
        # Callback for UI updates
        self.on_file_list_update = None
        self.on_chunk_received = None
        self.on_debug_message = None
        self.on_error_message = None
        self.on_checksum_result = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Provide the asyncio loop so we can set events from other threads safely."""
        self.loop = loop

    def request_file_list(self):
        """Request file list from server."""
        self._debug_log("📡 Requesting file list from server...")
        self.interface.sendText("!ls", destinationId=self.server_id)

    def on_receive(self, packet, interface):
        """Handle received messages."""
        try:
            # Always log that we got a packet (not just in debug mode)
            self._debug_log(f"📨 Packet received from {packet.get('fromId') or packet.get('from')}")
            
            if self.debug:
                self._debug_log(
                    f"Packet received: fromId={packet.get('fromId')}, "
                    f"from={packet.get('from')}, toId={packet.get('toId')}, "
                    f"to={packet.get('to')}"
                )
                if "decoded" in packet:
                    decoded = packet["decoded"]
                    self._debug_log(f"Decoded keys: {list(decoded.keys())}")
                    if "text" in decoded:
                        text_preview = decoded["text"][:80]
                        self._debug_log(f"Text content: {text_preview}")
                    else:
                        self._debug_log("No 'text' field in decoded")
                else:
                    self._debug_log("No 'decoded' field in packet")

            if "decoded" in packet and "text" in packet["decoded"]:
                text = packet["decoded"]["text"]
                self._debug_log(f"📝 Text message received: {text[:80]}...")

                # Get from_id - try string ID first, then fall back to numeric ID
                from_id = packet.get("fromId")
                if not from_id:
                    from_num = packet.get("from")
                    if from_num:
                        from_id = f"!{from_num:08x}"
                        self._debug_log(f"Converted numeric ID {from_num} to {from_id}")

                if from_id is None:
                    self._debug_log("⚠ Skipping packet with None fromId/from")
                    return

                # Normalize from_id
                normalized_from_id = (
                    from_id if from_id.startswith("!") else f"!{from_id}"
                )

                self._debug_log(
                    f"🔍 Checking: from={normalized_from_id}, expected={self.server_id}, match={normalized_from_id == self.server_id}"
                )

                # Only process messages from our server
                if normalized_from_id != self.server_id:
                    self._debug_log(f"❌ Filtered out (not from server)")
                    return
                
                self._debug_log(f"✅ Message from server accepted!")

                # Handle file list response (JSON)
                if text.startswith("{"):
                    self._debug_log(f"📋 Parsing JSON response: {text[:100]}...")
                    try:
                        import json
                        data = json.loads(text)
                        self._debug_log(f"✓ JSON parsed, keys: {list(data.keys())}")
                        if "files" in data:
                            self.file_list = data["files"]
                            self._debug_log(
                                f"✓ Received file list: {len(self.file_list)} files"
                            )
                            if self.on_file_list_update:
                                self._debug_log("📞 Calling on_file_list_update callback")
                                self.on_file_list_update(self.file_list)
                            else:
                                self._debug_log("⚠ No on_file_list_update callback set!")
                    except json.JSONDecodeError as e:
                        self._error_log(f"Error parsing JSON: {e}")

                # Handle chunk response
                elif text.startswith("!chunk"):
                    parts = text.split(maxsplit=3)
                    if len(parts) == 4:
                        _, filename, chunk_num, chunk_data = parts
                        self.received_chunk = {
                            "filename": filename,
                            "chunk_num": int(chunk_num),
                            "data": chunk_data,
                        }
                        self._debug_log(
                            f"✓ Chunk {int(chunk_num) + 1} received "
                            f"({len(chunk_data)} bytes)"
                        )
                        if self.loop:
                            self.loop.call_soon_threadsafe(self.chunk_event.set)
                        else:
                            self.chunk_event.set()
                        if self.on_chunk_received:
                            self.on_chunk_received(int(chunk_num) + 1)

                # Handle checksum response
                elif text.startswith("!checksum"):
                    parts = text.split(maxsplit=2)
                    if len(parts) == 3:
                        _, filename, md5_hash = parts
                        self.checksum_response = {
                            "filename": filename,
                            "md5_hash": md5_hash,
                        }
                        self._debug_log(f"✓ Checksum received: {md5_hash}")
                        if self.loop:
                            self.loop.call_soon_threadsafe(self.checksum_event.set)
                        else:
                            self.checksum_event.set()

                # Handle error response
                elif text.startswith("!error"):
                    self._error_log(f"Server error: {text}")

        except Exception as e:
            self._error_log(f"Error handling message: {e}")

    async def request_chunk_async(self, filename: str, chunk_num: int) -> bool:
        """Request a specific chunk from the server (async version).

        Returns:
            True if chunk received successfully, False on timeout.
        """
        self.received_chunk = None
        self.chunk_event.clear()

        # Send request
        request = f"!req {filename} {chunk_num}"
        self.interface.sendText(request, destinationId=self.server_id)
        self._debug_log(f"→ Requesting chunk {chunk_num + 1}...")

        # Wait for response with timeout
        try:
            await asyncio.wait_for(self.chunk_event.wait(), timeout=CHUNK_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            self._error_log(f"⏱ Timeout waiting for chunk {chunk_num + 1}")
            return False

    async def download_file_async(self, filename: str, total_chunks: int) -> bool:
        """Download a complete file from the server (async version).

        Args:
            filename: Name of the file to download.
            total_chunks: Total number of chunks for this file.

        Returns:
            True if download successful, False otherwise.
        """
        # Ensure download directory exists
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Check if file already exists
        output_path = self.download_dir / filename
        if output_path.exists():
            self._error_log(f"⚠️  File already exists: {output_path}")
            return False

        self._debug_log(f"📥 Downloading {filename} ({total_chunks} chunks)...")

        chunks_data = []

        for chunk_num in range(total_chunks):
            self._debug_log(f"Chunk {chunk_num + 1}/{total_chunks}")

            # Retry logic
            max_retries = 5
            for retry in range(max_retries):
                if await self.request_chunk_async(filename, chunk_num):
                    # Verify we got the right chunk
                    if (
                        self.received_chunk
                        and self.received_chunk["filename"] == filename
                        and self.received_chunk["chunk_num"] == chunk_num
                    ):
                        chunks_data.append(self.received_chunk["data"])
                        break
                    else:
                        self._error_log("⚠ Received wrong chunk, retrying...")
                else:
                    if retry < max_retries - 1:
                        self._error_log(f"🔄 Retry {retry + 1}/{max_retries - 1}")
                    else:
                        self._error_log(
                            f"✗ Failed to receive chunk {chunk_num + 1} "
                            f"after {max_retries} attempts"
                        )
                        return False

            await asyncio.sleep(0.5)  # Brief delay between chunks

        # Concatenate all chunks and decode
        try:
            self._debug_log("📝 Reassembling file...")
            full_encoded = "".join(chunks_data)
            file_data = base64.b64decode(full_encoded)

            # Write to file
            with open(output_path, "wb") as f:
                f.write(file_data)

            self._debug_log(f"✅ Download complete: {output_path}")
            self._debug_log(f"Size: {len(file_data)} bytes")

            # Validate checksum
            if await self.validate_checksum_async(filename, output_path):
                return True
            else:
                self._error_log("⚠ Warning: Checksum validation failed")
                return False

        except Exception as e:
            self._error_log(f"✗ Error saving file: {e}")
            return False

    async def validate_checksum_async(self, filename: str, file_path: Path) -> bool:
        """Validate downloaded file against server's checksum (async version).

        Args:
            filename: Name of the file.
            file_path: Path to the downloaded file.

        Returns:
            True if checksum matches, False otherwise.
        """
        self._debug_log("🔐 Validating checksum...")

        # Calculate MD5 of downloaded file
        md5_hash = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                md5_hash.update(chunk)
        local_hash = md5_hash.hexdigest()

        # Request checksum from server
        self.checksum_response = None
        self.checksum_event.clear()

        request = f"!check {filename}"
        self.interface.sendText(request, destinationId=self.server_id)
        self._debug_log("→ Requesting checksum from server...")

        # Wait for response with timeout
        try:
            await asyncio.wait_for(self.checksum_event.wait(), timeout=CHUNK_TIMEOUT)
        except asyncio.TimeoutError:
            self._error_log("⏱ Timeout waiting for checksum response")
            return False

        if not self.checksum_response:
            self._error_log("✗ No checksum response received")
            return False

        server_hash = self.checksum_response["md5_hash"]

        # Compare hashes
        if local_hash == server_hash:
            self._debug_log(f"✓ Checksum valid: {local_hash}")
            if self.on_checksum_result:
                self.on_checksum_result(True, local_hash)
            return True
        else:
            self._error_log("✗ Checksum mismatch!")
            self._error_log(f"  Local:  {local_hash}")
            self._error_log(f"  Server: {server_hash}")
            if self.on_checksum_result:
                self.on_checksum_result(False, local_hash, server_hash)
            return False

    def _debug_log(self, message: str):
        """Send debug message to UI."""
        if self.on_debug_message:
            self.on_debug_message(message)

    def _error_log(self, message: str):
        """Send error message to UI."""
        if self.on_error_message:
            self.on_error_message(message)


class MFTPClientApp(App):
    """Textual TUI for MFTP Client."""

    CSS = """
    Screen {
        background: $surface;
    }
    
    #title {
        height: 3;
        content-align: center middle;
        text-style: bold;
        color: $accent;
        background: $primary;
    }
    
    #status {
        height: 2;
        padding: 0 2;
        color: $warning;
        text-style: bold;
    }
    
    #main-container {
        height: 1fr;
    }
    
    #left-panel {
        width: 60%;
        height: 100%;
    }
    
    #right-panel {
        width: 40%;
        height: 100%;
        border-left: solid $primary;
    }
    
    .section-title {
        height: 2;
        padding: 0 2;
        text-style: bold;
        color: $secondary;
        background: $surface-darken-1;
    }
    
    #file-table {
        height: 1fr;
        border: solid $primary;
        margin: 0 2;
    }
    
    #button-container {
        height: auto;
        align: center middle;
        margin: 1 2;
    }
    
    Button {
        margin: 0 1;
    }
    
    #progress-container {
        height: 6;
        margin: 1 2;
        border: solid $primary;
        padding: 1;
    }
    
    #progress-bar {
        margin-top: 1;
    }
    
    #debug-log {
        height: 1fr;
        border: solid $primary;
        margin: 0 2 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "download", "Download"),
    ]

    status_text = reactive("Initializing...")
    progress_current = reactive(0)
    progress_total = reactive(100)
    progress_visible = reactive(False)

    def __init__(
        self,
        mesh_interface,
        server_id: str,
        download_dir: Path,
        debug: bool = False,
    ):
        super().__init__()
        self.client = FileTransferClient(
            mesh_interface, server_id, download_dir, debug
        )
        self.server_id = server_id
        self.download_dir = download_dir
        self.downloading = False
        
        # Set up callbacks
        self.client.on_file_list_update = self.update_file_list
        self.client.on_chunk_received = self.update_progress
        self.client.on_debug_message = self.add_debug_log
        self.client.on_error_message = self.add_error_log
        self.client.on_checksum_result = self.show_checksum_result

    def _safe_call(self, callback):
        """Safely call a function, using call_from_thread if needed."""
        if threading.current_thread() != threading.main_thread():
            self.call_from_thread(callback)
        else:
            callback()
    
    def compose(self) -> ComposeResult:
        """Compose the UI."""
        yield Header()
        yield Static(f"MFTP Client - Server: {self.server_id}", id="title")
        yield Static(self.status_text, id="status")
        
        with Horizontal(id="main-container"):
            # Left panel - file list and controls
            with Vertical(id="left-panel"):
                yield Static("📁 Available Files:", classes="section-title")
                yield DataTable(id="file-table")
                yield Container(
                    Button("Refresh List", variant="primary", id="refresh-btn"),
                    Button("Download", variant="success", id="download-btn"),
                    Button("Quit", variant="error", id="quit-btn"),
                    id="button-container",
                )
                
                # Progress section
                with Container(id="progress-container"):
                    yield Label("", id="progress-label")
                    yield ProgressBar(
                        total=self.progress_total,
                        show_eta=False,
                        id="progress-bar",
                    )
            
            # Right panel - debug log
            with Vertical(id="right-panel"):
                yield Static("🔍 Debug Log:", classes="section-title")
                yield Log(id="debug-log", highlight=True, auto_scroll=True)
        
        yield Footer()

    async def on_mount(self) -> None:
        """Called when app is mounted."""
        # Provide loop to client for thread-safe event signaling
        self.client.set_loop(asyncio.get_running_loop())

        # Set up message subscription
        def message_handler(packet, interface):
            self.client.on_receive(packet, interface)

        self._pub_handler = message_handler
        pub.subscribe(self._pub_handler, "meshtastic.receive.text")
        
        # Set up file table
        table = self.query_one("#file-table", DataTable)
        table.add_columns("#", "Filename", "Chunks", "Size")
        table.cursor_type = "row"
        
        # Hide progress initially
        progress_container = self.query_one("#progress-container")
        progress_container.display = False
        
        # Request initial file list
        await asyncio.sleep(1)
        self.add_debug_log("🔄 Requesting initial file list...")
        self.client.request_file_list()
        self.status_text = "Waiting for file list..."

    def update_file_list(self, file_list):
        """Update the file table with new file list."""
        self.add_debug_log(f"🔄 update_file_list called with {len(file_list)} files")
        def _update():
            table = self.query_one("#file-table", DataTable)
            table.clear()
            
            for i, file_info in enumerate(file_list):
                # Estimate size (chunks * 150 bytes base64 ~= 112.5 bytes original)
                estimated_size = file_info["chunks"] * 112
                size_str = f"{estimated_size}B" if estimated_size < 1024 else f"{estimated_size/1024:.1f}KB"
                
                table.add_row(
                    str(i + 1),
                    file_info["name"],
                    str(file_info["chunks"]),
                    size_str,
                )
            
            self.status_text = f"✓ {len(file_list)} file(s) available"
        
        self._safe_call(_update)
        self.add_debug_log(f"✓ File list updated: {len(file_list)} files")

    def update_progress(self, chunk_num: int):
        """Update progress bar when chunk is received."""
        if self.downloading:
            def _update():
                self.progress_current = chunk_num
                progress_bar = self.query_one("#progress-bar", ProgressBar)
                progress_bar.update(progress=chunk_num)
                
                progress_label = self.query_one("#progress-label", Label)
                progress_label.update(
                    f"Downloading: Chunk {chunk_num}/{self.progress_total}"
                )
            
            self._safe_call(_update)

    def add_debug_log(self, message: str):
        """Add message to debug log."""
        def _log():
            log = self.query_one("#debug-log", Log)
            log.write_line(message)
        
        self._safe_call(_log)

    def add_error_log(self, message: str):
        """Add error message to debug log."""
        def _log():
            log = self.query_one("#debug-log", Log)
            log.write_line(f"[bold red]{message}[/bold red]")
        
        self._safe_call(_log)

    def show_checksum_result(self, valid: bool, local_hash: str, server_hash: str = None):
        """Show checksum validation result."""
        if valid:
            self.add_debug_log(f"[bold green]✓ Checksum valid: {local_hash}[/bold green]")
        else:
            self.add_error_log(f"✗ Checksum mismatch! Local: {local_hash}, Server: {server_hash}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "refresh-btn":
            await self.action_refresh()
        elif event.button.id == "download-btn":
            await self.action_download()
        elif event.button.id == "quit-btn":
            self.action_quit()

    async def action_refresh(self) -> None:
        """Refresh the file list."""
        self.add_debug_log("🔄 Refreshing file list...")
        self.status_text = "Refreshing file list..."
        self.client.request_file_list()

    async def action_download(self) -> None:
        """Download the selected file."""
        if self.downloading:
            self.add_error_log("⚠ Download already in progress")
            return
        
        table = self.query_one("#file-table", DataTable)
        if table.cursor_row is None or table.cursor_row < 0:
            self.add_error_log("⚠ Please select a file to download")
            return
        
        if table.cursor_row >= len(self.client.file_list):
            self.add_error_log("⚠ Invalid file selection")
            return
        
        file_info = self.client.file_list[table.cursor_row]
        filename = file_info["name"]
        total_chunks = file_info["chunks"]
        
        # Show progress bar
        progress_container = self.query_one("#progress-container")
        progress_container.display = True
        
        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.update(total=total_chunks, progress=0)
        
        progress_label = self.query_one("#progress-label", Label)
        progress_label.update(f"Downloading: {filename}")
        
        self.progress_total = total_chunks
        self.progress_current = 0
        self.downloading = True
        self.status_text = f"Downloading {filename}..."
        
        # Start download
        self.add_debug_log(f"📥 Starting download: {filename}")
        success = await self.client.download_file_async(filename, total_chunks)
        
        self.downloading = False
        
        if success:
            self.status_text = f"✅ Downloaded: {filename}"
            self.add_debug_log(f"[bold green]✅ Download complete: {filename}[/bold green]")
        else:
            self.status_text = f"✗ Download failed: {filename}"
            self.add_error_log(f"✗ Download failed: {filename}")
        
        # Hide progress bar after a delay
        await asyncio.sleep(2)
        progress_container.display = False

    def action_quit(self) -> None:
        """Quit the application."""
        try:
            if hasattr(self, "_pub_handler") and self._pub_handler:
                pub.unsubscribe(self._pub_handler, "meshtastic.receive.text")
        except Exception:
            pass
        self.exit()


async def main_async():
    """Main entry point for MFTP TUI client."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="MFTP TUI Client - Meshtastic File Transfer Protocol Client"
    )
    parser.add_argument(
        "-s",
        "--server",
        type=str,
        required=True,
        help="Server node ID (e.g., !abcd1234 or abcd1234)",
    )
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        default=str(Path.home() / "Downloads" / "MFTP"),
        help="Download directory (default: ~/Downloads/MFTP)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output for message filtering",
    )
    args = parser.parse_args()

    # Normalize server ID
    server_id = args.server if args.server.startswith("!") else f"!{args.server}"

    # Setup download directory
    download_dir = Path(args.directory)

    print("MFTP TUI Client - Meshtastic File Transfer Protocol")
    print("=" * 50)
    print(f"Server: {server_id}")
    print(f"Download directory: {download_dir}")
    print()

    # Select device using TUI (async version)
    device_info = await select_device()

    if not device_info:
        print("No device selected. Exiting.")
        sys.exit(0)

    print(f"\nConnecting to {device_info}...")

    # Connect to the selected device
    mesh = MeshtasticConnection()
    if not mesh.connect(device_info):
        print("Failed to connect to device.")
        sys.exit(1)

    print("Connected to device successfully!")
    print(f"\nStarting TUI client...")
    print()

    try:
        # Create and run the TUI app
        app = MFTPClientApp(mesh.interface, server_id, download_dir, args.debug)
        await app.run_async()
    finally:
        # Clean up
        mesh.disconnect()


def main():
    """Synchronous wrapper for main_async."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
