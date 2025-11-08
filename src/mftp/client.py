"""MFTP Client - File transfer client for Meshtastic networks."""

import argparse
import base64
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path
from threading import Event

from pubsub import pub

from mftp.common import MeshtasticConnection, select_device_sync

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
        self.chunk_event = Event()
        self.checksum_response = None
        self.checksum_event = Event()

    def request_file_list(self):
        """Request file list from server."""
        print("📡 Requesting file list from server...")
        self.interface.sendText("!ls", destinationId=self.server_id)

    def on_receive(self, packet, interface):
        """Handle received messages."""
        try:
            if self.debug:
                print(
                    f"  [DEBUG] Packet received: {packet.get('fromId')} -> {packet.get('toId')}"
                )
                if "decoded" in packet and "text" in packet["decoded"]:
                    print(f"  [DEBUG] Text content: {packet['decoded']['text'][:80]}")

            if "decoded" in packet and "text" in packet["decoded"]:
                text = packet["decoded"]["text"]

                # Get from_id - try string ID first, then fall back to numeric ID
                from_id = packet.get("fromId")
                if not from_id:
                    # Fall back to numeric ID and convert to hex string
                    from_num = packet.get("from")
                    if from_num:
                        from_id = f"!{from_num:08x}"

                # Skip if from_id is None (shouldn't happen but can occur)
                if from_id is None:
                    if self.debug:
                        print(f"  [DEBUG] Skipping packet with None fromId/from")
                        print(f"  [DEBUG] Packet keys: {packet.keys()}")
                    return

                # Normalize from_id - add ! if not present
                normalized_from_id = (
                    from_id if from_id.startswith("!") else f"!{from_id}"
                )

                if self.debug:
                    print(
                        f"  [DEBUG] Received from {normalized_from_id} (expecting {self.server_id}): {text[:50]}..."
                    )

                # Only process messages from our server
                if normalized_from_id != self.server_id:
                    if self.debug:
                        print(f"  [DEBUG] Filtered out (not from server)")
                    return

                # Handle file list response (JSON)
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                        if "files" in data:
                            self.file_list = data["files"]
                            print(
                                f"\n✓ Received file list: {len(self.file_list)} files"
                            )
                    except json.JSONDecodeError as e:
                        print(f"\n✗ Error parsing JSON: {e}")

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
                        self.chunk_event.set()

                # Handle checksum response
                elif text.startswith("!checksum"):
                    parts = text.split(maxsplit=2)
                    if len(parts) == 3:
                        _, filename, md5_hash = parts
                        self.checksum_response = {
                            "filename": filename,
                            "md5_hash": md5_hash,
                        }
                        self.checksum_event.set()

                # Handle error response
                elif text.startswith("!error"):
                    print(f"\n✗ Server error: {text}")

        except Exception as e:
            print(f"❌ Error handling message: {e}")
            traceback.print_exc()

    def request_chunk(self, filename: str, chunk_num: int) -> bool:
        """Request a specific chunk from the server.

        Returns:
            True if chunk received successfully, False on timeout.
        """
        self.received_chunk = None
        self.chunk_event.clear()

        # Send request
        request = f"!req {filename} {chunk_num}"
        self.interface.sendText(request, destinationId=self.server_id)
        print(f"  → Requesting chunk {chunk_num + 1}...")

        # Wait for response with timeout
        if self.chunk_event.wait(timeout=CHUNK_TIMEOUT):
            return True
        else:
            print(f"  ⏱ Timeout waiting for chunk {chunk_num + 1}")
            return False

    def download_file(self, filename: str, total_chunks: int) -> bool:
        """Download a complete file from the server.

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
            print(f"\n⚠️  File already exists: {output_path}")
            response = input("Overwrite? (y/n): ").strip().lower()
            if response != "y":
                print("  ✗ Download cancelled")
                return False
            print()

        print(f"📥 Downloading {filename} ({total_chunks} chunks)...")

        chunks_data = []

        for chunk_num in range(total_chunks):
            print(f"  Chunk {chunk_num + 1}/{total_chunks}")

            # Retry logic - keep requesting until we get the chunk
            max_retries = 5
            for retry in range(max_retries):
                if self.request_chunk(filename, chunk_num):
                    # Verify we got the right chunk
                    if (
                        self.received_chunk
                        and self.received_chunk["filename"] == filename
                        and self.received_chunk["chunk_num"] == chunk_num
                    ):
                        chunks_data.append(self.received_chunk["data"])
                        break
                    else:
                        print(f"  ⚠ Received wrong chunk, retrying...")
                else:
                    if retry < max_retries - 1:
                        print(f"  🔄 Retry {retry + 1}/{max_retries - 1}")
                    else:
                        print(
                            f"  ✗ Failed to receive chunk {chunk_num + 1} after {max_retries} attempts"
                        )
                        return False

            time.sleep(0.5)  # Brief delay between chunks

        # Concatenate all chunks and decode
        try:
            print(f"\n  📝 Reassembling file...")
            full_encoded = "".join(chunks_data)
            file_data = base64.b64decode(full_encoded)

            # Write to file (output_path already defined earlier)
            with open(output_path, "wb") as f:
                f.write(file_data)

            print(f"  ✅ Download complete: {output_path}")
            print(f"  Size: {len(file_data)} bytes")

            # Validate checksum
            if self.validate_checksum(filename, output_path):
                return True
            else:
                print(f"  ⚠ Warning: Checksum validation failed")
                return False

        except Exception as e:
            print(f"  ✗ Error saving file: {e}")
            traceback.print_exc()
            return False

    def validate_checksum(self, filename: str, file_path: Path) -> bool:
        """Validate downloaded file against server's checksum.

        Args:
            filename: Name of the file.
            file_path: Path to the downloaded file.

        Returns:
            True if checksum matches, False otherwise.
        """
        print(f"\n  🔐 Validating checksum...")

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
        print(f"  → Requesting checksum from server...")

        # Wait for response with timeout
        if not self.checksum_event.wait(timeout=CHUNK_TIMEOUT):
            print(f"  ⏱ Timeout waiting for checksum response")
            return False

        if not self.checksum_response:
            print(f"  ✗ No checksum response received")
            return False

        server_hash = self.checksum_response["md5_hash"]

        # Compare hashes
        if local_hash == server_hash:
            print(f"  ✓ Checksum valid: {local_hash}")
            return True
        else:
            print(f"  ✗ Checksum mismatch!")
            print(f"    Local:  {local_hash}")
            print(f"    Server: {server_hash}")
            return False


def main():
    """Main entry point for MFTP client."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="MFTP Client - Meshtastic File Transfer Protocol Client"
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

    # Normalize server ID - add ! if not present
    server_id = args.server if args.server.startswith("!") else f"!{args.server}"

    # Setup download directory
    download_dir = Path(args.directory)

    print("MFTP Client - Meshtastic File Transfer Protocol")
    print("=" * 50)
    print(f"Server: {server_id}")
    print(f"Download directory: {download_dir}")
    print()

    # Select device using TUI
    device_info = select_device_sync()

    if not device_info:
        print("No device selected. Exiting.")
        sys.exit(0)

    print(f"\nConnecting to {device_info}...")

    # Connect to the selected device
    with MeshtasticConnection() as mesh:
        if not mesh.connect(device_info):
            print("Failed to connect to device.")
            sys.exit(1)

        print("Connected to device successfully!")
        print(f"\nMFTP Client ready. Using server: {server_id}")
        print(f"Downloads will be saved to: {download_dir}")
        if args.debug:
            print("Debug mode: enabled")
        print()

        # Create file transfer client
        client = FileTransferClient(mesh.interface, server_id, download_dir, args.debug)

        # Subscribe to messages
        def message_handler(packet, interface):
            client.on_receive(packet, interface)

        pub.subscribe(message_handler, "meshtastic.receive")

        # Give subscription time to register
        time.sleep(2)

        # Request initial file list with retry
        print("Requesting initial file list...")
        client.request_file_list()

        # Wait for response with feedback
        for i in range(6):  # Wait up to 6 seconds
            time.sleep(1)
            if client.file_list:
                break
            if i == 2:
                print("  Still waiting for response...")
            elif i == 5:
                print("  ⚠️  No response yet. Try 'ls' command to retry.")

        # Simple interactive loop
        print("\nCommands:")
        print("  ls - Refresh file list")
        print("  get <number> - Download file by number")
        print("  quit - Exit")
        print()

        try:
            while True:
                # Display current file list
                if client.file_list:
                    print("\nAvailable files:")
                    for i, file_info in enumerate(client.file_list):
                        print(
                            f"  {i + 1}. {file_info['name']} "
                            f"({file_info['chunks']} chunks)"
                        )
                    print("Use get <number> to download a file.")
                else:
                    print("\nNo files available. Use 'ls' to refresh.")

                command = input("\n> ").strip().lower()

                if command == "quit":
                    break
                elif command == "ls":
                    client.request_file_list()
                    time.sleep(2)
                elif command.startswith("get "):
                    try:
                        file_num = int(command.split()[1]) - 1
                        if 0 <= file_num < len(client.file_list):
                            file_info = client.file_list[file_num]
                            client.download_file(file_info["name"], file_info["chunks"])
                        else:
                            print("Invalid file number")
                    except (ValueError, IndexError):
                        print("Usage: get <number>")
                else:
                    print("Unknown command")

        except KeyboardInterrupt:
            print("\n\nShutting down client...")

        pub.unsubscribe(message_handler, "meshtastic.receive")


if __name__ == "__main__":
    main()
